"""Подключение Lenovo к серверу с резервной копией существующего .env."""
import os
import uuid
from pathlib import Path
from urllib.parse import urlparse

from dotenv import dotenv_values

ALLOWED = {"STUDIO_API_URL", "STUDIO_WORKER_TOKEN", "STUDIO_SERVER_ENABLED", "STUDIO_ENCODER"}


def connect_worker(root: Path, pairing: Path, apply=False):
    values = dict(dotenv_values(pairing, interpolate=False))
    if set(values) - ALLOWED:
        raise ValueError("Файл содержит посторонние настройки; разрешены только параметры Worker")
    url = values.get("STUDIO_API_URL") or ""
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.port == 0 or parsed.query or parsed.fragment:
        raise ValueError("Нужен HTTPS-адрес сервера без учётных данных")
    token = values.get("STUDIO_WORKER_TOKEN") or ""
    if len(token) < 32 or any(c in token for c in "\r\n'\\"):
        raise ValueError("Неверный отдельный токен Worker")
    if any(c in url for c in "\r\n'\\"):
        raise ValueError("Неподдерживаемые символы адреса")
    updates = {"STUDIO_API_URL": url, "STUDIO_WORKER_TOKEN": token, "STUDIO_SERVER_ENABLED": "false", "STUDIO_ENCODER": values.get("STUDIO_ENCODER") or "auto"}
    if updates["STUDIO_ENCODER"] not in ("auto", "libx264", "h264_nvenc"):
        raise ValueError("Неверный режим кодировщика")
    if not apply:
        return {"applied": False, "keys": sorted(updates)}
    path = root/".env"
    if path.is_symlink():
        raise ValueError("Ссылка вместо .env не изменяется")
    content = path.read_bytes() if path.is_file() else b""
    lines = content.decode("utf-8-sig").splitlines()
    seen = set()
    for i,line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if key in updates:
            lines[i] = key+"='"+updates[key]+"'"
            seen.add(key)
    lines += [key+"='"+value+"'" for key,value in updates.items() if key not in seen]
    backup = None
    if path.exists():
        backup = root/(".env.backup-"+uuid.uuid4().hex[:12])
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(content)
    temporary = root/(".env.new-"+uuid.uuid4().hex[:12])
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write("\n".join(lines)+"\n")
        out.flush()
        os.fsync(out.fileno())
    os.replace(temporary, path)
    return {"applied": True, "keys": sorted(updates), "backup": backup.name if backup else None}
