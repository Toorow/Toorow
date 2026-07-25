"""Tests for the Adjust connector pull path (kit epic-25, mocked respx only).

Covers the typed-error paths required by the connector standard (Story 25.2):
  - HTTP 401 raises AuthExpiredError with the provider payload preserved
  - HTTP 429 raises RateLimitError (breaker path, NOT classify_http_error)
  - HTTP 500 raises ProviderTransientError
  - HTTP 204 is a documented EMPTY report (0 rows, not an error)
  - HTTP 200 lands coerced canonical rows in raw_adjust_daily
  - app_token narrows the request via app_token__in (topology selection)

No test contacts the real Adjust API. Real live testing is a human gate
(AI-08 / AI-13 — see catalog_sources/ROLLOUT_NOTES.md).

Adjust documents HTTP statuses only (no error sub-codes); the error body shape
is not contractually specified, so payload preservation is asserted on the raw
body evidence.
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

_REPORT_URL = "https://automate.adjust.com/reports-service/report"
_FILTERS_URL = "https://automate.adjust.com/reports-service/filters_data"

# One representative RS API row: dimension keys as requested, metric values as
# STRINGS (verified contract — dev.adjust.com/en/api/rs-api/reports response
# format), plus the per-row attr_dependency object.
_API_ROW = {
    "attr_dependency": {},
    "day": "2026-07-01",
    "app_token": "abc123def456",
    "app": "Toorow Fitness",
    "network": "AppLovin",
    "campaign_id_network": "cmp-1001",
    "campaign_network": "Summer Push FR",
    "currency_code": "EUR",
    "cost": "1250.5",
    "installs": "64",
    "clicks": "8300",
    "impressions": "191000",
    "sessions": "540",
    "revenue": "310.75",
    "ad_revenue": "42.1",
    "all_revenue": "352.85",
}

_REPORT_200 = {
    "rows": [_API_ROW],
    "totals": {"installs": 64, "cost": 1250.5},
    "warnings": [],
    "pagination": None,
}


def _import_connector():
    spec = importlib.util.spec_from_file_location("connector_adjust", _CONNECTOR_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def connector():
    return _import_connector()


# ---------------------------------------------------------------------------
# 200 → canonical rows landed (coercion + manifest-driven renames)
# ---------------------------------------------------------------------------


@respx.mock
def test_pull_200_lands_coerced_canonical_rows(connector, tmp_path, monkeypatch):
    """A 200 report lands one canonical row with numeric coercion applied."""
    db_path = str(tmp_path / "adjust_200.duckdb")
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    respx.get(_REPORT_URL).mock(return_value=httpx.Response(200, json=_REPORT_200))

    with patch("core.nango_client.get_fresh_token", return_value="fake-adjust-token"):
        result = connector.pull(
            connection_id="conn_adjust_test",
            date_from="2026-07-01",
            date_to="2026-07-01",
            project_id="jean-adjust",
            pull_id="pull_adjust_200",
        )

    assert result == {
        "pull_id": "pull_adjust_200",
        "row_count": 1,
        "date_from": "2026-07-01",
        "date_to": "2026-07-01",
    }

    # Request built from the manifest (AD-2): source tokens, day dimension.
    request = respx.calls.last.request
    query = str(request.url.params)
    assert "dimensions=" in query and "day" in request.url.params["dimensions"]
    assert "campaign_id_network" in request.url.params["dimensions"]
    assert request.url.params["date_period"] == "2026-07-01:2026-07-01"
    assert "app_token__in" not in dict(request.url.params)
    # AD-3: token in header only, never in the URL.
    assert "fake-adjust-token" not in str(request.url)

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    rows = con.execute(
        "SELECT date, app_token, network, campaign_id, campaign_name, cost, "
        "installs, revenue, cost_source_currency, pull_id, project_id "
        "FROM raw_adjust_daily"
    ).fetchall()
    con.close()
    assert rows == [
        (
            "2026-07-01",
            "abc123def456",
            "AppLovin",
            "cmp-1001",
            "Summer Push FR",
            1250.5,
            64,
            310.75,
            "EUR",
            "pull_adjust_200",
            "jean-adjust",
        )
    ]


@respx.mock
def test_pull_scopes_to_selected_app_token(connector, tmp_path, monkeypatch):
    """A selected app (topology selection_level 'app') narrows via app_token__in."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "adjust_scope.duckdb"))

    respx.get(_REPORT_URL).mock(return_value=httpx.Response(200, json=_REPORT_200))

    with patch("core.nango_client.get_fresh_token", return_value="fake-adjust-token"):
        connector.pull_network_daily(
            connection_id="conn_adjust_test",
            date_from="2026-07-01",
            date_to="2026-07-01",
            project_id="jean-adjust",
            pull_id="pull_adjust_scope",
            app_token="abc123def456",
        )

    request = respx.calls.last.request
    assert request.url.params["app_token__in"] == "abc123def456"


