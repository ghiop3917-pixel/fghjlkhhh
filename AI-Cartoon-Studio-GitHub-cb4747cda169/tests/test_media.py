import os
import wave

import pytest
from PIL import Image

from studio.media import build_package, sha256, technical_qc, validate_manifest
from studio.schemas import Character, Script
from studio.worker import render


def passport():
    return Character(name="Макс", age=18, face="Овальное", hairstyle="Короткая", hair_color="#392717", body="Среднее", clothing="Куртка", clothing_color="#517c61", voice="", personality="Ироничный", speech="Короткие фразы")


def script():
    text = "Мы договорились встретиться после занятий у старого магазина. Я принёс деньги, но тебя там не было. Теперь объясни, почему мой телефон оказался у твоего брата."
    return Script(season_title="Тестовый сезон", episode_title="Проверка конвейера", synopsis="Тестовая постановка, не готовая серия", scenes=[{"character_id": "max", "text": text} for _ in range(3)], next_plan="Разговор друзей", unresolved=["Пропажа телефона"])


def test_package_real_files_and_integrity(tmp_path):
    def synthetic_fixture(text, path, model):
        # Только тест: тишина не используется вместо озвучки в production.
        with wave.open(str(path), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(16000)
            out.writeframes(b"\0\0"*16000*10)
        return 10.0
    folder = tmp_path/"package"
    manifest = build_package(folder, script(), {"max": passport()}, synth=synthetic_fixture)
    assert validate_manifest(manifest)["duration"] == 30
    assert "\\kf" in (folder/"subtitles.ass").read_text(encoding="utf-8")
    with Image.open(folder/"cover.png") as image:
        assert image.size == (1080, 1920)


def test_missing_piper_waits_without_partial_package(tmp_path, monkeypatch):
    import asyncio

    from studio.db import Store
    from studio.production import Production
    store = Store(tmp_path)
    store.put("character", "max", passport().model_dump())
    store.put("episode", "ep", {"script": script().model_dump(), "series": "main", "status": "materials_approved"})
    store.enqueue("materials", {"episode": "ep"}, "test-materials")
    monkeypatch.setattr("studio.speech.shutil.which", lambda _: None)
    assert asyncio.run(Production(store).tick()) is True
    task = store.tasks()[0]
    assert task["status"] == "pending" and task["attempts"] == 0
    assert not (tmp_path/"packages").exists()


def fixture_voice(calls):
    def synth(text, path, model):
        calls.append(text)
        with wave.open(str(path), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(8000)
            out.writeframes(b"\0\0" * 8000 * 10)
        return 10.0
    return synth


def test_edit_one_line_reuses_visuals_and_other_voices(tmp_path):
    calls = []
    original = script()
    original.music = "none"
    first = tmp_path / "first"
    build_package(first, original, {"max": passport()}, synth=fixture_voice(calls))
    originals = {p.name: sha256(p) for p in first.iterdir()}
    revised = original.model_copy(deep=True)
    revised.scenes[1].text += " Говори честно."
    calls.clear()
    manifest = build_package(tmp_path / "revision", revised, {"max": passport()}, synth=fixture_voice(calls), reuse_root=first)
    assert calls == [revised.scenes[1].text]
    assert manifest["reuse"] == {"images": 3, "audio": 2, "music": 0}
    assert {p.name: sha256(p) for p in first.iterdir()} == originals
    assert validate_manifest(manifest)


def test_visual_change_does_not_resynthesize_voice(tmp_path):
    calls = []
    original = script()
    original.music = "none"
    first = tmp_path / "first"
    build_package(first, original, {"max": passport()}, synth=fixture_voice(calls))
    revised = original.model_copy(deep=True)
    revised.scenes[1].emotion = "angry"
    calls.clear()
    manifest = build_package(tmp_path / "revision", revised, {"max": passport()}, synth=fixture_voice(calls), reuse_root=first)
    assert calls == []
    assert manifest["reuse"]["audio"] == 3
    assert manifest["reuse"]["images"] == 2


def test_new_hair_style_regenerates_images_without_voice(tmp_path):
    calls = []
    original = script()
    original.music = "none"
    first = tmp_path / "first"
    build_package(first, original, {"max": passport()}, synth=fixture_voice(calls))
    revised_character = passport()
    revised_character.hairstyle = "Длинные волосы"
    calls.clear()
    manifest = build_package(tmp_path / "revision", original, {"max": revised_character}, synth=fixture_voice(calls), reuse_root=first)
    assert calls == []
    assert manifest["reuse"]["audio"] == 3 and manifest["reuse"]["images"] == 0
    assert sha256(first / "scene-00.png") != sha256(tmp_path / "revision" / "scene-00.png")


def test_replaced_voice_at_same_path_invalidates_audio_only(tmp_path):
    calls = []
    voice = tmp_path / "voice.onnx"
    voice.write_bytes(b"test model version 1")
    character = passport()
    character.voice = str(voice)
    original = script()
    original.music = "none"
    first = tmp_path / "first"
    build_package(first, original, {"max": character}, synth=fixture_voice(calls))
    with voice.open("ab") as out:
        out.write(b"version 2")
    calls.clear()
    manifest = build_package(tmp_path / "revision", original, {"max": character}, synth=fixture_voice(calls), reuse_root=first)
    assert len(calls) == 3
    assert manifest["reuse"]["images"] == 3
    assert manifest["reuse"]["audio"] == 0


def test_corrupt_previous_asset_is_regenerated(tmp_path):
    calls = []
    original = script()
    original.music = "none"
    original.scenes[1].text += " Серьёзно."
    first = tmp_path / "first"
    build_package(first, original, {"max": passport()}, synth=fixture_voice(calls))
    with (first / "scene-01.wav").open("ab") as out:
        out.write(b"corruption")
    calls.clear()
    manifest = build_package(tmp_path / "revision", original, {"max": passport()}, synth=fixture_voice(calls), reuse_root=first)
    assert calls == [original.scenes[1].text]
    assert manifest["reuse"]["audio"] == 2


def test_music_reused_and_subtitles_rebuilt_for_reordered_scenes(tmp_path):
    calls = []
    original = script()
    original.scenes[1].text += " Серьёзно."
    first = tmp_path / "first"
    build_package(first, original, {"max": passport()}, synth=fixture_voice(calls))
    revised = original.model_copy(deep=True)
    revised.scenes.reverse()
    revised.scenes[0], revised.scenes[1] = revised.scenes[1], revised.scenes[0]
    calls.clear()
    manifest = build_package(tmp_path / "revision", revised, {"max": passport()}, synth=fixture_voice(calls), reuse_root=first)
    assert not calls
    assert manifest["reuse"] == {"images": 3, "audio": 3, "music": 1}
    assert (first / "subtitles.ass").read_bytes() != (tmp_path / "revision" / "subtitles.ass").read_bytes()


def test_previous_package_lookup_handles_intermediate_revision_and_cycle(tmp_path):
    from studio.db import Store
    from studio.production import Production
    store = Store(tmp_path)
    store.put("episode", "first", {"package": "original"})
    store.put("episode", "middle", {"previous_revision": "first"})
    service = Production(store)
    assert service.previous_package({"previous_revision": "middle"}) == tmp_path / "packages" / "original"
    store.put("episode", "cycle", {"previous_revision": "cycle"})
    assert service.previous_package({"previous_revision": "cycle"}) is None


@pytest.mark.skipif(os.getenv("STUDIO_RENDER_TEST") != "1", reason="Длительный реальный рендер включается STUDIO_RENDER_TEST=1")
def test_full_real_render_and_decode(tmp_path):
    def fixture_audio(text, path, model):
        import subprocess
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=24000", "-t", "10", str(path)], check=True)
        return 10.0
    folder = tmp_path/"render-package"
    manifest = build_package(folder, script(), {"max": passport()}, synth=fixture_audio)
    output = render(folder, manifest, "auto")
    report = technical_qc(output)
    assert report["pass"], report
    assert output.stat().st_size > 10000
