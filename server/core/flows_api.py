"""toorow -- Flow REST route handlers (Story 8.7, Epic 8 Part D).

Provides FLOWS_ROUTES: list[Route] -- the REST MIRROR of the MCP flow tools. Both
channels (REST here, MCP in core.main) call the SAME core.flows functions, so the
admin UI and the LLM agent share one validation + write + audit path (the "common
interface" invariant from Part D).

Routes:
  GET  /api/flows?project_id=&kind=          list flow summaries
  GET  /api/flows/{kind}/{id}                full declarative doc (merged for reports)
  POST /api/flows/validate                   dry-run validation (no DB touch)
  PUT  /api/flows                            validate + upsert (body: {project_id, definition})

Auth: shared _check_auth from core.admin_api (Bearer via core.api_auth). Identity
from the token drives AD-5 scoping + audit inside core.flows.
AD-8: the admin console communicates through this REST layer only.
French error messages throughout (Epic 8 Part B).

This module is NEVER imported by admin_api.py at module level (no circular import);
the orchestrator splices FLOWS_ROUTES into admin_api.router at startup.

ASCII-only stdout (AI-03). No private framework attributes (AI-02).
"""

from __future__ import annotations

import json
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Auth helper -- import from admin_api (safe: admin_api never imports us)
# ---------------------------------------------------------------------------


async def _check_auth(request: Request) -> tuple[bool, str]:
    """Delegate to the shared auth layer in core.admin_api."""
    from core.admin_api import _check_auth as _admin_check_auth  # noqa: PLC0415

    return await _admin_check_auth(request)


def _unauthorized() -> Response:
    return JSONResponse(
        {"code": "unauthorized", "message": "Authentification requise"},
        status_code=401,
    )


def _loaded_modules():
    """Best-effort access to the loaded-module registry (for report base packs)."""
    try:
        from core.main import get_loaded_modules  # noqa: PLC0415

        return get_loaded_modules()
    except Exception:  # pragma: no cover - registry unavailable in some unit contexts
        return []


# ---------------------------------------------------------------------------
# GET /api/flows
# ---------------------------------------------------------------------------


async def _list_flows(request: Request) -> Response:
    """GET /api/flows?project_id=&kind= -- list flow summaries.

    Error responses: 400 (missing project_id), 401, 404 (scope), 422 (bad kind), 500.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()

    project_id = (request.query_params.get("project_id") or "").strip()
    kind = (request.query_params.get("kind") or "").strip() or None
    if not project_id:
        return JSONResponse(
            {"code": "missing_param", "message": "project_id est requis"},
            status_code=400,
        )
    if kind is not None and kind not in ("datastream", "report"):
        return JSONResponse(
            {"code": "invalid_param", "message": "kind doit etre 'datastream' ou 'report'"},
            status_code=422,
        )

    from core.flows import FlowScopeError, list_flows  # noqa: PLC0415

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            flows = list_flows(project_id, identity or "anonymous", conn, kind=kind)
    except FlowScopeError:
        return JSONResponse({"code": "not_found", "message": "Projet introuvable"}, status_code=404)
    except Exception as exc:
        logger.error("flows_api: list_flows_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Erreur de base de donnees : {exc}"},
            status_code=500,
        )

    return JSONResponse(flows)


# ---------------------------------------------------------------------------
# GET /api/flows/{kind}/{id}
# ---------------------------------------------------------------------------


async def _get_flow(request: Request) -> Response:
    """GET /api/flows/{kind}/{id}?project_id= -- full declarative doc.

    Error responses: 400, 401, 404 (not found / scope), 422 (bad kind), 500.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()

    kind = request.path_params.get("kind", "").strip()
    flow_id = request.path_params.get("id", "").strip()
    project_id = (request.query_params.get("project_id") or "").strip()

    if kind not in ("datastream", "report"):
        return JSONResponse(
            {"code": "invalid_param", "message": "kind doit etre 'datastream' ou 'report'"},
            status_code=422,
        )
    if not project_id:
        return JSONResponse(
            {"code": "missing_param", "message": "project_id est requis"},
            status_code=400,
        )
    if not flow_id:
        return JSONResponse({"code": "missing_param", "message": "id est requis"}, status_code=400)

    from core.flows import FlowScopeError, get_flow  # noqa: PLC0415

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            flow = get_flow(
                project_id,
                kind,
                flow_id,
                identity or "anonymous",
                conn,
                loaded_modules=_loaded_modules(),
            )
    except FlowScopeError:
        return JSONResponse({"code": "not_found", "message": "Flux introuvable"}, status_code=404)
    except Exception as exc:
        logger.error("flows_api: get_flow_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Erreur de base de donnees : {exc}"},
            status_code=500,
        )

    if flow is None:
        return JSONResponse(
            {"code": "not_found", "message": f"Flux '{kind}/{flow_id}' introuvable"},
            status_code=404,
        )
    return JSONResponse(flow)


