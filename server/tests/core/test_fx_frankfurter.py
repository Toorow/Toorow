"""Tests for Story 39.5 -- live as-of-day FX provider (Frankfurter / ECB).

All OFFLINE (respx intercepts httpx at the transport level -- no live network,
E39-NFR07). Covers: structural Protocol conformance against the 39.4 seam,
identity short-circuit (no network), on_date=None, business-day fetch + provenance,
SOURCE_ECB label, carry-forward provenance (feed-driven, via the echoed date),
EUR-triangulation (both directions + EUR-on-a-side), determinism, fail-closed on an
uncovered pair / uncovered -> MissingAsOfRate / retry-exhaustion -> None, the
request-scoped per-date cache, the convert() no-fork seam proof, a no-un-mocked-
traffic guard, and the AD-2 generic-seam grep (fx_helper.py stays Frankfurter-free).

Harness header + fixture-loading calqués sur test_fx_helper.py / test_pull_gam.py.
"""

from __future__ import annotations

import json
import os
import re
from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core.fx_frankfurter import (  # noqa: E402
    FRANKFURTER_BASE_URL,
    POLICY_CARRY_FORWARD,
    SOURCE_ECB,
    FrankfurterAsOfRateProvider,
    FxFeedUnavailable,
    RateFeedClient,
    _carry_forward_marker,
    _eur_rate,
    _triangulate,
)
from core.fx_helper import (  # noqa: E402
    POLICY_IDENTITY,
    TIER_HISTORICAL,
    AsOfRateProvider,
    MissingAsOfRate,
    RateQuote,
    convert,
)

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "frankfurter"
_FX_MODULE_PATH = Path(__file__).resolve().parents[2] / "core" / "fx_helper.py"

# Match every Frankfurter /v1/{date} call regardless of query string.
_FEED_ROUTE = re.compile(rf"{re.escape(FRANKFURTER_BASE_URL)}/v1/\d{{4}}-\d{{2}}-\d{{2}}.*")

# Example dates.
_THURSDAY = date(2024, 8, 15)  # a publishing day (fixture date == requested)
_SUNDAY = date(2024, 8, 18)  # a non-publishing day -> carry-forward to Fri 08-16
_FRIDAY = date(2024, 8, 16)  # the effective (carry-forward) day the fixture echoes


def _load(name: str) -> dict:
    with (_FIXTURES / f"{name}.json").open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _mock_feed(payload: dict) -> respx.Route:
    """Mock every /v1/{date} GET with *payload* (a 200 JSON response)."""
    return respx.get(url__regex=_FEED_ROUTE.pattern).mock(
        return_value=httpx.Response(200, json=payload)
    )


# --------------------------------------------------------------------------- #
# 1-3. Protocol conformance & identity.
# --------------------------------------------------------------------------- #


def test_structural_protocol_conformance() -> None:
    """isinstance vs the @runtime_checkable 39.4 Protocol is True (the AD-4 seam)."""
    assert isinstance(FrankfurterAsOfRateProvider(), AsOfRateProvider)


@respx.mock
def test_identity_short_circuits_without_network() -> None:
    route = _mock_feed(_load("business_day_eur_usd_gbp"))
    quote = FrankfurterAsOfRateProvider().get_rate("EUR", "EUR", _THURSDAY)
    assert quote == RateQuote(
        rate=1.0,
        source=SOURCE_ECB,
        tier=TIER_HISTORICAL,
        as_of=_THURSDAY,
        policy=POLICY_IDENTITY,
    )
    assert route.call_count == 0  # identity never hits the feed


@respx.mock
def test_on_date_none_returns_none_without_network() -> None:
    route = _mock_feed(_load("business_day_eur_usd_gbp"))
    assert FrankfurterAsOfRateProvider().get_rate("USD", "EUR", None) is None
    assert route.call_count == 0


# --------------------------------------------------------------------------- #
# 4-7. As-of fetch, source label, carry-forward provenance.
# --------------------------------------------------------------------------- #


@respx.mock
def test_business_day_fetch_no_carry_forward() -> None:
    payload = _load("business_day_eur_usd_gbp")
    _mock_feed(payload)
    quote = FrankfurterAsOfRateProvider().get_rate("USD", "EUR", _THURSDAY)
    assert quote is not None
    assert quote.rate == pytest.approx(1.0 / payload["rates"]["USD"])
    assert quote.source == SOURCE_ECB
    assert quote.tier == TIER_HISTORICAL
    assert quote.as_of == _THURSDAY  # publishing day: effective == requested
    assert quote.policy is None  # no carry-forward on a publishing day


@respx.mock
def test_source_label_is_ecb() -> None:
    _mock_feed(_load("business_day_eur_usd_gbp"))
    quote = FrankfurterAsOfRateProvider().get_rate("USD", "GBP", _THURSDAY)
    assert quote is not None
    assert quote.source == SOURCE_ECB
    assert quote.tier == TIER_HISTORICAL


