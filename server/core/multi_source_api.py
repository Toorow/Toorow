"""toorow -- the door of a cross-source analysis (stories 66.4/66.5, opened by 66.7).

  POST /api/projects/{project_id}/analyze/multi-source/plans          compile + freeze
  POST /api/projects/{project_id}/analyze/multi-source/plans/{id}/execute

TWO ACTS, TWO ADDRESSES, and that is the product's own distinction between an
ANALYTICAL edit and running one. Compiling freezes what will be asked; executing
answers it. A single endpoint doing both would make every refusal look like a
failed query, and a person could never tell "your request is ambiguous" from
"the warehouse is down".

WHY 422 AND NOT 400 ON A REFUSAL. The request is well formed; the product refuses
what it asks for, by name, with the gesture that repairs it. A screen renders
`ambiguous_relationship` beside the two paths and asks the person to choose --
which is impossible if the answer is a flat "bad request".
"""

from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger(__name__)

_PLANS = "/api/projects/{project_id}/analyze/multi-source/plans"
_EXECUTE = _PLANS + "/{query_spec_version_id}/execute"


async def _check_auth(request: Request) -> tuple[bool, str]:
    from core.admin_api import _check_auth as _admin_check_auth  # noqa: PLC0415

    return await _admin_check_auth(request)


def _unauthorized() -> Response:
    return JSONResponse(
        {"code": "unauthorized", "message": "Authentication is required."}, status_code=401
    )


def _not_found() -> Response:
    return JSONResponse({"code": "not_found", "message": "Not found."}, status_code=404)


def _guard(
    project_id: str, identity: str, minimum_capability: str
) -> tuple[str | None, Response | None]:
    """Authorize the exact Project capability under the request-scoped RLS identity.

    Compiling a plan creates a Query Spec version and executing it creates a
    Result.  Organization membership alone is not write authority: it used to
    let a project viewer perform both mutations.  This now deliberately mirrors
    the existing single-source Analyze doors.
    """
    from core.admin_api import _strict_project_capability_allowed  # noqa: PLC0415
    from core.query_specs_api import analyze_connection  # noqa: PLC0415

    try:
        with analyze_connection(identity) as conn:
            allowed = _strict_project_capability_allowed(
                conn,
                identity=identity,
                project_id=project_id,
                minimum_capability=minimum_capability,
                hold_access=True,
            )
            if not allowed:
                return None, _not_found()
            with conn.cursor() as cur:
                cur.execute("SELECT org_id FROM app.projects WHERE id = %s", (project_id,))
                row = cur.fetchone()
            if not row or not row[0]:
                return None, _not_found()
            org_id = str(row[0])
    except Exception as exc:  # noqa: BLE001
        logger.error("multi_source_api: guard failed project=%s: %s", project_id, exc)
        return None, JSONResponse(
            {"code": "server_error", "message": "Server error."}, status_code=500
        )
    return org_id, None


