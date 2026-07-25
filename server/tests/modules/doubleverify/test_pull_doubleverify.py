"""Tests for the DoubleVerify connector pull path (kit epic-25, mocked respx only).

Covers the async 3-step DV Data API contract (Data Request -> Poll Status ->
Data Download CSV) and the typed-error paths required by the connector standard
(Story 25.2):
  - the 3-step happy path lands canonical long-format rows in raw_doubleverify_daily
  - AD-4: a '*_rate' column in the CSV is DROPPED (never stored as a metric)
  - HTTP 401 on the Data Request raises AuthExpiredError with payload preserved
  - HTTP 429 raises RateLimitError (breaker path, NOT classify_http_error)
  - HTTP 503 raises ProviderTransientError

No test contacts the real DoubleVerify API. DV's request/response JSON and base
URL are behind the developer-portal login, so the exact wire contract remains a
human live gate (AI-13 — see catalog_sources/ROLLOUT_NOTES.md). These mocks
encode the DOCUMENTED async shape and MUST be re-verified in the live pass.
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
    Path(__file__).parents[4] / "server" / "modules" / "doubleverify" / "connector.py"
)

_BASE = "https://api.doubleverify.com/data/v1"
_REQUEST_URL = f"{_BASE}/reports"
_REQUEST_ID = "req_abc123"
_STATUS_URL = f"{_BASE}/reports/{_REQUEST_ID}/status"
_DATA_URL = f"{_BASE}/reports/{_REQUEST_ID}/data"

# CSV as the DV Data Download returns it: header row + one data row. Includes a
# '*_rate' column (viewable_rate) that transform() must DROP (AD-4).
_CSV = (
    "date,advertiser_name,campaign,monitored_ads,measured_impressions,"
    "viewable_impressions,viewable_rate,fraud_sivt_incidents\n"
    "2026-07-01,Acme Corp,Summer Brand Push,100000,95000,72000,0.7579,1200\n"
)


def _import_connector():
    spec = importlib.util.spec_from_file_location("connector_doubleverify", _CONNECTOR_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def connector():
    return _import_connector()


# ---------------------------------------------------------------------------
# Happy path: request -> poll (ready) -> download CSV -> canonical long rows
# ---------------------------------------------------------------------------


@respx.mock
def test_pull_three_step_lands_rows_and_drops_rate(connector, tmp_path, monkeypatch):
    db_path = str(tmp_path / "dv_200.duckdb")
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    respx.post(_REQUEST_URL).mock(
        return_value=httpx.Response(200, json={"id": _REQUEST_ID})
    )
    respx.get(_STATUS_URL).mock(
        return_value=httpx.Response(200, json={"status": "ready"})
    )
    respx.get(_DATA_URL).mock(
        return_value=httpx.Response(200, text=_CSV, headers={"Content-Type": "text/csv"})
    )

    with patch("core.nango_client.get_fresh_token", return_value="fake-dv-hash"):
        result = connector.pull(
            connection_id="conn_dv_test",
            date_from="2026-07-01",
            date_to="2026-07-01",
            project_id="jean-dv",
            pull_id="pull_dv_200",
        )

    # 1 wide row -> 4 additive metrics (viewable_rate dropped): 4 long rows.
    assert result == {
        "pull_id": "pull_dv_200",
        "row_count": 4,
        "date_from": "2026-07-01",
        "date_to": "2026-07-01",
    }

    # AD-3: the token is a Bearer header, never in the URL.
    req = respx.calls[0].request
    assert req.headers["Authorization"] == "Bearer fake-dv-hash"
    assert "fake-dv-hash" not in str(req.url)
    # Data Download requests CSV explicitly.
    assert respx.calls[-1].request.headers["Accept"] == "text/csv"

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    metrics = {
        r[0]: r[1]
        for r in con.execute(
            "SELECT metric, value FROM raw_doubleverify_daily ORDER BY metric"
        ).fetchall()
    }
    breakdowns = con.execute(
        "SELECT DISTINCT breakdown_dimension, breakdown_value FROM raw_doubleverify_daily"
    ).fetchall()
    con.close()

    # AD-4: the ratio never lands as a stored metric.
    assert "viewable_rate" not in metrics
    assert metrics == {
        "monitored_ads": 100000.0,
        "measured_impressions": 95000.0,
        "viewable_impressions": 72000.0,
        "fraud_sivt_incidents": 1200.0,
    }
    # Most-specific present dimension (campaign) is the breakdown pair.
    assert breakdowns == [("campaign", "Summer Brand Push")]


# ---------------------------------------------------------------------------
# transform(): AD-4 rate/mean drop + identity rename (unit)
# ---------------------------------------------------------------------------


def test_transform_drops_all_ratio_shapes(connector):
    rows = connector.transform(
        [
            {
                "date": "2026-07-01",
                "campaign": "C1",
                "monitored_ads": 10,
                "viewable_rate": 0.5,
                "rate_100_percent_display_viewable": 0.4,
                "average_time_s_display_viewable_impressions": 3.2,
                "fraud_sivt_incidents": 2,
            }
        ]
    )
    assert rows == [
        {"date": "2026-07-01", "campaign": "C1", "monitored_ads": 10,
         "fraud_sivt_incidents": 2}
    ]


# ---------------------------------------------------------------------------
# 401 -> AuthExpiredError with payload preserved (Story 25.2 contract)
# ---------------------------------------------------------------------------


@respx.mock
def test_pull_401_raises_auth_expired_with_payload(connector, tmp_path, monkeypatch):
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "dv_401.duckdb"))

    body = {"error": "Invalid access token hash"}
    respx.post(_REQUEST_URL).mock(return_value=httpx.Response(401, json=body))

    from core.pull_errors import AuthExpiredError

    with patch("core.nango_client.get_fresh_token", return_value="expired-hash"):
        with pytest.raises(AuthExpiredError) as exc_info:
            connector.pull(
                connection_id="conn_dv_test",
                date_from="2026-07-01",
                date_to="2026-07-01",
                project_id="jean-dv",
                pull_id="pull_dv_401",
            )

    err = exc_info.value
    assert err.error_class == "auth_expired"
    assert err.provider_status == 401
    assert err.provider_payload == body


# ---------------------------------------------------------------------------
# 429 -> RateLimitError (breaker path, never classify_http_error)
# ---------------------------------------------------------------------------


@respx.mock
def test_pull_429_raises_rate_limit(connector, tmp_path, monkeypatch):
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "dv_429.duckdb"))

    respx.post(_REQUEST_URL).mock(
        return_value=httpx.Response(429, headers={"Retry-After": "30"}, json={})
    )

    from core.quota import RateLimitError

    with patch("core.nango_client.get_fresh_token", return_value="fake-dv-hash"):
        with pytest.raises(RateLimitError) as exc_info:
            connector.pull(
                connection_id="conn_dv_test",
                date_from="2026-07-01",
                date_to="2026-07-01",
                project_id="jean-dv",
                pull_id="pull_dv_429",
            )

    assert exc_info.value.platform == "doubleverify"
    assert exc_info.value.retry_after == 30


# ---------------------------------------------------------------------------
# 503 -> ProviderTransientError
# ---------------------------------------------------------------------------


@respx.mock
def test_pull_503_raises_provider_transient(connector, tmp_path, monkeypatch):
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "dv_503.duckdb"))

    respx.post(_REQUEST_URL).mock(
        return_value=httpx.Response(503, text="Service unavailable")
    )

    from core.pull_errors import ProviderTransientError

    with patch("core.nango_client.get_fresh_token", return_value="fake-dv-hash"):
        with pytest.raises(ProviderTransientError) as exc_info:
            connector.pull(
                connection_id="conn_dv_test",
                date_from="2026-07-01",
                date_to="2026-07-01",
                project_id="jean-dv",
                pull_id="pull_dv_503",
            )

    assert exc_info.value.error_class == "provider_transient"
    assert exc_info.value.provider_status == 503
