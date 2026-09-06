"""Story 38.8 -- Durable inbound-receipt ledger (record / advance / read).

This module is the durable-write half of Story 38.8. It owns the immutable,
append-only ``app.inbound_receipts`` table: one row per verified inbound
delivery resolved to a Datastream, de-duplicated on the provider's replay key.

Public surface:

  * ``hash_recipient``  : sha256 hex of a recipient address. The raw address
                           MUST be hashed before calling record_receipt; the
                           raw string is NEVER persisted or logged.
  * ``record_receipt``  : insert a RECEIVED row; ON CONFLICT reconciles to the
                           existing row (redelivery de-dup) with deduplicated=True.
  * ``mark_state``      : advance a row through its lifecycle states; sets
                           import_ledger_id on transition to LANDED.
  * ``get_receipt``     : safe read-model for one row (no raw address, no token).
  * ``list_receipts``   : newest-first safe read-models for a Datastream.

INVARIANTS (adversarially enforced):

  * SOURCE-AGNOSTIC. No provider/vendor vocabulary in this module -- not in code,
    not in comments, not in docstrings (AD-2). channel and provider_event_id are
    opaque declared data; no vendor name is referenced.

  * NO RAW RECIPIENT ADDRESS OR TOKEN. ``record_receipt`` accepts
    ``recipient_hash`` (sha256 hex, computed by the caller via ``hash_recipient``).
    The raw ``ds_<token>@<domain>`` string is NEVER stored, logged, placed in
    request_payload, audit payload, outbox payload, or any read-model.

  * EVERY WRITE ROUTES THROUGH ``operations.execute_operation`` (atomic audit +
    outbox, idempotent replay). No parallel audit path exists here.

  * APPEND-ONLY. The immutability trigger (migration 094) forbids DELETE and
    freezes identity columns; this module never attempts either.

  * DETERMINISTIC IDEMPOTENCY. No random id enters ``request_payload``. The row
    id ``inbrx_<ULID>`` is generated at WRITE TIME inside the mutation closure.
    Two record_receipt calls with the same idempotency_key replay cleanly.

  * NONDISCLOSING READ MODEL. ``get_receipt`` and ``list_receipts`` carry no
    raw address, no token, no credential hash. Identical shape via REST or MCP.

Mirrors ``inbound_credentials.py`` conventions: ``from __future__ import
annotations``, lazy imports of shared seams, mutations only through the
operation seam, ASCII-only source.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from ulid import ULID

from core.audit import declare_action
from core.operations import MutationResult, OperationSpec, execute_operation

# --- LES ACTIONS QUE CE MODULE ECRIT ------------------------------------
#
# AD-42 (2026-08-12). Celles-ci n'etaient declarees NULLE PART : la valeur
# etait retapee en dur ici, parce que la liste centrale de `core/audit.py`
# etait trop loin pour valoir le detour. Mesure ce jour-la sur le journal
# vivant : 29 des 64 actions reellement ecrites -- 45 % -- etaient dans ce
# cas, et rien ne pouvait distinguer une action d'une faute de frappe.
ACTION_INBOUND_RECEIPT_RECORDED = declare_action("inbound.receipt.recorded")
ACTION_INBOUND_RECEIPT_STATE_ADVANCED = declare_action("inbound.receipt.state_advanced")


# ---------------------------------------------------------------------------
# Valid state machine.
# ---------------------------------------------------------------------------

#: All valid receipt states.
RECEIPT_STATES: frozenset[str] = frozenset(
    {"RECEIVED", "PROCESSING", "LANDED", "REJECTED", "FAILED"}
)

#: Terminal states: a receipt in one of these has reached its final outcome.
_TERMINAL_STATES: frozenset[str] = frozenset({"LANDED", "REJECTED", "FAILED"})

#: Only these forward transitions are legal. Terminal states have no outgoing edge.
_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "RECEIVED": frozenset({"PROCESSING", "REJECTED", "FAILED"}),
    "PROCESSING": frozenset({"LANDED", "REJECTED", "FAILED"}),
}

#: Valid channel values (source-agnostic declared data).
RECEIPT_CHANNELS: frozenset[str] = frozenset({"email", "webhook", "upload"})

# ---------------------------------------------------------------------------
# Exceptions.
# ---------------------------------------------------------------------------


class InboundReceiptValidationError(ValueError):
    """Raised before SQL when receipt inputs are unsafe or malformed."""


class InboundReceiptNotFound(RuntimeError):
    """The receipt row does not exist or belongs to a different Datastream."""


class InboundReceiptStateError(RuntimeError):
    """A state transition was attempted that is not permitted (e.g. from terminal)."""


class InboundReceiptFingerprintMismatch(RuntimeError):
    """A provider replay key was reused for different canonical delivery bytes."""


# ---------------------------------------------------------------------------
# Public hash helper.
# ---------------------------------------------------------------------------


def hash_recipient(recipient: str) -> str:
    """Return the sha256 hex digest of the recipient address string.

    The caller MUST hash the raw address before passing it to ``record_receipt``.
    The raw ``ds_<token>@<domain>`` string must NEVER enter this module or any
    persistent store (E38-NFR03).

    Example::

        h = hash_recipient("ds_abc123@inbound.example.com")
        # h is a 64-character lowercase hex string
    """
    return hashlib.sha256(recipient.encode("utf-8")).hexdigest()


def canonical_receipt_fingerprint(
    *,
    datastream_id: str,
    credential_id: str | None,
    channel: str,
    recipient_hash: str | None,
    attachments: list[dict[str, Any]],
) -> str:
    """Hash normalized receipt identity plus ordered attachment evidence.

    Caller-controlled filenames are represented only by a digest. Attachment
    content is represented by its sha256 digest and ordinal; raw bytes and
    delivery capabilities never enter operation or audit payloads.
    """
    normalized: list[dict[str, Any]] = []
    for ordinal, attachment in enumerate(attachments):
        if not isinstance(attachment, dict):
            raise InboundReceiptValidationError("attachments must contain objects")
        content_sha256 = attachment.get("content_sha256")
        if not isinstance(content_sha256, str) or not _is_sha256(content_sha256):
            raise InboundReceiptValidationError(
                "each attachment requires a lowercase sha256 content digest"
            )
        raw_ordinal = attachment.get("ordinal", ordinal)
        if not isinstance(raw_ordinal, int) or raw_ordinal != ordinal:
            raise InboundReceiptValidationError(
                "attachment ordinals must be contiguous and ordered"
            )
        filename = str(attachment.get("filename") or "attachment")
        content_type = str(attachment.get("content_type") or "").strip().lower()
        size = attachment.get("size")
        if not isinstance(size, int) or size < 0:
            raise InboundReceiptValidationError(
                "each attachment requires a non-negative byte size"
            )
        normalized.append(
            {
                "ordinal": ordinal,
                "content_sha256": content_sha256,
                "filename_sha256": hashlib.sha256(filename.encode("utf-8")).hexdigest(),
                "content_type": content_type,
                "size": size,
            }
        )
    canonical = {
        "datastream_id": datastream_id.strip(),
        "credential_id": credential_id,
        "channel": channel.strip().lower(),
        "recipient_hash": recipient_hash,
        "attachments": normalized,
    }
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


# ---------------------------------------------------------------------------
# Internal helpers.
# ---------------------------------------------------------------------------


def _dt2iso(ts: Any) -> str | None:
    """Convert a timestamp value to ISO-8601 string or None."""
    if ts is None:
        return None
    try:
        return ts.isoformat()
    except AttributeError:
        return str(ts)


def _resolve_org_id(conn, *, datastream_id: str) -> str:
    """Resolve the owning org for a Datastream (via its project).

    The receipt operation is scoped to the datastream's real org so it satisfies
    the operations.effective_org_id FK to app.organizations. Fail-closed: a
    datastream with no resolvable org is a governance error, not a silent write.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT p.org_id FROM app.datastreams d "
            "JOIN app.projects p ON p.id = d.project_id "
            "WHERE d.id = %s",
            (datastream_id,),
        )
        row = cur.fetchone()
    if row is None or not row[0]:
        raise InboundReceiptValidationError(
            f"cannot resolve owning org for datastream {datastream_id!r}"
        )
    return row[0]


