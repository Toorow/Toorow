"""Seam tests for the two reference-vocabulary routes (Story 48.3).

Built on the production ASGI app rather than a hand-assembled router, because the
question these answer is "is the door actually mounted?" and a hand-built router
answers it about a router nobody serves.

Both routes are read-only projections of the governed vocabularies, so what has to
be true is narrow and checkable: they exist, they require authentication, they rank
deterministically, they are bounded, and the list they serve is the same one
`core.money_policy` validates against -- a selector offering a value the server
would refuse is worse than no selector.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture()
def client():
    from core.main import build_asgi_app
    from starlette.testclient import TestClient

    app = build_asgi_app()
    with patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "seam@test"))):
        with TestClient(app, raise_server_exceptions=False) as test_client:
            yield test_client


@pytest.fixture()
def client_unauth():
    from core.main import build_asgi_app
    from starlette.testclient import TestClient

    app = build_asgi_app()
    with patch("core.admin_api._check_auth", new=AsyncMock(return_value=(False, ""))):
        with TestClient(app, raise_server_exceptions=False) as test_client:
            yield test_client


ROUTES = ("/api/reference/currencies", "/api/reference/timezones")


@pytest.mark.parametrize("route", ROUTES)
def test_route_is_mounted_on_the_production_app(client, route):
    response = client.get(route)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["items"], "a vocabulary route that returns nothing is not serving one"
    assert body["total_selectable"] >= len(body["items"])


@pytest.mark.parametrize("route", ROUTES)
def test_route_requires_authentication(client_unauth, route):
    assert client_unauth.get(route).status_code == 401


def test_currency_route_serves_only_selectable_reporting_currencies(client):
    """A metal or a fund code resolves as a NATIVE currency and is not a reporting
    one. Offering XAU here would offer a value the Money Policy profile refuses."""
    body = client.get("/api/reference/currencies?limit=50").json()
    codes = {item["code"] for item in body["items"]}
    for excluded in ("XAU", "XDR", "XXX", "XTS"):
        assert excluded not in codes


def test_currency_route_carries_the_minor_unit(client):
    """The minor unit is why two totals in different currencies round differently,
    so the selector shows it rather than making the operator know it."""
    body = client.get("/api/reference/currencies?q=JPY").json()
    jpy = next(item for item in body["items"] if item["code"] == "JPY")
    assert jpy["minor_unit"] == 0
    assert body["authority"] == "ISO 4217"


def test_timezone_route_names_the_tzdb_release_it_answered_from(client):
    """A selector that cannot say which tz database it read is offering an
    unversioned answer, and a reporting date derived from it is not reproducible."""
    body = client.get("/api/reference/timezones?q=paris").json()
    assert body["tzdb_version"] and body["tzdb_version"] != "unknown"
    assert any(item["code"] == "Europe/Paris" for item in body["items"])


def test_timezone_route_excludes_compatibility_identifiers(client):
    body = client.get("/api/reference/timezones?q=etc&limit=50").json()
    codes = {item["code"] for item in body["items"]}
    assert not any(code.startswith("Etc/") for code in codes)


@pytest.mark.parametrize("route", ROUTES)
def test_ranking_is_deterministic_and_bounded(client, route):
    first = client.get(f"{route}?q=a&limit=10").json()["items"]
    second = client.get(f"{route}?q=a&limit=10").json()["items"]
    assert first == second, "the same query must return the same order"
    assert len(first) <= 10
    # A caller cannot ask for the whole vocabulary in one request.
    assert len(client.get(f"{route}?limit=9999").json()["items"]) <= 50


def test_the_selector_and_the_validator_agree(client):
    """Every code the currency selector offers must be one the Money Policy
    profile accepts. Two lists ranked by two rules is how a screen starts offering
    a value the server refuses."""
    from core.governance_rule_sets import get_profile
    from core.money_policy import PROFILE_MONEY

    validate = get_profile(PROFILE_MONEY).validate
    offered = client.get("/api/reference/currencies?limit=50").json()["items"]
    for item in offered:
        resolved = validate(
            {
                "reporting_currency": item["code"],
                "rate_source_priority": ["reference_bank"],
                "max_staleness_days": 4,
            }
        )
        assert resolved["reporting_currency"] == item["code"]
