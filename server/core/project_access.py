"""Fail-closed Organization and Project access resolution (AD-5)."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)


class ProjectAccessUnavailable(RuntimeError):
    """Raised when a Project access decision cannot be verified."""


_ORG_MANAGE_ROLES = frozenset({"owner", "admin"})
_ROLE_CAPABILITY = {"viewer": "view", "member": "edit", "admin": "manage", "owner": "manage"}
_CAPABILITY_ROLE = {"view": "viewer", "edit": "member", "manage": "owner"}


def resolve_org_role(
    org_id: str,
    identity: str,
    conn,
    *,
    auth_mode: str | None = None,
) -> str | None:
    """Return an active Organization membership role; never infer access."""
    del auth_mode
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT m.role
                FROM app.organizations o
                JOIN app.org_members m
                  ON m.org_id = o.id AND m.identity = %s AND m.status = 'active'
                WHERE o.id = %s AND o.status = 'active'
                """,
                (identity, org_id),
            )
            row = cur.fetchone()
    except Exception as exc:
        raise ProjectAccessUnavailable("organization access could not be verified") from exc
    return str(row[0]) if isinstance(row, (tuple, list)) and row else None


def identity_can_manage_org(org_id: str, identity: str, conn) -> bool:
    return resolve_org_role(org_id, identity, conn) in _ORG_MANAGE_ROLES


def identity_has_org_access(org_id: str, identity: str, conn) -> bool:
    return resolve_org_role(org_id, identity, conn) is not None


_CAPABILITY_ORDER = {"view": 1, "edit": 2, "manage": 3}


@dataclass(frozen=True)
class AccessDecision:
    allowed: bool
    reason: str
    capability: str | None = None
    org_id: str | None = None
    resource_path: tuple[str, ...] = ()


def _denied(reason: str) -> AccessDecision:
    return AccessDecision(False, reason)


