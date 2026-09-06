"""Story 65.9 -- HTTP doors for feedback-to-regression promotion.

Handlers in this module translate authenticated HTTP requests into the single
``core.feedback_regression`` application service.  Authorization and the
service call intentionally share one request-scoped connection: FORCE RLS must
observe the same OAuth identity that passed the project-role check.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core.feedback_regression import (
    FeedbackRegressionNotFound,
    FeedbackRegressionRefused,
    evaluate_feedback_regression_case,
    get_feedback_regression_draft,
    promote_feedback_regression,
    resolve_feedback_regression,
)

_BASE = "/api/projects/{project_id}/test"
_MAX_HTTP_BODY = 262_144
_NO_STORE = {"Cache-Control": "no-store", "Vary": "Authorization"}
_NOT_FOUND = {"code": "not_found", "message": "Not found"}


def _not_found() -> Response:
    return JSONResponse(_NOT_FOUND, 404, headers=_NO_STORE)


@asynccontextmanager
async def _authorized_connection(request: Request, role: str):
    """Yield auth plus the identity-bound connection, or one HTTP response."""
    from core.admin_api import _check_auth, _require_datastream_role  # noqa: PLC0415
    from core.db import request_connection  # noqa: PLC0415

    authorized, identity = await _check_auth(request)
    if not authorized:
        yield JSONResponse(
            {"code": "unauthorized", "message": "Authentication required"},
            401,
            headers=_NO_STORE,
        )
        return
    project_id = request.path_params["project_id"]
    with request_connection(str(identity)) as conn:
        denied = _require_datastream_role(project_id, identity, role, conn)
        if denied is not None:
            yield _not_found()
            return
        with conn.cursor() as cur:
            cur.execute("SELECT org_id FROM app.projects WHERE id = %s", (project_id,))
            row = cur.fetchone()
        if row is None:
            yield _not_found()
            return
        yield str(identity), str(row[0]), conn


def _no_query(request: Request) -> None:
    if request.query_params:
        raise FeedbackRegressionRefused(
            "unknown_query_parameter",
            f"Unknown query parameter: {sorted(request.query_params.keys())[0]}",
            [],
        )


async def _json_body(request: Request) -> dict:
    raw = await request.body()
    if len(raw) > _MAX_HTTP_BODY:
        raise OverflowError("request body exceeds 262144 bytes")
    if not raw.strip():
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("request body must be an object")
    return value


def _refused(exc: FeedbackRegressionRefused) -> Response:
    status = 409 if exc.code in {
        "idempotency_conflict",
        "stale_review_head",
        "stale_review_version",
    } else 422
    return JSONResponse(exc.as_dict(), status, headers=_NO_STORE)


def _invalid_body(exc: Exception) -> Response:
    return JSONResponse(
        {"code": "invalid_body", "message": str(exc)}, 400, headers=_NO_STORE
    )


def _bounded_json(document: object, status: int = 200) -> Response:
    encoded = json.dumps(document, separators=(",", ":"), default=str).encode("utf-8")
    if len(encoded) > _MAX_HTTP_BODY:
        return JSONResponse(
            {"code": "response_too_large", "message": "Response exceeds 262144 bytes."},
            413,
            headers=_NO_STORE,
        )
    return Response(encoded, status, media_type="application/json", headers=_NO_STORE)


async def _get_draft(request: Request) -> Response:
    async with _authorized_connection(request, "viewer") as auth:
        if isinstance(auth, Response):
            return auth
        _identity, org_id, conn = auth
        try:
            _no_query(request)
            payload = get_feedback_regression_draft(
                conn,
                org_id=org_id,
                project_id=request.path_params["project_id"],
                feedback_id=request.path_params["feedback_id"],
            )
        except FeedbackRegressionNotFound:
            return _not_found()
        except FeedbackRegressionRefused as exc:
            return _refused(exc)
        return _bounded_json(payload)


async def _create_regression_case(request: Request) -> Response:
    async with _authorized_connection(request, "member") as auth:
        if isinstance(auth, Response):
            return auth
        identity, org_id, conn = auth
        try:
            _no_query(request)
            body = await _json_body(request)
        except FeedbackRegressionRefused as exc:
            return _refused(exc)
        except OverflowError as exc:
            return JSONResponse(
                {"code": "body_too_large", "message": str(exc)}, 413, headers=_NO_STORE
            )
        except (ValueError, json.JSONDecodeError) as exc:
            return _invalid_body(exc)
        try:
            receipt = promote_feedback_regression(
                conn,
                org_id=org_id,
                project_id=request.path_params["project_id"],
                feedback_id=request.path_params["feedback_id"],
                actor=identity,
                payload=body,
            )
            conn.commit()
        except FeedbackRegressionNotFound:
            conn.rollback()
            return _not_found()
        except FeedbackRegressionRefused as exc:
            conn.rollback()
            return _refused(exc)
        except Exception:
            conn.rollback()
            raise
        return _bounded_json(receipt, 200 if receipt.get("status") == "replayed" else 201)


async def _evaluate_case(request: Request) -> Response:
    async with _authorized_connection(request, "member") as auth:
        if isinstance(auth, Response):
            return auth
        identity, org_id, conn = auth
        try:
            _no_query(request)
            body = await _json_body(request)
        except FeedbackRegressionRefused as exc:
            return _refused(exc)
        except OverflowError as exc:
            return JSONResponse(
                {"code": "body_too_large", "message": str(exc)}, 413, headers=_NO_STORE
            )
        except (ValueError, json.JSONDecodeError) as exc:
            return _invalid_body(exc)
        try:
            receipt = evaluate_feedback_regression_case(
                conn,
                org_id=org_id,
                project_id=request.path_params["project_id"],
                case_id=request.path_params["case_id"],
                actor=identity,
                payload=body,
            )
            conn.commit()
        except FeedbackRegressionNotFound:
            conn.rollback()
            return _not_found()
        except FeedbackRegressionRefused as exc:
            conn.rollback()
            return _refused(exc)
        except Exception:
            conn.rollback()
            raise
        return _bounded_json(receipt, 200 if receipt.get("status") == "replayed" else 201)


async def _resolve_feedback(request: Request) -> Response:
    async with _authorized_connection(request, "member") as auth:
        if isinstance(auth, Response):
            return auth
        identity, org_id, conn = auth
        try:
            _no_query(request)
            body = await _json_body(request)
        except FeedbackRegressionRefused as exc:
            return _refused(exc)
        except OverflowError as exc:
            return JSONResponse(
                {"code": "body_too_large", "message": str(exc)}, 413, headers=_NO_STORE
            )
        except (ValueError, json.JSONDecodeError) as exc:
            return _invalid_body(exc)
        try:
            receipt = resolve_feedback_regression(
                conn,
                org_id=org_id,
                project_id=request.path_params["project_id"],
                feedback_id=request.path_params["feedback_id"],
                actor=identity,
                payload=body,
            )
            conn.commit()
        except FeedbackRegressionNotFound:
            conn.rollback()
            return _not_found()
        except FeedbackRegressionRefused as exc:
            conn.rollback()
            return _refused(exc)
        except Exception:
            conn.rollback()
            raise
        return _bounded_json(receipt, 201 if receipt.get("status") == "resolved" else 200)


feedback_regression_routes = [
    Route(
        f"{_BASE}/feedback/{{feedback_id}}/regression-draft",
        endpoint=_get_draft,
        methods=["GET"],
    ),
    Route(
        f"{_BASE}/feedback/{{feedback_id}}/regression-cases",
        endpoint=_create_regression_case,
        methods=["POST"],
    ),
    Route(
        f"{_BASE}/feedback/{{feedback_id}}/regression-resolution",
        endpoint=_resolve_feedback,
        methods=["POST"],
    ),
    Route(
        f"{_BASE}/evaluation-cases/{{case_id}}/evaluate",
        endpoint=_evaluate_case,
        methods=["POST"],
    ),
]
