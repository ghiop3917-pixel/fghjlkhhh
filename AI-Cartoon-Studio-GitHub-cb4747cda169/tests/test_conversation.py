import asyncio
import json

import pytest

from studio.agents import Router
from studio.analytics import Comments, summarize_comments
from studio.conversation import apply_proposal, converse
from studio.db import Store


def test_natural_language_proposal_explicit_apply_idempotent(tmp_path, monkeypatch):
    store = Store(tmp_path)
    store.put("story", "main", {"next_plan": "Ссора"})
    async def answer(*args, **kwargs):
        return json.dumps({"answer": "Предлагаю примирение", "changes": [{"kind": "object", "category": "story", "id": "main", "body": {"next_plan": "Примирение"}}]})
    monkeypatch.setattr(Router, "ask", answer)
    result = asyncio.run(converse(store, "Помири друзей"))
    assert store.get("story", "main")["next_plan"] == "Ссора"
    assert result["proposal"]
    first = apply_proposal(store, result["proposal"])
    assert store.get("story", "main")["next_plan"] == "Примирение"
    assert apply_proposal(store, result["proposal"]) == first
    assert store.objects("story")[0]["revision"] == 2


def test_proposal_conflict_rolls_back_all_changes(tmp_path, monkeypatch):
    store = Store(tmp_path)
    store.put("story", "main", {"next_plan": "Ссора"})
    async def answer(*args, **kwargs):
        return json.dumps({"answer": "Правка", "changes": [{"kind": "object", "category": "preferences", "id": "owner", "body": {"style": "драма"}}, {"kind": "object", "category": "story", "id": "main", "body": {"next_plan": "Примирение"}}]})
    monkeypatch.setattr(Router, "ask", answer)
    result = asyncio.run(converse(store, "Измени"))
    store.put("story", "main", {"next_plan": "Новая ссора"})
    with pytest.raises(ValueError):
        apply_proposal(store, result["proposal"])
    assert store.get("preferences", "owner") is None
    assert store.get("proposal", result["proposal"])["status"] == "pending"


def test_raw_comments_not_stored(tmp_path, monkeypatch):
    store = Store(tmp_path)
    raw = "уникальный_исходный_комментарий_не_сохранять"
    async def answer(*args, **kwargs):
        return "Зрители хотят больше диалогов друзей"
    monkeypatch.setattr(Router, "ask", answer)
    result = asyncio.run(summarize_comments(store, Comments(comments=[raw])))
    assert result["raw_comments_stored"] is False
    with store.connect() as db:
        objects = str([tuple(r) for r in db.execute("SELECT * FROM objects")])
        history = str([tuple(r) for r in db.execute("SELECT * FROM history")])
        events = str([tuple(r) for r in db.execute("SELECT * FROM events")])
    assert raw not in objects+history+events
