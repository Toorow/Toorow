"""toorow -- Inbound health and import inbox REST surface (Story 38.14).

The three reads that did not exist:

  GET /api/connectors/{connector_name}/health
      -- layered health. Optional ?datastream_id= adds the delivery and data
         layers plus counters.

  GET /api/connectors/{connector_name}/datastreams/{datastream_id}/inbox
      -- one row PER ATTACHMENT, newest first, with its scan verdict and its
         delivery beside it. This is the surface an operator opens when a file
         "did not arrive".

  GET /api/connectors/{connector_name}/datastreams/{datastream_id}/deliveries/{receipt_id}
      -- one delivery's full timeline across its attachments.

  POST /api/connectors/{connector_name}/datastreams/{datastream_id}/routing-test
      -- Story 38.15 AC5. Walks the routing chain for one Datastream and writes
         NOTHING: the answer to "if a provider sends now, does it reach me?"
         asked before anyone sends. POST because it spends one resolution
         rate-limit event; see ``inbound_routing_test``.

GATING, identical to the credential surface so there is one authorization story
for the connector rather than two:
  * Bearer token required -> 401 when absent or invalid.
  * ``view`` capability on the Datastream for the Datastream-scoped reads.
  * Any authorization failure returns the NONDISCLOSING 404, never a 403: a 403
    would confirm that the resource exists (AD-5, E38-NFR01).

WHAT THIS SURFACE NEVER RETURNS, enforced upstream in ``inbound_health`` rather
than filtered here: tokens, signing secrets, recipient addresses, row samples,
raw bytes. The handlers below add no field of their own to the read model --
they serialise it. That is deliberate: a handler that reshapes is a handler that
can leak something the read model was careful to exclude, and it is how REST and
MCP drift into describing the same delivery differently.

Source-agnostic (AD-2): the connector is a name, no vendor vocabulary appears.
ASCII-only source. Lazy imports.
"""

from __future__ import annotations

import logging
import os
import re
import secrets

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger(__name__)

_NOT_FOUND: dict = {"code": "not_found", "message": "Resource not found"}


def _get_environment() -> str:
    return os.environ.get("TOOROW_ENVIRONMENT", "production").strip() or "production"


async def _check_auth(request: Request) -> tuple[bool, str]:
    from core.api_auth import authenticate_api_request  # noqa: PLC0415

    return await authenticate_api_request(request)


def _check_datastream_access(
    datastream_id: str, identity: str, *, minimum_capability: str = "view"
) -> bool | None:
    """Mirror of the credential surface's guard -- same rules, same failure mode.

    Returns None on a database error, which the caller maps to the nondisclosing
    404 exactly like a denial: an access check that could not run has NOT
    granted access.
    """
    auth_mode = os.environ.get("TOOROW_AUTH_MODE", "disabled").strip().lower()
    if auth_mode == "disabled":
        return True
    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.project_access import resolve_strict_resource_access  # noqa: PLC0415

        with get_connection() as conn:
            decision = resolve_strict_resource_access(
                identity,
                conn,
                datastream_id=datastream_id,
                minimum_capability=minimum_capability,
            )
        return decision.allowed
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "inbound_health_api: access check failed ds=%s: %s",
            datastream_id,
            type(exc).__name__,
        )
        return None


def _resolve_org_for_datastream(conn, datastream_id: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT p.org_id FROM app.datastreams d "
            "JOIN app.projects p ON p.id = d.project_id WHERE d.id = %s",
            (datastream_id,),
        )
        row = cur.fetchone()
    return row[0] if row and row[0] else None


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------


async def _get_health(request: Request) -> Response:
    """Layered connector health. ?datastream_id= adds the tenant-scoped layers."""
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

    datastream_id = (request.query_params.get("datastream_id") or "").strip() or None
    if datastream_id:
        # A Datastream-scoped read needs Datastream-scoped authority. Without
        # this, the platform-level layers would be a side door onto whether a
        # given Datastream exists.
        access = _check_datastream_access(datastream_id, identity)
        if not access:
            return JSONResponse(_NOT_FOUND, status_code=404)

    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.inbound_health import get_inbound_health  # noqa: PLC0415

        with get_connection() as conn:
            org_id = _resolve_org_for_datastream(conn, datastream_id) if datastream_id else None
            health = get_inbound_health(
                conn,
                connector_name=connector_name,
                environment=_get_environment(),
                datastream_id=datastream_id,
                org_id=org_id,
            )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "inbound_health_api: health read failed connector=%s: %s",
            connector_name,
            type(exc).__name__,
        )
        return JSONResponse(
            {"code": "server_error", "message": "Health unavailable"},
            status_code=500,
        )

    return JSONResponse(health, status_code=200)


# ---------------------------------------------------------------------------
# GET /inbox
# ---------------------------------------------------------------------------


