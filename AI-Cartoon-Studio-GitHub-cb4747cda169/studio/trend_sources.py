"""Разрешённые дополнительные источники: YouTube API и посты каналов Telegram бота."""
import hashlib
import os
import re
from datetime import datetime, timezone

import httpx


async def youtube_trends(store):
    key = os.getenv("YOUTUBE_DATA_API_KEY", "")
    if not key:
        return []
    region = os.getenv("STUDIO_TREND_REGION", "RU")
    if not re.fullmatch(r"[A-Z]{2}", region):
        raise ValueError("Регион YouTube — двухбуквенный код")
    today = datetime.now(timezone.utc).date().isoformat()
    if not store.quota("youtube_trends", today, 2):
        return []
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        response = await client.get("https://www.googleapis.com/youtube/v3/videos", params={"part": "snippet,statistics", "chart": "mostPopular", "regionCode": region, "maxResults": 25, "key": key})
        response.raise_for_status()
        items = response.json().get("items", [])
    now = datetime.now(timezone.utc)
    found = []
    for video in items:
        id = "youtube-"+video["id"]
        snippet, statistics = video["snippet"], video.get("statistics", {})
        published = datetime.fromisoformat(snippet["publishedAt"].replace("Z", "+00:00"))
        if not 0 <= (now-published).total_seconds() <= 7*86400:
            continue
        prior = store.get("trend", id, {})
        views = int(statistics["viewCount"]) if statistics.get("viewCount") is not None else None
        observations = prior.get("observations", [])[-13:]
        previous = observations[-1] if observations else None
        measured_rate = None
        if previous and views is not None and previous["views"] is not None:
            seconds = (now-datetime.fromisoformat(previous["at"])).total_seconds()
            if seconds >= 3600 and views >= previous["views"]:
                measured_rate = (views-previous["views"])*3600/seconds
        observations.append({"at": now.isoformat(), "views": views})
        body = {"title": snippet["title"][:250], "url": "https://www.youtube.com/watch?v="+video["id"], "source": "youtube_api", "published": published.isoformat(),
                "verification": "Показатели видео измерены; утверждения из видео требуют фактчекинга", "popularity": views, "competition": None,
                "views_per_hour_measured": measured_rate, "observations": observations, "status": prior.get("status", "proposed")}
        store.put("trend", id, body)
        if not prior:
            found.append({"id": id, **body})
    return sorted(found, key=lambda x: x["popularity"] or 0, reverse=True)[:5]


def telegram_channel_post(store, message, allowed):
    chat = message.get("chat", {})
    if str(chat.get("id")) not in allowed or chat.get("type") != "channel":
        return None
    text = (message.get("text") or message.get("caption") or "").strip()
    timestamp = message.get("date", 0)
    if not text or not isinstance(timestamp, int) or not 0 <= datetime.now(timezone.utc).timestamp()-timestamp <= 72*3600:
        return None
    username = chat.get("username", "")
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
        return None
    url = f"https://t.me/{username}/{int(message['message_id'])}"
    id = "telegram-"+hashlib.sha256(url.encode()).hexdigest()[:20]
    if store.get("trend", id):
        return id
    store.put("trend", id, {"title": text.splitlines()[0][:250], "summary": text[:1500], "url": url, "source": "telegram_channel",
                            "published": datetime.fromtimestamp(timestamp, timezone.utc).isoformat(), "verification": "Пост канала; фактические утверждения не подтверждены",
                            "popularity": None, "competition": None, "status": "proposed"})
    return id
