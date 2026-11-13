import importlib.util
from pathlib import Path
from types import SimpleNamespace


def installer():
    path = Path(__file__).resolve().parents[1]/"deploy"/"start.py"
    spec = importlib.util.spec_from_file_location("cartoon_startup_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SocketFixture:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def bind(self, address):
        assert address[1] in (80, 443)


class HealthFixture:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def setup(monkeypatch, tmp_path, running):
    module = installer()
    (tmp_path/".env").write_text("STUDIO_PUBLIC_URL='https://studio.testserver.net'\n")
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(module.shutil, "which", lambda _: "fixture")
    monkeypatch.setattr(module, "configure", lambda _: None)
    monkeypatch.setattr(module.os, "chdir", lambda _: None)
    monkeypatch.setattr(module.socket, "socket", lambda: SocketFixture())
    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *a,**k: HealthFixture())
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(stdout="container-id" if "ps" in command and running else "", returncode=0)
    monkeypatch.setattr(module.subprocess, "run", run)
    return module, commands


def test_first_boot_checks_before_up_and_seeds_after(monkeypatch, tmp_path):
    module, commands = setup(monkeypatch, tmp_path, False)
    module.main()
    check_index = next(i for i,c in enumerate(commands) if "doctor" in c)
    up_index = next(i for i,c in enumerate(commands) if "up" in c)
    seed_index = next(i for i,c in enumerate(commands) if "seed" in c)
    assert check_index < up_index < seed_index
    assert not any("down" in c or "rm" in c for c in commands)


def test_repeat_boot_does_not_rebuild_or_restart_running_studio(monkeypatch, tmp_path):
    module, commands = setup(monkeypatch, tmp_path, True)
    module.main()
    assert not any("up" in c or "build" in c or "restart" in c for c in commands)
