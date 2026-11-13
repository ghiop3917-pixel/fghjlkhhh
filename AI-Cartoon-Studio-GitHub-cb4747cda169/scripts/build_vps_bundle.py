"""Создать новый ZIP для VPS, не включая локальные ключи и результаты тестов."""
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from studio.distribution import build_bundle  # noqa: E402 — автономный запуск из каталога scripts

if __name__ == "__main__":
    print(build_bundle(root, root/"releases"))
