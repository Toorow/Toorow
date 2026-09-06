"""Story 50.1 -- the server-owned analytical API.

ONE SERVICE, MANY ADAPTERS. Every handler here is a thin translation of HTTP into
`core.query_specs` and `core.query_execution`. No validation, no SQL and no
outcome logic lives in this file, so a future MCP adapter can call the same two
modules and reach the same Results without a second analytical path (AC1, AC12).

NON-DISCLOSURE IS THE DEFAULT. `QuerySpecNotFound` becomes the same 404 envelope
whether the object is foreign, denied or absent, and the denial path answers
before any work is done so response timing does not become an enumeration oracle
(AC10). Refusals are 422 with their full structured reason list, because a caller
that cannot see why it was refused will guess -- and guessing is how a semantic
substitution gets built in a browser.

ROUTE ORDER. Literal segments are declared before parameterized ones, so
`/results/recent` can never be captured as a Result id. The order in
`query_spec_routes` is load-bearing, not cosmetic.

THE RLS FLOOR IS ARMED HERE, FOR ALL FOUR ANALYZE MODULES. See
`analyze_connection` below: every connection that touches an Analyze table is
opened through it, so the Epic-36 policies stop being decorative. The three
sibling modules import it rather than reproducing it.
"""

from __future__ import annotations

import json
from contextlib import contextmanager

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core import render_app_payload
from core.query_execution import accept_execution, run_execution
from core.query_specs import (
    DEFAULT_LIST_LIMIT,
    MAX_LIST_LIMIT,
    QuerySpecNotFound,
    QuerySpecRefused,
    create_query_spec_version,
    list_query_specs,
    validate_query_spec,
)

_BASE = "/api/projects/{project_id}/analyze"

#: One envelope for foreign, denied and absent. See the module docstring.
_NOT_FOUND = {"code": "not_found", "message": "Not found"}


def arm_access_floor(conn, identity: str) -> None:
    """Arm the Epic-36 floor on a BORROWED connection, for its transaction only.

    Kept for callers that hand in a connection they do not own -- tests, mostly.
    Anything that OPENS its connection should use `analyze_connection` below, so
    the context lives as long as the connection does. The auth-disabled
    `anonymous` carve-out is not decided here any more: `core.db` owns the rule,
    once, for the whole server.
    """
    from core.db import _auth_is_disabled, set_local_access_context  # noqa: PLC0415

    if _auth_is_disabled() and (not identity or identity == "anonymous"):
        return
    set_local_access_context(conn, identity or "anonymous", enforce_epic36=True)


@contextmanager
def analyze_connection(identity: str):
    """Yield a connection whose Epic-36 RLS floor is armed ON THAT CONNECTION.

    THE TRAP THIS CLOSES, and why the repair was never one line in `_authorize`:
    `_authorize` opens a connection of its own and closes it before it returns;
    every handler then opens a NEW one. Arming the first protects nothing.

    Story 21.6 generalized this seam from these four Analyze modules to the whole
    request path -- it is now `core.db.request_connection`, and this name is the
    Analyze-side alias so the four modules keep reading the way they read.
    """
    from core.db import request_connection  # noqa: PLC0415

    with request_connection(identity) as conn:
        yield conn


