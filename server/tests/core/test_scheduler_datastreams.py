"""Unit tests for Story 8.2 scheduler changes: dispatch_nightly datastream iteration.

Covers:
  - dispatch_nightly uses enabled datastreams as the primary dispatch unit.
  - refetch_days window is respected.
  - Legacy fallback fires for projects with zero enabled datastreams.
  - Module enablement filter (app.project_modules) is applied.
  - datastream_id is passed to enqueue_pull.

AD-2: no module names hardcoded (module_name is data in the mock rows).
"""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_queue(jobs=None):
    """Return a mock queue module with enqueue_pull."""
    q = MagicMock()
    q.enqueue_pull.return_value = {"job_id": "job_x", "pull_id": "pull_x", "state": "queued"}
    if jobs is not None:
        q.enqueue_pull.side_effect = jobs
    return q


def _make_get_connection(rows_by_query=None):
    """Build a get_connection mock that returns different rows per query.

    rows_by_query: list of row-lists to return in sequence (one per cursor.execute call).
    """
    conn_ctx = MagicMock()
    conn = MagicMock()
    conn_ctx.__enter__ = MagicMock(return_value=conn)
    conn_ctx.__exit__ = MagicMock(return_value=False)

    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value.__enter__ = lambda s: cur
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    if rows_by_query:
        cur.fetchall.side_effect = rows_by_query
    else:
        cur.fetchall.return_value = []

    get_conn = MagicMock(return_value=conn_ctx)
    return get_conn, conn, cur


# ---------------------------------------------------------------------------
# Tests for _dispatch_nightly_datastreams
# ---------------------------------------------------------------------------


