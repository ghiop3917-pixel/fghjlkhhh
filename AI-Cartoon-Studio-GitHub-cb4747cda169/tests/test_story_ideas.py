import asyncio
import json
from pathlib import Path

from studio.db import Store
from studio.emergencies import require_review, series_for
from studio.production import approve_topic
from studio.schemas import Script
from studio.story_ideas import invent_story_ideas


class CreativeModel:
    def __init__(self):
        self.calls = []

    async def ask(self, role, prompt, series, json_mode):
        self.calls.append(prompt)
        return json.dumps({"ideas": [
            {"title": "Часы показывают чужие воспоминания", "premise": "Мастер чинит старые часы и узнаёт, что они показывают не время, а чужие воспоминания, которые исчезают после просмотра.", "hook": "Он увидел завтрашний день своей матери", "genre": "Фантастическая семейная загадка", "world_rules": ["Часы показывают только одно воспоминание в день", "Просмотренное воспоминание нельзя вернуть"]},
            {"title": "Город, где обещания становятся видимыми", "premise": "Герой замечает, что невыполненные обещания жителей превратились в предметы, которые мешают городу жить и меняют отношения друзей.", "hook": "На пороге выросла дверь из давнего обещания", "genre": "Социальная фантастика", "world_rules": ["Выполненное обещание исчезает", "Предметы не дают прочитать мысли"]}
        ]}, ensure_ascii=False)


def test_open_ended_concepts_reach_writer_with_separate_world_memory(tmp_path):
    store = Store(tmp_path)
    model = CreativeModel()
    ids = asyncio.run(invent_story_ideas(store, model))["ideas"]
    assert len(ids) == 2
    payloads = []
    for id in ids:
        idea = store.get("trend", id)
        assert idea["format"] == "what_if_story" and not idea["url"]
        payload = json.loads(store.task(approve_topic(store, id))["payload"])
        assert payload["creative_brief"]["world_rules"] == idea["world_rules"]
        assert payload["format"] == "what_if_story"
        assert "emergency_topic" not in payload
        payloads.append(payload)
    assert payloads[0]["series"] != payloads[1]["series"]
    assert "НЕ ограничены" in model.calls[0]


def test_no_duplicate_concepts_and_no_unnecessary_safety_gate(tmp_path):
    store = Store(tmp_path)
    model = CreativeModel()
    assert len(asyncio.run(invent_story_ideas(store, model))["ideas"]) == 2
    assert not asyncio.run(invent_story_ideas(store, model))["ideas"]
    path = Path(__file__).resolve().parents[1] / "examples" / "critical_situations" / "nuclear.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(format="what_if_story", emergency_topic=None, safety_advice_ids=[])
    script = Script.model_validate(data)
    require_review({"script": script.model_dump()}, store)
    assert series_for(script, "whatif_first") == "whatif_first"
    assert series_for(script) == "what_if"
