"""toorow -- Overview (control tower) data layer (Story 8.4, Epic 8).

Provides:
  get_overview(project_id, conn) -> dict

Response shape:
  {
    "kpis": {
      "extracts_today_ok":      int,
      "extracts_today_failed":  int,
      "extracts_running":       int,
      "vs_yesterday_ok_delta":  int,   # signed: today_ok - yesterday_ok
    },
    "fleet": [
      {
        "id":            str,
        "name":          str,
        "module_name":   str,
        "enabled":       bool,
        "auth_status":   str | None,     # from connection_ref health
        "issues_count":  int,            # dead_letters_24h + failed_days_7d
        "last_extract":  {"date": str, "status": str} | None,
        "next_run":      str,            # "Demain 02h00" | "Manuel"
        "volume_7d":     list[int],      # 7 ints, oldest first, row_count per day
      },
      ...
    ],
    "deadletters_24h": int,              # project-wide dead letters in last 24h
    "mirror_last_sync": str | None,      # ISO-8601 or None
    "breakers": [
      {"platform": str, "state": str, "budget_remaining": int | None},
      ...
    ],
  }

Design decisions:
  - KPI queries use a single SQL pass over pull_jobs for today / yesterday windows.
    "today" = calendar date in UTC (DATE(enqueued_at AT TIME ZONE 'UTC') = CURRENT_DATE).
  - Fleet is assembled in two SQL calls: one for datastream rows + auth status +
    last pull, one for volume_7d + dead_letter counts per datastream. Python merges.
  - volume_7d reads pull_verifications.actual_rows (the same field the ledger uses).
    Missing days get 0.
  - issues_count = dead_letter_24h (per ds) + count of 'failed'/'dead_letter' pull_jobs
    in the last 7 calendar days for that stream.
  - next_run: schedule_mode='nightly' -> 'Demain {NIGHTLY_RUN_HOUR}h00';
              schedule_mode='manual'  -> 'Manuel'.
  - mirror_last_sync and breakers are obtained by calling core.main.health() --
    the same path used by admin_api._health_proxy -- to avoid SQL duplication.
  - OVERVIEW_ROUTES exports one Route for the orchestrator to splice in.

AD-2: no module-specific strings.
AD-5: all queries filtered by project_id.
AD-8: REST handler only -- no direct DB call from UI.
AI-03: ASCII-only log strings.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger(__name__)

# Nightly run hour (defaults to 02). Read once at module load.
_NIGHTLY_HOUR: str = os.environ.get("NIGHTLY_RUN_HOUR", "02").zfill(2)


# ---------------------------------------------------------------------------
# Auth helper (lazy import -- overview.py never imported by admin_api.py)
# ---------------------------------------------------------------------------


async def _check_auth(request: Request) -> tuple[bool, str]:
    """Delegate to the shared auth layer in core.admin_api."""
    from core.admin_api import _check_auth as _admin_check  # noqa: PLC0415

    return await _admin_check(request)


# ---------------------------------------------------------------------------
# Core data function
# ---------------------------------------------------------------------------


def get_overview(project_id: str, conn) -> dict:
    """Return the control-tower overview for *project_id*.

    Args:
        project_id: Project scope (AD-5 -- all queries filtered here).
        conn:       Open psycopg connection (not committed here).

    Returns:
        Dict with keys: kpis, fleet, deadletters_24h, mirror_last_sync, breakers.
        Never raises; returns safe defaults on partial DB errors.
    """
    today = date.today()
    yesterday = today - timedelta(days=1)
    today_str = today.isoformat()
    yesterday_str = yesterday.isoformat()
    seven_days_ago = (today - timedelta(days=7)).isoformat()

    # ------------------------------------------------------------------
    # 1. KPI: today's ok / failed / running + yesterday ok
    # ------------------------------------------------------------------
    kpis = _query_kpis(project_id, today_str, yesterday_str, conn)

    # ------------------------------------------------------------------
    # 2. Fleet base: datastreams + auth_status + last pull
    # ------------------------------------------------------------------
    fleet_base = _query_fleet_base(project_id, conn)

    # ------------------------------------------------------------------
    # 3. Volume 7d + issues per datastream
    # ------------------------------------------------------------------
    ds_ids = [row["id"] for row in fleet_base]
    volume_map, issues_map = _query_volume_and_issues(ds_ids, today_str, seven_days_ago, conn)

    # ------------------------------------------------------------------
    # 4. Assemble fleet rows
    # ------------------------------------------------------------------
    fleet = []
    for row in fleet_base:
        ds_id = row["id"]
        schedule_mode = row.get("schedule_mode") or "manual"
        next_run = f"Demain {_NIGHTLY_HOUR}h00" if schedule_mode == "nightly" else "Manuel"

        last_pull_state = row.get("last_pull_state")
        last_pull_date = row.get("last_pull_date")
        last_extract: dict | None = None
        if last_pull_date is not None:
            status = _derive_ledger_status(last_pull_state, row.get("last_pull_verdict"))
            last_extract = {"date": str(last_pull_date), "status": status}

        fleet.append({
            "id": ds_id,
            "name": row["name"],
            "module_name": row["module_name"],
            "enabled": row["enabled"],
            "auth_status": row.get("auth_status"),
            "issues_count": issues_map.get(ds_id, 0),
            "last_extract": last_extract,
            "next_run": next_run,
            "volume_7d": volume_map.get(ds_id, [0] * 7),
        })

    # ------------------------------------------------------------------
    # 5. Project-wide dead letters last 24h
    # ------------------------------------------------------------------
    deadletters_24h = _query_deadletters_24h(project_id, conn)

    # ------------------------------------------------------------------
    # 6. Mirror last sync + breakers from the health tool
    # ------------------------------------------------------------------
    mirror_last_sync, breakers = _query_health_data(project_id)

    return {
        "kpis": kpis,
        "fleet": fleet,
        "deadletters_24h": deadletters_24h,
        "mirror_last_sync": mirror_last_sync,
        "breakers": breakers,
    }


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _query_kpis(project_id: str, today_str: str, yesterday_str: str, conn) -> dict:
    """Single SQL pass over pull_jobs for KPI counts.

    Uses LEFT JOIN + UNION approach so that legacy pull_jobs (datastream_id IS NULL,
    resolved via connection_ref.project_id) are counted alongside datastream-bound ones.
    Fix [HIGH]: inner-join on app.datastreams excluded legacy rows (story review-epic-8 #1).
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH scoped_jobs AS (
                    -- Modern path: pull_job has a datastream FK -> project via datastream
                    SELECT pj.pull_id, pj.date_to, pj.state
                    FROM app.pull_jobs pj
                    JOIN app.datastreams ds ON ds.id = pj.datastream_id
                    WHERE ds.project_id = %s
                      AND (pj.date_to = %s::date OR pj.date_to = %s::date)

                    UNION ALL

                    -- Legacy path: datastream_id IS NULL -> project via connection_ref
                    SELECT pj.pull_id, pj.date_to, pj.state
                    FROM app.pull_jobs pj
                    JOIN app.connection_ref cr ON cr.id = pj.connection_ref_id
                    WHERE pj.datastream_id IS NULL
                      AND cr.project_id = %s
                      AND (pj.date_to = %s::date OR pj.date_to = %s::date)
                )
                SELECT
                    COUNT(*) FILTER (
                        WHERE sj.date_to = %s::date
                          AND sj.state = 'done'
                          AND (pv.verdict = 'ok' OR pv.verdict IS NULL)
                    ) AS today_ok,
                    COUNT(*) FILTER (
                        WHERE sj.date_to = %s::date
                          AND sj.state IN ('failed', 'dead_letter')
                    ) AS today_failed,
                    COUNT(*) FILTER (
                        WHERE sj.date_to = %s::date
                          AND sj.state IN ('queued', 'running')
                    ) AS today_running,
                    COUNT(*) FILTER (
                        WHERE sj.date_to = %s::date
                          AND sj.state = 'done'
                          AND (pv.verdict = 'ok' OR pv.verdict IS NULL)
                    ) AS yesterday_ok
                FROM scoped_jobs sj
                LEFT JOIN app.pull_verifications pv ON pv.pull_id = sj.pull_id
                """,
                (
                    project_id, today_str, yesterday_str,  # modern path
                    project_id, today_str, yesterday_str,  # legacy path
                    today_str, today_str, today_str,       # FILTER clauses today
                    yesterday_str,                          # FILTER clause yesterday
                ),
            )
            row = cur.fetchone()
    except Exception as exc:
        logger.warning("overview: kpis_query_failed project=%s: %s", project_id, exc)
        return {
            "extracts_today_ok": 0,
            "extracts_today_failed": 0,
            "extracts_running": 0,
            "vs_yesterday_ok_delta": 0,
        }

    if row is None:
        return {
            "extracts_today_ok": 0,
            "extracts_today_failed": 0,
            "extracts_running": 0,
            "vs_yesterday_ok_delta": 0,
        }

    today_ok = int(row[0] or 0)
    today_failed = int(row[1] or 0)
    today_running = int(row[2] or 0)
    yesterday_ok = int(row[3] or 0)

    return {
        "extracts_today_ok": today_ok,
        "extracts_today_failed": today_failed,
        "extracts_running": today_running,
        "vs_yesterday_ok_delta": today_ok - yesterday_ok,
    }


def _query_fleet_base(project_id: str, conn) -> list[dict]:
    """Fetch per-datastream base data: meta + auth_status + last pull summary."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    ds.id,
                    ds.name,
                    ds.module_name,
                    ds.enabled,
                    ds.schedule_mode,
                    ch.status           AS auth_status,
                    lp.date_to          AS last_pull_date,
                    lp.state            AS last_pull_state,
                    lp.verdict          AS last_pull_verdict
                FROM app.datastreams ds
                LEFT JOIN app.connection_ref cr ON cr.id = ds.connection_ref_id
                LEFT JOIN app.connection_health ch ON ch.connection_ref_id = cr.id
                LEFT JOIN LATERAL (
                    SELECT pj.date_to, pj.state, pv.verdict
                    FROM app.pull_jobs pj
                    LEFT JOIN app.pull_verifications pv ON pv.pull_id = pj.pull_id
                    WHERE pj.datastream_id = ds.id
                    ORDER BY pj.enqueued_at DESC
                    LIMIT 1
                ) lp ON true
                WHERE ds.project_id = %s
                ORDER BY ds.name ASC
                """,
                (project_id,),
            )
            cols = [d[0] for d in cur.description]
            rows = []
            for row in cur.fetchall():
                rec: dict = {}
                for col, val in zip(cols, row):
                    if col == "last_pull_date" and val is not None:
                        rec[col] = str(val)
                    else:
                        rec[col] = val
                rows.append(rec)
        return rows
    except Exception as exc:
        logger.warning("overview: fleet_base_failed project=%s: %s", project_id, exc)
        return []


def _query_volume_and_issues(
    ds_ids: list[str],
    today_str: str,
    seven_days_ago_str: str,
    conn,
) -> tuple[dict[str, list[int]], dict[str, int]]:
    """For each datastream in ds_ids return:
       - volume_map:  ds_id -> [row_count d-7, d-6, ..., d-1] (7 ints, oldest first; d-1=yesterday)
       - issues_map:  ds_id -> issues_count (dead_letters 24h + failed days 7d)
    """
    if not ds_ids:
        return {}, {}

    volume_map: dict[str, list[int]] = {}
    issues_map: dict[str, int] = {}

    # Build the 7-day date list (oldest first): d-7 through d-1 (yesterday).
    # Fix [HIGH #2]: window covers d-7..d-1 (yesterday inclusive), SQL uses
    # date_to <= yesterday so the last bucket (position 6 = yesterday) can be
    # non-zero.  "today" is never included because pulls for today are still running.
    # Formula: i=0 -> today-7, i=6 -> today-1 (=yesterday).
    today = date.fromisoformat(today_str)
    yesterday = today - timedelta(days=1)
    yesterday_str = yesterday.isoformat()
    date_window = [(today - timedelta(days=7 - i)).isoformat() for i in range(7)]
    # date_window[0] == today-7, date_window[6] == yesterday_str (today-1)

    try:
        # -- Volume: row_counts from pull_verifications per (datastream, date_to) --
        # We use the latest pull per (datastream, date_to) to avoid double-counting.
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    pj.datastream_id,
                    pj.date_to::text AS day,
                    pv.actual_rows
                FROM app.pull_verifications pv
                JOIN app.pull_jobs pj ON pj.pull_id = pv.pull_id
                WHERE pj.datastream_id = ANY(%s)
                  AND pj.date_to >= %s::date
                  AND pj.date_to <= %s::date
                  AND pj.state = 'done'
                ORDER BY pj.datastream_id, pj.date_to ASC, pj.enqueued_at DESC
                """,
                (ds_ids, seven_days_ago_str, yesterday_str),
            )
            # Collect the LATEST verification per (ds_id, day) (first row per group
            # thanks to ORDER BY enqueued_at DESC within each group -- we keep first seen).
            seen: dict[tuple[str, str], int] = {}
            for row in cur.fetchall():
                ds_id, day, actual_rows = row[0], row[1], row[2]
                key = (ds_id, day)
                if key not in seen:
                    seen[key] = int(actual_rows or 0)

        # Build 7-element lists.
        for ds_id in ds_ids:
            counts = []
            for day in date_window:
                counts.append(seen.get((ds_id, day), 0))
            volume_map[ds_id] = counts

    except Exception as exc:
        logger.warning("overview: volume_query_failed: %s", exc)
        for ds_id in ds_ids:
            volume_map[ds_id] = [0] * 7

    try:
        # -- Issues: COUNT DISTINCT pull_ids that are either a dead_letter in the last 24h
        #    OR a failed/dead_letter job in the last 7 days.
        # Fix [LOW #3]: the previous approach counted dl_24h + failed_7d, which
        # double-counted a dead_letter job that fell in BOTH windows.  Using
        # COUNT(DISTINCT pull_id) over the union of conditions gives the correct figure.
        cutoff_24h = (
            datetime.now(tz=timezone.utc) - timedelta(hours=24)
        ).isoformat()

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    pj.datastream_id,
                    COUNT(DISTINCT pj.pull_id) AS issues_count
                FROM app.pull_jobs pj
                WHERE pj.datastream_id = ANY(%s)
                  AND (
                        (pj.state = 'dead_letter' AND pj.enqueued_at >= %s::timestamptz)
                     OR (pj.state IN ('failed', 'dead_letter') AND pj.date_to >= %s::date)
                  )
                GROUP BY pj.datastream_id
                """,
                (ds_ids, cutoff_24h, seven_days_ago_str),
            )
            for row in cur.fetchall():
                ds_id = row[0]
                issues_map[ds_id] = int(row[1] or 0)

    except Exception as exc:
        logger.warning("overview: issues_query_failed: %s", exc)

    return volume_map, issues_map


def _query_deadletters_24h(project_id: str, conn) -> int:
    """Count project-wide dead_letter jobs in the last 24 hours."""
    try:
        cutoff_24h = (
            datetime.now(tz=timezone.utc) - timedelta(hours=24)
        ).isoformat()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                FROM app.pull_jobs pj
                JOIN app.datastreams ds ON ds.id = pj.datastream_id
                WHERE ds.project_id = %s
                  AND pj.state = 'dead_letter'
                  AND pj.enqueued_at >= %s::timestamptz
                """,
                (project_id, cutoff_24h),
            )
            row = cur.fetchone()
            return int(row[0] or 0) if row else 0
    except Exception as exc:
        logger.warning("overview: deadletters_24h_failed project=%s: %s", project_id, exc)
        return 0