class TestDispatchNightlyDatastreams:
    def test_dispatches_one_job_per_enabled_datastream(self):
        """One enabled datastream -> one enqueue_pull call with correct window."""
        from core.scheduler import _dispatch_nightly_datastreams

        as_of = date(2026, 7, 12)
        yesterday = date(2026, 7, 11)
        refetch_days = 3
        expected_from = (yesterday - timedelta(days=refetch_days - 1)).isoformat()
        expected_to = yesterday.isoformat()

        ds_row = {
            "ds_id": "ds_001",
            "project_id": "proj_a",
            "module_name": "google-analytics",
            "refetch_days": refetch_days,
            "connection_ref_id": "conn_x",
            "cr_status": "active",
            "cr_enabled": True,
        }

        get_conn, conn, cur = _make_get_connection()
        cols = list(ds_row.keys())
        cur.description = [(c,) for c in cols]
        cur.fetchall.return_value = [tuple(ds_row[c] for c in cols)]

        q = _make_mock_queue()

        jobs, count = _dispatch_nightly_datastreams(as_of, "scheduler", q, get_conn)

        assert count == 1
        assert len(jobs) == 1
        q.enqueue_pull.assert_called_once_with(
            "conn_x",
            expected_from,
            expected_to,
            requested_by="scheduler",
            datastream_id="ds_001",
        )

    def test_skips_datastream_with_no_connection(self):
        """Datastream with connection_ref_id=None is skipped."""
        from core.scheduler import _dispatch_nightly_datastreams

        as_of = date(2026, 7, 12)
        ds_row = {
            "ds_id": "ds_no_conn",
            "project_id": "proj_a",
            "module_name": "ga",
            "refetch_days": 3,
            "connection_ref_id": None,
            "cr_status": None,
            "cr_enabled": None,
        }
        get_conn, conn, cur = _make_get_connection()
        cols = list(ds_row.keys())
        cur.description = [(c,) for c in cols]
        cur.fetchall.return_value = [tuple(ds_row[c] for c in cols)]

        q = _make_mock_queue()
        jobs, count = _dispatch_nightly_datastreams(as_of, "scheduler", q, get_conn)

        assert count == 0
        assert jobs == []
        q.enqueue_pull.assert_not_called()

    def test_skips_datastream_with_inactive_connection(self):
        """Datastream whose connection is not active is skipped."""
        from core.scheduler import _dispatch_nightly_datastreams

        as_of = date(2026, 7, 12)
        ds_row = {
            "ds_id": "ds_revoked",
            "project_id": "proj_a",
            "module_name": "ga",
            "refetch_days": 3,
            "connection_ref_id": "conn_revoked",
            "cr_status": "revoked",
            "cr_enabled": True,
        }
        get_conn, conn, cur = _make_get_connection()
        cols = list(ds_row.keys())
        cur.description = [(c,) for c in cols]
        cur.fetchall.return_value = [tuple(ds_row[c] for c in cols)]

        q = _make_mock_queue()
        jobs, count = _dispatch_nightly_datastreams(as_of, "scheduler", q, get_conn)

        assert count == 0
        q.enqueue_pull.assert_not_called()

    def test_refetch_days_controls_window_size(self):
        """refetch_days=7 -> window starts 6 days before yesterday."""
        from core.scheduler import _dispatch_nightly_datastreams

        as_of = date(2026, 7, 12)
        yesterday = date(2026, 7, 11)
        refetch_days = 7
        expected_from = (yesterday - timedelta(days=6)).isoformat()
        expected_to = yesterday.isoformat()

        ds_row = {
            "ds_id": "ds_7d",
            "project_id": "proj_a",
            "module_name": "ga",
            "refetch_days": refetch_days,
            "connection_ref_id": "conn_x",
            "cr_status": "active",
            "cr_enabled": True,
        }
        get_conn, conn, cur = _make_get_connection()
        cols = list(ds_row.keys())
        cur.description = [(c,) for c in cols]
        cur.fetchall.return_value = [tuple(ds_row[c] for c in cols)]

        q = _make_mock_queue()
        _dispatch_nightly_datastreams(as_of, "scheduler", q, get_conn)

        q.enqueue_pull.assert_called_once_with(
            "conn_x",
            expected_from,
            expected_to,
            requested_by="scheduler",
            datastream_id="ds_7d",
        )

    def test_returns_empty_on_db_error(self):
        """DB error -> returns ([], 0) without raising."""
        from core.scheduler import _dispatch_nightly_datastreams

        get_conn = MagicMock(side_effect=Exception("DB down"))
        q = _make_mock_queue()
        jobs, count = _dispatch_nightly_datastreams(date.today(), "scheduler", q, get_conn)
        assert jobs == []
        assert count == 0

    def test_continues_on_enqueue_error(self):
        """Enqueue failure for one datastream does not prevent others from being queued."""
        from core.scheduler import _dispatch_nightly_datastreams

        as_of = date(2026, 7, 12)
        ds_rows = [
            {
                "ds_id": "ds_fail",
                "project_id": "proj_a",
                "module_name": "ga",
                "refetch_days": 3,
                "connection_ref_id": "conn_fail",
                "cr_status": "active",
                "cr_enabled": True,
            },
            {
                "ds_id": "ds_ok",
                "project_id": "proj_a",
                "module_name": "ga",
                "refetch_days": 3,
                "connection_ref_id": "conn_ok",
                "cr_status": "active",
                "cr_enabled": True,
            },
        ]
        get_conn, conn, cur = _make_get_connection()
        cols = list(ds_rows[0].keys())
        cur.description = [(c,) for c in cols]
        cur.fetchall.return_value = [tuple(r[c] for c in cols) for r in ds_rows]

        q = MagicMock()
        q.enqueue_pull.side_effect = [
            Exception("enqueue failed"),
            {"job_id": "job_ok", "pull_id": "pull_ok", "state": "queued"},
        ]

        jobs, count = _dispatch_nightly_datastreams(as_of, "scheduler", q, get_conn)
        assert count == 2  # Both were attempted
        assert len(jobs) == 1  # Only the successful one is in the list


# ---------------------------------------------------------------------------
# Tests for dispatch_nightly (full function with fallback logic)
# ---------------------------------------------------------------------------


def _make_db_ctx(fetchall_side_effect=None):
    """Build a mock DB connection context + cursor."""
    conn_ctx = MagicMock()
    conn = MagicMock()
    conn_ctx.__enter__ = MagicMock(return_value=conn)
    conn_ctx.__exit__ = MagicMock(return_value=False)
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value.__enter__ = lambda s: cur
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    if fetchall_side_effect is not None:
        cur.fetchall.side_effect = fetchall_side_effect
    else:
        cur.fetchall.return_value = []
    cur.description = [("id",), ("provider",), ("project_id",)]
    return conn_ctx, cur


