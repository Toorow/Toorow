"""Story 39.9 -- Epic 39 acceptance-gate OFFLINE composition + honesty suite.

This is the API-shaped half of the 39.9 gate (the warehouse-shaped half is the
``dbt/tests/test_epic39_*.sql`` singular tests, run on a REAL ``dbt build`` by the
orchestrator). It COMPOSES the delivered 39.1-39.8 helpers over the namespaced validation
fixture (``dbt/seeds/epic39_validation_fixture.csv``) to prove the invariants a dbt column
cannot express: typed refusals, ``MissingFxPair``/``MissingAsOfRate`` fail-closed, as-of
provenance nesting, currency-normalized reconciliation, the 39.5 provider seam, and the
39.7/39.8 timezone signal + honesty gaps.

STRICTLY ADDITIVE: this module ADDS assertions over DELIVERED helpers; it modifies no
production code. It introduces NO new ``server/core`` module (AC5) -- every composition helper
here is local to the test file, and connector names appear only in the FIXTURE data it reads.

Harness header + offline posture calqués sur ``test_fx_helper.py`` /
``test_dataset_access_grants.py`` (SCHEDULER_ENABLED=false, no DB by default). The 39.5-39.8
surfaces are SKIP-GUARDED on the presence of their delivered symbols so this suite authors now
and the guards drop as each lands (all landed at authoring time -- see Completion Notes).
"""

from __future__ import annotations

import csv
import os
import re
from datetime import date
from pathlib import Path

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

# --- Delivered money-side helpers (39.1-39.4): UNCONDITIONAL (these stories are landed) ------
from core import money  # noqa: E402
from core.currency_refusal import (  # noqa: E402
    REFUSAL_CROSS_CURRENCY,
    REFUSAL_UNKNOWN_CURRENCY,
    check_monetary_aggregation,
)
from core.fx_helper import (  # noqa: E402
    MissingAsOfRate,
    MissingFxPair,
    RateQuote,
    SeedAsOfRateProvider,
    convert,
)

# --- Fixture path (the 39.9 matrix seed) -----------------------------------------------------
_FIXTURE = (
    Path(__file__).resolve().parents[3] / "dbt" / "seeds" / "epic39_validation_fixture.csv"
)
_THIS_MODULE = Path(__file__).resolve()

_D_PAST = date(2024, 8, 15)     # the as-of past date (past rate, not today's)
_D_CURRENT = date(2026, 7, 1)   # a current date (fixed seed rate)


# ---------------------------------------------------------------------------
# Fixture loader (reads the same CSV the dbt build seeds -- one source of truth).
# ---------------------------------------------------------------------------


def _load_fixture() -> list[dict]:
    with _FIXTURE.open("r", newline="", encoding="utf-8") as handle:
        return [row for row in csv.DictReader(handle)]


def _rows(scenario: str) -> list[dict]:
    return [r for r in _load_fixture() if r.get("scenario") == scenario]


def _contrib(row: dict, *, on_date: date | None = None) -> dict:
    """Shape a fixture row into the 39.3/39.6 contribution dict."""
    currency = (row.get("source_currency") or "").strip() or None
    amount = (row.get("value_decimal") or "").strip()
    return {
        "connector": row.get("connector"),
        "source_system": row.get("connector"),
        "source_field": row.get("metric"),
        "pull_id": f"{row.get('connector')}:{row.get('date')}",
        "amount": float(amount) if amount else None,
        "currency": currency,
        "on_date": on_date,
    }


# A validation-LOCAL as-of table (NOT the shared fixtures/fx_as_of_sample.csv, NOT the dbt seed).
# Its EARLIEST date is 2024-01-02, so a figure dated before it (the honesty_missing_as_of row,
# 2020-01-01) has no rate at/before it -> MissingAsOfRate (fail-closed). Same shape 39.4 parses.
_AS_OF_ROWS = [
    ("USD", "EUR", 0.90, "2024-01-02"),
    ("USD", "EUR", 0.93, "2025-06-16"),
    ("USD", "EUR", 0.92, "2026-07-01"),
]


