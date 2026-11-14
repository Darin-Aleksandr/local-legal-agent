from pathlib import Path

from lexagent.cases import CaseStore
from lexagent.config import Settings
from lexagent.inbox import locate, normalize_value, process_inbox
from lexagent.library.store import Library
from lexagent.textio import Page
from tests.conftest import FakeLLM, make_pdf

APPLICATION = (
    "В САО «РЕСО-Гарантия»\nот Сургаева Ивана Петровича\n\nЗАЯВЛЕНИЕ о страховом возмещении\n"
    "Полис ОСАГО ХХХ 0123456789. ДТП произошло 12.09.2026 по адресу г. Москва, ул. Ленина, 1.\n"
    "Дата: 15.09.2026"
)
ANSWER_LETTER = "САО «РЕСО-Гарантия». Убыток № АТ-778899. По полису ХХХ 0123456789 выплачено 128 600 руб. 01.10.2026."


def reading(system: str, user: str) -> dict[str, object]:
    if "Убыток" in user:
        return {
            "doc_type": "ответ страховой",
            "summary": "ответ",
            "identifiers": ["ХХХ 0123456789", "АТ-778899"],
            "facts": [{"key": "insurer_claim_number", "value": "АТ-778899", "quote": "Убыток № АТ-778899"}],
            "payments": [{"paid_on": "01.10.2026", "amount": 128600, "quote": "выплачено 128 600 руб. 01.10.2026"}],
            "parties": [{"role": "ответчик", "name": "САО «РЕСО-Гарантия»"}],
        }
    if "Обзор" in user:
        return {"doc_type": "судебная практика по чужому делу", "summary": "решение по чужому делу"}
    return {
        "doc_type": "заявление в страховую",
        "summary": "заявление",
        "identifiers": ["ХХХ 0123456789"],
        "facts": [
            {"key": "claimant_name", "value": "Сургаев Иван Петрович", "quote": "от Сургаева Ивана Петровича"},
            {"key": "accident_date", "value": "12.09.2026", "quote": "ДТП произошло 12.09.2026"},
            {"key": "policy_number", "value": "ХХХ 0123456789", "quote": "Полис ОСАГО ХХХ 0123456789"},
            {"key": "insurer_name", "value": "САО «РЕСО-Гарантия»", "quote": "В САО «РЕСО-Гарантия»"},
            # выдумка: такого текста в документе нет
            {"key": "repair_cost_market", "value": "500000", "quote": "стоимость ремонта 500 000 руб."},
            {"key": "unknown_field", "value": "x", "quote": "Дата"},
        ],
    }


def test_inbox_routes_files_into_one_case(settings: Settings) -> None:
    make_pdf(settings.inbox / "заявление.pdf", [APPLICATION])
    (settings.inbox / "ответ.txt").write_text(ANSWER_LETTER, encoding="utf-8")
    (settings.inbox / "практика.txt").write_text("Обзор: решение районного суда по чужому делу.", encoding="utf-8")
    (settings.inbox / "ссылки.txt").write_text("https://vsrf.ru/x\n", encoding="utf-8")
    store, lib, llm = CaseStore(settings.cases), Library(settings.library / "l.sqlite"), FakeLLM({"Reading": reading})

    results = {r.file: r for r in process_inbox(settings.inbox, store, llm, library=lib, embed=llm.embed)}

    assert set(results) == {"заявление.pdf", "ответ.txt", "практика.txt"}  # ссылки.txt — для библиотеки
    assert results["заявление.pdf"].case == results["ответ.txt"].case
    assert any("500000" in d for d in results["заявление.pdf"].dropped)
    assert results["практика.txt"].library and lib.documents()
    case = store.all()[0]
    dossier = case.load()
    assert dossier.value("claimant_name") == "Сургаев Иван Петрович"
    assert dossier.facts["claimant_name"].source.page == 1  # type: ignore[union-attr]
    assert not dossier.facts["claimant_name"].confirmed
    assert "repair_cost_market" not in dossier.facts
    assert dossier.payments[0].amount == 128600
    assert {p.name for p in dossier.parties} == {"САО «РЕСО-Гарантия»"}
    assert sorted(p.name for p in case.originals.iterdir()) == ["hashes.json", "заявление.pdf", "ответ.txt"]
    assert (settings.inbox / "ссылки.txt").exists()
    assert not (settings.inbox / "заявление.pdf").exists()
    assert list((settings.inbox / "_разобрано").rglob("заявление.pdf"))


def test_locate_ignores_spacing_and_quotes() -> None:
    pages = [Page(1, "первая", "text"), Page(2, "В  САО «РЕСО-\nГарантия» выплачено", "text")]
    assert locate('САО "РЕСО-Гарантия" выплачено', pages) == 2
    assert locate("Ингосстрах", pages) is None
    assert locate("В", pages) is None


def test_normalize_values() -> None:
    assert normalize_value("repair_cost_market", "128 600,50 руб.") == "128600.50"
    assert normalize_value("accident_date", "2026-09-12") == "12.09.2026"
    assert normalize_value("claimant_name", " Сургаев   Иван ") == "Сургаев Иван"


def test_unreadable_file_reported(settings: Settings, tmp_path: Path) -> None:
    (settings.inbox / "архив.zip").write_bytes(b"PK")
    results = process_inbox(settings.inbox, CaseStore(settings.cases), FakeLLM())
    assert results[0].error
