"""Chantier B -- the door to "which Datastream answers for this measure's total".

ONE SERVICE, THIN ADAPTER. Every handler translates HTTP into `core.metric_grain`
and returns what that module composed. No arbitration, no derivation and no
refusal text lives here, so the console and the MCP door cannot end up describing
the same declaration in two different sentences.

A REFUSAL NAMES THE GESTURE. `MetricGrainRefused` carries a machine code and a
sentence written for the person who has to repair it, and this file passes both
through unchanged at 422. It never substitutes a table name for the gesture --
that is the defect chantier A exists to close, and adding a new instance of it
while closing chantier B would be absurd.

Contract: docs/product-architecture/analyze-and-test.md, "Amendment, chantier B".
"""

from __future__ import annotations

import json

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core.metric_grain import (
    MetricGrainRefused,
    declare_breakdown,
    declare_total,
    grain_coverage,
    withdraw_breakdown,
)

_BASE = "/api/projects/{project_id}/governance/metric-grain"

_NOT_FOUND = {"code": "not_found", "message": "Not found"}


async def _authorize(request: Request, role: str) -> tuple[str, str] | Response:
    """`role` is one of `viewer | member | admin | owner`.

    `identity_has_project_role` RAISES on anything else -- there is no lenient
    default -- so a write guarded by an invented name like `editor` answers 500
    on every call instead of refusing politely. Caught before deployment, and
    only because the vocabulary was read rather than guessed.
    """
    from core.admin_api import _check_auth, _require_datastream_role  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415

    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Authentication required"}, 401)
    project_id = request.path_params["project_id"]
    with get_connection() as conn:
        denied = _require_datastream_role(project_id, identity, role, conn)
        if denied is not None:
            return denied
        with conn.cursor() as cur:
            cur.execute("SELECT org_id FROM app.projects WHERE id = %s", (project_id,))
            row = cur.fetchone()
    if row is None:
        return JSONResponse(_NOT_FOUND, 404)
    return str(identity), str(row[0])


async def _json_body(request: Request) -> dict:
    raw = await request.body()
    if not raw.strip():
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("request body must be an object")
    return value


def _refused(exc: MetricGrainRefused) -> Response:
    return JSONResponse({"code": exc.code, "message": exc.message}, 422)


def _concept(conn, *, project_id: str, concept_id: str) -> tuple[str, str] | None:
    """(concept_id, name) for a concept of THIS project, or None.

    The name is read from the concept's own versions rather than taken from the
    caller: the equality between a concept's name and a mapping's canonical target
    is what makes a carrier a carrier, and letting a caller supply that name would
    let it declare a total for a measure the executor will never resolve.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT cv.name
            FROM app.semantic_concept_versions cv
            JOIN app.semantic_concepts c ON c.id = cv.concept_id
            WHERE cv.concept_id = %s AND c.project_id = %s
            """,
            (concept_id, project_id),
        )
        rows = cur.fetchall()
    if len(rows) != 1:
        return None
    return concept_id, str(rows[0][0])


async def _get_coverage(request: Request) -> Response:
    """GET {base}/{concept_id} -- carriers, declaration and the one next gesture."""
    auth = await _authorize(request, "viewer")
    if isinstance(auth, Response):
        return auth
    from core.db import get_connection  # noqa: PLC0415

    project_id = request.path_params["project_id"]
    concept_id = request.path_params["concept_id"]
    view_version_id = request.query_params.get("semantic_view_version_id") or None
    with get_connection() as conn:
        found = _concept(conn, project_id=project_id, concept_id=concept_id)
        if found is None:
            return JSONResponse(_NOT_FOUND, 404)
        return JSONResponse(
            grain_coverage(
                conn,
                project_id=project_id,
                concept_id=found[0],
                concept_name=found[1],
                view_version_id=view_version_id,
            ),
            headers={"Cache-Control": "no-store"},
        )


