"""Quels projets un flux alimente.

AD-43, 2026-08-13. Trois routes qui ne parlent que du lien entre un flux et un
projet -- le lier, les lister, le defaire. Le flux lui-meme et le projet
lui-meme sont deux objets deja loges ; leur RELATION en est un troisieme, avec
sa propre autorisation (`_enforce_org_manage`).
"""

from __future__ import annotations

import json
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core.audit import (
    declare_action,
    write_audit_row,
)

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

# --- les handlers, deplaces TELS QUELS -----------------------------------

ACTION_FLUX_LINKED = declare_action("flux_linked_to_project")

ACTION_FLUX_UNLINKED = declare_action("flux_unlinked_from_project")

async def _list_flux_projects(request: Request) -> Response:
    """GET /api/flux/{flux_id}/projects -- projects linked to a flux (AC2)."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    flux_id = request.path_params["flux_id"]
    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.project_access import identity_has_org_access  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT org_id FROM app.datastreams WHERE id = %s", (flux_id,))
                frow = cur.fetchone()
            # Story 21.5 follow-up (reads scoping): 404 if the flux is absent or in
            # an org the caller cannot see (NULL org -> legacy, treated as open).
            if frow is None or (
                frow[0] is not None
                and not identity_has_org_access(frow[0], identity or "anonymous", conn)
            ):
                return JSONResponse(
                    {"code": "not_found", "message": "flux not found"},
                    status_code=404,
                )
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT p.id, p.name, p.slug
                    FROM app.project_flux pf
                    JOIN app.projects p ON p.id = pf.project_id
                    WHERE pf.flux_id = %s
                    ORDER BY p.name ASC
                    """,
                    (flux_id,),
                )
                projects = [
                    {"project_id": r[0], "name": r[1], "slug": r[2]} for r in cur.fetchall()
                ]
    except Exception as exc:
        logger.error("admin_api: list_flux_projects db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )
    return JSONResponse({"projects": projects}, status_code=200)

async def _link_flux_to_project(request: Request) -> Response:
    """POST /api/flux/{flux_id}/projects -- link a flux to a project (AC2).

    Body: {"project_id": str}. 404 if the flux or the project does not exist;
    409 code "cross_org" if project.org_id != flux.org_id (a flux only feeds
    projects of its own org); 409 if the link already exists. Audited (AD-14).
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    flux_id = request.path_params["flux_id"]
    try:
        body: dict = json.loads(await request.body())
    except Exception as exc:
        return JSONResponse(
            {"code": "invalid_body", "message": f"Invalid JSON body: {exc}"},
            status_code=400,
        )
    project_id = (body.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse(
            {"code": "invalid_input", "message": "project_id is required"},
            status_code=422,
        )
    linked_by = identity or "anonymous"
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                # Resolve the flux (and its org) first. 404 if unknown.
                cur.execute("SELECT org_id FROM app.datastreams WHERE id = %s", (flux_id,))
                flux_row = cur.fetchone()
                if flux_row is None:
                    return JSONResponse(
                        {"code": "not_found", "message": "flux not found"},
                        status_code=404,
                    )
                flux_org_id = flux_row[0]
                # Story 21.5: only an owner/admin of the flux's OWNER org may link it.
                # A flux with no org yet (NULL) falls through to the existing 409
                # (cross_org) below; when owned, require manage. Default-open org ->
                # resolves to owner (keeps 21.4 tests green).
                if flux_org_id is not None:
                    denied = _enforce_org_manage(
                        flux_org_id, identity, conn, "link_flux_to_project"
                    )
                    if denied is not None:
                        return denied
                # Resolve the project (and its org). 404 if unknown.
                cur.execute("SELECT org_id FROM app.projects WHERE id = %s", (project_id,))
                proj_row = cur.fetchone()
                if proj_row is None:
                    return JSONResponse(
                        {"code": "not_found", "message": "project not found"},
                        status_code=404,
                    )
                project_org_id = proj_row[0]
                # review-stack F-MEDIUM: a flux or project not yet scoped to an org
                # (org_id NULL, legacy pre-21.5) is NOT linkable -- guard first so
                # `None != None` (False) cannot slip a NULL into project_flux.org_id
                # (NOT NULL) and surface as a raw 500.
                if flux_org_id is None or project_org_id is None:
                    return JSONResponse(
                        {
                            "code": "cross_org",
                            "message": "flux or project has no organization yet; cannot link",
                        },
                        status_code=409,
                    )
                # Friendly cross-org check (the composite FK is the structural
                # backstop; this yields a clear 409 instead of a bare FK error).
                if project_org_id != flux_org_id:
                    return JSONResponse(
                        {
                            "code": "cross_org",
                            "message": "a flux can only feed projects of its own org",
                        },
                        status_code=409,
                    )
                # Duplicate link -> 409.
                cur.execute(
                    "SELECT 1 FROM app.project_flux WHERE project_id = %s AND flux_id = %s",
                    (project_id, flux_id),
                )
                if cur.fetchone() is not None:
                    return JSONResponse(
                        {"code": "conflict", "message": "flux already linked to this project"},
                        status_code=409,
                    )
                cur.execute(
                    """
                    INSERT INTO app.project_flux (project_id, flux_id, org_id)
                    VALUES (%s, %s, %s)
                    RETURNING project_id, flux_id, org_id, created_at
                    """,
                    (project_id, flux_id, flux_org_id),
                )
                r = cur.fetchone()
            conn.commit()
    except Exception as exc:
        logger.error("admin_api: link_flux_to_project db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )
    write_audit_row(
        identity=linked_by,
        action=ACTION_FLUX_LINKED,
        provider_account="",
        connection_ref="",
        metadata={"flux_id": flux_id, "project_id": project_id, "org_id": flux_org_id},
    )
    return JSONResponse(
        {
            "project_id": r[0],
            "flux_id": r[1],
            "org_id": r[2],
            "created_at": r[3].isoformat() if r[3] else None,
        },
        status_code=201,
    )

async def _unlink_flux_from_project(request: Request) -> Response:
    """DELETE /api/flux/{flux_id}/projects/{project_id} -- unlink (AC2).

    Removes the M:N link. 404 if no such link. Audited (AD-14).
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    flux_id = request.path_params["flux_id"]
    project_id = request.path_params["project_id"]
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                # Story 21.5: only an owner/admin of the flux's OWNER org may unlink.
                # A flux with no org yet (NULL, legacy) is treated as open (compat).
                # Default-open org -> resolves to owner (keeps 21.4 tests green).
                cur.execute("SELECT org_id FROM app.datastreams WHERE id = %s", (flux_id,))
                flux_row = cur.fetchone()
                flux_org_id = flux_row[0] if flux_row is not None else None
                if flux_org_id is not None:
                    denied = _enforce_org_manage(
                        flux_org_id, identity, conn, "unlink_flux_from_project"
                    )
                    if denied is not None:
                        return denied
                # Delier un flux de son PROPRE projet le rend invisible a tous
                # ses ecrans de detail (ils resolvent par project_flux) tout en
                # le laissant liste par GET /api/datastreams, qui filtre sur
                # ds.project_id. C'est exactement l'etat casse repare ce jour,
                # atteignable ici par un appel normal. Le lien de partage se
                # retire, l'appartenance non.
                cur.execute(
                    "SELECT project_id FROM app.datastreams WHERE id = %s", (flux_id,)
                )
                owner = cur.fetchone()
                if owner and owner[0] == project_id:
                    return JSONResponse(
                        {
                            "code": "owner_link_required",
                            "message": (
                                "This project owns the Datastream, so the link "
                                "cannot be removed here. Archive or delete the "
                                "flux instead."
                            ),
                        },
                        status_code=409,
                    )
                cur.execute(
                    "DELETE FROM app.project_flux WHERE project_id = %s AND flux_id = %s",
                    (project_id, flux_id),
                )
                deleted = cur.rowcount
            conn.commit()
    except Exception as exc:
        logger.error("admin_api: unlink_flux_from_project db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )
    if not deleted:
        return JSONResponse(
            {"code": "not_found", "message": "link not found"},
            status_code=404,
        )
    write_audit_row(
        identity=identity or "anonymous",
        action=ACTION_FLUX_UNLINKED,
        provider_account="",
        connection_ref="",
        metadata={"flux_id": flux_id, "project_id": project_id},
    )
    return JSONResponse({"unlinked": True}, status_code=200)


# --- LES ROUTES, dans l'ordre exact ou elles etaient declarees ------------
#
# Starlette resout dans l'ordre de declaration. Chaque collection est
# epissee par `admin_api` a la position que ses routes occupaient : memes
# chemins, memes methodes, meme ordre. La preuve est un dump de
# `admin_api.router.routes` avant/apres, pas une lecture de diff.

FLUX_PROJECTS_ROUTES_1 = [
    # Story 21.4: flux (app.datastreams) org-scoped + linked to N projects.
    # DELETE (with /{project_id}) declared before the shorter GET/POST shapes.
    Route(
        "/api/flux/{flux_id}/projects/{project_id}",
        endpoint=_unlink_flux_from_project,
        methods=["DELETE"],
    ),
    Route(
        "/api/flux/{flux_id}/projects",
        endpoint=_list_flux_projects,
        methods=["GET"],
    ),
    Route(
        "/api/flux/{flux_id}/projects",
        endpoint=_link_flux_to_project,
        methods=["POST"],
    ),
]
