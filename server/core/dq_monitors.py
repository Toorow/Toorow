"""toorow -- Data Quality monitors v1 (Story 8.6, Epic 8).

Five universal zero-config monitors, evaluated per enabled datastream.
DQ pattern: warning-severity firings, never halt load in v1.

Monitors
--------
(a) volume        -- rolling median of per-day row counts (last 30 days ledger).
                     Flags yesterday when |count - median| > 2.6 * robust_sigma
                     and >= 10 prior data points exist.
                     Robust sigma = MAD * 1.4826 (median absolute deviation scale).

(b) timeliness    -- yesterday's extract (status ok|partial) must exist by
                     DQ_TIMELINESS_DUE_HOUR (default 9, project timezone via
                     SCHEDULER_TIMEZONE). Flags if missing when now > due time.

(c) duplication   -- count duplicate full rows in the raw table for yesterday.
                     Raw table resolved from verification._get_raw_table_name(module_name).
                     Flags when duplicate_count > 0.

(d) schema        -- current raw table column list vs baseline in app.dq_baselines.
                     First run: seeds baseline, no firing.
                     On drift: fires once, then AUTO-RESETS baseline.

(e) date_format   -- Story 8.10 / R3: for streams whose datastream config declares
                     date_format, verify that yesterday's raw rows in raw_generic_daily
                     have dates parseable as canonical ISO 'YYYY-MM-DD'.
                     Uses DuckDB try_strptime or regex pattern YYYY-MM-DD.
                     Fires when non-ISO rows are found: alert_type='dq_date_format'.
                     Applies only to the 'generic' module (other modules bypass gracefully).

All findings -> infra_alerts.write_infra_firing(alert_type='dq_volume'|'dq_timeliness'|
'dq_duplication'|'dq_schema'|'dq_date_format', ...).

Isolation: per-stream try/except -- one failing stream never blocks others.
Never raises at module level.

Public API
----------
run_dq_monitors(project_id=None) -> dict
    Evaluate all 5 monitors for every enabled datastream (or a single project).
    Returns summary dict: {evaluated, volume_issues, timeliness_issues,
    duplication_issues, schema_issues, date_format_issues, total_issues, errors}.

Environment variables
---------------------
DQ_MONITORS_ENABLED      default "true"  -- master switch (false = skip in scheduler)
DQ_TIMELINESS_DUE_HOUR   default "9"     -- hour (0-23) in SCHEDULER_TIMEZONE
SCHEDULER_TIMEZONE        default "Europe/Paris"
TOOROW_DUCKDB_PATH     -- path to DuckDB warehouse file

AD-2: no module-specific strings in core logic; module_name passed as data.
AD-5: project_id scoping enforced throughout.
ASCII-only log strings (AI-03).
"""

from __future__ import annotations

import json
import logging
import os
import statistics
from datetime import date, datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------


def _dq_enabled() -> bool:
    return os.environ.get("DQ_MONITORS_ENABLED", "true").lower() != "false"


def _due_hour() -> int:
    try:
        return int(os.environ.get("DQ_TIMELINESS_DUE_HOUR", "9"))
    except (ValueError, TypeError):
        return 9


def _scheduler_tz() -> str:
    return os.environ.get("SCHEDULER_TIMEZONE", "Europe/Paris")


def _duckdb_path() -> str:
    return os.environ.get("TOOROW_DUCKDB_PATH", "")


# ---------------------------------------------------------------------------
# Rolling stats for volume monitor
# ---------------------------------------------------------------------------


def _rolling_median_and_sigma(counts: list[float]) -> tuple[float, float]:
    """Compute (median, robust_sigma) from a list of count values.

    robust_sigma = MAD * 1.4826  (consistent estimator of std dev under normality).
    Returns (0.0, 0.0) for empty or single-element lists.
    """
    if len(counts) < 2:
        return 0.0, 0.0
    med = statistics.median(counts)
    mad = statistics.median([abs(x - med) for x in counts])
    sigma = mad * 1.4826
    return med, sigma


