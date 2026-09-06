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
_TOKEN_RE = re.compile(r"^ds_([A-Za-z0-9_-]{32,128})@")

# Recipient shape gate (AC "recipient must parse to a ds_<token> shape"). Token
# RESOLUTION to an ENABLED Datastream is Story 38.8 — here we only validate the
# lexical shape ``ds_<token>@<domain>``.
_RECIPIENT_RE = re.compile(r"^ds_[A-Za-z0-9_-]{32,128}@[A-Za-z0-9.\-]{1,255}$")

# Constant-shape rejection body. IDENTICAL for every failure class so an attacker
# cannot distinguish "unsigned" from "unknown recipient" from "oversize". No
# existence disclosure, no secret, no capability.
_FORBIDDEN_BODY = {"code": "forbidden", "message": "forbidden"}
_INTERNAL_BODY = {"code": "internal", "message": "internal"}


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


def _resolve_org_id(conn, *, datastream_id: str) -> str:
    """Resolve the organization before deriving any quarantine object key."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT d.org_id FROM app.datastreams d WHERE d.id = %s",
            (datastream_id,),
        )
        row = cur.fetchone()
    if row is None or not row[0]:
        raise ValueError("datastream organization is unresolved")
    return str(row[0])


def _retention_days() -> int:
    """Return the versioned quarantine retention duration (default 30 days)."""
    days = _env_int("INBOUND_QUARANTINE_RETENTION_DAYS", 30)
    if days < 1 or days > 3650:
        raise ValueError("INBOUND_QUARANTINE_RETENTION_DAYS must be in 1..3650")
    return days


def _internal() -> JSONResponse:
    """Generic retryable failure with no tenant, credential or storage detail."""
    return JSONResponse(_INTERNAL_BODY, status_code=500)


def _presented_capability(recipient: str, *, channel: str) -> str | None:
    """Extract the capability while accepting webhook tokens without an address."""
    address_match = _TOKEN_RE.match(recipient)
    if address_match is not None and _RECIPIENT_RE.fullmatch(recipient):
        return address_match.group(1)
    if channel == "webhook" and re.fullmatch(r"[A-Za-z0-9_-]{32,128}", recipient):
        return recipient
    return None


async def _handle(request: Request, *, channel: str) -> Response:
    """Authenticate, authorize, durably record, then acknowledge one delivery."""
    limits = _limits()
    if _header_bytes(request) > limits["max_header_bytes"]:
        return _forbidden()
    declared = _content_length(request)
    if declared is not None and declared > limits["max_body_bytes"]:
        return _forbidden()
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > limits["max_body_bytes"]:
            return _forbidden()
    request._body = body  # noqa: SLF001 - cache bounded body for form()

    if not _quarantine_enabled():
        logger.error("inbound: durable quarantine backend is not configured")
        return _internal()
    try:
        form = await _parse_form(
            request, max_files=limits["max_attachments"] + 50,
            max_fields=200, retain_bytes=True,
        )
    except Exception:  # noqa: BLE001
        return _forbidden()
    try:
        adapter = get_adapter(os.environ.get("INBOUND_PROVIDER") or "mailgun")
    except ValueError:
        logger.error("inbound: unknown INBOUND_PROVIDER configured")
        return _internal()
    receipt_request = ReceiptRequest(
        headers={k.lower(): v for k, v in request.headers.items()},
        form=form["fields"], files=form["files"],
        signing_secret=os.environ.get("INBOUND_SIGNING_SECRET", ""),
    )
    try:
        delivery = adapter.verify(receipt_request)
    except Exception:  # noqa: BLE001
        return _forbidden()
    if delivery is None or len(delivery.attachments_meta) > limits["max_attachments"]:
        return _forbidden()
    raw_capability = _presented_capability(delivery.recipient or "", channel=channel)
    if raw_capability is None:
        return _forbidden()

    from core.db import get_connection  # noqa: PLC0415
    from core.inbound_credentials import resolve_for_delivery  # noqa: PLC0415
    from core.inbound_raw_imports import (  # noqa: PLC0415
        list_raw_imports_for_receipt,
        record_raw_import,
    )
    from core.inbound_receipts import (  # noqa: PLC0415
        InboundReceiptFingerprintMismatch,
        assert_provider_event_fingerprint,
        canonical_receipt_fingerprint,
        get_receipt_by_provider_event,
        hash_recipient,
        record_receipt,
    )
    from core.operations import OperationIdempotencyConflict  # noqa: PLC0415

    token_hash = _sha256_hex(raw_capability)
    recipient_hash = hash_recipient(delivery.recipient)
    fingerprint_attachments = []
    for ordinal, attachment in enumerate(form["files"]):
        data = attachment.get("data")
        if not isinstance(data, bytes):
            return _internal()
        fingerprint_attachments.append({
            "ordinal": ordinal,
            "filename": attachment.get("name") or "attachment",
            "content_type": attachment.get("content_type"),
            "size": len(data),
            "content_sha256": hashlib.sha256(data).hexdigest(),
        })

    try:
        with get_connection() as conn:
            try:
                resolution = resolve_for_delivery(conn, raw_token=raw_capability)
                raw_capability = ""
                scope = resolution.get("scope") or {}
                if not resolution.get("allowed") or scope.get("channel") != channel:
                    conn.commit()
                    return _forbidden()
                datastream_id = str(scope["datastream_id"])
                credential_id = str(scope["credential_id"])
                org_id = _resolve_org_id(conn, datastream_id=datastream_id)
                receipt_fingerprint = canonical_receipt_fingerprint(
                    datastream_id=datastream_id, credential_id=credential_id,
                    channel=channel, recipient_hash=recipient_hash,
                    attachments=fingerprint_attachments,
                )
                assert_provider_event_fingerprint(
                    conn, datastream_id=datastream_id,
                    provider_event_id=delivery.provider_event_id,
                    receipt_fingerprint=receipt_fingerprint,
                )
                configured_retention_days = _retention_days()
                existing_receipt = get_receipt_by_provider_event(
                    conn, datastream_id=datastream_id,
                    provider_event_id=delivery.provider_event_id,
                )
                trace_id = hashlib.sha256(
                    delivery.provider_event_id.encode("utf-8")
                ).hexdigest()[:32]
                if existing_receipt is not None:
                    existing_raws = list_raw_imports_for_receipt(
                        conn, receipt_id=existing_receipt["receipt_id"],
                        datastream_id=datastream_id,
                    )
                    staged, retention_days = _stage_existing_replay(
                        org_id=org_id, datastream_id=datastream_id,
                        receipt_fingerprint=receipt_fingerprint,
                        provider_reference_hash=_sha256_hex(
                            delivery.provider_event_id
                        ),
                        fingerprint_attachments=fingerprint_attachments,
                        existing_raws=existing_raws,
                    )
                    receipt = {
                        **existing_receipt, "operation_outcome": "replayed"
                    }
                else:
                    retention_days = configured_retention_days
                    staged = _stage_quarantine(
                        delivery=delivery, org_id=org_id,
                        datastream_id=datastream_id,
                        receipt_fingerprint=receipt_fingerprint,
                        provider_reference_hash=_sha256_hex(
                            delivery.provider_event_id
                        ),
                        attachments=form["files"],
                        fingerprint_attachments=fingerprint_attachments,
                        retention_days=retention_days,
                    )
                    quarantine_uri = (
                        staged["attachments"][0]["quarantine_uri"]
                        if staged["attachments"] else None
                    )
                    total_bytes = sum(
                        item["size"] for item in staged["attachments"]
                    )
                    receipt = record_receipt(
                        conn, datastream_id=datastream_id,
                        credential_id=credential_id, channel=channel,
                        provider_event_id=delivery.provider_event_id,
                        recipient_hash=recipient_hash,
                        receipt_fingerprint=receipt_fingerprint,
                        attachment_count=len(staged["attachments"]),
                        total_bytes=total_bytes, quarantine_uri=quarantine_uri,
                        actor="inbound-receipt", host_context={},
                        trace_id=trace_id,
                        idempotency_key=(
                            f"receipt:{datastream_id}:"
                            f"{delivery.provider_event_id}"
                        ),
                    )
                    for attachment in staged["attachments"]:
                        record_raw_import(
                            conn, receipt_id=receipt["receipt_id"],
                            datastream_id=datastream_id,
                            ordinal=attachment["ordinal"],
                            size_bytes=attachment["size"],
                            content_hash=attachment["content_sha256"],
                            filename=attachment["filename"],
                            media_type_declared=attachment.get("content_type"),
                            quarantine_uri=attachment["quarantine_uri"],
                            retention_policy_version=(
                                "quarantine-retention-v1"
                            ),
                            retention_days=retention_days,
                            actor="inbound-receipt", host_context={},
                            trace_id=trace_id,
                            idempotency_key=(
                                f"raw-import:{receipt['receipt_id']}:"
                                f"{attachment['ordinal']}"
                            ),
                        )
                quarantine_uri = (
                    staged["attachments"][0]["quarantine_uri"]
                    if staged["attachments"] else None
                )
                # HTTP 202 is impossible until receipt and attachment evidence
                # exist durably. An exact replay reuses that evidence unchanged.
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    except (InboundReceiptFingerprintMismatch, OperationIdempotencyConflict):
        return _forbidden()
    except Exception:  # noqa: BLE001
        logger.exception("inbound: durable receipt failed")
        return _internal()

    manifest_error = _publish_manifest(
        staged=staged, delivery=delivery, channel=channel,
        token_hash=token_hash, recipient_hash=recipient_hash,
        receipt=receipt, receipt_fingerprint=receipt_fingerprint,
        quarantine_uri=quarantine_uri,
    )
    if manifest_error is not None:
        logger.error(
            "inbound: manifest publication failed receipt_id=%s error_type=%s",
            receipt["receipt_id"],
            type(manifest_error).__name__,
        )
        return _internal()
    logger.info("inbound: accepted durable receipt receipt_id=%s", receipt["receipt_id"])
    return JSONResponse(
        {"status": "accepted", "receipt_id": receipt["receipt_id"],
         "outcome": receipt.get("operation_outcome") or "succeeded"},
        status_code=202,
    )


def _stage_quarantine(
    *,
    delivery,  # noqa: ANN001
    org_id: str,
    datastream_id: str,
    receipt_fingerprint: str,
    provider_reference_hash: str,
    attachments: list[dict],
    fingerprint_attachments: list[dict],
    retention_days: int,
) -> dict:
    """Create immutable replay-safe attachments without the trigger marker."""
    from core.inbound_quarantine import (  # noqa: PLC0415
        content_scope_partition,
        open_quarantine_store,
    )

    store = open_quarantine_store()
    partition = content_scope_partition(org_id=org_id, datastream_id=datastream_id)
    manifest_attachments: list[dict] = []
    for ordinal, (attachment, evidence) in enumerate(
        zip(attachments, fingerprint_attachments, strict=True)
    ):
        data = attachment.get("data")
        if not isinstance(data, bytes):
            raise ValueError("attachment bytes missing for quarantine write")
        filename = str(attachment.get("name") or "attachment")
        content_sha256 = evidence["content_sha256"]
        # Full content SHA-256 is the address; organization and Datastream are
        # explicit ancestors. Ordinal keeps two identical attachments distinct
        # while still grouping them below the same content identity.
        object_name = f"{provider_reference_hash}-{ordinal:04d}"
        metadata = {
            "receipt_fingerprint": receipt_fingerprint,
            "receipt_reference": receipt_fingerprint,
            "attachment_ordinal": str(ordinal),
            "media_type": str(attachment.get("content_type") or "application/octet-stream"),
            "size_bytes": str(len(data)),
            "content_sha256": content_sha256,
            "provider_reference_sha256": provider_reference_hash,
            "retention_days": str(retention_days),
            "legal_hold": "false",
        }
        obj = store.put(
            partition=partition,
            message_id=content_sha256,
            filename=object_name,
            data=data,
            content_type=attachment.get("content_type"),
            metadata=metadata,
        )
        manifest_attachments.append({
            "ordinal": ordinal,
            "filename": filename,
            "quarantine_uri": obj.uri,
            "size": obj.size,
            "content_type": attachment.get("content_type"),
            "content_sha256": content_sha256,
        })
    return {
        "store": store,
        "partition": partition,
        "message_id": (
            "manifest-"
            + _sha256_hex(f"{receipt_fingerprint}:{provider_reference_hash}")
        ),
        "retention_policy": {
            "version": "quarantine-retention-v1", "days": retention_days,
        },
        "attachments": manifest_attachments,
    }


def _stage_existing_replay(
    *,
    org_id: str,
    datastream_id: str,
    receipt_fingerprint: str,
    provider_reference_hash: str,
    fingerprint_attachments: list[dict],
    existing_raws: list[dict],
) -> tuple[dict, int]:
    """Reload immutable evidence for an exact event replay.

    This deliberately ignores current deployment retention configuration: the
    original operation policy is immutable and must be replayed byte-for-byte.
    """
    from core.inbound_quarantine import (  # noqa: PLC0415
        content_scope_partition,
        open_quarantine_store,
    )

    if len(existing_raws) != len(fingerprint_attachments):
        raise ValueError("receipt replay attachment evidence is incomplete")
    attachments: list[dict] = []
    policy_pairs: set[tuple[str, int]] = set()
    for ordinal, (raw, expected) in enumerate(
        zip(existing_raws, fingerprint_attachments, strict=True)
    ):
        actual = (
            raw.get("ordinal"), raw.get("filename"),
            raw.get("media_type_declared"), raw.get("size_bytes"),
            raw.get("content_hash"),
        )
        wanted = (
            ordinal, expected["filename"], expected.get("content_type"),
            expected["size"], expected["content_sha256"],
        )
        if actual != wanted or not raw.get("quarantine_uri"):
            raise ValueError("receipt replay immutable attachment evidence differs")
        version = raw.get("retention_policy_version")
        days = raw.get("retention_days")
        if version != "quarantine-retention-v1" or not isinstance(days, int):
            raise ValueError("receipt replay retention evidence is incomplete")
        policy_pairs.add((version, days))
        attachments.append({
            "ordinal": ordinal, "filename": raw["filename"],
            "quarantine_uri": raw["quarantine_uri"],
            "size": raw["size_bytes"],
            "content_type": raw.get("media_type_declared"),
            "content_sha256": raw["content_hash"],
        })
    if len(policy_pairs) != 1:
        raise ValueError("receipt replay retention policy is inconsistent")
    version, retention_days = next(iter(policy_pairs))
    return ({
        "store": open_quarantine_store(),
        "partition": content_scope_partition(
            org_id=org_id, datastream_id=datastream_id
        ),
        "message_id": (
            "manifest-"
            + _sha256_hex(f"{receipt_fingerprint}:{provider_reference_hash}")
        ),
        "retention_policy": {"version": version, "days": retention_days},
        "attachments": attachments,
    }, retention_days)


def _sender_digests(address: str) -> dict:
    """``{sender_hash, sender_domain_hash}`` for a signed delivery, or ``{}``.

    Hashing happens HERE, in the internet-facing process, so the raw address
    never reaches durable storage. The declared allowlist is compared against
    these digests at processing (``core.inbound_sender_policy``); this process
    holds no database and makes no allow/deny decision of its own -- its answer
    stays the constant 403.
    """
    normalized = (address or "").strip().lower()
    if not normalized or "@" not in normalized:
        return {}
    domain = normalized.rsplit("@", 1)[1]
    if not domain:
        return {}
    return {
        "sender_hash": _sha256_hex(normalized),
        "sender_domain_hash": _sha256_hex(domain),
    }


def _publish_manifest(
    *,
    staged: dict,
    delivery,  # noqa: ANN001
    channel: str,
    token_hash: str,
    recipient_hash: str,
    receipt: dict,
    receipt_fingerprint: str,
    quarantine_uri: str | None,
) -> Exception | None:
    """Publish the reserved marker only after the receipt transaction commits."""
    # THE SENDER TRAVELS HASHED, never raw -- the same rule this module already
    # applies to the routing token and the recipient address. Two digests rather
    # than one so a Datastream can allow a whole domain without any address being
    # written down. Both are absent for a transport that carries no sender (a
    # webhook), and a manifest written before story 57.3 simply has neither.
    sender_digests = _sender_digests(getattr(delivery, "sender", ""))
    manifest = {
        "schema": _MANIFEST_SCHEMA,
        "receipt_id": receipt["receipt_id"],
        "receipt_fingerprint": receipt_fingerprint,
        "provider_event_id": delivery.provider_event_id,
        "channel": channel,
        "token_hash": token_hash,
        "recipient_hash": recipient_hash,
        **sender_digests,
        "received_at": receipt.get("created_at")
        or datetime.now(timezone.utc).isoformat(),
        "quarantine_uri": quarantine_uri,
        "retention_policy": staged["retention_policy"],
        "attachments": staged["attachments"],
    }
    try:
        staged["store"].put(
            partition=staged["partition"],
            message_id=staged["message_id"],
            filename=_MANIFEST_FILENAME,
            data=json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8"),
            content_type="application/json",
            metadata={
                "receipt_id": str(receipt["receipt_id"]),
                "receipt_fingerprint": receipt_fingerprint,
                "provider_reference_sha256": _sha256_hex(delivery.provider_event_id),
            },
        )
    except Exception as exc:  # noqa: BLE001
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
