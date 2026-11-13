import asyncio
import hashlib
import hmac
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from studio.agents import Router, Unavailable
from studio.api import create_app, telegram_owner
from studio.config import Settings
from studio.db import Store
from studio.media import safe_file, validate_manifest
from studio.production import Production, approve_topic


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path)


@pytest.fixture
def client(store):
    settings = Settings(data=store.root, api_token="a"*40, worker_token="b"*40)
    return TestClient(create_app(settings, store))


def test_queue_concurrent_claim_and_dedupe(store):
    id = store.enqueue("render", {"test": 1}, "same")
    assert store.enqueue("render", {"test": 2}, "same") == id
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda n: store.claim(["render"], str(n)), range(8)))
    assert sum(c is not None for c in claims) == 1
    assert claims[next(i for i,c in enumerate(claims) if c)]["attempts"] == 1


def test_lease_fencing_restart_and_retry_limit(store):
    id = store.enqueue("render", {}, "task")
    first = store.claim(["render"], "pc")
    with store.connect() as db:
        db.execute("UPDATE tasks SET lease_until=0 WHERE id=?", (id,))
    restarted = Store(store.root)
    second = restarted.claim(["render"], "pc2")
    assert second["lease_token"] != first["lease_token"]
    with pytest.raises(ValueError):
        store.finish(id, first["lease_token"], result={"wrong": True})
    store.finish(id, second["lease_token"], error="broken")
    with store.connect() as db:
        db.execute("UPDATE tasks SET available=0 WHERE id=?", (id,))
    third = store.claim(["render"], "pc")
    store.finish(id, third["lease_token"], error="broken")
    assert store.task(id)["status"] == "failed"
    assert store.claim(["render"], "pc") is None


def test_wait_for_provider_does_not_exhaust_attempts(store):
    id = store.enqueue("script", {}, "wait")
    t = store.claim(["script"], "server")
    store.finish(id, t["lease_token"], error="no model", wait=True)
    assert store.task(id)["attempts"] == 0
    assert store.task(id)["status"] == "pending"


def test_revision_restore_and_conflict(store):
    store.put("character", "max", {"name": "Макс"}, expected=0)
    store.put("character", "max", {"name": "Марк"}, expected=1)
    with pytest.raises(ValueError):
        store.put("character", "max", {}, expected=1)
    assert store.restore("character", "max", 1) == 3
    assert store.get("character", "max")["name"] == "Макс"


def test_backup_live_wal_and_archive_shape(store):
    store.put("story", "main", {"conflict": "Долг"})
    backup = store.backup()
    with sqlite3.connect(backup) as db:
        assert db.execute("SELECT count(*) FROM objects").fetchone()[0] == 1
        columns = [r[1] for r in db.execute("PRAGMA table_info(archive)")]
    assert columns == ["season_title", "episode_title", "full_script"]


def test_api_auth_separation_and_mode(client, store):
    assert client.get("/health").status_code == 200
    assert client.get("/api/dashboard").status_code == 401
    assert client.get("/api/dashboard", headers={"Authorization": "Bearer "+"b"*40}).status_code == 401
    owner = {"Authorization": "Bearer "+"a"*40}
    worker = {"Authorization": "Bearer "+"b"*40}
    assert client.post("/worker/claim", headers=owner, json={"worker_id": "pc"}).status_code == 401
    store.enqueue("render", {}, "render")
    client.post("/api/mode/VPS", headers=owner)
    assert client.post("/worker/claim", headers=worker, json={"worker_id": "pc"}).json() is None
    client.post("/api/mode/AUTO", headers=owner)
    assert client.post("/worker/claim", headers=worker, json={"worker_id": "pc"}).json()["id"]


def test_server_never_claims_render(store):
    store.enqueue("render", {}, "render")
    assert asyncio.run(Production(store).tick()) is False
    assert store.tasks()[0]["status"] == "pending"


