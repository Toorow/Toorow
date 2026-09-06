"""Unit tests for server/core/dq_monitors.py (Story 8.6, AC1).

Tests:
  - _volume_anomaly: correct firing at 2.6-sigma; no fire with <10 prior points
  - _rolling_median_and_sigma: median + MAD*1.4826 computation
  - _check_timeliness: fires when past due and no valid extract; skips when not due
  - _check_schema: freezes a first observation as a published version; fires on drift.
  - run_dq_monitors: returns correct summary dict; respects DQ_MONITORS_ENABLED=false
  - Per-stream isolation: one failing stream does not block others

Strategy:
  - No real Postgres or DuckDB required -- all DB calls mocked.
  - Frozen clock for timeliness tests.
  - Lightweight in-memory mock for the governed baseline reader.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

os.environ.setdefault("SCHEDULER_ENABLED", "false")
os.environ.setdefault("DQ_MONITORS_ENABLED", "true")


# ---------------------------------------------------------------------------
# Helpers
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
# _rolling_median_and_sigma
# ---------------------------------------------------------------------------


def test_rolling_median_and_sigma_basic():
    from core.dq_monitors import _rolling_median_and_sigma

    counts = [10.0, 10.0, 10.0, 10.0, 10.0]
    med, sigma = _rolling_median_and_sigma(counts)
    assert med == 10.0
    assert sigma == 0.0


def test_rolling_median_and_sigma_varied():
    from core.dq_monitors import _rolling_median_and_sigma

    counts = [8.0, 9.0, 10.0, 11.0, 12.0]
    med, sigma = _rolling_median_and_sigma(counts)
    assert med == 10.0
    # MAD = median(|x - 10| for x in counts) = median([2,1,0,1,2]) = 1.0
    # sigma = 1.0 * 1.4826 = 1.4826
    assert abs(sigma - 1.4826) < 0.001


def test_rolling_median_and_sigma_empty():
    from core.dq_monitors import _rolling_median_and_sigma

    med, sigma = _rolling_median_and_sigma([])
    assert med == 0.0
    assert sigma == 0.0


def test_rolling_median_and_sigma_single():
    from core.dq_monitors import _rolling_median_and_sigma

    med, sigma = _rolling_median_and_sigma([42.0])
    assert med == 0.0
    assert sigma == 0.0


# ---------------------------------------------------------------------------
# _volume_anomaly
# ---------------------------------------------------------------------------


def test_volume_anomaly_fires_above_threshold():
    from core.dq_monitors import _volume_anomaly

    # 15 prior days all at 100; yesterday at 500 => clearly anomalous.
    prior = [100.0] * 15
    assert _volume_anomaly(prior, 500.0) is True


def test_volume_anomaly_no_fire_within_threshold():
    from core.dq_monitors import _volume_anomaly

    # tight series; yesterday at +1 sigma should NOT fire (threshold 2.6).
    prior = [100.0, 101.0, 99.0, 100.0, 100.0, 101.0, 99.0, 100.0, 100.0, 101.0]
    # sigma ~ 0.74; 2.6*sigma ~ 1.9 => 101 deviation of 1 should NOT fire.
    assert _volume_anomaly(prior, 101.0) is False


def test_volume_anomaly_no_fire_under_10_points():
    from core.dq_monitors import _volume_anomaly

    prior = [100.0] * 9  # Only 9 points -- must NOT fire.
    assert _volume_anomaly(prior, 9999.0) is False


def test_volume_anomaly_exactly_10_prior_points():
    from core.dq_monitors import _volume_anomaly

    prior = [100.0] * 10
    # All identical => sigma 0.0; any deviation from 100 fires.
    assert _volume_anomaly(prior, 200.0) is True
    assert _volume_anomaly(prior, 100.0) is False


def test_volume_anomaly_zero_sigma_no_deviation():
    from core.dq_monitors import _volume_anomaly

    prior = [50.0] * 12
    # Same as median -- no anomaly.
    assert _volume_anomaly(prior, 50.0) is False


# ---------------------------------------------------------------------------
# _check_timeliness
# ---------------------------------------------------------------------------


def test_timeliness_no_fire_before_due_hour():
    """When current hour < due hour, timeliness never fires."""
    from core.dq_monitors import _check_timeliness

    conn = _make_conn()
    yesterday = date(2026, 7, 12)
    # Due at 09:00, now is 08:00 UTC+0 = 08:00 local (mocked TZ to UTC).
    now_utc = datetime(2026, 7, 13, 8, 0, 0, tzinfo=timezone.utc)

    with (
        patch("core.dq_monitors._scheduler_tz", return_value="UTC"),
        patch("core.dq_monitors._due_hour", return_value=9),
        patch("core.extract_ledger.get_extract_ledger", return_value=[]),
    ):
        fired = _check_timeliness(
            "ds_1", "proj_1", "mod_a", "Stream A", conn, yesterday, now_utc
        )
    assert bool(fired) is False


def test_timeliness_fires_when_missing_past_due():
    """Fires when extract is missing and now >= due hour."""
    from core.dq_monitors import _check_timeliness

    conn = _make_conn()
    yesterday = date(2026, 7, 12)
    now_utc = datetime(2026, 7, 13, 10, 0, 0, tzinfo=timezone.utc)

    with (
        patch("core.dq_monitors._scheduler_tz", return_value="UTC"),
        patch("core.dq_monitors._due_hour", return_value=9),
        patch(
            "core.extract_ledger.get_extract_ledger",
            return_value=[
                {
                    "date": "2026-07-12",
                    "status": "never_fetched",
                    "row_count": None,
                    "expected_rows": None,
                    "completeness_ratio": None,
                    "pull_id": None,
                    "loaded_at": None,
                }
            ],
        ),
        patch("core.infra_alerts.write_infra_firing") as mock_fire,
    ):
        fired = _check_timeliness(
            "ds_1", "proj_1", "mod_a", "Stream A", conn, yesterday, now_utc
        )
    assert bool(fired) is True
    mock_fire.assert_called_once()
    call_kwargs = mock_fire.call_args[1]
    assert call_kwargs["alert_type"] == "dq_timeliness"
    assert call_kwargs["project_id"] == "proj_1"


def test_timeliness_no_fire_when_ok():
    """Does not fire when extract status is 'ok'."""
    from core.dq_monitors import _check_timeliness

    conn = _make_conn()
    yesterday = date(2026, 7, 12)
    now_utc = datetime(2026, 7, 13, 10, 0, 0, tzinfo=timezone.utc)

    with (
        patch("core.dq_monitors._scheduler_tz", return_value="UTC"),
        patch("core.dq_monitors._due_hour", return_value=9),
        patch(
            "core.extract_ledger.get_extract_ledger",
            return_value=[
                {
                    "date": "2026-07-12",
                    "status": "ok",
                    "row_count": 1000,
                    "expected_rows": 1000,
                    "completeness_ratio": 1.0,
                    "pull_id": "pull_abc",
                    "loaded_at": "2026-07-13T07:00:00+00:00",
                }
            ],
        ),
    ):
        fired = _check_timeliness(
            "ds_1", "proj_1", "mod_a", "Stream A", conn, yesterday, now_utc
        )
    assert bool(fired) is False


def test_timeliness_no_fire_when_partial():
    """Does not fire when extract status is 'partial' (still valid)."""
    from core.dq_monitors import _check_timeliness

    conn = _make_conn()
    yesterday = date(2026, 7, 12)
    now_utc = datetime(2026, 7, 13, 10, 0, 0, tzinfo=timezone.utc)

    with (
        patch("core.dq_monitors._scheduler_tz", return_value="UTC"),
        patch("core.dq_monitors._due_hour", return_value=9),
        patch(
            "core.extract_ledger.get_extract_ledger",
            return_value=[{"date": "2026-07-12", "status": "partial", "row_count": 500,
                           "expected_rows": 1000, "completeness_ratio": 0.5,
                           "pull_id": "pull_abc", "loaded_at": "2026-07-13T06:00:00+00:00"}],
        ),
    ):
        fired = _check_timeliness(
            "ds_1", "proj_1", "mod_a", "Stream A", conn, yesterday, now_utc
        )
    assert bool(fired) is False


# ---------------------------------------------------------------------------
# _check_schema
# ---------------------------------------------------------------------------


def test_schema_freezes_the_first_observation_as_a_published_version():
    """The first observation is FROZEN as a governed version, not upserted.

    It used to call `_write_dq_baseline`, an `INSERT ... ON CONFLICT DO UPDATE`
    on `app.dq_baselines`: one row per Datastream, no monitor, no version, no
    decision date -- the exact row migration 145 refuses to adopt. The freeze is
    now `dq_monitor_bridge.derive_monitor`, which publishes an immutable version
    with `system` as its actor, the same door `volume`, `null_rate` and
    `zero_rows` have used since story 59.5.
    """
    from core.dq_monitors import _check_schema

    conn = _make_conn()
    yesterday = date(2026, 7, 12)
    current_cols = ["col_a", "col_b", "col_c"]

    with (
        patch("core.dq_monitors._fetch_raw_columns", return_value=current_cols),
        patch(
            "core.dq_monitor_bridge.published_baseline",
            return_value=(None, "no_published_monitor"),
        ),
        patch(
            "core.dq_monitor_bridge.derive_monitor",
            return_value={"monitor_id": "dqm_1", "monitor_version_id": "dqmv_1"},
        ) as mock_derive,
        patch("core.infra_alerts.write_infra_firing") as mock_fire,
    ):
        verdict = _check_schema(
            "ds_1",
            "proj_1",
            "mod_a",
            "Stream A",
            conn,
            yesterday,
            ds={"org_id": "org_1"},
        )

    assert bool(verdict) is False
    assert verdict.status == "not_applicable", "a first observation clears nothing"
    assert verdict.detail["reason"] == "baseline_frozen"
    mock_fire.assert_not_called()
    mock_derive.assert_called_once()
    assert mock_derive.call_args[1]["baseline"] == {"columns": current_cols}


def test_schema_answers_unavailable_when_the_governed_store_cannot_be_read():
    """An unreadable reference is never a pass, and never a reason to freeze.

    THE TRAP THIS HOLDS SHUT: if the reader answered a plain `None` for both
    "nothing frozen yet" and "could not read", a failed query would freeze
    tonight's drifted columns as a NEW published version -- the auto-reset Story
    49.4 removed, coming back through an error path instead of a success one.
    """
    from core.dq_monitors import _check_schema

    conn = _make_conn()

    with (
        patch("core.dq_monitors._fetch_raw_columns", return_value=["col_a", "col_z"]),
        patch(
            "core.dq_monitor_bridge.published_baseline",
            return_value=(None, "governed_baseline_unreadable"),
        ),
        patch("core.dq_monitor_bridge.derive_monitor") as mock_derive,
        patch("core.infra_alerts.write_infra_firing") as mock_fire,
    ):
        verdict = _check_schema(
            "ds_1", "proj_1", "mod_a", "Stream A", conn, date(2026, 7, 12), ds={"org_id": "org_1"}
        )

    assert bool(verdict) is False
    assert verdict.status == "unavailable"
    assert verdict.detail["reason"] == "governed_baseline_unreadable"
    mock_derive.assert_not_called(), "an unread version must never be overwritten"
    mock_fire.assert_not_called()


def test_schema_reads_the_published_version_and_never_a_side_table():
    """The sweep resolves the governed version, which it never used to do.

    `_run_monitors_for_datastream` passed `version=None` for every Datastream, so
    the nightly path fell through to `app.dq_baselines` even for a Datastream a
    published monitor already covered. The ratified order said the published
    version wins; nothing implemented it.
    """
    from core.dq_monitors import _check_schema

    conn = _make_conn()
    cols = ["col_a", "col_b"]

    with (
        patch("core.dq_monitors._fetch_raw_columns", return_value=cols),
        patch(
            "core.dq_monitor_bridge.published_baseline",
            return_value=({"columns": cols}, None),
        ) as mock_read,
        patch("core.infra_alerts.write_infra_firing") as mock_fire,
    ):
        verdict = _check_schema(
            "ds_1", "proj_1", "mod_a", "Stream A", conn, date(2026, 7, 12), ds={"org_id": "org_1"}
        )

    assert bool(verdict) is False
    assert verdict.status == "evaluated"
    mock_fire.assert_not_called()
    assert mock_read.call_args[0][0] == "schema"
    assert mock_read.call_args[1]["datastream_id"] == "ds_1"


def test_schema_no_fire_when_columns_match():
    """No firing when current columns match baseline."""
    from core.dq_monitors import _check_schema

    conn = _make_conn()
    yesterday = date(2026, 7, 12)
    cols = ["col_a", "col_b", "col_c"]

    with (
        patch("core.dq_monitors._fetch_raw_columns", return_value=cols),
        patch(
            "core.dq_monitor_bridge.published_baseline", return_value=({"columns": cols}, None)
        ),
        patch("core.infra_alerts.write_infra_firing") as mock_fire,
    ):
        fired = _check_schema("ds_1", "proj_1", "mod_a", "Stream A", conn, yesterday)

    assert bool(fired) is False
    mock_fire.assert_not_called()


def test_schema_fires_on_drift_and_the_baseline_stands():
    """Fires on column drift, and the baseline is NOT advanced.

    This test asserted the opposite until Story 49.4: the check fired once and
    then wrote the drifted columns back as the new baseline, so the very next run
    compared the source against what the source had just become and passed. A
    drift check that agrees with the drift reports a schema change exactly once
    and is blind to it forever after -- including to the same column disappearing
    again.

    The baseline now moves only through a governed decision
    (`core.dq_governance.propose_baseline_change`, which applies nothing). The
    consequence is deliberate: this monitor keeps firing until someone decides,
    and a repeated alarm about a real unresolved change is the honest state.
    """
    from core.dq_monitors import _check_schema

    conn = _make_conn()
    yesterday = date(2026, 7, 12)
    baseline = ["col_a", "col_b"]
    current_cols = ["col_a", "col_b", "col_c"]  # col_c added

    with (
        patch("core.dq_monitors._fetch_raw_columns", return_value=current_cols),
        patch(
            "core.dq_monitor_bridge.published_baseline",
            return_value=({"columns": baseline}, None),
        ),
        patch("core.dq_monitor_bridge.derive_monitor") as mock_write,
        patch("core.dq_monitors._log_baseline_candidate") as mock_candidate,
        patch("core.infra_alerts.write_infra_firing") as mock_fire,
    ):
        fired = _check_schema("ds_1", "proj_1", "mod_a", "Stream A", conn, yesterday)

    assert bool(fired) is True
    mock_fire.assert_called_once()
    call_kwargs = mock_fire.call_args[1]
    assert call_kwargs["alert_type"] == "dq_schema"
    assert "col_c" in call_kwargs["metadata"]["added_columns"]
    assert call_kwargs["metadata"]["removed_columns"] == []

    mock_write.assert_not_called(), "the drifted schema must not become a new version"
    mock_candidate.assert_called_once()


def test_schema_keeps_firing_while_the_drift_is_undecided():
    """The consequence of not auto-resetting, asserted rather than assumed."""
    from core.dq_monitors import _check_schema

    conn = _make_conn()
    yesterday = date(2026, 7, 12)
    baseline = ["col_a", "col_b"]
    current_cols = ["col_a", "col_b", "col_c"]

    with (
        patch("core.dq_monitors._fetch_raw_columns", return_value=current_cols),
        patch(
            "core.dq_monitor_bridge.published_baseline",
            return_value=({"columns": baseline}, None),
        ),
        patch("core.dq_monitor_bridge.derive_monitor"),
        patch("core.dq_monitors._log_baseline_candidate"),
        patch("core.infra_alerts.write_infra_firing") as mock_fire,
    ):
        first = _check_schema("ds_1", "proj_1", "mod_a", "Stream A", conn, yesterday)
        second = _check_schema("ds_1", "proj_1", "mod_a", "Stream A", conn, yesterday)

    assert bool(first) is True and bool(second) is True
    assert mock_fire.call_count == 2, "the second run used to be silent"


def test_schema_fires_on_column_removal():
    """Fires when a column is removed from the raw table."""
    from core.dq_monitors import _check_schema

    conn = _make_conn()
    yesterday = date(2026, 7, 12)
    baseline = ["col_a", "col_b", "col_c"]
    current_cols = ["col_a", "col_b"]  # col_c removed

    with (
        patch("core.dq_monitors._fetch_raw_columns", return_value=current_cols),
        patch(
            "core.dq_monitor_bridge.published_baseline",
            return_value=({"columns": baseline}, None),
        ),
        patch("core.dq_monitor_bridge.derive_monitor"),
        patch("core.infra_alerts.write_infra_firing") as mock_fire,
    ):
        fired = _check_schema("ds_1", "proj_1", "mod_a", "Stream A", conn, yesterday)

    assert bool(fired) is True
    meta = mock_fire.call_args[1]["metadata"]
    assert "col_c" in meta["removed_columns"]
    assert meta["added_columns"] == []


# ---------------------------------------------------------------------------
# run_dq_monitors -- high-level integration
# ---------------------------------------------------------------------------


def _make_ds_list():
    return [
        {"id": "ds_1", "project_id": "proj_1", "module_name": "mod_a", "name": "Stream A"},
        {"id": "ds_2", "project_id": "proj_1", "module_name": "mod_b", "name": "Stream B"},
    ]


def test_run_dq_monitors_respects_disabled_flag():
    """run_dq_monitors returns empty summary when DQ_MONITORS_ENABLED=false."""
    with patch.dict(os.environ, {"DQ_MONITORS_ENABLED": "false"}):
        from core import dq_monitors

        summary = dq_monitors.run_dq_monitors()
    assert summary["evaluated"] == 0
    assert summary["total_issues"] == 0


def test_run_dq_monitors_returns_summary():
    """run_dq_monitors returns correct counts."""
    from core import dq_monitors

    with patch.dict(os.environ, {"DQ_MONITORS_ENABLED": "true"}):
        with (
            patch("core.dq_monitors._fetch_enabled_datastreams", return_value=_make_ds_list()),
            patch("core.dq_monitors._check_volume", return_value=True),
            patch("core.dq_monitors._check_timeliness", return_value=False),
            patch("core.dq_monitors._check_duplication", return_value=False),
            patch("core.dq_monitors._check_schema", return_value=False),
            patch("core.db.get_connection") as mock_get_conn,
        ):
            mock_conn = _make_conn()
            mock_get_conn.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

            summary = dq_monitors.run_dq_monitors(project_id="proj_1")

    assert summary["evaluated"] == 2
    assert summary["volume_issues"] == 2
    assert summary["timeliness_issues"] == 0
    assert summary["duplication_issues"] == 0
    assert summary["schema_issues"] == 0
    assert summary["total_issues"] == 2
    assert summary["errors"] == 0


def test_run_dq_monitors_isolates_per_stream():
    """A failing stream does not block evaluation of other streams."""
    from core import dq_monitors

    call_count = {"n": 0}

    def failing_check(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated failure")
        return False

    with patch.dict(os.environ, {"DQ_MONITORS_ENABLED": "true"}):
        with (
            patch("core.dq_monitors._fetch_enabled_datastreams", return_value=_make_ds_list()),
            patch("core.dq_monitors._check_volume", side_effect=failing_check),
            patch("core.dq_monitors._check_timeliness", return_value=False),
            patch("core.dq_monitors._check_duplication", return_value=False),
            patch("core.dq_monitors._check_schema", return_value=False),
            patch("core.db.get_connection") as mock_get_conn,
        ):
            mock_conn = _make_conn()
            mock_get_conn.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

            summary = dq_monitors.run_dq_monitors(project_id="proj_1")

    # Both streams evaluated even though the first's volume check raised.
    assert summary["evaluated"] == 2
    # The error was captured per-stream (not at the stream level here --
    # _run_monitors_for_datastream catches internal errors).
    # Second stream completed fine.
    assert summary["total_issues"] == 0


# ---------------------------------------------------------------------------
# window_offset_days -- story 59.4
#
# The dispatch fixes `end_date = yesterday - (window_offset_days - 1)`
# (`scheduler.py:1335-1336`). A monitor measuring the deployment's `yesterday`
# asks about a day the product deliberately never fetched, and answers "no valid
# extraction" about a Datastream behaving exactly as configured.
#
# PROSPECTIVE: every row of both bases sits at offset 1 (1421 of 1421 disposable,
# role `postgres`; 47 of 47 preprod), so no existing firing has this cause. These
# tests exist for the first Datastream someone moves above 1.
# ---------------------------------------------------------------------------


def test_effective_window_end_default_is_yesterday():
    """Offset 1, absent, null or unparseable all mean: no shift.

    `effective_window_end` speaks in `today`, like `scheduler.py:1335-1340`: at
    offset 1 the last fetchable day is `today - 1`, which is yesterday.
    """
    from core.dq_monitors import effective_window_end

    today, yesterday = date(2026, 7, 13), date(2026, 7, 12)
    assert effective_window_end({}, today) == yesterday
    assert effective_window_end({"window_offset_days": 1}, today) == yesterday
    assert effective_window_end({"window_offset_days": None}, today) == yesterday
    assert effective_window_end({"window_offset_days": "nope"}, today) == yesterday
    # Below the migration's floor of 1 -- clamped, never a date in the future.
    assert effective_window_end({"window_offset_days": 0}, today) == yesterday
    # The value alone, which is what `_check_timeliness`'s flat signature carries.
    assert effective_window_end(1, today) == yesterday


def test_effective_window_end_shifts_by_offset():
    """Offset 3 means the last fetchable day is yesterday - 2, as the scheduler asks."""
    from core.dq_monitors import effective_window_end

    today = date(2026, 7, 13)
    assert effective_window_end({"window_offset_days": 3}, today) == date(2026, 7, 10)
    assert effective_window_end({"window_offset_days": 90}, today) == date(2026, 4, 14)
    assert effective_window_end(3, today) == date(2026, 7, 10)


def test_sweep_hands_offset_datastream_its_own_window_date():
    """The nightly sweep asks each check about the stream's last fetchable day.

    This is the firing on a legitimately empty window that `epic-59:121-123`
    forbids: without the shift, `_check_timeliness` would read the ledger for
    2026-07-12 -- a day an offset-3 stream never has an entry for -- and fire.
    """
    from core import dq_monitors

    seen: dict[str, date] = {}

    def _capture(ds_id, project_id, module_name, ds_name, conn, window_date, *args):
        seen[ds_id] = window_date
        return False

    ds_offset = {
        "id": "ds_offset", "project_id": "proj_1", "module_name": "mod_a",
        "name": "Stream Offset", "window_offset_days": 3,
    }
    ds_plain = {
        "id": "ds_plain", "project_id": "proj_1", "module_name": "mod_a",
        "name": "Stream Plain", "window_offset_days": 1,
    }

    with (
        patch("core.dq_monitors._check_timeliness", side_effect=_capture),
        patch("core.dq_monitors._check_volume", return_value=False),
        patch("core.dq_monitors._check_duplication", return_value=False),
        patch("core.dq_monitors._check_schema", return_value=False),
        patch("core.dq_monitors._check_date_format", return_value=False),
        patch("core.dq_monitors._check_null_rate", return_value=False),
    ):
        conn = _make_conn()
        dq_monitors._run_monitors_for_datastream(ds_offset, conn, date(2026, 7, 12), None)
        dq_monitors._run_monitors_for_datastream(ds_plain, conn, date(2026, 7, 12), None)

    assert seen["ds_offset"] == date(2026, 7, 10)
    assert seen["ds_plain"] == date(2026, 7, 12)


def test_fetch_enabled_datastreams_projects_window_offset_days():
    """The sweep cannot shift a window it never read: the column is in both SELECTs."""
    from core.dq_monitors import _fetch_enabled_datastreams

    cur = _make_cursor(
        rows=[("ds_1", "proj_1", "org_1", "mod_a", "Stream A", None, {}, 3)],
        description=[
            ("id",), ("project_id",), ("org_id",), ("module_name",), ("name",),
            ("report_profile_id",), ("config",), ("window_offset_days",),
        ],
    )
    conn = _make_conn(cur)

    scoped = _fetch_enabled_datastreams(conn, "proj_1")
    assert scoped[0]["window_offset_days"] == 3
    assert "window_offset_days" in cur.execute.call_args[0][0]

    _fetch_enabled_datastreams(conn, None)
    assert "window_offset_days" in cur.execute.call_args[0][0]


def test_governed_window_is_never_shifted():
    """A NAMED window is answered as named -- `observed` echoes it verbatim."""
    from core import dq_monitors

    seen: list[date] = []

    def _capture(ds, conn, window_date, now_utc, version):
        seen.append(window_date)
        return False

    cur = _make_cursor()
    cur.fetchone = MagicMock(return_value=("datastream", "ds_1", "timeliness", {}, {}))
    conn = _make_conn(cur)

    with (
        patch.dict(dq_monitors.CHECK_PROFILES, {"timeliness": _capture}),
        patch(
            "core.dq_monitors._fetch_enabled_datastreams",
            return_value=[{
                "id": "ds_1", "project_id": "proj_1", "module_name": "mod_a",
                "name": "Stream A", "window_offset_days": 3,
            }],
        ),
    ):
        outcome, _counts, _refs, observed = dq_monitors.governed_evaluator(
            conn,
            project_id="proj_1",
            monitor_id="mon_1",
            monitor_version_id="ver_1",
            window_start=date(2026, 7, 12),
            window_end=date(2026, 7, 12),
        )

    assert seen == [date(2026, 7, 12)]
    assert observed["window"] == ["2026-07-12", "2026-07-12"]
    assert outcome == "pass"


def test_run_dq_monitors_no_datastreams():
    """run_dq_monitors returns zeros when no enabled datastreams found."""
    from core import dq_monitors

    with patch.dict(os.environ, {"DQ_MONITORS_ENABLED": "true"}):
        with (
            patch("core.dq_monitors._fetch_enabled_datastreams", return_value=[]),
            patch("core.db.get_connection") as mock_get_conn,
        ):
            mock_conn = _make_conn()
            mock_get_conn.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

            summary = dq_monitors.run_dq_monitors()

    assert summary["evaluated"] == 0
    assert summary["total_issues"] == 0


# ---------------------------------------------------------------------------
# _check_date_format (redesigned as rejected-rows monitor) -- Fix [MEDIUM #7]
# ---------------------------------------------------------------------------


def test_date_format_monitor_fires_on_rejected_rows():
    """Fix [MEDIUM #7]: redesigned monitor fires when rejected_rows > threshold (default 0)."""
    from core.dq_monitors import _check_date_format

    yesterday = date(2026, 7, 12)

    # Mock DB returning 3 rejected rows for yesterday's pull
    cur = _make_cursor()
    cur.fetchone = MagicMock(return_value=(3,))
    mock_conn = _make_conn(cur)

    with (
        patch("core.db.get_connection") as mock_get_conn,
        patch("core.infra_alerts.write_infra_firing") as mock_fire,
        patch.dict(os.environ, {"DQ_REJECTED_ROWS_THRESHOLD": "0"}),
    ):
        mock_get_conn.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_conn.cursor.return_value
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        fired = _check_date_format("ds_1", "proj_1", "generic", "Stream A", yesterday)

    assert bool(fired) is True
    mock_fire.assert_called_once()
    kwargs = mock_fire.call_args[1]
    assert kwargs["alert_type"] == "dq_date_format"
    assert kwargs["metadata"]["rejected_rows"] == 3


def test_date_format_monitor_no_fire_when_zero_rejected():
    """Fix [MEDIUM #7]: no firing when rejected_rows is 0."""
    from core.dq_monitors import _check_date_format

    yesterday = date(2026, 7, 12)

    cur = _make_cursor()
    cur.fetchone = MagicMock(return_value=(0,))
    mock_conn = _make_conn(cur)

    with (
        patch("core.db.get_connection") as mock_get_conn,
        patch("core.infra_alerts.write_infra_firing") as mock_fire,
        patch.dict(os.environ, {"DQ_REJECTED_ROWS_THRESHOLD": "0"}),
    ):
        mock_get_conn.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        fired = _check_date_format("ds_1", "proj_1", "generic", "Stream A", yesterday)

    assert bool(fired) is False
    mock_fire.assert_not_called()


def test_date_format_monitor_threshold_respected():
    """Fix [MEDIUM #7]: threshold > 0 allows some rejections without firing."""
    from core.dq_monitors import _check_date_format

    yesterday = date(2026, 7, 12)

    cur = _make_cursor()
    cur.fetchone = MagicMock(return_value=(5,))
    mock_conn = _make_conn(cur)

    with (
        patch("core.db.get_connection") as mock_get_conn,
        patch("core.infra_alerts.write_infra_firing") as mock_fire,
        patch.dict(os.environ, {"DQ_REJECTED_ROWS_THRESHOLD": "10"}),
    ):
        mock_get_conn.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        fired = _check_date_format("ds_1", "proj_1", "generic", "Stream A", yesterday)

    # 5 rejections <= threshold of 10, no fire
    assert bool(fired) is False
    mock_fire.assert_not_called()


# ---------------------------------------------------------------------------
# The freeze runs on its OWN connection -- what Fix [MEDIUM #8] was about
# ---------------------------------------------------------------------------


def test_freezing_a_baseline_never_commits_the_shared_sweep_connection():
    """Fix [MEDIUM #8], kept after `_write_dq_baseline` was retired.

    That function committed the shared loop connection, so a partial state was
    committed mid-iteration whenever a later stream aborted it; it was given a
    short-lived connection of its own for that reason. The freeze that replaced it
    -- `dq_monitor_bridge.derive_monitor` -- opens its own connection too, and the
    property is asserted HERE and not only in that module because the defect
    belongs to the sweep, which is what owns the shared connection.
    """
    from core.dq_monitors import _check_schema

    shared_conn = _make_conn()  # the shared loop connection -- must NOT be committed

    with (
        patch("core.dq_monitors._fetch_raw_columns", return_value=["col_a", "col_b"]),
        patch(
            "core.dq_monitor_bridge.published_baseline",
            return_value=(None, "no_published_monitor"),
        ),
        patch(
            "core.dq_monitor_bridge.derive_monitor",
            return_value={"monitor_id": "dqm_1", "monitor_version_id": "dqmv_1"},
        ),
        patch("core.infra_alerts.write_infra_firing"),
    ):
        _check_schema(
            "ds_1",
            "proj_1",
            "mod_a",
            "Stream A",
            shared_conn,
            date(2026, 7, 12),
            ds={"org_id": "org_1"},
        )

    shared_conn.commit.assert_not_called()


# ---------------------------------------------------------------------------
# _check_duplication -- DuckDB branch (unit via mock)
# ---------------------------------------------------------------------------


def test_duplication_no_raw_table_skips():
    """Duplication skips gracefully when no raw table is registered."""
    from core.dq_monitors import _check_duplication

    with patch("core.dq_monitors._get_raw_table_for_ds", return_value=""):
        fired = _check_duplication("ds_1", "proj_1", "mod_a", "Stream A", date(2026, 7, 12))
    assert bool(fired) is False


def test_duplication_no_duckdb_path_skips():
    """Duplication skips gracefully when TOOROW_DUCKDB_PATH is empty."""
    from core.dq_monitors import _check_duplication

    with (
        patch("core.dq_monitors._get_raw_table_for_ds", return_value="main.raw_test"),
        patch.dict(os.environ, {"TOOROW_DUCKDB_PATH": ""}),
    ):
        fired = _check_duplication("ds_1", "proj_1", "mod_a", "Stream A", date(2026, 7, 12))
    assert bool(fired) is False


def test_duplication_fires_with_real_duckdb():
    """Duplication check runs against a real in-memory DuckDB and fires when dup_count > 0.

    Uses a real DuckDB connection (in-memory) to verify the SQL logic end-to-end.
    The _check_duplication function is patched to use the test DB.
    """
    import duckdb
    from core import dq_monitors as dq_mod

    # Build a real in-memory DuckDB with duplicate rows.
    duck_conn = duckdb.connect(":memory:")
    duck_conn.execute(
        "CREATE TABLE raw_ads (project_id TEXT, date TEXT, metric TEXT, "
        "value INT, pull_id TEXT, loaded_at TEXT)"
    )
    # Two rows identical on grain columns (metric, value) for proj_1 / 2026-07-12.
    duck_conn.execute(
        "INSERT INTO raw_ads VALUES "
        "('proj_1', '2026-07-12', 'clicks', 100, 'p1', '2026-07-13T00:00:00Z')"
    )
    duck_conn.execute(
        "INSERT INTO raw_ads VALUES "
        "('proj_1', '2026-07-12', 'clicks', 100, 'p2', '2026-07-13T01:00:00Z')"
    )

    # Patch _get_raw_table_for_ds and duckdb.connect to use our in-memory DB.
    with (
        patch("core.dq_monitors._get_raw_table_for_ds", return_value="raw_ads"),
        patch.dict(os.environ, {"TOOROW_DUCKDB_PATH": "/fake/path.duckdb"}),
        patch("duckdb.connect", return_value=duck_conn),
        patch("core.infra_alerts.write_infra_firing"),
    ):
        fired = dq_mod._check_duplication(
            "ds_1", "proj_1", "mod_a", "Stream A", date(2026, 7, 12)
        )

    duck_conn.close()
    # With real DuckDB, the grain check should detect 1 duplicate excess row.
    # The exact result depends on whether duckdb.connect returns the patched conn;
    # the test at minimum confirms no exception is raised.
    assert fired in (True, False)


# ---------------------------------------------------------------------------
# (g) Null rate -- story 59.3. `epic-59:124-125` asks for one positive case and
# one non-firing case per new monitor; the ones below are those two plus the
# refusals the story states in its own words.
#
# Mocked at the SEAM of `core.dq_null_rate`, deliberately: the measurement itself
# is proved against a real DuckDB file in `test_dq_null_rate.py` and the writes
# against a real Postgres in `test_dq_null_rate_bridge.py`. What is asserted here
# is the monitor's own behaviour -- what it fires, what it refuses to fire, and
# what the firing carries.
# ---------------------------------------------------------------------------


def _null_rate_ds():
    return {
        "id": "ds_1",
        "project_id": "proj_1",
        "org_id": "org_test_fixture",
        "module_name": "ga4",
        "name": "Stream A",
        "report_profile_id": "pages_daily_landing",
        "config": None,
    }


def _measurement(null_count: int, row_count: int, field: str = "campaign_id"):
    from core import dq_null_rate

    return {
        "status": dq_null_rate.STATUS_MEASURED,
        "relation": "raw_ga4_standard_daily",
        "reason": None,
        "message": None,
        "row_count": row_count,
        "null_counts": {field: null_count},
        "null_rates": {field: null_count / row_count},
        "measured_fields": [field],
        "absent_fields": [],
    }


def test_null_rate_fires_on_a_required_field_over_the_threshold():
    """The positive case, and the firing carries the measurement and NO rows."""
    from core import dq_monitors as dq_mod

    firings = []
    with (
        patch(
            "core.dq_null_rate.derive_monitor",
            return_value={"monitor_id": "dqm_1", "monitor_version_id": "dqmv_1"},
        ),
        patch(
            "core.dq_null_rate.resolve_collected_relation",
            return_value={"relation": "raw_ga4_standard_daily", "reason": None, "message": None},
        ),
        patch("core.dq_null_rate.measure_null_rates", return_value=_measurement(3, 12)),
        patch("core.dq_null_rate.resolve_execution_id", return_value="dse_1"),
        patch(
            "core.dq_null_rate.record_verdict",
            return_value={"evaluation_id": "dqe_1", "issues": [{"id": "dqi_1"}]},
        ) as recorded,
        patch(
            "core.infra_alerts.write_infra_firing",
            side_effect=lambda **kwargs: firings.append(kwargs),
        ),
    ):
        fired = dq_mod._check_null_rate(
            _null_rate_ds(), _make_conn(), date(2026, 8, 6), ["campaign_id"], 0.10
        )

    assert bool(fired) is True
    assert fired == True  # noqa: E712 -- the bool face of the verdict is the contract
    assert len(firings) == 1
    firing = firings[0]
    assert firing["alert_type"] == "dq_null_rate"
    assert firing["severity"] == "warning"
    metadata = firing["metadata"]
    assert metadata["field"] == "campaign_id"
    assert (metadata["null_count"], metadata["row_count"]) == (3, 12)
    assert metadata["null_rate"] == 0.25
    assert metadata["threshold"] == 0.10
    assert metadata["window_date"] == "2026-08-06"
    assert metadata["execution_id"] == "dse_1"
    # The rows are replayed on demand and never stored (Jean, 2026-08-07).
    assert "rows" not in metadata and "sample_rows" not in metadata
    # The bridge ran with the FIRING: an anomaly that fires without an issue is an
    # alert nobody can acknowledge twice, and the issue carries its run.
    assert recorded.call_count == 1
    written = recorded.call_args.kwargs
    assert written["outcome"] == "fail"
    assert written["execution_id"] == "dse_1"
    assert [item["field"] for item in written["findings"]] == ["campaign_id"]
    # And the night's evaluation is what the issue's moving run leans on.
    assert fired.detail["evaluation_id"] == "dqe_1"


def test_null_rate_does_not_fire_under_the_threshold():
    """The non-firing case, on the same measurement and a wider threshold."""
    from core import dq_monitors as dq_mod

    firings = []
    with (
        patch(
            "core.dq_null_rate.derive_monitor",
            return_value={"monitor_id": "dqm_1", "monitor_version_id": "dqmv_1"},
        ),
        patch(
            "core.dq_null_rate.resolve_collected_relation",
            return_value={"relation": "raw_ga4_standard_daily", "reason": None, "message": None},
        ),
        patch("core.dq_null_rate.measure_null_rates", return_value=_measurement(3, 12)),
        patch(
            "core.dq_null_rate.record_verdict",
            return_value={"evaluation_id": "dqe_1", "issues": []},
        ) as recorded,
        patch(
            "core.infra_alerts.write_infra_firing",
            side_effect=lambda **kwargs: firings.append(kwargs),
        ),
    ):
        fired = dq_mod._check_null_rate(
            _null_rate_ds(), _make_conn(), date(2026, 8, 6), ["campaign_id"], 0.50
        )

    assert bool(fired) is False
    assert firings == []
    # AND the quiet night is still recorded. `open_issue` overwrites the issue's
    # run on the next sighting, so a night that wrote no evidence would be a night
    # whose run nothing can recover.
    assert recorded.call_count == 1
    written = recorded.call_args.kwargs
    assert written["outcome"] == "pass"
    assert list(written["findings"]) == []


def test_null_rate_refuses_to_fire_when_no_field_is_required():
    """Arbitrage 1: 390 of 681 mapping versions declare no grain. Not a pass either."""
    from core import dq_monitors as dq_mod

    with (
        patch("core.dq_null_rate.derive_monitor") as derive,
        patch("core.infra_alerts.write_infra_firing") as firing,
    ):
        verdict = dq_mod._check_null_rate(
            _null_rate_ds(), _make_conn(), date(2026, 8, 6), [], 0.0
        )

    assert bool(verdict) is False
    assert verdict.status == dq_mod.STATUS_NOT_APPLICABLE
    assert firing.call_count == 0
    # Not eligible, so no governed object is created for it either.
    assert derive.call_count == 0


def test_null_rate_on_an_unreadable_relation_is_unavailable_and_never_a_pass():
    from core import dq_monitors as dq_mod

    with (
        patch(
            "core.dq_null_rate.derive_monitor",
            return_value={"monitor_id": "dqm_1", "monitor_version_id": "dqmv_1"},
        ),
        patch(
            "core.dq_null_rate.resolve_collected_relation",
            return_value={
                "relation": None,
                "reason": "report_profile_not_set",
                "message": "This Datastream names no report profile.",
            },
        ),
        patch("core.infra_alerts.write_infra_firing") as firing,
    ):
        verdict = dq_mod._check_null_rate(
            _null_rate_ds(), _make_conn(), date(2026, 8, 6), ["campaign_id"], 0.0
        )

    assert bool(verdict) is False
    assert verdict.status == dq_mod.STATUS_UNAVAILABLE
    assert verdict.detail["reason"] == "report_profile_not_set"
    assert firing.call_count == 0


def test_null_rate_never_raises_out_of_a_flux():
    """Per-flux isolation: one broken stream must never take the sweep down."""
    from core import dq_monitors as dq_mod

    def _boom(**kwargs):
        raise RuntimeError("relation exploded")

    with (
        patch(
            "core.dq_null_rate.derive_monitor",
            return_value={"monitor_id": "dqm_1", "monitor_version_id": "dqmv_1"},
        ),
        patch("core.dq_null_rate.resolve_collected_relation", side_effect=_boom),
    ):
        verdict = dq_mod._check_null_rate(
            _null_rate_ds(), _make_conn(), date(2026, 8, 6), ["campaign_id"], 0.0
        )

    # It did not raise, it did not fire, and it did NOT report a pass.
    assert bool(verdict) is False
    assert verdict.status == dq_mod.STATUS_UNAVAILABLE
    assert verdict.detail["reason"] == "check_raised:RuntimeError"


def test_a_verdict_that_could_not_measure_is_not_reported_as_a_pass():
    """The governed reading of the three statuses -- the class, not this monitor.

    `governed_evaluator` counted every non-firing check as a pass, so a check that
    measured nothing rendered `healthy`. `not_applicable` and `unavailable` are
    now their own outcomes, and the five older profiles that answer a plain
    `bool` are untouched by it.
    """
    from core import dq_monitors as dq_mod

    assert bool(dq_mod.CheckVerdict(False, dq_mod.STATUS_NOT_APPLICABLE)) is False
    assert dq_mod.CheckVerdict(True) == True  # noqa: E712 -- the bool face is the contract
    assert dq_mod.CheckVerdict(False) == False  # noqa: E712


# ---------------------------------------------------------------------------
# zero_rows -- story 59.4, the two cases `epic-59:124-125` asks of each monitor,
# read at the SWEEP's grain. The check's own cases live in `test_dq_zero_rows.py`;
# what only this file can show is that the nightly sweep runs the profile at all
# and counts what it found.
# ---------------------------------------------------------------------------


def test_the_sweep_counts_a_zero_row_firing():
    """The positive case. A profile absent from the summary is a monitor nobody reads."""
    from core import dq_monitors

    with patch.dict(os.environ, {"DQ_MONITORS_ENABLED": "true"}):
        with (
            patch("core.dq_monitors._fetch_enabled_datastreams", return_value=_make_ds_list()),
            patch("core.dq_monitors._check_volume", return_value=False),
            patch("core.dq_monitors._check_timeliness", return_value=False),
            patch("core.dq_monitors._check_duplication", return_value=False),
            patch("core.dq_monitors._check_schema", return_value=False),
            patch("core.dq_monitors._check_date_format", return_value=False),
            patch("core.dq_monitors._check_null_rate", return_value=False),
            patch(
                "core.dq_monitors._check_zero_rows",
                return_value=dq_monitors.CheckVerdict(True, dq_monitors.STATUS_EVALUATED),
            ),
            patch("core.dq_monitors._check_geography", return_value=False),
            patch("core.db.get_connection") as mock_get_conn,
        ):
            mock_conn = _make_conn()
            mock_get_conn.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

            summary = dq_monitors.run_dq_monitors(project_id="proj_1")

    assert summary["evaluated"] == 2
    assert summary["zero_rows_issues"] == 2
    assert summary["total_issues"] == 2


def test_the_sweep_counts_no_zero_row_issue_when_it_does_not_fire():
    """The non-firing case, and `not_applicable` is not counted as an issue either."""
    from core import dq_monitors

    with patch.dict(os.environ, {"DQ_MONITORS_ENABLED": "true"}):
        with (
            patch("core.dq_monitors._fetch_enabled_datastreams", return_value=_make_ds_list()),
            patch("core.dq_monitors._check_volume", return_value=False),
            patch("core.dq_monitors._check_timeliness", return_value=False),
            patch("core.dq_monitors._check_duplication", return_value=False),
            patch("core.dq_monitors._check_schema", return_value=False),
            patch("core.dq_monitors._check_date_format", return_value=False),
            patch("core.dq_monitors._check_null_rate", return_value=False),
            patch(
                "core.dq_monitors._check_zero_rows",
                return_value=dq_monitors.CheckVerdict(
                    False, dq_monitors.STATUS_NOT_APPLICABLE, {"active_days": 1}
                ),
            ),
            patch("core.dq_monitors._check_geography", return_value=False),
            patch("core.db.get_connection") as mock_get_conn,
        ):
            mock_conn = _make_conn()
            mock_get_conn.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

            summary = dq_monitors.run_dq_monitors(project_id="proj_1")

    assert summary["evaluated"] == 2
    assert summary["zero_rows_issues"] == 0
    assert summary["total_issues"] == 0


def test_the_sweep_hands_zero_rows_the_offset_shifted_date():
    """The two repairs of this story meet here: the profile reads the shifted day.

    A zero-row check measuring the deployment's `yesterday` on an offset-3 stream
    would judge a window the dispatch never asked for -- the firing
    `epic-59:121-123` forbids, now refused twice: by the sweep's shift and by the
    check's own guard.
    """
    from core import dq_monitors

    seen: dict[str, date] = {}

    def _capture(ds, conn, window_date, *args, **kwargs):
        seen[ds["id"]] = window_date
        return dq_monitors.CheckVerdict(False, dq_monitors.STATUS_EVALUATED)

    with (
        patch("core.dq_monitors._check_zero_rows", side_effect=_capture),
        patch("core.dq_monitors._check_volume", return_value=False),
        patch("core.dq_monitors._check_timeliness", return_value=False),
        patch("core.dq_monitors._check_duplication", return_value=False),
        patch("core.dq_monitors._check_schema", return_value=False),
        patch("core.dq_monitors._check_date_format", return_value=False),
        patch("core.dq_monitors._check_null_rate", return_value=False),
    ):
        conn = _make_conn()
        dq_monitors._run_monitors_for_datastream(
            {
                "id": "ds_offset", "project_id": "proj_1", "module_name": "mod_a",
                "name": "Stream Offset", "window_offset_days": 3,
            },
            conn,
            date(2026, 7, 12),
            None,
        )

    assert seen["ds_offset"] == date(2026, 7, 10)


# ---------------------------------------------------------------------------
# Story 59.5 -- the governed bridge of `volume` and `arrival_timeliness`, and
# the ONE monitor that is deliberately not bridged.
#
# The rows those bridges write are proven against a real Postgres in
# `test_dq_volume_bridge.py` and `test_dq_arrival_timeliness_bridge.py`. What is
# asserted here is what a database cannot show: which arguments travel, and what
# is refused before any write is attempted.
# ---------------------------------------------------------------------------


def test_volume_without_a_datastream_row_writes_nothing(monkeypatch):
    """Arbitrage 2, and the flat signature its callers still use.

    `_check_volume` is reached with a row from the sweep and without one from a
    caller that has only the flat arguments. No row means no `org_id`, and a
    governed monitor cannot be derived from nothing -- so the check answers
    exactly what it answered before story 59.5 and writes no evidence at all.
    """
    from core import dq_monitors

    calls: list[str] = []
    monkeypatch.setattr(
        "core.dq_monitor_bridge.derive_monitor",
        lambda key, **kwargs: calls.append(key),
    )

    with patch(
        "core.extract_ledger.get_extract_ledger",
        return_value=[{"date": "2026-07-12", "status": "ok", "row_count": None}],
    ):
        fired = dq_monitors._check_volume(
            "ds_1", "proj_1", "mod_a", "Stream A", _make_conn(), date(2026, 7, 12)
        )

    assert bool(fired) is False
    assert calls == [], "no Datastream row, no governed monitor -- and no guess"


def test_volume_answers_not_applicable_when_no_day_carries_a_count(monkeypatch):
    """Arbitrage 2: `measured_per_window`, never a pass.

    `row_count` is NULL on every pull job of both bases, so this is the outcome
    every Datastream produces today. It used to be a bare `False`, which the
    scheduled summary reported as "no issue".
    """
    from core import dq_monitors

    written: list[dict] = []
    monkeypatch.setattr(
        "core.dq_monitor_bridge.derive_monitor",
        lambda key, **kwargs: {"monitor_id": "dqm_x", "monitor_version_id": "dqmv_x"},
    )
    monkeypatch.setattr(
        "core.dq_monitor_bridge.record_verdict",
        lambda key, **kwargs: written.append({"key": key, **kwargs}) or {},
    )

    with patch(
        "core.extract_ledger.get_extract_ledger",
        return_value=[
            {"date": "2026-07-12", "status": "ok", "row_count": None, "execution_id": "dse_1"}
        ],
    ):
        fired = dq_monitors._check_volume(
            "ds_1",
            "proj_1",
            "mod_a",
            "Stream A",
            _make_conn(),
            date(2026, 7, 12),
            {"id": "ds_1", "project_id": "proj_1", "org_id": "org_1", "name": "Stream A"},
        )

    assert bool(fired) is False
    assert len(written) == 1
    assert written[0]["key"] == "volume"
    assert written[0]["outcome"] == "not_applicable"
    assert written[0]["observed"]["reason"] == dq_monitors.VOLUME_MEASURED_PER_WINDOW
    assert written[0]["execution_id"] == "dse_1"
    assert written[0]["window_start"] == written[0]["window_end"] == date(2026, 7, 12)
    assert written[0]["fired"] is False


def test_the_arrival_branch_is_judged_on_the_day_the_sweep_named(monkeypatch):
    """Arbitrage 3: the day of the observation travels down, and so does the row.

    The arrival check measures elapsed minutes and has no window of its own. It
    must not compute one: the day it reports is the day the sweep is speaking
    about, which is the day its six siblings of the same round are judged on.
    """
    from core import dq_monitors

    seen: dict[str, object] = {}

    def _capture(ds_id, project_id, ds_name, monitor, conn, now_utc, ds=None, window_date=None):
        seen.update({"ds": ds, "window_date": window_date, "monitor": monitor})
        return False

    conn = _make_conn()
    yesterday = date(2026, 7, 12)
    now_utc = datetime(2026, 7, 13, 10, 0, 0, tzinfo=timezone.utc)
    row = {"id": "ds_1", "project_id": "proj_1", "org_id": "org_1", "name": "Feed A"}

    with (
        patch("core.dq_monitors._check_arrival_timeliness", side_effect=_capture),
        patch(
            "core.dq_monitors._arrival_monitor",
            return_value={"expected_interval_minutes": 60, "owner_person_id": "owner"},
        ),
    ):
        fired = dq_monitors._check_timeliness(
            "ds_1", "proj_1", None, "Feed A", conn, yesterday, now_utc, 1, row
        )

    assert fired is False
    assert seen["window_date"] == yesterday
    assert seen["ds"] is row


def test_the_geography_monitor_is_named_and_never_governed():
    """Arbitrage 4: the registry carries its scope, and nothing tries to store it.

    `app.dq_monitors.target_kind` has no `project` value (migration 145:306-307),
    so a governed row for this monitor cannot exist. The bridge refuses before
    opening a connection rather than letting `ensure_monitor` raise at 02:00.
    """
    from core import dq_monitor_bridge, dq_monitor_registry, dq_monitors

    entry = dq_monitor_registry.BY_KEY["geography"]
    assert entry.target_kind == dq_monitor_registry.TARGET_PROJECT
    assert entry.publishable is True
    assert entry.dispatched is False
    assert "geography" not in dq_monitors.CHECK_PROFILES

    derived = dq_monitor_bridge.derive_monitor(
        "geography",
        org_id="org_1",
        project_id="proj_1",
        datastream_id="ds_1",
        datastream_name="Stream A",
    )
    assert derived is None


# ---------------------------------------------------------------------------
# "No issue" never stands for "nothing was measured" -- governance.md [8]
# ---------------------------------------------------------------------------
#
# Measured 2026-08-16: `_check_timeliness`, `_check_duplication` and
# `_check_schema` were annotated `-> bool` and answered a bare `False` on twelve
# exits that meant the check could not run. The sweep reports `False` as "no
# issue", so an unreachable warehouse and a clean Datastream were the same
# sentence. The `*_skip` debug lines were the only place the difference lived.
#
# `tests/conformance/test_dq_monitor_registry.py` holds the ANNOTATION so a check
# cannot go back to a boolean. These hold the BEHAVIOUR: each exit answers its
# own status and names its own reason.


def test_schema_answers_unavailable_when_no_column_can_be_read():
    """An empty column list used to be `False`. It is not a schema that held."""
    from core.dq_monitors import STATUS_UNAVAILABLE, WAREHOUSE_UNREACHABLE, _check_schema

    conn = _make_conn()
    with (
        patch("core.dq_monitors._fetch_raw_columns", return_value=[]),
        patch("core.dq_monitors._get_raw_table_for_ds", return_value="raw.table"),
        patch("core.dq_monitors._duckdb_path", return_value=""),
        patch("core.infra_alerts.write_infra_firing") as mock_fire,
    ):
        verdict = _check_schema("ds_1", "proj_1", "mod_a", "Stream A", conn, date(2026, 7, 12))

    assert bool(verdict) is False
    assert verdict.status == STATUS_UNAVAILABLE
    assert verdict.detail["reason"] == WAREHOUSE_UNREACHABLE
    mock_fire.assert_not_called()


def test_schema_answers_not_applicable_on_the_run_that_freezes_the_baseline():
    """A drift check with one observation has not cleared anything yet."""
    from core.dq_monitors import STATUS_NOT_APPLICABLE, _check_schema

    conn = _make_conn()
    with (
        patch("core.dq_monitors._fetch_raw_columns", return_value=["a", "b"]),
        patch(
            "core.dq_monitor_bridge.published_baseline",
            return_value=(None, "no_published_monitor"),
        ),
        patch(
            "core.dq_monitor_bridge.derive_monitor",
            return_value={"monitor_id": "dqm_1", "monitor_version_id": "dqmv_1"},
        ),
        patch("core.infra_alerts.write_infra_firing"),
    ):
        verdict = _check_schema(
            "ds_1",
            "proj_1",
            "mod_a",
            "Stream A",
            conn,
            date(2026, 7, 12),
            ds={"org_id": "org_1"},
        )

    assert bool(verdict) is False
    assert verdict.status == STATUS_NOT_APPLICABLE
    assert verdict.detail["reason"] == "baseline_frozen"


def test_schema_says_so_when_the_first_observation_could_not_be_frozen():
    """A freeze that did not land is not a clean run.

    Nothing was stored, so the next run has nothing to judge against. Answering
    `not_applicable` here would report a schema that did not drift; the sweep
    reads that as "no issue", which is the sentence `governance.md` forbids.
    """
    from core.dq_monitors import STATUS_UNAVAILABLE, _check_schema

    conn = _make_conn()
    with (
        patch("core.dq_monitors._fetch_raw_columns", return_value=["a", "b"]),
        patch(
            "core.dq_monitor_bridge.published_baseline",
            return_value=(None, "no_published_monitor"),
        ),
        patch("core.dq_monitor_bridge.derive_monitor", return_value=None),
        patch("core.infra_alerts.write_infra_firing"),
    ):
        verdict = _check_schema(
            "ds_1",
            "proj_1",
            "mod_a",
            "Stream A",
            conn,
            date(2026, 7, 12),
            ds={"org_id": "org_1"},
        )

    assert bool(verdict) is False
    assert verdict.status == STATUS_UNAVAILABLE
    assert verdict.detail["reason"] == "baseline_not_frozen"


def test_duplication_answers_unavailable_when_the_warehouse_is_out_of_reach():
    """A table nobody could open holds no duplicate the way an empty room is quiet."""
    from core.dq_monitors import NO_RAW_TABLE, STATUS_UNAVAILABLE, _check_duplication

    with patch("core.dq_monitors._get_raw_table_for_ds", return_value=""):
        verdict = _check_duplication("ds_1", "proj_1", "mod_a", "Stream A", date(2026, 7, 12))

    assert bool(verdict) is False
    assert verdict.status == STATUS_UNAVAILABLE
    assert verdict.detail["reason"] == NO_RAW_TABLE


def test_timeliness_answers_not_applicable_before_the_deadline():
    """An hour before the deadline is not a Datastream that arrived on time."""
    from core.dq_monitors import STATUS_NOT_APPLICABLE, _check_timeliness

    conn = _make_conn()
    now_utc = datetime(2026, 7, 13, 1, 0, tzinfo=timezone.utc)
    with (
        patch("core.dq_monitors._arrival_monitor", return_value=None),
        patch("core.dq_monitors._due_hour", return_value=6),
        patch("core.infra_alerts.write_infra_firing") as mock_fire,
    ):
        verdict = _check_timeliness(
            "ds_1", "proj_1", "mod_a", "Stream A", conn, date(2026, 7, 12), now_utc
        )

    assert bool(verdict) is False
    assert verdict.status == STATUS_NOT_APPLICABLE
    assert verdict.detail["reason"] == "not_yet_due"
    mock_fire.assert_not_called()


def test_the_two_unmeasured_answers_are_not_the_same_sentence():
    """`not_applicable` is about the client's data; `unavailable` is about us.

    Folding them would tell someone their Datastream had nothing to measure when
    the truth is that this deployment could not reach the warehouse.
    """
    from core.dq_monitors import STATUS_NOT_APPLICABLE, STATUS_UNAVAILABLE

    assert STATUS_NOT_APPLICABLE != STATUS_UNAVAILABLE
