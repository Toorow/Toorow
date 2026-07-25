"""Tests for gsc discover_accounts (Story 25.7, AC4).

Uses respx to mock the Webmasters API sites.list endpoint. No test contacts
the real API. Two paths are covered:
  * a mocked payload -> the flat site-level hierarchy shape core expects;
  * a 401 -> a typed core.pull_errors AuthExpiredError (Story 25.2 taxonomy).
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
    Path(__file__).parents[4] / "server" / "modules" / "gsc" / "connector.py"
)

_SITES_URL = "https://www.googleapis.com/webmasters/v3/sites"

# Mocked sites.list payload: 2 sites (one URL-prefix, one domain property).
_SITES_RESPONSE = {
    "siteEntry": [
        {
            "siteUrl": "https://example.com/",
            "permissionLevel": "siteOwner"
        },
        {
            "siteUrl": "sc-domain:example.com",
            "permissionLevel": "siteOwner"
        }
    ]
}


def _import_connector():
    spec = importlib.util.spec_from_file_location("connector_gsc_discover", _CONNECTOR_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def connector():
    return _import_connector()


@respx.mock
def test_discover_accounts_returns_site_list(connector):
    """AC4: mocked sites.list -> flat site list (id=siteUrl, label=siteUrl)."""
    route = respx.get(_SITES_URL).mock(
        return_value=httpx.Response(200, json=_SITES_RESPONSE)
    )

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        sites = connector.discover_accounts("conn_test")

    assert route.called, "Webmasters sites.list must have been called"
    assert len(sites) == 2
    ids = {s["id"] for s in sites}
    assert ids == {"https://example.com/", "sc-domain:example.com"}
    labels = {s["id"]: s["label"] for s in sites}
    assert labels["https://example.com/"] == "https://example.com/"
    assert labels["sc-domain:example.com"] == "sc-domain:example.com"


@respx.mock
def test_discover_accounts_uses_bearer_token(connector):
    """AD-3: token used as a Bearer header, never stored or logged."""
    captured: list[dict] = []

    def _capture(request: httpx.Request, **kwargs):
        captured.append(dict(request.headers))
        return httpx.Response(200, json=_SITES_RESPONSE)

    respx.get(_SITES_URL).mock(side_effect=_capture)

    with patch("core.nango_client.get_fresh_token", return_value="tok-gsc-xyz"):
        connector.discover_accounts("conn_test")

    assert captured and captured[0].get("authorization") == "Bearer tok-gsc-xyz"


@respx.mock
def test_discover_accounts_401_raises_auth_expired(connector):
    """AC4: a 401 -> typed AuthExpiredError (Story 25.2 taxonomy), payload preserved."""
    from core.pull_errors import AuthExpiredError

    respx.get(_SITES_URL).mock(
        return_value=httpx.Response(
            401, json={"error": {"code": 401, "message": "Request had invalid authentication credentials."}}
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
def test_discover_accounts_429_raises_rate_limit_error(connector):
    """429 -> RateLimitError (breaker path); retry_after from Retry-After header."""
    from core.quota import RateLimitError

    respx.get(_SITES_URL).mock(
        return_value=httpx.Response(
            429, headers={"Retry-After": "60"}, json={"error": {"code": 429}}
        )
    )

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        with pytest.raises(RateLimitError) as excinfo:
            connector.discover_accounts("conn_test")

    assert excinfo.value.platform == "gsc"
    assert excinfo.value.retry_after == 60


@respx.mock
def test_discover_accounts_empty_site_list(connector):
    """An account with no verified sites returns an empty list (not an error)."""
    respx.get(_SITES_URL).mock(
        return_value=httpx.Response(200, json={"siteEntry": []})
    )

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        sites = connector.discover_accounts("conn_test")

    assert sites == []


@respx.mock
def test_discover_accounts_missing_site_entry_key(connector):
    """If siteEntry key is absent (API returned no property), return empty list."""
    respx.get(_SITES_URL).mock(
        return_value=httpx.Response(200, json={})
    )

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        sites = connector.discover_accounts("conn_test")

    assert sites == []