def _safe_read_model(row_dict: dict[str, Any]) -> dict[str, Any]:
    """Build the nondisclosing read-model for one inbound_receipts row.

    Carries: receipt_id, datastream_id, credential_id, channel,
    provider_event_id, recipient_hash, attachment_count, total_bytes,
    quarantine_uri, state, import_ledger_id, error_code, error_detail,
    created_at, updated_at.

    NEVER carries a raw recipient address, a raw token, or any credential hash.
    Shape is identical whether returned by REST or MCP (E38-NFR03).
    """
    return {
        "receipt_id": row_dict.get("id"),
        "datastream_id": row_dict.get("datastream_id"),
        "credential_id": row_dict.get("credential_id"),
        "channel": row_dict.get("channel"),
        "provider_event_id": row_dict.get("provider_event_id"),
        "recipient_hash": row_dict.get("recipient_hash"),
        "receipt_fingerprint": row_dict.get("receipt_fingerprint"),
        "attachment_count": row_dict.get("attachment_count", 0),
        "total_bytes": row_dict.get("total_bytes"),
        "quarantine_uri": row_dict.get("quarantine_uri"),
        "state": row_dict.get("state"),
        "import_ledger_id": row_dict.get("import_ledger_id"),
        "error_code": row_dict.get("error_code"),
        "error_detail": row_dict.get("error_detail"),
        "created_at": _dt2iso(row_dict.get("created_at")),
        "updated_at": _dt2iso(row_dict.get("updated_at")),
    }


