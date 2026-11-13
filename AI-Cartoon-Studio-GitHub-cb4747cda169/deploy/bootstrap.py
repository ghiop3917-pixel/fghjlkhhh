"""Одна команда для чистого Ubuntu VPS; выполняется владельцем на сервере."""
import os
import platform
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def install_docker(*, system=Path('/'), run=subprocess.run, fetch=None):
    if platform.system() != 'Linux' or os.geteuid() != 0:
        raise RuntimeError('Запустите на Ubuntu VPS: sudo python3 deploy/bootstrap.py')
    if shutil.which('docker'):
        run(['docker', 'compose', 'version'], check=True)
        run(['docker', 'info', '--format', '{{.ServerVersion}}'], check=True)
        return
    release = dict(line.split('=', 1) for line in (system/'etc/os-release').read_text().splitlines() if '=' in line)
    if release.get('ID', '').strip('"') != 'ubuntu' or release.get('VERSION_ID', '').strip('"') != '24.04':
        raise RuntimeError('Автоустановка подготовлена для Ubuntu 24.04. На другом Linux сначала установите Docker самостоятельно')
    codename = release.get('VERSION_CODENAME', '').strip('"')
    if not re.fullmatch('[a-z]+', codename):
        raise RuntimeError('Не удалось определить выпуск Ubuntu')
    architecture = run(['dpkg', '--print-architecture'], capture_output=True, text=True, check=True).stdout.strip()
    if architecture not in ('amd64', 'arm64'):
        raise RuntimeError('Поддерживаются amd64 и arm64')
    conflicts = ('docker.io', 'docker-compose', 'docker-compose-v2', 'podman-docker', 'containerd', 'runc')
    for package in conflicts:
        state = run(['dpkg-query', '-W', '-f=${Status}', package], capture_output=True, text=True)
        if 'install ok installed' in state.stdout:
            raise RuntimeError('Найден существующий контейнерный пакет '+package+'. Он сохранён; требуется отдельная настройка Docker')
    apt = system/'etc/apt'
    key = apt/'keyrings/cartoon-docker.asc'
    source = apt/'sources.list.d/cartoon-docker.sources'
    text = f'Types: deb\nURIs: https://download.docker.com/linux/ubuntu\nSuites: {codename}\nComponents: stable\nArchitectures: {architecture}\nSigned-By: /etc/apt/keyrings/cartoon-docker.asc\n'
    sources = [apt/'sources.list', *sorted((apt/'sources.list.d').glob('*'))]
    if any(p != source and p.is_file() and 'download.docker.com' in p.read_text(errors='replace') for p in sources):
        raise RuntimeError('Уже есть репозиторий Docker. Существующие настройки не заменяются')
    resumed = key.is_file() and source.is_file() and source.read_text() == text and key.read_bytes().startswith(b'-----BEGIN PGP PUBLIC KEY BLOCK-----')
    if (key.exists() or source.exists()) and not resumed:
        raise RuntimeError('Файлы установки отличаются от ожидаемых; автоматическая перезапись запрещена')
    run(['apt-get', 'update'], check=True)
    run(['apt-get', 'install', '-y', '--no-remove', 'ca-certificates'], check=True)
    if fetch is None and not resumed:
        def fetch(url):
            with urllib.request.urlopen(url, timeout=30) as response:
                return response.read(65537)
    if not resumed:
        data = fetch('https://download.docker.com/linux/ubuntu/gpg')
        if len(data) > 65536 or not data.startswith(b'-----BEGIN PGP PUBLIC KEY BLOCK-----'):
            raise RuntimeError('Сервер Docker вернул некорректный ключ репозитория')
        key.parent.mkdir(parents=True, exist_ok=True)
        with key.open('xb') as output:
            output.write(data)
        key.chmod(0o644)
        with source.open('x') as output:
            output.write(text)
        source.chmod(0o644)
    run(['apt-get', 'update'], check=True)
    run(['apt-get', 'install', '-y', '--no-remove', 'docker-ce', 'docker-ce-cli', 'containerd.io', 'docker-buildx-plugin', 'docker-compose-plugin'], check=True)
    run(['systemctl', 'enable', '--now', 'docker'], check=True)
    run(['docker', 'info', '--format', '{{.ServerVersion}}'], check=True)


def main():
    install_docker()
    subprocess.run([sys.executable, str(ROOT/'deploy/start.py')], cwd=ROOT, check=True)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print('Установка остановлена: '+str(exc), file=sys.stderr)
        raise SystemExit(1)
