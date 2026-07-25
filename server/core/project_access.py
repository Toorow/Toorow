"""toorow -- Per-identity project access control (Story 7.4, AD-5, FR12).

The single enforcement point for "does this identity reach this project?". This
turns the AD-5 addressing tree (Project -> Tool -> Auth -> Report -> Dimension)
from a promise into an enforced property: no component reads data outside its
resolved project scope.

DEFAULT-OPEN FALLBACK (single-tenant compat)
--------------------------------------------
At P3-dev the admin API uses a single shared Bearer token: any holder is "the
admin". To make the ACL real WITHOUT breaking that single-tenant deployment (or
the existing test suite), enforcement is *per-project* and *opt-in by data*:

  - A project with ZERO rows in ``app.project_members`` is OPEN: every identity
    is granted (this is the historical behaviour and keeps existing tests green).
  - A project with >= 1 membership row is CLOSED: only enrolled identities pass.

An agency deployment thus onboards project-by-project by inserting membership
rows; the moment the first row lands for a project, that project is scoped.

By default the check fails OPEN on a DB error and logs at debug. Callers
guarding governed resources pass ``fail_closed=True`` and receive a
``ProjectAccessUnavailable`` error instead. Isolation is a data property proven
by the live-Postgres suite; the resilience default keeps legacy DB-less callers
available. The hard proof lives in ``server/tests/isolation/``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Identities that are always allowed (dev / disabled-auth mode). "anonymous" is
# the subject injected by api_auth when TOOROW_AUTH_MODE=disabled.
_ALWAYS_ALLOWED = frozenset({"anonymous"})


class ProjectAccessUnavailable(RuntimeError):
    """Raised when a caller requests a fail-closed scope decision."""


_ROLE_ORDER = {"viewer": 1, "member": 2, "owner": 3}


def resolve_project_role(
    project_id: str,
    identity: str,
    conn,
    *,
    auth_mode: str | None = None,
) -> str | None:
    """Resolve an active-project role without the legacy default-open fallback."""

    mode = (auth_mode or os.environ.get("TOOROW_AUTH_MODE", "disabled")).strip().lower()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT pm.role, p.status
                FROM app.projects p
                LEFT JOIN app.project_members pm
                  ON pm.project_id = p.id AND pm.identity = %s
                WHERE p.id = %s
                """,
                (identity, project_id),
            )
            row = cur.fetchone()
    except Exception as exc:
        raise ProjectAccessUnavailable("project role could not be verified") from exc

    if row is None:
        return None  # nonexistent project: deny without disclosing existence (AC9)
    if not isinstance(row, (tuple, list)) or len(row) < 2:
        raise ProjectAccessUnavailable("project role returned an invalid result")
    role, project_status = row[:2]
    if project_status != "active":
        return None
    if mode == "disabled" and identity == "anonymous":
        return "owner"
    if role not in _ROLE_ORDER:
        return None
    return str(role)


def identity_has_project_role(
    project_id: str,
    identity: str,
    minimum_role: str,
    conn,
    *,
    auth_mode: str | None = None,
) -> bool:
    """Return whether the strict resolved role meets ``minimum_role``."""

    if minimum_role not in _ROLE_ORDER:
        raise ValueError(f"unknown project role: {minimum_role}")
    role = resolve_project_role(
        project_id,
        identity,
        conn,
        auth_mode=auth_mode,
    )
    return role is not None and _ROLE_ORDER[role] >= _ROLE_ORDER[minimum_role]


