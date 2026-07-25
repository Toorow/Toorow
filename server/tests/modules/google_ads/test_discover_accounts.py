"""Story 26.2 -- MCC topology discovery + typed manager access refusal.

discover_accounts = listAccessibleCustomers + per-seed customer_client
expansion (levels manager -> client_account, ENABLED filter, composite child
ids '<cid>@<login_cid>'). check_account_access = the minimal 1-day metrics
probe; a selected MCC is refused typed (REQUESTED_METRICS_FOR_MANAGER ->
invalid_request). respx-mocked (AI-13: live probe deferred to 25.6).
"""

from __future__ import annotations

import json
import logging
from unittest.mock import patch

import httpx
import pytest
import respx
from core.pull_errors import InvalidRequestError, PermissionDeniedError

from .conftest import API_BASE

_LIST_URL = f"{API_BASE}/customers:listAccessibleCustomers"


def _client_row(cid, level, manager, name, status="ENABLED"):
    return {
        "customerClient": {
            "id": cid,
            "level": str(level),
            "manager": manager,
            "descriptiveName": name,
            "currencyCode": "EUR",
            "timeZone": "Europe/Paris",
            "status": status,
            "clientCustomer": f"customers/{cid}",
        }
    }


@respx.mock
def test_discover_accounts_builds_labeled_tree_with_composite_child_ids(connector):
    respx.get(_LIST_URL).mock(
        return_value=httpx.Response(
            200, json={"resourceNames": ["customers/111", "customers/222"]}
        )
    )
    # Seed 111 is an MCC: itself (level 0, manager) + one ENABLED client (333),
    # one CANCELED client (444, filtered out).
    respx.post(f"{API_BASE}/customers/111/googleAds:search").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    _client_row("111", 0, True, "Acme MCC"),
                    _client_row("333", 1, False, "Acme Client FR"),
                    _client_row("444", 1, False, "Old Client", status="CANCELED"),
                ]
            },
        )
    )
    # Seed 222 is a standalone client account.
    respx.post(f"{API_BASE}/customers/222/googleAds:search").mock(
        return_value=httpx.Response(
            200, json={"results": [_client_row("222", 0, False, "Solo Client")]}
        )
    )

    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        accounts = connector.discover_accounts("conn-1")

    by_id = {node["id"]: node for node in accounts}
    assert set(by_id) == {"111", "222"}

    mcc = by_id["111"]
    assert mcc["manager"] is True
    assert mcc["level"] == "manager"
    assert "MCC" in mcc["label"]
    children = mcc["children"]
    # CANCELED client filtered; composite id embeds the login CID.
    assert [c["id"] for c in children] == ["333@111"]
    assert children[0]["label"] == "Acme Client FR"
    assert children[0]["level"] == "client_account"
    assert children[0]["currency_code"] == "EUR"

    solo = by_id["222"]
    assert solo["manager"] is False
    assert solo["level"] == "client_account"
    assert "children" not in solo


@respx.mock
def test_discovery_expansion_sends_login_customer_id_header(connector):
    respx.get(_LIST_URL).mock(
        return_value=httpx.Response(200, json={"resourceNames": ["customers/111"]})
    )
    route = respx.post(f"{API_BASE}/customers/111/googleAds:search").mock(
        return_value=httpx.Response(
            200, json={"results": [_client_row("111", 0, False, "Solo")]}
        )
    )
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        connector.discover_accounts("conn-1")
    req = route.calls.last.request
    assert req.headers["login-customer-id"] == "111"
    assert req.headers["developer-token"] == "dev-token-test"
    query = json.loads(req.content)["query"]
    assert "FROM customer_client" in query
    assert "customer_client.level" in query


@respx.mock
def test_access_check_on_manager_is_typed_refusal(connector):
    """Story AC: selecting an MCC -> REQUESTED_METRICS_FOR_MANAGER, typed
    invalid_request (managers hold no metrics)."""
    body = {
        "error": {
            "code": 400,
            "status": "INVALID_ARGUMENT",
            "details": [
                {
                    "@type": (
                        "type.googleapis.com/google.ads.googleads.v24.errors."
                        "GoogleAdsFailure"
                    ),
                    "errors": [
                        {
                            "errorCode": {
                                "queryError": "REQUESTED_METRICS_FOR_MANAGER"
                            },
                            "message": "Metrics cannot be requested for a manager.",
                        }
                    ],
                }
            ],
        }
    }
    respx.post(f"{API_BASE}/customers/111/googleAds:search").mock(
        return_value=httpx.Response(400, json=body)
    )
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        with pytest.raises(InvalidRequestError) as exc:
            connector.check_account_access("conn-1", "111")
    assert exc.value.error_class == "invalid_request"
    assert "REQUESTED_METRICS_FOR_MANAGER" in str(exc.value.provider_payload)


@respx.mock
def test_access_check_ok_resolves_composite_account_id(connector):
    """The 1-day metrics probe targets the client CID with the embedded login."""
    route = respx.post(f"{API_BASE}/customers/333/googleAds:search").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "segments": {"date": "2026-07-20"},
                        "metrics": {"impressions": "10"},
                    }
                ]
            },
        )
    )
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        result = connector.check_account_access("conn-1", "333@111")
    assert result["ok"] is True
    assert result["customer_id"] == "333"
    assert result["login_customer_id"] == "111"
    req = route.calls.last.request
    assert req.headers["login-customer-id"] == "111"
    query = json.loads(req.content)["query"]
    assert "metrics.impressions" in query
    assert "DURING YESTERDAY" in query


