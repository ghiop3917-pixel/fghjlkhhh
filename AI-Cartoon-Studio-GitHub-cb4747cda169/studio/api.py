import asyncio
import hashlib
import hmac
import io
import json
import secrets
import time
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qsl

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from studio.agents import ROLES, Unavailable
from studio.analytics import Comments, Metrics, save_metrics, summarize_comments
from studio.character_art import character_frame
from studio.config import Settings
from studio.conversation import apply_proposal, converse
from studio.db import Store
from studio.emergencies import (
    NewGuidance,
    SafetyReview,
    briefing,
    catalog,
    confirm_review,
    get_topics,
    request_draft,
    request_ideas,
    save_guidance,
    series_for,
)
from studio.media import safe_file
from studio.platform_analytics import youtube_sync
from studio.production import approve_script, approve_topic, archive_reviewed
from studio.publication_plans import PublicationPlan, cancel_plan, plan_publication
from studio.publishing import cleanup_plan, youtube_publish
from studio.readiness import readiness
from studio.retention import CleanupConsent, approve_cleanup
from studio.schemas import Character, Edit, Lease, Scene, Script, WorkerClaim
from studio.story_ideas import request_story_ideas


def telegram_owner(init_data, bot_token, owner_id):
    pairs = parse_qsl(init_data, keep_blank_values=True)
    if len({k for k, _ in pairs}) != len(pairs):
        raise ValueError("Повторяющиеся поля авторизации")
    data = dict(pairs)
    received = data.pop("hash", "")
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, "\n".join(f"{k}={v}" for k, v in sorted(data.items())).encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(received, expected):
        raise ValueError("Неверная подпись Telegram")
    age = time.time() - int(data.get("auth_date", "0"))
    if not 0 <= age <= 900 or str(json.loads(data.get("user", "{} ")).get("id")) != str(owner_id):
        raise ValueError("Доступ разрешён только владельцу; обновите Mini App")


class Talk(BaseModel):
    role: str = "producer"
    text: str = Field(min_length=1, max_length=6000)
    series: str = "main"


class Review(BaseModel):
    checks: dict[str, bool]


class Publication(BaseModel):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    title: str = Field(min_length=1, max_length=100)
    description: str = Field(max_length=5000)
    confirm_private_upload: bool


class Schedule(BaseModel):
    daily_hour_utc: int = Field(ge=0, le=23)


