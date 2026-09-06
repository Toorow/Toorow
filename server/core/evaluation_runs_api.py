"""Stories 51.2 and 51.3 -- the server-owned Test evidence API.

THIN HANDLERS. Every endpoint here is a translation of HTTP into
`core.evaluation_runs`. No validation, no SQL and no verdict logic lives in this
file, so the console and any future adapter reach the same evidence through the
same module rather than through two contracts that drift.

ONE ROUTE PER LEVEL 3 TAB. `analyze-and-test.md:291` fixes the Evaluation Run
Workbench at Overview, Cases, Comparisons, Environment and Gate Decision. Each is
a separate server address, so each tab is shareable -- a tab kept in component
state is not an address and cannot be sent to a colleague.

NON-DISCLOSURE IS THE DEFAULT. `EvaluationNotFound` becomes the same 404 envelope
whether the object is foreign, denied or absent, and the denial path answers
before any work is done so response timing does not become an enumeration oracle.
Refusals are 422 with their full structured reason list, because a caller that
cannot see why it was refused will guess.

ROUTE ORDER. Literal segments are declared before parameterized ones, so
`run-profiles` and `evaluation-cases` can never be captured as a run id. The
order of `evaluation_run_routes` is load-bearing, not cosmetic.

THIS FILE DOES NOT MOUNT ITSELF. `server/core/admin_api.py` belongs to another
session. It exports one constant, exactly as `query_specs_api.py` exports
`query_spec_routes`; the orchestrator adds the import and the spread.
"""

from __future__ import annotations

import json

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core.adherence import ADHERENCE_DEFAULT_WINDOW_DAYS, adherence_overview
from core.evaluation_runs import (
    EvaluationNotFound,
    EvaluationRefused,
    active_baseline,
    add_run_case,
    approve_baseline,
    create_comparison,
    create_context_version_set,
    create_run_profile,
    emit_gate_decision,
    finalize_evaluation_run,
    list_evaluation_runs,
    list_run_profiles,
    open_evaluation_run,
    record_case_verdicts,
    run_cases,
    run_comparisons,
    run_environment,
    run_gate_decisions,
    run_overview,
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


def _refused(exc: EvaluationRefused) -> Response:
    return JSONResponse(exc.as_dict(), 422)


def _bad_body(exc: Exception) -> Response:
    return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)


# ---------------------------------------------------------------------------
# Run profiles and Context Version Sets.
# ---------------------------------------------------------------------------


async def _list_run_profiles(request: Request) -> Response:
    """GET {base}/run-profiles -- the named profiles a baseline can belong to."""
    auth = await _authorize(request, "viewer")
    if isinstance(auth, Response):
        return auth
    _identity, org_id = auth
    project_id = request.path_params["project_id"]

    from core.db import get_connection  # noqa: PLC0415

    with get_connection() as conn:
        profiles = list_run_profiles(conn, org_id=org_id, project_id=project_id)
    return JSONResponse({"run_profiles": profiles})


async def _create_run_profile(request: Request) -> Response:
    """POST {base}/run-profiles -- name a profile and fix its evidence mode."""
    auth = await _authorize(request, "member")
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth
    project_id = request.path_params["project_id"]
    try:
        body = await _json_body(request)
    except (ValueError, json.JSONDecodeError) as exc:
        return _bad_body(exc)

    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            created = create_run_profile(
                conn,
                org_id=org_id,
                project_id=project_id,
                name=body.get("name"),
                evidence_mode=body.get("evidence_mode"),
                description=body.get("description") or "",
                actor=identity,
            )
            conn.commit()
    except EvaluationRefused as exc:
        return _refused(exc)
    return JSONResponse(created, 201)


async def _create_context_version_set(request: Request) -> Response:
    """POST {base}/context-version-sets -- freeze one exact context resolution."""
    auth = await _authorize(request, "member")
    if isinstance(auth, Response):
        return auth
    _identity, org_id = auth
    project_id = request.path_params["project_id"]
    try:
        body = await _json_body(request)
    except (ValueError, json.JSONDecodeError) as exc:
        return _bad_body(exc)

    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            created = create_context_version_set(
                conn,
                org_id=org_id,
                project_id=project_id,
                entries=body.get("entries") or [],
            )
            conn.commit()
    except EvaluationRefused as exc:
        return _refused(exc)
    return JSONResponse(created, 201)


