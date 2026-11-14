"""Скачивание норм и практики из открытых источников.

Вежливо: один запрос в секунду на сайт, понятный User-Agent, только перечисленные в sources.toml
источники. Любая ошибка источника не роняет обновление — она попадает в отчёт («источник не ответил»).
Вёрстка сайтов меняется, поэтому разбор максимально простой: текст страницы и ссылки по шаблону.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import date
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse

import httpx
import pymupdf as fitz
from bs4 import BeautifulSoup

MONTHS = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}
DATE_WORDS = re.compile(r"(\d{1,2})\s+(" + "|".join(MONTHS) + r")\s+(\d{4})")
DATE_DIGITS = re.compile(r"\b(\d{2})\.(\d{2})\.(\d{4})\b")
VS_NUMBER = re.compile(r"№\s*(\d{1,3}-[А-ЯЁA-Z]{1,4}\d{2}-\d+(?:-[А-ЯЁA-Z]{1,3}\d{0,2})?)")
KS_NUMBER = re.compile(r"№\s*(\d{1,4}-[ПО](?:-?Р)?(?:/\d{4})?)")
EDITION = re.compile(r"\((ред\. от [\d.]+(?:, с изм\. от [\d.]+)?)")


class FetchError(RuntimeError):
    pass


@dataclass(frozen=True)
class Page:
    url: str
    title: str
    text: str
    links: list[tuple[str, str]]  # (абсолютный адрес, текст ссылки)
    is_pdf: bool


class Fetcher:
    def __init__(
        self, user_agent: str, *, delay: float = 1.0, timeout: float = 60, transport: httpx.BaseTransport | None = None
    ) -> None:
        self._backoff = 0 if transport else 1  # в тестах не ждём
        self._http = httpx.Client(
            transport=transport,
            headers={"User-Agent": user_agent, "Accept-Language": "ru-RU,ru;q=0.9"},
            follow_redirects=True,
            timeout=timeout,
        )
        self._delay = delay
        self._last: dict[str, float] = {}

    def get(self, url: str) -> httpx.Response:
        host = urlparse(url).netloc
        wait = self._delay - (time.monotonic() - self._last.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = self._http.get(url)
                self._last[host] = time.monotonic()
                if response.status_code in {429, 502, 503, 504}:
                    time.sleep(5 * (attempt + 1) * self._backoff)
                    continue
                response.raise_for_status()
                return response
            except httpx.HTTPError as exc:
                last_error = exc
                time.sleep(2 * (attempt + 1) * self._backoff)
        raise FetchError(f"{url}: {last_error or 'сайт не отвечает'}")

    def page(self, url: str) -> Page:
        response = self.get(url)
        content_type = response.headers.get("content-type", "")
        if "pdf" in content_type or response.content[:5] == b"%PDF-":
            return pdf_page(str(response.url), response.content)
        return html_page(str(response.url), response.text)

    def search(self, query: str, allow: re.Pattern[str], limit: int = 20) -> list[str]:
        """Поиск через DuckDuckGo (без ключа): только адреса официальных сайтов по шаблону allow."""
        page = self.page(f"https://html.duckduckgo.com/html/?q={quote_plus(query)}")
        found: list[str] = []
        for href, _ in page.links:
            target = _unwrap(href)
            if allow.search(target) and target not in found:
                found.append(target)
        return found[:limit]


def html_page(url: str, html: str) -> Page:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav", "form"]):
        tag.decompose()
    title_tag = soup.find("h1") or soup.find("title")
    title = " ".join(title_tag.get_text(" ").split()) if title_tag else url
    doc_title = soup.find("title")
    if doc_title and EDITION.search(doc_title.get_text()):  # редакция закона часто только в <title>
        title = " ".join(doc_title.get_text(" ").split())
    links = [(urljoin(url, a["href"]), " ".join(a.get_text(" ").split())) for a in soup.find_all("a", href=True)]
    text = re.sub(r"\n{3,}", "\n\n", soup.get_text("\n"))
    text = "\n".join(line.strip() for line in text.splitlines())
    return Page(url, title, text.strip(), links, is_pdf=False)


def pdf_page(url: str, content: bytes) -> Page:
    with fitz.open(stream=content, filetype="pdf") as doc:
        text = "\n".join(page.get_text("text") for page in doc)
    first = next((line.strip() for line in text.splitlines() if len(line.strip()) > 10), url)
    return Page(url, first[:200], text.strip(), [], is_pdf=True)


def first_date(text: str) -> date | None:
    """Дата документа: первая дата в шапке («г. Москва 30 июня 2026 г.» или «08.11.2022»)."""
    head = text[:3000]
    if m := DATE_WORDS.search(head):
        return _safe_date(int(m.group(3)), MONTHS[m.group(2)], int(m.group(1)))
    if m := DATE_DIGITS.search(head):
        return _safe_date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    return None


def vs_number(text: str) -> str:
    m = VS_NUMBER.search(text[:3000].replace("Дело№", "№").replace("Дело №", "№"))
    return m.group(1) if m else ""


def ks_number(text: str) -> str:
    m = KS_NUMBER.search(text[:3000])
    return m.group(1) if m else ""


def edition(title: str) -> str:
    m = EDITION.search(title)
    return m.group(1) if m else ""


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _unwrap(href: str) -> str:
    """DuckDuckGo заворачивает ссылки в /l/?uddg=<настоящий адрес>."""
    parsed = urlparse(href)
    if parsed.path.startswith("/l/") and "uddg" in parse_qs(parsed.query):
        return unquote(parse_qs(parsed.query)["uddg"][0])
    return href
