import asyncio
import json
import os
import subprocess
import uuid
from datetime import datetime, timezone

from studio.agents import Router, Unavailable, parse_json
from studio.config import flag
from studio.db import Store
from studio.emergencies import (
    get_topics,
    invent_ideas,
    request_ideas,
    require_review,
    validate_advice,
)
from studio.emergencies import instruction as emergency_instruction
from studio.media import (
    SpeechDurationError,
    build_package,
    safe_file,
    sha256,
    technical_qc,
)
from studio.publication_plans import dispatch_plan
from studio.schemas import Character, Script
from studio.speech import speech_command
from studio.story_ideas import (
    WRITER_INSTRUCTION,
    invent_story_ideas,
    request_story_ideas,
)
from studio.trends import collect

SCRIPT_INSTRUCTION = """Создай следующую серию. Верни только JSON со структурой:
{"season_title":"...","episode_title":"...","synopsis":"...",
"scenes":[{"character_id":"id существующего персонажа","text":"реплика","emotion":"neutral|angry|happy","background":"#182338"}],
"next_plan":"...","unresolved":["..."]}.
3–12 сцен, всего 55–145 русских слов, 30–60 секунд; история продолжается 2–4 дня.
В каждой сцене укажи setting из street/island/forest/interior/shelter/station/desert/space/rooftop,
camera из push_in/pull_out/pan_left/pan_right/static и description с описанием декорации.
Это варианты постановки, а не ограничение возможных сюжетов. Не помещай остров в городскую улицу.
Используй только character_id из контекста. Новость — вдохновение для вымысла,
не выдавай вымышленный криминальный сюжет о реальных людях за факт.
Если персонажей нет, не выдумывай id: попроси добавить паспорта."""