async def _get_active_baseline(request: Request) -> Response:
    """GET {base}/run-profiles/{id}/baseline -- the one explicitly approved run.

    `null` when nobody approved one. There is no route, and no code path, that
    creates a baseline as a side effect of finalizing a run.
    """
    auth = await _authorize(request, "viewer")
    if isinstance(auth, Response):
        return auth
    _identity, org_id = auth
    project_id = request.path_params["project_id"]

    from core.db import get_connection  # noqa: PLC0415

    with get_connection() as conn:
        baseline = active_baseline(
            conn,
            org_id=org_id,
            project_id=project_id,
            run_profile_id=request.path_params["run_profile_id"],
        )
    return JSONResponse({"baseline": baseline})


# ---------------------------------------------------------------------------
# Evaluation Runs.
# ---------------------------------------------------------------------------


async def _list_evaluation_runs(request: Request) -> Response:
    """GET {base}/evaluation-runs -- the Regression Runs collection.

    `offline` and `observed_cohort` stay distinct on every row and no field here
    merges them. There is no pass rate and no latest score: those are the
    on-screen form of the merge `analyze-and-test.md:210` forbids.
    """
    auth = await _authorize(request, "viewer")
    if isinstance(auth, Response):
        return auth
    _identity, org_id = auth
    project_id = request.path_params["project_id"]
    mode = request.query_params.get("evidence_mode") or None

    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            runs = list_evaluation_runs(
                conn, org_id=org_id, project_id=project_id, evidence_mode=mode
            )
    except EvaluationRefused as exc:
        return _refused(exc)
    return JSONResponse({"evaluation_runs": runs})


async def _open_evaluation_run(request: Request) -> Response:
    """POST {base}/evaluation-runs -- open a recording run with its pins.

    The tool-catalog version is resolved SERVER-SIDE from the live catalog
    (`skill_tool_catalog.list_skill_tool_catalog()`), never accepted from the
    request and never minted. When the catalog is unavailable the run is refused:
    a run whose catalog version was guessed is not comparable with any other.
    """
    auth = await _authorize(request, "member")
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth
    project_id = request.path_params["project_id"]
    try:
        body = await _json_body(request)
    except (ValueError, json.JSONDecodeError) as exc:
        return _bad_body(exc)

    from core.db import get_connection  # noqa: PLC0415
    from core.skill_tool_catalog import list_skill_tool_catalog  # noqa: PLC0415

    try:
        catalog = await list_skill_tool_catalog()
    except RuntimeError:
        return JSONResponse(
            {
                "code": "tool_catalog_unavailable",
                "message": "the live tool catalog is not available, so its exact "
                "version cannot be pinned; a run is not opened without it",
                "refusals": [],
            },
            422,
        )

    try:
        with get_connection() as conn:
            opened = open_evaluation_run(
                conn,
                org_id=org_id,
                project_id=project_id,
                run_profile_id=str(body.get("run_profile_id") or ""),
                semantic_view_id=body.get("semantic_view_id"),
                semantic_view_version_id=body.get("semantic_view_version_id"),
                context_version_set_id=body.get("context_version_set_id"),
                model_ref=body.get("model_ref"),
                host_capability_profile=body.get("host_capability_profile") or {},
                tool_catalog_version=catalog["catalog_version"],
                data_snapshot_ref=body.get("data_snapshot_ref") or {},
                as_of=body.get("as_of"),
                observed_cohort_id=body.get("observed_cohort_id"),
                actor=identity,
            )
            conn.commit()
    except EvaluationNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    except EvaluationRefused as exc:
        return _refused(exc)
    return JSONResponse(opened, 201)


