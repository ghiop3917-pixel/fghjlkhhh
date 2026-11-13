"""Запуск готового комплекта Lenovo двойным щелчком, без серверных ключей."""
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    if platform.system() != 'Windows' or sys.version_info < (3, 12):
        raise RuntimeError('Нужен Lenovo с Windows и Python 3.12 или новее')
    os.chdir(ROOT)
    if not (ROOT/'.env').is_file():
        raise RuntimeError('Нет файла подключения. Используйте архив LENOVO с вашего VPS')
    if not shutil.which('ffmpeg'):
        raise RuntimeError('FFmpeg не найден в PATH. Используйте подготовленный Lenovo с установленным FFmpeg')
    python=ROOT/'.worker-venv/Scripts/python.exe'
    if not python.exists():
        subprocess.run([sys.executable,'-m','venv',str(ROOT/'.worker-venv')],check=True)
    check=subprocess.run([str(python),'-c','import httpx, pydantic, dotenv, PIL, fastapi, uvicorn'],capture_output=True)
    if check.returncode:
        subprocess.run([str(python),'-m','pip','install',str(ROOT)],check=True)
    subprocess.run([str(python),'-m','studio','doctor','--profile','worker','--strict'],check=True)
    print('Lenovo подключён. Окно можно свернуть. Для остановки нажмите Ctrl+C.')
    subprocess.run([str(python),'-m','studio','worker'],check=True)


if __name__=='__main__':
    try:
        main()
    except (OSError,RuntimeError,subprocess.SubprocessError) as exc:
        print('Запуск остановлен: '+str(exc),file=sys.stderr)
        raise SystemExit(1)