def identity_has_project_access(
    project_id: str, identity: str, conn, *, fail_closed: bool = False
) -> bool:
    """Return True if *identity* may access *project_id*.

    Args:
        project_id: The resolved project id (post project_resolver).
        identity:   The subject string from the access token (AD-14). "anonymous"
                    in disabled-auth dev mode.
        conn:       An open psycopg connection (caller owns its lifecycle).

    Returns:
        True  -- access granted (member of the project, OR the project has no
                 membership rows at all => default-open single-tenant compat, OR
                 identity is the dev "anonymous" subject).
        False -- the project HAS membership rows and this identity is not among
                 them (multi-tenant scope violation).

    By default, DB errors fail open for legacy callers. With ``fail_closed=True``,
    DB errors and invalid result shapes raise ProjectAccessUnavailable.
    """
    if identity in _ALWAYS_ALLOWED:
        return True

    try:
        with conn.cursor() as cur:
            # One round-trip: is this identity a member, and does the project have
            # ANY members at all? EXISTS on both keeps it index-only and cheap.
            cur.execute(
                """
                SELECT
                    EXISTS (
                        SELECT 1 FROM app.project_members
                        WHERE project_id = %s AND identity = %s
                    ) AS is_member,
                    EXISTS (
                        SELECT 1 FROM app.project_members
                        WHERE project_id = %s
                    ) AS project_has_members,
                    -- review-epic-7 F-3: an ARCHIVED project is closed to
                    -- everyone, default-open cannot resurrect it.
                    EXISTS (
                        SELECT 1 FROM app.projects
                        WHERE id = %s AND status = 'archived'
                    ) AS project_archived
                """,
                (project_id, identity, project_id, project_id),
            )
            row = cur.fetchone()
    except Exception as exc:  # pragma: no cover - resilience path
        if fail_closed:
            raise ProjectAccessUnavailable("project access could not be verified") from exc
        logger.debug(
            "project_access: check_failed project=%s identity=%s (fail-open): %s",
            project_id,
            identity,
            exc,
        )
        return True

    # Mocked cursor (DB-less unit test) returns a non-tuple / Mock -> fail open.
    if row is None or not isinstance(row, (tuple, list)) or len(row) < 2:
        if fail_closed:
            raise ProjectAccessUnavailable("project access returned an invalid result")
        return True

    is_member, project_has_members = bool(row[0]), bool(row[1])
    if len(row) >= 3 and bool(row[2]):
        return False  # review-epic-7 F-3: archived project is closed to everyone

    # Default-open: a project with no membership rows is reachable by anyone.
    if not project_has_members:
        return True

    # Scoped: the project is enrolled -> only members pass.
    return is_member


# ===========================================================================
# Story 21.5 -- ORG-LEVEL access resolution (Epic 21, FR37/CAP-25).
#
# The Story 7.4 pivot above is per-PROJECT (project_members). Epic 21 adds an
# ORGANIZATION layer above the project. These functions are the org-aware twin,
# following the SAME default-open-until-enrolled pattern (human decision), so the
# 21.1-21.4 stack stays green (their orgs have zero members -> open, or are
# created via the admin API whose creator is auto-enrolled -> creator passes).
#
# DEFAULT-OPEN-UNTIL-ENROLLED (per human; Epic 21 decisions 4 & 6):
#   - An org with ZERO rows in app.org_members is OPEN: any identity acts as owner
#     (single-tenant / not-yet-onboarded compat).
#   - An org with >= 1 member is CLOSED: only enrolled identities pass, with role.
#   - owner/admin see EVERYTHING in the org (downward visibility: nothing a member
#     creates is ever hidden UPWARD from an owner/admin).
#   - member/viewer with ZERO project grants -> see ALL org projects (default).
#     The first resource_grants row (scope_type='project') for that (org, identity)
#     engages the allow-list: from then on they see ONLY granted projects.
#   - The dev/disabled-auth subject "anonymous" is ALWAYS allowed (owner), exactly
#     like the per-project functions above.
#
# RLS (2nd barrier) is Story 21.6; scoping the READ endpoints is a follow-up.
# ===========================================================================

_ORG_MANAGE_ROLES = frozenset({"owner", "admin"})


