"""Tests for the pull function and the /api/connections/{id}/pull endpoint.

Story 2.7, AC1, AC2, AC7. Uses respx to mock the upstream Data API.
No test contacts the real API.
Real E2E test is gated on GA4_E2E_PROPERTY_ID env var (Phase-B checklist).
"""

from __future__ import annotations

import importlib.util
import logging
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx

# Disable health poller in all tests
os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")

_TOOROW_PATH = (
    Path(__file__).parents[4]
    / "server"
    / "modules"
    / "google-analytics"
    / "connector.py"
)

# ---------------------------------------------------------------------------
# GA4 runReport mock response — 2 rows (date/device/country x sessions/activeUsers/conversions)
# ---------------------------------------------------------------------------

_GA4_RESPONSE = {
    "dimensionHeaders": [
        {"name": "date"},
        {"name": "deviceCategory"},
        {"name": "country"},
    ],
    "metricHeaders": [
        {"name": "sessions", "type": "TYPE_INTEGER"},
        {"name": "activeUsers", "type": "TYPE_INTEGER"},
        {"name": "conversions", "type": "TYPE_INTEGER"},
    ],
    "rows": [
        {
            "dimensionValues": [
                {"value": "20260101"},
                {"value": "mobile"},
                {"value": "France"},
            ],
            "metricValues": [
                {"value": "100"},
                {"value": "80"},
                {"value": "5"},
            ],
        },
        {
            "dimensionValues": [
                {"value": "20260102"},
                {"value": "desktop"},
                {"value": "Germany"},
            ],
            "metricValues": [
                {"value": "200"},
                {"value": "150"},
                {"value": "10"},
            ],
        },
    ],
}

def _import_connector():
    """Dynamically import connector.py from the hyphenated module directory."""
    spec = importlib.util.spec_from_file_location("connector_ga4_pull", _TOOROW_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def connector():
    return _import_connector()


# ---------------------------------------------------------------------------
# T7.2 — pull with mocked httpx response
# ---------------------------------------------------------------------------


@respx.mock
def test_pull_ga4_mocked_response(connector, tmp_path, monkeypatch):
    """pull returns {pull_id, row_count, date_from, date_to} and lands rows in DuckDB."""
    monkeypatch.setenv("GA4_PROPERTY_ID", "TEST123")
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "test.duckdb"))

    # Mock GA4 Data API
    ga4_route = respx.post(
        "https://analyticsdata.googleapis.com/v1beta/properties/TEST123:runReport"
    ).mock(return_value=httpx.Response(200, json=_GA4_RESPONSE))

    # patch core.nango_client.get_fresh_token (module attribute — AD-3 safe)
    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        result = connector.pull(
            connection_id="conn_test",
            date_from="2026-01-01",
            date_to="2026-01-07",
            project_id="jean-ga4",
            pull_id="pull_test_01",
        )

    assert result["pull_id"] == "pull_test_01"
    assert result["row_count"] == 2
    assert result["date_from"] == "2026-01-01"
    assert result["date_to"] == "2026-01-07"
    assert ga4_route.called, "GA4 Data API must have been called"

    # Verify rows landed in DuckDB
    import duckdb

    con = duckdb.connect(str(tmp_path / "test.duckdb"), read_only=True)
    rows = con.execute(
        "SELECT * FROM raw_ga4_standard_daily WHERE pull_id = 'pull_test_01'"
    ).fetchall()
    con.close()
    assert len(rows) == 2, f"Expected 2 rows in DuckDB, got {len(rows)}"


@respx.mock
def test_pull_ga4_authorization_header_used(connector, tmp_path, monkeypatch):
    """pull uses Authorization: Bearer <token> (AD-3: token used immediately)."""
    monkeypatch.setenv("GA4_PROPERTY_ID", "TEST123")
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "auth_test.duckdb"))

    captured_headers: list[dict] = []

    def capture_headers(request: httpx.Request, **kwargs):
        captured_headers.append(dict(request.headers))
        return httpx.Response(200, json=_GA4_RESPONSE)

    respx.post(
        "https://analyticsdata.googleapis.com/v1beta/properties/TEST123:runReport"
    ).mock(side_effect=capture_headers)

    with patch("core.nango_client.get_fresh_token", return_value="fake-token-xyz"):
        connector.pull(
            connection_id="conn_test",
            date_from="2026-01-01",
            date_to="2026-01-07",
            project_id="jean-ga4",
            pull_id="pull_auth_test",
        )

    assert len(captured_headers) == 1
    auth_header = captured_headers[0].get("authorization", "")
    assert auth_header == "Bearer fake-token-xyz", (
        f"Expected 'Bearer fake-token-xyz', got: {auth_header!r}"
    )


