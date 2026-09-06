"""Inbound async bridge: quarantine manifest lands -> worker runs.

The authenticated public receipt service commits durable receipt/audit/outbox
evidence, then publishes the reserved ``_manifest.json`` marker. This private
bridge consumes Pub/Sub object-finalize envelopes and invokes
``core.inbound_processing.process_inbound_delivery``.

Authentication is provided by private platform IAM, with an optional shared
secret for self-hosted deployments. Permanently malformed Pub/Sub envelopes are
acknowledged with 204 and bounded hashed evidence to prevent poison-message
retry loops. Valid worker failures are rolled back and return 500 for retry.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import re

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger("inbound.bridge")

#: The reserved manifest object name; only this object drives processing. Must
#: match ``inbound.receipt._MANIFEST_FILENAME`` and
#: ``core.inbound_processing._MANIFEST_FILENAME``.
_MANIFEST_SUFFIX = "/_manifest.json"

#: Header carrying the shared worker secret (our own infra vocabulary).
_WORKER_SECRET_HEADER = "x-inbound-worker-secret"

#: Constant-shape 403 body -- identical for missing/mismatched secret.
_FORBIDDEN_BODY = {"code": "forbidden", "message": "forbidden"}


def _forbidden() -> JSONResponse:
    """Constant-shape 403 -- same object for every auth-rejection reason."""
    return JSONResponse(_FORBIDDEN_BODY, status_code=403)


def _ack_poison_envelope() -> Response:
    """Acknowledge permanently malformed transport input without a retry loop."""
    return Response(status_code=204)


def _internal() -> JSONResponse:
    """Generic 500 so Pub/Sub redelivers; never leaks internal detail."""
    return JSONResponse(
        {"code": "internal", "message": "internal"},
        status_code=500,
    )


def _authorized(request: Request) -> bool | None:
    """Apply the self-hosted secret gate or defer to managed platform IAM.

    Managed Cloud Run explicitly disables this application gate and is protected
    by IAM/OIDC. Every other deployment fails closed when the secret is absent.
    """
    if os.environ.get("INBOUND_REQUIRE_WORKER_SECRET", "true").lower() == "false":
        return None
    configured = os.environ.get("INBOUND_WORKER_SECRET")
    if not configured:
        return False
    provided = request.headers.get(_WORKER_SECRET_HEADER, "")
    # Constant-time compare; both operands ASCII-encoded (compare_digest raises
    # on non-ASCII str operands, so encode defensively).
    try:
        return hmac.compare_digest(provided.encode("utf-8"), configured.encode("utf-8"))
    except Exception:  # noqa: BLE001 -- any compare failure is a rejection, not a 500
        return False


def _derive_object(envelope: dict) -> tuple[str, str] | None:
    """Derive ``(bucket, name)`` from a Pub/Sub push envelope, defensively.

    Prefers the GCS notification message attributes (``bucketId`` / ``objectId``)
    and falls back to a base64-decoded JSON data payload (``bucket`` / ``name``).
    Returns None when neither yields a usable pair (a malformed envelope).
    """
    message = envelope.get("message")
    if not isinstance(message, dict):
        return None

    # (a) attributes form -- GCS puts bucketId/objectId here.
    attributes = message.get("attributes")
    if isinstance(attributes, dict):
        bucket = attributes.get("bucketId")
        name = attributes.get("objectId")
        if isinstance(bucket, str) and bucket and isinstance(name, str) and name:
            return bucket, name

    # (b) base64 data form -- decode the JSON payload and read bucket/name.
    data = message.get("data")
    if isinstance(data, str) and data:
        try:
            decoded = base64.b64decode(data, validate=True)
            payload = json.loads(decoded.decode("utf-8"))
        except (binascii.Error, ValueError, UnicodeDecodeError):
            return None
        if isinstance(payload, dict):
            bucket = payload.get("bucket")
            name = payload.get("name")
            if isinstance(bucket, str) and bucket and isinstance(name, str) and name:
                return bucket, name

    return None


async def _handle(request: Request) -> Response:
    """Pub/Sub PUSH target: manifest lands -> invoke the worker."""
    # Auth first (fail-closed on a mismatch). None => no shared secret configured
    # => rely on the platform IAM gate (private service + Pub/Sub OIDC) and
    # proceed. False => a secret IS configured but the header did not match.
    auth = _authorized(request)
    if auth is False:
        return _forbidden()

    # Parse the push envelope defensively. A malformed body -> 400 (not retried
    # forever). Log a generic message only -- never the body.
    try:
        envelope = await request.json()
    except Exception:  # noqa: BLE001 -- unparseable JSON is a malformed envelope
        logger.warning("inbound_bridge: poison_envelope reason=unparseable")
        return _ack_poison_envelope()
    if not isinstance(envelope, dict):
        logger.warning("inbound_bridge: poison_envelope reason=not_object")
        return _ack_poison_envelope()

    # A correlation id for logs (never a secret/token). Pub/Sub messageId if any.
    message = envelope.get("message")
    message_id = ""
    if isinstance(message, dict):
        raw_mid = message.get("messageId") or message.get("message_id")
        if isinstance(raw_mid, str):
            message_id = hashlib.sha256(raw_mid.encode("utf-8")).hexdigest()[:16]

    derived = _derive_object(envelope)
    if derived is None:
        logger.warning(
            "inbound_bridge: poison_envelope reason=no_object correlation=%s",
            message_id or "none",
        )
        return _ack_poison_envelope()
    bucket, name = derived

    # Filter: only the reserved manifest object drives processing. Attachments
    # and any other finalized object are ACKed as ignored (normal, not an error).
    # A 204 MUST carry no body (h11 rejects a body under a 204 Content-Length):
    # return a bodiless 204 so Pub/Sub acks cleanly.
    if not name.endswith(_MANIFEST_SUFFIX):
        return Response(status_code=204)

    # Lazy imports of core (this direction is allowed; keeps the module
    # importable offline and avoids import cost on non-processing paths).
    from core.db import get_connection  # noqa: PLC0415
    from core.inbound_quarantine import open_quarantine_store  # noqa: PLC0415
    from core.inbound_scan_jobs import (  # noqa: PLC0415
        dispatch_job,
        enqueue_manifest_jobs,
    )

    try:
        store = open_quarantine_store()
        raw = store.get_bounded(f"gs://{bucket}/{name}", max_bytes=1024 * 1024)
        manifest = json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 -- read/parse failure; redeliver, no leak
        logger.warning(
            "inbound_bridge: manifest read/parse failed messageId=%s error=%s: %s",
            message_id,
            type(exc).__name__,
            str(exc)[:200],
        )
        return _internal()

    try:
        with get_connection() as conn:
            try:
                jobs = enqueue_manifest_jobs(
                    conn,
                    manifest_uri=f"gs://{bucket}/{name}",
                    manifest=manifest,
                    trace_id=message_id or None,
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        for job in jobs:
            if job.get("task_name") or job.get("state") != "QUEUED":
                continue
            try:
                with get_connection() as conn:
                    dispatch_job(conn, job_id=job["job_id"])
                    conn.commit()
            except Exception:  # noqa: BLE001
                logger.exception(
                    "inbound_bridge: task dispatch deferred job=%s",
                    job["job_id"],
                )
    except Exception:  # noqa: BLE001
        logger.exception(
            "inbound_bridge: job preparation failed messageId=%s",
            message_id,
        )
        return _internal()

    logger.info(
        "inbound_bridge: queued messageId=%s jobs=%d",
        message_id,
        len(jobs),
    )
    return JSONResponse({"status": "queued", "jobs": len(jobs)}, status_code=200)


async def _handle_scan_task(request: Request) -> Response:
    """Cloud Tasks target: one durable ledger job, one attachment scan."""
    if _authorized(request) is False:
        return _forbidden()
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return _ack_poison_envelope()
    job_id = payload.get("job_id") if isinstance(payload, dict) else None
    if not isinstance(job_id, str) or not re.fullmatch(r"inbscan_[0-9a-f]{24}", job_id):
        return _ack_poison_envelope()

    from core.db import get_connection  # noqa: PLC0415
    from core.inbound_processing import process_inbound_delivery  # noqa: PLC0415
    from core.inbound_quarantine import open_quarantine_store  # noqa: PLC0415
    from core.inbound_scan import ScanRetryableError  # noqa: PLC0415
    from core.inbound_scan_jobs import (  # noqa: PLC0415
        claim_job,
        fail_attempt,
        finish_job,
        reconcile_receipt,
    )

    with get_connection() as conn:
        job = claim_job(conn, job_id=job_id)
        conn.commit()
    if job is None:
        return JSONResponse({"status": "settled"}, status_code=200)

    try:
        store = open_quarantine_store()
        raw = store.get_bounded(job["manifest_uri"], max_bytes=1024 * 1024)
        manifest = json.loads(raw.decode("utf-8"))
        with get_connection() as conn:
            try:
                result = process_inbound_delivery(
                    conn,
                    manifest=manifest,
                    store=store,
                    actor="inbound-scan-worker",
                    trace_id=job.get("trace_id"),
                    attachment_ordinals={int(job["ordinal"])},
                    defer_receipt_finalization=True,
                    persist_scan_intent=True,
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        attachments = result.get("attachments") or []
        outcome = attachments[0] if attachments else {}
        with get_connection() as conn:
            finish_job(
                conn,
                job_id=job_id,
                status=str(result.get("status")),
                raw_import_id=outcome.get("raw_import_id"),
                error_code=outcome.get("error_code"),
            )
            reconcile_receipt(conn, receipt_id=job["receipt_id"], trace_id=job.get("trace_id"))
            conn.commit()
        return JSONResponse({"status": result.get("status")}, status_code=200)
    except ScanRetryableError as exc:
        error_code = exc.code
    except Exception:  # noqa: BLE001
        logger.exception("inbound_bridge: scan task failed job=%s", job_id)
        error_code = "scan_worker_error"

    with get_connection() as conn:
        state = fail_attempt(conn, job_id=job_id, error_code=error_code)
        if state == "dead_letter":
            reconcile_receipt(conn, receipt_id=job["receipt_id"], trace_id=job.get("trace_id"))
        conn.commit()
    if state == "dead_letter":
        return JSONResponse({"status": state}, status_code=200)
    return _internal()


async def _handle_reconcile(request: Request) -> Response:
    """Scheduler target: dispatch committed QUEUED rows that lost dispatch."""
    if _authorized(request) is False:
        return _forbidden()
    from core.db import get_connection  # noqa: PLC0415
    from core.inbound_scan_jobs import (  # noqa: PLC0415
        dispatch_job,
        queued_without_task,
        reconcile_stale_jobs,
    )

    with get_connection() as conn:
        reconcile_stale_jobs(conn)
        job_ids = queued_without_task(conn, limit=100)
        conn.commit()
    dispatched = 0
    for job_id in job_ids:
        try:
            with get_connection() as conn:
                dispatch_job(conn, job_id=job_id)
                conn.commit()
            dispatched += 1
        except Exception:  # noqa: BLE001
            logger.exception("inbound_bridge: reconciliation deferred job=%s", job_id)
    return JSONResponse({"status": "reconciled", "dispatched": dispatched})


routes = [
    Route("/v1/internal/inbound-process", _handle, methods=["POST"]),
    Route("/v1/internal/inbound-scan-task", _handle_scan_task, methods=["POST"]),
    Route("/v1/internal/inbound-scan-reconcile", _handle_reconcile, methods=["POST"]),
]

app = Starlette(routes=routes)


def build_bridge_app() -> Starlette:
    """Return the inbound bridge ASGI app (the Cloud Run entrypoint).

    Mirrors ``inbound.receipt.build_inbound_app``. Selected by the container
    entrypoint for the ``inbound-bridge`` service (see infra terraform).
    """
    return app
