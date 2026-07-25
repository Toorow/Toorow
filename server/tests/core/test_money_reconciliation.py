"""Story 39.6 -- offline tests for the cross-datastream money reconciliation orchestrator.

Every collaborator of ``reconcile_monetary_metric`` is injected with a fake (route_resolver /
converter / refusal_checker / is_monetary_resolver / reporting_currency_resolver) OR a REAL
seeded ``fx_helper.FixedRateProvider`` (a story-local seed dir for cross-currency pairs), so
NO DB and NO server is required. The E39-AD5 order, the fail-closed refusals, the E39-NFR06
no-drift proof, the AD-9 provenance, and the AD-2 source-agnosticism are all exercised here.

Central verification (ruff / pytest / dbt build) is the orchestrator's job (no-shell scope).
"""

from __future__ import annotations

from datetime import date

import pytest
from core import fx_helper, metric_reconciliation, money_reconciliation
from core.money_reconciliation import (
    ReconciledMoney,
    SourceContribution,
    reconcile_monetary_metric,
)

# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------


def _seeded_provider(tmp_path, rows):
    """Write a story-local fx_rates.csv and return a real FixedRateProvider over it.

    ``rows`` = iterable of (from_ccy, to_ccy, rate). Windows are open (2020..2099) so any
    date matches; ``rate_date`` is fixed. This drives REAL 39.4 conversion offline."""
    header = "from_currency,to_currency,rate,rate_date,rate_policy,valid_from,valid_to\n"
    lines = [header]
    for from_ccy, to_ccy, rate in rows:
        lines.append(f"{from_ccy},{to_ccy},{rate},2026-07-01,static,2020-01-01,2099-12-31\n")
    seeds_dir = tmp_path / "seeds"
    seeds_dir.mkdir()
    (seeds_dir / fx_helper.FX_RATES_SEED).write_text("".join(lines), encoding="utf-8")
    return fx_helper.FixedRateProvider(seeds_dir=seeds_dir)


def _route(status, **kw):
    return metric_reconciliation.RouteDecision(status=status, **kw)


def _fake_route(decision):
    """A route_resolver returning a fixed decision (ignores project/metric)."""
    return lambda project_id, metric: decision


def _monetary_true(metric, project_id):
    return True


def _spy_converter():
    """A converter that records every (amount, from_ccy, reporting_currency) call and returns
    an identity-shaped ConvertedAmount so the SUM can be asserted on the recorded values."""
    calls = []

    def _convert(
        amount, from_currency, *, reporting_currency, on_date=None, tier=None, provider=None
    ):
        calls.append(
            {
                "amount": amount,
                "from_currency": from_currency,
                "reporting_currency": reporting_currency,
            }
        )
        # Simulate a real conversion to the reporting currency (rate 2.0 for non-identity).
        rate = 1.0 if from_currency == reporting_currency else 2.0
        return fx_helper.ConvertedAmount(
            amount=amount * rate,
            reporting_currency=reporting_currency,
            source_amount=amount,
            source_currency=from_currency,
            fx={"rate": rate, "as_of_date": None, "source": "seed", "tier": "fixed"},
        )

    _convert.calls = calls
    return _convert


# ===========================================================================
# Offline -- the E39-AD5 composition (AC-1)
# ===========================================================================


def test_1_same_currency_sum_reconciles():
    """Two same-currency (EUR) revenue contributions, rule=SUM -> amount == a+b, RECONCILED."""
    route = _route(metric_reconciliation.RouteStatus.DIRECT_SUM, metric="revenue", method="SUM")
    result = reconcile_monetary_metric(
        "proj",
        "revenue",
        [
            {"connector": "a", "amount": 100.0, "currency": "EUR"},
            {"connector": "b", "amount": 40.0, "currency": "EUR"},
        ],
        reporting_currency="EUR",
        is_monetary=True,
        route_resolver=_fake_route(route),
    )
    assert result.status == money_reconciliation.RECONCILED
    assert result.amount == 140.0
    assert len(result.contributions) == 2
    for c in result.contributions:
        assert c.converted.fx["rate"] == 1.0  # identity block on same-currency


