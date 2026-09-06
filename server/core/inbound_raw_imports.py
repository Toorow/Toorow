"""Story 38.9 -- Per-attachment immutable raw evidence (record / advance / read).

This module owns ``app.inbound_raw_imports`` (migration 171): ONE row per
ATTACHMENT of an inbound delivery, with an outcome independent of its siblings.

WHY IT EXISTS, since a reader will reasonably ask why this is not just more
columns on ``inbound_receipts``. The receipt (Story 38.8, migration 094) has the
grain of the DELIVERY: one row, one ``quarantine_uri``, one ``state``, one
scalar ``attachment_count``. Story 38.9 AC1 asks for something the delivery
grain cannot express -- "a multi-attachment message produces independent records
and outcomes" -- and three later stories are built on that same grain: a scan
verdict per file (38.10 AC1), an inbox row per attachment (38.14), and the
ability to name one retained file for reprocessing (38.18 AC1). Adding a fourth
nullable column to the receipt would have satisfied none of them.

Public surface:

  * ``content_hash``          : sha256 hex of the stored bytes. The content
                                 address, and the basis of the storage path.
  * ``record_raw_import``     : insert one RECEIVED row per attachment;
                                 ON CONFLICT (receipt_id, ordinal) reconciles to
                                 the existing row with ``deduplicated=True``.
  * ``mark_raw_import_state`` : advance ONE attachment through its own lifecycle.
  * ``find_duplicate_content``: has this Datastream already received these exact
                                 bytes? Returns the evidence; makes NO decision.
  * ``get_raw_import`` / ``list_raw_imports`` / ``list_raw_imports_for_receipt``:
                                 nondisclosing read-models.

INVARIANTS, and they are the same four ``inbound_receipts`` carries -- this is a
sibling ledger, not a new idiom:

  * SOURCE-AGNOSTIC (AD-2). No provider or vendor vocabulary here, in code,
    comments or docstrings. ``channel`` is deliberately NOT duplicated onto this
    table: it belongs to the receipt, and two copies can disagree.

  * NO SECRET, NO SAMPLE (E38-NFR03). No token, no signature, no recipient
    address, no row content. ``filename`` IS stored, because an operator has to
    recognise their own file, and it is untrusted display data only: it never
    becomes a path component (the storage key is content-addressed) and never
    influences authentication.

  * EVERY WRITE ROUTES THROUGH ``operations.execute_operation`` (atomic audit +
    outbox, idempotent replay). No parallel audit path exists here.

  * APPEND-ONLY. The migration-171 trigger freezes the identity columns and
    refuses DELETE outside an audited tenant erasure; this module never attempts
    either. Note in particular that ``filename`` and ``media_type_declared`` are
    frozen: they are the SENDER'S CLAIM, and 38.10 exists to compare that claim
    against independently detected reality. Overwriting the claim would erase
    the mismatch. ``media_type_detected`` is the separate, mutable column.

  * DETERMINISTIC IDEMPOTENCY. No random id enters ``request_payload``; the
    ``inbraw_<ULID>`` is generated at write time inside the mutation closure.

The CALLER owns the transaction throughout: nothing here commits or rolls back.

ASCII-only source (AI-03). Mirrors ``inbound_receipts.py`` conventions.
"""

from __future__ import annotations

import hashlib
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
ACTION_INBOUND_RAW_IMPORT_RECORDED = declare_action("inbound.raw_import.recorded")
ACTION_INBOUND_RAW_IMPORT_STATE_CHANGED = declare_action("inbound.raw_import.state_changed")


# ---------------------------------------------------------------------------
# State machine.
# ---------------------------------------------------------------------------

#: All valid per-attachment states. Mirrors the CHECK constraint of migration 171.
#:
#:   RECEIVED -- bytes are durably stored; no control has run yet.
#:   SCANNING -- the 38.10 controls are running.
#:   ACCEPTED -- controls passed; the file may reach a parser.
#:   REJECTED -- a control refused it. Terminal.
#:   LANDED   -- it produced an import-ledger row. Terminal.
#:   FAILED   -- processing failed. Terminal.
RAW_IMPORT_STATES: frozenset[str] = frozenset(
    {"RECEIVED", "SCANNING", "ACCEPTED", "REJECTED", "LANDED", "FAILED", "DUPLICATE"}
)

#: Terminal states: this attachment has reached its final outcome and a replay
#: must not re-run it. The set is per ATTACHMENT, which is the whole point --
#: one file reaching REJECTED says nothing about its siblings.
TERMINAL_RAW_IMPORT_STATES: frozenset[str] = frozenset(
    {"REJECTED", "LANDED", "FAILED", "DUPLICATE"}
)

#: Conservative Story 38.9 policy: exact bytes within one Datastream produce a
#: fresh evidence row but no second execution. Version is persisted and audited.
DUPLICATE_POLICY_VERSION = "skip-exact-v1"
RETENTION_POLICY_VERSION = "quarantine-retention-v1"

_ALLOWED_RAW_TRANSITIONS: dict[str, frozenset[str]] = {
    "RECEIVED": frozenset({"SCANNING", "REJECTED", "FAILED", "DUPLICATE"}),
    "SCANNING": frozenset({"ACCEPTED", "REJECTED", "FAILED"}),
    "ACCEPTED": frozenset({"LANDED", "REJECTED", "FAILED", "DUPLICATE"}),
}