async def _add_run_case(request: Request) -> Response:
    """POST {base}/evaluation-runs/{run_id}/cases -- pin one question's subject."""
    auth = await _authorize(request, "member")
    if isinstance(auth, Response):
        return auth
    _identity, org_id = auth
    project_id = request.path_params["project_id"]
    run_id = request.path_params["run_id"]
    try:
        body = await _json_body(request)
    except (ValueError, json.JSONDecodeError) as exc:
        return _bad_body(exc)

    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            case = add_run_case(
                conn,
                org_id=org_id,
                project_id=project_id,
                run_id=run_id,
                golden_question_version_id=str(body.get("golden_question_version_id") or ""),
                result_id=body.get("result_id"),
                ai_path_id=body.get("ai_path_id"),
                ai_path_expected=bool(body.get("ai_path_expected", True)),
                capability_key=body.get("capability_key"),
            )
            if body.get("verdicts") is not None:
                case["verdicts"] = record_case_verdicts(
                    conn,
                    org_id=org_id,
                    project_id=project_id,
                    case_id=case["id"],
                    verdicts=body.get("verdicts"),
                )
            conn.commit()
    except EvaluationNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    except EvaluationRefused as exc:
        return _refused(exc)
    return JSONResponse(case, 201)


async def _record_case_verdicts(request: Request) -> Response:
    """POST {base}/evaluation-cases/{case_id}/verdicts -- six rows, or none.

    Supplying five, seven, or a dimension outside the ratified six is refused
    rather than partially written. `mcp_app_behavior` cannot be `pass` while no
    rendered artifact is pinned, and `path_quality` cannot be `pass` without a
    resolved observed AI Path -- both are refused here and by the database.
    """
    auth = await _authorize(request, "member")
    if isinstance(auth, Response):
        return auth
    _identity, org_id = auth
    project_id = request.path_params["project_id"]
    try:
        body = await _json_body(request)
    except (ValueError, json.JSONDecodeError) as exc:
        return _bad_body(exc)

    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            written = record_case_verdicts(
                conn,
                org_id=org_id,
                project_id=project_id,
                case_id=request.path_params["case_id"],
                verdicts=body.get("verdicts"),
            )
            conn.commit()
    except EvaluationNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    except EvaluationRefused as exc:
        return _refused(exc)
    return JSONResponse({"verdicts": written}, 201)


async def _finalize_evaluation_run(request: Request) -> Response:
    """POST {base}/evaluation-runs/{run_id}/finalize -- recording -> finalized.

    Finalizing creates, moves and updates no baseline. That is not an omission:
    `analyze-and-test.md:346` says a baseline is never updated automatically, and
    the only way one comes to exist is the explicit approval route below.
    """
    auth = await _authorize(request, "member")
    if isinstance(auth, Response):
        return auth
    _identity, org_id = auth
    project_id = request.path_params["project_id"]

    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            frozen = finalize_evaluation_run(
                conn,
                org_id=org_id,
                project_id=project_id,
                run_id=request.path_params["run_id"],
            )
            conn.commit()
    except EvaluationNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    except EvaluationRefused as exc:
        return _refused(exc)
    return JSONResponse(frozen, 200)


async def _execute_evaluation_run(request: Request) -> Response:
    """POST {base}/evaluation-runs/{run_id}/execute -- unroll the question set.

    The half of a Regression Run the word *execute* names
    (`analyze-and-test.md:239`). One request pins a case per Golden Question,
    freezes the run and writes six verdicts per case through the writer that can
    judge it -- instead of the caller issuing one request per question and
    remembering the order.

    ATOMIC. Everything commits together or nothing does, so a refusal on the
    seventh question never leaves a run whose question set is an accident of
    where the walk stopped.
    """
    auth = await _authorize(request, "member")
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth
    project_id = request.path_params["project_id"]
    try:
        body = await _json_body(request)
    except (ValueError, json.JSONDecodeError) as exc:
        return _bad_body(exc)

    from core.db import get_connection  # noqa: PLC0415
    from core.evaluation_run_executor import execute_evaluation_run  # noqa: PLC0415

    subjects = body.get("subjects") or {}
    if not isinstance(subjects, dict):
        return _bad_body(ValueError("`subjects` maps a Golden Question version id to its subject"))

    try:
        with get_connection() as conn:
            report = execute_evaluation_run(
                conn,
                org_id=org_id,
                project_id=project_id,
                run_id=request.path_params["run_id"],
                actor=identity,
                golden_question_version_ids=body.get("golden_question_version_ids"),
                subjects=subjects,
                finalize=bool(body.get("finalize", True)),
            )
            conn.commit()
    except EvaluationNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    except EvaluationRefused as exc:
        return _refused(exc)
    return JSONResponse(report, 201)