async def _authorize(request: Request, role: str = "viewer"):
    """Return (identity, org_id) or a Response. Denial answers before any work.

    The connection opened here resolves AUTHORIZATION -- project membership and
    the owning organization -- and never reads an Analyze table, so it is
    deliberately left unarmed: arming it would change which rows
    `identity_has_project_role` itself can see, which is a change to who is
    authorized rather than the addition of a floor.
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


def _refused(exc: QuerySpecRefused) -> Response:
    return JSONResponse(exc.as_dict(), 422)


async def _query_options(request: Request) -> Response:
    """GET {base}/query-options -- the governed choices for one exact pinned version.

    The server composes this from the SAME compiled artifact the validator reads,
    so what the UI offers and what the server accepts cannot drift. A browser that
    assembled its own member catalogue would be a second semantic authority, and
    would keep offering a member the day the compiler stops proving it.
    """
    auth = await _authorize(request, "viewer")
    if isinstance(auth, Response):
        return auth
    identity, _org_id = auth
    project_id = request.path_params["project_id"]
    view_id = request.query_params.get("semantic_view_id") or ""
    version_id = request.query_params.get("semantic_view_version_id") or ""
    if not view_id or not version_id:
        return JSONResponse(
            {
                "code": "missing_field",
                "message": "semantic_view_id and semantic_view_version_id are both required",
            },
            400,
        )

    # BOTH reads live inside the cursor block. They did not: `with conn.cursor()`
    # covered only the first `execute`, and everything after it worked on an
    # already-closed cursor -- so this route answered 500 on every call since the
    # day it was written. A `with` that closes one line too early breaks nothing
    # when you read the code, and everything when you run it.
    with analyze_connection(identity) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT status FROM app.semantic_view_versions
                WHERE id = %s AND view_id = %s AND project_id = %s
                """,
                (version_id, view_id, project_id),
            )
            version = cur.fetchone()
            if version is None:
                return JSONResponse(_NOT_FOUND, 404)
            cur.execute(
                """
                SELECT queryability_matrix FROM app.semantic_compiled_artifacts
                WHERE view_version_id = %s AND project_id = %s
                ORDER BY created_at DESC LIMIT 1
                """,
                (version_id, project_id),
            )
            compiled = cur.fetchone()

    matrix = (compiled[0] if compiled else None) or {}
    return JSONResponse(
        {
            "semantic_view_id": view_id,
            "semantic_view_version_id": version_id,
            # `executable` is stated rather than implied: a superseded version is
            # still readable, and the caller must be able to tell the difference
            # without inferring it from an empty member list.
            "executable": str(version[0]) == "published" and bool(matrix),
            "status": version[0],
            "measures": matrix.get("metrics") or [],
            "dimensions": matrix.get("dimensions") or [],
            "pairs": [
                {
                    "measure_id": c.get("metric_id"),
                    "dimension_id": c.get("dimension_id"),
                    "queryable": bool(c.get("queryable")),
                }
                for c in (matrix.get("cells") or [])
            ],
        }
    )


async def _list_query_specs(request: Request) -> Response:
    """GET {base}/query-specs?limit=&cursor= -- the Project's Query Specs.

    WHY THIS ROUTE EXISTS. `POST /query-specs` and `GET /query-specs/{id}` were
    the whole surface, so every caller that had to NAME a Query Spec had to
    already know its identifier. The Answerable Topic catalog is that caller: it
    pins `query_spec_version_id` on a topic, and with no collection to read it
    asked an operator to paste a `qsv_…` from memory. A governed pin that can
    only be made by someone who memorized an id is a governed pin nobody makes.

    Same authorization as `GET /query-specs/{id}` -- `viewer`, and the Project
    scope resolved by `_authorize` rather than taken from the query string. The
    Project is in the PATH here, as it is for every route in this module; a
    `project_id` parameter would be a second scope for the same request, and the
    two would disagree the day one of them is wrong.

    Bounded by construction: `limit` is clamped by the store, and a `limit` or
    `cursor` that is not what it claims to be is refused rather than coerced --
    a silently defaulted bound is how a picker starts showing a page nobody
    asked for.
    """
    auth = await _authorize(request, "viewer")
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth
    project_id = request.path_params["project_id"]

    unknown = set(request.query_params) - {"limit", "cursor"}
    if unknown:
        return JSONResponse(
            {"code": "invalid_query", "message": f"unsupported filter: {sorted(unknown)[0]}"},
            422,
        )
    raw_limit = request.query_params.get("limit")
    try:
        limit = int(raw_limit) if raw_limit not in (None, "") else DEFAULT_LIST_LIMIT
    except ValueError:
        return JSONResponse({"code": "invalid_query", "message": "limit must be an integer"}, 422)
    if not 1 <= limit <= MAX_LIST_LIMIT:
        return JSONResponse(
            {"code": "invalid_query", "message": f"limit must be 1..{MAX_LIST_LIMIT}"},
            422,
        )

    with analyze_connection(identity) as conn:
        payload = list_query_specs(
            conn,
            org_id=org_id,
            project_id=project_id,
            limit=limit,
            cursor=request.query_params.get("cursor") or None,
        )
    # An empty Project answers `{"query_specs": []}` and 200. It has authored no
    # Query Spec; that is an answer, and turning it into a 404 would make the
    # caller unable to tell it from a Project it may not read.
    return JSONResponse(payload)


