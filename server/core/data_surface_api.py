"""Thin, fail-closed HTTP routes for the canonical Project Data surface."""

from __future__ import annotations

import logging
from datetime import date

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core.data_surface import (
    DATASTREAM_FILTERED_LENSES,
    PAGED_LENSES,
    DataObjectNotFound,
    compose_data_surface,
)
from core.db import request_connection
from core.project_access import resolve_strict_resource_access

logger = logging.getLogger(__name__)


def _no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    return response


def _not_found() -> Response:
    return _no_store(
        JSONResponse(
            {"code": "not_found", "message": "Data object not found"},
            status_code=404,
        )
    )


async def _get_data_surface(request: Request, lens: str) -> Response:
    from core.admin_api import _check_auth

    authorized, identity = await _check_auth(request)
    if not authorized:
        return _no_store(
            JSONResponse(
                {"code": "unauthorized", "message": "Bearer token required"},
                status_code=401,
            )
        )

    project_id = (request.path_params.get("project_id") or "").strip()
    object_id = request.path_params.get("object_id")
    actor = identity or "anonymous"
    try:
        query = request.query_params
        if object_id is not None and query:
            raise ValueError("object detail routes accept no collection filters")
        # The paged collections, and the composer decides WHICH they are: a route
        # that kept its own copy of that set is how `sources` came to send a
        # cursor the server dropped on the floor -- the screen turned the page,
        # the parameter was ignored as "not a filter of this lens", and page two
        # answered with page one.
        if lens in PAGED_LENSES:
            allowed = {"q", "state", "limit", "cursor"}
            if lens in DATASTREAM_FILTERED_LENSES:
                allowed.add("datastream")
            if lens == "imports":
                allowed.update({"from", "to"})
            unknown = set(query) - allowed
            if unknown:
                raise ValueError(f"unsupported collection filter: {sorted(unknown)[0]}")
            limit = int(query.get("limit", "25"))
            cursor = int(query.get("cursor", "0"))
            if not 1 <= limit <= 100 or cursor < 0:
                raise ValueError("limit must be 1..100 and cursor must be a positive offset")
            filters = {
                "q": query.get("q", "").strip(),
                "state": query.get("state", "").strip(),
            }
            if lens in DATASTREAM_FILTERED_LENSES:
                filters["datastream"] = query.get("datastream", "").strip()
            if lens == "imports":
                date_from = query.get("from", "").strip()
                date_to = query.get("to", "").strip()
                if date_from:
                    date.fromisoformat(date_from)
                if date_to:
                    date.fromisoformat(date_to)
                if date_from and date_to and date_from > date_to:
                    raise ValueError("from must be earlier than or equal to to")
                filters.update({"from": date_from, "to": date_to})
        else:
            limit, cursor, filters = None, 0, None
        with request_connection(actor) as conn:
            decision = resolve_strict_resource_access(
                actor,
                conn,
                project_id=project_id,
                minimum_capability="view",
                hold_access=True,
            )
            if not decision.allowed:
                return _not_found()
            envelope = compose_data_surface(
                project_id,
                lens,
                conn,
                object_id=object_id,
                can_edit=decision.capability in {"edit", "manage"},
                filters=filters,
                limit=limit,
                cursor=cursor,
            )
        return _no_store(JSONResponse(envelope, status_code=200))
    except DataObjectNotFound:
        return _not_found()
    except ValueError as exc:
        return _no_store(
            JSONResponse({"code": "invalid_query", "message": str(exc)}, status_code=422)
        )
    except Exception as exc:  # fail closed without disclosing resource existence.
        logger.warning(
            "Data surface unavailable project=%s lens=%s: %s",
            project_id,
            lens,
            exc,
        )
        return _no_store(
            JSONResponse(
                {"code": "data_surface_unavailable", "message": "Data is unavailable"},
                status_code=503,
            )
        )


def _route(path: str, lens: str) -> Route:
    async def endpoint(request: Request) -> Response:
        return await _get_data_surface(request, lens)

    return Route(path, endpoint=endpoint, methods=["GET"], name=lens)


