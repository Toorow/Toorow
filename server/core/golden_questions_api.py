"""Story 51.1 -- the server-owned Golden Question capability.

ONE SERVICE, THIN HANDLERS. Every handler here is a translation of HTTP into
`core.golden_questions`. No validation, no SQL and no outcome logic lives in this
file, so the console and any future MCP adapter reach the same object through the
same rules -- the module contract of `server/core/query_specs_api.py:1-18`.

NON-DISCLOSURE IS THE DEFAULT. `GoldenQuestionNotFound` becomes the same 404
envelope whether the object is foreign, denied or absent, and the denial path
answers before any work is done so response timing does not become an enumeration
oracle. Refusals are 422 carrying the full structured reason list, because a
caller that cannot see why it was refused will guess.

ROUTE ORDER IS LOAD-BEARING. `/golden-questions/options` is declared before
`/golden-questions/{golden_question_id}`, so the literal can never be captured as
a question id.

THIS FILE DOES NOT MOUNT ITSELF. `server/core/admin_api.py` belongs to another
session; this module exports `golden_question_routes` and the orchestrator adds
the import and the spread, exactly as Story 50.1 did for `query_spec_routes`.
"""

from __future__ import annotations

import json

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core.golden_questions import (
    GoldenQuestionNotFound,
    GoldenQuestionRefused,
    create_golden_question,
    create_golden_question_version,
    get_golden_question,
    get_golden_question_version,
    golden_question_coverage,
    golden_question_options,
    list_golden_questions,
    set_lifecycle,
    validate_golden_question_version,
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


def _refused(exc: GoldenQuestionRefused) -> Response:
    return JSONResponse(exc.as_dict(), 422)


async def _list_golden_questions(request: Request) -> Response:
    """GET {base}/golden-questions -- the real product collection."""
    auth = await _authorize(request, "viewer")
    if isinstance(auth, Response):
        return auth
    _identity, org_id = auth
    project_id = request.path_params["project_id"]
    unknown = set(request.query_params) - {"q", "lifecycle", "limit", "cursor"}
    if unknown:
        return JSONResponse(
            {"code": "invalid_query", "message": f"unsupported filter: {sorted(unknown)[0]}"},
            422,
        )
    lifecycle = request.query_params.get("lifecycle") or None
    query = (request.query_params.get("q") or "").strip().casefold()
    try:
        limit = int(request.query_params.get("limit") or 25)
        cursor = int(request.query_params.get("cursor") or 0)
    except ValueError:
        return JSONResponse(
            {"code": "invalid_query", "message": "limit and cursor must be integers"},
            422,
        )
    if not 1 <= limit <= 100 or cursor < 0:
        return JSONResponse(
            {
                "code": "invalid_query",
                "message": "limit must be 1..100 and cursor must be positive",
            },
            422,
        )

    from core.db import get_connection  # noqa: PLC0415

    with get_connection() as conn:
        questions = list_golden_questions(
            conn, org_id=org_id, project_id=project_id, lifecycle=lifecycle
        )
    if query:
        questions = [
            question for question in questions
            if query in " ".join(
                str(question.get(key) or "") for key in ("id", "title", "owner")
            ).casefold()
        ]
    total = len(questions)
    if cursor and cursor >= total:
        return JSONResponse(
            {
                "code": "invalid_query",
                "message": "cursor does not reference an available collection page",
            },
            422,
        )
    page = questions[cursor : cursor + limit]
    next_cursor = cursor + limit if cursor + limit < total else None
    return JSONResponse({
        "golden_questions": page,
        "total": total,
        "bound": limit,
        "next_cursor": str(next_cursor) if next_cursor is not None else None,
        "applied_filters": {"q": query, "lifecycle": lifecycle or ""},
    })


async def _create_golden_question(request: Request) -> Response:
    """POST {base}/golden-questions -- validate, then create head plus version 1."""
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
            validated = validate_golden_question_version(
                conn,
                org_id=org_id,
                project_id=project_id,
                payload=body.get("definition") or {},
            )
            created = create_golden_question(
                conn,
                org_id=org_id,
                project_id=project_id,
                title=body.get("title") or "",
                owner=body.get("owner") or "",
                validated=validated,
                actor=identity,
            )
            conn.commit()
    except GoldenQuestionNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    except GoldenQuestionRefused as exc:
        return _refused(exc)
    return JSONResponse(created, 201)


async def _get_options(request: Request) -> Response:
    """GET {base}/golden-questions/options -- the governed pickers, live."""
    auth = await _authorize(request, "viewer")
    if isinstance(auth, Response):
        return auth
    _identity, org_id = auth
    project_id = request.path_params["project_id"]

    from core.db import get_connection  # noqa: PLC0415

    with get_connection() as conn:
        options = golden_question_options(conn, org_id=org_id, project_id=project_id)
    return JSONResponse(options)


async def _get_golden_question(request: Request) -> Response:
    """GET {base}/golden-questions/{id} -- head plus its immutable version list."""
    auth = await _authorize(request, "viewer")
    if isinstance(auth, Response):
        return auth
    _identity, org_id = auth
    project_id = request.path_params["project_id"]

    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            payload = get_golden_question(
                conn,
                org_id=org_id,
                project_id=project_id,
                golden_question_id=request.path_params["golden_question_id"],
            )
    except GoldenQuestionNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    return JSONResponse(payload)


async def _add_version(request: Request) -> Response:
    """POST {base}/golden-questions/{id}/versions -- revise, never mutate."""
    auth = await _authorize(request, "member")
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth
    project_id = request.path_params["project_id"]
    golden_question_id = request.path_params["golden_question_id"]
    try:
        body = await _json_body(request)
    except (ValueError, json.JSONDecodeError) as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)

    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            validated = validate_golden_question_version(
                conn,
                org_id=org_id,
                project_id=project_id,
                payload=body.get("definition") or {},
            )
            created = create_golden_question_version(
                conn,
                org_id=org_id,
                project_id=project_id,
                golden_question_id=golden_question_id,
                validated=validated,
                actor=identity,
            )
            conn.commit()
    except GoldenQuestionNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    except GoldenQuestionRefused as exc:
        return _refused(exc)
    return JSONResponse(created, 201)


