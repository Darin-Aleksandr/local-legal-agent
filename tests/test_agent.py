from datetime import date
from pathlib import Path

import pytest

from lexagent.agent import Agent, _keywords
from lexagent.config import Settings
from lexagent.library.authority import Kind
from lexagent.library.store import DocMeta
from tests.conftest import FakeLLM
from tests.test_compose import FULL
from tests.test_update import FakeFetcher


def agent_with_case(settings: Settings, llm: FakeLLM | None) -> Agent:
    agent = Agent(settings, llm, fetcher=FakeFetcher({}), sources=Path("нет.toml"))  # type: ignore[arg-type]
    case = agent.store.create("Сургаев РЕСО", keywords=["ХХХ 0123456789"])
    for key, value in FULL.items():
        case.set_fact(key, value, source=None, confirmed=True, reason="тест")
    agent.library.add(
        DocMeta(
            "plenum:31",
            "Постановление Пленума ВС РФ № 31",
            Kind.PLENUM,
            number="31",
            issued=date(2022, 11, 8),
            tags=("ОСАГО",),
        ),
        "37. Страховщик обязан организовать восстановительный ремонт транспортного средства.",
        llm.embed if llm else None,
    )
    return agent


@pytest.mark.parametrize(
    ("task", "action", "extra"),
    [
        ("разбери входящие", "inbox", {}),
        ("сделай иск по Сургаеву", "document", {"document": "иск", "case": "Сургаеву"}),
        ("подготовь претензию по делу Сургаев", "document", {"document": "претензия", "case": "Сургаев"}),
        ("посчитай неустойку по Сургаев на 01.12.2026", "calc", {"on": "01.12.2026"}),
        ("обнови библиотеку", "update", {}),
        ("проверь файлы для ГАС", "check", {}),
        ("что ВС говорит про смену формы возмещения?", "ask", {}),
    ],
)
def test_keyword_routing(task: str, action: str, extra: dict[str, str]) -> None:
    intent = _keywords(task)
    assert intent.action == action
    for key, value in extra.items():
        assert getattr(intent, key) == value


def test_document_end_to_end(settings: Settings) -> None:
    llm = FakeLLM(
        {
            "Intent": {"action": "document", "case": "Сургаев", "document": "иск", "on": "20.01.2026"},
            "LLMArguments": {"arguments": [{"text": "Страховщик обязан был организовать ремонт.", "sources": [1]}]},
        }
    )
    agent = agent_with_case(settings, llm)
    reply = agent.handle("сделай иск по Сургаеву")
    pdf = next(f for f in reply.files if f.suffix == ".pdf")
    assert pdf.name.startswith("Исковое_заявление_от_20012026_") and pdf.name.endswith("л.pdf")
    assert "✅" in reply.text, reply.text
    case = agent.store.all()[0]
    frozen = agent.freeze(case, pdf)
    assert frozen.files[0].parent == case.exports and "SHA256" in frozen.text
    assert "Уже заморожен" in agent.freeze(case, pdf).text
    assert '"event": "заморожен"' in (case.root / "logs" / "audit.log").read_text(encoding="utf-8")


def test_calc_and_dossier_without_llm(settings: Settings) -> None:
    agent = agent_with_case(settings, None)
    reply = agent.handle("посчитай неустойку по Сургаев на 20.01.2026")
    assert "Неустойка всего" in reply.text and "Цена иска" in reply.text
    assert "✓ claimant_name" in agent.handle("досье Сургаев").text


def test_ask_flags_bogus_citations(settings: Settings) -> None:
    llm = FakeLLM(
        {
            "Intent": {"action": "ask", "question": "ремонт"},
            "Answer": {"text": "Страховщик обязан организовать ремонт [1], а ещё [7].", "used": [1, 7]},
        }
    )
    reply = agent_with_case(settings, llm).handle("обязан ли страховщик организовать ремонт?")
    assert "[7]" in reply.text and "не существуют" in reply.text
    assert "Пленума" in reply.text.split("Источники")[1]


def test_unknown_case_lists_existing(settings: Settings) -> None:
    agent = agent_with_case(settings, None)
    agent.store.create("Иванов Альфа")
    assert "Есть:" in agent.handle("сделай иск по Петрову").text


def test_case_found_by_declined_surname(settings: Settings) -> None:
    agent = agent_with_case(settings, None)
    agent.store.create("Иванов Альфа")
    reply = agent.handle("посчитай неустойку по Сургаеву")
    assert "Неустойка" in reply.text
