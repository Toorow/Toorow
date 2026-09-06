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

A published item can be RETRACTED (migration 321): an audited state transition on
three columns that move together -- ``retracted_at`` / ``retracted_by`` /
``retracted_reason`` -- and never a DELETE. The row is the evidence that the claim was
made; destroying it loses the difference between "this was never said" and "this was
said, and later withdrawn". Every reader that shows a day keeps the item and labels it;
every surface that VOLUNTEERS assertions stops carrying it.

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

from core.audit import declare_action
from core.daily_insights_schema import (
    MAX_INSIGHTS_PER_DAY,
    canonical_payload_hash,
)

logger = logging.getLogger(__name__)

VALID_STATUSES = frozenset({"published", "no_insight", "blocked", "failed"})

#: Declared beside the code that writes it (AD-42 inversion, `core.audit`). A
#: retraction with no audit row is a withdrawal nobody can attribute afterwards,
#: which is the anonymous erasure migration 321 exists to refuse.
ACTION_DAILY_INSIGHT_RETRACTED = declare_action("daily_insight.retracted")

#: The columns a read hands back for one item. One list, so a reader cannot get a
#: payload without the state that says whether it is still asserted.
_INSIGHT_ROW_COLS = (
    "id", "run_id", "project_id", "insight_date", "slot", "payload",
    "payload_hash", "render_snapshot_id", "identity", "created_at",
    "query_spec_version_id", "result_id", "result_unavailable_reason",
    "retracted_at", "retracted_by", "retracted_reason",
)