@respx.mock
def test_carry_forward_provenance() -> None:
    # Sunday request; fixture echoes Friday 2024-08-16 as the effective date.
    payload = _load("weekend_carry_forward")
    assert payload["date"] == _FRIDAY.isoformat()
    _mock_feed(payload)
    quote = FrankfurterAsOfRateProvider().get_rate("USD", "EUR", _SUNDAY)
    assert quote is not None
    assert quote.as_of == _FRIDAY  # effective date, NOT the requested Sunday
    assert quote.policy == POLICY_CARRY_FORWARD


@respx.mock
def test_carry_forward_flows_into_convert_provenance() -> None:
    _mock_feed(_load("weekend_carry_forward"))
    result = convert(
        100.0,
        "USD",
        reporting_currency="EUR",
        tier="historical",
        on_date=_SUNDAY,
        provider=FrankfurterAsOfRateProvider(),
    )
    # AD-9: Friday's rate applied to Sunday is visible in the fx block.
    assert result.fx["as_of_date"] == _FRIDAY.isoformat()


# --------------------------------------------------------------------------- #
# 8-11. EUR-triangulation.
# --------------------------------------------------------------------------- #


@respx.mock
def test_triangulation_cross_rate_usd_gbp() -> None:
    payload = _load("cross_rate_usd_gbp")
    _mock_feed(payload)
    quote = FrankfurterAsOfRateProvider().get_rate("USD", "GBP", _THURSDAY)
    assert quote is not None
    expected = payload["rates"]["GBP"] / payload["rates"]["USD"]
    assert quote.rate == pytest.approx(expected, abs=1e-9)


@respx.mock
def test_triangulation_eur_on_to_side() -> None:
    payload = _load("cross_rate_usd_gbp")
    _mock_feed(payload)
    quote = FrankfurterAsOfRateProvider().get_rate("USD", "EUR", _THURSDAY)
    assert quote is not None
    assert quote.rate == pytest.approx(1.0 / payload["rates"]["USD"], abs=1e-9)


@respx.mock
def test_triangulation_eur_on_from_side() -> None:
    payload = _load("cross_rate_usd_gbp")
    _mock_feed(payload)
    quote = FrankfurterAsOfRateProvider().get_rate("EUR", "GBP", _THURSDAY)
    assert quote is not None
    assert quote.rate == pytest.approx(payload["rates"]["GBP"], abs=1e-9)


@respx.mock
def test_triangulation_determinism_cached() -> None:
    _mock_feed(_load("cross_rate_usd_gbp"))
    provider = FrankfurterAsOfRateProvider()
    first = provider.get_rate("USD", "GBP", _THURSDAY)
    second = provider.get_rate("USD", "GBP", _THURSDAY)
    assert first == second


def test_pure_triangulation_helpers() -> None:
    rates = {"USD": 1.09, "GBP": 0.85}
    assert _eur_rate(rates, "EUR") == 1.0
    assert _eur_rate(rates, "USD") == 1.09
    assert _eur_rate(rates, "XYZ") is None
    assert _triangulate(rates, "USD", "GBP") == pytest.approx(0.85 / 1.09)
    assert _triangulate(rates, "USD", "XYZ") is None  # missing leg
    assert _carry_forward_marker(_SUNDAY, _FRIDAY) == POLICY_CARRY_FORWARD
    assert _carry_forward_marker(_THURSDAY, _THURSDAY) is None


# --------------------------------------------------------------------------- #
# 12-14. Fail-closed.
# --------------------------------------------------------------------------- #


@respx.mock
def test_uncovered_symbol_returns_none() -> None:
    _mock_feed(_load("uncovered_symbol"))  # only USD present; XYZ absent
    quote = FrankfurterAsOfRateProvider().get_rate("USD", "XYZ", _THURSDAY)
    assert quote is None


@respx.mock
def test_uncovered_pair_raises_missing_as_of_through_convert() -> None:
    _mock_feed(_load("uncovered_symbol"))
    with pytest.raises(MissingAsOfRate) as excinfo:
        convert(
            100.0,
            "USD",
            reporting_currency="XYZ",
            tier="historical",
            on_date=_THURSDAY,
            provider=FrankfurterAsOfRateProvider(),
        )
    assert excinfo.value.from_currency == "USD"
    assert excinfo.value.to_currency == "XYZ"


@respx.mock
def test_retry_exhaustion_returns_none() -> None:
    route = respx.get(url__regex=_FEED_ROUTE.pattern).mock(
        return_value=httpx.Response(500)
    )
    provider = FrankfurterAsOfRateProvider(client=RateFeedClient(max_attempts=3))
    assert provider.get_rate("USD", "EUR", _THURSDAY) is None
    assert route.call_count == 3  # bounded retry exhausted


