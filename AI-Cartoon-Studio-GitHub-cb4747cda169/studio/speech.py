"""Piper и явно включаемая бесплатная резервная CPU-озвучка eSpeak NG."""
import os
import shutil
from pathlib import Path

from studio.agents import Unavailable


def speech_command(model: str, output: Path):
    if model:
        executable = os.getenv("PIPER_EXECUTABLE", "piper")
        if not shutil.which(executable) or not Path(model).is_file() or not Path(model+".json").is_file():
            raise Unavailable("Не подключена выбранная модель Piper с JSON; голос не заменяется автоматически")
        return [executable, "-m", model, "-f", str(output)]
    if os.getenv("STUDIO_TTS_FALLBACK", "none") == "espeak":
        executable = os.getenv("ESPEAK_EXECUTABLE", "espeak-ng")
        if not shutil.which(executable):
            raise Unavailable("Резервный eSpeak NG не установлен на сервере")
        speed = int(os.getenv("STUDIO_TTS_WPM", "150"))
        if not 100 <= speed <= 220:
            raise ValueError("Темп резервной речи должен быть от 100 до 220 слов/мин")
        return [executable, "--stdin", "-v", "ru", "-s", str(speed), "-w", str(output)]
    raise Unavailable("Подключите Piper с голосом или явно включите резерв STUDIO_TTS_FALLBACK=espeak")