# ---------------------------------------------------------------------------
# 204 → documented empty report (0 rows, NOT an error)
# ---------------------------------------------------------------------------


@respx.mock
def test_pull_204_returns_zero_rows(connector, tmp_path, monkeypatch):
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "adjust_204.duckdb"))

    respx.get(_REPORT_URL).mock(return_value=httpx.Response(204))

    with patch("core.nango_client.get_fresh_token", return_value="fake-adjust-token"):
        result = connector.pull(
            connection_id="conn_adjust_test",
            date_from="2026-07-01",
            date_to="2026-07-01",
            project_id="jean-adjust",
            pull_id="pull_adjust_204",
        )

    assert result["row_count"] == 0


# ---------------------------------------------------------------------------
# 401 → AuthExpiredError with payload preserved (Story 25.2 contract)
# ---------------------------------------------------------------------------


@respx.mock
def test_pull_401_raises_auth_expired_with_payload_preserved(
    connector, tmp_path, monkeypatch
):
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "adjust_401.duckdb"))

    body = {"error": "Invalid or missing credentials"}
    respx.get(_REPORT_URL).mock(return_value=httpx.Response(401, json=body))

    from core.pull_errors import AuthExpiredError

    with patch("core.nango_client.get_fresh_token", return_value="expired-token"):
        with pytest.raises(AuthExpiredError) as exc_info:
            connector.pull(
                connection_id="conn_adjust_test",
                date_from="2026-07-01",
                date_to="2026-07-01",
                project_id="jean-adjust",
                pull_id="pull_adjust_401",
            )

    err = exc_info.value
    assert err.error_class == "auth_expired"
    assert err.retryable is False
    assert err.user_action == "reconnect"
    assert err.provider_status == 401
    # Provider payload preserved as evidence.
    assert err.provider_payload == body


# ---------------------------------------------------------------------------
# 429 → RateLimitError (breaker path — never classify_http_error)
# ---------------------------------------------------------------------------


@respx.mock
def test_pull_raises_rate_limit_error_on_429(connector, tmp_path, monkeypatch):
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "adjust_429.duckdb"))

    respx.get(_REPORT_URL).mock(
        return_value=httpx.Response(429, headers={"Retry-After": "30"}, json={})
    )

    from core.quota import RateLimitError

    with patch("core.nango_client.get_fresh_token", return_value="fake-adjust-token"):
        with pytest.raises(RateLimitError) as exc_info:
            connector.pull(
                connection_id="conn_adjust_test",
                date_from="2026-07-01",
                date_to="2026-07-01",
                project_id="jean-adjust",
                pull_id="pull_adjust_429",
            )

    assert exc_info.value.platform == "adjust"
    assert exc_info.value.retry_after == 30


# ---------------------------------------------------------------------------
# 500 → ProviderTransientError
# ---------------------------------------------------------------------------


@respx.mock
def test_pull_500_raises_provider_transient(connector, tmp_path, monkeypatch):
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "adjust_500.duckdb"))

    respx.get(_REPORT_URL).mock(
        return_value=httpx.Response(503, text="Service unavailable")
    )

    from core.pull_errors import ProviderTransientError

    with patch("core.nango_client.get_fresh_token", return_value="fake-adjust-token"):
        with pytest.raises(ProviderTransientError) as exc_info:
            connector.pull(
                connection_id="conn_adjust_test",
                date_from="2026-07-01",
                date_to="2026-07-01",
                project_id="jean-adjust",
                pull_id="pull_adjust_503",
            )

    err = exc_info.value
    assert err.error_class == "provider_transient"
    assert err.retryable is True
    assert err.provider_status == 503