def _query_health_data(project_id: str) -> tuple[str | None, list[dict]]:
    """Obtain mirror_last_sync and breakers by calling core.main.health().

    Returns (mirror_last_sync_iso_or_None, breakers_list).
    This is the same code path as admin_api._health_proxy -- no SQL duplication.
    """
    mirror_last_sync: str | None = None
    breakers: list[dict] = []
    try:
        from core.main import health  # noqa: PLC0415

        result = health(project_id=project_id)
        data = result.get("data") or {}
        mirror = data.get("mirror_sync") or {}
        mirror_last_sync = mirror.get("last_synced_at") or None
        quota = data.get("quota") or []
        for b in quota:
            breakers.append({
                "platform": b.get("platform", ""),
                "state": b.get("state", ""),
                "budget_remaining": b.get("budget_remaining"),
            })
    except Exception as exc:
        logger.warning("overview: health_data_failed project=%s: %s", project_id, exc)
    return mirror_last_sync, breakers


def _derive_ledger_status(state: str | None, verdict: str | None) -> str:
    """Derive a ledger status string from pull_jobs.state + pull_verifications.verdict."""
    if state in ("queued", "running"):
        return "running"
    if state in ("failed", "dead_letter"):
        return "failed"
    if state == "done":
        if verdict == "ok" or verdict is None:
            return "ok"
        if verdict == "partial":
            return "partial"
        if verdict == "empty":
            return "empty"
        return "ok"
    return "never_fetched"


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


async def _get_overview(request: Request) -> Response:
    """GET /api/overview?project_id= -- control tower overview (Story 8.4).

    Query params:
        project_id  (required)

    Response (200):
        {kpis, fleet, deadletters_24h, mirror_last_sync, breakers}

    Error responses:
        400 -- missing project_id
        401 -- unauthorized
        500 -- DB error
    """
    authorized, _identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentification requise"},
            status_code=401,
        )

    project_id = (request.query_params.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse(
            {"code": "missing_param", "message": "project_id est requis"},
            status_code=400,
        )

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            data = get_overview(project_id, conn)
    except Exception as exc:
        logger.error("overview: handler_error project=%s: %s", project_id, exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Erreur base de donnees: {exc}"},
            status_code=500,
        )

    return JSONResponse(data)


# ---------------------------------------------------------------------------
# Routes (exported for orchestrator wiring)
# ---------------------------------------------------------------------------

OVERVIEW_ROUTES: list[Route] = [
    Route("/api/overview", endpoint=_get_overview, methods=["GET"]),
]