class TestDispatchNightlyFull:
    def test_uses_datastream_path_when_datastreams_exist(self):
        """_dispatch_nightly_datastreams is called by dispatch_nightly."""
        from core.scheduler import dispatch_nightly

        # get_connection (inline import from core.db) patches must target core.db.
        # queue is imported inline (`from core import queue`) so patch core.queue.enqueue_pull.
        conn_ctx, cur = _make_db_ctx(
            fetchall_side_effect=[
                [],  # fallback query: legacy connections (none)
            ]
        )

        mock_ds_jobs = [{"job_id": "job_ds", "pull_id": "pull_ds", "state": "queued"}]
        with patch(
            "core.scheduler._dispatch_nightly_datastreams", return_value=(mock_ds_jobs, 1)
        ) as mock_ds_dispatch:
            with patch("core.db.get_connection", return_value=conn_ctx):
                with patch(
                    "core.queue.enqueue_pull",
                    return_value={"job_id": "j", "pull_id": "p", "state": "queued"},
                ):
                    with patch.dict("os.environ", {"SYNC_ENABLED": "false"}):
                        result = dispatch_nightly(as_of_date=date(2026, 7, 12))

        # _dispatch_nightly_datastreams must have been called
        mock_ds_dispatch.assert_called_once()
        # Result includes the DS job
        assert any(j.get("job_id") == "job_ds" for j in result)

    def test_fallback_fires_for_projects_without_datastreams(self):
        """Legacy connections for projects with zero datastreams are dispatched."""
        from core.scheduler import dispatch_nightly

        as_of = date(2026, 7, 12)

        # get_connection is imported from core.db inside dispatch_nightly.
        conn_ctx, cur = _make_db_ctx(
            fetchall_side_effect=[
                [("conn_legacy", "ga", "proj_legacy")],  # legacy connection
            ]
        )

        mock_q = MagicMock()
        mock_q.enqueue_pull.return_value = {
            "job_id": "job_leg",
            "pull_id": "pull_leg",
            "state": "queued",
        }

        with patch("core.scheduler._dispatch_nightly_datastreams", return_value=([], 0)):
            with patch("core.db.get_connection", return_value=conn_ctx):
                with patch("core.queue.enqueue_pull", side_effect=mock_q.enqueue_pull):
                    with patch.dict("os.environ", {"SYNC_ENABLED": "false"}):
                        dispatch_nightly(as_of_date=as_of, requested_by="scheduler")

        # 2 windows per legacy connection (compute_nightly_work returns 2 windows)
        assert mock_q.enqueue_pull.call_count == 2

    def test_dispatch_nightly_returns_list(self):
        """dispatch_nightly always returns a list (even on empty DB)."""
        from core.scheduler import dispatch_nightly

        conn_ctx, cur = _make_db_ctx(fetchall_side_effect=[[]])

        with patch("core.scheduler._dispatch_nightly_datastreams", return_value=([], 0)):
            with patch("core.db.get_connection", return_value=conn_ctx):
                with patch(
                    "core.queue.enqueue_pull",
                    return_value={"job_id": "j", "pull_id": "p", "state": "queued"},
                ):
                    with patch.dict("os.environ", {"SYNC_ENABLED": "false"}):
                        result = dispatch_nightly(as_of_date=date(2026, 7, 12))

        assert isinstance(result, list)


def test_versioned_drafts_are_outside_the_legacy_scheduler_boundary():
    """Disabled v2 drafts neither dispatch nor suppress the legacy fallback."""
    import inspect

    from core.scheduler import _dispatch_nightly_datastreams, dispatch_nightly

    dispatch_source = inspect.getsource(_dispatch_nightly_datastreams)
    fallback_source = inspect.getsource(dispatch_nightly)
    assert "WHERE ds.enabled = TRUE" in dispatch_source
    assert "SELECT DISTINCT project_id FROM app.datastreams WHERE enabled = TRUE" in fallback_source
    assert "current_plan_version_id" not in dispatch_source


# ---------------------------------------------------------------------------
# Story 12.6: hourly recurring dispatch (today-inclusive window).
# ---------------------------------------------------------------------------


