"""Server-issued confirmation ceremony for first-scope entry commands.

The trusted admin console requests an opaque, short-lived secret for one exact
command payload.  Only the secret digest is persisted.  Consumption is bound to
the canonical person, command, payload, idempotency key and optional authority
context, and the same database transaction links it to the resulting durable
operation.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from ulid import ULID

from core.audit import declare_action

# --- LES ACTIONS QUE CE MODULE ECRIT ------------------------------------
#
# AD-42 (2026-08-12). Celles-ci n'etaient declarees NULLE PART : la valeur
# etait retapee en dur ici, parce que la liste centrale de `core/audit.py`
# etait trop loin pour valoir le detour. Mesure ce jour-la sur le journal
# vivant : 29 des 64 actions reellement ecrites -- 45 % -- etaient dans ce
# cas, et rien ne pouvait distinguer une action d'une faute de frappe.
ACTION_DATASTREAM_SETUP_CREATE_DRAFT = declare_action("datastream.setup.create_draft")
ACTION_HOSTED_ENTRY_SCOPE_CREATE = declare_action("hosted.entry_scope.create")


HOSTED_ENTRY_COMMAND = ACTION_HOSTED_ENTRY_SCOPE_CREATE
INSTANCE_CLAIM_COMMAND = "instance.claim"
PROJECT_SETTINGS_COMMAND = "project.settings.activate"
PROJECT_ACCESS_COMMAND = "project.access.change"
DATASTREAM_SETUP_CREATE_DRAFT_COMMAND = ACTION_DATASTREAM_SETUP_CREATE_DRAFT
DATASTREAM_CANDIDATE_PUBLISH_ACTIVATE_COMMAND = "datastream.candidate.publish_activate"
COUNTRY_HIERARCHY_PUBLISH_COMMAND = "governance.country.publish"
ENTRY_COMMANDS = frozenset(
    {
        HOSTED_ENTRY_COMMAND,
        INSTANCE_CLAIM_COMMAND,
        PROJECT_SETTINGS_COMMAND,
        PROJECT_ACCESS_COMMAND,
        DATASTREAM_SETUP_CREATE_DRAFT_COMMAND,
        DATASTREAM_CANDIDATE_PUBLISH_ACTIVATE_COMMAND,
        COUNTRY_HIERARCHY_PUBLISH_COMMAND,
    }
)
_CONFIRMATION_TTL = timedelta(minutes=15)


class EntryConfirmationValidationError(ValueError):
    """Confirmation input is malformed."""


class EntryConfirmationRefused(PermissionError):
    """A confirmation is absent, expired, already used or has stale bindings."""

    def __init__(self, code: str = "confirmation_invalid"):
        self.code = code
        super().__init__("entry confirmation refused")


@dataclass(frozen=True, slots=True)
class IssuedEntryConfirmation:
    confirmation_id: str
    confirmation_secret: str
    command_type: str
    payload_hash: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ConsumedEntryConfirmation:
    confirmation_id: str
    command_type: str
    actor_person_id: str
    payload_hash: str
    idempotency_key_hash: str
    operation_id: str | None
    replayed: bool


def canonical_payload_hash(command_type: str, request_payload: dict[str, Any]) -> str:
    _validate_command(command_type)
    if not isinstance(request_payload, dict):
        raise EntryConfirmationValidationError("request_payload must be an object")
    try:
        encoded = json.dumps(
            {"command_type": command_type, "request_payload": request_payload},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
    except (TypeError, ValueError) as exc:
        raise EntryConfirmationValidationError("request_payload must be JSON serializable") from exc
    if len(encoded.encode("utf-8")) > 32768:
        raise EntryConfirmationValidationError("request_payload is too large")
    return _sha256(encoded)


def issue_entry_confirmation(
    conn,
    *,
    actor_person_id: str,
    command_type: str,
    request_payload: dict[str, Any],
    idempotency_key: str,
    context_reference: str,
) -> IssuedEntryConfirmation:
    """Persist one immutable confirmation and return its raw secret exactly once."""

    actor = _bounded("actor_person_id", actor_person_id, maximum=128)
    if not actor.startswith("person_"):
        raise EntryConfirmationValidationError("actor_person_id is not canonical")
    _validate_command(command_type)
    request_key = _bounded("idempotency_key", idempotency_key, maximum=255)
    context = _bounded("context_reference", context_reference, maximum=2048)
    payload_hash = canonical_payload_hash(command_type, request_payload)
    confirmation_id = f"econf_{ULID()}"
    confirmation_secret = f"ecfs_{secrets.token_urlsafe(32)}"
    expires_at = datetime.now(timezone.utc) + _CONFIRMATION_TTL

    transaction = getattr(conn, "transaction", None)
    if not callable(transaction):
        raise EntryConfirmationValidationError("transactional confirmation storage unavailable")
    with transaction():
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.entry_confirmations
                    (id, command_type, actor_person_id, payload_hash,
                     idempotency_key_hash, context_reference_hash,
                     confirmation_secret_hash, expires_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    confirmation_id,
                    command_type,
                    actor,
                    payload_hash,
                    _sha256(request_key),
                    _sha256(context),
                    _sha256(confirmation_secret),
                    expires_at,
                ),
            )
    return IssuedEntryConfirmation(
        confirmation_id=confirmation_id,
        confirmation_secret=confirmation_secret,
        command_type=command_type,
        payload_hash=payload_hash,
        expires_at=expires_at,
    )


def consume_entry_confirmation(
    conn,
    *,
    confirmation_id: str,
    confirmation_secret: str,
    actor_person_id: str,
    command_type: str,
    request_payload: dict[str, Any],
    idempotency_key: str,
    context_reference: str,
) -> ConsumedEntryConfirmation:
    """Atomically consume a confirmation or replay its one linked operation.

    The caller must keep this call, the consequential mutation and
    :func:`bind_entry_confirmation_operation` in one outer transaction.
    """

    secret = _bounded("confirmation_secret", confirmation_secret, maximum=2048)
    return _consume(
        conn,
        confirmation_id=confirmation_id,
        secret_hash=_sha256(secret),
        actor_person_id=actor_person_id,
        command_type=command_type,
        request_payload=request_payload,
        idempotency_key=idempotency_key,
        context_reference=context_reference,
    )


def _consume(
    conn,
    *,
    confirmation_id: str,
    secret_hash: str,
    actor_person_id: str,
    command_type: str,
    request_payload: dict[str, Any],
    idempotency_key: str,
    context_reference: str,
) -> ConsumedEntryConfirmation:
    """The one consumption path. Both entry points recheck every binding here."""
    confirmation = _bounded("confirmation_id", confirmation_id, maximum=128)
    actor = _bounded("actor_person_id", actor_person_id, maximum=128)
    _validate_command(command_type)
    request_key = _bounded("idempotency_key", idempotency_key, maximum=255)
    context = _bounded("context_reference", context_reference, maximum=2048)
    payload_hash = canonical_payload_hash(command_type, request_payload)
    idempotency_hash = _sha256(request_key)
    context_hash = _sha256(context)

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT command_type, actor_person_id, payload_hash,
                   idempotency_key_hash, context_reference_hash,
                   confirmation_secret_hash, expires_at, consumed_at, operation_id
            FROM app.entry_confirmations
            WHERE id = %s
            FOR UPDATE
            """,
            (confirmation,),
        )
        row = cur.fetchone()
        if row is None:
            raise EntryConfirmationRefused()
        (
            stored_command,
            stored_actor,
            stored_payload_hash,
            stored_idempotency_hash,
            stored_context_hash,
            stored_secret_hash,
            expires_at,
            consumed_at,
            operation_id,
        ) = row
        supplied_bindings = (
            command_type,
            actor,
            payload_hash,
            idempotency_hash,
            context_hash,
        )
        stored_bindings = (
            stored_command,
            stored_actor,
            stored_payload_hash,
            stored_idempotency_hash,
            stored_context_hash,
        )
        if supplied_bindings != stored_bindings or not hmac.compare_digest(
            str(stored_secret_hash), secret_hash
        ):
            raise EntryConfirmationRefused()
        if consumed_at is not None:
            if operation_id is None:
                raise EntryConfirmationRefused("confirmation_used")
            return ConsumedEntryConfirmation(
                confirmation_id=confirmation,
                command_type=command_type,
                actor_person_id=actor,
                payload_hash=payload_hash,
                idempotency_key_hash=idempotency_hash,
                operation_id=str(operation_id),
                replayed=True,
            )
        now = datetime.now(timezone.utc)
        if not isinstance(expires_at, datetime) or expires_at <= now:
            raise EntryConfirmationRefused("confirmation_expired")
        cur.execute(
            """
            UPDATE app.entry_confirmations
            SET consumed_at = NOW()
            WHERE id = %s AND consumed_at IS NULL
            """,
            (confirmation,),
        )
        if cur.rowcount != 1:
            raise EntryConfirmationRefused("confirmation_used")

    return ConsumedEntryConfirmation(
        confirmation_id=confirmation,
        command_type=command_type,
        actor_person_id=actor,
        payload_hash=payload_hash,
        idempotency_key_hash=idempotency_hash,
        operation_id=None,
        replayed=False,
    )


