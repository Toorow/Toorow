"""toorow -- Queue abstraction for pull job dispatch (Story 3.2, AC1, AC3, AC8).

Story 3.3 additions:
  - Quota pre-check in _execute_job (AC3): calls quota.pre_check() before executing.
    If blocked (breaker_open or budget_exhausted): re-queues job (attempt_count unchanged).
  - RateLimitError handling: if pull fn raises RateLimitError, record_rate_limit() is called
    and the job is re-queued (attempt_count incremented -- rate-limit was an actual attempt).
  - Exponential backoff in _worker_loop: tracks consecutive quota-blocked cycles, sleeps
    min(base_delay * 2**count, MAX_BACKOFF_SECONDS) when blocked.

Exports:
  enqueue_pull(connection_ref_id, date_from, date_to, *, requested_by) -> dict
  get_job_status(job_id) -> dict | None
  start_queue_worker() -- start the background worker thread (called from build_asgi_app)
  JobState -- string constants: QUEUED, RUNNING, DONE, FAILED, DEAD_LETTER

Backend selection via QUEUE_BACKEND env var:
  "local"        (default): Postgres-backed job table + daemon thread worker.
  "cloud_tasks"  (Phase B): Cloud Tasks backend -- interface-complete, mock-tested only.

Design decisions (Story 3.2, Dev Agent Record):
  - Local backend: Option A (daemon thread) chosen over Option B (per-enqueue thread).
    Rationale: matches health_poller.py precedent; poll semantics support retry logic;
    GIL is not a concern (worker is I/O-bound); natural path toward Cloud Tasks swap.
  - ULID minting: _mint_job_id / _mint_pull_id defined here; admin_api.py imports
    _mint_pull_id from here as the canonical source (AD-7: core mints pull_id).
  - SKIP LOCKED: dequeue query uses FOR UPDATE SKIP LOCKED for concurrency safety
    if a second worker ever runs (Epic 3+ scale).
  - Connection pool: per-call get_connection() -- see TODO(AI-17) comments.

AD-2: core never imports from server/modules/*. Worker calls pull functions via
get_module_pull_fn(provider) from core.main only.
AD-7: pull_id is minted inside enqueue_pull(), before the job row is inserted.
HG-5: no provider-specific strings in this file.
HG-6: every get_connection() call carries the AI-17 TODO comment.
Windows/CI note (L-3): all log strings use ASCII-safe characters only.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time

from core.audit import declare_action
from core.pull_job_states import (
    CANCELLED,
    DEAD_LETTER,
    DONE,
    FAILED,
    PREVENTED,
    QUEUED,
    RUNNING,
    SUPERSEDED,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# JobState constants (AC1) -- READ from the registry, never re-typed (63.6)
# ---------------------------------------------------------------------------
#
# These five names used to be declared here, and `superseded` -- which migration
# 022 has written since -- was not among them. `core.pull_job_states` is the one
# owner; this module keeps the names it has always exported so its callers are
# untouched, and it no longer holds an opinion about what they mean.

# --- LES ACTIONS QUE CE MODULE ECRIT ------------------------------------
#
# AD-42 (2026-08-12) : declarees ICI, a cote du code qui les ecrit, et non
# dans `core/audit.py`. Ce fichier etait un carrefour -- 43 editions de 29
# sujets depuis juin, dont 34 n'ajoutaient qu'une constante -- et 45 % des
# actions reellement ecrites en production n'y etaient meme pas declarees,
# parce que la liste etait trop loin pour valoir le detour. `write_audit_row`
# refuse desormais une action que personne n'a declaree.
ACTION_PULL_COMPLETED = declare_action("pull.completed")
ACTION_PULL_FAILED = declare_action("pull.failed")
# AI-307. Its OWN action, not `pull.completed` carrying a zero: the audit log is
# the durable trace of what happened, and a window the source refused, recorded
# there as a completion, is the same lie this branch exists to stop.
ACTION_PULL_PREVENTED = declare_action("pull.prevented")



class JobState:
    """Namespace for job state string constants."""

    QUEUED = QUEUED
    RUNNING = RUNNING
    DONE = DONE
    FAILED = FAILED
    DEAD_LETTER = DEAD_LETTER
    SUPERSEDED = SUPERSEDED
    CANCELLED = CANCELLED
    PREVENTED = PREVENTED


# ---------------------------------------------------------------------------
# ULID helpers (AD-7: core mints IDs at dispatch)
# ---------------------------------------------------------------------------


def _mint_job_id() -> str:
    """Mint a new ULID with 'job_' prefix."""
    from ulid import ULID  # noqa: PLC0415

    return f"job_{ULID()}"


def _mint_pull_id() -> str:
    """Mint a new ULID with 'pull_' prefix (AD-7: core mints pull_id at dispatch)."""
    from ulid import ULID  # noqa: PLC0415

    return f"pull_{ULID()}"


def _capture_trace_id() -> str | None:
    """Return the current OTel trace_id (32-hex) or None (Story 5.1, AC5).

    Best-effort: None when tracing is disabled/unavailable. Never raises.
    """
    try:
        from core import tracing  # noqa: PLC0415

        return tracing.current_trace_id_hex()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_MAX_ATTEMPTS_DEFAULT = 3
_POLL_INTERVAL_DEFAULT = 5
_MAX_BACKOFF_DEFAULT = 300


def _max_attempts() -> int:
    try:
        return int(os.environ.get("QUEUE_MAX_ATTEMPTS", str(_MAX_ATTEMPTS_DEFAULT)))
    except (ValueError, TypeError):
        return _MAX_ATTEMPTS_DEFAULT


def _poll_interval() -> int:
    try:
        return int(os.environ.get("QUEUE_POLL_INTERVAL_SECONDS", str(_POLL_INTERVAL_DEFAULT)))
    except (ValueError, TypeError):
        return _POLL_INTERVAL_DEFAULT


def _max_backoff() -> int:
    """Maximum exponential-backoff sleep in seconds (Story 3.3, AC4)."""
    try:
        return int(os.environ.get("QUEUE_MAX_BACKOFF_SECONDS", str(_MAX_BACKOFF_DEFAULT)))
    except (ValueError, TypeError):
        return _MAX_BACKOFF_DEFAULT


# ---------------------------------------------------------------------------
# Local backend
# ---------------------------------------------------------------------------


class LocalBackend:
    """Postgres-backed job table with a daemon thread worker (Option A).

    enqueue_pull() inserts the job row and writes the ACTION_PULL_TRIGGERED audit row.
    get_job_status() reads the job row.
    The background worker polls for 'queued' rows, executes them, and transitions state.
    """

    def enqueue_pull(
        self,
        connection_ref_id: str,
        date_from: str,
        date_to: str,
        *,
        requested_by: str,
        datastream_id: str | None = None,
        execution_id: str | None = None,
    ) -> dict:
        """Mint job_id + pull_id, insert job row, write audit row. Returns job dict.

        AD-7: pull_id is minted here, before the DB insert.

        Story 8.2: optional datastream_id FK passthrough -- written to pull_jobs when
        provided by the scheduler dispatch (per-datastream mode) or the /run endpoint.
        The dedup index (uq_pull_jobs_active on connection_ref_id, date_from, date_to)
        is unchanged; datastream_id is NOT part of the dedup key.

        Story 63.1: optional execution_id -- the run this window belongs to
        (migration 218). It is NOT part of the dedup key either: a window already
        in flight stays owned by the run that enqueued it, and the newcomer's run
        simply owns one window fewer, which its declared total then shows.
        """
        from core.audit import ACTION_PULL_TRIGGERED, write_audit_row  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        job_id = _mint_job_id()
        pull_id = _mint_pull_id()

        # Story 5.1 (AC5): capture the current OTel trace_id (None when tracing is
        # disabled) so the worker -- running in a separate thread -- can continue the
        # same trace tree via migration 010's additive pull_jobs.trace_id column.
        trace_id = _capture_trace_id()

        # TODO(AI-17): replace with psycopg_pool.ConnectionPool at Epic 3 scale
        with get_connection() as conn:
            # Dedup fast-path: a quick SELECT before the INSERT avoids minting a
            # pull_id that will be thrown away in the common non-duplicate case.
            # review-3-2 F-2: idempotent enqueue -- an identical pending job
            # (same connection + DATASTREAM + window, not yet terminal) is
            # returned as-is instead of double-queueing (double-click => one job,
            # one spend).
            #
            # AI-302: the Datastream joined this key in migration 275. Without it,
            # nine Datastreams reading nine different `report_profile_id` of one
            # authorization collapsed into ONE pull on the first fleet dispatch --
            # eight profiles never pulled, and their clock advanced anyway because
            # a deduplicated answer is a real `running` job. Two DIFFERENT
            # Datastreams were never the same spend. COALESCE keeps the legacy
            # per-connection path (NULL datastream) deduplicating as before, where
            # a bare column would let NULLs miss each other.
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, pull_id, state FROM app.pull_jobs
                    WHERE connection_ref_id = %s
                      AND COALESCE(datastream_id, '') = COALESCE(%s, '')
                      AND date_from = %s AND date_to = %s
                      AND state IN ('queued', 'running')
                    LIMIT 1
                    """,
                    (connection_ref_id, datastream_id, date_from, date_to),
                )
                existing = cur.fetchone()
            if existing is not None:
                return {
                    "job_id": existing[0],
                    "pull_id": existing[1],
                    "state": existing[2],
                    "deduplicated": True,
                }

            # Dedup-safe INSERT: ON CONFLICT on the partial unique index
            # uq_pull_jobs_active (created by migration 022) ensures that a
            # concurrent enqueue_pull for the same (connection_ref_id, date_from,
            # date_to) with state IN ('queued','running') loses the race atomically.
            # Postgres infers the partial index from the column list + WHERE predicate.
            # RETURNING id: non-NULL -> we inserted; NULL -> conflict, fetch existing.
            #
            # Story 8.2: datastream_id column included when provided (nullable FK).
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.pull_jobs
                        (id, pull_id, connection_ref_id, date_from, date_to,
                         state, requested_by, trace_id, datastream_id, execution_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    -- AI-302 / migration 275: mirrors `uq_pull_jobs_active`
                    -- EXACTLY, expression included. A mismatch here does not
                    -- deduplicate -- it raises at INSERT.
                    ON CONFLICT (connection_ref_id, COALESCE(datastream_id, ''),
                                 date_from, date_to)
                        WHERE state IN ('queued', 'running')
                    DO NOTHING
                    RETURNING id
                    """,
                    (
                        job_id,
                        pull_id,
                        connection_ref_id,
                        date_from,
                        date_to,
                        QUEUED,
                        requested_by,
                        trace_id,
                        datastream_id,
                        execution_id,
                    ),
                )
                inserted_id = cur.fetchone()

            if inserted_id is None:
                # Another concurrent INSERT won the race; fetch its row.
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT id, pull_id, state FROM app.pull_jobs
                        WHERE connection_ref_id = %s
                          AND COALESCE(datastream_id, '') = COALESCE(%s, '')
                          AND date_from = %s AND date_to = %s
                          AND state IN ('queued', 'running')
                        LIMIT 1
                        """,
                        (connection_ref_id, datastream_id, date_from, date_to),
                    )
                    winner = cur.fetchone()
                conn.commit()
                if winner is not None:
                    return {
                        "job_id": winner[0],
                        "pull_id": winner[1],
                        "state": winner[2],
                        "deduplicated": True,
                    }
                # Extremely unlikely: the winning row already completed/failed by the
                # time we SELECTed -- fall through and let the caller retry.
                return {"job_id": job_id, "pull_id": pull_id, "state": QUEUED}

            conn.commit()

        # Write audit row (never raises per audit.py design)
        write_audit_row(
            identity=requested_by,
            action=ACTION_PULL_TRIGGERED,
            provider_account="",  # provider not yet resolved at enqueue time
            connection_ref=connection_ref_id,
            metadata={
                "job_id": job_id,
                "pull_id": pull_id,
                "date_from": date_from,
                "date_to": date_to,
            },
        )

        logger.info(
            "queue: enqueued job_id=%s pull_id=%s connection_ref_id=%s",
            job_id,
            pull_id,
            connection_ref_id,
        )

        return {"job_id": job_id, "pull_id": pull_id, "state": QUEUED}

    def get_job_status(self, job_id: str) -> dict | None:
        """SELECT the job row, return as dict or None."""
        from core.db import get_connection  # noqa: PLC0415

        # TODO(AI-17): replace with psycopg_pool.ConnectionPool at Epic 3 scale
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, pull_id, connection_ref_id, date_from, date_to,
                           state, requested_by, error_detail, attempt_count,
                           enqueued_at, started_at, completed_at
                    FROM app.pull_jobs
                    WHERE id = %s
                    """,
                    (job_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                cols = [desc[0] for desc in cur.description]
                record: dict = {}
                _TS_COLS = {"enqueued_at", "started_at", "completed_at"}
                for col, val in zip(cols, row):
                    if col in _TS_COLS and val is not None:
                        record[col] = val.isoformat()
                    elif col in ("date_from", "date_to") and val is not None:
                        record[col] = str(val)
                    else:
                        record[col] = val
        return record


# ---------------------------------------------------------------------------
# Worker internals (used by LocalBackend daemon thread)
# ---------------------------------------------------------------------------


def _dequeue_one(conn) -> dict | None:
    """Atomically CLAIM one queued job (review-3-2 F-1).

    The SELECT ... FOR UPDATE SKIP LOCKED and the transition to 'running'
    happen in a single UPDATE statement, committed before this returns: no
    window exists where the row lock is released while the job is still
    'queued', so a second worker (or a restart) can never double-claim it.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE app.pull_jobs
            SET state = 'running', started_at = now()
            WHERE id = (
                SELECT id FROM app.pull_jobs
                WHERE state = 'queued'
                ORDER BY enqueued_at
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            RETURNING id, pull_id, connection_ref_id, date_from, date_to,
                      requested_by, attempt_count, trace_id, datastream_id,
                      execution_id
            """,
        )
        row = cur.fetchone()
        if row is None:
            conn.commit()
            return None
        cols = [desc[0] for desc in cur.description]
        conn.commit()
        return dict(zip(cols, row))


def claim_job_by_id(conn, job_id: str) -> dict | None:
    """Atomically CLAIM the NAMED job, or return None (story 56.1, AD-36).

    ``_dequeue_one`` answers "give me something to do" -- the question a polling
    worker asks. A Cloud Tasks push asks the opposite one: "run THIS job". Using
    the polling claim to serve a push would execute an arbitrary queued job and
    report its outcome under another job's task, so the two claims cannot be the
    same function even though they share their discipline.

    That discipline is unchanged: the ``FOR UPDATE SKIP LOCKED`` sub-select and
    the transition to 'running' happen in one statement, committed before this
    returns, so a redelivered task (Cloud Tasks guarantees at-least-once) cannot
    double-claim a job already in flight.

    Returns None for every non-claimable case -- unknown id, already 'running',
    or terminal. The caller MUST distinguish them before answering the task:
    re-running a terminal job would re-pull data that already landed. See
    ``get_job_status`` for the state that tells them apart.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE app.pull_jobs
            SET state = 'running', started_at = now()
            WHERE id = (
                SELECT id FROM app.pull_jobs
                WHERE id = %s AND state = 'queued'
                FOR UPDATE SKIP LOCKED
            )
            RETURNING id, pull_id, connection_ref_id, date_from, date_to,
                      requested_by, attempt_count, trace_id, datastream_id,
                      execution_id
            """,
            (job_id,),
        )
        row = cur.fetchone()
        if row is None:
            conn.commit()
            return None
        cols = [desc[0] for desc in cur.description]
        conn.commit()
        return dict(zip(cols, row))


def recover_stale_running_jobs(max_running_seconds: int | None = None) -> int:
    """review-3-2 F-1b: re-queue jobs stuck in 'running' (crashed worker).

    A job running longer than QUEUE_RUNNING_VISIBILITY_SECONDS (default 5400)
    is presumed orphaned by a crash and reset to 'queued' with attempt_count
    incremented; jobs at max attempts dead-letter instead. Called by the worker
    loop at startup and periodically.

    Default raised from 1800 to 5400 (90 min) to avoid killing legitimate
    long pulls (G-13: long-pull visibility). Trade-off: a truly crashed job
    sits in 'running' for up to 90 min before recovery; this is acceptable
    because the job will be retried automatically once the visibility window
    expires.  Lower with QUEUE_RUNNING_VISIBILITY_SECONDS for tighter SLAs.

    Returns the number of jobs recovered.
    """
    if max_running_seconds is None:
        max_running_seconds = int(os.environ.get("QUEUE_RUNNING_VISIBILITY_SECONDS", "5400"))
    from core.db import get_connection  # noqa: PLC0415

    recovered = 0
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE app.pull_jobs
                    SET state = CASE
                            WHEN attempt_count + 1 >= %s THEN 'dead_letter'
                            ELSE 'queued'
                        END,
                        attempt_count = attempt_count + 1,
                        error_detail = 'recovered: worker crashed or visibility timeout'
                    WHERE state = 'running'
                      AND started_at < now() - make_interval(secs => %s)
                    RETURNING id, state, execution_id, pull_id
                    """,
                    (_max_attempts(), max_running_seconds),
                )
                recovered_rows = cur.fetchall()
                recovered = len(recovered_rows)
            conn.commit()

            # Story 63.1: the SEVENTH terminal exit, and the only bulk one.
            #
            # This statement dead-letters a window without ever entering
            # `_finish_job`, so the run it belonged to would stay `loading`
            # forever -- and `loading` holds `uq_datastream_executions_active`,
            # which answers 409 to every later publish AND to the next night's
            # dispatch. A crashed worker would have permanently frozen the
            # Datastream it crashed on. Re-queued rows are deliberately NOT
            # closed: their window is going to run again.
            for _job_id, _state, _execution_id, _pull_id in recovered_rows:
                if _state == DEAD_LETTER and _execution_id:
                    _record_window_outcome(
                        conn, _execution_id, _pull_id or "", "queue_recovery"
                    )
        if recovered:
            logger.warning('{"event": "queue_recovered_stale_jobs", "count": %d}', recovered)
    except Exception as exc:
        logger.warning("queue_recovery_failed: %s", exc)
    return recovered


