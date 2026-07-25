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
import threading
import time

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# JobState constants (AC1)
# ---------------------------------------------------------------------------

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
DEAD_LETTER = "dead_letter"


class JobState:
    """Namespace for job state string constants."""

    QUEUED = QUEUED
    RUNNING = RUNNING
    DONE = DONE
    FAILED = FAILED
    DEAD_LETTER = DEAD_LETTER


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
    ) -> dict:
        """Mint job_id + pull_id, insert job row, write audit row. Returns job dict.

        AD-7: pull_id is minted here, before the DB insert.

        Story 8.2: optional datastream_id FK passthrough -- written to pull_jobs when
        provided by the scheduler dispatch (per-datastream mode) or the /run endpoint.
        The dedup index (uq_pull_jobs_active on connection_ref_id, date_from, date_to)
        is unchanged; datastream_id is NOT part of the dedup key.
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
            # (same connection + window, not yet terminal) is returned as-is
            # instead of double-queueing (double-click => one job, one spend).
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, pull_id, state FROM app.pull_jobs
                    WHERE connection_ref_id = %s AND date_from = %s AND date_to = %s
                      AND state IN ('queued', 'running')
                    LIMIT 1
                    """,
                    (connection_ref_id, date_from, date_to),
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
                         state, requested_by, trace_id, datastream_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (connection_ref_id, date_from, date_to)
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
                    ),
                )
                inserted_id = cur.fetchone()

            if inserted_id is None:
                # Another concurrent INSERT won the race; fetch its row.
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT id, pull_id, state FROM app.pull_jobs
                        WHERE connection_ref_id = %s AND date_from = %s AND date_to = %s
                          AND state IN ('queued', 'running')
                        LIMIT 1
                        """,
                        (connection_ref_id, date_from, date_to),
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
                      requested_by, attempt_count, trace_id, datastream_id
            """,
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
                    RETURNING id
                    """,
                    (_max_attempts(), max_running_seconds),
                )
                recovered = len(cur.fetchall())
            conn.commit()
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
        logger.warning(
            "queue: resolve_datastream_profile_failed ds=%s: %s", datastream_id, exc
        )
        return ""


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
    encoded = _dump(
        str(message)[:500], None if provider_payload is None else "<truncated>"
    )
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


def _resolve_catalog_selection(
    provider: str,
    capability_report: dict,
    job: dict,
) -> dict | None:
    """Resolve a validated selection for a ``catalog_driven`` profile (Story 25.8).

    Returns:
      - ``None`` when the profile is NOT catalog_driven (unchanged dispatch —
        exact_bundle profiles are bit-identical: the pull is called with no
        ``selection=`` kwarg).
      - a resolved selection dict ``{"metrics","dimensions","source_fields"}``
        when the profile IS catalog_driven. The selection comes from the job
        payload key ``"selection"`` when present; absent -> the catalog's
        tier-core default (Dev Notes: a job without selection uses tier-core
        defaults from the catalog).

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

    module_dir = _Path(__file__).parent.parent / "modules" / provider
    catalog = load_catalog(module_dir)
    if catalog is None:
        raise InvalidRequestError(
            provider_status=None,
            provider_payload=None,
            message=(
                f"catalog_driven profile requires api_catalog.json for provider "
                f"{provider!r} but none was found"
            ),
        )

    selection = job.get("selection")  # None today; wired by the datastream path later
    resolved, issues = validate_selection(catalog, selection)
    if issues:
        raise InvalidRequestError(
            provider_status=None,
            provider_payload=None,
            message="; ".join(issue.message for issue in issues),
        )
    return resolved


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
    from core.audit import (  # noqa: PLC0415
        ACTION_PULL_COMPLETED,
        ACTION_PULL_FAILED,
        write_audit_row,
    )
    from core.db import get_connection  # noqa: PLC0415
    from core.main import get_module_pull_fn  # noqa: PLC0415
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

    provider = pre_ref["provider"] if pre_ref else None

    if provider is not None:
        read_cost = quota.get_read_cost(provider)
        can_proceed, reason = quota.pre_check(provider, read_cost)
        if not can_proceed:
            # Re-queue without incrementing attempt_count (AC3, AC4).
            # review-3-2 F-1: the atomic claim already moved the job to
            # 'running', so a quota block must explicitly RETURN it to the
            # queue (previously it had never left 'queued').
            logger.info(
                "quota_blocked: job=%s platform=%s reason=%s",
                job_id,
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
            "job.connector": provider or "",
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
                # Connection was deleted -- dead letter immediately
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE app.pull_jobs
                        SET state = 'dead_letter',
                            error_detail = %s,
                            completed_at = now()
                        WHERE id = %s
                        """,
                        (f"connection_ref '{connection_ref_id}' not found", job_id),
                    )
                conn.commit()
                logger.error(
                    "queue: job_id=%s dead_letter -- connection_ref not found: %s",
                    job_id,
                    connection_ref_id,
                )
                return True

            provider = ref["provider"]
            nango_connection_id = ref["nango_connection_id"]
            project_id = ref["project_id"]

            # Look up read_cost for post-success record_spend
            read_cost = quota.get_read_cost(provider)

            # AD-2: call pull function via registry, never import from modules directly.
            # Story 10.1 (Epic 10): profile-aware dispatch -- resolve the datastream's
            # report_profile_id so a non-default profile (e.g. GA4 'user_type_daily')
            # routes to its per-profile pull function (pull_user_type_daily). Falls back
            # to the default pull() when the datastream has no non-default profile (or
            # the job carries no datastream_id -- legacy per-connection path).
            _profile_id = _resolve_datastream_profile(conn, job.get("datastream_id"))
            pull_fn = get_module_pull_fn(provider, profile_id=_profile_id)
            if pull_fn is None:
                error_msg = f"no pull function registered for provider '{provider}'"
                with conn.cursor() as cur:
                    new_state = DEAD_LETTER if attempt_count >= max_att else FAILED
                    cur.execute(
                        """
                        UPDATE app.pull_jobs
                        SET state = %s,
                            error_detail = %s,
                            completed_at = now()
                        WHERE id = %s
                        """,
                        (new_state, error_msg, job_id),
                    )
                conn.commit()
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
                _manifest = _get_manifest_for_provider(provider) or {}
                _profiles = _manifest.get("report_profiles") or [{}]
                # Story 10.1 F-1: extraction_path and dispatch args must follow the
                # ACTIVE profile resolved above (_profile_id via
                # _resolve_datastream_profile), not report_profiles[0]. When
                # _profile_id is None (legacy standard job) the next(...) falls back
                # to report_profiles[0] -- standard_daily behaviour preserved.
                if _profile_id is None:
                    _active_profile = _profiles[0] if _profiles else {}
                else:
                    _active_profile = next(
                        (p for p in _profiles if p.get("id") == _profile_id), {}
                    )
                _extraction = _active_profile.get("extraction_path", "custom_pull")
                # Story 25.8: catalog_driven profiles resolve+validate a field
                # selection from the catalog and pass it to the pull as selection=.
                # exact_bundle profiles return None here and stay bit-identical
                # (no selection= kwarg). A drifted selection raises
                # InvalidRequestError -> the ConnectorError branch below.
                _capability_report = _capability_report_for_profile(_manifest, _profile_id)
                _selection = _resolve_catalog_selection(provider, _capability_report, job)
                if _extraction == "airbyte_sync":
                    from core.loader import dispatch_pull  # noqa: PLC0415
                    from core.main import get_loaded_modules  # noqa: PLC0415

                    result = dispatch_pull(
                        module_name=provider,
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
                    )
                else:
                    result = pull_fn(
                        connection_id=nango_connection_id,
                        date_from=date_from,
                        date_to=date_to,
                        project_id=project_id,
                        pull_id=pull_id,
                    )
                row_count = result.get("row_count", 0) if isinstance(result, dict) else 0

                # Story 3.3 (AC3): record spend after successful pull
                if read_cost > 0:
                    quota.record_spend(provider, read_cost)

                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE app.pull_jobs
                        SET state = 'done',
                            completed_at = now()
                        WHERE id = %s
                        """,
                        (job_id,),
                    )
                conn.commit()

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
                try:
                    from core.context_events import resolve_landing  # noqa: PLC0415
                    from core.verification import run_post_pull_verification  # noqa: PLC0415

                    manifest = _get_manifest_for_provider(provider)
                    _landing = resolve_landing(_active_profile, manifest.get("module_kind"))
                    if _landing == "context_events":
                        logger.info(
                            "queue: skipping populate verification for context_events "
                            "landing pull_id=%s provider=%s profile=%s (landing=%s)",
                            pull_id,
                            provider,
                            _active_profile.get("id", ""),
                            _landing,
                        )
                    else:
                        # review-epic-8 #7: thread the pull's rejected_rows count
                        # (generic connector reports unparseable-date rejects) into
                        # verification so the dq_date_format monitor has a signal.
                        _rejected = (
                            result.get("rejected_rows", 0)
                            if isinstance(result, dict)
                            else 0
                        )
                        run_post_pull_verification(
                            pull_id=pull_id,
                            connection_ref_id=connection_ref_id,
                            date_from=date_from,
                            date_to=date_to,
                            manifest=manifest,
                            provider=provider,
                            rejected_rows=_rejected,
                            project_id=project_id,  # Story 24.3: route count to org schema
                        )
                except Exception as exc:
                    logger.warning("queue: verification_hook_failed: %s", exc)

            except RateLimitError as exc:
                # Story 3.3 (AC3, AC4): trip breaker, then dead-letter if exhausted.
                # G-12: without this guard the job requeues forever -- after max_att
                # rate-limit attempts it must die rather than loop indefinitely.
                quota.record_rate_limit(exc.platform, exc.retry_after)
                if attempt_count >= max_att:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            UPDATE app.pull_jobs
                            SET state = 'dead_letter',
                                error_detail = %s,
                                completed_at = now()
                            WHERE id = %s
                            """,
                            (
                                f"rate_limit_exhausted after {attempt_count} attempts: "
                                f"{str(exc)[:1900]}",
                                job_id,
                            ),
                        )
                    conn.commit()
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
                if exc.error_class == "invalid_request":
                    logger.warning(
                        '{"event": "pull_invalid_request_drift", "job_id": "%s",'
                        ' "provider": "%s", "provider_status": %s}',
                        job_id,
                        provider,
                        exc.provider_status,
                    )

                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE app.pull_jobs
                        SET state = %s,
                            error_detail = %s,
                            completed_at = now()
                        WHERE id = %s
                        """,
                        (new_state, error_detail, job_id),
                    )
                conn.commit()

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

                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE app.pull_jobs
                        SET state = %s,
                            error_detail = %s,
                            completed_at = now()
                        WHERE id = %s
                        """,
                        (new_state, error_detail, job_id),
                    )
                conn.commit()

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


def _get_manifest_for_provider(provider: str) -> dict:
    """Return the manifest dict for the named provider from the loaded module registry.

    Uses a local import of core.main._loaded_modules (core->core, not AD-2 violation).
    _loaded_modules is populated once at startup and never mutated by the worker thread.

    Returns {} (empty dict) if the provider is not loaded; compute_expected_rows()
    uses days as its safe fallback when the manifest has no report_profiles.

    Story 3.5 (AC3, T4.2).
    """
    try:
        from core.main import _loaded_modules  # noqa: PLC0415

        for loaded in _loaded_modules:
            if loaded.name == provider:
                return loaded.manifest
    except Exception:
        pass
    return {}


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
                # No job found -- reset backoff (quota cleared or nothing queued)
                consecutive_quota_blocks = 0

        except Exception:
            logger.exception("queue_worker: poll cycle failed")
            consecutive_quota_blocks = 0

        # Story 3.3 (AC4): exponential backoff when quota-blocked
        if consecutive_quota_blocks > 0:
            max_bk = _max_backoff()
            sleep_secs = min(interval * (2 ** consecutive_quota_blocks), max_bk)
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
    """Cloud Tasks backend -- interface-complete, tested with mocks only (Phase B, AC8).

    At QUEUE_BACKEND=cloud_tasks:
      - enqueue_pull() still inserts the app.pull_jobs row (status endpoint).
      - Then enqueues a Cloud Tasks HTTP task pointing at the worker URL.
    HG-1: raises EnvironmentError if CLOUD_TASKS_PROJECT is unset at enqueue time.
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
        _tasks_client=None,
    ) -> dict:
        """Insert job row then dispatch Cloud Tasks HTTP task.

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

        # TODO(AI-17): replace with psycopg_pool.ConnectionPool at Epic 3 scale
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.pull_jobs
                        (id, pull_id, connection_ref_id, date_from, date_to,
                         state, requested_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        job_id,
                        pull_id,
                        connection_ref_id,
                        date_from,
                        date_to,
                        QUEUED,
                        requested_by,
                    ),
                )
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

        # Dispatch Cloud Tasks HTTP task
        location = os.environ.get("CLOUD_TASKS_LOCATION", "")
        queue_name = os.environ.get("CLOUD_TASKS_QUEUE_NAME", "")
        worker_url = os.environ.get("CLOUD_TASKS_WORKER_URL", "")

        tasks_client = _tasks_client or self._tasks_client
        if tasks_client is None:
            # Real client (Phase B only -- requires google-cloud-tasks installed)
            try:
                from google.cloud import tasks_v2  # noqa: PLC0415

                tasks_client = tasks_v2.CloudTasksClient()
            except ImportError as exc:
                raise EnvironmentError(
                    "google-cloud-tasks is not installed; add it to pyproject.toml for Phase B"
                ) from exc

        parent = tasks_client.queue_path(project, location, queue_name)
        task_url = f"{worker_url}/internal/worker/execute-pull/{job_id}"
        task = {
            "http_request": {
                "http_method": "POST",
                "url": task_url,
            }
        }
        tasks_client.create_task(request={"parent": parent, "task": task})

        logger.info(
            "queue: cloud_tasks enqueued job_id=%s url=%s", job_id, task_url
        )

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


def _topology_scope_refusal(
    connection_ref_id: str,
    *,
    requested_by: str,
    datastream_id: str | None = None,
) -> dict | None:
    """Story 25.5 (AC4): refuse the enqueue when a topology-declaring provider has
    no ready account scope, else return None (proceed).

    Returns a refusal DICT (never raises) mirroring the enqueue_pull return shape
    so callers branch on it exactly like the deduplicated case:
        {"state": "refused", "code": "account_not_selected", "message": ...}

    Backward compatible: a provider whose manifest does NOT declare
    account_topology is untouched (None -> proceed). Any failure to resolve the
    manifest also returns None -- the guard NEVER blocks the 12 current modules
    that have not yet declared topology (a resolution error must not become a
    silent refusal for them).
    """
    try:
        from core.db import get_connection, set_local_access_context  # noqa: PLC0415
        from core.project_access import (  # noqa: PLC0415
            epic36_production_access_enabled,
            resolve_provider_account_access,
        )

        # TODO(AI-17): replace with psycopg_pool.ConnectionPool at Epic 3 scale
        with get_connection() as conn:
            strict_gate = epic36_production_access_enabled()
            if strict_gate:
                set_local_access_context(conn, requested_by, enforce_epic36=True)
            ref = _resolve_connection_ref(conn, connection_ref_id)
            if ref is None:
                # Unknown connection: let the normal path handle it (the worker
                # dead-letters a missing connection_ref). Not a topology refusal.
                return None
            provider = ref["provider"]

            from core import account_topology  # noqa: PLC0415

            topology = account_topology.get_topology_for_provider(provider)
            if topology is None:
                # Legacy stays compatible; strict production refuses providers that
                # have not declared an exact account topology.
                return _strict_access_refusal() if strict_gate else None

            if account_topology.has_ready_scope(connection_ref_id, conn):
                if not strict_gate:
                    return None
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT account_id FROM app.connection_account_scope "
                        "WHERE connection_ref_id = %s AND state = 'ready'",
                        (connection_ref_id,),
                    )
                    scope = cur.fetchone()
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
                decision = resolve_provider_account_access(
                    requested_by,
                    conn,
                    credential_id=connection_ref_id,
                    external_account_id=scope[0],
                    beneficiary_org_id=beneficiary[0],
                    datastream_id=datastream_id,
                    project_id=None if datastream_id else ref["project_id"],
                )
                return None if decision.allowed else _strict_access_refusal()
    except Exception as exc:  # noqa: BLE001
        # Legacy providers stay fail-open. Once the Epic 36 gate is enabled, any
        # uncertainty at this authorization seam is a denial.
        logger.warning(
            "queue: topology_guard_skipped conn=%s: %s", connection_ref_id, exc
        )
        try:
            from core.project_access import epic36_production_access_enabled  # noqa: PLC0415

            if epic36_production_access_enabled():
                return _strict_access_refusal()
        except Exception:  # noqa: BLE001
            return _strict_access_refusal()
        return None

    logger.info(
        "queue: enqueue_refused conn=%s reason=account_not_selected", connection_ref_id
    )
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


def enqueue_pull(
    connection_ref_id: str,
    date_from: str,
    date_to: str,
    *,
    requested_by: str,
    datastream_id: str | None = None,
) -> dict:
    """Enqueue a pull job. Returns {"job_id", "pull_id", "state": "queued"}.

    Story 8.2: optional datastream_id FK passthrough written to app.pull_jobs.
    The dedup index (uq_pull_jobs_active) is unchanged -- datastream_id is NOT
    part of the dedup key (same connection + window deduplicates regardless of
    which datastream triggered it).

    Story 25.5 (AC4): when the provider's manifest declares account_topology and
    the connection has no state='ready' account scope, the pull is REFUSED with a
    clear dict ({"state": "refused", "code": "account_not_selected", ...}) instead
    of being enqueued. Modules without the key keep their exact prior behaviour.
    """
    refusal = _topology_scope_refusal(
        connection_ref_id,
        requested_by=requested_by,
        datastream_id=datastream_id,
    )
    if refusal is not None:
        return refusal
    # Story 34.3: honest backfill clamp for trial orgs (never a rejection).
    date_from, backfill_clamp = _clamp_trial_backfill(connection_ref_id, date_from, date_to)
    result = _backend.enqueue_pull(
        connection_ref_id,
        date_from,
        date_to,
        requested_by=requested_by,
        datastream_id=datastream_id,
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
        logger.warning(
            "queue: backfill_clamp_skipped conn=%s: %s", connection_ref_id, exc
        )
        return date_from, None


def get_job_status(job_id: str) -> dict | None:
    """Return job row as dict, or None if not found."""
    return _backend.get_job_status(job_id)


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