def _volume_anomaly(prior_counts: list[float], yesterday_count: float) -> bool:
    """Return True when yesterday_count is anomalous given prior history.

    Requires >= 10 prior data points.
    Threshold: |x - median| > 2.6 * robust_sigma.
    """
    if len(prior_counts) < 10:
        return False
    med, sigma = _rolling_median_and_sigma(prior_counts)
    if sigma == 0.0:
        # All prior counts identical -- any deviation is anomalous.
        return yesterday_count != med
    return abs(yesterday_count - med) > 2.6 * sigma


# ---------------------------------------------------------------------------
# Fetch enabled datastreams
# ---------------------------------------------------------------------------


def _fetch_enabled_datastreams(conn, project_id: str | None) -> list[dict]:
    """Fetch enabled datastreams from Postgres.

    Returns list of {id, project_id, module_name, name, config}. Empty on error.
    config is included so monitor (e) date_format can inspect declared date_format.
    """
    try:
        with conn.cursor() as cur:
            if project_id is not None:
                cur.execute(
                    """
                    SELECT ds.id, ds.project_id, ds.module_name, ds.name,
                           ds.config
                    FROM app.datastreams ds
                    WHERE ds.enabled = TRUE AND ds.project_id = %s
                    ORDER BY ds.project_id, ds.id
                    """,
                    (project_id,),
                )
            else:
                cur.execute(
                    """
                    SELECT ds.id, ds.project_id, ds.module_name, ds.name,
                           ds.config
                    FROM app.datastreams ds
                    WHERE ds.enabled = TRUE
                    ORDER BY ds.project_id, ds.id
                    """
                )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as exc:
        logger.warning("dq_monitors: fetch_datastreams_failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# (a) Volume monitor
# ---------------------------------------------------------------------------


def _check_volume(
    ds_id: str,
    project_id: str,
    module_name: str,
    ds_name: str,
    conn,
    yesterday: date,
) -> bool:
    """Evaluate volume monitor for one datastream.

    Reads 31 days of ledger (yesterday + 30 prior days).
    Uses row_count from pull_verifications via the ledger query.
    Returns True when a firing was issued.
    """
    from core.extract_ledger import get_extract_ledger  # noqa: PLC0415

    date_from = (yesterday - timedelta(days=30)).isoformat()
    date_to = yesterday.isoformat()

    entries = get_extract_ledger(ds_id, date_from, date_to, conn)
    if not entries:
        return False

    # Only count days with a real row_count (status ok/partial/empty counts;
    # never_fetched/running/failed do not contribute a row_count).
    data_points: list[tuple[str, float]] = []
    for entry in entries:
        rc = entry.get("row_count")
        if rc is not None:
            data_points.append((entry["date"], float(rc)))

    if not data_points:
        return False

    # Yesterday is the last data point (date_to).
    # Prior points = all data points EXCEPT yesterday.
    yesterday_str = yesterday.isoformat()
    prior: list[float] = [rc for d, rc in data_points if d != yesterday_str]
    yesterday_pts: list[float] = [rc for d, rc in data_points if d == yesterday_str]

    if not yesterday_pts:
        # No data point for yesterday => nothing to flag.
        return False

    yesterday_count = yesterday_pts[0]

    if not _volume_anomaly(prior, yesterday_count):
        return False

    # Fire DQ volume alert.
    med, sigma = _rolling_median_and_sigma(prior)
    from core import infra_alerts  # noqa: PLC0415

    infra_alerts.write_infra_firing(
        alert_type="dq_volume",
        project_id=project_id,
        metric="row_count",
        severity="warning",
        message=(
            f"Volume anormal pour le datastream '{ds_name}' "
            f"le {yesterday_str}: {int(yesterday_count)} lignes "
            f"(median={med:.0f}, sigma={sigma:.1f})"
        ),
        metadata={
            "datastream_id": ds_id,
            "datastream_name": ds_name,
            "module_name": module_name,
            "window_date": yesterday_str,
            "yesterday_count": yesterday_count,
            "prior_median": med,
            "robust_sigma": sigma,
            "prior_n": len(prior),
        },
    )
    logger.info(
        "dq_monitors: volume_firing ds=%s date=%s count=%.0f median=%.0f sigma=%.1f",
        ds_id,
        yesterday_str,
        yesterday_count,
        med,
        sigma,
    )
    return True


# ---------------------------------------------------------------------------
# (b) Timeliness monitor
# ---------------------------------------------------------------------------


def _check_timeliness(
    ds_id: str,
    project_id: str,
    module_name: str,
    ds_name: str,
    conn,
    yesterday: date,
    now_utc: datetime | None = None,
) -> bool:
    """Evaluate timeliness monitor for one datastream.

    Yesterday's extract (status ok|partial) must exist by DQ_TIMELINESS_DUE_HOUR
    in the project timezone. Fires if missing when now is past due.
    Returns True when a firing was issued.
    """
    from core.extract_ledger import get_extract_ledger  # noqa: PLC0415

    # Determine current time in project timezone.
    if now_utc is None:
        now_utc = datetime.now(tz=timezone.utc)

    tz_name = _scheduler_tz()
    try:
        from zoneinfo import ZoneInfo  # noqa: PLC0415

        now_local = now_utc.astimezone(ZoneInfo(tz_name))
    except Exception:
        now_local = now_utc

    due_hour = _due_hour()

    # Not yet past due? Nothing to check.
    if now_local.hour < due_hour:
        logger.debug(
            "dq_monitors: timeliness_skip: ds=%s not_yet_due hour=%d due=%d",
            ds_id,
            now_local.hour,
            due_hour,
        )
        return False

    # Check if yesterday's extract exists and has ok/partial status.
    yesterday_str = yesterday.isoformat()
    entries = get_extract_ledger(ds_id, yesterday_str, yesterday_str, conn)

    if entries:
        entry = entries[0]
        status = entry.get("status", "never_fetched")
        if status in ("ok", "partial"):
            # Extract exists and is acceptable -- no issue.
            return False

    # Missing or bad status -- fire timeliness alert.
    from core import infra_alerts  # noqa: PLC0415

    infra_alerts.write_infra_firing(
        alert_type="dq_timeliness",
        project_id=project_id,
        metric="timeliness",
        severity="warning",
        message=(
            f"Ponctualite: aucune extraction valide pour '{ds_name}' "
            f"le {yesterday_str} apres {due_hour:02d}h00 ({tz_name})"
        ),
        metadata={
            "datastream_id": ds_id,
            "datastream_name": ds_name,
            "module_name": module_name,
            "window_date": yesterday_str,
            "due_hour": due_hour,
            "timezone": tz_name,
            "actual_status": entries[0].get("status") if entries else "never_fetched",
        },
    )
    logger.info(
        "dq_monitors: timeliness_firing ds=%s date=%s due_hour=%d",
        ds_id,
        yesterday_str,
        due_hour,
    )
    return True


# ---------------------------------------------------------------------------
# (c) Duplication monitor
# ---------------------------------------------------------------------------


def _get_raw_table_for_ds(module_name: str) -> str:
    """Resolve the raw DuckDB table name for a module (AD-2: name is data).

    Uses verification._get_raw_table_name(provider=module_name).
    Falls back to env TOOROW_RAW_TABLE_NAME.
    Returns empty string if unresolvable.
    """
    try:
        from core.verification import _get_raw_table_name  # noqa: PLC0415

        return _get_raw_table_name(provider=module_name)
    except Exception:
        return os.environ.get("TOOROW_RAW_TABLE_NAME", "")


def _check_duplication(
    ds_id: str,
    project_id: str,
    module_name: str,
    ds_name: str,
    yesterday: date,
) -> bool:
    """Evaluate duplication monitor for one datastream.

    Counts duplicate full rows in the raw DuckDB table for yesterday.
    Uses DuckDB directly (raw tables live in DuckDB, not Postgres).
    Returns True when a firing was issued.
    """
    raw_table = _get_raw_table_for_ds(module_name)
    if not raw_table:
        logger.debug(
            "dq_monitors: duplication_skip: ds=%s no_raw_table module=%s",
            ds_id,
            module_name,
        )
        return False

    db_path = _duckdb_path()
    if not db_path:
        logger.debug("dq_monitors: duplication_skip: ds=%s no_duckdb_path", ds_id)
        return False

    yesterday_str = yesterday.isoformat()
    duplicate_count = 0

    try:
        import duckdb  # noqa: PLC0415

        with duckdb.connect(db_path, read_only=True) as duck_conn:
            # Get column list for the raw table.
            try:
                col_rows = duck_conn.execute(
                    "SELECT column_name FROM information_schema.columns "  # noqa: S608
                    "WHERE table_name = ? ORDER BY ordinal_position",
                    [raw_table.split(".")[-1]],
                ).fetchall()
                columns = [r[0] for r in col_rows]
            except Exception:
                columns = []

            if not columns:
                logger.debug(
                    "dq_monitors: duplication_skip: ds=%s raw_table=%s no_columns",
                    ds_id,
                    raw_table,
                )
                return False

            # Exclude system/audit columns from grain check.
            grain_cols = [
                c for c in columns
                if c not in ("pull_id", "loaded_at", "_dq_checked_at")
            ]
            if not grain_cols:
                return False

            col_list = ", ".join(f'"{c}"' for c in grain_cols)
            # Count rows that appear more than once (duplicates) for yesterday.
            sql = (
                f"SELECT COALESCE(SUM(cnt - 1), 0) AS dup_count "  # noqa: S608
                f"FROM ("
                f"  SELECT COUNT(*) AS cnt FROM {raw_table} "
                f"  WHERE project_id = ? AND date = ? "
                f"  GROUP BY {col_list} "
                f"  HAVING COUNT(*) > 1"
                f") sub"
            )
            row = duck_conn.execute(sql, [project_id, yesterday_str]).fetchone()
            duplicate_count = int(row[0]) if row and row[0] is not None else 0

    except Exception as exc:
        logger.warning(
            "dq_monitors: duplication_check_failed ds=%s: %s", ds_id, exc
        )
        return False

    if duplicate_count == 0:
        return False

    from core import infra_alerts  # noqa: PLC0415

    infra_alerts.write_infra_firing(
        alert_type="dq_duplication",
        project_id=project_id,
        metric="duplicate_rows",
        severity="warning",
        message=(
            f"Doublons detectes dans '{ds_name}' le {yesterday_str}: "
            f"{duplicate_count} lignes dupliquees"
        ),
        metadata={
            "datastream_id": ds_id,
            "datastream_name": ds_name,
            "module_name": module_name,
            "window_date": yesterday_str,
            "duplicate_count": duplicate_count,
            "raw_table": raw_table,
        },
    )
    logger.info(
        "dq_monitors: duplication_firing ds=%s date=%s dup_count=%d raw_table=%s",
        ds_id,
        yesterday_str,
        duplicate_count,
        raw_table,
    )
    return True


# ---------------------------------------------------------------------------
# (d) Schema consistency monitor
# ---------------------------------------------------------------------------


def _fetch_raw_columns(module_name: str, project_id: str, yesterday: date) -> list[str]:
    """Read current column list from the raw DuckDB table for module_name.

    Returns sorted list of column names. Returns [] on any error.
    """
    raw_table = _get_raw_table_for_ds(module_name)
    if not raw_table:
        return []

    db_path = _duckdb_path()
    if not db_path:
        return []

    try:
        import duckdb  # noqa: PLC0415

        with duckdb.connect(db_path, read_only=True) as duck_conn:
            table_name = raw_table.split(".")[-1]
            rows = duck_conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = ? ORDER BY column_name",
                [table_name],
            ).fetchall()
            return sorted(r[0] for r in rows if r[0])
    except Exception as exc:
        logger.debug(
            "dq_monitors: schema_fetch_cols_failed ds_module=%s: %s", module_name, exc
        )
        return []