def _resolve_connection_ref(conn, connection_ref_id: str) -> dict | None:
    """Fetch provider, nango_connection_id, project_id from connection_ref."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, nango_connection_id, provider, project_id
            FROM app.connection_ref
            WHERE id = %s
            """,
            (connection_ref_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "nango_connection_id": row[1],
            "provider": row[2],
            "project_id": row[3],
        }


def _resolve_datastream_profile(conn, datastream_id: str | None) -> str | None:
    """Fetch the report_profile_id for *datastream_id* (Story 10.1, Epic 10).

    Used for profile-aware pull dispatch: when a datastream targets a non-default
    report profile (e.g. GA4 'user_type_daily'), the worker resolves the matching
    per-profile pull function via get_module_pull_fn(provider, profile_id).

    Returns None only when datastream_id is None (legacy per-connection dispatch).
    For an explicit datastream, an absent profile or lookup failure returns the
    empty sentinel so strict dispatch cannot fall back to the default pull().
    """
    if not datastream_id:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT report_profile_id FROM app.datastreams WHERE id = %s",
                (datastream_id,),
            )
            row = cur.fetchone()
        return row[0] if row and row[0] else ""
    except Exception as exc:  # noqa: BLE001
        logger.warning("queue: resolve_datastream_profile_failed ds=%s: %s", datastream_id, exc)
        return ""


def _resolve_datastream_module(conn, datastream_id: str | None) -> str | None:
    """Fetch the module a datastream reads, for pull dispatch.

    Needed because the credential does not always name the module. One Google
    consent screen opens seven tools and is stored as provider='google', so the
    only thing that knows whether this job reads Search Console or Analytics is
    the datastream itself.

    Returns None when there is no datastream (legacy per-connection dispatch) or
    the lookup fails, so the caller falls back to the provider exactly as before.
    """
    if not datastream_id:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT module_name FROM app.datastreams WHERE id = %s",
                (datastream_id,),
            )
            row = cur.fetchone()
        return row[0] if row and row[0] else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("queue: resolve_datastream_module_failed ds=%s: %s", datastream_id, exc)
        return None


def _build_error_detail(
    *,
    error_class: str,
    user_action: str | None,
    provider_status: int | None,
    provider_payload,
    message: str,
    attempt_count: int,
) -> str:
    """Serialize a structured error_detail JSON string (Story 25.2, AC4).

    Shape: {"error_class","user_action","provider_status","provider_payload",
            "message","attempt_count"}. The whole JSON is truncated to fit the
    2000-char discipline of the error_detail TEXT column; when the full payload
    would overflow, it is dropped to a marker so the surrounding keys survive
    (error_class / user_action are the load-bearing fields for the ledger).

    The value stays valid JSON so readers can json.loads() it; legacy readers
    that treat error_detail as plain text still render the string as-is.
    """

    def _dump(message_value: str, payload_value) -> str:
        detail = {
            "error_class": error_class,
            "user_action": user_action,
            "provider_status": provider_status,
            "provider_payload": payload_value,
            "message": message_value,
            "attempt_count": attempt_count,
        }
        return json.dumps(detail, ensure_ascii=True)

    encoded = _dump(message, provider_payload)
    if len(encoded) <= 2000:
        return encoded

    # Too big: shrink the message, then drop the payload, keeping it valid JSON.
    encoded = _dump(str(message)[:500], provider_payload)
    if len(encoded) <= 2000:
        return encoded
    encoded = _dump(str(message)[:500], None if provider_payload is None else "<truncated>")
    if len(encoded) <= 2000:
        return encoded
    # Last resort: a minimal valid-JSON envelope with the taxonomy class only.
    return json.dumps(
        {
            "error_class": error_class,
            "user_action": user_action,
            "provider_status": provider_status,
            "provider_payload": "<truncated>",
            "message": "",
            "attempt_count": attempt_count,
        },
        ensure_ascii=True,
    )[:2000]


def _capability_report_for_profile(manifest: dict, profile_id: str | None) -> dict:
    """Return the source_capabilities.reports entry for *profile_id* (or {}).

    When *profile_id* is None (legacy per-connection dispatch), the first
    capability report is returned so its selection_mode is still inspectable.
    Source-agnostic: reads only the generic capability contract (HG-5).
    """
    descriptor = manifest.get("source_capabilities")
    if not isinstance(descriptor, dict):
        return {}
    reports = descriptor.get("reports") or []
    if profile_id is None:
        return reports[0] if reports else {}
    return next((r for r in reports if r.get("id") == profile_id), {})


def _resolve_plan_selection(conn, datastream_id: str | None) -> dict | None:
    """The field selection THIS Datastream's live plan composed, or None.

    The selector had no consumer. `datastream_source_catalogue` exposes a
    catalog_driven report's declared fields, the Processing step lets an operator
    choose among them, `datastream_intents.normalize_intent` normalizes the
    choice and `save_datastream_intent` writes it -- immutably, versioned -- into
    `app.datastream_plan_versions.normalized_payload` at `$.source.selection`.
    Then the pull path read `job.get("selection")` from a job row that HAS no
    such column (`app.pull_jobs` carries no JSONB at all), so the answer was
    always None and every catalog_driven pull fell back to the catalog's
    tier-core default. The operator's choice was stored, versioned, displayed --
    and had no effect on what was extracted.

    Read from the PLAN rather than from the job row, for the same reason
    `_resolve_datastream_profile` and `_resolve_selected_account` do: the plan is
    where the answer is authoritative, and a job is only a request to run one.
    A plan edited between enqueue and execution therefore takes effect on the
    next run -- the behaviour those two siblings already have.

    Returns None when there is nothing to say (no datastream_id, no current plan
    version, no `source.selection` in it, or the read fails). None is not a
    fallback to "no fields": `_resolve_catalog_selection` hands None to
    `catalog_contract.validate_selection`, which answers with the catalog's
    tier-core default -- the behaviour of every job before this wiring.
    """
    if not datastream_id:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT plan.normalized_payload "
                "FROM app.datastreams d "
                "JOIN app.datastream_plan_versions plan "
                "  ON plan.id = d.current_plan_version_id "
                " AND plan.datastream_id = d.id "
                "WHERE d.id = %s",
                (datastream_id,),
            )
            row = cur.fetchone()
        if not row or not row[0]:
            return None
        payload = row[0]
        if isinstance(payload, (str, bytes)):
            import json as _json  # noqa: PLC0415

            payload = _json.loads(payload)
        if not isinstance(payload, dict):
            return None
        source = payload.get("source")
        if not isinstance(source, dict):
            return None
        selection = source.get("selection")
        if not isinstance(selection, dict):
            return None
        # `validate_selection` consumes `metrics` and `dimensions` only; the plan
        # also carries `selection_mode`, `grain` and `filters`, which belong to
        # other consumers. Narrowed HERE so an unknown extra key can never be
        # read as a field id, and so an empty choice on both axes reads as "the
        # operator chose nothing", i.e. None -> tier-core default, rather than as
        # an empty selection nobody asked for.
        metrics = list(selection.get("metrics") or [])
        dimensions = list(selection.get("dimensions") or [])
        if not metrics and not dimensions:
            return None
        return {"metrics": metrics, "dimensions": dimensions}
    except Exception as exc:  # noqa: BLE001 -- never fails a pull on the read
        logger.warning("queue: resolve_plan_selection_failed ds=%s: %s", datastream_id, exc)
        return None


def _resolve_catalog_selection(
    module_name: str,
    capability_report: dict,
    job: dict,
    conn=None,
) -> dict | None:
    """Resolve a validated selection for a ``catalog_driven`` profile (Story 25.8).

    Returns:
      - ``None`` when the profile is NOT catalog_driven (unchanged dispatch —
        exact_bundle profiles are bit-identical: the pull is called with no
        ``selection=`` kwarg).
      - a resolved selection dict ``{"metrics","dimensions","source_fields"}``
        when the profile IS catalog_driven. The selection comes from the job
        payload key ``"selection"`` when present, else from the Datastream's live
        PLAN (``_resolve_plan_selection``, which needs *conn*); neither -> the
        catalog's tier-core default (Dev Notes: a job without selection uses
        tier-core defaults from the catalog).

    Raises ``core.pull_errors.InvalidRequestError`` when the selection references
    a field id absent from the catalog (the drift signal — never silent). This
    lands in the invalid_request branch of _execute_job (non-retryable, logged
    as pull_invalid_request_drift), exactly like a provider 400.

    AD-2: core reads the catalog via catalog_contract.load_catalog and never
    hardcodes provider vocabulary; the module name is only a directory key.
    """
    if capability_report.get("selection_mode") != "catalog_driven":
        return None

    from pathlib import Path as _Path  # noqa: PLC0415

    from core.catalog_contract import load_catalog, validate_selection  # noqa: PLC0415
    from core.pull_errors import InvalidRequestError  # noqa: PLC0415

    module_dir = _Path(__file__).parent.parent / "modules" / module_name
    catalog = load_catalog(module_dir)
    if catalog is None:
        raise InvalidRequestError(
            provider_status=None,
            provider_payload=None,
            message=(
                f"catalog_driven profile requires api_catalog.json for module "
                f"{module_name!r} but none was found"
            ),
        )

    # The plan's selection first -- that is where the Processing selector's answer
    # lives (`app.datastream_plan_versions.normalized_payload.$.source.selection`).
    # `job["selection"]` is kept ahead of it so a caller that hands a selection
    # directly (tests, a future enqueue-time override) still wins; `app.pull_jobs`
    # has no such column, so on the live path it is always absent and the plan
    # answers. Neither present -> None -> the catalog's tier-core default.
    selection = job.get("selection")
    if selection is None and conn is not None:
        selection = _resolve_plan_selection(conn, job.get("datastream_id"))
    resolved, issues = validate_selection(catalog, selection)
    if issues:
        raise InvalidRequestError(
            provider_status=None,
            provider_payload=None,
            message="; ".join(issue.message for issue in issues),
        )
    return resolved


#: The refusal code for a consent that verified SEVERAL accounts of the Connector
#: this Datastream reads, while the Datastream itself names none of them.
#:
#: It is not `account_not_selected` -- that one means "nothing was ever chosen
#: here", and its gesture is to go and choose. This one means "several were
#: chosen, and none of them is THIS stream's", whose gesture is to bind the
#: account on the Datastream. Answering it with a pick would be a guess, and the
#: guess is exactly the 2026-08-01 measurement: an Analytics pull receiving
#: `property_id='sc-domain:...'` because it took the consent's freshest account.
REFUSAL_ACCOUNT_SELECTION_AMBIGUOUS = "account_selection_ambiguous"


#: The refusal for an account question that could not be ASKED -- the read of the
#: verified accounts did not come back.
#:
#: It is a third state, and it was silence until 2026-08-31: the resolver caught
#: every exception, logged `resolve_selected_account_failed` at WARNING and
#: returned `None`, which the enqueue gate then rendered as `account_not_selected`
#: -- "nobody ever chose here", whose gesture is to go and choose. A person who
#: HAD chosen was sent to redo the one thing already done, and a job that reached
#: execution ran with no account at all. A read that failed is not an answer of
#: "none"; it is the absence of an answer, and it says so here.
REFUSAL_ACCOUNT_SELECTION_UNREADABLE = "account_selection_unreadable"


class AccountSelectionRefused(Exception):
    """The account this pull reads cannot be known, and the refusal says why.

    One base for the two causes so the callers branch ONCE: the enqueue gate and
    the executor both turn any of them into the same refusal shape, and a third
    cause added here reaches both without either being edited.
    """

    #: The code the refusal carries. Set by each cause.
    code: str = ""
    #: Whether running the same job again could answer differently. An ambiguity
    #: is repaired by a human gesture, so a retry only repeats it; an unreadable
    #: database is exactly what a retry is for.
    retryable: bool = False

    def refusal_message(self) -> str:  # pragma: no cover - both causes override it
        raise NotImplementedError


class AccountSelectionAmbiguous(AccountSelectionRefused):
    """Raised when the account this pull reads cannot be known without guessing.

    Carries the candidates so the refusal can say HOW MANY, which is the whole
    difference between an actionable sentence and "something is wrong".
    """

    code = REFUSAL_ACCOUNT_SELECTION_AMBIGUOUS
    retryable = False

    def __init__(self, connection_ref_id: str, connector: str | None, accounts: list[str]):
        self.connection_ref_id = connection_ref_id
        self.connector = connector
        self.accounts = list(accounts)
        super().__init__(
            f"{len(self.accounts)} verified accounts for connector "
            f"{connector or 'unknown'} on connection {connection_ref_id}"
        )

    def refusal_message(self) -> str:
        return (
            f"this connection has {len(self.accounts)} verified accounts for this "
            "connector and this Datastream names none of them; open the "
            "Datastream and choose the account it reads"
        )


class AccountSelectionUnreadable(AccountSelectionRefused):
    """Raised when the accounts of this connection could not be READ at all.

    Carries the cause for the log and the audit row -- never for the message: an
    operator cannot act on a `psycopg` string, and the gesture is the same one
    whatever broke underneath.
    """

    code = REFUSAL_ACCOUNT_SELECTION_UNREADABLE
    retryable = True

    def __init__(self, connection_ref_id: str, connector: str | None, cause: BaseException):
        self.connection_ref_id = connection_ref_id
        self.connector = connector
        self.cause = cause
        super().__init__(
            f"accounts of connection {connection_ref_id} unreadable for connector "
            f"{connector or 'unknown'}: {cause.__class__.__name__}: {cause}"
        )

    def refusal_message(self) -> str:
        return (
            "the accounts this connection has verified could not be read, so the "
            "account this Datastream reads is not known; nothing was pulled -- "
            "run it again"
        )


def _account_selection_refusal(exc: AccountSelectionRefused) -> dict:
    """The refusal an OPERATOR can act on, in the shape every caller branches on.

    Names the gesture, not the cause: the account is bound on the Datastream, at
    the account step of its setup. No table, no column, no connector-internal id.
    """
    return {"state": "refused", "code": exc.code, "message": exc.refusal_message()}


def _resolve_selected_account(
    conn,
    connection_ref_id: str,
    datastream_id: str | None = None,
    connector: str | None = None,
) -> str | None:
    """The account THIS Datastream reads.

    The wizard makes the operator choose an account and VERIFIES read access to
    it before anything is scheduled -- and then the pull ignored it: connectors
    read their target from a deployment-wide env var, so one property was pulled
    for every Datastream of the deployment, and none at all when the var was
    unset. The selection is the answer; it has to reach the extraction.

    IT IS THE DATASTREAM'S ANSWER, NOT THE AUTHORIZATION'S. This function used
    to read `connection_account_scope` alone, and that table held ONE row per
    credential (migration 046). So every Datastream reached through one Google
    consent pulled the same property -- ten GA4 properties collapsed to one, and
    selecting a Search Console site erased the Google Ads account chosen the day
    before. Migration 211 moved the binding onto `app.datastreams`, which is
    where `glossary.md` puts it: *"a Datastream selects one exposed scope"*.

    The credential-wide scope stays as the fallback, for the two cases where the
    Datastream genuinely has nothing to say: a job carrying no datastream_id (the
    legacy per-connection path), and a Datastream created before 211 whose
    authorization has a single verified account -- which is what it was pulling
    anyway.

    2026-08-30 -- AND THE FALLBACK STOPPED GUESSING. `ORDER BY verified_at DESC
    LIMIT 1` over the whole consent is one answer to a question that has N: one
    Google OAuth screen covers `gsc`, `google-analytics`, `google-ads` and four
    more, each with its own account space, and migration 211 left
    `app.datastreams.source_account_id` nullable, so the branch is reachable in
    production. The fallback is now narrowed to the accounts of *connector*
    (`app.credential_accounts.discovered_for_connector`, migration 210):

      * exactly one ready account of that connector -> that account. Not a
        guess: it is the only thing this connector could read here.
      * more than one -> `AccountSelectionAmbiguous`. The caller turns it into a
        REFUSAL that names the gesture; nothing is pulled under a coin flip.
      * none -> None, unchanged, which the enqueue gate already answers with
        `account_not_selected` -- the same gesture, choose an account.

    2026-08-31 -- AND THE READ THAT FAILS STOPPED ANSWERING "NONE". The `except`
    below returned `None` on any exception, which the enqueue gate renders as
    `account_not_selected`: an infrastructure failure was told to the operator as
    "you never chose an account", and the same `None` let the executor run a pull
    with no account. Both causes now leave through a refusal that says which one
    it is.

    Raises:
        AccountSelectionAmbiguous -- several ready accounts of this connector and
            no binding on the Datastream. Raised OUTSIDE the `try` below, so no
            clause can dress a refusal up as "no account".
        AccountSelectionUnreadable -- the accounts could not be read at all. It
            names what could not be read and carries the cause for the log; it is
            `retryable`, which the ambiguity is not.
    """
    from core.account_topology import ready_accounts_for_connector  # noqa: PLC0415

    try:
        with conn.cursor() as cur:
            if datastream_id:
                cur.execute(
                    "SELECT ca.external_account_id "
                    "FROM app.datastreams d "
                    "JOIN app.credential_accounts ca "
                    "  ON ca.source_account_id = d.source_account_id "
                    "WHERE d.id = %s",
                    (datastream_id,),
                )
                row = cur.fetchone()
                if row and row[0]:
                    return row[0]
        # ONE OWNER FOR THE READINESS PREDICATE (2026-08-31). This branch used to
        # re-spell `account_topology.ready_accounts_for_connector`'s SQL on the
        # caller's cursor, under a docstring saying it "cannot call it: that one
        # owns its connection" -- which stopped being true the day that function
        # took a `conn`. Two copies of one predicate are two answers waiting to
        # differ, and the copy that differs is the one the pull uses.
        candidates = ready_accounts_for_connector(connection_ref_id, connector, conn=conn)
    except AssertionError:
        # A broken invariant of THIS process, never an unreadable database.
        # `tests/support/statement_router.UnknownStatement` is one, and it exists
        # precisely so a fixture cannot answer a question it was never asked; a
        # clause that renamed it "the accounts could not be read" would put this
        # function back to measuring a path nobody walked.
        raise
    except Exception as exc:  # noqa: BLE001 -- named in a refusal, never swallowed
        logger.warning(
            "queue: %s conn=%s connector=%s: %s",
            REFUSAL_ACCOUNT_SELECTION_UNREADABLE,
            connection_ref_id,
            connector or "unknown",
            exc,
        )
        raise AccountSelectionUnreadable(connection_ref_id, connector, exc) from exc
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        # Raised OUTSIDE the try above, so the best-effort clause cannot turn a
        # refusal into a silent None.
        raise AccountSelectionAmbiguous(connection_ref_id, connector, candidates)
    return None


