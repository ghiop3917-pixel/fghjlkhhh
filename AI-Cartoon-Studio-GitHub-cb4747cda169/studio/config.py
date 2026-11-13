import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


def flag(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).lower() == "true"


@dataclass
class Settings:
    data: Path = field(default_factory=lambda: Path(os.getenv("STUDIO_DATA", "data")).resolve())
    api_token: str = field(default_factory=lambda: os.getenv("STUDIO_API_TOKEN", ""))
    worker_token: str = field(default_factory=lambda: os.getenv("STUDIO_WORKER_TOKEN", ""))
    server_enabled: bool = field(default_factory=lambda: flag("STUDIO_SERVER_ENABLED"))
    owner_id: str = field(default_factory=lambda: os.getenv("TELEGRAM_OWNER_ID", ""))
    bot_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))

    def validate_auth(self):
        if min(len(self.api_token), len(self.worker_token)) < 32:
            raise ValueError("Задайте два разных секретных токена длиной не менее 32 символов")
        if self.api_token == self.worker_token:
            raise ValueError("Токены владельца и Worker должны различаться")


def load() -> Settings:
    load_dotenv()
    return Settings()

