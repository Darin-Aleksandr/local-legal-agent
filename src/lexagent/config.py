"""Где лежат данные и какими моделями работать. Всё настраивается в config.toml рядом с программой."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_HOME = Path.home() / "ЮрАгент"


@dataclass(frozen=True)
class Models:
    chat: str = "qwen3.5:9b"  # черновики, разбор документов, сравнение позиций
    embed: str = "bge-m3"  # поиск по смыслу, хорошо понимает русский


@dataclass(frozen=True)
class Settings:
    home: Path = DEFAULT_HOME
    ollama_url: str = "http://127.0.0.1:11434"
    models: Models = field(default_factory=Models)
    tesseract_lang: str = "rus+eng"
    user_agent: str = "lexagent/0.1 (+https://github.com/Darin-Aleksandr/local-legal-agent)"

    @property
    def inbox(self) -> Path:
        return self.home / "Входящие"

    @property
    def cases(self) -> Path:
        return self.home / "Дела"

    @property
    def library(self) -> Path:
        return self.home / "Библиотека"

    def ensure(self) -> None:
        for path in (self.inbox, self.cases, self.library):
            path.mkdir(parents=True, exist_ok=True)


def load(path: Path | None = None) -> Settings:
    """config.toml рядом с программой или по переменной LEXAGENT_CONFIG; без файла — значения по умолчанию."""
    path = path or Path(os.environ.get("LEXAGENT_CONFIG", "config.toml"))
    if not path.is_file():
        return Settings()
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    models = Models(**raw.get("models", {}))
    home = Path(raw["home"]).expanduser() if raw.get("home") else DEFAULT_HOME
    rest = {k: v for k, v in raw.items() if k in {"ollama_url", "tesseract_lang"}}
    return Settings(home=home, models=models, **rest)