def resolve_strict_resource_access(
    identity: str,
    conn,
    *,
    project_id: str | None = None,
    datastream_id: str | None = None,
    minimum_capability: str = "view",
    auth_mode: str | None = None,
    hold_access: bool = False,
    include_archived_project: bool = False,
) -> AccessDecision:
    """Resolve one project/Datastream path with no default-open fallback.

    THE IDENTITY IS TRANSLATED HERE BECAUSE THIS IS WHERE IT IS COMPARED. Every
    branch below matches `identity` against `app.org_members.identity`, which
    carries the canonical `person_<ULID>`. Thirty MCP modules each keep their own
    copy of `token.claims.get("sub", ...)` and hand this function a raw OIDC
    subject, which matches no membership row: measured 2026-08-13, the owner of
    the organization was denied `not_found` on their own active project while the
    same call with their canonical id returned `owner_floor`.

    Repairing the thirty copies would leave the thirty-first free to be wrong.
    This is the single place all of them arrive at, so it is the place that
    translates -- and the translation is a no-op for the HTTP path, which already
    resolves its principal to a person before calling.
    """
    # AI-363 (amendment project-settings.md 2026-09-02): an archived Project
    # stays VISIBLE to the identities that hold a capability on it -- but only
    # to callers that say so. The default refuses archived rows exactly as
    # before, so none of the thirty MCP modules changes behaviour; the three
    # doors of projects_api (GET, DELETE, restore) pass the flag explicitly.
    project_statuses = ("active", "archived") if include_archived_project else ("active",)
    if minimum_capability not in _CAPABILITY_ORDER:
        raise ValueError(f"unknown capability: {minimum_capability}")
    # THE DECLARED EVALUATION IDENTITY, ratified 2026-08-24 in
    # `docs/product-architecture/analyze-and-test.md` (AI-305). Two clauses, and
    # the ORDER of them is the whole guarantee.
    #
    # First, the REFUSAL, and it is explicit rather than "not admitted". Outside a
    # declared evaluation environment this identity is refused BEFORE any read, so
    # its refusal in production cannot be overturned by a row: without this line,
    # `TOOROW_AUTH_MODE=oauth` never enters the door below at all, and an
    # `app.org_members` row for `person_EVALUATION` inserted in production would
    # have been honoured like anybody's. Measured while writing the negative
    # control test, which is why it is a line and not a comment.
    from core.evaluation_identity import (  # noqa: PLC0415
        evaluation_environment_declared,
        is_evaluation_identity,
    )

    identity_is_evaluation = is_evaluation_identity(identity)
    if identity_is_evaluation and not evaluation_environment_declared():
        return _denied("production_identity_required")

    mode = (auth_mode or os.environ.get("TOOROW_AUTH_MODE", "disabled")).strip().lower()
    if not identity or identity == "anonymous" or mode == "disabled":
        # Second, the ADMISSION, and it is a DOOR, not a grant. The eval harness
        # calls the tools in process, where auth is `disabled`, so this is the
        # line it would otherwise die on -- as `anonymous` it did, and the
        # business-path dimension of the corpus was mute because of it. Past here
        # NOTHING is relaxed: the membership row, the organization status, the
        # role and the capability floor are all still read below, so an
        # evaluation run that is not a member is refused exactly like anyone else.
        if not identity_is_evaluation:
            return _denied("production_identity_required")
    if bool(project_id) == bool(datastream_id):
        return _denied("invalid_resource")

    from core.identity_bridge import canonical_identity  # noqa: PLC0415

    identity = canonical_identity(identity, conn)

    try:
        if hold_access:
            with conn.cursor() as cur:
                if project_id:
                    cur.execute(
                        """
                        SELECT p.org_id, o.status
                        FROM app.projects p
                        JOIN app.organizations o ON o.id = p.org_id
                        WHERE p.id = %s AND p.status = ANY(%s)
                        FOR SHARE OF p, o
                        """,
                        (project_id, list(project_statuses)),
                    )
                    scope_type, scope_id = "project", project_id
                else:
                    cur.execute(
                        """
                        SELECT d.org_id, o.status
                        FROM app.datastreams d
                        JOIN app.organizations o ON o.id = d.org_id
                        WHERE d.id = %s
                        FOR SHARE OF d, o
                        """,
                        (datastream_id,),
                    )
                    scope_type, scope_id = "flux", datastream_id
                scope_row = cur.fetchone()
                if not isinstance(scope_row, (tuple, list)) or len(scope_row) < 2:
                    return _denied("not_found")
                org_id, org_status = scope_row[:2]
                cur.execute(
                    """
                    SELECT role, status
                    FROM app.org_members
                    WHERE org_id = %s AND identity = %s
                    FOR SHARE
                    """,
                    (org_id, identity),
                )
                member_row = cur.fetchone()
            if not isinstance(member_row, (tuple, list)) or len(member_row) < 2:
                return _denied("not_found")
            role, member_status = member_row[:2]
        else:
            with conn.cursor() as cur:
                if project_id:
                    cur.execute(
                        """
                        SELECT p.org_id, o.status, m.role, m.status
                        FROM app.projects p
                        JOIN app.organizations o ON o.id = p.org_id
                        LEFT JOIN app.org_members m
                          ON m.org_id = p.org_id AND m.identity = %s
                        WHERE p.id = %s AND p.status = ANY(%s)
                        """,
                        (identity, project_id, list(project_statuses)),
                    )
                    row = cur.fetchone()
                    scope_type, scope_id = "project", project_id
                else:
                    cur.execute(
                        """
                        SELECT d.org_id, o.status, m.role, m.status
                        FROM app.datastreams d
                        JOIN app.organizations o ON o.id = d.org_id
                        LEFT JOIN app.org_members m
                          ON m.org_id = d.org_id AND m.identity = %s
                        WHERE d.id = %s
                        """,
                        (identity, datastream_id),
                    )
                    row = cur.fetchone()
                    scope_type, scope_id = "flux", datastream_id
            if not isinstance(row, (tuple, list)) or len(row) < 4:
                return _denied("not_found")
            org_id, org_status, role, member_status = row[:4]
        if (
            org_status != "active"
            or member_status != "active"
            or role not in {"owner", "admin", "member", "viewer"}
        ):
            return _denied("not_found")
        path = (f"organization:{org_id}", f"{scope_type}:{scope_id}")
        if role == "owner":
            return AccessDecision(True, "owner_floor", "manage", str(org_id), path)

        with conn.cursor() as cur:
            grant_params = (org_id, identity, scope_type, scope_id)
            if hold_access:
                cur.execute(
                    """
                    SELECT capability FROM app.resource_grants
                    WHERE org_id = %s AND identity = %s
                      AND scope_type = %s AND scope_id = %s
                    FOR SHARE
                    """,
                    grant_params,
                )
            else:
                cur.execute(
                    """
                    SELECT capability FROM app.resource_grants
                    WHERE org_id = %s AND identity = %s
                      AND scope_type = %s AND scope_id = %s
                    """,
                    grant_params,
                )
            grant = cur.fetchone()
        if not isinstance(grant, (tuple, list)) or not grant:
            return _denied("grant_required")
        capability = str(grant[0])
        role_cap = {"viewer": "view", "member": "edit", "admin": "manage"}[str(role)]
        effective_rank = min(_CAPABILITY_ORDER.get(capability, 0), _CAPABILITY_ORDER[role_cap])
        if effective_rank < _CAPABILITY_ORDER[minimum_capability]:
            return _denied("insufficient_capability")
        effective = next(k for k, v in _CAPABILITY_ORDER.items() if v == effective_rank)
        return AccessDecision(True, "explicit_grant", effective, str(org_id), path)
    except Exception:
        logger.exception("strict resource access unavailable")
        return _denied("access_unavailable")


