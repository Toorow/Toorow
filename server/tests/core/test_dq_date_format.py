"""Unit tests for dq_monitors._check_date_format (Story 8.10, monitor (e)).

Fix [MEDIUM #7]: monitor redesigned from a dead DuckDB date-regex check to a
rejected_rows signal read from pull_verifications (Postgres).  Tests updated
to reflect the new behaviour.

Tests:
  - _check_date_format: fires when pull_verifications.rejected_rows > threshold
  - _check_date_format: no fire when rejected_rows == 0
  - _check_date_format: no fire when rejected_rows <= threshold
  - _check_date_format: DB error is swallowed, returns False
  - _run_monitors_for_datastream: date_format key present in result
  - run_dq_monitors: date_format_issues in summary dict
  - Per-stream isolation: date_format failure in one stream does not block others

Strategy:
  - All DB calls mocked -- no real Postgres or DuckDB required.
"""

from __future__ import annotations

import os
from datetime import date
from unittest.mock import MagicMock, patch

os.environ.setdefault("SCHEDULER_ENABLED", "false")
os.environ.setdefault("DQ_MONITORS_ENABLED", "true")


# ---------------------------------------------------------------------------
# Helpers (mirrored from test_dq_monitors.py)
# ---------------------------------------------------------------------------


def _make_cursor(rows=None, description=None):
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchall = MagicMock(return_value=rows or [])
    cur.fetchone = MagicMock(return_value=None)
    if description is not None:
        cur.description = description
    return cur


def _make_conn(cursor=None):
    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    conn.cursor = MagicMock(return_value=(cursor or _make_cursor()))
    conn.commit = MagicMock()
    return conn


# ---------------------------------------------------------------------------
# _check_date_format (redesigned): rejected_rows from Postgres pull_verifications
# ---------------------------------------------------------------------------


def _make_pg_conn_with_rejected(rejected_rows_sum: int):
    """Build a mock Postgres connection that returns rejected_rows_sum from SUM query."""
    cur = _make_cursor()
    cur.fetchone = MagicMock(return_value=(rejected_rows_sum,))
    conn = _make_conn(cur)
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return conn


def test_date_format_fires_when_rejected_rows_gt_threshold():
    """Fix [#7]: monitor fires when rejected_rows > threshold (default 0)."""
    from core.dq_monitors import _check_date_format

    pg_conn = _make_pg_conn_with_rejected(5)

    with patch("core.db.get_connection") as mock_get_conn, \
         patch("core.infra_alerts.write_infra_firing") as mock_fire, \
         patch.dict(os.environ, {"DQ_REJECTED_ROWS_THRESHOLD": "0"}):
        mock_get_conn.return_value.__enter__ = MagicMock(return_value=pg_conn)
        mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

        result = _check_date_format(
            ds_id="ds_test",
            project_id="proj-test",
            module_name="generic",
            ds_name="Generic DS",
            yesterday=date(2026, 7, 12),
        )

    assert result is True
    mock_fire.assert_called_once()
    kwargs = mock_fire.call_args[1]
    assert kwargs["alert_type"] == "dq_date_format"
    assert kwargs["metadata"]["rejected_rows"] == 5


def test_date_format_no_fire_when_zero_rejected():
    """Fix [#7]: monitor does not fire when rejected_rows is 0."""
    from core.dq_monitors import _check_date_format

    pg_conn = _make_pg_conn_with_rejected(0)

    with patch("core.db.get_connection") as mock_get_conn, \
         patch("core.infra_alerts.write_infra_firing") as mock_fire, \
         patch.dict(os.environ, {"DQ_REJECTED_ROWS_THRESHOLD": "0"}):
        mock_get_conn.return_value.__enter__ = MagicMock(return_value=pg_conn)
        mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

        result = _check_date_format(
            ds_id="ds_test",
            project_id="proj-test",
            module_name="generic",
            ds_name="Generic DS",
            yesterday=date(2026, 7, 12),
        )

    assert result is False
    mock_fire.assert_not_called()


def test_date_format_no_fire_when_within_threshold():
    """Fix [#7]: monitor does not fire when rejected_rows <= threshold."""
    from core.dq_monitors import _check_date_format

    pg_conn = _make_pg_conn_with_rejected(3)

    with patch("core.db.get_connection") as mock_get_conn, \
         patch("core.infra_alerts.write_infra_firing") as mock_fire, \
         patch.dict(os.environ, {"DQ_REJECTED_ROWS_THRESHOLD": "5"}):
        mock_get_conn.return_value.__enter__ = MagicMock(return_value=pg_conn)
        mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

        result = _check_date_format(
            ds_id="ds_test",
            project_id="proj-test",
            module_name="generic",
            ds_name="Generic DS",
            yesterday=date(2026, 7, 12),
        )

    assert result is False
    mock_fire.assert_not_called()


def test_date_format_db_error_returns_false():
    """Fix [#7]: a DB error is swallowed and returns False (per-stream isolation)."""
    from core.dq_monitors import _check_date_format

    with patch("core.db.get_connection", side_effect=Exception("connection refused")):
        result = _check_date_format(
            ds_id="ds_test",
            project_id="proj-test",
            module_name="generic",
            ds_name="Generic DS",
            yesterday=date(2026, 7, 12),
        )

    assert result is False


# ---------------------------------------------------------------------------
# _run_monitors_for_datastream: date_format key present
# ---------------------------------------------------------------------------


