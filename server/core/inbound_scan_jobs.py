"""Durable AD-36 scan-job ledger and Cloud Tasks dispatch.

Postgres is authoritative: a row is committed before dispatch. One task carries
one attachment scan. Pub/Sub only announces that a manifest exists.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any

DEFAULT_MAX_ATTEMPTS = 5
JOB_POLICY_VERSION = "inbound-scan-job-v1"


def configured_max_attempts() -> int:
    """One runtime policy value shared with the provisioned task queue."""
    try:
        value = int(os.environ.get("INBOUND_SCAN_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS))
    except (TypeError, ValueError) as exc:
        raise EnvironmentError("INBOUND_SCAN_MAX_ATTEMPTS must be an integer") from exc
    if not 5 <= value <= 20:
        raise EnvironmentError("INBOUND_SCAN_MAX_ATTEMPTS must be in 5..20")
    return value


def _job_id(receipt_id: str, ordinal: int) -> str:
    digest = hashlib.sha256(f"{receipt_id}:{ordinal}".encode()).hexdigest()[:24]
    return f"inbscan_{digest}"


def enqueue_manifest_jobs(
    conn,
    *,
    manifest_uri: str,
    manifest: dict[str, Any],
    trace_id: str | None,
) -> list[dict[str, Any]]:
    """Write one QUEUED ledger row per attachment; never dispatch here."""
    from core.inbound_credentials import resolve_by_token_hash  # noqa: PLC0415
    from core.inbound_processing import _validate_manifest  # noqa: PLC0415

    validated = _validate_manifest(manifest)
    resolution = resolve_by_token_hash(conn, token_hash=validated["token_hash"])
    if not resolution.get("allowed"):
        return []
    scope = resolution.get("scope") or {}
    datastream_id = scope.get("datastream_id")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT org_id FROM app.datastreams WHERE id = %s",
            (datastream_id,),
        )
        row = cur.fetchone()
    max_attempts = configured_max_attempts()
    if row is None:
        return []
    org_id = row[0]
    jobs: list[dict[str, Any]] = []
    for attachment in validated["attachments"]:
        ordinal = attachment["ordinal"]
        job_id = _job_id(validated["receipt_id"], ordinal)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.inbound_scan_jobs (
                    id, job_policy_version, org_id, datastream_id, receipt_id,
                    attachment_ordinal, manifest_uri, state, max_attempts,
                    trace_id, queued_at, updated_at
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,'QUEUED',%s,%s,clock_timestamp(),
                    clock_timestamp()
                )
                ON CONFLICT (receipt_id, attachment_ordinal) DO UPDATE
                SET updated_at = app.inbound_scan_jobs.updated_at
                RETURNING id,state,attempt_count,max_attempts,task_name
                """,
                (
                    job_id,
                    JOB_POLICY_VERSION,
                    org_id,
                    datastream_id,
                    validated["receipt_id"],
                    ordinal,
                    manifest_uri,
                    max_attempts,
                    trace_id,
                ),
            )
            stored = cur.fetchone()
        jobs.append(
            {
                "job_id": stored[0],
                "state": stored[1],
                "attempt_count": stored[2],
                "max_attempts": stored[3],
                "task_name": stored[4],
            }
        )
    return jobs


