"""toorow -- Unit tests for the deterministic daily spread (Story 22.1).

Offline / pure: no DB. Proves the cent-exact largest-remainder invariant, strict
Decimal usage, determinism, and edge cases (1 day, multi-month, leap year, long
range).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from core.mediaplan_store import MediaPlanValidationError, compute_spread


def _sum(spread: list[tuple[date, Decimal]]) -> Decimal:
    return sum((amt for _d, amt in spread), Decimal("0"))


def test_spread_sum_equals_budget_multi_month():
    # 10 000,00 EUR over 45 days (15 Mar -> 28 Apr, multi-month).
    spread = compute_spread(Decimal("10000.00"), date(2026, 3, 15), date(2026, 4, 28))
    assert len(spread) == 45
    assert _sum(spread) == Decimal("10000.00")
    assert spread[0][0] == date(2026, 3, 15)
    assert spread[-1][0] == date(2026, 4, 28)


def test_spread_100_over_3_days_largest_remainder():
    # 100,00 / 3 -> 33,34 + 33,33 + 33,33 (extra cent to the FIRST day).
    spread = compute_spread(Decimal("100.00"), date(2026, 1, 1), date(2026, 1, 3))
    amounts = [amt for _d, amt in spread]
    assert amounts == [Decimal("33.34"), Decimal("33.33"), Decimal("33.33")]
    assert _sum(spread) == Decimal("100.00")


def test_spread_one_cent_over_3_days():
    # 0,01 / 3 -> 0,01 + 0,00 + 0,00 (single remainder cent to the first day).
    spread = compute_spread(Decimal("0.01"), date(2026, 1, 1), date(2026, 1, 3))
    amounts = [amt for _d, amt in spread]
    assert amounts == [Decimal("0.01"), Decimal("0.00"), Decimal("0.00")]
    assert _sum(spread) == Decimal("0.01")


def test_spread_single_day():
    spread = compute_spread(Decimal("250.55"), date(2026, 6, 10), date(2026, 6, 10))
    assert spread == [(date(2026, 6, 10), Decimal("250.55"))]
    assert _sum(spread) == Decimal("250.55")


def test_spread_leap_day_2028():
    # Range spanning 29 Feb 2028 (leap year).
    spread = compute_spread(Decimal("1000.00"), date(2028, 2, 27), date(2028, 3, 2))
    days = [d for d, _amt in spread]
    assert date(2028, 2, 29) in days
    assert len(spread) == 5  # 27,28,29 Feb + 1,2 Mar
    assert _sum(spread) == Decimal("1000.00")


def test_spread_long_range_400_days():
    spread = compute_spread(Decimal("123456.78"), date(2026, 1, 1), date(2027, 2, 4))
    assert len(spread) == 400
    assert _sum(spread) == Decimal("123456.78")


def test_spread_zero_budget():
    spread = compute_spread(Decimal("0.00"), date(2026, 1, 1), date(2026, 1, 3))
    assert [amt for _d, amt in spread] == [Decimal("0.00")] * 3
    assert _sum(spread) == Decimal("0.00")


def test_spread_deterministic_byte_identical():
    a = compute_spread(Decimal("9999.99"), date(2026, 3, 1), date(2026, 5, 31))
    b = compute_spread(Decimal("9999.99"), date(2026, 3, 1), date(2026, 5, 31))
    assert a == b
    # And the string rendering is identical (byte-identical materialisation).
    assert [(d.isoformat(), format(amt, "f")) for d, amt in a] == [
        (d.isoformat(), format(amt, "f")) for d, amt in b
    ]


def test_spread_uses_decimal_not_float():
    spread = compute_spread(Decimal("100.00"), date(2026, 1, 1), date(2026, 1, 3))
    for _d, amt in spread:
        assert isinstance(amt, Decimal)


def test_spread_rejects_float_budget():
    with pytest.raises(MediaPlanValidationError):
        compute_spread(100.0, date(2026, 1, 1), date(2026, 1, 3))  # type: ignore[arg-type]


def test_spread_rejects_inverted_dates():
    with pytest.raises(MediaPlanValidationError):
        compute_spread(Decimal("100.00"), date(2026, 1, 3), date(2026, 1, 1))


def test_spread_rejects_negative_budget():
    with pytest.raises(MediaPlanValidationError):
        compute_spread(Decimal("-1.00"), date(2026, 1, 1), date(2026, 1, 3))


def test_spread_rejects_non_finite_budget():
    # Review F-3: NaN/Infinity compare False to 0 and must fail as 422, not 500.
    for bad in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")):
        with pytest.raises(MediaPlanValidationError):
            compute_spread(bad, date(2026, 1, 1), date(2026, 1, 3))


def test_parse_budget_rejects_non_finite():
    from core.mediaplan_store import _parse_budget

    for bad in ("NaN", "Infinity", "-Infinity"):
        with pytest.raises(MediaPlanValidationError):
            _parse_budget(bad)
