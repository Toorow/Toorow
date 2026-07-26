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


# Project-scoped Sources read-model row.
_CONN_COLS = [
    "id", "provider", "nango_connection_id", "project_id", "created_at",
    "status", "auth_path", "owner_org_id", "token_expiry", "owner_org_name",
    "viewer_org_id", "account_label", "account_state", "health_status",
    "last_checked_at", "last_fetched_at", "active_datastream_count",
    "provided_to_viewer", "has_outgoing_grant", "caller_manages_owner",
]

_NOW = datetime(2026, 7, 13, 10, 0, 0, tzinfo=timezone.utc)


def _conn_row(
    connection_id: str = "conn_TEST001",
    provider: str = "google-analytics",
    nango_id: str = "nango_ga_001",
    active_count: int = 2,
    health_status: str | None = "ok",
):
    return (
        connection_id, provider, nango_id, "proj_owner", _NOW,
        "active", "nango", "org_a", None, "Real Org", "org_a",
        "Real account", "ready", health_status, _NOW, _NOW, active_count,
        False, False, True,
    )


def _make_conn_rows(active_count: int = 2):
    return [_conn_row(active_count=active_count)]

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

    _fake_get_connection.cursor = mock_cursor
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

        with (
            patch("core.db.get_connection", new=fake_db),
            patch("core.project_access.identity_can_access_project_in_org", return_value=True),
        ):
            client = _build_client()
            response = client.get("/api/connections?project_id=proj_a")

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

        with (
            patch("core.db.get_connection", new=fake_db),
            patch("core.project_access.identity_can_access_project_in_org", return_value=True),
        ):
            client = _build_client()
            response = client.get("/api/connections?project_id=proj_a")

        assert response.status_code == 200
        conn = response.json()["connections"][0]
        assert conn["active_datastream_count"] == 0

    def test_multiple_connections_each_has_count(self):
        """Each connection in the list has its own active_datastream_count."""
        rows = [
            _conn_row("conn_A001", "google-analytics", "nango_a", 2),
            _conn_row("conn_B002", "meta-ads", "nango_b", 0, None),
        ]
        fake_db = _make_db_mock(rows)

        with (
            patch("core.db.get_connection", new=fake_db),
            patch("core.project_access.identity_can_access_project_in_org", return_value=True),
        ):
            client = _build_client()
            response = client.get("/api/connections?project_id=proj_a")

        assert response.status_code == 200
        conns = response.json()["connections"]
        assert len(conns) == 2
        counts = {c["id"]: c["active_datastream_count"] for c in conns}
        assert counts["conn_A001"] == 2
        assert counts["conn_B002"] == 0
class TestSourcesProjectScope:
    def test_datastream_count_is_scoped_to_the_requested_project(self):
        fake_db = _make_db_mock(_make_conn_rows(active_count=1))
        with (
            patch("core.db.get_connection", new=fake_db),
            patch("core.project_access.identity_can_access_project_in_org", return_value=True),
        ):
            response = _build_client().get("/api/connections?project_id=proj_a")

        assert response.status_code == 200
        sql, params = fake_db.cursor.execute.call_args.args
        assert "ds.project_id = %s" in sql
        assert params == ("proj_a", "anonymous", "proj_a")
    def test_project_id_is_required(self):
        client = _build_client()
        response = client.get("/api/connections")
        assert response.status_code == 400
        assert response.json()["code"] == "missing_project"

    def test_provided_credential_is_read_only_and_keeps_real_account_label(self):
        row = list(_conn_row())
        row[7] = "org_owner"
        row[10] = "org_viewer"
        row[11] = "Provider account 42"
        row[17] = True
        row[19] = True
        fake_db = _make_db_mock([tuple(row)])

        with (
            patch("core.db.get_connection", new=fake_db),
            patch("core.project_access.identity_can_access_project_in_org", return_value=True),
        ):
            response = _build_client().get("/api/connections?project_id=proj_viewer")

        assert response.status_code == 200
        connection = response.json()["connections"][0]
        assert connection["account_label"] == "Provider account 42"
        assert connection["exposure"] == "provided_by_org"
        assert connection["can_manage"] is False

    def test_database_failure_never_falls_back_to_global_nango_connections(self):
        @contextmanager
        def unavailable_db():
            raise RuntimeError("offline")
            yield  # pragma: no cover

        with (
            patch("core.db.get_connection", new=unavailable_db),
            patch("core.project_access.identity_can_access_project_in_org", return_value=True),
            patch("core.admin_api.nango_client._list_connections_async") as nango_list,
        ):
            response = _build_client().get("/api/connections?project_id=proj_a")

        assert response.status_code == 503
        assert response.json()["code"] == "db_error"
        nango_list.assert_not_called()
