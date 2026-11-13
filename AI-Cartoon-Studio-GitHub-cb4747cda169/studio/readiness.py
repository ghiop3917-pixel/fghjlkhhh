"""Диагностика настроек без сети, генерации материалов и раскрытия секретов."""
import importlib.util
import os
import platform
import shutil
from pathlib import Path
from urllib.parse import urlparse

from studio.config import Settings


def readiness(settings: Settings, profile="worker", env=None, root: Path | None = None):
    if profile not in ("worker", "server", "deployment"):
        raise ValueError("Профиль диагностики: worker, server или deployment")
    env = os.environ if env is None else env
    root = (root or Path.cwd()).resolve()
    checks = []

    def add(id, label, status, detail, action=""):
        checks.append({"id": id, "label": label, "status": status, "detail": detail, "action": action})

    def enabled(key):
        return env.get(key, "false").lower() == "true"

    def tool(name):
        found = bool(shutil.which(name))
        add("tool."+name, name, "ok" if found else "missing", "Найден в PATH" if found else "Не найден в PATH", "" if found else "Установите на соответствующей машине, не меняя другие зависимости")

    def remote_https(value):
        try:
            parsed = urlparse(value)
            if parsed.port == 0:
                return False
            return parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username and not parsed.password and not parsed.hostname.endswith("example.org") and parsed.hostname not in ("localhost", "127.0.0.1", "::1")
        except ValueError:
            return False

    if profile == "worker":
        add("platform", "Машина рендера", "ok" if platform.system() == "Windows" else "missing", "Windows Worker запускается только на Windows")
        for name in ("ffmpeg", "ffprobe"):
            tool(name)
        token = env.get("STUDIO_WORKER_TOKEN", settings.worker_token)
        add("worker.auth", "Токен Worker", "ok" if len(token) >= 32 else "missing", "Проверяется только наличие и длина, не авторизация на сервере", "Тот же отдельный Worker-токен должен быть задан на VPS и Lenovo")
        url = env.get("STUDIO_API_URL", "http://127.0.0.1:8000")
        if remote_https(url):
            add("worker.url", "Адрес VPS", "ok", "Задан удалённый HTTPS-адрес; соединение не проверялось")
        else:
            add("worker.url", "Адрес VPS", "missing", "Нет рабочего удалённого HTTPS-адреса", "Задайте STUDIO_API_URL после подключения VPS; loopback пригоден только для локального теста")
        add("encoder", "Кодировщик", "warning", "Наличие NVENC в списке FFmpeg не доказывает совместимость с драйвером", "Worker проверит запуск NVENC и при необходимости выберет libx264")
        encoder = env.get("STUDIO_ENCODER", "auto")
        add("encoder.mode", "Настройка кодировщика", "ok" if encoder in ("auto", "libx264", "h264_nvenc") else "missing", "Режим поддерживается" if encoder in ("auto", "libx264", "h264_nvenc") else "Неизвестный режим", "Используйте STUDIO_ENCODER=auto для проверки NVENC и резерва CPU")
        add("server.separation", "Разделение нагрузки", "warning" if settings.server_enabled else "ok", "Серверные процессы разрешены в настройках" if settings.server_enabled else "Серверная подготовка на Lenovo отключена", "На Lenovo оставьте STUDIO_SERVER_ENABLED=false")
    else:
        try:
            settings.validate_auth()
            add("server.auth", "Раздельные токены", "ok", "Два разных токена достаточной длины заданы")
        except ValueError:
            add("server.auth", "Раздельные токены", "missing", "Токены отсутствуют, слишком короткие либо совпадают", "Создайте конфигурацию через scripts/setup_env.py; существующий .env не перезаписывайте")
        if profile == "server":
            add("server.enabled", "Серверный режим", "ok" if settings.server_enabled else "missing", "Разрешён" if settings.server_enabled else "Отключён", "Включайте STUDIO_SERVER_ENABLED только на сервере или в явном локальном тесте")
            speech_tool = env.get("ESPEAK_EXECUTABLE", "espeak-ng") if not env.get("PIPER_MODEL") and env.get("STUDIO_TTS_FALLBACK") == "espeak" else env.get("PIPER_EXECUTABLE", "piper")
            for name in ("ffmpeg", "ffprobe", speech_tool):
                tool(name)
        else:
            for name in ("compose.yaml", "deploy/Dockerfile", "deploy/Caddyfile", "pyproject.toml"):
                add("file."+name, name, "ok" if (root/name).is_file() else "missing", "Файл найден" if (root/name).is_file() else "Файл отсутствует")
            add("docker", "Docker-сборка", "warning", "Docker найден, но сборка не запускалась" if shutil.which("docker") else "Docker отсутствует здесь; сборка не проверена", "На VPS выполните docker compose build и проверку сервисов")
            add("server.enabled", "Серверный режим Compose", "ok", "Compose включает серверный режим для серверных контейнеров; локальный .env менять для этого не нужно")
        bot = env.get("TELEGRAM_BOT_TOKEN", settings.bot_token)
        owner = env.get("TELEGRAM_OWNER_ID", settings.owner_id)
        add("telegram", "Telegram владельца", "ok" if bot and owner.isdigit() and int(owner) > 0 else "missing", "Проверяется заполнение токена и числового ID, не связь с Telegram", "Задайте TELEGRAM_BOT_TOKEN и TELEGRAM_OWNER_ID только на сервере")
        public = env.get("STUDIO_PUBLIC_URL", "")
        add("public.url", "HTTPS панели", "ok" if remote_https(public) else "missing", "Задан адрес; DNS и сертификат не проверялись" if remote_https(public) else "Нужен реальный публичный HTTPS-адрес", "Укажите STUDIO_PUBLIC_URL, STUDIO_DOMAIN и настройте DNS на VPS")
        if profile == "deployment":
            domain = env.get("STUDIO_DOMAIN", "")
            try:
                matches = bool(domain) and urlparse(public).hostname == domain and remote_https(public)
            except ValueError:
                matches = False
            add("public.domain", "Домен Caddy и панели", "ok" if matches else "missing", "Домены совпадают" if matches else "Домен отсутствует, шаблонный или не совпадает с адресом панели")
        cf = enabled("STUDIO_CLOUD_ENABLED") and enabled("CF_FREE_ACCOUNT_CONFIRMED") and bool(env.get("CF_ACCOUNT_ID")) and bool(env.get("CF_API_TOKEN")) and env.get("CF_MODEL", "@cf/qwen/qwen3.8-27b") == "@cf/qwen/qwen3.8-27b"
        gemini = enabled("STUDIO_CLOUD_ENABLED") and enabled("GEMINI_FREE_ACCOUNT_CONFIRMED") and bool(env.get("GEMINI_API_KEY")) and bool(env.get("GEMINI_MODEL"))
        local = bool(env.get("OLLAMA_MODEL"))
        if local:
            try:
                local = urlparse(env.get("OLLAMA_URL", "http://127.0.0.1:11434")).hostname in ("localhost", "127.0.0.1", "::1")
            except ValueError:
                local = False
        add("models", "Модель для идей и сценариев", "ok" if cf or gemini or (local and profile == "server") else "missing", "Есть настроенный маршрут; доступность, качество, регион и тариф не проверялись" if cf or gemini or local else "Нет включённого и заполненного маршрута модели", "Выберите разрешённый бесплатный провайдер; в Compose localhost Ollama не означает localhost хоста")
        if cf or gemini:
            add("free.billing", "Нулевой расход", "warning", "Флаг Free в .env не является проверкой billing поставщика", "Проверьте тариф и отсутствие платёжного подключения в аккаунте до включения реальных запросов")
        if local and profile == "deployment":
            add("ollama.container", "Ollama в контейнерах", "missing", "В базовом Compose нет Ollama; адрес localhost указывает внутрь контейнера", "Используйте native-развёртывание на VPS либо отдельно настройте безопасную серверную интеграцию")
        model = env.get("PIPER_MODEL", "")
        model_path = Path(model) if model else None
        if profile == "deployment" and model.startswith("/app/voices/"):
            candidate = (root/"voices"/model.removeprefix("/app/voices/")).resolve()
            model_path = candidate if candidate.is_relative_to((root/"voices").resolve()) else None
        elif profile == "deployment":
            model_path = None
        voice_ok = bool(model_path and model_path.is_file() and Path(str(model_path)+".json").is_file())
        if not model and env.get("STUDIO_TTS_FALLBACK") == "espeak":
            installed = bool(shutil.which(env.get("ESPEAK_EXECUTABLE", "espeak-ng")))
            add("voice", "Резервная озвучка eSpeak NG", "warning" if profile == "deployment" else "ok" if installed else "missing", "Включён роботизированный русский голос; в Dockerfile предусмотрена установка движка" if profile == "deployment" else "Движок найден; речь нужно прослушать" if installed else "Движок не найден на сервере", "Для более естественного голоса подключите Piper; выбранная модель не заменяется резервом автоматически")
        else:
            add("voice", "Piper и файл голоса", "ok" if voice_ok else "missing", "Модель и JSON найдены; совместимость и речь не проверялись" if voice_ok else "Не найдена пара файлов ONNX и ONNX.JSON", "Выберите русский голос с проверенной лицензией; для Compose поместите файлы в voices и укажите /app/voices/имя.onnx")
        add("voice.license", "Лицензия голоса", "warning", "Лицензия конкретной модели должна проверяться отдельно", "Не считайте лицензию движка лицензией всех голосов")
        add("publication", "Публикации", "warning" if enabled("YOUTUBE_ENABLED") else "disabled", "YouTube включён; нужны OAuth и отдельное согласование каждого MP4" if enabled("YOUTUBE_ENABLED") else "Автоматическая загрузка выключена; это не мешает подготовке и рендеру")
    for package in ("httpx", "dotenv"):
        add("python."+package, "Библиотека "+package, "ok" if importlib.util.find_spec(package) else "missing", "Проверяется установка, не совместимость всего окружения")
    return {"profile": profile, "configuration_ready": not any(c["status"] == "missing" for c in checks),
            "live_verified": False, "network_requests": 0, "checks": checks,
            "summary": {status: sum(c["status"] == status for c in checks) for status in ("ok", "missing", "warning", "disabled")}}


def text_report(report):
    labels = {"ok": "ЕСТЬ", "missing": "НУЖНО", "warning": "ПРОВЕРИТЬ", "disabled": "ВЫКЛЮЧЕНО"}
    lines = ["Диагностика AI Cartoon Studio · " + report["profile"], "Это проверка конфигурации без сети, а не подтверждение работы интеграций."]
    for check in report["checks"]:
        lines.append(f"[{labels[check['status']]}] {check['label']}: {check['detail']}")
        if check["action"]:
            lines.append("  → " + check["action"])
    lines.append("Секреты не выводятся. Платные запросы и генерация не выполнялись.")
    return "\n".join(lines)