def create_app(settings: Settings, store: Store | None = None):
    settings.validate_auth()
    store = store or Store(settings.data)
    app = FastAPI(title="AI Cartoon Studio", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store

    async def owner(request: Request):
        bearer = request.headers.get("authorization", "").removeprefix("Bearer ")
        if secrets.compare_digest(bearer, settings.api_token):
            return
        init = request.headers.get("x-telegram-init-data", "")
        if settings.bot_token and settings.owner_id and init:
            try:
                telegram_owner(init, settings.bot_token, settings.owner_id)
                return
            except (ValueError, TypeError, json.JSONDecodeError):
                pass
        raise HTTPException(401, "Требуется авторизация владельца")

    async def worker(authorization: str = Header(default="")):
        if not secrets.compare_digest(authorization, "Bearer " + settings.worker_token):
            raise HTTPException(401, "Неверный токен Worker")

    def active(id, lease):
        task = store.task(id)
        if not task or task["kind"] != "render" or task["status"] != "running" or task["lease_token"] != lease or task["lease_until"] <= time.time():
            raise HTTPException(409, "Аренда задачи недействительна")
        return task, json.loads(task["payload"])

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=400, content={"detail": str(exc)[:500]})

    @app.get("/health")
    async def health():
        return {"status": "ok", "service": "AI Cartoon Studio"}

    @app.get("/api/dashboard", dependencies=[Depends(owner)])
    async def dashboard():
        return {"tasks": store.tasks(), "episodes": store.objects("episode"), "characters": store.objects("character"),
                "trends": store.objects("trend"), "series": store.objects("series"), "analytics": store.objects("analytics"),
                "agents": ROLES, "proposals": [{"id": p["id"], "status": p["body"]["status"]} for p in store.objects("proposal")[:20]],
                "mode": store.get("settings", "mode", {"mode": "AUTO"}), "worker": store.get("worker", "latest", {})}

    @app.get("/api/readiness", dependencies=[Depends(owner)])
    async def configuration_readiness():
        return readiness(settings, "server")

    @app.post("/api/objects", dependencies=[Depends(owner)])
    async def edit(data: Edit):
        if data.category == "character":
            Character.model_validate(data.body)
        revision = store.put(data.category, data.id, data.body, data.expected_revision)
        store.event("owner.edit", {"category": data.category, "id": data.id, "revision": revision})
        return {"revision": revision, "note": "Уже собранные пакеты неизменяемы; для правки серии создайте новую редакцию сценария"}

    @app.get("/api/characters/{id}/preview", dependencies=[Depends(owner)])
    async def character_preview(id: str, emotion: Literal["neutral", "angry", "happy"] = "neutral"):
        body = store.get("character", id)
        if not body:
            raise HTTPException(404, "Персонаж не найден")
        passport = Character.model_validate(body)
        # PNG возвращается из памяти: предпросмотр не заполняет диск временными файлами.
        def draw():
            with io.BytesIO() as output:
                character_frame(passport, Scene(character_id=id, text="Предпросмотр", emotion=emotion), output)
                return output.getvalue()
        content = await asyncio.to_thread(draw)
        return Response(content, media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.post("/api/objects/{category}/{id}/restore/{revision}", dependencies=[Depends(owner)])
    async def restore(category: str, id: str, revision: int):
        if category not in ("character", "story", "preferences", "rules", "series"):
            raise ValueError("Откат этой категории не разрешён")
        return {"revision": store.restore(category, id, revision)}

    @app.post("/api/chat", dependencies=[Depends(owner)])
    async def chat(data: Talk):
        store.event("chat.user", {"role": data.role, "text": data.text})
        try:
            response = await converse(store, data.text, data.role, data.series)
        except Unavailable as exc:
            raise HTTPException(503, str(exc)) from None
        store.event("chat.assistant", {"role": data.role, "text": response["answer"]})
        return response

    @app.get("/api/proposals/{id}", dependencies=[Depends(owner)])
    async def proposal(id: str):
        data = store.get("proposal", id)
        if not data:
            raise HTTPException(404, "Предложение не найдено")
        return data

    @app.post("/api/proposals/{id}/apply", dependencies=[Depends(owner)])
    async def proposal_apply(id: str):
        return {"results": apply_proposal(store, id)}

    @app.get("/api/history", dependencies=[Depends(owner)])
    async def history():
        with store.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM events ORDER BY seq DESC LIMIT 100")]

    @app.post("/api/trends/{id}/approve", dependencies=[Depends(owner)])
    async def topic(id: str):
        return {"task": approve_topic(store, id)}

    @app.post("/api/trends/{id}/reject", dependencies=[Depends(owner)])
    async def reject(id: str):
        body = store.get("trend", id)
        if not body or body["status"] != "proposed":
            raise ValueError("Тема уже обработана или не найдена")
        body["status"] = "rejected"
        store.put("trend", id, body)
        return {"status": "rejected"}

    @app.post("/api/trends/refresh", dependencies=[Depends(owner)])
    async def trends_refresh():
        return {"task": store.enqueue("trends", {}, "manual-trends:"+secrets.token_hex(8))}

    @app.post("/api/episodes/import", dependencies=[Depends(owner)])
    async def import_script(script: Script):
        id = secrets.token_hex(12)
        store.put("episode", id, {"script": script.model_dump(), "series": series_for(script), "status": "awaiting_script_approval", "editor_review": {"pass": None, "source": "owner_import"}})
        return {"episode": id}

    @app.get("/api/emergencies", dependencies=[Depends(owner)])
    async def emergency_catalog():
        return catalog(store)

    @app.post("/api/emergencies/ideas", dependencies=[Depends(owner)])
    async def emergency_ideas():
        return {"task": request_ideas(store)}

    @app.post("/api/stories/ideas", dependencies=[Depends(owner)])
    async def story_ideas():
        return {"task": request_story_ideas(store)}

    @app.post("/api/emergencies/{topic}/guidance", dependencies=[Depends(owner)])
    async def new_emergency_guidance(topic: str, data: NewGuidance):
        return save_guidance(store, topic, data)

    @app.post("/api/emergencies/{topic}/draft", dependencies=[Depends(owner)])
    async def emergency_draft(topic: str):
        return {"task": request_draft(store, topic)}

    @app.get("/api/episodes/{id}/safety-review", dependencies=[Depends(owner)])
    async def safety_brief(id: str):
        episode = store.get("episode", id)
        if not episode:
            raise HTTPException(404, "Серия не найдена")
        return briefing(Script.model_validate(episode["script"]), get_topics(store))

    @app.post("/api/episodes/{id}/safety-review", dependencies=[Depends(owner)])
    async def safety_confirm(id: str, data: SafetyReview):
        return confirm_review(store, id, data)

    @app.post("/api/episodes/{id}/revision", dependencies=[Depends(owner)])
    async def revise(id: str, script: Script):
        episode = store.get("episode", id)
        if not episode:
            raise ValueError("Серия не найдена")
        new_id = secrets.token_hex(12)
        store.put("episode", new_id, {"script": script.model_dump(), "series": series_for(script, episode["series"]), "status": "awaiting_script_approval", "previous_revision": id, "creative_brief": episode.get("creative_brief")})
        return {"episode": new_id, "note": "Предыдущая версия и её материалы сохранены"}

    @app.post("/api/episodes/{id}/approve", dependencies=[Depends(owner)])
    async def script_approve(id: str):
        return {"task": approve_script(store, id)}

    @app.post("/api/episodes/{id}/review", dependencies=[Depends(owner)])
    async def review(id: str, data: Review):
        return archive_reviewed(store, id, data.checks)

    @app.get("/api/episodes/{id}/video", dependencies=[Depends(owner)])
    async def video(id: str):
        episode = store.get("episode", id, {})
        name = episode.get("result")
        if not name:
            raise HTTPException(404, "Видео ещё не готово")
        return FileResponse(safe_file(store.root/"results", name), media_type="video/mp4", filename=id+".mp4")

    @app.post("/api/episodes/{id}/youtube", dependencies=[Depends(owner)])
    async def publish(id: str, data: Publication):
        if not data.confirm_private_upload:
            raise ValueError("Требуется явное согласие на загрузку этого MP4")
        return await youtube_publish(store, id, data.sha256, data.title, data.description)

    @app.get("/api/episodes/{id}/cleanup-plan", dependencies=[Depends(owner)])
    async def cleanup(id: str):
        return cleanup_plan(store, id)

    @app.post("/api/episodes/{id}/publication-plan", dependencies=[Depends(owner)])
    async def publication_plan(id: str, data: PublicationPlan):
        return plan_publication(store, id, data)

    @app.post("/api/episodes/{id}/cleanup", dependencies=[Depends(owner)])
    async def cleanup_approve(id: str, data: CleanupConsent):
        return approve_cleanup(store, id, data)

    @app.get("/api/episodes/{id}/delivery", dependencies=[Depends(owner)])
    async def delivery(id: str):
        if not store.get("episode", id):
            raise ValueError("Серия не найдена")
        fields = ("status", "video_id", "sha256", "checked_at", "note")
        return {"youtube": {k: v for k, v in store.get("publication", id, {}).items() if k in fields},
                "tiktok": {k: v for k, v in store.get("tiktok_publication", id, {}).items() if k in fields},
                "plans": [{"id": r["id"], **{k: r["body"].get(k) for k in ("status", "title", "privacy", "publish_at")}}
                          for r in store.objects("publication_plan") if r["body"]["episode"] == id]}

    @app.post("/api/publication-plans/{id}/cancel", dependencies=[Depends(owner)])
    async def publication_cancel(id: str):
        return cancel_plan(store, id)

    @app.post("/api/episodes/{id}/youtube-readback", dependencies=[Depends(owner)])
    async def publication_readback(id: str):
        from studio.publishing import youtube_readback
        return await youtube_readback(store, id)

    @app.post("/api/episodes/{id}/tiktok-draft", dependencies=[Depends(owner)])
    async def tiktok_draft(id: str, request: Request):
        from studio.tiktok import TikTokConsent, upload_draft
        return await upload_draft(store, id, TikTokConsent.model_validate(await request.json()))

    @app.post("/api/episodes/{id}/tiktok-readback", dependencies=[Depends(owner)])
    async def tiktok_readback(id: str):
        from studio.tiktok import readback
        return await readback(store, id)

    @app.get("/api/analytics/summary", dependencies=[Depends(owner)])
    async def analytics_summary():
        from studio.analytics_summary import studio_summary
        return studio_summary(store)

    @app.post("/api/backup", dependencies=[Depends(owner)])
    async def backup():
        return {"backup": store.backup().name}

    @app.post("/api/analytics", dependencies=[Depends(owner)])
    async def metrics(data: Metrics):
        return save_metrics(store, data)

    @app.post("/api/analytics/{episode_id}/youtube-sync", dependencies=[Depends(owner)])
    async def sync_metrics(episode_id: str):
        try:
            return await youtube_sync(store, episode_id)
        except Unavailable as exc:
            raise HTTPException(503, str(exc)) from None

    @app.post("/api/analytics/comments", dependencies=[Depends(owner)])
    async def comments(data: Comments):
        try:
            return await summarize_comments(store, data)
        except Unavailable as exc:
            raise HTTPException(503, str(exc)) from None

    @app.post("/api/mode/{mode}", dependencies=[Depends(owner)])
    async def mode(mode: str):
        if mode not in ("AUTO", "PC", "VPS"):
            raise ValueError("Допустимы AUTO, PC и VPS")
        store.put("settings", "mode", {"mode": mode})
        return {"mode": mode, "render": "paused" if mode == "VPS" else "windows_only", "preparation": "server_only"}

    @app.post("/api/schedule", dependencies=[Depends(owner)])
    async def schedule(data: Schedule):
        store.put("settings", "schedule", data.model_dump())
        return data.model_dump()

    @app.get("/api/archive", dependencies=[Depends(owner)])
    async def archived_script(season_title: str, episode_title: str):
        with store.connect() as db:
            row = db.execute("SELECT * FROM archive WHERE season_title=? AND episode_title=?", (season_title, episode_title)).fetchone()
        if not row:
            raise HTTPException(404, "Сценарий не найден в архиве")
        return dict(row)

    @app.post("/worker/claim", dependencies=[Depends(worker)])
    async def claim(data: WorkerClaim):
        store.put("worker", "latest", {"id": data.worker_id, "last_seen": time.time(), **data.model_dump(exclude_none=True)})
        if store.get("settings", "mode", {}).get("mode") == "VPS":
            return None
        from studio.comfort import quiet_now, worker_problem
        from studio.config import flag
        if settings.bot_token and settings.owner_id:
            if quiet_now(store):
                from studio.comfort import pause_notices
                pause_notices(store)
                return None
            if worker_problem(store):
                return None
        task = store.claim(["render"], data.worker_id, require_render_notice=bool(settings.bot_token and settings.owner_id and flag("STUDIO_RENDER_WARNING", True)))
        if task:
            # Не отдаём весь payload или другие серверные данные.
            return {"id": task["id"], "lease_token": task["lease_token"]}
        return None

    @app.post("/worker/{id}/heartbeat", dependencies=[Depends(worker)])
    async def heartbeat(id: str, data: Lease):
        active(id, data.lease_token)
        store.heartbeat(id, data.lease_token)
        store.put("worker", "latest", store.get("worker", "latest", {}) | {"last_seen": time.time(), "task": id})
        if data.stage:
            store.put("render_progress", id, {"stage": data.stage, "scene": data.scene, "scenes": data.scenes, "updated": time.time()})
        return {"ok": True}

    @app.get("/worker/{id}/manifest", dependencies=[Depends(worker)])
    async def manifest(id: str, lease: str):
        _, payload = active(id, lease)
        return json.loads((safe_file(store.root/"packages", payload["package"])/"manifest.json").read_text(encoding="utf-8"))

    @app.get("/worker/{id}/asset/{name}", dependencies=[Depends(worker)])
    async def asset(id: str, name: str, lease: str):
        _, payload = active(id, lease)
        folder = safe_file(store.root/"packages", payload["package"])
        manifest = json.loads((folder/"manifest.json").read_text(encoding="utf-8"))
        if name not in manifest["files"]:
            raise HTTPException(404, "Материал не найден")
        return FileResponse(safe_file(folder, name))

    @app.put("/worker/{id}/result", dependencies=[Depends(worker)])
    async def upload(id: str, lease: str, request: Request):
        _, payload = active(id, lease)
        folder = store.root/"results"
        folder.mkdir(exist_ok=True)
        filename = id+"-"+secrets.token_hex(8)+".mp4"
        path = folder/filename
        size = 0
        with path.open("xb") as out:
            async for chunk in request.stream():
                size += len(chunk)
                if size > 250_000_000:
                    raise HTTPException(413, "MP4 превышает 250 МБ; частичный файл сохранён для диагностики")
                out.write(chunk)
        active(id, lease)
        if size < 1024:
            raise ValueError("Пустой или повреждённый MP4")
        store.finish(id, lease, result={"filename": filename})
        store.enqueue("qc", {"filename": filename, "episode": payload["episode"], "render_task": id}, "qc:"+id)
        return {"status": "awaiting_qc"}

    @app.post("/worker/{id}/failed", dependencies=[Depends(worker)])
    async def failed(id: str, data: Lease):
        active(id, data.lease_token)
        store.finish(id, data.lease_token, error="Windows Worker не завершил рендер; материалы сохранены")
        return {"ok": True}

    app.mount("/", StaticFiles(directory=Path(__file__).parent/"static", html=True), name="miniapp")
    return app
