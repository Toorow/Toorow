"""Story 51.4 -- the observed-evidence HTTP adapter.

ONE SERVICE, A THIN ADAPTER. Every handler here is a translation of HTTP into
`core.trace_observation`. No validation, no SQL and no evidence logic lives in
this file, so a future MCP adapter reaches the same cohorts and the same lenses
without a second observed-evidence path. This is the shape
`core.query_specs_api` established for Story 50.1.

NON-DISCLOSURE IS THE DEFAULT. `TraceObservationNotFound` becomes the same 404
envelope whether the object is foreign, denied or absent, and the denial path
answers before any work is done so response timing does not become an
enumeration oracle (AC10). Refusals are 422 carrying their full structured reason
list: a caller that cannot see why it was refused will guess, and guessing is how
an unqualified `latest` ends up stored.

WHAT IS DELIBERATELY ABSENT FROM THIS FILE. There is no approve route, no
promote route, no baseline route, no gate route and no blocking verdict. An
Observed Cohort is a reference window (`analyze-and-test.md:357-358`), and the
absence is the enforcement -- re-proved from the schema by migration 153, which
gives the cohort tables no approval column and refuses, in
`app.assert_baseline_run_is_offline`, to approve an observed run as a baseline.

ROUTE ORDER IS LOAD-BEARING. Literal segments are declared before parameterized
ones, so `/aggregates` and `/proposals` can never be captured as a cohort id.

MOUNTING. This module exports `trace_observation_routes` and does NOT edit
`server/core/admin_api.py`, which another session holds. The two lines its owner
adds are:

    from core.trace_observation_api import trace_observation_routes
    *trace_observation_routes,
"""

from __future__ import annotations

import json

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core.trace_observation import (
    TraceObservationNotFound,
    TraceObservationRefused,
    cohort_aggregates,
    decide_golden_question_proposal,
    emit_golden_question_proposals,
    list_golden_question_proposals,
    list_observed_cohorts,
    load_observed_cohort,
    load_trace_observation,
    resolve_observed_cohort,
)

_BASE = "/api/projects/{project_id}/test"

#: One envelope for foreign, denied and absent. See the module docstring.
_NOT_FOUND = {"code": "not_found", "message": "Not found"}


async def _authorize(request: Request, role: str = "viewer"):
    """Return (identity, org_id) or a Response. Denial answers before any work."""
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


def _refused(exc: TraceObservationRefused) -> Response:
    return JSONResponse(exc.as_dict(), 422)


async def _observed_cohorts_collection(request: Request) -> Response:
    """GET lists the frozen cohorts; POST resolves a new one.

    POST is a resolution, not a saved filter: it freezes membership and mints a
    new identity every time. Re-resolving the same window deliberately produces a
    SECOND cohort with its own denominator rather than refreshing the first.
    """
    if request.method == "POST":
        return await _resolve_cohort(request)
    return await _list_cohorts(request)


async def _list_cohorts(request: Request) -> Response:
    auth = await _authorize(request, "viewer")
    if isinstance(auth, Response):
        return auth
    _identity, org_id = auth
    project_id = request.path_params["project_id"]

    from core.db import get_connection  # noqa: PLC0415

    with get_connection() as conn:
        cohorts = list_observed_cohorts(
            conn,
            org_id=org_id,
            project_id=project_id,
            limit=int(request.query_params.get("limit") or 50),
            cursor=request.query_params.get("cursor") or None,
        )
    return JSONResponse({"cohorts": cohorts})


async def _resolve_cohort(request: Request) -> Response:
    auth = await _authorize(request, "member")
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth
    project_id = request.path_params["project_id"]
    try:
        body = await _json_body(request)
    except (ValueError, json.JSONDecodeError) as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)

    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            cohort = resolve_observed_cohort(
                conn,
                org_id=org_id,
                project_id=project_id,
                actor=identity,
                payload=body,
            )
            conn.commit()
    except TraceObservationRefused as exc:
        return _refused(exc)
    except TraceObservationNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    return JSONResponse(cohort, 201)


async def _get_cohort(request: Request) -> Response:
    """GET {base}/observed-cohorts/{id} -- the frozen selection and its members."""
    auth = await _authorize(request, "viewer")
    if isinstance(auth, Response):
        return auth
    _identity, org_id = auth
    project_id = request.path_params["project_id"]

    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            cohort = load_observed_cohort(
                conn,
                org_id=org_id,
                project_id=project_id,
                cohort_id=request.path_params["cohort_id"],
            )
    except TraceObservationNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    return JSONResponse(cohort)


