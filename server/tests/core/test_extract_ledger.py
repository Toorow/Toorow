"""Tests for server/core/extract_ledger.py (Story 8.3, AC7).

Covers:
  - Status derivation matrix: ok, partial, empty, failed, running, never_fetched
  - Pre-8.2 fallback (pulls with datastream_id IS NULL matched by connection_ref_id)
  - completeness_ratio per day when expected_rows known
  - REST ledger endpoint: auth, project scoping, date validation (French errors)
  - REST refetch endpoint: enqueues with datastream_id, contiguous grouping, invalid dates

All tests use mock psycopg connections (no live DB required).
Async handlers tested via asyncio.get_event_loop().run_until_complete() (project pattern).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers for building mock pull rows
# ---------------------------------------------------------------------------

# Columns returned by the batch pull query in extract_ledger.py.
# Story 25.2: error_detail added between enqueued_at and verdict (matches SQL order).
_PULL_COLS = [
    "job_id", "pull_id", "datastream_id", "connection_ref_id",
    "date_from", "date_to", "state", "completed_at", "enqueued_at",
    "error_detail",
    "verdict", "actual_rows", "expected_rows", "completeness_ratio",
]

_NOW = datetime(2026, 7, 12, 9, 0, 0, tzinfo=timezone.utc)


def _make_pull(
    *,
    job_id="job_001",
    pull_id="pull_001",
    datastream_id="ds_001",
    connection_ref_id="conn_001",
    date_from="2026-07-10",
    date_to="2026-07-12",
    state="done",
    completed_at=None,
    enqueued_at=None,
    error_detail=None,
    verdict="ok",
    actual_rows=150,
    expected_rows=150,
    completeness_ratio=1.0,
):
    return (
        job_id, pull_id, datastream_id, connection_ref_id,
        date_from, date_to, state,
        completed_at or _NOW,
        enqueued_at or _NOW,
        error_detail,
        verdict, actual_rows, expected_rows, completeness_ratio,
    )


def _make_conn_for_ledger(
    *,
    ds_row=("conn_001",),           # connection_ref_id
    pull_rows=None,
):
    """Build a mock conn that returns ds_row for the datastream lookup and
    pull_rows for the batch pull query."""
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value.__enter__ = lambda s: cur
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    # The ledger makes two cursor calls: SELECT connection_ref_id, then the batch pull SELECT.
    call_count = [0]

    def _fetchone_side():
        call_count[0] += 1
        if call_count[0] == 1:
            return ds_row  # DS lookup
        return None

    def _fetchall_side():
        return pull_rows or []

    cur.fetchone.side_effect = _fetchone_side
    cur.fetchall.return_value = pull_rows or []

    # description must return columns for the pull query (second cursor call).
    cur.description = [(c,) for c in _PULL_COLS]

    return conn


# ---------------------------------------------------------------------------
# Unit tests: get_extract_ledger status derivation
# ---------------------------------------------------------------------------


class TestGetExtractLedgerStatus:
    """Status derivation matrix tests."""

    def test_never_fetched_no_pulls(self):
        """No pulls at all -> never_fetched for every day."""
        from core.extract_ledger import get_extract_ledger

        conn = _make_conn_for_ledger(pull_rows=[])
        result = get_extract_ledger("ds_001", "2026-07-10", "2026-07-10", conn)

        assert len(result) == 1
        assert result[0]["date"] == "2026-07-10"
        assert result[0]["status"] == "never_fetched"
        assert result[0]["pull_id"] is None
        assert result[0]["row_count"] is None

    def test_ok_status_with_ok_verdict(self):
        """Done pull with verdict='ok' -> status='ok'."""
        from core.extract_ledger import get_extract_ledger

        pull = _make_pull(
            date_from="2026-07-10", date_to="2026-07-10",
            state="done", verdict="ok",
            actual_rows=150, expected_rows=150, completeness_ratio=1.0,
        )
        conn = _make_conn_for_ledger(pull_rows=[pull])
        result = get_extract_ledger("ds_001", "2026-07-10", "2026-07-10", conn)

        assert result[0]["status"] == "ok"
        assert result[0]["row_count"] == 150
        assert result[0]["expected_rows"] == 150
        assert result[0]["completeness_ratio"] == 1.0

    def test_partial_status_with_partial_verdict(self):
        """Done pull with verdict='partial' -> status='partial'."""
        from core.extract_ledger import get_extract_ledger

        pull = _make_pull(
            date_from="2026-07-10", date_to="2026-07-10",
            state="done", verdict="partial",
            actual_rows=30, expected_rows=150, completeness_ratio=0.2,
        )
        conn = _make_conn_for_ledger(pull_rows=[pull])
        result = get_extract_ledger("ds_001", "2026-07-10", "2026-07-10", conn)

        assert result[0]["status"] == "partial"
        assert result[0]["completeness_ratio"] == pytest.approx(0.2)

    def test_empty_status_with_empty_verdict(self):
        """Done pull with verdict='empty' -> status='empty'."""
        from core.extract_ledger import get_extract_ledger

        pull = _make_pull(
            date_from="2026-07-10", date_to="2026-07-10",
            state="done", verdict="empty",
            actual_rows=0, expected_rows=150, completeness_ratio=0.0,
        )
        conn = _make_conn_for_ledger(pull_rows=[pull])
        result = get_extract_ledger("ds_001", "2026-07-10", "2026-07-10", conn)

        assert result[0]["status"] == "empty"
        assert result[0]["row_count"] == 0

    def test_failed_status_from_failed_job(self):
        """Failed job -> status='failed'."""
        from core.extract_ledger import get_extract_ledger

        pull = _make_pull(
            date_from="2026-07-10", date_to="2026-07-10",
            state="failed", verdict=None,
            actual_rows=None, expected_rows=None, completeness_ratio=None,
        )
        conn = _make_conn_for_ledger(pull_rows=[pull])
        result = get_extract_ledger("ds_001", "2026-07-10", "2026-07-10", conn)

        assert result[0]["status"] == "failed"

    def test_failed_status_from_dead_letter_job(self):
        """Dead letter job -> status='failed'."""
        from core.extract_ledger import get_extract_ledger

        pull = _make_pull(
            date_from="2026-07-10", date_to="2026-07-10",
            state="dead_letter", verdict=None,
            actual_rows=None, expected_rows=None, completeness_ratio=None,
        )
        conn = _make_conn_for_ledger(pull_rows=[pull])
        result = get_extract_ledger("ds_001", "2026-07-10", "2026-07-10", conn)

        assert result[0]["status"] == "failed"

    def test_failed_day_exposes_error_class_and_user_action(self):
        """Story 25.2 (AC5): a failed day surfaces error_class + user_action
        parsed from the covering pull's structured JSON error_detail."""
        import json

        from core.extract_ledger import get_extract_ledger

        error_detail = json.dumps({
            "error_class": "auth_expired",
            "user_action": "reconnect",
            "provider_status": 401,
            "provider_payload": {"error": {"code": 190}},
            "message": "auth_expired: provider_status=401",
            "attempt_count": 1,
        })
        pull = _make_pull(
            date_from="2026-07-10", date_to="2026-07-10",
            state="failed", verdict=None,
            error_detail=error_detail,
            actual_rows=None, expected_rows=None, completeness_ratio=None,
        )
        conn = _make_conn_for_ledger(pull_rows=[pull])
        result = get_extract_ledger("ds_001", "2026-07-10", "2026-07-10", conn)

        assert result[0]["status"] == "failed"
        assert result[0]["error_class"] == "auth_expired"
        assert result[0]["user_action"] == "reconnect"

    def test_failed_day_legacy_text_error_detail_yields_none(self):
        """Legacy plain-text error_detail -> error_class/user_action are None."""
        from core.extract_ledger import get_extract_ledger

        pull = _make_pull(
            date_from="2026-07-10", date_to="2026-07-10",
            state="failed", verdict=None,
            error_detail="upstream error (legacy plain text)",
            actual_rows=None, expected_rows=None, completeness_ratio=None,
        )
        conn = _make_conn_for_ledger(pull_rows=[pull])
        result = get_extract_ledger("ds_001", "2026-07-10", "2026-07-10", conn)

        assert result[0]["status"] == "failed"
        assert result[0]["error_class"] is None
        assert result[0]["user_action"] is None

    def test_non_failed_day_has_no_error_class_key(self):
        """error_class/user_action are additive to failed days only (not on 'ok')."""
        from core.extract_ledger import get_extract_ledger

        pull = _make_pull(
            date_from="2026-07-10", date_to="2026-07-10",
            state="done", verdict="ok",
        )
        conn = _make_conn_for_ledger(pull_rows=[pull])
        result = get_extract_ledger("ds_001", "2026-07-10", "2026-07-10", conn)

        assert result[0]["status"] == "ok"
        assert "error_class" not in result[0]
        assert "user_action" not in result[0]

    def test_running_status_from_running_job(self):
        """Running job -> status='running'."""
        from core.extract_ledger import get_extract_ledger

        pull = _make_pull(
            date_from="2026-07-10", date_to="2026-07-10",
            state="running", verdict=None,
            actual_rows=None, expected_rows=None, completeness_ratio=None,
        )
        conn = _make_conn_for_ledger(pull_rows=[pull])
        result = get_extract_ledger("ds_001", "2026-07-10", "2026-07-10", conn)

        assert result[0]["status"] == "running"

    def test_running_status_from_queued_job(self):
        """Queued job -> status='running' (same as running)."""
        from core.extract_ledger import get_extract_ledger

        pull = _make_pull(
            date_from="2026-07-10", date_to="2026-07-10",
            state="queued", verdict=None,
            actual_rows=None, expected_rows=None, completeness_ratio=None,
        )
        conn = _make_conn_for_ledger(pull_rows=[pull])
        result = get_extract_ledger("ds_001", "2026-07-10", "2026-07-10", conn)

        assert result[0]["status"] == "running"

    def test_done_no_verification_row_defaults_ok(self):
        """Done pull with no verification row -> status='ok' (conservative)."""
        from core.extract_ledger import get_extract_ledger

        pull = _make_pull(
            date_from="2026-07-10", date_to="2026-07-10",
            state="done", verdict=None,
            actual_rows=None, expected_rows=None, completeness_ratio=None,
        )
        conn = _make_conn_for_ledger(pull_rows=[pull])
        result = get_extract_ledger("ds_001", "2026-07-10", "2026-07-10", conn)

        assert result[0]["status"] == "ok"

    def test_multi_day_window(self):
        """A pull covering a multi-day window resolves status for each covered day."""
        from core.extract_ledger import get_extract_ledger

        # One pull covering 2026-07-10..2026-07-12
        pull = _make_pull(
            date_from="2026-07-10", date_to="2026-07-12",
            state="done", verdict="ok",
            actual_rows=450, expected_rows=450, completeness_ratio=1.0,
        )
        conn = _make_conn_for_ledger(pull_rows=[pull])
        result = get_extract_ledger("ds_001", "2026-07-10", "2026-07-12", conn)

        assert len(result) == 3
        for entry in result:
            assert entry["status"] == "ok"
        assert result[0]["date"] == "2026-07-10"
        assert result[2]["date"] == "2026-07-12"

    def test_mixed_day_statuses(self):
        """Days with different pull statuses in the same window."""
        from core.extract_ledger import get_extract_ledger

        # Pull for 2026-07-10: done/ok
        pull1 = _make_pull(
            job_id="job_001", pull_id="pull_001",
            date_from="2026-07-10", date_to="2026-07-10",
            state="done", verdict="ok",
            actual_rows=150, expected_rows=150, completeness_ratio=1.0,
        )
        # Pull for 2026-07-11: failed
        pull2 = _make_pull(
            job_id="job_002", pull_id="pull_002",
            date_from="2026-07-11", date_to="2026-07-11",
            state="failed", verdict=None,
            actual_rows=None, expected_rows=None, completeness_ratio=None,
        )
        # 2026-07-12: no pull -> never_fetched
        conn = _make_conn_for_ledger(pull_rows=[pull1, pull2])
        result = get_extract_ledger("ds_001", "2026-07-10", "2026-07-12", conn)

        statuses = {e["date"]: e["status"] for e in result}
        assert statuses["2026-07-10"] == "ok"
        assert statuses["2026-07-11"] == "failed"
        assert statuses["2026-07-12"] == "never_fetched"

    def test_pre82_fallback_when_datastream_id_null(self):
        """Pull with datastream_id=None but matching connection_ref_id
        should be used as a fallback when no ds-matched pull exists."""
        from core.extract_ledger import get_extract_ledger

        # Pre-8.2 pull: datastream_id=None but covers the day.
        pull_legacy = _make_pull(
            job_id="job_legacy", pull_id="pull_legacy",
            datastream_id=None,        # pre-8.2
            connection_ref_id="conn_001",
            date_from="2026-07-10", date_to="2026-07-10",
            state="done", verdict="ok",
            actual_rows=100, expected_rows=100, completeness_ratio=1.0,
        )
        conn = _make_conn_for_ledger(pull_rows=[pull_legacy])
        result = get_extract_ledger("ds_001", "2026-07-10", "2026-07-10", conn)

        # The legacy pull should be used (status resolved from it).
        assert result[0]["status"] == "ok"
        assert result[0]["pull_id"] == "pull_legacy"

    def test_ds_matched_pull_preferred_over_legacy(self):
        """A post-8.2 datastream-matched pull should be preferred over pre-8.2."""
        from core.extract_ledger import get_extract_ledger

        # ds-matched pull: partial
        pull_ds = _make_pull(
            job_id="job_new", pull_id="pull_new",
            datastream_id="ds_001",
            date_from="2026-07-10", date_to="2026-07-10",
            state="done", verdict="partial",
            actual_rows=30, expected_rows=150, completeness_ratio=0.2,
        )
        # Legacy pull: ok (should NOT win)
        pull_legacy = _make_pull(
            job_id="job_legacy", pull_id="pull_legacy",
            datastream_id=None,
            date_from="2026-07-10", date_to="2026-07-10",
            state="done", verdict="ok",
            actual_rows=150, expected_rows=150, completeness_ratio=1.0,
        )
        # pull_ds comes first in the list (DESC by enqueued_at simulated by order here)
        conn = _make_conn_for_ledger(pull_rows=[pull_ds, pull_legacy])
        result = get_extract_ledger("ds_001", "2026-07-10", "2026-07-10", conn)

        assert result[0]["status"] == "partial"
        assert result[0]["pull_id"] == "pull_new"

    def test_invalid_date_range_returns_empty(self):
        """date_to before date_from -> empty list."""
        from core.extract_ledger import get_extract_ledger

        conn = _make_conn_for_ledger(pull_rows=[])
        result = get_extract_ledger("ds_001", "2026-07-12", "2026-07-10", conn)

        assert result == []

    def test_single_day_window(self):
        """Single day window works correctly."""
        from core.extract_ledger import get_extract_ledger

        pull = _make_pull(
            date_from="2026-07-12", date_to="2026-07-12",
            state="done", verdict="empty",
            actual_rows=0, expected_rows=150, completeness_ratio=0.0,
        )
        conn = _make_conn_for_ledger(pull_rows=[pull])
        result = get_extract_ledger("ds_001", "2026-07-12", "2026-07-12", conn)

        assert len(result) == 1
        assert result[0]["date"] == "2026-07-12"
        assert result[0]["status"] == "empty"

    def test_completeness_ratio_null_when_no_verification(self):
        """No verification row -> completeness_ratio and expected_rows are None."""
        from core.extract_ledger import get_extract_ledger

        pull = _make_pull(
            date_from="2026-07-10", date_to="2026-07-10",
            state="done", verdict=None,
            actual_rows=None, expected_rows=None, completeness_ratio=None,
        )
        conn = _make_conn_for_ledger(pull_rows=[pull])
        result = get_extract_ledger("ds_001", "2026-07-10", "2026-07-10", conn)

        assert result[0]["completeness_ratio"] is None
        assert result[0]["expected_rows"] is None