# ---------------------------------------------------------------------------
# Exceptions.
# ---------------------------------------------------------------------------


class RawImportValidationError(ValueError):
    """Raised before SQL when raw-import inputs are unsafe or malformed."""


class RawImportNotFound(RuntimeError):
    """Raised when a raw import cannot be found within the given Datastream."""


class RawImportStateError(RuntimeError):
    """Raised when a state transition is not permitted."""


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def content_hash(data: bytes) -> str:
    """Return the sha256 hex digest of *data* -- the content address.

    This is the identity of a raw import, and the basis of its storage key. It
    is deliberately a plain function of the BYTES: not of the filename, not of
    the declared media type, not of anything the sender controls beyond the
    payload itself.
    """
    if not isinstance(data, (bytes, bytearray)):
        raise RawImportValidationError("content_hash requires bytes")
    return hashlib.sha256(bytes(data)).hexdigest()


def _dt2iso(ts: Any) -> str | None:
    """Normalise a timestamp to an ISO-8601 string (or None)."""
    if ts is None:
        return None
    try:
        return ts.isoformat()
    except AttributeError:
        return str(ts)


def _resolve_org_id(conn, *, datastream_id: str) -> str:
    """Resolve the owning org for a Datastream (via its project).

    Fail-closed, exactly as ``inbound_receipts._resolve_org_id``: a Datastream
    with no resolvable org is a governance error, not a silent write.
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
        raise RawImportValidationError(
            f"cannot resolve owning org for datastream {datastream_id!r}"
        )
    return row[0]


_SELECT_COLS = (
    "id, receipt_id, datastream_id, ordinal, filename, media_type_declared, "
    "media_type_detected, size_bytes, content_hash, quarantine_uri, state, "
    "scan_verdict, error_code, error_detail, import_ledger_id, "
    "retention_expires_at, retention_policy_version, retention_days, legal_hold, "
    "duplicate_of_raw_import_id, "
    "duplicate_policy_version, quarantine_deleted_at, created_at, updated_at, "
    "dispatch_bundle, dispatch_bundle_fingerprint"
)

_COL_NAMES = [
    "id", "receipt_id", "datastream_id", "ordinal", "filename",
    "media_type_declared", "media_type_detected", "size_bytes", "content_hash",
    "quarantine_uri", "state", "scan_verdict", "error_code", "error_detail",
    "import_ledger_id", "retention_expires_at", "retention_policy_version",
    "retention_days", "legal_hold", "duplicate_of_raw_import_id", "duplicate_policy_version",
    "quarantine_deleted_at", "created_at", "updated_at", "dispatch_bundle",
    "dispatch_bundle_fingerprint",
]


def _row_to_dict(row: tuple) -> dict[str, Any]:
    """Map a raw DB row (ordered by ``_SELECT_COLS``) to a JSON-safe dict.

    ``execute_operation`` validates that a mutation ``result`` contains only
    JSON values, so timestamps are normalised here rather than at the edge.
    """
    d = dict(zip(_COL_NAMES, row))
    d["created_at"] = _dt2iso(d.get("created_at"))
    d["updated_at"] = _dt2iso(d.get("updated_at"))
    d["retention_expires_at"] = _dt2iso(d.get("retention_expires_at"))
    d["quarantine_deleted_at"] = _dt2iso(d.get("quarantine_deleted_at"))
    return d


def _safe_read_model(row_dict: dict[str, Any]) -> dict[str, Any]:
    """Build the nondisclosing read-model for one raw-import row.

    Carries the file's identity, claim-versus-detection pair, lifecycle and
    redacted scan evidence. NEVER carries bytes, a row sample, a token, a
    signature or a recipient address. The shape is identical whether it is
    returned over REST or through MCP (E38-NFR03, 38.15 AC2).
    """
    return {
        "raw_import_id": row_dict.get("id"),
        "receipt_id": row_dict.get("receipt_id"),
        "datastream_id": row_dict.get("datastream_id"),
        "ordinal": row_dict.get("ordinal"),
        # Untrusted display data. Never a path, never an auth input.
        "filename": row_dict.get("filename"),
        "media_type_declared": row_dict.get("media_type_declared"),
        "media_type_detected": row_dict.get("media_type_detected"),
        "size_bytes": row_dict.get("size_bytes"),
        "content_hash": row_dict.get("content_hash"),
        "quarantine_uri": row_dict.get("quarantine_uri"),
        "state": row_dict.get("state"),
        "scan_verdict": row_dict.get("scan_verdict"),
        "error_code": row_dict.get("error_code"),
        "error_detail": row_dict.get("error_detail"),
        "import_ledger_id": row_dict.get("import_ledger_id"),
        "retention_expires_at": row_dict.get("retention_expires_at"),
        "retention_policy_version": row_dict.get("retention_policy_version"),
        "retention_days": row_dict.get("retention_days"),
        "legal_hold": bool(row_dict.get("legal_hold", False)),
        "duplicate_of_raw_import_id": row_dict.get("duplicate_of_raw_import_id"),
        "duplicate_policy_version": row_dict.get("duplicate_policy_version"),
        "quarantine_deleted_at": row_dict.get("quarantine_deleted_at"),
        "dispatch_bundle_fingerprint": row_dict.get("dispatch_bundle_fingerprint"),
        "created_at": row_dict.get("created_at"),
        "updated_at": row_dict.get("updated_at"),
    }


def _load_row(conn, *, raw_import_id: str, datastream_id: str) -> dict | None:
    """Load one row by (id, datastream_id). The Datastream is part of the key.

    Scoping the read by Datastream is what makes a cross-tenant identifier
    indistinguishable from a nonexistent one (E38-NFR01).
    """
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_SELECT_COLS} FROM app.inbound_raw_imports "
            "WHERE id = %s AND datastream_id = %s",
            (raw_import_id, datastream_id),
        )
        row = cur.fetchone()
    return _row_to_dict(row) if row else None


# ---------------------------------------------------------------------------
# record_raw_import -- one immutable row per attachment.
# ---------------------------------------------------------------------------


def record_raw_import(
    conn,
    *,
    receipt_id: str,
    datastream_id: str,
    ordinal: int,
    size_bytes: int,
    content_hash: str,  # noqa: A002 -- deliberate: it IS the content hash
    filename: str | None = None,
    media_type_declared: str | None = None,
    quarantine_uri: str | None = None,
    retention_expires_at: Any = None,
    retention_policy_version: str = RETENTION_POLICY_VERSION,
    retention_days: int = 30,
    actor: str,
    host_context: dict | None = None,
    trace_id: str | None = None,
    idempotency_key: str,
) -> dict[str, Any]:
    """Record ONE attachment as immutable raw evidence, in state ``RECEIVED``.

    Idempotent at the FILE grain: ``ON CONFLICT (receipt_id, ordinal) DO
    NOTHING`` reconciles a re-expansion of the same delivery to the existing
    row and returns it with ``deduplicated=True``. That mirrors what
    ``(datastream_id, provider_event_id)`` does at the DELIVERY grain, so a
    redelivered manifest is safe at both grains rather than only at one.

    ``ordinal`` is the attachment's position WITHIN its delivery, and it must be
    stable for that idempotency to hold -- callers derive it from the manifest
    order, never from a filter that could reorder between two runs.

    The CALLER owns the transaction.

    Raises:
      RawImportValidationError: an input is missing or malformed.
    """
    # ------------------------------------------------------------------
    # Input guards, all before any SQL (fail closed).
    # ------------------------------------------------------------------
    if not isinstance(receipt_id, str) or not receipt_id.strip():
        raise RawImportValidationError("receipt_id is required")
    if not isinstance(datastream_id, str) or not datastream_id.strip():
        raise RawImportValidationError("datastream_id is required")
    if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 0:
        raise RawImportValidationError("ordinal must be a non-negative integer")
    if not isinstance(size_bytes, int) or isinstance(size_bytes, bool) or size_bytes < 0:
        raise RawImportValidationError(
            "size_bytes must be a non-negative integer"
        )
    if (
        not isinstance(content_hash, str)
        or len(content_hash) != 64
        or any(c not in "0123456789abcdef" for c in content_hash)
    ):
        raise RawImportValidationError(
            "content_hash must be a 64-character lowercase hex digest"
        )
    if retention_policy_version != RETENTION_POLICY_VERSION:
        raise RawImportValidationError(
            "unsupported retention_policy_version"
        )
    if (
        not isinstance(retention_days, int)
        or isinstance(retention_days, bool)
        or not 1 <= retention_days <= 3650
    ):
        raise RawImportValidationError(
            "retention_days must be an integer in 1..3650"
        )
    if not isinstance(actor, str) or not actor.strip():
        raise RawImportValidationError("actor is required")
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise RawImportValidationError("idempotency_key is required")

    receipt_id = receipt_id.strip()
    datastream_id = datastream_id.strip()
    actor = actor.strip()

    org_id = _resolve_org_id(conn, datastream_id=datastream_id)

    spec = OperationSpec(
        command_type=ACTION_INBOUND_RAW_IMPORT_RECORDED,
        actor=actor,
        effective_org_id=org_id,
        resource_path=(
            f"datastream:{datastream_id}",
            f"receipt:{receipt_id}",
            f"attachment:{ordinal}",
        ),
        idempotency_key=idempotency_key,
        host_context=host_context or {},
        versions={
            "policy": "inbound-raw-import-v1",
            "catalog": "inbound-raw-import-v1",
            "tool": "rest-v1",
        },
        # DETERMINISTIC: no random id. The content hash identifies the payload;
        # no byte of the payload itself enters the audit trail.
        request_payload={
            "receipt_id": receipt_id,
            "datastream_id": datastream_id,
            "ordinal": ordinal,
            "content_hash": content_hash,
            "size_bytes": size_bytes,
            "filename_sha256": hashlib.sha256(
                (filename or "").encode("utf-8")
            ).hexdigest(),
            "media_type_declared": media_type_declared,
            "quarantine_uri": quarantine_uri,
            "retention_expires_at": _dt2iso(retention_expires_at),
            "retention_policy_version": retention_policy_version,
            "retention_days": retention_days,
        },
        provider_references={},
        confirmation_mode="server",
        confirmation_reference=f"inbound-raw-import:{receipt_id}:{ordinal}",
        trace_id=trace_id,
    )

    _filename = filename
    _media_type_declared = media_type_declared
    _quarantine_uri = quarantine_uri
    _retention_expires_at = retention_expires_at
    _retention_policy_version = retention_policy_version
    _retention_days = retention_days
    _content_hash = content_hash

    def mutation(operation_conn, operation_id: str) -> MutationResult:  # noqa: ANN001
        from core.operations import _canonical_hash  # noqa: PLC0415

        raw_import_id = f"inbraw_{ULID()}"

        with operation_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.inbound_raw_imports "
                "(id, receipt_id, datastream_id, ordinal, filename, "
                "media_type_declared, size_bytes, content_hash, quarantine_uri, "
                "state, retention_expires_at, retention_policy_version, "
                "retention_days, operation_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'RECEIVED', "
                "COALESCE(%s, clock_timestamp() + make_interval(days => %s)), "
                "%s, %s, %s) "
                "ON CONFLICT (receipt_id, ordinal) DO NOTHING",
                (
                    raw_import_id,
                    receipt_id,
                    datastream_id,
                    ordinal,
                    _filename,
                    _media_type_declared,
                    size_bytes,
                    _content_hash,
                    _quarantine_uri,
                    _retention_expires_at,
                    _retention_days,
                    _retention_policy_version,
                    _retention_days,
                    operation_id,
                ),
            )

            if cur.rowcount == 1:
                cur.execute(
                    f"SELECT {_SELECT_COLS} FROM app.inbound_raw_imports "
                    "WHERE id = %s",
                    (raw_import_id,),
                )
                fresh = cur.fetchone()
                row_dict = _row_to_dict(fresh) if fresh else {
                    "id": raw_import_id,
                    "receipt_id": receipt_id,
                    "datastream_id": datastream_id,
                    "ordinal": ordinal,
                    "filename": _filename,
                    "media_type_declared": _media_type_declared,
                    "media_type_detected": None,
                    "size_bytes": size_bytes,
                    "content_hash": _content_hash,
                    "quarantine_uri": _quarantine_uri,
                    "state": "RECEIVED",
                    "scan_verdict": None,
                    "error_code": None,
                    "error_detail": None,
                    "import_ledger_id": None,
                    "retention_expires_at": None,
                    "retention_policy_version": _retention_policy_version,
                    "retention_days": _retention_days,
                    "legal_hold": False,
                    "duplicate_of_raw_import_id": None,
                    "duplicate_policy_version": None,
                    "quarantine_deleted_at": None,
                    "created_at": None,
                    "updated_at": None,
                }
                return MutationResult(
                    outcome="succeeded",
                    before_hash=None,
                    after_hash=_canonical_hash({
                        "id": raw_import_id,
                        "receipt_id": receipt_id,
                        "ordinal": ordinal,
                        "content_hash": _content_hash,
                        "state": "RECEIVED",
                    }),
                    result={**row_dict, "deduplicated": False},
                    outbox_payload={
                        "datastream_id": datastream_id,
                        "receipt_id": receipt_id,
                        "ordinal": ordinal,
                        "state": "RECEIVED",
                    },
                )

            # ON CONFLICT DO NOTHING: this attachment of this delivery was
            # already recorded. Reconcile to it rather than inserting a twin.
            cur.execute(
                f"SELECT {_SELECT_COLS} FROM app.inbound_raw_imports "
                "WHERE receipt_id = %s AND ordinal = %s",
                (receipt_id, ordinal),
            )
            existing = cur.fetchone()
            if existing is None:  # pragma: no cover -- row vanished mid-conflict
                raise RawImportValidationError(
                    "record_raw_import race: existing row not found after conflict"
                )
            row_dict = _row_to_dict(existing)
            immutable_expected = {
                "receipt_id": receipt_id,
                "datastream_id": datastream_id,
                "ordinal": ordinal,
                "filename": _filename,
                "media_type_declared": _media_type_declared,
                "size_bytes": size_bytes,
                "content_hash": _content_hash,
                "quarantine_uri": _quarantine_uri,
                "retention_expires_at": _dt2iso(_retention_expires_at),
                "retention_policy_version": _retention_policy_version,
                "retention_days": _retention_days,
            }
            if _retention_expires_at is None:
                immutable_expected.pop("retention_expires_at")
            immutable_actual = {
                key: row_dict.get(key) for key in immutable_expected
            }
            if immutable_actual != immutable_expected:
                raise RawImportValidationError(
                    "receipt attachment ordinal is bound to different immutable evidence"
                )
            return MutationResult(
                outcome="succeeded",
                before_hash=None,
                after_hash=_canonical_hash({
                    "id": row_dict["id"],
                    "receipt_id": receipt_id,
                    "ordinal": ordinal,
                    "content_hash": row_dict.get("content_hash"),
                    "state": row_dict.get("state", "RECEIVED"),
                }),
                result={**row_dict, "deduplicated": True},
                outbox_payload={
                    "datastream_id": datastream_id,
                    "receipt_id": receipt_id,
                    "ordinal": ordinal,
                    "state": row_dict.get("state", "RECEIVED"),
                    "deduplicated": True,
                },
            )

    op_result = execute_operation(conn, spec, mutation=mutation)
    data = op_result.result or {}
    if op_result.replayed:
        replay_raw_id = data.get("id")
        if not isinstance(replay_raw_id, str) or not replay_raw_id:
            raise RawImportValidationError(
                "raw-import operation replay has no durable identifier"
            )
        current = _load_row(
            conn, raw_import_id=replay_raw_id, datastream_id=datastream_id
        )
        if current is None:
            raise RawImportNotFound(
                "raw-import operation replay has no durable row"
            )
        data = {**current, "deduplicated": True}
    return {
        **_safe_read_model(data),
        "deduplicated": bool(op_result.replayed or data.get("deduplicated", False)),
    }


# ---------------------------------------------------------------------------
# mark_raw_import_state -- advance ONE attachment, independently.
# ---------------------------------------------------------------------------


def mark_raw_import_state(
    conn,
    *,
    raw_import_id: str,
    datastream_id: str,
    state: str,
    actor: str,
    host_context: dict | None = None,
    trace_id: str | None = None,
    idempotency_key: str,
    error_code: str | None = None,
    error_detail: str | None = None,
    import_ledger_id: str | None = None,
    media_type_detected: str | None = None,
    scan_verdict: dict | None = None,
    dispatch_bundle: dict[str, Any] | None = None,
    duplicate_of_raw_import_id: str | None = None,
    duplicate_policy_version: str | None = None,
) -> dict[str, Any]:
    """Advance ONE attachment to *state*, leaving its siblings untouched.

    This independence is the acceptance criterion, not an implementation detail:
    "rejection of one attachment neither publishes nor suppresses another
    attachment's valid execution" (Story 38.6 AC / 38.9 AC1). Nothing in this
    function reads or writes another row.

    Re-marking a row that is ALREADY in the requested state is a no-op that
    returns the current row -- a redelivered or retried worker must not fail on
    work it already completed. Moving OUT of a terminal state raises
    ``RawImportStateError``: a terminal outcome is evidence, and rewriting
    evidence is what the append-only contract exists to prevent.

    The CALLER owns the transaction.

    Raises:
      RawImportValidationError: an input is missing or the state is unknown.
      RawImportNotFound: no such raw import in this Datastream.
      RawImportStateError: the transition leaves a terminal state.
    """
    if not isinstance(raw_import_id, str) or not raw_import_id.strip():
        raise RawImportValidationError("raw_import_id is required")
    if not isinstance(datastream_id, str) or not datastream_id.strip():
        raise RawImportValidationError("datastream_id is required")
    if state not in RAW_IMPORT_STATES:
        raise RawImportValidationError(
            f"state must be one of {sorted(RAW_IMPORT_STATES)}"
        )
    if not isinstance(actor, str) or not actor.strip():
        raise RawImportValidationError("actor is required")
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise RawImportValidationError("idempotency_key is required")
    raw_import_id = raw_import_id.strip()
    datastream_id = datastream_id.strip()
    actor = actor.strip()

    if dispatch_bundle is not None and state != "ACCEPTED":
        raise RawImportValidationError(
            "dispatch_bundle is only valid for the ACCEPTED transition"
        )
    dispatch_bundle_fingerprint = None
    if dispatch_bundle is not None:
        from core.managed_file_dispatch import fingerprint_bundle  # noqa: PLC0415

        dispatch_bundle_fingerprint = fingerprint_bundle(dispatch_bundle)
    current = _load_row(
        conn, raw_import_id=raw_import_id, datastream_id=datastream_id
    )
    if current is None:
        raise RawImportNotFound(
            f"raw import {raw_import_id!r} not found in datastream "
            f"{datastream_id!r}"
        )

    current_state = current.get("state")
    if current_state == state:
        if (
            state == "ACCEPTED"
            and dispatch_bundle_fingerprint is not None
            and current.get("dispatch_bundle_fingerprint")
            != dispatch_bundle_fingerprint
        ):
            raise RawImportStateError(
                "accepted raw-import dispatch bundle evidence diverged"
            )
        return _safe_read_model(current)
    if current_state in TERMINAL_RAW_IMPORT_STATES:
        raise RawImportStateError(
            f"raw import {raw_import_id!r} is terminal ({current_state})"
        )
    if state == "LANDED" and (
        not isinstance(import_ledger_id, str) or not import_ledger_id.strip()
    ):
        raise RawImportValidationError("LANDED requires import_ledger_id")
    if state != "LANDED" and import_ledger_id is not None:
        raise RawImportValidationError("import_ledger_id is only valid for LANDED")
    if state == "DUPLICATE":
        if (
            not isinstance(duplicate_of_raw_import_id, str)
            or not duplicate_of_raw_import_id.strip()
            or duplicate_policy_version != DUPLICATE_POLICY_VERSION
        ):
            raise RawImportValidationError(
                "DUPLICATE requires duplicate evidence and the active policy version"
            )
    elif duplicate_of_raw_import_id is not None or duplicate_policy_version is not None:
        raise RawImportValidationError(
            "duplicate evidence is only valid for DUPLICATE"
        )

    org_id = _resolve_org_id(conn, datastream_id=datastream_id)

    spec = OperationSpec(
        command_type=ACTION_INBOUND_RAW_IMPORT_STATE_CHANGED,
        actor=actor,
        effective_org_id=org_id,
        resource_path=(
            f"datastream:{datastream_id}",
            f"raw_import:{raw_import_id}",
            f"state:{state}",
        ),
        idempotency_key=idempotency_key,
        host_context=host_context or {},
        versions={
            "policy": "inbound-raw-import-v1",
            "catalog": "inbound-raw-import-v1",
            "tool": "rest-v1",
        },
        request_payload={
            "raw_import_id": raw_import_id,
            "datastream_id": datastream_id,
            "state": state,
            "error_code": error_code,
            "error_detail_sha256": (
                hashlib.sha256(error_detail.encode("utf-8")).hexdigest()
                if error_detail is not None else None
            ),
            "import_ledger_id": import_ledger_id,
            "media_type_detected": media_type_detected,
            "scan_verdict_sha256": (
                hashlib.sha256(
                    json.dumps(scan_verdict, sort_keys=True, separators=(",", ":")).encode("utf-8")
                ).hexdigest() if scan_verdict is not None else None
            ),
            "dispatch_bundle_fingerprint": dispatch_bundle_fingerprint,
            "duplicate_of_raw_import_id": duplicate_of_raw_import_id,
            "duplicate_policy_version": duplicate_policy_version,
        },
        provider_references={},
        confirmation_mode="server",
        confirmation_reference=f"inbound-raw-import-state:{raw_import_id}:{state}",
        trace_id=trace_id,
    )

    _error_code = error_code
    _error_detail = error_detail
    _import_ledger_id = import_ledger_id
    _media_type_detected = media_type_detected
    _scan_verdict = scan_verdict
    _dispatch_bundle = dispatch_bundle
    _dispatch_bundle_fingerprint = dispatch_bundle_fingerprint
    _duplicate_of_raw_import_id = duplicate_of_raw_import_id
    _duplicate_policy_version = duplicate_policy_version

    def mutation(operation_conn, operation_id: str) -> MutationResult:  # noqa: ANN001
        from core.operations import _canonical_hash  # noqa: PLC0415

        with operation_conn.cursor() as cur:
            cur.execute(
                f"SELECT {_SELECT_COLS} FROM app.inbound_raw_imports "
                "WHERE id = %s AND datastream_id = %s FOR UPDATE",
                (raw_import_id, datastream_id),
            )
            locked = cur.fetchone()
            if locked is None:
                raise RawImportNotFound(
                    f"raw import {raw_import_id!r} vanished during state change"
                )
            before = _row_to_dict(locked)
            locked_state = before.get("state")
            if locked_state == state:
                if (
                    state == "ACCEPTED"
                    and _dispatch_bundle_fingerprint is not None
                    and before.get("dispatch_bundle_fingerprint")
                    != _dispatch_bundle_fingerprint
                ):
                    raise RawImportStateError(
                        "accepted raw-import dispatch bundle evidence diverged"
                    )
                return MutationResult(
                    outcome="succeeded", before_hash=None, after_hash=None,
                    result=before, outbox_payload={
                        "datastream_id": datastream_id,
                        "raw_import_id": raw_import_id,
                        "state": state,
                        "deduplicated": True,
                    },
                )
            if locked_state in TERMINAL_RAW_IMPORT_STATES:
                raise RawImportStateError(
                    f"raw import {raw_import_id!r} is terminal ({locked_state})"
                )
            if state not in _ALLOWED_RAW_TRANSITIONS.get(locked_state, frozenset()):
                raise RawImportStateError(
                    f"illegal raw-import transition {locked_state} -> {state}"
                )
            cur.execute(
                "UPDATE app.inbound_raw_imports SET "
                "  state = %s,"
                "  error_code = COALESCE(%s, error_code),"
                "  error_detail = COALESCE(%s, error_detail),"
                "  import_ledger_id = COALESCE(%s, import_ledger_id),"
                "  media_type_detected = COALESCE(%s, media_type_detected),"
                "  scan_verdict = COALESCE(%s::jsonb, scan_verdict),"
                "  dispatch_bundle = COALESCE(%s::jsonb, dispatch_bundle),"
                "  dispatch_bundle_fingerprint = "
                "    COALESCE(%s, dispatch_bundle_fingerprint),"
                "  duplicate_of_raw_import_id = COALESCE(%s, duplicate_of_raw_import_id),"
                "  duplicate_policy_version = COALESCE(%s, duplicate_policy_version),"
                "  operation_id = %s,"
                "  updated_at = clock_timestamp() "
                "WHERE id = %s AND datastream_id = %s AND state = %s",
                (
                    state, _error_code, _error_detail, _import_ledger_id,
                    _media_type_detected,
                    json.dumps(_scan_verdict) if _scan_verdict is not None else None,
                    (
                        json.dumps(_dispatch_bundle, sort_keys=True)
                        if _dispatch_bundle is not None
                        else None
                    ),
                    _dispatch_bundle_fingerprint,
                    _duplicate_of_raw_import_id, _duplicate_policy_version,
                    operation_id, raw_import_id, datastream_id, locked_state,
                ),
            )
            if cur.rowcount != 1:
                raise RawImportStateError("concurrent raw-import transition refused")
            cur.execute(
                f"SELECT {_SELECT_COLS} FROM app.inbound_raw_imports WHERE id = %s",
                (raw_import_id,),
            )
            row_dict = _row_to_dict(cur.fetchone())

        # THE OPERATION RESULT IS BOUNDED, THE ROW IS NOT (AI-321, 2026-08-29).
        # `execute_operation` refuses a result nested deeper than six levels or
        # larger than 32 KB, and the dispatch bundle this transition just wrote
        # -- a whole Template contract plus every mapping binding -- is both.
        # So the first governed upload on a Datastream the assistant had just
        # brought to ACTIVE died in `_validate_json` ("result is too deeply
        # nested"), AFTER the UPDATE, and the door rendered a 503. The bundle
        # stays on the row, where `_load_row` reads it; the result carries its
        # FINGERPRINT, which is the reference every consumer already uses.
        bounded = {key: value for key, value in row_dict.items() if key != "dispatch_bundle"}
        return MutationResult(
            outcome="succeeded",
            before_hash=_canonical_hash({
                "id": raw_import_id,
                "state": before.get("state"),
            }),
            after_hash=_canonical_hash({
                "id": raw_import_id,
                "state": state,
            }),
            result=bounded,
            outbox_payload={
                "datastream_id": datastream_id,
                "raw_import_id": raw_import_id,
                "previous_state": before.get("state"),
                "state": state,
                "error_code": _error_code,
            },
        )

    op_result = execute_operation(conn, spec, mutation=mutation)
    if op_result.replayed:
        current = _load_row(
            conn, raw_import_id=raw_import_id, datastream_id=datastream_id
        )
        if current is None:
            raise RawImportNotFound(
                "raw-import transition replay has no durable row"
            )
        return _safe_read_model(current)
    # `_safe_read_model` never carries the bundle (only its fingerprint), so the
    # bounded result IS enough for the read model -- no second SELECT.
    return _safe_read_model(op_result.result or {})


def place_raw_import_legal_hold(
    conn,
    *,
    store,  # noqa: ANN001 -- QuarantineStore protocol
    raw_import_id: str,
    datastream_id: str,
    actor: str,
    idempotency_key: str,
    host_context: dict | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Place a one-way legal hold on both object storage and durable evidence.

    Object hold is applied first. If the database operation fails, the object
    remains held, which is the fail-safe direction. Hold release deliberately
    requires a separate governed workflow.
    """
    if not isinstance(raw_import_id, str) or not raw_import_id.strip():
        raise RawImportValidationError("raw_import_id is required")
    if not isinstance(datastream_id, str) or not datastream_id.strip():
        raise RawImportValidationError("datastream_id is required")
    if not isinstance(actor, str) or not actor.strip():
        raise RawImportValidationError("actor is required")
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise RawImportValidationError("idempotency_key is required")
    current = _load_row(
        conn, raw_import_id=raw_import_id, datastream_id=datastream_id
    )
    if current is None:
        raise RawImportNotFound("raw import not found")
    if current.get("legal_hold"):
        return _safe_read_model(current)
    org_id = _resolve_org_id(conn, datastream_id=datastream_id)
    uri = current.get("quarantine_uri")
    digest = current.get("content_hash")
    store.set_legal_hold(
        uri, enabled=True, expected_org_id=org_id,
        expected_datastream_id=datastream_id, expected_content_hash=digest,
    )
    spec = OperationSpec(
        command_type="inbound.raw_import.legal_hold_changed", actor=actor,
        effective_org_id=org_id,
        resource_path=(f"datastream:{datastream_id}", f"raw_import:{raw_import_id}"),
        idempotency_key=idempotency_key, host_context=host_context or {},
        versions={
            "policy": RETENTION_POLICY_VERSION,
            "catalog": "inbound-raw-import-v1", "tool": "rest-v1",
        },
        request_payload={
            "raw_import_id": raw_import_id, "datastream_id": datastream_id,
            "legal_hold": True, "content_hash": digest, "quarantine_uri": uri,
        },
        provider_references={}, confirmation_mode="server",
        confirmation_reference=f"inbound-raw-import-hold:{raw_import_id}",
        trace_id=trace_id,
    )

    def mutation(operation_conn, operation_id: str) -> MutationResult:  # noqa: ANN001
        from core.operations import _canonical_hash  # noqa: PLC0415

        with operation_conn.cursor() as cur:
            cur.execute(
                f"SELECT {_SELECT_COLS} FROM app.inbound_raw_imports "
                "WHERE id = %s AND datastream_id = %s FOR UPDATE",
                (raw_import_id, datastream_id),
            )
            row = cur.fetchone()
            if row is None:
                raise RawImportNotFound("raw import vanished during legal hold")
            before = _row_to_dict(row)
            if before.get("legal_hold"):
                return MutationResult(
                    outcome="succeeded", before_hash=None, after_hash=None,
                    result=before, outbox_payload={
                        "raw_import_id": raw_import_id, "legal_hold": True,
                        "deduplicated": True,
                    },
                )
            cur.execute(
                "UPDATE app.inbound_raw_imports SET legal_hold = TRUE, "
                "operation_id = %s, updated_at = clock_timestamp() "
                "WHERE id = %s AND datastream_id = %s AND legal_hold = FALSE",
                (operation_id, raw_import_id, datastream_id),
            )
            if cur.rowcount != 1:
                raise RawImportStateError("concurrent legal-hold update refused")
            cur.execute(
                f"SELECT {_SELECT_COLS} FROM app.inbound_raw_imports WHERE id = %s",
                (raw_import_id,),
            )
            after = _row_to_dict(cur.fetchone())
        return MutationResult(
            outcome="succeeded",
            before_hash=_canonical_hash({"id": raw_import_id, "legal_hold": False}),
            after_hash=_canonical_hash({"id": raw_import_id, "legal_hold": True}),
            result=after, outbox_payload={
                "datastream_id": datastream_id, "raw_import_id": raw_import_id,
                "legal_hold": True,
            },
        )

    result = execute_operation(conn, spec, mutation=mutation)
    if result.replayed:
        current = _load_row(
            conn, raw_import_id=raw_import_id, datastream_id=datastream_id
        )
        if current is None:
            raise RawImportNotFound("legal-hold replay has no durable row")
        return _safe_read_model(current)
    return _safe_read_model(result.result or {})


