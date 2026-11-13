import json
import zipfile
from pathlib import Path

import pytest
from dotenv import dotenv_values

from studio.agents import Unavailable
from studio.db import Store
from studio.deployment import create_server_config
from studio.distribution import build_bundle
from studio.seed import seed
from studio.speech import speech_command
from studio.worker_setup import connect_worker


def server_values():
    return {"STUDIO_DOMAIN": "studio.testserver.net", "TELEGRAM_OWNER_ID": "123", "TELEGRAM_BOT_TOKEN": "123456789:"+"b"*35, "CF_ACCOUNT_ID": "a"*32, "CF_API_TOKEN": "cloudflare-test-key-not-real", "CF_FREE_ACCOUNT_CONFIRMED": "true"}


def test_setup_does_not_overwrite_and_pairing_contains_only_worker_access(tmp_path):
    (tmp_path/".env.example").write_text("STUDIO_API_TOKEN=\nSTUDIO_WORKER_TOKEN=\n")
    assert create_server_config(tmp_path, server_values())
    config = dotenv_values(tmp_path/".env")
    pair = dotenv_values(tmp_path/"worker-connection.env")
    assert pair["STUDIO_WORKER_TOKEN"] == config["STUDIO_WORKER_TOKEN"]
    assert len(config["STUDIO_API_TOKEN"]) == 64 and config["STUDIO_API_TOKEN"] != config["STUDIO_WORKER_TOKEN"]
    assert "TELEGRAM_BOT_TOKEN" not in pair and "CF_API_TOKEN" not in pair and "STUDIO_API_TOKEN" not in pair
    original = (tmp_path/".env").read_bytes()
    assert not create_server_config(tmp_path, {})
    assert (tmp_path/".env").read_bytes() == original


def test_setup_rejects_unconfirmed_free_account_before_writing(tmp_path):
    (tmp_path/".env.example").write_text("KEY=\n")
    values = server_values() | {"CF_FREE_ACCOUNT_CONFIRMED": "false"}
    with pytest.raises(ValueError):
        create_server_config(tmp_path, values)
    assert not (tmp_path/".env").exists()


def minimal_project(root):
    for name in ("compose.yaml", "pyproject.toml", ".env.example", "studio/worker.py", "deploy/start.py", "README.md"):
        path = root/name
        path.parent.mkdir(exist_ok=True, parents=True)
        path.write_text("test fixture", encoding="utf-8")


def test_bundle_excludes_private_files_and_checks_all_hashes(tmp_path):
    root = tmp_path/"project"
    minimal_project(root)
    (root/".env").write_text("STUDIO_API_TOKEN=private-secret-that-is-not-in-source")
    (root/"worker-connection.env").write_text("private")
    (root/"studio"/"__pycache__").mkdir()
    (root/"studio"/"__pycache__"/"private.pyc").write_bytes(b"private")
    archive = build_bundle(root, tmp_path/"output")
    with zipfile.ZipFile(archive) as z:
        names = z.namelist()
        assert ".env" not in names and "worker-connection.env" not in names
        assert not any("__pycache__" in n for n in names)
        manifest = json.loads(z.read("BUNDLE_MANIFEST.json"))
        assert "deploy/start.py" in manifest["files"]
        assert (z.getinfo("studio/worker.py").external_attr >> 16) & 0o777 == 0o644
    assert archive.with_suffix(".sha256").is_file()


def test_bundle_refuses_live_key_pasted_in_document(tmp_path):
    minimal_project(tmp_path)
    (tmp_path/".env").write_text("STUDIO_WORKER_TOKEN=secret-value-that-must-not-leak")
    (tmp_path/"README.md").write_text("secret-value-that-must-not-leak")
    with pytest.raises(ValueError, match="секрета"):
        build_bundle(tmp_path, tmp_path/"output")
    assert not (tmp_path/"output").exists()


def test_seed_preserves_existing_character(tmp_path):
    store = Store(tmp_path)
    assert seed(store)["created"]
    original = store.get("character", "max")
    assert not seed(store)["created"]
    assert store.get("character", "max") == original


def test_reserve_voice_is_explicit_and_does_not_replace_selected_model(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_TTS_FALLBACK", "espeak")
    monkeypatch.setattr("studio.speech.shutil.which", lambda _: "found")
    command = speech_command("", Path("test.wav"))
    assert "--stdin" in command and "ru" in command
    with pytest.raises(Unavailable, match="не заменяется"):
        speech_command(str(tmp_path/"missing.onnx"), Path("test.wav"))


def test_worker_connection_preserves_config_and_backup(tmp_path):
    current = b"# user settings\nCUSTOM=value\nSTUDIO_WORKER_TOKEN=old\nSTUDIO_SERVER_ENABLED=true\n"
    (tmp_path/".env").write_bytes(current)
    pair = tmp_path/"worker-connection.env"
    pair.write_text("STUDIO_API_URL=https://studio.testserver.net\nSTUDIO_WORKER_TOKEN="+"x"*40+"\nSTUDIO_SERVER_ENABLED=false\n")
    assert connect_worker(tmp_path, pair)["applied"] is False
    assert (tmp_path/".env").read_bytes() == current
    result = connect_worker(tmp_path, pair, apply=True)
    assert (tmp_path/result["backup"]).read_bytes() == current
    config = dotenv_values(tmp_path/".env")
    assert config["CUSTOM"] == "value" and config["STUDIO_SERVER_ENABLED"] == "false"
    assert config["STUDIO_WORKER_TOKEN"] == "x"*40


def test_worker_connection_refuses_server_credentials(tmp_path):
    pair = tmp_path/"pair.env"
    pair.write_text("STUDIO_API_TOKEN="+"x"*40)
    with pytest.raises(ValueError, match="посторонние"):
        connect_worker(tmp_path, pair, apply=True)
    assert not (tmp_path/".env").exists()