async def _context_adherence(request: Request) -> Response:
    """GET {base}/context-adherence -- what the pre-query gate measured here.

    The reader `app.query_adherence` never had. It answers one question -- when
    this Project was asked a data question, had the governed context been
    consulted first -- over an explicit window, with the denominator beside every
    count and an honest empty state that names the gesture which fills it.

    It is deliberately NOT the `context_adherence` dimension of a run: that one
    judges one pinned execution against one question. This one is the ambient
    rate over a period, and merging the two would make a project-wide average
    look like a verdict on a case.
    """
    auth = await _authorize(request, "viewer")
    if isinstance(auth, Response):
        return auth
    _identity, _org_id = auth
    project_id = request.path_params["project_id"]
    raw = request.query_params.get("days")
    try:
        days = int(raw) if raw else ADHERENCE_DEFAULT_WINDOW_DAYS
    except (TypeError, ValueError):
        return _bad_body(ValueError("`days` must be a whole number of days"))

    from core.db import get_connection  # noqa: PLC0415

    with get_connection() as conn:
        payload = adherence_overview(conn, project_id=project_id, days=days)
    return JSONResponse(payload)


async def _approve_baseline(request: Request) -> Response:
    """POST {base}/evaluation-runs/{run_id}/baseline -- an explicit approval.

    Actor and reason are mandatory. Replacing an existing baseline inserts a new
    approval and marks the previous one superseded; the previous approval is
    never rewritten, so what was approved stays readable after it stops being
    current.
    """
    auth = await _authorize(request, "owner")
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth
    project_id = request.path_params["project_id"]
    try:
        body = await _json_body(request)
    except (ValueError, json.JSONDecodeError) as exc:
        return _bad_body(exc)

    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            approved = approve_baseline(
                conn,
                org_id=org_id,
                project_id=project_id,
                run_id=request.path_params["run_id"],
                approved_by=str(body.get("approved_by") or identity),
                approval_reason=body.get("approval_reason"),
            )
            conn.commit()
    except EvaluationNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    except EvaluationRefused as exc:
        return _refused(exc)
    return JSONResponse(approved, 201)


# ---------------------------------------------------------------------------
# Comparisons and Gate Decisions.
# ---------------------------------------------------------------------------


async def _create_comparison(request: Request) -> Response:
    """POST {base}/evaluation-comparisons -- refuse any undeclared drift."""
    auth = await _authorize(request, "member")
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth
    project_id = request.path_params["project_id"]
    try:
        body = await _json_body(request)
    except (ValueError, json.JSONDecodeError) as exc:
        return _bad_body(exc)

    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            comparison = create_comparison(
                conn,
                org_id=org_id,
                project_id=project_id,
                baseline_run_id=str(body.get("baseline_run_id") or ""),
                candidate_run_id=str(body.get("candidate_run_id") or ""),
                comparison_kind=body.get("comparison_kind"),
                changed_pin_families=body.get("changed_pin_families") or [],
                actor=identity,
            )
            conn.commit()
    except EvaluationNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    except EvaluationRefused as exc:
        return _refused(exc)
    return JSONResponse(comparison, 201)


async def _emit_gate_decision(request: Request) -> Response:
    """POST {base}/gate-decisions -- evidence for the owner, never a transition.

    Nothing owned by Governance or Context Hub is written by this route. The
    owning workflow reads the decision before its own publish or activate step.
    """
    auth = await _authorize(request, "owner")
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth
    project_id = request.path_params["project_id"]
    try:
        body = await _json_body(request)
    except (ValueError, json.JSONDecodeError) as exc:
        return _bad_body(exc)

    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            decision = emit_gate_decision(
                conn,
                org_id=org_id,
                project_id=project_id,
                comparison_id=str(body.get("comparison_id") or ""),
                candidate_owner_workspace=body.get("candidate_owner_workspace"),
                candidate_object_type=body.get("candidate_object_type"),
                candidate_object_id=body.get("candidate_object_id"),
                candidate_version_id=body.get("candidate_version_id"),
                decided_by=str(body.get("decided_by") or identity),
            )
            conn.commit()
    except EvaluationNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    except EvaluationRefused as exc:
        return _refused(exc)
    return JSONResponse(decision, 201)


