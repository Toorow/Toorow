"""The Dossier doors: request parsing, authorization, and calls into the store.

Story 73-1. Same seam discipline as `analyze_artifacts_api` (whose helpers this
module reuses rather than re-deciding): `viewer` reads, `member` writes, the
org is resolved once, refusals are the store's own words.
"""

from __future__ import annotations

import json
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core.analyze_artifacts import ArtifactNotFound, ArtifactRefused
from core.analyze_artifacts_api import _NOT_FOUND, _authorize, _json_body, _refused
from core.dossiers import append_dossier_version, create_dossier, get_dossier, list_dossiers
from core.query_specs_api import analyze_connection

logger = logging.getLogger("uvicorn.error")

_BASE = "/api/projects/{project_id}/analyze"


async def _list_dossiers(request: Request) -> Response:
    """GET {base}/dossiers -- every live Dossier of the Project."""
    auth = await _authorize(request, "viewer")
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth
    project_id = request.path_params["project_id"]
    with analyze_connection(identity) as conn:
        payload = {"dossiers": list_dossiers(conn, org_id=org_id, project_id=project_id)}
    return JSONResponse(payload)


async def _create_dossier(request: Request) -> Response:
    """POST {base}/dossiers -- a head plus its version 1, in one act."""
    auth = await _authorize(request, "member")
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth
    project_id = request.path_params["project_id"]
    try:
        body = await _json_body(request)
    except (ValueError, json.JSONDecodeError) as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)
    try:
        with analyze_connection(identity) as conn:
            created = create_dossier(
                conn,
                org_id=org_id,
                project_id=project_id,
                label=body.get("label") or "",
                description=body.get("description"),
                blocks=body.get("blocks"),
                actor=identity,
            )
            conn.commit()
    except ArtifactNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    except ArtifactRefused as exc:
        return _refused(exc)
    return JSONResponse(created, 201)


async def _get_dossier(request: Request) -> Response:
    """GET {base}/dossiers/{id} -- head, versions, current blocks RESOLVED."""
    auth = await _authorize(request, "viewer")
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth
    project_id = request.path_params["project_id"]
    dossier_id = request.path_params["dossier_id"]
    try:
        with analyze_connection(identity) as conn:
            payload = get_dossier(
                conn, org_id=org_id, project_id=project_id, dossier_id=dossier_id
            )
    except ArtifactNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    return JSONResponse(payload)


async def _append_dossier_version(request: Request) -> Response:
    """POST {base}/dossiers/{id}/versions -- succeed, never edit in place."""
    auth = await _authorize(request, "member")
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth
    project_id = request.path_params["project_id"]
    dossier_id = request.path_params["dossier_id"]
    try:
        body = await _json_body(request)
    except (ValueError, json.JSONDecodeError) as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)
    try:
        with analyze_connection(identity) as conn:
            version = append_dossier_version(
                conn,
                org_id=org_id,
                project_id=project_id,
                dossier_id=dossier_id,
                actor=identity,
                blocks=body.get("blocks"),
                label=body.get("label"),
                description=body.get("description"),
            )
            conn.commit()
    except ArtifactNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    except ArtifactRefused as exc:
        return _refused(exc)
    return JSONResponse(version, 201)


DOSSIER_ROUTES = [
    Route(f"{_BASE}/dossiers", _list_dossiers, methods=["GET"]),
    Route(f"{_BASE}/dossiers", _create_dossier, methods=["POST"]),
    Route(f"{_BASE}/dossiers/{{dossier_id}}", _get_dossier, methods=["GET"]),
    Route(
        f"{_BASE}/dossiers/{{dossier_id}}/versions",
        _append_dossier_version,
        methods=["POST"],
    ),
]