_SELECT_COLS = (
    "id, datastream_id, credential_id, channel, provider_event_id, "
    "recipient_hash, receipt_fingerprint, attachment_count, total_bytes, "
    "quarantine_uri, state, import_ledger_id, error_code, error_detail, "
    "created_at, updated_at"
)

_COL_NAMES = [
    "id", "datastream_id", "credential_id", "channel", "provider_event_id",
    "recipient_hash", "receipt_fingerprint", "attachment_count", "total_bytes",
    "quarantine_uri", "state", "import_ledger_id", "error_code", "error_detail",
    "created_at", "updated_at",
]


def _row_to_dict(row: tuple) -> dict[str, Any]:
    """Map a raw DB row tuple (ordered by _SELECT_COLS) to a plain dict.

    Timestamp columns are normalised to ISO-8601 strings so the dict is
    JSON-serialisable -- ``execute_operation`` validates that the mutation
    ``result`` contains only JSON values (a raw datetime is rejected).
    """
    d = dict(zip(_COL_NAMES, row))
    d["created_at"] = _dt2iso(d.get("created_at"))
    d["updated_at"] = _dt2iso(d.get("updated_at"))
    return d


def _load_receipt_row(
    conn,
    *,
    receipt_id: str,
    datastream_id: str,
) -> dict[str, Any] | None:
    """Load one receipt row by (id, datastream_id); returns None if absent."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_SELECT_COLS} FROM app.inbound_receipts "
            "WHERE id = %s AND datastream_id = %s",
            (receipt_id, datastream_id),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return _row_to_dict(row)


# ---------------------------------------------------------------------------
# record_receipt -- insert a RECEIVED row; de-dup redelivery cleanly.
# ---------------------------------------------------------------------------


def assert_provider_event_fingerprint(
    conn,
    *,
    datastream_id: str,
    provider_event_id: str,
    receipt_fingerprint: str,
) -> None:
    """Reject a known provider-event replay with different canonical evidence.

    This read guard runs before quarantine creation on the public path. The
    unique constraint plus the same comparison inside ``record_receipt`` remain
    authoritative under races.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT receipt_fingerprint FROM app.inbound_receipts "
            "WHERE datastream_id = %s AND provider_event_id = %s",
            (datastream_id, provider_event_id),
        )
        row = cur.fetchone()
    if row is not None and not hmac.compare_digest(
        str(row[0] or ""), receipt_fingerprint
    ):
        raise InboundReceiptFingerprintMismatch(
            "provider event is already bound to another receipt fingerprint"
        )


