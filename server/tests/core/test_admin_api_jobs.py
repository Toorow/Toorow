"""Unit tests for GET /api/jobs list endpoint (Story 3.4, AC6).

Tests:
  - GET /api/jobs returns 200 with jobs array
  - GET /api/jobs?state=dead_letter returns filtered list
  - GET /api/jobs returns 401 when unauthorized
  - GET /api/jobs/{id} still works (no regression from adding list endpoint)

Strategy:
  - All DB calls mocked -- no real Postgres required.
  - SCHEDULER_ENABLED=false, HEALTH_POLLER_ENABLED=false, QUEUE_WORKER_ENABLED=false.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_JOB_COLS = [
    "id", "pull_id", "connection_ref_id", "date_from", "date_to",
    "state", "requested_by", "error_detail", "attempt_count",
    "enqueued_at", "started_at", "completed_at",
]

_DEAD_LETTER_JOB_ROW = (
    "job_dl_001",          # id
    "pull_dl_001",         # pull_id
    "conn_001",            # connection_ref_id
    "2026-07-01",          # date_from
    "2026-07-07",          # date_to
    "dead_letter",         # state
    "scheduler",           # requested_by
    "too many failures",   # error_detail
    3,                     # attempt_count
    datetime(2026, 7, 1, 2, 0, 0, tzinfo=timezone.utc),   # enqueued_at
    datetime(2026, 7, 1, 2, 0, 5, tzinfo=timezone.utc),   # started_at
    datetime(2026, 7, 1, 2, 0, 10, tzinfo=timezone.utc),  # completed_at
)


def _make_cursor_for_jobs(rows: list[tuple]):
    """Fake psycopg cursor that returns a list of job rows."""
    col_descs = [
        type("D", (), {"__getitem__": staticmethod(lambda i, c=c: c)})()
        for c in _JOB_COLS
    ]

    mock_cursor = MagicMock()
    mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cursor.__exit__ = MagicMock(return_value=False)
    mock_cursor.fetchall = MagicMock(return_value=rows)
    mock_cursor.description = col_descs

    mock_conn = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor = MagicMock(return_value=mock_cursor)
    mock_conn.commit = MagicMock()
    mock_conn.close = MagicMock()

    @contextmanager
    def _fake_get_connection():
        yield mock_conn

    return _fake_get_connection, mock_conn, mock_cursor


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestListJobsEndpoint:
    def _build_client(self):
        """Build a Starlette test client for the combined app."""
        from core.main import build_asgi_app
        from starlette.testclient import TestClient

        app = build_asgi_app()
        return TestClient(app, raise_server_exceptions=False)

    def test_list_jobs_returns_200_with_jobs_array(self):
        """GET /api/jobs returns 200 and a jobs list."""
        fake_db, _, _ = _make_cursor_for_jobs([_DEAD_LETTER_JOB_ROW])

        with patch("core.db.get_connection", new=fake_db):
            client = self._build_client()
            response = client.get("/api/jobs")

        assert response.status_code == 200
        data = response.json()
        assert "jobs" in data
        assert isinstance(data["jobs"], list)

    def test_list_jobs_returns_dead_letter_job(self):
        """GET /api/jobs?state=dead_letter returns the dead-letter job."""
        fake_db, _, _ = _make_cursor_for_jobs([_DEAD_LETTER_JOB_ROW])

        with patch("core.db.get_connection", new=fake_db):
            client = self._build_client()
            response = client.get("/api/jobs?state=dead_letter")

        assert response.status_code == 200
        data = response.json()
        assert len(data["jobs"]) == 1
        job = data["jobs"][0]
        assert job["state"] == "dead_letter"
        assert job["id"] == "job_dl_001"
        assert job["pull_id"] == "pull_dl_001"

    def test_list_jobs_no_filter_returns_all(self):
        """GET /api/jobs (no filter) returns all jobs."""
        fake_db, _, _ = _make_cursor_for_jobs([_DEAD_LETTER_JOB_ROW])

        with patch("core.db.get_connection", new=fake_db):
            client = self._build_client()
            response = client.get("/api/jobs")

        assert response.status_code == 200
        data = response.json()
        assert "jobs" in data

    def test_list_jobs_empty_result(self):
        """GET /api/jobs returns 200 with empty list when no jobs match."""
        fake_db, _, _ = _make_cursor_for_jobs([])

        with patch("core.db.get_connection", new=fake_db):
            client = self._build_client()
            response = client.get("/api/jobs?state=dead_letter")

        assert response.status_code == 200
        data = response.json()
        assert data["jobs"] == []

    def test_list_jobs_unauthorized(self):
        """GET /api/jobs without auth returns 401 in static/oauth auth modes."""
        # In disabled mode (default in tests), all requests pass through.
        # This test verifies the 401 branch by mocking _check_auth to return False.
        fake_db, _, _ = _make_cursor_for_jobs([])

        with patch("core.db.get_connection", new=fake_db), \
             patch("core.admin_api._check_auth", return_value=(False, "")):
            client = self._build_client()
            response = client.get("/api/jobs")

        assert response.status_code == 401
        data = response.json()
        assert data.get("code") == "unauthorized"

    def test_list_jobs_timestamps_are_iso_strings(self):
        """Timestamp columns in job rows are serialized as ISO-8601 strings."""
        fake_db, _, _ = _make_cursor_for_jobs([_DEAD_LETTER_JOB_ROW])

        with patch("core.db.get_connection", new=fake_db):
            client = self._build_client()
            response = client.get("/api/jobs")

        data = response.json()
        assert len(data["jobs"]) == 1
        job = data["jobs"][0]
        # Timestamps must be strings, not datetime objects
        assert isinstance(job["enqueued_at"], str)
        assert "T" in job["enqueued_at"]  # ISO-8601 format

    def test_get_job_by_id_still_works(self):
        """GET /api/jobs/{id} (single-job endpoint) is not broken by the list route."""
        # Verify the /api/jobs/{id} route still matches correctly alongside /api/jobs.
        job_dict = {
            "id": "job_abc",
            "pull_id": "pull_abc",
            "state": "done",
            "connection_ref_id": "conn_001",
            "date_from": "2026-07-01",
            "date_to": "2026-07-07",
            "attempt_count": 1,
            "error_detail": None,
            "enqueued_at": "2026-07-01T02:00:00+00:00",
            "started_at": "2026-07-01T02:00:05+00:00",
            "completed_at": "2026-07-01T02:00:10+00:00",
        }

        with patch("core.queue.get_job_status", return_value=job_dict):
            client = self._build_client()
            response = client.get("/api/jobs/job_abc")

        assert response.status_code == 200
        data = response.json()
        assert data["job_id"] == "job_abc"
        assert data["state"] == "done"
