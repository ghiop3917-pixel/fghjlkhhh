import importlib.util
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from studio.distribution import build_worker_bundle


def bootstrap(monkeypatch,tmp_path):
    path=Path(__file__).resolve().parents[1]/'deploy/bootstrap.py'
    spec=importlib.util.spec_from_file_location('bootstrap_fixture',path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.platform,'system',lambda:'Linux')
    monkeypatch.setattr(module.os,'geteuid',lambda:0,raising=False)
    monkeypatch.setattr(module.shutil,'which',lambda _:None)
    (tmp_path/'etc/apt/sources.list.d').mkdir(parents=True)
    (tmp_path/'etc/os-release').write_text('ID=ubuntu\nVERSION_ID="24.04"\nVERSION_CODENAME=noble\n')
    return module


def test_clean_bootstrap_installs_without_removal_or_shell(monkeypatch,tmp_path):
    module=bootstrap(monkeypatch,tmp_path)
    commands=[]
    def run(command,**kwargs):
        commands.append(command)
        assert not kwargs.get('shell')
        return SimpleNamespace(stdout='amd64' if '--print-architecture' in command else '',returncode=0)
    module.install_docker(system=tmp_path,run=run,fetch=lambda _:b'-----BEGIN PGP PUBLIC KEY BLOCK-----\nfixture')
    installs=[c for c in commands if c[:2]==['apt-get','install']]
    assert installs and all('--no-remove' in c for c in installs)
    assert not any('remove' in c or 'purge' in c for c in commands)
    assert (tmp_path/'etc/apt/sources.list.d/cartoon-docker.sources').is_file()


def test_bootstrap_preserves_conflicting_container_packages(monkeypatch,tmp_path):
    module=bootstrap(monkeypatch,tmp_path)
    commands=[]
    def run(command,**kwargs):
        commands.append(command)
        return SimpleNamespace(stdout='amd64' if '--print-architecture' in command else 'install ok installed')
    with pytest.raises(RuntimeError,match='сохранён'):
        module.install_docker(system=tmp_path,run=run,fetch=lambda _:pytest.fail('Сеть запрещена'))
    assert not any(c[0]=='apt-get' for c in commands)


def test_existing_docker_is_only_checked(monkeypatch,tmp_path):
    module=bootstrap(monkeypatch,tmp_path)
    monkeypatch.setattr(module.shutil,'which',lambda _:'docker')
    commands=[]
    module.install_docker(system=tmp_path,run=lambda c,**k:commands.append(c))
    assert [c[0] for c in commands]==['docker','docker']


def test_failed_package_install_does_not_start_docker(monkeypatch,tmp_path):
    module=bootstrap(monkeypatch,tmp_path)
    def run(command,**kwargs):
        if command[0]=='apt-get':
            raise subprocess.CalledProcessError(1,command)
        return SimpleNamespace(stdout='amd64' if '--print-architecture' in command else '')
    with pytest.raises(subprocess.CalledProcessError):
        module.install_docker(system=tmp_path,run=run,fetch=lambda _:pytest.fail('Сеть запрещена'))
    assert not (tmp_path/'etc/apt/sources.list.d/cartoon-docker.sources').exists()


def test_worker_bundle_contains_only_scoped_connection(tmp_path):
    (tmp_path/'studio').mkdir()
    (tmp_path/'deploy').mkdir()
    (tmp_path/'studio/worker.py').write_text('fixture')
    for name in ('pyproject.toml','deploy/worker_start.py','deploy/worker_start.cmd'):
        (tmp_path/name).write_text('fixture')
    (tmp_path/'.env').write_text('CF_API_TOKEN=must-not-be-shared\nSTUDIO_API_TOKEN=owner-secret')
    (tmp_path/'worker-connection.env').write_text('STUDIO_SERVER_ENABLED=false\nSTUDIO_API_URL=https://studio.test.net\nSTUDIO_WORKER_TOKEN='+'a'*64+'\nSTUDIO_ENCODER=auto\n')
    bundle=build_worker_bundle(tmp_path)
    with zipfile.ZipFile(bundle) as archive:
        assert 'ЗАПУСТИТЬ.cmd' in archive.namelist()
        assert b'must-not-be-shared' not in archive.read('.env')
        assert b'owner-secret' not in archive.read('.env')
        assert b'STUDIO_WORKER_TOKEN' in archive.read('.env')


def test_archive_entry_resumes_without_overwriting_settings(tmp_path):
    import hashlib
    import json
    path=Path(__file__).resolve().parents[1]/'deploy/archive_start.py'
    spec=importlib.util.spec_from_file_location('archive_fixture',path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    contents={'deploy/bootstrap.py':b'fixture','deploy/start.py':b'fixture','compose.yaml':b'fixture'}
    manifest={k:{'sha256':hashlib.sha256(v).hexdigest(),'size':len(v)} for k,v in contents.items()}
    archive=tmp_path/'source.zip'
    with zipfile.ZipFile(archive,'w') as output:
        for name,data in contents.items():
            output.writestr(name,data)
        output.writestr('BUNDLE_MANIFEST.json',json.dumps({'files':manifest}))
    folder=module.unpack(archive,tmp_path/'install')
    (folder/'.env').write_text('owner-settings')
    assert module.unpack(archive,tmp_path/'install')==folder
    assert (folder/'.env').read_text()=='owner-settings'
    (folder/'compose.yaml').write_text('user-change')
    with pytest.raises(ValueError,match='сохранены'):
        module.unpack(archive,tmp_path/'install')
    assert (folder/'compose.yaml').read_text()=='user-change'
