"""toorow -- Inverse-lineage + client-label REST API (Story 27.9).

Provides DIMENSION_LINEAGE_ROUTES: list[Route] -- a flat list admin_api.py splices into
its router at startup. This module is NEVER imported by admin_api.py at module level
(no circular import -- same pattern as metric_semantics_api.py / datamodel_api.py).

Routes:
  GET    /api/dimension-lineage/fed-by   what feeds a conformed dimension, for a project
  GET    /api/dimension-lineage/labels   resolved client labels (render/narrative/LLM)
  POST   /api/dimension-lineage/labels   set a client label (ORG or PROJECT scope)
  DELETE /api/dimension-lineage/labels/{canonical_dimension}   drop a label override

Auth: the shared _check_auth from core.admin_api. Reads require org membership
(identity_has_org_access), writes require org-manage (owner/admin). PLATFORM scope is
FORBIDDEN via the API (seeds are the authority), exactly like 27.2.

STRICT PROJECT/ORG ISOLATION (lesson F-3 of 27.2): a project_id is NEVER trusted on its
own -- it is verified to belong to the guarded org before any read, otherwise 404
(existence-hiding). The core read model itself has no guard (S-4 trust contract).

French error messages (house style). ASCII-only stdout (AI-03).
"""

from __future__ import annotations

import json
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger(__name__)

_VALID_WRITE_SCOPES = frozenset({"ORG", "PROJECT"})


async def _check_auth(request: Request) -> tuple[bool, str]:
    """Delegate to the shared auth layer in core.admin_api."""
    from core.admin_api import _check_auth as _admin_check_auth  # noqa: PLC0415

    return await _admin_check_auth(request)


def _unauthorized() -> Response:
    return JSONResponse(
        {"code": "unauthorized", "message": "Authentification requise."}, status_code=401
    )


def _not_found(message: str) -> Response:
    return JSONResponse({"code": "not_found", "message": message}, status_code=404)


def _server_error() -> Response:
    return JSONResponse(
        {"code": "server_error", "message": "Erreur serveur."}, status_code=500
    )


def _guard_org_and_project(
    org_id: str | None, project_id: str | None, identity: str, *, manage: bool
) -> Response | None:
    """Return an error Response when the caller may not touch (org_id, project_id).

    Resolves the org from the project when org_id is absent, verifies membership (or
    owner/admin when *manage*), and verifies the project belongs to that org. Any
    unresolvable piece -> 404 (never a fall-through to an unguarded read/write).
    """
    from core.db import get_connection  # noqa: PLC0415
    from core.project_access import (  # noqa: PLC0415
        identity_can_manage_org,
        identity_has_org_access,
    )

    guard_org_id = org_id
    try:
        with get_connection() as conn:
            if project_id is not None:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT org_id FROM app.projects WHERE id = %s", (project_id,)
                    )
                    row = cur.fetchone()
                if not row or not row[0]:
                    return _not_found("Projet introuvable.")
                project_org_id = row[0]
                if guard_org_id is None:
                    guard_org_id = project_org_id
                elif project_org_id != guard_org_id:
                    return _not_found("Projet introuvable.")
            if guard_org_id is None:
                return _not_found("Organisation introuvable.")
            allowed = (
                identity_can_manage_org(guard_org_id, identity, conn)
                if manage
                else identity_has_org_access(guard_org_id, identity, conn)
            )
    except Exception as exc:  # noqa: BLE001
        logger.error("dimension_lineage_api: guard failed: %s", exc)
        return _server_error()

    if not allowed:
        if manage:
            return JSONResponse(
                {"code": "forbidden", "message": "Droits insuffisants."}, status_code=403
            )
        return _not_found("Organisation introuvable.")
    return None


# ---------------------------------------------------------------------------
# GET /api/dimension-lineage/fed-by
# ---------------------------------------------------------------------------