#: Les quatre noms que ce pont connaissait AVANT que la declaration ne soit lue.
#: Conserves en repli pour la fenetre de deploiement, et pour eux seuls : un
#: connecteur qui n'utilise aucun de ces noms ET ne declare pas le sien etait
#: silencieusement prive du compte choisi par l'operateur.
_LEGACY_ACCOUNT_PARAM_NAMES = ("account_id", "site_url", "property_id", "account")


def _operating_context_kwargs(
    topology: dict, params, account_id: str, connection_ref_id: str | None
) -> dict:
    """The ANCESTORS the provider needs alongside the account, under their declared names.

    A selection is a path, not a leaf, and four connectors say so in their own
    hierarchies: a Google Ads child customer is queried with `login-customer-id`
    = its MANAGER, a DV360 advertiser under its PARTNER, a CM360 advertiser
    through a USER_PROFILE. The leaf alone is refused by those APIs, so a child
    account of an MCC -- an agency's ordinary case -- could be chosen and never
    queried.

    The mapping lives in the manifest (`account_topology.operating_context.
    parameters`: pull parameter -> level id) and the values come from the
    `selection_path` stored when the account was verified (migration 254). Core
    reads no level name (AD-2) and passes only what `pull()` declares.

    Empty when the module declares no context, when the path was not recorded,
    or when the level is absent from it -- silently doing nothing is right here:
    a connector with no hierarchy has no ancestor to pass.
    """
    mapping = ((topology.get("operating_context") or {}).get("parameters")) or {}
    if not isinstance(mapping, dict) or not mapping or not connection_ref_id:
        return {}
    levels = [
        level.get("id") for level in (topology.get("levels") or []) if isinstance(level, dict)
    ]
    try:
        from core.account_topology import get_scope  # noqa: PLC0415

        scope = get_scope(connection_ref_id, account_id) or {}
    except Exception:  # noqa: BLE001 -- a missing context never fails the pull itself
        return {}
    path = scope.get("selection_path") or []
    if isinstance(path, str):
        path = json.loads(path)
    if not isinstance(path, list) or not path:
        return {}

    # The path is root-first and mirrors `levels`, so a level's rank in the
    # declaration is its rank in the path.
    by_level = {
        levels[index]: str(node.get("id") or "")
        for index, node in enumerate(path)
        if index < len(levels) and isinstance(node, dict)
    }
    extra: dict = {}
    for parameter, level_id in mapping.items():
        value = by_level.get(level_id)
        if value and parameter in params:
            extra[parameter] = value
    return extra


def _account_kwargs(
    pull_fn, account_id: str | None, module_name: str | None = None,
    connection_ref_id: str | None = None,
) -> dict:
    """Pass the selected account under the name the CONNECTOR DECLARES.

    Aucun nom de provider n'apparait ici (HG-6) : la source est
    ``account_topology.pull_parameter`` du manifeste, comme le catalogue de
    champs est la source des champs.

    POURQUOI CE N'EST PLUS UNE LISTE DE QUATRE NOMS. Mesure du 2026-07-31 : 29
    connecteurs declarent qu'un humain doit choisir un compte, et 24 d'entre eux
    nommaient ce parametre autrement (``ad_account_id``, ``advertiser_id``,
    ``customer_id``, ``channel_id``, ``network_code``...). Le choix de
    l'operateur -- decouvert par ``discover_accounts``, verifie, stocke, relu par
    ``_resolve_selected_account`` -- mourait ICI, au dernier metre, sans un mot.
    Cinq connecteurs se rabattaient alors sur un ``*_ACCOUNT_ID`` d'environnement
    que ``core/account_topology.py`` declare pourtant DEPRECIE : en production la
    variable n'existe pas, donc ils levaient ; les autres tiraient sur le compte
    par defaut du jeton.

    Le silence etait le vrai defaut : un compte non transmis se voyait a
    l'arrivee des donnees, jamais dans un log. Il est desormais bruyant.
    """
    if not account_id:
        return {}
    import inspect  # noqa: PLC0415

    try:
        params = inspect.signature(pull_fn).parameters
    except (TypeError, ValueError):
        return {}

    manifest = _get_manifest_for_module(module_name) or {} if module_name else {}
    topology = manifest.get("account_topology") or {}
    declared = topology.get("pull_parameter")
    if declared:
        if declared in params:
            return {
                declared: account_id,
                **_operating_context_kwargs(topology, params, account_id, connection_ref_id),
            }
        logger.warning(
            "queue: account_param_declared_but_absent: module=%s declares "
            "account_topology.pull_parameter=%r and pull() has no such parameter -- "
            "the operator's selected account cannot be passed",
            module_name,
            declared,
        )
        return {}

    for name in _LEGACY_ACCOUNT_PARAM_NAMES:
        if name in params:
            return {name: account_id}

    if topology.get("selection_level"):
        logger.warning(
            "queue: selected_account_unreachable: module=%s declares "
            "selection_level=%r, so an operator chose an account -- and pull() "
            "declares no parameter to receive it and the manifest declares no "
            "account_topology.pull_parameter. The selection is being DROPPED",
            module_name,
            topology.get("selection_level"),
        )
    return {}


def _tracked_entity_kwargs(
    conn, pull_fn, project_id: str | None, datastream_id: str | None
) -> dict:
    """Pass the governed identities a PUBLISHED binding authorizes (Story 48.5).

    Same shape as ``_account_kwargs`` and for the same reason: the parameter name
    comes from the Connector's own declaration and is checked against the
    callable's signature, so no provider vocabulary appears here.

    Two silences are deliberate. A *candidate* binding contributes nothing --
    approving a registry is not changing a pipeline -- and any failure to read
    the bindings passes no values at all rather than falling back to some
    previous set: a pull that collects nothing is honest, a pull that collects
    yesterday's identities is not.
    """

    if not project_id or not datastream_id:
        return {}
    import inspect  # noqa: PLC0415

    try:
        params = inspect.signature(pull_fn).parameters
    except (TypeError, ValueError):
        return {}
    try:
        from core.entity_bindings import resolve_query_drivers  # noqa: PLC0415

        drivers = resolve_query_drivers(
            conn, project_id=project_id, datastream_id=datastream_id
        )
    except Exception as exc:  # noqa: BLE001 -- an unreadable binding store collects nothing
        logger.warning(
            "queue: tracked_entity_drivers_unavailable ds=%s: %s",
            datastream_id,
            type(exc).__name__,
        )
        return {}
    return {name: values for name, values in drivers.items() if name in params and values}


def _record_window_outcome(conn, execution_id, pull_id: str, actor: str) -> None:
    """Story 63.1: write where the run is, ONCE, at a window boundary.

    A window is one provider round trip -- ``google-ads`` builds a single GAQL
    over the range, paginates internally and inserts once -- so a nightly run
    writes once and a two-year catch-up writes 24 times. Never once per page and
    never once per landed row: the measure would cost more than the work it
    measures, on 39 connectors.

    Best-effort, like `record_boundary_evidence` and `record_observations_for_pull`
    beside it: the pull is done and recorded, and instrumentation must never undo
    it. A job carrying no ``execution_id`` (legacy per-connection dispatch) writes
    nothing rather than inventing a run.

    Not called directly by the worker: `_finish_job` is. See its docstring.
    """
    if not execution_id:
        return
    try:
        from core.execution_progress import record_and_close  # noqa: PLC0415

        record_and_close(conn, execution_id=str(execution_id), actor=actor)
        conn.commit()
    except Exception as exc:  # noqa: BLE001 -- must never undo the pull
        conn.rollback()
        logger.warning(
            "queue: execution_progress_failed pull_id=%s execution=%s: %s",
            pull_id,
            execution_id,
            exc,
        )


def _finish_job(
    conn,
    job: dict,
    state: str,
    *,
    error_detail: str | None = None,
    row_count: int | None = None,
    actor: str = "worker",
) -> None:
    """THE ONLY WAY A PULL JOB REACHES A TERMINAL STATE. Story 63.1.

    WHY IT IS A FUNCTION AND NOT A CONVENTION. `_execute_job` has SIX terminal
    exits -- success, connection_ref deleted, no pull function registered,
    rate-limit exhausted, typed provider error, unclassified error -- and the
    first version of this story wired the run closure into three of them. The
    other three left the execution in `loading`, which is in the
    `uq_datastream_executions_active` predicate (migration 042), so ONE
    provider outage would have made every later publish AND every following
    night's dispatch answer 409 for that Datastream, forever. That is the exact
    catastrophe `collected` exists to prevent, arriving through the failure door
    instead of the happy one.

    A convention ("remember to call the recorder") cannot hold six sites. A
    function can, and `tests/core/test_queue_records_execution_progress.py`
    refuses any new `UPDATE app.pull_jobs SET state = <terminal>` written
    anywhere else.

    Writes the job row, commits, THEN moves the run -- in that order, so a reader
    never sees a run declared over while its last window is still `running`. The
    run write is best-effort and never undoes the job write.
    """
    assignments = ["state = %s", "completed_at = now()"]
    params: list[object] = [state]
    if error_detail is not None:
        assignments.append("error_detail = %s")
        params.append(error_detail)
    if row_count is not None:
        # Story 63.1: what THIS window landed, recorded at the same boundary the
        # window is closed. It is here and not in `core.raw_landing.land_raw_rows`
        # on purpose: that is the write primitive the 39 connectors share, it
        # knows no window and no run, and a counter moving there with no measure
        # behind it would be false liveness.
        assignments.append("row_count = %s")
        params.append(row_count)
    params.append(job["id"])

    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE app.pull_jobs SET {', '.join(assignments)} WHERE id = %s",
            tuple(params),
        )
    conn.commit()

    _record_window_outcome(conn, job.get("execution_id"), job.get("pull_id") or "", actor)


def _record_prevented(
    conn,
    job: dict,
    prevented,
    *,
    module_name: str | None,
    provider: str | None,
    connection_ref_id: str | None,
    requested_by: str,
) -> None:
    """Close a window the SOURCE did not allow to run -- the one transport.

    TWO DOORS REACH THIS, AND THEY MUST WRITE THE SAME ROW. A connector that may
    abandon a profile RETURNS a prevented envelope (`prevented_by`); a connector
    whose profile must land rows RAISES a marked error (`prevented_by_error`).
    They are the same product fact -- an access has not been provisioned -- and a
    person reads them on the same day of the same grid. Written twice, they would
    be one edit away from a screen where a refused `reviews` day names its grant
    and a refused `location_daily` day does not.

    `row_count=None`, NEVER 0. A zero is a count that was taken; this window took
    none, so the column keeps its NULL and no total can absorb a number nobody
    measured.

    NO `error_class`, on purpose, on both doors. `datastream_diagnosis` reads
    `prevented_reason` / `prevented_message` off a `prevented` row and nothing
    else: this is not an error. Nothing is misconfigured, and there is nothing to
    retry until a person obtains the grant.
    """
    from core.audit import write_audit_row  # noqa: PLC0415

    _finish_job(
        conn,
        job,
        PREVENTED,
        row_count=None,
        error_detail=json.dumps(
            {
                "prevented_reason": prevented.reason,
                "prevented_message": prevented.message,
            }
        )[:1900],
        actor=requested_by,
    )
    write_audit_row(
        identity=requested_by,
        action=ACTION_PULL_PREVENTED,
        provider_account=provider,
        connection_ref=connection_ref_id,
        metadata={
            "job_id": job["id"],
            "pull_id": job["pull_id"],
            "prevented_reason": prevented.reason,
            "date_from": str(job["date_from"]),
            "date_to": str(job["date_to"]),
        },
    )
    # WARNING, not INFO: nothing is broken, and something a person has to go and
    # obtain is missing. The sentence logged is the connector's own, so the log
    # says what the screen says.
    logger.warning(
        "queue: job_id=%s prevented pull_id=%s module=%s reason=%s -- %s",
        job["id"],
        job["pull_id"],
        module_name,
        prevented.reason,
        prevented.message,
    )


def cancel_queued_jobs(conn, *, execution_id: str, reason: str) -> list[dict]:
    """Refuse every window of *execution_id* that has NOT started -- story 63.6.

    WHAT "STOP" CAN HONESTLY REACH, MEASURED. `_execute_job` calls the pull
    synchronously and consults nothing between two pages; the only mechanism that
    takes a `running` job back is `recover_stale_running_jobs`, after
    `QUEUE_RUNNING_VISIBILITY_SECONDS` -- default 5400. A provider call already in
    flight cannot be interrupted, so this refuses what has not begun and says so.
    The predicate names `queued` and nothing else.

    AND `queued` IS ENOUGH FOR BOTH BACKENDS. The local poller claims
    `state = 'queued'` only (`_dequeue_one`); a Cloud Tasks push has already been
    handed to the queue, but it claims through `claim_job_by_id`, which claims
    `'queued'` only too -- so the task finds nothing to run and
    `admin_api` answers `already_terminal` in 200, dropping it without a retry.

    THIS IS A TERMINAL WRITER, AND IT IS ON THE ALLOW-LIST FOR A REASON. Every
    function here that puts a pull job into a state it can never leave must close
    the run it belongs to (`test_queue_records_execution_progress.py`). This one
    does not close it afterwards: its ONE caller,
    `execution_progress.stop_collection_run`, moves the run to its terminal state
    FIRST, in the same transaction, and that order is what stops a window landing
    in between from re-deriving an open run.

    The caller owns the transaction; this does not commit. Returns the windows it
    refused, oldest first, so the caller can name the scope of what it did.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE app.pull_jobs
            SET state = '{CANCELLED}',
                completed_at = now(),
                error_detail = %s
            WHERE execution_id = %s AND state = '{QUEUED}'
            RETURNING id, date_from, date_to
            """,
            (reason, execution_id),
        )
        columns = [desc[0] for desc in cur.description]
        rows = [dict(zip(columns, row)) for row in cur.fetchall()]
    rows.sort(key=lambda row: (str(row.get("date_from")), str(row.get("id"))))
    return rows