async def _get_inbox(request: Request) -> Response:
    """One row per ATTACHMENT -- including the deliveries that never landed."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    datastream_id = (request.path_params.get("datastream_id") or "").strip()
    if not datastream_id:
        return JSONResponse(
            {"code": "missing_param", "message": "datastream_id is required"},
            status_code=400,
        )

    access = _check_datastream_access(datastream_id, identity)
    if not access:
        return JSONResponse(_NOT_FOUND, status_code=404)

    try:
        limit = int(request.query_params.get("limit", "50"))
        offset = int(request.query_params.get("offset", "0"))
    except ValueError:
        return JSONResponse(
            {"code": "invalid_param", "message": "limit and offset must be integers"},
            status_code=400,
        )

    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.inbound_health import get_attachment_inbox  # noqa: PLC0415

        with get_connection() as conn:
            items = get_attachment_inbox(
                conn, datastream_id=datastream_id, limit=limit, offset=offset
            )
    except ValueError as exc:
        # The page bounds are the caller's mistake, and saying which is useful.
        return JSONResponse({"code": "invalid_param", "message": str(exc)}, status_code=400)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "inbound_health_api: inbox read failed ds=%s: %s",
            datastream_id,
            type(exc).__name__,
        )
        return JSONResponse(
            {"code": "server_error", "message": "Inbox unavailable"},
            status_code=500,
        )

    return JSONResponse(
        {"items": items, "count": len(items), "limit": limit, "offset": offset},
        status_code=200,
    )


# ---------------------------------------------------------------------------
# GET /deliveries/{receipt_id}
# ---------------------------------------------------------------------------


async def _get_delivery(request: Request) -> Response:
    """One delivery's timeline across all of its attachments."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    datastream_id = (request.path_params.get("datastream_id") or "").strip()
    receipt_id = (request.path_params.get("receipt_id") or "").strip()
    if not datastream_id or not receipt_id:
        return JSONResponse(
            {
                "code": "missing_param",
                "message": "datastream_id and receipt_id are required",
            },
            status_code=400,
        )

    access = _check_datastream_access(datastream_id, identity)
    if not access:
        return JSONResponse(_NOT_FOUND, status_code=404)

    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.inbound_health import get_delivery_timeline  # noqa: PLC0415

        with get_connection() as conn:
            timeline = get_delivery_timeline(
                conn, receipt_id=receipt_id, datastream_id=datastream_id
            )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "inbound_health_api: delivery read failed ds=%s: %s",
            datastream_id,
            type(exc).__name__,
        )
        return JSONResponse(
            {"code": "server_error", "message": "Delivery unavailable"},
            status_code=500,
        )

    if timeline is None:
        # A receipt belonging to another Datastream is indistinguishable from
        # one that does not exist. That is the point.
        return JSONResponse(_NOT_FOUND, status_code=404)

    return JSONResponse(timeline, status_code=200)


# ---------------------------------------------------------------------------
# GET /raw-imports/{raw_import_id}/mapping-context   (Stories 38.16 / 38.17)
# ---------------------------------------------------------------------------


async def _get_mapping_context(request: Request) -> Response:
    """What the failed file contained, and the governed path to repair it.

    A read: it re-parses retained bytes and writes nothing. `view` is enough,
    because seeing why a file failed is inspection -- the repair itself goes
    through the governed change path this response points at, which has its own
    authorization.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    datastream_id = (request.path_params.get("datastream_id") or "").strip()
    raw_import_id = (request.path_params.get("raw_import_id") or "").strip()
    if not datastream_id or not raw_import_id:
        return JSONResponse(
            {
                "code": "missing_param",
                "message": "datastream_id and raw_import_id are required",
            },
            status_code=400,
        )

    if not _check_datastream_access(datastream_id, identity):
        return JSONResponse(_NOT_FOUND, status_code=404)

    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.inbound_mapping_entry import (  # noqa: PLC0415
            get_mapping_repair_context,
        )

        with get_connection() as conn:
            context = get_mapping_repair_context(
                conn, raw_import_id=raw_import_id, datastream_id=datastream_id
            )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "inbound_health_api: mapping context failed ds=%s: %s",
            datastream_id,
            type(exc).__name__,
        )
        return JSONResponse(
            {"code": "server_error", "message": "Mapping context unavailable"},
            status_code=500,
        )

    return JSONResponse(context, status_code=200)


async def _post_scan_job_recovery(request: Request) -> Response:
    """Authorized console recovery for one terminal scan job."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    datastream_id = (request.path_params.get("datastream_id") or "").strip()
    job_id = (request.path_params.get("job_id") or "").strip()
    if not datastream_id or not re.fullmatch(r"inbscan_[0-9a-f]{24}", job_id):
        return JSONResponse(_NOT_FOUND, status_code=404)
    if not _check_datastream_access(datastream_id, identity, minimum_capability="edit"):
        return JSONResponse(_NOT_FOUND, status_code=404)

    idempotency_key = request.headers.get("Idempotency-Key", "").strip()
    if not idempotency_key or len(idempotency_key) > 200:
        return JSONResponse(
            {
                "code": "invalid_idempotency_key",
                "message": "A bounded Idempotency-Key header is required",
            },
            status_code=400,
        )
    raw_trace = request.headers.get("X-Trace-Id", "").strip()
    trace_id = raw_trace if re.fullmatch(r"[0-9a-f]{32}", raw_trace) else secrets.token_hex(16)
    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.inbound_scan_jobs import recover_dead_letter  # noqa: PLC0415

        with get_connection() as conn:
            try:
                recovered = recover_dead_letter(
                    conn,
                    job_id=job_id,
                    datastream_id=datastream_id,
                    actor=identity,
                    trace_id=trace_id,
                    idempotency_key=idempotency_key,
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "inbound_health_api: scan recovery failed ds=%s: %s",
            datastream_id,
            type(exc).__name__,
        )
        return JSONResponse(
            {"code": "server_error", "message": "Recovery unavailable"},
            status_code=500,
        )
    if not recovered:
        return JSONResponse(_NOT_FOUND, status_code=404)
    return JSONResponse(
        {
            "status": recovered["status"],
            "job_id": recovered["job_id"],
            "trace_id": recovered["trace_id"],
            "recovery_no": recovered["recovery_no"],
            "replayed": recovered["replayed"],
        },
        status_code=202,
    )


