import json
import re
from datetime import date
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel

from lexagent.config import Settings
from lexagent.library.fetch import Fetcher, FetchError, edition, first_date, ks_number, vs_number
from lexagent.llm import LLMError, Ollama, parse
from tests.conftest import make_pdf

LAW_HTML = """<html><head><title>Федеральный закон от 25.04.2002 N 40-ФЗ (ред. от 01.09.2026) "Об ОСАГО"</title></head>
<body><nav>меню</nav><h1>40-ФЗ</h1><script>x()</script><a href="/document/cons_doc_LAW_36528/a1/">Статья 12. Порядок</a>
<p>Текст</p></body></html>"""
DDG = """<a href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fvsrf.ru%2Flk%2Fpractice%2Fstor_pdf%2F123&rut=x">r</a>
<a href="https://evil.example/stor_pdf/1">bad</a><a href="https://vsrf.ru/lk/practice/stor_pdf/456">ok</a>"""


def fetcher(tmp_path: Path, calls: list[str] | None = None) -> Fetcher:
    pdf = make_pdf(tmp_path / "d.pdf", ["ОПРЕДЕЛЕНИЕ\nот 1 октября 2026 г. № 18-КГ26-50-К4\nтекст"]).read_bytes()
    flaky = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        (calls if calls is not None else []).append(url)
        if "consultant" in url:
            return httpx.Response(200, html=LAW_HTML)
        if "duckduckgo" in url:
            return httpx.Response(200, html=DDG)
        if url.endswith("/doc.pdf"):
            return httpx.Response(200, content=pdf, headers={"content-type": "application/pdf"})
        if url.endswith("/flaky"):
            flaky["n"] += 1
            return httpx.Response(503) if flaky["n"] == 1 else httpx.Response(200, html="<h1>ok</h1>")
        return httpx.Response(404)

    return Fetcher("test", delay=0, transport=httpx.MockTransport(handler))


def test_html_law_page(tmp_path: Path) -> None:
    page = fetcher(tmp_path).page("https://www.consultant.ru/document/cons_doc_LAW_36528/")
    assert edition(page.title) == "ред. от 01.09.2026"
    assert ("https://www.consultant.ru/document/cons_doc_LAW_36528/a1/", "Статья 12. Порядок") in page.links
    assert "меню" not in page.text and "x()" not in page.text


def test_pdf_page_and_numbers(tmp_path: Path) -> None:
    page = fetcher(tmp_path).page("https://vsrf.ru/doc.pdf")
    assert page.is_pdf and vs_number(page.text) == "18-КГ26-50-К4"
    assert first_date(page.text) == date(2026, 10, 1)


def test_search_unwraps_and_filters(tmp_path: Path) -> None:
    found = fetcher(tmp_path).search("q", re.compile(r"https?://(www\.)?vsrf\.ru/lk/practice/stor_pdf/\d+"))
    assert found == ["https://vsrf.ru/lk/practice/stor_pdf/123", "https://vsrf.ru/lk/practice/stor_pdf/456"]


def test_retry_and_errors(tmp_path: Path) -> None:
    f = fetcher(tmp_path)
    assert f.page("https://x.ru/flaky").title == "ok"
    with pytest.raises(FetchError):
        f.page("https://x.ru/missing")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("г. Москва 30 июня 2026 г.", date(2026, 6, 30)),
        ("от 08.11.2022 № 31", date(2022, 11, 8)),
        ("31.02.2026", None),
        ("без даты", None),
    ],
)
def test_first_date(text: str, expected: date | None) -> None:
    assert first_date(text) == expected


def test_ks_number() -> None:
    assert ks_number("Постановление от 26 мая 2026 года № 34-П") == "34-П"
    assert ks_number("Определение № 1234-О/2025") == "1234-О/2025"


class Echo(BaseModel):
    answer: str


def test_ollama_client() -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen3.5:9b"}, {"name": "bge-m3:latest"}]})
        body = json.loads(request.content)
        seen.append(body)
        if request.url.path == "/api/embed":
            return httpx.Response(200, json={"embeddings": [[0.1, 0.2] for _ in body["input"]]})
        return httpx.Response(200, json={"message": {"content": '{"answer": "да"}'}})

    llm = Ollama(Settings(), transport=httpx.MockTransport(handler))
    assert parse(llm, "s", "u", Echo).answer == "да"
    assert seen[0]["think"] is False and "format" in seen[0]
    assert llm.embed(["a", "b"]) == [[0.1, 0.2], [0.1, 0.2]]
    assert llm.embed([]) == []
    assert "bge-m3:latest" in llm.models()


def test_ollama_down_and_bad_json() -> None:
    down = Ollama(Settings(), transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    with pytest.raises(LLMError):
        down.ask("s", "u")
    with pytest.raises(LLMError):
        down.models()
    with pytest.raises(LLMError):
        down.embed(["x"])
    junk = Ollama(
        Settings(),
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"message": {"content": "не json"}})),
    )
    with pytest.raises(LLMError):
        parse(junk, "s", "u", Echo)