def bind_confirmation_presence(
    conn,
    *,
    confirmation: IssuedEntryConfirmation,
    presence_evidence_hash: str,
    endpoint_binding: str,
    workspace_evidence_hash: str,
) -> None:
    """Register the trusted surface's server-minted presence for one confirmation.

    Called by the surface that issued the confirmation, in the same request, over
    its own authenticated session. It records evidence the server minted -- never
    a host self-report -- so a later confirmed write can authenticate on the
    binding instead of on a secret that must not cross the model channel.
    """
    if not isinstance(confirmation, IssuedEntryConfirmation):
        raise EntryConfirmationValidationError("an issued server confirmation is required")
    for name, value in (
        ("presence_evidence_hash", presence_evidence_hash),
        ("workspace_evidence_hash", workspace_evidence_hash),
    ):
        if not isinstance(value, str) or len(value) != 64 or not _is_hex(value):
            raise EntryConfirmationValidationError(f"{name} must be a 64-hex digest")
    endpoint = _bounded("endpoint_binding", endpoint_binding, maximum=2048)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.entry_confirmation_presence_bindings
                (confirmation_id, presence_evidence_hash, endpoint_binding_hash,
                 workspace_evidence_hash, payload_hash)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (confirmation_id) DO NOTHING
            """,
            (
                confirmation.confirmation_id,
                presence_evidence_hash,
                _sha256(endpoint),
                workspace_evidence_hash,
                confirmation.payload_hash,
            ),
        )


def resolve_presence_bound_confirmation(
    conn,
    *,
    command_type: str,
    request_payload: dict[str, Any],
    presence_evidence_hash: str,
    endpoint_binding: str,
    workspace_evidence_hash: str,
) -> str:
    """Return the confirmation id a trusted presence binding authorizes, or refuse.

    The caller presents no confirmation material: identity comes from evidence the
    server minted for this endpoint and workspace. Every binding dimension must
    match, the confirmation must still be live, and exactly one must qualify --
    an ambiguous match is refused rather than resolved by recency.
    """
    _validate_command(command_type)
    payload_hash = canonical_payload_hash(command_type, request_payload)
    for value in (presence_evidence_hash, workspace_evidence_hash):
        if not isinstance(value, str) or len(value) != 64 or not _is_hex(value):
            raise EntryConfirmationRefused("presence_evidence_invalid")
    endpoint = _bounded("endpoint_binding", endpoint_binding, maximum=2048)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT b.confirmation_id
            FROM app.entry_confirmation_presence_bindings b
            JOIN app.entry_confirmations c ON c.id = b.confirmation_id
            WHERE b.presence_evidence_hash = %s
              AND b.endpoint_binding_hash = %s
              AND b.workspace_evidence_hash = %s
              AND b.payload_hash = %s
              AND c.command_type = %s
              AND c.payload_hash = %s
              AND c.consumed_at IS NULL
              AND c.expires_at > NOW()
            """,
            (
                presence_evidence_hash,
                _sha256(endpoint),
                workspace_evidence_hash,
                payload_hash,
                command_type,
                payload_hash,
            ),
        )
        rows = cur.fetchall()
    if len(rows) != 1:
        raise EntryConfirmationRefused("confirmation_invalid")
    return str(rows[0][0])


