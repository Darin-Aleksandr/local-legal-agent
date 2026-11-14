"""Разбор папки «Входящие»: юрист кидает файлы как есть, агент раскладывает их по делам.

Защита от выдумок: каждое значение, которое нейросеть «нашла» в документе, должно сопровождаться
дословной цитатой. Код проверяет, что цитата действительно есть в тексте; нет цитаты — значение
отбрасывается. Всё распознанное попадает в досье как «не подтверждено» — пока человек не согласится.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from lexagent.cases import FIELDS, CaseStore, Party, Payment, Source, sha256
from lexagent.dates import parse_date
from lexagent.library.authority import Kind
from lexagent.library.store import DocMeta, Embed, Library
from lexagent.llm import LLM, parse
from lexagent.textio import UnsupportedFileError, extract

DocType = Literal[
    "заявление в страховую",
    "ответ страховой",
    "акт осмотра",
    "экспертиза",
    "решение финуполномоченного",
    "обращение к финуполномоченному",
    "решение суда",
    "определение суда",
    "исковое заявление",
    "претензия",
    "полис",
    "извещение о ДТП",
    "документы ГИБДД",
    "платёжный документ",
    "доверенность",
    "судебная практика по чужому делу",
    "образец документа",
    "другое",
]
LIBRARY_TYPES = {"судебная практика по чужому делу", "образец документа"}
SKIP = {"ссылки.txt", "_разобрано"}
MAX_CHARS = 14000


class FoundFact(BaseModel):
    key: str = Field(description="одно из полей: " + ", ".join(FIELDS))
    value: str
    quote: str = Field(description="дословный фрагмент документа, где это написано")


class FoundPayment(BaseModel):
    paid_on: str = Field(description="дата выплаты ДД.ММ.ГГГГ")
    amount: float
    quote: str


class FoundParty(BaseModel):
    role: str
    name: str
    address: str = ""


class Reading(BaseModel):
    doc_type: DocType
    summary: str = Field(description="о чём документ, 1 предложение")
    identifiers: list[str] = Field(
        default_factory=list, description="номера полиса, убытка, дела, решения ФУ; ФИО потерпевшего"
    )
    facts: list[FoundFact] = Field(default_factory=list)
    payments: list[FoundPayment] = Field(default_factory=list)
    parties: list[FoundParty] = Field(default_factory=list)


SYSTEM = (
    "Ты помощник юриста по спорам ОСАГО. Прочитай документ и заполни JSON. Правила: "
    "1) Бери только то, что прямо написано в документе. Чего нет — не заполняй. "
    "2) Для каждого факта и выплаты приведи quote — дословный фрагмент из документа (копируй символ в символ). "
    "3) Даты в формате ДД.ММ.ГГГГ, суммы числом в рублях. "
    "4) doc_type «судебная практика по чужому делу» — если это решение суда не по делу клиента, а образец "
    "практики; «образец документа» — шаблон без конкретных данных клиента."
)


@dataclass
class Routed:
    file: str
    doc_type: str
    case: str | None = None  # id дела, куда положили
    library: bool = False
    facts: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)  # «найденное» без подтверждающей цитаты
    error: str | None = None


def process_inbox(
    inbox: Path,
    store: CaseStore,
    llm: LLM,
    *,
    library: Library | None = None,
    embed: Embed | None = None,
    lang: str = "rus+eng",
) -> list[Routed]:
    results = []
    for path in sorted(inbox.iterdir()):
        if path.name in SKIP or path.name.startswith(".") or path.is_dir():
            continue
        results.append(process_file(path, store, llm, library=library, embed=embed, lang=lang))
        _archive(path, inbox)
    return results


def process_file(
    path: Path,
    store: CaseStore,
    llm: LLM,
    *,
    library: Library | None = None,
    embed: Embed | None = None,
    lang: str = "rus+eng",
) -> Routed:
    try:
        extracted = extract(path, lang)
    except UnsupportedFileError as exc:
        return Routed(path.name, "не прочитан", error=str(exc))
    text = extracted.text
    reading = parse(llm, SYSTEM, f"Файл: {path.name}\n\n{text[:MAX_CHARS]}", Reading)
    routed = Routed(path.name, reading.doc_type)

    if reading.doc_type in LIBRARY_TYPES and library is not None:
        kind = Kind.OWN if reading.doc_type == "образец документа" else _court_kind(text)
        library.add(
            DocMeta(f"own:{sha256(path)}", f"{reading.summary} ({path.name})", kind, tags=("ОСАГО", "свои")),
            text,
            embed,
        )
        routed.library = True
        return routed

    case = store.find(reading.identifiers) or store.create(_title(reading, path), keywords=reading.identifiers)
    routed.case = case.root.name
    original = case.add_original(path)
    case.save_text(original, text)
    dossier = case.load()
    dossier.keywords = sorted(set(dossier.keywords) | {i for i in reading.identifiers if i})
    case.save(dossier, f"разобран {original.name}: {reading.doc_type}")

    for fact in reading.facts:
        if fact.key not in FIELDS or not fact.value.strip():
            continue
        page = locate(fact.quote, extracted.pages)
        if page is None:
            routed.dropped.append(f"{FIELDS[fact.key][0]}: «{fact.value}» — нет такой цитаты в документе")
            continue
        value = normalize_value(fact.key, fact.value)
        case.set_fact(
            fact.key,
            value,
            source=Source(file=original.name, page=page, quote=fact.quote[:300]),
            confirmed=False,
            reason=f"из {original.name}",
        )
        routed.facts.append(f"{FIELDS[fact.key][0]}: {value}")

    dossier = case.load()
    for found in reading.payments:
        page = locate(found.quote, extracted.pages)
        paid_on = parse_date(found.paid_on)
        if page is None or paid_on is None:
            routed.dropped.append(f"выплата {found.amount} ₽ от {found.paid_on} — не подтверждена цитатой")
            continue
        if not any(p.paid_on == paid_on and abs(p.amount - found.amount) < 0.01 for p in dossier.payments):
            dossier.payments.append(
                Payment(
                    paid_on=paid_on,
                    amount=found.amount,
                    source=Source(file=original.name, page=page, quote=found.quote[:300]),
                )
            )
    for party in reading.parties:
        if not any(p.name.lower() == party.name.lower() for p in dossier.parties):
            dossier.parties.append(Party(role=party.role, name=party.name, address=party.address))
    case.save(dossier, f"выплаты и участники из {original.name}")
    return routed


def locate(quote: str, pages: list) -> int | None:  # type: ignore[type-arg]
    """Номер страницы, где есть цитата (без учёта пробелов, регистра и кавычек). Нет — значит, выдумано."""
    needle = _flat(quote)
    if len(needle) < 4:
        return None
    for page in pages:
        if needle in _flat(page.text):
            return int(page.number)
    return None


def normalize_value(key: str, value: str) -> str:
    kind = FIELDS[key][1]
    if kind == "date" and (parsed := parse_date(value)):
        return parsed.strftime("%d.%m.%Y")
    if kind == "money":
        match = re.search(r"\d[\d\s\u00a0]*(?:[.,]\d{1,2})?", value)
        if not match:
            return value.strip()
        return f"{float(re.sub(r'\s', '', match.group(0)).replace(',', '.')):.2f}"
    return " ".join(value.split())


def _flat(text: str) -> str:
    return re.sub(r"[\s«»\"'„“”.,;:]+", "", text).lower().replace("ё", "е")


def _title(reading: Reading, path: Path) -> str:
    names = {f.key: f.value for f in reading.facts}
    claimant = names.get("claimant_name", "").split(" ")[0]
    insurer = names.get("insurer_name", "")
    return " ".join(x for x in (claimant, insurer) if x) or path.stem


def _court_kind(text: str) -> Kind:
    head = text[:2000].upper()
    if "ВЕРХОВН" in head:
        return Kind.VS
    if "КАССАЦИОНН" in head:
        return Kind.CASSATION
    if "АПЕЛЛЯЦИОНН" in head:
        return Kind.APPEAL
    return Kind.FIRST


def _archive(path: Path, inbox: Path) -> None:
    target = inbox / "_разобрано" / date.today().isoformat()
    target.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), target / path.name)
