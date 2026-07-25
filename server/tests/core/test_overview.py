"""Unit tests for server/core/overview.py (Story 8.4, AC11).

Tests:
  - get_overview returns correct kpi structure when DB returns rows
  - kpi math: today_ok - yesterday_ok -> vs_yesterday_ok_delta
  - issues_count aggregation: dead_letter_24h + failed_7d
  - volume_7d: 7 ints per datastream, missing days get 0
  - fleet row shape is correct
  - deadletters_24h project-wide count
  - partial DB failure returns safe defaults
  - _derive_ledger_status covers all state/verdict combos
"""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers to build mock cursors
# ---------------------------------------------------------------------------


def _make_cursor(rows, cols=None):
    """Return a mock cursor that yields *rows* on fetchall() / fetchone()."""
    cur = MagicMock()
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)
    if cols:
        cur.description = [(c,) for c in cols]
    cur.fetchall.return_value = rows
    cur.fetchone.return_value = rows[0] if rows else None
    return cur


def _make_conn(cursors_sequence):
    """Return a mock conn whose cursor() context manager yields cursors in order."""
    conn = MagicMock()
    conn.__enter__ = lambda s: s
    conn.__exit__ = MagicMock(return_value=False)
    iter_cursors = iter(cursors_sequence)

    def _cursor():
        cur = next(iter_cursors)
        cm = MagicMock()
        cm.__enter__ = lambda s: cur
        cm.__exit__ = MagicMock(return_value=False)
        return cm

    conn.cursor = _cursor
    return conn


# ---------------------------------------------------------------------------
# _derive_ledger_status
# ---------------------------------------------------------------------------


def test_derive_ledger_status_running():
    from core.overview import _derive_ledger_status

    assert _derive_ledger_status("running", None) == "running"
    assert _derive_ledger_status("queued", None) == "running"


def test_derive_ledger_status_failed():
    from core.overview import _derive_ledger_status

    assert _derive_ledger_status("failed", None) == "failed"
    assert _derive_ledger_status("dead_letter", "ok") == "failed"


def test_derive_ledger_status_done_verdicts():
    from core.overview import _derive_ledger_status

    assert _derive_ledger_status("done", "ok") == "ok"
    assert _derive_ledger_status("done", None) == "ok"   # conservative
    assert _derive_ledger_status("done", "partial") == "partial"
    assert _derive_ledger_status("done", "empty") == "empty"


def test_derive_ledger_status_unknown():
    from core.overview import _derive_ledger_status

    assert _derive_ledger_status(None, None) == "never_fetched"
    assert _derive_ledger_status("other", None) == "never_fetched"


# ---------------------------------------------------------------------------
# _query_kpis
# ---------------------------------------------------------------------------


def test_query_kpis_happy_path():
    """KPI math: today_ok=5, today_failed=2, running=1, yesterday_ok=3 -> delta=2."""
    from core.overview import _query_kpis

    kpi_row = (5, 2, 1, 3)   # today_ok, today_failed, today_running, yesterday_ok
    cur = _make_cursor([kpi_row])
    conn = _make_conn([cur])

    result = _query_kpis("proj_a", "2026-07-13", "2026-07-12", conn)
    assert result["extracts_today_ok"] == 5
    assert result["extracts_today_failed"] == 2
    assert result["extracts_running"] == 1
    assert result["vs_yesterday_ok_delta"] == 2   # 5 - 3


def test_query_kpis_negative_delta():
    """Delta is negative when today is worse than yesterday."""
    from core.overview import _query_kpis

    kpi_row = (1, 4, 0, 8)
    cur = _make_cursor([kpi_row])
    conn = _make_conn([cur])

    result = _query_kpis("proj_a", "2026-07-13", "2026-07-12", conn)
    assert result["vs_yesterday_ok_delta"] == -7   # 1 - 8