def test_2_non_monetary_bypasses_module():
    """A non-monetary metric returns the resolve_route decision VERBATIM (27.3 untouched)."""
    route = _route(metric_reconciliation.RouteStatus.DIRECT_SUM, metric="sessions", method="SUM")
    resolver = _fake_route(route)
    result = reconcile_monetary_metric(
        "proj",
        "sessions",
        [{"connector": "a", "amount": 5.0, "currency": "EUR"}],
        is_monetary=False,
        route_resolver=resolver,
    )
    assert result.status == money_reconciliation.PASSTHROUGH
    assert result.amount is None
    # The route is the SAME object a direct resolve_route(...) call yields (verbatim).
    assert result.route is route
    assert result.contributions == ()


def test_3_combination_sees_only_reporting_currency(tmp_path):
    """USD + GBP -> EUR: each converted to EUR BEFORE the SUM; the summed inputs are the
    EUR converted amounts, never the raw USD/GBP amounts (load-bearing AC-1 proof)."""
    spy = _spy_converter()
    route = _route(metric_reconciliation.RouteStatus.DIRECT_SUM, metric="revenue", method="SUM")
    result = reconcile_monetary_metric(
        "proj",
        "revenue",
        [
            {"connector": "a", "amount": 10.0, "currency": "USD"},
            {"connector": "b", "amount": 20.0, "currency": "GBP"},
        ],
        reporting_currency="EUR",
        is_monetary=True,
        route_resolver=_fake_route(route),
        converter=spy,
    )
    assert result.status == money_reconciliation.RECONCILED
    # Converter was called for EACH contribution, always with reporting_currency='EUR'.
    assert [c["reporting_currency"] for c in spy.calls] == ["EUR", "EUR"]
    assert {c["from_currency"] for c in spy.calls} == {"USD", "GBP"}
    # The combined figure is the sum of the CONVERTED (EUR) amounts (10*2 + 20*2), never
    # the raw 10 + 20 = 30.
    assert result.amount == 60.0
    assert result.amount != 30.0


# ===========================================================================
# Offline -- fail-closed refusals (AC-2, E39-NFR02)
# ===========================================================================


def test_4_unknown_currency_refused():
    """A contribution with unknown/missing currency -> REFUSED, amount None, UNKNOWN gap."""
    result = reconcile_monetary_metric(
        "proj",
        "revenue",
        [{"connector": "a", "amount": 10.0, "currency": None, "source_system": "s1"}],
        reporting_currency="EUR",
        is_monetary=True,
        route_resolver=_fake_route(_route("DIRECT_SUM", metric="revenue", method="SUM")),
    )
    assert result.status == money_reconciliation.REFUSED
    assert result.amount is None
    assert result.refusal["code"] == "UNKNOWN_CURRENCY_GAP"


def test_5_missing_fx_pair_refused(tmp_path):
    """A nameable currency with no seed rate (MissingFxPair) -> REFUSED, typed FX gap."""
    provider = _seeded_provider(tmp_path, [("USD", "EUR", 0.9)])  # no GBP->EUR pair
    route = _route(metric_reconciliation.RouteStatus.DIRECT_SUM, metric="revenue", method="SUM")
    result = reconcile_monetary_metric(
        "proj",
        "revenue",
        [
            {"connector": "a", "amount": 10.0, "currency": "USD"},
            {"connector": "b", "amount": 20.0, "currency": "GBP"},
        ],
        reporting_currency="EUR",
        is_monetary=True,
        provider=provider,
        route_resolver=_fake_route(route),
    )
    assert result.status == money_reconciliation.REFUSED
    assert result.amount is None
    assert result.refusal["code"] == "FX_CONVERSION_GAP"
    assert result.refusal["from_currency"] == "GBP"
    assert result.refusal["to_currency"] == "EUR"


