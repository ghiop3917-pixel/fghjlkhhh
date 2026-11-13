"""Переносимый исходный комплект: allowlist, контрольные суммы, без рабочих данных."""
import hashlib
import json
import uuid
import zipfile
from pathlib import Path

DIRECTORIES = ("studio", "deploy", "scripts", "docs", "examples", "tests")
TOP_FILES = ("README.md", "pyproject.toml", "compose.yaml", ".env.example", ".gitignore", ".dockerignore", "Запуск.cmd")
BLOCKED = {"__pycache__", ".git", ".venv", "data", "worker-data", "reports", "releases", "build", ".env", "worker-connection.env"}


def build_bundle(root: Path, output: Path):
    root = root.resolve()
    selected = [root/name for name in TOP_FILES if (root/name).is_file()]
    for directory in DIRECTORIES:
        folder = root/directory
        if folder.exists():
            selected.extend(p for p in folder.rglob("*") if p.is_file())
    contents = {}
    for path in sorted(selected):
        relative = path.relative_to(root)
        if any(part in BLOCKED or (part.startswith(".env") and part != ".env.example") for part in relative.parts) or path.suffix in (".pyc", ".pyo", ".log", ".sqlite3", ".zip", ".whl"):
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError("Ссылка на внешний файл не включается в комплект")
        contents[relative.as_posix()] = path.read_bytes()
    required = {"compose.yaml", "pyproject.toml", ".env.example", "studio/worker.py", "deploy/start.py"}
    if "deploy/archive_start.py" in contents:
        contents["__main__.py"] = contents["deploy/archive_start.py"]
    if not required <= contents.keys():
        raise ValueError("В комплекте отсутствуют обязательные файлы проекта")
    # Дополнительная защита от случайной вставки текущих ключей в исходники/документы.
    if (root/".env").is_file():
        secret_keys = {"STUDIO_API_TOKEN", "STUDIO_WORKER_TOKEN", "TELEGRAM_BOT_TOKEN", "CF_API_TOKEN", "GEMINI_API_KEY", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN", "YOUTUBE_DATA_API_KEY", "TIKTOK_ACCESS_TOKEN", "TIKTOK_CLIENT_SECRET", "TIKTOK_REFRESH_TOKEN"}
        for line in (root/".env").read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key in secret_keys:
                value = value.strip().strip("'\"")
                if len(value) >= 16 and any(value.encode() in data for data in contents.values()):
                    raise ValueError("В исходном файле обнаружено значение действующего секрета; архив не создан")
    output.mkdir(parents=True, exist_ok=True)
    path = output/("AI-Cartoon-Studio-VPS-"+uuid.uuid4().hex[:12]+".zip")
    manifest = {"version": 1, "files": {name: {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)} for name,data in contents.items()}}
    def write_file(archive, name, data):
        info = zipfile.ZipInfo(name)
        info.create_system = 3
        info.external_attr = 0o100644 << 16
        info.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(info, data)

    with zipfile.ZipFile(path, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for name,data in contents.items():
            write_file(archive, name, data)
        write_file(archive, "BUNDLE_MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    with zipfile.ZipFile(path) as archive:
        for name, info in manifest["files"].items():
            if hashlib.sha256(archive.read(name)).hexdigest() != info["sha256"]:
                raise ValueError("Проверка архива не пройдена")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with path.with_suffix(".sha256").open("x", encoding="ascii") as out:
        out.write(digest+"  "+path.name+"\n")
    return path


def build_worker_bundle(root: Path):
    """Секретный комплект одного Worker. Никогда не размещается в static."""
    import os
    root=root.resolve()
    pairing=root/'worker-connection.env'
    if not pairing.is_file() or pairing.is_symlink():
        raise ValueError('Не найден безопасный файл подключения Lenovo')
    allowed={'STUDIO_SERVER_ENABLED','STUDIO_API_URL','STUDIO_WORKER_TOKEN','STUDIO_ENCODER'}
    values=pairing.read_text(encoding='utf-8')
    keys={line.split('=',1)[0] for line in values.splitlines() if line and not line.startswith('#')}
    if not keys <= allowed or 'STUDIO_WORKER_TOKEN' not in keys:
        raise ValueError('Файл подключения содержит недопустимые параметры')
    output=root/('LENOVO-'+uuid.uuid4().hex[:12]+'.zip')
    fd=os.open(output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'wb') as target, zipfile.ZipFile(target,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted((root/'studio').rglob('*')):
            if path.is_file() and path.suffix in ('.py','.js','.css','.html') and '__pycache__' not in path.parts:
                if path.is_symlink():
                    raise ValueError('Ссылки не включаются в комплект Lenovo')
                archive.write(path,path.relative_to(root).as_posix())
        for name in ('pyproject.toml','deploy/worker_start.py','deploy/worker_start.cmd'):
            archive.write(root/name,name)
        archive.writestr('.env',values)
        archive.writestr('ЗАПУСТИТЬ.cmd','@echo off\r\ncall "%~dp0deploy\\worker_start.cmd"\r\n')
    return output
