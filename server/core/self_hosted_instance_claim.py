"""Atomic one-time claim of a new self-hosted toorow instance.

This module is the domain seam only. It does not expose HTTP routes and it never
commits outside the supplied connection's transaction context. Bootstrap
bearers are accepted only as high-entropy capabilities and are persisted only
as SHA-256 digests.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ulid import ULID

from core.entry_confirmations import (
    INSTANCE_CLAIM_COMMAND,
    ConsumedEntryConfirmation,
    canonical_payload_hash,
)
from core.operations import (
    MutationResult,
    OperationResult,
    OperationSpec,
    execute_operation,
)

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,49}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_MAX_BOOTSTRAP_LIFETIME = timedelta(days=7)
_EXCHANGE_LIFETIME = timedelta(minutes=15)
_INERT_SEED_ORGANIZATION_IDS = ("org_default", "org_integ-test-project")


class SelfHostedClaimValidationError(ValueError):
    """Claim input is malformed or the deployment mode is not self-hosted."""


class SelfHostedClaimUnavailable(RuntimeError):
    """The instance or bootstrap capability cannot authorize a claim."""


@dataclass(frozen=True, slots=True)
class BootstrapCapability:
    capability_id: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class BootstrapExchange:
    exchange_id: str
    session_bearer: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class SelfHostedClaim:
    claim_id: str
    person_id: str
    org_id: str
    project_id: str
    journey_id: str
    operation_id: str
    audit_event_id: str
    outbox_event_id: str
    replayed: bool


def _bearer_hash(bearer: str) -> str:
    if not isinstance(bearer, str) or not 32 <= len(bearer) <= 2048:
        raise SelfHostedClaimValidationError(
            "bootstrap bearer must be a bounded high-entropy string"
        )
    if bearer != bearer.strip() or any(not char.isprintable() for char in bearer):
        raise SelfHostedClaimValidationError("bootstrap bearer is malformed")
    return hashlib.sha256(bearer.encode("utf-8")).hexdigest()


def _bounded_text(name: str, value: str, *, maximum: int = 100) -> str:
    if not isinstance(value, str):
        raise SelfHostedClaimValidationError(f"{name} must be a string")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum or any(
        not char.isprintable() for char in normalized
    ):
        raise SelfHostedClaimValidationError(f"{name} is invalid")
    return normalized


def _slug(name: str, value: str) -> str:
    if not isinstance(value, str) or not _SLUG_RE.fullmatch(value):
        raise SelfHostedClaimValidationError(f"{name} is invalid")
    return value


def _canonical_hash(value: dict[str, str]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _require_self_hosted(deployment_mode: str) -> None:
    if deployment_mode != "self_hosted":
        raise SelfHostedClaimValidationError(
            "instance claim is available only in self_hosted mode"
        )


def _has_existing_non_seed_organization(cur) -> bool:
    """Detect brownfield tenant scope that has not been reconciled into a claim."""

    cur.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM app.organizations
            WHERE id NOT IN (%s, %s)
        )
        """,
        _INERT_SEED_ORGANIZATION_IDS,
    )
    row = cur.fetchone()
    if not isinstance(row, (tuple, list)) or not row:
        raise SelfHostedClaimUnavailable("instance claim unavailable")
    return bool(row[0])


def provision_bootstrap_capability(
    conn,
    *,
    deployment_mode: str,
    bearer: str,
    expires_at: datetime,
) -> BootstrapCapability:
    """Persist an installer capability digest, rotating any active predecessor.

    This seam is intended for trusted deployment/startup orchestration, never an
    authenticated-user route. The raw bearer is neither returned nor persisted.
    """

    _require_self_hosted(deployment_mode)
    digest = _bearer_hash(bearer)
    now = datetime.now(timezone.utc)
    if (
        not isinstance(expires_at, datetime)
        or expires_at.tzinfo is None
        or expires_at <= now
        or expires_at > now + _MAX_BOOTSTRAP_LIFETIME
    ):
        raise SelfHostedClaimValidationError(
            "bootstrap expiry must be within the next seven days"
        )
    capability_id = f"iboot_{ULID()}"
    transaction = getattr(conn, "transaction", None)
    if not callable(transaction):
        raise SelfHostedClaimUnavailable("transactional claim storage unavailable")

    with transaction():
        with conn.cursor() as cur:
            cur.execute(
                "LOCK TABLE app.instance_bootstrap_capabilities, "
                "app.instance_bootstrap_exchange_sessions "
                "IN SHARE ROW EXCLUSIVE MODE"
            )
            cur.execute("SELECT 1 FROM app.instance_claims LIMIT 1")
            if cur.fetchone() is not None:
                raise SelfHostedClaimUnavailable("instance claim unavailable")
            if _has_existing_non_seed_organization(cur):
                raise SelfHostedClaimUnavailable("instance claim unavailable")
            cur.execute(
                """
                UPDATE app.instance_bootstrap_exchange_sessions
                SET state = 'revoked', revoked_at = NOW()
                WHERE state = 'active'
                """
            )
            cur.execute(
                """
                UPDATE app.instance_bootstrap_capabilities
                SET state = 'revoked', revoked_at = NOW()
                WHERE state IN ('active', 'exchanged')
                """
            )
            cur.execute(
                """
                INSERT INTO app.instance_bootstrap_capabilities
                    (id, bearer_hash, state, expires_at)
                VALUES (%s, %s, 'active', %s)
                """,
                (capability_id, digest, expires_at),
            )
    return BootstrapCapability(capability_id=capability_id, expires_at=expires_at)


