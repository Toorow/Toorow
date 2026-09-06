"""The runs a Datastream SHOWS -- the read model behind its object page.

WHY THIS IS NOT A `*_api.py`. It declares no route and parses no request. It
answers one question -- "what has run on this Datastream, and where is each run
now" -- for the handlers that render the object. `module-boundaries.md` asks a
route module to hold request parsing, authorization and a call into a service
module; these six functions ARE that service module, and they were the reason
`datastreams_api` would not fit under its ceiling.

Extracted from `admin_api.py` under AD-40 on 2026-08-12, unchanged.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime

from core import execution_states as _execution_states
from core.execution_progress import COLLECTION_PLAN_KIND as _COLLECTION_PLAN_KIND

logger = logging.getLogger("core.admin_api")

# --- les handlers, deplaces TELS QUELS -----------------------------------

def _normalize_run_interval(projection_plan: object) -> dict[str, str] | None:
    """Return an exact half-open run interval from persisted projection evidence."""
    if isinstance(projection_plan, str):
        try:
            projection_plan = json.loads(projection_plan)
        except (TypeError, ValueError):
            return None
    if not isinstance(projection_plan, dict):
        return None
    half_open = projection_plan.get("half_open_range")
    interval = half_open if isinstance(half_open, dict) else projection_plan.get("interval")
    if not isinstance(interval, dict):
        return None
    date_from = interval.get("from")
    to_exclusive = interval.get("to_exclusive")
    try:
        start = date.fromisoformat(str(date_from))
        end = date.fromisoformat(str(to_exclusive))
    except (TypeError, ValueError):
        return None
    if start >= end:
        return None
    return {"from": start.isoformat(), "to_exclusive": end.isoformat()}

def _read_current_published_execution(conn, ds_id: str, project_id: str) -> str | None:
    """Read app.datastreams.current_published_execution_id (project-scoped)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT current_published_execution_id FROM app.datastreams "
            "WHERE id = %s AND project_id = %s",
            (ds_id, project_id),
        )
        row = cur.fetchone()
    return row[0] if row is not None else None

def _enrich_datastream_runs(
    runs: list[dict],
    recent_imports: list[dict],
    current_published_execution_id: str | None,
) -> None:
    """Attach optional ledger/publication evidence without creating run rows."""
    ledger_by_execution = {
        row.get("execution_id"): row
        for row in recent_imports
        if isinstance(row, dict) and isinstance(row.get("execution_id"), str)
    }

    for run in runs:
        execution_id = run["id"]
        ledger = ledger_by_execution.get(execution_id)
        if ledger is not None:
            run["import_evidence"] = {
                "ledger_id": ledger.get("id"),
                "outcome": ledger.get("outcome"),
                "row_count": ledger.get("row_count"),
                "rejected_row_count": ledger.get("rejected_row_count"),
                "snapshot_observed_at": ledger.get("snapshot_observed_at"),
            }
        if execution_id == current_published_execution_id:
            run["publication_state"] = "current"

