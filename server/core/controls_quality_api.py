"""The Controls & Quality command routes (Story 49.4, AC7).

    POST /api/projects/{project_id}/governance/controls-quality/change-sets
    GET  /api/projects/{project_id}/governance/controls-quality/change-sets/{id}
    POST .../change-sets/{id}/prepare
    POST .../change-sets/{id}/confirm   (runs the owner command)
    POST /api/projects/{project_id}/governance/controls-quality/dq-monitors/{monitor_id}/evaluations

The reads stay on the Story 49.1 Governance surface; this is the only
consequential command family, exactly as `semantic_model_api` is for 49.3. The
two modules are deliberately the same shape -- authorization, strict Project
resolution, one typed error table -- because a second command family that behaved
differently would be a second thing to learn and a second one to get wrong.

Story 49.4's own words: "The Console and any future MCP surface must call the
same application services. Do not add a second MCP-specific control authority."
Everything here delegates to :mod:`core.controls_change_sets`.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core.controls_change_sets import (
    ControlsChangeSetError,
    ControlsChangeSetNotFound,
    ControlsChangeSetStale,
    confirm_change_set,
    create_change_set,
    prepare_change_set,
    read_change_set,
)
from core.controls_owner_commands import OwnerCommandError, owner_command
from core.db import request_connection
from core.project_access import resolve_strict_resource_access

logger = logging.getLogger(__name__)

_ROOT = "/api/projects/{project_id}/governance/controls-quality/change-sets"
_MONITOR_ROOT = "/api/projects/{project_id}/governance/controls-quality/dq-monitors"
_RULE_SET_ROOT = "/api/projects/{project_id}/governance/controls-quality/rule-sets"


def _no_store(response: Response) -> Response:
    """Control evidence is never cached: a stale impact is a wrong impact."""
    response.headers["Cache-Control"] = "no-store"
    return response


def _error(code: str, message: str, status: int) -> Response:
    return _no_store(JSONResponse({"code": code, "message": message}, status_code=status))


def _not_found() -> Response:
    # Non-disclosing: a cross-Project object and a missing one answer alike.
    return _error("not_found", "Not found", 404)


async def _body(request: Request) -> dict[str, Any]:
    import json  # noqa: PLC0415

    raw = await request.body()
    if not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise ControlsChangeSetError(f"invalid JSON body: {exc}") from exc
    if not isinstance(payload, dict):
        raise ControlsChangeSetError("the body must be a JSON object")
    return payload


async def _with_project(request: Request, capability: str, handler):
    from core.admin_api import _check_auth  # noqa: PLC0415

    authorized, identity = await _check_auth(request)
    if not authorized:
        return _error("unauthorized", "Authentication is required.", 401)
    project_id = (request.path_params.get("project_id") or "").strip()
    if not project_id:
        return _not_found()
    actor = identity or "anonymous"

    try:
        with request_connection(actor) as conn:
            decision = resolve_strict_resource_access(
                actor,
                conn,
                project_id=project_id,
                minimum_capability=capability,
                hold_access=True,
            )
            if not decision.allowed:
                if decision.reason == "insufficient_capability":
                    # Visibility and action authority are separate (AC12): the
                    # reader is told they cannot act, not that nothing exists.
                    return _error(
                        "denied",
                        "Your access to this Project does not include changing its "
                        "controls.",
                        403,
                    )
                if decision.reason == "access_unavailable":
                    return _error(
                        "controls_quality_unavailable", "Controls & Quality is unavailable", 503
                    )
                return _not_found()
            org_id = decision.org_id or ""
            if not org_id:
                return _not_found()
            response = await handler(conn, project_id, actor, org_id)
            conn.commit()
            return response
    except ControlsChangeSetNotFound:
        return _not_found()
    except ControlsChangeSetStale as exc:
        # 409: well formed, authorized, and the world moved underneath it.
        return _no_store(JSONResponse(exc.as_dict(), status_code=409))
    except ControlsChangeSetError as exc:
        return _no_store(JSONResponse(exc.as_dict(), status_code=422))
    except OwnerCommandError as exc:
        # The owner refused the intent: a missing profile, an invalid ladder, a
        # decision with no reason. 422, with the owner's own sentence, because a
        # 503 here would read as "try again later" for something that never will.
        return _no_store(JSONResponse(exc.as_dict(), status_code=422))
    except Exception as exc:  # noqa: BLE001 -- fail closed without disclosing
        logger.warning(
            "controls_quality_api: command unavailable project=%s: %s",
            request.path_params.get("project_id"),
            type(exc).__name__,
        )
        return _error("controls_quality_unavailable", "Controls & Quality is unavailable", 503)


def _serialize(record: dict[str, Any]) -> dict[str, Any]:
    """Everything a reviewer needs, and no confirmation material."""
    return {
        "schema_version": "controls-change-set.v1",
        "id": record["id"],
        "object_type": record["object_type"],
        "object_id": record["object_id"],
        "base_version_id": record["base_version_id"],
        "state": record["state"],
        "intent": record["intent"],
        "diff": record["diff"],
        "impact": record["impact"],
        "dependency_fingerprint": record["dependency_fingerprint"],
        "expires_at": record["expires_at"].isoformat() if record.get("expires_at") else None,
        "confirmed_at": (
            record["confirmation_used_at"].isoformat()
            if record.get("confirmation_used_at")
            else None
        ),
        "result_version_id": record["result_version_id"],
    }


async def _create(request: Request) -> Response:
    payload = await _body(request)
    key = request.headers.get("Idempotency-Key") or payload.get("idempotency_key") or ""

    async def handler(conn, project_id, actor, org_id):
        record = create_change_set(
            conn,
            project_id=project_id,
            object_type=str(payload.get("object_type") or ""),
            object_id=payload.get("object_id"),
            base_version_id=payload.get("base_version_id"),
            intent=payload.get("intent") or {},
            actor=actor,
            idempotency_key=key,
        )
        return _no_store(JSONResponse(_serialize(record), status_code=201))

    return await _with_project(request, "edit", handler)


async def _read(request: Request) -> Response:
    async def handler(conn, project_id, actor, org_id):
        record = read_change_set(
            conn,
            project_id=project_id,
            change_set_id=request.path_params["change_set_id"],
        )
        return _no_store(JSONResponse(_serialize(record)))

    return await _with_project(request, "view", handler)


async def _prepare(request: Request) -> Response:
    async def handler(conn, project_id, actor, org_id):
        record, token = prepare_change_set(
            conn,
            project_id=project_id,
            change_set_id=request.path_params["change_set_id"],
            actor=actor,
        )
        # The token is returned exactly once, and only to a caller with `edit`.
        # It is stored as a hash, so losing it means preparing again.
        return _no_store(
            JSONResponse({**_serialize(record), "confirmation_token": token})
        )

    return await _with_project(request, "edit", handler)


async def _confirm(request: Request) -> Response:
    payload = await _body(request)

    async def handler(conn, project_id, actor, org_id):
        record = confirm_change_set(
            conn,
            project_id=project_id,
            change_set_id=request.path_params["change_set_id"],
            confirmation_token=str(payload.get("confirmation_token") or ""),
            actor=actor,
            # Without this the confirmation was ceremony: the token was spent,
            # the drift recheck ran, the state moved to `confirmed`, and NOTHING
            # was published. `result_version_id` stayed NULL and no Project could
            # advance a Rule Set, a monitor version or a mapping decision.
            apply=owner_command(actor=actor, org_id=org_id),
        )
        return _no_store(
            JSONResponse(
                {
                    **_serialize(record),
                    "replayed": record.get("replayed", False),
                    **_owner_handoff(conn, project_id, record),
                }
            )
        )

    # `manage`, not `edit`: publishing and activating is a different authority
    # from drafting, and AC12 keeps them apart.
    return await _with_project(request, "manage", handler)


def _owner_handoff(conn, project_id: str, record: Mapping[str, Any]) -> dict[str, Any]:
    """What the OWNER command reported, when the decision called one.

    WHY THE RESPONSE HAD TO GROW (measured 2026-08-16). `_hand_off_to_data`
    records a refusal as `owner_outcome = 'failed'` and returns 200 on purpose --
    *"a failed handoff is a real recorded result"*, and raising would roll back
    the confirmation and erase the trace that the owner was ever asked. But the
    outcome stopped at the database: the confirm response carried
    `result_version_id` and nothing else, so the console closed its dialog on
    "Decision recorded" whether Data had published or refused. The person had
    just taken an APPEND-ONLY act and had to go read a table to find out it
    failed.

    Absent keys mean this decision called no owner, not that nobody looked --
    `not_applicable` is a stored value and it is returned as such.
    """
    version_id = record.get("result_version_id")
    if not version_id or str(record.get("object_type") or "") != "control-case":
        return {}
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT owner_outcome, owner_result
                  FROM app.control_case_decisions
                 WHERE id = %s AND project_id = %s
                """,
                (str(version_id), project_id),
            )
            row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001 -- the decision IS recorded either way
        logger.warning("controls_quality_api: owner outcome unreadable: %s", exc)
        return {}
    if row is None:
        return {}
    return {"owner_outcome": row[0], "owner_result": row[1]}