def resolve_org_role(
    org_id: str,
    identity: str,
    conn,
    *,
    auth_mode: str | None = None,
) -> str | None:
    """Resolve *identity*'s role in *org_id* under default-open-until-enrolled.

    Mirrors the Story 7.4 per-project pivot, transposed to the org layer:
      - disabled-auth + "anonymous" -> "owner" (dev / legacy short-circuit; this
        returns BEFORE any DB query, like identity_has_project_access).
      - org row missing -> None.
      - org status != 'active' (archived) -> None (closed to all).
      - org has ZERO members -> "owner" (DEFAULT-OPEN: unenrolled org is open, the
        caller acts as owner).
      - otherwise -> the enrolled role (may be None -> DENIED, default-closed).

    Uses ONE round trip (status + has-members + this identity's role).
    """
    mode = (auth_mode or os.environ.get("TOOROW_AUTH_MODE", "disabled")).strip().lower()

    # Disabled-auth dev/legacy: "anonymous" is owner. Short-circuit BEFORE any DB
    # access so a DB-less caller (mock conn) never touches the cursor.
    if mode == "disabled" and identity in _ALWAYS_ALLOWED:
        return "owner"

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                o.status,
                EXISTS (
                    SELECT 1 FROM app.org_members
                    WHERE org_id = %s AND status = 'active'
                ) AS has_active_members,
                (
                    SELECT role FROM app.org_members
                    WHERE org_id = %s AND identity = %s AND status = 'active'
                ) AS role
            FROM app.organizations o
            WHERE o.id = %s
            """,
            (org_id, org_id, identity, org_id),
        )
        row = cur.fetchone()

    if row is None:
        return None
    status, has_active_members, role = row[0], bool(row[1]), row[2]
    if status != "active":
        return None  # archived org is closed to everyone, default-open cannot open it
    # review-21.5 F-HIGH: only ACTIVE members count. A suspended/invited member
    # resolves to role NULL (no manage rights); an org with zero ACTIVE members is
    # OPEN (7.4 parity -- reopen-on-empty is a documented product decision, and the
    # "cannot suspend/remove the last owner" guard belongs to the delete/downgrade
    # paths built later).
    if not has_active_members:
        return "owner"  # DEFAULT-OPEN: no active member -> caller acts as owner
    return str(role) if role is not None else None


def identity_can_manage_org(org_id: str, identity: str, conn) -> bool:
    """Return True if *identity* is an owner/admin of *org_id* (manage capability).

    Manage = mutate the org (patch, add members, expose credentials, link flux).
    Under default-open-until-enrolled an unenrolled org resolves to "owner" -> True,
    which is what keeps the 21.1-21.4 mutation tests green.
    """
    return resolve_org_role(org_id, identity, conn) in _ORG_MANAGE_ROLES


def identity_has_org_access(org_id: str, identity: str, conn) -> bool:
    """Return True if *identity* may READ the org's resources (any role).

    Story 21.5 follow-up (reads scoping): an identity can see an org it belongs to
    (any active role), an org with zero active members (open), or when the dev
    disabled-auth "anonymous" subject is used. A non-member of an enrolled org is
    denied -> read endpoints return 404 (existence not disclosed, 7.4 pattern).
    """
    return resolve_org_role(org_id, identity, conn) is not None


def identity_can_access_project_in_org(project_id: str, identity: str, conn) -> bool:
    """Return True if *identity* may reach *project_id* under Epic 21 resolution.

    Algorithm (architecture-org-tenancy s5; Epic 21 decisions 4 & 6):
      1. Resolve the project's org_id. Missing project -> False.
      2. org_id IS NULL (legacy, no-org project) -> FALL BACK to the Story 7.4
         per-project default-open (identity_has_project_access), preserving legacy
         behaviour untouched.
      3. role = resolve_org_role(org_id, identity). None -> False (default-closed).
      4. role in {owner, admin} -> True (downward visibility: sees ALL org projects).
      5. member/viewer -> default-open WITHIN the org UNLESS the identity holds >= 1
         project grant; then the allow-list engages (only granted projects pass).
    """
    with conn.cursor() as cur:
        cur.execute("SELECT org_id FROM app.projects WHERE id = %s", (project_id,))
        row = cur.fetchone()

    if row is None:
        return False  # unknown project
    org_id = row[0]
    if org_id is None:
        # Legacy no-org project: keep Story 7.4 per-project default-open behaviour.
        return identity_has_project_access(project_id, identity, conn)

    role = resolve_org_role(org_id, identity, conn)
    if role is None:
        return False  # default-closed: non-member of an enrolled org
    if role in _ORG_MANAGE_ROLES:
        return True  # owner/admin see everything in the org

    # member/viewer: default-open within the org, allow-list from the 1st grant.
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                EXISTS (
                    SELECT 1 FROM app.resource_grants
                    WHERE org_id = %s AND identity = %s AND scope_type = 'project'
                ) AS has_project_grants,
                EXISTS (
                    SELECT 1 FROM app.resource_grants
                    WHERE org_id = %s AND identity = %s
                      AND scope_type = 'project' AND scope_id = %s
                ) AS grants_this_project
            """,
            (org_id, identity, org_id, identity, project_id),
        )
        grow = cur.fetchone()

    has_project_grants = bool(grow[0]) if grow is not None else False
    grants_this_project = bool(grow[1]) if grow is not None else False
    if not has_project_grants:
        return True  # default: a member sees ALL projects of the org
    return grants_this_project  # allow-list engaged: only granted projects pass


