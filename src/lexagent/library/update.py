"""Обновление библиотеки: свежие редакции законов, новая практика, проверка противоречий, отчёт.

Запускается сам при старте агента (не чаще раза в сутки) или кнопкой «Обновить библиотеку».
От юриста ничего не требуется; всё, что произошло, записано в отчёт «Библиотека/отчёты/…md».
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from pydantic import BaseModel

from lexagent.library.authority import Kind, Position, explain, stronger
from lexagent.library.fetch import Fetcher, FetchError, Page, edition, first_date, ks_number, vs_number
from lexagent.library.store import DocMeta, Embed, Library
from lexagent.llm import LLM, LLMError, parse

ARTICLE = re.compile(r"^Статья\s+(\d+(?:\.\d+)*)")
MAX_CONFLICT_CHUNKS = 12  # сколько фрагментов нового документа сверять — проверка не бесконечная


@dataclass
class Report:
    started: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def markdown(self) -> str:
        def section(title: str, items: list[str]) -> str:
            return f"## {title} ({len(items)})\n" + ("\n".join(f"- {i}" for i in items) or "- нет") + "\n"

        return "\n".join(
            [
                f"# Обновление библиотеки · {self.started}\n",
                section("Новые документы", self.added),
                section("Новые редакции (старые ушли в архив)", self.updated),
                section("Противоречия: какая позиция применяется", self.conflicts),
                section("Источники, которые не ответили — проверить адрес в sources.toml", self.errors),
                section("Без изменений", self.unchanged),
            ]
        )


class Verdict(BaseModel):
    contradicts: bool
    topic: str = ""
    summary: str = ""


CONFLICT_PROMPT = (
    "Ты юрист-аналитик. Сравни две правовые позиции по одному вопросу. Противоречие — это когда "
    "применение одной ведёт к другому результату, чем применение другой (например, разный лимит, разные "
    "основания, разный расчёт). Разные вопросы или дополнение друг друга — НЕ противоречие. "
    "Ответь JSON: contradicts (true/false), topic (вопрос, 3–8 слов), summary (в чём расхождение, 1–2 "
    "предложения). Не придумывай того, чего нет в текстах."
)


def load_sources(path: Path) -> dict[str, list[dict[str, object]]]:
    return tomllib.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def update_library(
    library: Library,
    fetcher: Fetcher,
    sources: dict[str, list[dict[str, object]]],
    *,
    embed: Embed | None = None,
    llm: LLM | None = None,
    links_file: Path | None = None,
    reports_dir: Path | None = None,
) -> Report:
    report = Report()
    fresh: list[int] = []
    for law in sources.get("law", []):
        _guard(report, str(law.get("title")), lambda law=law: _law(library, fetcher, law, embed, report))
    for doc in sources.get("document", []):
        _guard(
            report, str(doc.get("title")), lambda doc=doc: fresh.extend(_document(library, fetcher, doc, embed, report))
        )
    for search in sources.get("search", []):
        _guard(
            report, str(search.get("name")), lambda s=search: fresh.extend(_search(library, fetcher, s, embed, report))
        )
    if links_file and links_file.is_file():
        fresh.extend(_links(library, fetcher, links_file, embed, report))
    if embed and llm:
        for doc_id in fresh:
            _guard(
                report, f"проверка противоречий #{doc_id}", lambda d=doc_id: _conflicts(library, d, embed, llm, report)
            )
    if reports_dir:
        reports_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
        (reports_dir / f"{stamp}.md").write_text(report.markdown(), encoding="utf-8")
    return report


def _guard(report: Report, what: str, action: object) -> None:
    try:
        action()  # type: ignore[operator]
    except (FetchError, LLMError) as exc:
        report.errors.append(f"{what}: {exc}")


# ---------------------------------------------------------------- laws
def _law(library: Library, fetcher: Fetcher, law: dict[str, object], embed: Embed | None, report: Report) -> None:
    key, title, url = str(law["key"]), str(law["title"]), str(law["url"])
    page = fetcher.page(url)
    expect = str(law.get("expect", ""))
    if expect and expect not in page.title and expect not in page.text[:3000]:
        raise FetchError(f"по адресу {url} не тот документ (ожидали «{expect}») — адрес устарел")
    current = edition(page.title)
    active = library.active_by_key(key)
    if active and current and active.edition == current:
        report.unchanged.append(f"{title} ({current})")
        return
    wanted = {str(a) for a in law.get("articles", [])}  # type: ignore[union-attr]
    base = url.split("#")[0].rstrip("/")
    texts: list[str] = []
    seen: set[str] = set()
    for href, anchor in page.links:
        match = ARTICLE.match(anchor)
        if not match or not href.startswith(base) or href in seen:
            continue
        if wanted and match.group(1) not in wanted:
            continue
        seen.add(href)
        article = fetcher.page(href)
        body = _article_body(article, anchor)
        texts.append(f"{anchor}\n\n{body}")
    text = "\n\n".join(texts) if texts else page.text
    if wanted and not texts:
        raise FetchError(f"{title}: не нашёл ссылок на статьи {', '.join(sorted(wanted))} — вёрстка изменилась")
    meta = DocMeta(key, title, Kind.LAW, edition=current, url=url, tags=_tags(law))
    result = library.add(meta, text, embed)
    _note(report, result.status, f"{title} ({current or 'редакция не указана'})")


def _article_body(page: Page, anchor: str) -> str:
    """Текст статьи без меню сайта: от заголовка статьи до конца."""
    start = page.text.find(anchor[:30])
    return page.text[start + len(anchor) :].strip() if start >= 0 else page.text


# ---------------------------------------------------------------- practice
def _document(
    library: Library, fetcher: Fetcher, doc: dict[str, object], embed: Embed | None, report: Report
) -> list[int]:
    page = fetcher.page(str(doc["url"]))
    kind = Kind(str(doc.get("kind", "other")))
    issued = date.fromisoformat(str(doc["issued"])) if doc.get("issued") else first_date(page.text)
    meta = DocMeta(
        str(doc["key"]),
        str(doc["title"]),
        kind,
        number=str(doc.get("number", "")),
        issued=issued,
        url=str(doc["url"]),
        tags=_tags(doc),
    )
    result = library.add(meta, page.text, embed)
    _note(report, result.status, meta.title)
    return [result.doc_id] if result.status != "same" else []


def _search(
    library: Library, fetcher: Fetcher, search: dict[str, object], embed: Embed | None, report: Report
) -> list[int]:
    allow = re.compile(str(search["allow"]))
    require = [str(w).lower() for w in search.get("require", [])]  # type: ignore[union-attr]
    kind = Kind(str(search.get("kind", "other")))
    limit = int(search.get("max_new", 10))  # type: ignore[arg-type]
    known = library.known_urls()
    year = date.today().year
    candidates: list[str] = []
    for query in search.get("queries", []):  # type: ignore[union-attr]
        for y in (year, year - 1):
            candidates += [u for u in fetcher.search(str(query).format(year=y), allow) if u not in known]
    added: list[int] = []
    for url in dict.fromkeys(candidates):
        if len(added) >= limit:
            break
        try:
            page = fetcher.page(url)
        except FetchError as exc:
            report.errors.append(f"{search['name']}: {exc}")
            continue
        low = page.text.lower()
        if require and not all(word in low for word in require):
            continue  # не по теме: в библиотеку не берём
        doc_id = _add_practice(library, page, kind, _tags(search), embed, report)
        if doc_id:
            added.append(doc_id)
    return added


def _links(library: Library, fetcher: Fetcher, links_file: Path, embed: Embed | None, report: Report) -> list[int]:
    """Ссылки, которые юрист бросил в «Входящие/ссылки.txt»: скачать, разобрать, файл очистить."""
    added: list[int] = []
    left: list[str] = []
    for line in links_file.read_text(encoding="utf-8").splitlines():
        url = line.strip()
        if not url.startswith("http"):
            continue
        try:
            page = fetcher.page(url)
        except FetchError as exc:
            report.errors.append(f"ссылка из ссылки.txt: {exc}")
            left.append(url)
            continue
        kind = _guess_kind(url, page.text)
        doc_id = _add_practice(library, page, kind, ("ОСАГО",), embed, report)
        if doc_id:
            added.append(doc_id)
    links_file.write_text("\n".join(left) + ("\n" if left else ""), encoding="utf-8")
    return added


def _add_practice(
    library: Library, page: Page, kind: Kind, tags: tuple[str, ...], embed: Embed | None, report: Report
) -> int | None:
    number = ks_number(page.text) if kind is Kind.KS else vs_number(page.text)
    issued = first_date(page.text)
    title = _practice_title(kind, number, issued, page.title)
    key = f"{kind.value}:{number}" if number else f"url:{page.url}"
    result = library.add(
        DocMeta(key, title, kind, number=number, issued=issued, url=page.url, tags=tags), page.text, embed
    )
    _note(report, result.status, f"{title} — {page.url}")
    return result.doc_id if result.status != "same" else None


def _practice_title(kind: Kind, number: str, issued: date | None, fallback: str) -> str:
    when = f" от {issued:%d.%m.%Y}" if issued else ""
    if kind is Kind.VS and number:
        return f"Определение Судебной коллегии Верховного Суда РФ{when} № {number}"
    if kind is Kind.KS and number:
        prefix = "Постановление" if number.split("-")[1].startswith("П") else "Определение"
        return f"{prefix} Конституционного Суда РФ{when} № {number}"
    return fallback[:200]


def _guess_kind(url: str, text: str) -> Kind:
    head = text[:2000].upper()
    if "ksrf.ru" in url or "КОНСТИТУЦИОННОГО СУДА" in head or "КОНСТИТУЦИОННЫЙ СУД" in head:
        return Kind.KS
    if "ПЛЕНУМА ВЕРХОВНОГО СУДА" in head:
        return Kind.PLENUM
    if "ОБЗОР" in head and "ВЕРХОВН" in head:
        return Kind.REVIEW
    if "vsrf.ru" in url or "ВЕРХОВНЫЙ СУД" in head:
        return Kind.VS
    if "КАССАЦИОНН" in head:
        return Kind.CASSATION
    if "АПЕЛЛЯЦИОНН" in head:
        return Kind.APPEAL
    return Kind.OTHER


# ---------------------------------------------------------------- conflicts
def _conflicts(library: Library, doc_id: int, embed: Embed, llm: LLM, report: Report) -> None:
    """Сверка нового документа с уже известной практикой: расхождения — по правилу старшинства, в отчёт."""
    new_doc = library.document(doc_id)
    if new_doc.kind is Kind.LAW:
        return
    for chunk_id, heading, text in library.chunks_of(doc_id)[:MAX_CONFLICT_CHUNKS]:
        for other_id, _score in library.similar(chunk_id, embed, exclude_doc=doc_id, limit=2):
            old_doc = library.chunk_doc(other_id)
            if old_doc.kind is Kind.LAW or library.has_conflict(chunk_id, other_id):
                continue
            other_text = next(t for c, _h, t in library.chunks_of(old_doc.id) if c == other_id)
            verdict = parse(
                llm,
                CONFLICT_PROMPT,
                f"ПОЗИЦИЯ А ({new_doc.cite()}, {heading}):\n{text}\n\nПОЗИЦИЯ Б ({old_doc.cite()}):\n{other_text}",
                Verdict,
            )
            if not verdict.contradicts:
                continue
            new_pos, old_pos = Position(new_doc.kind, new_doc.issued), Position(old_doc.kind, old_doc.issued)
            winner = stronger(new_pos, old_pos)
            loser_chunk, reason = (
                (other_id, explain(new_pos, old_pos)) if winner is new_pos else (chunk_id, explain(old_pos, new_pos))
            )
            if library.add_conflict(chunk_id, other_id, loser_chunk, verdict.topic, verdict.summary, reason):
                applied = new_doc if winner is new_pos else old_doc
                report.conflicts.append(
                    f"«{verdict.topic}»: {verdict.summary} Применяется: {applied.cite()} ({reason})."
                )


def _note(report: Report, status: str, title: str) -> None:
    {"new": report.added, "updated": report.updated, "same": report.unchanged}[status].append(title)


def _tags(entry: dict[str, object]) -> tuple[str, ...]:
    return tuple(str(t) for t in entry.get("tags", []))  # type: ignore[union-attr]
