"""toorow -- Inbound processing worker: quarantine delivery -> import pipeline.

The internet-facing receipt service verifies a provider signature, writes the
delivered file bytes plus a MANIFEST to a quarantine store, and stops there --
it has NO database or warehouse. THIS worker runs inside the main server (with a
DB connection and warehouse access) and finishes the job: it resolves the
delivery to a governed Datastream, records a durable receipt, reads the
quarantined bytes, and drives them through the EXACT SAME import pipeline as a
direct upload (via ``inbound_ingest.ingest_inbound_file``).

The MANIFEST contract (schema ``inbound-delivery-manifest-v1``) is fixed and
shared with the receipt service::

    {
      "schema": "inbound-delivery-manifest-v1",
      "provider_event_id": str,   # replay/idempotency key
      "channel": "email" | "webhook",
      "token_hash": str,          # sha256 hex of the routing token (raw NEVER stored)
      "recipient_hash": str,      # sha256 hex of the recipient address
      "received_at": str,         # iso8601
      "attachments": [
        {"filename": str, "quarantine_uri": str, "size": int,
         "content_type": str | null}
      ]
    }

Design invariants:
  - AD-2: source-agnostic throughout. NO transport/vendor vocabulary appears in
    code, comments, or docstrings. ``channel`` is opaque declared data.
  - Fail-closed: an unknown/denied token records NOTHING against a Datastream and
    returns a constant-shape, non-enumerating ``{"status": "denied"}`` (mirrors
    the credential resolver's non-enumerating posture). A malformed manifest is a
    typed error before any lookup.
  - Transaction ownership: this function NEVER commits or rolls back. The CALLER
    owns the transaction (mirrors ``ingest_inbound_file`` / ``run_import``).
  - Idempotent redelivery: a redelivered manifest whose receipt is already in a
    terminal state (LANDED/REJECTED/FAILED) short-circuits WITHOUT re-ingesting.

Multi-attachment MVP decision:
  A well-formed delivery carries exactly ONE data attachment. The manifest file
  itself ("_manifest.json") is always ignored. If, after ignoring the manifest,
  ZERO data attachments remain, the receipt is marked FAILED with error_code
  ``no_data_attachment``. If MORE THAN ONE data attachment remains, the FIRST is
  processed and the remainder are NOT silently dropped: their filenames are
  returned under ``ignored_attachments`` and a warning is logged. (Splitting one
  delivery into multiple imports is out of scope for this MVP.)

ASCII-only source (AI-03). Lazy imports of the sibling modules inside the
function body (no import cycle with core.main), matching inbound_ingest.py style.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: The manifest schema tag this worker understands.
_MANIFEST_SCHEMA = "inbound-delivery-manifest-v1"

#: The reserved filename of the manifest object itself (never a data attachment).
_MANIFEST_FILENAME = "_manifest.json"

#: Terminal receipt states: a receipt in one of these has a final outcome and a
#: redelivery must NOT be re-ingested (idempotent redelivery).
_TERMINAL_RECEIPT_STATES: frozenset[str] = frozenset(
    {"LANDED", "REJECTED", "FAILED"}
)

#: Required top-level manifest keys.
_REQUIRED_MANIFEST_KEYS: tuple[str, ...] = (
    "schema",
    "provider_event_id",
    "channel",
    "token_hash",
    "recipient_hash",
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
            f"unexpected manifest schema {schema!r}; "
            f"expected {_MANIFEST_SCHEMA!r}"
        )
    for key in _REQUIRED_MANIFEST_KEYS:
        if key not in manifest:
            raise InboundProcessingValidationError(
                f"manifest is missing required key {key!r}"
            )
    provider_event_id = manifest.get("provider_event_id")
    if not isinstance(provider_event_id, str) or not provider_event_id.strip():
        raise InboundProcessingValidationError(
            "manifest provider_event_id must be a non-empty string"
        )
    token_hash = manifest.get("token_hash")
    if not isinstance(token_hash, str) or not token_hash.strip():
        raise InboundProcessingValidationError(
            "manifest token_hash must be a non-empty string"
        )
    attachments = manifest.get("attachments")
    if not isinstance(attachments, list):
        raise InboundProcessingValidationError(
            "manifest attachments must be a list"
        )
    return manifest


def _select_data_attachments(
    attachments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return the data attachments, ignoring the manifest object itself.

    Any entry whose filename equals the reserved manifest filename is dropped.
    Entries without a usable ``quarantine_uri`` are also dropped (they cannot be
    read). Order is preserved so ``[0]`` is deterministically the first.
    """
    out: list[dict[str, Any]] = []
    for att in attachments:
        if not isinstance(att, dict):
            continue
        filename = att.get("filename")
        if filename == _MANIFEST_FILENAME:
            continue
        if not att.get("quarantine_uri"):
            continue
        out.append(att)
    return out