def consume_presence_bound_confirmation(
    conn,
    *,
    confirmation_id: str,
    actor_person_id: str,
    command_type: str,
    request_payload: dict[str, Any],
    idempotency_key: str,
    context_reference: str,
) -> ConsumedEntryConfirmation:
    """Consume a confirmation whose trusted presence binding was already resolved.

    Identical to :func:`consume_entry_confirmation` in every binding it rechecks
    and in its single-use and replay behaviour -- it substitutes the server-side
    presence binding for the secret digest, and nothing else. The caller MUST have
    resolved the id through :func:`resolve_presence_bound_confirmation` first; this
    function refuses any confirmation carrying no presence binding, so it can never
    become a way to skip the secret on the console path.
    """
    confirmation = _bounded("confirmation_id", confirmation_id, maximum=128)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM app.entry_confirmation_presence_bindings WHERE confirmation_id = %s",
            (confirmation,),
        )
        if cur.fetchone() is None:
            raise EntryConfirmationRefused("confirmation_invalid")
        cur.execute(
            "SELECT confirmation_secret_hash FROM app.entry_confirmations WHERE id = %s",
            (confirmation,),
        )
        row = cur.fetchone()
    if row is None:
        raise EntryConfirmationRefused()
    # Reuse the one consumption path verbatim: the presence binding stands in for
    # possession of the secret, so the digest is presented back to it unchanged.
    return _consume(
        conn,
        confirmation_id=confirmation,
        secret_hash=str(row[0]),
        actor_person_id=actor_person_id,
        command_type=command_type,
        request_payload=request_payload,
        idempotency_key=idempotency_key,
        context_reference=context_reference,
    )