def _execute_job(job: dict) -> bool:
    """Execute one pull job: resolve connection, call pull fn, update state.

    This function runs inside the worker thread. The DB connection is managed
    here so that the row lock (SKIP LOCKED) is held for the duration.

    Story 3.3: quota pre-check runs BEFORE marking the job as running.
    If blocked, job stays 'queued' (attempt_count unchanged) and returns False.
    If a RateLimitError is raised by the pull fn, the breaker is tripped, and
    the job is re-queued (attempt_count IS incremented -- it was an actual attempt).

    Returns:
        True  -- job was executed (done, failed, or dead_letter)
        False -- job was quota-blocked and re-queued (attempt_count not incremented)
    """
    from core import quota  # noqa: PLC0415
    from core.audit import write_audit_row  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415
    from core.main import get_module_pull_fn  # noqa: PLC0415

    # `prevented_by_error` is BOUND BEFORE THE `try`, and that is load-bearing:
    # the exception handlers below call it, and a pull that failed BEFORE an
    # import placed inside the try -- resolving the manifest, resolving the
    # catalog selection -- would reach a handler where the name does not exist
    # and turn a provider error into a NameError.
    from core.pull_envelope import prevented_by, prevented_by_error  # noqa: PLC0415
    from core.pull_errors import ConnectorError  # noqa: PLC0415
    from core.quota import RateLimitError  # noqa: PLC0415

    job_id = job["id"]
    pull_id = job["pull_id"]
    connection_ref_id = job["connection_ref_id"]
    date_from = str(job["date_from"])
    date_to = str(job["date_to"])
    requested_by = job["requested_by"]
    attempt_count = job["attempt_count"] + 1

    max_att = _max_attempts()

    # ---------------------------------------------------------------------- #
    # Story 3.3 -- Quota pre-check (AC3)                                      #
    # Resolve provider first (needed for pre_check) via a quick DB look-up.   #
    # Pre-check happens BEFORE marking job as 'running' so attempt_count       #
    # is not incremented when blocked.                                         #
    # ---------------------------------------------------------------------- #
    # TODO(AI-17): replace with psycopg_pool.ConnectionPool when pull-volume grows at Epic 3 scale
    with get_connection() as _pre_conn:
        pre_ref = _resolve_connection_ref(_pre_conn, connection_ref_id)
        # AI-95: resolved ONCE here, for every module-keyed lookup below. The
        # quota bucket is registered under the module's manifest name
        # (core/loader.py:542), so asking it about a credential name returns
        # "no_quota" -- a whole connector silently exempt from its own budget.
        _datastream_module = _resolve_datastream_module(_pre_conn, job.get("datastream_id"))

    provider = pre_ref["provider"] if pre_ref else None
    # WHICH tool this job reads. The credential names the AUTHORIZATION, and for
    # every Nango connection that is also the module -- but one Google consent
    # screen opens seven tools and is stored as provider='google', which is the
    # name of no module at all. Only the datastream knows. Falls back to the
    # provider for the legacy per-connection path, where nothing knows better.
    module_name = _datastream_module or provider

    if provider is not None:
        read_cost = quota.get_read_cost(module_name)
        can_proceed, reason = quota.pre_check(module_name, read_cost)
        if not can_proceed:
            # Re-queue without incrementing attempt_count (AC3, AC4).
            # review-3-2 F-1: the atomic claim already moved the job to
            # 'running', so a quota block must explicitly RETURN it to the
            # queue (previously it had never left 'queued').
            logger.info(
                "quota_blocked: job=%s platform=%s credential=%s reason=%s",
                job_id,
                module_name,
                provider,
                reason,
            )
            # AI-24 (Story 4.1 AC12): set error_detail to reflect quota block type
            # so GET /api/jobs/{id} can expose quota_state to callers.
            _quota_detail = (
                "quota_blocked: circuit_open"
                if reason == "breaker_open"
                else "quota_blocked: budget_exhausted"
            )
            with get_connection() as _rq_conn:
                with _rq_conn.cursor() as cur:
                    cur.execute(
                        "UPDATE app.pull_jobs"
                        " SET state = 'queued', error_detail = %s WHERE id = %s",
                        (_quota_detail, job_id),
                    )
                _rq_conn.commit()
            return False

    # Story 5.1 (AC5): emit a worker span joined to the originating trace (via the
    # stored trace_id from migration 010). Carries job.id / pull.pull_id /
    # job.connector / job.latency_ms. No-op & never raises when tracing is disabled.
    import time as _time  # noqa: PLC0415

    from core import tracing  # noqa: PLC0415

    _job_t0 = _time.perf_counter()
    _trace_id = job.get("trace_id")
    _parent_meta = None
    if _trace_id:
        _tp = tracing.traceparent_from_trace_id(_trace_id)
        if _tp:
            _parent_meta = {"traceparent": _tp}

    with tracing.worker_span(
        "queue.pull",
        {
            "job.id": job_id,
            "pull.pull_id": pull_id,
            # AI-95: the span attribute is named 'connector', so it carries the
            # connector -- not the credential that opened seven of them.
            "job.connector": module_name or "",
            "job.credential": provider or "",
        },
        parent_meta=_parent_meta,
    ) as _job_span:
        # TODO(AI-17): replace with psycopg_pool.ConnectionPool at Epic 3 scale
        with get_connection() as conn:
            # review-3-2 F-1: the atomic claim already set state='running' and
            # started_at; only the attempt counter needs bumping here.
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE app.pull_jobs SET attempt_count = %s WHERE id = %s",
                    (attempt_count, job_id),
                )
            conn.commit()

            # Resolve connection_ref (provider, nango_connection_id, project_id)
            ref = _resolve_connection_ref(conn, connection_ref_id)
            if ref is None:
                # Connection was deleted -- dead letter immediately.
                _finish_job(
                    conn,
                    job,
                    DEAD_LETTER,
                    error_detail=f"connection_ref '{connection_ref_id}' not found",
                    actor=requested_by,
                )
                logger.error(
                    "queue: job_id=%s dead_letter -- connection_ref not found: %s",
                    job_id,
                    connection_ref_id,
                )
                return True

            provider = ref["provider"]
            # AI-95: re-affirmed from the authoritative read of the same row, so
            # the credential fallback still applies when the job carries no
            # datastream. The module itself was resolved once, before the
            # pre-check -- not once per consumer.
            module_name = _datastream_module or provider
            # A google_direct authorization has no Nango connection (migration
            # 128), and passing None sent the token service looking for a
            # connection called "None" in Nango -- HTTP 400, every Google pull
            # failed. The resolver documents that it accepts EITHER identifier
            # space; the caller has to hand it the one that exists.
            nango_connection_id = ref["nango_connection_id"] or connection_ref_id
            project_id = ref["project_id"]

            # Look up read_cost for post-success record_spend (keyed by module:
            # quota is registered under the manifest name, core/loader.py:542).
            read_cost = quota.get_read_cost(module_name)

            # AD-2: call pull function via registry, never import from modules directly.
            # Story 10.1 (Epic 10): profile-aware dispatch -- resolve the datastream's
            # report_profile_id so a non-default profile (e.g. GA4 'user_type_daily')
            # routes to its per-profile pull function (pull_user_type_daily). Falls back
            # to the default pull() when the datastream has no non-default profile (or
            # the job carries no datastream_id -- legacy per-connection path).
            _profile_id = _resolve_datastream_profile(conn, job.get("datastream_id"))
            # A job can outlive the answer its enqueue gate had: a second account
            # verified on the same consent between enqueue and execution makes the
            # unbound Datastream ambiguous here too. Dead-letter rather than pull:
            # the repair is a human gesture, so a retry would only repeat it.
            try:
                _selected_account = _resolve_selected_account(
                    conn, connection_ref_id, job.get("datastream_id"), connector=module_name
                )
            except AccountSelectionRefused as refused:
                error_msg = _account_selection_refusal(refused)["message"]
                # A refusal a human repairs is terminal -- a retry only repeats
                # it. A read that did not come back is exactly what the attempt
                # ladder is for, so it takes the ordinary failure path.
                refused_state = (
                    (DEAD_LETTER if attempt_count >= max_att else FAILED)
                    if refused.retryable
                    else DEAD_LETTER
                )
                _finish_job(
                    conn, job, refused_state, error_detail=error_msg, actor=requested_by
                )
                write_audit_row(
                    identity=requested_by,
                    action=ACTION_PULL_FAILED,
                    provider_account=provider,
                    connection_ref=connection_ref_id,
                    metadata={
                        "job_id": job_id,
                        "pull_id": pull_id,
                        "error": error_msg,
                        "code": refused.code,
                        "attempt_count": attempt_count,
                    },
                )
                logger.error(
                    "queue: job_id=%s %s (%s) -- %s",
                    job_id,
                    refused_state,
                    refused.code,
                    error_msg,
                )
                return True
            pull_fn = get_module_pull_fn(module_name, profile_id=_profile_id)
            if pull_fn is None:
                error_msg = f"no pull function registered for module '{module_name}'"
                new_state = DEAD_LETTER if attempt_count >= max_att else FAILED
                _finish_job(
                    conn, job, new_state, error_detail=error_msg, actor=requested_by
                )
                write_audit_row(
                    identity=requested_by,
                    action=ACTION_PULL_FAILED,
                    provider_account=provider,
                    connection_ref=connection_ref_id,
                    metadata={
                        "job_id": job_id,
                        "pull_id": pull_id,
                        "error": error_msg,
                        "attempt_count": attempt_count,
                    },
                )
                logger.error("queue: job_id=%s failed -- %s", job_id, error_msg)
                return True

            # Call the pull path (Story 2.7 signature; Story 3.1 extraction routing).
            # review-3-1 F-1: the manifest's extraction_path is honoured here so the
            # airbyte_sync route is reachable from a live job, not just unit tests.
            try:
                _manifest = _get_manifest_for_module(module_name) or {}
                _profiles = _manifest.get("report_profiles") or [{}]
                # Story 10.1 F-1: extraction_path and dispatch args must follow the
                # ACTIVE profile resolved above (_profile_id via
                # _resolve_datastream_profile), not report_profiles[0]. When
                # _profile_id is None (legacy standard job) the next(...) falls back
                # to report_profiles[0] -- standard_daily behaviour preserved.
                if _profile_id is None:
                    _active_profile = _profiles[0] if _profiles else {}
                else:
                    _active_profile = next((p for p in _profiles if p.get("id") == _profile_id), {})
                _extraction = _active_profile.get("extraction_path", "custom_pull")
                # Story 25.8: catalog_driven profiles resolve+validate a field
                # selection from the catalog and pass it to the pull as selection=.
                # exact_bundle profiles return None here and stay bit-identical
                # (no selection= kwarg). A drifted selection raises
                # InvalidRequestError -> the ConnectorError branch below.
                _capability_report = _capability_report_for_profile(_manifest, _profile_id)
                _selection = _resolve_catalog_selection(
                    module_name, _capability_report, job, conn
                )
                # An event Connector writes rows that must name their Datastream,
                # and a pull signature never carries it. Said here, as the setup
                # drivers say it, so a scheduled run binds exactly like a first
                # collection instead of guessing from the Connector's name.
                from core.context_events import collecting_for_datastream  # noqa: PLC0415

                _event_scope = collecting_for_datastream(job.get("datastream_id"))
                _event_scope.__enter__()
                try:
                    if _extraction == "airbyte_sync":
                        from core.loader import dispatch_pull  # noqa: PLC0415
                        from core.main import get_loaded_modules  # noqa: PLC0415

                        result = dispatch_pull(
                            module_name=module_name,
                            profile_id=_active_profile.get("id", ""),
                            loaded_modules=get_loaded_modules(),
                            connection_id=nango_connection_id,
                            airbyte_connection_id=_active_profile.get("airbyte_connection_id"),
                            date_from=date_from,
                            date_to=date_to,
                            project_id=project_id,
                            pull_id=pull_id,
                        )
                    elif _selection is not None:
                        result = pull_fn(
                            connection_id=nango_connection_id,
                            date_from=date_from,
                            date_to=date_to,
                            project_id=project_id,
                            pull_id=pull_id,
                            selection=_selection,
                            **_account_kwargs(
                                pull_fn, _selected_account, module_name, connection_ref_id
                            ),
                            **_tracked_entity_kwargs(
                                conn, pull_fn, project_id, job.get("datastream_id")
                            ),
                        )
                    else:
                        result = pull_fn(
                            connection_id=nango_connection_id,
                            date_from=date_from,
                            date_to=date_to,
                            project_id=project_id,
                            pull_id=pull_id,
                            **_account_kwargs(
                                pull_fn, _selected_account, module_name, connection_ref_id
                            ),
                            **_tracked_entity_kwargs(
                                conn, pull_fn, project_id, job.get("datastream_id")
                            ),
                        )
                finally:
                    _event_scope.__exit__(None, None, None)
                row_count = result.get("row_count", 0) if isinstance(result, dict) else 0

                # Story 3.3 (AC3): record spend after successful pull.
                # BEFORE the prevented branch below, and deliberately: a window
                # the provider refused still spent the round trip that got
                # refused. Not charging it would under-count what this connector
                # actually consumed.
                if read_cost > 0:
                    quota.record_spend(module_name, read_cost)

                # AI-307 -- A WINDOW THE SOURCE DID NOT ALLOW TO RUN IS NOT AN
                # EMPTY ONE, AND THIS IS THE ONE PLACE THAT CAN TELL THEM APART.
                #
                # The line above reads ONE key of the envelope. Everything else a
                # connector says about its pull was dropped here, including the
                # three keys `google-business-profile` has returned since it
                # shipped (`skipped`, `skip_reason`, `message`) under a docstring
                # promising "the worker logs an honest zero ... the sentence the
                # UI shows". Measured 2026-08-21: those keys had NO reader
                # anywhere in `server/`, `ui/` or `web/`. A `reviews` pull refused
                # by the legacy-host allowlist was written `done / 0 row` -- the
                # same row a pull that ran and honestly found nothing writes.
                #
                # AND THE ZERO DID NOT STOP AT THE ROW. Everything below this
                # branch is a consumer of "a pull landed": `record_run_output_
                # version`, the `pull.completed` audit row, `FACT_PULL_LANDED`,
                # the boundary / entity / tax evidence, `run_post_pull_
                # verification` and the context seed. The verification one is what
                # made the false zero expensive: it counts the pull's rows, finds
                # none, files verdict `empty`, and `empty` raises the STICKY
                # `populate_failed` that the enqueue gate reads -- so ONE
                # un-granted quota refused every Datastream behind the whole
                # authorization. Returning here is not a shortcut: not one of
                # those consumers has anything to consume.
                #
                # THE READER IS SHARED ON PURPOSE (`core.pull_envelope`). All 39
                # connectors face the same class of provisioning gate. Repairing
                # the one that needed it first would be the instance; this seam is
                # the class, and a connector still declares its own reasons and
                # its own sentences at home.
                _prevented = prevented_by(result, module_name=module_name)
                if _prevented is not None:
                    _record_prevented(
                        conn,
                        job,
                        _prevented,
                        module_name=module_name,
                        provider=provider,
                        connection_ref_id=connection_ref_id,
                        requested_by=requested_by,
                    )
                    return True

                _finish_job(conn, job, DONE, row_count=row_count, actor=requested_by)

                # WHERE THIS RUN LANDED, said once, so the analytical read can
                # find it. Without it the newest output version stays the
                # candidate's isolated relation, which does not survive the
                # candidate: a query answered `unavailable` on a Datastream whose
                # run had just landed rows.
                if job.get("datastream_id") and job.get("execution_id"):
                    try:
                        from core.datastream_activation import (  # noqa: PLC0415
                            record_run_output_version,
                        )

                        record_run_output_version(
                            conn,
                            project_id=project_id,
                            datastream_id=str(job["datastream_id"]),
                            execution_id=str(job["execution_id"]),
                            actor=requested_by,
                        )
                    except Exception as exc:  # noqa: BLE001 -- the pull stands
                        logger.warning(
                            "queue: run_output_not_recorded job=%s: %s", job_id, exc
                        )

                write_audit_row(
                    identity=requested_by,
                    action=ACTION_PULL_COMPLETED,
                    provider_account=provider,
                    connection_ref=connection_ref_id,
                    metadata={
                        "job_id": job_id,
                        "pull_id": pull_id,
                        "row_count": row_count,
                        "date_from": date_from,
                        "date_to": date_to,
                    },
                )
                logger.info(
                    "queue: job_id=%s done pull_id=%s row_count=%d",
                    job_id,
                    pull_id,
                    row_count,
                )

                # Story 56.7 (AD-36): announce the FACT. Everything below this
                # line is a consumer of "a pull landed" that today has to be
                # called by name, in-process, inside a try/except that turns its
                # failure into one WARNING. Publishing it is what lets a consumer
                # arrive later without editing this function -- the "se greffer
                # dessus" property Pub/Sub exists for.
                #
                # Best-effort ON PURPOSE and not by omission: the pull is done and
                # recorded, so an unreachable topic must not undo it. The fact is
                # also carried by the audit row written just above, which is the
                # durable trace; this is the notification, not the record.
                try:
                    from core.events import FACT_PULL_LANDED, publish_fact  # noqa: PLC0415

                    publish_fact(
                        FACT_PULL_LANDED,
                        {
                            "pull_id": pull_id,
                            "job_id": job_id,
                            "connection_ref_id": connection_ref_id,
                            "datastream_id": job.get("datastream_id"),
                            # AI-302: WHICH PROFILE THIS PULL WAS. The fact must
                            # carry it, because its verification subscriber counts
                            # the pull's rows and a Connector landing several
                            # relations cannot be counted in `report_profiles[0]`.
                            # Without it the subscriber fell back to the module's
                            # single registered table -- empty for youtube-analytics
                            # -- counted 0, filed `empty`, and that verdict raises a
                            # STICKY `populate_failed` that denies every later pull
                            # of the whole authorization.
                            "report_profile_id": _profile_id,
                            "project_id": project_id,
                            "module": module_name,
                            "date_from": date_from,
                            "date_to": date_to,
                            "row_count": row_count,
                        },
                        idempotency_key=pull_id,
                    )
                except Exception as exc:  # noqa: BLE001 -- see comment above
                    logger.warning("queue: fact_publish_failed pull_id=%s: %s", pull_id, exc)

                # AI-161: record WHAT THIS PULL OBSERVED about its day boundary.
                #
                # `time_boundary.record_boundary_evidence` had ZERO callers in the whole
                # repository while `capability_compilers` READ the table it fills, and
                # told the operator "Run this Datastream so its publication records the
                # source day boundary". They could run it forever and nothing was ever
                # written. A false instruction costs more than a silent gap: it spends
                # someone's time promising the gesture will work.
                #
                # HERE and not at publication: `commit_publication` is a pointer swap over
                # already-validated data and forbids recomputation, while the zone is
                # observed at PULL. Here and not in `land_raw_rows`: that is the warehouse
                # write primitive, shared by every connector and holding no datastream.
                # This function is the only place that knows the datastream, the project
                # AND that the run succeeded.
                #
                # A pull that observed NO zone is recorded too, with a null zone: the
                # compiler renders "published without a resolvable reporting timezone",
                # which is a different and truer statement than "no run ever recorded a
                # boundary". Absence of evidence and evidence of absence are not the same
                # screen.
                #
                # Best-effort for the same reason as the fact above: the pull is done and
                # recorded, and a governance write must never undo it.
                if job.get("datastream_id"):
                    try:
                        from core.report_timezone import observed_zone_from_pull  # noqa: PLC0415
                        from core.time_boundary import (  # noqa: PLC0415
                            GRAIN_DATE_ONLY,
                            ORIGIN_NONE,
                            ORIGIN_PULL_METADATA,
                            record_boundary_evidence,
                        )

                        # L'ORIGINE DOIT DIRE LA VERITE SUR CE QUI A ETE OBSERVE.
                        # `ck_datastream_time_boundary_origin` exige un fuseau des
                        # que l'origine est `pull_metadata` -- et c'est juste :
                        # citer une metadonnee de pull sans rien en avoir tire est
                        # une source inventee. Un pull qui n'observe aucun fuseau
                        # n'a donc pas cette origine, il n'en a aucune.
                        #
                        # Sans cela l'insertion violait la contrainte et le `except`
                        # ci-dessous l'avalait : la preuve etait PERDUE a chaque
                        # run, alors que le commentaire ci-dessus promet qu'un pull
                        # sans zone « is recorded too, with a null zone ».
                        # Mesure 2026-08-12 sur le premier run reel.
                        observed_zone = observed_zone_from_pull(result)

                        record_boundary_evidence(
                            conn,
                            project_id=project_id,
                            datastream_id=str(job["datastream_id"]),
                            execution_id=pull_id,
                            # DATE everywhere, never hourly: the platform invariant, not a
                            # per-connector guess. It makes the evidence structurally
                            # non-realignable, which is exactly the honest posture.
                            grain=GRAIN_DATE_ONLY,
                            observed_report_timezone=observed_zone,
                            evidence_origin=(
                                ORIGIN_PULL_METADATA if observed_zone else ORIGIN_NONE
                            ),
                        )
                        conn.commit()
                    except Exception as exc:  # noqa: BLE001 -- must never undo the pull
                        conn.rollback()
                        logger.warning(
                            "queue: boundary_evidence_failed pull_id=%s ds=%s: %s",
                            pull_id, job.get("datastream_id"), exc,
                        )

                    # Story 48.5 (AI-89 family): record WHAT THIS PULL OBSERVED about
                    # the entities it can see. Same defect as the boundary evidence
                    # directly above, found the same way and repaired at the same seam:
                    # `entity_bindings.record_observation` had ZERO callers in the whole
                    # repository while `observation_summary` and `capability_compilers`
                    # READ the table it fills. `app.entity_observation_versions` held
                    # zero rows, so the competitors surface could only ever answer "no
                    # candidate observed" -- a completeness criterion closed on a writer
                    # nothing invokes, which is what the 48.5 review refused.
                    #
                    # HERE for the reason stated above: this function is the only place
                    # that knows the datastream, the project AND that the run succeeded.
                    # The candidate fields are READ from the connector's tracked-entity
                    # declaration, never guessed, and a connector that declares none
                    # records nothing.
                    #
                    # Best-effort for the same reason as its two neighbours: the pull is
                    # done and recorded, and a governance write must never undo it.
                    try:
                        from core.datastreams import get_datastream  # noqa: PLC0415
                        from core.entity_bindings import (  # noqa: PLC0415
                            record_observations_for_pull,
                        )

                        _ds = get_datastream(str(job["datastream_id"]), project_id, conn)
                        if _ds:
                            _seen = record_observations_for_pull(
                                conn,
                                datastream=_ds,
                                project_id=project_id,
                                pull_id=pull_id,
                                date_from=date_from,
                                date_to=date_to,
                                actor=requested_by,
                                connection_ref_id=connection_ref_id,
                            )
                            conn.commit()
                            if _seen:
                                logger.info(
                                    "queue: observed_candidates pull_id=%s ds=%s n=%d",
                                    pull_id, job["datastream_id"], _seen,
                                )
                    except Exception as exc:  # noqa: BLE001 -- must never undo the pull
                        conn.rollback()
                        logger.warning(
                            "queue: entity_observations_failed pull_id=%s ds=%s: %s",
                            pull_id, job.get("datastream_id"), exc,
                        )

                    # Story 68.3: give every occurrence of a DESIGNATED key a
                    # verdict. Story 68.2 lets a mapping column declare which
                    # entity type its values are keys of; without this call the
                    # declaration is a comment -- `rank_candidates`, the alias
                    # corpus and the ratified `resolve_governed_node` taxonomy
                    # all existed with no production driver, so a bound column
                    # resolved to nothing and no coverage could be stated.
                    #
                    # The same seam and the same discipline as its neighbour
                    # directly above: only here do we know the datastream, the
                    # project and that the run succeeded; the designated columns
                    # are READ from the pinned mapping, never guessed; a
                    # Datastream that designates nothing writes nothing; and it
                    # is best-effort, because a governance write must never undo
                    # a pull that already landed.
                    try:
                        from core.datastreams import get_datastream  # noqa: PLC0415
                        from core.entity_key_matching import (  # noqa: PLC0415
                            record_verdicts_for_pull,
                        )

                        _ds = get_datastream(str(job["datastream_id"]), project_id, conn)
                        if _ds:
                            _verdicts = record_verdicts_for_pull(
                                conn,
                                datastream=_ds,
                                project_id=project_id,
                                pull_id=pull_id,
                                date_from=date_from,
                                date_to=date_to,
                                actor=requested_by,
                                connection_ref_id=connection_ref_id,
                            )
                            conn.commit()
                            if _verdicts:
                                logger.info(
                                    "queue: entity_key_verdicts pull_id=%s ds=%s n=%d",
                                    pull_id, job["datastream_id"], _verdicts,
                                )
                    except Exception as exc:  # noqa: BLE001 -- must never undo the pull
                        conn.rollback()
                        logger.warning(
                            "queue: entity_key_verdicts_failed pull_id=%s ds=%s: %s",
                            pull_id, job.get("datastream_id"), exc,
                        )

                    # Story 48.4 / Epic 41: the immutable Tax evidence table used to
                    # have no production writer. Record this successful pull even when
                    # it returned no Tax fields: typed gaps are materially different
                    # from a Datastream that has never run.
                    try:
                        from core.datastreams import get_datastream  # noqa: PLC0415
                        from core.tax_evidence import (  # noqa: PLC0415
                            record_tax_evidence_for_pull,
                        )

                        _tax_ds = get_datastream(
                            str(job["datastream_id"]), project_id, conn
                        )
                        if _tax_ds:
                            record_tax_evidence_for_pull(
                                conn,
                                project_id=project_id,
                                datastream=_tax_ds,
                                pull_id=pull_id,
                                pull_result=result if isinstance(result, dict) else None,
                                recorded_by=requested_by,
                            )
                            conn.commit()
                    except Exception as exc:  # noqa: BLE001 -- must never undo the pull
                        conn.rollback()
                        logger.warning(
                            "queue: tax_evidence_failed pull_id=%s ds=%s: %s",
                            pull_id, job.get("datastream_id"), exc,
                        )

                # Story 3.5 (AC3): post-pull populate verification hook.
                # run_post_pull_verification never raises; the outer try/except
                # is a safety net in case of unexpected failures.
                #
                # Story 4.5 (AC4) / Epic 31.2 (landing routing): a profile that
                # lands in context_events skips populate verification — it writes
                # app.context_events (Postgres) via persist_context_event(), not a
                # DuckDB raw_* table. The skip now routes on the ACTIVE profile's
                # 'landing' (resolve_landing: per-profile 'landing' wins, else derived
                # from module_kind) instead of module_kind alone. This makes a MIXED
                # connector correct: a context_events profile on a module_kind='kpi'
                # connector (e.g. YouTube 'video_upload') is skipped, while its kpi
                # profiles still verify. Generic — no provider strings (AD-2/HG-8).
                # Future path: Story 5.x may extend verification to count context_events
                # rows filtered by pull_id (Option A in Story 4.5 spec).
                # Story 56.7: the two hooks below become SUBSCRIBERS the moment a
                # subscriber can actually be reached. Until then they stay in-band,
                # because a deployment with no topic and no push backend has no
                # consumer at all -- unplugging them there would silently stop
                # verifying pulls, which is the opposite of what this story is for.
                # `facts_are_delivered()` is the single place that decides.
                from core.facts_api import facts_are_delivered  # noqa: PLC0415

                _delivered = facts_are_delivered()
                if _delivered:
                    logger.info(
                        "queue: downstream_delegated pull_id=%s -- verification and "
                        "context seed run as subscribers of pull.landed",
                        pull_id,
                    )
                try:
                    from core.context_events import resolve_landing  # noqa: PLC0415
                    from core.verification import run_post_pull_verification  # noqa: PLC0415

                    manifest = _get_manifest_for_module(module_name)
                    _landing = resolve_landing(_active_profile, manifest.get("module_kind"))
                    if _landing == "context_events":
                        logger.info(
                            "queue: skipping populate verification for context_events "
                            "landing pull_id=%s module=%s profile=%s (landing=%s)",
                            pull_id,
                            module_name,
                            _active_profile.get("id", ""),
                            _landing,
                        )
                    elif _delivered:
                        # The verification subscriber runs it, with its own
                        # invocation, status and retry. Doing it here TOO would
                        # race the subscriber for one UNIQUE(pull_id) row and make
                        # one of the two look broken.
                        pass
                    else:
                        # review-epic-8 #7: thread the pull's rejected_rows count
                        # (generic connector reports unparseable-date rejects) into
                        # verification so the dq_date_format monitor has a signal.
                        _rejected = (
                            result.get("rejected_rows", 0) if isinstance(result, dict) else 0
                        )
                        run_post_pull_verification(
                            pull_id=pull_id,
                            connection_ref_id=connection_ref_id,
                            date_from=date_from,
                            date_to=date_to,
                            manifest=manifest,
                            # The raw-table registry is keyed by module name
                            # (verification.register_raw_table_name): under an
                            # unknown name the lookup falls through to the global
                            # fallback -- i.e. ANOTHER connector's table, counted
                            # and served as this pull's verdict.
                            provider=module_name,
                            rejected_rows=_rejected,
                            project_id=project_id,  # Story 24.3: route count to org schema
                            # WHICH RELATION THIS PROFILE LANDS IN. The registry
                            # holds ONE table per module, and a Connector whose
                            # profiles land in several counted the wrong one: a
                            # breakdown pull was counted in the daily table,
                            # found zero rows of its own pull_id, and was filed
                            # `empty` -- which raises the sticky red flag and
                            # refuses every later run. Measured 2026-08-12, on a
                            # pull that had just landed 112 rows.
                            report_profile_id=_profile_id,
                        )
                except Exception as exc:
                    logger.warning("queue: verification_hook_failed: %s", exc)

                # Story 44.2: day-0 context seed after a successful landing.
                # Restricted to the module that just landed, so a nightly run is
                # a cheap no-op once its connector topic exists. Best-effort:
                # seed_project_context_best_effort swallows its own failures and
                # the guard below covers even an import error -- a seeding
                # problem must never mark a completed pull as failed.
                #
                # ensure_schema=False explicite (decision revue vague 2) : le
                # hook ne lance JAMAIS le generateur 11.2. Sinon CHAQUE pull
                # rouvrait DuckDB pour profiler tout l'entrepot, en concurrence
                # avec le writer qui vient d'atterrir. Les schema docs viennent
                # du passage nocturne ; les aretes describes se creent au
                # premier pull qui suit leur existence.
                # Story 56.7: same switch as the verification hook above -- the
                # context-seed subscriber owns this once a fact can be delivered.
                if not _delivered:
                    try:
                        from core.context_seed import (  # noqa: PLC0415
                            seed_project_context_best_effort,
                        )

                        seed_project_context_best_effort(
                            project_id, module_names=[module_name], ensure_schema=False
                        )
                    except Exception as exc:  # noqa: BLE001 -- never break a done pull
                        logger.warning("queue: context_seed_hook_failed: %s", exc)

            except RateLimitError as exc:
                # Story 3.3 (AC3, AC4): trip breaker, then dead-letter if exhausted.
                # G-12: without this guard the job requeues forever -- after max_att
                # rate-limit attempts it must die rather than loop indefinitely.
                quota.record_rate_limit(exc.platform, exc.retry_after)
                if attempt_count >= max_att:
                    _finish_job(
                        conn,
                        job,
                        DEAD_LETTER,
                        error_detail=(
                            f"rate_limit_exhausted after {attempt_count} attempts: "
                            f"{str(exc)[:1900]}"
                        ),
                        actor=requested_by,
                    )
                    logger.warning(
                        "queue: job_id=%s dead_letter -- rate_limit_exhausted"
                        " platform=%s attempts=%d",
                        job_id,
                        exc.platform,
                        attempt_count,
                    )
                else:
                    with conn.cursor() as cur:
                        # Re-queue: attempt_count is already committed above
                        cur.execute(
                            """
                            UPDATE app.pull_jobs
                            SET state = 'queued',
                                error_detail = %s
                            WHERE id = %s
                            """,
                            (str(exc)[:2000], job_id),
                        )
                    conn.commit()
                    logger.warning(
                        "queue: job_id=%s rate_limited platform=%s retry_after=%s"
                        " -- requeued (attempt %d/%d)",
                        job_id,
                        exc.platform,
                        exc.retry_after,
                        attempt_count,
                        max_att,
                    )

            except ConnectorError as exc:
                # AI-307 (2026-08-25) -- THE REFUSAL THAT ARRIVES AS AN EXCEPTION,
                # READ BEFORE ANYTHING IS CLASSIFIED.
                #
                # The branch above covers the pull that RETURNS a prevented
                # envelope, which a connector may only do on a profile it is
                # allowed to abandon. A profile that must land rows cannot: an
                # empty day reported as a success would fabricate a day, so it
                # RAISES -- and `google-business-profile` has said exactly that
                # since it shipped (`connector.py:348-350`), marking the raised
                # error with `precondition` / `precondition_message`. Measured
                # 2026-08-24, those two attributes had no reader outside that
                # module, so its DEFAULT pull -- `pull_location_daily` on a
                # project still at 0 QPM, i.e. every Business Profile project on
                # its first day -- landed here and was written
                # `failed / permission_denied / reconnect`. Three wrong words:
                # nothing is denied to that credential, reconnecting releases
                # nothing (the grant is a manual approval at Google), and `failed`
                # is what `scheduler._reschedule_failed_pulls` re-arms hourly --
                # the crash loop against a human approval queue that `prevented`
                # exists to stop, reached through the other door.
                #
                # THE READER IS THE CLASS'S, like `prevented_by` beside it. Any of
                # the 39 connectors may mark any raised error; one that marks none
                # keeps this branch bit-identical.
                _prevented = prevented_by_error(exc, module_name=module_name)
                if _prevented is not None:
                    _record_prevented(
                        conn,
                        job,
                        _prevented,
                        module_name=module_name,
                        provider=provider,
                        connection_ref_id=connection_ref_id,
                        requested_by=requested_by,
                    )
                    return True

                # Story 25.2 (AC3, AC4): per-class retry policy on typed errors.
                # Non-retryable classes (auth_expired/auth_revoked/permission_denied/
                # invalid_request) go straight to a terminal state WITHOUT consuming
                # further attempts: a single attempt is recorded, never requeued.
                # Retryable classes (provider_transient) keep today's generic-exception
                # attempt/backoff/dead-letter behaviour. Either way error_detail is the
                # structured JSON envelope (parseable; legacy plain-text readers tolerate it).
                # FAILED and DEAD_LETTER are both terminal (the poller only claims
                # state='queued'), so no requeue happens for either retryability;
                # retryable errors re-enter only via an explicit re-enqueue upstream.
                error_msg = str(exc)
                new_state = DEAD_LETTER if attempt_count >= max_att else FAILED

                error_detail = _build_error_detail(
                    error_class=exc.error_class,
                    user_action=exc.user_action,
                    provider_status=exc.provider_status,
                    provider_payload=exc.provider_payload,
                    message=error_msg,
                    attempt_count=attempt_count,
                )

                # invalid_request is the catalog-drift signal: log at WARNING with a
                # distinct event key so 25.4+ can alert on it. No alerting plumbing here.
                #
                # SAUF quand l'erreur porte une action a proposer (2026-08-01). Un pull
                # dont le compte n'a jamais ete choisi est `invalid_request` -- la
                # requete ne peut pas etre formee -- mais ce n'est PAS la preuve que
                # l'API a bouge. Le laisser emettre ici noyait le signal de derive sous
                # des defauts de configuration, et ce signal ne vaut que par sa purete :
                # il n'a pas d'autre usage que de declencher une inspection de catalogue.
                # `user_action` est exactement le discriminant -- une derive n'en a pas,
                # puisque personne ne peut la reparer depuis l'ecran.
                if exc.error_class == "invalid_request" and exc.user_action is None:
                    logger.warning(
                        '{"event": "pull_invalid_request_drift", "job_id": "%s",'
                        ' "provider": "%s", "module": "%s", "provider_status": %s}',
                        job_id,
                        provider,
                        # What drifted is a MODULE's catalog. The documented
                        # "provider" key keeps its meaning (which credential);
                        # "module" names the catalog to go and look at.
                        module_name,
                        exc.provider_status,
                    )

                # Story 63.1: a window that will not land still moves the run --
                # and what the run collected BEFORE this failure is kept. A
                # failed execution that showed no progress and one that never
                # started would read as the same absence, which is the defect
                # the acceptance names.
                _finish_job(
                    conn, job, new_state, error_detail=error_detail, actor=requested_by
                )

                # AI-341: an authorization refused at pull time is a fact of
                # health -- written HERE, where the truth was learned, in the
                # same transaction as the failure it records.
                _record_auth_red(conn, connection_ref_id, pull_id, exc.error_class)

                write_audit_row(
                    identity=requested_by,
                    action=ACTION_PULL_FAILED,
                    provider_account=provider,
                    connection_ref=connection_ref_id,
                    metadata={
                        "job_id": job_id,
                        "pull_id": pull_id,
                        "error": error_msg,
                        "error_class": exc.error_class,
                        "attempt_count": attempt_count,
                    },
                )
                logger.error(
                    "queue: job_id=%s %s error_class=%s (attempt %d/%d): %s",
                    job_id,
                    new_state,
                    exc.error_class,
                    attempt_count,
                    max_att,
                    error_msg,
                )

            except Exception as exc:
                # A PRECONDITION IS READ HERE TOO, because the contract is on the
                # ERROR and not on its class. A connector that marks an untyped
                # exception would otherwise have its refusal filed `unclassified`
                # -- and `unclassified` is RETRYABLE, so the window would be
                # re-attempted against a grant no retry can obtain. An exception
                # carrying no precondition (every one raised today outside
                # `google-business-profile`) takes the branch below untouched.
                _prevented = prevented_by_error(exc, module_name=module_name)
                if _prevented is not None:
                    _record_prevented(
                        conn,
                        job,
                        _prevented,
                        module_name=module_name,
                        provider=provider,
                        connection_ref_id=connection_ref_id,
                        requested_by=requested_by,
                    )
                    return True

                # Last-resort safety net (Story 25.2 AC3): any untyped exception is
                # treated as the taxonomy's "unclassified" class. Retry/dead-letter
                # semantics stay bit-identical to the pre-25.2 generic path
                # (retryable=True: failed until max attempts, then dead_letter); only
                # the error_detail is upgraded to the structured JSON shape.
                error_msg = str(exc)
                new_state = DEAD_LETTER if attempt_count >= max_att else FAILED

                error_detail = _build_error_detail(
                    error_class="unclassified",
                    user_action=None,
                    provider_status=None,
                    provider_payload=None,
                    message=error_msg,
                    attempt_count=attempt_count,
                )

                # Story 63.1: a window that will not land still moves the run --
                # and what the run collected BEFORE this failure is kept. A
                # failed execution that showed no progress and one that never
                # started would read as the same absence, which is the defect
                # the acceptance names.
                _finish_job(
                    conn, job, new_state, error_detail=error_detail, actor=requested_by
                )

                write_audit_row(
                    identity=requested_by,
                    action=ACTION_PULL_FAILED,
                    provider_account=provider,
                    connection_ref=connection_ref_id,
                    metadata={
                        "job_id": job_id,
                        "pull_id": pull_id,
                        "error": error_msg,
                        "error_class": "unclassified",
                        "attempt_count": attempt_count,
                    },
                )
                logger.error(
                    "queue: job_id=%s %s error_class=unclassified (attempt %d/%d): %s",
                    job_id,
                    new_state,
                    attempt_count,
                    max_att,
                    error_msg,
                )

        # Story 5.1 (AC5): record end-to-end pull latency on the worker span.
        _job_span.set("job.latency_ms", int((_time.perf_counter() - _job_t0) * 1000))

    return True