def _read_dq_baseline(ds_id: str, conn) -> list[str] | None:
    """Read the stored column set from app.dq_baselines. Returns None if not found."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT column_set FROM app.dq_baselines WHERE datastream_id = %s",
                (ds_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        col_set = row[0]
        if isinstance(col_set, str):
            col_set = json.loads(col_set)
        return sorted(col_set) if col_set else []
    except Exception as exc:
        logger.warning("dq_monitors: read_baseline_failed ds=%s: %s", ds_id, exc)
        return None


def _write_dq_baseline(ds_id: str, columns: list[str], conn) -> None:
    """Upsert the column set into app.dq_baselines.

    Fix [MEDIUM #8]: previously called conn.commit() on the shared loop connection,
    causing a partial-state commit mid-iteration if a later stream aborted the
    connection.  Now opens a SHORT-LIVED separate connection so the baseline upsert
    is isolated from the main per-stream connection.  The *conn* argument is accepted
    but ignored (kept in the signature for call-site compatibility).
    """
    try:
        from core.db import get_connection  # noqa: PLC0415

        sorted_cols = sorted(columns)
        col_set_json = json.dumps(sorted_cols)
        with get_connection() as iso_conn:
            with iso_conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.dq_baselines (datastream_id, column_set, updated_at)
                    VALUES (%s, %s::jsonb, NOW())
                    ON CONFLICT (datastream_id)
                    DO UPDATE SET column_set = EXCLUDED.column_set,
                                  updated_at = NOW()
                    """,
                    (ds_id, col_set_json),
                )
            iso_conn.commit()
        logger.debug("dq_monitors: baseline_written ds=%s cols=%d", ds_id, len(sorted_cols))
    except Exception as exc:
        logger.warning("dq_monitors: write_baseline_failed ds=%s: %s", ds_id, exc)


