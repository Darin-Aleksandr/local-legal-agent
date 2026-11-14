"""Какой документ главнее, когда позиции расходятся. Чистые правила, без нейросети.

1. Нормы закона: действует только последняя редакция. Старая уходит в архив и в ответах не участвует.
2. Практика: выше инстанция — сильнее позиция. КС → Пленум ВС → обзоры Президиума ВС → определения
   коллегий ВС → кассация → апелляция → первая инстанция.
3. При равной инстанции побеждает более поздний документ.
4. Расхождение никогда не разрешается молча: оно записывается в журнал конфликтов и попадает в отчёт.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum


class Kind(StrEnum):
    LAW = "law"  # федеральный закон, кодекс, положение ЦБ
    KS = "ks"  # постановление/определение Конституционного Суда
    PLENUM = "plenum"  # постановление Пленума ВС
    REVIEW = "review"  # обзор практики, утверждённый Президиумом ВС
    VS = "vs"  # определение судебной коллегии ВС
    CASSATION = "cassation"
    APPEAL = "appeal"
    FIRST = "first"
    OWN = "own"  # документы самого юриста: прошлые иски, решения по его делам
    OTHER = "other"


RANK: dict[Kind, int] = {
    Kind.KS: 100,
    Kind.PLENUM: 90,
    Kind.REVIEW: 80,
    Kind.VS: 70,
    Kind.CASSATION: 50,
    Kind.APPEAL: 40,
    Kind.FIRST: 30,
    Kind.OWN: 20,
    Kind.OTHER: 10,
    Kind.LAW: 0,  # нормы не конкурируют с практикой по рангу: у них своя линия редакций
}

LABEL: dict[Kind, str] = {
    Kind.LAW: "Закон",
    Kind.KS: "КС РФ",
    Kind.PLENUM: "Пленум ВС РФ",
    Kind.REVIEW: "Обзор ВС РФ",
    Kind.VS: "Определение ВС РФ",
    Kind.CASSATION: "Кассация",
    Kind.APPEAL: "Апелляция",
    Kind.FIRST: "Первая инстанция",
    Kind.OWN: "Документы юриста",
    Kind.OTHER: "Прочее",
}


@dataclass(frozen=True)
class Position:
    kind: Kind
    issued: date | None


def stronger(a: Position, b: Position) -> Position:
    """Чья позиция применяется: выше инстанция, при равенстве — свежее. Без даты — слабее датированного."""
    if RANK[a.kind] != RANK[b.kind]:
        return a if RANK[a.kind] > RANK[b.kind] else b
    if a.issued and b.issued:
        return a if a.issued >= b.issued else b
    return a if a.issued else b


def explain(winner: Position, loser: Position) -> str:
    if RANK[winner.kind] != RANK[loser.kind]:
        return f"{LABEL[winner.kind]} выше по инстанции, чем {LABEL[loser.kind]}"
    return "та же инстанция, применяется более поздний документ"
