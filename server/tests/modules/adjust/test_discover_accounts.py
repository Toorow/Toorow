"""Tests for adjust discover_accounts (playbook section 5, mocked respx only).

The Adjust topology is a single flat level: the API token's reachable APPS,
listed by the official Filters Data endpoint
(GET /reports-service/filters_data?required_filters=apps).
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

_CONNECTOR_PATH = (
    Path(__file__).parents[4] / "server" / "modules" / "adjust" / "connector.py"
)

_FILTERS_URL = "https://automate.adjust.com/reports-service/filters_data"


def _import_connector():
    spec = importlib.util.spec_from_file_location("connector_adjust_disc", _CONNECTOR_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def connector():
    return _import_connector()


@respx.mock
def test_discover_accounts_lists_reachable_apps(connector):
    """discover_accounts returns the generic [{id, label}] hierarchy from apps."""
    respx.get(_FILTERS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "apps": [
                    {"id": "abc123def456", "name": "Toorow Fitness"},
                    {"id": "zzz999yyy888", "name": ""},
                    {"name": "no-id-entry-ignored"},
                ]
            },
        )
    )

    with patch("core.nango_client.get_fresh_token", return_value="fake-adjust-token"):
        accounts = connector.discover_accounts("conn_adjust_test")

    assert accounts == [
        {"id": "abc123def456", "label": "Toorow Fitness"},
        # Empty name falls back to the id (never an empty label).
        {"id": "zzz999yyy888", "label": "zzz999yyy888"},
    ]
    request = respx.calls.last.request
    assert request.url.params["required_filters"] == "apps"
    assert request.headers["Authorization"] == "Bearer fake-adjust-token"


@respx.mock
def test_discover_accounts_401_raises_auth_expired(connector):
    """A 401 from filters_data raises the typed auth_expired error."""
    respx.get(_FILTERS_URL).mock(
        return_value=httpx.Response(401, json={"error": "unauthorized"})
    )

    from core.pull_errors import AuthExpiredError

    with patch("core.nango_client.get_fresh_token", return_value="expired-token"):
        with pytest.raises(AuthExpiredError):
            connector.discover_accounts("conn_adjust_test")