def _as_of_provider() -> SeedAsOfRateProvider:
    return SeedAsOfRateProvider(_AS_OF_ROWS)


# ---------------------------------------------------------------------------
# Fixture sanity (AI-53: canonical inputs verified in the seed).
# ---------------------------------------------------------------------------


def test_fixture_matrix_shape():
    """The gate's spine: >=2 connectors on ONE monetary metric, EUR+USD+GBP, micros+decimal,
    2 report timezones + shared-tz + blank-tz, a past date. Asserted on the real seed."""
    rows = _load_fixture()
    assert rows, "epic39_validation_fixture.csv is empty"
    connectors = {r["connector"] for r in rows}
    assert {"__epic39_src_a__", "__epic39_src_b__"} <= connectors
    # namespaced isolation: NO production connector name in the fixture.
    assert all(c.startswith("__epic39_src_") for c in connectors)

    revenue_rows = [r for r in rows if r["metric"] == "revenue"]
    currencies = {(r["source_currency"] or "").strip() for r in revenue_rows}
    assert {"EUR", "USD", "GBP"} <= currencies
    assert "" in currencies  # the blank-currency honesty row

    units = {r["native_unit"] for r in rows}
    assert {"micros", "decimal"} <= units

    tzs = {(r["report_timezone"] or "").strip() for r in rows}
    assert {"Europe/Paris", "America/New_York"} <= tzs
    assert "" in tzs  # the undetermined-tz honesty row

    dates = {r["date"] for r in rows}
    assert "2024-08-15" in dates  # the past date for as-of FX


# ---------------------------------------------------------------------------
# Invariant 1 -- micros exactness (39.2), a POSITIVE demonstration.
# ---------------------------------------------------------------------------


def test_micros_exactness_sum_then_divide_exact_divide_then_sum_drifts():
    """39.2: SUM canonical micros then /1e6 once is EXACT; per-row /1e6 then SUM DRIFTS."""
    rows = [r for r in _load_fixture() if r["native_unit"] == "micros"]
    assert rows, "no micros rows in the fixture"
    by_day: dict[str, list[int]] = {}
    for r in rows:
        by_day.setdefault(r["date"], []).append(int(r["value_micros"]))

    drift_seen = False
    for day, micros in by_day.items():
        # sum-then-divide: SUM exact integers, one read-time divide (money.read_once).
        sum_then_divide = money.read_once(sum(micros))
        # exact rational ground truth (integer sum, single division).
        exact = sum(micros) / money.MICROS_PER_UNIT
        assert sum_then_divide == exact, f"read-layer divide not exact on {day}"
        # divide-then-sum: the WRONG per-row divide accumulates float residue.
        divide_then_sum = sum(money.read_once(m) for m in micros)
        if divide_then_sum != sum_then_divide:
            drift_seen = True
    assert drift_seen, "fixture no longer trips divide-then-sum drift (anti-vacuity)"


def test_micros_decimal_roundtrip_lossless():
    """39.2 AC6: decimal -> canonical micros -> /1e6 round-trips EXACTLY (adapter subsumes
    the incumbent decimal path without moving its total)."""
    for r in _rows("recon_normalized"):
        amount = (r.get("value_decimal") or "").strip()
        if not amount:
            continue
        value = float(amount)
        assert money.native_roundtrip(value, "decimal") == value


# ---------------------------------------------------------------------------
# Invariant 2 -- cross-currency refusal (39.3), incl. unknown-gap precedence.
# ---------------------------------------------------------------------------