class DailyInsightRefusal(ValueError):
    """A named refusal on the retraction path -- it carries the gesture, not a cause.

    ``code`` is the machine word a door answers with; ``message`` is the sentence a
    person reads and it names what to do next.
    """

    def __init__(self, code: str, message: str, *, status: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass(frozen=True)
class InsightItem:
    """One insight to persist under a run.

    ``payload`` is the autonomous published-insight object (already validated by the 35.0
    validator upstream). ``slot`` is 0..2. ``render_snapshot_id`` is the optional lineage
    into ``app.render_snapshots``. ``payload_hash`` is computed here if omitted.

    ``query_spec_version_id`` / ``result_id`` are the governed lineage AI-294's design
    added (migration 281): publication derives a Query Spec behind the card and executes
    it, so the insight names the Result the ONE Share mechanism operates on. When the
    card could not be put behind a spec, ``result_unavailable_reason`` names the missing
    link -- the row never carries both a Result and a reason (DB CHECK).
    """

    slot: int
    payload: dict
    render_snapshot_id: str | None = None
    payload_hash: str | None = None
    query_spec_version_id: str | None = None
    result_id: str | None = None
    result_unavailable_reason: str | None = None


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
            # 0. A RETRACTED SLOT IS NOT A FREE SLOT (migration 321).
            # Publication is idempotent per (project, date, slot), so an upsert onto
            # a withdrawn claim would swap the prose beneath a retraction that names
            # a reason for the OLD prose -- the new claim would be born retracted, and
            # the withdrawal would stand over something it never judged. The trigger
            # refuses that write; this refuses it BEFORE the transaction, with the
            # gesture rather than a constraint name. A retraction is never unmade, so
            # the repair is the same one 286 gives: publish again, on a free slot.
            if items:
                # THE WHOLE DAY, not the requested slots (review of ae60c22a, R1):
                # computing "free" from the requested slots alone named a slot that
                # routinely carried a STANDING published claim, and following the
                # refusal's own gesture would have upserted over it -- the exact
                # destruction this block exists to abolish. Free means: no row of
                # any kind on that day, retracted or standing.
                cur.execute(
                    """
                    SELECT slot, retracted_at IS NOT NULL
                    FROM app.daily_insights
                    WHERE project_id = %s AND insight_date = %s
                    ORDER BY slot ASC
                    """,
                    (project_id, insight_date),
                )
                day = [(int(row[0]), bool(row[1])) for row in (cur.fetchall() or [])]
                requested = {it.slot for it in items}
                retracted = sorted(s for s, gone in day if gone and s in requested)
                if retracted:
                    occupied = {s for s, _ in day}
                    # ... and not the caller's own batch either (review residue 1):
                    # naming a slot the SAME run is about to fill sends the caller
                    # into "slots dupliques dans un meme run".
                    free = sorted(
                        set(range(MAX_INSIGHTS_PER_DAY)) - occupied - requested
                    )
                    where = (
                        f"slot {free[0] + 1} of that day is free"
                        if free
                        else "no slot of that day is free"
                    )
                    raise DailyInsightRefusal(
                        "slot_retracted",
                        "Insight "
                        + ", ".join(str(n + 1) for n in retracted)
                        + f" of {insight_date} was retracted, and a retracted claim is "
                        f"never rewritten. Publish this reading as a new insight -- "
                        f"{where}.",
                    )

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
                         payload_hash, render_snapshot_id, identity,
                         query_spec_version_id, result_id, result_unavailable_reason)
                    VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (project_id, insight_date, slot) DO UPDATE SET
                        run_id = EXCLUDED.run_id,
                        payload = EXCLUDED.payload,
                        payload_hash = EXCLUDED.payload_hash,
                        render_snapshot_id = EXCLUDED.render_snapshot_id,
                        identity = EXCLUDED.identity,
                        query_spec_version_id = EXCLUDED.query_spec_version_id,
                        result_id = EXCLUDED.result_id,
                        result_unavailable_reason = EXCLUDED.result_unavailable_reason
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
                        item.query_spec_version_id,
                        item.result_id,
                        item.result_unavailable_reason,
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
    """Run history for a project (most recent first), items NOT included. AD-5 scoped.

    The payloads stay out -- fourteen days of frozen cards is not a journal. What
    comes back instead are the two COUNTS the journal actually reads: how many
    insights the day carries, and how many of them were retracted. The count was
    missing until 2026-08-30 and `run_journal` derived `itemCount` from the absent
    item list, so every row of the history said `0` and the console's own condition
    (`itemCount > 0`) never opened a day. A journal that reports zero on every
    published day is a journal that says the opposite of what happened.
    """

    limit = min(max(1, int(limit)), 200)
    offset = max(0, int(offset))
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.id, r.project_id, r.insight_date, r.status, r.period_from,
                   r.period_to, r.host, r.prompt_version, r.contract_version,
                   r.created_at, r.updated_at,
                   (SELECT count(*) FROM app.daily_insights i WHERE i.run_id = r.id)
                       AS item_count,
                   (SELECT count(*) FROM app.daily_insights i
                     WHERE i.run_id = r.id AND i.retracted_at IS NOT NULL)
                       AS retracted_count,
                   (SELECT coalesce(array_agg(i.slot ORDER BY i.slot), '{}')
                      FROM app.daily_insights i WHERE i.run_id = r.id)
                       AS slots
            FROM app.daily_insight_runs r
            WHERE r.project_id = %s
            ORDER BY r.insight_date DESC
            LIMIT %s OFFSET %s
            """,
            (project_id, limit, offset),
        )
        return [_row_to_dict(cur, r) for r in cur.fetchall()]


def get_insight(insight_id: str, project_id: str, conn) -> dict | None:
    """Return one insight item (with full payload), AD-5 scoped, or None."""

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {", ".join(_INSIGHT_ROW_COLS)}
            FROM app.daily_insights
            WHERE id = %s AND project_id = %s
            """,  # noqa: S608 -- the column list is a module constant
            (insight_id, project_id),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return _row_to_dict(cur, row, json_cols=("payload",))


