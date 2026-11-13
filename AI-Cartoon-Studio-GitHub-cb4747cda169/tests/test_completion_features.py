import asyncio
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image, ImageChops

from studio.animation import background_image
from studio.character_art import character_frame
from studio.db import Store
from studio.imagery import prepare_backgrounds
from studio.media import sha256
from studio.publication_plans import (
    PublicationPlan,
    cancel_plan,
    dispatch_plan,
    plan_publication,
)
from studio.publishing import youtube_publish
from studio.retention import (
    CleanupConsent,
    approve_cleanup,
    cleanup_preview,
    execute_cleanup,
)
from studio.schemas import Character, Scene, Script
from studio.subtitles import Captioner


def script():
    path = Path(__file__).resolve().parents[1]/"examples"/"critical_situations"/"nuclear.json"
    body = json.loads(path.read_text(encoding="utf-8"))
    body.update(format="what_if_story", emergency_topic=None, safety_advice_ids=[])
    return Script.model_validate(body)


def reviewed(store):
    (store.root/"results").mkdir()
    video = store.root/"results"/"fixture.mp4"
    video.write_bytes(b"fixture video, not a real MP4")
    store.put("episode", "ep", {"status": "reviewed", "result": video.name, "script": script().model_dump(), "qc": {"sha256": sha256(video)}})
    with store.connect() as db:
        s = script()
        db.execute("INSERT INTO archive VALUES(?,?,?)", (s.season_title, s.episode_title, s.model_dump_json()))
    return sha256(video)


def test_approved_publication_plan_waits_without_youtube_and_can_cancel(tmp_path, monkeypatch):
    store = Store(tmp_path)
    digest = reviewed(store)
    data = PublicationPlan(sha256=digest, title="Тест", description="Вымышленная история", privacy="public", approve_exact_upload=True)
    plan = plan_publication(store, "ep", data)
    monkeypatch.setenv("YOUTUBE_ENABLED", "false")
    from studio.agents import Unavailable
    with pytest.raises(Unavailable):
        asyncio.run(dispatch_plan(store, plan["plan"]))
    assert cancel_plan(store, plan["plan"])["status"] == "cancelled"
    assert asyncio.run(dispatch_plan(store, plan["plan"]))["status"] == "cancelled"


def test_scheduled_youtube_upload_is_private_until_platform_schedule(tmp_path, monkeypatch):
    store = Store(tmp_path)
    digest = reviewed(store)
    when = (datetime.now(timezone.utc)+timedelta(days=2)).isoformat()
    for key in ("YOUTUBE_ENABLED", "YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN"):
        monkeypatch.setenv(key, "true" if key == "YOUTUBE_ENABLED" else "fixture")
    def handle(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "fixture"})
        if request.method == "POST":
            status = json.loads(request.content)["status"]
            assert status["privacyStatus"] == "private" and status["publishAt"] == when
            return httpx.Response(200, headers={"location": "https://www.googleapis.com/upload/session"})
        if request.method == "PUT":
            return httpx.Response(200, json={"id": "fixture-video"})
        return httpx.Response(200, json={"items": [{"id": "fixture-video", "status": {"privacyStatus": "private", "publishAt": when}}]})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handle), **kw))
    result = asyncio.run(youtube_publish(store, "ep", digest, "Тест", "Описание", "public", when))
    assert result["status"] == "scheduled"


def test_cleanup_requires_public_processed_video_and_preserves_important_files(tmp_path, monkeypatch):
    store = Store(tmp_path)
    digest = reviewed(store)
    publication = {"status": "confirmed_public", "video_id": "fixture", "sha256": digest, "remote_status": {"privacyStatus": "public", "uploadStatus": "processed"}}
    store.put("publication", "ep", publication)
    plan = cleanup_preview(store, "ep")
    assert plan["eligible"]
    approval = approve_cleanup(store, "ep", CleanupConsent(plan_sha256=plan["plan_sha256"], confirm_delete_listed_files=True, confirm_youtube_is_sufficient=True))
    async def readback(*args):
        return publication
    monkeypatch.setattr("studio.publishing.youtube_readback", readback)
    requested_removals = []
    result = asyncio.run(execute_cleanup(store, approval["consent"], delete_file=requested_removals.append))
    # Реальное удаление даже тестовых файлов в этой проверке не выполняется.
    assert requested_removals == [tmp_path/"results"/"fixture.mp4"]
    assert store.path.is_file() and (tmp_path/"backups"/result["backup"]).is_file()
    assert asyncio.run(execute_cleanup(store, approval["consent"], delete_file=requested_removals.append)) == result
    assert len(requested_removals) == 1


def test_private_or_unarchived_video_cannot_be_cleaned(tmp_path):
    store = Store(tmp_path)
    digest = reviewed(store)
    store.put("publication", "ep", {"status": "confirmed_private", "sha256": digest})
    assert not cleanup_preview(store, "ep")["eligible"]


def test_backgrounds_are_different_and_character_sprite_is_transparent(tmp_path):
    island = background_image(Scene(character_id="max", text="Тест", setting="island"))
    room = background_image(Scene(character_id="max", text="Тест", setting="interior"))
    assert ImageChops.difference(island, room).getbbox()
    c = Character(name="Макс", age=18, face="Овальное", hairstyle="Короткая", hair_color="#392717", body="Среднее", clothing="Куртка", clothing_color="#517c61", voice="", personality="Тест", speech="Тест")
    output = io.BytesIO()
    character_frame(c, Scene(character_id="max", text="Тест"), output, layer="character", mouth_open=True)
    output.seek(0)
    with Image.open(output) as image:
        assert image.mode == "RGBA" and image.getpixel((0,0))[3] == 0


def test_whisper_assisted_timing_preserves_original_script():
    class Model:
        def transcribe(self, *args, **kwargs):
            words = [SimpleNamespace(word=w, start=a, end=b) for w,a,b in (("Привет", .1,.6), ("друг", 1.,1.6), ("сегодня", 2.,2.8))]
            return iter([SimpleNamespace(words=words)]), None
    result = Captioner(Model()).align("unused.wav", "Привет, друг сегодня", 3.)
    assert result["method"] == "whisper_assisted" and result["confidence"] == 1
    assert result["words"][0]["text"] == "Привет,"
    assert result["words"][0]["end"] < result["words"][1]["end"]


def test_procedural_mode_makes_no_cloud_calls(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_VISUAL_PROVIDER", "procedural")
    assert asyncio.run(prepare_backgrounds(Store(tmp_path), script())) == {}
