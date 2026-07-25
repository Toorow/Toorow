"""Tests for notebook scheduling endpoints and nightly piggyback (Story 6.6, AC1, AC2, AC6).

Covers (from AC6):
  - test_schedule_notebook_sets_flag: PATCH scheduled=true -> scheduled=TRUE, rule='nightly'.
  - test_unschedule_clears_rule: PATCH scheduled=false -> schedule_rule=NULL.
  - test_invalid_schedule_rule_rejected: schedule_rule='weekly' -> 422 response.
  - test_run_due_notebooks_called_in_nightly: mock _run_due_notebooks; trigger
    run_nightly_steps(); assert _run_due_notebooks was called after alert steps.
  - test_scheduled_notebook_failure_does_not_block_next: mock run_notebook_direct to
    raise on first notebook; assert second notebook still runs.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from starlette.applications import Starlette  # noqa: E402
from starlette.routing import Mount  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

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
# test_schedule_notebook_sets_flag (AC6)
# ---------------------------------------------------------------------------


def test_schedule_notebook_sets_flag(client):
    """PATCH scheduled=true -> DB update sets scheduled=TRUE, schedule_rule='nightly'."""
    cursor_mock = MagicMock()
    cursor_mock.fetchone.return_value = (
        "nb_TEST",
        "proj_A",
        "Analyse SEO",
        "gsc/position_movements",
        "last_7d",
        None,
        True,
        "nightly",
        _fake_ts(),
        _fake_ts(),
    )
    cursor_mock.description = [
        ("id",), ("project_id",), ("title",), ("report_ref",), ("window_rule",),
        ("narrative_prompt",), ("scheduled",), ("schedule_rule",),
        ("created_at",), ("updated_at",),
    ]
    conn_mock = _make_mock_conn(cursor_mock)

    with patch("core.db.get_connection", return_value=conn_mock):
        resp = client.patch(
            "/api/notebooks/nb_TEST/schedule",
            json={"scheduled": True, "schedule_rule": "nightly"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["scheduled"] is True
    assert data["schedule_rule"] == "nightly"

    # Verify UPDATE was called with the right values
    execute_calls = cursor_mock.execute.call_args_list
    update_call = next(
        (c for c in execute_calls if "UPDATE" in str(c) and "scheduled" in str(c)),
        None,
    )
    assert update_call is not None
    params = update_call[0][1]
    assert params[0] is True  # scheduled
    assert params[1] == "nightly"  # schedule_rule


def test_unschedule_clears_rule(client):
    """PATCH scheduled=false -> schedule_rule=NULL in the DB call."""
    cursor_mock = MagicMock()
    cursor_mock.fetchone.return_value = (
        "nb_TEST", "proj_A", "Analyse SEO", "gsc/position_movements", "last_7d",
        None, False, None, _fake_ts(), _fake_ts(),
    )
    cursor_mock.description = [
        ("id",), ("project_id",), ("title",), ("report_ref",), ("window_rule",),
        ("narrative_prompt",), ("scheduled",), ("schedule_rule",),
        ("created_at",), ("updated_at",),
    ]
    conn_mock = _make_mock_conn(cursor_mock)

    with patch("core.db.get_connection", return_value=conn_mock):
        resp = client.patch(
            "/api/notebooks/nb_TEST/schedule",
            json={"scheduled": False, "schedule_rule": None},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["scheduled"] is False
    assert data["schedule_rule"] is None

    # Verify UPDATE was called with schedule_rule=None
    execute_calls = cursor_mock.execute.call_args_list
    update_call = next(
        (c for c in execute_calls if "UPDATE" in str(c) and "scheduled" in str(c)),
        None,
    )
    assert update_call is not None
    params = update_call[0][1]
    assert params[0] is False  # scheduled
    assert params[1] is None  # schedule_rule


def test_invalid_schedule_rule_rejected(client):
    """schedule_rule='weekly' -> 422 response (only 'nightly' supported)."""
    resp = client.patch(
        "/api/notebooks/nb_TEST/schedule",
        json={"scheduled": True, "schedule_rule": "weekly"},
    )
    assert resp.status_code == 422
    data = resp.json()
    assert "nightly" in data.get("message", "").lower()


def test_schedule_missing_scheduled_field_returns_400(client):
    """PATCH without 'scheduled' field -> 400."""
    resp = client.patch(
        "/api/notebooks/nb_TEST/schedule",
        json={"schedule_rule": "nightly"},
    )
    assert resp.status_code == 400


def test_schedule_notebook_not_found(client):
    """PATCH schedule on non-existent notebook -> 404."""
    cursor_mock = MagicMock()
    cursor_mock.fetchone.return_value = None
    conn_mock = _make_mock_conn(cursor_mock)

    with patch("core.db.get_connection", return_value=conn_mock):
        resp = client.patch(
            "/api/notebooks/nb_MISSING/schedule",
            json={"scheduled": True, "schedule_rule": "nightly"},
        )

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# test_run_due_notebooks_called_in_nightly (AC6)
# ---------------------------------------------------------------------------


def test_run_due_notebooks_called_in_nightly():
    """run_nightly_steps() must call _run_due_notebooks before _run_due_briefings.

    Updated for Story 6.7 (AC4): run_due_briefings is now the LAST step (6th).
    run_due_notebooks is the 5th step (penultimate). Both must run.
    """
    from datetime import date

    # Patch all individual step functions + _run_due_notebooks + _run_due_briefings
    call_order: list[str] = []

    def fake_dispatch(**kwargs):
        call_order.append("dispatch_nightly")

    def fake_alert():
        call_order.append("alert_check")

    def fake_business():
        call_order.append("business_alert_check")

    def fake_anomaly():
        call_order.append("anomaly_alert_check")

    def fake_due_notebooks():
        call_order.append("run_due_notebooks")

    def fake_due_briefings(nightly_run_id):
        call_order.append("run_due_briefings")

    with (
        patch("core.scheduler.dispatch_nightly", side_effect=fake_dispatch),
        patch("core.scheduler._run_alert_check", side_effect=fake_alert),
        patch("core.scheduler._run_business_alert_check", side_effect=fake_business),
        patch("core.scheduler._run_anomaly_alert_check", side_effect=fake_anomaly),
        patch("core.scheduler._run_due_notebooks", side_effect=fake_due_notebooks),
        patch("core.scheduler._run_due_briefings", side_effect=fake_due_briefings),
    ):
        from core.scheduler import run_nightly_steps

        run_nightly_steps(date.today())

    # Assert notebooks ran before briefings, and briefings is last
    assert "dispatch_nightly" in call_order
    assert "run_due_notebooks" in call_order
    assert "run_due_briefings" in call_order
    notebooks_pos = call_order.index("run_due_notebooks")
    briefings_pos = call_order.index("run_due_briefings")
    assert notebooks_pos < briefings_pos, (
        f"run_due_notebooks must come before run_due_briefings; got: {call_order}"
    )
    assert call_order[-1] == "run_due_briefings", (
        f"run_due_briefings must be the LAST step (Story 6.7); got: {call_order}"
    )


# ---------------------------------------------------------------------------
# test_scheduled_notebook_failure_does_not_block_next (AC6)
# ---------------------------------------------------------------------------


def test_scheduled_notebook_failure_does_not_block_next():
    """Failure in first scheduled notebook must not prevent second from running."""
    # Two scheduled notebooks: first raises, second succeeds
    nb1 = {"id": "nb_FAIL", "title": "Notebook FAIL"}
    nb2 = {"id": "nb_OK", "title": "Notebook OK"}

    run_calls: list[str] = []

    def fake_run_notebook_direct(notebook_id, as_of=None):
        if notebook_id == "nb_FAIL":
            raise RuntimeError("Simulated render failure")
        run_calls.append(notebook_id)

    cursor_mock = MagicMock()
    cursor_mock.fetchall.return_value = [
        (nb1["id"], nb1["title"]),
        (nb2["id"], nb2["title"]),
    ]
    cursor_mock.description = [("id",), ("title",)]
    conn_mock = _make_mock_conn(cursor_mock)

    with (
        patch("core.db.get_connection", return_value=conn_mock),
        patch("core.main.run_notebook_direct", side_effect=fake_run_notebook_direct),
        patch("core.scheduler._insert_meta_alert"),
    ):
        from core.scheduler import _run_due_notebooks

        _run_due_notebooks()

    # nb_OK must have been called despite nb_FAIL raising
    assert "nb_OK" in run_calls


def test_run_due_notebooks_empty_list():
    """_run_due_notebooks with no scheduled notebooks is a no-op."""
    cursor_mock = MagicMock()
    cursor_mock.fetchall.return_value = []
    cursor_mock.description = [("id",), ("title",)]
    conn_mock = _make_mock_conn(cursor_mock)

    with patch("core.db.get_connection", return_value=conn_mock):
        from core.scheduler import _run_due_notebooks

        _run_due_notebooks()  # Should not raise
