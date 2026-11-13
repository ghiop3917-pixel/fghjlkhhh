import asyncio
import json
import os
import time

import httpx

from studio.agents import ROLES, Router, Unavailable
from studio.conversation import apply_proposal, converse
from studio.emergencies import request_draft, request_ideas, series_for
from studio.production import approve_script, approve_topic
from studio.schemas import Character, Script
from studio.story_ideas import request_story_ideas

HELP = """AI Cartoon Studio
/status — очередь и состояние
/topics — предложенные темы
/find — новая проверка RSS
/episodes — сценарии и статусы
/ideas — ИИ придумает любые новые сюжетные ситуации
/memes — проверить свежие публикации о TikTok-мемах
/meme ссылка описание — добавить TikTok-мем для сюжетов
/emergency auto — ИИ сам придумывает ситуации
/emergency nuclear|suspicious_object|armed_attack — выбрать тип ЧС
/mode AUTO|PC|VPS — режим рендера
/schedule час_UTC — время ежедневного поиска
/quiet начало конец [UTC-смещение] — тихие часы; /quiet off — выключить
/summary [час] — сводка сейчас или час ежедневной сводки (местное время)
/agent роль текст — обратиться к агенту
/character id JSON — сохранить паспорт
/script JSON — импорт сценария
/approve id — согласовать сценарий
/backup — резервная копия базы
/app — открыть панель
Обычный текст направляется продюсеру. Ответы ИИ — предложения; изменение объектов выполняется явными командами.
Роли: """ + ", ".join(ROLES)