async def _post_event_configuration_command(request: Request, command: str) -> Response:
    from core.admin_api import _check_auth
    from core.event_configurations import (
        EventConfigurationInvalid,
        EventConfigurationNotFound,
        EventConfigurationStale,
        activate_event_configuration_version,
        confirm_event_configuration_version,
        create_event_configuration,
        create_event_configuration_version,
    )
    from core.operations import OperationIdempotencyConflict

    authorized, identity = await _check_auth(request)
    if not authorized:
        return _no_store(
            JSONResponse(
                {"code": "unauthorized", "message": "Bearer token required"}, status_code=401
            )
        )
    idempotency_key = (request.headers.get("Idempotency-Key") or "").strip()
    if not idempotency_key:
        return _no_store(
            JSONResponse(
                {"code": "missing_header", "message": "Idempotency-Key header is required"},
                status_code=422,
            )
        )
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("body must be an object")
    except Exception as exc:
        return _no_store(
            JSONResponse({"code": "invalid_body", "message": str(exc)}, status_code=400)
        )

    project_id = (request.path_params.get("project_id") or "").strip()
    actor = identity or "anonymous"
    try:
        with request_connection(actor) as conn:
            decision = resolve_strict_resource_access(
                actor,
                conn,
                project_id=project_id,
                minimum_capability="edit",
                hold_access=True,
            )
            if not decision.allowed or not decision.org_id:
                return _not_found()
            common = {
                "conn": conn,
                "project_id": project_id,
                "org_id": decision.org_id,
                "actor": actor,
                "idempotency_key": idempotency_key,
            }
            if command == "create-configuration":
                result = create_event_configuration(
                    **common,
                    datastream_id=(request.path_params.get("datastream_id") or "").strip(),
                    name=str(body.get("name") or ""),
                )
                status_code = 201
            elif command == "create-version":
                result = create_event_configuration_version(
                    **common,
                    event_configuration_id=(request.path_params.get("object_id") or "").strip(),
                    source_mapping=body.get("source_mapping"),
                    collection_policy=body.get("collection_policy"),
                )
                status_code = 201
            elif command == "confirm-version":
                result = confirm_event_configuration_version(
                    **common,
                    event_configuration_id=(request.path_params.get("object_id") or "").strip(),
                    version_id=(request.path_params.get("version_id") or "").strip(),
                )
                status_code = 200
            else:
                result = activate_event_configuration_version(
                    **common,
                    event_configuration_id=(request.path_params.get("object_id") or "").strip(),
                    version_id=(request.path_params.get("version_id") or "").strip(),
                )
                status_code = 200
            conn.commit()
        return _no_store(JSONResponse(result, status_code=status_code))
    except EventConfigurationNotFound:
        return _not_found()
    except (EventConfigurationStale, OperationIdempotencyConflict) as exc:
        return _no_store(JSONResponse({"code": "conflict", "message": str(exc)}, status_code=409))
    except EventConfigurationInvalid as exc:
        return _no_store(
            JSONResponse({"code": "invalid_input", "message": str(exc)}, status_code=422)
        )
    except Exception as exc:
        logger.warning(
            "Event Configuration command unavailable project=%s command=%s: %s",
            project_id,
            command,
            exc,
        )
        return _no_store(
            JSONResponse(
                {
                    "code": "event_configuration_unavailable",
                    "message": "Event Configuration command is unavailable",
                },
                status_code=503,
            )
        )


def _command_route(path: str, command: str) -> Route:
    async def endpoint(request: Request) -> Response:
        return await _post_event_configuration_command(request, command)

    return Route(path, endpoint=endpoint, methods=["POST"], name=command)


DATA_SURFACE_ROUTES = [
    _command_route(
        "/api/projects/{project_id}/datastreams/{datastream_id}/event-configurations",
        "create-configuration",
    ),
    _command_route(
        "/api/projects/{project_id}/event-configurations/{object_id}/versions", "create-version"
    ),
    _command_route(
        "/api/projects/{project_id}/event-configurations/{object_id}/versions/{version_id}/confirmations",
        "confirm-version",
    ),
    _command_route(
        "/api/projects/{project_id}/event-configurations/{object_id}/versions/{version_id}/activate",
        "activate-version",
    ),
    _route("/api/projects/{project_id}/data-overview", "overview"),
    _route("/api/projects/{project_id}/datastreams", "datastreams"),
    _route("/api/projects/{project_id}/source-accounts", "sources"),
    _route("/api/projects/{project_id}/source-accounts/{object_id}", "sources"),
    _route("/api/projects/{project_id}/imports", "imports"),
    _route("/api/projects/{project_id}/imports/{object_id}", "imports"),
    _route("/api/projects/{project_id}/event-configurations", "events"),
    _route("/api/projects/{project_id}/event-configurations/{object_id}", "events"),
    _route("/api/projects/{project_id}/connectors", "connectors"),
    _route("/api/projects/{project_id}/connectors/{object_id}", "connectors"),
]
