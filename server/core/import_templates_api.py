"""toorow -- Import template catalog and inbound Datastream REST surface (Story 38.6).

Admin REST surface for the import template catalog and inbound Datastream creation:

  GET  /api/connectors/{connector_name}/templates
       -- Org-member read; returns the template catalog (safe read-model:
          code, version, title, required/optional fields, is_generic). No secret.

  POST /api/connectors/{connector_name}/datastreams
       -- Org-member with at least project-member authority; Idempotency-Key
          required; creates an inbound managed-feed Datastream bound to a
          versioned template + enabled channels. Nondisclosing 404 for access
          failures (AD-5). The created Datastream binding lives on
          app.datastreams.config (AC5: no second ledger).

GATING (nondisclosing 404-not-403, AD-5):
  * Bearer token required -> 401 when absent/invalid.
  * GET: authenticated caller; project-member check.
  * POST: caller must have project-member access (minimum 'member' role).
  * A caller without project access receives a nondisclosing 404.

IDEMPOTENCY:
  * POST requires Idempotency-Key header (422 when absent).
  * Conflicting key/payload -> 409.

Source-agnostic: connector referenced only by connector_name string (AD-2).
No provider/vendor vocabulary in this module. ASCII-only strings. Lazy imports.
"""

from __future__ import annotations

import json
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared helpers (mirrors connector_activation_api.py).
# ---------------------------------------------------------------------------

_NOT_FOUND = {"code": "not_found", "message": "Resource not found"}


async def _check_auth(request: Request) -> tuple[bool, str]:
    from core.api_auth import authenticate_api_request  # noqa: PLC0415

    return await authenticate_api_request(request)


def _idempotency_key(request: Request) -> str | None:
    """Extract and return the Idempotency-Key header value, or None."""
    return request.headers.get("Idempotency-Key") or None


def _trace_id(request: Request) -> str | None:
    """Extract a validated X-Trace-Id header or return None."""
    import re  # noqa: PLC0415

    raw = request.headers.get("X-Trace-Id", "").strip()
    if raw and re.fullmatch(r"[0-9a-f]{32}", raw):
        return raw
    return None


