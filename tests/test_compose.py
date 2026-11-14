from datetime import date
from pathlib import Path

import pymupdf as fitz

from lexagent.cases import CaseStore, Dossier, Fact, Payment
from lexagent.documents.compose import compose
from lexagent.documents.render import to_docx, to_pdf
from lexagent.library.authority import Kind
from lexagent.library.store import DocMeta, Library
from tests.conftest import FakeLLM

FULL = {
    "claimant_name": "Сургаев Иван Петрович",
    "claimant_address": "г. Москва, ул. Ленина, 1",
    "claimant_phone": "+7 900 000-00-00",
    "insurer_name": "САО «РЕСО-Гарантия»",
    "insurer_inn": "7710045520",
    "insurer_address": "г. Москва, Нагорный пр., 6",
    "policy_number": "ХХХ 0123456789",
    "insurer_claim_number": "АТ-778899",
    "accident_date": "12.09.2025",
    "accident_place": "г. Москва, ул. Ленина, 1",
    "vehicle": "Kia Rio, А123АА777",
    "culprit_name": "Петров П. П.",
    "application_date": "15.09.2025",
    "repair_cost_market": "250000",
    "expertise_cost": "8000",
    "fu_decision_number": "У-25-123456/5010-007",
    "fu_decision_date": "10.12.2025",
    "court_name": "Нагатинский районный суд г. Москвы",
}


def dossier(**override: str) -> Dossier:
    facts = {k: Fact(value=v, confirmed=True) for k, v in {**FULL, **override}.items()}
    return Dossier(
        case_id="x",
        title="Сургаев",
        facts=facts,
        payments=[Payment(paid_on=date(2025, 10, 10), amount=128600, confirmed=True)],
    )


def library(tmp_path: Path, llm: FakeLLM) -> Library:
    lib = Library(tmp_path / "lib.sqlite")
    lib.add(
        DocMeta(
            "plenum:31",
            "Постановление Пленума ВС РФ от 08.11.2022 № 31",
            Kind.PLENUM,
            number="31",
            issued=date(2022, 11, 8),
            tags=("ОСАГО",),
        ),
        "37. Страховщик обязан организовать восстановительный ремонт; смена формы возмещения без согласия "
        "потерпевшего не допускается.\n\n76. Неустойка 1 процент за каждый день просрочки взыскивается по "
        "день фактического исполнения обязательства.",
        llm.embed,
    )
    return lib


def test_lawsuit_numbers_consistent(tmp_path: Path) -> None:
    llm = FakeLLM(
        {
            "LLMArguments": {
                "arguments": [
                    {"text": "Страховщик был обязан организовать ремонт.", "sources": [1]},
                    {"text": "Довод без опоры.", "sources": []},
                    {"text": "См. определение № 5-КГ99-1-К2, которого нет.", "sources": [1, 99]},
                ]
            }
        }
    )
    draft = compose("иск", dossier(), library=library(tmp_path, llm), llm=llm, embed=llm.embed, on=date(2026, 1, 20))
    text = draft.text()
    # 250 000 − 128 600 = 121 400; неустойка: с 06.10.2025 по 10.10.2025 на 250 000 и далее на 121 400
    assert "121 400,00 руб." in text
    price_line = next(line for line in draft.header if line.startswith("Цена иска"))
    assert price_line.split(": ")[1] in "\n".join(draft.body)
    assert "Госпошлина: не уплачивается" in text
    assert len(draft.arguments) == 2
    assert "[ПРОВЕРИТЬ]" in draft.arguments[1].text and "5-КГ99-1-К2" not in text
    assert all("Пленума" in s for a in draft.arguments for s in a.sources)
    assert any("без источника" in c for c in draft.checks)
    assert any("моральный вред" in c for c in draft.checks)


def test_missing_and_unconfirmed_are_flagged(tmp_path: Path) -> None:
    d = dossier()
    del d.facts["insurer_inn"]
    d.facts["claimant_name"].confirmed = False
    draft = compose("претензия", d, on=date(2026, 1, 20))
    assert "[ПРОВЕРИТЬ] инн страховой" in draft.text()
    assert any(c.startswith("нет в досье: ИНН") for c in draft.checks)
    assert any(c.startswith("не подтверждено: ФИО") for c in draft.checks)
    assert any("нет доводов" in c for c in draft.checks)


def test_above_limit_goes_to_culprit(tmp_path: Path) -> None:
    draft = compose("иск", dossier(repair_cost_market="650000"), on=date(2026, 1, 20))
    assert "271 400,00 руб." in draft.text()  # 400 000 − 128 600
    assert any("к виновнику" in c for c in draft.checks)


def test_without_llm_quotes_library(tmp_path: Path) -> None:
    llm = FakeLLM()
    draft = compose("обращение", dossier(), library=library(tmp_path, llm), on=date(2026, 1, 20))
    assert draft.arguments and draft.arguments[0].text.startswith("«")


def test_render_docx_and_pdf(tmp_path: Path) -> None:
    case = CaseStore(tmp_path).create("x")
    draft = compose("иск", dossier(), on=date(2026, 1, 20))
    docx = to_docx(draft, case.work / "иск.docx")
    pdf = to_pdf(docx, tmp_path / "pdf")
    with fitz.open(pdf) as doc:
        text = "".join(page.get_text() for page in doc)
    assert "ИСКОВОЕ ЗАЯВЛЕНИЕ" in text and "Проверить перед подачей" in text
    clean = to_pdf(to_docx(draft, tmp_path / "clean" / "иск.docx", with_checks=False), tmp_path / "clean")
    with fitz.open(clean) as doc:
        assert "Проверить перед подачей" not in "".join(p.get_text() for p in doc)


def test_capped_penalty_has_no_open_ended_demand() -> None:
    draft = compose("иск", dossier(), on=date(2026, 11, 15))
    penalty = next(d for d in draft.demands if d.startswith("Взыскать неустойку"))
    assert "400 000,00 руб." in penalty and "фактического исполнения" not in penalty