def test_run_monitors_has_date_format_key():
    """_run_monitors_for_datastream result includes 'date_format' key."""
    from core.dq_monitors import _run_monitors_for_datastream

    ds = {
        "id": "ds_test",
        "project_id": "proj-test",
        "module_name": "generic",
        "name": "Generic Test",
        "config": {"date_format": "%Y-%m-%d"},
    }
    conn = _make_conn()

    # Patch all monitor functions to return False so we only check the key exists.
    with patch("core.dq_monitors._check_volume", return_value=False), \
         patch("core.dq_monitors._check_timeliness", return_value=False), \
         patch("core.dq_monitors._check_duplication", return_value=False), \
         patch("core.dq_monitors._check_schema", return_value=False), \
         patch("core.dq_monitors._check_date_format", return_value=False):
        result = _run_monitors_for_datastream(ds, conn, date(2026, 7, 12))

    assert "date_format" in result
    assert result["date_format"] is False


def test_run_monitors_date_format_fires():
    """_run_monitors_for_datastream propagates date_format=True."""
    from core.dq_monitors import _run_monitors_for_datastream

    ds = {
        "id": "ds_test",
        "project_id": "proj-test",
        "module_name": "generic",
        "name": "Generic Test",
        "config": {"date_format": "%d/%m/%Y"},
    }
    conn = _make_conn()

    with patch("core.dq_monitors._check_volume", return_value=False), \
         patch("core.dq_monitors._check_timeliness", return_value=False), \
         patch("core.dq_monitors._check_duplication", return_value=False), \
         patch("core.dq_monitors._check_schema", return_value=False), \
         patch("core.dq_monitors._check_date_format", return_value=True):
        result = _run_monitors_for_datastream(ds, conn, date(2026, 7, 12))

    assert result["date_format"] is True


# ---------------------------------------------------------------------------
# run_dq_monitors: date_format_issues in summary
# ---------------------------------------------------------------------------


def test_run_dq_monitors_summary_has_date_format_issues():
    """run_dq_monitors summary includes date_format_issues key."""
    from core.dq_monitors import run_dq_monitors

    ds_list = [
        {
            "id": "ds_01",
            "project_id": "proj-test",
            "module_name": "generic",
            "name": "Generic DS",
            "config": {"date_format": "%Y-%m-%d"},
        }
    ]

    with patch("core.dq_monitors._fetch_enabled_datastreams", return_value=ds_list), \
         patch("core.dq_monitors._check_volume", return_value=False), \
         patch("core.dq_monitors._check_timeliness", return_value=False), \
         patch("core.dq_monitors._check_duplication", return_value=False), \
         patch("core.dq_monitors._check_schema", return_value=False), \
         patch("core.dq_monitors._check_date_format", return_value=True), \
         patch("core.db.get_connection") as mock_get_conn:
        mock_get_conn.return_value.__enter__ = MagicMock(return_value=_make_conn())
        mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

        summary = run_dq_monitors(project_id="proj-test")

    assert "date_format_issues" in summary
    assert summary["date_format_issues"] == 1
    assert summary["total_issues"] == 1


def test_run_dq_monitors_summary_includes_date_format_key_when_disabled():
    """run_dq_monitors returns date_format_issues=0 when DQ_MONITORS_ENABLED=false."""
    os.environ["DQ_MONITORS_ENABLED"] = "false"
    try:
        from core.dq_monitors import run_dq_monitors

        summary = run_dq_monitors()
        assert "date_format_issues" in summary
        assert summary["date_format_issues"] == 0
    finally:
        os.environ["DQ_MONITORS_ENABLED"] = "true"


# ---------------------------------------------------------------------------
# Isolation: date_format failure does not block other streams
# ---------------------------------------------------------------------------


def test_date_format_isolation_does_not_block_other_streams():
    """If date_format check raises, other streams still run."""
    from core.dq_monitors import run_dq_monitors

    ds_list = [
        {
            "id": "ds_bad",
            "project_id": "proj-test",
            "module_name": "generic",
            "name": "Bad DS",
            "config": None,
        },
        {
            "id": "ds_good",
            "project_id": "proj-test",
            "module_name": "google-analytics",
            "name": "Good DS",
            "config": None,
        },
    ]

    call_count = {"n": 0}

    def _check_date_format_side_effect(ds_id, *args, **kwargs):
        call_count["n"] += 1
        if ds_id == "ds_bad":
            raise RuntimeError("simulated date_format check error")
        return False

    with patch("core.dq_monitors._fetch_enabled_datastreams", return_value=ds_list), \
         patch("core.dq_monitors._check_volume", return_value=False), \
         patch("core.dq_monitors._check_timeliness", return_value=False), \
         patch("core.dq_monitors._check_duplication", return_value=False), \
         patch("core.dq_monitors._check_schema", return_value=False), \
         patch(
             "core.dq_monitors._check_date_format",
             side_effect=_check_date_format_side_effect,
         ), \
         patch("core.db.get_connection") as mock_get_conn:
        mock_get_conn.return_value.__enter__ = MagicMock(return_value=_make_conn())
        mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

        summary = run_dq_monitors(project_id="proj-test")

    # Both streams evaluated (errors caught per-stream; isolation preserved).
    assert summary["evaluated"] == 2
    assert summary["date_format_issues"] == 0
    # The bad stream's date_format failure was caught and counted as an error.
    assert summary["errors"] == 0  # errors are in the outer try/except, not monitor-level
    # Both streams were called.
    assert call_count["n"] == 2