def test_6_missing_asof_rate_refused():
    """Historical tier with no as-of rate -> REFUSED, amount None (MissingAsOfRate)."""
    provider = fx_helper.SeedAsOfRateProvider([])  # empty -> no rate at/before any date
    route = _route(metric_reconciliation.RouteStatus.DIRECT_SUM, metric="revenue", method="SUM")
    result = reconcile_monetary_metric(
        "proj",
        "revenue",
        [
            {"connector": "a", "amount": 10.0, "currency": "USD", "on_date": date(2026, 7, 5)},
            {"connector": "b", "amount": 20.0, "currency": "GBP", "on_date": date(2026, 7, 5)},
        ],
        reporting_currency="EUR",
        is_monetary=True,
        tier=fx_helper.TIER_HISTORICAL,
        provider=provider,
        route_resolver=_fake_route(route),
    )
    assert result.status == money_reconciliation.REFUSED
    assert result.amount is None
    assert result.refusal["kind"] == "MissingAsOfRate"


def test_7_known_cross_currency_is_converted_not_refused(tmp_path):
    """USD + GBP -> EUR with BOTH seed pairs present -> RECONCILED (NOT refused), sum on the
    converted EUR values, each contribution carries its distinct fx block."""
    provider = _seeded_provider(tmp_path, [("USD", "EUR", 0.9), ("GBP", "EUR", 1.1)])
    route = _route(metric_reconciliation.RouteStatus.DIRECT_SUM, metric="revenue", method="SUM")
    result = reconcile_monetary_metric(
        "proj",
        "revenue",
        [
            {"connector": "a", "amount": 100.0, "currency": "USD"},
            {"connector": "b", "amount": 100.0, "currency": "GBP"},
        ],
        reporting_currency="EUR",
        is_monetary=True,
        provider=provider,
        route_resolver=_fake_route(route),
    )
    assert result.status == money_reconciliation.RECONCILED
    assert result.amount == pytest.approx(100 * 0.9 + 100 * 1.1)
    rates = sorted(c.converted.fx["rate"] for c in result.contributions)
    assert rates == [0.9, 1.1]


# ===========================================================================
# Offline -- E39-NFR06 no-drift + exactness (AC-4, AC-5)
# ===========================================================================


def test_8_single_source_bit_identical():
    """One monetary contribution, from==reporting -> SINGLE_SOURCE, amount byte-identical."""
    contrib_amount = 123.456789
    result = reconcile_monetary_metric(
        "proj",
        "revenue",
        [{"connector": "a", "amount": contrib_amount, "currency": "EUR"}],
        reporting_currency="EUR",
        is_monetary=True,
        route_resolver=_fake_route(_route("DIRECT_SUM", metric="revenue", method="SUM")),
    )
    assert result.status == money_reconciliation.SINGLE_SOURCE
    # Bit-identical: identity FX echoes the source amount, no ``* 1.0`` re-round.
    assert result.amount is contrib_amount
    assert result.contributions[0].converted.fx["rate"] == 1.0


def test_9_sums_then_no_per_contribution_divide(tmp_path):
    """Micros-style amounts: 39.6 SUMS the (converted) values as-is and never divides per
    contribution -- the total equals the integer sum (single read-time divide is the caller's)."""
    provider = _seeded_provider(tmp_path, [("USD", "EUR", 1.0)])  # rate 1.0 -> exact micros
    route = _route(metric_reconciliation.RouteStatus.DIRECT_SUM, metric="revenue", method="SUM")
    micros = [1_000_001, 2_000_002, 3_000_003]
    result = reconcile_monetary_metric(
        "proj",
        "revenue",
        [{"connector": f"c{i}", "amount": m, "currency": "USD"} for i, m in enumerate(micros)],
        reporting_currency="EUR",
        is_monetary=True,
        provider=provider,
        route_resolver=_fake_route(route),
    )
    assert result.status == money_reconciliation.RECONCILED
    assert result.amount == sum(micros)  # integer-exact sum, no per-row divide