def project_exists(project_id: str, conn, *, include_archived: bool = False) -> bool:
    """True when *project_id* names an ACTIVE row of app.projects.

    Existence and authorization are two different questions, and the
    disabled-auth developer bypass answers only the second. Without this, `make
    dev` granted every capability on every string: `?project_id=proj_TYPO`
    passed the gate and the handler answered `200 {"topics": []}` -- an
    invented project that reads as an empty one. The strict seam already
    refuses it with a 404, so the two modes disagreed on the same URL (live
    finding C1: 200 here, 404 on the taxonomy routes, 500 on an outage).

    Fail-closed: an unreachable table is NOT an existing project.
    """
    if not project_id:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM app.projects WHERE id = %s AND status = ANY(%s)",
                (project_id, ["active", "archived"] if include_archived else ["active"]),
            )
            return cur.fetchone() is not None
    except Exception:
        logger.warning("project existence could not be verified: %s", project_id)
        return False


def identity_can_read_project(
    project_id: str,
    identity: str,
    conn,
    *,
    fail_closed: bool = True,
    auth_mode: str | None = None,
) -> bool:
    """Return the strict Project read decision; no default-open mode exists."""
    del fail_closed
    return resolve_strict_resource_access(
        identity,
        conn,
        project_id=project_id,
        minimum_capability="view",
        auth_mode=auth_mode,
    ).allowed


def identity_has_project_role(
    project_id: str,
    identity: str,
    minimum_role: str,
    conn,
    *,
    auth_mode: str | None = None,
) -> bool:
    """Map legacy role floors onto the strict effective capability model."""
    capability = _ROLE_CAPABILITY.get(minimum_role)
    if capability is None:
        raise ValueError(f"unknown project role: {minimum_role}")
    return resolve_strict_resource_access(
        identity,
        conn,
        project_id=project_id,
        minimum_capability=capability,
        auth_mode=auth_mode,
    ).allowed


