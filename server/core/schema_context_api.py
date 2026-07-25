"""toorow -- Schema Context Generator REST route (Story 11.2, fix pass).

Provides SCHEMA_CONTEXT_ROUTES: list[Route] -- spliced into admin_api.router at
startup (NOT into 11.1's CONTEXT_ROUTES).

Route:
  POST /api/admin/context/generate-schema-context -> run the generator synchronously

ADMIN-only gate (CRITICAL fix): a full-warehouse schema-context regeneration is
expensive; a member-gated regen would be a DoS vector. This handler reuses the
deny-by-default platform-write gate (core.context_api.check_platform_write_authorized)
so only platform administrators (CONTEXT_PLATFORM_WRITERS, or disabled-auth dev
mode) may trigger a run.

AD-17: this route calls the single-writer generator (schema_context_gen). No other
REST/CRUD path in the codebase writes app.schema_context or app.schema_context_versions.
"""

from __future__ import annotations

import logging
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger(__name__)


async def _check_auth(request: Request) -> tuple[bool, str]:
    """Delegate to the shared admin auth layer."""
    from core.admin_api import _check_auth as _admin_check_auth  # noqa: PLC0415

    return await _admin_check_auth(request)


def _check_admin_authorized(conn: Any, identity: str) -> bool:
    """ADMIN-only gate: reuse the deny-by-default platform-write allowlist.

    Delegates to core.context_api.check_platform_write_authorized so the policy
    (CONTEXT_PLATFORM_WRITERS env allowlist, or anonymous in disabled-auth dev
    mode) is shared with the rest of the platform-scope write surface.
    """
    from core.context_api import check_platform_write_authorized  # noqa: PLC0415

    return check_platform_write_authorized(conn, identity)


async def _generate_schema_context(request: Request) -> Response:
    """POST /api/admin/context/generate-schema-context.

    Body: {"project_id": str, "allowlist": [str, ...] | null}
    Returns: {"processed": N, "updated": U, "skipped": S, "errors": []}
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentification requise"},
            status_code=401,
        )

    try:
        body = await request.json()
    except Exception:
        body = {}

    project_id = (body.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse(
            {"code": "invalid_param", "message": "project_id requis"}, status_code=422
        )

    raw_allowlist = body.get("allowlist")
    allowlist: list[str] | None = None
    if raw_allowlist is not None:
        if not isinstance(raw_allowlist, list):
            return JSONResponse(
                {"code": "invalid_param", "message": "allowlist doit être une liste"},
                status_code=422,
            )
        allowlist = [str(x) for x in raw_allowlist]

    from core.db import get_connection, get_warehouse_connection  # noqa: PLC0415
    from core.schema_context_gen import generate_schema_context  # noqa: PLC0415

    try:
        with get_connection() as conn:
            # ADMIN-only gate: a member-gated full-warehouse regen would be a DoS.
            if not _check_admin_authorized(conn, identity):
                logger.warning(
                    "schema_context_api: generate denied for identity=%s project=%s",
                    identity,
                    project_id,
                )
                return JSONResponse(
                    {"code": "forbidden", "message": "Accès administrateur requis"},
                    status_code=403,
                )

            # AD-8: warehouse opened read-only (structural enforcement).
            with get_warehouse_connection(read_only=True) as warehouse_conn:
                result = generate_schema_context(
                    conn,
                    project_id=project_id,
                    warehouse_conn=warehouse_conn,
                    allowlist=allowlist,
                    changed_by=identity,
                )
            return JSONResponse(result, status_code=200)
    except Exception as exc:
        logger.warning("schema_context_api: generate error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Erreur lors de la génération du contexte de schéma"},
            status_code=500,
        )


SCHEMA_CONTEXT_ROUTES: list[Route] = [
    Route(
        "/api/admin/context/generate-schema-context",
        endpoint=_generate_schema_context,
        methods=["POST"],
    ),
]
