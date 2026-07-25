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
from typing import Any

from ulid import ULID

from core.operations import MutationResult, OperationSpec, execute_operation

# ---------------------------------------------------------------------------
# Valid state machine.
# ---------------------------------------------------------------------------

#: All valid receipt states.
RECEIPT_STATES: frozenset[str] = frozenset(
    {"RECEIVED", "PROCESSING", "LANDED", "REJECTED", "FAILED"}
)

#: Terminal states: a receipt in one of these has reached its final outcome.
_TERMINAL_STATES: frozenset[str] = frozenset({"LANDED", "REJECTED", "FAILED"})

#: Valid channel values (source-agnostic declared data).
RECEIPT_CHANNELS: frozenset[str] = frozenset({"email", "webhook"})

# ---------------------------------------------------------------------------
# Exceptions.
# ---------------------------------------------------------------------------


class InboundReceiptValidationError(ValueError):
    """Raised before SQL when receipt inputs are unsafe or malformed."""


class InboundReceiptNotFound(RuntimeError):
    """The receipt row does not exist or belongs to a different Datastream."""


class InboundReceiptStateError(RuntimeError):
    """A state transition was attempted that is not permitted (e.g. from terminal)."""


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
    "recipient_hash, attachment_count, total_bytes, quarantine_uri, "
    "state, import_ledger_id, error_code, error_detail, created_at, updated_at"
)

_COL_NAMES = [
    "id", "datastream_id", "credential_id", "channel", "provider_event_id",
    "recipient_hash", "attachment_count", "total_bytes", "quarantine_uri",
    "state", "import_ledger_id", "error_code", "error_detail",
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


def record_receipt(
    conn,
    *,
    datastream_id: str,
    credential_id: str | None,
    channel: str,
    provider_event_id: str,
    recipient_hash: str | None,
    attachment_count: int = 0,
    total_bytes: int | None = None,
    quarantine_uri: str | None = None,
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
        if not isinstance(recipient_hash, str) or len(recipient_hash) != 64:
            raise InboundReceiptValidationError(
                "recipient_hash must be a 64-character hex string or None"
            )

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
        command_type="inbound.receipt.recorded",
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
            "attachment_count": attachment_count,
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
                "recipient_hash, attachment_count, total_bytes, quarantine_uri, "
                "state, operation_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'RECEIVED', %s) "
                "ON CONFLICT (datastream_id, provider_event_id) DO NOTHING",
                (
                    receipt_id,
                    datastream_id,
                    _credential_id,
                    channel,
                    provider_event_id,
                    _recipient_hash,
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
    safe = _safe_read_model(data)
    return {**safe, "deduplicated": bool(data.get("deduplicated", False))}


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
) -> dict[str, Any]:
    """Advance the state of an inbound receipt.

    Sets ``import_ledger_id`` when transitioning to LANDED.
    Clears or sets ``error_code`` / ``error_detail`` as appropriate for
    FAILED / REJECTED transitions.

    Raises ``InboundReceiptValidationError`` for unknown ``state`` values
    (fail-closed: no silent pass-through).
    Raises ``InboundReceiptNotFound`` when the row is absent or belongs to a
    different Datastream.

    The identity columns (datastream_id, channel, provider_event_id,
    recipient_hash) are frozen by the immutability trigger; this function
    only touches the mutable lifecycle columns.
    """
    # ------------------------------------------------------------------
    # Input validation.
    # ------------------------------------------------------------------
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

    receipt_id = receipt_id.strip()
    datastream_id = datastream_id.strip()
    actor = actor.strip()

    # Load the existing row to get the current state for audit purposes.
    current = _load_receipt_row(conn, receipt_id=receipt_id, datastream_id=datastream_id)
    if current is None:
        raise InboundReceiptNotFound(
            "receipt not found for this datastream"
        )

    prior_state = current.get("state", "RECEIVED")

    org_id = _resolve_org_id(conn, datastream_id=datastream_id)

    spec = OperationSpec(
        command_type="inbound.receipt.state_advanced",
        actor=actor,
        effective_org_id=org_id,
        resource_path=(
            f"datastream:{datastream_id}",
            f"receipt:{receipt_id}",
        ),
        idempotency_key=idempotency_key,
        host_context=host_context,
        versions={
            "policy": "inbound-receipt-v1",
            "catalog": "inbound-receipt-v1",
            "tool": "rest-v1",
        },
        request_payload={
            "receipt_id": receipt_id,
            "datastream_id": datastream_id,
            "from_state": prior_state,
            "target_state": state,
            "import_ledger_id": import_ledger_id,
            "error_code": error_code,
        },
        provider_references={},
        confirmation_mode="server",
        confirmation_reference=(
            f"inbound-receipt:{receipt_id}:state:{state}"
        ),
        trace_id=trace_id,
    )

    _import_ledger_id = import_ledger_id
    _error_code = error_code
    _error_detail = error_detail
    _target_state = state

    def mutation(operation_conn, operation_id: str) -> MutationResult:  # noqa: ANN001
        from core.operations import _canonical_hash  # noqa: PLC0415

        with operation_conn.cursor() as cur:
            cur.execute(
                "UPDATE app.inbound_receipts "
                "SET state = %s, "
                "import_ledger_id = COALESCE(%s, import_ledger_id), "
                "error_code = %s, "
                "error_detail = %s, "
                "operation_id = %s, "
                "updated_at = NOW() "
                "WHERE id = %s AND datastream_id = %s",
                (
                    _target_state,
                    _import_ledger_id,
                    _error_code,
                    _error_detail,
                    operation_id,
                    receipt_id,
                    datastream_id,
                ),
            )

            # Read back authoritative row after update.
            cur.execute(
                f"SELECT {_SELECT_COLS} FROM app.inbound_receipts "
                "WHERE id = %s AND datastream_id = %s",
                (receipt_id, datastream_id),
            )
            updated_row = cur.fetchone()

        if updated_row is None:  # pragma: no cover -- row vanished after update
            raise InboundReceiptNotFound(
                "receipt row vanished during mark_state transition"
            )

        row_dict = _row_to_dict(updated_row)
        result = {**row_dict}
        return MutationResult(
            outcome="succeeded",
            before_hash=_canonical_hash({"state": prior_state}),
            after_hash=_canonical_hash({"id": receipt_id, "state": _target_state}),
            result=result,
            outbox_payload={
                "datastream_id": datastream_id,
                "receipt_id": receipt_id,
                "from_state": prior_state,
                "state": _target_state,
                "import_ledger_id": _import_ledger_id,
            },
        )

    op_result = execute_operation(conn, spec, mutation=mutation)
    data = op_result.result or {}
    return _safe_read_model(data)


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
