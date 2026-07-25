"""Tests for GET /api/connections -- Fix [MEDIUM/spec #10]: active_datastream_count field.

Tests:
  - GET /api/connections includes active_datastream_count for each connection
  - active_datastream_count is 0 when no enabled datastreams
  - active_datastream_count is correct when enabled datastreams exist

Strategy:
  - All DB calls mocked -- no real Postgres required.
  - SCHEDULER_ENABLED=false, HEALTH_POLLER_ENABLED=false.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")


# Connection row shape after fix #10:
# id, provider, nango_connection_id, project_id, created_at,
# health_status, last_checked_at, last_fetched_at, active_datastream_count
_CONN_COLS = [
    "id", "provider", "nango_connection_id", "project_id", "created_at",
    "health_status", "last_checked_at", "last_fetched_at", "active_datastream_count",
]

_NOW = datetime(2026, 7, 13, 10, 0, 0, tzinfo=timezone.utc)


def _make_conn_rows(active_count: int = 2):
    return [(
        "conn_TEST001",      # id
        "google-analytics",  # provider
        "nango_ga_001",      # nango_connection_id
        "proj_a",            # project_id
        _NOW,                # created_at
        "active",            # health_status
        _NOW,                # last_checked_at
        _NOW,                # last_fetched_at
        active_count,        # active_datastream_count
    )]


def _make_db_mock(rows, cols=_CONN_COLS):
    col_descs = [(c,) for c in cols]

    mock_cursor = MagicMock()
    mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cursor.__exit__ = MagicMock(return_value=False)
    mock_cursor.fetchall = MagicMock(return_value=rows)
    mock_cursor.description = [type("D", (), {"__getitem__": staticmethod(
        lambda i, c=c: c[0] if i == 0 else c)})() for c in col_descs]

    mock_conn = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor = MagicMock(return_value=mock_cursor)

    @contextmanager
    def _fake_get_connection():
        yield mock_conn

    return _fake_get_connection


def _build_client():
    from core.main import build_asgi_app
    from starlette.testclient import TestClient

    app = build_asgi_app()
    return TestClient(app, raise_server_exceptions=False)


class TestListConnectionsActiveDatastreamCount:
    """Tests for the active_datastream_count field in GET /api/connections."""

    def test_active_datastream_count_present_in_response(self):
        """Fix [#10]: GET /api/connections response includes active_datastream_count."""
        fake_db = _make_db_mock(_make_conn_rows(active_count=3))

        with patch("core.db.get_connection", new=fake_db):
            client = _build_client()
            response = client.get("/api/connections")

        assert response.status_code == 200
        data = response.json()
        assert "connections" in data
        assert len(data["connections"]) == 1
        conn = data["connections"][0]
        assert "active_datastream_count" in conn, (
            "active_datastream_count must be in the connection response"
        )
        assert conn["active_datastream_count"] == 3

    def test_active_datastream_count_zero_when_no_streams(self):
        """active_datastream_count is 0 when connection has no enabled datastreams."""
        fake_db = _make_db_mock(_make_conn_rows(active_count=0))

        with patch("core.db.get_connection", new=fake_db):
            client = _build_client()
            response = client.get("/api/connections")

        assert response.status_code == 200
        conn = response.json()["connections"][0]
        assert conn["active_datastream_count"] == 0

    def test_multiple_connections_each_has_count(self):
        """Each connection in the list has its own active_datastream_count."""
        rows = [
            ("conn_A001", "google-analytics", "nango_a", "proj_a", _NOW,
             "active", _NOW, _NOW, 2),
            ("conn_B002", "meta-ads", "nango_b", "proj_a", _NOW,
             None, None, None, 0),
        ]
        fake_db = _make_db_mock(rows)

        with patch("core.db.get_connection", new=fake_db):
            client = _build_client()
            response = client.get("/api/connections")

        assert response.status_code == 200
        conns = response.json()["connections"]
        assert len(conns) == 2
        counts = {c["id"]: c["active_datastream_count"] for c in conns}
        assert counts["conn_A001"] == 2
        assert counts["conn_B002"] == 0
