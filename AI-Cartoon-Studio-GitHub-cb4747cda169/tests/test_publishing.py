import asyncio

import httpx
import pytest

from studio.db import Store
from studio.media import sha256
from studio.publishing import youtube_publish


def setup_episode(tmp_path, monkeypatch):
    store = Store(tmp_path)
    (tmp_path/"results").mkdir()
    path = tmp_path/"results"/"fixture.mp4"
    path.write_bytes(b"test fixture, not a real video")
    digest = sha256(path)
    store.put("episode", "ep", {"status": "reviewed", "result": path.name, "qc": {"sha256": digest}})
    for k in ("YOUTUBE_ENABLED", "YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN"):
        monkeypatch.setenv(k, "true" if k == "YOUTUBE_ENABLED" else "test-credential")
    return store, digest


def test_youtube_private_readback_and_duplicate_guard(tmp_path, monkeypatch):
    store, digest = setup_episode(tmp_path, monkeypatch)
    requests = []
    def handle(request):
        requests.append(request)
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "fake"})
        if request.method == "POST":
            assert b'"privacyStatus":"private"' in request.content
            return httpx.Response(200, headers={"location": "https://www.googleapis.com/upload/test-session"})
        if request.method == "PUT":
            return httpx.Response(200, json={"id": "test-video"})
        return httpx.Response(200, json={"items": [{"id": "test-video", "status": {"privacyStatus": "private"}}]})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handle), **kw))
    result = asyncio.run(youtube_publish(store, "ep", digest, "Тест", "Описание"))
    assert result["status"] == "confirmed_private"
    count = len(requests)
    with pytest.raises(ValueError):
        asyncio.run(youtube_publish(store, "ep", digest, "Тест", "Описание"))
    assert len(requests) == count


def test_youtube_ambiguous_upload_no_automatic_retry(tmp_path, monkeypatch):
    store, digest = setup_episode(tmp_path, monkeypatch)
    def handle(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "fake"})
        if request.method == "POST":
            return httpx.Response(200, headers={"location": "https://www.googleapis.com/upload/test-session"})
        raise httpx.ReadTimeout("test disconnect")
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handle), **kw))
    with pytest.raises(ValueError, match="сверки"):
        asyncio.run(youtube_publish(store, "ep", digest, "Тест", "Описание"))
    assert store.get("publication", "ep")["status"] == "needs_reconciliation"
    with pytest.raises(ValueError, match="уже начата"):
        asyncio.run(youtube_publish(store, "ep", digest, "Тест", "Описание"))
