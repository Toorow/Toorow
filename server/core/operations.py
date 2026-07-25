"""Transactional operation, normalized audit and generic outbox foundation.

Every write accepts an existing Postgres connection. This module never commits:
the domain caller owns the authoritative transaction and rollback boundary.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Callable

from ulid import ULID

_SECRET_KEY = re.compile(
    r"(^|[_-])(token|secret|password|credential|authorization|api[_-]?key|key)($|[_-])",
    re.IGNORECASE,
)
# Keys whose final segment is a pure identifier/reference/digest are safe to
# persist even when they contain a secret-like word: `credential_id` is a
# foreign key, `connection_ref` a reference handle, `bearer_hash` a one-way
# digest -- none carry raw secret material. The value-bearing forms
# (`credential`, `api_key`, `credential_secret`) are still rejected.
_SAFE_ID_SUFFIX = re.compile(r"(?:^|[_-])(id|ids|ref|refs|hash|hashes)$", re.IGNORECASE)


def _is_secret_key(key: str) -> bool:
    """True when the key looks like it would carry raw secret material."""
    if not _SECRET_KEY.search(key):
        return False
    return _SAFE_ID_SUFFIX.search(key) is None
_HASH = re.compile(r"^[0-9a-f]{64}$")
_HOST_KEYS = {
    "host",
    "workspace_id",
    "workspace_type",
    "capability_profile",
    "client_id",
}
_VERSION_KEYS = {"policy", "catalog", "tool"}
_PROVIDER_REFERENCE_KEYS = {
    "connection_ref",
    "account_id",
    "provider_operation_id",
    "provider_delivery_id",
    "external_reference",
}
_SUPPORT_EVIDENCE_KEYS = {"class", "evidence_hash", "correlation_ids", "redactions"}


class OperationValidationError(ValueError):
    """Raised before SQL when operation evidence is unsafe or malformed."""


class OperationIdempotencyConflict(RuntimeError):
    """The same key was already bound to a different immutable request."""


@dataclass(frozen=True)
class OperationSpec:
    command_type: str
    actor: str
    effective_org_id: str
    resource_path: tuple[str, ...]
    idempotency_key: str
    host_context: dict[str, Any]
    versions: dict[str, Any]
    request_payload: dict[str, Any]
    provider_references: dict[str, Any]
    confirmation_mode: str
    confirmation_reference: str | None
    trace_id: str | None


@dataclass(frozen=True)
class PreparedOperation:
    spec: OperationSpec
    idempotency_key_hash: str
    confirmation_reference_hash: str | None
    request_hash: str


@dataclass(frozen=True)
class MutationResult:
    outcome: str
    before_hash: str | None
    after_hash: str | None
    result: dict[str, Any]
    outbox_payload: dict[str, Any]


@dataclass(frozen=True)
class OperationResult:
    operation_id: str
    outcome: str
    result: dict[str, Any]
    audit_event_id: str | None
    outbox_event_id: str | None
    replayed: bool


@dataclass(frozen=True)
class CommittedAuditReference:
    audit_event_id: str


@dataclass(frozen=True)
class OutboxEvent:
    event_id: str
    operation_id: str
    event_type: str
    payload: dict[str, Any]
    attempts: int

    @property
    def consumer_idempotency_key(self) -> str:
        return self.event_id


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_hash(value: Any) -> str:
    return _sha256(json.dumps(value, sort_keys=True, separators=(",", ":")))


def _validate_json(
    value: Any,
    *,
    name: str,
    allowed_keys: set[str] | None = None,
) -> None:
    if not isinstance(value, dict):
        raise OperationValidationError(f"{name} must be an object")
    if allowed_keys is not None and not set(value).issubset(allowed_keys):
        raise OperationValidationError(f"{name} contains unsupported keys")

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 6:
            raise OperationValidationError(f"{name} is too deeply nested")
        if isinstance(node, dict):
            if len(node) > 100:
                raise OperationValidationError(f"{name} contains too many keys")
            for key, item in node.items():
                if _is_secret_key(str(key)):
                    raise OperationValidationError(f"{name} contains a secret-like key")
                walk(item, depth + 1)
        elif isinstance(node, (list, tuple)):
            if len(node) > 100:
                raise OperationValidationError(f"{name} contains too many items")
            for item in node:
                walk(item, depth + 1)
        elif node is not None and not isinstance(node, (str, int, float, bool)):
            raise OperationValidationError(f"{name} contains an unsupported value")
        elif isinstance(node, str) and len(node) > 2048:
            raise OperationValidationError(f"{name} contains an oversized string")

    walk(value)
    if len(json.dumps(value, separators=(",", ":"))) > 32768:
        raise OperationValidationError(f"{name} is too large")


def prepare_operation(spec: OperationSpec) -> PreparedOperation:
    for name in ("command_type", "actor", "effective_org_id", "idempotency_key"):
        value = getattr(spec, name)
        if not isinstance(value, str) or not value.strip():
            raise OperationValidationError(f"{name} is required")
    if len(spec.idempotency_key) > 255:
        raise OperationValidationError("idempotency_key is too long")
    if not spec.resource_path or len(spec.resource_path) > 16:
        raise OperationValidationError("resource_path must contain 1..16 nodes")
    if any(not isinstance(node, str) or not node or len(node) > 256 for node in spec.resource_path):
        raise OperationValidationError("resource_path contains an invalid node")
    if spec.confirmation_mode not in {"none", "server", "host", "human"}:
        raise OperationValidationError("invalid confirmation_mode")
    if spec.trace_id is not None and not re.fullmatch(r"[0-9a-f]{32}", spec.trace_id):
        raise OperationValidationError("trace_id must be 32 lowercase hex characters")
    _validate_json(spec.host_context, name="host_context", allowed_keys=_HOST_KEYS)
    _validate_json(spec.versions, name="versions", allowed_keys=_VERSION_KEYS)
    _validate_json(spec.request_payload, name="request_payload")
    _validate_json(
        spec.provider_references,
        name="provider_references",
        allowed_keys=_PROVIDER_REFERENCE_KEYS,
    )
    request_hash = _canonical_hash(
        {
            "command_type": spec.command_type,
            "resource_path": spec.resource_path,
            "request": spec.request_payload,
            "versions": spec.versions,
        }
    )
    return PreparedOperation(
        spec=spec,
        idempotency_key_hash=_sha256(spec.idempotency_key),
        confirmation_reference_hash=(
            _sha256(spec.confirmation_reference) if spec.confirmation_reference else None
        ),
        request_hash=request_hash,
    )


def _existing_operation(cur, prepared: PreparedOperation):
    spec = prepared.spec
    cur.execute(
        """
        SELECT id, state, result, audit_event_id, outbox_event_id, request_hash
        FROM app.operations
        WHERE effective_org_id = %s AND command_type = %s
          AND idempotency_key_hash = %s
        """,
        (spec.effective_org_id, spec.command_type, prepared.idempotency_key_hash),
    )
    return cur.fetchone()


def _replayed_result(existing, prepared: PreparedOperation) -> OperationResult:
    if existing[5] != prepared.request_hash:
        raise OperationIdempotencyConflict(
            "idempotency key is already bound to a different request"
        )
    return OperationResult(
        operation_id=existing[0],
        outcome=existing[1],
        result=existing[2] or {},
        audit_event_id=existing[3],
        outbox_event_id=existing[4],
        replayed=True,
    )


def execute_operation(
    conn,
    spec: OperationSpec,
    *,
    mutation: Callable[[Any, str], MutationResult],
) -> OperationResult:
    """Run mutation + audit + outbox on *conn* or return an idempotent replay."""
    prepared = prepare_operation(spec)
    with conn.cursor() as cur:
        existing = _existing_operation(cur, prepared)
        if existing:
            return _replayed_result(existing, prepared)
        operation_id = f"op_{ULID()}"
        cur.execute(
            """
            INSERT INTO app.operations
                (id, effective_org_id, command_type, actor, resource_path,
                 host_context, versions, request_hash, provider_references,
                 confirmation_mode, confirmation_reference_hash,
                 idempotency_key_hash, trace_id, state)
            VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s,
                    %s::jsonb, %s, %s, %s, %s, 'pending')
            ON CONFLICT (effective_org_id, command_type, idempotency_key_hash)
            DO NOTHING RETURNING id
            """,
            (
                operation_id,
                spec.effective_org_id,
                spec.command_type,
                spec.actor,
                json.dumps(spec.resource_path),
                json.dumps(spec.host_context),
                json.dumps(spec.versions),
                prepared.request_hash,
                json.dumps(spec.provider_references),
                spec.confirmation_mode,
                prepared.confirmation_reference_hash,
                prepared.idempotency_key_hash,
                spec.trace_id,
            ),
        )
        inserted = cur.fetchone()
        if inserted is None:
            existing = _existing_operation(cur, prepared)
            if not existing:
                raise RuntimeError("idempotent operation disappeared")
            return _replayed_result(existing, prepared)
        operation_id = inserted[0]

    changed = mutation(conn, operation_id)
    if changed.outcome not in {"succeeded", "failed", "outcome_unknown"}:
        raise OperationValidationError("mutation returned an invalid outcome")
    for name, value in (("before_hash", changed.before_hash), ("after_hash", changed.after_hash)):
        if value is not None and not _HASH.match(value):
            raise OperationValidationError(f"{name} must be a sha256 hash")
    _validate_json(changed.result, name="result")
    _validate_json(changed.outbox_payload, name="outbox_payload")

    audit_event_id = f"audit_{ULID()}"
    outbox_event_id = f"opout_{ULID()}"
    versions = spec.versions
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.audit_log
                (id, identity, action, provider_account, connection_ref, metadata,
                 operation_id, effective_org_id, resource_path, host_context,
                 policy_version, catalog_version, tool_version, before_hash,
                 after_hash, confirmation_reference_hash, idempotency_key_hash,
                 outcome, trace_id, created_at)
            VALUES (%s, %s, %s, '', NULL, '{}'::jsonb, %s, %s, %s::jsonb,
                    %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            """,
            (
                audit_event_id,
                spec.actor,
                spec.command_type,
                operation_id,
                spec.effective_org_id,
                json.dumps(spec.resource_path),
                json.dumps(spec.host_context),
                versions.get("policy"),
                versions.get("catalog"),
                versions.get("tool"),
                changed.before_hash,
                changed.after_hash,
                prepared.confirmation_reference_hash,
                prepared.idempotency_key_hash,
                changed.outcome,
                spec.trace_id,
            ),
        )
        cur.execute(
            """
            INSERT INTO app.operation_outbox
                (id, operation_id, event_type, payload)
            VALUES (%s, %s, %s, %s::jsonb)
            """,
            (
                outbox_event_id,
                operation_id,
                spec.command_type,
                json.dumps(changed.outbox_payload),
            ),
        )
        cur.execute(
            """
            UPDATE app.operations
            SET state = %s, outcome = %s, before_hash = %s, after_hash = %s,
                result = %s::jsonb, audit_event_id = %s, outbox_event_id = %s,
                completed_at = NOW(), updated_at = NOW()
            WHERE id = %s
            """,
            (
                changed.outcome,
                changed.outcome,
                changed.before_hash,
                changed.after_hash,
                json.dumps(changed.result),
                audit_event_id,
                outbox_event_id,
                operation_id,
            ),
        )
    return OperationResult(
        operation_id,
        changed.outcome,
        changed.result,
        audit_event_id,
        outbox_event_id,
        False,
    )


