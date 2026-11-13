import asyncio
import json

import httpx

from studio.agents import Router
from studio.db import Store


def test_current_cloudflare_model_schema_and_json_mode(tmp_path, monkeypatch):
    for key,value in {"STUDIO_CLOUD_ENABLED": "true", "CF_FREE_ACCOUNT_CONFIRMED": "true", "CF_ACCOUNT_ID": "a"*32, "CF_API_TOKEN": "fixture-key", "CF_MODEL": "@cf/qwen/qwen3.8-27b"}.items():
        monkeypatch.setenv(key, value)
    requests = []
    def handle(request):
        requests.append(request)
        body = json.loads(request.content)
        assert body["reasoning_effort"] == "low"
        assert body["max_completion_tokens"] == 2400
        assert body["response_format"] == {"type": "json_object"}
        assert str(request.url).endswith("@cf/qwen/qwen3.8-27b")
        return httpx.Response(200, json={"success": True, "result": {"choices": [{"message": {"content": '{"test":"ok"}'}}]}})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs))
    answer = asyncio.run(Router(Store(tmp_path)).ask("producer", "Верни тестовый JSON", json_mode=True))
    assert json.loads(answer)["test"] == "ok" and len(requests) == 1
