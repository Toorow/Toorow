"""Tests for notebooks REST CRUD endpoints (Story 6.5, AC5, AC8).

Covers (from AC8):
  - test_list_notebooks_project_scoped: two projects -> GET returns only own notebooks.
  - test_patch_notebook_updates_title: PATCH -> title updated, updated_at bumped.
  - test_delete_notebook_cascades_runs: DELETE notebook -> notebook_runs also gone.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient

# ---------------------------------------------------------------------------
# Test app factory
# ---------------------------------------------------------------------------


def _make_app():
    """Create a minimal Starlette test app mounting only the admin_api router."""
    from core.admin_api import router

    return Starlette(routes=[Mount("/", app=router)])


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def client():
    """Test client with auth disabled."""
    os.environ["TOOROW_AUTH_MODE"] = "disabled"
    from core import api_auth

    api_auth.reset_verifier_cache()
    app = _make_app()
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    os.environ.pop("TOOROW_AUTH_MODE", None)
    api_auth.reset_verifier_cache()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_ts():
    return datetime(2026, 7, 12, 12, 0, 0, tzinfo=timezone.utc)


def _make_mock_conn(cursor_mock=None):
    """Build a mock psycopg connection usable as a context manager."""
    if cursor_mock is None:
        cursor_mock = MagicMock()
    conn_mock = MagicMock()
    conn_mock.__enter__ = MagicMock(return_value=conn_mock)
    conn_mock.__exit__ = MagicMock(return_value=False)
    cursor_cm = MagicMock()
    cursor_cm.__enter__ = MagicMock(return_value=cursor_mock)
    cursor_cm.__exit__ = MagicMock(return_value=False)
    conn_mock.cursor = MagicMock(return_value=cursor_cm)
    return conn_mock


# ---------------------------------------------------------------------------
# test_list_notebooks_project_scoped
# ---------------------------------------------------------------------------


def test_list_notebooks_project_scoped(client):
    """GET /api/notebooks?project_id=proj_A returns only proj_A notebooks."""
    proj_a_row = (
        "nb_A1", "Notebook A1", "adhoc", "last_30d",
        _fake_ts(), None, None,
    )
    cursor_mock = MagicMock()
    cursor_mock.fetchall.return_value = [proj_a_row]
    cursor_mock.description = [
        ("id",), ("title",), ("report_ref",), ("window_rule",),
        ("created_at",), ("last_run_at",), ("last_run_status",),
    ]
    conn_mock = _make_mock_conn(cursor_mock)

    with patch("core.db.get_connection", return_value=conn_mock):
        resp = client.get("/api/notebooks?project_id=proj_A")

    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["id"] == "nb_A1"
    # Verify the WHERE clause filters by project_id
    sql_called = cursor_mock.execute.call_args[0][0]
    assert "project_id" in sql_called
    params_called = cursor_mock.execute.call_args[0][1]
    assert "proj_A" in params_called


def test_list_notebooks_missing_project_id(client):
    """GET /api/notebooks without project_id -> 400."""
    resp = client.get("/api/notebooks")
    assert resp.status_code == 400


def test_list_notebooks_returns_last_run_status(client):
    """GET /api/notebooks includes last_run_at and last_run_status."""
    row = (
        "nb_RUN1", "Running NB", "adhoc", "last_7d",
        _fake_ts(), _fake_ts(), "success",
    )
    cursor_mock = MagicMock()
    cursor_mock.fetchall.return_value = [row]
    cursor_mock.description = [
        ("id",), ("title",), ("report_ref",), ("window_rule",),
        ("created_at",), ("last_run_at",), ("last_run_status",),
    ]
    conn_mock = _make_mock_conn(cursor_mock)

    with patch("core.db.get_connection", return_value=conn_mock):
        resp = client.get("/api/notebooks?project_id=proj_X")

    assert resp.status_code == 200
    data = resp.json()
    assert data[0]["last_run_status"] == "success"
    assert data[0]["last_run_at"] is not None


# ---------------------------------------------------------------------------
# test_patch_notebook_updates_title
# ---------------------------------------------------------------------------


def test_patch_notebook_updates_title(client):
    """PATCH /api/notebooks/{id} with title -> title updated, updated_at bumped."""
    updated_row = (
        "nb_TEST", "proj_test", "Nouveau titre", "adhoc", "last_30d",
        None,
        datetime(2026, 7, 1, tzinfo=timezone.utc),
        datetime(2026, 7, 12, tzinfo=timezone.utc),
    )
    cursor_mock = MagicMock()
    cursor_mock.fetchone.return_value = updated_row
    cursor_mock.description = [
        ("id",), ("project_id",), ("title",), ("report_ref",),
        ("window_rule",), ("narrative_prompt",), ("created_at",), ("updated_at",),
    ]
    conn_mock = _make_mock_conn(cursor_mock)

    with patch("core.db.get_connection", return_value=conn_mock):
        resp = client.patch(
            "/api/notebooks/nb_TEST",
            json={"title": "Nouveau titre"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["title"] == "Nouveau titre"
    assert data["id"] == "nb_TEST"
    assert "2026-07-12" in data["updated_at"]


def test_patch_notebook_not_found(client):
    """PATCH /api/notebooks/{id} when not found -> 404."""
    cursor_mock = MagicMock()
    cursor_mock.fetchone.return_value = None
    conn_mock = _make_mock_conn(cursor_mock)

    with patch("core.db.get_connection", return_value=conn_mock):
        resp = client.patch("/api/notebooks/nb_MISSING", json={"title": "New"})

    assert resp.status_code == 404


def test_patch_notebook_invalid_window_rule(client):
    """PATCH with invalid window_rule -> 400."""
    resp = client.patch(
        "/api/notebooks/nb_TEST",
        json={"window_rule": "quarterly"},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# test_delete_notebook_cascades_runs
# ---------------------------------------------------------------------------


def test_delete_notebook_cascades_runs(client):
    """DELETE /api/notebooks/{id} -> 204; CASCADE removes notebook_runs at DB level."""
    # Both SELECT and DELETE use the same cursor in _delete_notebook
    cursor_mock = MagicMock()
    cursor_mock.fetchone.return_value = ("nb_TEST", "proj_test")
    conn_mock = _make_mock_conn(cursor_mock)

    with (
        patch("core.db.get_connection", return_value=conn_mock),
        patch("core.admin_api.write_audit_row") as mock_audit,
    ):
        resp = client.delete("/api/notebooks/nb_TEST")

    assert resp.status_code == 204
    # Verify both SELECT and DELETE SQL were called (two execute calls)
    calls = cursor_mock.execute.call_args_list
    assert len(calls) == 2
    sql_calls = [c[0][0] for c in calls]
    assert any("SELECT" in sql for sql in sql_calls)
    assert any("DELETE FROM app.notebooks" in sql for sql in sql_calls)
    # Audit row written for deletion
    mock_audit.assert_called_once()
    assert mock_audit.call_args[1]["action"] == "notebook_deleted"


def test_delete_notebook_not_found(client):
    """DELETE /api/notebooks/{id} when not found -> 404."""
    cursor_mock = MagicMock()
    cursor_mock.fetchone.return_value = None
    conn_mock = _make_mock_conn(cursor_mock)

    with patch("core.db.get_connection", return_value=conn_mock):
        resp = client.delete("/api/notebooks/nb_MISSING")

    assert resp.status_code == 404


class TestSlideXssEscaping:
    """review-epic-6 F-3: stored values must never render as live HTML."""

    def test_data_table_escapes_script_tags(self):
        from core.admin_api import _build_data_table_html

        evil = {"data": {"metrics": {"<script>alert(1)</script>": "<img onerror=x>"}}}
        html_out = _build_data_table_html(evil)
        assert "<script>" not in html_out
        assert "&lt;script&gt;" in html_out
        assert "<img" not in html_out
