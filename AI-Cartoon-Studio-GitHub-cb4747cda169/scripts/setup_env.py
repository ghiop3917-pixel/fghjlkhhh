"""Создать .env без перезаписи, без вывода секретов и изменения политик Windows."""
import secrets
from pathlib import Path

root = Path(__file__).resolve().parents[1]
path = root / ".env"
if path.exists():
    print("Файл .env уже существует. Настройки сохранены, перезапись не выполнялась.")
else:
    content = (root/".env.example").read_text(encoding="utf-8")
    content = content.replace("STUDIO_API_TOKEN=\n", "STUDIO_API_TOKEN="+secrets.token_hex(32)+"\n")
    content = content.replace("STUDIO_WORKER_TOKEN=\n", "STUDIO_WORKER_TOKEN="+secrets.token_hex(32)+"\n")
    with path.open("x", encoding="utf-8") as file:
        file.write(content)
    if __import__("os").name != "nt":
        path.chmod(0o600)
    print("Создан .env с отдельными случайными токенами. Сервер, облака и публикация отключены.")