def _get_manifest_for_module(module_name: str | None) -> dict:
    """Return the manifest dict for the named MODULE from the loaded registry.

    Uses a local import of core.main._loaded_modules (core->core, not AD-2 violation).
    _loaded_modules is populated once at startup and never mutated by the worker thread.

    Returns {} (empty dict) if the module is not loaded; compute_expected_rows()
    uses days as its safe fallback when the manifest has no report_profiles.

    AI-95: this function used to be named ``_get_manifest_for_module`` and that
    name was the trap. ``connection_ref.provider`` names the CREDENTIAL, and a
    Google direct grant is stored as 'google' -- the name of no module at all, so
    the lookup returned {} and the caller silently got the day-count fallback
    instead of the profile's real expectation. The parameter is a module name;
    the name now says so.

    Story 3.5 (AC3, T4.2).
    """
    try:
        from core.main import _loaded_modules  # noqa: PLC0415

        for loaded in _loaded_modules:
            if loaded.name == module_name:
                return loaded.manifest
    except Exception:
        pass
    return {}


def _create_push_task(path: str, *, tasks_client=None) -> str:
    """Create ONE Cloud Tasks HTTP task addressed at *path*. Returns the URL.

    Shared by the pull queue and the activation queue so the two cannot drift
    into two dispatch behaviours -- the divergence that made the push backend
    unrunnable in the first place (AI-97) was exactly that kind of drift.

    HG-1: raises EnvironmentError when the project is unset, rather than silently
    creating nothing.
    """
    project = os.environ.get("CLOUD_TASKS_PROJECT", "").strip()
    if not project:
        raise EnvironmentError(
            "CLOUD_TASKS_PROJECT env var is required for QUEUE_BACKEND=cloud_tasks"
        )
    location = os.environ.get("CLOUD_TASKS_LOCATION", "")
    queue_name = os.environ.get("CLOUD_TASKS_QUEUE_NAME", "")
    worker_url = os.environ.get("CLOUD_TASKS_WORKER_URL", "")

    if tasks_client is None:
        try:
            from google.cloud import tasks_v2  # noqa: PLC0415

            tasks_client = tasks_v2.CloudTasksClient()
        except ImportError as exc:
            raise EnvironmentError(
                "google-cloud-tasks is not installed; add it to pyproject.toml for Phase B"
            ) from exc

    # THE TASK MUST CARRY A CREDENTIAL, or the worker refuses it forever.
    # `admin_api._check_internal_auth` accepts a Google OIDC id token minted for
    # this service -- verified against `INTERNAL_OIDC_AUDIENCE` -- or the
    # `X-Internal-Auth` shared secret, which a Cloud Task cannot carry safely
    # (it would be stored in the queue). OIDC is the stronger form and the one
    # the guard checks first.
    #
    # Measured 2026-08-07: without it, every task 401s. Two `setup_preview` jobs
    # sat in `queued` while Cloud Tasks retried them and the queue reported
    # RUNNING -- a push backend that had never executed a single job, of any
    # kind, and looked healthy doing it.
    service_account = os.environ.get("CLOUD_TASKS_OIDC_SERVICE_ACCOUNT", "").strip()
    audience = os.environ.get("INTERNAL_OIDC_AUDIENCE", "").strip() or worker_url
    if not service_account or not audience:
        # Fail closed, like the missing-project guard above: a task nobody can
        # accept is worse than no task -- it retries for hours, the queue looks
        # busy, and the job stays `queued` with nothing to say why.
        raise EnvironmentError(
            "CLOUD_TASKS_OIDC_SERVICE_ACCOUNT and an audience "
            "(INTERNAL_OIDC_AUDIENCE or CLOUD_TASKS_WORKER_URL) are required for "
            "QUEUE_BACKEND=cloud_tasks: a task without an OIDC token is refused 401"
        )

    parent = tasks_client.queue_path(project, location, queue_name)
    task_url = f"{worker_url}{path}"
    task = {
        "http_request": {
            "http_method": "POST",
            "url": task_url,
            "oidc_token": {
                "service_account_email": service_account,
                "audience": audience,
            },
        }
    }
    tasks_client.create_task(request={"parent": parent, "task": task})
    return task_url


