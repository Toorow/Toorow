"""Scoped invitation issuance with ephemeral fragment-only bearer handoff."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from ulid import ULID

from core.operations import (
    MutationResult,
    OperationResult,
    OperationSpec,
    execute_operation,
    prepare_operation,
)


class InvitationValidationError(ValueError):
    """The immutable invitation request is malformed or unsafe."""


@dataclass(frozen=True)
class IdentityBinding:
    identity_hash: str


@dataclass(frozen=True)
class InvitationGrant:
    scope_type: str
    scope_id: str
    capability: str


@dataclass(frozen=True)
class InvitationIssueResult:
    invitation_id: str
    state: str
    expires_at: str | None
    operation_id: str
    audit_event_id: str | None
    delivery_url: str | None
    replayed: bool


def _pepper() -> bytes:
    value = os.environ.get("TOOROW_INVITATION_PEPPER", "")
    if len(value.encode("utf-8")) < 32:
        raise InvitationValidationError("TOOROW_INVITATION_PEPPER must contain 32 bytes")
    return value.encode("utf-8")


def normalize_invited_identity(value: str) -> str:
    if not isinstance(value, str):
        raise InvitationValidationError("invited_identity must be a string")
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    if len(normalized) > 320 or normalized.count("@") != 1:
        raise InvitationValidationError("invited_identity must be one bounded email identity")
    local, domain = normalized.rsplit("@", 1)
    if not local or not domain or "." not in domain or any(ch.isspace() for ch in normalized):
        raise InvitationValidationError("invited_identity is malformed")
    try:
        domain = domain.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise InvitationValidationError("invited_identity domain is malformed") from exc
    return f"{local}@{domain}"


def prepare_identity_binding(normalized_identity: str) -> IdentityBinding:
    digest = hmac.new(
        _pepper(), f"invited-identity:{normalized_identity}".encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return IdentityBinding(digest)


def _bearer_hash(bearer: str) -> str:
    return hmac.new(
        _pepper(), f"invitation-bearer:{bearer}".encode("utf-8"), hashlib.sha256
    ).hexdigest()


def build_fragment_delivery_url(bearer: str) -> str:
    origin = os.environ.get("TOOROW_INVITATION_ORIGIN", "").rstrip("/")
    parsed = urlsplit(origin)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise InvitationValidationError("TOOROW_INVITATION_ORIGIN must be an HTTPS origin")
    return f"{origin}/invite#invite={bearer}"


def validate_invitation_request(
    *,
    invited_identity: str,
    role: str,
    project_grants: tuple[InvitationGrant, ...],
    datastream_grants: tuple[InvitationGrant, ...],
    expires_in_hours: int,
) -> tuple[str, tuple[InvitationGrant, ...]]:
    normalized = normalize_invited_identity(invited_identity)
    if role not in {"owner", "admin", "member", "viewer"}:
        raise InvitationValidationError("invalid invited role")
    if type(expires_in_hours) is not int or not 1 <= expires_in_hours <= 168:
        raise InvitationValidationError("expires_in_hours must be an integer in 1..168")
    grants = tuple(project_grants) + tuple(datastream_grants)
    if len(grants) > 100:
        raise InvitationValidationError("too many invitation grants")
    seen: set[tuple[str, str]] = set()
    typed_grants = tuple((grant, "project") for grant in project_grants) + tuple(
        (grant, "flux") for grant in datastream_grants
    )
    for grant, expected in typed_grants:
        if not isinstance(grant, InvitationGrant):
            raise InvitationValidationError("invalid invitation grant")
        if grant.scope_type != expected or grant.capability not in {"view", "edit", "manage"}:
            raise InvitationValidationError("invalid invitation grant")
        if not grant.scope_id or len(grant.scope_id) > 256:
            raise InvitationValidationError("invalid invitation scope")
        key = (grant.scope_type, grant.scope_id)
        if key in seen:
            raise InvitationValidationError("duplicate invitation scope")
        seen.add(key)
    return normalized, tuple(sorted(grants, key=lambda item: (item.scope_type, item.scope_id)))


def issue_invitation(
    conn,
    *,
    invited_identity: str,
    org_id: str,
    role: str,
    project_grants: tuple[InvitationGrant, ...],
    datastream_grants: tuple[InvitationGrant, ...],
    issuer: str,
    expires_in_hours: int,
    policy_version: str,
    idempotency_key: str,
    host_context: dict,
    trace_id: str | None,
) -> InvitationIssueResult:
    """Issue after the adapter has authorized every requested resource."""
    normalized, grants = validate_invitation_request(
        invited_identity=invited_identity,
        role=role,
        project_grants=project_grants,
        datastream_grants=datastream_grants,
        expires_in_hours=expires_in_hours,
    )
    identity_hash = prepare_identity_binding(normalized).identity_hash
    bindings = [grant.__dict__ for grant in grants]
    spec = OperationSpec(
        command_type="invitation.issue",
        actor=issuer,
        effective_org_id=org_id,
        resource_path=(f"organization:{org_id}", f"invitation-subject:{identity_hash}"),
        idempotency_key=idempotency_key,
        host_context=host_context,
        versions={"policy": policy_version, "catalog": "invitation-v1", "tool": "rest-v1"},
        request_payload={
            "invited_identity_hash": identity_hash,
            "role": role,
            "grants": bindings,
            "expires_in_hours": expires_in_hours,
        },
        provider_references={},
        confirmation_mode="server",
        confirmation_reference=None,
        trace_id=trace_id,
    )
    prepare_operation(spec)
    handoff: dict[str, str] = {}

    def mutation(operation_conn, operation_id: str) -> MutationResult:
        bearer = secrets.token_urlsafe(32)
        invitation_id = f"invite_{ULID()}"
        expires_at = datetime.now(tz=timezone.utc) + timedelta(hours=expires_in_hours)
        with operation_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.invitations
                    (id, invited_identity_hash, org_id, invited_role, grant_bindings,
                     issuer, policy_version, bearer_hash, expires_at, operation_id)
                VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s)
                """,
                (
                    invitation_id,
                    identity_hash,
                    org_id,
                    role,
                    json.dumps(bindings),
                    issuer,
                    policy_version,
                    _bearer_hash(bearer),
                    expires_at,
                    operation_id,
                ),
            )
        handoff["delivery_url"] = build_fragment_delivery_url(bearer)
        # First-value funnel (E36-FR09): the delivery handoff was minted -> record the
        # invitation_delivery stage. subject is the pseudonymous identity hash (already a
        # one-way digest); the project (if any grant scopes one) lets the tenant read it.
        # Best-effort -- NEVER breaks issuance.
        from core.first_value_instrumentation import record_stage  # noqa: PLC0415

        _delivery_project = next(
            (g["scope_id"] for g in bindings if g.get("scope_type") == "project"), None
        )
        record_stage(
            operation_conn,
            org_id=org_id,
            project_id=_delivery_project or org_id,
            subject=identity_hash,
            stage="invitation_delivery",
            outcome="succeeded",
        )
        result = {
            "invitation_id": invitation_id,
            "state": "pending",
            "expires_at": expires_at.isoformat(),
        }
        return MutationResult(
            outcome="succeeded",
            before_hash=None,
            after_hash=hashlib.sha256(
                json.dumps(result, sort_keys=True).encode("utf-8")
            ).hexdigest(),
            result=result,
            outbox_payload={
                "invitation_id": invitation_id,
                "delivery_kind": "invitation_email",
            },
        )

    operation: OperationResult = execute_operation(conn, spec, mutation=mutation)
    return InvitationIssueResult(
        invitation_id=operation.result["invitation_id"],
        state=operation.result["state"],
        expires_at=operation.result.get("expires_at"),
        operation_id=operation.operation_id,
        audit_event_id=operation.audit_event_id,
        delivery_url=None if operation.replayed else handoff.get("delivery_url"),
        replayed=operation.replayed,
    )


