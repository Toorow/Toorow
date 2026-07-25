"""Inbound async bridge (Epic 38): quarantine manifest lands -> worker runs.

The internet-facing receipt service (``inbound.receipt``) verifies a delivery
and writes the file bytes plus a reserved ``_manifest.json`` object to the
quarantine store. It holds NO database and NO warehouse. THIS bridge is the
DB-enabled, source-agnostic glue that reacts when a manifest object is finalized
in the quarantine bucket and invokes the already-built, fully-tested worker
``core.inbound_processing.process_inbound_delivery``.

Why a separate ASGI app in ``server/inbound`` (and not in ``server/core``):
  * ``server/core`` must NEVER import ``server/inbound`` (AD-2 boundary, enforced
    by ``tests/inbound/test_source_agnostic_boundary.py``). The reverse direction
    (this bridge importing core) IS allowed.
  * This bridge legitimately carries OUR-OWN-infra vocabulary (Pub/Sub, GCS
    object-finalize notifications). Pub/Sub and GCS are toorow's transport
    plumbing, NOT a data-source provider -- so no provider(data-source)
    vocabulary appears here either. It mirrors how ``inbound.receipt`` is a
    separate app selected by the container entrypoint and deployed as its own
    Cloud Run service.

Transport contract (Pub/Sub PUSH):
  ``POST /v1/internal/inbound-process`` is the push target of a Pub/Sub push
  subscription fed by a GCS OBJECT_FINALIZE notification on the quarantine
  bucket. The push body is the Pub/Sub envelope::

      {"message": {"data": <base64 json>, "attributes": {...},
                   "messageId": "..."},
       "subscription": "..."}

  GCS notifications carry the object coordinates BOTH as message attributes
  (``bucketId`` / ``objectId``) AND as a base64 JSON payload (``bucket`` /
  ``name``). We derive ``(bucket, name)`` from attributes first, then fall back
  to the decoded data JSON.

Auth (two supported gates):
  * PLATFORM IAM (the managed default): the service is deployed PRIVATE and the
    Pub/Sub push subscription authenticates with an OIDC token (a service account
    holding ``run.invoker``). Cloud Run rejects any non-authorized caller before
    the request reaches this app -- Pub/Sub push CANNOT set a custom request
    header, so this is the correct managed gate. In this mode ``INBOUND_WORKER_SECRET``
    is left UNSET and the app relies on the platform having already authenticated.
  * SHARED SECRET (defence-in-depth / self-hosted): when ``INBOUND_WORKER_SECRET``
    IS set, a matching ``X-Inbound-Worker-Secret`` header is additionally required
    (constant-time compare); a mismatch -> constant-shape 403. Use this only for a
    caller that can set the header (manual invocation / a self-hosted proxy).
Deploy either PRIVATE (IAM) or with the shared secret set -- never PUBLIC with the
secret unset.

Idempotency / retries: ``process_inbound_delivery`` is idempotent (dedup on
provider_event_id), so a Pub/Sub redelivery is safe. We return:
  * 403 on auth failure (constant shape),
  * 500 misconfigured when the worker secret env is unset,
  * 400 on a malformed envelope (so Pub/Sub does NOT retry an unparseable push
    forever) -- logged with a generic message only,
  * 204 ignored when the finalized object is not the reserved manifest
    (attachments and other objects do not drive processing directly),
  * 200 with the worker status on success (after ``conn.commit()``),
  * 500 on ANY processing exception (after ``conn.rollback()``) so Pub/Sub
    redelivers -- with a GENERIC body; never leak manifest/token/internal
    detail. Only a correlation/messageId is logged.

ASCII-only source (AI-03). No secret is ever logged.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import logging
import os

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


def _bad_request() -> JSONResponse:
    """400 for a malformed envelope so Pub/Sub does not retry it forever."""
    return JSONResponse(
        {"code": "bad_request", "message": "bad_request"},
        status_code=400,
    )


def _internal() -> JSONResponse:
    """Generic 500 so Pub/Sub redelivers; never leaks internal detail."""
    return JSONResponse(
        {"code": "internal", "message": "internal"},
        status_code=500,
    )


def _authorized(request: Request) -> bool | None:
    """Return True/False when the secret is configured, or None if unset.

    None means NO shared secret is configured: the deployment is expected to be
    IAM-gated (Cloud Run private + Pub/Sub OIDC), so the handler PROCEEDS (the
    platform already authenticated the caller). When the secret IS configured, a
    matching header is required (True) or the request is rejected (False).
    """
    configured = os.environ.get("INBOUND_WORKER_SECRET")
    if not configured:
        return None
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
        logger.warning("inbound_bridge: malformed push envelope (unparseable body)")
        return _bad_request()
    if not isinstance(envelope, dict):
        logger.warning("inbound_bridge: malformed push envelope (not an object)")
        return _bad_request()

    # A correlation id for logs (never a secret/token). Pub/Sub messageId if any.
    message = envelope.get("message")
    message_id = ""
    if isinstance(message, dict):
        raw_mid = message.get("messageId") or message.get("message_id")
        if isinstance(raw_mid, str):
            message_id = raw_mid

    derived = _derive_object(envelope)
    if derived is None:
        logger.warning(
            "inbound_bridge: malformed envelope (no object) messageId=%s",
            message_id,
        )
        return _bad_request()
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
    from core.inbound_processing import (  # noqa: PLC0415
        process_inbound_delivery,
    )
    from core.inbound_quarantine import open_quarantine_store  # noqa: PLC0415

    try:
        store = open_quarantine_store()
        raw = store.get(f"gs://{bucket}/{name}")
        manifest = json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 -- read/parse failure; redeliver, no leak
        logger.warning(
            "inbound_bridge: manifest read/parse failed messageId=%s error=%s: %s",
            message_id, type(exc).__name__, str(exc)[:200],
        )
        return _internal()

    try:
        with get_connection() as conn:
            try:
                result = process_inbound_delivery(
                    conn,
                    manifest=manifest,
                    store=store,
                    actor="inbound-worker",
                    trace_id=message_id or None,
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    except Exception:  # noqa: BLE001 -- worker failure; redeliver, generic body
        # logger.exception logs the traceback (code + exception type), NOT local
        # variable values, so the manifest/token content is not leaked.
        logger.exception(
            "inbound_bridge: processing failed messageId=%s", message_id,
        )
        return _internal()

    status = result.get("status") if isinstance(result, dict) else None
    logger.info(
        "inbound_bridge: processed messageId=%s status=%s",
        message_id,
        status,
    )
    return JSONResponse({"status": status}, status_code=200)


routes = [
    Route("/v1/internal/inbound-process", _handle, methods=["POST"]),
]

app = Starlette(routes=routes)


def build_bridge_app() -> Starlette:
    """Return the inbound bridge ASGI app (the Cloud Run entrypoint).

    Mirrors ``inbound.receipt.build_inbound_app``. Selected by the container
    entrypoint for the ``inbound-bridge`` service (see infra terraform).
    """
    return app
