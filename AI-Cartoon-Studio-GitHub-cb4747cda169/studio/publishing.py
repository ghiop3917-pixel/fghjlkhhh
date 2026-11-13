"""Публикация только точного, проверенного владельцем MP4. TikTok — ручной экспорт."""
import os
import time
from datetime import datetime, timezone

import httpx

from studio.config import flag
from studio.emergencies import check_publication
from studio.media import sha256


async def youtube_publish(store, episode_id: str, expected_hash: str, title: str, description: str, privacy="private", publish_at=None):
    if not flag("YOUTUBE_ENABLED"):
        raise ValueError("YouTube-публикация отключена")
    if privacy not in ("private", "unlisted", "public"):
        raise ValueError("Недопустимая видимость YouTube")
    status = {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}
    if publish_at:
        when = datetime.fromisoformat(publish_at.replace("Z", "+00:00"))
        if when.tzinfo is None or when <= datetime.now(timezone.utc):
            raise ValueError("Для расписания нужна будущая дата с часовым поясом")
        status.update(privacyStatus="private", publishAt=when.astimezone(timezone.utc).isoformat())
    episode = store.get("episode", episode_id)
    if not episode or episode.get("status") != "reviewed":
        raise ValueError("Серия не прошла просмотр владельцем")
    check_publication(episode, description, store)
    path = store.root / "results" / episode["result"]
    if sha256(path) != expected_hash or expected_hash != episode["qc"]["sha256"]:
        raise ValueError("Согласование относится к другому MP4")
    if store.get("publication", episode_id):
        raise ValueError("Публикация уже начата; повторная отправка запрещена до сверки с YouTube")
    keys = [os.getenv(k) for k in ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN")]
    if not all(keys):
        raise ValueError("Не настроен OAuth YouTube")
    # Конкурентные публикации блокирует уникальная запись objects внутри транзакции.
    store.put("publication", episode_id, {"status": "reserved", "sha256": expected_hash, "requested_privacy": privacy, "publish_at": publish_at}, expected=0)
    async with httpx.AsyncClient(timeout=180, follow_redirects=False) as client:
        try:
            auth = await client.post("https://oauth2.googleapis.com/token", data={"client_id": keys[0], "client_secret": keys[1], "refresh_token": keys[2], "grant_type": "refresh_token"})
            auth.raise_for_status()
            headers = {"Authorization": "Bearer " + auth.json()["access_token"]}
            r = await client.post("https://www.googleapis.com/upload/youtube/v3/videos", params={"uploadType": "resumable", "part": "snippet,status"},
                                  headers={**headers, "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(path.stat().st_size)},
                                  json={"snippet": {"title": title[:100], "description": description[:5000], "categoryId": "1", "defaultLanguage": "ru", "defaultAudioLanguage": "ru"}, "status": status})
            r.raise_for_status()
            location = r.headers["location"]
            from urllib.parse import urlparse
            target = urlparse(location)
            if target.scheme != "https" or target.hostname != "www.googleapis.com":
                raise ValueError("Неожиданный адрес загрузки YouTube")
            store.put("publication", episode_id, {"status": "uploading", "sha256": expected_hash, "session": location, "requested_privacy": privacy, "publish_at": publish_at})
            async def chunks():
                with path.open("rb") as f:
                    while chunk := f.read(1024*1024):
                        yield chunk
            uploaded = await client.put(location, headers={**headers, "Content-Type": "video/mp4", "Content-Length": str(path.stat().st_size)}, content=chunks())
            uploaded.raise_for_status()
            video = uploaded.json()
            video_id = video["id"]
            # Сохраняем ID до readback: потеря ответа чтения не должна потерять связь с видео.
            current = store.get("publication", episode_id)
            store.put("publication", episode_id, current | {"video_id": video_id, "status": "awaiting_readback"})
            check = await client.get("https://www.googleapis.com/youtube/v3/videos", params={"part": "status", "id": video_id}, headers=headers)
            check.raise_for_status()
            items = check.json().get("items", [])
            if not items or items[0]["id"] != video_id:
                raise ValueError("YouTube не подтвердил видео при чтении")
            remote = items[0]["status"]
            state = "scheduled" if publish_at and remote.get("publishAt") else "confirmed_"+remote.get("privacyStatus", "unknown")
            if (privacy != "private" and not publish_at and remote.get("privacyStatus") != privacy) or (publish_at and not remote.get("publishAt")):
                state = "platform_limited"
            record = {"status": state, "sha256": expected_hash, "video_id": video_id, "remote_status": remote, "requested_privacy": privacy, "publish_at": publish_at, "checked_at": time.time()}
            store.put("publication", episode_id, record)
            return record
        except Exception:
            current = store.get("publication", episode_id, {})
            current["status"] = "needs_reconciliation"
            store.put("publication", episode_id, current)
            raise ValueError("Результат публикации требует сверки с YouTube; автоматический повтор заблокирован") from None


async def youtube_readback(store, episode_id):
    record = store.get("publication", episode_id, {})
    if not record.get("video_id"):
        raise ValueError("Нет ID YouTube для сверки; не загружайте видео повторно вслепую")
    keys = [os.getenv(k) for k in ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN")]
    if not all(keys):
        from studio.agents import Unavailable
        raise Unavailable("Для сверки YouTube нужен OAuth")
    async with httpx.AsyncClient(timeout=40, follow_redirects=False) as client:
        token = await client.post("https://oauth2.googleapis.com/token", data={"client_id": keys[0], "client_secret": keys[1], "refresh_token": keys[2], "grant_type": "refresh_token"})
        token.raise_for_status()
        response = await client.get("https://www.googleapis.com/youtube/v3/videos", headers={"Authorization": "Bearer "+token.json()["access_token"]}, params={"part": "status", "id": record["video_id"]})
        response.raise_for_status()
        items = response.json().get("items", [])
    if not items:
        record.update(status="not_found", checked_at=time.time())
    else:
        remote = items[0]["status"]
        state = "scheduled" if remote.get("publishAt") and remote.get("privacyStatus") == "private" else "confirmed_"+remote.get("privacyStatus", "unknown")
        if remote.get("uploadStatus") in ("failed", "rejected", "deleted"):
            state = "remote_failed"
        record.update(status=state, remote_status=remote, checked_at=time.time())
    store.put("publication", episode_id, record)
    return record


def cleanup_plan(store, episode_id):
    from studio.retention import cleanup_preview
    return cleanup_preview(store, episode_id)