async def _evaluate(request: Request) -> Response:
    """Run one monitor now -- as a durable operation, not as a background thread.

    This replaces `POST /api/dq/evaluate`, which handed the work to a
    request-scoped ``ThreadPoolExecutor`` and returned 202 immediately. Nothing
    could then say whether the run happened, and two identical requests ran
    twice. Story 49.4 AC7: "Manual DQ evaluation uses a durable operation/queue
    and the same evaluator as scheduled work. No request-scoped background thread
    is authoritative."

    `edit`, not `manage`: an evaluation writes evidence and changes no governed
    decision. It is not a read either -- it costs work and leaves a durable row.
    """

    payload = await _body(request)
    key = request.headers.get("Idempotency-Key") or payload.get("idempotency_key") or ""

    async def handler(conn, project_id, actor, org_id):
        from datetime import date, timedelta  # noqa: PLC0415

        from core.dq_governance import DqGovernanceError, evaluate_monitor  # noqa: PLC0415
        from core.dq_monitors import governed_evaluator  # noqa: PLC0415

        window_end = _as_date(payload.get("window_end")) or (date.today() - timedelta(days=1))
        window_start = _as_date(payload.get("window_start")) or window_end
        try:
            outcome = evaluate_monitor(
                conn,
                project_id=project_id,
                org_id=org_id,
                monitor_id=request.path_params["monitor_id"],
                actor=actor,
                idempotency_key=key,
                window_start=window_start,
                window_end=window_end,
                evaluator=governed_evaluator,
                trace_id=request.headers.get("X-Trace-Id"),
            )
        except DqGovernanceError as exc:
            # A monitor with no published version, or a refused verdict. Both are
            # the caller's problem to fix, not an outage.
            return _error("unprocessable", str(exc), 422)
        return _no_store(JSONResponse(outcome, status_code=200 if outcome["replayed"] else 201))

    return await _with_project(request, "edit", handler)