async def _get_version(request: Request) -> Response:
    """GET {base}/golden-questions/{id}/versions/{version_id} -- one frozen version."""
    auth = await _authorize(request, "viewer")
    if isinstance(auth, Response):
        return auth
    _identity, org_id = auth
    project_id = request.path_params["project_id"]

    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            payload = get_golden_question_version(
                conn,
                org_id=org_id,
                project_id=project_id,
                golden_question_id=request.path_params["golden_question_id"],
                version_id=request.path_params["version_id"],
            )
    except GoldenQuestionNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    return JSONResponse(payload)


async def _get_coverage(request: Request) -> Response:
    """GET {base}/golden-questions/{id}/coverage -- pins, and honest absences.

    Every dimension whose owner is undelivered answers `unverifiable` with its
    reason code and owner story. Nothing here can read as green.
    """
    auth = await _authorize(request, "viewer")
    if isinstance(auth, Response):
        return auth
    _identity, org_id = auth
    project_id = request.path_params["project_id"]

    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            payload = golden_question_coverage(
                conn,
                org_id=org_id,
                project_id=project_id,
                golden_question_id=request.path_params["golden_question_id"],
            )
    except GoldenQuestionNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    return JSONResponse(payload)


async def _set_lifecycle(request: Request) -> Response:
    """POST {base}/golden-questions/{id}/lifecycle -- declared transitions only."""
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
            payload = set_lifecycle(
                conn,
                org_id=org_id,
                project_id=project_id,
                golden_question_id=request.path_params["golden_question_id"],
                lifecycle=str(body.get("lifecycle") or ""),
                owner=body.get("owner"),
                actor=identity,
            )
            conn.commit()
    except GoldenQuestionNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    except GoldenQuestionRefused as exc:
        return _refused(exc)
    return JSONResponse(payload)


# Literal segments first: `options` is declared ahead of `{golden_question_id}`,
# and `versions` ahead of nothing that could shadow it. The order is a contract,
# not a formatting choice.
golden_question_routes = [
    Route(f"{_BASE}/golden-questions", endpoint=_list_golden_questions, methods=["GET"]),
    Route(f"{_BASE}/golden-questions", endpoint=_create_golden_question, methods=["POST"]),
    Route(f"{_BASE}/golden-questions/options", endpoint=_get_options, methods=["GET"]),
    Route(
        f"{_BASE}/golden-questions/{{golden_question_id}}/versions/{{version_id}}",
        endpoint=_get_version,
        methods=["GET"],
    ),
    Route(
        f"{_BASE}/golden-questions/{{golden_question_id}}/versions",
        endpoint=_add_version,
        methods=["POST"],
    ),
    Route(
        f"{_BASE}/golden-questions/{{golden_question_id}}/coverage",
        endpoint=_get_coverage,
        methods=["GET"],
    ),
    Route(
        f"{_BASE}/golden-questions/{{golden_question_id}}/lifecycle",
        endpoint=_set_lifecycle,
        methods=["POST"],
    ),
    Route(
        f"{_BASE}/golden-questions/{{golden_question_id}}",
        endpoint=_get_golden_question,
        methods=["GET"],
    ),
]