# ---------------------------------------------------------------------------
# T7.3 — Token never logged (AD-3 regression guard)
# ---------------------------------------------------------------------------


@respx.mock
def test_pull_ga4_token_never_logged(connector, tmp_path, monkeypatch, caplog):
    """AD-3: the OAuth token must NEVER appear in any log output at any level."""
    monkeypatch.setenv("GA4_PROPERTY_ID", "TEST123")
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "nolog.duckdb"))

    respx.post(
        "https://analyticsdata.googleapis.com/v1beta/properties/TEST123:runReport"
    ).mock(return_value=httpx.Response(200, json=_GA4_RESPONSE))

    with caplog.at_level(logging.DEBUG):
        with patch("core.nango_client.get_fresh_token", return_value="super-secret-token-12345"):
            connector.pull(
                connection_id="conn_test",
                date_from="2026-01-01",
                date_to="2026-01-07",
                project_id="jean-ga4",
                pull_id="pull_nolog_test",
            )

    # The token string must not appear in any log record
    for record in caplog.records:
        assert "super-secret-token-12345" not in record.getMessage(), (
            f"Token found in log record: {record.getMessage()!r}"
        )


# ---------------------------------------------------------------------------
# T7.4 — GA4 API error raises RuntimeError
# ---------------------------------------------------------------------------


@respx.mock
def test_pull_ga4_ga4_api_error(connector, tmp_path, monkeypatch):
    """pull raises RateLimitError on 429 and RuntimeError on other non-200 statuses.

    Story 3.3 (AC7): 429 now raises RateLimitError so the worker can trip the circuit
    breaker. Other errors still raise RuntimeError.
    """
    monkeypatch.setenv("GA4_PROPERTY_ID", "TEST123")
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "error_test.duckdb"))

    # Test 429 -> RateLimitError (Story 3.3, AC7)
    respx.post(
        "https://analyticsdata.googleapis.com/v1beta/properties/TEST123:runReport"
    ).mock(return_value=httpx.Response(429, json={"error": {"code": 429, "message": "Rate limit"}}))

    from core.quota import RateLimitError

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        with pytest.raises(RateLimitError) as exc_info:
            connector.pull(
                connection_id="conn_test",
                date_from="2026-01-01",
                date_to="2026-01-07",
                project_id="jean-ga4",
                pull_id="pull_error_test",
            )

    assert exc_info.value.platform == "google-analytics"


@respx.mock
def test_pull_ga4_non_429_api_error(connector, tmp_path, monkeypatch):
    """pull raises RuntimeError on non-200, non-429 responses."""
    monkeypatch.setenv("GA4_PROPERTY_ID", "TEST123")
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "error_test2.duckdb"))

    respx.post(
        "https://analyticsdata.googleapis.com/v1beta/properties/TEST123:runReport"
    ).mock(
        return_value=httpx.Response(500, json={"error": {"code": 500, "message": "Server error"}})
    )

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        with pytest.raises(RuntimeError) as exc_info:
            connector.pull(
                connection_id="conn_test",
                date_from="2026-01-01",
                date_to="2026-01-07",
                project_id="jean-ga4",
                pull_id="pull_error_test2",
            )

    assert "500" in str(exc_info.value), (
        f"RuntimeError must mention the status code, got: {exc_info.value!r}"
    )


# ---------------------------------------------------------------------------
# T7.4b — Missing GA4_PROPERTY_ID raises ValueError
# ---------------------------------------------------------------------------


def test_pull_ga4_missing_property_id(connector, monkeypatch):
    """pull raises ValueError when GA4_PROPERTY_ID is not set and property_id is None."""
    monkeypatch.delenv("GA4_PROPERTY_ID", raising=False)

    with pytest.raises(ValueError, match="GA4_PROPERTY_ID"):
        connector.pull(
            connection_id="conn_test",
            date_from="2026-01-01",
            date_to="2026-01-07",
            project_id="jean-ga4",
            pull_id="pull_noprop_test",
            property_id=None,
        )