def dispatch_activation_task(job_id: str, *, tasks_client=None) -> bool:
    """Dispatch the push task for an activation job that is ALREADY COMMITTED.

    WHY THIS IS NOT CALLED FROM ``enqueue_activation_work``, which would be the
    obvious place. All three callers enqueue inside THEIR OWN transaction --
    deliberately, so the job row commits atomically with the state change that
    justifies it. Dispatching from inside that transaction would address a task
    at a row that has not committed yet and may never commit: the task would
    arrive first and find nothing, or arrive for work that was rolled back. AD-36
    fixes the order -- the row exists, THEN the task -- so the dispatch has to
    happen after the caller's commit, which only the caller knows about.

    A forgotten call is therefore possible, and that is survivable BY DESIGN: the
    row is the ledger, so the reconciliation sweep (story 56.4) re-dispatches any
    activation job left 'queued' with no live task. A missed dispatch costs
    latency, never the operation.

    No-op (returns False) when the backend is not cloud_tasks: the polling worker
    is the dispatcher in that mode, and calling this from a dev machine must not
    require a Cloud Tasks project.
    """
    if os.environ.get("QUEUE_BACKEND", "local") != "cloud_tasks":
        return False
    try:
        url = _create_push_task(f"/internal/worker/execute-activation/{job_id}",
                                tasks_client=tasks_client)
    except Exception as exc:  # noqa: BLE001 -- the row is committed; the sweep catches it
        logger.warning(
            "queue: activation_dispatch_failed job_id=%s: %s -- the row is committed, "
            "the reconciliation sweep will re-dispatch it",
            job_id,
            exc,
        )
        return False
    logger.info("queue: cloud_tasks enqueued activation job_id=%s url=%s", job_id, url)
    return True


def dispatch_pull_task(job_id: str, *, tasks_client=None) -> bool:
    """Dispatch the push task for a pull job whose row is already committed.

    Symmetric to ``dispatch_activation_task``. ``CloudTasksBackend.enqueue_pull``
    creates the task itself because it owns its transaction; this exists for the
    reconciliation sweep, which finds rows whose task was never created or was
    lost.
    """
    if os.environ.get("QUEUE_BACKEND", "local") != "cloud_tasks":
        return False
    try:
        url = _create_push_task(f"/internal/worker/execute-pull/{job_id}",
                                tasks_client=tasks_client)
    except Exception as exc:  # noqa: BLE001 -- the sweep runs again
        logger.warning("queue: pull_dispatch_failed job_id=%s: %s", job_id, exc)
        return False
    logger.info("queue: cloud_tasks re-dispatched pull job_id=%s url=%s", job_id, url)
    return True


def _reconcile_grace_seconds() -> int:
    """How long a committed row may sit unclaimed before it is presumed taskless."""
    try:
        return max(60, int(os.environ.get("QUEUE_RECONCILE_GRACE_SECONDS", "300")))
    except ValueError:
        return 300


def reconcile_pending_tasks(*, grace_seconds: int | None = None, limit: int = 200,
                            tasks_client=None) -> dict:
    """Re-dispatch work the LEDGER shows pending and no live task is serving.

    Story 56.4 (AD-36). This exists because the row and the task are two systems
    written in sequence: ``enqueue`` commits the row, then creates the task. The
    second can fail after the first succeeded -- or the caller can simply never
    reach its dispatch -- and the job then sits 'queued' with nothing coming for
    it. Nothing recovered that case: ``recover_stale_running_jobs`` re-queues
    jobs stuck in 'running', which is the opposite failure.

    IT RE-DISPATCHES, IT NEVER EXECUTES, and that distinction is the whole
    contract. The moment this function runs a job itself it has become a second
    worker, one invocation would carry N jobs, and AD-36's readability -- one
    task, one job, one status -- is gone. Incompleteness criterion 9 exists for
    exactly this.

    "No live task" cannot be asked of Cloud Tasks cheaply, so AGE is the proxy: a
    row still 'queued' after the grace period is presumed taskless. Re-dispatching
    one that did have a task is harmless -- the claim refuses anything that is not
    claimable, so the duplicate is a no-op 200.

    Returns counts; never raises. No-op under QUEUE_BACKEND=local, where the
    polling worker needs no dispatcher.
    """
    result = {"pull_redispatched": 0, "activation_redispatched": 0, "backend": "local"}
    if os.environ.get("QUEUE_BACKEND", "local") != "cloud_tasks":
        return result
    result["backend"] = "cloud_tasks"
    grace = grace_seconds if grace_seconds is not None else _reconcile_grace_seconds()

    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id FROM app.pull_jobs
                    WHERE state = 'queued'
                      AND enqueued_at < now() - (%s * interval '1 second')
                    ORDER BY enqueued_at
                    LIMIT %s
                    """,
                    (grace, limit),
                )
                pull_ids = [row[0] for row in cur.fetchall()]
                cur.execute(
                    """
                    SELECT id FROM app.datastream_activation_jobs
                    WHERE state IN ('queued', 'failed')
                      AND attempt_count < %s
                      AND enqueued_at < now() - (%s * interval '1 second')
                    ORDER BY enqueued_at
                    LIMIT %s
                    """,
                    (_max_attempts(), grace, limit),
                )
                activation_ids = [row[0] for row in cur.fetchall()]
    except Exception as exc:  # noqa: BLE001 -- the sweep is periodic; it runs again
        logger.warning("queue: reconcile_read_failed: %s", exc)
        return result

    for job_id in pull_ids:
        if dispatch_pull_task(job_id, tasks_client=tasks_client):
            result["pull_redispatched"] += 1
    for job_id in activation_ids:
        if dispatch_activation_task(job_id, tasks_client=tasks_client):
            result["activation_redispatched"] += 1

    if result["pull_redispatched"] or result["activation_redispatched"]:
        logger.info(
            "queue: reconciled pull=%d activation=%d grace=%ds",
            result["pull_redispatched"],
            result["activation_redispatched"],
            grace,
        )
    return result


def claim_activation_job_by_id(conn, job_id: str) -> dict | None:
    """Atomically CLAIM the NAMED activation job, or return None (story 56.3).

    Same relation to ``_dequeue_activation_job`` as ``claim_job_by_id`` has to
    ``_dequeue_one``: identical discipline, opposite question. The eligible
    states are the queue's own -- 'queued' AND 'failed' under the attempt ceiling
    -- because a failed activation job is retried in place rather than re-queued.
    """
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id,kind,project_id,draft_id,datastream_id,execution_id,
                      correlation_id,payload,requested_by,attempt_count
                 FROM app.datastream_activation_jobs
                WHERE id=%s AND state IN ('queued','failed') AND attempt_count < %s
                FOR UPDATE SKIP LOCKED""",
            (job_id, _max_attempts()),
        )
        row = cur.fetchone()
        if row is None:
            conn.commit()
            return None
        columns = [description[0] for description in cur.description]
        job = dict(zip(columns, row))
        cur.execute(
            "UPDATE app.datastream_activation_jobs SET state='running',"
            "attempt_count=attempt_count+1,started_at=NOW(),error_code=NULL WHERE id=%s",
            (job["id"],),
        )
    conn.commit()
    job["attempt_count"] = int(job["attempt_count"]) + 1
    return job


def execute_claimed_activation_job(job: dict) -> None:
    """Run one ALREADY-CLAIMED activation job (story 56.3).

    Public door onto the same execution the polling worker uses, for the same
    reason as ``execute_claimed_job``: two entry points, one behaviour.

    Returns None -- the outcome lives on the row (state 'done', 'failed' or
    'dead_letter'), which the caller reads to decide what to answer the task.
    """
    _execute_activation_job(job)


def get_activation_job_state(job_id: str) -> str | None:
    """Return the activation job's state, or None when the id is unknown."""
    from core.db import get_connection  # noqa: PLC0415

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT state FROM app.datastream_activation_jobs WHERE id=%s",
                (job_id,),
            )
            row = cur.fetchone()
    return row[0] if row else None


def execute_claimed_job(job: dict) -> bool:
    """Run one ALREADY-CLAIMED job (story 56.2, AD-36).

    The push path and the polling worker execute the same code; only the way the
    job was chosen differs (``claim_job_by_id`` versus ``_dequeue_one``). This is
    the public door onto that shared execution, so a push endpoint never has to
    import the private ``_execute_job`` -- and so the two paths can never drift
    into two behaviours, which is the whole reason the substrate can be switched
    at all.

    The caller MUST have claimed the job first: the row is expected in 'running'.

    Returns:
        True  -- executed (done, failed, or dead_letter recorded on the row)
        False -- quota-blocked and returned to 'queued', no attempt spent
    """
    return _execute_job(job)


def _worker_loop(*, _sleeper=time.sleep) -> None:
    """Infinite poll loop -- runs in a daemon thread.

    Polls app.pull_jobs for 'queued' rows with SKIP LOCKED, executes each job,
    then sleeps for QUEUE_POLL_INTERVAL_SECONDS before polling again.

    Story 3.3 (AC4): implements exponential backoff when quota-blocked.
    ``consecutive_quota_blocks`` counts how many consecutive poll cycles ended
    in a quota block. Sleep = min(interval * 2**count, MAX_BACKOFF_SECONDS).
    The counter resets whenever a job executes (quota-ok or no job found).

    ``_sleeper`` is injectable for tests (avoids real sleep in unit tests).
    """
    interval = _poll_interval()
    logger.info("queue_worker: loop started (interval=%ss)", interval)

    consecutive_quota_blocks = 0

    recover_stale_running_jobs()  # review-3-2 F-1b: startup recovery
    _last_recovery = 0.0
    while True:
        # periodic recovery sweep (every ~10 min of loop time)
        _now = time.monotonic()
        if _now - _last_recovery > 600:
            recover_stale_running_jobs()
            _last_recovery = _now
        try:
            from core.db import get_connection  # noqa: PLC0415

            # TODO(AI-17): replace with psycopg_pool.ConnectionPool at Epic 3 scale
            with get_connection() as conn:
                job = _dequeue_one(conn)

            if job is not None:
                logger.info("queue_worker: dispatching job_id=%s", job["id"])
                executed = _execute_job(job)
                if executed:
                    # Job was executed (done/failed/dead_letter) -- reset backoff
                    consecutive_quota_blocks = 0
                else:
                    # Quota-blocked -- increment backoff counter
                    consecutive_quota_blocks += 1
            else:
                # Pull queue is empty: use the same worker cycle and durable claim
                # discipline for setup preview / candidate materialization work.
                with get_connection() as conn:
                    activation_job = _dequeue_activation_job(conn)
                if activation_job is not None:
                    logger.info(
                        "queue_worker: dispatching activation job_id=%s",
                        activation_job["id"],
                    )
                    _execute_activation_job(activation_job)
                consecutive_quota_blocks = 0

        except Exception:
            logger.exception("queue_worker: poll cycle failed")
            consecutive_quota_blocks = 0

        # Story 3.3 (AC4): exponential backoff when quota-blocked
        if consecutive_quota_blocks > 0:
            max_bk = _max_backoff()
            sleep_secs = min(interval * (2**consecutive_quota_blocks), max_bk)
            logger.info(
                "queue_worker: quota_backoff sleep=%ds consecutive_blocks=%d",
                sleep_secs,
                consecutive_quota_blocks,
            )
            _sleeper(sleep_secs)
        else:
            _sleeper(interval)


# ---------------------------------------------------------------------------
# Cloud Tasks backend stub (AC8)
# ---------------------------------------------------------------------------


