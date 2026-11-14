import re
from pathlib import Path

from lexagent.library.authority import Kind
from lexagent.library.fetch import FetchError, Page
from lexagent.library.store import Library
from lexagent.library.update import update_library
from tests.conftest import FakeLLM

BASE = "https://www.consultant.ru/document/cons_doc_LAW_36528"


class FakeFetcher:
    def __init__(self, pages: dict[str, Page], results: dict[str, list[str]] | None = None) -> None:
        self.pages = pages
        self.results = results or {}
        self.fetched: list[str] = []

    def page(self, url: str) -> Page:
        self.fetched.append(url)
        if url not in self.pages:
            raise FetchError(f"{url}: 404")
        return self.pages[url]

    def search(self, query: str, allow: re.Pattern[str], limit: int = 20) -> list[str]:
        return [u for u in self.results.get(query, []) if allow.match(u)]


def law_pages(edition: str, art12: str) -> dict[str, Page]:
    toc = Page(
        f"{BASE}/",
        f"Закон 40-ФЗ (ред. от {edition})",
        "Оглавление 40-ФЗ",
        [
            (f"{BASE}/a1/", "Статья 12. Порядок"),
            (f"{BASE}/a2/", "Статья 13. Иное"),
            ("https://www.consultant.ru/law/", "Статья про сайт"),
        ],
        False,
    )
    return {
        f"{BASE}/": toc,
        f"{BASE}/a1/": Page(f"{BASE}/a1/", "", f"Меню сайта Статья 12. Порядок\n{art12}", [], False),
        f"{BASE}/a2/": Page(f"{BASE}/a2/", "", "Статья 13. Иное\nТекст статьи 13.", [], False),
    }


LAW = {
    "law": [{"key": "law:40-ФЗ", "title": "Закон об ОСАГО", "url": f"{BASE}/", "expect": "40-ФЗ", "tags": ["ОСАГО"]}]
}


def test_law_edition_tracking(tmp_path: Path) -> None:
    lib = Library(tmp_path / "lib.sqlite")
    report = update_library(lib, FakeFetcher(law_pages("01.01.2025", "Срок 30 дней.")), LAW)  # type: ignore[arg-type]
    assert report.added and not report.errors
    doc = lib.active_by_key("law:40-ФЗ")
    assert doc and doc.edition == "ред. от 01.01.2025"
    assert [h for _c, h, _t in lib.chunks_of(doc.id)] == ["Статья 12. Порядок", "Статья 13. Иное"]

    fetcher = FakeFetcher(law_pages("01.01.2025", "Срок 30 дней."))
    report = update_library(lib, fetcher, LAW)  # type: ignore[arg-type]
    assert report.unchanged and fetcher.fetched == [f"{BASE}/"]  # статьи не перекачиваем

    report = update_library(
        lib,
        FakeFetcher(law_pages("01.09.2026", "Срок 20 дней.")),
        LAW,  # type: ignore[arg-type]
        reports_dir=tmp_path / "отчёты",
    )
    assert report.updated
    assert "20 дней" in lib.search("срок дней")[0].text
    assert list((tmp_path / "отчёты").glob("*.md"))


def test_wrong_document_at_url_reported(tmp_path: Path) -> None:
    pages = law_pages("01.01.2025", "x")
    pages[f"{BASE}/"] = Page(f"{BASE}/", "Какой-то другой закон", "другое", [], False)
    report = update_library(Library(tmp_path / "l.sqlite"), FakeFetcher(pages), LAW)  # type: ignore[arg-type]
    assert "адрес устарел" in report.errors[0]


def test_search_filters_topic_and_checks_conflicts(tmp_path: Path) -> None:
    lib = Library(tmp_path / "lib.sqlite")
    old_url = "https://vsrf.ru/lk/practice/stor_pdf/111"
    new_url = "https://vsrf.ru/lk/practice/stor_pdf/222"
    off_topic = "https://vsrf.ru/lk/practice/stor_pdf/333"
    text_old = (
        "ОПРЕДЕЛЕНИЕ от 10 декабря 2025 г. № 5-КГ25-100-К2\nСтраховщик не организовал ремонт "
        "транспортного средства, убытки сверх лимита взыскиваются со страховщика."
    )
    text_new = (
        "ОПРЕДЕЛЕНИЕ от 1 октября 2026 г. № 18-КГ26-50-К4\nСтраховщик не организовал ремонт "
        "транспортного средства, убытки сверх лимита со страховщика не взыскиваются."
    )
    pages = {
        old_url: Page(old_url, "", text_old, [], True),
        new_url: Page(new_url, "", text_new, [], True),
        off_topic: Page(off_topic, "", "Определение о налоге на имущество", [], True),
    }
    query = "site:vsrf.ru ОСАГО {year}"
    from datetime import date

    results = {
        query.format(year=date.today().year): [old_url, off_topic, "https://evil.example/x"],
        query.format(year=date.today().year - 1): [new_url],
    }
    sources = {
        "search": [
            {
                "name": "ВС",
                "kind": "vs",
                "queries": [query],
                "allow": r"https?://(www\.)?vsrf\.ru/lk/practice/stor_pdf/\d+",
                "require": ["страхов", "транспортн"],
                "tags": ["ОСАГО"],
                "max_new": 5,
            }
        ]
    }
    llm = FakeLLM({"Verdict": {"contradicts": True, "topic": "убытки сверх лимита", "summary": "разный вывод"}})
    report = update_library(lib, FakeFetcher(pages, results), sources, embed=llm.embed, llm=llm)  # type: ignore[arg-type]
    titles = " ".join(report.added)
    assert "5-КГ25-100-К2" in titles and "18-КГ26-50-К4" in titles and "налог" not in titles
    assert len(report.conflicts) == 1 and "18-КГ26-50-К4" in report.conflicts[0].split("Применяется:")[1]
    hits = lib.search("убытки сверх лимита страховщик", embed=llm.embed)
    assert [h.document.number for h in hits] == ["18-КГ26-50-К4"]
    assert all(d.kind is Kind.VS for d in lib.documents())


def test_links_file_consumed(tmp_path: Path) -> None:
    links = tmp_path / "ссылки.txt"
    url = "https://ksrf.ru/doc/34"
    links.write_text(f"{url}\nhttps://dead.example/\nне ссылка\n", encoding="utf-8")
    pages = {url: Page(url, "", "ПОСТАНОВЛЕНИЕ КОНСТИТУЦИОННОГО СУДА РФ от 26 мая 2026 г. № 34-П\nтекст", [], False)}
    lib = Library(tmp_path / "lib.sqlite")
    report = update_library(lib, FakeFetcher(pages), {}, links_file=links)  # type: ignore[arg-type]
    assert "№ 34-П" in report.added[0]
    assert links.read_text(encoding="utf-8").strip() == "https://dead.example/"  # неудачная ссылка остаётся
    assert lib.documents()[0].kind is Kind.KS
