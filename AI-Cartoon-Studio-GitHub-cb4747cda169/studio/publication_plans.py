"""Явно согласованный план публикации точного MP4 и точных метаданных."""
import secrets
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, StrictBool, model_validator

from studio.agents import Unavailable
from studio.config import flag
from studio.emergencies import check_publication
from studio.media import safe_file, sha256
from studio.publishing import youtube_publish


class PublicationPlan(BaseModel):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    title: str = Field(min_length=1, max_length=100)
    description: str = Field(max_length=5000)
    privacy: Literal["private", "unlisted", "public"] = "private"
    publish_at: datetime | None = None
    approve_exact_upload: StrictBool

    @model_validator(mode="after")
    def approval(self):
        if self.approve_exact_upload is not True:
            raise ValueError("Нужно явное согласие на отправку конкретного файла и метаданных")
        if self.publish_at and (self.publish_at.tzinfo is None or self.publish_at <= datetime.now(timezone.utc)):
            raise ValueError("Расписание должно содержать будущую дату с часовым поясом")
        if self.publish_at and self.privacy != "public":
            raise ValueError("Расписание publish_at предназначено для публичного выпуска")
        return self


def plan_publication(store, episode_id, data: PublicationPlan):
    episode = store.get("episode", episode_id)
    if not episode or episode.get("status") != "reviewed":
        raise ValueError("Серия должна пройти просмотр и семь итоговых проверок")
    path = safe_file(store.root/"results", episode["result"])
    if data.sha256 != episode["qc"]["sha256"] or sha256(path) != data.sha256:
        raise ValueError("Согласование относится к другому MP4")
    check_publication(episode, data.description, store)
    if store.get("publication", episode_id):
        raise ValueError("Отправка уже начата; сначала выполните сверку")
    id = secrets.token_hex(12)
    # На одну серию может быть только один активный план.
    plans = store.objects("publication_plan")
    if any(p["body"]["episode"] == episode_id and p["body"]["status"] in ("approved", "dispatching", "sent") for p in plans):
        raise ValueError("План для серии уже согласован")
    body = data.model_dump(mode="json") | {"episode": episode_id, "status": "approved"}
    store.put("publication_plan", id, body, expected=0)
    task = store.enqueue("publication", {"plan": id}, "publication-plan:"+id)
    store.event("owner.publication_approved", {"plan": id, "episode": episode_id, "sha256": data.sha256, "privacy": data.privacy})
    return {"plan": id, "task": task}


def cancel_plan(store, id):
    plan = store.get("publication_plan", id)
    if not plan or plan["status"] != "approved":
        raise ValueError("Можно отменить только ещё не отправленный план")
    store.put("publication_plan", id, plan | {"status": "cancelled"})
    return {"status": "cancelled"}


async def dispatch_plan(store, id):
    plan = store.get("publication_plan", id)
    if not plan:
        raise ValueError("План не найден")
    if plan["status"] in ("cancelled", "sent"):
        return {"status": plan["status"]}
    existing = store.get("publication", plan["episode"])
    if existing:
        if existing["status"] in ("confirmed_private", "confirmed_unlisted", "confirmed_public", "scheduled", "platform_limited"):
            store.put("publication_plan", id, plan | {"status": "sent", "remote": existing})
            return existing
        raise ValueError("Предыдущая отправка требует сверки; повторная загрузка запрещена")
    if not flag("YOUTUBE_ENABLED"):
        raise Unavailable("Согласованный план ожидает подключения YouTube; отправка отключена")
    store.put("publication_plan", id, plan | {"status": "dispatching"})
    result = await youtube_publish(store, plan["episode"], plan["sha256"], plan["title"], plan["description"], plan["privacy"], plan.get("publish_at"))
    store.put("publication_plan", id, plan | {"status": "sent", "remote": result})
    return result
