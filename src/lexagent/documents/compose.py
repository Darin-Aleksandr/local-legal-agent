"""Сборка документов: претензия страховщику, обращение к финансовому уполномоченному, исковое заявление.

Кто что делает:
- реквизиты и суммы — код, из досье и calc.py (нейросеть их не трогает);
- правовая позиция — нейросеть, но только по найденным в библиотеке фрагментам: каждый довод обязан
  ссылаться на номер фрагмента; довод без ссылки выбрасывается; номер дела, которого нет во фрагментах,
  заменяется на [ПРОВЕРИТЬ];
- всё, чего нет в досье или что не подтверждено, помечено [ПРОВЕРИТЬ] и вынесено в список проверок.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

from lexagent import calc
from lexagent.cases import FIELDS, Dossier
from lexagent.dates import parse_date
from lexagent.library.store import Embed, Hit, Library
from lexagent.llm import LLM, parse

DocKind = Literal["претензия", "обращение", "иск"]
CHECK = "[ПРОВЕРИТЬ]"
MORAL_DEFAULT = Decimal("10000")
CASE_NUMBER = re.compile(r"№\s*\d{1,3}-[А-ЯA-Z]{1,4}\d{2}-\d+(?:-[А-ЯA-Zа-я\d]+)*|№\s*\d+-[ПО]")

TITLES = {"претензия": "Претензия", "обращение": "Обращение к финансовому уполномоченному", "иск": "Исковое заявление"}
QUERIES = {
    "претензия": [
        "обязанность страховщика организовать восстановительный ремонт",
        "неустойка 1 процент",
        "смена формы возмещения без согласия потерпевшего",
    ],
    "обращение": [
        "обращение к финансовому уполномоченному досудебный порядок",
        "неустойка 1 процент",
        "восстановительный ремонт без учета износа",
    ],
    "иск": [
        "убытки при неисполнении обязанности организовать ремонт",
        "неустойка 1 процент по день исполнения",
        "штраф 50 процентов потребитель",
        "компенсация морального вреда потребителю",
        "лимит ответственности страховщика 400 000",
    ],
}


@dataclass
class Money:
    """Все суммы дела — одним расчётом, чтобы в тексте, требованиях и пошлине были одни и те же числа."""

    owed: Decimal
    paid: Decimal
    principal: Decimal
    penalty: calc.Penalty | None
    expertise: Decimal
    fine: Decimal
    moral: Decimal
    price: Decimal
    duty: calc.Duty
    above_limit: Decimal  # что за пределами 400 000 — не к страховщику

    def lines(self) -> list[str]:
        out = [
            f"Страховщик должен был возместить: {calc.rub(self.owed)}",
            f"Выплачено страховщиком: {calc.rub(self.paid)}",
            f"Недоплата: {calc.rub(self.principal)}",
        ]
        if self.penalty:
            out += self.penalty.explain()
        out += [f"Цена иска (недоплата + неустойка): {calc.rub(self.price)}", self.duty.explanation]
        return out


@dataclass
class Argument:
    text: str
    sources: list[str]


@dataclass
class Draft:
    kind: DocKind
    title: str
    header: list[str]
    subtitle: str
    body: list[str]
    arguments: list[Argument]
    demands: list[str]
    attachments: list[str]
    signature: list[str]
    calculation: list[str]
    checks: list[str] = field(default_factory=list)

    def text(self) -> str:
        parts = ["\n".join(self.header), "", self.title.upper(), self.subtitle, "", *self.body, ""]
        parts += [f"{a.text} ({'; '.join(a.sources)})" for a in self.arguments]
        parts += ["", "ПРОШУ:" if self.kind != "иск" else "На основании изложенного, ПРОШУ:"]
        parts += [f"{i}. {d}" for i, d in enumerate(self.demands, 1)]
        parts += ["", "Приложения:", *[f"{i}. {a}" for i, a in enumerate(self.attachments, 1)], "", *self.signature]
        return tidy("\n".join(parts))


def tidy(text: str) -> str:
    """«руб..» и «П. П..» после подстановки сумм и имён в конец предложения → одна точка."""
    return re.sub(r"(?<!\.)\.\.(?!\.)", ".", text)


class LLMArgument(BaseModel):
    text: str = Field(description="довод, 2–5 предложений, официально-деловой стиль")
    sources: list[int] = Field(description="номера фрагментов [N], на которых довод основан; минимум один")


class LLMArguments(BaseModel):
    arguments: list[LLMArgument]


ARGUE = (
    "Ты юрист, пишешь мотивировочную часть документа по спору ОСАГО на стороне потерпевшего. "
    "Опирайся ТОЛЬКО на пронумерованные фрагменты норм и практики. Каждый довод — с номерами фрагментов, "
    "на которые он опирается. Не упоминай документы, номера дел и пункты, которых нет во фрагментах. "
    "Если для довода нет фрагмента — не пиши этот довод. Суммы не пиши: их подставит программа. "
    "Если фрагменты противоречат друг другу, используй тот, что отмечен «применяется»."
)


class Facts:
    """Доступ к досье с учётом пробелов: нет значения — [ПРОВЕРИТЬ], не подтверждено — в список проверок."""

    def __init__(self, dossier: Dossier) -> None:
        self.dossier = dossier
        self.checks: list[str] = []

    def __call__(self, key: str) -> str:
        fact = self.dossier.facts.get(key)
        label = FIELDS[key][0]
        if fact is None or not fact.value.strip():
            self._check(f"нет в досье: {label}")
            return f"{CHECK} {label.lower()}"
        if not fact.confirmed:
            self._check(f"не подтверждено: {label} = {fact.value}")
        return fact.value

    def money(self, key: str) -> Decimal | None:
        fact = self.dossier.facts.get(key)
        if fact is None:
            return None
        try:
            return calc.money(fact.value)
        except ArithmeticError:
            self._check(f"не число: {FIELDS[key][0]} = {fact.value}")
            return None

    def date(self, key: str) -> date | None:
        value = self.dossier.value(key)
        parsed = parse_date(value) if value else None
        if value and parsed is None:
            self._check(f"не дата: {FIELDS[key][0]} = {value}")
        return parsed

    def _check(self, item: str) -> None:
        if item not in self.checks:
            self.checks.append(item)


def compute(facts: Facts, until: date, *, moral: Decimal = MORAL_DEFAULT) -> Money:
    """Расчёт по досье. Источник «должен был»: рыночная стоимость ремонта (убытки, если страховщик не
    организовал ремонт), иначе — Единая методика без износа; в любом случае не больше лимита 400 000."""
    market = facts.money("repair_cost_market")
    unified = facts.money("repair_cost_unified")
    base = market if market is not None else unified
    if base is None:
        facts._check("нет суммы ущерба: нужна экспертиза (рыночная стоимость или Единая методика)")
        base = Decimal(0)
    owed = min(base, calc.OSAGO_LIMIT)
    above = max(base - calc.OSAGO_LIMIT, Decimal(0))
    if above:
        facts._check(
            f"ущерб больше лимита на {calc.rub(above)}: эту часть — к виновнику (ст. 1072 ГК РФ); "
            "проверить актуальную позицию КС/ВС о лимите в отчёте библиотеки"
        )
    payments = [(p.paid_on, calc.money(p.amount)) for p in facts.dossier.payments]
    for p in facts.dossier.payments:
        if not p.confirmed:
            facts._check(f"не подтверждена выплата {calc.rub(calc.money(p.amount))} от {p.paid_on:%d.%m.%Y}")
    paid = calc.money(sum((a for _d, a in payments), Decimal(0)))
    principal = max(owed - paid, Decimal(0))
    applied = facts.date("application_date")
    penalty = calc.osago_penalty(owed, applied, until, payments) if applied else None
    if applied is None:
        facts._check("нет даты заявления страховщику — неустойка не рассчитана")
    price = principal + (penalty.total if penalty else Decimal(0))
    return Money(
        owed=owed,
        paid=paid,
        principal=principal,
        penalty=penalty,
        expertise=facts.money("expertise_cost") or Decimal(0),
        fine=calc.osago_fine(principal),
        moral=moral,
        price=price,
        duty=calc.claim_duty(price),
        above_limit=above,
    )


def compose(
    kind: DocKind,
    dossier: Dossier,
    *,
    library: Library | None = None,
    llm: LLM | None = None,
    embed: Embed | None = None,
    on: date | None = None,
) -> Draft:
    on = on or date.today()
    facts = Facts(dossier)
    money = compute(facts, on)
    hits = _research(kind, library, embed)
    arguments = _arguments(kind, facts, hits, llm)
    if not arguments:
        facts._check("нет доводов с опорой на библиотеку: обновите библиотеку или добавьте практику")
    builder = {"претензия": _claim, "обращение": _ombudsman, "иск": _lawsuit}[kind]
    draft = builder(facts, money, on)
    draft.arguments = arguments
    draft.calculation = money.lines()
    draft.checks = facts.checks
    return draft


# ---------------------------------------------------------------- research & arguments
def _research(kind: DocKind, library: Library | None, embed: Embed | None) -> list[Hit]:
    if library is None:
        return []
    seen: dict[int, Hit] = {}
    for query in QUERIES[kind]:
        for hit in library.search(query, k=4, embed=embed, tags=["ОСАГО"]):
            seen.setdefault(hit.chunk_id, hit)
    return list(seen.values())[:14]


def _source(hit: Hit) -> str:
    heading = hit.heading.strip()
    return f"{heading}, {hit.document.cite()}" if heading else hit.document.cite()


def _arguments(kind: DocKind, facts: Facts, hits: list[Hit], llm: LLM | None) -> list[Argument]:
    if not hits:
        return []
    if llm is None:  # без нейросети — честные цитаты норм вместо рассуждений
        return [Argument(f"«{_first_sentences(h.text)}»", [_source(h)]) for h in hits[:5]]
    context = "\n\n".join(f"[{i}] {_source(h)}\n{h.text[:1500]}" for i, h in enumerate(hits, 1))
    situation = "; ".join(f"{FIELDS[k][0]}: {f.value}" for k, f in facts.dossier.facts.items())
    reply = parse(
        llm, ARGUE, f"Документ: {TITLES[kind]}.\nОбстоятельства: {situation}\n\nФрагменты:\n{context}", LLMArguments
    )
    allowed_text = " ".join(h.text for h in hits)
    out = []
    for arg in reply.arguments:
        refs = [hits[n - 1] for n in dict.fromkeys(arg.sources) if 1 <= n <= len(hits)]
        if not refs:
            facts._check(f"довод без источника удалён: «{arg.text[:80]}…»")
            continue
        out.append(Argument(_guard_numbers(arg.text, allowed_text, facts), [_source(h) for h in refs]))
    return out


def _guard_numbers(text: str, allowed: str, facts: Facts) -> str:
    """Номер дела, которого нет в найденных документах, — выдумка: заменяем и сообщаем."""

    def replace(match: re.Match[str]) -> str:
        number = match.group(0)
        if re.sub(r"\s", "", number) in re.sub(r"\s", "", allowed):
            return number
        facts._check(f"нейросеть упомянула {number}, которого нет в библиотеке — удалено")
        return CHECK

    return CASE_NUMBER.sub(replace, text)


def _first_sentences(text: str, limit: int = 400) -> str:
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    cut = flat[:limit]
    return cut[: cut.rfind(". ") + 1] or cut + "…"


# ---------------------------------------------------------------- documents
def _claimant(f: Facts) -> list[str]:
    return [f"от {f('claimant_name')}", f"адрес: {f('claimant_address')}", f"тел.: {f('claimant_phone')}"]


def _circumstances(f: Facts, m: Money) -> list[str]:
    body = [
        f"{f('accident_date')} по адресу: {f('accident_place')} произошло дорожно-транспортное происшествие, "
        f"в результате которого причинены повреждения принадлежащему мне транспортному средству "
        f"{f('vehicle')}. Виновником ДТП является {f('culprit_name')}.",
        f"Гражданская ответственность застрахована в {f('insurer_name')} (полис {f('policy_number')}).",
        f"{f('application_date')} я обратился к страховщику с заявлением о страховом возмещении "
        f"(убыток № {f('insurer_claim_number')}), предоставив все необходимые документы и транспортное средство "
        "для осмотра.",
    ]
    if m.paid:
        body.append(
            f"Страховщик выплатил {calc.rub(m.paid)}, что не покрывает причитающееся возмещение: "
            f"размер ущерба в пределах лимита составляет {calc.rub(m.owed)}, недоплата — "
            f"{calc.rub(m.principal)}."
        )
    else:
        body.append(
            f"Страховое возмещение не выплачено, ремонт не организован. Размер ущерба в пределах лимита — "
            f"{calc.rub(m.owed)}."
        )
    if m.penalty and m.penalty.periods:
        body.append(
            f"Срок рассмотрения заявления истёк {m.penalty.deadline:%d.%m.%Y}. Неустойка за период по "
            f"{m.penalty.periods[-1].end:%d.%m.%Y} составляет {calc.rub(m.penalty.total)} "
            "(расчёт приложен)."
        )
    return body


def _claim(f: Facts, m: Money, on: date) -> Draft:
    demands = [f"Выплатить страховое возмещение (убытки) в размере {calc.rub(m.principal)}."]
    if m.penalty and m.penalty.total:
        demands.append(f"Выплатить неустойку в размере {calc.rub(m.penalty.total)}.")
    if m.expertise:
        demands.append(f"Возместить расходы на независимую экспертизу в размере {calc.rub(m.expertise)}.")
    demands.append("Ответ направить в течение 15 рабочих дней (п. 1 ст. 16.1 Закона об ОСАГО).")
    return Draft(
        "претензия",
        TITLES["претензия"],
        [f"В {f('insurer_name')}", f"ИНН {f('insurer_inn')}", f"адрес: {f('insurer_address')}", "", *_claimant(f)],
        f"(заявление о несогласии с размером и формой страхового возмещения, убыток № {f('insurer_claim_number')})",
        _circumstances(f, m),
        [],
        demands,
        [
            "Копия заключения независимой экспертизы.",
            "Расчёт неустойки.",
            "Реквизиты для перечисления денежных средств.",
        ],
        [f"{on:%d.%m.%Y}", f"______________ / {f('claimant_name')}"],
        [],
    )


def _ombudsman(f: Facts, m: Money, on: date) -> Draft:
    demands = [f"Взыскать с {f('insurer_name')} страховое возмещение (убытки) в размере {calc.rub(m.principal)}."]
    if m.penalty and m.penalty.total:
        demands.append(
            f"Взыскать неустойку в размере {calc.rub(m.penalty.total)} и далее по день фактического "
            "исполнения обязательства."
        )
    if m.expertise:
        demands.append(f"Взыскать расходы на независимую экспертизу в размере {calc.rub(m.expertise)}.")
    body = [
        *_circumstances(f, m),
        "С претензией я обращался к страховщику; требования в добровольном порядке не удовлетворены "
        f"{CHECK} дата претензии и ответа страховщика.",
    ]
    f._check("обращение к ФУ: указать дату претензии и ответа страховщика, приложить их копии")
    return Draft(
        "обращение",
        TITLES["обращение"],
        [
            "Уполномоченному по правам потребителей финансовых услуг",
            "",
            *_claimant(f),
            "",
            f"Финансовая организация: {f('insurer_name')}, ИНН {f('insurer_inn')}",
            f"адрес: {f('insurer_address')}",
        ],
        "(в порядке ст. 17 Федерального закона от 04.06.2018 № 123-ФЗ)",
        body,
        [],
        demands,
        [
            "Копия заявления о страховом возмещении.",
            "Копия претензии и ответа страховщика.",
            "Копия заключения независимой экспертизы.",
            "Расчёт неустойки.",
            "Сведения о направлении претензии (опись, квитанция).",
        ],
        [f"{on:%d.%m.%Y}", f"______________ / {f('claimant_name')}"],
        [],
    )


def _lawsuit(f: Facts, m: Money, on: date) -> Draft:
    insurer = f("insurer_name")
    demands = [f"Взыскать с {insurer} в пользу истца страховое возмещение (убытки) в размере {calc.rub(m.principal)}."]
    if m.penalty and m.penalty.total:
        first, last = m.penalty.periods[0].start, m.penalty.periods[-1].end
        demand = (
            f"Взыскать неустойку за период с {first:%d.%m.%Y} по {last:%d.%m.%Y} в размере {calc.rub(m.penalty.total)}"
        )
        left = calc.OSAGO_LIMIT - m.penalty.total
        if left > 0 and m.principal > 0:
            demand += (
                f", а также неустойку в размере 1% от суммы недоплаты за каждый день с {last:%d.%m.%Y} "
                f"по день фактического исполнения, но не более {calc.rub(left)} в совокупности"
            )
        demands.append(demand + ".")
    demands += [
        f"Взыскать штраф в размере 50% от суммы недоплаченного страхового возмещения "
        f"({calc.rub(m.fine)} на дату подачи иска).",
        f"Взыскать компенсацию морального вреда в размере {calc.rub(m.moral)}.",
    ]
    if m.expertise:
        demands.append(f"Взыскать расходы на независимую экспертизу в размере {calc.rub(m.expertise)}.")
    f._check(f"моральный вред {calc.rub(m.moral)} — сумма по умолчанию, скорректируйте")
    fu = f"решение финансового уполномоченного № {f('fu_decision_number')} от {f('fu_decision_date')}"
    body = [
        *_circumstances(f, m),
        f"Досудебный порядок соблюдён: {fu}. Истец с решением не согласен.",
        f"Цена иска: {calc.rub(m.price)} ({calc.in_words(m.price)}). {m.duty.explanation}.",
    ]
    return Draft(
        "иск",
        TITLES["иск"],
        [
            f"В {f('court_name')}",
            "",
            f"Истец: {f('claimant_name')}",
            f"адрес: {f('claimant_address')}",
            f"тел.: {f('claimant_phone')}",
            "",
            f"Ответчик: {insurer}",
            f"ИНН {f('insurer_inn')}",
            f"адрес: {f('insurer_address')}",
            "",
            "Третье лицо: Уполномоченный по правам потребителей финансовых услуг",
            "",
            f"Цена иска: {calc.rub(m.price)}",
            "Госпошлина: " + ("не уплачивается (ЗоЗПП)" if not m.duty.amount else calc.rub(m.duty.amount)),
        ],
        "о взыскании страхового возмещения, неустойки, штрафа и компенсации морального вреда",
        body,
        [],
        demands,
        [
            "Документы, подтверждающие направление копий иска ответчику и третьему лицу.",
            "Копия решения финансового уполномоченного.",
            "Копия заявления о страховом возмещении и претензии.",
            "Копия заключения независимой экспертизы.",
            "Расчёт неустойки.",
            "Копия документа, удостоверяющего личность / доверенности представителя.",
        ],
        [f"{on:%d.%m.%Y}", f"______________ / {f('claimant_name')}"],
        [],
    )