def duplicate_content_decision(
    conn,
    *,
    datastream_id: str,
    raw_import_id: str,
    content_hash: str,  # noqa: A002
) -> dict[str, Any]:
    """Apply the conservative versioned exact-content duplicate policy."""
    prior = find_duplicate_content(
        conn, datastream_id=datastream_id, content_hash=content_hash,
        exclude_raw_import_id=raw_import_id,
    )
    eligible = [row for row in prior if row.get("state") in {"LANDED", "DUPLICATE"}]
    original = eligible[0] if eligible else None
    return {
        "policy_version": DUPLICATE_POLICY_VERSION,
        "skip_execution": original is not None,
        "duplicate_of_raw_import_id": (original or {}).get("raw_import_id"),
    }


# ---------------------------------------------------------------------------
# Reads.
# ---------------------------------------------------------------------------


def get_raw_import(
    conn, *, raw_import_id: str, datastream_id: str
) -> dict[str, Any] | None:
    """Return the safe read-model for one raw import, or None if absent.

    None covers both "no such row" and "a row belonging to another Datastream",
    deliberately: the two must be indistinguishable (E38-NFR01).
    """
    row = _load_row(
        conn, raw_import_id=raw_import_id, datastream_id=datastream_id
    )
    return _safe_read_model(row) if row else None


