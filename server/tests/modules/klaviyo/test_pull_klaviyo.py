"""Tests for the Klaviyo connector pull() function — Story 25.7.

Covers the typed-error paths required by the connector standard (Story 25.2):
  - HTTP 401 raises AuthExpiredError with the Klaviyo JSON:API payload preserved
  - HTTP 429 raises RateLimitError (breaker path, NOT classify_http_error)
  - HTTP 500 raises a ConnectorError subclass (provider_transient)

No test contacts the real Klaviyo API. Real live testing is a human gate (AI-08).

Klaviyo uses JSON:API error format:
  {"errors": [{"id": "...", "status": "401", "code": "...", "title": "...", "detail": "..."}]}
The error_map in manifest.json is keyed on HTTP status only (no numeric sub-codes).
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
    Path(__file__).parents[4] / "server" / "modules" / "klaviyo" / "connector.py"
)

_KLAVIYO_CAMPAIGN_URL = "https://a.klaviyo.com/api/campaign-values-reports/"
_KLAVIYO_FLOW_URL = "https://a.klaviyo.com/api/flow-series-reports/"

_JSONAPI_401 = {
    "errors": [
        {
            "id": "test-error-id-001",
            "status": "401",
            "code": "not_authenticated",
            "title": "Unauthorized",
            "detail": "Authentication credentials were not provided or are invalid.",
        }
    ]
}

_JSONAPI_500 = {
    "errors": [
        {
            "id": "test-error-id-002",
            "status": "500",
            "code": "internal_server_error",
            "title": "Internal Server Error",
            "detail": "An unexpected error occurred on Klaviyo's servers.",
        }
    ]
}


def _import_connector():
    spec = importlib.util.spec_from_file_location("connector_klaviyo", _CONNECTOR_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def connector():
    return _import_connector()


# ---------------------------------------------------------------------------
# 401 → AuthExpiredError with JSON:API payload preserved (Story 25.2 contract)
# ---------------------------------------------------------------------------


@respx.mock
def test_pull_401_raises_auth_expired_with_payload_preserved(
    connector, tmp_path, monkeypatch
):
    """Story 25.2: a 401 Klaviyo response raises auth_expired with the payload preserved.

    Proves the connector routes non-200/non-429 through classify_http_error and that
    the parsed Klaviyo JSON:API error body survives as evidence on the typed error.
    """
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "kl_401.duckdb"))
    monkeypatch.setenv("KLAVIYO_CONVERSION_METRIC_ID", "ABCDEF123")

    respx.post(_KLAVIYO_CAMPAIGN_URL).mock(
        return_value=httpx.Response(401, json=_JSONAPI_401)
    )
    respx.post(_KLAVIYO_FLOW_URL).mock(
        return_value=httpx.Response(401, json=_JSONAPI_401)
    )

    from core.pull_errors import AuthExpiredError

    with patch("core.nango_client.get_fresh_token", return_value="fake-klaviyo-key"):
        with pytest.raises(AuthExpiredError) as exc_info:
            connector.pull(
                connection_id="conn_kl_test",
                date_from="2026-07-01",
                date_to="2026-07-01",
                project_id="jean-klaviyo",
                pull_id="pull_kl_401",
                conversion_metric_id="ABCDEF123",
            )

    err = exc_info.value
    assert err.error_class == "auth_expired"
    assert err.retryable is False
    assert err.user_action == "reconnect"
    assert err.provider_status == 401
    # JSON:API payload preserved (errors array must be present as evidence)
    assert isinstance(err.provider_payload, dict)
    assert "errors" in err.provider_payload
    assert err.provider_payload["errors"][0]["id"] == "test-error-id-001"


# ---------------------------------------------------------------------------
# 422 → InvalidRequestError (AI-114: a judgment that used to be DOUBLY dead)
# ---------------------------------------------------------------------------


@respx.mock
def test_pull_422_is_invalid_request_not_a_retryable_unknown(
    connector, tmp_path, monkeypatch
):
    """AI-114 (2026-08-01): this verdict is NEW because the old one never fired.

    manifest.error_map carried a bare "422" key, and it was dead twice over:
    core.pull_errors.classify_http_error only ever looks up
    "<status>:<provider_code>", AND _post_reporting called it WITHOUT passing
    the map at all. So a Klaviyo 422 came out `unclassified`, whose retryable
    flag is True -- the worker re-sent a request the API had already refused as
    unprocessable.

    SUITE, le meme jour (AI-116). Le verdict a MIGRE de `_STATUS_OVERRIDES` vers
    `core.pull_errors._base_class_for_status` : 422 (Unprocessable Entity) est une
    semantique HTTP generique, pas une lecture propre a Klaviyo, et le laisser
    chez un seul module signifiait que les 37 autres continuaient a rejouer un
    422 jusqu'au dead_letter. La derniere assertion de ce test disait
    << sans la surcharge, core repondrait unclassified >> : elle prouvait que la
    surcharge portait quelque chose. Elle dit maintenant l'inverse -- core le
    porte pour tout le monde -- et c'est un renforcement, pas un relachement : le
    comportement observable du pull, lui, n'a pas bouge d'un iota.

    Ce qui NE migre pas, et le test voisin le garde : le 404, dont six modules
    donnent deux lectures opposees trois contre trois.
    """
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "kl_422.duckdb"))
    monkeypatch.setenv("KLAVIYO_CONVERSION_METRIC_ID", "ABCDEF123")

    body = {"errors": [{"status": "422", "code": "invalid", "detail": "bad interval"}]}
    respx.post(_KLAVIYO_CAMPAIGN_URL).mock(return_value=httpx.Response(422, json=body))
    respx.post(_KLAVIYO_FLOW_URL).mock(return_value=httpx.Response(422, json=body))

    from core.pull_errors import InvalidRequestError, classify_http_error

    with patch("core.nango_client.get_fresh_token", return_value="fake-klaviyo-key"):
        with pytest.raises(InvalidRequestError) as exc_info:
            connector.pull(
                connection_id="conn_kl_test",
                date_from="2026-07-01",
                date_to="2026-07-01",
                project_id="jean-klaviyo",
                pull_id="pull_kl_422",
                conversion_metric_id="ABCDEF123",
            )

    err = exc_info.value
    assert err.error_class == "invalid_request"
    assert err.retryable is False
    assert err.provider_status == 422
    assert err.provider_payload == body
    # What core would have said on its own -- i.e. what the dead key produced.
    assert classify_http_error(422, body).error_class == "invalid_request"
    assert classify_http_error(422, body).retryable is False


# ---------------------------------------------------------------------------
# 429 → RateLimitError (breaker path — does NOT go through classify_http_error)
# ---------------------------------------------------------------------------


@respx.mock
def test_pull_raises_rate_limit_error_on_429(connector, tmp_path, monkeypatch):
    """pull raises RateLimitError('klaviyo', ...) on a 429 response.

    HTTP 429 must NOT reach classify_http_error — it goes through the dedicated
    RateLimitError path so the worker can trip the circuit breaker.
    """
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "kl_429.duckdb"))
    monkeypatch.setenv("KLAVIYO_CONVERSION_METRIC_ID", "ABCDEF123")

    respx.post(_KLAVIYO_CAMPAIGN_URL).mock(
        return_value=httpx.Response(
            429,
            headers={"Retry-After": "30"},
            json={"errors": [{"status": "429", "code": "too_many_requests"}]},
        )
    )
    respx.post(_KLAVIYO_FLOW_URL).mock(
        return_value=httpx.Response(429, headers={"Retry-After": "30"}, json={})
    )

    from core.quota import RateLimitError

    with patch("core.nango_client.get_fresh_token", return_value="fake-klaviyo-key"):
        with pytest.raises(RateLimitError) as exc_info:
            connector.pull(
                connection_id="conn_kl_test",
                date_from="2026-07-01",
                date_to="2026-07-01",
                project_id="jean-klaviyo",
                pull_id="pull_kl_429",
                conversion_metric_id="ABCDEF123",
            )

    assert exc_info.value.platform == "klaviyo"
    assert exc_info.value.retry_after == 30


# ---------------------------------------------------------------------------
# 500 → provider_transient (ProviderTransientError)
# ---------------------------------------------------------------------------


@respx.mock
def test_pull_500_raises_provider_transient(connector, tmp_path, monkeypatch):
    """pull raises ProviderTransientError on HTTP 500 (provider-side failure)."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "kl_500.duckdb"))
    monkeypatch.setenv("KLAVIYO_CONVERSION_METRIC_ID", "ABCDEF123")

    respx.post(_KLAVIYO_CAMPAIGN_URL).mock(
        return_value=httpx.Response(500, json=_JSONAPI_500)
    )
    respx.post(_KLAVIYO_FLOW_URL).mock(
        return_value=httpx.Response(500, json=_JSONAPI_500)
    )

    from core.pull_errors import ProviderTransientError

    with patch("core.nango_client.get_fresh_token", return_value="fake-klaviyo-key"):
        with pytest.raises(ProviderTransientError) as exc_info:
            connector.pull(
                connection_id="conn_kl_test",
                date_from="2026-07-01",
                date_to="2026-07-01",
                project_id="jean-klaviyo",
                pull_id="pull_kl_500",
                conversion_metric_id="ABCDEF123",
            )

    err = exc_info.value
    assert err.error_class == "provider_transient"
    assert err.retryable is True
    assert err.provider_status == 500
    assert "errors" in err.provider_payload


# ---------------------------------------------------------------------------
# Missing KLAVIYO_CONVERSION_METRIC_ID raises ValueError before any HTTP call
# ---------------------------------------------------------------------------


def test_pull_raises_value_error_without_conversion_metric_id(
    connector, tmp_path, monkeypatch
):
    """pull raises ValueError immediately if conversion_metric_id is absent."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "kl_nomid.duckdb"))
    monkeypatch.delenv("KLAVIYO_CONVERSION_METRIC_ID", raising=False)

    with patch("core.nango_client.get_fresh_token", return_value="fake-klaviyo-key"):
        with pytest.raises(ValueError, match="KLAVIYO_CONVERSION_METRIC_ID"):
            connector.pull(
                connection_id="conn_kl_test",
                date_from="2026-07-01",
                date_to="2026-07-01",
                project_id="jean-klaviyo",
                pull_id="pull_kl_nomid",
                conversion_metric_id=None,
            )