def _check_project_member(project_id: str, identity: str, conn) -> bool:
    """Return True when identity has at least member access to project_id."""
    from core.project_access import identity_has_project_access  # noqa: PLC0415

    try:
        return identity_has_project_access(project_id, identity, conn)
    except Exception as exc:  # noqa: BLE001
        logger.error("import_templates_api: project access check failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# GET /api/connectors/{connector_name}/templates
# ---------------------------------------------------------------------------


async def _get_templates(request: Request) -> Response:
    """GET /api/connectors/{connector_name}/templates

    Returns the full template catalog (safe read-model). No secret.
    Authenticated callers see the catalog; the connector_name is accepted
    but not used as a filter (all templates are platform-wide reference data).
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.import_templates import list_templates  # noqa: PLC0415

        with get_connection() as conn:
            templates = list_templates(conn)
    except Exception as exc:
        logger.error("import_templates_api: GET /templates error: %s", exc)
        return JSONResponse(
            {"code": "server_error", "message": "Template catalog unavailable"},
            status_code=500,
        )

    return JSONResponse({"templates": templates}, status_code=200)


# ---------------------------------------------------------------------------
# POST /api/connectors/{connector_name}/datastreams
# ---------------------------------------------------------------------------


async def _post_inbound_datastream(request: Request) -> Response:
    """POST /api/connectors/{connector_name}/datastreams

    Creates an inbound managed-feed Datastream bound to a versioned template
    and one or more enabled channels ({email, webhook, upload}).

    Body (JSON):
      project_id        (required) -- the project to create the Datastream in.
      name              (required) -- human label for the Datastream.
      template_code     (required) -- template catalog code.
      template_version  (required) -- exact pinned version (int).
      channels          (required) -- list of channel strings.
      org_id            (required) -- organisation scope for the audit/operation.

    Idempotency-Key header is required (422 when absent). The same key with the
    same payload replays cleanly. Conflicting payload -> 409.

    Access: caller must be a project member (at least 'member' role).
    Non-member / unknown project -> nondisclosing 404.
    """
    from core.operations import OperationIdempotencyConflict  # noqa: PLC0415

    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    connector_name = (request.path_params.get("connector_name") or "").strip()
    if not connector_name:
        return JSONResponse(
            {"code": "missing_param", "message": "connector_name is required"},
            status_code=400,
        )

    idempotency_key = _idempotency_key(request)
    if not idempotency_key:
        return JSONResponse(
            {"code": "missing_header", "message": "Idempotency-Key header is required"},
            status_code=422,
        )

    try:
        body_bytes = await request.body()
        body: dict = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception as exc:
        return JSONResponse(
            {"code": "invalid_body", "message": f"Invalid JSON body: {exc}"},
            status_code=400,
        )

    project_id = (body.get("project_id") or "").strip()
    name = (body.get("name") or "").strip()
    template_code = (body.get("template_code") or "").strip()
    org_id = (body.get("org_id") or "").strip()
    channels = body.get("channels") or []

    if not project_id:
        return JSONResponse(
            {"code": "missing_param", "message": "project_id is required"},
            status_code=400,
        )
    if not name:
        return JSONResponse(
            {"code": "missing_param", "message": "name is required"},
            status_code=400,
        )
    if not template_code:
        return JSONResponse(
            {"code": "missing_param", "message": "template_code is required"},
            status_code=400,
        )
    if not org_id:
        return JSONResponse(
            {"code": "missing_param", "message": "org_id is required"},
            status_code=400,
        )

    try:
        template_version = int(body.get("template_version") or 0)
        if template_version < 1:
            raise ValueError("must be >= 1")
    except (TypeError, ValueError):
        return JSONResponse(
            {"code": "invalid_param", "message": "template_version must be a positive integer"},
            status_code=400,
        )

    if not isinstance(channels, list) or not channels:
        return JSONResponse(
            {"code": "missing_param", "message": "channels must be a non-empty list"},
            status_code=400,
        )

    trace = _trace_id(request)

    # Access gate: caller must be a project member (nondisclosing 404 on failure).
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            if not _check_project_member(project_id, identity, conn):
                return JSONResponse(_NOT_FOUND, status_code=404)
    except Exception as exc:
        logger.error(
            "import_templates_api: project access check failed project=%s: %s",
            project_id,
            exc,
        )
        return JSONResponse(_NOT_FOUND, status_code=404)

    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.import_templates import (  # noqa: PLC0415
            TemplateChannelError,
            TemplateNotFound,
            create_inbound_datastream,
        )

        with get_connection() as conn:
            datastream = create_inbound_datastream(
                conn,
                project_id=project_id,
                name=name,
                template_code=template_code,
                template_version=template_version,
                channels=channels,
                created_by=identity,
                org_id=org_id,
                idempotency_key=idempotency_key,
                host_context={},
                trace_id=trace,
            )
            conn.commit()
    except TemplateNotFound as exc:
        return JSONResponse(
            {"code": "template_not_found", "message": str(exc)},
            status_code=422,
        )
    except TemplateChannelError as exc:
        return JSONResponse(
            {"code": "invalid_channels", "message": str(exc)},
            status_code=422,
        )
    except OperationIdempotencyConflict:
        return JSONResponse(
            {
                "code": "conflict",
                "message": "Idempotency-Key already bound to a different datastream request",
            },
            status_code=409,
        )
    except Exception as exc:
        logger.error(
            "import_templates_api: POST /datastreams error cn=%s project=%s: %s",
            connector_name,
            project_id,
            exc,
        )
        return JSONResponse(
            {"code": "server_error", "message": "Datastream creation failed"},
            status_code=500,
        )

    return JSONResponse(
        {
            "connector_name": connector_name,
            "project_id": project_id,
            "datastream": datastream,
        },
        status_code=201,
    )


# ---------------------------------------------------------------------------
# Route table.
# ---------------------------------------------------------------------------

IMPORT_TEMPLATE_ROUTES: list[Route] = [
    Route(
        "/api/connectors/{connector_name}/templates",
        endpoint=_get_templates,
        methods=["GET"],
    ),
    Route(
        "/api/connectors/{connector_name}/datastreams",
        endpoint=_post_inbound_datastream,
        methods=["POST"],
    ),
]