async def _adoption(request: Request) -> Response:
    """What adopting this preset would publish, before anything is written.

    A GET, deliberately: the plan is a read, and a read that had to be a POST is
    a read people are afraid to take.
    """

    async def handler(conn, project_id, actor, org_id):
        from core.rule_set_adoption import AdoptionRefused, plan_adoption  # noqa: PLC0415

        preset = (request.query_params.get("preset_version_id") or "").strip()
        if not preset:
            return _error(
                "preset_version_required",
                "Name the preset version to adopt: a plan for 'some preset' is a "
                "plan for nothing.",
                422,
            )
        try:
            plan = plan_adoption(
                conn,
                project_id=project_id,
                org_id=org_id,
                rule_set_id=request.path_params["rule_set_id"],
                preset_version_id=preset,
            )
        except AdoptionRefused as exc:
            # 422 with the refusal's own sentence: every one of them names the
            # gesture that clears it, and a 503 would read as "try again later"
            # for something that never will.
            return _no_store(JSONResponse(exc.as_dict(), status_code=422))
        return _no_store(JSONResponse({"schema_version": "rule-set-adoption.v1", **plan}))

    return await _with_project(request, "view", handler)


async def _adopt(request: Request) -> Response:
    """Create, prepare and confirm the rule-set Change Set. Three recorded steps."""

    payload = await _body(request)
    key = request.headers.get("Idempotency-Key") or payload.get("idempotency_key") or ""

    async def handler(conn, project_id, actor, org_id):
        from core.rule_set_adoption import AdoptionRefused, adopt_preset  # noqa: PLC0415

        try:
            outcome = adopt_preset(
                conn,
                project_id=project_id,
                org_id=org_id,
                rule_set_id=request.path_params["rule_set_id"],
                preset_version_id=str(payload.get("preset_version_id") or "").strip(),
                answers=payload.get("answers") or {},
                actor=actor,
                idempotency_key=str(key) or None,
            )
        except AdoptionRefused as exc:
            return _no_store(JSONResponse(exc.as_dict(), status_code=422))
        return _no_store(
            JSONResponse(
                {
                    "schema_version": "rule-set-adoption-result.v1",
                    "change_set": _serialize(outcome["change_set"]),
                    "replayed": outcome["replayed"],
                },
                status_code=200 if outcome["replayed"] else 201,
            )
        )

    return await _with_project(request, "edit", handler)