class TestDispatchHourlyDatastreams:
    def _hourly_row(self, **over):
        row = {
            "ds_id": "ds_h1",
            "project_id": "proj_a",
            "module_name": "google-analytics",
            "refetch_days": 3,
            "date_window_days": None,
            "source_kind": "connector_pull",
            "connection_ref_id": "conn_x",
            "cr_status": "active",
            "cr_enabled": True,
        }
        row.update(over)
        return row

    def test_hourly_window_is_today_inclusive(self):
        from core.scheduler import _dispatch_hourly_datastreams

        as_of = date(2026, 7, 12)  # today
        window = 3
        expected_from = (as_of - timedelta(days=window - 1)).isoformat()
        expected_to = as_of.isoformat()  # today INCLUSIVE (not yesterday)

        row = self._hourly_row(refetch_days=window)
        get_conn, conn, cur = _make_get_connection()
        cols = list(row.keys())
        cur.description = [(c,) for c in cols]
        cur.fetchall.return_value = [tuple(row[c] for c in cols)]
        q = _make_mock_queue()

        jobs, count = _dispatch_hourly_datastreams(as_of, "scheduler", q, get_conn)

        assert count == 1 and len(jobs) == 1
        q.enqueue_pull.assert_called_once_with(
            "conn_x", expected_from, expected_to,
            requested_by="scheduler", datastream_id="ds_h1",
        )

    def test_hourly_excludes_external_bq(self):
        from core.scheduler import _dispatch_hourly_datastreams

        row = self._hourly_row(ds_id="ds_ext", source_kind="external_bq")
        get_conn, conn, cur = _make_get_connection()
        cols = list(row.keys())
        cur.description = [(c,) for c in cols]
        cur.fetchall.return_value = [tuple(row[c] for c in cols)]
        q = _make_mock_queue()

        jobs, count = _dispatch_hourly_datastreams(date(2026, 7, 12), "scheduler", q, get_conn)

        assert count == 0
        q.enqueue_pull.assert_not_called()

    def test_hourly_skips_inactive_connection(self):
        from core.scheduler import _dispatch_hourly_datastreams

        row = self._hourly_row(cr_status="revoked")
        get_conn, conn, cur = _make_get_connection()
        cols = list(row.keys())
        cur.description = [(c,) for c in cols]
        cur.fetchall.return_value = [tuple(row[c] for c in cols)]
        q = _make_mock_queue()

        jobs, count = _dispatch_hourly_datastreams(date(2026, 7, 12), "scheduler", q, get_conn)

        assert count == 0
        q.enqueue_pull.assert_not_called()


# ---------------------------------------------------------------------------
# Story 12.10: managed-feed sync dispatch step (env-guarded, adapter=None Phase-B).
# ---------------------------------------------------------------------------


class TestManagedFeedSyncStep:
    def test_skipped_when_flag_off(self, monkeypatch):
        from core import scheduler

        monkeypatch.delenv("MANAGED_FEED_SYNC_ENABLED", raising=False)
        called = {"db": False}

        def _fail_get_connection():
            called["db"] = True
            raise AssertionError("must not read DB when flag is off")

        monkeypatch.setattr("core.db.get_connection", _fail_get_connection, raising=False)
        scheduler._run_managed_feed_syncs()  # returns cleanly, no DB read
        assert called["db"] is False

    def test_dispatches_per_enabled_schedule_with_none_adapter(self, monkeypatch):
        from core import scheduler

        monkeypatch.setenv("MANAGED_FEED_SYNC_ENABLED", "true")
        sched_row = {
            "datastream_id": "ds_sheet",
            "project_id": "proj_a",
            "connection_id": "conn_g",
            "spreadsheet_id": "sheet_1",
            "sheet_range": "A:F",
            "sheet_name": "Budgets",
            "column_mapping": {"date_column": "date"},
            "cadence_mode": "daily",
            "cadence_policy": {"mode": "daily"},
            "quota_profile": {},
            "last_watermark": None,
        }
        get_conn, conn, cur = _make_get_connection()
        cols = list(sched_row.keys())
        cur.description = [(c,) for c in cols]
        cur.fetchall.return_value = [tuple(sched_row[c] for c in cols)]
        monkeypatch.setattr("core.db.get_connection", get_conn, raising=False)

        calls = []

        def _fake_dispatch(**kwargs):
            calls.append(kwargs)
            return {"outcome": "validated_pending_publication"}

        monkeypatch.setattr(
            "core.google_sheets_sync.dispatch_managed_feed_sync", _fake_dispatch, raising=False
        )

        scheduler._run_managed_feed_syncs()

        assert len(calls) == 1
        assert calls[0]["datastream_id"] == "ds_sheet"
        assert calls[0]["sheets_adapter"] is None  # PHASE_B_LIVE_BLOCKED
        assert calls[0]["actor"] == "scheduler"