def record_receipt(
    conn,
    *,
    datastream_id: str,
    credential_id: str | None,
    channel: str,
    provider_event_id: str,
    recipient_hash: str | None,
    receipt_fingerprint: str,
    attachment_count: int = 0,
    total_bytes: int | None = None,
    quarantine_uri: str | None = None,
    #: WHO SENT IT, hashed exactly like `recipient_hash` beside it and never in
    #: clear (migration 214, story 57.3). A receipt that records who a delivery
    #: was addressed to but not who sent it cannot answer, later, why an import
    #: was refused -- and the replay path (`core.inbound_reprocess`) has no other
    #: source: it never sees the manifest. Both default to None, which reads as
    #: "unknown sender" and is refused by a declared allowlist, never waived.
    sender_hash: str | None = None,
    sender_domain_hash: str | None = None,
    actor: str,
    host_context: dict[str, Any],
    trace_id: str | None,
    idempotency_key: str,
) -> dict[str, Any]:
    """Record a verified inbound delivery as a RECEIVED row.

    Inserts into ``app.inbound_receipts``. If a row already exists for
    (datastream_id, provider_event_id) -- a replayed delivery -- the function
    reconciles to the existing row without inserting a duplicate. The returned
    dict includes ``{"deduplicated": True}`` in that case.

    Parameters
    ----------
    datastream_id:
        The resolved target Datastream id.
    credential_id:
        The ``dic_<ULID>`` credential row that resolved this delivery (nullable).
    channel:
        'email' or 'webhook' (opaque declared data; no vendor vocabulary).
    provider_event_id:
        The provider's idempotency/replay key for this delivery event.
    recipient_hash:
        sha256 hex of the recipient address (computed via ``hash_recipient``).
        NULL is accepted when the caller cannot determine a recipient.
        The raw address MUST NOT be passed here (E38-NFR03).
    attachment_count:
        Number of attachments reported by the provider (>= 0).
    total_bytes:
        Declared total byte size (nullable; provider-reported).
    quarantine_uri:
        Opaque pointer to stored bytes (nullable; set later if quarantined).
    actor:
        Identity string of the caller (operator or system process).
    host_context:
        Opaque host context forwarded to execute_operation.
    trace_id:
        Optional distributed-trace id.
    idempotency_key:
        Caller-supplied idempotency key; replays with the same key are no-ops.

    Returns
    -------
    dict with the safe read-model fields plus ``deduplicated`` (bool).
    ``deduplicated=False`` means a new row was inserted.
    ``deduplicated=True`` means a prior row was found and returned.
    """
    # ------------------------------------------------------------------
    # Input validation (fail-closed before SQL).
    # ------------------------------------------------------------------
    if not isinstance(datastream_id, str) or not datastream_id.strip():
        raise InboundReceiptValidationError("datastream_id is required")
    if not isinstance(channel, str) or channel not in RECEIPT_CHANNELS:
        raise InboundReceiptValidationError(
            f"channel must be one of {sorted(RECEIPT_CHANNELS)}"
        )
    if not isinstance(provider_event_id, str) or not provider_event_id.strip():
        raise InboundReceiptValidationError("provider_event_id is required")
    if not isinstance(actor, str) or not actor.strip():
        raise InboundReceiptValidationError("actor is required")
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise InboundReceiptValidationError("idempotency_key is required")
    if not isinstance(attachment_count, int) or attachment_count < 0:
        raise InboundReceiptValidationError(
            "attachment_count must be a non-negative integer"
        )
    if recipient_hash is not None:
        if not isinstance(recipient_hash, str) or not _is_sha256(recipient_hash):
            raise InboundReceiptValidationError(
                "recipient_hash must be a lowercase sha256 digest or None"
            )
    if not isinstance(receipt_fingerprint, str) or not _is_sha256(receipt_fingerprint):
        raise InboundReceiptValidationError(
            "receipt_fingerprint must be a lowercase sha256 digest"
        )
    if total_bytes is not None and (
        not isinstance(total_bytes, int) or total_bytes < 0
    ):
        raise InboundReceiptValidationError("total_bytes must be non-negative or None")

    datastream_id = datastream_id.strip()
    provider_event_id = provider_event_id.strip()
    actor = actor.strip()

    org_id = _resolve_org_id(conn, datastream_id=datastream_id)

    # ------------------------------------------------------------------
    # Build OperationSpec.
    # DETERMINISTIC PAYLOAD: no random id, no raw address, no token in
    # request_payload. The row id is generated at WRITE TIME inside the
    # mutation closure (mirrors inbound_credentials.py pattern).
    # ------------------------------------------------------------------
    spec = OperationSpec(
        command_type=ACTION_INBOUND_RECEIPT_RECORDED,
        actor=actor,
        effective_org_id=org_id,
        resource_path=(
            f"datastream:{datastream_id}",
            f"channel:{channel}",
            f"provider_event:{provider_event_id}",
        ),
        idempotency_key=idempotency_key,
        host_context=host_context,
        versions={
            "policy": "inbound-receipt-v1",
            "catalog": "inbound-receipt-v1",
            "tool": "rest-v1",
        },
        # NO raw address, NO token. recipient_hash is a one-way digest.
        request_payload={
            "datastream_id": datastream_id,
            "credential_id": credential_id,
            "channel": channel,
            "provider_event_id": provider_event_id,
            "recipient_hash": recipient_hash,
            "receipt_fingerprint": receipt_fingerprint,
            "attachment_count": attachment_count,
            "total_bytes": total_bytes,
            "quarantine_uri": quarantine_uri,
        },
        provider_references={},
        confirmation_mode="server",
        confirmation_reference=(
            f"inbound-receipt:{datastream_id}:{provider_event_id}"
        ),
        trace_id=trace_id,
    )

    # Capture closure variables.
    _credential_id = credential_id
    _recipient_hash = recipient_hash
    _sender_hash = sender_hash
    _sender_domain_hash = sender_domain_hash
    _receipt_fingerprint = receipt_fingerprint
    _total_bytes = total_bytes
    _quarantine_uri = quarantine_uri
    _attachment_count = attachment_count

    def mutation(operation_conn, operation_id: str) -> MutationResult:  # noqa: ANN001
        from core.operations import _canonical_hash  # noqa: PLC0415

        receipt_id = f"inbrx_{ULID()}"

        with operation_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.inbound_receipts "
                "(id, datastream_id, credential_id, channel, provider_event_id, "
                "recipient_hash, sender_hash, sender_domain_hash, "
                "receipt_fingerprint, attachment_count, total_bytes, "
                "quarantine_uri, state, operation_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                "'RECEIVED', %s) "
                "ON CONFLICT (datastream_id, provider_event_id) DO NOTHING",
                (
                    receipt_id,
                    datastream_id,
                    _credential_id,
                    channel,
                    provider_event_id,
                    _recipient_hash,
                    _sender_hash,
                    _sender_domain_hash,
                    _receipt_fingerprint,
                    _attachment_count,
                    _total_bytes,
                    _quarantine_uri,
                    operation_id,
                ),
            )

            if cur.rowcount == 1:
                # Read back DB-authoritative timestamps.
                cur.execute(
                    f"SELECT {_SELECT_COLS} FROM app.inbound_receipts "
                    "WHERE id = %s",
                    (receipt_id,),
                )
                ts_row = cur.fetchone()
                row_dict = _row_to_dict(ts_row) if ts_row else {
                    "id": receipt_id,
                    "datastream_id": datastream_id,
                    "credential_id": _credential_id,
                    "channel": channel,
                    "provider_event_id": provider_event_id,
                    "recipient_hash": _recipient_hash,
                    "receipt_fingerprint": _receipt_fingerprint,
                    "attachment_count": _attachment_count,
                    "total_bytes": _total_bytes,
                    "quarantine_uri": _quarantine_uri,
                    "state": "RECEIVED",
                    "import_ledger_id": None,
                    "error_code": None,
                    "error_detail": None,
                    "created_at": None,
                    "updated_at": None,
                }
                result = {**row_dict, "deduplicated": False}
                return MutationResult(
                    outcome="succeeded",
                    before_hash=None,
                    after_hash=_canonical_hash({
                        "id": receipt_id,
                        "datastream_id": datastream_id,
                        "channel": channel,
                        "provider_event_id": provider_event_id,
                        "state": "RECEIVED",
                    }),
                    result=result,
                    outbox_payload={
                        "datastream_id": datastream_id,
                        "channel": channel,
                        "provider_event_id": provider_event_id,
                        "state": "RECEIVED",
                    },
                )

            # ON CONFLICT DO NOTHING: redelivery. Reconcile to the existing row.
            cur.execute(
                f"SELECT {_SELECT_COLS} FROM app.inbound_receipts "
                "WHERE datastream_id = %s AND provider_event_id = %s",
                (datastream_id, provider_event_id),
            )
            existing_row = cur.fetchone()
            if existing_row is None:  # pragma: no cover -- row vanished
                raise InboundReceiptValidationError(
                    "record_receipt race: existing row not found after conflict"
                )
            row_dict = _row_to_dict(existing_row)
            if not hmac.compare_digest(
                str(row_dict.get("receipt_fingerprint") or ""),
                _receipt_fingerprint,
            ):
                raise InboundReceiptFingerprintMismatch(
                    "provider event is already bound to another receipt fingerprint"
                )
            result = {**row_dict, "deduplicated": True}
            return MutationResult(
                outcome="succeeded",
                before_hash=None,
                after_hash=_canonical_hash({
                    "id": row_dict["id"],
                    "datastream_id": datastream_id,
                    "channel": channel,
                    "provider_event_id": provider_event_id,
                    "state": row_dict.get("state", "RECEIVED"),
                }),
                result=result,
                outbox_payload={
                    "datastream_id": datastream_id,
                    "channel": channel,
                    "provider_event_id": provider_event_id,
                    "state": row_dict.get("state", "RECEIVED"),
                    "deduplicated": True,
                },
            )

    op_result = execute_operation(conn, spec, mutation=mutation)
    data = op_result.result or {}
    if op_result.replayed:
        replay_receipt_id = data.get("id")
        if not isinstance(replay_receipt_id, str) or not replay_receipt_id:
            raise InboundReceiptValidationError(
                "receipt operation replay has no durable receipt identifier"
            )
        current = _load_receipt_row(
            conn, receipt_id=replay_receipt_id, datastream_id=datastream_id
        )
        if current is None:
            raise InboundReceiptNotFound(
                "receipt operation replay has no durable receipt row"
            )
        data = {**current, "deduplicated": True}
    safe = _safe_read_model(data)
    return {
        **safe,
        "deduplicated": bool(op_result.replayed or data.get("deduplicated", False)),
        "operation_outcome": op_result.outcome,
    }


