"""Story 25.7 — linkedin-ads discover_accounts (single flat topology level).

Uses respx to mock GET /rest/adAccounts?q=search. No test contacts the real API.
Covers:
  * a mocked cursor-paged payload -> the generic flat 'ad_account' hierarchy shape
    core expects, with ids as full sponsoredAccount URNs;
  * a 401 -> a typed core.pull_errors auth_expired (Story 25.2 taxonomy);
  * a 429 -> RateLimitError('linkedin-ads', ...).
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
    Path(__file__).parents[4] / "server" / "modules" / "linkedin-ads" / "connector.py"
)

_AD_ACCOUNTS_URL = "https://api.linkedin.com/rest/adAccounts"


def _import_connector():
    spec = importlib.util.spec_from_file_location("connector_li_discover", _CONNECTOR_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def connector():
    return _import_connector()


@respx.mock
def test_discover_accounts_flat_hierarchy_with_cursor_paging(connector):
    """discover_accounts returns full sponsoredAccount URNs across cursor pages."""
    respx.get(_AD_ACCOUNTS_URL).mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "elements": [
                        {"id": 507404993, "name": "Dunder Mifflin Account", "status": "ACTIVE"},
                        {"id": 502840441, "name": "", "status": "ACTIVE"},
                    ],
                    "metadata": {"nextPageToken": "TOKEN_PAGE_2"},
                },
            ),
            httpx.Response(
                200,
                json={
                    "elements": [{"id": 999888, "name": "Third Account"}],
                    "metadata": {},
                },
            ),
        ]
    )

    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        accounts = connector.discover_accounts("conn_li_1")

    assert accounts == [
        {"id": "urn:li:sponsoredAccount:507404993", "label": "Dunder Mifflin Account"},
        # empty name falls back to the URN
        {"id": "urn:li:sponsoredAccount:502840441", "label": "urn:li:sponsoredAccount:502840441"},
        {"id": "urn:li:sponsoredAccount:999888", "label": "Third Account"},
    ]


@respx.mock
def test_discover_accounts_sends_search_finder_and_version_header(connector):
    """The finder query is q=search and the LinkedIn-Version header is sent."""
    captured: list[httpx.Request] = []

    def _capture(request: httpx.Request):
        captured.append(request)
        return httpx.Response(200, json={"elements": [], "metadata": {}})

    respx.get(_AD_ACCOUNTS_URL).mock(side_effect=_capture)

    with patch("core.nango_client.get_fresh_token", return_value="tok-xyz"):
        connector.discover_accounts("conn_li_1")

    req = captured[0]
    assert req.url.params.get("q") == "search"
    assert req.headers.get("linkedin-version")
    assert req.headers.get("authorization") == "Bearer tok-xyz"


@respx.mock
def test_discover_accounts_401_raises_auth_expired(connector):
    respx.get(_AD_ACCOUNTS_URL).mock(
        return_value=httpx.Response(
            401,
            json={"message": "Empty oauth2_access_token", "serviceErrorCode": 401, "status": 401},
        )
    )

    from core.pull_errors import ConnectorError

    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        with pytest.raises(ConnectorError) as excinfo:
            connector.discover_accounts("conn_li_1")

    assert excinfo.value.error_class == "auth_expired"
    assert excinfo.value.provider_payload["serviceErrorCode"] == 401


@respx.mock
def test_discover_accounts_429_raises_rate_limit_error(connector):
    respx.get(_AD_ACCOUNTS_URL).mock(
        return_value=httpx.Response(
            429, headers={"Retry-After": "30"},
            json={"message": "TOO_MANY_REQUESTS", "serviceErrorCode": 429, "status": 429},
        )
    )

    from core.quota import RateLimitError

    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        with pytest.raises(RateLimitError) as excinfo:
            connector.discover_accounts("conn_li_1")

    assert excinfo.value.platform == "linkedin-ads"
    assert excinfo.value.retry_after == 30
