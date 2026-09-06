"""toorow -- Reprocess retained raw evidence, REST surface (Story 38.18).

  GET  .../raw-imports/{raw_import_id}/reprocess
       -- the PROPOSAL. Reads the retained object and verifies its integrity;
          writes nothing. This is what an operator confirms.

  POST .../raw-imports/{raw_import_id}/reprocess
       -- the EXECUTION. Requires Idempotency-Key and a stated ``reason``.
          Creates a NEW execution; mutates no prior one.

  GET  .../datastreams/{datastream_id}/reprocess-scope
       -- the SCOPE proposal (38.18 AC1). The count BEFORE the act, with the
          objects it names, and its declared bound (``scan_truncated``).

  POST .../datastreams/{datastream_id}/reprocess-scope
       -- the confirmed scope, executed as ONE durable operation. The body
          carries the ENUMERATED members, never the criterion: a criterion
          re-resolved at commit could name a different set than the one a
          person read.

PREPARE IS A GET AND COMMIT IS A POST, and that is not decoration: a preparation
that could be triggered by a link prefetch must have no side effect, and this one
has none -- it reads an object and hashes it. The commit carries the header that
makes a retry safe.

TWO CAPABILITY LEVELS, because they are two different acts:
  * ``view`` to prepare -- seeing whether a file could be re-run is inspection.
  * ``edit`` to execute -- it can move the published pointer.

A REFUSAL IS A 409, NOT A 500. Retention expired, an unreadable object and an
integrity mismatch are all states of the world, not server faults, and each
returns its stable ``code`` so a client renders the reason instead of parsing a
sentence. A 500 would tell an operator to retry something that will never work.

Any authorization failure returns the nondisclosing 404 (AD-5, E38-NFR01).
Source-agnostic (AD-2). ASCII-only source. Lazy imports.
"""

from __future__ import annotations

import json
import logging
import os

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger(__name__)

_NOT_FOUND: dict = {"code": "not_found", "message": "Resource not found"}


async def _check_auth(request: Request) -> tuple[bool, str]:
    from core.api_auth import authenticate_api_request  # noqa: PLC0415

    return await authenticate_api_request(request)


def _check_datastream_access(
    datastream_id: str, identity: str, *, minimum_capability: str
) -> bool | None:
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
            "inbound_reprocess_api: access check failed ds=%s: %s",
            datastream_id,
            type(exc).__name__,
        )
        return None


def _trace_id(request: Request) -> str | None:
    import re  # noqa: PLC0415

    raw = request.headers.get("X-Trace-Id", "").strip()
    return raw if raw and re.fullmatch(r"[0-9a-f]{32}", raw) else None


def _params(request: Request) -> tuple[str, str]:
    return (
        (request.path_params.get("datastream_id") or "").strip(),
        (request.path_params.get("raw_import_id") or "").strip(),
    )