async def _get_cohort_aggregates(request: Request) -> Response:
    """GET {base}/observed-cohorts/{id}/aggregates -- never a bare percentage.

    Every entry carries numerator, denominator, coverage and the exact version
    filters the cohort pinned (AC7).
    """
    auth = await _authorize(request, "viewer")
    if isinstance(auth, Response):
        return auth
    _identity, org_id = auth
    project_id = request.path_params["project_id"]

    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            aggregates = cohort_aggregates(
                conn,
                org_id=org_id,
                project_id=project_id,
                cohort_id=request.path_params["cohort_id"],
            )
    except TraceObservationNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    return JSONResponse(aggregates)


async def _cohort_proposals(request: Request) -> Response:
    """GET lists proposals; POST records the ones the frozen membership suggests.

    Neither creates, edits nor versions a Golden Question. Story 51.1 owns that
    object and its authoring path.
    """
    role = "member" if request.method == "POST" else "viewer"
    auth = await _authorize(request, role)
    if isinstance(auth, Response):
        return auth
    _identity, org_id = auth
    project_id = request.path_params["project_id"]
    cohort_id = request.path_params["cohort_id"]

    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            if request.method == "POST":
                proposals = emit_golden_question_proposals(
                    conn, org_id=org_id, project_id=project_id, cohort_id=cohort_id
                )
                conn.commit()
            else:
                proposals = list_golden_question_proposals(
                    conn, org_id=org_id, project_id=project_id, cohort_id=cohort_id
                )
    except TraceObservationNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    return JSONResponse({"proposals": proposals, "blocking": False})


async def _decide_proposal(request: Request) -> Response:
    """POST .../proposals/{id}/decision -- accept or decline, and nothing else.

    Accepting names a Golden Question that ALREADY exists; the composite foreign
    key refuses one this Project does not own. There is no route here that
    creates, approves, promotes, gates or blocks anything.
    """
    auth = await _authorize(request, "member")
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth
    project_id = request.path_params["project_id"]
    try:
        body = await _json_body(request)
    except (ValueError, json.JSONDecodeError) as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)

    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            decided = decide_golden_question_proposal(
                conn,
                org_id=org_id,
                project_id=project_id,
                cohort_id=request.path_params["cohort_id"],
                proposal_id=request.path_params["proposal_id"],
                state=str(body.get("state") or ""),
                actor=identity,
                golden_question_id=body.get("golden_question_id"),
            )
            conn.commit()
    except TraceObservationRefused as exc:
        return _refused(exc)
    except TraceObservationNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    return JSONResponse(decided)


async def _get_trace_observation(request: Request) -> Response:
    """GET {base}/trace-observations/{ai_path_id} -- the five ratified lenses.

    The `Result & Render` lens resolves the exact Result and states, in the same
    payload, that the rendered half has no owner. It never renders a placeholder
    thumbnail and never mints a render identifier (AC6).
    """
    auth = await _authorize(request, "viewer")
    if isinstance(auth, Response):
        return auth
    _identity, org_id = auth
    project_id = request.path_params["project_id"]

    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            observation = load_trace_observation(
                conn,
                org_id=org_id,
                project_id=project_id,
                ai_path_id=request.path_params["ai_path_id"],
            )
    except TraceObservationNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    return JSONResponse(observation)


# Literal segments first: `aggregates`, `proposals` and `decision` are declared
# ahead of the bare `{cohort_id}` route so a literal can never be read as an id.
trace_observation_routes = [
    Route(
        f"{_BASE}/observed-cohorts",
        endpoint=_observed_cohorts_collection,
        methods=["GET", "POST"],
    ),
    Route(
        f"{_BASE}/observed-cohorts/{{cohort_id}}/aggregates",
        endpoint=_get_cohort_aggregates,
        methods=["GET"],
    ),
    Route(
        f"{_BASE}/observed-cohorts/{{cohort_id}}/proposals/{{proposal_id}}/decision",
        endpoint=_decide_proposal,
        methods=["POST"],
    ),
    Route(
        f"{_BASE}/observed-cohorts/{{cohort_id}}/proposals",
        endpoint=_cohort_proposals,
        methods=["GET", "POST"],
    ),
    Route(
        f"{_BASE}/observed-cohorts/{{cohort_id}}",
        endpoint=_get_cohort,
        methods=["GET"],
    ),
    Route(
        f"{_BASE}/trace-observations/{{ai_path_id}}",
        endpoint=_get_trace_observation,
        methods=["GET"],
    ),
]