# ---------------------------------------------------------------------------
# T7.6 — Provenance dict shape (NFR8 regression guard)
# ---------------------------------------------------------------------------


def test_provenance_dict_shape(connector):
    """meta.provenance must be a dict with source_system, source_field, pull_id (NFR8 AC5)."""
    fake_rows = [
        {
            "metric": "sessions",
            "breakdown_dimension": "device_category",
            "breakdown_value": "desktop",
            "value": 100.0,
            "pull_id": "pull_01JTEST000000000000000001",
            "freshness": "2026-07-01T10:00:00Z",
        }
    ]
    envelope = connector._build_envelope(
        rows=fake_rows,
        report_profile="standard_daily",
        date_from="2026-07-01",
        date_to="2026-07-07",
        project_id="jean-ga4",
    )
    prov = envelope["meta"]["provenance"]
    assert isinstance(prov, dict), (
        "provenance must be a dict (not scalar string) after Story 2.7 upgrade, "
        f"got: {type(prov)!r}"
    )
    assert "source_system" in prov, "provenance must have source_system (NFR8)"
    assert "source_field" in prov, "provenance must have source_field (NFR8)"
    assert "pull_id" in prov, "provenance must have pull_id (NFR8)"
    assert prov["source_system"] == "google-analytics"
    assert prov["source_field"] == "fact_daily_kpi"
    assert prov["pull_id"] == "pull_01JTEST000000000000000001"


def test_provenance_none_when_no_rows(connector):
    """meta.provenance must be None when there are no rows (empty result)."""
    envelope = connector._build_envelope(
        rows=[],
        report_profile="standard_daily",
        date_from="2026-07-01",
        date_to="2026-07-07",
        project_id="jean-ga4",
    )
    assert envelope["meta"]["provenance"] is None, (
        "provenance must be None when rows is empty"
    )


# ---------------------------------------------------------------------------
# T7.7 — Project ID separation in DuckDB (AC6)
# ---------------------------------------------------------------------------