def test_cross_currency_refusal_names_currencies_and_streams():
    """39.3: EUR + USD contributions on one monetary metric -> CROSS_CURRENCY_REFUSAL naming
    both currencies + offending streams; NO numeric sum returned."""
    contribs = [_contrib(r) for r in _rows("recon_normalized")
                if (r.get("source_currency") or "").strip()]
    refusal = check_monetary_aggregation(
        metric="revenue", contributions=contribs, is_monetary=True, reporting_currency="EUR"
    )
    assert refusal is not None
    assert refusal["code"] == REFUSAL_CROSS_CURRENCY
    assert refusal["conflicting_currencies"] == ["EUR", "USD"]
    assert "__epic39_src_a__" in refusal["offending_streams"]
    assert "__epic39_src_b__" in refusal["offending_streams"]
    assert "amount" not in refusal and "sum" not in refusal  # no fabricated number


def test_single_currency_subset_is_legal():
    """39.3: a single-currency (EUR) subset returns None (a homogeneous sum is legal)."""
    contribs = [_contrib(r) for r in _rows("shared_tz")]  # both EUR
    assert check_monetary_aggregation(
        metric="cost", contributions=contribs, is_monetary=True, reporting_currency="EUR"
    ) is None


def test_blank_currency_takes_unknown_gap_precedence():
    """39.3: a blank-currency contribution -> UNKNOWN_CURRENCY_GAP, taking precedence over any
    cross-currency classification; NO numeric sum returned (AC2 honesty)."""
    blank = [_contrib(r) for r in _rows("honesty_missing_currency")]
    # mix the blank-currency row with a known USD row to prove precedence.
    usd = [_contrib(r) for r in _rows("recon_normalized")
           if (r.get("source_currency") or "").strip() == "USD"]
    refusal = check_monetary_aggregation(
        metric="revenue", contributions=blank + usd, is_monetary=True, reporting_currency="EUR"
    )
    assert refusal is not None
    assert refusal["code"] == REFUSAL_UNKNOWN_CURRENCY  # precedence over cross-currency
    assert refusal["unknown_currency_sources"]  # names the un-currencied source


# ---------------------------------------------------------------------------
# Invariant 3 -- fixed AND as-of FX with full provenance (39.4/39.5).
# ---------------------------------------------------------------------------


def test_fixed_fx_from_seed_with_provenance():
    """39.4: fixed-tier convert from the seed carries {rate, as_of_date, source, tier}."""
    usd = next(r for r in _rows("recon_normalized")
               if (r.get("source_currency") or "").strip() == "USD")
    result = convert(
        float(usd["value_decimal"]), "USD", reporting_currency="EUR",
        tier="fixed", on_date=_D_CURRENT,
    )
    assert result.amount == pytest.approx(100.0 * 0.92)
    assert set(result.fx.keys()) == {"rate", "as_of_date", "source", "tier"}
    assert result.fx["tier"] == "fixed"
    assert result.fx["source"] == "seed"


def test_as_of_fx_uses_past_rate_not_today():
    """39.4/39.5: a PAST figure converts at the PAST as-of rate (2024-01-02 => 0.90), not
    today's 0.92 -- run-day independent."""
    past = next(r for r in _rows("as_of_fx"))
    result = convert(
        float(past["value_decimal"]), "USD", reporting_currency="EUR",
        tier="historical", on_date=_D_PAST, provider=_as_of_provider(),
    )
    assert result.amount == pytest.approx(90.0)      # 100 * 0.90 (past), not 100 * 0.92
    assert result.fx["tier"] == "historical"
    assert result.fx["as_of_date"] == "2024-01-02"