async def _fed_by(request: Request) -> Response:
    """GET /api/dimension-lineage/fed-by?project_id=&canonical_dimension=[&org_id=]

    Answers "what feeds this conformed dimension in this project?" -- one row per
    (connector, report, source field) with the mapping status and the scope where it is
    confirmed. Missing provenance is reported as 'unknown'/gaps, never invented.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()

    org_id = (request.query_params.get("org_id") or "").strip() or None
    project_id = (request.query_params.get("project_id") or "").strip() or None
    canonical_dimension = (
        request.query_params.get("canonical_dimension") or ""
    ).strip() or None

    if project_id is None:
        return JSONResponse(
            {"code": "missing_param", "message": "project_id est requis."}, status_code=400
        )
    if canonical_dimension is None:
        return JSONResponse(
            {"code": "missing_param", "message": "canonical_dimension est requis."},
            status_code=400,
        )

    guard = _guard_org_and_project(org_id, project_id, identity, manage=False)
    if guard is not None:
        return guard

    try:
        from core.dimension_lineage import get_fed_by  # noqa: PLC0415

        result = get_fed_by(
            canonical_dimension=canonical_dimension, project_id=project_id
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "dimension_lineage_api: fed_by failed project=%s: %s", project_id, exc
        )
        return _server_error()

    return JSONResponse(result)


# ---------------------------------------------------------------------------
# GET /api/dimension-lineage/labels
# ---------------------------------------------------------------------------


async def _get_labels(request: Request) -> Response:
    """GET /api/dimension-lineage/labels?org_id=[&project_id=][&dimensions=a,b]

    Returns the resolved client labels (cascade PROJECT > ORG > PLATFORM). With
    *dimensions* the answer covers exactly those identifiers, each carrying its
    label_source ('client' or 'fallback_identifier'); without it, only the dimensions
    that actually have a stored label are returned.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()

    org_id = (request.query_params.get("org_id") or "").strip() or None
    project_id = (request.query_params.get("project_id") or "").strip() or None
    raw_dimensions = (request.query_params.get("dimensions") or "").strip()
    dimensions = [d.strip() for d in raw_dimensions.split(",") if d.strip()]

    if org_id is None and project_id is None:
        return JSONResponse(
            {"code": "missing_param", "message": "org_id ou project_id est requis."},
            status_code=400,
        )

    guard = _guard_org_and_project(org_id, project_id, identity, manage=False)
    if guard is not None:
        return guard

    try:
        from core.dimension_conformance import resolve_dimension_labels  # noqa: PLC0415
        from core.dimension_lineage import build_label_map  # noqa: PLC0415

        effective_org_id = org_id
        if effective_org_id is None and project_id is not None:
            from core.dimension_conformance import _project_org_id  # noqa: PLC0415

            effective_org_id = _project_org_id(project_id)

        resolved = resolve_dimension_labels(
            org_id=effective_org_id, project_id=project_id
        )
        if dimensions:
            labels = build_label_map(dimensions, resolved)
        else:
            labels = build_label_map(list(resolved), resolved)
    except Exception as exc:  # noqa: BLE001
        logger.error("dimension_lineage_api: labels read failed org=%s: %s", org_id, exc)
        return _server_error()

    return JSONResponse(
        {"scope": {"org_id": org_id, "project_id": project_id}, "labels": labels}
    )


# ---------------------------------------------------------------------------
# POST /api/dimension-lineage/labels
# ---------------------------------------------------------------------------