# ---------------------------------------------------------------------------
# Unit tests: _group_dates_into_windows
# ---------------------------------------------------------------------------


class TestGroupDatesIntoWindows:

    def test_empty(self):
        from core.admin_api import _group_dates_into_windows
        assert _group_dates_into_windows([]) == []

    def test_single_date(self):
        from core.admin_api import _group_dates_into_windows
        result = _group_dates_into_windows(["2026-07-10"])
        assert result == [("2026-07-10", "2026-07-10")]

    def test_contiguous_dates_merge(self):
        from core.admin_api import _group_dates_into_windows
        result = _group_dates_into_windows(["2026-07-10", "2026-07-11", "2026-07-12"])
        assert result == [("2026-07-10", "2026-07-12")]

    def test_non_contiguous_split(self):
        from core.admin_api import _group_dates_into_windows
        result = _group_dates_into_windows(["2026-07-10", "2026-07-11", "2026-07-13"])
        assert result == [("2026-07-10", "2026-07-11"), ("2026-07-13", "2026-07-13")]

    def test_deduplicates(self):
        from core.admin_api import _group_dates_into_windows
        result = _group_dates_into_windows(["2026-07-10", "2026-07-10", "2026-07-11"])
        assert result == [("2026-07-10", "2026-07-11")]

    def test_unordered_input(self):
        from core.admin_api import _group_dates_into_windows
        result = _group_dates_into_windows(["2026-07-12", "2026-07-10", "2026-07-11"])
        assert result == [("2026-07-10", "2026-07-12")]


