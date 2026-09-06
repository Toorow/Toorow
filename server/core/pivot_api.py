"""toorow -- the pivot door (story 66.6).

  POST /api/projects/{project_id}/analyze/results/{result_id}/pivot

A POST for a read, and deliberately: the request carries wells, filters and a
grand-total policy, and a query string that held them would be unreadable and
capped by URL length long before the pivot was. Nothing is written.

WHAT IT REFUSES. 422 with the exact code when the request does not fit the
Result -- an unknown field, a measure in the rows well, no value, the same field
twice. Each refusal names what the Result actually carries, so a screen can
repair the request instead of guessing.

WHAT IT NEVER DOES. It does not re-run the query. A pivot is a projection of one
immutable Result, and the response repeats that Result's id and content hash so
a caller can prove the table, the pivot and the chart are the same answer.
"""

from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger(__name__)

_PIVOT = "/api/projects/{project_id}/analyze/results/{result_id}/pivot"


async def _check_auth(request: Request) -> tuple[bool, str]:
    from core.admin_api import _check_auth as _admin_check_auth  # noqa: PLC0415

    return await _admin_check_auth(request)


def _not_found() -> Response:
    return JSONResponse({"code": "not_found", "message": "Not found."}, status_code=404)


def _guard(project_id: str, identity: str) -> tuple[str | None, Response | None]:
    from core.project_access import resolve_strict_resource_access  # noqa: PLC0415
    from core.query_specs_api import analyze_connection  # noqa: PLC0415

    try:
        with analyze_connection(identity) as conn:
            decision = resolve_strict_resource_access(
                identity, conn, project_id=project_id, minimum_capability="view"
            )
    except Exception as exc:  # noqa: BLE001
        logger.error("pivot_api: guard failed project=%s: %s", project_id, exc)
        return None, JSONResponse(
            {"code": "server_error", "message": "Server error."}, status_code=500
        )
    if not decision.allowed or not decision.org_id:
        return None, _not_found()
    return str(decision.org_id), None


def load_result_payload(conn, *, org_id: str, project_id: str, result_id: str) -> dict:
    """The immutable Result this pivot projects, read inside its own scope.

    Scope is part of the WHERE clause rather than a check applied to a row already
    read: a foreign Result does not resolve, and answers exactly like an absent
    one.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.content_hash, r.outcome, r.truncated, p.result_schema, p.rows_chunk
              FROM app.query_results r
              JOIN app.query_result_payloads p ON p.result_id = r.id
             WHERE r.id = %s AND r.org_id = %s AND r.project_id = %s
            """,
            (result_id, org_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        raise LookupError(result_id)
    return {
        "content_hash": row[0],
        "outcome": row[1],
        "truncated": bool(row[2]),
        "schema": row[3] or {"fields": []},
        "rows": row[4] or [],
    }


async def _pivot(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentication is required."}, status_code=401
        )
    project_id = request.path_params["project_id"]
    result_id = request.path_params["result_id"]
    org_id, refusal = _guard(project_id, identity)
    if refusal is not None:
        return refusal

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    if not isinstance(body, dict):
        body = {}

    from core.pivot_projection import PivotRefused, project  # noqa: PLC0415
    from core.query_specs_api import analyze_connection  # noqa: PLC0415

    try:
        with analyze_connection(identity) as conn:
            payload = load_result_payload(
                conn, org_id=org_id, project_id=project_id, result_id=result_id
            )
    except LookupError:
        return _not_found()
    except Exception as exc:  # noqa: BLE001
        logger.error("pivot_api: result read failed project=%s: %s", project_id, exc)
        return JSONResponse(
            {
                "code": "result_unavailable",
                "message": (
                    "That Result could not be read, so it cannot be pivoted. This is not "
                    "an empty answer."
                ),
            },
            status_code=503,
        )

    if payload["outcome"] not in {"success", "empty"}:
        # A refused or unavailable Result has no values to rearrange, and saying
        # so beats returning an empty matrix that would read as "nothing matched".
        return JSONResponse(
            {
                "code": "result_not_pivotable",
                "message": (
                    f"That Result is {payload['outcome']}, so there is nothing to pivot. "
                    "Its evidence stays readable on the Result itself."
                ),
                "outcome": payload["outcome"],
            },
            status_code=422,
        )

    if payload["truncated"]:
        return JSONResponse(
            {
                "code": "truncated_result_not_pivotable",
                "message": (
                    "That Result is a bounded prefix, so a pivot or total would be partial. "
                    "Narrow the analysis and run it again."
                ),
            },
            status_code=422,
        )

    try:
        matrix = project(
            result_id=result_id,
            content_hash=payload["content_hash"],
            schema=payload["schema"],
            rows=payload["rows"],
            request=body,
            # The continuation binds the Project too: a token minted here can
            # never resume a page of another Project's Result, whatever the
            # request repeats.
            project_id=project_id,
        )
    except PivotRefused as exc:
        return JSONResponse(
            {"code": exc.code, "message": exc.message, "detail": exc.detail}, status_code=422
        )

    return JSONResponse({"project_id": project_id, "pivot": matrix})


PIVOT_ROUTES: list[Route] = [Route(_PIVOT, _pivot, methods=["POST"])]

__all__ = ["PIVOT_ROUTES", "load_result_payload"]