class CloudTasksBackend:
    """Cloud Tasks backend -- the AD-36 production path (Phase B, AC8).

    At QUEUE_BACKEND=cloud_tasks:
      - enqueue_pull() still inserts the app.pull_jobs row (status endpoint).
      - Then enqueues a Cloud Tasks HTTP task pointing at the worker URL.
    HG-1: raises EnvironmentError if CLOUD_TASKS_PROJECT is unset at enqueue time.

    THE ROW BEFORE THE TASK, AND THAT ORDER IS THE CONTRACT (AD-36). An operation
    exists because a Postgres row says so, never because a message is in flight:
    a queue has a retention policy and cannot be queried, joined or audited three
    months later. The consequence is deliberate -- a row can outlive a lost task,
    which the reconciliation sweep (story 56.4) re-dispatches. The reverse order
    would lose the operation itself.

    THIS CLASS WAS "interface-complete" AND COULD NOT RUN. Measured 2026-07-31
    (AI-97): it declared no ``datastream_id`` while the module-level
    ``enqueue_pull`` always passes one, so the FIRST enqueue after flipping
    ``QUEUE_BACKEND`` raised ``TypeError: unexpected keyword argument``. Two more
    divergences from ``LocalBackend`` travelled with it and are closed here,
    because each is silent rather than loud:

      * ``datastream_id`` is what lets the worker find the MODULE when the
        credential does not name it (AI-95). Dropping it does not fail -- it
        makes a Google job look up a module called 'google', which is none;
      * ``trace_id`` joins the worker span to the request that asked for the
        pull. Without it the push path loses exactly the readability the
        migration is for;
      * the INSERT carried no ``ON CONFLICT``, while ``uq_pull_jobs_active``
        exists. A double click would have raised UniqueViolation instead of
        returning the running job -- and, worse, dispatched a SECOND task for a
        job already in flight.
    """

    def __init__(self, _tasks_client=None):
        """_tasks_client: injected in tests; None = real google-cloud-tasks client."""
        self._tasks_client = _tasks_client

    def enqueue_pull(
        self,
        connection_ref_id: str,
        date_from: str,
        date_to: str,
        *,
        requested_by: str,
        datastream_id: str | None = None,
        execution_id: str | None = None,
        _tasks_client=None,
    ) -> dict:
        """Insert job row then dispatch Cloud Tasks HTTP task.

        Same signature as ``LocalBackend.enqueue_pull``: the module-level
        ``enqueue_pull`` calls whichever backend is configured with the SAME
        keywords, so a divergence here is a TypeError at the first push.

        HG-1: raises EnvironmentError if CLOUD_TASKS_PROJECT unset.
        """
        project = os.environ.get("CLOUD_TASKS_PROJECT", "").strip()
        if not project:
            raise EnvironmentError(
                "CLOUD_TASKS_PROJECT env var is required for QUEUE_BACKEND=cloud_tasks"
            )

        from core.audit import ACTION_PULL_TRIGGERED, write_audit_row  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        job_id = _mint_job_id()
        pull_id = _mint_pull_id()
        trace_id = _capture_trace_id()

        # TODO(AI-17): replace with psycopg_pool.ConnectionPool at Epic 3 scale
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.pull_jobs
                        (id, pull_id, connection_ref_id, date_from, date_to,
                         state, requested_by, trace_id, datastream_id, execution_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    -- AI-302 / migration 275: mirrors `uq_pull_jobs_active`
                    -- EXACTLY, expression included. A mismatch here does not
                    -- deduplicate -- it raises at INSERT.
                    ON CONFLICT (connection_ref_id, COALESCE(datastream_id, ''),
                                 date_from, date_to)
                        WHERE state IN ('queued', 'running')
                    DO NOTHING
                    RETURNING id
                    """,
                    (
                        job_id,
                        pull_id,
                        connection_ref_id,
                        date_from,
                        date_to,
                        QUEUED,
                        requested_by,
                        trace_id,
                        datastream_id,
                        execution_id,
                    ),
                )
                inserted_id = cur.fetchone()

            if inserted_id is None:
                # An identical window is already queued or running, so a task
                # already exists for it. Return the live job WITHOUT dispatching
                # a second task: two tasks for one row would run the same pull
                # twice and bill the provider twice for one operator click.
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT id, pull_id, state FROM app.pull_jobs
                        WHERE connection_ref_id = %s
                          AND COALESCE(datastream_id, '') = COALESCE(%s, '')
                          AND date_from = %s AND date_to = %s
                          AND state IN ('queued', 'running')
                        LIMIT 1
                        """,
                        (connection_ref_id, datastream_id, date_from, date_to),
                    )
                    existing = cur.fetchone()
                conn.commit()
                if existing is not None:
                    return {
                        "job_id": existing[0],
                        "pull_id": existing[1],
                        "state": existing[2],
                        "deduplicated": True,
                    }
            conn.commit()

        # Write audit row
        write_audit_row(
            identity=requested_by,
            action=ACTION_PULL_TRIGGERED,
            provider_account="",
            connection_ref=connection_ref_id,
            metadata={
                "job_id": job_id,
                "pull_id": pull_id,
                "date_from": date_from,
                "date_to": date_to,
            },
        )

        # Dispatch the Cloud Tasks HTTP task -- through the SHARED builder, so the
        # pull queue and the activation queue cannot drift into two dispatch
        # behaviours (story 56.3).
        task_url = _create_push_task(
            f"/internal/worker/execute-pull/{job_id}",
            tasks_client=_tasks_client or self._tasks_client,
        )

        logger.info("queue: cloud_tasks enqueued job_id=%s url=%s", job_id, task_url)

        return {"job_id": job_id, "pull_id": pull_id, "state": QUEUED}

    def get_job_status(self, job_id: str) -> dict | None:
        """Same as LocalBackend -- reads from app.pull_jobs."""
        from core.db import get_connection  # noqa: PLC0415

        # TODO(AI-17): replace with psycopg_pool.ConnectionPool at Epic 3 scale
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, pull_id, connection_ref_id, date_from, date_to,
                           state, requested_by, error_detail, attempt_count,
                           enqueued_at, started_at, completed_at
                    FROM app.pull_jobs
                    WHERE id = %s
                    """,
                    (job_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                cols = [desc[0] for desc in cur.description]
                record: dict = {}
                _TS_COLS = {"enqueued_at", "started_at", "completed_at"}
                for col, val in zip(cols, row):
                    if col in _TS_COLS and val is not None:
                        record[col] = val.isoformat()
                    elif col in ("date_from", "date_to") and val is not None:
                        record[col] = str(val)
                    else:
                        record[col] = val
        return record


# ---------------------------------------------------------------------------
# Module-level backend selection (mirrors TOOROW_DB_MODE / BigQuery pattern)
# ---------------------------------------------------------------------------

_BACKEND_ENV = os.environ.get("QUEUE_BACKEND", "local")
_backend: LocalBackend | CloudTasksBackend = (
    CloudTasksBackend() if _BACKEND_ENV == "cloud_tasks" else LocalBackend()
)


#: The actors that are a CLOCK, not a person (AI-301). `scheduler` is the literal
#: default of `scheduler.dispatch_nightly` / `dispatch_hourly`; `queue` and
#: `worker` are the drain side. They have no human identity and no organisation,
#: which is exactly the case `db.background_connection` was written for.
#:
#: The membership test is deliberately CLOSED and the default is the STRICT side:
#: an actor this set does not know stays on the isolated request connection. A
#: new machine actor therefore fails visibly (its rows are hidden, as today)
#: rather than silently acquiring an unisolated connection.
_BACKGROUND_ACTORS = frozenset({"scheduler", "queue", "worker"})


def _is_background_actor(requested_by: str | None) -> bool:
    """Is this dispatch a clock rather than somebody's request? (AI-301)"""
    return str(requested_by or "").strip().lower() in _BACKGROUND_ACTORS


def _topology_scope_refusal(
    connection_ref_id: str,
    *,
    requested_by: str,
    datastream_id: str | None = None,
    module_name: str | None = None,
) -> dict | None:
    """Story 25.5 (AC4): refuse the enqueue when a topology-declaring provider has
    no ready account scope, else return None (proceed).

    Returns a refusal DICT (never raises) mirroring the enqueue_pull return shape
    so callers branch on it exactly like the deduplicated case:
        {"state": "refused", "code": "account_not_selected", "message": ...}

    Story 46.4 retired the runtime flag this used to be gated on, so the strict
    policy is unconditional. That flipped two defaults, and the docstring above
    described the OLD ones until it was corrected here:
      * a provider whose manifest does not declare `account_topology` is now
        REFUSED rather than waved through. All 37 shipped manifests declare one,
        so this branch guards a registry regression, not a live connector;
      * a failure to resolve the manifest is a denial, not a proceed --
        uncertainty at an authorization seam cannot mean yes.
    An unknown connection is still NOT a topology refusal: the worker
    dead-letters it, which is a different, non-authorization outcome.

    AI-96 [1]: the topology is resolved under the MODULE name, not the
    credential's. `app.connection_ref.provider` names the AUTHORIZATION -- a
    direct Google consent is stored 'google', which is the name of no module,
    because that one consent screen opens Search Console, Analytics, Ads and
    four others. Asking the registry for a module called 'google' returned None,
    the fail-closed branch above fired, and EVERY Google pull was refused at
    enqueue -- on the only connector family that authenticates in production.

    This is the same defect AI-95 closed inside `_execute_job`, at a site that
    lot could not reach: the refusal runs at ENQUEUE, so the job it rejects
    never exists and `_execute_job` never sees it. The rule is unchanged and
    still cuts both ways -- module name wherever a manifest, a catalogue, a
    quota or a topology is looked up; credential name only in the audit trail.

    The gate itself is NOT loosened. A datastream whose module resolves to
    nothing is still refused, and a connection with no datastream still
    resolves by its provider -- which is correct for the 37 Nango connectors,
    whose credential IS their module.
    """
    try:
        from core.db import background_connection, request_connection  # noqa: PLC0415
        from core.project_access import (  # noqa: PLC0415
            resolve_provider_account_access,
            resolve_scheduled_account_access,
        )

        # TODO(AI-17): replace with psycopg_pool.ConnectionPool at Epic 3 scale
        #
        # AI-301: THE ENQUEUE SIDE IS NOT ALWAYS A REQUEST, and assuming it was
        # stopped every collection on the platform for five days. The comment
        # that stood here said "runs inside somebody's request ... so it acquires
        # armed". That is true of the manual door. It is false of the nightly
        # dispatcher, which `background_connection` already describes as its own
        # case: "Scheduler and queue run without a human identity and legitimately
        # work for several organizations at once -- the nightly dispatch reads
        # every armed Datastream ... these paths are not requests". Run armed with
        # no organisation context, RLS returned 0 rows for `app.datastreams`, so
        # the module fell back to the credential's `google` and the org_id lookup
        # below found nothing -- two fail-closed denials from one blindness.
        #
        # The BARRIER IS NOT WEAKENED, and that is the point of splitting here:
        # what moves to the unisolated connection is the two ROUTING reads (which
        # module this pull is about, which organisation it benefits). The access
        # decision itself stays exactly where it was -- `resolve_provider_account_access`,
        # below, on the same actor -- and it is what answers allowed/denied.
        connection_scope = (
            background_connection(
                "queue.enqueue gate: the clock has no human identity, and it must "
                "read the Datastream it is about (AI-301)"
            )
            if _is_background_actor(requested_by)
            else request_connection(requested_by)
        )
        with connection_scope as conn:
            # Always strict since Story 46.4 removed the runtime flag; kept as a
            # name because the refusal branches below read it.
            strict_gate = True
            ref = _resolve_connection_ref(conn, connection_ref_id)
            if ref is None:
                # Unknown connection: let the normal path handle it (the worker
                # dead-letters a missing connection_ref). Not a topology refusal.
                return None
            provider = ref["provider"]
            # The credential names the authorization; only the datastream knows
            # which tool this pull reads. None means "nothing knows better" --
            # the legacy per-connection path and the 37 Nango connectors, whose
            # credential is their module -- so the provider stands in unchanged.
            #
            # AI-301: THE CALLER'S DECLARED MODULE WINS, and it is why this
            # argument exists. `_resolve_datastream_module` re-reads
            # `app.datastreams` on THIS connection, which is `request_connection`
            # -- role `connector`, RLS armed, and no organisation context. That
            # read returned 0 rows from 2026-08-12 on, so `module_name` fell back
            # to the credential's `google`, the registry has no module by that
            # name, and the fail-closed branch below refused EVERY Google pull at
            # enqueue for five days. The scheduler already holds `module_name` in
            # the row it selected under its own connection; re-deriving it here
            # asked a blinded connection a question its caller had answered.
            # Reading it again stays as the fallback for callers that pass none.
            resolved_module = module_name or _resolve_datastream_module(conn, datastream_id)
            module_name = resolved_module or provider

            from core import account_topology  # noqa: PLC0415

            topology = account_topology.get_topology_for_provider(module_name)
            if topology is None:
                # Legacy stays compatible; strict production refuses providers that
                # have not declared an exact account topology.
                return _strict_access_refusal() if strict_gate else None

            # The account this enqueue is ABOUT. Same rule as the extraction
            # (`_resolve_selected_account`): the Datastream's own binding first,
            # the authorization-wide verified scope only when it has none. Asking
            # the credential here while the pull asks the Datastream would gate
            # one account and then read another.
            # `connector=module_name`: the account space is the CONNECTOR's, not
            # the consent's (2026-08-30). Without it the fallback below picked the
            # freshest account of the whole Google grant and gated a Search Console
            # site for an Analytics pull.
            try:
                selected_account = _resolve_selected_account(
                    conn, connection_ref_id, datastream_id, connector=module_name
                )
            except AccountSelectionRefused as refused:
                logger.info(
                    "queue: enqueue_refused conn=%s reason=%s",
                    connection_ref_id,
                    refused.code,
                )
                return _account_selection_refusal(refused)
            if selected_account and account_topology.is_account_ready(
                connection_ref_id, selected_account, conn
            ):
                if not strict_gate:
                    return None
                with conn.cursor() as cur:
                    scope = (selected_account,)
                    if datastream_id:
                        cur.execute(
                            "SELECT org_id FROM app.datastreams WHERE id = %s",
                            (datastream_id,),
                        )
                    else:
                        cur.execute(
                            "SELECT org_id FROM app.projects WHERE id = %s",
                            (ref["project_id"],),
                        )
                    beneficiary = cur.fetchone()
                if not scope or not beneficiary:
                    return _strict_access_refusal()
                # AI-301: the clock answers with the Datastream's ACTIVATION, a
                # person with their membership. Same gate after the first link --
                # owner, health, scope, availability and exposure are re-read
                # unchanged either way. A clock without a datastream_id has no
                # activation to stand on and keeps the identity path, which
                # denies it: there is nothing to authorize the pull.
                if _is_background_actor(requested_by) and datastream_id:
                    decision = resolve_scheduled_account_access(
                        conn,
                        credential_id=connection_ref_id,
                        external_account_id=scope[0],
                        beneficiary_org_id=beneficiary[0],
                        datastream_id=datastream_id,
                    )
                else:
                    decision = resolve_provider_account_access(
                        requested_by,
                        conn,
                        credential_id=connection_ref_id,
                        external_account_id=scope[0],
                        beneficiary_org_id=beneficiary[0],
                        datastream_id=datastream_id,
                        project_id=None if datastream_id else ref["project_id"],
                    )
                if decision.allowed:
                    return None
                return _refusal_for_denial(decision, connection_ref_id, conn)
    except Exception as exc:  # noqa: BLE001
        # Uncertainty at an authorization seam is a denial. This used to be
        # fail-OPEN for legacy providers unless the Epic 36 gate was on; Story 46.4
        # retired that gate, so the refusal is unconditional and the nested
        # try/except that dressed it up as a choice is gone with it.
        logger.warning("queue: topology_guard_denied conn=%s: %s", connection_ref_id, exc)
        return _strict_access_refusal()

    logger.info("queue: enqueue_refused conn=%s reason=account_not_selected", connection_ref_id)
    return {
        "state": "refused",
        "code": "account_not_selected",
        "message": (
            "no reporting account is selected and verified for this connection; "
            "select an account before enqueuing a pull"
        ),
    }


def _strict_access_refusal() -> dict:
    """Return a deliberately non-disclosing production authorization denial."""
    return {
        "state": "refused",
        "code": "access_denied",
        "message": "connection/account scope is not authorized for this resource",
    }


#: The refusal code for a door closed by the connection's HEALTH, not by a right.
#:
#: AI-302: `access_denied` covered both, and they are not the same repair. "You
#: are not authorized for this account" is answered by a grant or an account
#: selection; "the last collection on this authorization landed nothing" is
#: answered by looking at that collection. Nine Datastreams were refused
#: `access_denied` for five days on 2026-08-12 and again on 2026-08-17, and the
#: operator had no way to tell which of the two they were reading -- so nobody
#: looked at the pull, because nothing said there was a pull to look at.
REFUSAL_CONNECTION_UNHEALTHY = "connection_unhealthy"


#: The typed error classes that are facts of HEALTH, and the status each writes.
#:
#: AI-341: the health poll reads local state only (token blob + expiry), so a
#: provider-side refusal is invisible to it -- the pull is where the truth is
#: learned, and for six days it kept that truth to itself while the health row
#: said `ok`. `revoked`: the authorization is dead, reconnecting is the only
#: gesture, the enqueue gate closes. `provider_denied` (migration 331): the
#: authorization is alive but the provider refuses the data -- the gate stays
#: OPEN, because the daily probe is the detector of restoration and a verified
#: `ok` pull lifts the red with no console gesture (execution-substrate.md,
#: Decided 2026-08-31).
_AUTH_RED_STATUS = {
    "auth_expired": "revoked",
    "auth_revoked": "revoked",
    "permission_denied": "provider_denied",
}


def _record_auth_red(conn, connection_ref_id: str, pull_id: str, error_class: str) -> None:
    """A pull refused on its AUTHORIZATION writes the health row, same transaction.

    The red names the pull that raised it (`provider_denied_pull_id` /
    `provider_denied_at`, the migration 276 rule), and every status this writes
    nulls the labels of the statuses it replaces -- a label may never describe a
    state the row no longer holds. Best-effort by design: a health row that
    cannot be written must not turn a recorded failure into a raised one.
    """
    status = _AUTH_RED_STATUS.get(error_class)
    if status is None or not connection_ref_id:
        return
    from datetime import datetime, timezone  # noqa: PLC0415

    denied_pull = pull_id if status == "provider_denied" else None
    denied_at = datetime.now(tz=timezone.utc) if denied_pull else None
    try:
        # A SAVEPOINT, because "best-effort" is a lie without one: a refused
        # write poisons the surrounding transaction, and what it would take
        # down is the recorded failure itself.
        with conn.cursor() as cur:
            cur.execute("SAVEPOINT auth_red")
            try:
                cur.execute(
                    """
                    INSERT INTO app.connection_health
                        (connection_ref_id, status, last_checked_at,
                         provider_denied_pull_id, provider_denied_at)
                    VALUES (%(id)s, %(status)s, NOW(), %(denied_pull)s, %(denied_at)s)
                    ON CONFLICT (connection_ref_id) DO UPDATE
                        SET status = EXCLUDED.status,
                            last_checked_at = EXCLUDED.last_checked_at,
                            provider_denied_pull_id = EXCLUDED.provider_denied_pull_id,
                            provider_denied_at = EXCLUDED.provider_denied_at,
                            populate_failed_pull_id = NULL,
                            populate_failed_verdict = NULL,
                            populate_failed_at = NULL
                    """,
                    {
                        "id": connection_ref_id,
                        "status": status,
                        "denied_pull": denied_pull,
                        "denied_at": denied_at,
                    },
                )
                cur.execute("RELEASE SAVEPOINT auth_red")
            except Exception:
                cur.execute("ROLLBACK TO SAVEPOINT auth_red")
                raise
    except Exception as exc:  # noqa: BLE001 -- the failure being recorded outranks this write
        logger.warning(
            "queue: auth_red_write_failed conn=%s class=%s: %s",
            connection_ref_id,
            error_class,
            exc,
        )


