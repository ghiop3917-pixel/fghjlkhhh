"""Очистка только конкретных производственных файлов после подтверждённой публикации."""
import hashlib
import json
import os
import secrets
import time
import zipfile

from pydantic import BaseModel, Field, StrictBool

from studio.media import safe_file, sha256


class CleanupConsent(BaseModel):
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirm_delete_listed_files: StrictBool
    confirm_youtube_is_sufficient: StrictBool


def _files(store, episode):
    candidates = []
    if episode.get("package"):
        if (store.root/"packages"/episode["package"]).is_symlink():
            raise ValueError("Ссылка вместо пакета не очищается")
        folder = safe_file(store.root/"packages", episode["package"])
        if folder.exists():
            for path in sorted(folder.rglob("*")):
                if path.is_symlink():
                    raise ValueError("Ссылки внутри пакета не допускаются к очистке")
                if path.is_file():
                    safe_file(folder, path.relative_to(folder).as_posix())
                    candidates.append(path)
    if episode.get("result"):
        if (store.root/"results"/episode["result"]).is_symlink():
            raise ValueError("Ссылка вместо MP4 не очищается")
        path = safe_file(store.root/"results", episode["result"])
        if path.is_symlink():
            raise ValueError("Ссылка вместо MP4 не очищается")
        if path.is_file():
            candidates.append(path)
    return candidates


def cleanup_preview(store, episode_id):
    episode = store.get("episode", episode_id)
    if not episode:
        raise ValueError("Серия не найдена")
    publication = store.get("publication", episode_id, {})
    # Пакет не должен использоваться другой серией или активной задачей.
    shared = any(r["id"] != episode_id and episode.get("package") and r["body"].get("package") == episode["package"] for r in store.objects("episode"))
    with store.connect() as db:
        tasks = db.execute("SELECT payload FROM tasks WHERE status IN ('pending','running') AND kind IN ('render','materials','qc','publication')").fetchall()
        script = episode.get("script", {})
        archived = db.execute("SELECT full_script FROM archive WHERE season_title=? AND episode_title=?", (script.get("season_title"), script.get("episode_title"))).fetchone()
    from studio.schemas import Script
    try:
        archived_ok = bool(archived) and Script.model_validate_json(archived[0]).model_dump() == Script.model_validate(script).model_dump()
    except ValueError:
        archived_ok = False
    active = any(json.loads(t[0]).get("episode") == episode_id or (episode.get("package") and json.loads(t[0]).get("package") == episode["package"]) for t in tasks)
    files = [{"path": p.relative_to(store.root).as_posix(), "size": p.stat().st_size, "sha256": sha256(p)} for p in _files(store, episode)]
    remote = publication.get("remote_status", {})
    eligible = bool(files) and archived_ok and episode.get("status") == "reviewed" and publication.get("status") == "confirmed_public" and remote.get("uploadStatus") == "processed" and publication.get("sha256") == episode.get("qc", {}).get("sha256") and not shared and not active
    body = {"episode": episode_id, "files": files, "video_id": publication.get("video_id"), "video_sha256": publication.get("sha256")}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    return body | {"plan_sha256": digest, "eligible": eligible, "publication_status": publication.get("status", "none"),
                   "reason": "Нужно явное разрешение на список; перед удалением выполняются резервная копия и новая сверка YouTube" if eligible else "Нужны обработанное публичное видео, отсутствие активных/общих пакетов и успешный просмотр",
                   "preserve": ["archive", "character", "references", "story", "analytics", "logs", "secrets"], "backup_required": True,
                   "note": "Эта политика считает достаточным подтверждение YouTube только после отдельного согласия владельца; TikTok-черновик не считается публикацией"}


def approve_cleanup(store, episode_id, data: CleanupConsent):
    plan = cleanup_preview(store, episode_id)
    if not plan["eligible"] or data.plan_sha256 != plan["plan_sha256"] or data.confirm_delete_listed_files is not True or data.confirm_youtube_is_sufficient is not True:
        raise ValueError("Очистка не согласована для текущего списка файлов и условия публикации")
    id = secrets.token_hex(12)
    store.put("cleanup_consent", id, {"status": "approved", "plan": plan, "created": time.time()})
    task = store.enqueue("cleanup", {"consent": id}, "cleanup:"+id)
    return {"consent": id, "task": task}


def important_backup(store):
    db_copy = store.backup()
    folder = store.root/"backups"
    path = folder/("important-"+secrets.token_hex(12)+".zip")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as target, zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(db_copy, "studio.sqlite3")
        refs = store.root/"references"
        for protected in (refs, store.root/"secrets"):
            if not protected.exists():
                continue
            for file in protected.rglob("*"):
                if file.is_symlink():
                    raise ValueError("Резервная копия эталонов не должна проходить по ссылкам")
                if file.is_file():
                    archive.write(file, file.relative_to(store.root).as_posix())
        # Внешние эталоны нельзя молча оставить без копии.
        for character in store.objects("character"):
            for name in character["body"].get("references", []):
                reference = safe_file(store.root, name)
                if not reference.is_file() or not reference.is_relative_to(refs.resolve()):
                    raise ValueError("Сначала перенесите все эталоны в data/references и проверьте их доступность")
    with zipfile.ZipFile(path) as archive:
        if archive.testzip() is not None or "studio.sqlite3" not in archive.namelist():
            raise ValueError("Проверка резервной копии не пройдена")
    return path


async def execute_cleanup(store, id, *, delete_file=None):
    consent = store.get("cleanup_consent", id)
    if not consent:
        raise ValueError("Разрешение очистки не найдено")
    if consent["status"] == "done":
        return consent["result"]
    if consent["status"] != "approved" or time.time()-consent["created"] > 24*3600:
        raise ValueError("Разрешение очистки устарело или уже выполняется; нужна проверка владельцем")
    episode_id = consent["plan"]["episode"]
    from studio.publishing import youtube_readback
    await youtube_readback(store, episode_id)
    plan = cleanup_preview(store, episode_id)
    if not plan["eligible"] or plan["plan_sha256"] != consent["plan"]["plan_sha256"]:
        raise ValueError("Файлы или статус публикации изменились; удаление не выполняется")
    backup = important_backup(store)
    store.put("cleanup_consent", id, consent | {"status": "deleting", "backup": backup.name})
    # После начала частичной очистки автоматический повтор не должен расширять список.
    removed = []
    for item in plan["files"]:
        path = safe_file(store.root, item["path"])
        if sha256(path) != item["sha256"]:
            raise ValueError("Файл изменился во время очистки; оставшиеся файлы сохранены")
        if delete_file is None:
            path.unlink()
        else:
            delete_file(path)
        removed.append(item["path"])
    result = {"removed": removed, "backup": backup.name}
    store.put("cleanup_consent", id, consent | {"status": "done", "result": result})
    store.event("cleanup.done", {"episode": episode_id, "count": len(removed), "backup": backup.name})
    return result
