"""Сборка wheel без скачивания зависимостей и без включения .env/данных."""
import os
import uuid
import zipfile
from pathlib import Path

from setuptools import build_meta

root = Path(__file__).resolve().parents[1]
output = root / "reports" / ("package-build-"+uuid.uuid4().hex[:12])
output.mkdir(parents=True, exist_ok=False)
os.chdir(root)
# Прямой локальный backend не использует кеш/временную установку pip и ничего не скачивает.
build_meta.build_wheel(str(output))
wheels = list(output.glob("*.whl"))
if len(wheels) != 1:
    raise RuntimeError("Не получен единственный пакет проекта")
with zipfile.ZipFile(wheels[0]) as wheel:
    names = set(wheel.namelist())
    required = {"studio/cli.py", "studio/readiness.py", "studio/story_ideas.py", "studio/worker.py", "studio/static/index.html", "studio/static/app.js", "studio/static/style.css"}
    if not required <= names:
        raise RuntimeError("В пакете отсутствуют необходимые модули или интерфейс")
    if any(n == ".env" or n.startswith(("data/", "worker-data/", "reports/", ".git/")) for n in names):
        raise RuntimeError("В пакете обнаружены непредназначенные для распространения файлы")
    for name in names:
        if name.startswith("studio/") and not name.endswith("/"):
            source = root/name
            if not source.is_file() or source.read_bytes() != wheel.read(name):
                raise RuntimeError("В пакете обнаружен устаревший или неожиданный исходный файл")
print("Пакет собран без сети. Секреты и рабочие данные не включены.")
print(wheels[0])