def claim_outbox_batch(conn, *, worker_id: str, limit: int = 50) -> list[OutboxEvent]:
    bounded = max(1, min(int(limit), 50))
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH claimed AS (
                SELECT id FROM app.operation_outbox
                WHERE state = 'pending' AND retry_at <= NOW()
                ORDER BY retry_at, created_at
                FOR UPDATE SKIP LOCKED
                LIMIT %s
            )
            UPDATE app.operation_outbox o
            SET state = 'processing', locked_by = %s, locked_at = NOW(),
                attempts = attempts + 1, updated_at = NOW()
            FROM claimed WHERE o.id = claimed.id
            RETURNING o.id, o.operation_id, o.event_type, o.payload, o.attempts
            """,
            (bounded, worker_id),
        )
        rows = cur.fetchall()
    return [OutboxEvent(*row) for row in rows]


def record_delivery_result(
    conn,
    *,
    event_id: str,
    confirmed: bool,
    uncertain: bool = False,
    error_class: str | None = None,
) -> None:
    with conn.cursor() as cur:
        if confirmed:
            cur.execute(
                """
                UPDATE app.operation_outbox
                SET state = 'delivered', delivered_at = NOW(), locked_by = NULL,
                    locked_at = NULL, last_error_class = NULL, updated_at = NOW()
                WHERE id = %s AND state <> 'delivered'
                """,
                (event_id,),
            )
            return
        cur.execute(
            """
            UPDATE app.operation_outbox
            SET state = 'pending', locked_by = NULL, locked_at = NULL,
                retry_at = NOW() + INTERVAL '1 minute', last_error_class = %s,
                updated_at = NOW()
            WHERE id = %s
            RETURNING operation_id
            """,
            (error_class, event_id),
        )
        row = cur.fetchone()
        if uncertain and row:
            cur.execute(
                """
                UPDATE app.operations SET state = 'outcome_unknown',
                    outcome = 'outcome_unknown', updated_at = NOW()
                WHERE id = %s AND state <> 'outcome_unknown'
                """,
                (row[0],),
            )


def prepare_support_disclosure(
    conn,
    *,
    actor: str,
    effective_org_id: str,
    resource_path: tuple[str, ...],
    idempotency_key: str,
    host_context: dict[str, Any],
    versions: dict[str, Any],
    disclosure_class: str,
    evidence_hash: str,
    redaction_hash: str,
    trace_id: str | None,
) -> OperationResult:
    """Create the durable operation/audit/outbox before Support evidence is returned."""
    if not _HASH.match(evidence_hash) or not _HASH.match(redaction_hash):
        raise OperationValidationError("Support evidence requires sha256 hashes")

    def mutation(_conn, _operation_id: str) -> MutationResult:
        result = {
            "disclosure_class": disclosure_class,
            "evidence_hash": evidence_hash,
            "redaction_hash": redaction_hash,
        }
        return MutationResult(
            outcome="succeeded",
            before_hash=None,
            after_hash=_canonical_hash(result),
            result=result,
            outbox_payload=result,
        )

    return execute_operation(
        conn,
        OperationSpec(
            command_type="support.disclosure",
            actor=actor,
            effective_org_id=effective_org_id,
            resource_path=resource_path,
            idempotency_key=idempotency_key,
            host_context=host_context,
            versions=versions,
            request_payload={
                "disclosure_class": disclosure_class,
                "evidence_hash": evidence_hash,
                "redaction_hash": redaction_hash,
            },
            provider_references={},
            confirmation_mode="human",
            confirmation_reference=None,
            trace_id=trace_id,
        ),
        mutation=mutation,
    )


def confirm_support_audit_committed(conn, audit_event_id: str) -> CommittedAuditReference:
    """Verify the audit from a post-commit transaction before building a response."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM app.audit_log "
            "WHERE id = %s AND action = 'support.disclosure'",
            (audit_event_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError("a committed audit reference is required")
    return CommittedAuditReference(str(row[0]))


def build_support_disclosure_response(
    *, audit_reference: CommittedAuditReference, evidence: dict[str, Any]
) -> dict[str, Any]:
    """Build bounded evidence only from a reference verified after commit."""
    if not isinstance(audit_reference, CommittedAuditReference):
        raise RuntimeError("a committed audit reference is required")
    _validate_json(evidence, name="evidence", allowed_keys=_SUPPORT_EVIDENCE_KEYS)
    return {"audit_event_id": audit_reference.audit_event_id, "evidence": evidence}


def reconcile_operation_outcome(
    conn,
    *,
    operation_id: str,
    resolved_outcome: str,
    evidence_hash: str,
) -> bool:
    """Resolve one uncertain operation from durable evidence, exactly once."""
    if resolved_outcome not in {"succeeded", "failed"} or not _HASH.match(evidence_hash):
        raise OperationValidationError("invalid reconciliation evidence")
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE app.operations
            SET state = %s, outcome = %s,
                result = COALESCE(result, '{}'::jsonb)
                         || jsonb_build_object('reconciliation_evidence_hash', %s),
                completed_at = NOW(), updated_at = NOW()
            WHERE id = %s AND state = 'outcome_unknown'
            """,
            (resolved_outcome, resolved_outcome, evidence_hash, operation_id),
        )
        return cur.rowcount == 1

def dispatch_outbox_event(
    event: OutboxEvent,
    handler: Callable[..., Any],
) -> Any:
    """Dispatch with the stable event ID as the downstream idempotency key."""
    return handler(event.payload, idempotency_key=event.consumer_idempotency_key)


def reconcile_uncertain_operation(
    conn,
    *,
    operation_id: str,
    resolver: Callable[[str, dict[str, Any]], tuple[str, str]],
) -> bool:
    """Resolve using only the original durable provider references and evidence."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT provider_references FROM app.operations "
            "WHERE id = %s AND state = 'outcome_unknown' FOR UPDATE",
            (operation_id,),
        )
        row = cur.fetchone()
    if row is None:
        return False
    resolved_outcome, evidence_hash = resolver(operation_id, row[0] or {})
    return reconcile_operation_outcome(
        conn,
        operation_id=operation_id,
        resolved_outcome=resolved_outcome,
        evidence_hash=evidence_hash,
    )