def test_query_kpis_no_rows():
    """Safe default when fetchone returns None."""
    from core.overview import _query_kpis

    cur = MagicMock()
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone.return_value = None
    conn = MagicMock()
    cm = MagicMock()
    cm.__enter__ = lambda s: cur
    cm.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value = cm

    result = _query_kpis("proj_a", "2026-07-13", "2026-07-12", conn)
    assert result["extracts_today_ok"] == 0
    assert result["vs_yesterday_ok_delta"] == 0


def test_query_kpis_db_error_returns_zeros():
    """DB exception -> zeros returned, no raise."""
    from core.overview import _query_kpis

    conn = MagicMock()
    conn.cursor.side_effect = Exception("connection refused")

    result = _query_kpis("proj_a", "2026-07-13", "2026-07-12", conn)
    assert result == {
        "extracts_today_ok": 0,
        "extracts_today_failed": 0,
        "extracts_running": 0,
        "vs_yesterday_ok_delta": 0,
    }


def test_query_kpis_legacy_pull_counted():
    """Fix [HIGH #1]: legacy pulls (datastream_id IS NULL) must count in KPI totals.

    The SQL now uses a UNION ALL CTE resolving project via connection_ref for the
    legacy path.  The mock simulates that the DB returns aggregated counts inclusive
    of legacy rows (the CTE is evaluated server-side; from Python's perspective the
    output shape is identical).
    """
    from core.overview import _query_kpis

    # Row returned includes legacy pulls counted in today_ok
    kpi_row = (7, 1, 0, 4)   # today_ok=7 includes 2 legacy pulls, delta=3
    cur = _make_cursor([kpi_row])
    conn = _make_conn([cur])

    result = _query_kpis("proj_a", "2026-07-13", "2026-07-12", conn)
    # Legacy pulls correctly inflated today_ok to 7 (would be 5 without them)
    assert result["extracts_today_ok"] == 7
    assert result["vs_yesterday_ok_delta"] == 3   # 7 - 4


# ---------------------------------------------------------------------------
# _query_volume_and_issues
# ---------------------------------------------------------------------------


def test_volume_7d_fills_missing_days_with_zeros():
    """Missing days in pull_verifications -> 0 in the 7-element list."""
    from core.overview import _query_volume_and_issues

    today = date(2026, 7, 13)
    today_str = today.isoformat()
    seven_ago_str = (today - timedelta(days=7)).isoformat()

    # Only one day of data: d-0 (today-1 = 2026-07-12)
    vol_rows = [("ds_001", "2026-07-12", 500)]

    vol_cur = _make_cursor(vol_rows)
    issues_cur = _make_cursor([])   # no issues

    cursors_seq = [vol_cur, issues_cur]
    conn = _make_conn(cursors_seq)

    volume_map, issues_map = _query_volume_and_issues(
        ["ds_001"], today_str, seven_ago_str, conn
    )

    assert len(volume_map["ds_001"]) == 7
    # Fix [HIGH #2]: date_window now uses d-7..d-1 formula, so 2026-07-12 is index 6.
    date_window = [(today - timedelta(days=7 - i)).isoformat() for i in range(7)]
    idx = date_window.index("2026-07-12")
    assert idx == 6, f"2026-07-12 (yesterday) must be index 6 (d-1), got {idx}"
    assert volume_map["ds_001"][idx] == 500
    # All other positions 0
    for i, v in enumerate(volume_map["ds_001"]):
        if i != idx:
            assert v == 0


def test_volume_7d_last_bucket_nonzero():
    """Fix [HIGH #2]: position 6 (yesterday = today-1) can be non-zero.

    The SQL now uses date_to <= yesterday instead of date_to < today, so
    yesterday's data appears in the last slot of the sparkline.
    """
    from core.overview import _query_volume_and_issues

    today = date(2026, 7, 13)
    yesterday = today - timedelta(days=1)  # 2026-07-12
    today_str = today.isoformat()
    seven_ago_str = (today - timedelta(days=7)).isoformat()

    # Yesterday's data (should land in position 6)
    vol_rows = [("ds_001", yesterday.isoformat(), 999)]

    vol_cur = _make_cursor(vol_rows)
    issues_cur = _make_cursor([])

    conn = _make_conn([vol_cur, issues_cur])
    volume_map, _ = _query_volume_and_issues(["ds_001"], today_str, seven_ago_str, conn)

    assert volume_map["ds_001"][6] == 999, (
        "Yesterday (position 6) must be non-zero -- date_to <= yesterday is now the SQL bound"
    )


