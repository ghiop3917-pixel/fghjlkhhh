import asyncio
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from studio.agents import Unavailable
from studio.db import Store
from studio.emergencies import (
    NewGuidance,
    SafetyReview,
    briefing,
    confirm_review,
    get_topics,
    instruction,
    invent_ideas,
    save_guidance,
)
from studio.production import approve_script, approve_topic
from studio.schemas import Script


def guidance():
    # Исключительно тестовые данные, не настоящая памятка для публикации.
    return NewGuidance(title="Тестовая новая категория", scope="Границы тестового сценария без реальных рекомендаций.",
                       advice={"first_step": {"text": "Это тестовая реплика для проверки программного конвейера, а не реальная рекомендация.", "source": "official_source"}},
                       sources={"official_source": {"title": "Тестовая ссылка", "url": "https://www.mchs.gov.ru/"}}, sources_checked=True)


class NewTopicModel:
    async def ask(self, *args, **kwargs):
        return json.dumps({"ideas": [{"title": "Земля дрогнула во время экзамена", "premise": "Герои оказываются в незнакомой ситуации и должны понять, как получить официальную информацию о происходящем.",
                                      "hook": "Класс внезапно замолчал", "emergency_topic": "earthquake", "topic_title": "Землетрясение", "research_queries": ["МЧС землетрясение рекомендации"]}]})


def test_ai_new_category_is_stored_without_fake_sources(tmp_path):
    store = Store(tmp_path)
    result = asyncio.run(invent_ideas(store, NewTopicModel()))
    idea = store.get("trend", result["ideas"][0])
    assert idea["emergency_topic"] == "earthquake"
    assert idea["guidance_status"] == "needs_sources" and not idea["url"]
    assert store.get("research_request", "earthquake")["status"] == "needs_sources"
    id = approve_topic(store, result["ideas"][0])
    assert json.loads(store.task(id)["payload"])["emergency_topic"] == "earthquake"
    with pytest.raises(Unavailable, match="ожидает проверки"):
        instruction("earthquake", store)


def test_new_guidance_unlocks_topic_and_complete_review(tmp_path):
    store = Store(tmp_path)
    idea_id = asyncio.run(invent_ideas(store, NewTopicModel()))["ideas"][0]
    task_id = approve_topic(store, idea_id)
    with store.connect() as db:
        db.execute("UPDATE tasks SET available=9999999999 WHERE id=?", (task_id,))
    save_guidance(store, "earthquake", guidance())
    assert store.get("trend", idea_id)["guidance_status"] == "ready"
    assert store.task(task_id)["available"] == 0
    assert "earthquake" in get_topics(store)
    assert "тестовая реплика" in instruction("earthquake", store)
    sample = Path(__file__).resolve().parents[1] / "examples" / "critical_situations" / "nuclear.json"
    data = json.loads(sample.read_text(encoding="utf-8"))
    data["emergency_topic"] = "earthquake"
    data["safety_advice_ids"] = ["first_step"]
    data["scenes"][1]["text"] = guidance().advice["first_step"].text
    script = Script.model_validate(data)
    store.put("episode", "new", {"status": "awaiting_script_approval", "series": "critical_situations", "script": script.model_dump()})
    info = briefing(script, get_topics(store))
    confirm_review(store, "new", SafetyReview(script_sha256=info["script_sha256"], registry_sha256=info["registry_sha256"], sources_checked=True, advice_checked=True, fictional_label_checked=True))
    assert approve_script(store, "new")


def test_unverified_guidance_and_unofficial_links_rejected(tmp_path):
    data = guidance().model_dump()
    data["sources_checked"] = False
    with pytest.raises(ValidationError):
        NewGuidance.model_validate(data)
    data = guidance().model_dump()
    data["sources"]["official_source"]["url"] = "https://mchs.gov.ru.attacker.example/"
    with pytest.raises(ValidationError):
        NewGuidance.model_validate(data)
    with pytest.raises(ValueError, match="не перезаписываются"):
        save_guidance(Store(tmp_path), "nuclear", guidance())
