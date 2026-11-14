from datetime import date
from decimal import Decimal as D

import pytest

from lexagent import calc


def test_deadline_skips_new_year_holidays() -> None:
    # 26–31 декабря = 6 дней, 1–8 января не считаются, ещё 14 дней → 22 января
    assert calc.decision_deadline(date(2025, 12, 25)) == date(2026, 1, 22)
    # без праздников — ровно 20 календарных дней
    assert calc.decision_deadline(date(2026, 3, 10)) == date(2026, 3, 30)


def test_deadline_counts_weekends() -> None:
    # выходные считаются: исключаются только нерабочие праздничные дни
    assert calc.decision_deadline(date(2026, 9, 1)) == date(2026, 9, 21)


def test_penalty_shrinks_after_partial_payment() -> None:
    penalty = calc.osago_penalty(D(200000), date(2026, 1, 10), date(2026, 3, 1), [(date(2026, 2, 10), D(150000))])
    assert penalty.deadline == date(2026, 1, 30)
    assert [(p.base, p.days) for p in penalty.periods] == [(D(200000), 11), (D(50000), 19)]
    assert penalty.total == D("31500.00")


def test_payment_in_time_reduces_base_from_start() -> None:
    penalty = calc.osago_penalty(D(100000), date(2026, 3, 2), date(2026, 4, 1), [(date(2026, 3, 10), D(60000))])
    assert penalty.periods[0].base == D(40000)


def test_full_payment_stops_penalty() -> None:
    penalty = calc.osago_penalty(D(100000), date(2026, 3, 2), date(2026, 6, 1), [(date(2026, 4, 1), D(100000))])
    assert penalty.periods[-1].end == date(2026, 4, 1)
    assert len(penalty.periods) == 1


def test_penalty_capped_by_insured_sum() -> None:
    penalty = calc.osago_penalty(D(400000), date(2024, 1, 15), date(2026, 1, 15))
    assert penalty.capped
    assert penalty.total == calc.OSAGO_LIMIT
    assert "п. 6 ст. 16.1" in "\n".join(penalty.explain())


def test_owed_above_limit_is_cut() -> None:
    penalty = calc.osago_penalty(D(900000), date(2026, 3, 2), date(2026, 3, 25))
    assert penalty.periods[0].base == calc.OSAGO_LIMIT


@pytest.mark.parametrize(
    ("price", "duty"),
    [
        (50000, "4000"),
        (100000, "4000"),
        (250000, "8500.00"),
        (450000, "13750.00"),
        (800000, "21000.00"),
        (2000000, "35000.00"),
        (5000000, "59000.00"),
        (200000000, "464000.00"),
        (10**10, "900000"),
    ],
)
def test_duty_scale(price: int, duty: str) -> None:
    assert calc.duty_scale(D(price)) == D(duty)


def test_consumer_exempt_up_to_million() -> None:
    assert calc.claim_duty(D(999999)).amount == 0
    over = calc.claim_duty(D(1500000))
    assert over.amount == calc.duty_scale(D(1500000)) - calc.duty_scale(D(1000000)) == D("5000.00")
    assert calc.claim_duty(D(200000), consumer=False).amount == D("7000.00")


def test_fine_half() -> None:
    assert calc.osago_fine(D("101.01")) == D("50.51")


@pytest.mark.parametrize(
    ("value", "words"),
    [
        ("128600", "сто двадцать восемь тысяч шестьсот рублей 00 копеек"),
        ("1021.05", "одна тысяча двадцать один рубль 05 копеек"),
        ("2000000", "два миллиона рублей 00 копеек"),
        ("0.5", "ноль рублей 50 копеек"),
        ("12", "двенадцать рублей 00 копеек"),
        ("3002001", "три миллиона две тысячи один рубль 00 копеек"),
    ],
)
def test_in_words(value: str, words: str) -> None:
    assert calc.in_words(D(value)) == words


def test_rub_format() -> None:
    assert calc.rub(D("1234567.8")) == "1 234 567,80 руб."
