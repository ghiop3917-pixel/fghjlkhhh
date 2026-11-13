"""Короткая серверная проверка речи и графики без финального рендера и облаков."""
import json
import os
import shutil
import subprocess
import uuid
import wave

from studio.character_art import character_frame
from studio.config import load
from studio.db import Store
from studio.quality import inspect_audio
from studio.schemas import Character, Scene
from studio.seed import seed
from studio.speech import speech_command


def main():
    settings = load()
    settings.validate_auth()
    if not settings.server_enabled:
        raise ValueError('Серверная проверка разрешена только в серверном окружении')
    store = Store(settings.data)
    if shutil.disk_usage(store.root).free < 1024**3:
        raise ValueError('Для материалов нужно минимум 1 ГБ свободного места')
    seed(store)
    folder=store.root/'startup-checks'/uuid.uuid4().hex
    folder.mkdir(parents=True)
    audio=folder/'voice.wav'
    command=speech_command(os.getenv('PIPER_MODEL',''), audio)
    result=subprocess.run(command, input='Проверка студии. Голос работает, можно готовить новую историю.\n', text=True, capture_output=True, timeout=90)
    if result.returncode or not audio.is_file():
        raise ValueError('Пробная озвучка не создана. Проверьте движок и модель голоса')
    report=inspect_audio(audio)
    if not report['pass']:
        raise ValueError('Пробная озвучка слишком тиха или перегружена')
    with wave.open(str(audio),'rb') as wav:
        if not 1 <= wav.getnframes()/wav.getframerate() <= 30:
            raise ValueError('Некорректная длительность пробной речи')
    character=Character.model_validate(store.get('character','max'))
    character_frame(character,Scene(character_id='max',text='Проверка',setting='island'),folder/'character.png')
    (folder/'report.json').write_text(json.dumps({'pass':True,'scope':'speech_and_image_only','cloud_checked':False},ensure_ascii=False),encoding='utf-8')
    print('Серверная проверка речи, графики, записи данных и места на диске пройдена. MP4 не создавался.')


if __name__=='__main__':
    main()