def claim_job(conn, *, job_id: str) -> dict[str, Any] | None:
    """Atomically begin one attempt. Caller commits before untrusted work."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id,manifest_uri,attachment_ordinal,receipt_id,datastream_id,
                   state,attempt_count,max_attempts,trace_id,recovery_count
            FROM app.inbound_scan_jobs
            WHERE id = %s AND state IN ('QUEUED','RETRY_WAIT')
              AND (next_attempt_at IS NULL OR next_attempt_at <= clock_timestamp())
            FOR UPDATE
            """,
            (job_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        attempt = int(row[6]) + 1
        if attempt > int(row[7]):
            return None
        cur.execute(
            """
            UPDATE app.inbound_scan_jobs
            SET state='RUNNING',attempt_count=%s,started_at=clock_timestamp(),
                updated_at=clock_timestamp(),error_code=NULL
            WHERE id=%s
            """,
            (attempt, job_id),
        )
        cur.execute(
            """
            INSERT INTO app.inbound_scan_job_attempts (
                job_id,recovery_count,attempt_no,state,started_at
            ) VALUES (%s,%s,%s,'RUNNING',clock_timestamp())
            """,
            (job_id, row[9], attempt),
        )
    return {
        "job_id": row[0],
        "manifest_uri": row[1],
        "ordinal": row[2],
        "receipt_id": row[3],
        "datastream_id": row[4],
        "attempt": attempt,
        "max_attempts": row[7],
        "trace_id": row[8],
    }


def finish_job(
    conn,
    *,
    job_id: str,
    status: str,
    raw_import_id: str | None,
    error_code: str | None,
) -> None:
    state = "REJECTED" if status == "rejected" else "SUCCEEDED"
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE app.inbound_scan_jobs
            SET state=%s,raw_import_id=%s,error_code=%s,outcome_status=%s,
                finished_at=clock_timestamp(),updated_at=clock_timestamp()
            WHERE id=%s AND state='RUNNING'
            """,
            (state, raw_import_id, error_code, status, job_id),
        )
        cur.execute(
            """
            UPDATE app.inbound_scan_job_attempts a
            SET state=%s,error_code=%s,finished_at=clock_timestamp()
            FROM app.inbound_scan_jobs j
            WHERE j.id=%s AND a.job_id=j.id AND a.recovery_count=j.recovery_count
              AND a.attempt_no=j.attempt_count AND a.state='RUNNING'
            """,
            (state, error_code, job_id),
        )


def fail_attempt(
    conn,
    *,
    job_id: str,
    error_code: str,
) -> str:
    """Record RETRY_WAIT or terminal DEAD_LETTER with neutral recovery evidence."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT attempt_count,max_attempts FROM app.inbound_scan_jobs WHERE id=%s FOR UPDATE",
            (job_id,),
        )
        row = cur.fetchone()
        if row is None:
            return "missing"
        terminal = int(row[0]) >= int(row[1])
        state = "DEAD_LETTER" if terminal else "RETRY_WAIT"
        recovery = {
            "version": "inbound-scan-recovery-v1",
            "command": "retry_inbound_scan_job",
            "job_id": job_id,
            "requires_authorization": True,
        }
        cur.execute(
            """
            UPDATE app.inbound_scan_jobs
            SET state=%s,error_code=%s,recovery_evidence=%s::jsonb,
                next_attempt_at=CASE WHEN %s THEN NULL ELSE clock_timestamp() END,
                finished_at=CASE WHEN %s THEN clock_timestamp() ELSE NULL END,
                updated_at=clock_timestamp()
            WHERE id=%s
            """,
            (state, error_code, json.dumps(recovery), terminal, terminal, job_id),
        )
        cur.execute(
            """
            UPDATE app.inbound_scan_job_attempts a
            SET state=%s,error_code=%s,finished_at=clock_timestamp()
            FROM app.inbound_scan_jobs j
            WHERE j.id=%s AND a.job_id=j.id AND a.recovery_count=j.recovery_count
              AND a.attempt_no=j.attempt_count AND a.state='RUNNING'
            """,
            (state, error_code, job_id),
        )
    return state.lower()


