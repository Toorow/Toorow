"""Tests for google-analytics discover_accounts (Story 25.5, AC5 + AC7).

Uses respx to mock the GA Admin API accountSummaries.list endpoint. No test
contacts the real API. Two paths are covered:
  * a mocked payload -> the generic account>property hierarchy shape core expects;
  * a 401 -> a typed core.pull_errors auth_expired (Story 25.2 taxonomy).
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import respx

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")

_CONNECTOR_PATH = (
    Path(__file__).parents[4]
    / "server"
    / "modules"
    / "google-analytics"
    / "connector.py"
)

_ADMIN_SUMMARIES_URL = "https://analyticsadmin.googleapis.com/v1beta/accountSummaries"

# accountSummaries.list mocked payload: 1 account, 2 properties.
_ADMIN_RESPONSE = {
    "accountSummaries": [
        {
            "account": "accounts/111",
            "displayName": "Acme Corp",
            "propertySummaries": [
                {"property": "properties/456", "displayName": "Acme Web"},
                {"property": "properties/789", "displayName": "Acme App"},
            ],
        }
    ]
}


def _import_connector():
    spec = importlib.util.spec_from_file_location("connector_ga4_discover", _CONNECTOR_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def connector():
    return _import_connector()


@respx.mock
def test_discover_accounts_maps_payload_to_hierarchy(connector):
    """AC5/AC7: mocked accountSummaries -> account>property hierarchy (opaque ids)."""
    route = respx.get(_ADMIN_SUMMARIES_URL).mock(
        return_value=httpx.Response(200, json=_ADMIN_RESPONSE)
    )

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        accounts = connector.discover_accounts("conn_test")

    assert route.called, "GA Admin accountSummaries.list must have been called"
    assert len(accounts) == 1
    account = accounts[0]
    assert account["id"] == "accounts/111"
    assert account["label"] == "Acme Corp"
    child_ids = {c["id"] for c in account["children"]}
    assert child_ids == {"properties/456", "properties/789"}
    # selection_level is 'property' -- the property ids are the reachable set.
    labels = {c["id"]: c["label"] for c in account["children"]}
    assert labels["properties/456"] == "Acme Web"


@respx.mock
def test_discover_accounts_uses_bearer_token(connector):
    """AD-3: the token is used as a Bearer header, never logged."""
    captured: list[dict] = []

    def _capture(request: httpx.Request, **kwargs):
        captured.append(dict(request.headers))
        return httpx.Response(200, json=_ADMIN_RESPONSE)

    respx.get(_ADMIN_SUMMARIES_URL).mock(side_effect=_capture)

    with patch("core.nango_client.get_fresh_token", return_value="tok-xyz"):
        connector.discover_accounts("conn_test")

    assert captured and captured[0].get("authorization") == "Bearer tok-xyz"


@respx.mock
def test_discover_accounts_401_raises_auth_expired(connector):
    """AC5/AC7: a 401 -> typed auth_expired (Story 25.2 taxonomy), payload preserved."""
    from core.pull_errors import AuthExpiredError

    respx.get(_ADMIN_SUMMARIES_URL).mock(
        return_value=httpx.Response(
            401, json={"error": {"code": 401, "message": "invalid credentials"}}
        )
    )

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        with pytest.raises(AuthExpiredError) as excinfo:
            connector.discover_accounts("conn_test")

    err = excinfo.value
    assert err.error_class == "auth_expired"
    assert err.user_action == "reconnect"
    assert err.provider_status == 401


@respx.mock
def test_discover_accounts_paginates(connector):
    """Discovery follows nextPageToken until exhausted (accounts accumulate)."""
    page1 = {
        "accountSummaries": [
            {
                "account": "accounts/1",
                "displayName": "One",
                "propertySummaries": [{"property": "properties/10", "displayName": "P10"}],
            }
        ],
        "nextPageToken": "tok2",
    }
    page2 = {
        "accountSummaries": [
            {
                "account": "accounts/2",
                "displayName": "Two",
                "propertySummaries": [{"property": "properties/20", "displayName": "P20"}],
            }
        ]
    }
    responses = [httpx.Response(200, json=page1), httpx.Response(200, json=page2)]
    respx.get(_ADMIN_SUMMARIES_URL).mock(side_effect=responses)

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        accounts = connector.discover_accounts("conn_test")

    ids = {a["id"] for a in accounts}
    assert ids == {"accounts/1", "accounts/2"}