def test_issues_count_distinct():
    """Fix [LOW #3]: issues_count uses COUNT DISTINCT -- returns single int from DB."""
    from core.overview import _query_volume_and_issues

    today = date(2026, 7, 13)
    today_str = today.isoformat()
    seven_ago_str = (today - timedelta(days=7)).isoformat()

    vol_rows = [("ds_001", "2026-07-12", 100)]
    # New shape after fix: (ds_id, issues_count) -- COUNT DISTINCT returns one int
    issues_rows = [("ds_001", 4)]

    vol_cur = _make_cursor(vol_rows)
    issues_cur = _make_cursor(issues_rows)

    conn = _make_conn([vol_cur, issues_cur])
    _, issues_map = _query_volume_and_issues(
        ["ds_001"], today_str, seven_ago_str, conn
    )
    # Dead_letter in both windows is counted once, not twice
    assert issues_map["ds_001"] == 4


def test_issues_count_aggregation():
    """issues_count = distinct pull_ids that are failed or dead_letter."""
    from core.overview import _query_volume_and_issues

    today = date(2026, 7, 13)
    today_str = today.isoformat()
    seven_ago_str = (today - timedelta(days=7)).isoformat()

    vol_rows = [("ds_001", "2026-07-12", 100)]
    # New shape: (ds_id, issues_count) as COUNT DISTINCT
    issues_rows = [("ds_001", 5)]

    vol_cur = _make_cursor(vol_rows)
    issues_cur = _make_cursor(issues_rows)

    conn = _make_conn([vol_cur, issues_cur])
    _, issues_map = _query_volume_and_issues(
        ["ds_001"], today_str, seven_ago_str, conn
    )
    assert issues_map["ds_001"] == 5


def test_empty_ds_ids_returns_empty_maps():
    """No datastreams -> no queries, empty dicts returned."""
    from core.overview import _query_volume_and_issues

    vol_map, iss_map = _query_volume_and_issues([], "2026-07-13", "2026-07-06", None)
    assert vol_map == {}
    assert iss_map == {}


# ---------------------------------------------------------------------------
# _query_deadletters_24h
# ---------------------------------------------------------------------------


def test_deadletters_24h_count():
    from core.overview import _query_deadletters_24h

    cur = _make_cursor([(7,)])
    conn = _make_conn([cur])

    result = _query_deadletters_24h("proj_a", conn)
    assert result == 7


def test_deadletters_24h_db_error_returns_zero():
    from core.overview import _query_deadletters_24h

    conn = MagicMock()
    conn.cursor.side_effect = Exception("timeout")

    result = _query_deadletters_24h("proj_a", conn)
    assert result == 0


# ---------------------------------------------------------------------------
# get_overview integration (mocked)
# ---------------------------------------------------------------------------


