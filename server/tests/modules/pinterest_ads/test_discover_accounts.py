"""Story 26.3 -- flat ad-account topology (pattern 25.5 / meta-ads).

discover_accounts pages GET /ad_accounts (bookmark cursor,
include_shared_accounts=true) into the generic flat list core's topology
flow consumes; check_account_access runs the minimal 1-day tier-core probe
on the selected account and surfaces the typed permission_denied refusal.
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest
import respx
from core.pull_errors import PermissionDeniedError

from .conftest import AD_ACCOUNT, API_BASE

_AD_ACCOUNTS_URL = f"{API_BASE}/ad_accounts"
_ACCOUNT_ANALYTICS_URL = f"{API_BASE}/ad_accounts/{AD_ACCOUNT}/analytics"


@respx.mock
def test_discover_accounts_paginates_bookmark_and_includes_shared(connector):
    page_one = {
        "items": [
            {"id": "549755885175", "name": "Acme", "currency": "USD",
             "country": "US"},
        ],
        "bookmark": "cursor-2",
    }
    page_two = {
        "items": [
            {"id": "549755885299", "name": "Shared brand", "currency": "EUR",
             "country": "FR"},
        ],
        "bookmark": None,
    }
    route = respx.get(_AD_ACCOUNTS_URL).mock(
        side_effect=[
            httpx.Response(200, json=page_one),
            httpx.Response(200, json=page_two),
        ]
    )
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        accounts = connector.discover_accounts("conn-1")

    assert [a["id"] for a in accounts] == ["549755885175", "549755885299"]
    assert accounts[0] == {
        "id": "549755885175",
        "label": "Acme (USD)",
        "level": "ad_account",
        "currency": "USD",
        "country": "US",
    }
    first_params = route.calls[0].request.url.params
    assert first_params["include_shared_accounts"] == "true"
    second_params = route.calls[1].request.url.params
    assert second_params["bookmark"] == "cursor-2"


@respx.mock
def test_check_account_access_probes_one_day_tier_core(connector):
    route = respx.get(_ACCOUNT_ANALYTICS_URL).mock(
        return_value=httpx.Response(200, json=[])
    )
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        result = connector.check_account_access("conn-1", AD_ACCOUNT)
    assert result == {"account_id": AD_ACCOUNT, "ok": True}
    params = route.calls.last.request.url.params
    assert params["columns"] == "SPEND_IN_MICRO_DOLLAR"
    assert params["granularity"] == "DAY"
    assert params["start_date"] == params["end_date"]


@respx.mock
def test_check_account_access_permission_denied_is_typed(connector):
    body = {"code": 3, "message": "Your application consumer type is not supported."}
    respx.get(_ACCOUNT_ANALYTICS_URL).mock(
        return_value=httpx.Response(403, json=body)
    )
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        with pytest.raises(PermissionDeniedError):
            connector.check_account_access("conn-1", AD_ACCOUNT)
