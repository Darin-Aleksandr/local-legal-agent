"""Локальная нейросеть через Ollama: ответы в строгом JSON и эмбеддинги для поиска.

Нейросеть здесь — исполнитель узких задач (вытащить поля из скана, написать абзац по найденным
источникам, сравнить две позиции). Факты, расчёты и проверки делает код, а не модель.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

import httpx
from pydantic import BaseModel

from lexagent.config import Settings


class LLMError(RuntimeError):
    pass


class LLM(Protocol):
    def ask(self, system: str, user: str, schema: type[BaseModel] | None = None) -> str: ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...


def parse[T: BaseModel](llm: LLM, system: str, user: str, schema: type[T]) -> T:
    """Ответ модели, проверенный по схеме: лишнее отбрасывается, кривой JSON — ошибка, а не догадка."""
    raw = llm.ask(system, user, schema)
    try:
        return schema.model_validate_json(raw)
    except ValueError as exc:
        raise LLMError(f"модель вернула ответ не по схеме: {raw[:300]}") from exc


class Ollama:
    def __init__(
        self, settings: Settings, *, timeout: float = 600, transport: httpx.BaseTransport | None = None
    ) -> None:
        self._url = settings.ollama_url.rstrip("/")
        self._chat = settings.models.chat
        self._embed = settings.models.embed
        self._http = httpx.Client(timeout=timeout, transport=transport)

    def ask(self, system: str, user: str, schema: type[BaseModel] | None = None) -> str:
        body: dict[str, Any] = {
            "model": self._chat,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "stream": False,
            "think": False,
            # temperature 0: одинаковый вход — одинаковый ответ; большой контекст под документы дела
            "options": {"temperature": 0, "num_ctx": 16384},
        }
        if schema is not None:
            body["format"] = schema.model_json_schema()
        try:
            response = self._http.post(f"{self._url}/api/chat", json=body)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise LLMError(f"Ollama недоступна ({self._url}): {exc}. Запущена ли Ollama?") from exc
        return str(response.json()["message"]["content"])

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            response = self._http.post(f"{self._url}/api/embed", json={"model": self._embed, "input": texts})
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise LLMError(f"Ollama недоступна ({self._url}): {exc}") from exc
        return [list(map(float, v)) for v in response.json()["embeddings"]]

    def models(self) -> list[str]:
        try:
            response = self._http.get(f"{self._url}/api/tags", timeout=5)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise LLMError(f"Ollama недоступна ({self._url}): {exc}") from exc
        return [m["name"] for m in response.json().get("models", [])]


def as_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)