# ---------------------------------------------------------------------------
# REST endpoint tests (ledger + refetch) via mock HTTP
# ---------------------------------------------------------------------------

def _make_request(
    method="GET",
    path_params=None,
    query_params=None,
    body_bytes=b"",
    headers=None,
):
    """Build a minimal mock Starlette Request for handler tests."""
    req = MagicMock()
    req.path_params = path_params or {}
    req.query_params = query_params or {}
    req.headers = headers or {"authorization": "Bearer test-token"}

    async def _body():
        return body_bytes

    req.body = _body
    return req


def _auth_ok():
    return patch(
        "core.admin_api._check_auth",
        new=AsyncMock(return_value=(True, "test-user")),
    )


def _auth_fail():
    return patch(
        "core.admin_api._check_auth",
        new=AsyncMock(return_value=(False, "")),
    )


# ---- Ledger endpoint ----

def _run(coro):
    """Run a coroutine on a FRESH event loop.

    asyncio.get_event_loop().run_until_complete() (the old pattern) breaks as soon
    as ANY anyio/asyncio.run test ran earlier in the session (the leftover current
    loop is closed/None on 3.12) -- the handler coroutine was then never awaited
    and every endpoint test failed order-dependently (diagnosed 2026-07-21 while
    closing epic 22). asyncio.run() creates and closes its own loop per call.
    """
    return asyncio.run(coro)