# ---------------------------------------------------------------------------
# Composing a version. One door, every family (`governance.md`, "A Rule Set
# version is drafted, then published", 2026-08-24).
#
# Two acts, two addresses, and the read is at the address it writes to: `GET`
# says what could be composed and what a new version would carry forward, `POST`
# composes the draft, and the draft is put in force at its own address. A single
# call that composed and published would be a gesture whose consequence cannot be
# read before it is taken -- the reason the adoption above reads its plan first.
# ---------------------------------------------------------------------------


def _serialize_version(version: Mapping[str, Any]) -> dict[str, Any]:
    """One Rule Set version, as a screen reads it.

    The content hash is here because it is what makes "has this changed?"
    answerable at a glance; the payload is not, because the workbench reads the
    version through the Governance read model and two readers of one payload are
    two answers free to disagree.
    """

    def _iso(value: Any) -> Any:
        return value.isoformat() if hasattr(value, "isoformat") else value

    return {
        "id": version["id"],
        "rule_set_id": version["rule_set_id"],
        "version_number": version["version_number"],
        "status": version["status"],
        "profile": version["profile"],
        "label": version.get("label"),
        "content_hash": version.get("content_hash"),
        "effective_from": _iso(version.get("effective_from")),
        "effective_to": _iso(version.get("effective_to")),
        "created_by": version.get("created_by"),
        "created_at": _iso(version.get("created_at")),
    }


async def _authoring_plan(request: Request) -> Response:
    """The declared form, the answers in force, and the open draft if there is one."""

    async def handler(conn, project_id, actor, org_id):
        from core.rule_set_authoring import AuthoringRefused, plan_authoring  # noqa: PLC0415

        try:
            plan = plan_authoring(
                conn, project_id=project_id, rule_set_id=request.path_params["rule_set_id"]
            )
        except AuthoringRefused as exc:
            if exc.code == "not_found":
                return _not_found()
            return _no_store(JSONResponse(exc.as_dict(), status_code=422))
        return _no_store(JSONResponse({"schema_version": "rule-set-authoring.v1", **plan}))

    return await _with_project(request, "view", handler)


