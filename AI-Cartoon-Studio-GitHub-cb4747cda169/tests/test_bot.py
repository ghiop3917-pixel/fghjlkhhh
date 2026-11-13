import asyncio
import time

import httpx
import pytest

from studio.bot import Bot
from studio.config import Settings
from studio.db import Store


def test_notification_retries_transport_failure_and_stops_after_success(tmp_path, monkeypatch):
    store = Store(tmp_path)
    store.put("trend", "topic", {"status": "proposed", "title": "Тест", "url": "https://example.org"})
    bot = Bot(store, Settings(data=tmp_path))
    calls = []
    async def send(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("тестовый обрыв")
    monkeypatch.setattr(bot, "send", send)
    with pytest.raises(httpx.ConnectError):
        asyncio.run(bot.notifications(None))
    asyncio.run(bot.notifications(None))
    asyncio.run(bot.notifications(None))
    assert len(calls) == 2
    assert store.get("notification", "topic:topic")["status"] == "sent"


def test_render_waits_ten_minutes_after_delivered_warning(tmp_path, monkeypatch):
    store=Store(tmp_path)
    store.put("worker", "latest", {"last_seen": time.time()})
    store.put("episode", "ep", {"script": {"episode_title": "Один на острове"}})
    task=store.enqueue("render", {"episode": "ep", "package": "pkg"}, "render-test")
    bot=Bot(store, Settings(data=tmp_path))
    messages=[]
    async def send(client,text,*args):
        messages.append(text)
    monkeypatch.setattr(bot,"send",send)
    assert store.claim(["render"],"pc",require_render_notice=True) is None
    asyncio.run(bot.render_warning(None))
    asyncio.run(bot.render_warning(None))
    assert len(messages)==1 and "Через 10 минут" in messages[0] and "Один на острове" in messages[0]
    assert store.claim(["render"],"pc",require_render_notice=True) is None
    notice=store.get("render_warning",task)
    monkeypatch.setattr("studio.db.time.time",lambda:notice["ready_at"]+1)
    assert store.claim(["render"],"pc",require_render_notice=True)["id"]==task


def test_undelivered_warning_retries_and_does_not_release_render(tmp_path,monkeypatch):
    store=Store(tmp_path)
    store.put("worker","latest",{"last_seen":time.time()})
    task=store.enqueue("render",{"episode":"ep","package":"pkg"},"test")
    bot=Bot(store,Settings(data=tmp_path))
    async def failed(*args):
        raise httpx.ConnectError("Нет Telegram")
    monkeypatch.setattr(bot,"send",failed)
    with pytest.raises(httpx.ConnectError):
        asyncio.run(bot.render_warning(None))
    assert store.get("render_warning",task) is None
    assert store.claim(["render"],"pc",require_render_notice=True) is None


def test_offline_worker_does_not_trigger_warning(tmp_path,monkeypatch):
    store=Store(tmp_path)
    store.enqueue("render",{"episode":"ep","package":"pkg"},"test")
    bot=Bot(store,Settings(data=tmp_path))
    async def unexpected(*args):
        pytest.fail("Выключенный ПК не должен обещать скорый рендер")
    monkeypatch.setattr(bot,"send",unexpected)
    asyncio.run(bot.render_warning(None))
