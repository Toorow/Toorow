"""Tests for Story 39.2 -- the per-source money adapter + canonical-micros read helper.

Offline (no DB, no dbt): the adapter is pure arithmetic, so every AC is provable directly:
  * to_canonical_micros: decimal x1e6, cents x1e4, micros no-op; unknown unit -> fail-CLOSED
  * read_units: divides a canonical-micros metric ONCE; unknown metric -> fail-SOFT (no divide)
  * native_roundtrip: the DECIMAL round-trip is exact (AC6 -- adapter subsumes the incumbent
    decimal path losslessly)
  * exactness demonstration (AC5): sum-then-divide is exact vs divide-then-sum drifts, on a
    fixture chosen to expose float drift (values near 2^53, non-round micros)
  * load_units: parses money_metric_units.csv -> exact canonical-metric set + units
  * seed well-formedness: every unit in {micros,decimal,cents}; every declared metric is in
    39.1's monetary:true classification (via _classify_monetary, always available offline)
  * AD-2 grep: money.py names no connector/provider

Pattern: env header + no DB, mirrors test_metric_semantics.py / test_dataset_access_grants.py.
"""

from __future__ import annotations

import os
from fractions import Fraction
from pathlib import Path

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core import money  # noqa: E402

_SEEDS_DIR = Path(money.__file__).resolve().parents[2] / "dbt" / "seeds"


# ---------------------------------------------------------------------------
# (a) to_canonical_micros -- the adapter normalization (INPUT -> canonical micros).
# ---------------------------------------------------------------------------


def test_to_canonical_micros_decimal_scales_by_1e6():
    assert money.to_canonical_micros(1.0, "decimal") == 1_000_000
    assert money.to_canonical_micros(3000000.3, "decimal") == 3_000_000_300_000
    assert money.to_canonical_micros(0.01, "decimal") == 10_000  # a cent expressed decimal


def test_to_canonical_micros_cents_scales_by_1e4():
    assert money.to_canonical_micros(1.0, "cents") == 10_000  # 1 cent = 10_000 micros
    assert money.to_canonical_micros(100.0, "cents") == 1_000_000  # 100 cents = 1 unit
    assert money.to_canonical_micros(12345.0, "cents") == 123_450_000


def test_to_canonical_micros_micros_is_noop():
    # micros IS canonical -- the adapter normalization is a no-op scale of 1.
    assert money.to_canonical_micros(9_999_999_000_000, "micros") == 9_999_999_000_000
    assert money.to_canonical_micros(0, "micros") == 0


def test_to_canonical_micros_unknown_unit_fails_closed():
    # fail-CLOSED: never guess a scale for an undeclared native encoding.
    with pytest.raises(money.MoneyAdapterError):
        money.to_canonical_micros(1.0, "yen")
    with pytest.raises(money.MoneyAdapterError):
        money.to_canonical_micros(1.0, "")


# ---------------------------------------------------------------------------
# (b) read_units -- the read-time division (canonical micros -> display, ONCE).
# ---------------------------------------------------------------------------


def test_read_units_divides_only_canonical_micros_once():
    units = {"ad_revenue": "micros", "revenue": "decimal"}
    # ad_revenue declared micros => the mart holds canonical micros => /1e6 once.
    assert money.read_units(9_999_999_000_000, "ad_revenue", units) == 9_999_999.0
    # revenue declared decimal => the mart already holds display units (AD-6, adapter not yet
    # adopted) => do NOT divide. Dividing it would fabricate a wrong number -- the 39.4 footgun
    # this =='micros' gate closes (read_units now matches the SQL is_canonical_micros branch).
    assert money.read_units(1_000_000, "revenue", units) == 1_000_000


def test_read_units_unknown_metric_fails_soft_not_divided():
    # (c) fail-SOFT: an unknown metric is treated as already-display -- NOT divided. Dividing a
    # non-canonical (e.g. a count) value would fabricate a wrong number.
    units = {"ad_revenue": "micros"}
    assert money.read_units(42_000, "impressions", units) == 42_000
    assert money.read_units(42_000, "impressions", units) != 0.042  # would be the wrong /1e6


# ---------------------------------------------------------------------------
# (d) AC6 -- the DECIMAL round-trip is exact (adapter subsumes the incumbent decimal path).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "x",
    [
        0.0, 1.0, 12.34, 999.99, 0.01, 3000000.3, 1234567.89, 500001.0,
        42.42, 7.77, 100000.55, 0.99,
    ],
)
def test_native_roundtrip_decimal_is_exact(x):
    # normalize decimal -> canonical micros (x1e6) then divide back (/1e6) == the original,
    # EXACTLY. This is the E39-NFR06 guarantee at the unit level: the adapter moves no total.
    assert money.native_roundtrip(x, "decimal") == x


def test_native_roundtrip_cents_is_exact():
    for cents in (1.0, 50.0, 100.0, 12345.0):
        # cents -> micros (x1e4) -> display (/1e6) == cents/100 exactly
        assert money.native_roundtrip(cents, "cents") == cents / 100.0


# ---------------------------------------------------------------------------
# (f) AC5 -- exactness DEMONSTRATED: sum-then-divide exact vs divide-then-sum drifts.
# ---------------------------------------------------------------------------


