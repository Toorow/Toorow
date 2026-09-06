"""Inviter quelqu un dans une organisation, et defaire cette invitation.

AD-43, 2026-08-12. Quatre routes qui partagent `/api/organizations/...` avec
l objet et n ont rien d autre en commun avec lui : emettre, lister, revoquer,
renvoyer. Le test d AD-42 tient -- un changement ici ne toucherait jamais le
CRUD de l organisation.
"""

from __future__ import annotations

import json
import logging
import os

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

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

async def _enforce_platform_admin(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import."""
    from core.admin_api import _enforce_platform_admin as _impl  # noqa: PLC0415
    return await _impl(*args, **kwargs)

def _invitation_no_store(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import."""
    from core.admin_api import _invitation_no_store as _impl  # noqa: PLC0415
    return _impl(*args, **kwargs)

# --- les handlers, deplaces TELS QUELS -----------------------------------

async def _list_invitations(request: Request) -> Response:
    """Return the secret-free invitation lifecycle projection for one scope.

    ``GET /api/organizations/{org_id}/invitations`` lists that organization's
    invitations (org manager). ``GET /api/invitations`` lists the ENTRY
    invitations, those that name no organization (platform admin) -- so an
    invitation without an org never falls out of every view.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"}, status_code=401
        )
    from core.db import get_connection  # noqa: PLC0415
    from core.invitations import list_safe_invitations  # noqa: PLC0415

    org_id = request.path_params.get("org_id")
    if org_id is None:
        denied = await _enforce_platform_admin(request, identity, "list_entry_invitations")
        if denied is not None:
            return denied
    try:
        with get_connection() as conn:
            if org_id is not None:
                denied = _enforce_org_manage(org_id, identity, conn, "list_invitations")
                if denied is not None:
                    return denied
            rows = list_safe_invitations(conn, org_id=org_id)
    except Exception as exc:
        logger.error("admin_api: list_invitations failed: %s", type(exc).__name__)
        return JSONResponse(
            {"code": "operation_failed", "message": "Invitations unavailable"},
            status_code=500,
        )
    return _invitation_no_store(JSONResponse({"items": rows}))

async def _revoke_invitation(request: Request) -> Response:
    return await _mutate_invitation_lifecycle(request, action="revoke")

async def _resend_invitation(request: Request) -> Response:
    return await _mutate_invitation_lifecycle(request, action="resend")

async def _issue_invitation(request: Request) -> Response:
    """Issue one exact invitation after strict authorization.

    ONE endpoint handler, two scopes -- the same invitation object either way:

    * ``POST /api/organizations/{org_id}/invitations`` -- join THAT organization.
      Unchanged: the caller must be an active manager of the org and hold manage
      on every requested resource.
    * ``POST /api/invitations`` -- the ENTRY invitation, with no organization at
      all. Issued by a PLATFORM admin (``TOOROW_SUPER_ADMINS``); grants nothing,
      because there is no org to grant anything in.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"}, status_code=401
        )
    org_id = request.path_params.get("org_id")
    # Platform scope: gate FIRST, before reading the body or complaining about a
    # missing header -- a caller who is not allow-listed must learn nothing at all
    # about this endpoint, not even that it validates requests.
    if org_id is None:
        denied = await _enforce_platform_admin(request, identity, "issue_entry_invitation")
        if denied is not None:
            return denied
    idempotency_key = (request.headers.get("Idempotency-Key") or "").strip()
    if not idempotency_key:
        return JSONResponse(
            {"code": "missing_idempotency_key", "message": "Idempotency-Key is required"},
            status_code=422,
        )
    from core.invitations import InvitationValidationError, issue_invitation  # noqa: PLC0415
    from core.operations import OperationIdempotencyConflict  # noqa: PLC0415

    try:
        body = json.loads(await request.body())
        if not isinstance(body, dict):
            raise InvitationValidationError("invitation body must be an object")
        project_grants = _parse_invitation_grants(body, "project_grants", "project")
        datastream_grants = _parse_invitation_grants(body, "datastream_grants", "flux")
        role = str(body.get("role") or "").strip()
        invited_identity = body.get("invited_identity")
        expires_in_hours = body.get("expires_in_hours", 48)
        if org_id is None and (project_grants or datastream_grants):
            return JSONResponse(
                {
                    "code": "invalid_invitation",
                    "message": (
                        "An invitation without an organization grants nothing: "
                        "project and datastream grants are org-scoped."
                    ),
                },
                status_code=422,
            )
        from core import tracing  # noqa: PLC0415
        from core.db import get_connection, set_local_access_context  # noqa: PLC0415
        from core.project_access import resolve_strict_resource_access  # noqa: PLC0415

        # DELIBERATELY NOT `request_connection`, and this is an ordering, not an
        # oversight. `_enforce_org_manage` resolves WHO IS AUTHORIZED to issue an
        # invitation; arming the floor before it would change which
        # `app.org_members` rows that check itself can see, which changes the
        # answer rather than adding a net under it. The floor goes up
        # immediately after, for every statement that reads on this connection.
        with get_connection() as conn:
            if org_id is not None:
                denied = _enforce_org_manage(org_id, identity, conn, "issue_invitation")
                if denied is not None:
                    return denied
            set_local_access_context(conn, identity, enforce_epic36=True)
            if org_id is not None:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT role FROM app.org_members "
                        "WHERE org_id = %s AND identity = %s AND status = 'active'",
                        (org_id, identity),
                    )
                    issuer_row = cur.fetchone()
                if issuer_row is None or (role == "owner" and issuer_row[0] != "owner"):
                    return JSONResponse(
                        {"code": "not_found", "message": "Invitation scope not found"},
                        status_code=404,
                    )
            for grant in project_grants + datastream_grants:
                kwargs = (
                    {"project_id": grant.scope_id}
                    if grant.scope_type == "project"
                    else {"datastream_id": grant.scope_id}
                )
                decision = resolve_strict_resource_access(
                    identity, conn, minimum_capability="manage", **kwargs
                )
                if not decision.allowed or decision.org_id != org_id:
                    return JSONResponse(
                        {"code": "not_found", "message": "Invitation scope not found"},
                        status_code=404,
                    )
            result = issue_invitation(
                conn,
                invited_identity=invited_identity,
                org_id=org_id,
                role=role,
                project_grants=project_grants,
                datastream_grants=datastream_grants,
                issuer=identity,
                expires_in_hours=expires_in_hours,
                policy_version=os.environ.get("TOOROW_POLICY_VERSION", "v1"),
                idempotency_key=idempotency_key,
                host_context={
                    "host": "rest",
                    "workspace_id": (request.headers.get("X-Workspace-Id") or "console")[:256],
                },
                trace_id=tracing.current_trace_id_hex(),
            )
            conn.commit()
    except InvitationValidationError as exc:
        return JSONResponse({"code": "invalid_invitation", "message": str(exc)}, status_code=422)
    except OperationIdempotencyConflict:
        return JSONResponse(
            {"code": "conflict", "message": "operation conflicts with existing state"},
            status_code=409,
        )
    except Exception as exc:
        logger.error("admin_api: issue_invitation failed: %s", type(exc).__name__)
        return JSONResponse(
            {"code": "operation_failed", "message": "Invitation could not be issued"},
            status_code=500,
        )
    payload = {
        "invitation_id": result.invitation_id,
        "state": result.state,
        "expires_at": result.expires_at,
        "operation_id": result.operation_id,
        "audit_event_id": result.audit_event_id,
        "replayed": result.replayed,
    }
    if result.delivery_url is not None:
        payload["delivery_handoff"] = {"url": result.delivery_url, "single_return": True}
    return Response(
        json.dumps(payload),
        status_code=201,
        media_type="application/vnd.toorow.invitation-handoff+json",
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )

