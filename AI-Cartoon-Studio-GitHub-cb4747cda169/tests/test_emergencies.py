import json
import os
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from studio.api import create_app
from studio.config import Settings
from studio.db import Store
from studio.emergencies import (
    DISCLAIMER,
    SOURCES,
    SafetyReview,
    briefing,
    check_publication,
    confirm_review,
    fingerprint,
    request_draft,
    validate_advice,
)
from studio.media import build_package
from studio.production import Production, approve_script
from studio.schemas import Character, Script


def sample(topic="nuclear"):
    path = Path(__file__).resolve().parents[1] / "examples" / "critical_situations" / (topic + ".json")
    return Script.model_validate_json(path.read_text(encoding="utf-8"))


def review(script):
    return SafetyReview(script_sha256=fingerprint(script), registry_sha256=briefing(script)["registry_sha256"], sources_checked=True, advice_checked=True, fictional_label_checked=True)


@pytest.mark.parametrize("topic", ["nuclear", "suspicious_object", "armed_attack"])
def test_examples_have_spoken_advice_and_primary_sources(topic):
    script = sample(topic)
    validate_advice(script)
    info = briefing(script)
    assert all(a["reference"]["url"].startswith("https://") for a in info["advice"])
    assert info["script_sha256"] == fingerprint(script)


def test_emergency_cannot_proceed_without_matching_human_review(tmp_path):
    store = Store(tmp_path)
    script = sample()
    store.put("episode", "ep", {"status": "awaiting_script_approval", "script": script.model_dump()})
    with pytest.raises(ValueError, match="Сначала проверьте"):
        approve_script(store, "ep")
    assert not store.tasks()
    confirm_review(store, "ep", review(script))
    assert approve_script(store, "ep")
    changed = store.get("episode", "ep")
    changed["status"] = "awaiting_script_approval"
    changed["script"]["scenes"][0]["text"] += " Он волновался."
    store.put("episode", "ep", changed)
    with pytest.raises(ValueError, match="Сначала проверьте"):
        approve_script(store, "ep")


def test_wrong_source_or_changed_advice_is_not_accepted():
    script = sample()
    script.safety_advice_ids = ["attack_escape"]
    with pytest.raises(ValueError, match="не относится"):
        validate_advice(script)
    script = sample()
    script.scenes[1].text = "Самостоятельно придуманный совет вместо проверенного."
    with pytest.raises(ValueError, match="дословно"):
        validate_advice(script)


def test_publication_requires_disclaimer_sources_and_current_review(tmp_path):
    script = sample()
    episode = {"script": script.model_dump(), "safety_review": review(script).model_dump() | {"registry_sha256": briefing(script)["registry_sha256"]}}
    with pytest.raises(ValueError, match="В описании"):
        check_publication(episode, "Описание без источников")
    info = briefing(script)
    description = DISCLAIMER + "\n" + "\n".join(a["reference"]["url"] for a in info["advice"])
    check_publication(episode, description)


def test_emergency_api_and_separate_story_queue(tmp_path):
    store = Store(tmp_path)
    client = TestClient(create_app(Settings(data=tmp_path, api_token="a"*40, worker_token="b"*40), store))
    assert client.get("/api/emergencies").status_code == 401
    owner = {"Authorization": "Bearer " + "a"*40}
    assert len(client.get("/api/emergencies", headers=owner).json()["topics"]) == 3
    r = client.post("/api/emergencies/armed_attack/draft", headers=owner)
    assert r.status_code == 200
    task = store.task(r.json()["task"])
    payload = json.loads(task["payload"])
    assert payload["series"] == "critical_situations" and payload["format"] == "emergency_story"
    assert client.post("/api/emergencies/unknown/draft", headers=owner).status_code == 400
    imported = client.post("/api/episodes/import", headers=owner, json=sample().model_dump())
    episode_id = imported.json()["episode"]
    assert store.get("episode", episode_id)["series"] == "critical_situations"
    assert client.post(f"/api/episodes/{episode_id}/approve", headers=owner).status_code == 400
    info = client.get(f"/api/episodes/{episode_id}/safety-review", headers=owner).json()
    assert client.post(f"/api/episodes/{episode_id}/safety-review", headers=owner, json=review(sample()).model_dump()).status_code == 200
    assert info["script_sha256"] == fingerprint(sample())
    assert client.post(f"/api/episodes/{episode_id}/approve", headers=owner).status_code == 200


def test_emergency_package_contains_fiction_label_and_sources(tmp_path):
    def synth(text, path, model):
        with wave.open(str(path), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(8000)
            out.writeframes(b"\0\0" * 8000 * 8)
        return 8.
    passport = Character(name="Макс", age=18, face="Овальное", hairstyle="Короткая", hair_color="#392717", body="Среднее", clothing="Куртка", clothing_color="#517c61", voice="", personality="Ироничный", speech="Короткие фразы")
    folder = tmp_path / "package"
    build_package(folder, sample(), {"max": passport}, synth=synth)
    assert "Учебная история" in (folder / "subtitles.ass").read_text(encoding="utf-8")
    metadata = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["format"] == "emergency_story" and metadata["safety_sources"]
    assert DISCLAIMER in metadata["description"]


def test_draft_without_model_waits_normally(tmp_path):
    import asyncio
    store = Store(tmp_path)
    task_id = request_draft(store, "nuclear")
    assert asyncio.run(Production(store).tick())
    assert store.task(task_id)["status"] == "pending"
    assert store.task(task_id)["attempts"] == 0


def test_source_registry_change_requires_new_review(tmp_path, monkeypatch):
    store = Store(tmp_path)
    script = sample()
    store.put("episode", "ep", {"status": "awaiting_script_approval", "script": script.model_dump()})
    confirm_review(store, "ep", review(script))
    monkeypatch.setitem(SOURCES, "cdc_inside", SOURCES["cdc_inside"] | {"title": "Обновлённый источник"})
    with pytest.raises(ValueError, match="Сначала проверьте"):
        approve_script(store, "ep")


@pytest.mark.skipif(os.getenv("STUDIO_RENDER_TEST") != "1", reason="Реальный рендер учебной маркировки включается отдельно")
def test_emergency_disclaimer_real_render(tmp_path):
    import subprocess

    from PIL import Image

    from studio.media import technical_qc
    from studio.worker import render

    def synth(text, path, model):
        # Звуковой сигнал используется только для проверки монтажа, не вместо реальной речи.
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=24000", "-t", "8", str(path)], check=True)
        return 8.
    passport = Character(name="Макс", age=18, face="Овальное", hairstyle="Короткая", hair_color="#392717", body="Среднее", clothing="Куртка", clothing_color="#517c61", voice="", personality="Тест", speech="Тест")
    folder = tmp_path / "package"
    manifest = build_package(folder, sample(), {"max": passport}, synth=synth)
    output = render(folder, manifest, "auto")
    report = technical_qc(output)
    assert report["pass"], report
    frame = tmp_path / "frame.png"
    subprocess.run(["ffmpeg", "-v", "error", "-ss", "1", "-i", str(output), "-frames:v", "1", str(frame)], check=True)
    with Image.open(frame) as image:
        # В исходной декорации эта область однотонная; светлые пиксели — встроенная маркировка.
        crop = image.convert("RGB").crop((80, 65, 1000, 220))
        pixels = crop.tobytes()
        assert sum(all(c > 180 for c in pixel) for pixel in zip(pixels[::3], pixels[1::3], pixels[2::3])) > 200
