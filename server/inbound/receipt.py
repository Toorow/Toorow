"""Bounded inbound receipt handler (Epic 38, Story 38.1).

A thin, internet-facing ASGI app exposing:

  * ``POST /v1/webhooks/inbound-email``
  * ``POST /v1/webhooks/inbound-file``   (transport-neutral)

Fail-closed order (Dev Notes §Security posture):
  1. bound the RAW request first — max header size, then max body size (declared
     Content-Length AND a hard streaming cap) — reject before any parse;
  2. the provider signature is carried IN the body (Mailgun signs form fields),
     so a *bounded* form parse (size-, field- and file-capped) reads only the
     signed fields, then the adapter verifies signature + replay. Nothing from
     the body is TRUSTED (attachments, recipient) until verification passes;
  3. only for a verified delivery: enforce the attachment-count bound and the
     ``ds_<token>`` recipient shape;
  4. nothing durable runs in this slice.

Any failure at 1-4 -> a CONSTANT-SHAPE HTTP 403: identical body/shape for
unsigned / oversize / unknown-recipient, with no existence disclosure and no
secret in the error (E38-NFR03). A verified, in-bounds, well-shaped delivery ->
HTTP 202 + a correlation id.

Durable write (Story 38.6): ONLY for a verified + in-bounds + well-shaped
delivery, and ONLY when a quarantine backend is configured (either
``INBOUND_QUARANTINE_BUCKET`` or ``INBOUND_QUARANTINE_LOCAL_ROOT``), the
delivered file bytes plus a fixed-schema MANIFEST are written to the quarantine
store. This process holds object-create only and NO database; the downstream
worker (``core.inbound_processing``) resolves the routing token and imports. The
raw routing token and the raw recipient address are NEVER written -- only their
sha256 hashes. When NO quarantine backend is configured, nothing durable is
written (unchanged 38.1 acknowledge-only behaviour).

The signing secret is read from the ``INBOUND_SIGNING_SECRET`` env (injected by
Cloud Run as a Secret Manager reference). It is never logged.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from inbound.adapters import ReceiptRequest, get_adapter

logger = logging.getLogger("inbound.receipt")

#: Manifest schema tag consumed VERBATIM by the downstream worker
#: (``core.inbound_processing``). Do not change without updating that contract.
_MANIFEST_SCHEMA = "inbound-delivery-manifest-v1"

#: Reserved filename of the manifest object itself within a delivery partition.
_MANIFEST_FILENAME = "_manifest.json"

#: Extracts the routing token from a ``ds_<token>@<domain>`` recipient. The
#: recipient has already passed ``_RECIPIENT_RE`` when this is applied.
_TOKEN_RE = re.compile(r"^ds_([A-Za-z0-9]{1,64})@")

# Recipient shape gate (AC "recipient must parse to a ds_<token> shape"). Token
# RESOLUTION to an ENABLED Datastream is Story 38.8 — here we only validate the
# lexical shape ``ds_<token>@<domain>``.
_RECIPIENT_RE = re.compile(r"^ds_[A-Za-z0-9]{1,64}@[A-Za-z0-9.\-]{1,255}$")

# Constant-shape rejection body. IDENTICAL for every failure class so an attacker
# cannot distinguish "unsigned" from "unknown recipient" from "oversize". No
# existence disclosure, no secret, no capability.
_FORBIDDEN_BODY = {"code": "forbidden", "message": "forbidden"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _limits() -> dict[str, int]:
    return {
        "max_body_bytes": _env_int("INBOUND_MAX_BODY_BYTES", 26_214_400),  # 25 MiB
        "max_header_bytes": _env_int("INBOUND_MAX_HEADER_BYTES", 16_384),  # 16 KiB
        "max_attachments": _env_int("INBOUND_MAX_ATTACHMENTS", 20),
    }


def _forbidden() -> JSONResponse:
    """Constant-shape 403 — same object for every rejection reason."""
    return JSONResponse(_FORBIDDEN_BODY, status_code=403)


def _header_bytes(request: Request) -> int:
    total = 0
    for name, value in request.headers.raw:
        total += len(name) + len(value)
    return total


def _content_length(request: Request) -> int | None:
    raw = request.headers.get("content-length")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _quarantine_enabled() -> bool:
    """True iff a quarantine backend is explicitly configured.

    Gate for the durable write: when NEITHER env var is set we behave exactly as
    the 38.1 acknowledge-only handler (verify + bound + shape-check -> 202, and
    NOTHING durable). This preserves the existing tests and lets operators opt in
    to the durable path per environment. We deliberately do NOT trigger on the
    ``open_quarantine_store`` dev-temp fallback -- only an explicit backend.
    """
    bucket = os.environ.get("INBOUND_QUARANTINE_BUCKET", "").strip()
    local_root = os.environ.get("INBOUND_QUARANTINE_LOCAL_ROOT", "").strip()
    return bool(bucket or local_root)


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


async def _handle(request: Request, *, channel: str) -> Response:
    """Shared receipt path for both the email and file routes.

    ``channel`` is bound per route ("email" for the email route, "webhook" for
    the file route) and recorded verbatim in the manifest; it is opaque declared
    data, never provider vocabulary.
    """
    limits = _limits()

    # (3a) Header-size bound — cheap, do it before touching the body.
    if _header_bytes(request) > limits["max_header_bytes"]:
        return _forbidden()

    # (3b) Body-size bound — reject declared-oversize before reading the body.
    declared = _content_length(request)
    if declared is not None and declared > limits["max_body_bytes"]:
        return _forbidden()

    # Read the body with a hard cap even if Content-Length lied/absent, then
    # cache it on the request so the subsequent form parse re-reads it (Starlette
    # streams from ``request._body`` once set — a manual stream() alone would
    # exhaust the body before ``form()`` could see it).
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > limits["max_body_bytes"]:
            return _forbidden()
    request._body = body  # noqa: SLF001 — cache the bounded body for form()

    # Parse the form defensively, with field/file caps so an unauthenticated
    # body cannot make the multipart parser do unbounded work. A malformed or
    # over-cap body is a forbidden-shape request. Attachment BYTES are retained
    # ONLY when a quarantine backend is configured (bounded by max_body_bytes,
    # already enforced above); otherwise we keep the 38.1 metadata-only parse.
    retain_bytes = _quarantine_enabled()
    try:
        form = await _parse_form(
            request,
            max_files=limits["max_attachments"] + 50,
            max_fields=200,
            retain_bytes=retain_bytes,
        )
    except Exception:  # noqa: BLE001 — any parse failure is a constant-shape 403
        return _forbidden()

    # (1) select adapter (deploy-time config). An unknown provider is an operator
    # misconfiguration, not a client-shaped condition -> 500, not a 403 (and we
    # do NOT echo the provider name to the client).
    provider = os.environ.get("INBOUND_PROVIDER") or "mailgun"
    try:
        adapter = get_adapter(provider)
    except ValueError:
        logger.error("inbound: unknown INBOUND_PROVIDER configured")
        return JSONResponse(
            {"code": "misconfigured", "message": "receipt runtime misconfigured"},
            status_code=500,
        )

    signing_secret = os.environ.get("INBOUND_SIGNING_SECRET", "")

    receipt_request = ReceiptRequest(
        headers={k.lower(): v for k, v in request.headers.items()},
        form=form["fields"],
        files=form["files"],
        signing_secret=signing_secret,
    )

    # (2) verify signature/replay before trusting anything in the body. Any
    # verifier error (e.g. a malformed attacker signature) must fail closed with
    # the constant-shape 403 — never propagate into a distinguishable 500.
    try:
        delivery = adapter.verify(receipt_request)
    except Exception:  # noqa: BLE001 — verifier failure must not leak or 500
        return _forbidden()
    if delivery is None:
        return _forbidden()

    # (3c) attachment-count bound.
    if len(delivery.attachments_meta) > limits["max_attachments"]:
        return _forbidden()

    # (4) recipient must parse to the ds_<token> shape (resolution is 38.8).
    if not delivery.recipient or not _RECIPIENT_RE.match(delivery.recipient):
        return _forbidden()

    # Verified + bounded + well-shaped -> accept.
    correlation_id = f"inbrx_{uuid.uuid4().hex}"

    # Durable write (gated). Only past this point -- i.e. AFTER signature
    # verification -- do we ever trust or persist body bytes. When no quarantine
    # backend is configured we skip it entirely (unchanged 38.1 behaviour).
    if retain_bytes:
        write_error = _write_to_quarantine(
            delivery=delivery,
            channel=channel,
            attachments=form["files"],
            correlation_id=correlation_id,
        )
        if write_error is not None:
            # A storage failure AFTER a valid delivery is a SERVER condition, not
            # a client-shaped one: return a generic 500 (lets the provider retry)
            # rather than the constant-shape 403. Never leak token/recipient or
            # any internal detail -- log only the correlation id.
            logger.error(
                "inbound: quarantine write failed correlation_id=%s",
                correlation_id,
            )
            return JSONResponse(
                {"code": "internal", "message": "internal"},
                status_code=500,
            )

    logger.info(
        "inbound: accepted delivery correlation_id=%s provider_event_id=%s",
        correlation_id,
        delivery.provider_event_id,
    )
    return JSONResponse(
        {"status": "accepted", "correlation_id": correlation_id},
        status_code=202,
    )


def _write_to_quarantine(
    *,
    delivery,  # noqa: ANN001 — InboundDelivery (no import needed here)
    channel: str,
    attachments: list[dict],
    correlation_id: str,
) -> Exception | None:
    """Write attachment bytes + a manifest to the quarantine store.

    Returns ``None`` on success, or the caught exception on any failure (the
    caller maps a non-None result to a generic 500). The raw routing token and
    the raw recipient address are NEVER written -- only their sha256 hashes. The
    manifest schema is fixed and consumed verbatim by ``core.inbound_processing``.
    """
    from core.inbound_quarantine import open_quarantine_store  # noqa: PLC0415

    try:
        # Extract the routing token from ds_<token>@<domain>. The recipient has
        # already matched _RECIPIENT_RE, so this match is expected to succeed.
        match = _TOKEN_RE.match(delivery.recipient or "")
        if match is None:
            # Defensive: treat an unexpected shape as a server condition rather
            # than persisting an un-partitionable delivery.
            return ValueError("recipient did not yield a routing token")
        token = match.group(1)
        token_hash = _sha256_hex(token)
        recipient_hash = _sha256_hex(delivery.recipient)

        store = open_quarantine_store()
        message_id = delivery.provider_event_id

        manifest_attachments: list[dict] = []
        for att in attachments:
            filename = att.get("name") or att.get("filename") or "attachment"
            content_type = att.get("content_type")
            data = att.get("data")
            if data is None:
                # Bytes must have been retained for a durable write.
                return ValueError("attachment bytes missing for quarantine write")
            obj = store.put(
                partition=token_hash,
                message_id=message_id,
                filename=filename,
                data=data,
                content_type=content_type,
            )
            manifest_attachments.append(
                {
                    "filename": filename,
                    "quarantine_uri": obj.uri,
                    "size": obj.size,
                    "content_type": content_type,
                }
            )

        manifest = {
            "schema": _MANIFEST_SCHEMA,
            "provider_event_id": delivery.provider_event_id,
            "channel": channel,
            "token_hash": token_hash,
            "recipient_hash": recipient_hash,
            "received_at": datetime.now(timezone.utc).isoformat(),
            "attachments": manifest_attachments,
        }
        store.put(
            partition=token_hash,
            message_id=message_id,
            filename=_MANIFEST_FILENAME,
            data=json.dumps(manifest).encode("utf-8"),
            content_type="application/json",
        )
    except Exception as exc:  # noqa: BLE001 — any storage failure -> generic 500
        return exc
    return None


async def _parse_form(
    request: Request,
    *,
    max_files: int,
    max_fields: int,
    retain_bytes: bool = False,
) -> dict:
    """Extract fields + attachment metadata (optionally retaining bytes).

    Returns ``{"fields": {name: value}, "files": [{name,size,content_type}, ...]}``.
    Uses Starlette's multipart/urlencoded parser (capped at ``max_files`` /
    ``max_fields`` so an unauthenticated body cannot force unbounded parsing).

    When ``retain_bytes`` is False (the 38.1 default), ``UploadFile`` entries are
    reduced to metadata only -- no bytes retained. When True, each file entry
    ALSO carries a ``data`` key with the attachment's bytes, needed for the
    durable quarantine write; the raw body is already size-bounded by
    ``max_body_bytes`` so this buffering is bounded. The adapter reads only
    name/size/content_type, so the extra ``data`` key is inert to verification.
    """
    fields: dict[str, str] = {}
    files: list[dict] = []
    form = await request.form(max_files=max_files, max_fields=max_fields)
    try:
        for key, value in form.multi_items():
            # Starlette UploadFile has a ``filename`` attribute.
            filename = getattr(value, "filename", None)
            if filename is not None:
                size = getattr(value, "size", None)
                content_type = getattr(value, "content_type", None)
                entry: dict = {
                    "name": filename,
                    "size": size,
                    "content_type": content_type,
                }
                if retain_bytes:
                    data = await value.read()
                    entry["data"] = data
                    # Prefer the true byte length as size (Starlette's UploadFile
                    # ``size`` may be None for small in-memory parts).
                    if size is None:
                        entry["size"] = len(data)
                files.append(entry)
            else:
                fields[key] = value
    finally:
        await form.close()
    return {"fields": fields, "files": files}


async def _handle_email(request: Request) -> Response:
    """Email inbound route -> channel "email"."""
    return await _handle(request, channel="email")


async def _handle_file(request: Request) -> Response:
    """Transport-neutral file inbound route -> channel "webhook"."""
    return await _handle(request, channel="webhook")


routes = [
    Route("/v1/webhooks/inbound-email", _handle_email, methods=["POST"]),
    Route("/v1/webhooks/inbound-file", _handle_file, methods=["POST"]),
]

app = Starlette(routes=routes)


def build_inbound_app() -> Starlette:
    """Return the inbound receipt ASGI app (the Cloud Run entrypoint)."""
    return app
