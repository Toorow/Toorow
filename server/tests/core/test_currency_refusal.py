"""Offline pure-engine tests for the cross-currency refusal engine (Story 39.3, Epic 39).

No DB. Exercises core.currency_refusal.check_monetary_aggregation + _is_monetary_metric.

Invariants under test (ACs as tests):
  - Cross-currency sum is a TYPED refusal, never a silent sum (AC1) -- tests 5-7, 13, 16.
  - Unknown currency fails CLOSED, never defaulted, with precedence (AC2) -- tests 8-12, 14.
  - Provenance completeness: unknown_currency_sources carries the triple -- test 11.
  - Determinism: sorted lists, byte-identical repeat -- test 15.
  - No provider names in the engine (AD-2) -- test 18.
"""

from __future__ import annotations

from pathlib import Path

from core.currency_refusal import (
    REFUSAL_CROSS_CURRENCY,
    REFUSAL_UNKNOWN_CURRENCY,
    MoneyAggregationRefused,
    _is_monetary_metric,
    check_monetary_aggregation,
)


def _c(amount, currency, **kw):
    """Build a contribution dict with the AD-9 provenance triple defaulted."""
    base = {
        "amount": amount,
        "currency": currency,
        "source_system": kw.get("source_system", "sys"),
        "source_field": kw.get("source_field", "fld"),
        "pull_id": kw.get("pull_id", "pull-1"),
    }
    if "datastream_id" in kw:
        base["datastream_id"] = kw["datastream_id"]
    return base


# ---------------------------------------------------------------------------
# Legal cases (return None)
# ---------------------------------------------------------------------------


def test_single_currency_is_legal():  # 1
    contribs = [_c(10, "EUR"), _c(20, "EUR")]
    assert check_monetary_aggregation(
        metric="revenue", contributions=contribs, is_monetary=True
    ) is None


def test_single_currency_equal_reporting_is_legal():  # 2
    contribs = [_c(10, "EUR"), _c(5, "EUR")]
    assert check_monetary_aggregation(
        metric="revenue", contributions=contribs, is_monetary=True, reporting_currency="EUR"
    ) is None


def test_single_currency_not_equal_reporting_is_still_legal():  # 2b (§B.2)
    contribs = [_c(10, "USD"), _c(5, "USD")]
    assert check_monetary_aggregation(
        metric="revenue", contributions=contribs, is_monetary=True, reporting_currency="EUR"
    ) is None


def test_empty_contributions_is_legal():  # 3
    assert check_monetary_aggregation(
        metric="revenue", contributions=[], is_monetary=True
    ) is None


def test_non_monetary_metric_noops_even_with_mixed_currencies():  # 4
    contribs = [_c(10, "EUR"), _c(20, "USD")]
    assert check_monetary_aggregation(
        metric="clicks", contributions=contribs, is_monetary=False
    ) is None


# ---------------------------------------------------------------------------
# CROSS_CURRENCY_REFUSAL (AC1)
# ---------------------------------------------------------------------------


def test_two_distinct_currencies_refused():  # 5
    contribs = [_c(10, "USD"), _c(20, "EUR")]
    refusal = check_monetary_aggregation(
        metric="revenue", contributions=contribs, is_monetary=True
    )
    assert refusal is not None
    assert refusal["code"] == REFUSAL_CROSS_CURRENCY
    assert refusal["conflicting_currencies"] == ["EUR", "USD"]  # sorted


def test_refusal_names_metric_and_streams_sorted():  # 6
    contribs = [
        _c(10, "USD", datastream_id="ds-z"),
        _c(20, "EUR", datastream_id="ds-a"),
    ]
    refusal = check_monetary_aggregation(
        metric="ad_revenue", contributions=contribs, is_monetary=True
    )
    assert refusal["metric"] == "ad_revenue"
    assert refusal["offending_streams"] == ["ds-a", "ds-z"]  # sorted


def test_three_currencies_all_present_sorted_deduped():  # 7
    contribs = [_c(1, "USD"), _c(2, "EUR"), _c(3, "GBP"), _c(4, "EUR")]
    refusal = check_monetary_aggregation(
        metric="cost", contributions=contribs, is_monetary=True
    )
    assert refusal["conflicting_currencies"] == ["EUR", "GBP", "USD"]


# ---------------------------------------------------------------------------
# UNKNOWN_CURRENCY_GAP (AC2) + precedence
# ---------------------------------------------------------------------------


def test_none_currency_is_gap_even_if_others_agree():  # 8
    contribs = [_c(10, "EUR"), _c(20, None), _c(30, "EUR")]
    refusal = check_monetary_aggregation(
        metric="cost", contributions=contribs, is_monetary=True
    )
    assert refusal["code"] == REFUSAL_UNKNOWN_CURRENCY


def test_whitespace_currency_is_gap():  # 9
    contribs = [_c(10, "EUR"), _c(20, "   ")]
    refusal = check_monetary_aggregation(
        metric="cost", contributions=contribs, is_monetary=True
    )
    assert refusal["code"] == REFUSAL_UNKNOWN_CURRENCY


