"""toorow -- Extract ledger: day-grain status per (datastream, date) (Story 8.3).

Provides:
  get_extract_ledger(datastream_id, date_from, date_to, conn) -> list[dict]

Each returned dict covers one calendar day:
  {
    "date":               "YYYY-MM-DD",
    "status":             "ok" | "partial" | "empty" | "failed" | "running" | "never_fetched",
    "row_count":          int | None,   -- actual_rows from pull_verifications (None if no ver.)
    "expected_rows":      int | None,   -- from manifest profile; None when not declared
    "completeness_ratio": float | None, -- actual/expected; None when expected unknown
    "pull_id":            str | None,   -- pull_id of the covering pull
    "loaded_at":          str | None,   -- ISO-8601 timestamp of pull completion
    # Story 25.2 (AC5): present ONLY on failed days (status == "failed").
    "error_class":        str | None,   -- canonical taxonomy class; None for legacy text
    "user_action":        str | None,   -- e.g. "reconnect"; None when not user-actionable
  }

Status derivation (AD-7-consistent, Refinement R1):
  For each calendar day D, find the LATEST pull_jobs row covering D
  (date_from <= D <= date_to) for the given datastream_id.

  Priority 1: pulls with matching datastream_id (post-8.2 mode).
  Priority 2 (pre-8.2 fallback): pulls where datastream_id IS NULL but
    connection_ref_id = ds.connection_ref_id AND date_from <= D AND date_to >= D.

  If a covering pull is found:
    - state IN ('queued', 'running')        -> status = 'running'
    - state IN ('failed', 'dead_letter')    -> status = 'failed'
      (unless a NEWER successful pull also covers D -- then use that)
    - state = 'done': join pull_verifications by pull_id:
        - verdict = 'ok'     -> status = 'ok'
        - verdict = 'partial' -> status = 'partial'
        - verdict = 'empty'  -> status = 'empty'
        - no verification yet -> status = 'ok' (conservative; verification pending)
  If no covering pull found at all: status = 'never_fetched'.

Design decisions:
  - We always prefer the LATEST pull (by enqueued_at DESC) covering the day.
    A re-fetch turning a failed pull into a successful one resolves to the new status.
  - expected_rows is read from pull_verifications.expected_rows (set at verification
    time from the manifest profile), NOT re-computed here. When the pull has no
    verification row (e.g., still running or verification failed silently),
    expected_rows and completeness_ratio are None.
  - This function issues a single batch query for the entire date range, not N+1
    per-day queries. The result is assembled in Python.

AD-2: no module-specific strings.
AD-5: caller must supply a project-scoped datastream_id (already enforced by the REST
      layer before calling this function).
ASCII-only log strings (AI-03).
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta

logger = logging.getLogger(__name__)


def _parse_error_class(error_detail) -> tuple[str | None, str | None]:
    """Extract (error_class, user_action) from a pull's error_detail (Story 25.2).

    Story 25.2 writes error_detail as a structured JSON envelope. Legacy pulls
    stored plain text (e.g. "upstream error"). Parse best-effort: try json.loads
    and read the two keys; on any failure (legacy text, malformed, non-dict)
    return (None, None). Never raises. Additive: the pair is only surfaced on
    failed days, existing keys are untouched.
    """
    if not error_detail or not isinstance(error_detail, str):
        return None, None
    try:
        parsed = json.loads(error_detail)
    except (ValueError, TypeError):
        return None, None
    if not isinstance(parsed, dict):
        return None, None
    return parsed.get("error_class"), parsed.get("user_action")

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_extract_ledger(
    datastream_id: str,
    date_from: str,
    date_to: str,
    conn,
) -> list[dict]:
    """Return day-grain extract ledger for *datastream_id* over [date_from, date_to].

    Args:
        datastream_id: The ds_<ULID> identifier (already project-scoped by caller).
        date_from:     Inclusive start date, YYYY-MM-DD.
        date_to:       Inclusive end date, YYYY-MM-DD.
        conn:          Open psycopg connection (not committed here).

    Returns:
        List of dicts, one per calendar day, ordered date ASC.
        See module docstring for field definitions.
    """
    try:
        d_from = date.fromisoformat(date_from)
        d_to = date.fromisoformat(date_to)
    except (ValueError, TypeError) as exc:
        logger.warning("extract_ledger: invalid_dates ds=%s: %s", datastream_id, exc)
        return []

    if d_to < d_from:
        return []

    # Build the list of calendar days.
    days: list[date] = []
    cur_d = d_from
    while cur_d <= d_to:
        days.append(cur_d)
        cur_d += timedelta(days=1)

    # ---------------------------------------------------------------------------
    # Step 1: Fetch the datastream's connection_ref_id for the pre-8.2 fallback.
    # ---------------------------------------------------------------------------
    connection_ref_id: str | None = None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT connection_ref_id FROM app.datastreams WHERE id = %s",
                (datastream_id,),
            )
            row = cur.fetchone()
            if row:
                connection_ref_id = row[0]
    except Exception as exc:
        logger.warning(
            "extract_ledger: ds_lookup_failed ds=%s: %s", datastream_id, exc
        )

    # ---------------------------------------------------------------------------
    # Step 2: Batch-fetch all pull_jobs covering this date range for this
    # datastream (post-8.2) and also pre-8.2 pulls via connection_ref_id overlap.
    # Ordered enqueued_at DESC so we can pick the LATEST pull per day.
    # ---------------------------------------------------------------------------
    pulls: list[dict] = []
    try:
        # review-epic-8 CRITICAL: this is an OVERLAP test, not containment.
        # A pull overlaps the window when pull.date_from <= window_end (date_to)
        # AND pull.date_to >= window_start (date_from). The previous binding
        # (date_from<=date_from AND date_to>=date_to) required the pull to FULLY
        # CONTAIN the window, so any narrow nightly pull was excluded and every
        # multi-day ledger query returned 'never_fetched'.
        # Base query has 3 placeholders (datastream_id, window_end, window_start);
        # the UNION branch extends 3 more. The previous list carried 2 extra
        # window params, so the connection_ref path passed 8 params for 6
        # placeholders -> the query ALWAYS raised and the fail-soft path returned
        # 'never_fetched' for every day.
        params: list = [datastream_id, date_to, date_from]
        sql = """
            SELECT
                pj.id         AS job_id,
                pj.pull_id,
                pj.datastream_id,
                pj.connection_ref_id,
                pj.date_from,
                pj.date_to,
                pj.state,
                pj.completed_at,
                pj.enqueued_at,
                pj.error_detail,
                pv.verdict,
                pv.actual_rows,
                pv.expected_rows,
                pv.completeness_ratio
            FROM app.pull_jobs pj
            LEFT JOIN app.pull_verifications pv ON pv.pull_id = pj.pull_id
            WHERE pj.datastream_id = %s
              AND pj.date_from <= %s::date
              AND pj.date_to   >= %s::date
        """
        # Pre-8.2 fallback: also match by connection_ref_id when available.
        if connection_ref_id:
            sql += """
              UNION ALL
            SELECT
                pj.id         AS job_id,
                pj.pull_id,
                pj.datastream_id,
                pj.connection_ref_id,
                pj.date_from,
                pj.date_to,
                pj.state,
                pj.completed_at,
                pj.enqueued_at,
                pj.error_detail,
                pv.verdict,
                pv.actual_rows,
                pv.expected_rows,
                pv.completeness_ratio
            FROM app.pull_jobs pj
            LEFT JOIN app.pull_verifications pv ON pv.pull_id = pj.pull_id
            WHERE pj.datastream_id IS NULL
              AND pj.connection_ref_id = %s
              AND pj.date_from <= %s::date
              AND pj.date_to   >= %s::date
            """
            params.extend([connection_ref_id, date_to, date_from])
        sql += " ORDER BY enqueued_at DESC"

        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            for row in cur.fetchall():
                rec: dict = {}
                for col, val in zip(cols, row):
                    if col in ("date_from", "date_to") and val is not None:
                        rec[col] = str(val)
                    elif col in ("completed_at", "enqueued_at") and val is not None:
                        rec[col] = val.isoformat()
                    else:
                        rec[col] = val
                pulls.append(rec)
    except Exception as exc:
        logger.warning(
            "extract_ledger: pulls_query_failed ds=%s: %s", datastream_id, exc
        )
        pulls = []

    # ---------------------------------------------------------------------------
    # Step 3: For each calendar day, find the latest pull covering it.
    # Prefer post-8.2 pulls (datastream_id matches) over pre-8.2 (fallback).
    # Within same tier, take the one with latest enqueued_at (list is DESC sorted).
    # ---------------------------------------------------------------------------

    def _covers(pull: dict, day: date) -> bool:
        try:
            pf = date.fromisoformat(pull["date_from"])
            pt = date.fromisoformat(pull["date_to"])
            return pf <= day <= pt
        except (ValueError, TypeError):
            return False

    result: list[dict] = []
    for day in days:
        # Find best pull: prefer datastream_id-matching first.
        best_pull: dict | None = None
        for pull in pulls:
            if not _covers(pull, day):
                continue
            if best_pull is None:
                best_pull = pull
                continue
            # Prefer datastream_id match over fallback pulls.
            current_is_ds = best_pull.get("datastream_id") == datastream_id
            candidate_is_ds = pull.get("datastream_id") == datastream_id
            if candidate_is_ds and not current_is_ds:
                best_pull = pull
            # Already DESC-sorted; first matching pull in same tier wins.

        result.append(_day_to_ledger_entry(day, best_pull))

    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _day_to_ledger_entry(day: date, pull: dict | None) -> dict:
    """Convert a (day, best covering pull) pair into a ledger row dict."""
    day_str = day.isoformat()

    if pull is None:
        return {
            "date": day_str,
            "status": "never_fetched",
            "row_count": None,
            "expected_rows": None,
            "completeness_ratio": None,
            "pull_id": None,
            "loaded_at": None,
        }

    state: str = pull.get("state") or ""
    verdict: str | None = pull.get("verdict")
    actual_rows: int | None = pull.get("actual_rows")
    expected_rows: int | None = pull.get("expected_rows")
    completeness_ratio: float | None = pull.get("completeness_ratio")
    loaded_at: str | None = pull.get("completed_at")

    if state in ("queued", "running"):
        status = "running"
    elif state in ("failed", "dead_letter"):
        status = "failed"
    elif state == "done":
        # Derive from verification verdict.
        if verdict == "ok":
            status = "ok"
        elif verdict == "partial":
            status = "partial"
        elif verdict == "empty":
            status = "empty"
        else:
            # No verification row yet (still processing or silently failed).
            status = "ok"
    else:
        # Unknown state — treat conservatively.
        status = "never_fetched"

    # Cast completeness_ratio to float if it came as Decimal from psycopg.
    if completeness_ratio is not None:
        try:
            completeness_ratio = float(completeness_ratio)
        except (TypeError, ValueError):
            completeness_ratio = None

    entry = {
        "date": day_str,
        "status": status,
        "row_count": actual_rows,
        "expected_rows": expected_rows,
        "completeness_ratio": completeness_ratio,
        "pull_id": pull.get("pull_id"),
        "loaded_at": loaded_at,
    }

    # Story 25.2 (AC5): failed days surface the canonical error_class + user_action
    # parsed from the covering pull's structured error_detail. Legacy plain-text
    # error_detail yields (None, None). Additive: only present on failed days.
    if status == "failed":
        error_class, user_action = _parse_error_class(pull.get("error_detail"))
        entry["error_class"] = error_class
        entry["user_action"] = user_action

    return entry