def test_sum_then_divide_exact_vs_divide_then_sum_drifts():
    # A fixture chosen to EXPOSE float drift: many rows whose per-row /1e6 is a repeating
    # binary fraction (X.1 -- 0.1 is inexact in binary) accumulated onto a large magnitude, so
    # divide-then-sum drops low bits on every add. Sum-then-divide keeps the total exact-in-int
    # (an integer count of micros) and divides ONCE -- no per-row residue can accumulate.
    row = 90_000_000_000_000 + 100_000  # /1e6 = 90_000_000.1 (repeating-binary fraction)
    micros_rows = [row] * 200_000

    # Ground truth: exact rational sum-then-divide.
    exact = float(Fraction(sum(micros_rows), money.MICROS_PER_UNIT))

    # Sum-then-divide (the contract): sum integers exactly, divide the total ONCE.
    sum_then_divide = money.read_once(sum(micros_rows))

    # Divide-then-sum (the anti-pattern the contract forbids): /1e6 per row, then accumulate.
    divide_then_sum = 0.0
    for v in micros_rows:
        divide_then_sum += money.read_once(v)

    # Sum-then-divide is EXACT (matches the rational to the bit).
    assert sum_then_divide == exact
    # Divide-then-sum DRIFTS off the exact value (positive demonstration, not just "no rows").
    assert divide_then_sum != sum_then_divide
    assert abs(divide_then_sum - exact) > abs(sum_then_divide - exact)
    assert abs(divide_then_sum - exact) > 1.0  # the drift is material, not a last-bit wobble


# ---------------------------------------------------------------------------
# (e) load_units -- parses the seed: exact canonical-metric set + units.
# ---------------------------------------------------------------------------


def test_load_units_parses_seed():
    units = money.load_units(_SEEDS_DIR)
    # TODAY every WIRED money metric arrives DECIMAL at the mart (adjust emits ad_revenue/
    # all_revenue decimal; GAM's native=micros field is not mart-wired). No metric is 'micros'
    # in the seed until a connector adopts the adapter and lands canonical micros -- see the
    # per-source collision note in money_metric_units.csv.
    for m in (
        "ad_revenue", "all_revenue", "revenue", "cost", "refunds", "refund_amount", "fees",
        "conversions_value", "deal_amount", "attributed_revenue", "target_revenue",
        "budget_declared",
    ):
        assert units[m] == "decimal", m
    assert "micros" not in set(units.values())  # no wired micros emitter yet
    # non-money metrics are ABSENT (only monetary metrics are listed).
    for non_money in ("sessions", "impressions", "clicks", "conversions", "roas", "ctr"):
        assert non_money not in units, non_money


# Story 39.1's pure classifier under-covers three genuinely-monetary metric names that this
# seed MUST carry (they are REAL money metrics emitted into fact_daily_kpi / declared in
# AC-53): 'refunds' + 'fees' (Stripe/Square dedicated positive money rows, stripe/square mart
# blocks) end in a plural the _MONETARY_TOKEN_RE families don't anchor, and 'budget_declared'
# (google-sheets plan money, AC-53) ends in '_declared' with no money token. This is a 39.1
# classifier GAP (its token families would need '_refunds'/'_fees'/'budget' — NOT 39.2's to
# fix; metric_semantics.py is out of scope + a sibling's surface). Recorded as a deviation.
# The subset cross-check therefore holds for every OTHER declared metric and skip-tolerates
# these three known gaps.
_KNOWN_39_1_CLASSIFIER_GAPS = frozenset({"refunds", "fees", "budget_declared"})


def test_seed_units_are_well_formed_and_monetary():
    """Every declared unit is in {micros,decimal,cents}; every declared metric is monetary.

    The monetary cross-check uses _classify_monetary (39.1's pure classifier), always
    available offline (not skip-guarded). It asserts the seed's metric set is a subset of
    39.1's monetary:true classification (the governed set) EXCEPT for the three documented
    39.1-classifier gaps above, which are real money metrics 39.1 under-classifies.
    """
    from core.metric_semantics import _classify_monetary  # noqa: PLC0415

    units = money.load_units(_SEEDS_DIR)
    assert units, "seed parsed to an empty map"
    for metric, unit in units.items():
        assert unit in money.ACCEPTED_NATIVE_UNITS, f"{metric}: bad unit {unit!r}"
        if metric in _KNOWN_39_1_CLASSIFIER_GAPS:
            # Documented 39.1 gap: real money metric the classifier misses. Assert the gap is
            # still real so this tolerance auto-drops the day 39.1 widens its token families.
            assert not _classify_monetary(metric), (
                f"{metric} now classifies monetary -- drop it from _KNOWN_39_1_CLASSIFIER_GAPS"
            )
            continue
        assert _classify_monetary(metric), f"{metric} declared in money seed but not monetary"


# ---------------------------------------------------------------------------
# (g) AD-2 -- money.py names no connector/provider.
# ---------------------------------------------------------------------------


def test_no_provider_name_in_module():
    """AD-2: money.py contains no connector/provider name. Canonical metric names come from
    the seed (money_metric_units.csv is a dbt artefact, not a provider name)."""
    source = Path(money.__file__).read_text(encoding="utf-8").lower()
    forbidden = [
        "google-analytics", "google_analytics", "meta-ads", "meta_ads",
        "tiktok", "linkedin", "shopify", "stripe", "adjust", "hubspot",
        "facebook", "pinterest", "amazon", "microsoft", "snapchat", "klaviyo",
        "square", "woocommerce", "google-ad-manager", "google_ad_manager", "gam",
        "currencycode",
    ]
    hits = [name for name in forbidden if name in source]
    assert not hits, f"provider name(s) hard-coded in money.py: {hits}"