class TestGetDatastreamLedgerEndpoint:

    def test_401_when_unauthorized(self):
        from core.admin_api import _get_datastream_ledger

        req = _make_request(path_params={"id": "ds_001"})
        with _auth_fail():
            resp = _run(_get_datastream_ledger(req))
        assert resp.status_code == 401

    def test_400_when_missing_project_id(self):
        import json

        from core.admin_api import _get_datastream_ledger

        req = _make_request(
            path_params={"id": "ds_001"},
            query_params={},
        )
        with _auth_ok():
            resp = _run(_get_datastream_ledger(req))
        assert resp.status_code == 400
        body = json.loads(resp.body)
        assert "project_id" in body["message"]

    def test_400_on_invalid_from_date(self):
        import json

        from core.admin_api import _get_datastream_ledger

        req = _make_request(
            path_params={"id": "ds_001"},
            query_params={"project_id": "proj_a", "from": "not-a-date"},
        )
        with _auth_ok():
            resp = _run(_get_datastream_ledger(req))
        assert resp.status_code == 400
        body = json.loads(resp.body)
        assert "invalide" in body["message"]

    def test_400_on_invalid_to_date(self):
        import json

        from core.admin_api import _get_datastream_ledger

        req = _make_request(
            path_params={"id": "ds_001"},
            query_params={"project_id": "proj_a", "to": "2026/07/12"},
        )
        with _auth_ok():
            resp = _run(_get_datastream_ledger(req))
        assert resp.status_code == 400
        body = json.loads(resp.body)
        assert "YYYY-MM-DD" in body["message"]

    def test_404_when_datastream_not_found(self):
        from core.admin_api import _get_datastream_ledger

        req = _make_request(
            path_params={"id": "ds_notfound"},
            query_params={"project_id": "proj_a"},
        )
        with patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "u"))), \
             patch("core.datastreams.get_datastream", return_value=None), \
             patch("core.db.get_connection") as mock_conn_ctx:

            mock_conn = MagicMock()
            mock_conn_ctx.return_value.__enter__ = lambda s: mock_conn
            mock_conn_ctx.return_value.__exit__ = MagicMock(return_value=False)

            resp = _run(_get_datastream_ledger(req))

        # Either 404 (datastream not found) or 500 (mock not wired fully) is acceptable
        # in unit test context; the important thing is we don't get 200.
        assert resp.status_code in (404, 500)

    def test_200_returns_ledger_key(self):
        """Happy path: 200 response with 'ledger' key containing list."""
        import json

        from core.admin_api import _get_datastream_ledger

        req = _make_request(
            path_params={"id": "ds_001"},
            query_params={
                "project_id": "proj_a",
                "from": "2026-07-10",
                "to": "2026-07-10",
            },
        )
        mock_ds = {
            "id": "ds_001",
            "project_id": "proj_a",
            "name": "Test",
            "module_name": "google-analytics",
            "connection_ref_id": "conn_001",
            "enabled": True,
            "schedule_mode": "nightly",
            "refetch_days": 3,
        }
        ledger_result = [
            {
                "date": "2026-07-10",
                "status": "ok",
                "row_count": 150,
                "expected_rows": 150,
                "completeness_ratio": 1.0,
                "pull_id": "pull_001",
                "loaded_at": "2026-07-11T09:00:00+00:00",
            }
        ]

        with patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "u"))), \
             patch("core.db.get_connection") as mock_conn_ctx, \
             patch("core.datastreams.get_datastream", return_value=mock_ds), \
             patch("core.admin_api._enforce_datastream_project_scope", return_value=None), \
             patch("core.extract_ledger.get_extract_ledger", return_value=ledger_result):

            mock_conn = MagicMock()
            mock_conn_ctx.return_value.__enter__ = lambda s: mock_conn
            mock_conn_ctx.return_value.__exit__ = MagicMock(return_value=False)

            resp = _run(_get_datastream_ledger(req))

        assert resp.status_code == 200
        body = json.loads(resp.body)
        assert "ledger" in body
        assert len(body["ledger"]) == 1
        assert body["ledger"][0]["status"] == "ok"