async def _get_reprocess_proposal(request: Request) -> Response:
    """The proposal. Inspection only -- reads the object, writes nothing."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    datastream_id, raw_import_id = _params(request)
    if not datastream_id or not raw_import_id:
        return JSONResponse(
            {
                "code": "missing_param",
                "message": "datastream_id and raw_import_id are required",
            },
            status_code=400,
        )

    if not _check_datastream_access(datastream_id, identity, minimum_capability="view"):
        return JSONResponse(_NOT_FOUND, status_code=404)

    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.inbound_reprocess import prepare_reprocess  # noqa: PLC0415

        # LA VERSION CIBLE ARRIVE EN QUERY sur un GET : la proposition est une
        # LECTURE, et un corps sur un GET est ce qu'un proxy ou un cache a le
        # droit de jeter. Absente = la paire en vigueur, comportement inchange.
        target = (request.query_params.get("target_mapping_version_id") or "").strip()
        with get_connection() as conn:
            proposal = prepare_reprocess(
                conn,
                raw_import_id=raw_import_id,
                datastream_id=datastream_id,
                target_mapping_version_id=target or None,
            )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "inbound_reprocess_api: prepare failed ds=%s: %s",
            datastream_id,
            type(exc).__name__,
        )
        return JSONResponse(
            {"code": "server_error", "message": "Proposal unavailable"},
            status_code=500,
        )

    return JSONResponse(proposal, status_code=200)


async def _post_reprocess(request: Request) -> Response:
    """Execute the reprocess. Idempotency-Key required; a reason is required."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    datastream_id, raw_import_id = _params(request)
    if not datastream_id or not raw_import_id:
        return JSONResponse(
            {
                "code": "missing_param",
                "message": "datastream_id and raw_import_id are required",
            },
            status_code=400,
        )

    idempotency_key = request.headers.get("Idempotency-Key")
    if not idempotency_key:
        # Without it a lost response cannot be reconciled, and a client retry
        # would import the same file twice.
        return JSONResponse(
            {
                "code": "missing_idempotency_key",
                "message": "Idempotency-Key header is required",
            },
            status_code=422,
        )

    # Executing can move the published pointer: that is an edit, not a read.
    if not _check_datastream_access(datastream_id, identity, minimum_capability="edit"):
        return JSONResponse(_NOT_FOUND, status_code=404)

    try:
        body = json.loads(await request.body() or b"{}")
    except (ValueError, UnicodeDecodeError):
        return JSONResponse(
            {"code": "invalid_body", "message": "Body must be JSON"}, status_code=400
        )
    reason = (body.get("reason") or "").strip() if isinstance(body, dict) else ""
    if not reason:
        return JSONResponse(
            {
                "code": "missing_reason",
                "message": "A reason is required: a reprocess changes published data.",
            },
            status_code=422,
        )

    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.inbound_reprocess import (  # noqa: PLC0415
            ReprocessUnavailable,
            ReprocessValidationError,
            execute_reprocess,
        )

        with get_connection() as conn:
            try:
                result = execute_reprocess(
                    conn,
                    raw_import_id=raw_import_id,
                    datastream_id=datastream_id,
                    actor=identity,
                    idempotency_key=idempotency_key,
                    reason=reason,
                    target_mapping_version_id=(
                        str(body.get("target_mapping_version_id") or "").strip() or None
                    ),
                    trace_id=_trace_id(request),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    except ReprocessUnavailable as exc:
        # A state of the world, not a server fault: 409 with the stable code, so
        # the client renders the reason rather than telling the operator to
        # retry something that will never work.
        return JSONResponse(
            {"code": exc.code, "message": str(exc)}, status_code=409
        )
    except ReprocessValidationError as exc:
        return JSONResponse(
            {"code": "invalid_request", "message": str(exc)}, status_code=422
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "inbound_reprocess_api: execute failed ds=%s: %s",
            datastream_id,
            type(exc).__name__,
        )
        return JSONResponse(
            {"code": "server_error", "message": "Reprocess failed"},
            status_code=500,
        )

    return JSONResponse(result, status_code=200)


def _scope_params(request: Request) -> str:
    return (request.path_params.get("datastream_id") or "").strip()


def _id_list(raw: str | None) -> list[str] | None:
    """Comma-separated identifiers on a GET, or None when the key is absent.

    An EMPTY value is not the same as an absent one: `?raw_import_ids=` is a
    request for nothing, and returning the criterion branch instead would answer
    a question nobody asked.
    """
    if raw is None:
        return None
    return [part.strip() for part in raw.split(",") if part.strip()]


async def _get_reprocess_scope(request: Request) -> Response:
    """The SCOPE proposal: the count before the act, and the objects it names.

    Inspection only. The bound is declared in the answer (`scan_truncated`,
    `scan_limit`), never applied silently.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    datastream_id = _scope_params(request)
    if not datastream_id:
        return JSONResponse(
            {"code": "missing_param", "message": "datastream_id is required"},
            status_code=400,
        )

    if not _check_datastream_access(datastream_id, identity, minimum_capability="view"):
        return JSONResponse(_NOT_FOUND, status_code=404)

    params = request.query_params
    raw_import_ids = _id_list(params.get("raw_import_ids"))
    criterion: dict = {}
    for key in ("received_from", "received_to"):
        value = (params.get(key) or "").strip()
        if value:
            criterion[key] = value
    states = _id_list(params.get("states"))
    if states:
        criterion["states"] = states

    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.inbound_reprocess import (  # noqa: PLC0415
            MAX_SCOPE_MEMBERS,
            ReprocessValidationError,
            prepare_reprocess_scope,
        )

        try:
            limit = int(params.get("limit") or MAX_SCOPE_MEMBERS)
        except ValueError:
            return JSONResponse(
                {"code": "invalid_request", "message": "limit must be an integer"},
                status_code=422,
            )
        target = (params.get("target_mapping_version_id") or "").strip()
        with get_connection() as conn:
            try:
                proposal = prepare_reprocess_scope(
                    conn,
                    datastream_id=datastream_id,
                    raw_import_ids=raw_import_ids,
                    criterion=criterion or None,
                    target_mapping_version_id=target or None,
                    limit=limit,
                )
            except ReprocessValidationError as exc:
                return JSONResponse(
                    {"code": "invalid_request", "message": str(exc)}, status_code=422
                )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "inbound_reprocess_api: scope prepare failed ds=%s: %s",
            datastream_id,
            type(exc).__name__,
        )
        return JSONResponse(
            {"code": "server_error", "message": "Proposal unavailable"},
            status_code=500,
        )

    return JSONResponse(proposal, status_code=200)


async def _post_reprocess_scope(request: Request) -> Response:
    """Execute a confirmed scope as ONE durable operation.

    The body carries the ENUMERATED members, never the criterion: a criterion
    re-resolved here could name a different set than the one a person read and
    confirmed.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    datastream_id = _scope_params(request)
    if not datastream_id:
        return JSONResponse(
            {"code": "missing_param", "message": "datastream_id is required"},
            status_code=400,
        )

    idempotency_key = request.headers.get("Idempotency-Key")
    if not idempotency_key:
        return JSONResponse(
            {
                "code": "missing_idempotency_key",
                "message": "Idempotency-Key header is required",
            },
            status_code=422,
        )

    if not _check_datastream_access(datastream_id, identity, minimum_capability="edit"):
        return JSONResponse(_NOT_FOUND, status_code=404)

    try:
        body = json.loads(await request.body() or b"{}")
    except (ValueError, UnicodeDecodeError):
        return JSONResponse(
            {"code": "invalid_body", "message": "Body must be JSON"}, status_code=400
        )
    if not isinstance(body, dict):
        return JSONResponse(
            {"code": "invalid_body", "message": "Body must be a JSON object"},
            status_code=400,
        )
    reason = (body.get("reason") or "").strip()
    if not reason:
        return JSONResponse(
            {
                "code": "missing_reason",
                "message": "A reason is required: a reprocess changes published data.",
            },
            status_code=422,
        )
    raw_import_ids = body.get("raw_import_ids")
    if not isinstance(raw_import_ids, list) or not raw_import_ids:
        return JSONResponse(
            {
                "code": "missing_scope",
                "message": (
                    "raw_import_ids is required: a scope is confirmed on the "
                    "files it names, never on a criterion re-evaluated here."
                ),
            },
            status_code=422,
        )

    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.inbound_reprocess import (  # noqa: PLC0415
            ReprocessUnavailable,
            ReprocessValidationError,
            execute_reprocess_scope,
        )

        with get_connection() as conn:
            try:
                result = execute_reprocess_scope(
                    conn,
                    datastream_id=datastream_id,
                    raw_import_ids=[str(i) for i in raw_import_ids],
                    actor=identity,
                    idempotency_key=idempotency_key,
                    reason=reason,
                    target_mapping_version_id=(
                        str(body.get("target_mapping_version_id") or "").strip() or None
                    ),
                    trace_id=_trace_id(request),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    except ReprocessUnavailable as exc:
        # The scope moved between the count and the act. The members that moved
        # travel with the refusal so the operator re-reads a scope rather than a
        # sentence -- all of them belong to the Datastream they already hold.
        payload = {"code": exc.code, "message": str(exc)}
        members = getattr(exc, "members", None)
        if members:
            payload["members"] = members
        return JSONResponse(payload, status_code=409)
    except ReprocessValidationError as exc:
        return JSONResponse(
            {"code": "invalid_request", "message": str(exc)}, status_code=422
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "inbound_reprocess_api: scope execute failed ds=%s: %s",
            datastream_id,
            type(exc).__name__,
        )
        return JSONResponse(
            {"code": "server_error", "message": "Reprocess failed"},
            status_code=500,
        )

    return JSONResponse(result, status_code=200)


INBOUND_REPROCESS_ROUTES: list[Route] = [
    Route(
        "/api/connectors/{connector_name}/datastreams/{datastream_id}"
        "/raw-imports/{raw_import_id}/reprocess",
        endpoint=_get_reprocess_proposal,
        methods=["GET"],
    ),
    Route(
        "/api/connectors/{connector_name}/datastreams/{datastream_id}"
        "/raw-imports/{raw_import_id}/reprocess",
        endpoint=_post_reprocess,
        methods=["POST"],
    ),
    Route(
        "/api/connectors/{connector_name}/datastreams/{datastream_id}"
        "/reprocess-scope",
        endpoint=_get_reprocess_scope,
        methods=["GET"],
    ),
    Route(
        "/api/connectors/{connector_name}/datastreams/{datastream_id}"
        "/reprocess-scope",
        endpoint=_post_reprocess_scope,
        methods=["POST"],
    ),
]
