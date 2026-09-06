"""Inbound processing worker: durable quarantine receipt -> import pipeline.

The public receipt service has already authenticated and authorized the
delivery, created immutable attachment objects, and committed receipt,
audit/outbox evidence before publishing the manifest marker. This worker
re-resolves the credential defensively, reconciles the durable receipt, reads
quarantined bytes, and drives every attachment through the same governed import
pipeline as a direct upload.

The caller finalizes delivery state. Managed publication may commit durable
dispatch phases internally; provider-event/operation idempotency makes redelivery
safe and terminal receipts never re-ingest.
"""

from __future__ import annotations

import hmac
import json
import logging
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger(__name__)

#: The manifest schema tag this worker understands.
_MANIFEST_SCHEMA = "inbound-delivery-manifest-v1"

#: The reserved filename of the manifest object itself (never a data attachment).
_MANIFEST_FILENAME = "_manifest.json"

#: Terminal receipt states: a receipt in one of these has a final outcome and a
#: redelivery must NOT be re-ingested (idempotent redelivery).
_TERMINAL_RECEIPT_STATES: frozenset[str] = frozenset({"LANDED", "REJECTED", "FAILED"})

#: Required top-level manifest keys.
_REQUIRED_MANIFEST_KEYS: tuple[str, ...] = (
    "schema",
    "receipt_id",
    "provider_event_id",
    "channel",
    "token_hash",
    "recipient_hash",
    "receipt_fingerprint",
    "retention_policy",
    "attachments",
)


# ---------------------------------------------------------------------------
# Typed exceptions.
# ---------------------------------------------------------------------------


class InboundProcessingValidationError(ValueError):
    """The delivery manifest is malformed (missing schema tag or required keys).

    A ValueError subclass so callers that already map 4xx off ValueError keep
    working; distinct type so the worker's own guards are separable.
    """


# ---------------------------------------------------------------------------
# Internal helpers.
# ---------------------------------------------------------------------------


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _parser_review_error_detail(exc: Exception) -> str | None:
    """Persist only bounded issue codes and defused field labels, never row values.

    AI-321 (2026-08-29): every OTHER typed refusal was persisted as its class name
    and nothing else -- `error_code = DatastreamNotIngestable`, `error_detail =`
    NULL -- so a person reading the raw import (or the QA walk reading it) knew
    the import had been refused and not WHY, while the refusal had a sentence
    naming the gesture. The sentence is kept, neutralised and bounded; a row
    value never reaches it because these refusals are raised before parsing.
    """
    from core.inbound_scan import neutralise_untrusted  # noqa: PLC0415

    if getattr(exc, "code", None) != "parser_review_required":
        sentence = neutralise_untrusted(str(exc) or None, max_len=500)
        return sentence or None

    repair = getattr(exc, "repair", {})
    raw_issues = repair.get("issues", []) if isinstance(repair, dict) else []
    issues: list[dict[str, str | None]] = []
    for raw in raw_issues[:20]:
        if not isinstance(raw, dict):
            continue
        code = str(raw.get("code") or "")[:64]
        if not code or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789_" for char in code):
            continue
        issues.append(
            {
                "code": code,
                "field": neutralise_untrusted(
                    str(raw["field"]) if raw.get("field") is not None else None,
                    max_len=120,
                ),
            }
        )
    return json.dumps({"schema": "parser-review-v1", "issues": issues}, sort_keys=True)


def _validate_manifest(manifest: Any) -> dict[str, Any]:
    """Validate the manifest shape; return it unchanged, else raise.

    Fail-closed before any lookup: an unrecognised schema tag or a missing
    required key is an ``InboundProcessingValidationError``.
    """
    if not isinstance(manifest, dict):
        raise InboundProcessingValidationError("manifest must be a mapping")
    schema = manifest.get("schema")
    if schema != _MANIFEST_SCHEMA:
        raise InboundProcessingValidationError(
            f"unexpected manifest schema {schema!r}; expected {_MANIFEST_SCHEMA!r}"
        )
    for key in _REQUIRED_MANIFEST_KEYS:
        if key not in manifest:
            raise InboundProcessingValidationError(f"manifest is missing required key {key!r}")
    receipt_id = manifest.get("receipt_id")
    if not isinstance(receipt_id, str) or not receipt_id.strip():
        raise InboundProcessingValidationError("manifest receipt_id must be a non-empty string")
    provider_event_id = manifest.get("provider_event_id")
    if (
        not isinstance(provider_event_id, str)
        or not provider_event_id.strip()
        or len(provider_event_id) > 512
    ):
        raise InboundProcessingValidationError(
            "manifest provider_event_id must be a bounded non-empty string"
        )
    channel = manifest.get("channel")
    if channel not in {"email", "webhook"}:
        raise InboundProcessingValidationError("manifest channel is invalid")
    token_hash = manifest.get("token_hash")
    if not isinstance(token_hash, str) or not _is_sha256(token_hash):
        raise InboundProcessingValidationError("manifest token_hash must be a lowercase SHA-256")
    recipient_hash = manifest.get("recipient_hash")
    if recipient_hash is not None and (
        not isinstance(recipient_hash, str) or not _is_sha256(recipient_hash)
    ):
        raise InboundProcessingValidationError(
            "manifest recipient_hash must be a lowercase SHA-256 or null"
        )
    fingerprint = manifest.get("receipt_fingerprint")
    if not isinstance(fingerprint, str) or not _is_sha256(fingerprint):
        raise InboundProcessingValidationError(
            "manifest receipt_fingerprint must be a lowercase SHA-256"
        )
    retention_policy = manifest.get("retention_policy")
    if not isinstance(retention_policy, dict):
        raise InboundProcessingValidationError("manifest retention_policy must be a mapping")
    if retention_policy.get("version") != "quarantine-retention-v1":
        raise InboundProcessingValidationError("manifest retention policy version is unsupported")
    retention_days = retention_policy.get("days")
    if (
        not isinstance(retention_days, int)
        or isinstance(retention_days, bool)
        or not 1 <= retention_days <= 3650
    ):
        raise InboundProcessingValidationError("manifest retention policy days are invalid")
    attachments = manifest.get("attachments")
    if not isinstance(attachments, list):
        raise InboundProcessingValidationError("manifest attachments must be a list")
    for index, attachment in enumerate(attachments):
        if not isinstance(attachment, dict):
            raise InboundProcessingValidationError(f"attachment {index} must be a mapping")
        if attachment.get("ordinal") != index:
            raise InboundProcessingValidationError(f"attachment {index} has an invalid ordinal")
        filename = attachment.get("filename")
        uri = attachment.get("quarantine_uri")
        size = attachment.get("size")
        digest = attachment.get("content_sha256")
        content_type = attachment.get("content_type")
        if not isinstance(filename, str) or not filename:
            raise InboundProcessingValidationError(f"attachment {index} filename is invalid")
        if not isinstance(uri, str) or not uri:
            raise InboundProcessingValidationError(f"attachment {index} quarantine_uri is invalid")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise InboundProcessingValidationError(f"attachment {index} size is invalid")
        if not isinstance(digest, str) or not _is_sha256(digest):
            raise InboundProcessingValidationError(f"attachment {index} content_sha256 is invalid")
        if content_type is not None and not isinstance(content_type, str):
            raise InboundProcessingValidationError(f"attachment {index} content_type is invalid")
    return manifest