def test_get_overview_shape():
    """get_overview returns a dict with all required keys and correct fleet row shape."""
    from core.overview import get_overview

    today = date.today()
    yest_str = (today - timedelta(days=1)).isoformat()

    kpi_row = (3, 1, 0, 2)   # today_ok, today_failed, today_running, yesterday_ok

    fleet_cols = [
        "id", "name", "module_name", "enabled", "schedule_mode",
        "auth_status", "last_pull_date", "last_pull_state", "last_pull_verdict",
    ]
    fleet_rows = [
        ("ds_001", "GA Standard", "google-analytics", True, "nightly",
         "active", yest_str, "done", "ok"),
    ]

    # Cursors in order: kpi, fleet_base, volume, issues, deadletters_24h
    kpi_cur = MagicMock()
    kpi_cur.__enter__ = lambda s: s
    kpi_cur.__exit__ = MagicMock(return_value=False)
    kpi_cur.fetchone.return_value = kpi_row

    fleet_cur = MagicMock()
    fleet_cur.__enter__ = lambda s: s
    fleet_cur.__exit__ = MagicMock(return_value=False)
    fleet_cur.description = [(c,) for c in fleet_cols]
    fleet_cur.fetchall.return_value = fleet_rows

    vol_cur = MagicMock()
    vol_cur.__enter__ = lambda s: s
    vol_cur.__exit__ = MagicMock(return_value=False)
    vol_cur.fetchall.return_value = []

    issues_cur = MagicMock()
    issues_cur.__enter__ = lambda s: s
    issues_cur.__exit__ = MagicMock(return_value=False)
    issues_cur.fetchall.return_value = []

    dl_cur = MagicMock()
    dl_cur.__enter__ = lambda s: s
    dl_cur.__exit__ = MagicMock(return_value=False)
    dl_cur.fetchone.return_value = (4,)

    cursors = [kpi_cur, fleet_cur, vol_cur, issues_cur, dl_cur]
    conn = _make_conn(cursors)

    with patch("core.overview._query_health_data", return_value=("2026-07-13T02:00:00+00:00", [])):
        result = get_overview("proj_a", conn)

    # Top-level keys
    assert set(result.keys()) == {
        "kpis", "fleet", "deadletters_24h", "mirror_last_sync", "breakers",
    }

    # KPIs
    assert result["kpis"]["extracts_today_ok"] == 3
    assert result["kpis"]["extracts_today_failed"] == 1
    assert result["kpis"]["vs_yesterday_ok_delta"] == 1   # 3 - 2

    # Fleet row shape
    assert len(result["fleet"]) == 1
    row = result["fleet"][0]
    assert row["id"] == "ds_001"
    assert row["name"] == "GA Standard"
    assert row["enabled"] is True
    assert row["auth_status"] == "active"
    assert len(row["volume_7d"]) == 7
    assert row["last_extract"]["status"] == "ok"
    assert row["next_run"].startswith("Demain")

    # Infrastructure
    assert result["deadletters_24h"] == 4
    assert result["mirror_last_sync"] == "2026-07-13T02:00:00+00:00"
    assert result["breakers"] == []


def test_get_overview_manual_schedule():
    """schedule_mode=manual -> next_run='Manuel'."""
    from core.overview import get_overview

    today = date.today()
    yest_str = (today - timedelta(days=1)).isoformat()

    fleet_cols = [
        "id", "name", "module_name", "enabled", "schedule_mode",
        "auth_status", "last_pull_date", "last_pull_state", "last_pull_verdict",
    ]
    fleet_rows = [
        ("ds_002", "Manual DS", "csv", True, "manual",
         None, yest_str, "done", "ok"),
    ]

    kpi_cur = MagicMock()
    kpi_cur.__enter__ = lambda s: s
    kpi_cur.__exit__ = MagicMock(return_value=False)
    kpi_cur.fetchone.return_value = (0, 0, 0, 0)

    fleet_cur = MagicMock()
    fleet_cur.__enter__ = lambda s: s
    fleet_cur.__exit__ = MagicMock(return_value=False)
    fleet_cur.description = [(c,) for c in fleet_cols]
    fleet_cur.fetchall.return_value = fleet_rows

    vol_cur = MagicMock()
    vol_cur.__enter__ = lambda s: s
    vol_cur.__exit__ = MagicMock(return_value=False)
    vol_cur.fetchall.return_value = []

    issues_cur = MagicMock()
    issues_cur.__enter__ = lambda s: s
    issues_cur.__exit__ = MagicMock(return_value=False)
    issues_cur.fetchall.return_value = []

    dl_cur = MagicMock()
    dl_cur.__enter__ = lambda s: s
    dl_cur.__exit__ = MagicMock(return_value=False)
    dl_cur.fetchone.return_value = (0,)

    conn = _make_conn([kpi_cur, fleet_cur, vol_cur, issues_cur, dl_cur])

    with patch("core.overview._query_health_data", return_value=(None, [])):
        result = get_overview("proj_a", conn)

    assert result["fleet"][0]["next_run"] == "Manuel"