def _authorize_invitation_binding(
    conn,
    *,
    identity: str,
    org_id: str | None,
    invitation_id: str,
    require_bindings: bool = True,
) -> Response | None:
    """Require manage on the org and every immutable invitation resource binding.

    ``org_id is None`` is the ENTRY-invitation scope: the platform gate has
    already been applied by the caller, and such an invitation holds no grant
    binding, so all that remains is to confirm the invitation really lives in
    that scope (``org_id IS NULL``) -- never in someone's organization.

    ``require_bindings=False`` IS THE REVOCATION, and the distinction is the
    repair of audit 12, P2-6 (2026-08-17). Re-resolving every granted scope is
    right when the act HANDS SOMETHING OUT: resending re-delivers a bearer that
    will materialise those grants, so the sender must still hold manage on each
    of them. Revoking hands nothing out -- it takes the invitation away. Making
    it prove authority over the scopes meant that archiving or deleting one
    granted project left the invitation IRREVOCABLE by its own org manager, a
    pending grant nobody could withdraw. A revocation validates nothing; it
    removes. Manage on the organization, and the invitation living in that
    organization, are the whole authority it needs.
    """
    if org_id is not None:
        denied = _enforce_org_manage(org_id, identity, conn, "manage_invitation")
        if denied is not None:
            return denied
    with conn.cursor() as cur:
        cur.execute(
            "SELECT grant_bindings FROM app.invitations "
            "WHERE id = %s AND org_id IS NOT DISTINCT FROM %s",
            (invitation_id, org_id),
        )
        row = cur.fetchone()
    if row is None or not isinstance(row[0], list):
        return JSONResponse({"code": "not_found", "message": "Not found"}, status_code=404)
    if not require_bindings:
        return None
    from core.project_access import resolve_strict_resource_access  # noqa: PLC0415

    for grant in row[0]:
        if not isinstance(grant, dict) or grant.get("scope_type") not in {"project", "flux"}:
            return JSONResponse({"code": "not_found", "message": "Not found"}, status_code=404)
        kwargs = (
            {"project_id": grant.get("scope_id")}
            if grant["scope_type"] == "project"
            else {"datastream_id": grant.get("scope_id")}
        )
        decision = resolve_strict_resource_access(
            identity,
            conn,
            minimum_capability="manage",
            **kwargs,
        )
        if not decision.allowed or decision.org_id != org_id:
            return JSONResponse({"code": "not_found", "message": "Not found"}, status_code=404)
    return None