async def _set_label(request: Request) -> Response:
    """POST /api/dimension-lineage/labels

    body: {"scope_level": "ORG"|"PROJECT", "org_id"?, "project_id"?,
           "canonical_dimension": "...", "display_label": "...", "description"?}

    Names a dimension in the client's own words. The stable identifier is NEVER changed.
    Auth: org-manage. PLATFORM is refused (seeds are the authority).
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()

    try:
        body = json.loads(await request.body())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse(
            {"code": "invalid_json", "message": "Corps JSON invalide."}, status_code=400
        )
    if not isinstance(body, dict):
        return JSONResponse(
            {"code": "invalid_json", "message": "Corps JSON invalide."}, status_code=400
        )

    scope_level = (body.get("scope_level") or "").strip().upper()
    if scope_level == "PLATFORM":
        return JSONResponse(
            {
                "code": "forbidden",
                "message": "La portee PLATFORM ne peut pas etre modifiee via l'API.",
            },
            status_code=403,
        )
    if scope_level not in _VALID_WRITE_SCOPES:
        return JSONResponse(
            {"code": "invalid_param", "message": "scope_level doit etre ORG ou PROJECT."},
            status_code=422,
        )

    org_id = (body.get("org_id") or "").strip() or None
    project_id = (body.get("project_id") or "").strip() or None
    canonical_dimension = (body.get("canonical_dimension") or "").strip() or None
    display_label = (body.get("display_label") or "").strip() or None

    if canonical_dimension is None or display_label is None:
        return JSONResponse(
            {
                "code": "missing_param",
                "message": "canonical_dimension et display_label sont requis.",
            },
            status_code=422,
        )
    if scope_level == "PROJECT" and project_id is None:
        return JSONResponse(
            {"code": "missing_param", "message": "project_id est requis."},
            status_code=422,
        )
    if scope_level == "ORG" and org_id is None:
        return JSONResponse(
            {"code": "missing_param", "message": "org_id est requis."}, status_code=422
        )

    guard = _guard_org_and_project(org_id, project_id, identity, manage=True)
    if guard is not None:
        return guard

    try:
        from core.dimension_conformance import (  # noqa: PLC0415
            InvalidScope,
            set_dimension_label,
        )

        row = set_dimension_label(
            canonical_dimension=canonical_dimension,
            display_label=display_label,
            description=(body.get("description") or None),
            scope_level=scope_level,
            org_id=org_id if scope_level == "ORG" else None,
            project_id=project_id if scope_level == "PROJECT" else None,
            identity=identity,
        )
    except (InvalidScope, ValueError) as exc:
        # Never propagate the internal text (it can leak scope/id internals), S-1 of 27.2.
        logger.warning("dimension_lineage_api: set_label rejected: %s", exc)
        return JSONResponse(
            {"code": "invalid_scope", "message": "Scope invalide."}, status_code=422
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("dimension_lineage_api: set_label failed: %s", exc)
        return _server_error()

    return JSONResponse(row, status_code=201)


# ---------------------------------------------------------------------------
# DELETE /api/dimension-lineage/labels/{canonical_dimension}
# ---------------------------------------------------------------------------


async def _delete_label(request: Request) -> Response:
    """DELETE /api/dimension-lineage/labels/{canonical_dimension}
       ?scope_level=ORG|PROJECT[&org_id=][&project_id=]

    Drops one label override; the less specific scope (or the identifier fallback)
    becomes visible again. Auth: org-manage. PLATFORM refused.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()

    canonical_dimension = (request.path_params.get("canonical_dimension") or "").strip()
    scope_level = (request.query_params.get("scope_level") or "").strip().upper()
    org_id = (request.query_params.get("org_id") or "").strip() or None
    project_id = (request.query_params.get("project_id") or "").strip() or None

    if scope_level == "PLATFORM":
        return JSONResponse(
            {
                "code": "forbidden",
                "message": "La portee PLATFORM ne peut pas etre supprimee via l'API.",
            },
            status_code=403,
        )
    if scope_level not in _VALID_WRITE_SCOPES:
        return JSONResponse(
            {"code": "invalid_param", "message": "scope_level doit etre ORG ou PROJECT."},
            status_code=422,
        )
    if not canonical_dimension:
        return _not_found("Libelle introuvable.")

    guard = _guard_org_and_project(org_id, project_id, identity, manage=True)
    if guard is not None:
        return guard

    try:
        from core.dimension_conformance import (  # noqa: PLC0415
            InvalidScope,
            delete_dimension_label,
        )

        deleted = delete_dimension_label(
            canonical_dimension=canonical_dimension,
            scope_level=scope_level,
            org_id=org_id if scope_level == "ORG" else None,
            project_id=project_id if scope_level == "PROJECT" else None,
            identity=identity,
        )
    except InvalidScope as exc:
        logger.warning("dimension_lineage_api: delete_label rejected: %s", exc)
        return JSONResponse(
            {"code": "invalid_scope", "message": "Scope invalide."}, status_code=422
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("dimension_lineage_api: delete_label failed: %s", exc)
        return _server_error()

    if not deleted:
        return _not_found("Libelle introuvable.")
    return JSONResponse({"deleted": True})


# ---------------------------------------------------------------------------
# Route list (static paths before the parametrized one).
# ---------------------------------------------------------------------------

DIMENSION_LINEAGE_ROUTES: list[Route] = [
    Route("/api/dimension-lineage/fed-by", _fed_by, methods=["GET"]),
    Route("/api/dimension-lineage/labels", _get_labels, methods=["GET"]),
    Route("/api/dimension-lineage/labels", _set_label, methods=["POST"]),
    Route(
        "/api/dimension-lineage/labels/{canonical_dimension}",
        _delete_label,
        methods=["DELETE"],
    ),
]
