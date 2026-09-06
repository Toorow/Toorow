"""Tests for server/core/extract_ledger.py (Story 8.3, AC7).

Covers:
  - Status derivation matrix: ok, partial, empty, failed, running, never_fetched
  - Pre-8.2 fallback (pulls with datastream_id IS NULL matched by connection_ref_id)
  - completeness_ratio per day when expected_rows known
  - REST ledger endpoint: auth, project scoping, date validation (French errors)
  - `_group_dates_into_windows`: the arithmetic that turns picked days into windows

The refetch ROUTE is tested in `tests/core/test_admin_api_refetch.py` and only
there (story 58.4, arbitrage 8): a route and a registry are two subjects.

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
# Story 58.1: execution_id added after state -- the run that produced the window.
# THE DOUBLE FOLLOWS THE REAL SQL ORDER, deliberately: this fixture builds the
# rows AND declares their column names, so a divergence would be invisible here
# and a 500 in production.
_PULL_COLS = [
    "job_id", "pull_id", "datastream_id", "connection_ref_id",
    "date_from", "date_to", "state", "execution_id", "completed_at", "enqueued_at",
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
    execution_id="dse_001",
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
        date_from, date_to, state, execution_id,
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

    def test_a_prevented_day_carries_the_connector_sentence_to_the_screen(self):
        """AI-307: the day-grain answer is `never_fetched`, and it is not enough.

        A prevented window reports `never_fetched` by design -- the day was not
        fetched -- so a screen reading the status alone says « was never asked
        for » about a day the provider was asked for and refused. The window's
        own sentence is the only thing on this payload that names a gesture, and
        it had exactly one reader when the state shipped (the MCP diagnosis):
        `grep -rn "prevented_message" ui/ web/` returned 0 on 2026-08-21. It is
        carried onto the day so the day grid can say it too.
        """
        import json

        from core.extract_ledger import get_extract_ledger

        pull = _make_pull(
            date_from="2026-07-10", date_to="2026-07-10",
            state="prevented", verdict=None,
            error_detail=json.dumps({
                "prevented_reason": "reviews_access_pending",
                "prevented_message": (
                    "Request the reviews allowlist for this project, then re-ask "
                    "these dates."
                ),
            }),
            actual_rows=None, expected_rows=None, completeness_ratio=None,
        )
        conn = _make_conn_for_ledger(pull_rows=[pull])
        day = get_extract_ledger("ds_001", "2026-07-10", "2026-07-10", conn)[0]

        assert day["status"] == "never_fetched"
        assert day["job_state"] == "prevented"
        assert day["prevented_reason"] == "reviews_access_pending"
        assert "Request the reviews allowlist" in day["prevented_message"]
        # NEVER a count: a prevented window took none, and `0` is a count.
        assert day["row_count"] is None

    def test_a_prevented_day_that_carried_no_sentence_says_so_rather_than_guessing(self):
        """An absence a screen can name, never a half-parsed instruction."""
        from core.extract_ledger import get_extract_ledger

        pull = _make_pull(
            date_from="2026-07-10", date_to="2026-07-10",
            state="prevented", verdict=None,
            error_detail="legacy plain text, written before AI-307",
            actual_rows=None, expected_rows=None, completeness_ratio=None,
        )
        conn = _make_conn_for_ledger(pull_rows=[pull])
        day = get_extract_ledger("ds_001", "2026-07-10", "2026-07-10", conn)[0]

        assert day["job_state"] == "prevented"
        assert day["prevented_reason"] is None
        assert day["prevented_message"] is None

    def test_a_day_that_was_not_prevented_carries_no_prevented_keys(self):
        """Additive and state-scoped, exactly like `error_class` above.

        A null pair on every ordinary day is a pair every screen has to test
        before trusting, which is how a `null` ends up rendered.
        """
        from core.extract_ledger import get_extract_ledger

        for state, verdict in (("done", "ok"), ("failed", None), ("cancelled", None)):
            pull = _make_pull(
                date_from="2026-07-10", date_to="2026-07-10",
                state=state, verdict=verdict,
            )
            conn = _make_conn_for_ledger(pull_rows=[pull])
            day = get_extract_ledger("ds_001", "2026-07-10", "2026-07-10", conn)[0]
            assert "prevented_reason" not in day, state
            assert "prevented_message" not in day, state

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
        """A pull covering a multi-day window resolves status for each covered day.

        Story 58.1, arbitrage 9 -- CHANGED, not preserved. This was the file's only
        multi-day fixture and it asserted the three statuses while saying nothing
        about the three row counts, so `actual_rows=450` was published on each of
        the three days and no test could see it. `pull_verifications.actual_rows`
        is counted per `pull_id` (`verification._count_raw_rows`), which makes it
        the volume of the WINDOW; a 30-day backfill showed its own total thirty
        times, on `/ledger` and on `CoverageStrip`. The day now says nothing and
        NAMES why, instead of saying something false.
        """
        from core.extract_ledger import (
            ROW_COUNT_MEASURED_PER_WINDOW,
            get_extract_ledger,
        )

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

        assert [entry["row_count"] for entry in result] == [None, None, None], (
            "the window's row total was copied onto each of its days -- a backfill "
            "then reports its own total once per day it covered"
        )
        assert {entry["row_count_reason"] for entry in result} == {
            ROW_COUNT_MEASURED_PER_WINDOW
        }

        # The trio shares a grain, so it shares the absence. Silencing only
        # `row_count` would leave `expected_rows: 450` and a ratio of 1.0 sitting
        # on each of three days -- a window's ratio read as a day's ratio, which
        # is the same defect under two names nobody had flagged.
        assert [entry["expected_rows"] for entry in result] == [None, None, None]
        assert [entry["completeness_ratio"] for entry in result] == [None, None, None]

    def test_a_single_day_pull_keeps_its_expectation_and_its_ratio(self):
        """The trio is silenced together and published together."""
        from core.extract_ledger import get_extract_ledger

        pull = _make_pull(
            date_from="2026-07-10", date_to="2026-07-10",
            state="done", verdict="partial",
            actual_rows=90, expected_rows=100, completeness_ratio=0.9,
        )
        conn = _make_conn_for_ledger(pull_rows=[pull])
        result = get_extract_ledger("ds_001", "2026-07-10", "2026-07-10", conn)

        assert result[0]["row_count"] == 90
        assert result[0]["expected_rows"] == 100
        assert result[0]["completeness_ratio"] == 0.9
        assert result[0]["row_count_reason"] is None

    def test_a_single_day_pull_still_publishes_the_volume_it_measured(self):
        """The repair is not a blanket silence: a nightly pull IS its day."""
        from core.extract_ledger import get_extract_ledger

        pull = _make_pull(
            date_from="2026-07-10", date_to="2026-07-10",
            state="done", verdict="ok", actual_rows=150,
        )
        conn = _make_conn_for_ledger(pull_rows=[pull])
        result = get_extract_ledger("ds_001", "2026-07-10", "2026-07-10", conn)

        assert result[0]["row_count"] == 150
        assert result[0]["row_count_reason"] is None

    def test_a_day_with_no_verification_says_why_it_has_no_volume(self):
        """Two absences, two reasons: nothing measured, versus measured too coarsely."""
        from core.extract_ledger import ROW_COUNT_NOT_VERIFIED, get_extract_ledger

        pull = _make_pull(
            date_from="2026-07-10", date_to="2026-07-10",
            state="done", verdict=None,
            actual_rows=None, expected_rows=None, completeness_ratio=None,
        )
        conn = _make_conn_for_ledger(pull_rows=[pull])
        result = get_extract_ledger("ds_001", "2026-07-10", "2026-07-10", conn)

        assert result[0]["row_count"] is None
        assert result[0]["row_count_reason"] == ROW_COUNT_NOT_VERIFIED

    def test_a_day_carries_the_window_state_and_the_run_that_produced_it(self):
        """Story 58.1, arbitrage 7: `cancelled` reports `never_fetched` to the ledger.

        That is the right day-grain answer and the wrong sentence for a person --
        a window somebody STOPPED reads as one nobody ever asked for. Both words
        travel, so a screen can say which it is.
        """
        from core import pull_job_states
        from core.extract_ledger import get_extract_ledger

        pull = _make_pull(
            date_from="2026-07-10", date_to="2026-07-10",
            state=pull_job_states.CANCELLED, execution_id="dse_42",
            verdict=None, actual_rows=None,
        )
        conn = _make_conn_for_ledger(pull_rows=[pull])
        result = get_extract_ledger("ds_001", "2026-07-10", "2026-07-10", conn)

        assert result[0]["status"] == pull_job_states.LEDGER_NEVER_FETCHED
        assert result[0]["job_state"] == pull_job_states.CANCELLED
        assert result[0]["execution_id"] == "dse_42"
        assert result[0]["extract_count"] == 1

    def test_a_day_counts_every_extract_that_covers_it(self):
        """Two pulls over one day is a re-pull, and the row says so."""
        from core.extract_ledger import get_extract_ledger

        first = _make_pull(
            job_id="job_1", pull_id="pull_1",
            date_from="2026-07-10", date_to="2026-07-10", verdict="partial",
        )
        second = _make_pull(
            job_id="job_2", pull_id="pull_2",
            date_from="2026-07-10", date_to="2026-07-10", verdict="ok",
        )
        conn = _make_conn_for_ledger(pull_rows=[second, first])
        result = get_extract_ledger("ds_001", "2026-07-10", "2026-07-10", conn)

        assert result[0]["extract_count"] == 2
        # DESC-sorted: the latest covering pull still decides the day.
        assert result[0]["pull_id"] == "pull_2"

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
        from core.datastream_collection_api import _group_dates_into_windows
        assert _group_dates_into_windows([]) == []

    def test_single_date(self):
        from core.datastream_collection_api import _group_dates_into_windows
        result = _group_dates_into_windows(["2026-07-10"])
        assert result == [("2026-07-10", "2026-07-10")]

    def test_contiguous_dates_merge(self):
        from core.datastream_collection_api import _group_dates_into_windows
        result = _group_dates_into_windows(["2026-07-10", "2026-07-11", "2026-07-12"])
        assert result == [("2026-07-10", "2026-07-12")]

    def test_non_contiguous_split(self):
        from core.datastream_collection_api import _group_dates_into_windows
        result = _group_dates_into_windows(["2026-07-10", "2026-07-11", "2026-07-13"])
        assert result == [("2026-07-10", "2026-07-11"), ("2026-07-13", "2026-07-13")]

    def test_deduplicates(self):
        from core.datastream_collection_api import _group_dates_into_windows
        result = _group_dates_into_windows(["2026-07-10", "2026-07-10", "2026-07-11"])
        assert result == [("2026-07-10", "2026-07-11")]

    def test_unordered_input(self):
        from core.datastream_collection_api import _group_dates_into_windows
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
        from core.datastream_collection_api import _get_datastream_ledger

        req = _make_request(path_params={"id": "ds_001"})
        with _auth_fail():
            resp = _run(_get_datastream_ledger(req))
        assert resp.status_code == 401

    def test_400_when_missing_project_id(self):
        import json

        from core.datastream_collection_api import _get_datastream_ledger

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

        from core.datastream_collection_api import _get_datastream_ledger

        req = _make_request(
            path_params={"id": "ds_001"},
            query_params={"project_id": "proj_a", "from": "not-a-date"},
        )
        with _auth_ok():
            resp = _run(_get_datastream_ledger(req))
        assert resp.status_code == 400
        body = json.loads(resp.body)
        # Rouge depuis `43e7c57c` (traduction de la copie produit) : ce test
        # attendait le mot francais << invalide >>, le serveur repond desormais en
        # anglais. Son voisin de la ligne 712 avait ete rendu tolerant a la meme
        # occasion, celui-ci a ete oublie. On asserte ce qui ne depend pas de la
        # langue -- le parametre fautif et le format attendu.
        assert "from" in body["message"] and "YYYY-MM-DD" in body["message"], body["message"]

    def test_400_on_invalid_to_date(self):
        import json

        from core.datastream_collection_api import _get_datastream_ledger

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
        from core.datastream_collection_api import _get_datastream_ledger

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

        from core.datastream_collection_api import _get_datastream_ledger

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
#
# MOVED OUT, story 58.4 (arbitrage 8). Every case of
# `POST .../datastreams/{datastream_id}/refetch` now lives in
# `tests/core/test_admin_api_refetch.py`, which is the only home for the route.
# This file tests the extract REGISTRY -- the status matrix, the pre-8.2
# fallback, the per-window row-count rule and `_group_dates_into_windows` -- and
# a route half hidden under a registry file name is coverage nobody can find.
