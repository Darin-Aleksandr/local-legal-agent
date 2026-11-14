"""Вся арифметика дела — кодом, с расшифровкой каждой цифры. Нейросеть ничего не считает.

Карта отказов: «цена иска в тексте, в заявлении о пошлине и в расчётах различалась; неустойка не
пересчитывалась при переносе даты подачи». Здесь каждая сумма вычисляется из досье в одном месте,
а документы берут готовые числа отсюда. Поменялась дата — пересчитались все документы.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

OSAGO_LIMIT = Decimal("400000")  # страховая сумма по вреду имуществу, п. «б» ст. 7 Закона об ОСАГО
PENALTY_RATE = Decimal("0.01")  # 1% в день, п. 21 ст. 12 Закона об ОСАГО
FINE_RATE = Decimal("0.5")  # штраф 50%, п. 3 ст. 16.1 Закона об ОСАГО
DECISION_DAYS = 20  # срок страховщика: 20 календарных дней без нерабочих праздничных, п. 21 ст. 12
CONSUMER_EXEMPTION = Decimal("1000000")  # п. 3 ст. 333.36 НК РФ

# Нерабочие праздничные дни по ст. 112 ТК РФ (без переносов выходных: для срока по п. 21 ст. 12
# Закона об ОСАГО исключаются именно праздники, а не выходные).
HOLIDAYS = {(1, d) for d in range(1, 9)} | {(2, 23), (3, 8), (5, 1), (5, 9), (6, 12), (11, 4)}

# Шкала пп. 1 п. 1 ст. 333.19 НК РФ (ред. Федерального закона № 259-ФЗ, с 08.09.2024):
# (верхняя граница цены иска, база, ставка на превышение, от какой суммы превышение)
DUTY_SCALE: list[tuple[Decimal, Decimal, Decimal, Decimal]] = [
    (Decimal("100000"), Decimal("4000"), Decimal("0"), Decimal("0")),
    (Decimal("300000"), Decimal("4000"), Decimal("0.03"), Decimal("100000")),
    (Decimal("500000"), Decimal("10000"), Decimal("0.025"), Decimal("300000")),
    (Decimal("1000000"), Decimal("15000"), Decimal("0.02"), Decimal("500000")),
    (Decimal("3000000"), Decimal("25000"), Decimal("0.01"), Decimal("1000000")),
    (Decimal("8000000"), Decimal("45000"), Decimal("0.007"), Decimal("3000000")),
    (Decimal("24000000"), Decimal("80000"), Decimal("0.0035"), Decimal("8000000")),
    (Decimal("50000000"), Decimal("136000"), Decimal("0.003"), Decimal("24000000")),
    (Decimal("100000000"), Decimal("214000"), Decimal("0.002"), Decimal("50000000")),
]
DUTY_CAP = Decimal("900000")
APPEAL_DUTY_PERSON = Decimal("3000")  # апелляционная и частная жалоба, физлицо, пп. 19 п. 1 ст. 333.19


def money(value: Decimal | float | int | str) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def rub(value: Decimal) -> str:
    """«123 456,78 руб.» — как пишут в документах."""
    whole, _, cents = f"{money(value):.2f}".partition(".")
    groups = f"{int(whole):,}".replace(",", " ")
    return f"{groups},{cents} руб."


def is_holiday(day: date) -> bool:
    return (day.month, day.day) in HOLIDAYS


def decision_deadline(application: date) -> date:
    """Последний день срока страховщика: 20 календарных дней со дня, следующего за подачей заявления,
    без нерабочих праздничных дней (п. 21 ст. 12 Закона об ОСАГО)."""
    day, counted = application, 0
    while counted < DECISION_DAYS:
        day += timedelta(days=1)
        if not is_holiday(day):
            counted += 1
    return day


@dataclass(frozen=True)
class PenaltyPeriod:
    start: date
    end: date
    base: Decimal
    days: int
    amount: Decimal


@dataclass
class Penalty:
    deadline: date
    periods: list[PenaltyPeriod] = field(default_factory=list)
    capped: bool = False

    @property
    def total(self) -> Decimal:
        total = money(sum((p.amount for p in self.periods), Decimal(0)))
        return min(total, OSAGO_LIMIT)

    def explain(self) -> list[str]:
        lines = [
            f"Срок страховщика истёк {self.deadline:%d.%m.%Y} (20 дней без нерабочих праздничных, "
            "п. 21 ст. 12 Закона об ОСАГО); неустойка — со следующего дня, 1% в день."
        ]
        lines += [
            f"{p.start:%d.%m.%Y}–{p.end:%d.%m.%Y}: {rub(p.base)} × 1% × {p.days} дн. = {rub(p.amount)}"
            for p in self.periods
        ]
        if self.capped:
            lines.append(f"Итог ограничен страховой суммой {rub(OSAGO_LIMIT)} (п. 6 ст. 16.1 Закона об ОСАГО).")
        lines.append(f"Неустойка всего: {rub(self.total)}")
        return lines


def osago_penalty(
    owed: Decimal, application: date, until: date, payments: list[tuple[date, Decimal]] | None = None
) -> Penalty:
    """Неустойка 1% в день на недоплаченную часть страхового возмещения.

    owed — сколько страховщик должен был выплатить (по Единой методике без износа, но не больше лимита).
    payments — выплаты страховщика: каждая уменьшает базу со следующего дня после выплаты.
    until — по какой день считаем (обычно день подачи иска или день фактической выплаты).
    """
    owed = min(money(owed), OSAGO_LIMIT)
    deadline = decision_deadline(application)
    penalty = Penalty(deadline)
    events = sorted((d, money(a)) for d, a in (payments or []))
    base = max(owed - sum((a for d, a in events if d <= deadline), Decimal(0)), Decimal(0))
    start = deadline + timedelta(days=1)
    # Каждая просроченная выплата: неустойка на старую базу по день выплаты включительно, дальше — на остаток.
    stops = [(d, a) for d, a in events if deadline < d <= until] + [(until, Decimal(0))]
    for end, amount in stops:
        if base > 0 and end >= start:
            days = (end - start).days + 1
            penalty.periods.append(PenaltyPeriod(start, end, base, days, money(base * PENALTY_RATE * days)))
        base = max(base - amount, Decimal(0))
        start = max(start, end + timedelta(days=1))
    raw = sum((p.amount for p in penalty.periods), Decimal(0))
    penalty.capped = raw > OSAGO_LIMIT
    return penalty


def osago_fine(unpaid_awarded: Decimal) -> Decimal:
    """Штраф 50% от суммы страхового возмещения, не выплаченной добровольно (п. 3 ст. 16.1)."""
    return money(money(unpaid_awarded) * FINE_RATE)


def duty_scale(price: Decimal) -> Decimal:
    price = money(price)
    for upper, base, rate, over in DUTY_SCALE:
        if price <= upper:
            return money(base + (price - over) * rate) if rate else base
    return min(money(Decimal("314000") + (price - Decimal("100000000")) * Decimal("0.0015")), DUTY_CAP)


@dataclass(frozen=True)
class Duty:
    amount: Decimal
    explanation: str


def claim_duty(price: Decimal, *, consumer: bool = True) -> Duty:
    """Госпошлина по иску. Потребитель (ОСАГО — потребительский спор) освобождён до 1 млн руб. цены иска;
    сверх — платит разницу между пошлиной от цены иска и пошлиной от 1 млн (п. 3 ст. 333.36 НК РФ)."""
    full = duty_scale(price)
    if not consumer:
        return Duty(full, f"пп. 1 п. 1 ст. 333.19 НК РФ от цены иска {rub(price)}: {rub(full)}")
    if price <= CONSUMER_EXEMPTION:
        return Duty(
            Decimal(0),
            "Истец освобождён от уплаты госпошлины: защита прав потребителя, цена иска "
            "не превышает 1 000 000 руб. (пп. 4 п. 2, п. 3 ст. 333.36 НК РФ)",
        )
    due = money(full - duty_scale(CONSUMER_EXEMPTION))
    return Duty(
        due,
        f"Цена иска {rub(price)} больше 1 000 000 руб.: {rub(full)} − {rub(duty_scale(CONSUMER_EXEMPTION))} "
        f"= {rub(due)} (п. 3 ст. 333.36 НК РФ)",
    )


# ---------------------------------------------------------------- сумма прописью
_ONES = ["", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"]
_ONES_F = ["", "одна", "две", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"]
_TEENS = [
    "десять",
    "одиннадцать",
    "двенадцать",
    "тринадцать",
    "четырнадцать",
    "пятнадцать",
    "шестнадцать",
    "семнадцать",
    "восемнадцать",
    "девятнадцать",
]
_TENS = ["", "", "двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят", "семьдесят", "восемьдесят", "девяносто"]
_HUNDREDS = ["", "сто", "двести", "триста", "четыреста", "пятьсот", "шестьсот", "семьсот", "восемьсот", "девятьсот"]
_SCALES = [
    (("рубль", "рубля", "рублей"), False),
    (("тысяча", "тысячи", "тысяч"), True),
    (("миллион", "миллиона", "миллионов"), False),
    (("миллиард", "миллиарда", "миллиардов"), False),
]


def _plural(n: int, forms: tuple[str, str, str]) -> str:
    if n % 100 in range(11, 20):
        return forms[2]
    return forms[0] if n % 10 == 1 else forms[1] if n % 10 in (2, 3, 4) else forms[2]


def _triad(n: int, feminine: bool) -> list[str]:
    words = [_HUNDREDS[n // 100]]
    rest = n % 100
    if 10 <= rest < 20:
        words.append(_TEENS[rest - 10])
    else:
        words += [_TENS[rest // 10], (_ONES_F if feminine else _ONES)[rest % 10]]
    return [w for w in words if w]


def in_words(value: Decimal | float) -> str:
    """«128 600,00» → «сто двадцать восемь тысяч шестьсот рублей 00 копеек»."""
    amount = money(value)
    rubles, kopecks = int(amount), int((amount - int(amount)) * 100)
    if rubles == 0:
        words = ["ноль", "рублей"]
    else:
        words, scale, n = [], 0, rubles
        while n:
            triad = n % 1000
            forms, feminine = _SCALES[scale]
            if triad or scale == 0:
                words = [*_triad(triad, feminine), _plural(triad, forms), *words] if triad else [forms[2], *words]
            n //= 1000
            scale += 1
    kop = _plural(kopecks, ("копейка", "копейки", "копеек"))
    return f"{' '.join(words)} {kopecks:02d} {kop}"