async def _mutate_invitation_lifecycle(request: Request, *, action: str) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"}, status_code=401
        )
    org_id = request.path_params.get("org_id")
    invitation_id = request.path_params["invitation_id"]
    # Platform scope (entry invitation): gate before anything else is answered.
    if org_id is None:
        denied = await _enforce_platform_admin(request, identity, f"{action}_entry_invitation")
        if denied is not None:
            return denied
    idempotency_key = (request.headers.get("Idempotency-Key") or "").strip()
    if not idempotency_key:
        return _invitation_no_store(
            JSONResponse(
                {"code": "missing_idempotency_key", "message": "Idempotency-Key is required"},
                status_code=422,
            )
        )
    from core import tracing  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415
    from core.invitations import (  # noqa: PLC0415
        InvitationLifecycleConflict,
        InvitationValidationError,
        resend_invitation,
        transition_invitation,
    )
    from core.operations import OperationIdempotencyConflict  # noqa: PLC0415

    try:
        body = json.loads(await request.body()) if action == "resend" else {}
        with get_connection() as conn:
            denied = _authorize_invitation_binding(
                conn,
                identity=identity,
                org_id=org_id,
                invitation_id=invitation_id,
                # Revoking removes; only resending hands the grants out again.
                require_bindings=(action != "revoke"),
            )
            if denied is not None:
                return denied
            common = {
                "conn": conn,
                "invitation_id": invitation_id,
                "actor": identity,
                "idempotency_key": idempotency_key,
                "host_context": {
                    "host": "rest",
                    "workspace_id": (request.headers.get("X-Workspace-Id") or "console")[:256],
                },
                "trace_id": tracing.current_trace_id_hex(),
            }
            if action == "resend":
                result = resend_invitation(
                    **common,
                    expires_in_hours=(
                        body.get("expires_in_hours", 48) if isinstance(body, dict) else 48
                    ),
                )
            else:
                result = transition_invitation(**common, transition=action)
            conn.commit()
    except (InvitationLifecycleConflict, OperationIdempotencyConflict):
        return _invitation_no_store(
            JSONResponse(
                {"code": "conflict", "message": "Invitation lifecycle already resolved"},
                status_code=409,
            )
        )
    except (InvitationValidationError, json.JSONDecodeError, TypeError) as exc:
        return _invitation_no_store(
            JSONResponse({"code": "invalid_request", "message": str(exc)}, status_code=422)
        )
    except Exception as exc:
        logger.error("admin_api: invitation_%s failed: %s", action, type(exc).__name__)
        return _invitation_no_store(
            JSONResponse(
                {"code": "operation_failed", "message": "Invitation unavailable"},
                status_code=500,
            )
        )
    payload = {
        "invitation_id": result.invitation_id,
        "state": result.state,
        "operation_id": result.operation_id,
        "audit_event_id": result.audit_event_id,
        "replayed": result.replayed,
    }
    if action == "resend" and result.delivery_url is not None:
        payload["delivery_handoff"] = {"url": result.delivery_url, "single_return": True}
    return _invitation_no_store(
        Response(
            json.dumps(payload),
            media_type=(
                "application/vnd.toorow.invitation-handoff+json"
                if action == "resend"
                else "application/json"
            ),
        )
    )