def record_delivery_evidence(
    conn,
    *,
    event_id: str,
    confirmed: bool,
    delivered: bool = False,
    uncertain: bool = False,
    evidence_class: str | None = None,
    evidence_hash: str | None = None,
) -> None:
    """Apply sanitized adapter evidence and reuse generic outbox retry state."""
    if evidence_hash is not None and not re.fullmatch(r"[0-9a-f]{64}", evidence_hash):
        raise InvitationValidationError("delivery evidence must be a sha256 hash")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT operation_id FROM app.operation_outbox WHERE id = %s FOR UPDATE",
            (event_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise InvitationValidationError("delivery event not found")
        operation_id = row[0]
        if confirmed:
            state = "delivered" if delivered else "delivery_failed"
            cur.execute(
                """
                UPDATE app.invitations
                SET state = %s, delivery_evidence_class = %s,
                    delivery_evidence_hash = %s, updated_at = NOW()
                WHERE operation_id = %s AND state = 'pending'
                """,
                (state, evidence_class, evidence_hash, operation_id),
            )
    from core.operations import record_delivery_result  # noqa: PLC0415

    record_delivery_result(
        conn,
        event_id=event_id,
        confirmed=confirmed,
        uncertain=uncertain,
        error_class=None if delivered else evidence_class,
    )


class InvitationExchangeError(RuntimeError):
    """A nondisclosing invitation exchange or acceptance denial."""


class InvitationAcceptanceConflict(RuntimeError):
    """A valid invitation conflicts with pre-existing authority."""


@dataclass(frozen=True)
class InvitationExchangeResult:
    session_value: str
    max_age_seconds: int


@dataclass(frozen=True)
class InvitationAcceptanceResult:
    invitation_id: str
    org_id: str
    role: str
    explicit_grants: tuple[dict, ...]
    explicit_none: bool
    operation_id: str
    audit_event_id: str | None
    outbox_event_id: str | None
    next_url: str
    replayed: bool


def _exchange_session_hash(value: str) -> str:
    return hmac.new(
        _pepper(), f"invitation-exchange:{value}".encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _unavailable() -> InvitationExchangeError:
    return InvitationExchangeError("invitation is unavailable")


def exchange_invitation(
    conn,
    *,
    bearer: str,
    verified_identity: str,
    max_age_seconds: int = 600,
) -> InvitationExchangeResult:
    """Consume one bearer into a narrow authenticated acceptance session."""
    if not isinstance(bearer, str) or not 32 <= len(bearer) <= 512:
        raise _unavailable()
    try:
        normalized = normalize_invited_identity(verified_identity)
        subject_hash = prepare_identity_binding(normalized).identity_hash
        candidate_hash = _bearer_hash(bearer)
    except InvitationValidationError as exc:
        raise _unavailable() from exc
    now = datetime.now(tz=timezone.utc)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, invited_identity_hash, state, expires_at, superseded_by,
                   bearer_hash, bearer_consumed_at, policy_version
            FROM app.invitations
            WHERE bearer_hash = %s
            FOR UPDATE
            """,
            (candidate_hash,),
        )
        row = cur.fetchone()
        if (
            row is None
            or not hmac.compare_digest(row[5], candidate_hash)
            or not hmac.compare_digest(row[1], subject_hash)
            or row[2] not in {"pending", "delivered"}
            or _aware_utc(row[3]) <= now
            or row[4] is not None
            or row[6] is not None
        ):
            raise _unavailable()
        session_value = secrets.token_urlsafe(32)
        session_hash = _exchange_session_hash(session_value)
        exchange_id = f"ixs_{ULID()}"
        expires_at = min(_aware_utc(row[3]), now + timedelta(seconds=max_age_seconds))
        cur.execute(
            """
            UPDATE app.invitations
            SET bearer_consumed_at = %s, updated_at = %s
            WHERE id = %s AND bearer_consumed_at IS NULL
            """,
            (now, now, row[0]),
        )
        if cur.rowcount != 1:
            raise _unavailable()
        cur.execute(
            """
            INSERT INTO app.invitation_exchange_sessions
                (id, invitation_id, verified_subject_hash, session_hash, expires_at)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (exchange_id, row[0], subject_hash, session_hash, expires_at),
        )
    return InvitationExchangeResult(
        session_value=session_value,
        max_age_seconds=max(1, int((expires_at - now).total_seconds())),
    )


def _acceptance_result_from_operation(
    operation: OperationResult,
) -> InvitationAcceptanceResult:
    payload = operation.result
    grants = tuple(payload.get("explicit_grants") or ())
    return InvitationAcceptanceResult(
        invitation_id=payload["invitation_id"],
        org_id=payload["org_id"],
        role=payload["role"],
        explicit_grants=grants,
        explicit_none=not grants,
        operation_id=operation.operation_id,
        audit_event_id=operation.audit_event_id,
        outbox_event_id=operation.outbox_event_id,
        next_url=payload.get("next_url", "/onboarding/responsibilities"),
        replayed=operation.replayed,
    )


def accept_invitation(
    conn,
    *,
    session_value: str,
    verified_identity: str,
    confirmed: bool,
    idempotency_key: str,
    host_context: dict,
    trace_id: str | None,
) -> InvitationAcceptanceResult:
    """Atomically activate the exact invitation binding for its verified subject."""
    if confirmed is not True or not isinstance(session_value, str) or len(session_value) < 32:
        raise _unavailable()
    try:
        normalized = normalize_invited_identity(verified_identity)
        subject_hash = prepare_identity_binding(normalized).identity_hash
        session_hash = _exchange_session_hash(session_value)
    except InvitationValidationError as exc:
        raise _unavailable() from exc
    now = datetime.now(tz=timezone.utc)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.id, i.id, s.verified_subject_hash, s.expires_at,
                   s.consumed_at, s.accepted_operation_id, i.org_id,
                   i.invited_role, i.grant_bindings, i.state, i.expires_at,
                   i.superseded_by, i.policy_version, s.resolution, i.issuer
            FROM app.invitation_exchange_sessions s
            JOIN app.invitations i ON i.id = s.invitation_id
            WHERE s.session_hash = %s
            FOR UPDATE OF s, i
            """,
            (session_hash,),
        )
        row = cur.fetchone()
        if row is None or not hmac.compare_digest(row[2], subject_hash):
            raise _unavailable()
        if row[4] is not None:
            resolution = row[13] if len(row) > 13 else None
            if not isinstance(resolution, dict) or not row[5]:
                raise _unavailable()
            cur.execute(
                "SELECT state, result, audit_event_id, outbox_event_id "
                "FROM app.operations WHERE id = %s",
                (row[5],),
            )
            stored = cur.fetchone()
            if stored is None:
                raise _unavailable()
            return _acceptance_result_from_operation(
                OperationResult(
                    row[5], stored[0], stored[1] or resolution, stored[2], stored[3], True
                )
            )
        configured_policy = os.environ.get("TOOROW_POLICY_VERSION")
        if (
            _aware_utc(row[3]) <= now
            or row[9] not in {"pending", "delivered"}
            or _aware_utc(row[10]) <= now
            or row[11] is not None
            or (configured_policy and configured_policy != row[12])
        ):
            raise _unavailable()

    exchange_id, invitation_id = row[0], row[1]
    org_id, role, raw_bindings = row[6], row[7], row[8]
    if not isinstance(raw_bindings, list):
        raise _unavailable()
    bindings: tuple[dict, ...] = tuple(
        {
            "scope_type": item.get("scope_type"),
            "scope_id": item.get("scope_id"),
            "capability": item.get("capability"),
        }
        for item in raw_bindings
        if isinstance(item, dict)
    )
    if len(bindings) != len(raw_bindings):
        raise _unavailable()
    for binding in bindings:
        grant = InvitationGrant(
            str(binding["scope_type"] or ""),
            str(binding["scope_id"] or ""),
            str(binding["capability"] or ""),
        )
        expected = (grant,) if grant.scope_type == "project" else ()
        if grant.scope_type not in {"project", "flux"}:
            raise _unavailable()
        expected_flux = (grant,) if grant.scope_type == "flux" else ()
        validate_invitation_request(
            invited_identity=normalized,
            role=role,
            project_grants=expected,
            datastream_grants=expected_flux,
            expires_in_hours=1,
        )

    spec = OperationSpec(
        command_type="invitation.accept",
        actor=verified_identity,
        effective_org_id=org_id,
        resource_path=(f"organization:{org_id}", f"invitation:{invitation_id}"),
        idempotency_key=idempotency_key,
        host_context=host_context,
        versions={"policy": row[12], "catalog": "invitation-v1", "tool": "rest-v1"},
        request_payload={
            "invitation_id": invitation_id,
            "verified_subject_hash": subject_hash,
            "role": role,
            "grants": list(bindings),
        },
        provider_references={},
        confirmation_mode="human",
        confirmation_reference=f"invitation:{invitation_id}:confirmed",
        trace_id=trace_id,
    )
    prepare_operation(spec)

    def mutation(operation_conn, operation_id: str) -> MutationResult:
        with operation_conn.cursor() as cur:
            for item in bindings:
                table = "projects" if item["scope_type"] == "project" else "datastreams"
                cur.execute(
                    f"SELECT 1 FROM app.{table} WHERE id = %s AND org_id = %s",
                    (item["scope_id"], org_id),
                )
                if cur.fetchone() is None:
                    raise _unavailable()
            cur.execute(
                "SELECT COUNT(*) FROM app.resource_grants WHERE org_id = %s AND identity = %s",
                (org_id, verified_identity),
            )
            existing_grants = cur.fetchone()
            if existing_grants and existing_grants[0]:
                raise InvitationAcceptanceConflict("existing grants conflict with invitation")
            cur.execute(
                "SELECT role, status FROM app.org_members "
                "WHERE org_id = %s AND identity = %s FOR UPDATE",
                (org_id, verified_identity),
            )
            member = cur.fetchone()
            if member is None:
                cur.execute(
                    """
                    INSERT INTO app.org_members
                        (id, org_id, identity, role, status, invited_by, invited_at, joined_at)
                    SELECT %s, org_id, %s, invited_role, 'active', issuer, created_at, %s
                    FROM app.invitations WHERE id = %s
                    """,
                    (f"omem_{ULID()}", verified_identity, now, invitation_id),
                )
            elif member != (role, "invited"):
                raise InvitationAcceptanceConflict("existing membership conflicts with invitation")
            else:
                cur.execute(
                    "UPDATE app.org_members SET status = 'active', joined_at = %s "
                    "WHERE org_id = %s AND identity = %s AND status = 'invited'",
                    (now, org_id, verified_identity),
                )
            for item in bindings:
                cur.execute(
                    """
                    INSERT INTO app.resource_grants
                        (id, org_id, identity, scope_type, scope_id, capability, granted_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        f"rgrant_{ULID()}",
                        org_id,
                        verified_identity,
                        item["scope_type"],
                        item["scope_id"],
                        item["capability"],
                        verified_identity,
                    ),
                )
            cur.execute(
                """
                UPDATE app.invitations
                SET state = 'accepted', accepted_at = %s, updated_at = %s
                WHERE id = %s AND state IN ('pending', 'delivered')
                  AND accepted_at IS NULL
                """,
                (now, now, invitation_id),
            )
            if cur.rowcount != 1:
                raise _unavailable()
            project_id = next(
                (item["scope_id"] for item in bindings if item["scope_type"] == "project"),
                None,
            )
            from core.setup_responsibilities import bootstrap_journey_from_acceptance

            journey_id = bootstrap_journey_from_acceptance(
                operation_conn,
                invitation_id=invitation_id,
                org_id=org_id,
                project_id=project_id,
                operator_identity=verified_identity,
                toorow_admin_identity=row[14] if len(row) > 14 else verified_identity,
                accepted_at=now,
            )
            result = {
                "invitation_id": invitation_id,
                "org_id": org_id,
                "role": role,
                "explicit_grants": list(bindings),
                "next_url": "/onboarding/responsibilities",
                "journey_id": journey_id,
            }
            # First-value funnel (E36-FR09): record the acceptance stage and bind this
            # journey to its project ONCE, so the tenant-facing read can surface it. The
            # pseudonymous ref is built inside the seam from (org, project, subject); the
            # raw identity never reaches analytics. Best-effort -- NEVER breaks acceptance.
            from core.first_value_instrumentation import record_stage  # noqa: PLC0415

            record_stage(
                operation_conn,
                org_id=org_id,
                project_id=project_id,
                subject=verified_identity,
                stage="invitation_acceptance",
                outcome="succeeded",
                bind_project=bool(project_id),
            )
            cur.execute(
                """
                UPDATE app.invitation_exchange_sessions
                SET consumed_at = %s, accepted_operation_id = %s, resolution = %s::jsonb
                WHERE id = %s AND consumed_at IS NULL
                """,
                (now, operation_id, json.dumps(result), exchange_id),
            )
            if cur.rowcount != 1:
                raise _unavailable()
        return MutationResult(
            outcome="succeeded",
            before_hash=None,
            after_hash=hashlib.sha256(
                json.dumps(result, sort_keys=True).encode("utf-8")
            ).hexdigest(),
            result=result,
            outbox_payload={"invitation_id": invitation_id, "transition": "accepted"},
        )

    return _acceptance_result_from_operation(execute_operation(conn, spec, mutation=mutation))


