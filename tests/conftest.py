from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pymupdf as fitz
import pytest
from pydantic import BaseModel

from lexagent.config import Settings

FONT = "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf"


class FakeLLM:
    """Нейросеть-заглушка: ответ по имени схемы. Эмбеддинги — детерминированный «мешок слов»."""

    def __init__(self, answers: dict[str, Any | Callable[[str, str], Any]] | None = None) -> None:
        self.answers = answers or {}
        self.calls: list[tuple[str, str]] = []

    def ask(self, system: str, user: str, schema: type[BaseModel] | None = None) -> str:
        name = schema.__name__ if schema else "text"
        self.calls.append((name, user))
        answer = self.answers.get(name)
        if callable(answer):
            answer = answer(system, user)
        if answer is None:
            raise KeyError(f"FakeLLM: нет ответа для {name}")
        return answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            vec = [0.0] * 64
            for word in text.lower().split():
                vec[int(hashlib.md5(word[:6].encode()).hexdigest(), 16) % 64] += 1.0
            vectors.append(vec)
        return vectors


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings(home=tmp_path / "ЮрАгент")
    s.ensure()
    return s


def make_pdf(path: Path, pages: list[str], *, rotate: int = 0) -> Path:
    doc = fitz.open()
    for text in pages:
        page = doc.new_page(width=595, height=842)
        page.insert_font(fontname="lib", fontfile=FONT)
        page.insert_textbox(fitz.Rect(60, 60, 540, 800), text, fontname="lib", fontsize=13)
        if rotate:
            page.set_rotation(rotate)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    doc.close()
    return path