def _read_current_candidate_execution(
    conn, ds_id: str, project_id: str, published_execution_id: str | None
) -> dict | None:
    """Read the newest NON-terminal execution (the current candidate), or None.

    A candidate is an execution that is not the published pointer and not in a
    terminal state (published / failed / cancelled) -- i.e. one still flowing toward
    publication (created / loading / validating / ready / publishing). Surfaces its
    DQ state (``state``), freshness (``state_changed_at``),
    row_count and content_hash for the 12.14 UI. Project-scoped; reuses the 042
    execution columns (no new table).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, state, state_changed_at, content_hash, row_count,
                   plan_version_id, mapping_version_id, error_code, created_at
            FROM app.datastream_executions
            WHERE datastream_id = %s AND project_id = %s
              -- Story 63.1: the states come from `core.execution_states`, the
              -- one place that classifies them. A literal list here was how
              -- `collected` -- terminal, and never a candidate to review --
              -- would have put a permanent "Review candidate" on every
              -- scheduled Datastream.
              AND state = ANY(%s)
              -- A recurring collection publishes nothing by design: it is not a
              -- candidate awaiting review even while it is in flight.
              AND COALESCE(projection_plan_ref->>'kind', '') <> %s
              -- Cast required: with no published execution the parameter is
              -- NULL, and an uncast placeholder gives Postgres nothing to infer
              -- a type from ("could not determine data type of parameter $3").
              -- The whole Overview then answered 503 -- for every Datastream
              -- that had never published, which is every new one.
              AND (%s::text IS NULL OR id <> %s::text)
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (
                ds_id,
                project_id,
                list(_execution_states.CANDIDATE_STATES),
                _COLLECTION_PLAN_KIND,
                published_execution_id,
                published_execution_id,
            ),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
    record: dict = {}
    for col, val in zip(cols, row):
        if val is not None and hasattr(val, "isoformat"):
            record[col] = val.isoformat()
        else:
            record[col] = val
    return record

def _read_datastream_runs(
    conn, ds_id: str, project_id: str, *, limit: int = 100
) -> list[dict]:
    """Read the universal execution timeline; ledger rows never define membership."""
    bounded_limit = max(1, min(int(limit), 200))
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT e.id, e.state, e.state_changed_at, e.row_count, e.plan_version_id,
                   e.mapping_version_id, e.error_code, e.created_at, e.projection_plan_ref,
                   e.created_by,
                   EXISTS (
                       SELECT 1 FROM app.datastream_publication_log pl
                       WHERE pl.execution_id = e.id AND pl.project_id = e.project_id
                   ) AS was_published
            FROM app.datastream_executions e
            WHERE datastream_id = %s AND project_id = %s
            ORDER BY created_at DESC, id DESC
            LIMIT %s
            """,
            (ds_id, project_id, bounded_limit),
        )
        rows = cur.fetchall()
    runs: list[dict] = []
    for row in rows:
        created_at = row[7]
        state_changed_at = row[2]
        duration_seconds = None
        if isinstance(created_at, datetime) and isinstance(state_changed_at, datetime):
            duration_seconds = max(0, int((state_changed_at - created_at).total_seconds()))
        projection_plan = row[8]
        if isinstance(projection_plan, str):
            try:
                projection_plan = json.loads(projection_plan)
            except (TypeError, ValueError):
                projection_plan = None
        runs.append(
            {
                "id": row[0],
                "state": row[1],
                "state_changed_at": (
                    state_changed_at.isoformat()
                    if state_changed_at is not None and hasattr(state_changed_at, "isoformat")
                    else state_changed_at
                ),
                "row_count": row[3],
                "plan_version_id": row[4],
                "mapping_version_id": row[5],
                "error_code": row[6],
                "created_at": (
                    created_at.isoformat()
                    if created_at is not None and hasattr(created_at, "isoformat")
                    else created_at
                ),
                "created_by": row[9],
                "duration_seconds": duration_seconds,
                "recovery_kind": (
                    projection_plan.get("recovery_kind")
                    if isinstance(projection_plan, dict)
                    and isinstance(projection_plan.get("recovery_kind"), str)
                    else None
                ),
                "recovery_interval": _normalize_run_interval(projection_plan),
                "import_evidence": None,
                "publication_state": (
                    "previously_published" if bool(row[10]) else "unpublished"
                ),
            }
        )
    return runs

def _read_latest_execution(conn, ds_id: str, project_id: str) -> dict | None:
    """Return the newest execution, including terminal failures, for health evidence."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, state, state_changed_at, content_hash, row_count,
                   plan_version_id, mapping_version_id, error_code, created_at
            FROM app.datastream_executions
            WHERE datastream_id = %s AND project_id = %s
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (ds_id, project_id),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
    record: dict = {}
    for col, val in zip(cols, row):
        record[col] = val.isoformat() if val is not None and hasattr(val, "isoformat") else val
    return record