def test_10_priority_routes_to_mart_no_number(tmp_path):
    """Rule=PRIORITY routing to cross_source_revenue -> ROUTED_TO_MART, amount None,
    target_mart carried, converted per-source contributions carried as drill-down."""
    provider = _seeded_provider(tmp_path, [("USD", "EUR", 0.9)])
    route = _route(
        metric_reconciliation.RouteStatus.ROUTED_TO_MART,
        metric="revenue",
        method="PRIORITY",
        target_mart="cross_source_revenue",
        priority_order=("a", "b"),
    )
    result = reconcile_monetary_metric(
        "proj",
        "revenue",
        [
            {"connector": "a", "amount": 10.0, "currency": "USD"},
            {"connector": "b", "amount": 20.0, "currency": "EUR"},
        ],
        reporting_currency="EUR",
        is_monetary=True,
        provider=provider,
        route_resolver=_fake_route(route),
    )
    assert result.status == money_reconciliation.ROUTED_TO_MART
    assert result.amount is None
    assert result.route.target_mart == "cross_source_revenue"
    assert result.route.priority_order == ("a", "b")
    assert len(result.contributions) == 2  # drill-down present, core did NOT read the mart


# ===========================================================================
# Offline -- AD-9 provenance (AC-3)
# ===========================================================================


def test_11_ad9_provenance_no_mutation(tmp_path):
    """as_ad9_provenance returns metric/reporting_currency/method + one entry per source with
    its fx block, and does NOT mutate the input contribution dicts / ConvertedAmounts."""
    provider = _seeded_provider(tmp_path, [("USD", "EUR", 0.9)])
    route = _route(metric_reconciliation.RouteStatus.DIRECT_SUM, metric="revenue", method="SUM")
    source_dicts = [
        {"connector": "a", "amount": 10.0, "currency": "USD"},
        {"connector": "b", "amount": 20.0, "currency": "EUR"},
    ]
    before = [dict(d) for d in source_dicts]
    result = reconcile_monetary_metric(
        "proj",
        "revenue",
        source_dicts,
        reporting_currency="EUR",
        is_monetary=True,
        provider=provider,
        route_resolver=_fake_route(route),
    )
    prov = result.as_ad9_provenance()
    assert prov["metric"] == "revenue"
    assert prov["reporting_currency"] == "EUR"
    assert prov["method"] == "SUM"
    assert len(prov["contributions"]) == 2
    for entry in prov["contributions"]:
        assert set(entry) >= {"connector", "source_amount", "source_currency", "fx"}
        assert set(entry["fx"]) == {"rate", "as_of_date", "source", "tier"}
    # No mutation of the caller's inputs.
    assert source_dicts == before


def test_12_keep_separate_no_total():
    """KEEP_SEPARATE decision -> status KEEP_SEPARATE, amount None, per-source contributions
    carried, route.reason.code == 'KEEP_SEPARATE' (27.3 D-5 honoured)."""
    reason = metric_reconciliation.ReconciliationReason(
        code=metric_reconciliation.CODE_KEEP_SEPARATE,
        message="series per source",
        emitters=("a", "b"),
    )
    route = _route(
        metric_reconciliation.RouteStatus.KEEP_SEPARATE,
        metric="revenue",
        method="KEEP_SEPARATE",
        reason=reason,
    )
    result = reconcile_monetary_metric(
        "proj",
        "revenue",
        [
            {"connector": "a", "amount": 10.0, "currency": "EUR"},
            {"connector": "b", "amount": 20.0, "currency": "EUR"},
        ],
        reporting_currency="EUR",
        is_monetary=True,
        route_resolver=_fake_route(route),
    )
    assert result.status == money_reconciliation.KEEP_SEPARATE
    assert result.amount is None
    assert len(result.contributions) == 2
    assert result.route.reason.code == "KEEP_SEPARATE"