async def _create_query_spec(request: Request) -> Response:
    """POST {base}/query-specs -- validate, then append version 1."""
    auth = await _authorize(request, "member")
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth
    project_id = request.path_params["project_id"]
    try:
        body = await _json_body(request)
    except (ValueError, json.JSONDecodeError) as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)

    view_id = str(body.get("semantic_view_id") or "")
    version_id = str(body.get("semantic_view_version_id") or "")
    if not view_id or not version_id:
        # Both or neither: a half pin would make the server choose a version, and
        # choosing is exactly what pinning exists to prevent.
        return JSONResponse(
            {
                "code": "missing_field",
                "message": "semantic_view_id and semantic_view_version_id are both required",
            },
            400,
        )

    try:
        with analyze_connection(identity) as conn:
            validated = validate_query_spec(
                conn,
                project_id=project_id,
                semantic_view_id=view_id,
                semantic_view_version_id=version_id,
                payload=body.get("spec") or {},
            )
            created = create_query_spec_version(
                conn,
                org_id=org_id,
                project_id=project_id,
                validated=validated,
                actor=identity,
                name=body.get("name"),
            )
            conn.commit()
    except QuerySpecNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    except QuerySpecRefused as exc:
        return _refused(exc)
    return JSONResponse(created, 201)


async def _add_query_spec_version(request: Request) -> Response:
    """POST {base}/query-specs/{id}/versions -- revise, never mutate."""
    auth = await _authorize(request, "member")
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth
    project_id = request.path_params["project_id"]
    query_spec_id = request.path_params["query_spec_id"]
    try:
        body = await _json_body(request)
    except (ValueError, json.JSONDecodeError) as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)

    try:
        with analyze_connection(identity) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT semantic_view_id FROM app.query_specs
                    WHERE id = %s AND org_id = %s AND project_id = %s
                    """,
                    (query_spec_id, org_id, project_id),
                )
                head = cur.fetchone()
            if head is None:
                return JSONResponse(_NOT_FOUND, 404)
            validated = validate_query_spec(
                conn,
                project_id=project_id,
                semantic_view_id=str(head[0]),
                semantic_view_version_id=str(body.get("semantic_view_version_id") or ""),
                payload=body.get("spec") or {},
            )
            created = create_query_spec_version(
                conn,
                org_id=org_id,
                project_id=project_id,
                validated=validated,
                actor=identity,
                query_spec_id=query_spec_id,
            )
            conn.commit()
    except QuerySpecNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    except QuerySpecRefused as exc:
        return _refused(exc)
    return JSONResponse(created, 201)


async def _get_query_spec(request: Request) -> Response:
    """GET {base}/query-specs/{id} -- head plus its immutable version list."""
    auth = await _authorize(request, "viewer")
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth
    project_id = request.path_params["project_id"]
    query_spec_id = request.path_params["query_spec_id"]

    with analyze_connection(identity) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, semantic_view_id, name, current_version_id, created_by, created_at
            FROM app.query_specs WHERE id = %s AND org_id = %s AND project_id = %s
            """,
            (query_spec_id, org_id, project_id),
        )
        head = cur.fetchone()
        if head is None:
            return JSONResponse(_NOT_FOUND, 404)
        cur.execute(
            """
            SELECT id, version_number, semantic_view_version_id, content_hash,
                   predecessor_version_id, created_at
            FROM app.query_spec_versions
            WHERE query_spec_id = %s AND project_id = %s
            ORDER BY version_number DESC
            """,
            (query_spec_id, project_id),
        )
        versions = cur.fetchall()
    return JSONResponse(
        {
            "id": head[0],
            "semantic_view_id": head[1],
            "name": head[2],
            "current_version_id": head[3],
            "created_by": head[4],
            "created_at": head[5].isoformat() if head[5] else None,
            "versions": [
                {
                    "id": v[0],
                    "version_number": v[1],
                    "semantic_view_version_id": v[2],
                    "content_hash": v[3],
                    "predecessor_version_id": v[4],
                    "created_at": v[5].isoformat() if v[5] else None,
                }
                for v in versions
            ],
        }
    )


