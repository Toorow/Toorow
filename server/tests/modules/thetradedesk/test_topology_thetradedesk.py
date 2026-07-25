"""Story 28.2 -- account topology: Partner -> Advertiser discovery + access check.

discover_accounts fans over POST /v3/advertiser/query/partner with the partner
seat; every advertiser node carries the composite opaque id
'<PartnerId>:<AdvertiserId>'. check_account_access resolves the selected
advertiser. Auth is minted before use (TTD-Auth header). No env-var account
fallback (doctrine).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from .conftest import API_HOST

_AUTH_URL = f"{API_HOST}/v3/authentication"
_ADVERTISER_QUERY_URL = f"{API_HOST}/v3/advertiser/query/partner"


def _mock_auth():
    exp = (datetime.now(tz=timezone.utc) + timedelta(hours=6)).isoformat().replace(
        "+00:00", "Z"
    )
    respx.post(_AUTH_URL).mock(
        return_value=httpx.Response(200, json={"Token": "ttd-tok", "ExpirationDateUtc": exp})
    )


@respx.mock
def test_discover_accounts_returns_partner_with_advertiser_children(connector):
    _mock_auth()
    query_route = respx.post(_ADVERTISER_QUERY_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "Result": [
                    {"AdvertiserId": "adv-1", "AdvertiserName": "Acme", "CurrencyCode": "USD"},
                    {"AdvertiserId": "adv-2", "AdvertiserName": "Globex", "CurrencyCode": "EUR"},
                ]
            },
        )
    )
    nodes = connector.discover_accounts("conn-ttd")
    assert len(nodes) == 1
    partner = nodes[0]
    assert partner["level"] == "partner"
    assert partner["id"] == "partner-xyz"
    children = partner["children"]
    assert {c["id"] for c in children} == {"partner-xyz:adv-1", "partner-xyz:adv-2"}
    assert all(c["level"] == "advertiser" for c in children)
    # Auth minted before use -> TTD-Auth on the discovery call.
    assert query_route.calls.last.request.headers["ttd-auth"] == "ttd-tok"
    body = query_route.calls.last.request.read()
    assert b"partner-xyz" in body


@respx.mock
def test_discover_accounts_requires_partner_id(connector, monkeypatch):
    monkeypatch.delenv("THETRADEDESK_PARTNER_ID", raising=False)
    with pytest.raises(ValueError, match="THETRADEDESK_PARTNER_ID"):
        connector.discover_accounts("conn-ttd")


@respx.mock
def test_check_account_access_resolves_selected_advertiser(connector):
    _mock_auth()
    respx.get(f"{API_HOST}/v3/advertiser/adv-123").mock(
        return_value=httpx.Response(200, json={"AdvertiserId": "adv-123", "CurrencyCode": "USD"})
    )
    result = connector.check_account_access("conn-ttd", "partner-xyz:adv-123")
    assert result["ok"] is True
    assert result["partner_id"] == "partner-xyz"
    assert result["advertiser_id"] == "adv-123"


@respx.mock
def test_check_account_access_403_is_permission_denied(connector):
    from core.pull_errors import PermissionDeniedError

    _mock_auth()
    respx.get(f"{API_HOST}/v3/advertiser/adv-999").mock(
        return_value=httpx.Response(403, json={"Message": "not permitted"})
    )
    with pytest.raises(PermissionDeniedError):
        connector.check_account_access("conn-ttd", "partner-xyz:adv-999")


def test_bad_account_id_shape_is_refused(connector):
    with pytest.raises(ValueError, match="PartnerId"):
        connector._split_selected_account("just-an-advertiser")


def test_resolve_advertiser_selection_explicit_arg_wins(connector):
    partner_id, advertiser_id = connector._resolve_advertiser_selection(
        "conn-ttd", "partner-xyz:adv-77"
    )
    assert (partner_id, advertiser_id) == ("partner-xyz", "adv-77")