def _parse_invitation_grants(body: dict, field: str, scope_type: str):
    from core.invitations import InvitationGrant, InvitationValidationError  # noqa: PLC0415

    raw = body.get(field, [])
    if not isinstance(raw, list):
        raise InvitationValidationError(f"{field} must be an array")
    grants = []
    for item in raw:
        if not isinstance(item, dict):
            raise InvitationValidationError(f"{field} contains an invalid grant")
        grants.append(
            InvitationGrant(
                scope_type,
                str(item.get("scope_id") or "").strip(),
                str(item.get("capability") or "view").strip(),
            )
        )
    return tuple(grants)




#: Le cookie de l'echange -- court, strict, et lu par les deux bouts du parcours.
_INVITATION_EXCHANGE_COOKIE = "toorow_invitation_exchange"

# --- L'ARRIVEE : ce qu'une invitation devient entre les mains de qui la recoit
#
# AD-43, sixieme etape, 2026-08-13. Emettre une invitation et l'accepter sont
# le MEME objet vu des deux bouts : ajouter un champ a une invitation touche
# les deux. Elles etaient dans deux fichiers ; la page d'amorce, l'echange du
# jeton et l'acceptation rejoignent ici l'emission, la liste et la revocation.
# Ce qui reste au joint est de l'AUTORISATION -- `_check_invitation_identity`
# et `_invitation_no_store` ont d'autres lecteurs.

