"""Qui appartient a une organisation, avec quel role, et ce que ce role ouvre.

AD-43, 2026-08-12. Cinq routes : lister les membres, en ajouter un, en retirer
un, changer son role ou son etat, et lire les autorisations qui en decoulent.
Le plancher du dernier proprietaire n est PAS ici -- il vit dans
`core.org_lifecycle`, parce que c est une regle, pas une porte.
"""

from __future__ import annotations

import json
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core.audit import (
    ACTION_CROSS_SCOPE_ATTEMPT,
    declare_action,
    write_audit_row,
)
from core.org_lifecycle import (
    _would_demote_last_owner,
    _would_orphan_last_owner,
)
from core.organizations_api import _org_row_to_dict

logger = logging.getLogger("core.admin_api")

# --- le joint qui reste dans admin_api -----------------------------------
async def _check_auth(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import."""
    from core.admin_api import _check_auth as _impl  # noqa: PLC0415
    return await _impl(*args, **kwargs)

def _enforce_org_manage(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import."""
    from core.admin_api import _enforce_org_manage as _impl  # noqa: PLC0415
    return _impl(*args, **kwargs)

def _serialize_authorization(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import."""
    from core.me_api import _serialize_authorization as _impl  # noqa: PLC0415
    return _impl(*args, **kwargs)

# --- les handlers, deplaces TELS QUELS -----------------------------------

# `org_member_added` is NOT declared here any more. Nothing writes it: direct
# enrolment is refused (see `_add_org_member`), and the invitation path writes
# its own actions. A declaration with no writer is exactly the dead vocabulary
# `test_audit_actions_are_declared_by_their_writer.py` was written against.

ACTION_ORG_MEMBER_REMOVED = declare_action("org_member_removed")

ACTION_ORG_MEMBER_UPDATED = declare_action("org_member_updated")

_AUTHORIZATION_QUERY = """
    SELECT
        r.id,
        r.provider,
        r.nango_connection_id,
        r.project_id,
        r.created_at,
        r.status,
        r.auth_path,
        r.owner_org_id,
        r.owner_identity,
        r.token_expiry,
        o.name AS owner_org_name,
        up.display_name AS owner_display_name,
        up.email AS owner_email,
        s.account_label,
        s.state AS account_state,
        (
            SELECT count(*) FROM app.connection_account_scope sc
            WHERE sc.connection_ref_id = r.id AND sc.account_id IS NOT NULL
        ) AS selected_account_count,
        h.status AS health_status,
        h.last_checked_at,
        h.last_fetched_at,
        EXISTS (
            SELECT 1 FROM app.credential_account_grants g
            WHERE g.credential_id = r.id AND g.status = 'active'
        ) AS has_outgoing_grant,
        EXISTS (
            SELECT 1 FROM app.org_members m
            WHERE m.org_id = r.owner_org_id
              AND m.identity = %(identity)s
              AND m.status = 'active'
              AND m.role IN ('owner', 'admin')
        ) AS caller_manages_owner
    FROM app.connection_ref r
    LEFT JOIN app.organizations o ON o.id = r.owner_org_id
    LEFT JOIN app.user_profiles up ON up.identity = r.owner_identity
    -- LATERAL, not a plain join: since migration 211 an authorization holds one
    -- scope row per selected account, and a flat join listed the same
    -- authorization once per account. `account_label`/`account_state` describe
    -- the most recently verified one; `selected_account_count` says how many
    -- there are, so a surface can stop implying there is only ever one.
    LEFT JOIN LATERAL (
        SELECT sc.account_label, sc.state
        FROM app.connection_account_scope sc
        WHERE sc.connection_ref_id = r.id
        ORDER BY (sc.state = 'ready') DESC, sc.verified_at DESC NULLS LAST
        LIMIT 1
    ) s ON TRUE
    LEFT JOIN app.connection_health h ON h.connection_ref_id = r.id
    WHERE {scope}
    ORDER BY r.created_at DESC
"""

_ORG_MEMBER_STATUSES = frozenset({"invited", "active", "suspended"})

_ORG_ROLES = frozenset({"owner", "admin", "member", "viewer"})

# Story 21.5 security follow-up: strict role hierarchy (owner > admin > member >
# viewer). Used to (a) forbid self-escalation / minting a role above the actor's
# own (FIX 3) and (b) identify "managers" (owner|admin) that keep an enrolled org
# from silently reopening (FIX 1b).
_ORG_ROLE_RANK = {"viewer": 1, "member": 2, "admin": 3, "owner": 4}

async def _list_org_members(request: Request) -> Response:
    """GET /api/organizations/{org_id}/members -- members of an org (21.8 AC4).

    Same read-scoping as _get_org: a non-member of an enrolled org gets 404 so
    existence is never disclosed (7.4 pattern). The caller's *own* membership
    status gates the route; suspended members are still visible in the list
    (they are members with status='suspended').
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    org_id = request.path_params["org_id"]
    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.project_access import identity_has_org_access  # noqa: PLC0415

        with get_connection() as conn:
            if not identity_has_org_access(org_id, identity or "anonymous", conn):
                return JSONResponse(
                    {"code": "not_found", "message": "organization not found"},
                    status_code=404,
                )
            with conn.cursor() as cur:
                # UNE LISTE DE MEMBRES DOIT NOMMER DES GENS.
                #
                # `identity` est un identifiant canonique (`person_01K…`) depuis la
                # migration 111 : il est parfait pour muter une ligne, et illisible
                # pour décider laquelle. L'écran ne pouvait donc afficher que des
                # ULID, et personne — Jean compris — ne pouvait dire quelle ligne
                # était qui. L'e-mail vérifié EXISTE en clair
                # (`app.person_identities.verified_email`, posé à la première
                # connexion) : le read-model le résout ici, une fois, plutôt que de
                # laisser chaque écran inventer sa jointure.
                #
                # `LEFT JOIN LATERAL` et non un JOIN : une identité héritée
                # d'avant la 111 est un e-mail brut sans ligne dans
                # `person_identities`, et un JOIN ordinaire la ferait DISPARAÎTRE
                # de la liste des membres. Perdre un membre pour cause de nom
                # manquant serait pire que de l'afficher sans nom.
                cur.execute(
                    "SELECT m.id, m.org_id, m.identity, m.role, m.status, "
                    "m.invited_by, m.invited_at, m.joined_at, m.created_at, "
                    "pi.verified_email "
                    "FROM app.org_members m "
                    "LEFT JOIN LATERAL ("
                    "    SELECT verified_email FROM app.person_identities"
                    "     WHERE person_id = m.identity AND verified_email IS NOT NULL"
                    "     ORDER BY verified_email_at DESC NULLS LAST LIMIT 1"
                    ") pi ON TRUE "
                    "WHERE m.org_id = %s ORDER BY m.created_at ASC",
                    (org_id,),
                )
                cols = [d[0] for d in cur.description]
                members = [_org_row_to_dict(cols, r) for r in cur.fetchall()]
    except Exception as exc:
        logger.error("admin_api: list_org_members db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )
    return JSONResponse({"members": members}, status_code=200)

async def _add_org_member(request: Request) -> Response:
    """POST /api/organizations/{org_id}/members -- always refuses (409).

    THIS ROUTE NO LONGER ENROLLS ANYONE, and keeping it is the point: a caller
    that still posts here must be told which gesture replaces it, not answered
    404 as if the surface had never existed.

    Direct enrolment wrote a caller-supplied string straight into
    `app.org_members.identity`. Under canonical identity that column holds a
    `person_<ULID>` minted by `resolve_canonical_identity` from a VERIFIED
    (issuer, subject) pair -- so the route could only ever insert a key that
    authorizes nobody, or, worse, a hand-typed one colliding with a real person.
    Membership is created by accepting an invitation; that path binds the person
    the token proves. Until 2026-08-24 this refusal was conditional on an
    environment flag that defaulted to OFF, so an environment that merely never
    set it kept the writing path open. See
    `tests/conformance/test_no_identity_flag_returns.py`.
    """
    authorized, _identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    return JSONResponse(
        {
            "code": "invitation_required",
            "message": "New members must accept an organization invitation.",
        },
        status_code=409,
    )

def _cut_sessions_of(target: str, *, actor: str, reason: str) -> int:
    """Cut the browser sessions of a person whose membership just ended.

    WHY THIS EXISTS. `reviews/audit-2026-08-17/12-acces-utilisateurs.md:286-290`:
    the role revokes but the session does not. Removing or suspending a member
    closed their DB guards and left the sealed ticket serving every read that
    does not touch `org_members` -- for up to eight hours.

    WHY IT CUTS THE PERSON, NOT THE ORG. The browser ticket is bound to a person
    (issuer, subject), never to an organization; there is no org-scoped session
    to cut, so a member who also belongs to another org is signed out of that
    one too. That is the safe direction and it is deliberate: they sign in again
    and get a ticket carrying their now-correct, reduced access. The alternative
    -- leaving the ticket alive -- is the defect.

    Never raises: the membership change already happened and is the caller's
    answer. The count is returned so the audit row records what was actually
    cut rather than what was intended.
    """
    from core.session_revocation import (  # noqa: PLC0415
        revocation_enabled,
        revoke_sessions_for_identity,
    )

    if not revocation_enabled():
        return 0
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            revoked = revoke_sessions_for_identity(
                conn,
                identity=target,
                revoked_by=actor or "anonymous",
                reason=reason,
            )
            conn.commit()
            return len(revoked)
    except Exception:  # noqa: BLE001
        logger.exception("org_member_session_revocation_failed target=%s", target)
        return 0

async def _remove_org_member(request: Request) -> Response:
    """DELETE /api/organizations/{org_id}/members/{identity} -- remove a member.

    Manage-gated (owner/admin). 404 if not a member. 409 if it would remove the
    org's last active owner (would silently reopen the tenant).
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    org_id = request.path_params["org_id"]
    target = request.path_params["identity"]
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            denied = _enforce_org_manage(org_id, identity, conn, "remove_member")
            if denied is not None:
                return denied
            with conn.cursor() as cur:
                # AI-348 (organization-settings.md, 2026-09-02): the last
                # active OWNER is refused before the manager floor even looks --
                # an org kept managed by an admin who can never mint an owner
                # again is a dead end, not a managed org.
                if _would_demote_last_owner(cur, org_id, target, new_role=None, new_status=None):
                    return JSONResponse(
                        {
                            "code": "last_owner_transfer_required",
                            "message": (
                                "cannot demote or remove the last owner. Promote "
                                "another active member to owner first, then retry."
                            ),
                        },
                        status_code=409,
                    )
                if _would_orphan_last_owner(cur, org_id, target, new_role=None, new_status=None):
                    return JSONResponse(
                        {
                            "code": "conflict",
                            "message": "cannot remove the last active owner",
                        },
                        status_code=409,
                    )
                cur.execute(
                    "DELETE FROM app.org_members WHERE org_id = %s AND identity = %s",
                    (org_id, target),
                )
                deleted = cur.rowcount
            conn.commit()
    except Exception as exc:
        logger.error("admin_api: remove_org_member db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )
    if not deleted:
        return JSONResponse({"code": "not_found", "message": "member not found"}, status_code=404)
    sessions_cut = _cut_sessions_of(
        target, actor=identity or "anonymous", reason="removed from organization"
    )
    write_audit_row(
        identity=identity or "anonymous",
        action=ACTION_ORG_MEMBER_REMOVED,
        provider_account="",
        connection_ref="",
        metadata={
            "org_id": org_id,
            "member_identity": target,
            "sessions_revoked": sessions_cut,
        },
    )
    return JSONResponse({"removed": True}, status_code=200)

async def _update_org_member(request: Request) -> Response:
    """PATCH /api/organizations/{org_id}/members/{identity} -- change role/status.

    Manage-gated. 404 if not a member. 409 if the change would drop the org's last
    active owner (downgrade or suspend).
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    org_id = request.path_params["org_id"]
    target = request.path_params["identity"]
    try:
        body: dict = json.loads(await request.body())
    except Exception as exc:
        return JSONResponse(
            {"code": "invalid_body", "message": f"Invalid JSON body: {exc}"},
            status_code=400,
        )
    updates: dict = {}
    if "role" in body:
        role = (body.get("role") or "").strip().lower()
        if role not in _ORG_ROLES:
            return JSONResponse(
                {"code": "invalid_input", "message": f"invalid role: {role!r}"},
                status_code=422,
            )
        updates["role"] = role
    if "status" in body:
        status = (body.get("status") or "").strip().lower()
        if status not in _ORG_MEMBER_STATUSES:
            return JSONResponse(
                {"code": "invalid_input", "message": f"invalid status: {status!r}"},
                status_code=422,
            )
        updates["status"] = status
    if not updates:
        return JSONResponse(
            {"code": "invalid_input", "message": "no updatable fields (role|status)"},
            status_code=422,
        )

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            denied = _enforce_org_manage(org_id, identity, conn, "update_member")
            if denied is not None:
                return denied
            # FIX 3: when changing role, forbid assigning above the actor's own rank
            # and forbid the actor promoting itself (self-escalation to owner).
            if "role" in updates:
                denied = _enforce_role_assignment(
                    org_id, identity, target, updates["role"], conn, "update_member"
                )
                if denied is not None:
                    return denied
            with conn.cursor() as cur:
                # AI-348: same refusal, same lock, on the update door.
                if _would_demote_last_owner(
                    cur,
                    org_id,
                    target,
                    new_role=updates.get("role"),
                    new_status=updates.get("status"),
                ):
                    return JSONResponse(
                        {
                            "code": "last_owner_transfer_required",
                            "message": (
                                "cannot demote or remove the last owner. Promote "
                                "another active member to owner first, then retry."
                            ),
                        },
                        status_code=409,
                    )
                if _would_orphan_last_owner(
                    cur,
                    org_id,
                    target,
                    new_role=updates.get("role"),
                    new_status=updates.get("status"),
                ):
                    return JSONResponse(
                        {
                            "code": "conflict",
                            "message": "cannot drop the last active owner",
                        },
                        status_code=409,
                    )
                set_parts = [f"{col} = %s" for col in updates]
                params = list(updates.values()) + [org_id, target]
                cur.execute(
                    "UPDATE app.org_members SET "
                    + ", ".join(set_parts)
                    + " WHERE org_id = %s AND identity = %s "
                    + "RETURNING id, org_id, identity, role, status, "
                    + "invited_by, invited_at, joined_at, created_at",
                    params,
                )
                row = cur.fetchone()
                if row is None:
                    return JSONResponse(
                        {"code": "not_found", "message": "member not found"},
                        status_code=404,
                    )
                cols = [d[0] for d in cur.description]
                member = _org_row_to_dict(cols, row)
            conn.commit()
    except Exception as exc:
        logger.error("admin_api: update_org_member db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )
    # Suspension is the case the audit named explicitly: a suspended member kept
    # a live session. A role CHANGE is not cut -- the ticket carries no role, so
    # the next request already reads the new one from `org_members`.
    sessions_cut = 0
    if updates.get("status") == "suspended":
        sessions_cut = _cut_sessions_of(
            target, actor=identity or "anonymous", reason="suspended in organization"
        )
    write_audit_row(
        identity=identity or "anonymous",
        action=ACTION_ORG_MEMBER_UPDATED,
        provider_account="",
        connection_ref="",
        metadata={
            "org_id": org_id,
            "member_identity": target,
            "fields": sorted(updates),
            "sessions_revoked": sessions_cut,
        },
    )
    return JSONResponse(member, status_code=200)

async def _list_org_authorizations(request: Request) -> Response:
    """GET /api/organizations/{org_id}/authorizations -- the org's credentials.

    Story 42.9, organization level. Lists what the organization owns (whoever
    plugged it in, named) plus what another owner exposes to it. Restricted to
    owner/admin: this is the administration view, and it names people.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    subject = identity or "anonymous"
    org_id = request.path_params.get("org_id", "")
    if not org_id:
        return JSONResponse(
            {"code": "missing_id", "message": "org_id is required"}, status_code=400
        )

    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.project_access import identity_can_manage_org  # noqa: PLC0415

        with get_connection() as conn:
            if not identity_can_manage_org(org_id, subject, conn):
                return JSONResponse(
                    {"code": "not_found", "message": "Organization not found"},
                    status_code=404,
                )
            with conn.cursor() as cur:
                cur.execute(
                    _AUTHORIZATION_QUERY.format(
                        scope="""
                        r.owner_org_id = %(org_id)s
                        OR EXISTS (
                            SELECT 1 FROM app.credential_account_grants g
                            WHERE g.credential_id = r.id
                              AND g.status = 'active'
                              AND g.grantee_org_id = %(org_id)s
                        )
                        """
                    ),
                    {"identity": subject, "org_id": org_id},
                )
                cols = [desc[0] for desc in cur.description]
                rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as exc:
        logger.error("admin_api: list_org_authorizations db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Authorizations are temporarily unavailable"},
            status_code=503,
        )

    return JSONResponse(
        {
            "authorizations": [
                # Owned by another org == reached through a grant: the person who
                # plugged it in is not disclosed, and no action is offered.
                _serialize_authorization(r, subject, r.get("owner_org_id") != org_id)
                for r in rows
            ]
        }
    )

def _enforce_role_assignment(
    org_id: str,
    actor_identity: str,
    target_identity: str,
    assigned_role: str,
    conn,
    operation: str,
) -> Response | None:
    """Story 21.5 security follow-up (FIX 3): forbid role self-escalation.

    The actor already passed the manage gate (owner|admin). This adds the strict
    hierarchy rule (owner > admin > member > viewer):
      - the actor may NOT assign a role strictly HIGHER than its own resolved active
        role (an admin cannot mint/promote an owner);
      - the actor may NOT raise its OWN role (no self-promotion, even to an equal-or-
        higher rank than it currently holds).
    An owner (top rank) can assign any role to others. Returns a 403 Response on
    refusal (audited via ACTION_CROSS_SCOPE_ATTEMPT), or None when allowed.
    """
    from core.project_access import resolve_org_role  # noqa: PLC0415

    actor_role = resolve_org_role(org_id, actor_identity or "anonymous", conn)
    actor_rank = _ORG_ROLE_RANK.get(actor_role or "", 0)
    assigned_rank = _ORG_ROLE_RANK.get(assigned_role, 0)

    # Deny assigning a role strictly above the actor's own rank. This single rule
    # covers both threats: an admin minting/promoting an OWNER (target is someone
    # else) AND an actor promoting ITSELF (self-promotion is by definition a jump to
    # a rank above the actor's current one). An owner (top rank) is never blocked,
    # and any assignment at or below the actor's rank is allowed.
    if assigned_rank <= actor_rank:
        return None

    write_audit_row(
        identity=actor_identity or "anonymous",
        action=ACTION_CROSS_SCOPE_ATTEMPT,
        provider_account="",
        connection_ref="",
        metadata={
            "org_id": org_id,
            "operation": operation,
            "reason": "role_escalation",
            "actor_role": actor_role,
            "assigned_role": assigned_role,
            "target_identity": target_identity,
        },
    )
    logger.warning(
        "admin_api: org_role_escalation_denied identity=%s org=%s op=%s "
        "actor_role=%s assigned_role=%s",
        actor_identity,
        org_id,
        operation,
        actor_role,
        assigned_role,
    )
    return JSONResponse(
        {
            "code": "forbidden",
            "message": ("Acces refuse : vous ne pouvez pas attribuer un role superieur au votre."),
        },
        status_code=403,
    )

def _owner_display_name(ref: dict, owner_identity) -> str | None:
    """WHO connected this credential, in a person's words -- never a row key.

    The fallback used to end on the raw identity, so the Credentials screen
    printed `person_01KYHQX4RK24Z6QJCYWX6DYVSQ` and `anonymous` in a column
    headed "Connected by". Measured 2026-08-11 on the served console. A ULID is
    the vocabulary of the database (CLAUDE.md: "vocabulaire de l'utilisateur,
    pas celui de la base"), and it identifies nobody: `app.persons` carries id
    and timestamps only, so there is no directory to look it up in.

    Two absences, two sentences, because they are not the same fact:
      * `anonymous` -- the consent predates the identity being carried at all,
        so no person can be named and the row says so;
      * a person with no `user_profiles` row -- somebody real, whose name this
        deployment has never been told.
    """
    named = ref.get("owner_display_name") or ref.get("owner_email")
    if named:
        return str(named)
    if not owner_identity:
        return None
    if str(owner_identity) == "anonymous":
        return "Connected before sign-in recorded a person"
    return "A member of this organization"


# --- LES ROUTES, dans l'ordre exact ou elles etaient declarees ------------
#
# Starlette resout dans l'ordre de declaration ; chaque collection est epissee
# a la position que ses routes occupaient. La preuve est un dump avant/apres.

ORG_MEMBERS_ROUTES_1 = [
    # Story 21.8 AC4: read side of org membership (added after 21.5).
    Route(
        "/api/organizations/{org_id}/members",
        endpoint=_list_org_members,
        methods=["GET"],
    ),
    Route(
        "/api/organizations/{org_id}/members",
        endpoint=_add_org_member,
        methods=["POST"],
    ),
    # Story 21.5 follow-up: manage an existing member (remove / change role|status).
    Route(
        "/api/organizations/{org_id}/members/{identity}",
        endpoint=_remove_org_member,
        methods=["DELETE"],
    ),
    Route(
        "/api/organizations/{org_id}/members/{identity}",
        endpoint=_update_org_member,
        methods=["PATCH"],
    ),
    # Story 42.9: the organization-level authorization view. Declared before the
    # bare {org_id} routes, per the sub-resource ordering rule used below.
    Route(
        "/api/organizations/{org_id}/authorizations",
        endpoint=_list_org_authorizations,
        methods=["GET"],
    ),
]
