"""Учебные сюжетные истории: проверяемые советы отделены от художественного вымысла."""
import hashlib
import json
import secrets
from datetime import datetime, timezone
from urllib.parse import urlparse

from pydantic import BaseModel, Field, StrictBool, model_validator

from studio.schemas import Script

REVIEWED_ON = "2026-11-12"
DISCLAIMER = "Учебная история. События вымышлены. Следуйте указаниям местных экстренных служб."
SOURCES = {
    "cdc_inside": {"title": "CDC — укрытие при радиационной ЧС", "url": "https://www.cdc.gov/radiation-emergencies/response/get-inside.html"},
    "cdc_updates": {"title": "CDC — официальные сообщения при радиационной ЧС", "url": "https://www.cdc.gov/radiation-emergencies/response/stay-tuned.html"},
    "mchs": {"title": "МЧС — действия при угрозе террористического акта", "url": "https://41.mchs.gov.ru/deyatelnost/poleznaya-informaciya/dopolnitelnye-stranicy/god-grazhdanskoy-oborony/rekomendacii-dlya-naseleniya-i-rukovoditeley/rekomendacii-dlya-naseleniya/deystviya-pri-ugroze-i-osushchestvlenii-terroristicheskogo-akta"},
    "protectuk": {"title": "Counter Terrorism Policing — Run Hide Tell", "url": "https://www.protectuk.police.uk/advice-and-guidance/response/run-hide-tell"},
}
TOPICS = {
    "nuclear": {"title": "После ядерного взрыва: решение об укрытии", "scope": "Герои пережили первоначальное воздействие. Не изображай укрытие гарантией выживания в эпицентре.", "advice": {
        "nuclear_shelter": {"text": "Как только это безопасно, укройся в здании. Выбирай подвал или середину здания, подальше от наружных стен и крыши.", "source": "cdc_inside"},
        "nuclear_updates": {"text": "Оставайся в укрытии и следи за официальными сообщениями экстренных служб о безопасном выходе и эвакуации.", "source": "cdc_updates"},
    }},
    "suspicious_object": {"title": "Угроза теракта: подозрительный предмет", "scope": "Бесхозный предмет не является доказательством теракта. Герои не исследуют и не обезвреживают его.", "advice": {
        "object_distance": {"text": "Не приближайся к подозрительному предмету, не трогай и не перемещай его.", "source": "mchs"},
        "object_report": {"text": "Отойди от находки и сообщи о ней полиции или сотруднику объекта, не подвергая себя опасности.", "source": "mchs"},
    }},
    "armed_attack": {"title": "Вооружённое нападение: безопасный выход", "scope": "Вымышленное нападение. Не показывай тактику нападающих, устройство оружия и способы нападения.", "advice": {
        "attack_escape": {"text": "Если есть безопасный путь, уходи от опасности. Оставь вещи и не задерживайся ради съёмки.", "source": "protectuk"},
        "attack_hide": {"text": "Если безопасно уйти нельзя, укройся, запрись или забаррикадируй дверь, отойди от неё. Отключи звук и вибрацию телефона.", "source": "protectuk"},
        "attack_report": {"text": "Когда это безопасно, сообщи полиции, что происходит и где ты находишься.", "source": "protectuk"},
    }},
}


class SafetyReview(BaseModel):
    script_sha256: str
    registry_sha256: str
    sources_checked: StrictBool
    advice_checked: StrictBool
    fictional_label_checked: StrictBool


def get_topics(store=None):
    topics = dict(TOPICS)
    if store is not None:
        for row in store.objects("emergency_guidance"):
            if row["body"].get("sources_checked") is True and row["id"] not in TOPICS:
                topics[row["id"]] = row["body"]
    return topics


def catalog(store=None):
    return {"disclaimer": DISCLAIMER, "reviewed_on": REVIEWED_ON, "topics": get_topics(store), "sources": SOURCES}