class Production:
    def __init__(self, store: Store):
        self.store = store
        self.router = Router(store)

    async def handle(self, task):
        payload = task["payload"]
        if task["kind"] == "emergency_ideas":
            return await invent_ideas(self.store, self.router)
        if task["kind"] == "story_ideas":
            from studio.memes import refresh_memes
            await refresh_memes(self.store)
            return await invent_story_ideas(self.store, self.router)
        if task["kind"] == "memes":
            from studio.memes import refresh_memes
            return await refresh_memes(self.store)
        if task["kind"] == "publication":
            return await dispatch_plan(self.store, payload["plan"])
        if task["kind"] == "publication_readback":
            from studio.publishing import youtube_readback
            return await youtube_readback(self.store, payload["episode"])
        if task["kind"] == "cleanup":
            from studio.retention import execute_cleanup
            return await execute_cleanup(self.store, payload["consent"])
        if task["kind"] == "script":
            episode_id = payload.get("episode", task["id"])
            existing = self.store.get("episode", episode_id)
            if existing:
                return {"episode": episode_id, "status": existing["status"]}
            if not self.store.objects("character"):
                raise Unavailable("Добавьте хотя бы один паспорт персонажа")
            series = payload.get("series", "main")
            previous = self.store.get("story", series, {}).get("last_episode")
            if previous and flag("YOUTUBE_ANALYTICS_ENABLED") and self.store.get("publication", previous, {}).get("video_id"):
                from studio.platform_analytics import youtube_sync
                await youtube_sync(self.store, previous)
            instruction = SCRIPT_INSTRUCTION + "\nТема: " + payload["topic"]
            if payload.get("format") == "emergency_story":
                instruction += emergency_instruction(payload["emergency_topic"], self.store)
            elif payload.get("format") == "what_if_story":
                instruction += WRITER_INSTRUCTION
            if task.get("error"):
                instruction += "\nИсправь дефект предыдущей попытки: " + task["error"][:1000]
            text = await self.router.ask("writer", instruction, series, True)
            script = Script.model_validate(parse_json(text))
            if payload.get("format") == "what_if_story" and script.format != "what_if_story":
                raise ValueError("Сценарист изменил выбранный формат «А что, если…»")
            if payload.get("format") == "emergency_story" and (script.format != "emergency_story" or script.emergency_topic != payload["emergency_topic"]):
                raise ValueError("Сценарист изменил выбранный учебный формат или тему ЧС")
            validate_advice(script, get_topics(self.store))
            known = {c["id"] for c in self.store.objects("character")}
            if any(s.character_id not in known for s in script.scenes):
                raise ValueError("Сценарий ссылается на неизвестного персонажа")
            # Проверка отдельной ролью не подменяет технический QC и просмотр владельца.
            review_prompt = "Проверь сюжет и возрастной формат. Верни JSON {\"pass\":true|false,\"issues\":[...]}.\n"
            if script.format == "emergency_story":
                review_prompt += emergency_instruction(script.emergency_topic, self.store) + "\nОсобенно проверь все реплики на опасные непроверенные советы.\n"
            elif script.format == "what_if_story":
                review_prompt += WRITER_INSTRUCTION + "\nПроверь соблюдение исходного допущения: " + payload["topic"] + "\n"
            review = await self.router.ask("editor", review_prompt + script.model_dump_json(), payload.get("series", "main"), True)
            verdict = parse_json(review)
            if verdict.get("pass") is not True:
                raise ValueError("Редактор отклонил сюжет: " + json.dumps(verdict.get("issues", []), ensure_ascii=False)[:400])
            episode = payload.get("episode", task["id"])
            self.store.put("episode", episode, {"script": script.model_dump(), "series": payload.get("series", "main"), "status": "awaiting_script_approval", "editor_review": verdict, "creative_brief": payload.get("creative_brief")})
            return {"episode": episode, "status": "awaiting_script_approval"}
        if task["kind"] == "materials":
            episode = self.store.get("episode", payload["episode"])
            if not episode:
                raise ValueError("Серия не найдена")
            render_key = "render-materials:" + task["id"]
            with self.store.connect() as db:
                existing = db.execute("SELECT id,payload FROM tasks WHERE dedupe=?", (render_key,)).fetchone()
            if existing:
                package = json.loads(existing["payload"])["package"]
                if episode["status"] == "materials_approved":
                    episode.update(status="awaiting_pc", package=package, render_task=existing["id"])
                    self.store.put("episode", payload["episode"], episode)
                return {"package": package, "render_task": existing["id"], "recovered": True}
            if episode["status"] != "materials_approved":
                raise ValueError("Нет согласованного сценария")
            require_review(episode, self.store)
            script = Script.model_validate(episode["script"])
            characters = {r["id"]: Character.model_validate(r["body"]) for r in self.store.objects("character")}
            from pathlib import Path
            for scene in script.scenes:
                character = characters.get(scene.character_id)
                if not character:
                    raise ValueError("Не найден персонаж сценария")
                model = character.voice or os.getenv("PIPER_MODEL", "")
                speech_command(model, Path("preflight-unused.wav"))
            package_id = uuid.uuid4().hex
            root = self.store.root / "packages" / package_id
            root.parent.mkdir(exist_ok=True)
            reuse_root = safe_file(self.store.root/"packages", payload["reuse_package"]) if payload.get("reuse_package") else self.previous_package(episode)
            from studio.imagery import prepare_backgrounds
            backgrounds = await prepare_backgrounds(self.store, script)
            try:
                manifest = await asyncio.to_thread(build_package, root, script, characters, reuse_root=reuse_root, topics=get_topics(self.store), backgrounds=backgrounds, exclude_audio=payload.get("exclude_audio"))
            except SpeechDurationError as exc:
                from studio.quality import schedule_repair
                repair = schedule_repair(self.store, payload["episode"], {"package": {"checks": {"subtitle": {"pass": False, "issues": [str(exc)]}}}})
                if repair:
                    return {"quality_pass": False, "repair": repair, "note": "Исходные материалы сохранены; новая редакция потребует согласования"}
                raise
            # Проверяем актуальность: правка во время подготовки не должна попасть в рендер.
            latest = self.store.get("episode", payload["episode"])
            if latest != episode:
                raise ValueError("Серия изменилась во время подготовки; старый пакет сохранён")
            render_id = self.store.enqueue("render", {"episode": payload["episode"], "package": package_id}, render_key)
            episode.update(status="awaiting_pc", package=package_id, render_task=render_id, reuse=manifest["reuse"])
            self.store.put("episode", payload["episode"], episode)
            return {"package": package_id, "duration": manifest["duration"], "render_task": render_id, "reuse": manifest["reuse"]}
        if task["kind"] == "qc":
            path = self.store.root / "results" / payload["filename"]
            try:
                report = await asyncio.to_thread(technical_qc, path)
            except (OSError, ValueError, subprocess.SubprocessError):
                report = {"pass": False, "issues": ["Видео повреждено или не декодируется"], "scope": "technical_only"}
            episode = self.store.get("episode", payload["episode"])
            if episode.get("render_task") != payload["render_task"]:
                raise ValueError("QC относится к устаревшей версии серии")
            episode.update(status="awaiting_review" if report["pass"] else "qc_failed", qc=report, result=payload["filename"])
            from studio.quality import inspect_package, schedule_repair
            try:
                package_report = await asyncio.to_thread(inspect_package, self.store, episode)
            except (OSError, ValueError, KeyError):
                package_report = {"pass": False, "checks": {"visual": {"pass": False, "issues": ["Пакет повреждён или неполон"]}}}
            report["package"] = package_report
            report["pass"] = report["pass"] and package_report["pass"]
            if not report["pass"]:
                episode["status"] = "qc_failed"
            self.store.put("episode", payload["episode"], episode)
            if not report["pass"]:
                repair = schedule_repair(self.store, payload["episode"], report)
                if repair:
                    return {"quality_pass": False, "repair": repair}
                raise ValueError("QC остановлен: лимит исправлений исчерпан или нужна правка владельца")
            return report
        if task["kind"] == "script_revision":
            original = self.store.get("episode", payload["episode"])
            if not original:
                raise ValueError("Исходная серия для исправления не найдена")
            new_id = "repair-"+task["id"]
            if self.store.get("episode", new_id):
                return {"episode": new_id, "status": "awaiting_script_approval"}
            request = SCRIPT_INSTRUCTION + "\nИсправь только отмеченные дефекты, сохрани персонажей, формат и смысл.\nДефекты: " + json.dumps(payload["issues"], ensure_ascii=False) + "\nИсходный сценарий: " + json.dumps(original["script"], ensure_ascii=False)
            repaired = Script.model_validate(parse_json(await self.router.ask("editor", request, original["series"], True)))
            if repaired.format != original["script"].get("format", "drama"):
                raise ValueError("Исправление не должно менять формат серии")
            if repaired.emergency_topic != original["script"].get("emergency_topic"):
                raise ValueError("Исправление не должно менять тему памятки")
            original_cast = {s["character_id"] for s in original["script"]["scenes"]}
            if any(s.character_id not in original_cast for s in repaired.scenes):
                raise ValueError("Исправление не должно добавлять несогласованных персонажей")
            validate_advice(repaired, get_topics(self.store))
            self.store.put("episode", new_id, {"script": repaired.model_dump(), "series": original["series"], "status": "awaiting_script_approval", "previous_revision": payload["episode"], "creative_brief": original.get("creative_brief"), "repair_attempts": original.get("repair_attempts", 0)})
            self.store.put("episode", payload["episode"], original | {"status": "repaired_draft_created", "repaired_episode": new_id})
            return {"episode": new_id, "status": "awaiting_script_approval", "note": "Исправленный текст требует повторного согласования; исходник сохранён"}
        if task["kind"] == "trends":
            urls = [u.strip() for u in os.getenv("STUDIO_RSS_URLS", "").split(",") if u.strip()]
            topics = await collect(self.store, urls)
            from studio.trend_sources import youtube_trends
            try:
                topics += await youtube_trends(self.store)
            except Exception as exc:
                self.store.event("trend.youtube_unavailable", {"type": type(exc).__name__})
            return {"topics": topics, "note": "Оценка редакционной свежести; вирусность и конкуренция не измерены"}
        if task["kind"] == "weekly":
            from studio.analytics_summary import studio_summary
            data = self.store.objects("analytics")
            if not data:
                return {"status": "no_data", "message": "Измеренных данных пока нет"}
            summary = await self.router.ask("analytics", "Составь краткий недельный отчёт. Не делай причинных выводов из корреляций. Данные: " + json.dumps({"latest": data, "aggregate": studio_summary(self.store)}, ensure_ascii=False))
            self.store.put("report", payload["week"], {"summary": summary})
            return {"week": payload["week"]}
        raise ValueError("Неизвестный серверный тип задачи")

    def previous_package(self, episode):
        previous_id = episode.get("previous_revision")
        visited = set()
        # Промежуточная редакция могла не дойти до подготовки материалов.
        for _ in range(20):
            if not previous_id or previous_id in visited:
                return None
            visited.add(previous_id)
            previous = self.store.get("episode", previous_id, {})
            if previous.get("package"):
                return safe_file(self.store.root / "packages", previous["package"])
            previous_id = previous.get("previous_revision")
        return None

    async def tick(self):
        # Сервер никогда не получает render-задачи.
        task = self.store.claim(["script", "materials", "qc", "trends", "weekly", "emergency_ideas", "story_ideas", "memes", "publication", "publication_readback", "cleanup", "script_revision"], "vps-production")
        if task is None:
            return False
        async def pulse():
            while True:
                await asyncio.sleep(25)
                self.store.heartbeat(task["id"], task["lease_token"])
        heartbeat = asyncio.create_task(pulse())
        try:
            result = await self.handle(task)
            self.store.finish(task["id"], task["lease_token"], result=result)
        except Unavailable as exc:
            self.store.finish(task["id"], task["lease_token"], error=str(exc), wait=True)
        except Exception as exc:
            # Не сохраняем HTTP-заголовки, URL с ключами или полный ответ облачного сервиса.
            message = str(exc)[:500] if isinstance(exc, ValueError) else "Ошибка производства: " + type(exc).__name__
            self.store.finish(task["id"], task["lease_token"], error=message)
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
        return True

    def schedule(self):
        now = datetime.now(timezone.utc)
        backup_day = now.date().isoformat()
        if not self.store.get("backup_day", backup_day):
            backup = self.store.backup()
            self.store.put("backup_day", backup_day, {"filename": backup.name})
        # Закрываем окно сбоя между подтверждением render и постановкой QC.
        with self.store.connect() as db:
            completed = db.execute("SELECT id,payload,result FROM tasks WHERE kind='render' AND status='done'").fetchall()
        for row in completed:
            payload, result = json.loads(row["payload"]), json.loads(row["result"])
            self.store.enqueue("qc", {"filename": result["filename"], "episode": payload["episode"], "render_task": row["id"]}, "qc:"+row["id"])
        hour = self.store.get("settings", "schedule", {}).get("daily_hour_utc", int(os.getenv("STUDIO_DAILY_HOUR_UTC", "7")))
        from studio.comfort import local_now
        meme_time=local_now(self.store,now.timestamp())
        if flag('STUDIO_MEME_RADAR',True) and meme_time.hour>=13:
            self.store.enqueue('memes',{},'memes:'+meme_time.date().isoformat())
        if now.hour >= hour:
            self.store.enqueue("trends", {}, "trends:"+now.date().isoformat())
            if flag("STUDIO_STORY_AUTOPLAN", True):
                request_story_ideas(self.store, "story-ideas:"+now.date().isoformat())
            elif flag("STUDIO_EMERGENCY_AUTOPLAN", True):
                request_ideas(self.store, "emergency-ideas:"+now.date().isoformat())
        if now.weekday() == 0:
            week = now.strftime("%G-W%V")
            self.store.enqueue("weekly", {"week": week}, "weekly:"+week)
        if flag("YOUTUBE_ENABLED"):
            bucket = int(now.timestamp() // 900)
            for record in self.store.objects("publication"):
                if record["body"].get("video_id") and record["body"]["status"] in ("scheduled", "awaiting_readback", "needs_reconciliation"):
                    self.store.enqueue("publication_readback", {"episode": record["id"]}, f"readback:{record['id']}:{bucket}")

    async def run(self):
        while True:
            try:
                self.schedule()
                await self.tick()
            except Exception as exc:
                self.store.event("scheduler.error", {"type": type(exc).__name__})
            await asyncio.sleep(2)


def approve_topic(store, id):
    topic = store.get("trend", id)
    if not topic:
        raise ValueError("Тема не найдена")
    if topic.get("status") == "rejected":
        raise ValueError("Тема отклонена")
    # Dedupe сначала: повторный callback не запускает второй сценарий.
    payload = {"topic": topic["title"], "series": "main"}
    if topic.get("format") == "what_if_story":
        seed = {k: topic[k] for k in ("title", "premise", "hook", "genre", "world_rules")}
        seed['meme_sources']=[self_source for id in topic.get('meme_ids',[]) if (self_source:=store.get('meme_signal',id))]
        payload.update(format="what_if_story", series="whatif_"+id[-20:], creative_brief=seed,
                       topic=json.dumps(seed, ensure_ascii=False))
    if topic.get("format") == "emergency_story":
        payload.update(format="emergency_story", emergency_topic=topic["emergency_topic"], series="critical_situations",
                       topic=json.dumps({"title": topic["title"], "premise": topic["premise"], "hook": topic["hook"]}, ensure_ascii=False))
    task = store.enqueue("script", payload, "topic:"+id)
    topic["status"] = "approved"
    store.put("trend", id, topic)
    return task


def approve_script(store, id):
    episode = store.get("episode", id)
    if not episode:
        raise ValueError("Серия не найдена")
    if episode["status"] == "materials_approved":
        return episode.get("materials_task")
    if episode["status"] != "awaiting_script_approval":
        raise ValueError("Серия уже находится в производстве")
    require_review(episode, store)
    task = store.enqueue("materials", {"episode": id}, "materials:"+id+":"+sha256_text(episode["script"]))
    episode.update(status="materials_approved", materials_task=task)
    store.put("episode", id, episode)
    return task


def sha256_text(value):
    import hashlib
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def archive_reviewed(store, id, checks: dict):
    episode = store.get("episode", id)
    required = {"script", "characters", "audio", "visual", "subtitles", "trend", "producer"}
    if not episode or episode.get("status") != "awaiting_review" or not episode.get("qc", {}).get("pass"):
        raise ValueError("Сначала нужен успешный технический QC")
    if set(checks) != required or any(v is not True for v in checks.values()):
        raise ValueError("Нужны семь явно подтверждённых проверок владельца")
    path = store.root / "results" / episode["result"]
    if sha256(path) != episode["qc"]["sha256"]:
        raise ValueError("Видео изменилось после QC")
    script = Script.model_validate(episode["script"])
    require_review(episode, store)
    from studio.quality import save_character_references
    if episode.get("package"):
        save_character_references(store, episode | {"id": id})
    with store.connect() as db:
        existing = db.execute("SELECT full_script FROM archive WHERE season_title=? AND episode_title=?", (script.season_title, script.episode_title)).fetchone()
        content = script.model_dump_json()
        if existing and existing[0] != content:
            raise ValueError("В архиве уже есть серия с таким названием; выберите уникальное название")
        db.execute("INSERT OR IGNORE INTO archive VALUES(?,?,?)", (script.season_title, script.episode_title, content))
    episode.update(status="reviewed", human_checks=checks)
    store.put("episode", id, episode)
    store.put("story", episode["series"], {"last_episode": id, "recent_events": script.synopsis, "unresolved": script.unresolved, "next_plan": script.next_plan,
                                          "creative_brief": episode.get("creative_brief")})
    return episode