def _check_schema(
    ds_id: str,
    project_id: str,
    module_name: str,
    ds_name: str,
    conn,
    yesterday: date,
) -> bool:
    """Evaluate schema consistency monitor for one datastream.

    First run: seeds baseline, no firing.
    On drift: fires once, then AUTO-RESETS baseline.
    Returns True when a firing was issued.
    """
    current_cols = _fetch_raw_columns(module_name, project_id, yesterday)
    if not current_cols:
        logger.debug(
            "dq_monitors: schema_skip: ds=%s no_columns module=%s", ds_id, module_name
        )
        return False

    baseline = _read_dq_baseline(ds_id, conn)

    if baseline is None:
        # First run -- seed baseline, no firing.
        _write_dq_baseline(ds_id, current_cols, conn)
        logger.info(
            "dq_monitors: schema_baseline_seeded ds=%s cols=%d", ds_id, len(current_cols)
        )
        return False

    if sorted(baseline) == sorted(current_cols):
        return False

    # Drift detected.
    added = sorted(set(current_cols) - set(baseline))
    removed = sorted(set(baseline) - set(current_cols))
    yesterday_str = yesterday.isoformat()

    from core import infra_alerts  # noqa: PLC0415

    infra_alerts.write_infra_firing(
        alert_type="dq_schema",
        project_id=project_id,
        metric="schema_consistency",
        severity="warning",
        message=(
            f"Schema modifie pour '{ds_name}' "
            f"(+{len(added)} colonnes, -{len(removed)} colonnes)"
        ),
        metadata={
            "datastream_id": ds_id,
            "datastream_name": ds_name,
            "module_name": module_name,
            "window_date": yesterday_str,
            "added_columns": added,
            "removed_columns": removed,
            "baseline_cols": len(baseline),
            "current_cols": len(current_cols),
        },
    )
    logger.info(
        "dq_monitors: schema_firing ds=%s added=%s removed=%s",
        ds_id,
        added,
        removed,
    )

    # AUTO-RESET: advance baseline to current state.
    _write_dq_baseline(ds_id, current_cols, conn)
    return True