def list_raw_imports(
    conn, *, datastream_id: str, limit: int = 100, offset: int = 0
) -> list[dict[str, Any]]:
    """Newest-first safe read-models for one Datastream's attachments.

    This is the read behind the attachment inbox (38.14 / E38-UX06): one row per
    ATTACHMENT, not per delivery.
    """
    if not isinstance(limit, int) or limit <= 0 or limit > 500:
        raise RawImportValidationError("limit must be an integer in 1..500")
    if not isinstance(offset, int) or offset < 0:
        raise RawImportValidationError("offset must be a non-negative integer")
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_SELECT_COLS} FROM app.inbound_raw_imports "
            "WHERE datastream_id = %s "
            "ORDER BY created_at DESC, ordinal ASC "
            "LIMIT %s OFFSET %s",
            (datastream_id, limit, offset),
        )
        rows = cur.fetchall()
    return [_safe_read_model(_row_to_dict(r)) for r in rows]


def list_raw_imports_for_receipt(
    conn, *, receipt_id: str, datastream_id: str
) -> list[dict[str, Any]]:
    """All attachments of ONE delivery, in manifest order.

    Ordered by ``ordinal`` rather than by time so the list reads the way the
    delivery was composed, and so two calls cannot disagree.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_SELECT_COLS} FROM app.inbound_raw_imports "
            "WHERE receipt_id = %s AND datastream_id = %s "
            "ORDER BY ordinal ASC",
            (receipt_id, datastream_id),
        )
        rows = cur.fetchall()
    return [_safe_read_model(_row_to_dict(r)) for r in rows]


def find_duplicate_content(
    conn,
    *,
    datastream_id: str,
    content_hash: str,  # noqa: A002 -- deliberate: it IS the content hash
    exclude_raw_import_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return earlier raw imports of this Datastream carrying the SAME bytes.

    This function reports; it does not decide. Whether re-arrival of identical
    content is a duplicate to skip or a legitimate re-send is a per-Datastream
    POLICY question (Story 38.9 AC4), and answering it here -- or with a unique
    constraint in the schema -- would answer it for every tenant, permanently.
    The caller reads the policy and decides; this gives it the evidence.

    Ordered oldest-first so the first element is the ORIGINAL arrival.
    """
    if (
        not isinstance(content_hash, str)
        or len(content_hash) != 64
    ):
        raise RawImportValidationError(
            "content_hash must be a 64-character hex digest"
        )
    sql = (
        f"SELECT {_SELECT_COLS} FROM app.inbound_raw_imports "
        "WHERE datastream_id = %s AND content_hash = %s"
    )
    params: list[Any] = [datastream_id, content_hash]
    if exclude_raw_import_id:
        sql += " AND id <> %s"
        params.append(exclude_raw_import_id)
    sql += " ORDER BY created_at ASC, ordinal ASC"
    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()
    return [_safe_read_model(_row_to_dict(r)) for r in rows]
