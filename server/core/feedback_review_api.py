"""Story 51.5 -- the Test feedback API: thin HTTP over one application service.

ONE SERVICE, MANY ADAPTERS. Every handler here is a translation of HTTP into
`core.feedback_review`. No validation, no SQL and no vocabulary lives in this
file, so an MCP adapter can call the same module and reach the same rows without
a second feedback path. That matters concretely: `submit_feedback`
(`core/main.py`) is a real MCP tool whose current store cannot pin a Result, and
the only way it stops being a second writer is for there to be nothing to
reimplement here.

NAMESPACE. These routes live under `/api/projects/{project_id}/test/feedback`.
The legacy `GET /api/feedback` (Story 5.5) is a different, unpinned read and is
deliberately not covered, aliased or redirected by this prefix: shadowing it
would make an old caller believe it had been upgraded.

NON-DISCLOSURE IS THE DEFAULT. Foreign, denied and absent all answer the same
404 envelope, and the denial path answers before any work is done so response
timing does not become an enumeration oracle. Refusals are 422 with their full
structured reason list, because a caller that cannot see why it was refused will
guess.

ROUTE ORDER IS LOAD-BEARING. Starlette matches in declaration order, so the
literal `aggregates` and `critical-negatives` segments are declared before
`{feedback_id}`. Reversed, `/feedback/aggregates` would be served as an
annotation whose id happens to be the word "aggregates".

MOUNTING. `server/core/admin_api.py` belongs to another session, so this module
exposes `feedback_review_routes` and the orchestrator adds the two lines --
exactly the shape Story 50.1 used for `query_spec_routes`.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core.feedback_review import (
    FeedbackNotFound,
    FeedbackRefused,
    aggregate_feedback,
    append_review_version,
    decode_review_cursor,
    encode_review_cursor,
    get_annotation,
    list_annotations,
    list_review_versions,
    list_unresolved_critical_negatives,
    submit_ai_path_step_feedback,
    submit_exact_feedback,
)

_BASE = "/api/projects/{project_id}/test"
_MAX_HTTP_BODY = 262_144
_NO_STORE = {"Cache-Control": "no-store", "Vary": "Authorization"}

#: One envelope for foreign, denied and absent. See the module docstring.
_NOT_FOUND = {"code": "not_found", "message": "Not found"}


def _not_found() -> Response:
    return JSONResponse(_NOT_FOUND, 404, headers=_NO_STORE)


@asynccontextmanager
async def _authorized_connection(request: Request, role: str = "viewer"):
    """Yield auth or the identity-bound connection used for authorization and work."""
    from core.admin_api import _check_auth, _require_datastream_role  # noqa: PLC0415
    from core.db import request_connection  # noqa: PLC0415

    authorized, identity = await _check_auth(request)
    if not authorized:
        yield JSONResponse(
            {"code": "unauthorized", "message": "Authentication required"}, 401,
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


def _refused(exc: FeedbackRefused) -> Response:
    status = 409 if exc.code in {"stale_review_head", "idempotency_conflict"} else 422
    return JSONResponse(exc.as_dict(), status, headers=_NO_STORE)


def _bounded_json(document: object, status: int = 200) -> Response:
    """Enforce the transport ceiling after service-level count bounds."""
    encoded = json.dumps(document, separators=(",", ":"), default=str).encode("utf-8")
    if isinstance(document, dict):
        versions = document.get("versions")
        while isinstance(versions, list) and versions and len(encoded) > _MAX_HTTP_BODY:
            versions.pop()
            document["truncated"] = True
            document["next_cursor"] = (
                encode_review_cursor(
                    project_id=str(document["project_id"]),
                    feedback_id=str(document["feedback_id"]),
                    version_number=int(versions[-1]["version_number"]),
                )
                if versions
                else None
            )
            encoded = json.dumps(document, separators=(",", ":"), default=str).encode("utf-8")
        review = document.get("review")
        while (
            isinstance(review, dict)
            and isinstance(review.get("versions"), list)
            and review["versions"]
            and len(encoded) > _MAX_HTTP_BODY
        ):
            review["versions"].pop()
            review["versions_truncated"] = True
            review["versions_next_cursor"] = (
                encode_review_cursor(
                    project_id=str(document["project_id"]),
                    feedback_id=str(document["id"]),
                    version_number=int(review["versions"][-1]["version_number"]),
                )
                if review["versions"]
                else None
            )
            encoded = json.dumps(document, separators=(",", ":"), default=str).encode("utf-8")
        verdicts = document.get("automated_verdicts")
        while (
            isinstance(verdicts, dict)
            and isinstance(verdicts.get("items"), list)
            and verdicts["items"]
            and len(encoded) > _MAX_HTTP_BODY
        ):
            verdicts["items"].pop()
            verdicts["truncated"] = True
            encoded = json.dumps(document, separators=(",", ":"), default=str).encode("utf-8")
    if len(encoded) > _MAX_HTTP_BODY:
        return JSONResponse(
            {"code": "response_too_large", "message": "Narrow the requested window."},
            413,
            headers=_NO_STORE,
        )
    return Response(encoded, status, media_type="application/json", headers=_NO_STORE)


def _query_filters(
    request: Request,
    *,
    allowed_extra: tuple[str, ...] = (),
    include_filters: bool = True,
) -> dict[str, str | None]:
    names = (
        "observed_from",
        "observed_to",
        "semantic_view_version_id",
        "business_domain_id",
        "business_domain_version_number",
        "skill_version_id",
        "capability",
        "result_type",
        "target_kind",
        "source",
        "surface",
        "visualization_spec_version_id",
        "renderer_build_id",
        "runtime_build_id",
        "theme_version",
        "formatter_version",
    ) if include_filters else ()
    allowed = set(names) | set(allowed_extra)
    unknown = sorted(set(request.query_params.keys()) - allowed)
    if unknown:
        raise FeedbackRefused(
            "unknown_query_parameter", f"Unknown query parameter: {unknown[0]}", []
        )
    duplicate = next(
        (
            name
            for name in request.query_params.keys()
            if len(request.query_params.getlist(name)) != 1
        ),
        None,
    )
    if duplicate is not None:
        raise FeedbackRefused(
            "malformed_query_parameter", f"Query parameter must occur once: {duplicate}", []
        )
    return {name: request.query_params.get(name) for name in names}


async def _create_feedback(request: Request) -> Response:
    """POST {base}/feedback -- record one annotation on an exact Result."""
    from core.analyze_feedback import (  # noqa: PLC0415
        FeedbackContextError,
        score_feedback_after_commit,
        verify_feedback_context,
    )
    async with _authorized_connection(request, "viewer") as auth:
        if isinstance(auth, Response):
            return auth
        identity, org_id, conn = auth
        project_id = request.path_params["project_id"]
        try:
            _query_filters(request, include_filters=False)
            body = await _json_body(request)
        except FeedbackRefused as exc:
            return _refused(exc)
        except OverflowError as exc:
            return JSONResponse(
                {"code": "body_too_large", "message": str(exc)}, 413, headers=_NO_STORE
            )
        except (ValueError, json.JSONDecodeError) as exc:
            return JSONResponse(
                {"code": "invalid_body", "message": str(exc)}, 400, headers=_NO_STORE
            )
        verified_claims: dict = {}
        try:
            verified_claims = verify_feedback_context(body.get("context"))
        except FeedbackContextError as exc:
            if exc.code == "feedback_context_expired":
                verified_claims = dict(exc.verified_claims)
        try:
            try:
                created = submit_exact_feedback(
                    conn,
                    org_id=org_id,
                    project_id=project_id,
                    actor=identity,
                    payload=body,
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        except FeedbackNotFound:
            return _not_found()
        except FeedbackRefused as exc:
            return _refused(exc)
        score_feedback_after_commit(
            status=str(created.get("status")),
            trace_id=verified_claims.get("w3c_trace_id"),
            polarity=str(body.get("polarity") or ""),
            comment=body.get("comment") if isinstance(body.get("comment"), str) else None,
        )
        return _bounded_json(created, 201 if created.get("status") == "recorded" else 200)


async def _create_ai_path_step_feedback(request: Request) -> Response:
    """POST {base}/feedback/ai-path-steps -- annotate ONE observed AI Path step.

    Story 49.6 AC8. The control is rendered by the Context Hub AI Path screen and
    the command lives HERE, with Test, because `analyze-and-test.md:193` gives
    Test the evaluation and leaves the reading surface only the entry point.
    Context Hub creates no Feedback table and never writes one: it posts a path,
    an ordinal, a polarity and a comment, and every owner is re-resolved by
    `submit_ai_path_step_feedback` on the request-scoped connection.

    The `Idempotency-Key` header is the console's own convention (Story 46.4:
    minted once per node+action and reused for the retry), and it is REQUIRED
    here rather than defaulted: a thumbs-down that silently double-writes on a
    slow network turns one judgement into two rows the aggregate then counts
    twice.

    Same `viewer` grant as `_create_feedback` above -- filing a reaction is a
    reader's right; `_create_review`, which JUDGES one, is the `member` route.
    """
    async with _authorized_connection(request, "viewer") as auth:
        if isinstance(auth, Response):
            return auth
        identity, org_id, conn = auth
        project_id = request.path_params["project_id"]
        try:
            _query_filters(request, include_filters=False)
            body = await _json_body(request)
        except FeedbackRefused as exc:
            return _refused(exc)
        except OverflowError as exc:
            return JSONResponse(
                {"code": "body_too_large", "message": str(exc)}, 413, headers=_NO_STORE
            )
        except (ValueError, json.JSONDecodeError) as exc:
            return JSONResponse(
                {"code": "invalid_body", "message": str(exc)}, 400, headers=_NO_STORE
            )
        retry_key = (request.headers.get("Idempotency-Key") or "").strip()
        if not retry_key:
            return JSONResponse(
                {
                    "code": "missing_idempotency_key",
                    "message": (
                        "Idempotency-Key is required so one reaction "
                        "cannot be recorded twice."
                    ),
                },
                422,
                headers=_NO_STORE,
            )
        unknown = sorted(set(body) - {"ai_path_id", "step_ordinal", "polarity", "comment"})
        if unknown:
            return JSONResponse(
                {"code": "invalid_body", "message": f"Unknown field: {unknown[0]}"},
                400,
                headers=_NO_STORE,
            )
        try:
            try:
                created = submit_ai_path_step_feedback(
                    conn,
                    org_id=org_id,
                    project_id=project_id,
                    actor=identity,
                    path_id=body.get("ai_path_id"),
                    step_ordinal=body.get("step_ordinal"),
                    polarity=body.get("polarity"),
                    comment=body.get("comment"),
                    retry_key=retry_key,
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        except FeedbackNotFound:
            return _not_found()
        except FeedbackRefused as exc:
            return _refused(exc)
        return _bounded_json(created, 201 if created.get("status") == "recorded" else 200)


async def _list_feedback(request: Request) -> Response:
    """GET {base}/feedback -- the Level 2 collection."""
    async with _authorized_connection(request, "viewer") as auth:
        if isinstance(auth, Response):
            return auth
        _identity, org_id, conn = auth
        project_id = request.path_params["project_id"]
        try:
            listing = list_annotations(
                conn,
                org_id=org_id,
                project_id=project_id,
                filters=_query_filters(
                    request, allowed_extra=("limit", "cursor", "polarity")
                ),
                limit=int(request.query_params.get("limit") or 50),
                cursor=request.query_params.get("cursor"),
                polarity=request.query_params.get("polarity"),
            )
        except FeedbackRefused as exc:
            return _refused(exc)
        except ValueError as exc:
            return JSONResponse(
                {"code": "invalid_query", "message": str(exc)}, 400, headers=_NO_STORE
            )
        return _bounded_json(listing)


async def _feedback_aggregates(request: Request) -> Response:
    """GET {base}/feedback/aggregates -- counts with their denominator and filters."""
    async with _authorized_connection(request, "viewer") as auth:
        if isinstance(auth, Response):
            return auth
        _identity, org_id, conn = auth
        project_id = request.path_params["project_id"]
        try:
            aggregates = aggregate_feedback(
                conn,
                org_id=org_id,
                project_id=project_id,
                filters=_query_filters(request, allowed_extra=("limit", "cursor")),
                limit=int(request.query_params.get("limit") or 200),
                cursor=request.query_params.get("cursor"),
            )
        except FeedbackNotFound:
            return _not_found()
        except FeedbackRefused as exc:
            return _refused(exc)
        except ValueError as exc:
            return JSONResponse(
                {"code": "invalid_query", "message": str(exc)}, 400, headers=_NO_STORE
            )
        return _bounded_json(aggregates)


async def _feedback_critical_negatives(request: Request) -> Response:
    """GET {base}/feedback/critical-negatives -- the open improvement leads."""
    async with _authorized_connection(request, "viewer") as auth:
        if isinstance(auth, Response):
            return auth
        _identity, org_id, conn = auth
        project_id = request.path_params["project_id"]
        try:
            _query_filters(
                request, allowed_extra=("limit", "cursor"), include_filters=False
            )
            leads = list_unresolved_critical_negatives(
                conn,
                org_id=org_id,
                project_id=project_id,
                limit=int(request.query_params.get("limit") or 50),
                cursor=request.query_params.get("cursor"),
            )
        except FeedbackRefused as exc:
            return _refused(exc)
        except ValueError as exc:
            return JSONResponse(
                {"code": "invalid_query", "message": str(exc)}, 400, headers=_NO_STORE
            )
        return _bounded_json(leads)


async def _get_feedback(request: Request) -> Response:
    """GET {base}/feedback/{id} -- one annotation, its pins and its honest gaps."""
    async with _authorized_connection(request, "viewer") as auth:
        if isinstance(auth, Response):
            return auth
        _identity, org_id, conn = auth
        project_id = request.path_params["project_id"]
        try:
            _query_filters(request, include_filters=False)
            annotation = get_annotation(
                conn,
                org_id=org_id,
                project_id=project_id,
                feedback_id=request.path_params["feedback_id"],
            )
        except FeedbackNotFound:
            return _not_found()
        except FeedbackRefused as exc:
            return _refused(exc)
        return _bounded_json(annotation)


async def _create_review(request: Request) -> Response:
    """POST {base}/feedback/{id}/reviews -- append one immutable review version."""
    async with _authorized_connection(request, "member") as auth:
        if isinstance(auth, Response):
            return auth
        identity, org_id, conn = auth
        project_id = request.path_params["project_id"]
        try:
            _query_filters(request, include_filters=False)
            body = await _json_body(request)
        except FeedbackRefused as exc:
            return _refused(exc)
        except OverflowError as exc:
            return JSONResponse(
                {"code": "body_too_large", "message": str(exc)}, 413, headers=_NO_STORE
            )
        except (ValueError, json.JSONDecodeError) as exc:
            return JSONResponse(
                {"code": "invalid_body", "message": str(exc)}, 400, headers=_NO_STORE
            )
        try:
            updated = append_review_version(
                conn,
                org_id=org_id,
                project_id=project_id,
                feedback_id=request.path_params["feedback_id"],
                reviewer=identity,
                payload=body,
            )
            conn.commit()
        except FeedbackNotFound:
            conn.rollback()
            return _not_found()
        except FeedbackRefused as exc:
            conn.rollback()
            return _refused(exc)
        return _bounded_json(updated, 200 if updated["status"] == "replayed" else 201)


async def _list_reviews(request: Request) -> Response:
    """GET {base}/feedback/{id}/reviews -- the append-only review history."""
    async with _authorized_connection(request, "viewer") as auth:
        if isinstance(auth, Response):
            return auth
        _identity, org_id, conn = auth
        project_id = request.path_params["project_id"]
        feedback_id = request.path_params["feedback_id"]
        try:
            _query_filters(
                request, allowed_extra=("limit", "cursor"), include_filters=False
            )
            # Resolved first: listing versions of an annotation the caller cannot
            # see must answer like an absent one, not like an empty history.
            get_annotation(conn, org_id=org_id, project_id=project_id, feedback_id=feedback_id)
            bounded = max(1, min(int(request.query_params.get("limit") or 50), 200))
            before = decode_review_cursor(
                request.query_params.get("cursor"),
                project_id=project_id,
                feedback_id=feedback_id,
            )
            versions = list_review_versions(
                conn,
                org_id=org_id,
                project_id=project_id,
                feedback_id=feedback_id,
                limit=bounded + 1,
                before_version=before,
            )
        except FeedbackNotFound:
            return _not_found()
        except FeedbackRefused as exc:
            return _refused(exc)
        except ValueError as exc:
            return JSONResponse(
                {"code": "invalid_query", "message": str(exc)}, 400, headers=_NO_STORE
            )
        truncated = len(versions) > bounded
        versions = versions[:bounded]
        return _bounded_json(
            {
                "project_id": project_id,
                "feedback_id": feedback_id,
                "versions": versions,
                "truncated": truncated,
                "next_cursor": (
                    encode_review_cursor(
                        project_id=project_id,
                        feedback_id=feedback_id,
                        version_number=int(versions[-1]["version_number"]),
                    )
                    if truncated and versions
                    else None
                ),
            }
        )


# Literal segments first: `aggregates` and `critical-negatives` are declared
# ahead of `{feedback_id}` so a literal can never be read as an annotation id.
feedback_review_routes = [
    Route(f"{_BASE}/feedback/aggregates", endpoint=_feedback_aggregates, methods=["GET"]),
    Route(
        f"{_BASE}/feedback/critical-negatives",
        endpoint=_feedback_critical_negatives,
        methods=["GET"],
    ),
    # BEFORE the object route below: Starlette matches the PATH first, so a POST
    # to this literal segment would otherwise be read as an annotation whose id
    # happens to be the words 'ai-path-steps', and answered 405.
    Route(
        f"{_BASE}/feedback/ai-path-steps",
        endpoint=_create_ai_path_step_feedback,
        methods=["POST"],
    ),
    Route(f"{_BASE}/feedback", endpoint=_create_feedback, methods=["POST"]),
    Route(f"{_BASE}/feedback", endpoint=_list_feedback, methods=["GET"]),
    Route(
        f"{_BASE}/feedback/{{feedback_id}}/reviews",
        endpoint=_create_review,
        methods=["POST"],
    ),
    Route(
        f"{_BASE}/feedback/{{feedback_id}}/reviews",
        endpoint=_list_reviews,
        methods=["GET"],
    ),
    Route(f"{_BASE}/feedback/{{feedback_id}}", endpoint=_get_feedback, methods=["GET"]),
]