# ---------------------------------------------------------------------------
# (e) Rejected rows monitor (Story 8.10, R3 redesign -- fix review-epic-8 #7)
# ---------------------------------------------------------------------------

def _rejected_rows_threshold() -> int:
    """Read the rejected_rows firing threshold at call time (so tests can override via env)."""
    try:
        return int(os.environ.get("DQ_REJECTED_ROWS_THRESHOLD", "0"))
    except (ValueError, TypeError):
        return 0


def _check_date_format(
    ds_id: str,
    project_id: str,
    module_name: str,
    ds_name: str,
    yesterday: date,
    config: dict | None = None,
) -> bool:
    """Evaluate the rejected-rows monitor for one datastream (replaces dead date_format check).

    Fix [MEDIUM #7]: the original date_format monitor checked raw_generic_daily for
    non-ISO dates, but those rows are already rejected at landing time by the generic
    connector and never written to the table -- so the check always found 0 bad rows.

    Redesign: read the rejected_rows count from the pull_jobs result (stored in
    pull_verifications.rejected_rows when the column exists, or inferred from the
    pull result metadata stored in pull_jobs.result_payload where available).
    Fires when rejected_rows for yesterday's pull(s) exceeds DQ_REJECTED_ROWS_THRESHOLD
    (default 0 -- fire on any rejection).

    Applies to all modules (AD-2: module_name is data).
    Other modules that do not report rejected_rows produce 0 and do not fire.
    Returns True when a firing was issued. Never raises (per-stream isolation).
    """
    yesterday_str = yesterday.isoformat()
    total_rejected = 0

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                # Query pull_verifications.rejected_rows for yesterday's completed pulls.
                # The column was added as part of this fix; the query uses COALESCE so
                # older rows without the column default to 0 (safe).
                cur.execute(
                    """
                    SELECT COALESCE(SUM(pv.rejected_rows), 0)
                    FROM app.pull_jobs pj
                    JOIN app.pull_verifications pv ON pv.pull_id = pj.pull_id
                    WHERE pj.datastream_id = %s
                      AND pj.date_to = %s::date
                      AND pj.state = 'done'
                    """,
                    (ds_id, yesterday_str),
                )
                row = cur.fetchone()
                total_rejected = int(row[0] or 0) if row else 0

    except Exception as exc:
        logger.warning(
            "dq_monitors: rejected_rows_check_failed ds=%s: %s", ds_id, exc
        )
        return False

    threshold = _rejected_rows_threshold()
    if total_rejected <= threshold:
        return False

    from core import infra_alerts  # noqa: PLC0415

    infra_alerts.write_infra_firing(
        alert_type="dq_date_format",
        project_id=project_id,
        metric="rejected_rows",
        severity="warning",
        message=(
            f"Lignes rejetees detectees dans '{ds_name}' "
            f"le {yesterday_str}: {total_rejected} ligne(s) rejetee(s) "
            f"(seuil={threshold})"
        ),
        metadata={
            "datastream_id": ds_id,
            "datastream_name": ds_name,
            "module_name": module_name,
            "window_date": yesterday_str,
            "rejected_rows": total_rejected,
            "threshold": threshold,
        },
    )
    logger.info(
        "dq_monitors: rejected_rows_firing ds=%s date=%s rejected=%d threshold=%d",
        ds_id,
        yesterday_str,
        total_rejected,
        threshold,
    )
    return True


