"""Стандартная библиотека: первичная конфигурация VPS без вывода секретов."""
import getpass
import os
import re
import secrets
import sys
from pathlib import Path


def quote(value):
    value = str(value)
    if any(c in value for c in ("\n", "\r", "\0", "'", "\\")):
        raise ValueError("Значение содержит неподдерживаемые символы; используйте однострочный ключ")
    return "'"+value+"'"


def create_server_config(root: Path, values: dict):
    path = root/".env"
    if path.exists():
        return False
    if (root/"worker-connection.env").exists():
        raise ValueError("Секретный файл подключения уже существует; он не заменяется новым автоматически")
    domain = str(values.get("STUDIO_DOMAIN", "")).lower().strip()
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,63}", domain) or "example." in domain or ".." in domain:
        raise ValueError("Нужен настоящий домен VPS без схемы и пути")
    owner = str(values.get("TELEGRAM_OWNER_ID", ""))
    if not owner.isdigit() or int(owner) <= 0 or not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{20,}", str(values.get("TELEGRAM_BOT_TOKEN", ""))):
        raise ValueError("Нужны токен Telegram-бота и числовой ID владельца")
    if values.get("CF_FREE_ACCOUNT_CONFIRMED") != "true" or not re.fullmatch(r"[a-fA-F0-9]{32}", str(values.get("CF_ACCOUNT_ID", ""))) or len(str(values.get("CF_API_TOKEN", ""))) < 20:
        raise ValueError("Нужны доступы к подтверждённому бесплатному Cloudflare Workers AI")
    final = dict(values) | {"STUDIO_DOMAIN": domain, "STUDIO_PUBLIC_URL": "https://"+domain,
                            "STUDIO_API_TOKEN": secrets.token_hex(32), "STUDIO_WORKER_TOKEN": secrets.token_hex(32),
                            "STUDIO_CLOUD_ENABLED": "true", "CF_MODEL": "@cf/qwen/qwen3.8-27b",
                            "STUDIO_SERVER_ENABLED": "false", "YOUTUBE_ENABLED": "false", "YOUTUBE_ANALYTICS_ENABLED": "false",
                            "STUDIO_TTS_FALLBACK": "espeak", "PIPER_MODEL": ""}
    lines = (root/".env.example").read_text(encoding="utf-8").splitlines()
    known = set()
    for i,line in enumerate(lines):
        if line and not line.startswith("#") and "=" in line:
            key = line.split("=", 1)[0]
            known.add(key)
            if key in final:
                lines[i] = key+"="+quote(final[key])
    lines += [key+"="+quote(value) for key,value in final.items() if key not in known]
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write("\n".join(lines)+"\n")
    pairing = root/"worker-connection.env"
    if not pairing.exists():
        fd = os.open(pairing, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write("# Секретный файл: перенесите только на Lenovo, не публикуйте.\n")
            out.write("STUDIO_SERVER_ENABLED=false\nSTUDIO_API_URL="+quote("https://"+domain)+"\nSTUDIO_WORKER_TOKEN="+quote(final["STUDIO_WORKER_TOKEN"])+"\nSTUDIO_ENCODER=auto\n")
    return True


def configure(root: Path):
    if (root/".env").exists():
        print("Существующий .env сохранён без перезаписи. Запуск будет использовать его настройки.")
        return
    if not sys.stdin.isatty():
        raise ValueError("Первый запуск требует интерактивного SSH-терминала для скрытого ввода секретов")
    print("Первичная настройка VPS. Секреты вводятся скрыто и не отправляются в чат.")
    domain = input("Домен, направленный на этот VPS: ").strip()
    owner = input("Числовой Telegram ID владельца: ").strip()
    bot = getpass.getpass("Токен бота от BotFather: ")
    account = getpass.getpass("Cloudflare Account ID: ")
    token = getpass.getpass("Cloudflare API Token с доступом Workers AI: ")
    if input("Подтвердите Free-аккаунт без платёжного подключения: введите FREE: ").strip() != "FREE":
        raise ValueError("Бесплатность аккаунта не подтверждена. Конфигурация не создана")
    if create_server_config(root, {"STUDIO_DOMAIN": domain, "TELEGRAM_OWNER_ID": owner, "TELEGRAM_BOT_TOKEN": bot,
                                   "CF_ACCOUNT_ID": account, "CF_API_TOKEN": token, "CF_FREE_ACCOUNT_CONFIRMED": "true"}):
        print("Созданы .env и секретный worker-connection.env. Публикация отключена.")
        print("Резервная русская озвучка — eSpeak NG с роботизированным тембром. Piper можно подключить позже.")