def _select_data_attachments(
    attachments: list[dict[str, Any]],
) -> list[tuple[int, dict[str, Any]]]:
    """Return every validated descriptor, including a file named _manifest.json."""
    return [(attachment["ordinal"], attachment) for attachment in attachments]


def _receipt_quarantine_uri(
    manifest: dict[str, Any],
    data_attachments: list[tuple[int, dict[str, Any]]],
) -> str | None:
    """Pick the receipt's quarantine_uri: the manifest's own, else the first file.

    The receipt keeps ONE pointer because its grain is the delivery. The
    per-file pointers live on ``app.inbound_raw_imports``, one per attachment --
    this value is a convenience for the delivery-level read, never the authority
    on where a given attachment's bytes are.
    """
    own = manifest.get("quarantine_uri")
    if isinstance(own, str) and own:
        return own
    if data_attachments:
        first_uri = data_attachments[0][1].get("quarantine_uri")
        if isinstance(first_uri, str) and first_uri:
            return first_uri
    return None


def _resolve_project_id(conn, *, datastream_id: str) -> str | None:
    """Read the project_id for a datastream row; None if the row is absent."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT project_id FROM app.datastreams WHERE id = %s",
            (datastream_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return row[0]


def _resolve_org_id(conn, *, datastream_id: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT org_id FROM app.datastreams WHERE id = %s",
            (datastream_id,),
        )
        row = cur.fetchone()
    return row[0] if row else None


@contextmanager
def _attachment_savepoint(conn, *, ordinal: int):
    """Isolate one sibling on real PostgreSQL connections."""
    cursor_factory = getattr(conn, "cursor", None)
    if not callable(cursor_factory):
        yield
        return
    name = f"inbound_attachment_{ordinal}"
    with cursor_factory() as cur:
        cur.execute(f"SAVEPOINT {name}")
    try:
        yield
    except Exception:
        with cursor_factory() as cur:
            cur.execute(f"ROLLBACK TO SAVEPOINT {name}")
            cur.execute(f"RELEASE SAVEPOINT {name}")
        raise
    else:
        with cursor_factory() as cur:
            cur.execute(f"RELEASE SAVEPOINT {name}")


def _normalize_trace_id(trace_id: str | None) -> str | None:
    """Return a valid operations trace_id (32 lowercase hex) or None.

    A transport correlation id (e.g. a numeric Pub/Sub messageId) is not a valid
    operations trace_id; derive a stable 32-hex digest from any non-conforming
    value instead of letting execute_operation reject the write.
    """
    if not trace_id:
        return None
    import re as _re  # noqa: PLC0415

    if _re.fullmatch(r"[0-9a-f]{32}", trace_id):
        return trace_id
    import hashlib as _hashlib  # noqa: PLC0415

    return _hashlib.sha256(trace_id.encode("utf-8")).hexdigest()[:32]


def _process_one_attachment(
    conn,
    *,
    store,  # noqa: ANN001 -- a QuarantineStore (Protocol)
    ordinal: int,
    attachment: dict[str, Any],
    receipt_id: str,
    datastream_id: str,
    project_id: str,
    org_id: str,
    channel: str,
    provider_event_id: str,
    retention_policy_version: str,
    retention_days: int,
    actor: str,
    trace_id: str | None,
    persist_scan_intent: bool,
    ingest_inbound_file,  # noqa: ANN001 -- injected seam (lazy import at caller)
    typed_ingest_errors: tuple,
) -> dict[str, Any]:
    """Take ONE attachment from quarantined bytes to a terminal outcome.

    Returns a per-file outcome dict::

        {"ordinal": int, "status": "landed"|"rejected"|"failed"|"duplicate",
         "raw_import_id": str | None, "import_ledger_id": str | None,
         "error_code": str | None, "content_hash": str | None,
         "filename": str | None}

    This function never raises for an expected failure: every branch produces an
    outcome, because a raised exception here would abort the siblings, which is
    exactly the coupling Story 38.9 forbids.
    """
    from core.inbound_raw_imports import (  # noqa: PLC0415
        TERMINAL_RAW_IMPORT_STATES,
        mark_raw_import_state,
        record_raw_import,
    )
    from core.inbound_raw_imports import (
        content_hash as raw_content_hash,
    )

    filename = attachment.get("filename")
    declared_type = attachment.get("content_type")

    def _outcome(
        status: str,
        *,
        raw_import_id: str | None = None,
        import_ledger_id: str | None = None,
        error_code: str | None = None,
        digest: str | None = None,
        dispatch_result: dict[str, Any] | None = None,
        error_detail: str | None = None,
    ) -> dict[str, Any]:
        return {
            "ordinal": ordinal,
            "status": status,
            "raw_import_id": raw_import_id,
            "import_ledger_id": import_ledger_id,
            "error_code": error_code,
            # The sentence beside the code (AI-321): a person is told WHICH
            # column disappeared, not only that the import failed.
            "error_detail": error_detail,
            "content_hash": digest,
            # Untrusted display data, echoed back so an operator can recognise
            # which of their files this outcome is about.
            "filename": filename,
            "dispatch_result": dispatch_result,
        }

    # ------------------------------------------------------------------
    # The manifest carries the immutable evidence created before HTTP 202.
    # Reconcile that RECEIVED row before touching storage so unreadable or
    # corrupted objects still retain a durable per-attachment outcome.
    # ------------------------------------------------------------------
    expected_digest = attachment["content_sha256"]
    expected_size = attachment["size"]
    raw = record_raw_import(
        conn,
        receipt_id=receipt_id,
        datastream_id=datastream_id,
        ordinal=ordinal,
        size_bytes=expected_size,
        content_hash=expected_digest,
        filename=filename,
        media_type_declared=declared_type,
        quarantine_uri=attachment["quarantine_uri"],
        retention_policy_version=retention_policy_version,
        retention_days=retention_days,
        actor=actor,
        host_context={},
        trace_id=trace_id,
        idempotency_key=f"raw-import:{receipt_id}:{ordinal}",
    )
    raw_import_id = raw.get("raw_import_id")
    from core.inbound_scan import (  # noqa: PLC0415
        DEFAULT_MAX_BYTES,
        oversized_object_verdict,
    )

    if raw.get("deduplicated") and raw.get("state") in TERMINAL_RAW_IMPORT_STATES:
        return _outcome(
            "duplicate",
            raw_import_id=raw_import_id,
            import_ledger_id=raw.get("import_ledger_id"),
            error_code=raw.get("error_code"),
            digest=expected_digest,
        )

    if expected_size > DEFAULT_MAX_BYTES:
        verdict = oversized_object_verdict(
            declared_size=expected_size,
            declared_type=declared_type if isinstance(declared_type, str) else None,
        )
        mark_raw_import_state(
            conn,
            raw_import_id=raw_import_id,
            datastream_id=datastream_id,
            state="SCANNING",
            actor=actor,
            host_context={},
            trace_id=trace_id,
            idempotency_key=f"raw-import-state:{raw_import_id}:SCANNING",
        )
        mark_raw_import_state(
            conn,
            raw_import_id=raw_import_id,
            datastream_id=datastream_id,
            state="REJECTED",
            actor=actor,
            host_context={},
            trace_id=trace_id,
            idempotency_key=f"raw-import-state:{raw_import_id}:REJECTED",
            error_code=verdict.reason,
            media_type_detected=verdict.detected_type,
            scan_verdict=verdict.as_scan_verdict(),
        )
        return _outcome(
            "rejected",
            raw_import_id=raw_import_id,
            error_code=verdict.reason,
            digest=expected_digest,
        )

    from core.inbound_quarantine import QuarantineSizeError  # noqa: PLC0415

    try:
        data = store.get_bounded(
            attachment["quarantine_uri"],
            max_bytes=DEFAULT_MAX_BYTES,
            expected_org_id=org_id,
            expected_datastream_id=datastream_id,
            expected_content_hash=expected_digest,
        )
    except QuarantineSizeError as exc:
        verdict = oversized_object_verdict(
            declared_size=expected_size,
            declared_type=declared_type if isinstance(declared_type, str) else None,
            observed_size=exc.observed_size,
        )
        mark_raw_import_state(
            conn,
            raw_import_id=raw_import_id,
            datastream_id=datastream_id,
            state="SCANNING",
            actor=actor,
            host_context={},
            trace_id=trace_id,
            idempotency_key=f"raw-import-state:{raw_import_id}:SCANNING",
        )
        mark_raw_import_state(
            conn,
            raw_import_id=raw_import_id,
            datastream_id=datastream_id,
            state="REJECTED",
            actor=actor,
            host_context={},
            trace_id=trace_id,
            idempotency_key=f"raw-import-state:{raw_import_id}:REJECTED",
            error_code=verdict.reason,
            media_type_detected=verdict.detected_type,
            scan_verdict=verdict.as_scan_verdict(),
        )
        return _outcome(
            "rejected",
            raw_import_id=raw_import_id,
            error_code=verdict.reason,
            digest=expected_digest,
        )
    except Exception:  # noqa: BLE001
        mark_raw_import_state(
            conn,
            raw_import_id=raw_import_id,
            datastream_id=datastream_id,
            state="FAILED",
            actor=actor,
            host_context={},
            trace_id=trace_id,
            idempotency_key=f"raw-import-state:{raw_import_id}:FAILED",
            error_code="quarantine_read_error",
        )
        logger.warning(
            "inbound_processing: quarantine read failed ds=%s ordinal=%s trace=%s",
            datastream_id,
            ordinal,
            trace_id,
        )
        return _outcome(
            "failed",
            raw_import_id=raw_import_id,
            error_code="quarantine_read_error",
            digest=expected_digest,
        )

    digest = raw_content_hash(data)
    if len(data) != expected_size or not hmac.compare_digest(digest, expected_digest):
        mark_raw_import_state(
            conn,
            raw_import_id=raw_import_id,
            datastream_id=datastream_id,
            state="FAILED",
            actor=actor,
            host_context={},
            trace_id=trace_id,
            idempotency_key=f"raw-import-state:{raw_import_id}:FAILED",
            error_code="quarantine_integrity_mismatch",
        )
        return _outcome(
            "failed",
            raw_import_id=raw_import_id,
            error_code="quarantine_integrity_mismatch",
            digest=expected_digest,
        )

    if raw.get("deduplicated") and raw.get("state") in TERMINAL_RAW_IMPORT_STATES:
        # This exact file of this exact delivery already reached a final
        # outcome. Re-running it would create a second execution for evidence
        # that is already settled.
        logger.info(
            "inbound_processing: attachment already terminal ds=%s ordinal=%s state=%s trace=%s",
            datastream_id,
            ordinal,
            raw.get("state"),
            trace_id,
        )
        return _outcome(
            "duplicate",
            raw_import_id=raw_import_id,
            import_ledger_id=raw.get("import_ledger_id"),
            error_code=raw.get("error_code"),
            digest=digest,
        )

    # ------------------------------------------------------------------
    # Story 38.10: bound and scan BEFORE a parser is offered the bytes.
    #
    # This is the only gate between untrusted content and a parser, so it runs
    # unconditionally and its refusal is terminal. It returns a verdict rather
    # than raising, precisely so that one refused file cannot abort its
    # siblings -- the independence 38.9 AC1 requires.
    #
    # The verdict is recorded on the row (media_type_detected + scan_verdict),
    # which is why `media_type_declared` is frozen: the pair claim/detected is
    # the evidence, and it only means something if the claim cannot be edited
    # after the fact.
    # ------------------------------------------------------------------
    from core.inbound_discovery import (  # noqa: PLC0415
        POSTURE_DISCOVERY,
        delivery_posture,
    )
    from core.inbound_scan import scan_raw_import  # noqa: PLC0415

    posture = delivery_posture(conn, datastream_id=datastream_id)
    dispatch_bundle_resolver = None
    if (
        posture != POSTURE_DISCOVERY
        and getattr(ingest_inbound_file, "__module__", "") == "core.inbound_ingest"
    ):
        from core.inbound_ingest import (  # noqa: PLC0415
            resolve_dispatch_bundle_for_acceptance,
        )

        def dispatch_bundle_resolver() -> dict[str, Any]:
            # Runs only after the bytes pass scanning. The resolver locks the
            # current pins and ACCEPTED persists that exact bundle in the same
            # database transaction.
            return resolve_dispatch_bundle_for_acceptance(
                conn,
                datastream_id=datastream_id,
                project_id=project_id,
            )

    verdict = scan_raw_import(
        conn,
        raw_import_id=raw_import_id,
        datastream_id=datastream_id,
        data=data,
        declared_type=declared_type if isinstance(declared_type, str) else None,
        actor=actor,
        trace_id=trace_id,
        persist_intent=persist_scan_intent,
        dispatch_bundle_resolver=dispatch_bundle_resolver,
    )
    if not verdict.accepted:
        return _outcome(
            "rejected",
            raw_import_id=raw_import_id,
            error_code=verdict.reason,
            digest=digest,
        )

    # ------------------------------------------------------------------
    # THE FIRST DELIVERY REVEALS THE SHAPE (AI-113).
    #
    # A Datastream in `draft` has never published anything, so there is nothing
    # for this file to be ingested INTO -- and `ingest_inbound_file` would
    # rightly refuse it, leaving the attachment FAILED and the operator with a
    # file that is safe, hashed, scanned and useless.
    #
    # It is ACCEPTED instead. The bytes stay retained and describable, and
    # `inbound_discovery.observe_first_delivery` turns them into the same setup
    # observation an uploaded file produces -- so the wizard can show the shape
    # and a human can validate it. Nothing is published here; that still takes
    # the activation path and a confirmation.
    #
    # Only `draft` takes this branch. An ACTIVE Datastream missing a plan or a
    # mapping is a real misconfiguration and must keep failing loudly: routing
    # it here would trade an actionable failure for a silent description.
    # ------------------------------------------------------------------
    if posture == POSTURE_DISCOVERY:
        # No state write here: the scan gate above already left this row
        # ACCEPTED, and re-marking it would be a second audited operation
        # asserting a transition that did not happen. ACCEPTED already means
        # exactly what this branch needs it to mean -- the bytes passed the
        # controls and may be described.
        logger.info(
            "inbound_processing: retained for setup review ds=%s ordinal=%s trace=%s",
            datastream_id,
            ordinal,
            trace_id,
        )
        return _outcome(
            "observed",
            raw_import_id=raw_import_id,
            digest=digest,
        )

    # ------------------------------------------------------------------
    # Drive THIS file through the same import pipeline a direct upload uses.
    #
    # The per-attachment message_id is load-bearing: `run_import` is idempotent
    # on it, so passing the bare provider_event_id for every attachment of one
    # delivery would make attachments 2..N replay attachment 1's import instead
    # of creating their own. Suffixing with the ordinal is what keeps N files
    # into N executions.
    # ------------------------------------------------------------------
    try:
        result = ingest_inbound_file(
            conn,
            datastream_id=datastream_id,
            project_id=project_id,
            file_bytes=data,
            filename=filename,
            channel=channel,
            message_id=f"{provider_event_id}#{ordinal}",
            actor=actor,
            trace_id=trace_id,
            raw_import_id=raw_import_id,
        )
    except typed_ingest_errors as exc:
        error_code = getattr(exc, "code", type(exc).__name__)
        if error_code in {
            "dispatch_promotion_reconciliation_required",
            "publication_reconciliation_required",
            "dispatch_reconciliation_required",
        }:
            return _outcome(
                "reconcile_required",
                raw_import_id=raw_import_id,
                error_code=error_code,
                digest=digest,
            )
        mark_raw_import_state(
            conn,
            raw_import_id=raw_import_id,
            datastream_id=datastream_id,
            state="FAILED",
            actor=actor,
            host_context={},
            trace_id=trace_id,
            idempotency_key=f"raw-import-state:{raw_import_id}:FAILED",
            error_code=error_code,
            error_detail=_parser_review_error_detail(exc),
        )
        logger.warning(
            "inbound_processing: ingest rejected ds=%s ordinal=%s error=%s trace=%s",
            datastream_id,
            ordinal,
            error_code,
            trace_id,
        )
        return _outcome(
            "failed",
            raw_import_id=raw_import_id,
            error_code=error_code,
            error_detail=_parser_review_error_detail(exc),
            digest=digest,
        )

    if result.get("blocked"):
        reason = result.get("reason")
        if reason == "dispatch_reconciliation_required":
            return _outcome(
                "reconcile_required",
                raw_import_id=raw_import_id,
                error_code=reason,
                digest=digest,
                dispatch_result=result,
            )
        mark_raw_import_state(
            conn,
            raw_import_id=raw_import_id,
            datastream_id=datastream_id,
            state="REJECTED",
            actor=actor,
            host_context={},
            trace_id=trace_id,
            idempotency_key=f"raw-import-state:{raw_import_id}:REJECTED",
            error_code=reason,
        )
        return _outcome(
            "rejected",
            raw_import_id=raw_import_id,
            error_code=reason,
            digest=digest,
        )

    ledger = result.get("ledger")
    import_ledger_id = ledger.get("id") if isinstance(ledger, dict) else None

    mark_raw_import_state(
        conn,
        raw_import_id=raw_import_id,
        datastream_id=datastream_id,
        state="LANDED",
        actor=actor,
        host_context={},
        trace_id=trace_id,
        idempotency_key=f"raw-import-state:{raw_import_id}:LANDED",
        import_ledger_id=import_ledger_id,
    )
    return _outcome(
        "landed",
        raw_import_id=raw_import_id,
        import_ledger_id=import_ledger_id,
        digest=digest,
        dispatch_result=result,
    )


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------


def process_authorized_upload(
    conn,
    *,
    datastream_id: str,
    project_id: str,
    file_bytes: bytes,
    filename: str | None,
    media_type: str | None,
    actor: str,
    idempotency_key: str,
    trace_id: str | None = None,
    store=None,  # noqa: ANN001
) -> dict[str, Any]:
    """Route an authorized Upload through receipt, quarantine, scan and dispatch.

    Authorization belongs to the REST/MCP caller. This function rechecks the
    Datastream/Project scope, persists the same immutable evidence as asynchronous
    channels, then invokes the exact same attachment worker.
    """
    import hashlib  # noqa: PLC0415

    if not file_bytes:
        raise InboundProcessingValidationError("file_bytes is empty")
    if not idempotency_key or not idempotency_key.strip():
        raise InboundProcessingValidationError("idempotency_key is required")
    if _resolve_project_id(conn, datastream_id=datastream_id) != project_id:
        raise InboundProcessingValidationError("datastream not found")
    org_id = _resolve_org_id(conn, datastream_id=datastream_id)
    if not org_id:
        raise InboundProcessingValidationError("datastream organization unavailable")

    digest = hashlib.sha256(file_bytes).hexdigest()
    provider_event_id = "upload-" + hashlib.sha256(
        f"{datastream_id}:{idempotency_key}".encode("utf-8")
    ).hexdigest()
    trace_id = _normalize_trace_id(trace_id)

    from core.inbound_quarantine import (  # noqa: PLC0415
        content_scope_partition,
        open_quarantine_store,
    )
    from core.inbound_receipts import (  # noqa: PLC0415
        canonical_receipt_fingerprint,
        get_receipt,
        mark_state,
        record_receipt,
    )

    store = store or open_quarantine_store()
    attachment_evidence = {
        "ordinal": 0,
        "filename": filename or "upload",
        "size": len(file_bytes),
        "content_type": media_type,
        "content_sha256": digest,
    }
    receipt_fingerprint = canonical_receipt_fingerprint(
        datastream_id=datastream_id,
        credential_id=None,
        channel="upload",
        recipient_hash=None,
        attachments=[attachment_evidence],
    )
    receipt = record_receipt(
        conn,
        datastream_id=datastream_id,
        credential_id=None,
        channel="upload",
        provider_event_id=provider_event_id,
        recipient_hash=None,
        receipt_fingerprint=receipt_fingerprint,
        attachment_count=1,
        total_bytes=len(file_bytes),
        quarantine_uri=None,
        actor=actor,
        host_context={},
        trace_id=trace_id,
        idempotency_key=f"receipt:{datastream_id}:{provider_event_id}",
    )
    receipt_id = receipt["receipt_id"]
    persisted_receipt = get_receipt(
        conn, receipt_id=receipt_id, datastream_id=datastream_id
    )
    if persisted_receipt["state"] in {"LANDED", "REJECTED", "FAILED"}:
        terminal_status = {
            "LANDED": "landed",
            "REJECTED": "rejected",
            "FAILED": "failed",
        }[persisted_receipt["state"]]
        return {
            "status": terminal_status,
            "receipt_id": receipt_id,
            "datastream_id": datastream_id,
            "import_ledger_id": persisted_receipt.get("import_ledger_id"),
            "attachments": [],
            "dispatch_result": None,
            "replayed": True,
        }
    partition = content_scope_partition(org_id=org_id, datastream_id=datastream_id)
    stored = store.put(
        partition=partition,
        message_id=digest,
        filename=hashlib.sha256(provider_event_id.encode("utf-8")).hexdigest(),
        data=file_bytes,
        content_type=media_type,
        metadata={
            "content_sha256": digest,
            "size_bytes": str(len(file_bytes)),
            "channel": "upload",
        },
    )
    attachment = {
        **attachment_evidence,
        "quarantine_uri": stored.uri,
        "size": stored.size,
    }
    mark_state(
        conn,
        receipt_id=receipt_id,
        datastream_id=datastream_id,
        state="PROCESSING",
        actor=actor,
        host_context={},
        trace_id=trace_id,
        idempotency_key=f"receipt-state:{receipt_id}:PROCESSING",
    )

    from core.csv_excel_import import CsvExcelImportError  # noqa: PLC0415
    from core.datastream_publication import PublicationError  # noqa: PLC0415
    from core.inbound_ingest import (  # noqa: PLC0415
        DatastreamNotIngestable,
        InboundIngestValidationError,
        ingest_inbound_file,
    )
    from core.managed_feed_ledger import ManagedFeedError  # noqa: PLC0415
    from core.managed_file_dispatch import ManagedFileDispatchError  # noqa: PLC0415

    outcome = _process_one_attachment(
        conn,
        store=store,
        ordinal=0,
        attachment=attachment,
        receipt_id=receipt_id,
        datastream_id=datastream_id,
        project_id=project_id,
        org_id=org_id,
        channel="upload",
        provider_event_id=provider_event_id,
        retention_policy_version="quarantine-retention-v1",
        retention_days=30,
        actor=actor,
        trace_id=trace_id,
        ingest_inbound_file=ingest_inbound_file,
        persist_scan_intent=False,
        typed_ingest_errors=(
            InboundIngestValidationError,
            DatastreamNotIngestable,
            CsvExcelImportError,
            ManagedFeedError,
            ManagedFileDispatchError,
            PublicationError,
        ),
    )
    if outcome["status"] == "reconcile_required":
        return {
            "status": "reconcile_required",
            "receipt_id": receipt_id,
            "datastream_id": datastream_id,
            "import_ledger_id": outcome.get("import_ledger_id"),
            "attachments": [outcome],
            "dispatch_result": outcome.get("dispatch_result"),
        }
    if outcome["status"] == "landed":
        receipt_state, status = "LANDED", "landed"
    elif outcome["status"] in {"rejected", "duplicate"}:
        receipt_state, status = "REJECTED", outcome["status"]
    elif outcome["status"] == "observed":
        return {
            "status": "observed",
            "receipt_id": receipt_id,
            "datastream_id": datastream_id,
            "attachments": [outcome],
        }
    else:
        receipt_state, status = "FAILED", "failed"
    mark_state(
        conn,
        receipt_id=receipt_id,
        datastream_id=datastream_id,
        state=receipt_state,
        actor=actor,
        host_context={},
        trace_id=trace_id,
        idempotency_key=f"receipt-state:{receipt_id}:{receipt_state}",
        error_code=outcome.get("error_code"),
        import_ledger_id=outcome.get("import_ledger_id"),
    )
    return {
        "status": status,
        "receipt_id": receipt_id,
        "datastream_id": datastream_id,
        "import_ledger_id": outcome.get("import_ledger_id"),
        "attachments": [outcome],
        "dispatch_result": outcome.get("dispatch_result"),
    }


def process_inbound_delivery(
    conn,
    *,
    manifest: dict,
    store=None,  # noqa: ANN001 -- a QuarantineStore (Protocol); resolved lazily
    actor: str = "inbound-worker",
    trace_id: str | None = None,
    attachment_ordinals: set[int] | None = None,
    defer_receipt_finalization: bool = False,
    persist_scan_intent: bool = False,
) -> dict:
    """Process one quarantined inbound delivery end-to-end (fail-closed).

    Steps:
      1. Validate the manifest shape (schema tag + required keys).
      2. Resolve the routing token_hash to a governed Datastream. If the token is
         unknown/denied, record NOTHING and return a constant-shape
         ``{"status": "denied"}`` (non-enumerating).
      3. Record a durable receipt (idempotent on provider_event_id). If the
         receipt was already terminal (LANDED/REJECTED/FAILED), short-circuit to
         ``{"status": "duplicate"}`` WITHOUT re-ingesting.
      4. Mark the receipt PROCESSING.
      5. Select the data attachments (ignore the manifest object). Zero -> mark
         FAILED (no_data_attachment).
      6. Resolve the Datastream's project_id.
      7. For EACH attachment, independently: read its bytes, record one
         immutable raw import, drive it through ``ingest_inbound_file`` (the
         same pipeline a direct upload uses) and mark its own terminal state.
      8. Fold the per-file outcomes into ONE receipt state, without letting the
         fold hide a partial failure.

    The CALLER owns the transaction: this function NEVER commits or rolls back.

    Returns a structured dict::

      {"status": "landed"|"rejected"|"failed"|"duplicate"|"denied",
       "receipt_id": str | None,
       "datastream_id": str | None,
       "import_ledger_id": str | None,   # FIRST landed file; see `attachments`
       "ignored_attachments": list[str], # always [], kept for old callers
       "attachments": [                  # the authoritative per-file answer
         {"ordinal": int, "status": str, "raw_import_id": str | None,
          "import_ledger_id": str | None, "error_code": str | None,
          "content_hash": str | None, "filename": str | None}, ...]}

    Raises:
      InboundProcessingValidationError: the manifest is malformed.
    """
    # Lazy imports of the sibling modules (avoid import cycles; matches
    # inbound_ingest.py style).
    from core.csv_excel_import import CsvExcelImportError  # noqa: PLC0415
    from core.datastream_publication import PublicationError  # noqa: PLC0415
    from core.inbound_credentials import resolve_by_token_hash  # noqa: PLC0415
    from core.inbound_ingest import (  # noqa: PLC0415
        DatastreamNotIngestable,
        InboundIngestValidationError,
        ingest_inbound_file,
    )
    from core.inbound_receipts import (  # noqa: PLC0415
        get_receipt,
        get_receipt_by_provider_event,
        mark_state,
        record_receipt,
    )
    from core.managed_feed_ledger import ManagedFeedError  # noqa: PLC0415
    from core.managed_file_dispatch import ManagedFileDispatchError  # noqa: PLC0415
    from core.operations import OperationIdempotencyConflict  # noqa: PLC0415

    # A transport correlation id (e.g. a Pub/Sub messageId) is NOT a valid
    # operations trace_id (which must be 32 lowercase hex, per execute_operation).
    # Normalise it so a well-formed delivery never 500s on the trace_id contract:
    # keep a valid 32-hex id, derive a stable one from any other non-empty value,
    # else None.
    trace_id = _normalize_trace_id(trace_id)

    # ------------------------------------------------------------------
    # Step 1: validate manifest shape (fail-closed before any lookup).
    # ------------------------------------------------------------------
    manifest = _validate_manifest(manifest)
    provider_event_id = manifest["provider_event_id"].strip()
    token_hash = manifest["token_hash"].strip()
    recipient_hash = manifest.get("recipient_hash")
    raw_attachments = manifest.get("attachments") or []
    receipt_fingerprint = manifest["receipt_fingerprint"]
    retention_policy = manifest["retention_policy"]
    retention_policy_version = retention_policy["version"]
    retention_days = retention_policy["days"]

    # ------------------------------------------------------------------
    # Step 2: resolve the routing token_hash to a Datastream.
    # Unknown/denied -> record NOTHING; constant-shape non-enumerating denial.
    # (Log nothing sensitive -- no token_hash, no recipient_hash.)
    # ------------------------------------------------------------------
    resolution = resolve_by_token_hash(conn, token_hash=token_hash)
    if not resolution.get("allowed"):
        logger.info("inbound_processing: delivery denied trace=%s", trace_id)
        return {
            "status": "denied",
            "receipt_id": None,
            "datastream_id": None,
            "import_ledger_id": None,
            "ignored_attachments": [],
        }

    scope = resolution.get("scope") or {}
    datastream_id = scope.get("datastream_id")
    credential_id = scope.get("credential_id")
    channel = scope.get("channel")
    if manifest["channel"] != channel:
        raise InboundProcessingValidationError(
            "manifest channel does not match the resolved credential"
        )

    from core.inbound_receipts import canonical_receipt_fingerprint  # noqa: PLC0415

    canonical_fingerprint = canonical_receipt_fingerprint(
        datastream_id=datastream_id,
        credential_id=credential_id,
        channel=channel,
        recipient_hash=recipient_hash,
        attachments=raw_attachments,
    )
    if not hmac.compare_digest(receipt_fingerprint, canonical_fingerprint):
        raise InboundProcessingValidationError(
            "manifest receipt fingerprint does not match its attachment evidence"
        )

    # Compute receipt aggregates from the manifest attachments.
    data_attachments = _select_data_attachments(raw_attachments)
    total_bytes = 0
    for att in raw_attachments:
        if isinstance(att, dict):
            size = att.get("size")
            if isinstance(size, int) and size > 0:
                total_bytes += size
    quarantine_uri = _receipt_quarantine_uri(manifest, data_attachments)

    # ------------------------------------------------------------------
    # Step 3: record the receipt (idempotent on provider_event_id). Only now,
    # once we know the Datastream, is a receipt written. If the redelivered
    # receipt is already terminal, short-circuit WITHOUT re-ingesting.
    # ------------------------------------------------------------------
    # DEUX ECRIVAINS, UN SEUL RECU. Depuis que le service de reception ecrit
    # lui-meme le recu durable (stories 38.8 / 38.9), il l'a DEJA pose quand le
    # bridge arrive. Les deux emploient la meme cle d'idempotence -- c'est le
    # meme acte -- mais pas la meme charge : le service signe `inbound-receipt`
    # et ne porte pas les empreintes d'expediteur du manifeste. `execute_operation`
    # voyait donc une cle connue liee a une requete differente et levait
    # `OperationIdempotencyConflict`, ce qui laissait la livraison bloquee en
    # RECEIVED pour toujours. Mesure vivante 2026-08-08, premier e-mail reel :
    # `inbrx_01KZFFZKCSD7ZYAYWJBJ7ATQ7V`.
    #
    # Le bridge REPREND donc le recu au lieu de le reecrire. C'est la seule
    # lecture juste : celui qui a recu les octets est celui qui atteste les avoir
    # recus, et le bridge attesterait apres coup un fait dont il n'a pas ete
    # temoin.
    try:
        receipt = record_receipt(
            conn,
            datastream_id=datastream_id,
            credential_id=credential_id,
            channel=channel,
            provider_event_id=provider_event_id,
            recipient_hash=recipient_hash,
            # WHO SENT IT, already hashed by the internet-facing handler and stored
            # here so the replay path can read it (story 57.3). The manifest is the
            # only place these digests exist, and a replay -- hours or weeks later --
            # never sees a manifest. Absent for a webhook, and for any receipt
            # written before migration 214.
            sender_hash=manifest.get("sender_hash"),
            sender_domain_hash=manifest.get("sender_domain_hash"),
            receipt_fingerprint=receipt_fingerprint,
            attachment_count=len(data_attachments),
            total_bytes=total_bytes or None,
            quarantine_uri=quarantine_uri,
            actor=actor,
            host_context={},
            trace_id=trace_id,
            idempotency_key=f"receipt:{datastream_id}:{provider_event_id}",
        )
    except OperationIdempotencyConflict:
        # LE RECU EXISTE DEJA, ecrit par le service qui a recu les octets.
        # Meme cle -- c'est le meme acte -- mais pas la meme charge : lui
        # signe `inbound-receipt` et ne porte pas les empreintes
        # d'expediteur du manifeste. Le conflit EST le signal, et le
        # reprendre est la seule lecture juste : celui qui a recu atteste,
        # le bridge poursuit.
        receipt = get_receipt_by_provider_event(
            conn, datastream_id=datastream_id, provider_event_id=provider_event_id
        )
        if receipt is None:
            raise
    receipt_id = receipt.get("receipt_id")
    if receipt_id != manifest["receipt_id"]:
        raise InboundProcessingValidationError(
            "manifest receipt_id does not match durable receipt evidence"
        )

    if receipt.get("deduplicated"):
        existing = get_receipt(conn, receipt_id=receipt_id, datastream_id=datastream_id)
        existing_state = (existing or {}).get("state")
        if existing_state in _TERMINAL_RECEIPT_STATES:
            logger.info(
                "inbound_processing: duplicate terminal receipt ds=%s state=%s trace=%s",
                datastream_id,
                existing_state,
                trace_id,
            )
            return {
                "status": "duplicate",
                "receipt_id": receipt_id,
                "datastream_id": datastream_id,
                "import_ledger_id": (existing or {}).get("import_ledger_id"),
                "ignored_attachments": [],
            }

    # ------------------------------------------------------------------
    # Step 4: mark PROCESSING.
    # ------------------------------------------------------------------
    mark_state(
        conn,
        receipt_id=receipt_id,
        datastream_id=datastream_id,
        state="PROCESSING",
        actor=actor,
        host_context={},
        trace_id=trace_id,
        idempotency_key=f"receipt-state:{receipt_id}:PROCESSING",
    )

    # ------------------------------------------------------------------
    # Step 5: select data attachment(s).
    # ------------------------------------------------------------------
    if attachment_ordinals is not None:
        data_attachments = [item for item in data_attachments if item[0] in attachment_ordinals]

    if not data_attachments:
        mark_state(
            conn,
            receipt_id=receipt_id,
            datastream_id=datastream_id,
            state="FAILED",
            actor=actor,
            host_context={},
            trace_id=trace_id,
            idempotency_key=f"receipt-state:{receipt_id}:FAILED",
            error_code="no_data_attachment",
        )
        logger.warning(
            "inbound_processing: no data attachment ds=%s trace=%s",
            datastream_id,
            trace_id,
        )
        return {
            "status": "failed",
            "receipt_id": receipt_id,
            "datastream_id": datastream_id,
            "import_ledger_id": None,
            "ignored_attachments": [],
        }

    # Nothing is ignored any more: every data attachment gets its own raw
    # import, its own execution and its own outcome (Story 38.9 AC1). The key is
    # kept in the return value, permanently empty, because callers and tests
    # written against the single-attachment shortcut still read it.
    ignored_attachments: list[str] = []

    # ------------------------------------------------------------------
    # Step 6: resolve project_id for the Datastream.
    # ------------------------------------------------------------------
    project_id = _resolve_project_id(conn, datastream_id=datastream_id)
    org_id = _resolve_org_id(conn, datastream_id=datastream_id)
    if project_id is None or org_id is None:
        mark_state(
            conn,
            receipt_id=receipt_id,
            datastream_id=datastream_id,
            state="FAILED",
            actor=actor,
            host_context={},
            trace_id=trace_id,
            idempotency_key=f"receipt-state:{receipt_id}:FAILED",
            error_code="datastream_not_found",
        )
        return {
            "status": "failed",
            "receipt_id": receipt_id,
            "datastream_id": datastream_id,
            "import_ledger_id": None,
            "ignored_attachments": ignored_attachments,
        }

    # ------------------------------------------------------------------
    # Step 7: process EACH attachment independently.
    #
    from core.inbound_scan import ScanRetryableError  # noqa: PLC0415

    # One attachment == one immutable raw import == one execution == one
    # outcome. Nothing in this loop lets one file decide another's fate: that
    # independence IS the acceptance criterion (38.9 AC1), not a nicety.
    # ------------------------------------------------------------------
    if store is None:
        from core.inbound_quarantine import open_quarantine_store  # noqa: PLC0415

        store = open_quarantine_store()

    attachment_outcomes: list[dict[str, Any]] = []

    for ordinal, attachment in data_attachments:
        try:
            # Managed publication commits a durable dispatch intent between
            # cross-store phases. A surrounding SQL savepoint cannot survive
            # those intentional commit boundaries; sibling isolation is instead
            # provided by one raw import / ledger / execution / dispatch per file.
            commit = getattr(conn, "commit", None)
            if callable(commit):
                commit()  # sibling boundary: prior evidence cannot be rolled back
            outcome = _process_one_attachment(
                conn,
                store=store,
                ordinal=ordinal,
                attachment=attachment,
                receipt_id=receipt_id,
                datastream_id=datastream_id,
                project_id=project_id,
                org_id=org_id,
                channel=manifest["channel"],
                provider_event_id=provider_event_id,
                retention_policy_version=retention_policy_version,
                retention_days=retention_days,
                actor=actor,
                trace_id=trace_id,
                ingest_inbound_file=ingest_inbound_file,
                persist_scan_intent=persist_scan_intent,
                typed_ingest_errors=(
                    InboundIngestValidationError,
                    DatastreamNotIngestable,
                    CsvExcelImportError,
                    ManagedFeedError,
                    ManagedFileDispatchError,
                    PublicationError,
                ),
            )
            if callable(commit):
                commit()  # persist this sibling before the next begins
        except ScanRetryableError:
            raise
        except Exception as exc:  # noqa: BLE001 -- sibling isolation boundary
            # Clear a failed statement before persisting sibling evidence. Durable
            # dispatch phase commits are already journalled and remain recoverable.
            rollback = getattr(conn, "rollback", None)
            if callable(rollback):
                rollback()
            stable_code = getattr(exc, "code", None)
            if not isinstance(stable_code, str) or not stable_code:
                stable_code = "attachment_processing_error"
            stable_code = stable_code[:128]
            logger.exception(
                "inbound_processing: unexpected sibling failure ds=%s ordinal=%s",
                datastream_id,
                ordinal,
            )
            failed_raw_id = None
            try:
                from core.inbound_raw_imports import (  # noqa: PLC0415
                    TERMINAL_RAW_IMPORT_STATES,
                    mark_raw_import_state,
                    record_raw_import,
                )

                failed_raw = record_raw_import(
                    conn,
                    receipt_id=receipt_id,
                    datastream_id=datastream_id,
                    ordinal=ordinal,
                    size_bytes=attachment["size"],
                    content_hash=attachment["content_sha256"],
                    filename=attachment["filename"],
                    media_type_declared=attachment.get("content_type"),
                    quarantine_uri=attachment["quarantine_uri"],
                    retention_policy_version=retention_policy_version,
                    retention_days=retention_days,
                    actor=actor,
                    host_context={},
                    trace_id=trace_id,
                    idempotency_key=f"raw-import:{receipt_id}:{ordinal}",
                )
                failed_raw_id = failed_raw["raw_import_id"]
                if failed_raw.get("state") not in TERMINAL_RAW_IMPORT_STATES:
                    mark_raw_import_state(
                        conn,
                        raw_import_id=failed_raw_id,
                        datastream_id=datastream_id,
                        state="FAILED",
                        actor=actor,
                        host_context={},
                        trace_id=trace_id,
                        idempotency_key=(f"raw-import-state:{failed_raw_id}:FAILED"),
                        error_code=stable_code,
                    )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "inbound_processing: could not persist sibling failure ds=%s ordinal=%s",
                    datastream_id,
                    ordinal,
                )
            # AND THE RUN, so the Runs tab can show this failure at all.
            #
            # `spec-43-17b:26` forbids using the managed-feed ledger as the run
            # universe: `app.datastream_executions` is that universe and Runs
            # reads it. A delivery that aborted before `open_import` wrote only
            # to `app.inbound_raw_imports`, so a person checking whether their
            # file had failed was shown an EMPTY run list. Three review lenses
            # traced this path independently.
            #
            # Its own try: recording the run must never mask the sibling failure
            # it is recording, and a Datastream with no executable plan/mapping
            # pair returns None rather than inventing a run that never was.
            failed_execution_id = None
            try:
                from core.datastream_publication import (  # noqa: PLC0415
                    record_failed_execution,
                )

                failed_execution_id = record_failed_execution(
                    conn,
                    project_id=project_id,
                    datastream_id=datastream_id,
                    actor=actor,
                    error_code=stable_code,
                    error_detail="Inbound delivery failed before any import opened.",
                    idempotency_key=f"inbound-failure:{receipt_id}:{ordinal}",
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "inbound_processing: could not record failed run ds=%s ordinal=%s",
                    datastream_id,
                    ordinal,
                )
            outcome = {
                "ordinal": ordinal,
                "status": "failed",
                "raw_import_id": failed_raw_id,
                "execution_id": failed_execution_id,
                "import_ledger_id": None,
                "error_code": stable_code,
                "content_hash": attachment.get("content_sha256"),
                "filename": attachment.get("filename"),
            }
        attachment_outcomes.append(outcome)

    # ------------------------------------------------------------------
    if defer_receipt_finalization:
        outcome = (
            attachment_outcomes[0]
            if attachment_outcomes
            else {
                "status": "failed",
                "raw_import_id": None,
                "error_code": "scan_job_attachment_missing",
            }
        )
        return {
            "status": outcome["status"],
            "receipt_id": receipt_id,
            "datastream_id": datastream_id,
            "attachments": attachment_outcomes,
        }

    # Step 8: fold the per-attachment outcomes into ONE receipt state.
    #
    # The fold is deliberately optimistic-with-evidence: if ANY attachment
    # landed, the delivery landed -- because it did, and saying otherwise would
    # hide real data that is now queryable. A partial failure is not swallowed
    # either: `error_code` carries `partial_attachment_failure` on a LANDED
    # receipt, and the per-file states remain the authority. The receipt is a
    # summary; `app.inbound_raw_imports` is the record.
    # ------------------------------------------------------------------
    landed = [o for o in attachment_outcomes if o["status"] == "landed"]
    rejected = [o for o in attachment_outcomes if o["status"] == "rejected"]
    observed = [o for o in attachment_outcomes if o["status"] == "observed"]
    reconciling = [
        o for o in attachment_outcomes if o["status"] == "reconcile_required"
    ]
    duplicates = [o for o in attachment_outcomes if o["status"] == "duplicate"]

    first_ledger_id = landed[0]["import_ledger_id"] if landed else None

    if reconciling and not landed and not rejected:
        return {
            "status": "reconcile_required",
            "receipt_id": receipt_id,
            "datastream_id": datastream_id,
            "import_ledger_id": None,
            "ignored_attachments": ignored_attachments,
            "attachments": attachment_outcomes,
        }

    if observed and not landed and not rejected:
        # Every attachment is retained for setup review (a draft Datastream).
        # This is NOT a failure and must not be recorded as one: the delivery
        # arrived, it is intact, and it is waiting for a human -- which is
        # exactly what the journey asks of it. The receipt stays PROCESSING
        # because its outcome genuinely is not settled yet.
        return {
            "status": "observed",
            "receipt_id": receipt_id,
            "datastream_id": datastream_id,
            "import_ledger_id": None,
            "ignored_attachments": ignored_attachments,
            "attachments": attachment_outcomes,
        }

    if landed:
        receipt_state = "LANDED"
        status = "landed"
        error_code = (
            "partial_attachment_failure" if len(landed) != len(attachment_outcomes) else None
        )
    elif rejected:
        receipt_state = "REJECTED"
        status = "rejected"
        error_code = rejected[0]["error_code"]
    elif duplicates:
        receipt_state = "REJECTED"
        status = "duplicate"
        error_code = "duplicate_content_skipped"
    else:
        receipt_state = "FAILED"
        status = "failed"
        error_code = attachment_outcomes[0]["error_code"] if attachment_outcomes else None

    mark_state(
        conn,
        receipt_id=receipt_id,
        datastream_id=datastream_id,
        state=receipt_state,
        actor=actor,
        host_context={},
        trace_id=trace_id,
        idempotency_key=f"receipt-state:{receipt_id}:{receipt_state}",
        error_code=error_code,
        import_ledger_id=first_ledger_id,
    )

    return {
        "status": status,
        "receipt_id": receipt_id,
        "datastream_id": datastream_id,
        # The delivery-level ledger id stays for compatibility with the callers
        # written before per-attachment evidence existed. With more than one
        # attachment it is the FIRST landed one, and `attachments` below is the
        # complete answer -- a reader that needs every ledger id must use that.
        "import_ledger_id": first_ledger_id,
        "ignored_attachments": ignored_attachments,
        "attachments": attachment_outcomes,
    }