def _receipt_quarantine_uri(
    manifest: dict[str, Any],
    data_attachments: list[dict[str, Any]],
) -> str | None:
    """Pick the receipt's quarantine_uri: the manifest's own, else first attachment."""
    own = manifest.get("quarantine_uri")
    if isinstance(own, str) and own:
        return own
    if data_attachments:
        first_uri = data_attachments[0].get("quarantine_uri")
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


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------


def process_inbound_delivery(
    conn,
    *,
    manifest: dict,
    store=None,  # noqa: ANN001 -- a QuarantineStore (Protocol); resolved lazily
    actor: str = "inbound-worker",
    trace_id: str | None = None,
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
      5. Select the data attachment(s) (ignore the manifest object). Zero -> mark
         FAILED (no_data_attachment). More than one -> process the FIRST and
         return the remainder under ``ignored_attachments`` (logged, never
         silently dropped). See the module docstring for the MVP rationale.
      6. Resolve the Datastream's project_id.
      7. Read the chosen attachment bytes from the quarantine store.
      8. Drive the bytes through ``ingest_inbound_file`` (same pipeline as a
         direct upload).
      9. Map the ingest outcome to a receipt state: written -> LANDED (with
         import_ledger_id); blocked -> REJECTED (error_code = reason); a typed
         ingest error -> FAILED (error_code, no internals leaked).

    The CALLER owns the transaction: this function NEVER commits or rolls back.

    Returns a structured dict::

      {"status": "landed"|"rejected"|"failed"|"duplicate"|"denied",
       "receipt_id": str | None,
       "datastream_id": str | None,
       "import_ledger_id": str | None,
       "ignored_attachments": list[str]}

    Raises:
      InboundProcessingValidationError: the manifest is malformed.
    """
    # Lazy imports of the sibling modules (avoid import cycles; matches
    # inbound_ingest.py style).
    from core.inbound_credentials import resolve_by_token_hash  # noqa: PLC0415
    from core.inbound_ingest import (  # noqa: PLC0415
        DatastreamNotIngestable,
        InboundIngestValidationError,
        ingest_inbound_file,
    )
    from core.inbound_receipts import (  # noqa: PLC0415
        get_receipt,
        mark_state,
        record_receipt,
    )

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
    receipt = record_receipt(
        conn,
        datastream_id=datastream_id,
        credential_id=credential_id,
        channel=channel,
        provider_event_id=provider_event_id,
        recipient_hash=recipient_hash,
        attachment_count=len(data_attachments),
        total_bytes=total_bytes or None,
        quarantine_uri=quarantine_uri,
        actor=actor,
        host_context={},
        trace_id=trace_id,
        idempotency_key=f"receipt:{datastream_id}:{provider_event_id}",
    )
    receipt_id = receipt.get("receipt_id")

    if receipt.get("deduplicated"):
        existing = get_receipt(
            conn, receipt_id=receipt_id, datastream_id=datastream_id
        )
        existing_state = (existing or {}).get("state")
        if existing_state in _TERMINAL_RECEIPT_STATES:
            logger.info(
                "inbound_processing: duplicate terminal receipt ds=%s state=%s "
                "trace=%s",
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

    chosen = data_attachments[0]
    ignored_attachments: list[str] = []
    if len(data_attachments) > 1:
        ignored_attachments = [
            str(att.get("filename"))
            for att in data_attachments[1:]
            if att.get("filename") is not None
        ]
        logger.warning(
            "inbound_processing: multiple data attachments ds=%s -- processing "
            "first, ignoring %s trace=%s",
            datastream_id,
            ignored_attachments,
            trace_id,
        )

    # ------------------------------------------------------------------
    # Step 6: resolve project_id for the Datastream.
    # ------------------------------------------------------------------
    project_id = _resolve_project_id(conn, datastream_id=datastream_id)
    if project_id is None:
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
    # Step 7: read the chosen attachment bytes from the quarantine store.
    # ------------------------------------------------------------------
    if store is None:
        from core.inbound_quarantine import open_quarantine_store  # noqa: PLC0415

        store = open_quarantine_store()

    try:
        data = store.get(chosen["quarantine_uri"])
    except Exception:  # noqa: BLE001 -- fail closed; do not leak store internals.
        mark_state(
            conn,
            receipt_id=receipt_id,
            datastream_id=datastream_id,
            state="FAILED",
            actor=actor,
            host_context={},
            trace_id=trace_id,
            idempotency_key=f"receipt-state:{receipt_id}:FAILED",
            error_code="quarantine_read_error",
        )
        logger.warning(
            "inbound_processing: quarantine read failed ds=%s trace=%s",
            datastream_id,
            trace_id,
        )
        return {
            "status": "failed",
            "receipt_id": receipt_id,
            "datastream_id": datastream_id,
            "import_ledger_id": None,
            "ignored_attachments": ignored_attachments,
        }

    # ------------------------------------------------------------------
    # Step 8: drive the bytes through the direct-upload import pipeline.
    # ------------------------------------------------------------------
    try:
        result = ingest_inbound_file(
            conn,
            datastream_id=datastream_id,
            project_id=project_id,
            file_bytes=data,
            filename=chosen.get("filename"),
            channel=manifest["channel"],
            message_id=provider_event_id,
            actor=actor,
            trace_id=trace_id,
        )
    except (InboundIngestValidationError, DatastreamNotIngestable) as exc:
        # Typed pre-parse errors: mark FAILED with a stable error_code; do NOT
        # leak internals into the receipt or the return value.
        error_code = type(exc).__name__
        mark_state(
            conn,
            receipt_id=receipt_id,
            datastream_id=datastream_id,
            state="FAILED",
            actor=actor,
            host_context={},
            trace_id=trace_id,
            idempotency_key=f"receipt-state:{receipt_id}:FAILED",
            error_code=error_code,
        )
        logger.warning(
            "inbound_processing: ingest rejected ds=%s error=%s trace=%s",
            datastream_id,
            error_code,
            trace_id,
        )
        return {
            "status": "failed",
            "receipt_id": receipt_id,
            "datastream_id": datastream_id,
            "import_ledger_id": None,
            "ignored_attachments": ignored_attachments,
        }

    # ------------------------------------------------------------------
    # Step 9: map the ingest outcome to a receipt state.
    # ------------------------------------------------------------------
    if result.get("blocked"):
        mark_state(
            conn,
            receipt_id=receipt_id,
            datastream_id=datastream_id,
            state="REJECTED",
            actor=actor,
            host_context={},
            trace_id=trace_id,
            idempotency_key=f"receipt-state:{receipt_id}:REJECTED",
            error_code=result.get("reason"),
        )
        return {
            "status": "rejected",
            "receipt_id": receipt_id,
            "datastream_id": datastream_id,
            "import_ledger_id": None,
            "ignored_attachments": ignored_attachments,
        }

    ledger = result.get("ledger")
    import_ledger_id = ledger.get("id") if isinstance(ledger, dict) else None

    mark_state(
        conn,
        receipt_id=receipt_id,
        datastream_id=datastream_id,
        state="LANDED",
        actor=actor,
        host_context={},
        trace_id=trace_id,
        idempotency_key=f"receipt-state:{receipt_id}:LANDED",
        import_ledger_id=import_ledger_id,
    )
    return {
        "status": "landed",
        "receipt_id": receipt_id,
        "datastream_id": datastream_id,
        "import_ledger_id": import_ledger_id,
        "ignored_attachments": ignored_attachments,
    }