def test_unrecognized_iso_code_is_gap_failclosed():  # 10
    contribs = [_c(10, "EUR"), _c(20, "XYZ")]
    refusal = check_monetary_aggregation(
        metric="cost", contributions=contribs, is_monetary=True
    )
    assert refusal["code"] == REFUSAL_UNKNOWN_CURRENCY


def test_unknown_sources_names_provenance_triple():  # 11
    contribs = [
        _c(10, "EUR"),
        _c(20, None, source_system="meta-ads", source_field="cost_source_value",
           pull_id="pull-42"),
    ]
    refusal = check_monetary_aggregation(
        metric="cost", contributions=contribs, is_monetary=True
    )
    srcs = refusal["unknown_currency_sources"]
    assert srcs == [
        {"source_system": "meta-ads", "source_field": "cost_source_value", "pull_id": "pull-42"}
    ]


def test_unknown_precedence_over_cross_currency():  # 12
    # EUR + USD (would be cross-currency) AND a None -> gap wins, no partial cross sum.
    contribs = [_c(10, "EUR"), _c(20, "USD"), _c(30, None)]
    refusal = check_monetary_aggregation(
        metric="cost", contributions=contribs, is_monetary=True
    )
    assert refusal["code"] == REFUSAL_UNKNOWN_CURRENCY


# ---------------------------------------------------------------------------
# Messages / metadata
# ---------------------------------------------------------------------------


def test_cross_currency_message_interpolates_codes_and_metric():  # 13
    contribs = [_c(10, "USD"), _c(20, "EUR")]
    refusal = check_monetary_aggregation(
        metric="revenue", contributions=contribs, is_monetary=True
    )
    msg = refusal["message"]
    assert "revenue" in msg
    assert "EUR" in msg and "USD" in msg


def test_unknown_message_names_metric_and_source():  # 14
    contribs = [_c(10, None, source_system="src-x", source_field="fld-y")]
    refusal = check_monetary_aggregation(
        metric="cost", contributions=contribs, is_monetary=True
    )
    msg = refusal["message"]
    assert "cost" in msg
    assert "src-x" in msg and "fld-y" in msg


def test_determinism_byte_identical():  # 15
    contribs = [_c(10, "USD"), _c(20, "EUR")]
    a = check_monetary_aggregation(metric="revenue", contributions=contribs, is_monetary=True)
    b = check_monetary_aggregation(metric="revenue", contributions=contribs, is_monetary=True)
    assert a == b


def test_resolvable_via_on_cross_currency():  # 16
    contribs = [_c(10, "USD"), _c(20, "EUR")]
    refusal = check_monetary_aggregation(
        metric="revenue", contributions=contribs, is_monetary=True
    )
    assert refusal["resolvable_via"] == "fx_conversion"


# ---------------------------------------------------------------------------
# _is_monetary_metric seam (proxy behaviour)
# ---------------------------------------------------------------------------


def test_is_monetary_metric_proxy():  # 17
    # Explicit definition flag wins.
    assert _is_monetary_metric("anything", definition={"monetary": True}) is True
    assert _is_monetary_metric("revenue", definition={"monetary": False}) is False
    # format=='currency' definition -> monetary.
    assert _is_monetary_metric("x", definition={"format": "currency"}) is True
    # Money-named metrics (via the 39.1 classifier or the proxy fallback) -> True.
    assert _is_monetary_metric("revenue") is True
    assert _is_monetary_metric("cost") is True
    assert _is_monetary_metric("net_media_cost") is True
    # Non-money / ratio / dimension -> False.
    assert _is_monetary_metric("clicks") is False
    assert _is_monetary_metric("") is False


def test_ad2_no_provider_names_in_engine():  # 18
    src = Path(__file__).resolve().parents[2] / "core" / "currency_refusal.py"
    text = src.read_text(encoding="utf-8").lower()
    # A representative set of connector/provider names that must NOT appear in core money code.
    for provider in (
        "meta-ads", "google-ads", "google-ad-manager", "tiktok", "linkedin",
        "shopify", "stripe", "woocommerce", "square", "supermetrics", "nango",
    ):
        # Allow the strings only inside comments that quote them as examples? No -- the engine
        # must be fully provider-agnostic. Assert absence outright.
        assert provider not in text, f"provider name {provider!r} leaked into currency_refusal.py"


# ---------------------------------------------------------------------------
# Exception carrier
# ---------------------------------------------------------------------------


def test_money_aggregation_refused_carries_payload():
    payload = {"code": REFUSAL_CROSS_CURRENCY, "message": "boom"}
    exc = MoneyAggregationRefused(payload)
    assert exc.refusal is payload
    assert "boom" in str(exc)


def test_reporting_currency_echoed_in_refusal():
    contribs = [_c(10, "USD"), _c(20, "EUR")]
    refusal = check_monetary_aggregation(
        metric="revenue", contributions=contribs, is_monetary=True, reporting_currency="EUR"
    )
    assert refusal["reporting_currency"] == "EUR"