# ---------------------------------------------------------------------------
# mark_state -- advance a receipt through its lifecycle states.
# ---------------------------------------------------------------------------


def mark_state(
    conn,
    *,
    receipt_id: str,
    datastream_id: str,
    state: str,
    actor: str,
    host_context: dict[str, Any],
    trace_id: str | None,
    idempotency_key: str,
    import_ledger_id: str | None = None,
    error_code: str | None = None,
    error_detail: str | None = None,
    scan_recovery: bool = False,
) -> dict[str, Any]:
    """Advance a receipt through the forward-only lifecycle graph atomically."""
    if not isinstance(receipt_id, str) or not receipt_id.strip():
        raise InboundReceiptValidationError("receipt_id is required")
    if not isinstance(datastream_id, str) or not datastream_id.strip():
        raise InboundReceiptValidationError("datastream_id is required")
    if not isinstance(state, str) or state not in RECEIPT_STATES:
        raise InboundReceiptValidationError(
            f"state must be one of {sorted(RECEIPT_STATES)}"
        )
    if not isinstance(actor, str) or not actor.strip():
        raise InboundReceiptValidationError("actor is required")
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise InboundReceiptValidationError("idempotency_key is required")
    if scan_recovery and state != "PROCESSING":
        raise InboundReceiptValidationError(
            "scan_recovery is valid only for a PROCESSING transition"
        )
    if state == "LANDED" and (
        not isinstance(import_ledger_id, str) or not import_ledger_id.strip()
    ):
        raise InboundReceiptValidationError(
            "LANDED requires a non-empty import_ledger_id"
        )
    if state != "LANDED" and import_ledger_id is not None:
        raise InboundReceiptValidationError(
            "import_ledger_id is only valid for LANDED"
        )
    if state not in {"FAILED", "REJECTED"} and (
        error_code is not None or error_detail is not None
    ):
        raise InboundReceiptValidationError(
            "error evidence is only valid for FAILED or REJECTED"
        )
    if error_code is not None and (
        not isinstance(error_code, str) or len(error_code) > 128
    ):
        raise InboundReceiptValidationError("error_code must be at most 128 characters")
    if error_detail is not None and (
        not isinstance(error_detail, str) or len(error_detail) > 2048
    ):
        raise InboundReceiptValidationError(
            "error_detail must be at most 2048 characters"
        )

    receipt_id = receipt_id.strip()
    datastream_id = datastream_id.strip()
    actor = actor.strip()
    import_ledger_id = import_ledger_id.strip() if import_ledger_id else None
    error_detail_hash = (
        hashlib.sha256(error_detail.encode("utf-8")).hexdigest()
        if error_detail is not None
        else None
    )
    # Early nondisclosing absence check; the mutation still locks and rechecks
    # atomically, and this observation never enters the idempotency payload.
    if _load_receipt_row(
        conn, receipt_id=receipt_id, datastream_id=datastream_id
    ) is None:
        raise InboundReceiptNotFound("receipt not found for this datastream")
    org_id = _resolve_org_id(conn, datastream_id=datastream_id)

    spec = OperationSpec(
        command_type=ACTION_INBOUND_RECEIPT_STATE_ADVANCED,
        actor=actor,
        effective_org_id=org_id,
        resource_path=(f"datastream:{datastream_id}", f"receipt:{receipt_id}"),
        idempotency_key=idempotency_key,
        host_context=host_context,
        versions={
            "policy": "inbound-receipt-v2",
            "catalog": "inbound-receipt-v2",
            "tool": "rest-v1",
        },
        # Stable caller intent only. The locked prior state is audit evidence,
        # not part of the retry hash.
        request_payload={
            "receipt_id": receipt_id,
            "datastream_id": datastream_id,
            "target_state": state,
            "import_ledger_id": import_ledger_id,
            "error_code": error_code,
            "error_detail_hash": error_detail_hash,
            "scan_recovery": scan_recovery,
        },
        provider_references={},
        confirmation_mode="server",
        confirmation_reference=f"inbound-receipt:{receipt_id}:state:{state}",
        trace_id=trace_id,
    )

    def mutation(operation_conn, operation_id: str) -> MutationResult:  # noqa: ANN001
        from core.operations import _canonical_hash  # noqa: PLC0415

        with operation_conn.cursor() as cur:
            cur.execute(
                f"SELECT {_SELECT_COLS} FROM app.inbound_receipts "
                "WHERE id = %s AND datastream_id = %s FOR UPDATE",
                (receipt_id, datastream_id),
            )
            locked_row = cur.fetchone()
            if locked_row is None:
                raise InboundReceiptNotFound("receipt not found for this datastream")
            locked = _row_to_dict(locked_row)
            prior_state = str(locked.get("state"))
            recovery_transition = (
                scan_recovery
                and prior_state == "FAILED"
                and state == "PROCESSING"
            )
            if prior_state in _TERMINAL_STATES and not recovery_transition:
                raise InboundReceiptStateError(
                    f"terminal receipt state {prior_state} is immutable"
                )
            if not recovery_transition and state not in _ALLOWED_TRANSITIONS.get(
                prior_state, frozenset()
            ):
                raise InboundReceiptStateError(
                    f"transition {prior_state} -> {state} is not permitted"
                )

            next_import_id = import_ledger_id if state == "LANDED" else None
            next_error_code = error_code if state in {"FAILED", "REJECTED"} else None
            next_error_detail = error_detail if state in {"FAILED", "REJECTED"} else None
            cur.execute(
                "UPDATE app.inbound_receipts SET state = %s, import_ledger_id = %s, "
                "error_code = %s, error_detail = %s, operation_id = %s, "
                "updated_at = clock_timestamp() WHERE id = %s AND datastream_id = %s "
                "AND state = %s RETURNING " + _SELECT_COLS,
                (
                    state,
                    next_import_id,
                    next_error_code,
                    next_error_detail,
                    operation_id,
                    receipt_id,
                    datastream_id,
                    prior_state,
                ),
            )
            updated_row = cur.fetchone()
        if updated_row is None:
            raise InboundReceiptStateError("receipt state changed concurrently")

        row_dict = _row_to_dict(updated_row)
        return MutationResult(
            outcome="succeeded",
            before_hash=_canonical_hash(
                {
                    "receipt_id": receipt_id,
                    "state": prior_state,
                    "import_ledger_id": locked.get("import_ledger_id"),
                }
            ),
            after_hash=_canonical_hash(
                {
                    "receipt_id": receipt_id,
                    "state": state,
                    "import_ledger_id": next_import_id,
                    "error_code": next_error_code,
                    "error_detail_hash": error_detail_hash,
                }
            ),
            result=row_dict,
            outbox_payload={
                "datastream_id": datastream_id,
                "receipt_id": receipt_id,
                "from_state": prior_state,
                "state": state,
                "import_ledger_id": next_import_id,
                "error_code": next_error_code,
                "error_detail_hash": error_detail_hash,
            },
        )

    op_result = execute_operation(conn, spec, mutation=mutation)
    return _safe_read_model(op_result.result or {})


