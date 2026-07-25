"""Tests for per-project module enablement (Story 7.2, AC8).

Covers:
  - test_is_module_enabled_no_row_returns_default: no project_modules row -> MODULE_DEFAULT_ENABLED.
  - test_is_module_enabled_explicit_true: row with enabled=True -> True.
  - test_is_module_enabled_explicit_false: row with enabled=False -> False.
  - test_get_report_disabled_module_returns_tool_error: get_report disabled module -> ToolError.
  - test_scheduler_skips_disabled_module_connections: dispatch_nightly disabled module -> no pull.
  - test_available_modules_excludes_disabled: GET /api/modules/available disabled module
    -> enabled: false.

Strategy:
  - is_module_enabled tests: mock the DB connection cursor.
  - get_report / admin_api tests: mock DB, module catalog, and report renderer.
  - scheduler test: mock DB with the new JOIN query response.
  - No real Postgres required for unit tests (mocked); DB integration tests skip when unavailable.
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import contextmanager
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Prevent background threads from starting during tests.
os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")
os.environ.setdefault("MODULE_DEFAULT_ENABLED", "true")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_conn(fetchone_return=None, fetchall_return=None):
    """Build a mock psycopg connection + cursor."""
    mock_cursor = MagicMock()
    mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cursor.__exit__ = MagicMock(return_value=False)
    mock_cursor.fetchone = MagicMock(return_value=fetchone_return)
    mock_cursor.fetchall = MagicMock(return_value=fetchall_return or [])
    mock_cursor.description = []

    mock_conn = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor = MagicMock(return_value=mock_cursor)
    mock_conn.commit = MagicMock()

    return mock_conn, mock_cursor


def _make_fake_get_connection(mock_conn):
    """Return a get_connection() context manager factory yielding mock_conn."""
    @contextmanager
    def _fake_get_connection():
        yield mock_conn

    return _fake_get_connection


# ---------------------------------------------------------------------------
# AC8.1 -- is_module_enabled: no row -> MODULE_DEFAULT_ENABLED
# ---------------------------------------------------------------------------


class TestIsModuleEnabled:
    def test_is_module_enabled_no_row_returns_default_true(self):
        """No project_modules row -> returns MODULE_DEFAULT_ENABLED (default True)."""
        from core.module_enablement import is_module_enabled

        mock_conn, mock_cursor = _make_mock_conn(fetchone_return=None)

        with patch.dict(os.environ, {"MODULE_DEFAULT_ENABLED": "true"}):
            result = is_module_enabled("google-analytics", "default", mock_conn)

        assert result is True

    def test_is_module_enabled_no_row_returns_default_false(self):
        """No project_modules row -> returns MODULE_DEFAULT_ENABLED=false."""
        from core.module_enablement import is_module_enabled

        mock_conn, mock_cursor = _make_mock_conn(fetchone_return=None)

        with patch.dict(os.environ, {"MODULE_DEFAULT_ENABLED": "false"}):
            result = is_module_enabled("google-analytics", "default", mock_conn)

        assert result is False

    def test_is_module_enabled_explicit_true(self):
        """Row with enabled=True -> returns True."""
        from core.module_enablement import is_module_enabled

        # fetchone returns (True,) -- the enabled column.
        mock_conn, mock_cursor = _make_mock_conn(fetchone_return=(True,))

        result = is_module_enabled("meta-ads", "proj_A", mock_conn)
        assert result is True

    def test_is_module_enabled_explicit_false(self):
        """Row with enabled=False -> returns False."""
        from core.module_enablement import is_module_enabled

        mock_conn, mock_cursor = _make_mock_conn(fetchone_return=(False,))

        result = is_module_enabled("meta-ads", "proj_A", mock_conn)
        assert result is False

    def test_is_module_enabled_db_error_returns_default(self):
        """DB error during query -> returns default (True) and logs a warning."""
        from core.module_enablement import is_module_enabled

        mock_conn = MagicMock()
        mock_conn.cursor = MagicMock(side_effect=Exception("DB down"))

        with patch.dict(os.environ, {"MODULE_DEFAULT_ENABLED": "true"}):
            result = is_module_enabled("gsc", "proj_A", mock_conn)

        # Must not raise; returns the default.
        assert result is True


# ---------------------------------------------------------------------------
# AC8.4 -- get_report: disabled module -> ToolError code=module_disabled
# ---------------------------------------------------------------------------


class TestGetReportDisabledModule:
    def test_get_report_disabled_module_returns_tool_error(self):
        """get_report with a disabled module raises ToolError code=module_disabled."""
        from core.main import get_report
        from fastmcp.exceptions import ToolError

        # Mock the DB connection so is_module_enabled returns False.
        mock_conn, mock_cursor = _make_mock_conn(fetchone_return=(False,))
        fake_db = _make_fake_get_connection(mock_conn)

        # Mock _resolve_project to return the project id unchanged.
        with patch("core.main._resolve_project", return_value="proj_test"), \
             patch("core.db.get_connection", new=fake_db):
            with pytest.raises(ToolError) as exc_info:
                get_report(
                    project_id="proj_test",
                    report_id="meta-ads/campaign_overview",
                    date_from="2026-07-01",
                    date_to="2026-07-07",
                )

        err = json.loads(exc_info.value.args[0])
        assert err["code"] == "module_disabled"
        assert "meta-ads" in err["message"]


# ---------------------------------------------------------------------------
# AC8.5 -- scheduler: dispatch_nightly skips disabled module connections
# ---------------------------------------------------------------------------


class TestSchedulerSkipsDisabledModuleConnections:
    def test_scheduler_skips_disabled_module_connections(self):
        """dispatch_nightly fallback: disabled module connection -> no pull enqueued.

        Story 8.2: dispatch_nightly now runs _dispatch_nightly_datastreams first.
        This test exercises the legacy fallback path (zero datastreams) where the
        module-enablement JOIN filter in the fallback SQL means only enabled-module
        connections are returned.  We mock _dispatch_nightly_datastreams to ([], 0)
        so only the fallback query runs, then verify 2 windows are dispatched for
        the single enabled connection.
        """
        from core.scheduler import dispatch_nightly

        # Fallback query returns one connection for the enabled module.
        # "meta-ads" (disabled) would appear without the JOIN filter; not present here.
        enabled_rows = [
            ("conn_enabled", "google-analytics", "proj_A"),
        ]

        mock_cursor = MagicMock()
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchall = MagicMock(return_value=enabled_rows)
        mock_cursor.description = [
            type("D", (), {"__getitem__": staticmethod(lambda i, c=c: c)})()
            for c in ["id", "provider", "project_id"]
        ]

        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor = MagicMock(return_value=mock_cursor)
        mock_conn.commit = MagicMock()

        @contextmanager
        def _fake_db():
            yield mock_conn

        mock_job = {"job_id": "job_1", "pull_id": "pull_1", "state": "queued"}

        # Patch _dispatch_nightly_datastreams to simulate zero datastreams
        # (forces the fallback path).
        with patch("core.scheduler._dispatch_nightly_datastreams", return_value=([], 0)), \
             patch("core.db.get_connection", new=_fake_db), \
             patch("core.queue.enqueue_pull", return_value=mock_job) as mock_enqueue:
            result = dispatch_nightly(as_of_date=date(2026, 7, 12))

        # One legacy connection -> 2 windows (compute_nightly_work).
        assert mock_enqueue.call_count == 2
        assert len(result) == 2

    def test_scheduler_zero_connections_after_filter(self):
        """dispatch_nightly fallback: all connections filtered out -> empty result.

        Story 8.2: _dispatch_nightly_datastreams is patched to ([], 0); fallback
        query returns empty list; no enqueue_pull calls.
        """
        from core.scheduler import dispatch_nightly

        mock_cursor = MagicMock()
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchall = MagicMock(return_value=[])
        mock_cursor.description = [
            type("D", (), {"__getitem__": staticmethod(lambda i, c=c: c)})()
            for c in ["id", "provider", "project_id"]
        ]

        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor = MagicMock(return_value=mock_cursor)

        @contextmanager
        def _fake_db():
            yield mock_conn

        with patch("core.scheduler._dispatch_nightly_datastreams", return_value=([], 0)), \
             patch("core.db.get_connection", new=_fake_db), \
             patch("core.queue.enqueue_pull") as mock_enqueue:
            result = dispatch_nightly(as_of_date=date(2026, 7, 12))

        assert result == []
        mock_enqueue.assert_not_called()


# ---------------------------------------------------------------------------
# AC8.6 -- GET /api/modules/available: disabled module shows enabled: false
# ---------------------------------------------------------------------------


class TestAvailableModulesEndpoint:
    def test_available_modules_excludes_disabled(self):
        """GET /api/modules/available with module disabled -> enabled: false in response."""
        from core.admin_api import _list_available_modules
        from starlette.datastructures import QueryParams

        # Mock request.
        req = MagicMock()
        req.query_params = QueryParams({"project_id": "proj_test"})

        # Discovery catalog: one module.
        discovery_catalog = [
            {"name": "meta-ads", "display_name": "Meta Ads"},
        ]

        # project_modules rows: meta-ads is explicitly disabled.
        pm_fetchall = [("meta-ads", False)]  # (module_name, enabled)
        conn_counts_fetchall = []  # no active connections

        mock_cursor = MagicMock()
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        # fetchall is called twice: once for pm_rows, once for conn_counts.
        mock_cursor.fetchall = MagicMock(side_effect=[pm_fetchall, conn_counts_fetchall])

        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor = MagicMock(return_value=mock_cursor)

        @contextmanager
        def _fake_db():
            yield mock_conn

        with patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "test"))), \
             patch("core.admin_api._module_discovery_catalog", return_value=discovery_catalog), \
             patch("core.db.get_connection", new=_fake_db):
            resp = asyncio.run(_list_available_modules(req))

        data = json.loads(resp.body)
        assert len(data) == 1
        assert data[0]["module_name"] == "meta-ads"
        assert data[0]["enabled"] is False
        assert data[0]["explicitly_set"] is True

    def test_available_modules_default_enabled_when_no_row(self):
        """GET /api/modules/available with no explicit row -> enabled: true (default)."""
        from core.admin_api import _list_available_modules
        from starlette.datastructures import QueryParams

        req = MagicMock()
        req.query_params = QueryParams({"project_id": "proj_test"})

        discovery_catalog = [
            {"name": "google-analytics", "display_name": "Google Analytics 4"},
        ]

        # No rows in project_modules for this module.
        pm_fetchall = []
        conn_counts_fetchall = []

        mock_cursor = MagicMock()
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchall = MagicMock(side_effect=[pm_fetchall, conn_counts_fetchall])

        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor = MagicMock(return_value=mock_cursor)

        @contextmanager
        def _fake_db():
            yield mock_conn

        with patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "test"))), \
             patch("core.admin_api._module_discovery_catalog", return_value=discovery_catalog), \
             patch("core.db.get_connection", new=_fake_db), \
             patch.dict(os.environ, {"MODULE_DEFAULT_ENABLED": "true"}):
            resp = asyncio.run(_list_available_modules(req))

        data = json.loads(resp.body)
        assert len(data) == 1
        assert data[0]["module_name"] == "google-analytics"
        assert data[0]["enabled"] is True
        assert data[0]["explicitly_set"] is False
