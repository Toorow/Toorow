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
            patch("core.context_events_api.insert_audit_row"),
        ):
            resp = client.post(
                "/api/context-events?project_id=proj_test",
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
            "/api/context-events?project_id=proj_test",
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
            "/api/context-events?project_id=proj_test",
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
            "/api/context-events?project_id=proj_test",
            content=json.dumps({"project_id": "proj_test"}),
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 400

    def test_create_event_401_no_auth_in_static_mode(self, client_auth_required):
        """No Bearer token in static mode returns 401."""
        resp = client_auth_required.post(
            "/api/context-events?project_id=proj_test",
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
        # `version_history` flipped to true with migration 328: the server now
        # answers the earlier wordings of any annotation it lists, and an event
        # nobody corrected answers with an empty history rather than with an
        # absent feature. `usage` is still false, and still says so.
        assert data["capabilities"] == {
            "can_write": True,
            "version_history": True,
            "usage": False,
        }
        # The per-row dict carries the top-level one PLUS the decision that is
        # per-row by nature: migration 286 lets a human correct a manual, live
        # event and nothing else, so `can_correct` cannot be a project-wide fact.
        # This mocked row carries no `source` column (the legacy 8-column shape),
        # which the handler reads as manual -- migration 055's own default.
        assert data["events"][0]["capabilities"] == {
            **data["capabilities"],
            "can_correct": True,
        }

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
        # Verify the LISTING query was scoped to project_id. It is the LAST
        # execute, not the only one: the disabled-auth gate now runs an
        # existence probe first, so that an unknown project id answers 404
        # instead of a 200 carrying an empty list (live finding C1).
        executes = mock_cur.execute.call_args_list
        assert "FROM app.projects" in executes[0].args[0]
        assert "FROM app.context_events" in executes[-1].args[0]
        assert "proj_specific" in executes[-1].args[1]  # second arg is the params tuple

# ---------------------------------------------------------------------------
# Epic 43.22 -- strict project authorization
# ---------------------------------------------------------------------------


def _authorized_headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-token-abc", "content-type": "application/json"}


def test_context_events_get_cross_scope_is_404_without_read(client_auth_required):
    conn = _make_mock_db_conn(cursor=_make_mock_cursor(fetchall_result=[]))

    class ConnCtx:
        def __enter__(self):
            return conn

        def __exit__(self, *args):
            return False

    with (
        patch("core.db.get_connection", return_value=ConnCtx()),
        patch("core.api_auth.authenticate_api_request", return_value=(True, "person_EXAMPLE")),
        patch("core.admin_api._strict_project_capability_allowed", return_value=False) as guard,
    ):
        response = client_auth_required.get(
            "/api/context-events?project_id=foreign_project",
            headers=_authorized_headers(),
        )

    assert response.status_code == 404
    conn.cursor.assert_not_called()
    assert guard.call_args.kwargs["minimum_capability"] == "view"


def test_context_events_post_cross_scope_is_404_without_mutation_or_audit(
    client_auth_required,
):
    conn = _make_mock_db_conn(cursor=_make_mock_cursor())

    class ConnCtx:
        def __enter__(self):
            return conn

        def __exit__(self, *args):
            return False

    with (
        patch("core.db.get_connection", return_value=ConnCtx()),
        patch("core.api_auth.authenticate_api_request", return_value=(True, "person_EXAMPLE")),
        patch("core.admin_api._strict_project_capability_allowed", return_value=False) as guard,
        patch("core.context_events_api.insert_audit_row") as audit,
    ):
        response = client_auth_required.post(
            "/api/context-events?project_id=foreign_project",
            content=json.dumps(
                {
                    "project_id": "foreign_project",
                    "event_date": "2026-07-04",
                    "type": "business",
                    "label": "Forbidden event",
                }
            ),
            headers=_authorized_headers(),
        )

    assert response.status_code == 404
    conn.cursor.assert_not_called()
    audit.assert_not_called()
    assert guard.call_args.kwargs["minimum_capability"] == "edit"


def test_context_events_access_resolution_failure_is_generic_404(
    client_auth_required,
):
    conn = _make_mock_db_conn(cursor=_make_mock_cursor())

    class ConnCtx:
        def __enter__(self):
            return conn

        def __exit__(self, *args):
            return False

    with (
        patch("core.db.get_connection", return_value=ConnCtx()),
        patch("core.api_auth.authenticate_api_request", return_value=(True, "person_EXAMPLE")),
        patch(
            "core.admin_api._strict_project_capability_allowed",
            side_effect=RuntimeError("membership DB unavailable"),
        ),
        patch("core.context_events_api.insert_audit_row") as audit,
    ):
        response = client_auth_required.post(
            "/api/context-events?project_id=proj_test",
            content=json.dumps(
                {
                    "project_id": "proj_test",
                    "event_date": "2026-07-04",
                    "type": "business",
                    "label": "Should not persist",
                }
            ),
            headers=_authorized_headers(),
        )

    assert response.status_code == 404
    assert response.json() == {"code": "not_found", "message": "Project not found"}
    conn.cursor.assert_not_called()
    audit.assert_not_called()


def test_context_events_get_allowed_uses_view_capability(client_auth_required):
    cursor = _make_mock_cursor(fetchall_result=[], description=[])
    conn = _make_mock_db_conn(cursor=cursor)

    class ConnCtx:
        def __enter__(self):
            return conn

        def __exit__(self, *args):
            return False

    with (
        patch("core.db.get_connection", return_value=ConnCtx()),
        patch("core.api_auth.authenticate_api_request", return_value=(True, "person_EXAMPLE")),
        patch("core.admin_api._strict_project_capability_allowed", return_value=True) as guard,
    ):
        response = client_auth_required.get(
            "/api/context-events?project_id=proj_test",
            headers=_authorized_headers(),
        )

    assert response.status_code == 200
    cursor.execute.assert_called_once()
    assert [call.kwargs["minimum_capability"] for call in guard.call_args_list] == [
        "view",
        "edit",
    ]
    assert all(call.args[0] is conn for call in guard.call_args_list)

# ---------------------------------------------------------------------------
# Epic 43.23a -- URL scope and single-transaction authorization
# ---------------------------------------------------------------------------


def test_context_events_post_requires_url_project_before_database(client):
    database = MagicMock()

    with patch("core.db.get_connection", database):
        response = client.post(
            "/api/context-events",
            content=json.dumps(
                {
                    "project_id": "proj_test",
                    "event_date": "2026-07-04",
                    "type": "business",
                    "label": "Event",
                }
            ),
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 422
    assert response.json()["code"] == "missing_project"
    database.assert_not_called()


def test_context_events_post_rejects_json_primitive_stably(client):
    database = MagicMock()

    with patch("core.db.get_connection", database):
        response = client.post(
            "/api/context-events?project_id=proj_test",
            content="[]",
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 422
    assert response.json() == {
        "code": "invalid_body",
        "message": "JSON body must be an object",
    }
    database.assert_not_called()


def test_context_events_post_rejects_body_url_project_mismatch(client):
    database = MagicMock()

    with patch("core.db.get_connection", database):
        response = client.post(
            "/api/context-events?project_id=proj_a",
            content=json.dumps(
                {
                    "project_id": "proj_b",
                    "event_date": "2026-07-04",
                    "type": "business",
                    "label": "Mismatched event",
                }
            ),
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 422
    assert response.json()["code"] == "project_scope_mismatch"
    database.assert_not_called()


def test_context_events_post_authorizes_on_insert_connection(client_auth_required):
    from datetime import date

    row = (
        "evt_01",
        "proj_test",
        date(2026, 7, 4),
        "business",
        "Event",
        _fake_ts(),
    )
    description = [
        ("id",),
        ("project_id",),
        ("event_date",),
        ("type",),
        ("label",),
        ("created_at",),
    ]
    cursor = _make_mock_cursor(fetchone_result=row, description=description)
    conn = _make_mock_db_conn(cursor=cursor)

    class ConnCtx:
        def __enter__(self):
            return conn

        def __exit__(self, *args):
            return False

    database = MagicMock(return_value=ConnCtx())
    with (
        patch("core.db.get_connection", database),
        patch("core.api_auth.authenticate_api_request", return_value=(True, "person_EXAMPLE")),
        patch("core.admin_api._strict_project_capability_allowed", return_value=True) as guard,
        patch("core.context_events_api.insert_audit_row") as audit,
    ):
        response = client_auth_required.post(
            "/api/context-events?project_id=proj_test",
            content=json.dumps(
                {
                    "project_id": "proj_test",
                    "event_date": "2026-07-04",
                    "type": "business",
                    "label": "Event",
                }
            ),
            headers=_authorized_headers(),
        )

    assert response.status_code == 201
    database.assert_called_once_with()
    assert guard.call_args.args[0] is conn
    assert guard.call_args.kwargs["minimum_capability"] == "edit"
    assert guard.call_args.kwargs["hold_access"] is True
    assert audit.call_args.args[0] is conn
    assert "INSERT INTO app.context_events" in cursor.execute.call_args.args[0]

@pytest.mark.parametrize(
    ("field_name", "field_value"),
    [
        ("event_date", 20260704),
        ("type", ["business"]),
        ("label", True),
        ("description", {"text": "details"}),
    ],
)
def test_context_events_post_rejects_non_string_fields_before_database(
    client, field_name, field_value
):
    body = {
        "project_id": "proj_test",
        "event_date": "2026-07-04",
        "type": "business",
        "label": "Event",
    }
    body[field_name] = field_value
    database = MagicMock()
    with patch("core.db.get_connection", database):
        response = client.post(
            "/api/context-events?project_id=proj_test",
            content=json.dumps(body),
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_field"
    database.assert_not_called()


def test_context_events_post_rejects_impossible_calendar_date(client):
    database = MagicMock()
    with patch("core.db.get_connection", database):
        response = client.post(
            "/api/context-events?project_id=proj_test",
            content=json.dumps(
                {
                    "project_id": "proj_test",
                    "event_date": "2026-02-30",
                    "type": "business",
                    "label": "Event",
                }
            ),
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 422
    database.assert_not_called()