# ---------------------------------------------------------------------------
# The five Level 3 tabs. One address each.
# ---------------------------------------------------------------------------


def _read_tab(reader):
    """Build a viewer-scoped GET handler around one read-model function."""

    async def _handler(request: Request) -> Response:
        auth = await _authorize(request, "viewer")
        if isinstance(auth, Response):
            return auth
        _identity, org_id = auth

        from core.db import get_connection  # noqa: PLC0415

        try:
            with get_connection() as conn:
                payload = reader(
                    conn,
                    org_id=org_id,
                    project_id=request.path_params["project_id"],
                    run_id=request.path_params["run_id"],
                )
        except EvaluationNotFound:
            return JSONResponse(_NOT_FOUND, 404)
        return JSONResponse(payload)

    return _handler


_run_overview_tab = _read_tab(run_overview)
_run_cases_tab = _read_tab(run_cases)
_run_comparisons_tab = _read_tab(run_comparisons)
_run_environment_tab = _read_tab(run_environment)
_run_gate_decision_tab = _read_tab(run_gate_decisions)


# Literal segments first. `run-profiles`, `context-version-sets`,
# `evaluation-comparisons`, `evaluation-cases` and `gate-decisions` are declared
# ahead of every `{run_id}` route, and each of the five workbench tabs is
# declared ahead of the bare run address, so a literal can never be read as an id.
evaluation_run_routes = [
    Route(f"{_BASE}/run-profiles", endpoint=_list_run_profiles, methods=["GET"]),
    Route(f"{_BASE}/run-profiles", endpoint=_create_run_profile, methods=["POST"]),
    Route(
        f"{_BASE}/run-profiles/{{run_profile_id}}/baseline",
        endpoint=_get_active_baseline,
        methods=["GET"],
    ),
    Route(
        f"{_BASE}/context-version-sets",
        endpoint=_create_context_version_set,
        methods=["POST"],
    ),
    Route(
        f"{_BASE}/evaluation-comparisons",
        endpoint=_create_comparison,
        methods=["POST"],
    ),
    Route(f"{_BASE}/gate-decisions", endpoint=_emit_gate_decision, methods=["POST"]),
    Route(f"{_BASE}/context-adherence", endpoint=_context_adherence, methods=["GET"]),
    Route(
        f"{_BASE}/evaluation-cases/{{case_id}}/verdicts",
        endpoint=_record_case_verdicts,
        methods=["POST"],
    ),
    Route(f"{_BASE}/evaluation-runs", endpoint=_list_evaluation_runs, methods=["GET"]),
    Route(f"{_BASE}/evaluation-runs", endpoint=_open_evaluation_run, methods=["POST"]),
    Route(
        f"{_BASE}/evaluation-runs/{{run_id}}/cases",
        endpoint=_run_cases_tab,
        methods=["GET"],
    ),
    Route(
        f"{_BASE}/evaluation-runs/{{run_id}}/cases",
        endpoint=_add_run_case,
        methods=["POST"],
    ),
    Route(
        f"{_BASE}/evaluation-runs/{{run_id}}/comparisons",
        endpoint=_run_comparisons_tab,
        methods=["GET"],
    ),
    Route(
        f"{_BASE}/evaluation-runs/{{run_id}}/environment",
        endpoint=_run_environment_tab,
        methods=["GET"],
    ),
    Route(
        f"{_BASE}/evaluation-runs/{{run_id}}/gate-decision",
        endpoint=_run_gate_decision_tab,
        methods=["GET"],
    ),
    Route(
        f"{_BASE}/evaluation-runs/{{run_id}}/execute",
        endpoint=_execute_evaluation_run,
        methods=["POST"],
    ),
    Route(
        f"{_BASE}/evaluation-runs/{{run_id}}/finalize",
        endpoint=_finalize_evaluation_run,
        methods=["POST"],
    ),
    Route(
        f"{_BASE}/evaluation-runs/{{run_id}}/baseline",
        endpoint=_approve_baseline,
        methods=["POST"],
    ),
    Route(f"{_BASE}/evaluation-runs/{{run_id}}", endpoint=_run_overview_tab, methods=["GET"]),
]