async def _put_total(request: Request) -> Response:
    """PUT {base}/{concept_id}/total -- name the carrier that holds the total."""
    auth = await _authorize(request, "member")
    if isinstance(auth, Response):
        return auth
    identity, _org_id = auth
    from core.db import get_connection  # noqa: PLC0415

    project_id = request.path_params["project_id"]
    concept_id = request.path_params["concept_id"]
    try:
        body = await _json_body(request)
    except ValueError as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)
    total_datastream_id = str(body.get("total_datastream_id") or "")
    if not total_datastream_id:
        return JSONResponse(
            {
                "code": "missing_field",
                "message": "Name the Datastream that holds the total of this measure.",
            },
            400,
        )
    with get_connection() as conn:
        found = _concept(conn, project_id=project_id, concept_id=concept_id)
        if found is None:
            return JSONResponse(_NOT_FOUND, 404)
        try:
            declare_total(
                conn,
                project_id=project_id,
                concept_id=found[0],
                concept_name=found[1],
                total_datastream_id=total_datastream_id,
                identity=identity,
                note=(str(body["note"]).strip() or None) if body.get("note") else None,
            )
        except MetricGrainRefused as exc:
            conn.rollback()
            return _refused(exc)
        conn.commit()
        return JSONResponse(
            grain_coverage(
                conn,
                project_id=project_id,
                concept_id=found[0],
                concept_name=found[1],
                view_version_id=request.query_params.get("semantic_view_version_id") or None,
            )
        )


async def _put_breakdown(request: Request) -> Response:
    """PUT {base}/{concept_id}/breakdowns/{datastream_id} -- what it sums to."""
    auth = await _authorize(request, "member")
    if isinstance(auth, Response):
        return auth
    identity, _org_id = auth
    from core.db import get_connection  # noqa: PLC0415

    project_id = request.path_params["project_id"]
    concept_id = request.path_params["concept_id"]
    datastream_id = request.path_params["datastream_id"]
    try:
        body = await _json_body(request)
    except ValueError as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)

    tolerance = body.get("tolerance_ratio")
    with get_connection() as conn:
        found = _concept(conn, project_id=project_id, concept_id=concept_id)
        if found is None:
            return JSONResponse(_NOT_FOUND, 404)
        try:
            declare_breakdown(
                conn,
                project_id=project_id,
                concept_id=found[0],
                concept_name=found[1],
                datastream_id=datastream_id,
                sums_to=str(body.get("sums_to") or ""),
                reason=(str(body["reason"]) if body.get("reason") is not None else None),
                tolerance_ratio=(float(tolerance) if tolerance is not None else None),
                identity=identity,
            )
        except MetricGrainRefused as exc:
            conn.rollback()
            return _refused(exc)
        except (TypeError, ValueError):
            conn.rollback()
            return JSONResponse(
                {
                    "code": "invalid_tolerance",
                    "message": "A tolerance is a share of the total, such as 0.01 for one percent.",
                },
                400,
            )
        conn.commit()
        return JSONResponse(
            grain_coverage(
                conn,
                project_id=project_id,
                concept_id=found[0],
                concept_name=found[1],
                view_version_id=request.query_params.get("semantic_view_version_id") or None,
            )
        )


async def _delete_breakdown(request: Request) -> Response:
    """DELETE {base}/{concept_id}/breakdowns/{datastream_id}.

    The statement goes; the gap does not. It reads `undeclared` afterwards, which
    is the honest state -- not an absence of gap.
    """
    auth = await _authorize(request, "member")
    if isinstance(auth, Response):
        return auth
    identity, _org_id = auth
    from core.db import get_connection  # noqa: PLC0415

    project_id = request.path_params["project_id"]
    concept_id = request.path_params["concept_id"]
    datastream_id = request.path_params["datastream_id"]
    with get_connection() as conn:
        found = _concept(conn, project_id=project_id, concept_id=concept_id)
        if found is None:
            return JSONResponse(_NOT_FOUND, 404)
        try:
            withdraw_breakdown(
                conn,
                project_id=project_id,
                concept_id=found[0],
                datastream_id=datastream_id,
                identity=identity,
            )
        except MetricGrainRefused as exc:
            conn.rollback()
            return _refused(exc)
        conn.commit()
        return JSONResponse(
            grain_coverage(
                conn,
                project_id=project_id,
                concept_id=found[0],
                concept_name=found[1],
                view_version_id=request.query_params.get("semantic_view_version_id") or None,
            )
        )


#: Route order: the literal `/total` and `/breakdowns/...` segments are declared
#: before the bare `{concept_id}` read, so neither can be captured by it.
metric_grain_routes = [
    Route(f"{_BASE}/{{concept_id}}/total", endpoint=_put_total, methods=["PUT"]),
    Route(
        f"{_BASE}/{{concept_id}}/breakdowns/{{datastream_id}}",
        endpoint=_put_breakdown,
        methods=["PUT"],
    ),
    Route(
        f"{_BASE}/{{concept_id}}/breakdowns/{{datastream_id}}",
        endpoint=_delete_breakdown,
        methods=["DELETE"],
    ),
    Route(f"{_BASE}/{{concept_id}}", endpoint=_get_coverage, methods=["GET"]),
]