def _refusal_for_denial(decision, connection_ref_id: str, conn) -> dict:
    """Turn one denied AccessDecision into the refusal an OPERATOR can act on.

    Two codes, because there are two gestures. Everything that is a matter of
    RIGHTS stays `access_denied` and stays non-disclosing -- naming which of owner,
    grant, scope or exposure is missing would answer a question the caller has not
    proven they may ask. Health is different: it is the caller's OWN connection,
    the Fleet already shows it, and hiding it is what made five days illegible.
    """
    if getattr(decision, "reason", "") != "connection_unhealthy":
        return _strict_access_refusal()
    return _connection_unhealthy_refusal(connection_ref_id, conn)


def _connection_unhealthy_refusal(connection_ref_id: str, conn) -> dict:
    """The health refusal, quoting the pull that raised the red when there is one.

    THE MESSAGE NAMES THE GESTURE, never the cause. Three health states close this
    door and each has its own repair:

      * `populate_failed` -- a collection landed nothing (or too little) and the
        flag is sticky. Migration 276 records WHICH collection and WHEN, so the
        sentence sends the operator to that window instead of to a status.
      * `revoked` -- the authorization is dead. Reconnect it; no collection will
        revive it.
      * anything else, including no health row at all -- nothing has ever been
        checked. Refresh the connection.

    Reads the health row on the connection it was handed, and degrades to the
    generic health sentence if that read fails: a refusal must never raise.
    """
    status, pull_id, verdict, raised_at = "", "", "", None
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT status, populate_failed_pull_id, populate_failed_verdict,
                       populate_failed_at
                FROM app.connection_health WHERE connection_ref_id = %s
                """,
                (connection_ref_id,),
            )
            row = cur.fetchone()
        if row:
            status = str(row[0] or "")
            pull_id = str(row[1] or "")
            verdict = str(row[2] or "")
            raised_at = row[3]
    except Exception as exc:  # noqa: BLE001 -- a refusal never raises
        logger.warning(
            "queue: health_refusal_read_failed conn=%s: %s", connection_ref_id, exc
        )

    if status == "populate_failed":
        # This gate reads the STATUS, never the verdict -- the verdict only picks
        # the words. `partial` survives here for the reds raised before 2026-08-17
        # (AI-101): a sparse window no longer raises the sticky red, so no NEW
        # `populate_failed` can carry that verdict, and the historical ones must
        # keep saying what they actually saw.
        landed = "too few rows" if verdict == "partial" else "no rows"
        # Named only when the row HAS it. Reds raised before migration 276 carry
        # nothing, and inventing a collection to point at would be worse than the
        # silence it replaces.
        parts = [pull_id] if pull_id else []
        if pull_id and raised_at is not None:
            parts.append(getattr(raised_at, "date", lambda: raised_at)().isoformat())
        which = f" (collection {', '.join(parts)})" if parts else ""
        message = (
            f"the last collection on this connection landed {landed}{which}, so further "
            "collections are held back. Open that collection to see what it returned, "
            "then re-verify the reporting account on this connection to release it"
        )
    elif status == "revoked":
        message = (
            "this connection's authorization is no longer valid, so no collection can "
            "run on it. Reconnect it to release its Datastreams"
        )
    elif status == "provider_denied":
        # AI-341: normally this status leaves the door OPEN (the daily probe is
        # the detector of restoration), so this branch speaks only if a later
        # gate closes on it -- and then it must not borrow the `revoked`
        # sentence, because reconnecting the same account repairs nothing.
        message = (
            "the provider refuses this connection's access to the data it collects. "
            "Restore the connected account's access at the provider, or reconnect "
            "with an account that has it"
        )
    else:
        message = (
            "this connection has not been confirmed as reachable, so no collection can "
            "run on it. Refresh the connection to check it"
        )
    logger.warning(
        "queue: enqueue_refused conn=%s code=%s health=%s pull_id=%s",
        connection_ref_id,
        REFUSAL_CONNECTION_UNHEALTHY,
        status or "unknown",
        pull_id or "-",
    )
    return {
        "state": "refused",
        "code": REFUSAL_CONNECTION_UNHEALTHY,
        "message": message,
    }


def _backfill_ceiling_refusal(clamp: dict) -> dict:
    """Story 58.4: the requested window is entirely older than the entitlement.

    THE SENTENCE IS ACTIONABLE, which is the whole reason this is a refusal and
    not a silent clamp: it names the date the organisation's backfill reaches
    back to, so the person learns their day is outside their recovery window
    instead of watching a run start and produce nothing.

    The shape is the one every caller of `enqueue_pull` already branches on --
    `state: "refused"` is this function's ANSWER vocabulary and not a value
    `app.pull_jobs.state` may hold, which is why no row is written for it.
    """
    floor = clamp.get("floor")
    days = clamp.get("max_backfill_days")
    return {
        "state": "refused",
        "code": clamp.get("reason") or "window_before_backfill_ceiling",
        "message": (
            f"this organisation's backfill reaches back to {floor} "
            f"({days} days); the requested window ends on {clamp.get('date_to')}, "
            "which is before it, so no day of it can be collected"
        ),
    }


def enqueue_pull(
    connection_ref_id: str,
    date_from: str,
    date_to: str,
    *,
    requested_by: str,
    datastream_id: str | None = None,
    execution_id: str | None = None,
    module_name: str | None = None,
) -> dict:
    """Enqueue a pull job. Returns {"job_id", "pull_id", "state": "queued"}.

    Story 63.1: optional execution_id -- the run this window belongs to, so the
    worker can write where the run is when the window ends (migration 218).

    Story 8.2: optional datastream_id FK passthrough written to app.pull_jobs.
    The dedup index (uq_pull_jobs_active) is unchanged -- datastream_id is NOT
    part of the dedup key (same connection + window deduplicates regardless of
    which datastream triggered it).

    Story 25.5 (AC4): when the provider's manifest declares account_topology and
    the connection has no state='ready' account scope, the pull is REFUSED with a
    clear dict ({"state": "refused", "code": "account_not_selected", ...}) instead
    of being enqueued. Modules without the key keep their exact prior behaviour.

    AI-301: `module_name` is the module the CALLER already resolved. Pass it
    whenever you have it -- the gate below otherwise re-reads it on an RLS-armed
    connection that may not see the row, and an unresolved module fails closed.
    """
    refusal = _topology_scope_refusal(
        connection_ref_id,
        requested_by=requested_by,
        datastream_id=datastream_id,
        module_name=module_name,
    )
    if refusal is not None:
        return refusal
    # Story 34.3: honest backfill clamp for trial orgs (never a rejection).
    date_from, backfill_clamp = _clamp_trial_backfill(connection_ref_id, date_from, date_to)
    # Story 58.4: AND A WINDOW THAT KEEPS NO DAY IS NOT A WINDOW. The ceiling
    # reports the emptiness rather than deciding (its contract is unchanged); the
    # decision is here, because this is the function that would otherwise write
    # the row. It used to write `date_from > date_to` into `app.pull_jobs` and
    # nothing downstream refused it -- a run left on a line that means nothing.
    if backfill_clamp is not None and backfill_clamp.get("empty"):
        return _backfill_ceiling_refusal(backfill_clamp)
    result = _backend.enqueue_pull(
        connection_ref_id,
        date_from,
        date_to,
        requested_by=requested_by,
        datastream_id=datastream_id,
        execution_id=execution_id,
    )
    if backfill_clamp is not None and isinstance(result, dict):
        result["backfill_clamp"] = backfill_clamp
    return result


def _clamp_trial_backfill(
    connection_ref_id: str, date_from: str, date_to: str
) -> tuple[str, dict | None]:
    """Story 34.3: clamp date_from to the org's trial ceiling; never rejects.

    Returns (effective_date_from, clamp_info|None). clamp_info is a dict only when
    the window was actually reduced (so the caller can signal the clamp honestly);
    None means no clamp (full/internal, recent pull, or unresolved org). Fail-open
    on any error -- a governance read failure must not block a legitimate pull.
    """
    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.trial_enforcement import clamp_backfill_window  # noqa: PLC0415

        with get_connection() as conn:
            clamp = clamp_backfill_window(connection_ref_id, date_from, date_to, conn)
        if clamp.get("clamped"):
            return clamp["date_from"], clamp
        return date_from, None
    except Exception as exc:  # noqa: BLE001
        logger.warning("queue: backfill_clamp_skipped conn=%s: %s", connection_ref_id, exc)
        return date_from, None


def get_job_status(job_id: str) -> dict | None:
    """Return job row as dict, or None if not found."""
    return _backend.get_job_status(job_id)


def enqueue_activation_work(
    *,
    kind: str,
    project_id: str,
    correlation_id: str,
    payload: dict,
    requested_by: str,
    draft_id: str | None = None,
    datastream_id: str | None = None,
    execution_id: str | None = None,
    conn=None,
) -> dict:
    """Append exact setup/candidate work to the shared persistent queue."""
    if kind not in {"setup_preview", "candidate_materialization"}:
        raise ValueError("unsupported activation work kind")
    if not isinstance(payload, dict) or not project_id or not correlation_id or not requested_by:
        raise ValueError("activation work scope is incomplete")
    expected = (
        kind == "setup_preview" and draft_id and not datastream_id and not execution_id
    ) or (kind == "candidate_materialization" and not draft_id and datastream_id and execution_id)
    if not expected:
        raise ValueError("activation work scope does not match its kind")
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 16384:
        raise ValueError("activation work payload is too large")

    from ulid import ULID  # noqa: PLC0415

    from core.db import get_connection  # noqa: PLC0415

    if conn is None:
        with get_connection() as owned_connection:
            result = enqueue_activation_work(
                kind=kind,
                project_id=project_id,
                correlation_id=correlation_id,
                payload=payload,
                requested_by=requested_by,
                draft_id=draft_id,
                datastream_id=datastream_id,
                execution_id=execution_id,
                conn=owned_connection,
            )
            owned_connection.commit()
            return result

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id,state FROM app.datastream_activation_jobs "
            "WHERE kind=%s AND project_id=%s AND correlation_id=%s",
            (kind, project_id, correlation_id),
        )
        existing = cur.fetchone()
        if existing is not None:
            return {"job_id": existing[0], "state": existing[1], "replayed": True}
        job_id = f"dsaj_{ULID()}"
        cur.execute(
            """INSERT INTO app.datastream_activation_jobs
               (id,kind,project_id,draft_id,datastream_id,execution_id,
                correlation_id,payload,requested_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)""",
            (
                job_id,
                kind,
                project_id,
                draft_id,
                datastream_id,
                execution_id,
                correlation_id,
                encoded,
                requested_by,
            ),
        )
    return {"job_id": job_id, "state": QUEUED, "replayed": False}


def _dequeue_activation_job(conn) -> dict | None:
    """Claim one setup/candidate job using the pull queue's SKIP LOCKED discipline."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id,kind,project_id,draft_id,datastream_id,execution_id,
                      correlation_id,payload,requested_by,attempt_count
                 FROM app.datastream_activation_jobs
                WHERE state IN ('queued','failed') AND attempt_count < %s
                ORDER BY enqueued_at FOR UPDATE SKIP LOCKED LIMIT 1""",
            (_max_attempts(),),
        )
        row = cur.fetchone()
        if row is None:
            return None
        columns = [description[0] for description in cur.description]
        job = dict(zip(columns, row))
        cur.execute(
            "UPDATE app.datastream_activation_jobs SET state='running',"
            "attempt_count=attempt_count+1,started_at=NOW(),error_code=NULL WHERE id=%s",
            (job["id"],),
        )
    conn.commit()
    job["attempt_count"] = int(job["attempt_count"]) + 1
    return job


def _activation_error_code(exc: BaseException) -> str:
    """The code a dead letter carries: the exception's own, else its TYPE.

    This was `getattr(exc, "code", "activation_work_failed")`, so every untyped
    exception landed under one word. Measured 2026-08-12: 17 candidate jobs, 17
    dead letters, all reading `activation_work_failed` -- and the actual cause
    was a `TypeError` on a missing pull argument. One word for every unnamed
    failure is why nobody could see, from the table, that a whole class of jobs
    had never once succeeded. A typed exception still wins: `code` is the
    deliberate answer, this is only the fallback the generic replaced.
    """
    declared = getattr(exc, "code", None)
    if isinstance(declared, str) and declared.strip():
        return declared
    name = type(exc).__name__
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
    return snake or "activation_work_failed"


def _execute_activation_job(job: dict) -> None:
    """Run registered source I/O outside core and retain only safe proof."""
    from core.db import get_connection  # noqa: PLC0415

    try:
        from core.inbound_seam import resolve_inbound  # noqa: PLC0415

        install_runtime_activation_drivers = resolve_inbound(
            "install_runtime_activation_drivers"
        )
        execute_activation_job = resolve_inbound("execute_activation_job")

        # Mount the deployment's real drivers before claiming any work. The
        # adapter matrix is otherwise a set of refusals, and a mode whose driver
        # is missing must fail this job rather than reach a fabricated preview.
        install_runtime_activation_drivers()

        with get_connection() as conn:
            execute_activation_job(conn, job)
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE app.datastream_activation_jobs SET state='done',"
                    "completed_at=NOW(),error_code=NULL WHERE id=%s",
                    (job["id"],),
                )
            conn.commit()
    except Exception as exc:  # noqa: BLE001 -- bounded durable worker failure
        terminal = int(job.get("attempt_count") or 0) >= _max_attempts()
        error_code = _activation_error_code(exc)
        logger.exception("queue_worker: activation job failed id=%s", job.get("id"))
        if terminal and job.get("execution_id"):
            # AI-321 (2026-08-28): a dead-lettered candidate left its execution
            # `created` for ever, and `create_execution` then refused EVERY
            # later confirm of the Datastream with concurrent_execution_active
            # -- rendered 503 by the workbench door. The execution is the run
            # universe; a run whose job is dead is a FAILED run, said so.
            from core.datastream_publication import (  # noqa: PLC0415
                _fail_execution_out_of_band,
            )

            _fail_execution_out_of_band(
                str(job["execution_id"]),
                str(job.get("requested_by") or "system:activation-worker"),
                str(error_code)[:128],
                str(exc)[:500],
                get_connection,
            )
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE app.datastream_activation_jobs SET state=%s,error_code=%s,"
                    "completed_at=CASE WHEN %s THEN NOW() ELSE NULL END WHERE id=%s",
                    (
                        DEAD_LETTER if terminal else FAILED,
                        str(error_code)[:128],
                        terminal,
                        job["id"],
                    ),
                )
            conn.commit()


# ---------------------------------------------------------------------------
# Worker lifecycle (called from build_asgi_app)
# ---------------------------------------------------------------------------


def start_queue_worker() -> None:
    """Start the background queue worker thread (call once at server startup).

    Reads QUEUE_WORKER_ENABLED env var (default "true"). Set to "false" in CI
    and test environments to suppress the background thread.
    Only started for the "local" backend (Cloud Tasks uses push-based dispatch).

    Do NOT call at module level -- the thread must NOT start on import.
    """
    enabled = os.environ.get("QUEUE_WORKER_ENABLED", "true").lower()
    if enabled != "true":
        logger.info("queue_worker: disabled via QUEUE_WORKER_ENABLED=%s", enabled)
        return

    backend_env = os.environ.get("QUEUE_BACKEND", "local")
    if backend_env != "local":
        logger.info("queue_worker: skipped for QUEUE_BACKEND=%s (push-based)", backend_env)
        return

    # Double-start guard (mirrors health_poller.py review-2-5 F-03)
    if any(t.name == "queue-worker" for t in threading.enumerate()):
        logger.info("queue_worker: already running -- not starting a second thread")
        return

    t = threading.Thread(target=_worker_loop, daemon=True, name="queue-worker")
    t.start()
    logger.info(
        "queue_worker: started (interval=%ss max_attempts=%d)",
        _poll_interval(),
        _max_attempts(),
    )


def dispatch_after_commit(*job_ids: str | None) -> None:
    """Push every activation job named, AFTER the caller committed; never raises.

    AI-321 (2026-08-29). `enqueue_activation_work` writes the row inside the
    caller's transaction and the push task must follow the commit -- so every
    DOOR that mints a candidate has to remember the push. The wizard's doors
    did (`_dispatch_activation_task`, story 56.3); the workbench change door
    and the project-settings door (country fan-out) did not, so their candidates
    sat `queued` / `created` until the ten-minute reconciliation sweep -- and a
    person confirming a mapping saw a run that never started. One helper, every
    door: a forgotten push is survivable by design, a silent one is not.
    """
    for job_id in job_ids:
        if not job_id:
            continue
        try:
            dispatch_activation_task(str(job_id))
        except Exception as exc:  # noqa: BLE001 -- latency, not loss (see docstring)
            logger.warning(
                "activation dispatch skipped for job_id=%s: %s -- the row is committed "
                "and the reconciliation sweep will pick it up",
                job_id,
                exc,
            )