async def _compile(request: Request) -> Response:
    """POST {plans} -- compile one exact plan and freeze it as a version."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()
    project_id = request.path_params["project_id"]
    org_id, refusal = _guard(project_id, identity, "edit")
    if refusal is not None:
        return refusal

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    if not isinstance(body, dict):
        body = {}

    from core.match_profile import (  # noqa: PLC0415
        ProfileReceiptInvalid,
        ProfileRefused,
        profile_match,
        reuse_profile_receipt,
    )
    from core.multi_source_plan import (  # noqa: PLC0415
        PlanRefused,
        compile_plan,
        store_plan_version,
    )
    from core.query_specs_api import analyze_connection  # noqa: PLC0415

    try:
        with analyze_connection(identity) as conn:
            supplied_receipts = body.get("profile_receipts") or []
            if not isinstance(supplied_receipts, list) or len(supplied_receipts) > 4:
                raise PlanRefused(
                    "malformed_profile_receipts",
                    "Profile receipts must be a bounded list with at most one receipt per edge.",
                )
            filters = body.get("filters") or []

            def _profile(
                left: str,
                right: str,
                key_version_id: str,
                relationship_name: str,
                view_version_id: str,
            ):
                # The measured veto of story 66.3, consulted at compile time. A
                # profile that cannot be produced is NOT a green light: it comes
                # back as review_required, and the plan is still frozen -- what it
                # must never do is let an `unsafe` measurement through silently.
                for token in supplied_receipts:
                    try:
                        return reuse_profile_receipt(
                            conn,
                            token=token,
                            project_id=project_id,
                            left_datastream_id=left,
                            right_datastream_id=right,
                            common_key_version_id=key_version_id,
                            relationship_name=relationship_name,
                            view_version_id=view_version_id,
                            window=filters,
                        )
                    except ProfileReceiptInvalid:
                        continue
                try:
                    return profile_match(
                        conn,
                        project_id=project_id,
                        left_datastream_id=left,
                        right_datastream_id=right,
                        common_key_version_id=key_version_id,
                        relationship_name=relationship_name,
                        view_version_id=view_version_id,
                        window=filters,
                    )
                except ProfileRefused as exc:
                    raise PlanRefused(
                        "profile_unavailable",
                        "The selected match could not be measured, so it was not compiled.",
                        detail={"code": exc.code, "missing_link": exc.missing_link},
                    ) from exc

            compiled = compile_plan(
                conn,
                project_id=project_id,
                request=body,
                # Profiling is safety evidence, not a client preference.  The
                # former public `skip_profile` flag let any caller bypass an
                # observed unsafe fan-out and freeze the plan anyway.
                profile_lookup=_profile,
            )
            stored = store_plan_version(
                conn,
                org_id=org_id,
                project_id=project_id,
                compiled=compiled,
                actor=identity,
                name=body.get("name"),
            )
            conn.commit()
    except PlanRefused as exc:
        return JSONResponse(
            {"code": exc.code, "message": exc.message, "detail": exc.detail}, status_code=422
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("multi_source_api: compile failed project=%s: %s", project_id, exc)
        return JSONResponse(
            {
                "code": "plan_unavailable",
                "message": (
                    "The analysis could not be frozen because its owner could not be "
                    "read. Nothing was stored."
                ),
            },
            status_code=503,
        )

    return JSONResponse(
        {"project_id": project_id, "plan": compiled["plan"], **stored}, status_code=201
    )


async def _execute(request: Request) -> Response:
    """POST {execute} -- run one frozen plan; always leaves exactly one Result."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()
    project_id = request.path_params["project_id"]
    version_id = request.path_params["query_spec_version_id"]
    org_id, refusal = _guard(project_id, identity, "edit")
    if refusal is not None:
        return refusal

    from core.multi_source_execution import ExecutionRefused, execute_plan  # noqa: PLC0415
    from core.multi_source_plan import PlanRefused  # noqa: PLC0415
    from core.query_specs_api import analyze_connection  # noqa: PLC0415

    try:
        with analyze_connection(identity) as conn:
            result = execute_plan(
                conn,
                org_id=org_id,
                project_id=project_id,
                query_spec_version_id=version_id,
                actor=identity,
            )
            conn.commit()
    except PlanRefused as exc:
        # `plan_not_found` and `not_a_multi_source_plan` are both 404 shaped: a
        # caller must not learn from this door that a Query Spec version exists
        # under another contract or in another Project.
        if exc.code in {"plan_not_found", "not_a_multi_source_plan"}:
            return _not_found()
        return JSONResponse({"code": exc.code, "message": exc.message}, status_code=422)
    except ExecutionRefused as exc:
        # A REFUSAL IS NOT AN OUTAGE. `ExecutionRefused` fell through to the
        # generic handler below, so every named refusal this executor raises --
        # `profile_not_ready`, `profile_expired`, and now the merge bound
        # re-read on the frozen plan -- reached the person as a 503 saying "the
        # analysis could not be run", which names no gesture and invites a
        # retry that cannot succeed. The condition and its sentence are the
        # whole point of raising them.
        return JSONResponse({"code": exc.code, "message": exc.message}, status_code=422)
    except Exception as exc:  # noqa: BLE001
        logger.error("multi_source_api: execute failed project=%s: %s", project_id, exc)
        return JSONResponse(
            {
                "code": "execution_unavailable",
                "message": "The analysis could not be run. No partial answer was recorded.",
            },
            status_code=503,
        )

    return JSONResponse({"project_id": project_id, "result": result}, status_code=201)


MULTI_SOURCE_ROUTES: list[Route] = [
    # `/execute` before the bare collection: Starlette resolves in order.
    Route(_EXECUTE, _execute, methods=["POST"]),
    Route(_PLANS, _compile, methods=["POST"]),
]

__all__ = ["MULTI_SOURCE_ROUTES"]
