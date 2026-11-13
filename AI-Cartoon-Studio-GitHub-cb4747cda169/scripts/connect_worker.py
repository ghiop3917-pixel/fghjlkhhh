"""Запускается владельцем на Lenovo; --apply явно разрешает обновить подключение."""
import argparse
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from studio.worker_setup import connect_worker  # noqa: E402

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Подключить Lenovo к VPS без потери существующих настроек")
    parser.add_argument("file", nargs="?", help="Секретный worker-connection.env с VPS")
    parser.add_argument("--apply", action="store_true", help="Применить проверенные параметры, сохранив резервную копию .env")
    args = parser.parse_args()
    path = Path((args.file or input("Путь к worker-connection.env: ")).strip().strip('"'))
    try:
        result = connect_worker(root, path, args.apply)
        print("Подключение сохранено. Теперь запустите python -m studio worker." if result["applied"] else "Файл проверен. Для применения добавьте --apply. Секреты не выводятся.")
        if result.get("backup"):
            print("Предыдущая конфигурация сохранена в секретной резервной копии: "+result["backup"])
    except (ValueError, OSError):
        parser.exit(1, "Не удалось применить файл подключения. Проверьте путь, права и параметры; секреты не выводятся.\n")