def get_receipt_by_provider_event(
    conn, *, datastream_id: str, provider_event_id: str
) -> dict[str, Any] | None:
    """Return one scoped durable receipt for replay reconciliation."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_SELECT_COLS} FROM app.inbound_receipts "
            "WHERE datastream_id = %s AND provider_event_id = %s",
            (datastream_id, provider_event_id),
        )
        row = cur.fetchone()
    return _safe_read_model(_row_to_dict(row)) if row else None


# ---------------------------------------------------------------------------
# get_receipt -- safe read-model for one row.
# ---------------------------------------------------------------------------


def get_receipt(
    conn,
    *,
    receipt_id: str,
    datastream_id: str,
) -> dict[str, Any] | None:
    """Return the safe read-model for one inbound receipt, or None if absent.

    None means the receipt does not exist for this Datastream (callers return
    a nondisclosing 404). Does NOT raise for absence.

    NEVER returns a raw recipient address, raw token, or credential hash
    (E38-NFR03). Identical shape via REST or MCP.
    """
    row_dict = _load_receipt_row(
        conn, receipt_id=receipt_id, datastream_id=datastream_id
    )
    if row_dict is None:
        return None
    return _safe_read_model(row_dict)


# ---------------------------------------------------------------------------
# list_receipts -- newest-first safe read-models for one Datastream.
# ---------------------------------------------------------------------------


def list_receipts(
    conn,
    *,
    datastream_id: str,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return the most recent inbound receipts for one Datastream.

    Results are ordered newest-first (created_at DESC), capped at ``limit``.
    NEVER returns a raw recipient address, raw token, or credential hash
    (E38-NFR03). Identical shape via REST or MCP.

    Parameters
    ----------
    datastream_id:
        The Datastream to list receipts for.
    limit:
        Maximum number of rows to return (default 50, bounded at caller).
    """
    if not isinstance(limit, int) or limit < 1:
        limit = 50

    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_SELECT_COLS} FROM app.inbound_receipts "
            "WHERE datastream_id = %s "
            "ORDER BY created_at DESC "
            "LIMIT %s",
            (datastream_id, limit),
        )
        rows = cur.fetchall()

    return [_safe_read_model(_row_to_dict(row)) for row in rows]