class Bot:
    def __init__(self, store, settings):
        self.store, self.settings = store, settings
        self.router = Router(store)

    async def call(self, client, method, body):
        r = await client.post(f"https://api.telegram.org/bot{self.settings.bot_token}/{method}", json=body)
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError("Telegram отклонил запрос")
        return data["result"]

    async def send(self, client, text, markup=None):
        body = {"chat_id": self.settings.owner_id, "text": text[:4000]}
        if markup:
            body["reply_markup"] = markup
        return await self.call(client, "sendMessage", body)

    async def command(self, text):
        from studio.bot_comfort import accept_feedback, summary
        from studio.comfort import preferences
        feedback=accept_feedback(self,text)
        if feedback:
            return feedback
        parts = text.split(maxsplit=1)
        command = parts[0].split("@")[0] if parts else ""
        arg = parts[1] if len(parts) == 2 else ""
        if command == '/meme':
            from studio.memes import submit_meme
            if len(arg.split(maxsplit=1))!=2:
                return 'Пришлите /meme https://www.tiktok.com/... описание того, что происходит в меме'
            url,description=arg.split(maxsplit=1)
            submit_meme(self.store,url,description)
            return 'Мем сохранён по вашему описанию. /ideas — придумать оригинальные сюжеты с его учётом. Сам ролик не просматривался'
        if command == '/memes':
            self.store.enqueue('memes',{},'manual-memes:'+str(time.time_ns()))
            from studio.memes import recent_signals
            signals=recent_signals(self.store)
            return 'Проверка мемов: один раз в день, в 13:00 по времени студии (по умолчанию Москва). До этого времени проверка ждёт расписания. Это описания источников, не просмотр ленты TikTok.\n'+('\n\n'.join(m['title']+'\n'+m['url'] for m in signals) or 'Свежих подтверждённых источником сигналов пока нет')
        if command == "/quiet":
            prefs=preferences(self.store)
            if arg=='off':
                prefs['quiet_enabled']=False
            else:
                values=arg.split()
                if len(values) not in (2,3) or any(not v.lstrip('-').isdigit() for v in values):
                    return 'Например: /quiet 23 9 3 — с 23:00 до 09:00 по Москве. /quiet off — выключить'
                start,end=int(values[0]),int(values[1])
                offset=int(values[2]) if len(values)==3 else prefs['utc_offset']
                if not 0<=start<=23 or not 0<=end<=23 or start==end or not -12<=offset<=14:
                    raise ValueError('Часы: 0–23, начало и конец должны различаться; UTC-смещение: −12…14')
                prefs.update(quiet_enabled=True,quiet_start=start,quiet_end=end,utc_offset=offset)
            self.store.put('settings','comfort',prefs)
            return 'Тихие часы сохранены. Уже запущенный рендер не прерывается'
        if command == "/summary":
            if not arg:
                return summary(self.store)
            if not arg.isdigit() or not 0<=int(arg)<=23:
                raise ValueError('Укажите местный час 0–23: /summary 19')
            self.store.put('settings','comfort',preferences(self.store)|{'summary_hour':int(arg)})
            return 'Ежедневная сводка: '+arg+':00 по часовому поясу студии'
        if command in ("/start", "/help"):
            return HELP
        if command == "/status":
            tasks = self.store.tasks()
            counts = {s: sum(t["status"] == s for t in tasks) for s in ("pending", "running", "done", "failed")}
            return "Очередь: " + json.dumps(counts, ensure_ascii=False) + "\nРендер выполняется только Windows Worker."
        if command == "/mode":
            if arg not in ("AUTO", "PC", "VPS"):
                return "Укажите /mode AUTO, PC или VPS. VPS приостанавливает финальный рендер."
            self.store.put("settings", "mode", {"mode": arg})
            return "Режим: " + arg
        if command == "/find":
            self.store.enqueue("trends", {}, "bot-trends:"+str(time.time_ns()))
            return "Проверка настроенных RSS добавлена в очередь"
        if command == "/schedule":
            if not arg.isdigit() or not 0 <= int(arg) <= 23:
                raise ValueError("Укажите час UTC от 0 до 23. Например, /schedule 7 — 10:00 Москвы")
            self.store.put("settings", "schedule", {"daily_hour_utc": int(arg)})
            return f"Ежедневный поиск: {int(arg):02}:00 UTC"
        if command == "/episodes":
            return "\n\n".join(f"{r['id']}\n{r['body']['script']['episode_title']} — {r['body']['status']}\n" + "\n".join(s["text"] for s in r["body"]["script"]["scenes"]) for r in self.store.objects("episode")[:5]) or "Серий пока нет"
        if command == "/emergency":
            if arg == "auto":
                return "ИИ придумает новые ситуации. Задача: " + request_ideas(self.store) + "\nПредложения придут с кнопками выбора."
            return "Учебная история добавлена в очередь: " + request_draft(self.store, arg) + "\nПосле сценария откройте панель: для этого формата обязательна проверка советов и источников."
        if command == "/ideas":
            return "ИИ придумает разные сюжеты «А что, если…», без фиксированного списка ситуаций. Задача: " + request_story_ideas(self.store)
        if command == "/approve":
            return "Подготовка согласована. Задача: " + str(approve_script(self.store, arg))
        if command == "/character":
            id, raw = arg.split(maxsplit=1)
            if not id.replace("-", "").replace("_", "").isalnum() or len(id) > 64:
                raise ValueError("Некорректный ID персонажа")
            passport = Character.model_validate_json(raw)
            revision = self.store.put("character", id, passport.model_dump())
            return f"Паспорт {id} сохранён. Версия {revision}. Старые версии сохранены."
        if command == "/script":
            import secrets
            script = Script.model_validate_json(arg)
            id = secrets.token_hex(12)
            self.store.put("episode", id, {"script": script.model_dump(), "series": series_for(script), "status": "awaiting_script_approval"})
            return f"Сценарий сохранён: {id}. Прочитайте /episodes, затем /approve {id}"
        if command == "/backup":
            return "Резервная копия базы: " + self.store.backup().name
        if command == "/agent":
            role, request = arg.split(maxsplit=1)
            response = await converse(self.store, request, role)
        else:
            response = await converse(self.store, text)
        answer = response["answer"]
        if response.get("proposal"):
            answer += "\n\nПредложение правки: " + response["proposal"] + "\nПоказать изменения и согласовать: /proposal " + response["proposal"]
        self.store.event("chat.user", {"text": text})
        self.store.event("chat.assistant", {"text": answer})
        return answer

    async def notifications(self, client):
        from studio.bot_comfort import (
            daily_summary,
            deliver_video,
            progress_notifications,
        )
        from studio.comfort import pause_notices, quiet_now
        from studio.config import flag
        if self.settings.bot_token and self.settings.owner_id:
            if quiet_now(self.store):
                pause_notices(self.store)
                return
            await daily_summary(self,client)
            await progress_notifications(self,client)
        if flag("STUDIO_RENDER_WARNING", True) and self.store.get("settings", "mode", {}).get("mode") != "VPS":
            await self.render_warning(client)
        from studio.memes import recent_signals
        for meme in recent_signals(self.store)[:3]:
            key='meme:'+meme['id']
            if self.store.get('notification',key,{}).get('status')=='sent':
                continue
            await self.send(client,'🧩 Мем для вдохновения: '+meme['title']+'\n'+meme['summary'][:450]+'\n'+meme['verification']+'\n'+meme['url']+'\n/ideas — предложить оригинальные сюжеты. Видео не просматривалось')
            self.store.put('notification',key,{'status':'sent'})
        for topic in self.store.objects("trend")[:20]:
            key = "topic:"+topic["id"]
            if topic["body"]["status"] != "proposed" or self.store.get("notification", key, {}).get("status") == "sent":
                continue
            self.store.put("notification", key, {"status": "sending"})
            body = topic["body"]
            details = "\nВымышленная идея ИИ.\n" + body.get("premise", "") + "\nЗавязка: " + body.get("hook", "") + "\nСсылка — памятка по безопасности." if body.get("format") == "emergency_story" else "\nОдин источник. Вирусность не измерена."
            if body.get("format") == "what_if_story":
                details = "\nВымышленный сюжет «А что, если…».\n" + body["premise"] + "\nЗавязка: " + body["hook"] + "\nЖанр: " + body["genre"]
            await self.send(client, body["title"]+details+"\n"+body["url"], {"inline_keyboard": [[{"text": "Выбрать", "callback_data": "topic:"+topic["id"]}, {"text": "Отклонить", "callback_data": "reject:"+topic["id"]}]]})
            self.store.put("notification", key, {"status": "sent"})
        for task in self.store.tasks():
            if task["status"] != "failed" or self.store.get("notification", "failed:"+task["id"], {}).get("status") == "sent":
                continue
            self.store.put("notification", "failed:"+task["id"], {"status": "sending"})
            await self.send(client, f"Задача {task['kind']} остановлена после трёх попыток. ID: {task['id']}\n{task['error']}")
            self.store.put("notification", "failed:"+task["id"], {"status": "sent"})
        for row in self.store.objects("episode")[:20]:
            status = row["body"]["status"]
            if status=='awaiting_review' and row['body'].get('result') and row['body'].get('qc',{}).get('pass'):
                await deliver_video(self,client,row)
            key = "episode:"+row["id"]+":"+status
            if self.store.get("notification", key, {}).get("status") == "sent":
                continue
            self.store.put("notification", key, {"status": "sending"})
            label={'materials_approved':'Готовятся сцены, речь и субтитры','awaiting_pc':'Материалы готовы, ожидание Lenovo','awaiting_script_approval':'Сценарий ждёт вашего согласования','awaiting_review':'Видео готово к просмотру','reviewed':'Видео одобрено'}.get(status,status)
            await self.send(client, f"Серия: {row['body']['script']['episode_title']}\nID: {row['id']}\nСтатус: {label}\nСценарии: /episodes. Просмотр видео и проверки: /app")
            self.store.put("notification", key, {"status": "sent"})

    async def render_warning(self, client):
        now = time.time()
        from studio.bot_comfort import controls
        from studio.comfort import worker_problem
        problem=worker_problem(self.store)
        with self.store.connect() as db:
            if db.execute("SELECT 1 FROM tasks WHERE kind='render' AND status='running' AND lease_until>?", (now,)).fetchone():
                return
            row = db.execute("SELECT id,payload FROM tasks WHERE kind='render' AND status='pending' AND attempts<3 AND available<=? ORDER BY priority DESC,created LIMIT 1", (now,)).fetchone()
        if problem:
            if row and self.settings.bot_token and self.settings.owner_id:
                from studio.comfort import local_now
                key='worker-problem:'+local_now(self.store).date().isoformat()+':'+str(__import__('hashlib').sha256(problem.encode()).hexdigest()[:12])
                if not self.store.get('notification',key):
                    await self.send(client,'⚠️ Рендер ожидает: '+problem)
                    self.store.put('notification',key,{'status':'sent'})
            return
        if not row or self.store.get("render_warning", row["id"], {}).get("status") == "sent":
            return
        payload = json.loads(row["payload"])
        episode = self.store.get("episode", payload["episode"], {})
        title = episode.get("script", {}).get("episode_title", payload["episode"])
        await self.send(client, f"⏳ Через 10 минут начнётся рендер видео «{title}» на Lenovo.\nОставьте ноутбук и окно Worker включёнными. Если Lenovo отключится, начало перенесётся до его подключения.", controls(row['id']))
        # Отсчёт только после успешного ответа Telegram; очередь переживает перезапуск.
        self.store.put("render_warning", row["id"], {"status": "sent", "sent_at": time.time(), "ready_at": time.time()+600})

    async def run(self):
        if not self.settings.bot_token or not self.settings.owner_id:
            raise ValueError("Не настроены Telegram Bot Token и ID владельца")
        async with httpx.AsyncClient(timeout=40, follow_redirects=False) as client:
            while True:
                try:
                    offset = self.store.get("settings", "telegram_offset", {"value": 0})["value"]
                    updates = await self.call(client, "getUpdates", {"offset": offset, "timeout": 20, "allowed_updates": ["message", "callback_query", "channel_post"]})
                    for update in updates:
                        if update.get("channel_post"):
                            from studio.trend_sources import telegram_channel_post
                            channels = {c.strip() for c in os.getenv("STUDIO_TELEGRAM_SOURCE_CHANNELS", "").split(",") if c.strip()}
                            telegram_channel_post(self.store, update["channel_post"], channels)
                        # Запись update_id исключает повторное исполнение уже принятой команды.
                        uid = str(update["update_id"])
                        if not self.store.get("telegram_update", uid):
                            self.store.put("telegram_update", uid, {"accepted": True})
                            try:
                                callback = update.get("callback_query")
                                message = callback.get("message", {}) if callback else update.get("message", {})
                                sender = callback.get("from", {}) if callback else message.get("from", {})
                                if str(sender.get("id")) == self.settings.owner_id and message.get("chat", {}).get("type") == "private" and str(message.get("chat", {}).get("id")) == self.settings.owner_id:
                                    if callback:
                                        data = callback.get("data", "")
                                        if data.startswith(('r:','v:')):
                                            from studio.bot_comfort import (
                                                callback as comfort_callback,
                                            )
                                            answer=await comfort_callback(self,client,data)
                                        elif data.startswith("topic:"):
                                            answer = "Тема выбрана. Задача: " + approve_topic(self.store, data[6:])
                                        elif data.startswith("reject:"):
                                            topic = self.store.get("trend", data[7:])
                                            if not topic or topic["status"] != "proposed":
                                                raise ValueError("Тема уже обработана")
                                            topic["status"] = "rejected"
                                            self.store.put("trend", data[7:], topic)
                                            answer = "Тема отклонена. /find — поискать другие"
                                        elif data.startswith("apply:"):
                                            results = apply_proposal(self.store, data[6:])
                                            answer = "Согласованная правка применена: " + json.dumps(results, ensure_ascii=False)
                                        else:
                                            answer = "Неизвестная кнопка"
                                        await self.call(client, "answerCallbackQuery", {"callback_query_id": callback["id"], "text": "Обработано"})
                                        await self.send(client, answer)
                                    elif message.get("text") == "/app":
                                        url = os.getenv("STUDIO_PUBLIC_URL", "")
                                        if not url.startswith("https://"):
                                            raise ValueError("Mini App требует публичного HTTPS-адреса VPS")
                                        await self.send(client, "Панель студии", {"inline_keyboard": [[{"text": "Открыть студию", "web_app": {"url": url}}]]})
                                    elif message.get("text", "").startswith("/proposal "):
                                        proposal_id = message["text"].split(maxsplit=1)[1]
                                        proposal = self.store.get("proposal", proposal_id)
                                        if not proposal:
                                            raise ValueError("Предложение не найдено")
                                        serialized = json.dumps(proposal["changes"], ensure_ascii=False, indent=2)
                                        if len(serialized) > 3400:
                                            await self.send(client, "Изменения слишком объёмны для Telegram-сообщения. Проверьте предложение в панели, ID: "+proposal_id)
                                        else:
                                            await self.send(client, "Проверьте before и новое содержимое. Уже собранные материалы сохранятся.\n"+serialized,
                                                            {"inline_keyboard": [[{"text": "Подтвердить эту правку", "callback_data": "apply:"+proposal_id}]]})
                                    elif message.get("text"):
                                        await self.send(client, await self.command(message["text"]))
                            except (ValueError, Unavailable) as exc:
                                await self.send(client, str(exc)[:1500])
                        self.store.put("settings", "telegram_offset", {"value": update["update_id"]+1})
                    await self.notifications(client)
                except Exception as exc:
                    self.store.event("telegram.error", {"type": type(exc).__name__})
                    await asyncio.sleep(10)