@respx.mock
def test_retry_exhaustion_raises_missing_as_of_through_convert() -> None:
    respx.get(url__regex=_FEED_ROUTE.pattern).mock(return_value=httpx.Response(503))
    with pytest.raises(MissingAsOfRate):
        convert(
            100.0,
            "USD",
            reporting_currency="EUR",
            tier="historical",
            on_date=_THURSDAY,
            provider=FrankfurterAsOfRateProvider(client=RateFeedClient(max_attempts=2)),
        )


@respx.mock
def test_connect_error_is_retried_then_fails_closed() -> None:
    route = respx.get(url__regex=_FEED_ROUTE.pattern).mock(
        side_effect=httpx.ConnectError("boom")
    )
    provider = FrankfurterAsOfRateProvider(client=RateFeedClient(max_attempts=3))
    assert provider.get_rate("USD", "EUR", _THURSDAY) is None
    assert route.call_count == 3


def test_client_raises_fx_feed_unavailable_on_persistent_500() -> None:
    # Belt-and-suspenders: a raw MockTransport (no respx) proves the typed error.
    transport = httpx.MockTransport(lambda request: httpx.Response(500))
    client = RateFeedClient(transport=transport, max_attempts=2)
    with pytest.raises(FxFeedUnavailable):
        client.fetch_rates(_THURSDAY, symbols=["USD"])


# --------------------------------------------------------------------------- #
# 15-16. Robustness / request-scoped per-date cache.
# --------------------------------------------------------------------------- #


@respx.mock
def test_same_date_multi_pair_hits_feed_once() -> None:
    route = _mock_feed(_load("cross_rate_usd_gbp"))
    provider = FrankfurterAsOfRateProvider()
    a = provider.get_rate("USD", "EUR", _THURSDAY)
    b = provider.get_rate("GBP", "EUR", _THURSDAY)
    assert a is not None and b is not None
    assert route.call_count == 1  # cached per date


@respx.mock
def test_different_dates_fetch_separately() -> None:
    route = _mock_feed(_load("cross_rate_usd_gbp"))
    provider = FrankfurterAsOfRateProvider()
    provider.get_rate("USD", "EUR", _THURSDAY)
    provider.get_rate("USD", "EUR", date(2024, 8, 14))
    assert route.call_count == 2  # cache keyed by date


# --------------------------------------------------------------------------- #
# 17-18. convert() no-fork seam proof + no-live-traffic guard.
# --------------------------------------------------------------------------- #


@respx.mock  # global router (default assert_all_called=True) so _mock_feed's respx.get lands here
def test_convert_seam_is_only_integration_point() -> None:
    payload = _load("cross_rate_usd_gbp")
    _mock_feed(payload)
    rate = payload["rates"]["GBP"] / payload["rates"]["USD"]
    result = convert(
        100.0,
        "USD",
        reporting_currency="GBP",
        tier="historical",
        on_date=_THURSDAY,
        provider=FrankfurterAsOfRateProvider(),
    )
    assert result.amount == pytest.approx(100.0 * rate)
    assert result.source_amount == 100.0
    assert result.source_currency == "USD"
    assert result.fx == {
        "rate": pytest.approx(rate),
        "as_of_date": _THURSDAY.isoformat(),
        "source": SOURCE_ECB,
        "tier": TIER_HISTORICAL,
    }


@respx.mock  # global router; respx's default assert_all_mocked=True raises on ANY un-mocked request
def test_no_unmocked_traffic_guard() -> None:
    # Proof nothing escapes to a live host (E39-NFR07): the global router's default
    # assert_all_mocked=True raises on any request not matched by _mock_feed's route. Using the
    # bare @respx.mock (not @respx.mock(...args)) so _mock_feed's module-level respx.get lands on
    # the SAME active router the request is checked against.
    _mock_feed(_load("business_day_eur_usd_gbp"))
    quote = FrankfurterAsOfRateProvider().get_rate("USD", "EUR", _THURSDAY)
    assert quote is not None


# --------------------------------------------------------------------------- #
# 19. AD-2 generic-seam purity.
# --------------------------------------------------------------------------- #


def test_fx_helper_stays_frankfurter_free() -> None:
    # AD-2 / E39-AD4: fx_helper stays feed-AGNOSTIC -- the invariant is CODE decoupling, not a
    # word ban. fx_helper's docstring may legitimately NAME the seam ("Frankfurter/ECB feed
    # plugs in here"); what it must NOT do is import the concrete provider or make a network call.
    text = _FX_MODULE_PATH.read_text(encoding="utf-8")
    assert "fx_frankfurter" not in text, "fx_helper.py must not reference the concrete provider"
    assert "import httpx" not in text and "import requests" not in text, (
        "fx_helper.py must make no network call (the live feed is Story 39.5's provider)"
    )
    assert "api.frankfurter" not in text, "fx_helper.py must not hardcode a feed URL"
