"""Story 25.7 — LinkedIn Ads pull() typed-error path + error_map shape.

Uses respx to mock the LinkedIn Marketing adAnalytics endpoint. No test contacts
the real API (real LinkedIn E2E is a human gate, AI-08 / Phase B). Proves the
connector routes non-200/non-429 through classify_http_error with the manifest
error_map, that a 429 raises RateLimitError('linkedin-ads', ...), and that the
error_map is well-formed.
"""

from __future__ import annotations

import importlib.util
import json
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

_AD_ANALYTICS_URL = "https://api.linkedin.com/rest/adAnalytics"


def _import_connector():
    spec = importlib.util.spec_from_file_location("connector_linkedin_pull", _CONNECTOR_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def connector():
    return _import_connector()


@respx.mock
def test_pull_401_raises_auth_expired_with_payload_preserved(connector, tmp_path, monkeypatch):
    """A 401 LinkedIn response raises auth_expired with the provider payload preserved.

    LinkedIn's error body shape is {"message", "serviceErrorCode", "status"}; the
    pure-HTTP base class already yields auth_expired for a 401, and the parsed body
    survives as evidence on the typed error.
    """
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "li_401.duckdb"))

    li_error = {
        "message": "Empty oauth2_access_token",
        "serviceErrorCode": 401,
        "status": 401,
    }
    respx.get(_AD_ANALYTICS_URL).mock(return_value=httpx.Response(401, json=li_error))

    from core.pull_errors import AuthExpiredError

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        with pytest.raises(AuthExpiredError) as exc_info:
            connector.pull(
                connection_id="conn_test",
                date_from="2026-07-01",
                date_to="2026-07-03",
                project_id="jean-linkedin",
                pull_id="pull_li_401",
                account_id="502840441",
            )

    err = exc_info.value
    assert err.error_class == "auth_expired"
    assert err.retryable is False
    assert err.user_action == "reconnect"
    assert err.provider_status == 401
    # LinkedIn payload preserved intact (serviceErrorCode is the evidence we keep).
    assert err.provider_payload["serviceErrorCode"] == 401
    assert err.provider_payload["message"] == "Empty oauth2_access_token"


@respx.mock
def test_pull_403_raises_permission_denied(connector, tmp_path, monkeypatch):
    """A 403 (USER_NOT_AUTHORIZED) routes to permission_denied."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "li_403.duckdb"))

    li_error = {
        "message": "Not enough permissions to access: adAnalytics",
        "serviceErrorCode": 403,
        "status": 403,
    }
    respx.get(_AD_ANALYTICS_URL).mock(return_value=httpx.Response(403, json=li_error))

    from core.pull_errors import PermissionDeniedError

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        with pytest.raises(PermissionDeniedError) as exc_info:
            connector.pull(
                connection_id="conn_test",
                date_from="2026-07-01",
                date_to="2026-07-03",
                project_id="jean-linkedin",
                pull_id="pull_li_403",
                account_id="502840441",
            )
    assert exc_info.value.error_class == "permission_denied"
    assert exc_info.value.provider_payload["serviceErrorCode"] == 403


@respx.mock
def test_pull_raises_rate_limit_error_on_429(connector, tmp_path, monkeypatch):
    """pull raises RateLimitError('linkedin-ads', ...) on a 429 (data throttle)."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "li_429.duckdb"))

    respx.get(_AD_ANALYTICS_URL).mock(
        return_value=httpx.Response(
            429, headers={"Retry-After": "60"},
            json={"message": "TOO_MANY_REQUESTS", "serviceErrorCode": 429, "status": 429},
        )
    )

    from core.quota import RateLimitError

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        with pytest.raises(RateLimitError) as exc_info:
            connector.pull(
                connection_id="conn_test",
                date_from="2026-07-01",
                date_to="2026-07-03",
                project_id="jean-linkedin",
                pull_id="pull_li_429",
                account_id="502840441",
            )

    assert exc_info.value.platform == "linkedin-ads"
    assert exc_info.value.retry_after == 60


def test_manifest_error_map_shape():
    """manifest.json carries a well-formed LinkedIn error_map.

    Keys are "<status>:<code>"; values are canonical pull_errors classes. See the
    manifest _error_map_note for why LinkedIn's serviceErrorCode is not extractable
    by the generic classifier (the map is keyed <status>:<status>).
    """
    from core.pull_errors import ERROR_CLASSES

    manifest = json.loads(
        (_CONNECTOR_PATH.parent / "manifest.json").read_text(encoding="utf-8")
    )
    error_map = manifest.get("error_map")
    assert isinstance(error_map, dict) and error_map, "manifest must declare error_map"

    for key, cls in error_map.items():
        status, _, code = key.partition(":")
        assert status.isdigit() and code, f"malformed error_map key: {key!r}"
        assert cls in ERROR_CLASSES, f"{cls!r} is not a canonical error class"

    assert error_map["401:401"] == "auth_expired"
    assert error_map["403:403"] == "permission_denied"
    assert error_map["400:400"] == "invalid_request"
    assert error_map["500:500"] == "provider_transient"
    assert manifest.get("_error_map_note"), "the serviceErrorCode caveat must be documented"


def test_load_error_map_cached(connector):
    """_load_error_map returns the manifest error_map and caches it."""
    m1 = connector._load_error_map()
    m2 = connector._load_error_map()
    assert m1 is m2
    assert m1.get("401:401") == "auth_expired"