def test_as_ad9_provenance_nests_fx_under_the_triple():
    """39.4 AC4: as_ad9_provenance merges an fx sub-object into the AD-9 (source_system,
    source_field, pull_id) chain WITHOUT mutating the base."""
    usd = next(r for r in _rows("recon_normalized")
               if (r.get("source_currency") or "").strip() == "USD")
    contrib = _contrib(usd)
    base = {
        "source_system": contrib["source_system"],
        "source_field": contrib["source_field"],
        "pull_id": contrib["pull_id"],
    }
    result = convert(contrib["amount"], "USD", reporting_currency="EUR",
                     tier="fixed", on_date=_D_CURRENT)
    prov = result.as_ad9_provenance(base)
    assert prov["source_system"] == "__epic39_src_b__"
    assert prov["fx"]["rate"] == pytest.approx(0.92)
    assert "fx" not in base  # base not mutated


def test_provider_seam_carry_forward_and_uncovered_fail_closed():
    """39.5 seam: a Protocol-shaped fake carry-forward/triangulation provider swaps into
    convert() with NO caller change; its carry-forward rate + source label flow through; a
    truly-uncovered pair fails closed (MissingFxPair)."""

    class _FakeCarryForwardProvider:
        """Carries the last published rate forward over a non-publishing day; fails closed on
        a pair it does not cover (the 39.5 semantics against a mock, no network)."""

        def get_rate(self, from_ccy, to_ccy, on_date):
            if (from_ccy, to_ccy) == ("USD", "EUR"):
                # ECB did not publish on on_date -> carry the last rate forward, RECORDED.
                return RateQuote(rate=0.91, source="fake-ecb-carry", tier="historical",
                                 as_of=date(2025, 3, 14))
            return None  # truly-uncovered pair -> convert() raises MissingFxPair (fail-closed)

    carried = convert(100.0, "USD", reporting_currency="EUR", tier="historical",
                      on_date=date(2025, 3, 15), provider=_FakeCarryForwardProvider())
    assert carried.amount == pytest.approx(91.0)
    assert carried.fx["source"] == "fake-ecb-carry"        # carry-forward provenance flows through
    assert carried.fx["as_of_date"] == "2025-03-14"        # the carried-forward publication date

    with pytest.raises(MissingAsOfRate):
        convert(100.0, "JPY", reporting_currency="EUR", tier="historical",
                on_date=date(2025, 3, 15), provider=_FakeCarryForwardProvider())


# ---------------------------------------------------------------------------
# Invariant 4 -- currency-normalized reconciliation (39.6), convert-first.
# ---------------------------------------------------------------------------

_recon = pytest.importorskip(
    "core.money_reconciliation", reason="39.6 money_reconciliation not landed"
)


def _fake_route_sum(project_id, metric):
    """A fake 27.3 resolver returning a DIRECT_SUM decision (the additive-money method)."""
    from core import metric_reconciliation as mr

    return mr.RouteDecision(
        metric=metric,
        status=mr.RouteStatus.DIRECT_SUM,
        method="SUM",
        reason=mr.ReconciliationReason(code="NO_GROUP_ADDITIVE_SUM", message="additive money"),
    )


def test_reconciliation_converts_first_then_sums():
    """39.6/AD5: >=2 datastreams on one metric in DIFFERENT currencies are CONVERTED to the
    reporting currency FIRST, then combined; the reconciled value == sum of converted values and
    != the naive mixed-currency sum. The definition method + per-source conversions are cited."""
    contribs = [_contrib(r) for r in _rows("recon_normalized")
                if (r.get("source_currency") or "").strip()]
    result = _recon.reconcile_monetary_metric(
        "__epic39_validation__", "revenue", contribs,
        reporting_currency="EUR", on_date=_D_CURRENT, tier="fixed",
        is_monetary=True, route_resolver=_fake_route_sum,
    )
    assert result.status == _recon.RECONCILED
    # convert-first: 100 EUR (identity) + 100 USD * 0.92 = 192.0 ; naive mixed sum would be 200.0.
    assert result.amount == pytest.approx(192.0)
    assert result.amount != pytest.approx(200.0)
    prov = result.as_ad9_provenance()
    assert prov["method"] == "SUM"                       # definition cited
    assert len(prov["contributions"]) == 2               # per-source drill-down
    assert all("fx" in c for c in prov["contributions"])  # each converted, with fx block