# Story 36.1 -- strict AD-5 production access. Legacy helpers above intentionally
# keep their brownfield semantics; every Epic 36 surface must call this seam.
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


def epic36_production_access_enabled(*, auth_mode: str | None = None) -> bool:
    """Return whether the default-off Epic 36 production gate can open."""
    mode = (auth_mode or os.environ.get("TOOROW_AUTH_MODE", "disabled")).strip().lower()
    enabled = os.environ.get("TOOROW_EPIC36_PRODUCTION_ENABLED", "false").strip().lower()
    return enabled in {"1", "true", "yes"} and mode != "disabled"


def resolve_strict_resource_access(
    identity: str,
    conn,
    *,
    project_id: str | None = None,
    datastream_id: str | None = None,
    minimum_capability: str = "view",
    auth_mode: str | None = None,
) -> AccessDecision:
    """Resolve one project/Datastream path with no default-open fallback."""
    if minimum_capability not in _CAPABILITY_ORDER:
        raise ValueError(f"unknown capability: {minimum_capability}")
    mode = (auth_mode or os.environ.get("TOOROW_AUTH_MODE", "disabled")).strip().lower()
    if not identity or identity == "anonymous" or mode == "disabled":
        return _denied("production_identity_required")
    if bool(project_id) == bool(datastream_id):
        return _denied("invalid_resource")

    try:
        with conn.cursor() as cur:
            if project_id:
                cur.execute(
                    """
                    SELECT p.org_id, o.status, m.role, m.status
                    FROM app.projects p
                    JOIN app.organizations o ON o.id = p.org_id
                    LEFT JOIN app.org_members m
                      ON m.org_id = p.org_id AND m.identity = %s
                    WHERE p.id = %s AND p.status = 'active'
                    """,
                    (identity, project_id),
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
        if org_status != "active" or member_status != "active" or role not in {
            "owner", "admin", "member", "viewer"
        }:
            return _denied("not_found")
        path = (f"organization:{org_id}", f"{scope_type}:{scope_id}")
        if role == "owner":
            return AccessDecision(True, "owner_floor", "manage", str(org_id), path)

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT capability FROM app.resource_grants
                WHERE org_id = %s AND identity = %s
                  AND scope_type = %s AND scope_id = %s
                """,
                (org_id, identity, scope_type, scope_id),
            )
            grant = cur.fetchone()
        if not isinstance(grant, (tuple, list)) or not grant:
            return _denied("grant_required")
        capability = str(grant[0])
        role_cap = {"viewer": "view", "member": "edit", "admin": "manage"}[str(role)]
        effective_rank = min(
            _CAPABILITY_ORDER.get(capability, 0), _CAPABILITY_ORDER[role_cap]
        )
        if effective_rank < _CAPABILITY_ORDER[minimum_capability]:
            return _denied("insufficient_capability")
        effective = next(k for k, v in _CAPABILITY_ORDER.items() if v == effective_rank)
        return AccessDecision(True, "explicit_grant", effective, str(org_id), path)
    except Exception:
        logger.exception("strict resource access unavailable")
        return _denied("access_unavailable")


def resolve_provider_account_access(
    identity: str,
    conn,
    *,
    credential_id: str,
    external_account_id: str,
    beneficiary_org_id: str,
    project_id: str | None = None,
    datastream_id: str | None = None,
) -> AccessDecision:
    """Revalidate one exact provider-account exposure before provider use."""
    resource = resolve_strict_resource_access(
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
                LEFT JOIN app.connection_account_scope s ON s.connection_ref_id = cr.id
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
        if health != "ok":
            return _denied("connection_unhealthy")
        if scope_state != "ready" or selected_account != external_account_id or not available:
            return _denied("account_not_ready")
        if resource.reason == "owner_floor" and owner_org == beneficiary_org_id:
            path = resource.resource_path + (
                f"credential:{credential_id}", f"account:{external_account_id}"
            )
            return AccessDecision(
                True, "owner_account_floor", "manage", beneficiary_org_id, path
            )
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
            f"credential:{credential_id}", f"account:{external_account_id}"
        )
        return AccessDecision(
            True, "exact_account_exposure", resource.capability, beneficiary_org_id, path
        )
    except Exception:
        logger.exception("provider account access unavailable")
        return _denied("access_unavailable")