def exchange_bootstrap_capability(
    conn,
    *,
    deployment_mode: str,
    bootstrap_bearer: str,
) -> BootstrapExchange:
    """Consume the URL-fragment bearer into one short-lived tokenless session.

    The returned session bearer is sensitive and must be set only in an
    HttpOnly, Secure, SameSite=Strict cookie by the dedicated no-store exchange
    endpoint. The original installer bearer cannot be exchanged again.
    """

    _require_self_hosted(deployment_mode)
    capability_hash = _bearer_hash(bootstrap_bearer)
    session_bearer = secrets.token_urlsafe(32)
    session_hash = _bearer_hash(session_bearer)
    exchange_id = f"ibx_{ULID()}"
    transaction = getattr(conn, "transaction", None)
    if not callable(transaction):
        raise SelfHostedClaimUnavailable("transactional claim storage unavailable")

    with transaction():
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, expires_at
                FROM app.instance_bootstrap_capabilities
                WHERE bearer_hash = %s
                  AND state = 'active'
                  AND expires_at > NOW()
                FOR UPDATE
                """,
                (capability_hash,),
            )
            capability = cur.fetchone()
            if capability is None:
                raise SelfHostedClaimUnavailable("instance claim unavailable")
            capability_id, capability_expiry = str(capability[0]), capability[1]
            cur.execute(
                """
                UPDATE app.instance_bootstrap_capabilities
                SET state = 'exchanged', exchanged_at = NOW()
                WHERE id = %s AND state = 'active'
                """,
                (capability_id,),
            )
            if cur.rowcount != 1:
                raise SelfHostedClaimUnavailable("instance claim unavailable")
            cur.execute(
                """
                INSERT INTO app.instance_bootstrap_exchange_sessions
                    (id, bootstrap_capability_id, session_hash, state, expires_at)
                VALUES (%s, %s, %s, 'active',
                        LEAST(%s, NOW() + INTERVAL '15 minutes'))
                RETURNING expires_at
                """,
                (exchange_id, capability_id, session_hash, capability_expiry),
            )
            inserted = cur.fetchone()
            if inserted is None or not isinstance(inserted[0], datetime):
                raise SelfHostedClaimUnavailable("instance claim unavailable")
            exchange_expiry = inserted[0]

    return BootstrapExchange(
        exchange_id=exchange_id,
        session_bearer=session_bearer,
        expires_at=exchange_expiry,
    )


def bootstrap_exchange_session_is_ready(
    conn,
    *,
    deployment_mode: str,
    bootstrap_exchange_bearer: str,
) -> bool:
    """Check whether a tokenless browser session can resume the claim form."""

    _require_self_hosted(deployment_mode)
    exchange_hash = _bearer_hash(bootstrap_exchange_bearer)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM app.instance_bootstrap_exchange_sessions AS exchange
                JOIN app.instance_bootstrap_capabilities AS capability
                  ON capability.id = exchange.bootstrap_capability_id
                WHERE exchange.session_hash = %s
                  AND exchange.state = 'active'
                  AND exchange.expires_at > NOW()
                  AND capability.state = 'exchanged'
                  AND capability.expires_at > NOW()
            )
            AND NOT EXISTS (SELECT 1 FROM app.instance_claims)
            AND NOT EXISTS (
                SELECT 1
                FROM app.organizations
                WHERE id NOT IN (%s, %s)
            )
            """,
            (exchange_hash, *_INERT_SEED_ORGANIZATION_IDS),
        )
        row = cur.fetchone()
    return bool(row and row[0])


