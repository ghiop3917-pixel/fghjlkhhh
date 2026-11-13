"""Первый запуск на VPS с Docker Compose. Другие сервисы не останавливает."""
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from studio.deployment import configure  # noqa: E402 — автономный запуск из каталога deploy


def main():
    if platform.system() != "Linux":
        raise RuntimeError("Этот установщик запускается на Linux VPS; на Lenovo используйте Worker")
    if not shutil.which("docker"):
        raise RuntimeError("Сначала установите Docker Engine и Compose по официальной инструкции: https://docs.docker.com/engine/install/ubuntu/")
    os.chdir(ROOT)
    project = "cartoon-"+__import__("hashlib").sha256(str(ROOT).encode()).hexdigest()[:8]
    command = ["docker", "compose", "-p", project]
    subprocess.run(["docker", "compose", "version"], check=True)
    configure(ROOT)
    # Значения .env не выводятся через docker compose config.
    subprocess.run(command+["config", "--quiet"], check=True)
    running = subprocess.run(command+["ps", "--status", "running", "--quiet"], capture_output=True, text=True, check=True)
    if running.stdout.strip():
        print("Эта студия уже запущена. Перезапуск и обновление автоматически не выполняются.")
        if (ROOT/"worker-connection.env").is_file():
            from studio.distribution import build_worker_bundle
            print("Секретный комплект Lenovo: "+build_worker_bundle(ROOT).name+". Скачайте по SFTP и откройте ЗАПУСТИТЬ.cmd в отдельной папке на Lenovo.")
        return
    for port in (80, 443):
        with socket.socket() as probe:
            try:
                probe.bind(("0.0.0.0", port))
            except OSError:
                raise RuntimeError("Порт 80/443 занят или недоступен. Другие сервисы не останавливаются") from None
    (ROOT/"voices").mkdir(exist_ok=True)
    subprocess.run(command+["build"], check=True)
    subprocess.run(command+["run", "--no-deps", "--name", project+"-check-"+uuid.uuid4().hex[:8], "api", "python", "-m", "studio", "doctor", "--profile", "server", "--strict"], check=True)
    subprocess.run(command+["run", "--no-deps", "--name", project+"-smoke-"+uuid.uuid4().hex[:8], "api", "python", "-m", "studio.server_smoke"], check=True)
    subprocess.run(command+["up", "-d"], check=True)
    subprocess.run(command+["exec", "-T", "api", "python", "-m", "studio", "seed"], check=True)
    # HTTPS и доступность проверяются отдельно от статической конфигурации.
    public = ""
    for line in (ROOT/".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("STUDIO_PUBLIC_URL="):
            public = line.split("=", 1)[1].strip().strip("'\"")
    from urllib.parse import urlparse
    parsed = urlparse(public)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise RuntimeError("Некорректный HTTPS-адрес панели")
    for _ in range(12):
        try:
            with urllib.request.urlopen(public+"/health", timeout=3) as response:
                if response.status == 200:
                    if (ROOT/"worker-connection.env").is_file():
                        from studio.distribution import build_worker_bundle
                        bundle=build_worker_bundle(ROOT)
                        print("Готов секретный комплект Lenovo: "+bundle.name+". Скачайте его по SFTP, распакуйте в отдельную папку и откройте ЗАПУСТИТЬ.cmd. Не публикуйте архив.")
                    print("HTTPS API отвечает. Откройте бота и /app. Студия предлагает новые идеи ежедневно.")
                    return
        except OSError:
            time.sleep(2)
    raise RuntimeError("Контейнеры запущены, но HTTPS ещё не подтверждён. Проверьте DNS, порты и журнал proxy. Сервисы не удаляются")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print("Запуск остановлен: "+str(exc), file=sys.stderr)
        raise SystemExit(1)
