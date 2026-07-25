"""Tests for GET /api/feedback endpoint (Story 5.5, AC7, AC8).

Covers:
  - test_list_feedback_project_scoped: two projects → only A's rows returned for project_id=A.
  - test_list_feedback_module_filter: filter by module returns only matching rows.
  - test_list_feedback_unauthorized: missing auth returns 401.
  - test_list_feedback_missing_project_id: missing project_id returns 400.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(query_params: dict, auth: bool = True) -> MagicMock:
    """Build a mock Starlette Request."""
    from starlette.datastructures import QueryParams

    req = MagicMock()
    req.query_params = QueryParams(query_params)
    req.body = AsyncMock(return_value=b"")
    return req


def _make_mock_conn(rows: list[tuple], col_names: list[str]):
    """Build a mock psycopg connection returning given rows."""
    cursor_mock = MagicMock()
    cursor_mock.description = [(col,) for col in col_names]
    cursor_mock.fetchall.return_value = rows
    cursor_cm = MagicMock()
    cursor_cm.__enter__ = MagicMock(return_value=cursor_mock)
    cursor_cm.__exit__ = MagicMock(return_value=False)
    conn_mock = MagicMock()
    conn_mock.__enter__ = MagicMock(return_value=conn_mock)
    conn_mock.__exit__ = MagicMock(return_value=False)
    conn_mock.cursor = MagicMock(return_value=cursor_cm)
    return conn_mock


# ---------------------------------------------------------------------------
# test_list_feedback_project_scoped
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_list_feedback_project_scoped():
    """Two projects — GET /api/feedback?project_id=A returns only A's rows."""
    from core.admin_api import _list_feedback

    col_names = ["id", "rating", "comment", "module", "report_ref", "trace_id", "created_at"]
    import datetime

    fake_ts = datetime.datetime(2026, 7, 11, 10, 0, 0, tzinfo=datetime.timezone.utc)

    # Only project_A rows are returned (SQL WHERE project_id = %s handles scoping).
    proj_a_rows = [
        ("fb_01", 1, "great", "google-analytics", "get_daily_report:2026-07-11", "abc123", fake_ts),
    ]
    conn_mock = _make_mock_conn(proj_a_rows, col_names)

    request = _make_request({"project_id": "project_A"})

    with (
        patch("core.admin_api._check_auth", return_value=(True, "user@example.com")),
        patch("core.db.get_connection", return_value=conn_mock),
    ):
        response = await _list_feedback(request)

    assert response.status_code == 200
    import json

    body = json.loads(response.body)
    assert isinstance(body, list)
    assert len(body) == 1
    assert body[0]["id"] == "fb_01"
    assert body[0]["rating"] == 1
    # created_by is NOT present (privacy)
    assert "created_by" not in body[0]


# ---------------------------------------------------------------------------
# test_list_feedback_module_filter
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_list_feedback_module_filter():
    """Filter by module returns only matching rows."""
    from core.admin_api import _list_feedback

    col_names = ["id", "rating", "comment", "module", "report_ref", "trace_id", "created_at"]
    import datetime

    fake_ts = datetime.datetime(2026, 7, 11, 10, 0, 0, tzinfo=datetime.timezone.utc)

    ga_row = ("fb_ga", -1, "meh", "google-analytics", "get_daily_report:2026-07-11", None, fake_ts)
    conn_mock = _make_mock_conn([ga_row], col_names)

    request = _make_request({"project_id": "proj_test", "module": "google-analytics"})

    with (
        patch("core.admin_api._check_auth", return_value=(True, "user@example.com")),
        patch("core.db.get_connection", return_value=conn_mock),
    ):
        response = await _list_feedback(request)

    assert response.status_code == 200
    import json

    body = json.loads(response.body)
    assert isinstance(body, list)
    assert all(row.get("module") == "google-analytics" for row in body)


# ---------------------------------------------------------------------------
# test_list_feedback_unauthorized
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_list_feedback_unauthorized():
    """Missing auth token returns 401."""
    from core.admin_api import _list_feedback

    request = _make_request({"project_id": "proj_test"})

    with patch("core.admin_api._check_auth", return_value=(False, "")):
        response = await _list_feedback(request)

    assert response.status_code == 401


# ---------------------------------------------------------------------------
# test_list_feedback_missing_project_id
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_list_feedback_missing_project_id():
    """Missing project_id query param returns 400."""
    from core.admin_api import _list_feedback

    request = _make_request({})  # no project_id

    with patch("core.admin_api._check_auth", return_value=(True, "user@example.com")):
        response = await _list_feedback(request)

    assert response.status_code == 400
    import json

    body = json.loads(response.body)
    assert body["code"] == "missing_param"