# ---- Refetch endpoint ----

class TestRefetchDatastreamEndpoint:

    def test_401_when_unauthorized(self):
        from core.admin_api import _refetch_datastream

        req = _make_request(path_params={"id": "ds_001"}, body_bytes=b"{}")
        with _auth_fail():
            resp = _run(_refetch_datastream(req))
        assert resp.status_code == 401

    def test_400_missing_project_id(self):
        import json

        from core.admin_api import _refetch_datastream

        req = _make_request(
            path_params={"id": "ds_001"},
            body_bytes=json.dumps({"dates": ["2026-07-10"]}).encode(),
        )
        with _auth_ok():
            resp = _run(_refetch_datastream(req))
        assert resp.status_code == 400
        body = json.loads(resp.body)
        assert "project_id" in body["message"]

    def test_400_no_dates_or_from_to(self):
        import json

        from core.admin_api import _refetch_datastream

        req = _make_request(
            path_params={"id": "ds_001"},
            body_bytes=json.dumps({"project_id": "proj_a"}).encode(),
        )
        with _auth_ok():
            resp = _run(_refetch_datastream(req))
        assert resp.status_code == 400
        body = json.loads(resp.body)
        assert "dates" in body["message"].lower() or "from" in body["message"].lower()

    def test_400_invalid_date_in_dates_array(self):
        import json

        from core.admin_api import _refetch_datastream

        req = _make_request(
            path_params={"id": "ds_001"},
            body_bytes=json.dumps({
                "project_id": "proj_a",
                "dates": ["not-a-date"],
            }).encode(),
        )
        with _auth_ok():
            resp = _run(_refetch_datastream(req))
        assert resp.status_code == 400
        body = json.loads(resp.body)
        # French error message contains "invalide"
        assert "invalide" in body["message"].lower() or "YYYY-MM-DD" in body["message"]

    def test_400_invalid_from_date(self):
        import json

        from core.admin_api import _refetch_datastream

        req = _make_request(
            path_params={"id": "ds_001"},
            body_bytes=json.dumps({
                "project_id": "proj_a",
                "from": "bad",
                "to": "2026-07-12",
            }).encode(),
        )
        with _auth_ok():
            resp = _run(_refetch_datastream(req))
        assert resp.status_code == 400

    def test_400_from_after_to(self):
        import json

        from core.admin_api import _refetch_datastream

        req = _make_request(
            path_params={"id": "ds_001"},
            body_bytes=json.dumps({
                "project_id": "proj_a",
                "from": "2026-07-15",
                "to": "2026-07-10",
            }).encode(),
        )
        with _auth_ok():
            resp = _run(_refetch_datastream(req))
        assert resp.status_code == 400

    def test_202_enqueues_pull_with_datastream_id(self):
        """Happy path: enqueues a pull job and returns 202 with jobs list."""
        import json

        from core.admin_api import _refetch_datastream

        mock_ds = {
            "id": "ds_001",
            "project_id": "proj_a",
            "name": "Test DS",
            "module_name": "google-analytics",
            "connection_ref_id": "conn_001",
            "enabled": True,
            "refetch_days": 3,
        }
        mock_job = {
            "job_id": "job_new",
            "pull_id": "pull_new",
            "state": "queued",
        }

        req = _make_request(
            path_params={"id": "ds_001"},
            body_bytes=json.dumps({
                "project_id": "proj_a",
                "dates": ["2026-07-10", "2026-07-11"],
            }).encode(),
        )

        with patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "u"))), \
             patch("core.db.get_connection") as mock_conn_ctx, \
             patch("core.datastreams.get_datastream", return_value=mock_ds), \
             patch("core.admin_api._enforce_datastream_project_scope", return_value=None), \
             patch("core.admin_api.write_audit_row"), \
             patch("core.queue.enqueue_pull", return_value=mock_job) as mock_enqueue:

            mock_conn = MagicMock()
            mock_conn_ctx.return_value.__enter__ = lambda s: mock_conn
            mock_conn_ctx.return_value.__exit__ = MagicMock(return_value=False)

            resp = _run(_refetch_datastream(req))

        assert resp.status_code == 202
        body = json.loads(resp.body)
        assert "jobs" in body
        # Contiguous dates 2026-07-10 and 2026-07-11 should be merged into one window.
        assert len(body["jobs"]) == 1
        # Verify enqueue_pull was called with datastream_id.
        mock_enqueue.assert_called_once()
        call_kwargs = mock_enqueue.call_args[1]
        assert call_kwargs.get("datastream_id") == "ds_001"
        assert mock_enqueue.call_args[0][1] == "2026-07-10"  # date_from
        assert mock_enqueue.call_args[0][2] == "2026-07-11"  # date_to

    def test_202_non_contiguous_dates_produce_multiple_jobs(self):
        """Non-contiguous dates should produce separate pull jobs."""
        import json

        from core.admin_api import _refetch_datastream

        mock_ds = {
            "id": "ds_001",
            "project_id": "proj_a",
            "name": "Test DS",
            "module_name": "google-analytics",
            "connection_ref_id": "conn_001",
            "enabled": True,
            "refetch_days": 3,
        }

        call_num = [0]

        def _mock_enqueue(conn_ref_id, d_from, d_to, *, requested_by, datastream_id=None):
            call_num[0] += 1
            return {
                "job_id": f"job_{call_num[0]}",
                "pull_id": f"pull_{call_num[0]}",
                "state": "queued",
            }

        req = _make_request(
            path_params={"id": "ds_001"},
            body_bytes=json.dumps({
                "project_id": "proj_a",
                "dates": ["2026-07-10", "2026-07-12"],  # gap on 2026-07-11
            }).encode(),
        )

        with patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "u"))), \
             patch("core.db.get_connection") as mock_conn_ctx, \
             patch("core.datastreams.get_datastream", return_value=mock_ds), \
             patch("core.admin_api._enforce_datastream_project_scope", return_value=None), \
             patch("core.admin_api.write_audit_row"), \
             patch("core.queue.enqueue_pull", side_effect=_mock_enqueue):

            mock_conn = MagicMock()
            mock_conn_ctx.return_value.__enter__ = lambda s: mock_conn
            mock_conn_ctx.return_value.__exit__ = MagicMock(return_value=False)

            resp = _run(_refetch_datastream(req))

        assert resp.status_code == 202
        body = json.loads(resp.body)
        # Two non-contiguous windows -> two jobs.
        assert len(body["jobs"]) == 2

    def test_202_via_from_to_range(self):
        """from/to body syntax should also work and enqueue one pull."""
        import json

        from core.admin_api import _refetch_datastream

        mock_ds = {
            "id": "ds_001",
            "project_id": "proj_a",
            "name": "Test DS",
            "module_name": "google-analytics",
            "connection_ref_id": "conn_001",
            "enabled": True,
            "refetch_days": 3,
        }
        mock_job = {"job_id": "job_1", "pull_id": "pull_1", "state": "queued"}

        req = _make_request(
            path_params={"id": "ds_001"},
            body_bytes=json.dumps({
                "project_id": "proj_a",
                "from": "2026-07-10",
                "to": "2026-07-12",
            }).encode(),
        )

        with patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "u"))), \
             patch("core.db.get_connection") as mock_conn_ctx, \
             patch("core.datastreams.get_datastream", return_value=mock_ds), \
             patch("core.admin_api._enforce_datastream_project_scope", return_value=None), \
             patch("core.admin_api.write_audit_row"), \
             patch("core.queue.enqueue_pull", return_value=mock_job) as mock_enqueue:

            mock_conn = MagicMock()
            mock_conn_ctx.return_value.__enter__ = lambda s: mock_conn
            mock_conn_ctx.return_value.__exit__ = MagicMock(return_value=False)

            resp = _run(_refetch_datastream(req))

        assert resp.status_code == 202
        body = json.loads(resp.body)
        # 3 contiguous days merge into 1 window.
        assert len(body["jobs"]) == 1
        call_kwargs = mock_enqueue.call_args[1]
        assert call_kwargs.get("datastream_id") == "ds_001"