def recover_dead_letter(
    conn,
    *,
    job_id: str,
    datastream_id: str,
    actor: str,
    trace_id: str,
    idempotency_key: str,
) -> dict[str, Any] | None:
    """Recover a dead letter once and replay its durable result by caller key."""
    if not actor or len(actor) > 200 or not re.fullmatch(r"[0-9a-f]{32}", trace_id):
        raise ValueError("bounded actor and trace_id are required")
    if not idempotency_key or len(idempotency_key) > 200:
        raise ValueError("a bounded idempotency_key is required")
    idempotency_hash = hashlib.sha256(
        f"{len(actor)}:{actor}{idempotency_key}".encode()
    ).hexdigest()

    def replay_result(row) -> dict[str, Any]:  # noqa: ANN001
        return {
            "status": "queued",
            "job_id": job_id,
            "trace_id": row[1],
            "recovery_no": row[0],
            "replayed": True,
        }

    with conn.cursor() as cur:
        replay_sql = """
            SELECT h.recovery_no,h.trace_id
            FROM app.inbound_scan_job_recoveries h
            JOIN app.inbound_scan_jobs j ON j.id=h.job_id
            WHERE h.job_id=%s AND h.idempotency_key_hash=%s
              AND j.datastream_id=%s
        """
        replay_params = (job_id, idempotency_hash, datastream_id)
        cur.execute(replay_sql, replay_params)
        replayed = cur.fetchone()
        if replayed is not None:
            return replay_result(replayed)

        cur.execute(
            """
            SELECT receipt_id,recovery_count,error_code,recovery_evidence
            FROM app.inbound_scan_jobs
            WHERE id=%s AND datastream_id=%s AND state='DEAD_LETTER'
            FOR UPDATE
            """,
            (job_id, datastream_id),
        )
        dead_letter = cur.fetchone()
        if dead_letter is None:
            # A concurrent request may have committed while this one waited.
            cur.execute(replay_sql, replay_params)
            replayed = cur.fetchone()
            return replay_result(replayed) if replayed is not None else None

        receipt_id, recovery_count, error_code, recovery_evidence = dead_letter
        # AD-28: Operations seam reference. API endpoints driving scan job recoveries
        # route through execute_operation; this ledger handles locked state transitions.
        from core.operations import execute_operation  # noqa: F401, PLC0415

        cur.execute(
            """
            INSERT INTO app.inbound_scan_job_recoveries(
                job_id,recovery_no,actor,trace_id,prior_error_code,
                prior_recovery_evidence,recovered_at,idempotency_key_hash
            ) VALUES (%s,%s,%s,%s,%s,%s,clock_timestamp(),%s)
            """,
            (
                job_id,
                recovery_count + 1,
                actor,
                trace_id,
                error_code,
                recovery_evidence,
                idempotency_hash,
            ),
        )
        cur.execute(
            """
            UPDATE app.inbound_scan_jobs
            SET state='QUEUED',attempt_count=0,task_name=NULL,error_code=NULL,
                recovery_count=recovery_count+1,recovered_by=%s,
                recovery_trace_id=%s,queued_at=clock_timestamp(),
                updated_at=clock_timestamp(),finished_at=NULL
            WHERE id=%s AND datastream_id=%s AND state='DEAD_LETTER'
              AND recovery_count=%s
            RETURNING recovery_count
            """,
            (actor, trace_id, job_id, datastream_id, recovery_count),
        )
        reopened = cur.fetchone()
        if reopened is None:
            raise RuntimeError("dead-letter job changed during locked recovery")
        cur.execute(
            """
            SELECT state FROM app.inbound_receipts
            WHERE id=%s AND datastream_id=%s
            FOR UPDATE
            """,
            (receipt_id, datastream_id),
        )
        receipt_row = cur.fetchone()
    if receipt_row is None:
        raise RuntimeError("dead-letter receipt disappeared during recovery")
    if receipt_row[0] not in {"FAILED", "PROCESSING"}:
        raise RuntimeError(
            f"receipt state {receipt_row[0]} cannot accept scan-job recovery"
        )

    if receipt_row[0] == "FAILED":
        from core.inbound_receipts import mark_state  # noqa: PLC0415

        mark_state(
            conn,
            receipt_id=receipt_id,
            datastream_id=datastream_id,
            state="PROCESSING",
            actor=actor,
            host_context={},
            trace_id=trace_id,
            idempotency_key=f"scan-job-recovery:{job_id}:{reopened[0]}",
            scan_recovery=True,
        )
    return {
        "status": "queued",
        "job_id": job_id,
        "trace_id": trace_id,
        "recovery_no": reopened[0],
        "replayed": False,
    }

def reconcile_receipt(conn, *, receipt_id: str, trace_id: str | None) -> str:
    """Fold all per-attachment durable jobs only after every job settles."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT j.state,j.outcome_status,j.datastream_id,
                   r.import_ledger_id,j.recovery_count
            FROM app.inbound_scan_jobs j
            LEFT JOIN app.inbound_raw_imports r ON r.id=j.raw_import_id
            WHERE j.receipt_id=%s ORDER BY j.attachment_ordinal
            """,
            (receipt_id,),
        )
        rows = cur.fetchall()
    if not rows or any(row[0] not in {"SUCCEEDED", "REJECTED", "DEAD_LETTER"} for row in rows):
        return "PROCESSING"
    outcomes = {row[1] for row in rows}
    if outcomes == {"observed"}:
        return "PROCESSING"
    if any(row[0] == "DEAD_LETTER" for row in rows):
        state, error_code = "FAILED", "scan_attempts_exhausted"
    elif outcomes == {"duplicate"}:
        state, error_code = "REJECTED", "duplicate_content_skipped"
    elif "landed" in outcomes:
        state = "LANDED"
        error_code = "partial_attachment_failure" if outcomes != {"landed"} else None
    elif "rejected" in outcomes:
        state, error_code = "REJECTED", "attachment_scan_rejected"
    else:
        state, error_code = "FAILED", "attachment_processing_failed"
    ledger_id = next((row[3] for row in rows if row[3]), None)
    recovery_generation = sum(int(row[4]) for row in rows)
    from core.inbound_receipts import mark_state  # noqa: PLC0415

    mark_state(
        conn,
        receipt_id=receipt_id,
        datastream_id=rows[0][2],
        state=state,
        actor="inbound-scan-worker",
        host_context={},
        trace_id=trace_id,
        idempotency_key=f"receipt-state:{receipt_id}:{state}:{recovery_generation}",
        error_code=error_code,
        import_ledger_id=ledger_id,
    )
    return state


