"""Точка входа исходного ZIP: python3 AI-Cartoon-Studio-VPS-....zip."""
import hashlib
import json
import os
import platform
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath


def unpack(archive_path, base):
    archive_path=Path(archive_path).resolve()
    digest=hashlib.sha256(archive_path.read_bytes()).hexdigest()
    target=Path(base)/('cartoon-studio-'+digest[:16])
    with zipfile.ZipFile(archive_path) as archive:
        names=archive.namelist()
        if len(names)!=len(set(names)):
            raise ValueError('В архиве повторяющиеся пути')
        manifest=json.loads(archive.read('BUNDLE_MANIFEST.json'))['files']
        if set(names)!=set(manifest)|{'BUNDLE_MANIFEST.json'}:
            raise ValueError('Список файлов архива не совпал с манифестом')
        if not {'deploy/bootstrap.py','deploy/start.py','compose.yaml'} <= manifest.keys():
            raise ValueError('В архиве нет установщика')
        # Сначала проверяем всё; повреждённый архив не создаёт рабочую папку.
        for name,info in manifest.items():
            path=PurePosixPath(name)
            if path.is_absolute() or '..' in path.parts or '\\' in name or ':' in name:
                raise ValueError('Недопустимый путь в архиве')
            data=archive.read(name)
            if len(data)!=info['size'] or hashlib.sha256(data).hexdigest()!=info['sha256']:
                raise ValueError('Нарушена целостность файла '+name)
            destination=target/name
            if target.exists():
                if destination.is_symlink() or target.is_symlink() or any(p.is_symlink() for p in destination.parents if p!=Path(base).parent):
                    raise ValueError('Ссылки в каталоге установки запрещены')
                if destination.exists() and (not destination.is_file() or destination.read_bytes()!=data):
                    raise ValueError('Исходники в папке установки изменены; они сохранены без перезаписи')
        target.mkdir(mode=0o700,parents=True,exist_ok=True)
        for name in manifest:
            destination=target/name
            if destination.exists():
                continue
            destination.parent.mkdir(parents=True,exist_ok=True)
            with destination.open('xb') as output:
                output.write(archive.read(name))
        # Рабочие .env, секретные комплекты и данные при повторном запуске не заменяются.
    return target


def main():
    if platform.system()!='Linux' or os.geteuid()!=0:
        raise RuntimeError('На VPS выполните sudo python3 ИМЯ_АРХИВА.zip')
    target=unpack(Path(sys.argv[0]),Path('/opt'))
    print('Папка студии: '+str(target),flush=True)
    subprocess.run([sys.executable,str(target/'deploy/bootstrap.py')],cwd=target,check=True)


if __name__=='__main__':
    try:
        main()
    except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as exc:
        print('Запуск остановлен: '+str(exc),file=sys.stderr)
        raise SystemExit(1)
