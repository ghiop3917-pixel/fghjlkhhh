"""Чтение собственных YouTube-метрик по OAuth. Неизвестные метрики остаются null."""
import os
from datetime import datetime, timedelta, timezone

import httpx

from studio.agents import Unavailable
from studio.analytics import Comments, Metrics, save_metrics, summarize_comments
from studio.config import flag


async def youtube_sync(store, episode_id):
    if not flag("YOUTUBE_ANALYTICS_ENABLED"):
        raise Unavailable("Автоматическое чтение YouTube отключено")
    episode = store.get("episode", episode_id)
    publication = store.get("publication", episode_id, {})
    if not episode or not publication.get("video_id"):
        raise ValueError("У серии нет подтверждённого YouTube video_id")
    keys = [os.getenv(k) for k in ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN")]
    if not all(keys):
        raise Unavailable("Для чтения метрик нужен OAuth YouTube")
    data = Metrics(episode_id=episode_id, series=episode["series"])
    comments = []
    curve = []
    async with httpx.AsyncClient(timeout=40, follow_redirects=False) as client:
        response = await client.post("https://oauth2.googleapis.com/token", data={"client_id": keys[0], "client_secret": keys[1], "refresh_token": keys[2], "grant_type": "refresh_token"})
        if response.status_code != 200:
            raise Unavailable("OAuth чтения YouTube недоступен; проверьте разрешения")
        headers = {"Authorization": "Bearer " + response.json()["access_token"]}
        video_id = publication["video_id"]
        response = await client.get("https://www.googleapis.com/youtube/v3/videos", params={"part": "statistics", "id": video_id}, headers=headers)
        response.raise_for_status()
        items = response.json().get("items", [])
        if items:
            views = items[0].get("statistics", {}).get("viewCount")
            data.views = int(views) if views is not None else None
        today = datetime.now(timezone.utc).date()
        response = await client.get("https://youtubeanalytics.googleapis.com/v2/reports", headers=headers,
                                    params={"ids": "channel==MINE", "startDate": (today-timedelta(days=30)).isoformat(), "endDate": today.isoformat(), "metrics": "averageViewPercentage,subscribersGained", "filters": "video=="+video_id})
        if response.status_code == 200:
            report = response.json()
            rows = report.get("rows", [])
            if rows:
                values = dict(zip([c["name"] for c in report["columnHeaders"]], rows[0]))
                percentage = values.get("averageViewPercentage")
                data.retention = float(percentage)/100 if percentage is not None else None
                data.subscribers = int(values["subscribersGained"]) if "subscribersGained" in values else None
        else:
            store.event("analytics.partial", {"episode": episode_id, "reason": "YouTube Analytics API недоступен; удержание не заполнено"})
        response = await client.get("https://youtubeanalytics.googleapis.com/v2/reports", headers=headers,
                                    params={"ids": "channel==MINE", "startDate": (today-timedelta(days=30)).isoformat(), "endDate": today.isoformat(), "dimensions": "elapsedVideoTimeRatio", "metrics": "audienceWatchRatio,relativeRetentionPerformance", "filters": "video=="+video_id})
        if response.status_code == 200:
            raw = response.json()
            names = [column["name"] for column in raw.get("columnHeaders", [])]
            curve = [dict(zip(names, row)) for row in raw.get("rows", [])]
        response = await client.get("https://www.googleapis.com/youtube/v3/commentThreads", headers=headers,
                                    params={"part": "snippet", "videoId": video_id, "maxResults": 100, "textFormat": "plainText", "order": "time"})
        if response.status_code == 200:
            for item in response.json().get("items", []):
                text = item["snippet"]["topLevelComment"]["snippet"].get("textDisplay", "")
                # Имена/ID комментаторов не извлекаем.
                if text and sum(map(len, comments))+len(text) <= 12000:
                    comments.append(text[:2000])
        else:
            store.event("analytics.comments_unavailable", {"episode": episode_id})
    result = save_metrics(store, data)
    result["source"] = "youtube_oauth"
    result["retention_curve"] = curve
    result["rewatch_count_note"] = "Кривая удержания не превращается в выдуманный счётчик повторных просмотров"
    store.put("metrics", episode_id, result)
    current = store.get("analytics", episode["series"], {})
    current["last_metrics"] = result
    store.put("analytics", episode["series"], current)
    if comments:
        await summarize_comments(store, Comments(series=episode["series"], comments=comments))
    return result