def retract_insight(
    *,
    project_id: str,
    insight_id: str,
    retracted_by: str,
    reason: str,
    conn,
) -> dict:
    """Withdraw a published insight -- an audited state transition, never a DELETE.

    `proactive-assertions.md`, decision 4: a proactive assertion is erasable, and
    the erasure is a retraction. The row STAYS, because it is the evidence that the
    claim was made: it may already have been read, quoted, forwarded, or opened at
    the Result its publication produced (migration 281). Deleting it would lose the
    difference between "this was never said" and "this was said, and later
    withdrawn" -- the opposite defect from the one being repaired.

    THE REASON IS REQUIRED and is not decoration. A withdrawal nobody can judge
    later is the same object as an anonymous erasure: the next reader has to decide
    whether the claim was wrong or merely inconvenient, and nothing on the row
    answers. Migration 321 CHECKs the same rule, and the three columns move
    together or not at all.

    A RETRACTION IS NEVER UNMADE (the trigger of 321 refuses it). A retraction filed
    by mistake is repaired the way this family repairs everything -- by publishing
    again, on a free slot of the same day. `record_run` refuses to overwrite a
    retracted slot for the same reason, and names the free one.

    Returns the retracted row. Raises `DailyInsightRefusal` (not found / already
    retracted / missing reason); the caller maps `code` onto its own vocabulary.
    """

    from core.audit import insert_audit_row  # noqa: PLC0415

    reason_clean = str(reason).strip() if reason else ""
    if not reason_clean:
        raise DailyInsightRefusal(
            "missing_reason",
            "Say why this insight is withdrawn -- a retraction with no reason is one "
            "nobody can judge later.",
        )

    try:
        with conn.cursor() as cur:
            # FOR UPDATE: two people withdrawing the same claim at once would
            # otherwise both pass the already-retracted check and the second write
            # would hit the trigger with a constraint name instead of the sentence
            # below. Scoped by project (AD-5): a row of another project is ABSENT,
            # never forbidden.
            cur.execute(
                """
                SELECT retracted_at, retracted_by, slot, insight_date
                FROM app.daily_insights
                WHERE id = %s AND project_id = %s
                FOR UPDATE
                """,
                (insight_id, project_id),
            )
            current = cur.fetchone()
            if current is None:
                raise DailyInsightRefusal(
                    "not_found",
                    "This insight does not exist in this project.",
                    status=404,
                )
            if current[0] is not None:
                raise DailyInsightRefusal(
                    "already_retracted",
                    f"This insight was already retracted on {str(current[0])[:10]}, "
                    f"by {current[1]}. A retraction is never unmade -- publish the new "
                    f"reading on a free slot of that day.",
                    status=409,
                )

            cur.execute(
                f"""
                UPDATE app.daily_insights
                   SET retracted_at = now(), retracted_by = %s, retracted_reason = %s
                 WHERE id = %s AND project_id = %s
             RETURNING {", ".join(_INSIGHT_ROW_COLS)}
                """,  # noqa: S608 -- the column list is a module constant
                (retracted_by, reason_clean, insight_id, project_id),
            )
            row = cur.fetchone()
            if row is None:  # pragma: no cover -- the row is locked above
                raise RuntimeError("UPDATE RETURNING returned no row")
            retracted = _row_to_dict(cur, row, json_cols=("payload",))

        insert_audit_row(
            conn,
            identity=retracted_by,
            action=ACTION_DAILY_INSIGHT_RETRACTED,
            provider_account="",
            connection_ref="",
            metadata={
                "insight_id": insight_id,
                "project_id": project_id,
                "insight_date": str(retracted.get("insight_date")),
                "slot": retracted.get("slot"),
                "reason": reason_clean,
            },
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    logger.debug(
        "daily_insights: retracted insight=%s project=%s by=%s",
        insight_id,
        project_id,
        retracted_by,
    )
    return retracted


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fetch_items(cur, project_id: str, insight_date: str) -> list[dict]:
    cur.execute(
        f"""
        SELECT {", ".join(_INSIGHT_ROW_COLS)}
        FROM app.daily_insights
        WHERE project_id = %s AND insight_date = %s
        ORDER BY slot ASC
        """,  # noqa: S608 -- the column list is a module constant
        (project_id, insight_date),
    )
    return [_row_to_dict(cur, r, json_cols=("payload",)) for r in cur.fetchall()]


def _row_to_dict(cur, row, *, json_cols: tuple[str, ...] = ()) -> dict:
    """AI-219: dates by TYPE; only the JSON columns still answer to their names.

    Naming a JSON column is unavoidable -- a `str` is a legitimate value for a
    `text` column and only the caller knows this one holds a document. Naming a
    date column was never necessary, and the five names here were exactly the
    five that existed the day this was written.
    """
    from core.row_json import row_to_json  # noqa: PLC0415

    record = row_to_json(cur.description, row)
    for col in json_cols:
        if isinstance(record.get(col), str):
            try:
                record[col] = json.loads(record[col])
            except Exception:  # noqa: BLE001
                pass
    return record