def _is_hex(value: str) -> bool:
    return all(char in "0123456789abcdef" for char in value)


def bind_entry_confirmation_operation(
    conn, *, confirmation: ConsumedEntryConfirmation, operation_id: str
) -> None:
    """Link the consumed confirmation to its one durable operation."""

    if not isinstance(confirmation, ConsumedEntryConfirmation):
        raise EntryConfirmationValidationError("a consumed server confirmation is required")
    operation = _bounded("operation_id", operation_id, maximum=128)
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE app.entry_confirmations
            SET operation_id = %s
            WHERE id = %s
              AND consumed_at IS NOT NULL
              AND (operation_id IS NULL OR operation_id = %s)
            RETURNING operation_id
            """,
            (operation, confirmation.confirmation_id, operation),
        )
        row = cur.fetchone()
    if row is None or str(row[0]) != operation:
        raise EntryConfirmationRefused("confirmation_used")


def _validate_command(command_type: str) -> None:
    if command_type not in ENTRY_COMMANDS:
        raise EntryConfirmationValidationError("unsupported entry command")


def _bounded(name: str, value: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise EntryConfirmationValidationError(f"{name} must be a string")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > maximum
        or any(not char.isprintable() for char in normalized)
    ):
        raise EntryConfirmationValidationError(f"{name} is invalid")
    return normalized


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
