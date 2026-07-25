"""toorow -- Daily-insight run/item store (Epic 35, Story 35.3).

Persistence of agentic daily insights decided under gate 35.0 (option A). A published
insight is a DURABLE, self-contained, project-scoped business artifact: ``payload`` freezes
the editorial insight AND the resolved card envelope, so it survives the originating LLM
run (Epic 35 §7). Unlike ``app.render_snapshots`` (bounded retention), these rows are NOT
purged.

Two entities (migration 061):
  - ``app.daily_insight_runs``  : one run per ``(project_id, insight_date)``, with a status
    that keeps the states distinct -- ``published`` / ``no_insight`` / ``blocked`` /
    ``failed`` -- and an ABSENT run ("task did not run") distinct from all of them.
  - ``app.daily_insights``      : 0..3 items per run, ``slot`` unique per
    ``(project_id, insight_date, slot)`` (idempotent publication), ``payload_hash`` and a
    ``render_snapshot_id`` lineage.

Design mirrors ``server/core/snapshots.py`` (ulid ids, ``%s::jsonb`` casts, AD-5 via
``WHERE project_id = %s``, offline-testable with a mocked psycopg connection). No MCP tool
is registered here -- the ``publish_daily_insights`` / readiness / preview tools are Story
35.2 and consume this repository.

ASCII-only stdout (L-3). FR accented copy in exceptions.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from core.daily_insights_schema import (
    MAX_INSIGHTS_PER_DAY,
    canonical_payload_hash,
)

logger = logging.getLogger(__name__)

VALID_STATUSES = frozenset({"published", "no_insight", "blocked", "failed"})


@dataclass(frozen=True)
class InsightItem:
    """One insight to persist under a run.

    ``payload`` is the autonomous published-insight object (already validated by the 35.0
    validator upstream). ``slot`` is 0..2. ``render_snapshot_id`` is the optional lineage
    into ``app.render_snapshots``. ``payload_hash`` is computed here if omitted.
    """

    slot: int
    payload: dict
    render_snapshot_id: str | None = None
    payload_hash: str | None = None


def _mint_run_id() -> str:
    from ulid import ULID  # noqa: PLC0415

    return f"dir_{ULID()}"


def _mint_insight_id() -> str:
    from ulid import ULID  # noqa: PLC0415

    return f"din_{ULID()}"


def record_run(
    *,
    project_id: str,
    insight_date: str,
    status: str,
    items: list[InsightItem] | None = None,
    period_from: str | None = None,
    period_to: str | None = None,
    coverage: dict | None = None,
    host: str | None = None,
    prompt_version: str | None = None,
    contract_version: str | None = None,
    identity: str | None = None,
    trace_id: str | None = None,
    conn,
) -> str:
    """Atomically write a run and its 0..3 items in ONE transaction.

    Idempotent per ``(project_id, insight_date)`` (the run is upserted) and per
    ``(project_id, insight_date, slot)`` (each item upserted by slot). Rolls back on any
    failure -- never a partial run. Returns the run id.

    ``items`` may be empty: a run with no items and status ``no_insight`` is a valid,
    distinct outcome ("no meaningful insight today"), separate from ``blocked`` (J-1 data
    not ready) and ``failed``.
    """

    if status not in VALID_STATUSES:
        raise ValueError(f"statut invalide {status!r} (attendu: {sorted(VALID_STATUSES)})")
    items = items or []
    if len(items) > MAX_INSIGHTS_PER_DAY:
        raise ValueError(f"{len(items)} insights depasse le budget {MAX_INSIGHTS_PER_DAY}/jour")
    slots = [it.slot for it in items]
    if len(set(slots)) != len(slots):
        raise ValueError("slots dupliques dans un meme run")
    if status == "published" and not items:
        raise ValueError("statut 'published' exige au moins un insight")
    if status != "published" and items:
        raise ValueError(f"statut {status!r} ne doit pas porter d'insights")

    try:
        with conn.cursor() as cur:
            # 1. Upsert the run (one per project/date). Refresh status/period/provenance.
            cur.execute(
                """
                SELECT id FROM app.daily_insight_runs
                WHERE project_id = %s AND insight_date = %s
                """,
                (project_id, insight_date),
            )
            existing = cur.fetchone()
            run_id = existing[0] if existing else _mint_run_id()

            if existing:
                cur.execute(
                    """
                    UPDATE app.daily_insight_runs
                    SET status = %s, period_from = %s, period_to = %s,
                        coverage = %s::jsonb, host = %s, prompt_version = %s,
                        contract_version = %s, identity = %s, trace_id = %s,
                        updated_at = now()
                    WHERE id = %s
                    """,
                    (
                        status,
                        period_from,
                        period_to,
                        json.dumps(coverage) if coverage is not None else None,
                        host,
                        prompt_version,
                        contract_version,
                        identity,
                        trace_id,
                        run_id,
                    ),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO app.daily_insight_runs
                        (id, project_id, insight_date, status, period_from, period_to,
                         coverage, host, prompt_version, contract_version, identity, trace_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s)
                    """,
                    (
                        run_id,
                        project_id,
                        insight_date,
                        status,
                        period_from,
                        period_to,
                        json.dumps(coverage) if coverage is not None else None,
                        host,
                        prompt_version,
                        contract_version,
                        identity,
                        trace_id,
                    ),
                )

            # 2. Upsert each item by slot (idempotent publication).
            for item in items:
                if not (0 <= item.slot < MAX_INSIGHTS_PER_DAY):
                    raise ValueError(
                        f"slot {item.slot} hors bornes [0, {MAX_INSIGHTS_PER_DAY - 1}]"
                    )
                payload_hash = item.payload_hash or canonical_payload_hash(item.payload)
                cur.execute(
                    """
                    INSERT INTO app.daily_insights
                        (id, run_id, project_id, insight_date, slot, payload,
                         payload_hash, render_snapshot_id, identity)
                    VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
                    ON CONFLICT (project_id, insight_date, slot) DO UPDATE SET
                        run_id = EXCLUDED.run_id,
                        payload = EXCLUDED.payload,
                        payload_hash = EXCLUDED.payload_hash,
                        render_snapshot_id = EXCLUDED.render_snapshot_id,
                        identity = EXCLUDED.identity
                    """,
                    (
                        _mint_insight_id(),
                        run_id,
                        project_id,
                        insight_date,
                        item.slot,
                        json.dumps(item.payload),
                        payload_hash,
                        item.render_snapshot_id,
                        identity,
                    ),
                )

        conn.commit()
        logger.debug(
            "daily_insights: recorded run=%s project=%s date=%s status=%s items=%d",
            run_id,
            project_id,
            insight_date,
            status,
            len(items),
        )
        return run_id
    except Exception:
        conn.rollback()
        raise


