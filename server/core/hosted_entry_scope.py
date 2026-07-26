"""Atomic creation of the first hosted tenant scope from an accepted ENTRY."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ulid import ULID

from core.entry_confirmations import (
    HOSTED_ENTRY_COMMAND,
    ConsumedEntryConfirmation,
    canonical_payload_hash,
)
from core.operations import MutationResult, OperationResult, OperationSpec, execute_operation

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,49}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


class HostedEntryScopeValidationError(ValueError):
    """The deployment mode or scope input is invalid."""


class HostedEntryScopeUnavailable(RuntimeError):
    """No accepted, unconsumed ENTRY entitlement can authorize this command."""


@dataclass(frozen=True, slots=True)
class HostedEntryScope:
    consumption_id: str
    invitation_id: str
    person_id: str
    org_id: str
    project_id: str
    journey_id: str
    operation_id: str
    audit_event_id: str
    outbox_event_id: str
    next_url: str
    replayed: bool


def _bounded_text(name: str, value: str, *, maximum: int = 100) -> str:
    if not isinstance(value, str):
        raise HostedEntryScopeValidationError(f"{name} must be a string")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > maximum
        or any(not char.isprintable() for char in normalized)
    ):
        raise HostedEntryScopeValidationError(f"{name} is invalid")
    return normalized


def _slug(name: str, value: str) -> str:
    if not isinstance(value, str) or not _SLUG_RE.fullmatch(value):
        raise HostedEntryScopeValidationError(f"{name} is invalid")
    return value


def _canonical_hash(value: dict[str, str]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _scope_from_operation(operation: OperationResult) -> HostedEntryScope:
    result = operation.result
    required = (
        "consumption_id",
        "invitation_id",
        "person_id",
        "org_id",
        "project_id",
        "journey_id",
        "next_url",
    )
    if (
        operation.outcome != "succeeded"
        or not operation.audit_event_id
        or not operation.outbox_event_id
        or any(not isinstance(result.get(key), str) for key in required)
    ):
        raise HostedEntryScopeUnavailable("hosted ENTRY scope result unavailable")
    return HostedEntryScope(
        consumption_id=result["consumption_id"],
        invitation_id=result["invitation_id"],
        person_id=result["person_id"],
        org_id=result["org_id"],
        project_id=result["project_id"],
        journey_id=result["journey_id"],
        operation_id=operation.operation_id,
        audit_event_id=operation.audit_event_id,
        outbox_event_id=operation.outbox_event_id,
        next_url=result["next_url"],
        replayed=operation.replayed,
    )

def create_hosted_entry_scope(
    conn,
    *,
    deployment_mode: str,
    person_id: str,
    organization_name: str,
    organization_slug: str,
    project_name: str,
    project_slug: str,
    idempotency_key: str,
    confirmation: ConsumedEntryConfirmation,
    currency: str = "EUR",
    timezone_name: str = "Europe/Paris",
    host_context: dict | None = None,
    versions: dict | None = None,
    trace_id: str | None = None,
) -> HostedEntryScope:
    """Consume one accepted ENTRY and create its first org/project atomically."""

    if deployment_mode != "hosted":
        raise HostedEntryScopeValidationError(
            "hosted ENTRY scope creation is available only in hosted mode"
        )
    canonical_person_id = _bounded_text("person_id", person_id, maximum=128)
    if not canonical_person_id.startswith("person_"):
        raise HostedEntryScopeValidationError("person_id is not canonical")
    org_name = _bounded_text("organization_name", organization_name)
    org_slug = _slug("organization_slug", organization_slug)
    first_project_name = _bounded_text("project_name", project_name)
    first_project_slug = _slug("project_slug", project_slug)
    request_key = _bounded_text("idempotency_key", idempotency_key, maximum=255)
    if not isinstance(currency, str) or not _CURRENCY_RE.fullmatch(currency):
        raise HostedEntryScopeValidationError("currency must be an ISO 4217 code")
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, TypeError) as exc:
        raise HostedEntryScopeValidationError("timezone_name is invalid") from exc

    confirmed_payload = {
        "organization_name": org_name,
        "organization_slug": org_slug,
        "project_name": first_project_name,
        "project_slug": first_project_slug,
        "currency": currency,
        "timezone": timezone_name,
    }
    if (
        not isinstance(confirmation, ConsumedEntryConfirmation)
        or confirmation.command_type != HOSTED_ENTRY_COMMAND
        or confirmation.actor_person_id != canonical_person_id
        or confirmation.payload_hash
        != canonical_payload_hash(HOSTED_ENTRY_COMMAND, confirmed_payload)
        or confirmation.idempotency_key_hash
        != hashlib.sha256(request_key.encode("utf-8")).hexdigest()
    ):
        raise HostedEntryScopeValidationError(
            "a matching consumed server confirmation is required"
        )

    transaction = getattr(conn, "transaction", None)
    if not callable(transaction):
        raise HostedEntryScopeUnavailable("transactional scope storage unavailable")

    consumption_id = f"entryscope_{ULID()}"
    org_id = f"org_{ULID()}"
    org_member_id = f"omem_{ULID()}"
    project_id = f"proj_{ULID()}"
    project_member_id = f"pmem_{ULID()}"
    # Platform-scope operation idempotency has no org dimension. Namespace the
    # caller key by canonical person so two tenants choosing the same local key
    # cannot block each other.
    scoped_idempotency_key = _canonical_hash(
        {"person_id": canonical_person_id, "request_key": request_key}
    )

    def mutation(mutation_conn, operation_id: str) -> MutationResult:
        with mutation_conn.cursor() as cur:
            # The canonical person row is the serialization point for every
            # self-service attempt by this person.
            cur.execute(
                "SELECT id FROM app.persons WHERE id = %s FOR UPDATE",
                (canonical_person_id,),
            )
            if cur.fetchone() is None:
                raise HostedEntryScopeUnavailable("hosted ENTRY scope unavailable")


            cur.execute(
                """
                SELECT invitation.id
                FROM app.invitations AS invitation
                JOIN app.invitation_exchange_sessions AS exchange
                  ON exchange.invitation_id = invitation.id
                LEFT JOIN app.hosted_entry_scope_consumptions AS consumption
                  ON consumption.invitation_id = invitation.id
                WHERE invitation.org_id IS NULL
                  AND invitation.state = 'accepted'
                  AND invitation.accepted_at IS NOT NULL
                  AND invitation.superseded_by IS NULL
                  AND exchange.person_id = %s
                  AND exchange.consumed_at IS NOT NULL
                  AND exchange.accepted_operation_id IS NOT NULL
                  AND consumption.id IS NULL
                ORDER BY invitation.accepted_at, invitation.id
                LIMIT 1
                FOR UPDATE OF invitation, exchange
                """,
                (canonical_person_id,),
            )
            entitlement = cur.fetchone()
            if entitlement is None:
                raise HostedEntryScopeUnavailable("hosted ENTRY scope unavailable")
            invitation_id = str(entitlement[0])

            # Deferred scope FKs let the immutable receipt arbitrate before any
            # authority row is created. Unique person/invitation constraints make
            # concurrent submissions fail closed.
            cur.execute(
                """
                INSERT INTO app.hosted_entry_scope_consumptions
                    (id, person_id, invitation_id, org_id, project_id, operation_id)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING id
                """,
                (
                    consumption_id,
                    canonical_person_id,
                    invitation_id,
                    org_id,
                    project_id,
                    operation_id,
                ),
            )
            if cur.fetchone() is None:
                raise HostedEntryScopeUnavailable("hosted ENTRY scope unavailable")

            cur.execute(
                """
                INSERT INTO app.organizations (id, name, slug, status, created_by)
                VALUES (%s, %s, %s, 'active', %s)
                """,
                (org_id, org_name, org_slug, canonical_person_id),
            )
            cur.execute(
                """
                INSERT INTO app.org_members
                    (id, org_id, identity, role, status, joined_at)
                VALUES (%s, %s, %s, 'owner', 'active', NOW())
                """,
                (org_member_id, org_id, canonical_person_id),
            )
            cur.execute(
                """
                INSERT INTO app.projects
                    (id, name, slug, status, currency, timezone, created_by, org_id)
                VALUES (%s, %s, %s, 'active', %s, %s, %s, %s)
                """,
                (
                    project_id,
                    first_project_name,
                    first_project_slug,
                    currency,
                    timezone_name,
                    canonical_person_id,
                    org_id,
                ),
            )
            cur.execute(
                """
                INSERT INTO app.project_members
                    (id, project_id, identity, role)
                VALUES (%s, %s, %s, 'owner')
                """,
                (project_member_id, project_id, canonical_person_id),
            )
            from core.setup_responsibilities import bootstrap_journey_from_acceptance

            journey_id = bootstrap_journey_from_acceptance(
                mutation_conn,
                invitation_id=invitation_id,
                org_id=org_id,
                project_id=project_id,
                operator_identity=canonical_person_id,
                toorow_admin_identity=canonical_person_id,
                accepted_at=datetime.now(timezone.utc),
            )

        result = {
            "consumption_id": consumption_id,
            "invitation_id": invitation_id,
            "person_id": canonical_person_id,
            "org_id": org_id,
            "project_id": project_id,
            "journey_id": journey_id,
            "next_url": f"/p/{project_id}/overview/getting-started",
        }
        return MutationResult(
            outcome="succeeded",
            before_hash=None,
            after_hash=_canonical_hash(result),
            result=result,
            outbox_payload={"kind": "hosted.entry_scope.created", **result},
        )

    with transaction():
        operation = execute_operation(
            conn,
            OperationSpec(
                command_type=HOSTED_ENTRY_COMMAND,
                actor=canonical_person_id,
                effective_org_id=None,
                resource_path=("platform:entry", f"person:{canonical_person_id}"),
                idempotency_key=scoped_idempotency_key,
                host_context=host_context or {},
                versions=versions or {},
                request_payload={
                    "person_id": canonical_person_id,
                    "organization_name": org_name,
                    "organization_slug": org_slug,
                    "project_name": first_project_name,
                    "project_slug": first_project_slug,
                    "currency": currency,
                    "timezone": timezone_name,
                },
                provider_references={},
                confirmation_mode="human",
                confirmation_reference=confirmation.confirmation_id,
                trace_id=trace_id,
            ),
            mutation=mutation,
        )
    return _scope_from_operation(operation)
