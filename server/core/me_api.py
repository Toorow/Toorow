"""Mon compte : mon profil, mes autorisations, et son effacement.

AD-43, 2026-08-13. Cinq routes qui ne parlent que de l appelant lui-meme -- pas
d un projet, pas d une organisation. `_delete_me` (226 lignes, quatre requetes)
est l un des quatre handlers qui SONT de la logique metier ; il est ici, seul
visible, ce qui est la moitie du travail avant de le reparer.
"""

from __future__ import annotations

import json
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core.audit import declare_action
from core.org_lifecycle import (
    _account_deletion_facts,
    _erase_org_transactional,
)
from core.org_members_api import (
    # Deux noms encore lus par le reste d'admin_api ; leur proprietaire est le
    # sujet, et ce module l'importe deja pour ses routes -- pas de cycle.
    _AUTHORIZATION_QUERY,
    _owner_display_name,
)
from core.row_json import json_scalar

logger = logging.getLogger("core.admin_api")

# --- le joint qui reste dans admin_api -----------------------------------
async def _check_auth(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import."""
    from core.admin_api import _check_auth as _impl  # noqa: PLC0415
    return await _impl(*args, **kwargs)

# --- les handlers, deplaces TELS QUELS -----------------------------------

# --- LES ACTIONS QUE CE MODULE ECRIT ------------------------------------
#
# AD-42 (2026-08-12) : declarees ICI, a cote du code qui les ecrit, et non
# dans `core/audit.py`. Ce fichier etait un carrefour -- 43 editions de 29
# sujets depuis juin, dont 34 n'ajoutaient qu'une constante -- et 45 % des
# actions reellement ecrites en production n'y etaient meme pas declarees,
# parce que la liste etait trop loin pour valoir le detour. `write_audit_row`
# refuse desormais une action que personne n'a declaree.
ACTION_ACCOUNT_ERASED = declare_action("account_erased")

async def _get_my_profile(request: Request) -> Response:
    """GET /api/me/profile -- the caller's own profile (AC3)."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    ident = identity or "anonymous"
    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.user_profiles import fetch_user_profile  # noqa: PLC0415

        with get_connection() as conn:
            profile = fetch_user_profile(ident, conn)
            # Resolved on the SAME connection as the profile, and from the
            # identity alone -- see the comment below.
            from core.super_admin import identity_is_super_admin  # noqa: PLC0415

            is_platform_admin = identity_is_super_admin(ident, conn=conn)
    except Exception as exc:
        logger.error("admin_api: get_my_profile db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )
    # Whether this caller may create an organization, answered on the ONE
    # endpoint that already says who the caller is.
    #
    # The server has gated organization creation on the super-admin allow-list
    # since epic 34 (`:7141`, `:7151`), and the console knew nothing about it:
    # `grep -rn "super_admin" ui/admin/src` returned nothing outside tests. So an
    # operator who may add an organization had no way to learn that they may, and
    # no control to do it — `/create-org` is routed
    # (`ui/admin/src/App.tsx#DirectCreateOrgEntry`) and reachable only by typing
    # the address.
    #
    # Computed by `identity_is_super_admin`, the SAME resolution every gate
    # calls, and from the AUTH SUBJECT alone. A screen that decided this any
    # other way could show a button the server then refuses, which is the
    # failure mode this repository keeps finding. It stays advisory: the gate is
    # the authority, this is only what the screen is allowed to offer.
    #
    # It used to read `profile.get("email")` first -- the profile email is
    # self-declared and PATCHable by the person themselves (`_patch_my_profile`
    # below), so an authority signal was reading a field its own subject writes
    # (audit 12, P1-2). That path is gone: the email now comes from
    # `app.person_identities`, where only a verified sign-in puts it.
    profile["is_super_admin"] = is_platform_admin
    return JSONResponse(profile, status_code=200)

async def _patch_my_profile(request: Request) -> Response:
    """PATCH /api/me/profile -- upsert the caller's own profile (AC3).

    Body: {"display_name"?, "email"?, "avatar_url"?, "avatar_source"?}. The
    identity is ALWAYS the auth subject -- a body-supplied identity is ignored,
    so a user can only ever write their own profile.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    ident = identity or "anonymous"
    try:
        body: dict = json.loads(await request.body())
    except Exception as exc:
        return JSONResponse(
            {"code": "invalid_body", "message": f"Invalid JSON body: {exc}"},
            status_code=400,
        )

    def _opt_str(key: str, maxlen: int) -> tuple[str | None, Response | None]:
        if key not in body:
            return None, None
        raw = body.get(key)
        val = str(raw).strip() if raw else None
        if val is not None and len(val) > maxlen:
            return None, JSONResponse(
                {"code": "invalid_input", "message": f"{key} too long (max {maxlen})"},
                status_code=422,
            )
        return val, None

    display_name, e1 = _opt_str("display_name", 255)
    if e1:
        return e1
    email, e2 = _opt_str("email", 320)
    if e2:
        return e2
    avatar_url, e3 = _opt_str("avatar_url", 2048)
    if e3:
        return e3
    avatar_source, e4 = _opt_str("avatar_source", 64)
    if e4:
        return e4
    # Default source to 'self' when an avatar is set without an explicit source.
    if avatar_url and "avatar_source" not in body:
        avatar_source = "self"

    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.user_profiles import upsert_user_profile  # noqa: PLC0415

        with get_connection() as conn:
            profile = upsert_user_profile(
                ident,
                conn,
                display_name=display_name,
                email=email,
                avatar_url=avatar_url,
                avatar_source=avatar_source,
                clear_fields=frozenset(
                    key
                    for key in ("display_name", "email", "avatar_url", "avatar_source")
                    if key in body and body.get(key) is None
                ),
            )
            conn.commit()
    except Exception as exc:
        logger.error("admin_api: patch_my_profile db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )
    return JSONResponse(profile, status_code=200)

async def _get_my_deletion_preview(request: Request) -> Response:
    """GET /api/me/deletion-preview -- what erasing this account would do.

    Read-only, no side effect. The caller can only ever preview THEIR own
    account: the identity comes from the auth subject, never from the request.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    ident = identity or ""
    if not ident:
        return JSONResponse(
            {
                "code": "identity_required",
                "message": "An authenticated identity is required to preview an erasure.",
            },
            status_code=403,
        )

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            facts = _account_deletion_facts(conn, ident)
            conn.rollback()
    except Exception:
        logger.exception("admin_api: my_deletion_preview db_error")
        return JSONResponse(
            {"code": "db_error", "message": "database operation failed"},
            status_code=500,
        )
    return JSONResponse(facts, status_code=200)

async def _delete_me(request: Request) -> Response:
    """DELETE /api/me -- erase the caller's account (RGPD right to erasure).

    Requires header ``X-Confirm-Delete: erase-account`` (422 otherwise).
    409 when the caller is the last owner of an org that still has other active
    members -- the message says what to do instead.

    Sequencing: each org that leaves with the account is erased in its OWN
    transaction (``_erase_org_transactional`` + commit), then the account rows
    are erased in a final one. A single giant transaction is impossible here --
    the warehouse drop is external and not transactional, so a rollback after a
    successful drop would restore rows whose data is already gone. Per-org
    atomicity is the strongest invariant that actually holds, and it is the one
    that matters: no org is ever half-erased. Erasure being monotonic, a retry
    after a mid-sequence failure simply resumes (the erased orgs no longer
    appear in the facts).
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    ident = identity or ""
    if not ident:
        # "anonymous" is not an account; erasing it would erase nothing and
        # audit a lie.
        return JSONResponse(
            {
                "code": "identity_required",
                "message": "An authenticated identity is required to erase an account.",
            },
            status_code=403,
        )

    if request.headers.get("X-Confirm-Delete", "") != "erase-account":
        return JSONResponse(
            {
                "code": "confirmation_required",
                "message": (
                    "Include header X-Confirm-Delete: erase-account to confirm "
                    "permanent erasure of your account."
                ),
            },
            status_code=422,
        )

    from core.db import get_connection  # noqa: PLC0415

    # Phase 1: decide. Read-only snapshot; nothing is touched if it refuses.
    try:
        with get_connection() as conn:
            facts = _account_deletion_facts(conn, ident)
            conn.rollback()
    except Exception:
        logger.exception("admin_api: delete_me facts_failed")
        return JSONResponse(
            {"code": "db_error", "message": "database operation failed"},
            status_code=500,
        )

    if facts["blockers"]:
        return JSONResponse(
            {
                "code": "account_deletion_blocked",
                "message": (
                    "Your account cannot be erased yet: one or more organizations "
                    "depend on it. Resolve the blockers below and retry."
                ),
                "blockers": facts["blockers"],
                "sole_owner_of": facts["sole_owner_of"],
            },
            status_code=409,
        )

    # Phase 2: erase the organizations that belong to nobody but the caller,
    # one atomic transaction each (warehouse datasets included).
    erased_orgs: list[dict] = []
    for org in facts["organizations_erased_with_account"]:
        try:
            org_cm = get_connection()
            org_conn = org_cm.__enter__()
        except Exception:
            logger.exception("admin_api: delete_me db_open_failed org=%s", org["org_id"])
            return JSONResponse(
                {"code": "db_error", "message": "database connection failed"},
                status_code=500,
            )
        try:
            result, error = _erase_org_transactional(
                org_conn,
                org["org_id"],
                name=org["name"],
                slug=org["slug"],
                identity=ident,
            )
            if error is not None:
                logger.warning(
                    "admin_api: delete_me org_erasure_refused identity=%s org=%s status=%s",
                    ident,
                    org["org_id"],
                    error.status_code,
                )
                return JSONResponse(
                    {
                        "code": "org_erasure_failed",
                        "message": (
                            f'Organization "{org["name"]}" could not be erased, so '
                            "your account was NOT erased. Resolve the cause and retry "
                            "-- organizations already erased stay erased."
                        ),
                        "org_id": org["org_id"],
                        "cause": json.loads(error.body),
                        "organizations_erased": erased_orgs,
                    },
                    status_code=error.status_code,
                )
            org_conn.commit()
            erased_orgs.append(
                {
                    "org_id": org["org_id"],
                    "name": org["name"],
                    "removed": result["removed"],
                    "warehouse_datasets": result["warehouse_datasets"],
                }
            )
        except Exception:
            try:
                org_conn.rollback()
            except Exception:
                pass
            logger.exception("admin_api: delete_me org_erasure_failed org=%s", org["org_id"])
            return JSONResponse(
                {"code": "db_error", "message": "database operation failed"},
                status_code=500,
            )
        finally:
            try:
                org_cm.__exit__(None, None, None)
            except Exception:
                pass

    # Phase 3: erase the person -- grants, memberships and profile.
    try:
        acct_cm = get_connection()
        acct_conn = acct_cm.__enter__()
    except Exception:
        logger.exception("admin_api: delete_me db_open_failed identity=%s", ident)
        return JSONResponse(
            {"code": "db_error", "message": "database connection failed"},
            status_code=500,
        )

    try:
        from core.audit import insert_audit_row  # noqa: PLC0415

        with acct_conn.cursor() as cur:
            cur.execute("DELETE FROM app.resource_grants WHERE identity = %s", (ident,))
            project_grants = cur.rowcount or 0
            cur.execute("DELETE FROM app.org_members WHERE identity = %s", (ident,))
            org_memberships = cur.rowcount or 0
            cur.execute("DELETE FROM app.user_profiles WHERE identity = %s", (ident,))
            profile_rows = cur.rowcount or 0

        insert_audit_row(
            acct_conn,
            identity=ident,
            action=ACTION_ACCOUNT_ERASED,
            provider_account="",
            connection_ref="",
            metadata={
                "org_memberships": org_memberships,
                "project_grants": project_grants,
                "profile_erased": bool(profile_rows),
                "organizations_erased": [o["org_id"] for o in erased_orgs],
            },
        )
        # Counted AFTER the audit insert so the number includes the entry that
        # records this very erasure -- what the user is told is retained is
        # exactly what remains.
        with acct_conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM app.audit_log WHERE identity = %s", (ident,))
            row = cur.fetchone()
            retained_audit = int(row[0]) if row else 0
        acct_conn.commit()
    except Exception:
        try:
            acct_conn.rollback()
        except Exception:
            pass
        logger.exception("admin_api: delete_me account_erasure_failed identity=%s", ident)
        return JSONResponse(
            {"code": "db_error", "message": "database operation failed"},
            status_code=500,
        )
    finally:
        try:
            acct_cm.__exit__(None, None, None)
        except Exception:
            pass

    return JSONResponse(
        {
            "deleted": True,
            "identity": ident,
            "erased": {
                "profile": bool(profile_rows),
                "org_memberships": org_memberships,
                "project_grants": project_grants,
                "organizations": erased_orgs,
            },
            # Said out loud rather than discovered later: the audit ledger keeps
            # the identity. It is the proof that the erasure happened (and that
            # every earlier action was legitimately taken); an erasure that also
            # erased its own evidence could not be demonstrated to anyone.
            "retained": {
                "audit_entries": retained_audit,
                "reason": (
                    "Audit entries are kept as the durable, legally required trace "
                    "of platform actions -- including this erasure itself. They "
                    "record actions, not personal content."
                ),
            },
        },
        status_code=200,
    )

async def _revoke_my_sessions(request: Request) -> Response:
    """POST /api/me/sessions/revoke -- sign out of every window, everywhere.

    The distinct gesture from `POST /api/auth/logout`, which closes only the
    window it was clicked in. This one posts a "not before" bound on the caller,
    so every ticket minted before this instant is refused at its next call --
    the one thing a person can do about a laptop they no longer hold. Signing in
    again afterwards works: the bound is temporal, not a ban.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    from core.audit import write_audit_row  # noqa: PLC0415
    from core.browser_oidc import clear_session_cookie, get_browser_session  # noqa: PLC0415
    from core.session_revocation import (  # noqa: PLC0415
        ACTION_SESSIONS_REVOKED_FOR_PERSON,
        revocation_enabled,
        revoke_principal,
        revoke_sessions_for_identity,
    )

    if not revocation_enabled():
        return JSONResponse(
            {
                "code": "unavailable",
                "message": "Session revocation is disabled on this deployment.",
            },
            status_code=503,
        )
    session = get_browser_session(request)
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            if session is not None:
                # The browser path knows its exact issuer, so it records it
                # rather than the any-issuer wildcard.
                revoked = [
                    revoke_principal(
                        conn,
                        subject=session.subject,
                        issuer=session.issuer,
                        revoked_by=identity or session.subject,
                        reason="signed out everywhere",
                    )
                ]
            else:
                revoked = revoke_sessions_for_identity(
                    conn,
                    identity=identity or "",
                    revoked_by=identity or "anonymous",
                    reason="signed out everywhere",
                )
            conn.commit()
    except Exception as exc:
        logger.error("me_api: revoke_my_sessions db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )
    write_audit_row(
        identity=identity or "anonymous",
        action=ACTION_SESSIONS_REVOKED_FOR_PERSON,
        provider_account="",
        connection_ref="",
        metadata={"scope": "principal", "reason": "signed out everywhere", "self": True},
    )
    return clear_session_cookie(JSONResponse({"revoked": len(revoked)}, status_code=200))

async def _list_my_authorizations(request: Request) -> Response:
    """GET /api/me/authorizations -- the credentials THIS person plugged in.

    Story 42.9, user level. Scoped on ``owner_identity`` and nothing else, so it
    spans every organization the person belongs to: the question this surface
    answers is "what did I connect, under my name, and where is it usable" --
    which is exactly what someone needs before revoking or reconnecting.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    subject = identity or "anonymous"

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                _AUTHORIZATION_QUERY.format(scope="r.owner_identity = %(identity)s"),
                {"identity": subject},
            )
            cols = [desc[0] for desc in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as exc:
        logger.error("admin_api: list_my_authorizations db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Authorizations are temporarily unavailable"},
            status_code=503,
        )

    return JSONResponse(
        {"authorizations": [_serialize_authorization(r, subject, False) for r in rows]}
    )

def _credential_authorship(ref: dict, identity: str | None, provided: bool) -> dict:
    """Authorship of a credential, and what the caller may do with it (Story 42.9).

    A credential has TWO attachments and they answer different questions (mig 101):
    ``owner_identity`` is the PERSON who consented at the provider; ``owner_org_id``
    is the organization the credential is usable in. This helper only ever speaks
    about the person -- usage stays resolved on the org, per that migration's
    standing warning, and nothing here may leak into a sync authorization.

    Two distinct gates, decided by Jean 2026-07-27:

    * ``can_update`` -- the author alone. Reconnecting means re-consenting at the
      provider under one's own name; an admin cannot do it on someone else's behalf,
      so offering the action to anyone else would be a button that cannot work.
    * ``can_revoke`` -- the author, OR an owner/admin of the credential's org as a
      backstop. Without the backstop a departing employee's credential would stay
      usable by the whole organization with nobody able to cut it off from the app.

    Cross-org non-disclosure: when the credential merely reaches the viewer through
    an account grant, the beneficiary org is entitled to the provider identity, the
    owning ORGANIZATION, health and expiry -- never the person, and never an action
    (EXPERIENCE.md: the beneficiary "cannot revoke the owner's credential").
    """
    owner_identity = ref.get("owner_identity")
    if provided:
        return {
            "owner_identity": None,
            "owner_display_name": None,
            "is_mine": False,
            "can_update": False,
            "can_revoke": False,
        }
    is_mine = bool(owner_identity) and owner_identity == identity
    return {
        "owner_identity": owner_identity,
        "owner_display_name": _owner_display_name(ref, owner_identity),
        "is_mine": is_mine,
        "can_update": is_mine,
        "can_revoke": is_mine or bool(ref.get("caller_manages_owner")),
    }

def _serialize_authorization(ref: dict, identity: str | None, provided: bool) -> dict:
    """One credential, as the user- and org-level authorization surfaces read it."""
    # AI-219: the type decides, never a list of names. The four names this loop
    # used to carry were the four `connection_ref` date columns of the day it was
    # written; the fifth would have rendered a mute 500 from inside `render()`.
    for column, value in list(ref.items()):
        ref[column] = json_scalar(value)
    health_status = ref.get("health_status")
    if ref.get("status") == "revoked":
        health_status = "revoked"
    return {
        "id": ref["id"],
        "nango_connection_id": ref["nango_connection_id"],
        "provider": ref["provider"],
        "project_id": ref.get("project_id"),
        "created_at": ref.get("created_at"),
        "status": ref.get("status"),
        "auth_path": ref.get("auth_path"),
        "health": (
            None
            if health_status is None
            else {
                "status": health_status,
                "last_checked_at": ref.get("last_checked_at"),
                "last_fetched_at": ref.get("last_fetched_at"),
            }
        ),
        "owner_org_id": ref.get("owner_org_id"),
        "owner_org_name": ref.get("owner_org_name"),
        "token_expiry": ref.get("token_expiry"),
        "account_label": ref.get("account_label"),
        "account_state": ref.get("account_state"),
        "selected_account_count": ref.get("selected_account_count") or 0,
        "exposure": (
            "provided_by_org"
            if provided
            else "shared_with_org"
            if ref.get("has_outgoing_grant")
            else "owned"
        ),
        **_credential_authorship(ref, identity, provided),
    }


# --- LES ROUTES, dans l'ordre exact ou elles etaient declarees ------------
#
# Starlette resout dans l'ordre de declaration ; chaque collection est epissee
# a la position que ses routes occupaient. La preuve est un dump avant/apres.

ME_ROUTES_1 = [
    # Story 21.2: self-service global user profile.
    Route("/api/me/profile", endpoint=_get_my_profile, methods=["GET"]),
    Route("/api/me/profile", endpoint=_patch_my_profile, methods=["PATCH"]),
    # RGPD account erasure: preview declared before the bare /api/me route.
    Route(
        "/api/me/deletion-preview",
        endpoint=_get_my_deletion_preview,
        methods=["GET"],
    ),
    Route("/api/me", endpoint=_delete_me, methods=["DELETE"]),
]

ME_ROUTES_2 = [
    # Story 42.9: user-level authorizations -- the credentials I plugged in,
    # across every organization I belong to.
    Route("/api/me/authorizations", endpoint=_list_my_authorizations, methods=["GET"]),
    # 67-15d: sign out of every window. Distinct from POST /api/auth/logout,
    # which closes only the window it was clicked in.
    Route("/api/me/sessions/revoke", endpoint=_revoke_my_sessions, methods=["POST"]),
]
