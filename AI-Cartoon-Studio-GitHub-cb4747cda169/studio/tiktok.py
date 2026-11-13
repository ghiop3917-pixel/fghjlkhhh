"""Официальная доставка в черновики TikTok. Публичное размещение завершает автор."""
import json
import math
import os
import secrets
import time
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field, StrictBool

from studio.config import flag
from studio.media import safe_file, sha256


class TikTokConsent(BaseModel):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    approve_exact_upload: StrictBool
    understand_creator_must_complete_post: StrictBool


async def access_token(store, client):
    location = store.get("credential_location", "tiktok")
    cached = {}
    if location:
        file = safe_file(store.root/"secrets", location["file"])
        cached = json.loads(file.read_text(encoding="utf-8"))
        if cached.get("expires_at", 0) > time.time()+90:
            return cached["access_token"]
    refresh = cached.get("refresh_token") or os.getenv("TIKTOK_REFRESH_TOKEN")
    key, secret = os.getenv("TIKTOK_CLIENT_KEY"), os.getenv("TIKTOK_CLIENT_SECRET")
    if refresh and key and secret:
        response = await client.post("https://open.tiktokapis.com/v2/oauth/token/", data={"client_key": key, "client_secret": secret, "grant_type": "refresh_token", "refresh_token": refresh})
        response.raise_for_status()
        data = response.json()
        if not data.get("access_token"):
            raise ValueError("Не удалось обновить OAuth TikTok")
        folder = store.root/"secrets"
        folder.mkdir(exist_ok=True)
        file = folder/("tiktok-"+secrets.token_hex(8)+".json")
        fd = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump({"access_token": data["access_token"], "refresh_token": data.get("refresh_token", refresh), "expires_at": time.time()+int(data.get("expires_in", 0))}, output)
        store.put("credential_location", "tiktok", {"file": file.name})
        return data["access_token"]
    token = os.getenv("TIKTOK_ACCESS_TOKEN")
    if not token:
        raise ValueError("Для TikTok нужны OAuth и разрешение video.upload")
    return token


async def upload_draft(store, episode_id, consent: TikTokConsent):
    if not flag("TIKTOK_UPLOAD_ENABLED") or not flag("TIKTOK_AUTHORIZED_APP"):
        raise ValueError("Доставка в TikTok выключена; необходимо разрешённое платформой приложение и OAuth")
    if consent.approve_exact_upload is not True or consent.understand_creator_must_complete_post is not True:
        raise ValueError("Подтвердите отправку точного MP4 и завершение публикации внутри TikTok")
    episode = store.get("episode", episode_id)
    if not episode or episode.get("status") != "reviewed":
        raise ValueError("Сначала нужны итоговые проверки владельца")
    file = safe_file(store.root/"results", episode["result"])
    if consent.sha256 != episode["qc"]["sha256"] or sha256(file) != consent.sha256:
        raise ValueError("Согласован другой MP4")
    if store.get("tiktok_publication", episode_id):
        raise ValueError("Доставка уже начиналась; повтор заблокирован до сверки")
    size = file.stat().st_size
    count = max(1, math.ceil(size/(64*1024*1024)))
    chunk_size = math.ceil(size/count)
    store.put("tiktok_publication", episode_id, {"status": "reserved", "sha256": consent.sha256}, expected=0)
    async with httpx.AsyncClient(timeout=180, follow_redirects=False) as client:
        try:
            token = await access_token(store, client)
            response = await client.post("https://open.tiktokapis.com/v2/post/publish/inbox/video/init/", headers={"Authorization": "Bearer "+token}, json={"source_info": {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk_size, "total_chunk_count": count}})
            response.raise_for_status()
            result = response.json()
            if result.get("error", {}).get("code") != "ok":
                raise ValueError("TikTok не разрешил доставку")
            data = result["data"]
            target = urlparse(data["upload_url"])
            if target.scheme != "https" or not target.hostname or not target.hostname.endswith(".tiktokapis.com") or target.username or target.password or target.port not in (None, 443):
                raise ValueError("Неожиданный адрес загрузки TikTok")
            record = {"status": "uploading", "sha256": consent.sha256, "publish_id": data["publish_id"]}
            store.put("tiktok_publication", episode_id, record)
            with file.open("rb") as source:
                offset = 0
                for _ in range(count):
                    chunk = source.read(chunk_size)
                    # Токен владельца не отправляется на upload-хост; подписанный URL получен от TikTok.
                    sent = await client.put(data["upload_url"], content=chunk, headers={"Content-Type": "video/mp4", "Content-Length": str(len(chunk)), "Content-Range": f"bytes {offset}-{offset+len(chunk)-1}/{size}"})
                    sent.raise_for_status()
                    offset += len(chunk)
            record.update(status="awaiting_creator_post", note="Откройте входящие уведомления TikTok, завершите редактирование и публикацию. Доставка не считается публичной публикацией.")
            store.put("tiktok_publication", episode_id, record)
            return record
        except Exception:
            record = store.get("tiktok_publication", episode_id, {}) | {"status": "needs_reconciliation"}
            store.put("tiktok_publication", episode_id, record)
            raise ValueError("Результат TikTok требует сверки; MP4 сохранён, автоматическая повторная отправка запрещена") from None


async def readback(store, episode_id):
    record = store.get("tiktok_publication", episode_id, {})
    if not record.get("publish_id"):
        raise ValueError("Нет TikTok publish_id для сверки")
    async with httpx.AsyncClient(timeout=40, follow_redirects=False) as client:
        token = await access_token(store, client)
        response = await client.post("https://open.tiktokapis.com/v2/post/publish/status/fetch/", headers={"Authorization": "Bearer "+token}, json={"publish_id": record["publish_id"]})
        response.raise_for_status()
        data = response.json()
    if data.get("error", {}).get("code") != "ok":
        raise ValueError("TikTok не подтвердил статус доставки")
    record.update(remote_status=data.get("data", {}), checked_at=time.time())
    store.put("tiktok_publication", episode_id, record)
    return record
