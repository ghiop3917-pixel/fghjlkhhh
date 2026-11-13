"""Независимые измеримые проверки и ограниченное восстановление производства."""
import json
import math
import shutil
import sys
import time
import uuid
import wave
from array import array

from PIL import Image, ImageChops

from studio.db import encode
from studio.media import safe_file, sha256, validate_manifest
from studio.schemas import Script


def inspect_audio(path):
    with wave.open(str(path), "rb") as wav:
        if wav.getsampwidth() != 2:
            return {"pass": False, "issues": ["Нужен PCM16 WAV"]}
        samples = array("h", wav.readframes(wav.getnframes()))
        if sys.byteorder != "little":
            samples.byteswap()
    count = max(1, len(samples))
    rms = math.sqrt(sum(v*v for v in samples)/count)
    clipping = sum(abs(v) >= 32760 for v in samples)/count
    issues = []
    if rms < 60:
        issues.append("Речь отсутствует или слишком тиха")
    if clipping > .01:
        issues.append("Более 1% отсчётов клиппированы")
    return {"pass": not issues, "issues": issues, "rms": round(rms, 2), "clipping_ratio": clipping, "scope": "signal_only_not_voice_acting"}


def inspect_package(store, episode):
    script = Script.model_validate(episode["script"])
    folder = safe_file(store.root/"packages", episode["package"])
    manifest = validate_manifest(json.loads((folder/"manifest.json").read_text(encoding="utf-8")))
    checks = {name: {"pass": True, "issues": []} for name in ("script", "character", "audio", "visual", "subtitle", "trend", "producer")}
    bad_audio = []
    for name,info in manifest["files"].items():
        path = safe_file(folder, name)
        if not path.is_file() or sha256(path) != info["sha256"]:
            checks["visual"]["issues"].append("Нарушена целостность: "+name)
    for i,(scene, scripted) in enumerate(zip(manifest["scenes"], script.scenes)):
        audio = inspect_audio(safe_file(folder, scene["audio"]))
        if not audio["pass"]:
            bad_audio.append(i)
            checks["audio"]["issues"].append({"scene": i, **audio})
        for name in (scene["image"], *(scene.get("animation", {}).get("sprites", []))):
            with Image.open(safe_file(folder, name)) as image:
                image.verify()
            with Image.open(safe_file(folder, name)) as image:
                if image.size != (1080, 1920):
                    checks["visual"]["issues"].append("Неверное разрешение: "+name)
        reference = store.get("character_reference", scripted.character_id+":"+scene["character_hash"])
        sprites = scene.get("animation", {}).get("sprites", [])
        if reference and sprites:
            if sha256(safe_file(store.root, reference["path"])) != reference["sha256"]:
                checks["character"]["issues"].append({"scene": i, "reason": "Нарушена целостность эталона"})
            with Image.open(safe_file(store.root, reference["path"])) as master, Image.open(safe_file(folder, sprites[0])) as current:
                # Эти области не меняются при смене эмоции и моргании базового рига.
                for region in ((400, 550, 680, 620), (430, 920, 650, 1180)):
                    if ImageChops.difference(master.convert("RGB").crop(region), current.convert("RGB").crop(region)).getbbox():
                        checks["character"]["issues"].append({"scene": i, "reason": "Персонаж отличается от одобренного эталона этой версии"})
        elif not reference:
            checks["character"]["needs_owner_reference_review"] = True
        if any(len(w) > 28 for w in scripted.text.split()):
            checks["subtitle"]["issues"].append({"scene": i, "reason": "Слишком длинное слово для мобильных субтитров"})
        if len(scripted.text)/float(scene["duration"]) > 25:
            checks["subtitle"]["issues"].append({"scene": i, "reason": "Более 25 символов/с: речь и чтение слишком быстрые"})
    if len(manifest["scenes"]) != len(script.scenes):
        checks["script"]["issues"].append("Число сцен не совпало со сценарием")
    content = (folder/manifest["subtitles"]).read_text(encoding="utf-8")
    if script.format in ("emergency_story", "what_if_story") and "вымышлен" not in content:
        checks["subtitle"]["issues"].append("Нет маркировки вымышленной истории")
    checks["trend"]["scope"] = "Вымысел или источник проверяется владельцем; метрики популярности не выдумываются"
    checks["producer"]["needs_owner_review"] = True
    for check in checks.values():
        check["pass"] = not check["issues"]
    return {"pass": all(c["pass"] for c in checks.values()), "checks": checks, "bad_audio_scenes": bad_audio,
            "scope": "automatic_technical_and_reference_checks_plus_required_owner_review"}