async def _check_invitation_principal(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import."""
    from core.admin_api import _check_invitation_principal as _impl  # noqa: PLC0415
    return await _impl(*args, **kwargs)


async def _invitation_bootstrap(_request: Request) -> Response:
    """Generic no-store shell; Story 36.4 owns authenticated bearer exchange."""
    import secrets  # noqa: PLC0415

    nonce = secrets.token_urlsafe(18)
    html = """<!doctype html><html><head><meta charset="utf-8">
<meta name="referrer" content="no-referrer"><title>Continue to Toorow</title></head>
<body><main><h1>Continue securely</h1><p>Sign in to continue.</p></main>
<script nonce="__NONCE__">'use strict';const raw=location.hash.startsWith('#invite=')
?location.hash.slice(8):'';history.replaceState(null,'',location.pathname);
if(raw){fetch('/api/invitations/exchange',{method:'POST',credentials:'same-origin',
headers:{'Content-Type':'application/json'},body:JSON.stringify({bearer:raw})});}</script>
</body></html>""".replace("__NONCE__", nonce)
    return Response(
        html,
        media_type="text/html",
        headers={
            "Cache-Control": "no-store, max-age=0",
            "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": (
                f"default-src 'none'; script-src 'nonce-{nonce}'; "
                "connect-src 'self'; style-src 'none'; img-src 'none'; "
                "font-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
            ),
            "X-Frame-Options": "DENY",
            "X-Content-Type-Options": "nosniff",
        },
    )


async def _exchange_invitation(request: Request) -> Response:
    """Exchange a fragment bearer only for its matching protected-auth subject."""
    authorized, identity, canonical_principal = await _check_invitation_principal(request)
    if (
        not authorized
        or identity == "anonymous"
        or os.environ.get("TOOROW_AUTH_MODE", "disabled").strip().lower() == "disabled"
    ):
        return _invitation_no_store(
            JSONResponse(
                {"code": "unauthorized", "message": "Authentication required"}, status_code=401
            )
        )
    from core.invitations import InvitationExchangeError, exchange_invitation  # noqa: PLC0415

    try:
        body = json.loads(await request.body())
        bearer = body.get("bearer") if isinstance(body, dict) else None
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            exchanged = exchange_invitation(
                conn,
                bearer=bearer,
                verified_identity=identity,
                person_id=(
                    canonical_principal.person_id if canonical_principal is not None else None
                ),
            )
            conn.commit()
    except (InvitationExchangeError, json.JSONDecodeError, TypeError):
        return _invitation_no_store(
            JSONResponse(
                {"code": "not_found", "message": "Invitation unavailable"}, status_code=404
            )
        )
    except Exception as exc:
        logger.error("admin_api: invitation_exchange failed: %s", type(exc).__name__)
        return _invitation_no_store(
            JSONResponse(
                {"code": "operation_failed", "message": "Invitation unavailable"}, status_code=500
            )
        )
    response = _invitation_no_store(
        JSONResponse(
            {
                "ready_to_accept": True,
                "preview": {
                    "organization_id": exchanged.preview.organization_id,
                    "organization_label": exchanged.preview.organization_label,
                    "authority": {
                        "role_derived": exchanged.preview.role_derived,
                        "explicit_grants": list(exchanged.preview.explicit_grants),
                        "explicit_none": not exchanged.preview.explicit_grants,
                    },
                    "expires_at": exchanged.preview.expires_at.isoformat(),
                },
            }
        )
    )
    response.set_cookie(
        _INVITATION_EXCHANGE_COOKIE,
        exchanged.session_value,
        max_age=exchanged.max_age_seconds,
        path="/api/invitations",
        secure=True,
        httponly=True,
        samesite="strict",
    )
    return response


async def _accept_invitation(request: Request) -> Response:
    """Confirm exact membership/grants using the narrow exchange cookie."""
    authorized, identity, canonical_principal = await _check_invitation_principal(request)
    if (
        not authorized
        or identity == "anonymous"
        or os.environ.get("TOOROW_AUTH_MODE", "disabled").strip().lower() == "disabled"
    ):
        return _invitation_no_store(
            JSONResponse(
                {"code": "unauthorized", "message": "Authentication required"}, status_code=401
            )
        )
    idempotency_key = (request.headers.get("Idempotency-Key") or "").strip()
    if not idempotency_key:
        return _invitation_no_store(
            JSONResponse(
                {"code": "missing_idempotency_key", "message": "Idempotency-Key is required"},
                status_code=422,
            )
        )
    session_value = request.cookies.get(_INVITATION_EXCHANGE_COOKIE, "")
    from core.invitations import (  # noqa: PLC0415
        InvitationAcceptanceConflict,
        InvitationExchangeError,
        accept_invitation,
    )
    from core.operations import OperationIdempotencyConflict  # noqa: PLC0415

    try:
        body = json.loads(await request.body())
        confirmed = body.get("confirmed") if isinstance(body, dict) else False
        from core import tracing  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            accepted = accept_invitation(
                conn,
                session_value=session_value,
                verified_identity=identity,
                person_id=(
                    canonical_principal.person_id if canonical_principal is not None else None
                ),
                confirmed=confirmed,
                idempotency_key=idempotency_key,
                host_context={
                    "host": "rest",
                    "workspace_id": (request.headers.get("X-Workspace-Id") or "console")[:256],
                },
                trace_id=tracing.current_trace_id_hex(),
            )
            conn.commit()
    except InvitationExchangeError:
        return _invitation_no_store(
            JSONResponse(
                {"code": "not_found", "message": "Invitation unavailable"}, status_code=404
            )
        )
    except (InvitationAcceptanceConflict, OperationIdempotencyConflict):
        return _invitation_no_store(
            JSONResponse(
                {"code": "conflict", "message": "Invitation conflicts with existing access"},
                status_code=409,
            )
        )
    except (json.JSONDecodeError, TypeError):
        return _invitation_no_store(
            JSONResponse(
                {"code": "invalid_confirmation", "message": "Confirmation required"},
                status_code=422,
            )
        )
    except Exception as exc:
        logger.error("admin_api: invitation_accept failed: %s", type(exc).__name__)
        return _invitation_no_store(
            JSONResponse(
                {"code": "operation_failed", "message": "Invitation unavailable"}, status_code=500
            )
        )
    # WHO this person is -- resolved here, because acceptance is the one moment
    # every arrival passes through, whether they are joining an organization or
    # about to create their own. Jean, 2026-07-26: "invitation -> on te demande
    # ton nom et ton prénom (si pas dispo dans l'oath) -> tu accèdes à une
    # organisation (ou ça te permet d'en créer une nouvelle)".
    #
    # Canonical mode keys the profile on person_id. Legacy mode keeps the raw
    # subject until an explicit backfill has mapped existing memberships.
    #
    # Best-effort in the strongest sense: acceptance has already COMMITTED. A
    # failure here must not change its outcome, so everything below is inside one
    # try/except and the worst case is `needs_name: true` -- the console asks.
    profile_name: str | None = None
    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.user_profiles import fetch_user_profile, upsert_user_profile  # noqa: PLC0415

        if canonical_principal is not None:
            ok_subject = True
            subject = canonical_principal.person_id
            token_name = canonical_principal.display_name
        else:
            from core.api_auth import authenticate_subject_and_name  # noqa: PLC0415

            ok_subject, subject, token_name = await authenticate_subject_and_name(request)
        if ok_subject and subject:
            with get_connection() as profile_conn:
                existing = (fetch_user_profile(subject, profile_conn) or {}).get("display_name")
                existing = existing.strip() if isinstance(existing, str) else ""
                if existing:
                    # Never overwrite: a name the person typed themselves outranks
                    # whatever the provider carries.
                    profile_name = existing
                elif token_name:
                    upsert_user_profile(subject, profile_conn, display_name=token_name)
                    profile_conn.commit()
                    profile_name = token_name
    except Exception as exc:  # noqa: BLE001
        logger.warning("admin_api: could not resolve a profile name at acceptance: %s", exc)

    response = _invitation_no_store(
        JSONResponse(
            {
                "invitation_id": accepted.invitation_id,
                "organization_id": accepted.org_id,
                # The console asks for a first/last name if and only if this is
                # true, so it must be false whenever a name is actually on file.
                "profile": {"display_name": profile_name, "needs_name": not profile_name},
                "authority": {
                    "role_derived": accepted.role,
                    "explicit_grants": list(accepted.explicit_grants),
                    "explicit_none": accepted.explicit_none,
                },
                "operation_id": accepted.operation_id,
                "audit_event_id": accepted.audit_event_id,
                "outbox_event_id": accepted.outbox_event_id,
                "next_url": accepted.next_url,
                "replayed": accepted.replayed,
            }
        )
    )
    response.delete_cookie(
        _INVITATION_EXCHANGE_COOKIE,
        path="/api/invitations",
        secure=True,
        httponly=True,
        samesite="strict",
    )
    return response

# --- LES ROUTES, dans l'ordre exact ou elles etaient declarees ------------
#
# Starlette resout dans l'ordre de declaration ; chaque collection est epissee
# a la position que ses routes occupaient. La preuve est un dump avant/apres.

INVITATIONS_ROUTES_1 = [
    # ENTRY invitations -- the SAME invitation object, with no target
    # organization (migration 109). Platform-admin scope; the static routes
    # above are declared first so they always win the match.
    Route("/api/invitations", endpoint=_list_invitations, methods=["GET"]),
    Route("/api/invitations", endpoint=_issue_invitation, methods=["POST"]),
    Route(
        "/api/invitations/{invitation_id}/revoke",
        endpoint=_revoke_invitation,
        methods=["POST"],
    ),
    Route(
        "/api/invitations/{invitation_id}/resend",
        endpoint=_resend_invitation,
        methods=["POST"],
    ),
]

INVITATIONS_ROUTES_2 = [
    Route(
        "/api/organizations/{org_id}/invitations",
        endpoint=_list_invitations,
        methods=["GET"],
    ),
    Route(
        "/api/organizations/{org_id}/invitations/{invitation_id}/revoke",
        endpoint=_revoke_invitation,
        methods=["POST"],
    ),
    Route(
        "/api/organizations/{org_id}/invitations/{invitation_id}/resend",
        endpoint=_resend_invitation,
        methods=["POST"],
    ),
    Route(
        "/api/organizations/{org_id}/invitations",
        endpoint=_issue_invitation,
        methods=["POST"],
    ),
]

INVITATIONS_ARRIVAL_ROUTES = [
    Route("/invite", endpoint=_invitation_bootstrap, methods=["GET"]),
    Route("/api/invitations/exchange", endpoint=_exchange_invitation, methods=["POST"]),
    Route("/api/invitations/accept", endpoint=_accept_invitation, methods=["POST"]),
]