def test_no_model_does_not_invent_result(store, monkeypatch):
    monkeypatch.setenv("STUDIO_CLOUD_ENABLED", "false")
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    with pytest.raises(Unavailable):
        asyncio.run(Router(store).ask("writer", "Придумай сюжет"))


def test_quota_is_atomic(store):
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: store.quota("model", "today", 3), range(20)))
    assert sum(results) == 3


def test_telegram_signature_owner_expiry():
    def signed(user=42, age=0):
        data = {"auth_date": str(int(time.time())-age), "user": json.dumps({"id": user}), "query_id": "test"}
        key = hmac.new(b"WebAppData", b"secret", hashlib.sha256).digest()
        data["hash"] = hmac.new(key, "\n".join(f"{k}={v}" for k,v in sorted(data.items())).encode(), hashlib.sha256).hexdigest()
        return urlencode(data)
    telegram_owner(signed(), "secret", "42")
    with pytest.raises(ValueError):
        telegram_owner(signed(user=43), "secret", "42")
    with pytest.raises(ValueError):
        telegram_owner(signed(age=1000), "secret", "42")
    with pytest.raises(ValueError):
        telegram_owner(signed()+"&auth_date=1", "secret", "42")


@pytest.mark.parametrize("name", ["../.env", "/etc/passwd", "C:\\secret", "foo/../../secret", "a\\..\\b"])
def test_path_traversal_blocked(store, name):
    with pytest.raises(ValueError):
        safe_file(store.root, name)


def test_topic_approval_idempotent(store):
    store.put("trend", "topic", {"title": "Тема", "status": "proposed"})
    assert approve_topic(store, "topic") == approve_topic(store, "topic")
    assert len(store.tasks()) == 1


def test_upload_requires_active_lease(client, store):
    worker = {"Authorization": "Bearer "+"b"*40}
    id = store.enqueue("render", {"episode": "ep", "package": "pkg"}, "job")
    lease = store.claim(["render"], "pc")["lease_token"]
    response = client.put(f"/worker/{id}/result?lease=wrong", headers=worker, content=b"x"*2000)
    assert response.status_code == 409
    response = client.put(f"/worker/{id}/result?lease={lease}", headers=worker, content=b"x"*2000)
    assert response.status_code == 200
    assert store.task(id)["status"] == "done"
    assert client.put(f"/worker/{id}/result?lease={lease}", headers=worker, content=b"x"*2000).status_code == 409
    assert len([t for t in store.tasks() if t["kind"] == "qc"]) == 1


def test_qc_recovery_gap(store):
    id = store.enqueue("render", {"episode": "ep", "package": "pkg"}, "recover")
    t = store.claim(["render"], "pc")
    store.finish(id, t["lease_token"], result={"filename": "video.mp4"})
    service = Production(store)
    service.schedule()
    service.schedule()
    assert len([t for t in store.tasks() if t["kind"] == "qc"]) == 1


def test_materials_recovery_does_not_enqueue_second_render(store):
    store.put("episode", "ep", {"status": "materials_approved"})
    id = store.enqueue("materials", {"episode": "ep"}, "materials-recovery")
    task = store.claim(["materials"], "server")
    render_id = store.enqueue("render", {"episode": "ep", "package": "preserved"}, "render-materials:"+id)
    result = asyncio.run(Production(store).handle(task))
    assert result["recovered"] is True
    assert store.get("episode", "ep")["render_task"] == render_id
    assert len([t for t in store.tasks() if t["kind"] == "render"]) == 1


def test_publication_and_cleanup_default_off(client):
    owner = {"Authorization": "Bearer "+"a"*40}
    r = client.post("/api/episodes/missing/youtube", headers=owner, json={"sha256": "0"*64, "title": "Тест", "description": "", "confirm_private_upload": True})
    assert r.status_code == 400
    assert "отключена" in r.json()["detail"]


def test_invalid_manifest_rejected():
    with pytest.raises(ValueError):
        validate_manifest({"version": 1, "width": 1920, "height": 1080})


def test_static_panel_no_auth_secrets(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Обзор студии" in r.text
    assert "a"*40 not in r.text
