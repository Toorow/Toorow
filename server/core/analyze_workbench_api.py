"""Story 50.2 -- HTTP adapters for the Explore facets and the Result lenses.

THIN BY CONTRACT. Every handler translates HTTP into `core.analyze_workbench`
and back. No SQL, no composition and no outcome logic lives here, so a future MCP
adapter (Story 50.6) reaches the same lenses through the same module without a
second read path forming behind it.

WHY THESE ROUTES AND NOT MORE OF STORY 50.1's. `core.query_specs_api` already
owns creating, versioning, executing and retrieving. Those are untouched and this
module never duplicates one of them: `/query-facets` composes the governed option
bundle AC3 needs (which `/query-options` does not carry), `/results/{id}/lens/…`
composes the six read projections AC6-AC11 need, and
`/query-spec-versions/{id}` returns the immutable spec that Story 50.1's
`GET /query-specs/{id}` lists by identity but does not include. A second way to
CREATE anything would be the competing API the story forbids; there is none here,
and every route below is GET.

ROUTE ORDER IS LOAD-BEARING. `query-facets` is a literal segment declared before
any parameterized route in this list, and `/results/{id}/lens/{lens}` is declared
before nothing that could swallow it. When the orchestrator splices this list
into the application it must keep the order, and the seam test asserts it.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core.analyze_workbench import (
    LENSES,
    UnknownLens,
    WorkbenchNotFound,
    compose_query_facets,
    compose_result_lens,
    load_query_spec_version,
)

# The RLS floor is armed in ONE place for all four Analyze modules; see
# `core.query_specs_api.analyze_connection` for why it cannot live in `_authorize`.
from core.query_specs_api import analyze_connection

_BASE = "/api/projects/{project_id}/analyze"

#: One envelope for foreign, denied and absent -- the same literal
#: `core.query_specs_api` uses, so the two halves of the analytical API cannot be
#: told apart by their denial text either.
_NOT_FOUND = {"code": "not_found", "message": "Not found"}


async def _authorize(request: Request, role: str = "viewer"):
    """Return (identity, org_id) or a Response. Denial answers before any work.

    Deliberately identical to `core.query_specs_api._authorize` in behaviour and
    in ORDER: authentication, then role, then Project resolution. Doing the work
    first and checking afterwards would make response time a signal about whether
    an object exists.
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


async def _query_facets(request: Request) -> Response:
    """GET {base}/query-facets -- AC3's one governed option response."""
    auth = await _authorize(request, "viewer")
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth
    project_id = request.path_params["project_id"]
    view_id = request.query_params.get("semantic_view_id") or ""
    version_id = request.query_params.get("semantic_view_version_id") or ""
    if not view_id or not version_id:
        # Both or neither. A half pin would force the server to choose a version,
        # and a shared address that follows whatever became current is exactly
        # what pinning exists to prevent.
        return JSONResponse(
            {
                "code": "missing_field",
                "message": "semantic_view_id and semantic_view_version_id are both required",
            },
            400,
        )

    try:
        with analyze_connection(identity) as conn:
            body = compose_query_facets(
                conn,
                org_id=org_id,
                project_id=project_id,
                semantic_view_id=view_id,
                semantic_view_version_id=version_id,
            )
    except WorkbenchNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    return JSONResponse(body)


async def _result_lens(request: Request) -> Response:
    """GET {base}/results/{id}/lens/{lens} -- one of the six declared lenses."""
    auth = await _authorize(request, "viewer")
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth
    project_id = request.path_params["project_id"]
    result_id = request.path_params["result_id"]
    lens = request.path_params["lens"]

    try:
        with analyze_connection(identity) as conn:
            body = compose_result_lens(
                conn,
                org_id=org_id,
                project_id=project_id,
                result_id=result_id,
                lens=lens,
                include_feedback_context=True,
            )
            conn.commit()
    except UnknownLens:
        # 404, not 400, and not a redirect to `view`: an address naming a lens
        # this contract does not declare must stay Unknown rather than quietly
        # showing a different lens under the address someone shared.
        return JSONResponse(
            {"code": "unknown_lens", "message": f"Unknown lens. Declared: {', '.join(LENSES)}"},
            404,
        )
    except WorkbenchNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    return JSONResponse(body)


async def _query_spec_version(request: Request) -> Response:
    """GET {base}/query-spec-versions/{id} -- the immutable analytical intent."""
    auth = await _authorize(request, "viewer")
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth
    project_id = request.path_params["project_id"]
    version_id = request.path_params["query_spec_version_id"]

    try:
        with analyze_connection(identity) as conn:
            body = load_query_spec_version(
                conn,
                org_id=org_id,
                project_id=project_id,
                query_spec_version_id=version_id,
            )
    except WorkbenchNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    return JSONResponse(body)


#: Mounted by `core.admin_api`. Literal segments first, as in
#: `core.query_specs_api`: `query-facets` and `query-spec-versions` can never be
#: read as an identifier because no parameterized route precedes them here.
ANALYZE_WORKBENCH_ROUTES = [
    Route(f"{_BASE}/query-facets", endpoint=_query_facets, methods=["GET"]),
    Route(
        f"{_BASE}/query-spec-versions/{{query_spec_version_id}}",
        endpoint=_query_spec_version,
        methods=["GET"],
    ),
    Route(
        f"{_BASE}/results/{{result_id}}/lens/{{lens}}",
        endpoint=_result_lens,
        methods=["GET"],
    ),
]

#: Alias kept so a caller can spread either name. `query_spec_routes` in the
#: sibling module uses the lowercase form; the uppercase one matches the majority
#: convention in `admin_api`.
ROUTES = ANALYZE_WORKBENCH_ROUTES