def save_character_references(store, episode):
    folder = safe_file(store.root/"packages", episode["package"])
    manifest = json.loads((folder/"manifest.json").read_text(encoding="utf-8"))
    references = store.root/"references"
    references.mkdir(exist_ok=True)
    for scene in manifest["scenes"]:
        sprites = scene.get("animation", {}).get("sprites", [])
        if not sprites:
            continue
        id = scene["character_id"]+":"+scene["character_hash"]
        if store.get("character_reference", id):
            continue
        path = references/(uuid.uuid4().hex+".png")
        shutil.copyfile(safe_file(folder, sprites[0]), path)
        store.put("character_reference", id, {"path": path.relative_to(store.root).as_posix(), "sha256": sha256(path), "approved_episode": episode.get("id"), "method": "owner_approved_vector_reference"}, expected=0)


def schedule_repair(store, episode_id, report):
    """Максимум три исправления; неизвестные творческие дефекты передаются владельцу."""
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM objects WHERE category='episode' AND id=?", (episode_id,)).fetchone()
        episode = json.loads(row["body"])
        attempts = episode.get("repair_attempts", 0)
        if attempts >= 3:
            return None
        checks = report.get("package", {}).get("checks", {})
        if checks.get("script", {}).get("pass") is False:
            return None
        bad_subtitles = checks.get("subtitle", {}).get("issues", [])
        if any("слово" in str(issue) or "быстрые" in str(issue) or "длительность" in str(issue).lower() or "озвучка" in str(issue).lower() for issue in bad_subtitles):
            kind = "script_revision"
            payload = {"episode": episode_id, "issues": bad_subtitles}
            status = "awaiting_script_repair"
        elif any(checks.get(name, {}).get("pass") is False for name in ("audio", "visual", "character", "subtitle")):
            kind = "materials"
            payload = {"episode": episode_id, "exclude_audio": report.get("package", {}).get("bad_audio_scenes", []), "reuse_package": episode["package"]}
            status = "materials_approved"
        else:
            kind = "render"
            payload = {"episode": episode_id, "package": episode["package"]}
            status = "awaiting_pc"
        dedupe = f"repair:{episode_id}:{attempts+1}"
        existing = db.execute("SELECT id FROM tasks WHERE dedupe=?", (dedupe,)).fetchone()
        task_id = existing[0] if existing else uuid.uuid4().hex
        db.execute("INSERT OR IGNORE INTO tasks(id,kind,status,priority,created,available,payload,dedupe) VALUES(?,?,'pending',1,?,?,?,?)", (task_id, kind, time.time(), time.time(), encode(payload), dedupe))
        episode.update(status=status, repair_attempts=attempts+1, last_quality_report=report)
        if kind == "render":
            episode["render_task"] = task_id
        db.execute("UPDATE objects SET revision=revision+1,body=?,updated=? WHERE category='episode' AND id=?", (encode(episode), time.time(), episode_id))
        db.execute("INSERT INTO history(category,id,revision,body,created) VALUES('episode',?,?,?,?)", (episode_id, row["revision"]+1, encode(episode), time.time()))
    store.event("quality.repair_scheduled", {"episode": episode_id, "attempt": attempts+1, "kind": kind, "task": task_id})
    return {"task": task_id, "kind": kind, "attempt": attempts+1}
