"""Chantier C -- the one door that arms a Datastream's events.

TWO ADDRESSES, AND THE READ COMES FIRST. `GET .../event-stream` says what arming
would create -- the exact `source_mapping` and `collection_policy`, derived --
and whether this Datastream is already armed. `POST` performs it. A gesture whose
consequence cannot be read before it happens is a gesture people click to find
out what it does.

IT COMPOSES, IT DOES NOT REIMPLEMENT. The four governed operations stay exactly
where they are (`core.event_configurations`), each with its own operation record,
evidence and audit row. What this door removes is the requirement to know that
four of them exist.

Contract: docs/product-architecture/data.md, "Amendment, chantier C".
"""

from __future__ import annotations

import json

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core.event_stream_arming import (
    EventStreamRefused,
    arm_event_stream,
    existing_configuration,
    plan_event_stream,
)

_PATH = "/api/projects/{project_id}/datastreams/{datastream_id}/event-stream"

_NOT_FOUND = {"code": "not_found", "message": "Not found"}


async def _authorize(request: Request, role: str) -> tuple[str, str] | Response:
    """`role` is one of `viewer | member | admin | owner` -- the closed vocabulary
    of `identity_has_project_role`, which raises on anything else."""
    from core.admin_api import _check_auth, _require_datastream_role  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415

    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Authentication required"}, 401)
    project_id = request.path_params["project_id"]
    datastream_id = request.path_params["datastream_id"]
    with get_connection() as conn:
        denied = _require_datastream_role(
            project_id, identity, role, conn, datastream_id=datastream_id
        )
        if denied is not None:
            return denied
        with conn.cursor() as cur:
            cur.execute("SELECT org_id FROM app.projects WHERE id = %s", (project_id,))
            row = cur.fetchone()
    if row is None:
        return JSONResponse(_NOT_FOUND, 404)
    return str(identity), str(row[0])


def _refused(exc: EventStreamRefused) -> Response:
    return JSONResponse({"code": exc.code, "message": exc.message}, 422)


async def _read_event_stream(request: Request) -> Response:
    """GET -- what arming would create, and what is already armed."""
    auth = await _authorize(request, "viewer")
    if isinstance(auth, Response):
        return auth
    from core.db import get_connection  # noqa: PLC0415

    project_id = request.path_params["project_id"]
    datastream_id = request.path_params["datastream_id"]
    with get_connection() as conn:
        live = existing_configuration(
            conn, project_id=project_id, datastream_id=datastream_id
        )
        try:
            plan = plan_event_stream(
                conn, project_id=project_id, datastream_id=datastream_id
            )
        except EventStreamRefused as exc:
            if exc.code == "datastream_not_found":
                return JSONResponse(_NOT_FOUND, 404)
            # NOT A 422. Asking what would happen is a legitimate question with a
            # legitimate answer -- "nothing, and here is why". A refusal here would
            # make the screen unable to draw the reason it must show.
            return JSONResponse(
                {
                    "datastream_id": datastream_id,
                    "armed": live,
                    "plan": None,
                    "blocked_reason": exc.message,
                    "blocked_code": exc.code,
                },
                headers={"Cache-Control": "no-store"},
            )
    return JSONResponse(
        {
            "datastream_id": datastream_id,
            "armed": live,
            "plan": plan,
            "blocked_reason": None,
            "blocked_code": None,
        },
        headers={"Cache-Control": "no-store"},
    )


async def _arm(request: Request) -> Response:
    """POST -- create, version, confirm and activate, in that order."""
    auth = await _authorize(request, "member")
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth
    from core.db import get_connection  # noqa: PLC0415

    project_id = request.path_params["project_id"]
    datastream_id = request.path_params["datastream_id"]
    raw = await request.body()
    try:
        body = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return JSONResponse({"code": "invalid_body", "message": "Invalid JSON body"}, 400)
    if not isinstance(body, dict):
        return JSONResponse(
            {"code": "invalid_body", "message": "request body must be an object"}, 400
        )

    with get_connection() as conn:
        try:
            armed = arm_event_stream(
                conn,
                project_id=project_id,
                datastream_id=datastream_id,
                org_id=org_id,
                actor=identity,
                name=(str(body["name"]).strip() or None) if body.get("name") else None,
                trace_id=request.headers.get("x-trace-id"),
            )
        except EventStreamRefused as exc:
            conn.rollback()
            if exc.code == "datastream_not_found":
                return JSONResponse(_NOT_FOUND, 404)
            return _refused(exc)
        conn.commit()
    return JSONResponse(armed, status_code=201)


event_stream_arming_routes = [
    Route(_PATH, endpoint=_read_event_stream, methods=["GET"]),
    Route(_PATH, endpoint=_arm, methods=["POST"]),
]