async def _execute_query_spec_version(request: Request) -> Response:
    """POST {base}/query-spec-versions/{id}/execute -- accept, run, terminalize."""
    auth = await _authorize(request, "member")
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth
    project_id = request.path_params["project_id"]
    version_id = request.path_params["query_spec_version_id"]

    try:
        with analyze_connection(identity) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT spec, semantic_view_version_id FROM app.query_spec_versions
                    WHERE id = %s AND org_id = %s AND project_id = %s
                    """,
                    (version_id, org_id, project_id),
                )
                row = cur.fetchone()
            if row is None:
                return JSONResponse(_NOT_FOUND, 404)
            attempt = accept_execution(
                conn,
                org_id=org_id,
                project_id=project_id,
                query_spec_version_id=version_id,
                actor=identity,
            )
            result = run_execution(
                conn,
                attempt=attempt,
                org_id=org_id,
                project_id=project_id,
                spec=row[0] or {},
                semantic_view_version_id=str(row[1]),
            )
            conn.commit()
    except QuerySpecNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    # 202: the attempt was accepted and a Result exists. Its OUTCOME may well be
    # `unavailable` -- that is a delivered answer, not a failed request, and
    # mapping it to 5xx would hide inspectable evidence behind a transport error.
    return JSONResponse({"attempt_id": attempt["attempt_id"], **result}, 202)


async def _get_result(request: Request) -> Response:
    """GET {base}/results/{id} -- the immutable Result envelope."""
    auth = await _authorize(request, "viewer")
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth
    project_id = request.path_params["project_id"]
    result_id = request.path_params["result_id"]

    with analyze_connection(identity) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, attempt_id, query_spec_version_id, outcome, ai_path_id,
                   ai_path_absent_literal, content_hash, row_count, cell_count,
                   byte_count, truncated, predecessor_result_id, started_at, ended_at
            FROM app.query_results WHERE id = %s AND org_id = %s AND project_id = %s
            """,
            (result_id, org_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        return JSONResponse(_NOT_FOUND, 404)
    return JSONResponse(
        {
            "id": row[0],
            "attempt_id": row[1],
            "query_spec_version_id": row[2],
            "outcome": row[3],
            # Exactly one of the two is set; the database CHECK guarantees it, so
            # a caller never has to reason about a missing AI Path.
            "ai_path": row[4] or row[5],
            "content_hash": row[6],
            "row_count": row[7],
            "cell_count": row[8],
            "byte_count": row[9],
            "truncated": row[10],
            "predecessor_result_id": row[11],
            "started_at": row[12].isoformat() if row[12] else None,
            "ended_at": row[13].isoformat() if row[13] else None,
        }
    )


async def _get_result_evidence(request: Request) -> Response:
    """GET {base}/results/{id}/evidence -- Result truth and bounded payload."""
    auth = await _authorize(request, "viewer")
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth
    project_id = request.path_params["project_id"]
    result_id = request.path_params["result_id"]

    with analyze_connection(identity) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT r.outcome, r.row_count, r.truncated, r.content_hash,
                       p.content_hash, p.result_schema, p.manifest, p.rows_chunk,
                       r.ai_path_id, ap.lifecycle
                FROM app.query_results r
                JOIN app.query_result_payloads p
                  ON p.result_id = r.id
                 AND p.org_id = r.org_id
                 AND p.project_id = r.project_id
                LEFT JOIN app.ai_paths ap
                  ON ap.id = r.ai_path_id
                 AND ap.org_id = r.org_id
                 AND ap.project_id = r.project_id
                WHERE r.id = %s AND r.org_id = %s AND r.project_id = %s
                """,
                (result_id, org_id, project_id),
            )
            row = cur.fetchone()
        if row is None:
            return JSONResponse(_NOT_FOUND, 404)
        if row[3] != row[4]:
            return JSONResponse(
                {
                    "code": "result_identity_mismatch",
                    "message": (
                        "The Result payload does not match its immutable identity. "
                        "Run the analysis again to create a new Result."
                    ),
                },
                409,
            )
        from core.analyze_feedback import (  # noqa: PLC0415
            feedback_fields_from_schema,
            feedback_fields_from_visualization_spec,
            mint_eligible_delivery_feedback_context,
        )
        from core.tracing import current_trace_id_hex  # noqa: PLC0415

        pins = {}
        render_id = (request.query_params.get("render_id") or "").strip()
        if render_id:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT r.id, r.result_id, r.result_content_hash,
                           r.visualization_spec_version_id, r.renderer_build_id,
                           r.runtime_build_id, r.theme_version, r.formatter_version,
                           v.spec
                    FROM app.renders r
                    JOIN app.visualization_spec_versions v
                      ON v.id = r.visualization_spec_version_id
                     AND v.org_id = r.org_id AND v.project_id = r.project_id
                    WHERE r.id = %s AND r.org_id = %s AND r.project_id = %s
                    """,
                    (render_id, org_id, project_id),
                )
                render = cur.fetchone()
            if render is None or render[1] != result_id or render[2] != row[3]:
                return JSONResponse(_NOT_FOUND, 404)
            pins = {
                "render_id": render[0],
                "visualization_spec_version_id": render[3],
                "renderer_build_id": render[4],
                "runtime_build_id": render[5],
                "theme_version": render[6],
                "formatter_version": render[7],
            }
        path_ordinals = None
        if len(row) > 9 and row[8] is not None and row[9] == "finalized":
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT ordinal FROM app.ai_path_steps
                    WHERE path_id = %s AND org_id = %s AND project_id = %s
                    ORDER BY ordinal
                    """,
                    (row[8], org_id, project_id),
                )
                path_ordinals = [int(item[0]) for item in cur.fetchall()]
        delivered_rows = list(row[7] or [])[:1000]
        delivered_fields = (
            feedback_fields_from_visualization_spec(row[5], render[8], row[6])
            if render_id
            else feedback_fields_from_schema(row[5])
        )
        feedback_context = mint_eligible_delivery_feedback_context(
            conn,
            org_id=org_id,
            project_id=project_id,
            result_id=result_id,
            result_content_hash=row[3],
            surface="console",
            stored_rows=row[7],
            delivered_rows=delivered_rows,
            delivered_fields=delivered_fields,
            ai_path_id=row[8] if len(row) > 8 else None,
            path_step_ordinals=path_ordinals,
            pins=pins,
            w3c_trace_id=current_trace_id_hex(),
        )
        conn.commit()
    response_body = {
        "result_id": result_id,
        "content_hash": row[3],
        "outcome": row[0],
        "row_count": row[1],
        "truncated": row[2],
        "schema": row[5],
        "manifest": render_app_payload.project_result_manifest(row[6]),
        "rows": row[7],
    }
    if feedback_context is not None:
        response_body["feedback_context"] = feedback_context
    return JSONResponse(response_body)


# Literal segments first: `query-spec-versions` and `results` are declared ahead
# of any `{query_spec_id}` route so a literal can never be read as an id.
query_spec_routes = [
    Route(f"{_BASE}/query-options", endpoint=_query_options, methods=["GET"]),
    Route(f"{_BASE}/query-specs", endpoint=_list_query_specs, methods=["GET"]),
    Route(f"{_BASE}/query-specs", endpoint=_create_query_spec, methods=["POST"]),
    Route(
        f"{_BASE}/query-spec-versions/{{query_spec_version_id}}/execute",
        endpoint=_execute_query_spec_version,
        methods=["POST"],
    ),
    Route(f"{_BASE}/results/{{result_id}}/evidence", endpoint=_get_result_evidence,
          methods=["GET"]),
    Route(f"{_BASE}/results/{{result_id}}", endpoint=_get_result, methods=["GET"]),
    Route(
        f"{_BASE}/query-specs/{{query_spec_id}}/versions",
        endpoint=_add_query_spec_version,
        methods=["POST"],
    ),
    Route(f"{_BASE}/query-specs/{{query_spec_id}}", endpoint=_get_query_spec, methods=["GET"]),
]
