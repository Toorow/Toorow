"""Unit tests for /api/alert-definitions CRUD routes (Story 5.3, AC5, AC9).

Tests per AC9:
  - test_create_definition_valid: POST with valid metric+operator -> 201, audit row written.
  - test_create_definition_invalid_operator: operator not in whitelist -> 422.
  - test_create_definition_unknown_metric: metric not in dim_metric or semantic set -> 422.
  - test_toggle_enabled: PATCH -> enabled flips; audit row written.
  - test_list_with_last_firing: GET returns last firing date+value per definition.

Strategy:
  - Uses Starlette TestClient on the admin_api.router (no real Postgres required).
  - DB calls patched at core.db.get_connection level.
  - Auth is bypassed by patching authenticate_api_request to always return (True, "test-user").
  - write_audit_row is patched to verify it's called (never raises).
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient

# Guard background threads
os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core.admin_api import router  # noqa: E402

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_cursor(rows=None, description=None):
    """Build a MagicMock cursor."""
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchall = MagicMock(return_value=rows or [])
    cur.fetchone = MagicMock(return_value=(rows[0] if rows else None))
    cur.description = description or []
    cur.execute = MagicMock()
    return cur


def _make_conn(cursor=None):
    """Build a MagicMock psycopg connection."""
    cur = cursor or _make_cursor()
    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    conn.cursor = MagicMock(return_value=cur)
    conn.commit = MagicMock()
    return conn


@contextmanager
def _conn_ctx(conn):
    yield conn


@pytest.fixture()
def client():
    """TestClient for admin_api.router with auth bypassed."""
    with (
        patch(
            "core.api_auth.authenticate_api_request",
            return_value=(True, "test-user"),
        ),
    ):
        with TestClient(router, raise_server_exceptions=True) as c:
            yield c


# ---------------------------------------------------------------------------
# T9.2 tests
# ---------------------------------------------------------------------------


class TestCreateDefinitionValid:
    """test_create_definition_valid (AC9)."""

    def test_create_definition_valid(self, client: TestClient):
        """POST with valid metric+operator -> 201, audit row written."""
        now = datetime.now(tz=timezone.utc)
        insert_row = (
            "alrt_TEST",
            "proj1",
            "cost",
            ">",
            50.0,
            None,
            True,
            "test-user",
            now,
            now,
        )
        insert_description = [
            ("id",),
            ("project_id",),
            ("metric",),
            ("operator",),
            ("threshold",),
            ("connector",),
            ("enabled",),
            ("created_by",),
            ("created_at",),
            ("updated_at",),
        ]
        insert_cur = _make_cursor(rows=[insert_row], description=insert_description)
        conn = _make_conn(insert_cur)

        with (
            patch("core.db.get_connection", return_value=_conn_ctx(conn)),
            patch("core.admin_api.write_audit_row") as mock_audit,
        ):
            resp = client.post(
                "/api/alert-definitions",
                json={
                    "project_id": "proj1",
                    "metric": "cost",
                    "operator": ">",
                    "threshold": 50.0,
                },
            )

        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["id"] == "alrt_TEST"
        assert body["metric"] == "cost"
        assert body["operator"] == ">"
        assert body["threshold"] == 50.0
        assert body["enabled"] is True

        # Audit row must have been written
        mock_audit.assert_called_once()
        call_kwargs = mock_audit.call_args
        assert call_kwargs[1]["action"] == "alert_def.created"

    def test_create_definition_with_connector(self, client: TestClient):
        """POST with connector field -> connector is stored."""
        now = datetime.now(tz=timezone.utc)
        insert_row = (
            "alrt_C01",
            "proj1",
            "clicks",
            "<",
            100.0,
            "google-ads",
            True,
            "test-user",
            now,
            now,
        )
        insert_description = [
            ("id",),
            ("project_id",),
            ("metric",),
            ("operator",),
            ("threshold",),
            ("connector",),
            ("enabled",),
            ("created_by",),
            ("created_at",),
            ("updated_at",),
        ]
        insert_cur = _make_cursor(rows=[insert_row], description=insert_description)
        conn = _make_conn(insert_cur)

        with (
            patch("core.db.get_connection", return_value=_conn_ctx(conn)),
            patch("core.admin_api.write_audit_row"),
        ):
            resp = client.post(
                "/api/alert-definitions",
                json={
                    "project_id": "proj1",
                    "metric": "clicks",
                    "operator": "<",
                    "threshold": 100.0,
                    "connector": "google-ads",
                },
            )

        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["connector"] == "google-ads"


class TestCreateDefinitionInvalidOperator:
    """test_create_definition_invalid_operator (AC9)."""

    def test_create_definition_invalid_operator(self, client: TestClient):
        """operator not in whitelist -> 422."""
        resp = client.post(
            "/api/alert-definitions",
            json={
                "project_id": "proj1",
                "metric": "cost",
                "operator": "!=",  # invalid operator
                "threshold": 50.0,
            },
        )

        assert resp.status_code == 422, resp.text
        body = resp.json()
        assert body["code"] == "invalid_operator"

    def test_create_definition_sql_injection_operator(self, client: TestClient):
        """SQL injection attempt in operator field -> 422."""
        resp = client.post(
            "/api/alert-definitions",
            json={
                "project_id": "proj1",
                "metric": "cost",
                "operator": "'; DROP TABLE app.alert_definitions; --",
                "threshold": 50.0,
            },
        )
        assert resp.status_code == 422, resp.text


class TestCreateDefinitionUnknownMetric:
    """test_create_definition_unknown_metric (AC9)."""

    def test_create_definition_unknown_metric(self, client: TestClient):
        """metric not in dim_metric or semantic set -> 422."""
        resp = client.post(
            "/api/alert-definitions",
            json={
                "project_id": "proj1",
                "metric": "totally_fake_metric_xyz",  # unknown metric
                "operator": ">",
                "threshold": 50.0,
            },
        )

        assert resp.status_code == 422, resp.text
        body = resp.json()
        assert body["code"] == "unknown_metric"

    def test_create_definition_valid_semantic_metric(self, client: TestClient):
        """Semantic metric (cpa, roas, ctr) is accepted as valid."""
        now = datetime.now(tz=timezone.utc)
        insert_row = (
            "alrt_CPA",
            "proj1",
            "cpa",
            ">",
            2.0,
            None,
            True,
            "test-user",
            now,
            now,
        )
        insert_description = [
            ("id",),
            ("project_id",),
            ("metric",),
            ("operator",),
            ("threshold",),
            ("connector",),
            ("enabled",),
            ("created_by",),
            ("created_at",),
            ("updated_at",),
        ]
        insert_cur = _make_cursor(rows=[insert_row], description=insert_description)
        conn = _make_conn(insert_cur)

        with (
            patch("core.db.get_connection", return_value=_conn_ctx(conn)),
            patch("core.admin_api.write_audit_row"),
        ):
            resp = client.post(
                "/api/alert-definitions",
                json={
                    "project_id": "proj1",
                    "metric": "cpa",  # semantic metric
                    "operator": ">",
                    "threshold": 2.0,
                },
            )

        assert resp.status_code == 201, resp.text


class TestToggleEnabled:
    """test_toggle_enabled (AC9)."""

    def test_toggle_enabled(self, client: TestClient):
        """PATCH -> enabled flips; audit row written."""
        now = datetime.now(tz=timezone.utc)
        updated_row = (
            "alrt_TOG",
            "proj1",
            "cost",
            ">",
            50.0,
            None,
            False,  # enabled flipped to False
            "test-user",
            now,
            now,
        )
        updated_description = [
            ("id",),
            ("project_id",),
            ("metric",),
            ("operator",),
            ("threshold",),
            ("connector",),
            ("enabled",),
            ("created_by",),
            ("created_at",),
            ("updated_at",),
        ]
        update_cur = _make_cursor(rows=[updated_row], description=updated_description)
        conn = _make_conn(update_cur)

        with (
            patch("core.db.get_connection", return_value=_conn_ctx(conn)),
            patch("core.admin_api.write_audit_row") as mock_audit,
        ):
            resp = client.patch(
                "/api/alert-definitions/alrt_TOG",
                json={"enabled": False},
            )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["id"] == "alrt_TOG"
        assert body["enabled"] is False

        # Audit row must have been written
        mock_audit.assert_called_once()
        call_kwargs = mock_audit.call_args
        assert call_kwargs[1]["action"] == "alert_def.updated"

    def test_patch_not_found(self, client: TestClient):
        """PATCH on non-existent id -> 404."""
        update_cur = _make_cursor(rows=None)  # no row returned -> not found
        update_cur.fetchone = MagicMock(return_value=None)
        conn = _make_conn(update_cur)

        with (
            patch("core.db.get_connection", return_value=_conn_ctx(conn)),
            patch("core.admin_api.write_audit_row"),
        ):
            resp = client.patch(
                "/api/alert-definitions/alrt_NOTEXIST",
                json={"enabled": True},
            )

        assert resp.status_code == 404, resp.text


class TestListWithLastFiring:
    """test_list_with_last_firing (AC9)."""

    def test_list_with_last_firing(self, client: TestClient):
        """GET returns last firing date+value per definition."""
        now = datetime.now(tz=timezone.utc)
        row = (
            "alrt_LIST01",  # id
            "proj1",  # project_id
            "cost",  # metric
            ">",  # operator
            100.0,  # threshold
            None,  # connector
            True,  # enabled
            "test-user",  # created_by
            now,  # created_at
            now,  # updated_at
            date(2026, 7, 10),  # last_firing_date
            135.5,  # last_firing_value
        )
        description = [
            ("id",),
            ("project_id",),
            ("metric",),
            ("operator",),
            ("threshold",),
            ("connector",),
            ("enabled",),
            ("created_by",),
            ("created_at",),
            ("updated_at",),
            ("last_firing_date",),
            ("last_firing_value",),
        ]
        cur = _make_cursor(rows=[row], description=description)
        conn = _make_conn(cur)

        with patch("core.db.get_connection", return_value=_conn_ctx(conn)):
            resp = client.get("/api/alert-definitions?project_id=proj1")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "definitions" in body
        definitions = body["definitions"]
        assert len(definitions) == 1
        defn = definitions[0]
        assert defn["id"] == "alrt_LIST01"
        assert defn["metric"] == "cost"
        assert defn["last_firing_date"] == "2026-07-10"
        assert defn["last_firing_value"] == 135.5

    def test_list_missing_project_id(self, client: TestClient):
        """GET without project_id -> 400."""
        resp = client.get("/api/alert-definitions")
        assert resp.status_code == 400, resp.text

    def test_delete_soft(self, client: TestClient):
        """DELETE -> soft-delete (enabled=false), returns {"deleted": true}."""
        del_cur = _make_cursor(rows=[("alrt_DEL",)])
        del_cur.fetchone = MagicMock(return_value=("alrt_DEL",))
        conn = _make_conn(del_cur)

        with (
            patch("core.db.get_connection", return_value=_conn_ctx(conn)),
            patch("core.admin_api.write_audit_row") as mock_audit,
        ):
            resp = client.delete("/api/alert-definitions/alrt_DEL")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["deleted"] is True
        assert body["id"] == "alrt_DEL"

        # Audit written with deleted action
        mock_audit.assert_called_once()
        assert mock_audit.call_args[1]["action"] == "alert_def.deleted"