class GuidanceSource(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    url: str = Field(max_length=2000)

    @model_validator(mode="after")
    def official_url(self):
        parsed = urlparse(self.url)
        domains = ("mchs.gov.ru", "cdc.gov", "ready.gov", "gov.uk", "protectuk.police.uk", "weather.gov", "noaa.gov", "usgs.gov", "who.int", "ifrc.org", "redcross.org")
        host = parsed.hostname or ""
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443) or not any(host == d or host.endswith("."+d) for d in domains):
            raise ValueError("Нужна HTTPS-ссылка на поддерживаемый официальный источник")
        return self


class GuidanceAdvice(BaseModel):
    text: str = Field(min_length=10, max_length=500)
    source: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")


class NewGuidance(BaseModel):
    title: str = Field(min_length=3, max_length=120)
    scope: str = Field(min_length=10, max_length=1500)
    advice: dict[str, GuidanceAdvice] = Field(min_length=1, max_length=3)
    sources: dict[str, GuidanceSource] = Field(min_length=1, max_length=5)
    sources_checked: StrictBool

    @model_validator(mode="after")
    def references(self):
        if not self.sources_checked or any(a.source not in self.sources for a in self.advice.values()):
            raise ValueError("Проверьте источники и соответствие каждого совета источнику")
        import re
        if any(not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", key) for key in self.advice):
            raise ValueError("ID совета: латиница, цифры и подчёркивание")
        return self


def save_guidance(store, topic, data: NewGuidance):
    import re
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", topic) or topic in TOPICS:
        raise ValueError("Выберите новый ID темы; встроенные памятки не перезаписываются")
    body = data.model_dump() | {"reviewed_on": datetime.now(timezone.utc).date().isoformat()}
    revision = store.put("emergency_guidance", topic, body)
    first_source = next(iter(body["sources"].values()))
    for row in store.objects("trend"):
        if row["body"].get("format") == "emergency_story" and row["body"].get("emergency_topic") == topic:
            proposal = row["body"] | {"guidance_status": "ready", "url": first_source["url"], "verification": "Вымышленная идея ИИ; памятка согласована владельцем"}
            store.put("trend", row["id"], proposal)
    store.put("research_request", topic, {"status": "guidance_reviewed", "revision": revision})
    with store.connect() as db:
        rows = db.execute("SELECT id,payload FROM tasks WHERE kind='script' AND status='pending'").fetchall()
        for row in rows:
            if json.loads(row["payload"]).get("emergency_topic") == topic:
                db.execute("UPDATE tasks SET available=0 WHERE id=?", (row["id"],))
    return {"topic": topic, "revision": revision}


def series_for(script: Script, default="main"):
    if script.format == "what_if_story":
        return default if default.startswith("whatif_") else "what_if"
    return "critical_situations" if script.format == "emergency_story" else "main" if default in ("critical_situations", "what_if") or default.startswith("whatif_") else default


def validate_advice(script: Script, topics=None):
    if script.format != "emergency_story":
        return
    topics = TOPICS if topics is None else topics
    if script.emergency_topic not in topics:
        raise ValueError("Для нового вида ЧС ещё не проверены советы и источники")
    topic = topics[script.emergency_topic]
    if len(set(script.safety_advice_ids)) != len(script.safety_advice_ids):
        raise ValueError("Советы не должны дублироваться")
    spoken = " ".join(s.text for s in script.scenes)
    for id in script.safety_advice_ids:
        if id not in topic["advice"]:
            raise ValueError("Совет не относится к выбранной ЧС")
        if topic["advice"][id]["text"] not in spoken:
            raise ValueError("Проверенный совет должен прозвучать в сценарии дословно")


def fingerprint(script: Script):
    return hashlib.sha256(json.dumps(script.model_dump(), sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def briefing(script: Script, topics=None):
    topics = TOPICS if topics is None else topics
    validate_advice(script, topics)
    if script.format != "emergency_story":
        raise ValueError("Это обычная драматическая серия")
    topic = topics[script.emergency_topic]
    advice = topic["advice"]
    selected = [{"id": id, **advice[id], "reference": (SOURCES | topic.get("sources", {}))[advice[id]["source"]]} for id in script.safety_advice_ids]
    reviewed_on = topic.get("reviewed_on", REVIEWED_ON)
    registry_hash = hashlib.sha256(json.dumps({"advice": selected, "disclaimer": DISCLAIMER, "reviewed_on": reviewed_on, "scope": topic["scope"], "topic": script.emergency_topic}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {"script_sha256": fingerprint(script), "registry_sha256": registry_hash, "disclaimer": DISCLAIMER, "reviewed_on": reviewed_on,
            "advice": selected,
            "review": "Проверьте также все остальные реплики: реестр советов не доказывает безопасность художественного текста."}


def require_review(episode, store=None):
    script = Script.model_validate(episode["script"])
    topics = get_topics(store)
    validate_advice(script, topics)
    if script.format == "emergency_story":
        review = episode.get("safety_review", {})
        if review.get("script_sha256") != fingerprint(script) or review.get("registry_sha256") != briefing(script, topics)["registry_sha256"] or any(review.get(k) is not True for k in ("sources_checked", "advice_checked", "fictional_label_checked")):
            raise ValueError("Сначала проверьте советы и источники для этой версии учебного сценария")


def check_publication(episode, description: str, store=None):
    if episode.get("script", {}).get("format") != "emergency_story":
        return
    require_review(episode, store)
    script = Script.model_validate(episode["script"])
    info = briefing(script, get_topics(store))
    if DISCLAIMER not in description or any(a["reference"]["url"] not in description for a in info["advice"]):
        raise ValueError("В описании учебного видео нужны пометка о вымысле и ссылки на источники советов")


def confirm_review(store, id: str, data: SafetyReview):
    episode = store.get("episode", id)
    if not episode or episode["status"] != "awaiting_script_approval":
        raise ValueError("Проверка доступна перед согласованием сценария")
    script = Script.model_validate(episode["script"])
    info = briefing(script, get_topics(store))
    if data.script_sha256 != info["script_sha256"] or data.registry_sha256 != info["registry_sha256"] or not all((data.sources_checked, data.advice_checked, data.fictional_label_checked)):
        raise ValueError("Подтвердите три проверки для текущего сценария")
    episode["safety_review"] = data.model_dump() | {"registry_reviewed_on": info["reviewed_on"], "registry_sha256": info["registry_sha256"]}
    store.put("episode", id, episode)
    return {"status": "safety_reviewed", "script_sha256": info["script_sha256"]}


def instruction(topic: str, store=None):
    topics = get_topics(store)
    if topic not in topics:
        from studio.agents import Unavailable
        raise Unavailable("Новая тема ожидает проверки источников и советов")
    return ("\nФормат emergency_story: сюжетная учебная история 16+, по-русски, 30–60 секунд. "
            "Герои сталкиваются с опасностью, выбирают действие и показывают его последствия. "
            "Без натуралистических травм, романтизации террористов, шуток над жертвами, оружейных инструкций, "
            "гарантий выживания и вымышленных медицинских рекомендаций. Не изображай реальный текущий теракт. "
            "Не привязывай действия к этничности или религии. Не придумывай расстояния, дозировки и сроки. "
            "Прямые указания местных служб имеют приоритет; не переноси номера служб другой страны. "
            "В JSON обязательно format=emergency_story, emergency_topic=" + topic +
            ", safety_advice_ids=[ID одного или двух советов из реестра]. "
            "Вставь выбранные советы отдельными репликами дословно. Остальное — связный вымышленный сюжет. "
            "Не включай дополнительные непроверенные советы. В финале герой уже делает безопасный выбор, "
            "а не ждёт продолжения ради базового действия. Следующую серию можно посвятить последствиям. "
            "Реестр и границы сюжета: " + json.dumps(topics[topic], ensure_ascii=False))


def request_draft(store, topic: str):
    topics = get_topics(store)
    if topic not in topics:
        raise ValueError("Сначала добавьте и проверьте памятку для нового вида ЧС")
    return store.enqueue("script", {"topic": topics[topic]["title"], "series": "critical_situations",
                                   "format": "emergency_story", "emergency_topic": topic}, "emergency:"+secrets.token_hex(12))


class StoryIdea(BaseModel):
    title: str = Field(min_length=5, max_length=120)
    premise: str = Field(min_length=30, max_length=1000)
    hook: str = Field(min_length=5, max_length=200)
    emergency_topic: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    topic_title: str = Field(default="", max_length=120)
    research_queries: list[str] = Field(default_factory=list, max_length=3)


class Ideas(BaseModel):
    ideas: list[StoryIdea] = Field(min_length=1, max_length=3)


def request_ideas(store, dedupe=None):
    return store.enqueue("emergency_ideas", {}, dedupe or "emergency-ideas:"+secrets.token_hex(12))


async def invent_ideas(store, router):
    recent = [r for r in store.objects("trend") if r["body"].get("format") == "emergency_story"]
    if sum(r["body"].get("status") == "proposed" for r in recent) >= 6:
        return {"status": "awaiting_choice", "note": "Сначала выберите или отклоните накопленные идеи"}
    prompt = ("Сам придумай три разные оригинальные вымышленные ситуации для учебного мультсериала 16+. "
              "Не выбирай готовый пример: придумай место, обстоятельства, конфликт персонажей и трудный, но безопасный выбор. "
              "Без кровавых подробностей, инструкций по нападению, шуток над пострадавшими и заявлений о реальном событии. "
              "Не придумывай сами советы по спасению: позже сценарист использует проверенный реестр. "
              "Сам придумывай НОВЫЕ виды опасностей: природные, бытовые, транспортные, техногенные и другие. "
              "Пожары, наводнения, землетрясения, штормы, аварии и потеря в лесу — только примеры, НЕ закрытый список. "
              "Хотя бы одна идея должна относиться к виду, которого ещё нет в реестре. Для новой темы придумай латинский ID, "
              "русское название topic_title и research_queries для поиска официальных рекомендаций. Не выдумывай URL и проверенные советы. "
              "Верни JSON {\"ideas\":[{\"title\":\"...\",\"premise\":\"сюжетная ситуация и конфликт\",\"hook\":\"завязка\",\"emergency_topic\":\"новый_код\",\"topic_title\":\"вид опасности\",\"research_queries\":[\"запрос к официальным источникам\"]}]}. "
              "Уже проверенные темы и границы: " + json.dumps({k: {"title": v["title"], "scope": v["scope"]} for k,v in get_topics(store).items()}, ensure_ascii=False) +
              "\nНе повторяй недавние идеи: " + json.dumps([r["body"]["title"] for r in recent[:20]], ensure_ascii=False))
    from studio.agents import parse_json
    generated = Ideas.model_validate(parse_json(await router.ask("producer", prompt, "critical_situations", True)))
    ids = []
    for idea in generated.ideas:
        id = "idea-" + hashlib.sha256(idea.title.strip().casefold().encode()).hexdigest()[:20]
        if store.get("trend", id):
            continue
        topic = get_topics(store).get(idea.emergency_topic)
        source = (SOURCES | topic.get("sources", {}))[next(iter(topic["advice"].values()))["source"]] if topic else None
        store.put("trend", id, {**idea.model_dump(), "format": "emergency_story", "status": "proposed",
                               "verification": "Вымышленная идея ИИ; памятка проверена" if topic else "Новый вид опасности: нужны источники и проверка советов",
                               "guidance_status": "ready" if topic else "needs_sources",
                               "url": source["url"] if source else "", "published": datetime.now(timezone.utc).isoformat(),
                               "popularity": None, "competition": None, "source": "ai_story_planner"})
        if not topic:
            store.put("research_request", idea.emergency_topic, {"status": "needs_sources", "topic_title": idea.topic_title,
                                                               "queries": idea.research_queries, "proposal": id})
        ids.append(id)
    return {"ideas": ids, "status": "awaiting_choice"}
