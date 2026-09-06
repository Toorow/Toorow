"""Connections, addressed through the project that scopes them.

AD-43, 2026-08-12. Two routes: opening a direct Google connection, and revoking
one. They live under `/api/projects/…` because a connection is reached through
the project that may use it -- the scope is the project, the subject is the
connection.
"""

from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core.connection_revocation import (
    _apply_credential_revocation,
)

logger = logging.getLogger("core.admin_api")

# --- le joint qui reste dans admin_api -----------------------------------
async def _check_auth(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import.

    Une soixantaine de suites patchent `core.admin_api._check_auth` ; un import
    de tete ignorerait le patch EN SILENCE et lirait la production.
    """
    from core.admin_api import _check_auth as _impl  # noqa: PLC0415
    return await _impl(*args, **kwargs)

def _project_not_found_response(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import.

    Une soixantaine de suites patchent `core.admin_api._project_not_found_response` ; un import
    de tete ignorerait le patch EN SILENCE et lirait la production.
    """
    from core.admin_api import _project_not_found_response as _impl  # noqa: PLC0415
    return _impl(*args, **kwargs)

# --- les handlers, deplaces TELS QUELS -----------------------------------

async def _initiate_google_direct_connection(request: Request) -> Response:
    """POST /api/projects/{project_id}/connections/google_direct

    Story 43.13: Owner-authorized server endpoint that atomically creates or
    resolves the initial project-scoped google_direct connection_ref and starts
    unified Google consent (AD-21).
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Token d'acces requis"}, status_code=401
        )
    project_id = request.path_params.get("project_id", "").strip()
    if not project_id:
        return JSONResponse(
            {"code": "missing_param", "message": "Le parametre 'project_id' est requis."},
            status_code=400,
        )

    from ulid import ULID  # noqa: PLC0415

    from core.db import get_connection  # noqa: PLC0415
    from core.project_access import (  # noqa: PLC0415
        identity_can_manage_org,
        identity_can_read_project,
    )

    try:
        with get_connection() as conn:
            allowed = identity_can_read_project(project_id, identity or "anonymous", conn)
            if not allowed:
                return _project_not_found_response()

            with conn.cursor() as cur:
                cur.execute(
                    "SELECT org_id FROM app.projects WHERE id = %s AND status = 'active'",
                    (project_id,),
                )
                proj_row = cur.fetchone()
                if not proj_row:
                    return _project_not_found_response()
                org_id = proj_row[0]

                if not identity_can_manage_org(org_id, identity or "anonymous", conn):
                    return JSONResponse(
                        {
                            "code": "forbidden",
                            "message": (
                                "Only the owning organization can initialize this connection."
                            ),
                        },
                        status_code=403,
                    )

                # ONE Google authorization per project was the rule this query
                # enforced: `LIMIT 1` over the project's google rows meant the
                # second consent overwrote the first, and an organization with
                # ten Google accounts could register exactly one of them. There
                # is nothing in the target that says so -- `glossary.md` puts no
                # cardinality on Source Authorization, and an agency holding one
                # consent per client is the ordinary case.
                #
                # What IS worth reusing is an authorization that was STARTED and
                # never finished: a row with no token behind it, minted by this
                # same endpoint for this same person, that a closed consent tab
                # left behind. Reusing it keeps abandoned attempts from piling
                # up; anything that ever received a token is a real, distinct
                # authorization and is left alone.
                cur.execute(
                    """
                    SELECT id FROM app.connection_ref
                    WHERE project_id = %s
                      AND (provider = 'google' OR provider = 'google_direct')
                      AND auth_path = 'google_direct'
                      AND owner_identity = %s
                      AND encrypted_token_blob IS NULL
                      AND status = 'active'
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (project_id, identity or "anonymous"),
                )
                row = cur.fetchone()
                if row:
                    connection_ref_id = row[0]
                else:
                    connection_ref_id = f"conn_{ULID()}"
                    # Two constraint violations lived in this one INSERT, and
                    # both answered 500 on the FIRST "Connect Google" of any
                    # deployment -- the next two links in the chain migration 128
                    # documents:
                    #
                    #   owner_identity  NOT NULL since migration 101 (a credential
                    #     belongs to the PERSON who consents, distinctly from the
                    #     organization it is usable in). Same value and fallback as
                    #     the Nango path above; the callback then records the real
                    #     identity carried by the verified state.
                    #   status  CHECK (status IN ('active','revoked')) -- 'pending'
                    #     was never a legal value. It is also not the question this
                    #     column answers: status is the lifecycle of the REFERENCE,
                    #     while "is there a usable token yet" is health, which reads
                    #     not_connected until the consent completes. A 'pending' row
                    #     would additionally be invisible to every `status='active'`
                    #     read, including the wizard's own list, and nothing flips it
                    #     back afterwards.
                    cur.execute(
                        """
                        INSERT INTO app.connection_ref (
                            id, project_id, owner_org_id, owner_identity,
                            provider, auth_path, status, created_at
                        ) VALUES (%s, %s, %s, %s, 'google', 'google_direct', 'active', NOW())
                        """,
                        (connection_ref_id, project_id, org_id, identity or "anonymous"),
                    )
            conn.commit()
    except Exception as exc:
        logger.error("admin_api: initiate_google_direct db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Erreur base de donnees."}, status_code=500
        )

    from core.google_oauth import GoogleOAuthConfigError, build_authorize_url  # noqa: PLC0415

    try:
        authorize_url = build_authorize_url(
            project_id=project_id,
            connection_ref_id=connection_ref_id,
            identity=identity or "anonymous",
        )
    except GoogleOAuthConfigError:
        return JSONResponse(
            {
                "code": "oauth_not_configured",
                "message": (
                    "Le client OAuth Google n'est pas configure sur le serveur "
                    "(variables GOOGLE_OAUTH_* manquantes)."
                ),
                "connection_ref_id": connection_ref_id,
            },
            status_code=503,
        )
    except Exception as exc:
        logger.error("admin_api: initiate_google_direct error: %s", type(exc).__name__)
        return JSONResponse(
            {"code": "oauth_error", "message": "Impossible de construire l'URL d'autorisation."},
            status_code=500,
        )

    return JSONResponse({"connection_ref_id": connection_ref_id, "authorize_url": authorize_url})