async def _author_draft(request: Request) -> Response:
    """Compose a draft version. Nothing in force changes until it is published."""

    payload = await _body(request)

    async def handler(conn, project_id, actor, org_id):
        from core.rule_set_authoring import AuthoringRefused, draft_from_answers  # noqa: PLC0415

        answers = payload.get("answers")
        if not isinstance(answers, dict):
            return _error(
                "answers_required",
                "Answer the questions on this Rule Set before composing a version.",
                422,
            )
        try:
            version = draft_from_answers(
                conn,
                project_id=project_id,
                rule_set_id=request.path_params["rule_set_id"],
                answers=answers,
                actor=actor,
                label=str(payload.get("label") or "").strip() or None,
                description=str(payload.get("description") or "").strip() or None,
            )
        except AuthoringRefused as exc:
            if exc.code == "not_found":
                return _not_found()
            return _no_store(JSONResponse(exc.as_dict(), status_code=422))
        return _no_store(
            JSONResponse(
                {
                    "schema_version": "rule-set-draft.v1",
                    "version": _serialize_version(version),
                },
                status_code=201,
            )
        )

    return await _with_project(request, "edit", handler)


async def _publish_draft(request: Request) -> Response:
    """Put a composed draft in force, through a Change Set. The second act."""

    payload = await _body(request)
    key = request.headers.get("Idempotency-Key") or payload.get("idempotency_key") or ""

    async def handler(conn, project_id, actor, org_id):
        from core.rule_set_authoring import AuthoringRefused, publish_draft  # noqa: PLC0415

        try:
            outcome = publish_draft(
                conn,
                project_id=project_id,
                org_id=org_id,
                rule_set_id=request.path_params["rule_set_id"],
                version_id=request.path_params["version_id"],
                actor=actor,
                idempotency_key=str(key) or None,
            )
        except AuthoringRefused as exc:
            if exc.code == "not_found":
                return _not_found()
            return _no_store(JSONResponse(exc.as_dict(), status_code=422))
        return _no_store(
            JSONResponse(
                {
                    "schema_version": "rule-set-publication.v1",
                    "version": _serialize_version(outcome["version"]),
                    # `None` when the version was already in force: the gesture's
                    # outcome is true and no change set was minted to make it so.
                    "change_set": (
                        _serialize(outcome["change_set"]) if outcome["change_set"] else None
                    ),
                    "replayed": outcome["replayed"],
                },
                status_code=200 if outcome["replayed"] else 201,
            )
        )

    return await _with_project(request, "edit", handler)


def _as_date(value: Any):
    from datetime import date  # noqa: PLC0415

    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ControlsChangeSetError(f"invalid date {value!r}: {exc}") from exc

CONTROLS_QUALITY_ROUTES: list[Route] = [
    # More specific paths first, so `/prepare` is never swallowed by `{id}`.
    Route(f"{_ROOT}/{{change_set_id}}/prepare", _prepare, methods=["POST"]),
    Route(f"{_ROOT}/{{change_set_id}}/confirm", _confirm, methods=["POST"]),
    Route(f"{_ROOT}/{{change_set_id}}", _read, methods=["GET"]),
    Route(_ROOT, _create, methods=["POST"]),
    Route(f"{_MONITOR_ROOT}/{{monitor_id}}/evaluations", _evaluate, methods=["POST"]),
    # One address, read first. `governance.md`, "A ladder is adopted where it is
    # read" (2026-08-17).
    Route(f"{_RULE_SET_ROOT}/{{rule_set_id}}/adoption", _adoption, methods=["GET"]),
    Route(f"{_RULE_SET_ROOT}/{{rule_set_id}}/adoption", _adopt, methods=["POST"]),
    # Composing a version, then putting it in force. The publication path is more
    # specific and is declared first, for the reason the comment above gives.
    Route(
        f"{_RULE_SET_ROOT}/{{rule_set_id}}/versions/{{version_id}}/publication",
        _publish_draft,
        methods=["POST"],
    ),
    Route(f"{_RULE_SET_ROOT}/{{rule_set_id}}/versions", _authoring_plan, methods=["GET"]),
    Route(f"{_RULE_SET_ROOT}/{{rule_set_id}}/versions", _author_draft, methods=["POST"]),
]