@respx.mock
def test_discover_accounts_expands_nested_mcc_depth_two(connector):
    """Review 26.2 F-9: an MCC inside an MCC (depth 2) is expanded -- the
    grand-child client carries the ROOT seed as login_cid (any ancestor MCC
    is a valid login-customer-id)."""
    respx.get(_LIST_URL).mock(
        return_value=httpx.Response(200, json={"resourceNames": ["customers/111"]})
    )
    # Root MCC 111: itself + sub-MCC 555 + one direct client 333.
    respx.post(f"{API_BASE}/customers/111/googleAds:search").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    _client_row("111", 0, True, "Root MCC"),
                    _client_row("555", 1, True, "Sub MCC"),
                    _client_row("333", 1, False, "Direct Client"),
                ]
            },
        )
    )
    # Sub-MCC 555: itself + its client 666.
    sub_route = respx.post(f"{API_BASE}/customers/555/googleAds:search").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    _client_row("555", 0, True, "Sub MCC"),
                    _client_row("666", 1, False, "Nested Client"),
                ]
            },
        )
    )
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        accounts = connector.discover_accounts("conn-1")

    assert [node["id"] for node in accounts] == ["111"]
    root = accounts[0]
    children_by_id = {c["id"]: c for c in root["children"]}
    assert set(children_by_id) == {"555", "333@111"}
    sub = children_by_id["555"]
    assert sub["manager"] is True and sub["level"] == "manager"
    # Depth-2 client: composite id with the ROOT login cid.
    assert [c["id"] for c in sub["children"]] == ["666@111"]
    # The sub-MCC expansion kept login-customer-id = the root seed.
    assert sub_route.calls.last.request.headers["login-customer-id"] == "111"


@respx.mock
def test_discover_accounts_tolerates_a_failing_seed(connector, caplog):
    """Review 26.2 F-9: a 403 on ONE seed expansion is a warning + continue --
    the other seeds still discover (per-seed tolerant discovery)."""
    respx.get(_LIST_URL).mock(
        return_value=httpx.Response(
            200, json={"resourceNames": ["customers/111", "customers/222"]}
        )
    )
    respx.post(f"{API_BASE}/customers/111/googleAds:search").mock(
        return_value=httpx.Response(
            403,
            json={
                "error": {
                    "code": 403,
                    "status": "PERMISSION_DENIED",
                    "details": [
                        {
                            "@type": (
                                "type.googleapis.com/google.ads.googleads.v24."
                                "errors.GoogleAdsFailure"
                            ),
                            "errors": [
                                {
                                    "errorCode": {
                                        "authorizationError": "USER_PERMISSION_DENIED"
                                    },
                                    "message": "no access",
                                }
                            ],
                        }
                    ],
                }
            },
        )
    )
    respx.post(f"{API_BASE}/customers/222/googleAds:search").mock(
        return_value=httpx.Response(
            200, json={"results": [_client_row("222", 0, False, "Solo Client")]}
        )
    )
    with caplog.at_level(logging.WARNING):
        with patch("core.nango_client.get_fresh_token", return_value="tok"):
            accounts = connector.discover_accounts("conn-1")
    assert [node["id"] for node in accounts] == ["222"]
    assert "google_ads_discover_seed_failed" in caplog.text
    assert "seed=111" in caplog.text


@respx.mock
def test_discover_accounts_raises_when_every_seed_fails(connector):
    """All seeds failing must NOT return an empty tree that reads as 'no
    accounts' -- the last typed error surfaces."""
    respx.get(_LIST_URL).mock(
        return_value=httpx.Response(200, json={"resourceNames": ["customers/111"]})
    )
    respx.post(f"{API_BASE}/customers/111/googleAds:search").mock(
        return_value=httpx.Response(
            403, json={"error": {"code": 403, "status": "PERMISSION_DENIED"}}
        )
    )
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        with pytest.raises(PermissionDeniedError):
            connector.discover_accounts("conn-1")


@respx.mock
def test_discover_accounts_dedups_direct_and_mcc_reachable_client(
    connector, caplog
):
    """Review 26.2 F-9: a client accessible directly AND via an MCC keeps the
    DIRECT id (its own root node); the composite duplicate under the MCC is
    dropped and logged."""
    respx.get(_LIST_URL).mock(
        return_value=httpx.Response(
            200, json={"resourceNames": ["customers/111", "customers/333"]}
        )
    )
    respx.post(f"{API_BASE}/customers/111/googleAds:search").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    _client_row("111", 0, True, "Acme MCC"),
                    _client_row("333", 1, False, "Acme Client FR"),
                ]
            },
        )
    )
    respx.post(f"{API_BASE}/customers/333/googleAds:search").mock(
        return_value=httpx.Response(
            200, json={"results": [_client_row("333", 0, False, "Acme Client FR")]}
        )
    )
    with caplog.at_level(logging.INFO):
        with patch("core.nango_client.get_fresh_token", return_value="tok"):
            accounts = connector.discover_accounts("conn-1")

    by_id = {node["id"]: node for node in accounts}
    assert set(by_id) == {"111", "333"}
    # The MCC node carries NO composite duplicate of 333.
    assert "children" not in by_id["111"] or all(
        not c["id"].startswith("333@") for c in by_id["111"]["children"]
    )
    # The direct id is the selectable one.
    assert by_id["333"]["manager"] is False
    assert "google_ads_topology_duplicate_account" in caplog.text