@pytest.mark.parametrize(
    "route_status, expected",
    [
        (metric_reconciliation.RouteStatus.UNRULED_OVERLAP, money_reconciliation.UNRULED_OVERLAP),
        (metric_reconciliation.RouteStatus.NOT_COMBINABLE, money_reconciliation.NOT_COMBINABLE),
        (
            metric_reconciliation.RouteStatus.OVERRIDE_NOT_MATERIALIZED,
            money_reconciliation.OVERRIDE_NOT_MATERIALIZED,
        ),
    ],
)
def test_13_no_combine_reasons_pass_through(route_status, expected):
    """UNRULED_OVERLAP / NOT_COMBINABLE / OVERRIDE_NOT_MATERIALIZED pass through with amount
    None and their 27.3 reason unchanged (honest no-combine)."""
    reason = metric_reconciliation.ReconciliationReason(
        code=route_status, message="m", emitters=("a", "b")
    )
    route = _route(route_status, metric="revenue", reason=reason)
    result = reconcile_monetary_metric(
        "proj",
        "revenue",
        [
            {"connector": "a", "amount": 10.0, "currency": "EUR"},
            {"connector": "b", "amount": 20.0, "currency": "EUR"},
        ],
        reporting_currency="EUR",
        is_monetary=True,
        route_resolver=_fake_route(route),
    )
    assert result.status == expected
    assert result.amount is None
    assert result.route.reason is reason


# ===========================================================================
# Offline -- source-agnostic / AD-2 (AC-6)
# ===========================================================================


def test_14_no_provider_names_in_module_or_route():
    """AD-2: the orchestrator module + the new route carry ZERO provider/connector names."""
    import pathlib

    core_dir = pathlib.Path(money_reconciliation.__file__).resolve().parent
    text = (core_dir / "money_reconciliation.py").read_text(encoding="utf-8").lower()
    route_text = (core_dir / "money_api.py").read_text(encoding="utf-8").lower()
    forbidden = (
        "meta",
        "google",
        "tiktok",
        "linkedin",
        "adwords",
        "gsc",
        "ga4",
        "shopify",
        "woocommerce",
        "snapchat",
        "pinterest",
        "amazon",
        "criteo",
    )
    for name in forbidden:
        assert name not in text, f"provider name '{name}' leaked into money_reconciliation.py"
    # money_api.py may legitimately mention none of these either (the appended route).
    for name in ("google", "tiktok", "linkedin", "shopify", "woocommerce"):
        assert name not in route_text, f"provider name '{name}' leaked into money_api.py route"


def test_15_reporting_currency_resolved_per_project():
    """Reporting currency is resolved per-project (fake resolver -> USD) and threaded into
    EVERY convert call, never a hardcoded EUR."""
    spy = _spy_converter()
    route = _route(metric_reconciliation.RouteStatus.DIRECT_SUM, metric="revenue", method="SUM")
    result = reconcile_monetary_metric(
        "proj",
        "revenue",
        [
            {"connector": "a", "amount": 10.0, "currency": "USD"},
            {"connector": "b", "amount": 20.0, "currency": "GBP"},
        ],
        # reporting_currency NOT passed -> resolved via the injected resolver.
        is_monetary=True,
        converter=spy,
        route_resolver=_fake_route(route),
        reporting_currency_resolver=lambda pid: "USD",
    )
    assert result.reporting_currency == "USD"
    assert all(c["reporting_currency"] == "USD" for c in spy.calls)


# ===========================================================================
# Dataclass sanity -- frozen / immutable
# ===========================================================================


def test_dataclasses_are_frozen():
    conv = fx_helper.convert(10.0, "EUR", reporting_currency="EUR")
    contrib = SourceContribution(
        connector="a", source_amount=10.0, source_currency="EUR", converted=conv
    )
    result = ReconciledMoney(
        metric="revenue", status="RECONCILED", amount=10.0, reporting_currency="EUR"
    )
    with pytest.raises(Exception):
        contrib.connector = "b"  # type: ignore[misc]
    with pytest.raises(Exception):
        result.amount = 99.0  # type: ignore[misc]