def identity_can_access_project_in_org(project_id: str, identity: str, conn) -> bool:
    """Compatibility name for the same strict Project read decision."""
    return identity_can_read_project(project_id, identity, conn)


def effective_project_role(project_id: str, identity: str, conn) -> str | None:
    """Return a role-shaped label derived from strict effective capability."""
    decision = resolve_strict_resource_access(identity, conn, project_id=project_id)
    return _CAPABILITY_ROLE.get(decision.capability) if decision.allowed else None


#: The reason an authority carries when it comes from a Datastream's stored
#: activation rather than from the person standing in front of the screen.
SCHEDULED_ACTIVATION = "scheduled_activation"


def resolve_scheduled_account_access(
    conn,
    *,
    credential_id: str,
    external_account_id: str,
    beneficiary_org_id: str,
    datastream_id: str,
) -> AccessDecision:
    """The clock's authority to pull: the ACTIVATION, not a person. (AI-301)

    WHY THIS EXISTS. `resolve_provider_account_access` opens on
    `resolve_strict_resource_access(identity, ...)`, which answers from
    `app.org_members`. The nightly dispatcher has no identity and no membership,
    so it was denied `not_found` on every window -- measured on production
    2026-08-17, with the deployed `TOOROW_AUTH_MODE=oauth`:

        scheduler   -> allowed=False reason=not_found
        the owner   -> allowed=True  reason=owner_account_floor

    and the whole platform collected nothing from 2026-08-12 to 2026-08-17.

    THE BRANCH NOT TAKEN, and it was already refused in writing. Giving the clock
    a service identity is what `db.background_connection` rejects: "it would need
    an `app.org_members` row per organization for an actor who is nobody, which
    fabricates a member in the membership registry". A membership that names
    nobody would then be indistinguishable from one that names someone.

    SO THE AUTHORITY IS THE ONE THAT ALREADY EXISTS. A person authorized this
    pull when they ACTIVATED the Datastream against this account; the row is that
    decision, still standing. This function re-reads it at every dispatch rather
    than trusting it once: a Datastream turned off, archived, left as a draft or
    belonging to another organization carries no authority tonight, whatever it
    carried when someone pressed the button.

    WHAT IS NOT RELAXED, and it is most of the gate. Only the FIRST link changes.
    Credential owner still active, connection health, account scope still `ready`,
    the account still `available`, and the exposure -- owner floor or an active
    `credential_account_grants` row -- are all re-validated unchanged by
    `resolve_provider_account_access` below, because none of them asks who is
    calling. Revoke the grant, unverify the account or archive the Datastream and
    the clock stops, tonight.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT org_id, enabled, lifecycle_state, archived_at
                FROM app.datastreams WHERE id = %s
                """,
                (datastream_id,),
            )
            row = cur.fetchone()
    except Exception:
        logger.exception("scheduled account access unavailable")
        return _denied("access_unavailable")
    if not row:
        return _denied("not_found")
    org_id, enabled, lifecycle_state, archived_at = row[:4]
    if org_id != beneficiary_org_id:
        return _denied("not_found")
    if not enabled or lifecycle_state != "active" or archived_at is not None:
        return _denied("datastream_not_active")
    activation = AccessDecision(
        True,
        SCHEDULED_ACTIVATION,
        "view",
        beneficiary_org_id,
        (f"datastream:{datastream_id}",),
    )
    return resolve_provider_account_access(
        "",
        conn,
        credential_id=credential_id,
        external_account_id=external_account_id,
        beneficiary_org_id=beneficiary_org_id,
        datastream_id=datastream_id,
        _resource=activation,
    )