def get_run(project_id: str, insight_date: str, conn) -> dict | None:
    """Return the run for ``(project_id, insight_date)`` plus its items, or None.

    AD-5 scoped: a run of another project is never returned (WHERE project_id).
    """

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, project_id, insight_date, status, period_from, period_to,
                   coverage, host, prompt_version, contract_version, identity,
                   trace_id, created_at, updated_at
            FROM app.daily_insight_runs
            WHERE project_id = %s AND insight_date = %s
            """,
            (project_id, insight_date),
        )
        row = cur.fetchone()
        if row is None:
            return None
        run = _row_to_dict(cur, row, json_cols=("coverage",))
        run["insights"] = _fetch_items(cur, project_id, insight_date)
        return run


def list_runs(project_id: str, conn, *, limit: int = 50, offset: int = 0) -> list[dict]:
    """Run history for a project (most recent first), items NOT included. AD-5 scoped."""

    limit = min(max(1, int(limit)), 200)
    offset = max(0, int(offset))
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, project_id, insight_date, status, period_from, period_to,
                   host, prompt_version, contract_version, created_at, updated_at
            FROM app.daily_insight_runs
            WHERE project_id = %s
            ORDER BY insight_date DESC
            LIMIT %s OFFSET %s
            """,
            (project_id, limit, offset),
        )
        return [_row_to_dict(cur, r) for r in cur.fetchall()]


def get_insight(insight_id: str, project_id: str, conn) -> dict | None:
    """Return one insight item (with full payload), AD-5 scoped, or None."""

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, run_id, project_id, insight_date, slot, payload,
                   payload_hash, render_snapshot_id, identity, created_at
            FROM app.daily_insights
            WHERE id = %s AND project_id = %s
            """,
            (insight_id, project_id),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return _row_to_dict(cur, row, json_cols=("payload",))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fetch_items(cur, project_id: str, insight_date: str) -> list[dict]:
    cur.execute(
        """
        SELECT id, run_id, project_id, insight_date, slot, payload,
               payload_hash, render_snapshot_id, identity, created_at
        FROM app.daily_insights
        WHERE project_id = %s AND insight_date = %s
        ORDER BY slot ASC
        """,
        (project_id, insight_date),
    )
    return [_row_to_dict(cur, r, json_cols=("payload",)) for r in cur.fetchall()]


def _row_to_dict(cur, row, *, json_cols: tuple[str, ...] = ()) -> dict:
    cols = [d[0] for d in cur.description]
    record: dict = {}
    for col, val in zip(cols, row):
        if (
            col in ("created_at", "updated_at", "insight_date", "period_from", "period_to")
            and val is not None
        ):
            record[col] = val.isoformat()
        elif col in json_cols and isinstance(val, str):
            try:
                record[col] = json.loads(val)
            except Exception:  # noqa: BLE001
                record[col] = val
        else:
            record[col] = val
    return record
