from datetime import date
from pathlib import Path

from lexagent.library.authority import Kind, Position, stronger
from lexagent.library.store import DocMeta, Library, chunk
from tests.conftest import FakeLLM

LAW_OLD = "Статья 12. Порядок\n\nСтраховщик рассматривает заявление в течение 30 дней.\n\nСтатья 13. Иное\n\nТекст."
LAW_NEW = LAW_OLD.replace("30 дней", "20 календарных дней")


def test_new_edition_replaces_old_in_search(tmp_path: Path) -> None:
    lib = Library(tmp_path / "lib.sqlite")
    old = lib.add(DocMeta("law:40", "Закон об ОСАГО", Kind.LAW, edition="ред. от 01.01.2025"), LAW_OLD)
    new = lib.add(DocMeta("law:40", "Закон об ОСАГО", Kind.LAW, edition="ред. от 01.09.2026"), LAW_NEW)
    assert (old.status, new.status) == ("new", "updated")
    assert lib.add(DocMeta("law:40", "Закон об ОСАГО", Kind.LAW), LAW_NEW + "  ").status == "same"
    hits = lib.search("срок рассмотрения заявления страховщиком дней")
    assert hits and all("20 календарных" in h.text for h in hits if h.heading.startswith("Статья 12"))
    assert lib.document(old.doc_id).status == "superseded"
    assert "ред. от 01.09.2026" in hits[0].document.cite()


def test_overruled_chunk_excluded(tmp_path: Path) -> None:
    lib = Library(tmp_path / "lib.sqlite")
    llm = FakeLLM()
    a = lib.add(
        DocMeta("vs:1", "Определение ВС", Kind.VS, issued=date(2025, 12, 1)),
        "Убытки сверх лимита взыскиваются со страховщика.",
        llm.embed,
    )
    b = lib.add(
        DocMeta("ks:34", "Постановление КС", Kind.KS, issued=date(2026, 5, 26)),
        "Убытки сверх лимита со страховщика не взыскиваются.",
        llm.embed,
    )
    chunk_a, chunk_b = lib.chunks_of(a.doc_id)[0][0], lib.chunks_of(b.doc_id)[0][0]
    assert lib.add_conflict(chunk_b, chunk_a, chunk_a, "лимит", "разный вывод", "КС сильнее")
    hits = lib.search("убытки сверх лимита", embed=llm.embed)
    assert [h.document.kind for h in hits] == [Kind.KS]


def test_authority_order() -> None:
    plenum = Position(Kind.PLENUM, date(2022, 11, 8))
    fresh_vs = Position(Kind.VS, date(2026, 10, 1))
    ks = Position(Kind.KS, date(2026, 5, 26))
    assert stronger(plenum, fresh_vs) is plenum  # свежесть не бьёт уровень
    assert stronger(fresh_vs, ks) is ks
    older = Position(Kind.VS, date(2024, 1, 1))
    assert stronger(older, fresh_vs) is fresh_vs  # при равном уровне — свежее


def test_chunking_by_articles_and_points() -> None:
    parts = chunk(LAW_NEW, Kind.LAW)
    assert [h for h, _ in parts][:2] == ["Статья 12. Порядок", "Статья 13. Иное"]
    plenum = "1. Первый пункт текста.\n\n2. Второй пункт текста.\n\n37. Про ремонт."
    assert [h for h, _ in chunk(plenum, Kind.PLENUM)] == ["п. 1", "п. 2", "п. 37"]