# ---------------------------------------------------------------------------
# POST /api/flows/validate
# ---------------------------------------------------------------------------


async def _validate_flow(request: Request) -> Response:
    """POST /api/flows/validate -- dry-run validation. No DB touch, no scope check.

    Body: the flow definition object (or {"definition": {...}}).
    Response (200): {"ok": bool, "errors": [{"path", "message"}]}.
    """
    authorized, _identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()

    try:
        body_bytes = await request.body()
        body: dict = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception as exc:
        return JSONResponse(
            {"code": "invalid_body", "message": f"Corps JSON invalide : {exc}"},
            status_code=400,
        )

    definition = body.get("definition") if isinstance(body, dict) and "definition" in body else body

    from core.flows import validate_flow  # noqa: PLC0415

    ok, errors = validate_flow(definition)
    return JSONResponse({"ok": ok, "errors": errors})


# ---------------------------------------------------------------------------
# PUT /api/flows
# ---------------------------------------------------------------------------


async def _upsert_flow(request: Request) -> Response:
    """PUT /api/flows -- validate + upsert a flow.

    Body: {"project_id": str, "definition": {...}}.
    Response (200): {"kind", "id", "changed", "flow", "diff"}.

    Error responses: 400 (body/project_id), 401, 404 (scope), 409 (name conflict),
    422 (validation), 500.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()

    try:
        body_bytes = await request.body()
        body: dict = json.loads(body_bytes)
    except Exception as exc:
        return JSONResponse(
            {"code": "invalid_body", "message": f"Corps JSON invalide : {exc}"},
            status_code=400,
        )

    project_id = (body.get("project_id") or "").strip()
    definition = body.get("definition")
    if not project_id:
        return JSONResponse(
            {"code": "missing_param", "message": "project_id est requis"},
            status_code=400,
        )
    if not isinstance(definition, dict):
        return JSONResponse(
            {"code": "missing_param", "message": "definition (objet) est requis"},
            status_code=400,
        )
    if definition.get("kind") == "datastream" and definition.get("schema_version") == "2":
        definition = dict(definition)
        header_key = (request.headers.get("Idempotency-Key") or "").strip()
        body_key = (definition.get("idempotency_key") or "").strip()
        if header_key and body_key and header_key != body_key:
            return JSONResponse(
                {
                    "code": "idempotency_conflict",
                    "message": "Idempotency-Key differe de la definition.",
                },
                status_code=409,
            )
        if header_key:
            definition["idempotency_key"] = header_key

    from core.flows import (  # noqa: PLC0415
        FlowConflictError,
        FlowScopeError,
        FlowUnavailableError,
        FlowValidationError,
        upsert_flow,
    )

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            result = upsert_flow(
                project_id,
                definition,
                identity or "anonymous",
                conn,
                loaded_modules=_loaded_modules(),
            )
    except FlowValidationError as exc:
        return JSONResponse(
            {"code": "validation_error", "message": str(exc), "errors": exc.errors},
            status_code=422,
        )
    except FlowScopeError:
        return JSONResponse({"code": "not_found", "message": "Flux introuvable"}, status_code=404)
    except FlowConflictError as exc:
        return JSONResponse({"code": "conflict", "message": str(exc)}, status_code=409)
    except FlowUnavailableError as exc:
        return JSONResponse({"code": "unavailable", "message": str(exc)}, status_code=503)
    except Exception as exc:
        logger.error("flows_api: upsert_flow_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Erreur de base de donnees : {exc}"},
            status_code=500,
        )

    return JSONResponse(result)


# ---------------------------------------------------------------------------
# Exported route list (orchestrator wires into admin_api.router)
# ---------------------------------------------------------------------------

FLOWS_ROUTES: list[Route] = [
    # Static routes MUST precede the parametrized /{kind}/{id} route so Starlette
    # matches list/validate/upsert first.
    Route("/api/flows/validate", endpoint=_validate_flow, methods=["POST"]),
    Route("/api/flows", endpoint=_list_flows, methods=["GET"]),
    Route("/api/flows", endpoint=_upsert_flow, methods=["PUT"]),
    Route("/api/flows/{kind}/{id}", endpoint=_get_flow, methods=["GET"]),
]