async def _revoke_connection(request: Request) -> Response:
    """POST /api/projects/{project_id}/connections/{connection_id}/revoke.

    Per-connection revocation endpoint (Story 7.3, AC4).
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    project_id = request.path_params.get("project_id", "")
    connection_id = request.path_params.get("connection_id", "")
    subject = identity or "anonymous"

    if not project_id or not connection_id:
        return JSONResponse(
            {"code": "missing_id", "message": "project_id and connection_id are required"},
            status_code=400,
        )

    # Step 1: enforce the requested project and credential-owner scopes before mutation.
    try:
        from core.db import request_connection  # noqa: PLC0415
        from core.project_access import (  # noqa: PLC0415
            identity_can_manage_org,
            resolve_strict_resource_access,
        )

        with request_connection(subject) as conn:
            # Strict, always -- see the note on the project read above.
            project_allowed = resolve_strict_resource_access(
                subject, conn, project_id=project_id, minimum_capability="manage"
            ).allowed
            if not project_allowed:
                return JSONResponse(
                    {"code": "not_found", "message": "Connection not found"},
                    status_code=404,
                )

            with conn.cursor() as cur:
                # THE CONNECTION MUST BELONG TO THE PROJECT THE CALLER ASSERTED.
                #
                # This joined `app.projects` on the ASSERTED id and then selected
                # the connection by `r.id` ALONE -- so the two ids were never
                # required to agree. A caller holding project A could revoke a
                # connection of project B: not a leak of a datum, the revocation
                # of somebody else's credential, on a path whose whole subject is
                # authorization.
                #
                # It stayed invisible because the test that asks for it
                # (`test_revoke_connection_wrong_project_returns_404`) got its 404
                # from a caller who held NOTHING. Enrolling that caller properly is
                # what made the missing predicate answer 200.
                #
                # `connection_ref.project_id` is NOT NULL, so the comparison is
                # total -- there is no row this predicate cannot judge.
                cur.execute(
                    """
                    SELECT r.id, r.nango_connection_id, r.provider, r.status,
                           r.owner_org_id, r.owner_identity, p.org_id
                    FROM app.connection_ref r
                    JOIN app.projects p ON p.id = r.project_id AND p.status = 'active'
                    WHERE r.id = %s AND r.project_id = %s
                    """,
                    (connection_id, project_id),
                )
                row = cur.fetchone()
            if row is None:
                return JSONResponse(
                    {"code": "not_found", "message": "Connection not found"},
                    status_code=404,
                )
            (
                _conn_id,
                nango_connection_id,
                provider,
                current_status,
                owner_org_id,
                owner_identity,
                project_org_id,
            ) = row
            # Story 42.9 -- who may cut a credential off (Jean, 2026-07-27):
            # its AUTHOR, because it is their access under their name; or an
            # owner/admin of the owning org as a backstop, because otherwise a
            # departing employee's credential stays usable by everyone with nobody
            # able to stop it. The two are recorded distinctly in the audit row --
            # a backstop revocation is not the owner's own decision and must not
            # read like one later.
            author_revoke = bool(owner_identity) and owner_identity == subject
            org_backstop = identity_can_manage_org(owner_org_id, subject, conn)
            if owner_org_id != project_org_id or not (author_revoke or org_backstop):
                return JSONResponse(
                    {
                        "code": "forbidden",
                        "message": (
                            "Only the person who connected this credential, or an "
                            "owner/admin of the organization that owns it, can revoke it"
                        ),
                    },
                    status_code=403,
                )
            revoked_as = "author" if author_revoke else "org_admin_backstop"
    except Exception as exc:
        logger.error("admin_api: revoke_connection db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )

    # Steps 2-5: Nango delete, cache purge, mark revoked, audit -- shared with the
    # credential-scoped endpoint so both surfaces have identical effects.
    nango_deleted, error = _apply_credential_revocation(
        connection_id=connection_id,
        nango_connection_id=nango_connection_id,
        provider=provider,
        subject=subject,
        revoked_as=revoked_as,
        owner_identity=owner_identity,
        project_id=project_id,
    )
    if error is not None:
        return error

    # Step 6: Return result.
    return JSONResponse(
        {"status": "revoked", "nango_deleted": nango_deleted},
        status_code=200,
    )


# --- LES ROUTES, dans l'ordre exact ou elles etaient declarees ------------
#
# Starlette resout dans l'ordre de declaration. Chaque collection est epissee
# par `admin_api` a la position que ses routes occupaient : memes chemins,
# memes methodes, meme ordre. La preuve est un dump avant/apres, pas une
# lecture de diff.

PROJECT_CONNECTIONS_ROUTES_1 = [
    # Story 43.13: Owner-authorized google_direct initial connection endpoint
    Route(
        "/api/projects/{project_id}/connections/google_direct",
        endpoint=_initiate_google_direct_connection,
        methods=["POST"],
    ),
]

PROJECT_CONNECTIONS_ROUTES_2 = [
    # Story 7.3 (AC4): per-connection revocation endpoint.
    Route(
        "/api/projects/{project_id}/connections/{connection_id}/revoke",
        endpoint=_revoke_connection,
        methods=["POST"],
    ),
]