class InvitationLifecycleConflict(RuntimeError):
    """The requested lifecycle transition cannot win the current state."""


@dataclass(frozen=True)
class InvitationTransitionResult:
    invitation_id: str
    state: str
    operation_id: str | None
    audit_event_id: str | None
    outbox_event_id: str | None
    replayed: bool


def list_safe_invitations(
    conn,
    *,
    org_id: str,
    now: datetime | None = None,
) -> list[dict]:
    """Return the authorized adapter's secret-free invitation status projection."""
    observed_at = now or datetime.now(tz=timezone.utc)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, invited_role, grant_bindings, issuer, policy_version, state,
                   expires_at, delivery_evidence_class, delivery_evidence_hash,
                   created_at, accepted_at, revoked_at, superseded_by
            FROM app.invitations
            WHERE org_id = %s
            ORDER BY created_at DESC, id DESC
            """,
            (org_id,),
        )
        rows = cur.fetchall()
    results = []
    for row in rows:
        state = row[5]
        if state in {"pending", "delivered"} and _aware_utc(row[6]) <= observed_at:
            state = "expired"
        grants = row[2] if isinstance(row[2], list) else []
        results.append(
            {
                "invitation_id": row[0],
                "role": row[1],
                "explicit_grants": grants,
                "explicit_none": not grants,
                "issuer": row[3],
                "policy_version": row[4],
                "state": state,
                "expires_at": row[6].isoformat(),
                "delivery_evidence": {
                    "class": row[7],
                    "evidence_hash": row[8],
                },
                "created_at": row[9].isoformat(),
                "accepted_at": row[10].isoformat() if row[10] else None,
                "revoked_at": row[11].isoformat() if row[11] else None,
                "superseded_by": row[12],
                "available_actions": (
                    ["resend", "revoke"]
                    if row[5] in {"pending", "delivered", "delivery_failed"}
                    else []
                ),
            }
        )
    return results


def transition_invitation(
    conn,
    *,
    invitation_id: str,
    actor: str,
    transition: str,
    idempotency_key: str,
    host_context: dict,
    trace_id: str | None,
) -> InvitationTransitionResult:
    """Revoke or durably expire one unaccepted invitation under a row lock."""
    if transition not in {"revoke", "expire"}:
        raise InvitationLifecycleConflict("unsupported invitation transition")
    now = datetime.now(tz=timezone.utc)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, org_id, state, expires_at, superseded_by, accepted_at
            FROM app.invitations WHERE id = %s FOR UPDATE
            """,
            (invitation_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise InvitationLifecycleConflict("invitation unavailable")
    target = "revoked" if transition == "revoke" else "expired"
    if row[2] == target:
        return InvitationTransitionResult(row[0], target, None, None, None, True)
    if row[2] not in {"pending", "delivered", "delivery_failed"} or row[5] is not None:
        raise InvitationLifecycleConflict("invitation lifecycle is already resolved")
    if transition == "expire" and _aware_utc(row[3]) > now:
        raise InvitationLifecycleConflict("invitation has not expired")
    spec = OperationSpec(
        command_type=f"invitation.{transition}",
        actor=actor,
        effective_org_id=row[1],
        resource_path=(f"organization:{row[1]}", f"invitation:{row[0]}"),
        idempotency_key=idempotency_key,
        host_context=host_context,
        versions={
            "policy": os.environ.get("TOOROW_POLICY_VERSION", "v1"),
            "catalog": "invitation-v1",
            "tool": "rest-v1",
        },
        request_payload={"invitation_id": row[0], "transition": target},
        provider_references={},
        confirmation_mode="human",
        confirmation_reference=f"invitation:{row[0]}:{target}",
        trace_id=trace_id,
    )

    def mutation(operation_conn, _operation_id: str) -> MutationResult:
        with operation_conn.cursor() as cur:
            if transition == "revoke":
                cur.execute(
                    """
                    UPDATE app.invitations
                    SET state = 'revoked', revoked_at = %s, updated_at = %s
                    WHERE id = %s AND state IN ('pending', 'delivered', 'delivery_failed')
                      AND accepted_at IS NULL
                    """,
                    (now, now, row[0]),
                )
            else:
                cur.execute(
                    """
                    UPDATE app.invitations
                    SET state = 'expired', updated_at = %s
                    WHERE id = %s AND state IN ('pending', 'delivered', 'delivery_failed')
                      AND accepted_at IS NULL AND expires_at <= %s
                    """,
                    (now, row[0], now),
                )
            if cur.rowcount != 1:
                raise InvitationLifecycleConflict("invitation lifecycle race lost")
            cur.execute(
                """
                UPDATE app.invitation_exchange_sessions
                SET consumed_at = COALESCE(consumed_at, %s),
                    resolution = COALESCE(resolution, %s::jsonb)
                WHERE invitation_id = %s
                """,
                (now, json.dumps({"state": target}), row[0]),
            )
        result = {"invitation_id": row[0], "state": target}
        return MutationResult(
            outcome="succeeded",
            before_hash=None,
            after_hash=hashlib.sha256(
                json.dumps(result, sort_keys=True).encode("utf-8")
            ).hexdigest(),
            result=result,
            outbox_payload={"invitation_id": row[0], "transition": target},
        )

    operation = execute_operation(conn, spec, mutation=mutation)
    return InvitationTransitionResult(
        row[0],
        operation.result["state"],
        operation.operation_id,
        operation.audit_event_id,
        operation.outbox_event_id,
        operation.replayed,
    )


def resend_invitation(
    conn,
    *,
    invitation_id: str,
    actor: str,
    expires_in_hours: int,
    idempotency_key: str,
    host_context: dict,
    trace_id: str | None,
) -> InvitationIssueResult:
    """Supersede an unaccepted invitation and return only the fresh handoff once."""
    if type(expires_in_hours) is not int or not 1 <= expires_in_hours <= 168:
        raise InvitationValidationError("expires_in_hours must be an integer in 1..168")
    now = datetime.now(tz=timezone.utc)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, invited_identity_hash, org_id, invited_role, grant_bindings,
                   policy_version, state, expires_at, superseded_by, accepted_at
            FROM app.invitations WHERE id = %s FOR UPDATE
            """,
            (invitation_id,),
        )
        row = cur.fetchone()
    if row is not None and row[6] == "revoked" and row[8] and row[9] is None:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT i.id, i.state, i.expires_at, i.operation_id, o.audit_event_id
                FROM app.invitations i
                LEFT JOIN app.operations o ON o.id = i.operation_id
                WHERE i.id = %s
                """,
                (row[8],),
            )
            replacement = cur.fetchone()
        if replacement is None:
            raise InvitationLifecycleConflict("invitation replacement unavailable")
        return InvitationIssueResult(
            invitation_id=replacement[0],
            state=replacement[1],
            expires_at=replacement[2].isoformat(),
            operation_id=replacement[3],
            audit_event_id=replacement[4],
            delivery_url=None,
            replayed=True,
        )
    if (
        row is None
        or row[6] not in {"pending", "delivered", "delivery_failed"}
        or row[8] is not None
        or row[9] is not None
    ):
        raise InvitationLifecycleConflict("invitation cannot be resent")
    bindings = row[4] if isinstance(row[4], list) else []
    spec = OperationSpec(
        command_type="invitation.resend",
        actor=actor,
        effective_org_id=row[2],
        resource_path=(f"organization:{row[2]}", f"invitation:{row[0]}"),
        idempotency_key=idempotency_key,
        host_context=host_context,
        versions={"policy": row[5], "catalog": "invitation-v1", "tool": "rest-v1"},
        request_payload={
            "invitation_id": row[0],
            "role": row[3],
            "grants": bindings,
            "expires_in_hours": expires_in_hours,
        },
        provider_references={},
        confirmation_mode="human",
        confirmation_reference=f"invitation:{row[0]}:resend",
        trace_id=trace_id,
    )
    handoff: dict[str, str] = {}

    def mutation(operation_conn, operation_id: str) -> MutationResult:
        bearer = secrets.token_urlsafe(32)
        replacement_id = f"invite_{ULID()}"
        expires_at = now + timedelta(hours=expires_in_hours)
        with operation_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.invitations
                    (id, invited_identity_hash, org_id, invited_role, grant_bindings,
                     issuer, policy_version, bearer_hash, expires_at, operation_id)
                VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s)
                """,
                (
                    replacement_id,
                    row[1],
                    row[2],
                    row[3],
                    json.dumps(bindings),
                    actor,
                    row[5],
                    _bearer_hash(bearer),
                    expires_at,
                    operation_id,
                ),
            )
            cur.execute(
                """
                UPDATE app.invitations
                SET state = 'revoked', revoked_at = %s, superseded_by = %s, updated_at = %s
                WHERE id = %s AND state IN ('pending', 'delivered', 'delivery_failed')
                  AND accepted_at IS NULL AND superseded_by IS NULL
                """,
                (now, replacement_id, now, row[0]),
            )
            if cur.rowcount != 1:
                raise InvitationLifecycleConflict("invitation resend race lost")
            cur.execute(
                """
                UPDATE app.invitation_exchange_sessions
                SET consumed_at = COALESCE(consumed_at, %s),
                    resolution = COALESCE(resolution, %s::jsonb)
                WHERE invitation_id = %s
                """,
                (now, json.dumps({"state": "superseded"}), row[0]),
            )
        handoff["delivery_url"] = build_fragment_delivery_url(bearer)
        result = {
            "invitation_id": replacement_id,
            "state": "pending",
            "expires_at": expires_at.isoformat(),
        }
        return MutationResult(
            outcome="succeeded",
            before_hash=None,
            after_hash=hashlib.sha256(
                json.dumps(result, sort_keys=True).encode("utf-8")
            ).hexdigest(),
            result=result,
            outbox_payload={
                "invitation_id": replacement_id,
                "delivery_kind": "invitation_email",
                "transition": "replacement_issued",
            },
        )

    operation = execute_operation(conn, spec, mutation=mutation)
    return InvitationIssueResult(
        invitation_id=operation.result["invitation_id"],
        state=operation.result["state"],
        expires_at=operation.result.get("expires_at"),
        operation_id=operation.operation_id,
        audit_event_id=operation.audit_event_id,
        delivery_url=None if operation.replayed else handoff.get("delivery_url"),
        replayed=operation.replayed,
    )