async def _post_routing_test(request: Request) -> Response:
    """A Datastream-level synthetic routing test -- 38.15 AC5.

    POST rather than GET even though it publishes nothing: it spends one
    resolution rate-limit event, and a GET that consumes budget is a GET a proxy
    or a prefetch is entitled to repeat.

    ``view``, like every other read on this surface. The MCP tool that runs the
    SAME command has exactly one authorization helper, and this module's header
    is explicit that the connector gets << one authorization story rather than
    two >>. Requiring `edit` here and `view` there would mean the same question,
    asked through two channels, answered differently -- which is precisely what
    38.15 AC2 forbids. The quota this spends is the caller's own Datastream's,
    and the rate limiter is what bounds a caller who loops it.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    datastream_id = (request.path_params.get("datastream_id") or "").strip()
    connector_name = (request.path_params.get("connector_name") or "").strip()
    if not datastream_id:
        return JSONResponse(_NOT_FOUND, status_code=404)
    if not _check_datastream_access(datastream_id, identity, minimum_capability="view"):
        return JSONResponse(_NOT_FOUND, status_code=404)

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 -- an absent body is a valid request here
        body = {}
    channel = str((body or {}).get("channel") or "email").strip().lower()

    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.inbound_credentials import (  # noqa: PLC0415
            datastream_matches_connector,
        )
        from core.inbound_routing_test import (  # noqa: PLC0415
            RoutingTestValidationError,
            run_datastream_routing_test,
        )

        with get_connection() as conn:
            # LE CHEMIN REST NOMME UN CONNECTEUR ; le Datastream en porte un.
            # S'ils divergent, la reponse est le 404 non divulguant -- confirmer
            # l'existence sous un mauvais connecteur serait une enumeration.
            if not datastream_matches_connector(
                conn, datastream_id=datastream_id, connector_name=connector_name
            ):
                return JSONResponse(_NOT_FOUND, status_code=404)
            try:
                report = run_datastream_routing_test(
                    conn, datastream_id=datastream_id, channel=channel
                )
            except RoutingTestValidationError as exc:
                return JSONResponse(
                    {"code": "invalid_channel", "message": str(exc)}, status_code=400
                )
            # Le test n'ecrit rien ; le commit ne libere que l'evenement de
            # cadencement pose par la resolution.
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "inbound_health_api: routing test failed ds=%s: %s",
            datastream_id,
            type(exc).__name__,
        )
        return JSONResponse(
            {"code": "server_error", "message": "Routing test unavailable"},
            status_code=500,
        )
    return JSONResponse(report, status_code=200)


INBOUND_HEALTH_ROUTES: list[Route] = [
    # More-specific paths first; Starlette resolves in declaration order.
    Route(
        "/api/connectors/{connector_name}/datastreams/{datastream_id}/scan-jobs/{job_id}/recover",
        endpoint=_post_scan_job_recovery,
        methods=["POST"],
    ),
    Route(
        "/api/connectors/{connector_name}/datastreams/{datastream_id}/routing-test",
        endpoint=_post_routing_test,
        methods=["POST"],
    ),
    Route(
        "/api/connectors/{connector_name}/datastreams/{datastream_id}"
        "/raw-imports/{raw_import_id}/mapping-context",
        endpoint=_get_mapping_context,
        methods=["GET"],
    ),
    Route(
        "/api/connectors/{connector_name}/datastreams/{datastream_id}/deliveries/{receipt_id}",
        endpoint=_get_delivery,
        methods=["GET"],
    ),
    Route(
        "/api/connectors/{connector_name}/datastreams/{datastream_id}/inbox",
        endpoint=_get_inbox,
        methods=["GET"],
    ),
    Route(
        "/api/connectors/{connector_name}/health",
        endpoint=_get_health,
        methods=["GET"],
    ),
]
