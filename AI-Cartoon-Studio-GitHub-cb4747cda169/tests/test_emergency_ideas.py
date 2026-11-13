import asyncio
import json

from studio.db import Store
from studio.emergencies import invent_ideas, request_ideas
from studio.production import Production, approve_topic


class ModelFixture:
    def __init__(self):
        self.calls = []

    async def ask(self, role, prompt, series, json_mode):
        self.calls.append((role, prompt, series))
        return json.dumps({"ideas": [{"title": "Сумка на последнем автобусе", "premise": "Друзья опаздывают домой и спорят, стоит ли подходить к неизвестной сумке у закрытого павильона.", "hook": "Последний автобус уже отъезжает", "emergency_topic": "suspicious_object"}]}, ensure_ascii=False)


def test_model_invents_premise_and_chosen_idea_reaches_writer(tmp_path):
    store = Store(tmp_path)
    model = ModelFixture()
    result = asyncio.run(invent_ideas(store, model))
    id = result["ideas"][0]
    body = store.get("trend", id)
    assert "павильона" in body["premise"]
    assert body["source"] == "ai_story_planner" and body["popularity"] is None
    assert model.calls[0][2] == "critical_situations"
    task_id = approve_topic(store, id)
    payload = json.loads(store.task(task_id)["payload"])
    assert payload["format"] == "emergency_story"
    assert payload["emergency_topic"] == "suspicious_object"
    assert "павильона" in payload["topic"]
    assert approve_topic(store, id) == task_id


def test_same_model_title_does_not_create_duplicate_proposals(tmp_path):
    store = Store(tmp_path)
    model = ModelFixture()
    assert len(asyncio.run(invent_ideas(store, model))["ideas"]) == 1
    assert asyncio.run(invent_ideas(store, model))["ideas"] == []
    assert len(store.objects("trend")) == 1
    assert "Сумка на последнем автобусе" in model.calls[-1][1]


def test_unselected_backlog_limits_model_calls(tmp_path):
    store = Store(tmp_path)
    for i in range(6):
        store.put("trend", str(i), {"format": "emergency_story", "status": "proposed", "title": str(i)})
    model = ModelFixture()
    assert asyncio.run(invent_ideas(store, model))["status"] == "awaiting_choice"
    assert not model.calls


def test_daily_queue_deduplicates_and_waits_without_provider(tmp_path, monkeypatch):
    store = Store(tmp_path)
    monkeypatch.setenv("STUDIO_CLOUD_ENABLED", "false")
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    id = request_ideas(store, "daily-test")
    assert request_ideas(store, "daily-test") == id
    assert asyncio.run(Production(store).tick())
    assert store.task(id)["status"] == "pending" and store.task(id)["attempts"] == 0
    assert not store.objects("trend")