def claim_self_hosted_instance(
    conn,
    *,
    deployment_mode: str,
    bootstrap_exchange_bearer: str,
    claimant_person_id: str,
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
) -> SelfHostedClaim:
    """Claim the singleton instance and create its first usable tenant scope."""

    _require_self_hosted(deployment_mode)
    exchange_hash = _bearer_hash(bootstrap_exchange_bearer)
    person_id = _bounded_text("claimant_person_id", claimant_person_id, maximum=128)
    if not person_id.startswith("person_"):
        raise SelfHostedClaimValidationError("claimant_person_id is not canonical")
    org_name = _bounded_text("organization_name", organization_name)
    org_slug = _slug("organization_slug", organization_slug)
    project_name = _bounded_text("project_name", project_name)
    project_slug = _slug("project_slug", project_slug)
    idempotency_key = _bounded_text("idempotency_key", idempotency_key, maximum=255)
    if not _CURRENCY_RE.fullmatch(currency):
        raise SelfHostedClaimValidationError("currency must be an ISO 4217 code")
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, TypeError) as exc:
        raise SelfHostedClaimValidationError("timezone_name is invalid") from exc

    confirmed_payload = {
        "organization_name": org_name,
        "organization_slug": org_slug,
        "project_name": project_name,
        "project_slug": project_slug,
        "currency": currency,
        "timezone": timezone_name,
    }
    if (
        not isinstance(confirmation, ConsumedEntryConfirmation)
        or confirmation.command_type != INSTANCE_CLAIM_COMMAND
        or confirmation.actor_person_id != person_id
        or confirmation.payload_hash
        != canonical_payload_hash(INSTANCE_CLAIM_COMMAND, confirmed_payload)
        or confirmation.idempotency_key_hash
        != hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    ):
        raise SelfHostedClaimValidationError(
            "a matching consumed server confirmation is required"
        )

    claim_id = f"iclaim_{ULID()}"
    instance_member_id = f"imem_{ULID()}"
    org_id = f"org_{ULID()}"
    org_member_id = f"omem_{ULID()}"
    project_id = f"proj_{ULID()}"
    project_member_id = f"pmem_{ULID()}"
    transaction = getattr(conn, "transaction", None)
    if not callable(transaction):
        raise SelfHostedClaimUnavailable("transactional claim storage unavailable")

    def mutation(mutation_conn, operation_id: str) -> MutationResult:
        with mutation_conn.cursor() as cur:
            cur.execute(
                """
                SELECT exchange.id, capability.id
                FROM app.instance_bootstrap_exchange_sessions AS exchange
                JOIN app.instance_bootstrap_capabilities AS capability
                  ON capability.id = exchange.bootstrap_capability_id
                WHERE exchange.session_hash = %s
                  AND exchange.state = 'active'
                  AND exchange.expires_at > NOW()
                  AND capability.state = 'exchanged'
                  AND capability.expires_at > NOW()
                FOR UPDATE OF exchange, capability
                """,
                (exchange_hash,),
            )
            exchange = cur.fetchone()
            if exchange is None:
                raise SelfHostedClaimUnavailable("instance claim unavailable")
            exchange_id, capability_id = str(exchange[0]), str(exchange[1])

            # Serialize root creation with legacy organization/project writers.
            # Historical clean databases contain inert system seed scopes, so
            # only those exact seed ids are ignored. Any other organization is
            # an unreconciled brownfield tenant and blocks first-owner creation;
            # otherwise instance_claims remains the authoritative state.
            cur.execute(
                "LOCK TABLE app.organizations, app.projects "
                "IN SHARE ROW EXCLUSIVE MODE"
            )
            if _has_existing_non_seed_organization(cur):
                raise SelfHostedClaimUnavailable("instance claim unavailable")

            # The singleton row arbitrates concurrent attempts using either the
            # same or different valid capabilities. Deferred FKs let it win the
            # race before the organization and project rows are inserted.
            cur.execute(
                """
                INSERT INTO app.instance_claims
                    (singleton_key, id, bootstrap_capability_id, owner_person_id,
                     org_id, project_id, operation_id)
                VALUES (1, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (singleton_key) DO NOTHING
                RETURNING id
                """,
                (
                    claim_id,
                    capability_id,
                    person_id,
                    org_id,
                    project_id,
                    operation_id,
                ),
            )
            if cur.fetchone() is None:
                raise SelfHostedClaimUnavailable("instance claim unavailable")

            cur.execute(
                """
                INSERT INTO app.instance_members (id, person_id, role, status)
                VALUES (%s, %s, 'owner', 'active')
                """,
                (instance_member_id, person_id),
            )
            cur.execute(
                """
                INSERT INTO app.organizations
                    (id, name, slug, status, created_by)
                VALUES (%s, %s, %s, 'active', %s)
                """,
                (org_id, org_name, org_slug, person_id),
            )
            cur.execute(
                """
                INSERT INTO app.org_members
                    (id, org_id, identity, role, status, joined_at)
                VALUES (%s, %s, %s, 'owner', 'active', NOW())
                """,
                (org_member_id, org_id, person_id),
            )
            cur.execute(
                """
                INSERT INTO app.projects
                    (id, name, slug, status, currency, timezone, created_by, org_id)
                VALUES (%s, %s, %s, 'active', %s, %s, %s, %s)
                """,
                (
                    project_id,
                    project_name,
                    project_slug,
                    currency,
                    timezone_name,
                    person_id,
                    org_id,
                ),
            )
            cur.execute(
                """
                INSERT INTO app.project_members
                    (id, project_id, identity, role)
                VALUES (%s, %s, %s, 'owner')
                """,
                (project_member_id, project_id, person_id),
            )
            from core.setup_responsibilities import bootstrap_journey_from_acceptance

            journey_id = bootstrap_journey_from_acceptance(
                mutation_conn,
                invitation_id=None,
                org_id=org_id,
                project_id=project_id,
                operator_identity=person_id,
                toorow_admin_identity=person_id,
                accepted_at=datetime.now(timezone.utc),
            )
            cur.execute(
                """
                UPDATE app.instance_bootstrap_exchange_sessions
                SET state = 'consumed', consumed_at = NOW(),
                    consumed_by_person_id = %s
                WHERE id = %s AND state = 'active'
                """,
                (person_id, exchange_id),
            )
            if cur.rowcount != 1:
                raise SelfHostedClaimUnavailable("instance claim unavailable")
            cur.execute(
                """
                UPDATE app.instance_bootstrap_capabilities
                SET state = 'consumed', consumed_at = NOW(),
                    consumed_by_person_id = %s
                WHERE id = %s AND state = 'exchanged'
                """,
                (person_id, capability_id),
            )
            if cur.rowcount != 1:
                raise SelfHostedClaimUnavailable("instance claim unavailable")

        result = {
            "claim_id": claim_id,
            "person_id": person_id,
            "org_id": org_id,
            "project_id": project_id,
            "journey_id": journey_id,
        }
        return MutationResult(
            outcome="succeeded",
            before_hash=None,
            after_hash=_canonical_hash(result),
            result=result,
            outbox_payload={"kind": "instance.claimed", **result},
        )

    with transaction():
        operation = execute_operation(
            conn,
            OperationSpec(
                command_type=INSTANCE_CLAIM_COMMAND,
                actor=person_id,
                effective_org_id=None,
                resource_path=("instance", "singleton"),
                idempotency_key=idempotency_key,
                host_context=host_context or {},
                versions=versions or {},
                request_payload={
                    "person_id": person_id,
                    "organization_name": org_name,
                    "organization_slug": org_slug,
                    "project_name": project_name,
                    "project_slug": project_slug,
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

    return _claim_from_operation(operation)


def _claim_from_operation(operation: OperationResult) -> SelfHostedClaim:
    result = operation.result
    required = ("claim_id", "person_id", "org_id", "project_id", "journey_id")
    if (
        operation.outcome != "succeeded"
        or not operation.audit_event_id
        or not operation.outbox_event_id
        or any(not isinstance(result.get(key), str) for key in required)
    ):
        raise SelfHostedClaimUnavailable("instance claim result unavailable")
    return SelfHostedClaim(
        claim_id=result["claim_id"],
        person_id=result["person_id"],
        org_id=result["org_id"],
        project_id=result["project_id"],
        journey_id=result["journey_id"],
        operation_id=operation.operation_id,
        audit_event_id=operation.audit_event_id,
        outbox_event_id=operation.outbox_event_id,
        replayed=operation.replayed,
    )
