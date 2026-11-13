"""Нейросетевые декорации на сервере с бесплатным лимитом и CPU-резервом."""
import base64
import hashlib
import io
import json
import os
import secrets
from datetime import datetime, timezone

import httpx
from PIL import Image, ImageOps

from studio.agents import Unavailable
from studio.config import flag
from studio.media import safe_file, sha256


def background_key(scene):
    return hashlib.sha256(json.dumps({"setting": scene.setting, "description": scene.description, "palette": scene.background, "model": "flux-1-schnell-v1"}, sort_keys=True).encode()).hexdigest()


async def prepare_backgrounds(store, script):
    if os.getenv("STUDIO_VISUAL_PROVIDER", "procedural") != "cloudflare":
        return {}
    strict = flag("STUDIO_VISUAL_REQUIRE_CLOUD")
    if not (flag("STUDIO_CLOUD_ENABLED") and flag("CF_FREE_ACCOUNT_CONFIRMED") and os.getenv("CF_ACCOUNT_ID") and os.getenv("CF_API_TOKEN")):
        if strict:
            raise Unavailable("Для нейродекораций нужен явно включённый бесплатный Cloudflare")
        return {}
    assets = store.root/"assets"
    assets.mkdir(exist_ok=True)
    results = {}
    async with httpx.AsyncClient(timeout=90, follow_redirects=False) as client:
        for index, scene in enumerate(script.scenes):
            key = background_key(scene)
            cached = store.get("image_asset", key)
            if cached:
                path = safe_file(assets, cached["file"])
                if path.is_file() and sha256(path) == cached["sha256"]:
                    results[index] = path
                    continue
            day = datetime.now(timezone.utc).date().isoformat()
            if not store.quota("cloudflare_images", day, int(os.getenv("CF_DAILY_IMAGES", "2"))):
                if strict:
                    raise Unavailable("Бесплатный локальный лимит изображений исчерпан; задача ждёт")
                continue
            prompt = ("Vertical 2D animated film background, layered depth, clean readable shapes, no people, no text, "
                      "no logos, no graphic injuries. Leave open space in the middle for a character. Setting: " + scene.setting + ". " + scene.description)[:2048]
            try:
                response = await client.post(f"https://api.cloudflare.com/client/v4/accounts/{os.environ['CF_ACCOUNT_ID']}/ai/run/@cf/black-forest-labs/flux-1-schnell",
                                             headers={"Authorization": "Bearer "+os.environ["CF_API_TOKEN"]}, json={"prompt": prompt, "steps": 4})
                response.raise_for_status()
                if len(response.content) > 16_000_000:
                    raise ValueError("Изображение превышает лимит")
                raw = base64.b64decode(response.json()["result"]["image"], validate=True)
                with Image.open(io.BytesIO(raw)) as image:
                    if not 128 <= image.width <= 4096 or not 128 <= image.height <= 4096:
                        raise ValueError("Недопустимое разрешение изображения")
                    image.load()
                    fitted = ImageOps.fit(image.convert("RGB"), (1080, 1920))
                path = assets/(key+"-"+secrets.token_hex(4)+".png")
                fitted.save(path)
                store.put("image_asset", key, {"file": path.name, "sha256": sha256(path), "provider": "cloudflare", "model": "flux-1-schnell"})
                results[index] = path
            except (httpx.HTTPError, ValueError, OSError, KeyError, TypeError):
                store.event("image.provider_unavailable", {"scene": index, "fallback": not strict})
                if strict:
                    raise Unavailable("Нейрогенерация временно недоступна; платный резерв не используется") from None
    return results