def resolve_provider_account_access(
    identity: str,
    conn,
    *,
    credential_id: str,
    external_account_id: str,
    beneficiary_org_id: str,
    project_id: str | None = None,
    datastream_id: str | None = None,
    _resource: AccessDecision | None = None,
) -> AccessDecision:
    """Revalidate one exact provider-account exposure before provider use.

    `_resource` lets a caller supply the authority for the FIRST link instead of
    resolving it from a human identity -- see `resolve_scheduled_account_access`,
    which is the only caller that passes it. Everything after that link is
    unchanged and still runs: it does not depend on who is asking.
    """
    resource = _resource or resolve_strict_resource_access(
        identity,
        conn,
        project_id=project_id,
        datastream_id=datastream_id,
        minimum_capability="view",
    )
    if not resource.allowed or resource.org_id != beneficiary_org_id:
        return resource if not resource.allowed else _denied("not_found")
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT cr.owner_org_id, owner.status, h.status, s.state,
                       s.account_id, ca.available
                FROM app.connection_ref cr
                JOIN app.organizations owner ON owner.id = cr.owner_org_id
                JOIN app.credential_accounts ca
                  ON ca.credential_id = cr.id AND ca.external_account_id = %s
                LEFT JOIN app.connection_health h ON h.connection_ref_id = cr.id
                -- The scope OF THIS ACCOUNT. Joined on the credential alone, it
                -- answered with whichever account happened to be selected under
                -- that authorization -- so access to account A was decided on
                -- the verification state of account B. One row per credential
                -- hid it; migration 211 allows several, and the question was
                -- always account-shaped.
                LEFT JOIN app.connection_account_scope s
                  ON s.connection_ref_id = cr.id
                 AND s.account_id = ca.external_account_id
                WHERE cr.id = %s
                """,
                (external_account_id, credential_id),
            )
            account = cur.fetchone()
        if not isinstance(account, (tuple, list)) or len(account) < 6:
            return _denied("not_found")
        owner_org, owner_status, health, scope_state, selected_account, available = account[:6]
        if owner_status != "active":
            return _denied("credential_owner_inactive")
        # `stale` is not broken -- it is "a refresh is due", which is the normal
        # state of a Google credential one hour after consent (an access token
        # lives 60 minutes; the refresh happens at use, not on a timer). Denying
        # on it made every Google authorization unusable an hour after it was
        # created: reading its catalog, creating a Datastream from it and
        # scheduling it all answered `connection_unhealthy`, while the credential
        # was perfectly able to refresh itself on the next call. Only a health
        # that says the authorization is GONE closes the door; `unknown` (health
        # never polled) does too, because nothing has been verified.
        #
        # AI-341: `provider_denied` does NOT close it. The authorization is
        # alive -- the provider refuses the data -- and the daily probe this
        # door admits is exactly the detector of restoration: the first
        # verified `ok` pull lifts the red with no console gesture. Closing
        # here would deadlock the red behind the door that detects its repair
        # (execution-substrate.md, Decided 2026-08-31).
        if health not in ("ok", "stale", "provider_denied"):
            return _denied("connection_unhealthy")
        if scope_state != "ready" or selected_account != external_account_id or not available:
            return _denied("account_not_ready")
        if (
            resource.reason in ("owner_floor", SCHEDULED_ACTIVATION)
            and owner_org == beneficiary_org_id
        ):
            path = resource.resource_path + (
                f"credential:{credential_id}",
                f"account:{external_account_id}",
            )
            return AccessDecision(True, "owner_account_floor", "manage", beneficiary_org_id, path)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id FROM app.credential_account_grants
                WHERE credential_id = %s AND external_account_id = %s
                  AND grantee_org_id = %s AND status = 'active'
                """,
                (credential_id, external_account_id, beneficiary_org_id),
            )
            exposure = cur.fetchone()
        if not exposure:
            return _denied("account_exposure_required")
        path = resource.resource_path + (
            f"credential:{credential_id}",
            f"account:{external_account_id}",
        )
        return AccessDecision(
            True, "exact_account_exposure", resource.capability, beneficiary_org_id, path
        )
    except Exception:
        logger.exception("provider account access unavailable")
        return _denied("access_unavailable")