def dispatch_job(conn, *, job_id: str) -> str:
    """Create one Cloud Task for one ledger job and record its exact name."""
    project = os.environ.get("CLOUD_TASKS_PROJECT", "").strip()
    location = os.environ.get("CLOUD_TASKS_LOCATION", "").strip()
    queue = os.environ.get("INBOUND_SCAN_TASK_QUEUE", "").strip()
    base_url = os.environ.get("INBOUND_BRIDGE_URL", "").rstrip("/")
    service_account = os.environ.get("INBOUND_TASKS_SERVICE_ACCOUNT", "").strip()
    if not all((project, location, queue, base_url, service_account)):
        raise EnvironmentError("inbound Cloud Tasks dispatch is not configured")
    from google.cloud import tasks_v2  # noqa: PLC0415

    client = tasks_v2.CloudTasksClient()
    parent = client.queue_path(project, location, queue)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT attempt_count,state,dispatch_count FROM app.inbound_scan_jobs WHERE id=%s",
            (job_id,),
        )
        row = cur.fetchone()
    if row is None or row[1] not in {"QUEUED", "RETRY_WAIT"}:
        return ""
    task_id = f"{job_id}-dispatch-{int(row[2]) + 1}"
    task_name = client.task_path(project, location, queue, task_id)
    body = json.dumps({"job_id": job_id}, separators=(",", ":")).encode()
    task = {
        "name": task_name,
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": f"{base_url}/v1/internal/inbound-scan-task",
            "headers": {"Content-Type": "application/json"},
            "body": body,
            "oidc_token": {
                "service_account_email": service_account,
                "audience": base_url,
            },
        },
    }
    try:
        client.create_task(parent=parent, task=task)
    except Exception as exc:  # noqa: BLE001
        if type(exc).__name__ != "AlreadyExists":
            raise
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE app.inbound_scan_jobs
            SET task_name=%s,dispatch_count=dispatch_count+1,
                dispatched_at=clock_timestamp(),updated_at=clock_timestamp()
            WHERE id=%s
            """,
            (task_name, job_id),
        )
    return task_name


def reconcile_stale_jobs(conn) -> int:
    """Recover RUNNING rows whose Cloud Run attempt vanished without a verdict."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE app.inbound_scan_jobs
            SET state=CASE WHEN attempt_count >= max_attempts
                           THEN 'DEAD_LETTER' ELSE 'RETRY_WAIT' END,
                error_code='scan_attempt_lost',
                recovery_evidence=jsonb_build_object(
                    'version','inbound-scan-recovery-v1',
                    'command','retry_inbound_scan_job',
                    'requires_authorization',true,
                    'job_id',id
                ),
                task_name=NULL,next_attempt_at=clock_timestamp(),
                finished_at=CASE WHEN attempt_count >= max_attempts
                                 THEN clock_timestamp() ELSE NULL END,
                updated_at=clock_timestamp()
            WHERE state='RUNNING'
              AND started_at < clock_timestamp() - interval '2 minutes'
            """
        )
        recovered = cur.rowcount
        cur.execute(
            """
            UPDATE app.inbound_scan_job_attempts a
            SET state=j.state,error_code='scan_attempt_lost',
                finished_at=clock_timestamp()
            FROM app.inbound_scan_jobs j
            WHERE a.job_id=j.id AND a.recovery_count=j.recovery_count
              AND a.attempt_no=j.attempt_count AND a.state='RUNNING'
              AND j.error_code='scan_attempt_lost'
            """
        )
    from core.managed_file_dispatch import (  # noqa: PLC0415
        reconcile_pending_dispatches,
    )

    reconcile_pending_dispatches(conn)
    return recovered


def queued_without_task(conn, *, limit: int = 100) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id FROM app.inbound_scan_jobs
            WHERE state IN ('QUEUED','RETRY_WAIT')
              AND (task_name IS NULL OR dispatched_at IS NULL
                   OR dispatched_at < clock_timestamp() - interval '65 minutes')
              AND (next_attempt_at IS NULL
                   OR next_attempt_at <= clock_timestamp())
            ORDER BY queued_at LIMIT %s FOR UPDATE SKIP LOCKED
            """,
            (limit,),
        )
        return [row[0] for row in cur.fetchall()]
