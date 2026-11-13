import json

from fastapi.testclient import TestClient

from studio.api import create_app
from studio.config import Settings
from studio.db import Store
from studio.readiness import readiness, text_report


def settings(tmp_path):
    return Settings(data=tmp_path, api_token="owner-secret-value"*3, worker_token="worker-secret-value"*3, server_enabled=False, owner_id="", bot_token="")


def test_worker_report_is_offline_and_does_not_require_owner_or_model(tmp_path, monkeypatch):
    monkeypatch.setattr("studio.readiness.platform.system", lambda: "Windows")
    monkeypatch.setattr("studio.readiness.shutil.which", lambda _: "found")
    cfg = settings(tmp_path)
    report = readiness(cfg, "worker", env={"STUDIO_WORKER_TOKEN": cfg.worker_token, "STUDIO_API_URL": "https://studio.example.net"})
    assert report["configuration_ready"] is True
    assert report["network_requests"] == 0 and report["live_verified"] is False
    assert "models" not in {c["id"] for c in report["checks"]}
    text = json.dumps(report)+text_report(report)
    assert cfg.api_token not in text and cfg.worker_token not in text


def test_server_missing_providers_and_voice_are_explicit(tmp_path, monkeypatch):
    monkeypatch.setattr("studio.readiness.shutil.which", lambda _: None)
    report = readiness(settings(tmp_path), "server", env={})
    checks = {c["id"]: c for c in report["checks"]}
    assert checks["models"]["status"] == "missing"
    assert checks["voice"]["status"] == "missing"
    assert checks["telegram"]["status"] == "missing"
    assert not report["configuration_ready"]


def test_deployment_maps_voice_pair_without_creating_files(tmp_path):
    voices = tmp_path/"voices"
    voices.mkdir()
    (voices/"ru.onnx").write_bytes(b"fixture model")
    (voices/"ru.onnx.json").write_text("{}")
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    env = {"PIPER_MODEL": "/app/voices/ru.onnx", "OLLAMA_MODEL": "test", "STUDIO_PUBLIC_URL": "https://studio.example.net", "STUDIO_DOMAIN": "wrong.example.net"}
    report = readiness(settings(tmp_path), "deployment", env=env, root=tmp_path)
    checks = {c["id"]: c for c in report["checks"]}
    assert checks["voice"]["status"] == "ok"
    assert checks["ollama.container"]["status"] == "missing"
    assert checks["public.domain"]["status"] == "missing"
    assert before == sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))


def test_diagnostic_does_not_echo_cloud_or_telegram_credentials(tmp_path):
    env = {"STUDIO_CLOUD_ENABLED": "true", "CF_FREE_ACCOUNT_CONFIRMED": "true", "CF_ACCOUNT_ID": "account-secret", "CF_API_TOKEN": "cf-secret", "TELEGRAM_BOT_TOKEN": "tg-secret", "TELEGRAM_OWNER_ID": "123"}
    report = readiness(settings(tmp_path), "server", env=env)
    output = json.dumps(report)+text_report(report)
    assert all(secret not in output for secret in ("cf-secret", "tg-secret", "account-secret"))
    assert next(c for c in report["checks"] if c["id"] == "models")["status"] == "ok"
    assert report["live_verified"] is False


def test_diagnostic_api_is_owner_only(tmp_path):
    cfg = settings(tmp_path)
    client = TestClient(create_app(cfg, Store(tmp_path)))
    assert client.get("/api/readiness").status_code == 401
    r = client.get("/api/readiness", headers={"Authorization": "Bearer "+cfg.api_token})
    assert r.status_code == 200 and r.json()["live_verified"] is False
    assert cfg.api_token not in r.text and cfg.worker_token not in r.text


def test_invalid_worker_port_and_encoder_are_not_ready(tmp_path, monkeypatch):
    monkeypatch.setattr("studio.readiness.platform.system", lambda: "Windows")
    monkeypatch.setattr("studio.readiness.shutil.which", lambda _: "found")
    report = readiness(settings(tmp_path), "worker", env={"STUDIO_API_URL": "https://studio.example.net:bad", "STUDIO_ENCODER": "unknown"})
    checks = {c["id"]: c for c in report["checks"]}
    assert checks["worker.url"]["status"] == "missing"
    assert checks["encoder.mode"]["status"] == "missing"


def test_credentials_in_url_are_rejected_without_echo(tmp_path):
    report = readiness(settings(tmp_path), "worker", env={"STUDIO_API_URL": "https://user:private-password@studio.example.net"})
    assert "private-password" not in json.dumps(report)
    assert next(c for c in report["checks"] if c["id"] == "worker.url")["status"] == "missing"