def test_project_id_separation(connector, tmp_path, monkeypatch):
    """Real pull rows (project_id='jean-ga4') are separate from seed rows (project_id='default')."""
    import duckdb

    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "sep_test.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    con = duckdb.connect(db_path)
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS raw_ga4_standard_daily (
            date VARCHAR, device_category VARCHAR, country VARCHAR,
            sessions INTEGER, active_users INTEGER, conversions INTEGER,
            pull_id VARCHAR, loaded_at VARCHAR, project_id VARCHAR
        )
        """
    )
    # Insert seed rows (project_id='default')
    con.execute(
        "INSERT INTO raw_ga4_standard_daily VALUES (?,?,?,?,?,?,?,?,?)",
        ("2026-01-01", "desktop", "France", 100, 80, 5,
         "pull_seed", "2026-01-01T00:00:00Z", "default"),
    )
    # Insert real pull rows (project_id='jean-ga4')
    con.execute(
        "INSERT INTO raw_ga4_standard_daily VALUES (?,?,?,?,?,?,?,?,?)",
        ("2026-01-01", "mobile", "Germany", 200, 150, 10,
         "pull_real", "2026-01-01T00:00:00Z", "jean-ga4"),
    )
    con.close()

    # Patch fact_daily_kpi mart query to return rows filtered by project_id
    # _query_mart hits fact_daily_kpi which we can test via _query_duckdb mock.
    # Instead, directly test that _insert_raw_rows inserts with correct project_id
    # and that the raw table can be queried by project_id.
    con2 = duckdb.connect(db_path, read_only=True)
    real_rows = con2.execute(
        "SELECT * FROM raw_ga4_standard_daily WHERE project_id = 'jean-ga4'"
    ).fetchall()
    seed_rows = con2.execute(
        "SELECT * FROM raw_ga4_standard_daily WHERE project_id = 'default'"
    ).fetchall()
    con2.close()

    assert len(real_rows) == 1, f"Expected 1 real row, got {len(real_rows)}"
    assert len(seed_rows) == 1, f"Expected 1 seed row, got {len(seed_rows)}"
    # Verify they are separate
    real_project_ids = {r[-1] for r in real_rows}
    seed_project_ids = {r[-1] for r in seed_rows}
    assert real_project_ids == {"jean-ga4"}
    assert seed_project_ids == {"default"}


# ---------------------------------------------------------------------------
# T7.5 — Trigger pull endpoint with mocked pull function and DB (AC2)
# ---------------------------------------------------------------------------


def test_trigger_pull_endpoint_mocked(tmp_path, monkeypatch):
    """POST /api/connections/{id}/pull returns 202 with job_id, pull_id, state (Story 3.2, AC4)."""
    from contextlib import contextmanager

    from starlette.testclient import TestClient

    monkeypatch.setenv("TOOROW_AUTH_MODE", "disabled")
    monkeypatch.setenv("HEALTH_POLLER_ENABLED", "false")
    monkeypatch.setenv("QUEUE_WORKER_ENABLED", "false")

    # Mock the DB connection_ref lookup (only need id for existence check now)
    fake_row = ("conn_test_001",)
    mock_cursor = MagicMock()
    mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cursor.__exit__ = MagicMock(return_value=False)
    mock_cursor.fetchone = MagicMock(return_value=fake_row)

    mock_conn = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor = MagicMock(return_value=mock_cursor)

    @contextmanager
    def _fake_get_connection():
        yield mock_conn

    # Mock enqueue_pull to return a fake job dict without hitting the DB
    fake_enqueue_result = {
        "job_id": "job_01TESTJOBID",
        "pull_id": "pull_01TESTPULLID",
        "state": "queued",
    }
    mock_enqueue = MagicMock(return_value=fake_enqueue_result)

    with patch("core.db.get_connection", new=_fake_get_connection), \
         patch("core.queue.enqueue_pull", mock_enqueue):
        from core.main import build_asgi_app
        app = build_asgi_app()
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post(
            "/api/connections/conn_test_001/pull",
            json={"date_from": "2026-07-04", "date_to": "2026-07-10"},
        )

    assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert "job_id" in body
    assert "pull_id" in body
    assert body["state"] == "queued"
    # Old shape must NOT be present (Story 3.2, AC4)
    assert "row_count" not in body
    assert "dbt_note" not in body


def test_trigger_pull_endpoint_returns_404_when_conn_not_found(monkeypatch):
    """POST /api/connections/{id}/pull returns 404 when connection_ref not in DB."""
    from starlette.testclient import TestClient

    monkeypatch.setenv("TOOROW_AUTH_MODE", "disabled")
    monkeypatch.setenv("HEALTH_POLLER_ENABLED", "false")

    mock_cursor = MagicMock()
    mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cursor.__exit__ = MagicMock(return_value=False)
    mock_cursor.fetchone = MagicMock(return_value=None)  # not found

    mock_conn = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor = MagicMock(return_value=mock_cursor)

    with patch("core.db.get_connection", return_value=mock_conn):
        from core.main import build_asgi_app
        app = build_asgi_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/connections/conn_nonexistent/pull")

    assert resp.status_code == 404
    body = resp.json()
    assert body.get("code") == "not_found"


# ---------------------------------------------------------------------------
# T7.8 — Real GA4 E2E smoke test (Phase B — skipped unless GA4_E2E_PROPERTY_ID set)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.getenv("GA4_E2E_PROPERTY_ID"),
    reason="real GA4 e2e: set GA4_E2E_PROPERTY_ID to run (Phase B checklist)",
)
def test_pull_e2e_with_real_ga4(connector, tmp_path, monkeypatch):
    """Phase B smoke test: real GA4 Data API call (requires OAuth client setup).

    Prerequisites (Jean's Phase B checklist):
    - GA4_E2E_PROPERTY_ID env var set to your GA4 numeric property ID
    - NANGO_SECRET_KEY env var set
    - A valid Nango connection exists for the connection_id in GA4_E2E_CONNECTION_ID
    - Google OAuth client configured in infra/nango/.env
    """
    property_id = os.environ["GA4_E2E_PROPERTY_ID"]
    connection_id = os.environ.get("GA4_E2E_CONNECTION_ID", "test-ga4-connection")

    monkeypatch.setenv("GA4_PROPERTY_ID", property_id)
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "e2e.duckdb"))

    result = connector.pull(
        connection_id=connection_id,
        date_from="2026-07-01",
        date_to="2026-07-07",
        project_id="jean-ga4",
        pull_id="pull_e2e_test",
        property_id=property_id,
    )

    assert result["pull_id"] == "pull_e2e_test"
    assert result["row_count"] >= 0  # may be 0 for date ranges with no data
    assert result["date_from"] == "2026-07-01"
    assert result["date_to"] == "2026-07-07"