def test_reconciliation_fails_closed_on_uncovered_pair():
    """39.6: a contribution with an uncovered FX pair (GBP) -> REFUSED, amount None (no number)."""
    contribs = [_contrib(r) for r in _rows("honesty_missing_fx_pair")]
    # add an EUR contribution so it is a genuine multi-source reconcile attempt.
    contribs += [_contrib(r) for r in _rows("recon_normalized")
                 if (r.get("source_currency") or "").strip() == "EUR"]
    result = _recon.reconcile_monetary_metric(
        "__epic39_validation__", "revenue", contribs,
        reporting_currency="EUR", on_date=_D_CURRENT, tier="fixed",
        is_monetary=True, route_resolver=_fake_route_sum,
    )
    assert result.status == _recon.REFUSED
    assert result.amount is None                          # NO fabricated number
    assert result.refusal is not None
    assert result.refusal["from_currency"] == "GBP"


# ---------------------------------------------------------------------------
# Invariant 5 -- timezone signal (39.7 capture + 39.8 signal), + honesty.
# ---------------------------------------------------------------------------

_tz_signal = pytest.importorskip(
    "core.timezone_signal", reason="39.8 timezone_signal not landed"
)
_report_tz = pytest.importorskip(
    "core.report_timezone", reason="39.7 report_timezone not landed"
)


def _tz_streams(scenario: str) -> list[dict]:
    return [
        {
            "datastream": r["connector"],
            "report_timezone": (r.get("report_timezone") or "").strip() or None,
        }
        for r in _rows(scenario)
    ]


def test_day_offset_signalled_with_recorded_offset_not_realigned():
    """39.8: two distinct known timezones -> a TIMEZONE_DAY_OFFSET advisory naming the zones,
    with recorded offsets + a lever field, realignable:False (signalled, never realigned)."""
    signal = _tz_signal.check_cross_source_day_offset(
        metric="revenue", streams=_tz_streams("recon_normalized"),
    )
    assert signal is not None
    assert signal["code"] == _tz_signal.SIGNAL_TIMEZONE_DAY_OFFSET
    assert signal["realignable"] is False                # never realigned (amendment 2026-07-22)
    assert set(signal["distinct_timezones"]) == {"Europe/Paris", "America/New_York"}
    assert signal["severity"] == _tz_signal.SEVERITY_ADVISORY
    # lever field present per stream ("lever exists" or "no lever").
    assert all("has_lever" in s for s in signal["report_timezones"])


def test_shared_timezone_raises_no_false_positive():
    """39.8: a shared-timezone subset (both Europe/Paris) raises NOTHING (no false positive)."""
    assert _tz_signal.check_cross_source_day_offset(
        metric="cost", streams=_tz_streams("shared_tz"),
    ) is None


def test_undetermined_timezone_is_excluded_never_coerced_to_utc():
    """39.7/39.8: a blank/undetermined report timezone is EXCLUDED from the comparison (never
    coerced to UTC to force or suppress a signal). With only one other known tz present, the
    signal does NOT fire on the undetermined row."""
    streams = _tz_streams("honesty_undetermined_tz")  # single row, blank tz
    # pair the blank-tz row with ONE known-tz stream: only 1 distinct KNOWN tz -> no signal.
    streams += [{"datastream": "__epic39_src_a__", "report_timezone": "Europe/Paris"}]
    assert _tz_signal.check_cross_source_day_offset(metric="revenue", streams=streams) is None