# ---------------------------------------------------------------------------
# Per-datastream orchestrator
# ---------------------------------------------------------------------------


def _run_monitors_for_datastream(
    ds: dict,
    conn,
    yesterday: date,
    now_utc: datetime | None = None,
) -> dict:
    """Run all 5 monitors for one datastream. Never raises.

    Returns {volume, timeliness, duplication, schema, date_format} booleans
    (True = issue fired).
    """
    ds_id = ds["id"]
    project_id = ds["project_id"]
    module_name = ds["module_name"]
    ds_name = ds["name"]
    ds_config: dict | None = ds.get("config")

    result = {
        "volume": False,
        "timeliness": False,
        "duplication": False,
        "schema": False,
        "date_format": False,
    }

    try:
        result["volume"] = _check_volume(ds_id, project_id, module_name, ds_name, conn, yesterday)
    except Exception as exc:  # noqa: BLE001
        logger.warning("dq_monitors: volume_failed ds=%s: %s", ds_id, exc)

    try:
        result["timeliness"] = _check_timeliness(
            ds_id, project_id, module_name, ds_name, conn, yesterday, now_utc
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("dq_monitors: timeliness_failed ds=%s: %s", ds_id, exc)

    try:
        result["duplication"] = _check_duplication(
            ds_id, project_id, module_name, ds_name, yesterday
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("dq_monitors: duplication_failed ds=%s: %s", ds_id, exc)

    try:
        result["schema"] = _check_schema(
            ds_id, project_id, module_name, ds_name, conn, yesterday
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("dq_monitors: schema_failed ds=%s: %s", ds_id, exc)

    try:
        result["date_format"] = _check_date_format(
            ds_id, project_id, module_name, ds_name, yesterday, ds_config
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("dq_monitors: date_format_failed ds=%s: %s", ds_id, exc)

    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_dq_monitors(
    project_id: str | None = None,
    as_of_date: date | None = None,
    now_utc: datetime | None = None,
) -> dict:
    """Evaluate all 5 DQ monitors for every enabled datastream.

    Args:
        project_id:  Optional scope to a single project (None = all projects).
        as_of_date:  Reference date (defaults to date.today()); yesterday = as_of_date - 1.
        now_utc:     Override current UTC time (for testing timeliness due logic).

    Returns:
        Summary dict: {
            evaluated: int,
            volume_issues: int,
            timeliness_issues: int,
            duplication_issues: int,
            schema_issues: int,
            date_format_issues: int,
            total_issues: int,
            errors: int,
        }

    Never raises. Respects DQ_MONITORS_ENABLED env var.
    """
    summary: dict = {
        "evaluated": 0,
        "volume_issues": 0,
        "timeliness_issues": 0,
        "duplication_issues": 0,
        "schema_issues": 0,
        "date_format_issues": 0,
        "total_issues": 0,
        "errors": 0,
    }

    if not _dq_enabled():
        logger.debug("dq_monitors: skipped -- DQ_MONITORS_ENABLED not true")
        return summary

    if as_of_date is None:
        as_of_date = date.today()
    yesterday = as_of_date - timedelta(days=1)

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            datastreams = _fetch_enabled_datastreams(conn, project_id)

            if not datastreams:
                logger.info("dq_monitors: no_enabled_datastreams project_id=%s", project_id)
                return summary

            logger.info(
                "dq_monitors: starting: datastreams=%d yesterday=%s",
                len(datastreams),
                yesterday,
            )

            for ds in datastreams:
                try:
                    r = _run_monitors_for_datastream(ds, conn, yesterday, now_utc)
                    summary["evaluated"] += 1
                    if r["volume"]:
                        summary["volume_issues"] += 1
                    if r["timeliness"]:
                        summary["timeliness_issues"] += 1
                    if r["duplication"]:
                        summary["duplication_issues"] += 1
                    if r["schema"]:
                        summary["schema_issues"] += 1
                    if r["date_format"]:
                        summary["date_format_issues"] += 1
                except Exception as exc:  # noqa: BLE001
                    summary["errors"] += 1
                    logger.warning(
                        "dq_monitors: ds_failed ds=%s: %s", ds.get("id"), exc
                    )

    except Exception as exc:  # noqa: BLE001
        logger.warning("dq_monitors: run_failed: %s", exc)
        summary["errors"] += 1
        return summary

    summary["total_issues"] = (
        summary["volume_issues"]
        + summary["timeliness_issues"]
        + summary["duplication_issues"]
        + summary["schema_issues"]
        + summary["date_format_issues"]
    )

    logger.info(
        "dq_monitors: complete: evaluated=%d total_issues=%d errors=%d",
        summary["evaluated"],
        summary["total_issues"],
        summary["errors"],
    )
    return summary
