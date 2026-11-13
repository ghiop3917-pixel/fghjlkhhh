"""Предлагаемые правки обычным языком, проверяемый diff и явное применение владельцем."""
import secrets

from pydantic import BaseModel, Field

from studio.agents import Router, parse_json
from studio.emergencies import series_for
from studio.schemas import Character, Edit, Script


class Proposal(BaseModel):
    answer: str = Field(max_length=6000)
    changes: list[dict] = Field(default_factory=list, max_length=5)


async def converse(store, text, role="producer", series="main"):
    episodes = store.objects("episode")[:2]
    prompt = ("Запрос владельца: " + text + "\nПоследние серии (если неоднозначно, спроси ID): " +
              str([{"id": r["id"], "status": r["body"]["status"], "script": r["body"]["script"]} for r in episodes]) +
              "\nВерни JSON {\"answer\":\"ответ по-русски\",\"changes\":[]}. "
              "Если пользователь просит правку, предложи полные новые объекты в changes. "
              "Формат правки паспорта/памяти: {\"kind\":\"object\",\"category\":\"character|story|preferences|rules|series\",\"id\":\"ID\",\"body\":{...}}. "
              "Формат правки сценария: {\"kind\":\"episode\",\"id\":\"существующий ID\",\"script\":{...полный сценарий...}}. "
              "Ничего не удаляй. Для внешних действий, оплаты, публикации changes=[]; объясни нужное согласование. "
              "Не выдумывай существующие ID. Новые объекты допустимы только если владелец просил их создать. "
              "Не включай исходные внешние комментарии в ответ. Применение правок ещё НЕ произошло.")
    result = await Router(store).ask(role, prompt, series, True)
    try:
        response = Proposal.model_validate(parse_json(result))
    except ValueError:
        return {"answer": result[:6000], "changes_applied": False, "proposal": None}
    changes = []
    for change in response.changes:
        if change.get("kind") == "object":
            category, id = change.get("category"), change.get("id")
            rows = store.objects(category) if category in ("character", "story", "preferences", "rules", "series") else []
            revision = next((r["revision"] for r in rows if r["id"] == id), 0)
            edit = Edit(category=category, id=id, body=change.get("body", {}), expected_revision=revision)
            if category == "character":
                Character.model_validate(edit.body)
            before = store.get(category, id, {})
            changes.append({"kind": "object", **edit.model_dump(), "before": before})
        elif change.get("kind") == "episode":
            id = change.get("id")
            old = store.get("episode", id)
            if not old:
                raise ValueError("Агент предложил правку неизвестной серии")
            script = Script.model_validate(change.get("script"))
            changes.append({"kind": "episode", "id": id, "script": script.model_dump(), "before": old["script"], "series": series_for(script, old["series"]), "creative_brief": old.get("creative_brief")})
        else:
            raise ValueError("Неподдерживаемая правка от агента")
    proposal_id = None
    if changes:
        proposal_id = secrets.token_hex(12)
        store.put("proposal", proposal_id, {"status": "pending", "changes": changes})
    return {"answer": response.answer, "changes_applied": False, "proposal": proposal_id, "changes": changes}


def apply_proposal(store, id):
    proposal = store.get("proposal", id)
    if not proposal:
        raise ValueError("Предложение не найдено")
    if proposal["status"] == "applied":
        return proposal["results"]
    changes = proposal["changes"]
    # Единая транзакция всех объектов, эпизодов и статуса предложения.
    # Store.put использует отдельное соединение, поэтому здесь пишем через тот же db.
    import time

    from studio.db import encode
    results = []
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT body FROM objects WHERE category='proposal' AND id=?", (id,)).fetchone()
        import json
        current = json.loads(row[0])
        if current["status"] == "applied":
            return current["results"]
        def write(category, object_id, body, expected=None):
            row = db.execute("SELECT revision FROM objects WHERE category=? AND id=?", (category, object_id)).fetchone()
            revision = row[0] if row else 0
            if expected is not None and expected != revision:
                raise ValueError("Данные изменились после предложения; запросите новую правку")
            now = time.time()
            db.execute("INSERT INTO history(category,id,revision,body,created) VALUES(?,?,?,?,?)", (category, object_id, revision+1, encode(body), now))
            db.execute("INSERT INTO objects VALUES(?,?,?,?,?) ON CONFLICT(category,id) DO UPDATE SET revision=excluded.revision,body=excluded.body,updated=excluded.updated", (category, object_id, revision+1, encode(body), now))
            return revision+1
        for change in changes:
            if change["kind"] == "object":
                revision = write(change["category"], change["id"], change["body"], change["expected_revision"])
                results.append({"id": change["id"], "revision": revision})
            else:
                row = db.execute("SELECT body FROM objects WHERE category='episode' AND id=?", (change["id"],)).fetchone()
                old = json.loads(row[0]) if row else {}
                if old.get("script") != change["before"]:
                    raise ValueError("Сценарий изменился после предложения")
                new_id = secrets.token_hex(12)
                write("episode", new_id, {"script": change["script"], "series": change["series"], "status": "awaiting_script_approval", "previous_revision": change["id"], "creative_brief": change.get("creative_brief")}, 0)
                results.append({"episode": new_id})
        proposal.update(status="applied", results=results)
        write("proposal", id, proposal)
    store.event("owner.proposal_applied", {"proposal": id, "results": results})
    return results
