"""Tests for admin_api context events REST endpoints (Story 4.3, AC4, AC8 / T9.3).

Covers:
  - POST /api/context-events: 201 on valid input.
  - POST /api/context-events: 422 on label > 120 chars.
  - POST /api/context-events: 401 on missing auth (static mode).
  - GET /api/context-events: returns event list filtered by project_id.
  - GET /api/context-events: 400 on missing project_id.
  - GET /api/context-events: 401 on missing auth (static mode).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient

# ---------------------------------------------------------------------------
# Test app factory
# ---------------------------------------------------------------------------


def _make_app():
    """Create a minimal Starlette test app mounting only the admin_api router."""
    from core.admin_api import router

    app = Starlette(
        routes=[Mount("/", app=router)],
    )
    return app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

import pytest  # noqa: E402


@pytest.fixture()
def client():
    """Test client with auth disabled (default TOOROW_AUTH_MODE=disabled)."""
    os.environ["TOOROW_AUTH_MODE"] = "disabled"
    from core import api_auth

    api_auth.reset_verifier_cache()
    app = _make_app()
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    # Restore
    os.environ.pop("TOOROW_AUTH_MODE", None)
    api_auth.reset_verifier_cache()


@pytest.fixture()
def client_auth_required():
    """Test client with static auth mode (any Bearer token required)."""
    os.environ["TOOROW_AUTH_MODE"] = "static"
    os.environ["TOOROW_STATIC_TOKEN"] = "test-token-abc"
    from core import api_auth

    api_auth.reset_verifier_cache()
    app = _make_app()
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    os.environ.pop("TOOROW_AUTH_MODE", None)
    os.environ.pop("TOOROW_STATIC_TOKEN", None)
    api_auth.reset_verifier_cache()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_ts():
    return datetime(2026, 7, 4, 12, 0, 0, tzinfo=timezone.utc)


def _make_mock_db_conn(cursor=None):
    """Create a mock psycopg connection context manager."""
    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    if cursor is not None:
        conn.cursor.return_value = cursor
    return conn


def _make_mock_cursor(fetchone_result=None, fetchall_result=None, description=None):
    """Create a mock psycopg cursor context manager."""
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    if fetchone_result is not None:
        cur.fetchone.return_value = fetchone_result
    if fetchall_result is not None:
        cur.fetchall.return_value = fetchall_result
    if description is not None:
        cur.description = description
    return cur


# ---------------------------------------------------------------------------
# POST /api/context-events tests
# ---------------------------------------------------------------------------


class TestPostContextEvents:
    """POST /api/context-events endpoint tests."""

    def test_create_event_201_valid_input(self, client):
        """Valid input returns 201 with the created event."""
        from datetime import date

        fake_ts = _fake_ts()
        fake_date = date(2026, 7, 4)

        # RETURNING: id, project_id, event_date, type, label, created_at
        fake_row = ("evt_01", "proj_test", fake_date, "business", "Lancement campagne", fake_ts)
        fake_desc = [
            ("id",),
            ("project_id",),
            ("event_date",),
            ("type",),
            ("label",),
            ("created_at",),
        ]

        mock_cur = _make_mock_cursor(fetchone_result=fake_row, description=fake_desc)
        mock_conn = _make_mock_db_conn(cursor=mock_cur)

        import core.db as db_module

        class FakeConnCtx:
            def __enter__(self):
                return mock_conn

            def __exit__(self, *args):
                return False

        with (
            patch.object(db_module, "get_connection", return_value=FakeConnCtx()),
            patch("core.admin_api.write_audit_row"),
        ):
            resp = client.post(
                "/api/context-events",
                content=json.dumps(
                    {
                        "project_id": "proj_test",
                        "event_date": "2026-07-04",
                        "type": "business",
                        "label": "Lancement campagne",
                    }
                ),
                headers={"content-type": "application/json"},
            )

        assert resp.status_code == 201
        data = resp.json()
        assert data.get("id") == "evt_01"
        assert data.get("project_id") == "proj_test"
        assert data.get("type") == "business"
        assert data.get("label") == "Lancement campagne"

    def test_create_event_422_label_too_long(self, client):
        """Label > 120 chars returns 422 with French error."""
        long_label = "a" * 121
        resp = client.post(
            "/api/context-events",
            content=json.dumps(
                {
                    "project_id": "proj_test",
                    "event_date": "2026-07-04",
                    "type": "business",
                    "label": long_label,
                }
            ),
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 422
        data = resp.json()
        assert "error" in data
        assert "120" in data["error"]

    def test_create_event_422_invalid_date(self, client):
        """Invalid date format returns 422."""
        resp = client.post(
            "/api/context-events",
            content=json.dumps(
                {
                    "project_id": "proj_test",
                    "event_date": "not-a-date",
                    "type": "business",
                    "label": "Valid label",
                }
            ),
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 422

    def test_create_event_400_missing_required_fields(self, client):
        """Missing required fields return 400."""
        resp = client.post(
            "/api/context-events",
            content=json.dumps({"project_id": "proj_test"}),
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 400

    def test_create_event_401_no_auth_in_static_mode(self, client_auth_required):
        """No Bearer token in static mode returns 401."""
        resp = client_auth_required.post(
            "/api/context-events",
            content=json.dumps(
                {
                    "project_id": "proj_test",
                    "event_date": "2026-07-04",
                    "type": "business",
                    "label": "Valid label",
                }
            ),
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /api/context-events tests
# ---------------------------------------------------------------------------


class TestGetContextEvents:
    """GET /api/context-events endpoint tests."""

    def test_list_events_200_with_project_id(self, client):
        """Valid project_id returns 200 with events list."""
        from datetime import date

        fake_ts = _fake_ts()
        fake_date = date(2026, 7, 4)

        fake_rows = [
            (
                "evt_01",
                "proj_test",
                fake_date,
                "business",
                "Event 1",
                "Details",
                "user1",
                fake_ts,
            )
        ]
        fake_desc = [
            ("id",),
            ("project_id",),
            ("event_date",),
            ("type",),
            ("label",),
            ("description",),
            ("created_by",),
            ("created_at",),
        ]

        mock_cur = _make_mock_cursor(fetchall_result=fake_rows, description=fake_desc)
        mock_conn = _make_mock_db_conn(cursor=mock_cur)

        import core.db as db_module

        class FakeConnCtx:
            def __enter__(self):
                return mock_conn

            def __exit__(self, *args):
                return False

        with patch.object(db_module, "get_connection", return_value=FakeConnCtx()):
            resp = client.get("/api/context-events?project_id=proj_test")

        assert resp.status_code == 200
        data = resp.json()
        assert "events" in data
        assert len(data["events"]) == 1
        assert data["events"][0]["id"] == "evt_01"
        assert data["events"][0]["project_id"] == "proj_test"

    def test_list_events_400_missing_project_id(self, client):
        """Missing project_id query param returns 400."""
        resp = client.get("/api/context-events")
        assert resp.status_code == 400
        data = resp.json()
        assert "project_id" in data.get("message", "").lower()

    def test_list_events_401_no_auth_in_static_mode(self, client_auth_required):
        """No Bearer token in static mode returns 401."""
        resp = client_auth_required.get("/api/context-events?project_id=proj_test")
        assert resp.status_code == 401

    def test_list_events_filters_by_project_id(self, client):
        """Query is scoped to project_id (DB receives correct param)."""
        fake_rows: list = []
        fake_desc = [
            ("id",),
            ("project_id",),
            ("event_date",),
            ("type",),
            ("label",),
            ("description",),
            ("created_by",),
            ("created_at",),
        ]

        mock_cur = _make_mock_cursor(fetchall_result=fake_rows, description=fake_desc)
        mock_conn = _make_mock_db_conn(cursor=mock_cur)

        import core.db as db_module

        class FakeConnCtx:
            def __enter__(self):
                return mock_conn

            def __exit__(self, *args):
                return False

        with patch.object(db_module, "get_connection", return_value=FakeConnCtx()):
            resp = client.get("/api/context-events?project_id=proj_specific")

        assert resp.status_code == 200
        data = resp.json()
        assert data["events"] == []
        # Verify the execute was called with project_id as first param
        mock_cur.execute.assert_called_once()
        call_args = mock_cur.execute.call_args
        assert "proj_specific" in call_args[0][1]  # second arg is the params tuple