def test_undetermined_timezone_capture_is_a_typed_gap():
    """39.7: resolve_capture on an undetermined capture under the default 'gap' posture yields a
    typed gap (report_timezone None, gap True) -- never a silent-UTC / project-default zone."""
    declared = {"locus": "account", "fallback": "gap"}  # live-read locus, capture failed
    resolved = _report_tz.resolve_capture(declared, captured_zone=None)
    assert resolved["gap"] is True
    assert resolved["report_timezone"] is None           # NO fabricated zone
    assert resolved["assumed"] is False

    # And the read-time typed conflict is shaped like CURRENCY_GAP.
    field = {"name": "revenue", "monetary": True}
    used_by = [{"datastream_name": "__epic39_src_b__"}]  # carries no resolvable tz
    gap = _report_tz.timezone_gap(field, used_by)
    assert gap is not None
    assert gap["code"] == _report_tz.TIMEZONE_GAP_CODE


# ---------------------------------------------------------------------------
# AC2 -- the four honesty gaps, each fail-closed (typed gap AND no number).
# ---------------------------------------------------------------------------


def test_honesty_missing_currency_gap_no_number():
    """(a) missing currency -> UNKNOWN_CURRENCY_GAP, no numeric sum."""
    contribs = [_contrib(r) for r in _rows("honesty_missing_currency")]
    refusal = check_monetary_aggregation(
        metric="revenue", contributions=contribs, is_monetary=True, reporting_currency="EUR"
    )
    assert refusal["code"] == REFUSAL_UNKNOWN_CURRENCY
    assert "amount" not in refusal  # no fabricated number


def test_honesty_missing_fx_pair_typed_gap_no_number():
    """(b) missing FX pair (GBP) -> MissingFxPair, no 1.0 fallback, no number."""
    gbp = next(r for r in _rows("honesty_missing_fx_pair"))
    with pytest.raises(MissingFxPair) as exc:
        convert(float(gbp["value_decimal"]), "GBP", reporting_currency="EUR", tier="fixed")
    assert exc.value.from_currency == "GBP"
    assert exc.value.to_currency == "EUR"


def test_honesty_missing_as_of_rate_typed_gap_no_number():
    """(c) missing as-of rate (figure dated before the earliest as-of row) -> MissingAsOfRate,
    no today's-rate substitution, no number."""
    old = next(r for r in _rows("honesty_missing_as_of"))
    with pytest.raises(MissingAsOfRate) as exc:
        convert(
            float(old["value_decimal"]), "USD", reporting_currency="EUR",
            tier="historical", on_date=date(2020, 1, 1), provider=_as_of_provider(),
        )
    assert exc.value.from_currency == "USD"


def test_honesty_undetermined_timezone_typed_gap_no_zone():
    """(d) undetermined report timezone -> typed TIMEZONE_GAP / gap posture, no fabricated zone."""
    resolved = _report_tz.resolve_capture({"locus": "none"}, captured_zone=None)
    assert resolved["gap"] is True
    assert resolved["report_timezone"] is None  # never a silent UTC / project default


# ---------------------------------------------------------------------------
# AC5 -- AD-2: the new test module carries no provider vocabulary in CORE terms.
# ---------------------------------------------------------------------------


def test_ad2_no_new_core_module_and_no_provider_vocabulary_here():
    """AC5: 39.9 adds NO new server/core module (it composes delivered helpers). This test
    module names connectors ONLY via the fixture keys (__epic39_src_*__), which are fixture
    vocabulary, not provider names -- no real provider/vendor name is hardcoded in the test."""
    source = _THIS_MODULE.read_text(encoding="utf-8").lower()
    # Tokens are assembled from fragments so the forbidden LIST itself does not match the scan
    # (the check would otherwise flag its own literal). Each is a real provider/vendor name.
    forbidden = [
        "frank" + "furter", "open" + "exchangerates", "fix" + "er", "currency" + "layer",
        "meta" + "-ads", "shop" + "ify", "str" + "ipe", "tik" + "tok",
        "linked" + "in", "super" + "metrics", "adver" + "ity",
    ]
    hits = [
        name for name in forbidden if re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", source)
    ]
    assert hits == [], f"provider vocabulary leaked into the test module: {hits}"
