"""Read-only Project-scoped API for the canonical Datastream Workbench."""

from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from core import datastream_landed_file as landed_file
from core.datastream_workbench import (
    TABS,
    WorkbenchNotFound,
    WorkbenchValidationError,
    read_tab,
    read_workbench,
)
from core.datastream_workbench_cost import CostCascadeUnavailable
from core.datastream_workbench_placements import (
    PlacementEvidenceUnavailable,
    PlacementMatchNotProposed,
)

# Every payload below carries `timestamptz` columns straight from the read
# model. Rendering them with the stock `JSONResponse` raised `TypeError` after
# the handler had succeeded, and `_error` turned that into a permanent 503.
from core.json_encoding import SafeJSONResponse as JSONResponse
from core.plan_line_placements import PlacementNotFoundError, PlacementValidationError
from core.plan_spend_decisions import (
    SpendDecisionNotFoundError,
    SpendDecisionValidationError,
)

logger = logging.getLogger(__name__)


async def _authorize(request: Request, role: str = "viewer") -> str | Response:
    from core.admin_api import _check_auth, _require_datastream_role  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415

    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Authentication required"}, 401)
    project_id = request.path_params["project_id"]
    datastream_id = request.path_params["datastream_id"]
    with get_connection() as conn:
        denied = _require_datastream_role(
            project_id,
            identity,
            role,
            conn,
            datastream_id=datastream_id,
        )
    return denied if denied is not None else str(identity or "")


def _error(exc: Exception) -> JSONResponse:
    from core.datastream_change import DatastreamChangeHostRefused  # noqa: PLC0415
    from core.dq_issue_rows import InvalidIssueStatus, IssueNotFound  # noqa: PLC0415

    # STORY 38.17 AC5, and BEFORE the `ValueError` branch below because
    # `DatastreamChangeError` is one: without this branch a host that cannot
    # confirm would answer `422 invalid_request`, which says the request was
    # malformed -- it was not -- and drops the console continuation on the floor.
    # 403 and not 404: `_authorize` has already granted this caller the member
    # role on this Datastream, so nothing here discloses an existence the caller
    # was not already holding. What is refused is the SURFACE, not the object.
    if isinstance(exc, DatastreamChangeHostRefused):
        return JSONResponse(
            {
                "code": exc.code,
                "message": str(exc),
                "confirmation_available": False,
                "console": exc.continuation,
            },
            403,
        )
    if isinstance(exc, WorkbenchNotFound):
        return JSONResponse({"code": "not_found", "message": "Datastream Workbench not found"}, 404)
    # STORY 59.1. Both are checked BEFORE the `ValueError` branch below, because
    # `InvalidIssueStatus` is one and would otherwise answer `invalid_request` --
    # a code that says nothing about which of the two refusals fired. And the
    # anomaly's 404 carries the Workbench's own sentence: an issue of another
    # Project and an issue that never existed must be indistinguishable.
    if isinstance(exc, IssueNotFound):
        return JSONResponse({"code": "not_found", "message": "Anomaly not found"}, 404)
    if isinstance(exc, InvalidIssueStatus):
        return JSONResponse({"code": "invalid_status", "message": str(exc)}, 422)
    # THE BROKEN CASCADE KEEPS ITS OWN SENTENCE (story 58.6). Falling into the
    # catch-all below would report "Datastream evidence is unavailable", which is
    # the Workbench's sentence for every tab -- and a reader could not tell an
    # unreachable warehouse from a Project that has published no rule. Empty and
    # broken share no word on this tab, and the wire is where that starts.
    # A BLOCKING EXECUTION IS A 409 WITH ITS ID, not a 503 (AI-321, 2026-08-28):
    # `create_execution` refuses a confirm while another execution of the
    # Datastream is non-terminal, and the catch-all below turned that rule into
    # "Datastream evidence is unavailable" -- the sentence the executions door
    # already refuses to say for this case (datastream_executions_api.py:103).
    from core.datastream_publication import ConcurrentExecutionActive  # noqa: PLC0415

    if isinstance(exc, ConcurrentExecutionActive):
        return JSONResponse(
            {
                "code": "concurrent_execution_active",
                "message": (
                    "An execution of this Datastream is still running or stuck; "
                    "wait for it to finish, or fail it from the Runs tab, then confirm again"
                ),
                "blocking_execution_id": exc.blocking_execution_id,
            },
            409,
        )
    if isinstance(exc, CostCascadeUnavailable):
        return JSONResponse({"code": "cost_cascade_unavailable", "message": str(exc)}, 503)
    # STORY 61.1, and the same reason as the line above it: an unreachable mart
    # must not read as a plan whose every campaign is matched. The two 503s carry
    # different codes because they are different repairs -- one is the fee/tax
    # ladder, the other the plan-versus-actual reading.
    if isinstance(exc, PlacementEvidenceUnavailable):
        return JSONResponse({"code": "placement_evidence_unavailable", "message": str(exc)}, 503)
    # The typed placement refusals, BEFORE the `ValueError` branch: both inherit
    # `MediaPlanError`, which is a `ValueError`, so without these two a missing
    # attachment would answer `422 invalid_request` -- a code that says neither
    # what is missing nor that anything is.
    if isinstance(exc, PlacementNotFoundError):
        return JSONResponse({"code": "placement_not_found", "message": str(exc)}, 404)
    if isinstance(exc, PlacementValidationError):
        return JSONResponse({"code": "invalid_placement", "message": str(exc)}, 422)
    # STORY 61.2, same reason as the two above: both inherit `MediaPlanError`,
    # which is a `ValueError`. Without these two, an acceptance with no reason
    # would answer `invalid_request` -- a code that says neither what is missing
    # nor that a reason is what makes the row a decision.
    if isinstance(exc, SpendDecisionNotFoundError):
        return JSONResponse({"code": "plan_not_found", "message": str(exc)}, 404)
    if isinstance(exc, SpendDecisionValidationError):
        return JSONResponse({"code": "invalid_spend_decision", "message": str(exc)}, 422)
    # STORY 61.3, and the same reason again: this is a `ValueError`, and without
    # this branch confirming a campaign the engine never proposed would answer
    # `invalid_request` -- a code that says nothing about the one guard that keeps
    # the recorded level the engine's rather than the caller's.
    if isinstance(exc, PlacementMatchNotProposed):
        return JSONResponse({"code": "match_not_proposed", "message": str(exc)}, 422)
    # THE TRIAL ALLOWANCE REFUSAL KEEPS ITS OWN SENTENCE, AND ITS OWN NUMBERS.
    #
    # `confirm_rollback` re-arms a Datastream that was stopped, which consumes an
    # allowance exactly as starting one does, so `rollback_dataset` now refuses
    # it. `TrialDatastreamLimitError` is NOT a `ValueError`, so without this
    # branch it fell into the catch-all below and a person over their plan read
    # "Datastream evidence is unavailable" -- a sentence about a broken server,
    # for a rule that can name the gesture that repairs. 409 and `to_dict()` are
    # what every other surface already gives it (`datastreams_api.py:221`,
    # `datastream_collection_api.py:186`).
    from core.trial_enforcement import TrialDatastreamLimitError  # noqa: PLC0415

    if isinstance(exc, TrialDatastreamLimitError):
        return JSONResponse(exc.to_dict(), 409)
    # STORY 71.3, and BEFORE the `ValueError` branch: `MeasurementGrainRefused` is
    # one, and the whole point of confirming a grain here is the refusal it carries
    # -- a head or a member the mapping has not bound to the MDM, or a bad
    # concept_kind from the MDM itself. The catch-all `invalid_request` would drop
    # the code the panel reads to say WHICH column needs binding.
    from core.metric_dimensions import (  # noqa: PLC0415
        MeasurementGrainNotFound,
        MeasurementGrainRefused,
    )

    if isinstance(exc, MeasurementGrainNotFound):
        return JSONResponse({"code": "not_found", "message": "Measurement grain not found"}, 404)
    if isinstance(exc, MeasurementGrainRefused):
        return JSONResponse({"code": exc.code, "message": exc.message}, 422)
    if isinstance(exc, (WorkbenchValidationError, ValueError)):
        return JSONResponse({"code": "invalid_request", "message": str(exc)}, 422)
    # UNE EXCEPTION INCONNUE LAISSE UNE TRACE, sinon le 503 est un mur.
    #
    # Mesure du 2026-08-04 : `GET .../workbench/overview` rendait 503 en boucle
    # dans la console de Jean, et les journaux Cloud Run ne portaient QUE la
    # ligne HTTP -- ce module n'avait aucun logger. Le corps rendu au client ne
    # change pas ; c'est la trace serveur qui manquait.
    #
    # `exc_info=exc` et non `logger.exception()` : cette derniere ne trace que
    # l'exception COURANTE, et `_error` recoit la sienne en argument.
    logger.error("datastream_workbench: unmapped_error %s", type(exc).__name__, exc_info=exc)
    return JSONResponse(
        {"code": "unavailable", "message": "Datastream evidence is unavailable"}, 503
    )


async def _read_base(request: Request) -> Response:
    actor = await _authorize(request)
    if isinstance(actor, Response):
        return actor
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            payload = read_workbench(
                conn,
                project_id=request.path_params["project_id"],
                datastream_id=request.path_params["datastream_id"],
            )
        return JSONResponse(payload)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


async def _read_tab(request: Request) -> Response:
    actor = await _authorize(request)
    if isinstance(actor, Response):
        return actor
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            payload = read_tab(
                conn,
                project_id=request.path_params["project_id"],
                datastream_id=request.path_params["datastream_id"],
                tab=request.url.path.rsplit("/", 1)[-1],
                # Story 61.1: `placements` chooses WHICH media plan it reads, and
                # nothing else on this route reads the query string. Passed as a
                # plain dict so the read model stays free of Starlette.
                options=dict(request.query_params),
            )
        return JSONResponse(payload)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


async def _json_body(request: Request) -> dict:
    import json

    raw = await request.body()
    value = json.loads(raw) if raw.strip() else {}
    if not isinstance(value, dict):
        raise WorkbenchValidationError("Request body must be an object")
    return value


async def _read_execution_sample(request: Request) -> Response:
    actor = await _authorize(request)
    if isinstance(actor, Response):
        return actor

    project_id = request.path_params["project_id"]
    datastream_id = request.path_params["datastream_id"]
    # execution_id is in path_params but read_datastream_sample relies on mart boundaries.

    stage = (request.query_params.get("stage") or "processed").strip().lower()
    date_from = (request.query_params.get("date_from") or "").strip()
    date_to = (request.query_params.get("date_to") or "").strip()

    if not date_from or not date_to:
        return JSONResponse(
            {
                "code": "missing_param",
                "message": "date_from et date_to sont requis",
            },
            status_code=400,
        )

    try:
        limit = int(request.query_params.get("limit") or "5")
    except (TypeError, ValueError):
        return JSONResponse(
            {"code": "invalid_param", "message": "limit must be an integer"},
            status_code=400,
        )

    try:
        from core.cache_warehouse import read_datastream_sample  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT module_name FROM app.datastreams "
                    "WHERE id = %s AND project_id = %s AND archived_at IS NULL",
                    (datastream_id, project_id),
                )
                row = cur.fetchone()
            if row is None:
                raise WorkbenchNotFound("Datastream Workbench not found")

            connector = row[0] or ""

            # Since the read model uses DuckDB/BigQuery for fact_daily_kpi, we must NOT
            # pass execution_id. The mart merges executions. The response honestly
            # declares what it served via `served_stage`.
            payload = read_datastream_sample(
                project_id=project_id,
                connector=connector,
                stage=stage,
                date_from=date_from,
                date_to=date_to,
                limit=limit,
            )

        # The frontend components enforce a rigid echo check.
        payload["project_id"] = project_id
        payload["datastream_id"] = datastream_id

        return JSONResponse(payload)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


async def _read_landed_file(request: Request) -> Response:
    """GET `…/workbench/landed-file` -- the last file that ARRIVED, read back.

    Lot B1, amendment 6 of the 2026-08-11 review. A `managed_feed` is PUSHED: it
    has no report profile, no connector relation and no collection window, so the
    day axis beside it is refused for it -- and until this route existed nothing
    took its place. The reading, its bounded masked rows and every one of its
    refusals belong to `core.datastream_landed_file`; this handler owns only the
    authorization and the bound on `limit`.
    """
    actor = await _authorize(request)
    if isinstance(actor, Response):
        return actor
    try:
        limit = int(request.query_params.get("limit") or landed_file.DEFAULT_ROWS)
    except (TypeError, ValueError):
        return JSONResponse(
            {"code": "invalid_param", "message": "limit must be an integer"}, 400
        )
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            payload = landed_file.read_landed_file(
                conn,
                project_id=request.path_params["project_id"],
                datastream_id=request.path_params["datastream_id"],
                limit=limit,
            )
        return JSONResponse(payload)
    except LookupError as exc:
        return _error(WorkbenchNotFound(str(exc)))
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


async def _prepare_change(request: Request) -> Response:
    actor = await _authorize(request, "member")
    if isinstance(actor, Response):
        return actor
    try:
        from core.datastream_change import prepare_change  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        body = await _json_body(request)
        idempotency_key = request.headers.get("Idempotency-Key", "").strip()
        kind = request.url.path.split("/workbench/", 1)[1].split("/", 1)[0]
        with get_connection() as conn, conn.transaction():
            result = prepare_change(
                conn,
                project_id=request.path_params["project_id"],
                datastream_id=request.path_params["datastream_id"],
                kind=kind,
                proposed_payload=body.get("proposed_payload"),
                actor=str(actor),
                idempotency_key=idempotency_key,
                raw_import_id=(
                    str(body.get("raw_import_id")) if body.get("raw_import_id") else None
                ),
            )
        return JSONResponse(result, 201)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


async def _confirm_change(request: Request) -> Response:
    actor = await _authorize(request, "member")
    if isinstance(actor, Response):
        return actor
    try:
        from core.datastream_change import confirm_change, require_confirming_host  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        # STORY 38.17 AC5, BEFORE any connection is opened: a host that has
        # declared it is not an interactive surface must not reach the writer at
        # all. The refusal carries the console destination, because "you cannot
        # do this here" without "here is where you can" is a dead end.
        require_confirming_host(
            request.headers.get("X-Toorow-Host"),
            datastream_id=request.path_params["datastream_id"],
        )
        body = await _json_body(request)
        with get_connection() as conn, conn.transaction():
            result = confirm_change(
                conn,
                project_id=request.path_params["project_id"],
                datastream_id=request.path_params["datastream_id"],
                preparation_id=request.path_params["preparation_id"],
                confirmation_secret=str(body.get("confirmation_secret") or ""),
                actor=str(actor),
            )
        # THE PUSH FOLLOWS THE COMMIT (AI-321, 2026-08-29): the candidate this
        # confirm minted was committed by the `with` above and nothing pushed it
        # -- it waited for the reconciliation sweep, minutes, while the person
        # who confirmed watched a run that never started.
        from core.queue import dispatch_after_commit  # noqa: PLC0415

        dispatch_after_commit(result.get("candidate_job_id"))
        return JSONResponse(result)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


async def _confirm_measurement_grain(request: Request) -> Response:
    """POST {base}/mapping/measurement-grains -- confirm a candidate grain (71.3).

    The gesture derived on the Mapping tab: a metric bound to the MDM, reported
    against the dimensions bound beside it. `confirm_measurement_grain` refuses a
    head or a member no binding of the current mapping backs, then declares the
    grain as a governed MDM version -- via the MDM, on the caller's transaction.
    """
    actor = await _authorize(request, "member")
    if isinstance(actor, Response):
        return actor
    try:
        from core.datastream_workbench import confirm_measurement_grain  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        body = await _json_body(request)
        members = body.get("members")
        if members is None:
            members = body.get("member_field_ids")
        with get_connection() as conn, conn.transaction():
            grain = confirm_measurement_grain(
                conn,
                project_id=request.path_params["project_id"],
                datastream_id=request.path_params["datastream_id"],
                name=body.get("name"),
                head_field_id=body.get("head") or body.get("head_field_id"),
                member_field_ids=members if isinstance(members, list) else [],
                description=body.get("description"),
                actor=str(actor),
            )
        return JSONResponse({"measurement_grain": grain}, 201)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


async def _prepare_rollback(request: Request) -> Response:
    actor = await _authorize(request, "member")
    if isinstance(actor, Response):
        return actor
    try:
        from core.datastream_rollback import prepare_rollback  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        body = await _json_body(request)
        with get_connection() as conn, conn.transaction():
            result = prepare_rollback(
                conn,
                project_id=request.path_params["project_id"],
                datastream_id=request.path_params["datastream_id"],
                target_execution_id=(str(body.get("target_execution_id") or "") or None),
                actor=str(actor),
            )
        return JSONResponse(result, 201)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


async def _confirm_rollback(request: Request) -> Response:
    actor = await _authorize(request, "member")
    if isinstance(actor, Response):
        return actor
    try:
        from core.datastream_rollback import confirm_rollback  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        body = await _json_body(request)
        with get_connection() as conn, conn.transaction():
            result = confirm_rollback(
                conn,
                project_id=request.path_params["project_id"],
                datastream_id=request.path_params["datastream_id"],
                preparation_id=request.path_params["preparation_id"],
                confirmation_secret=str(body.get("confirmation_secret") or ""),
                actor=str(actor),
            )
        return JSONResponse(result)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


async def _read_anomaly_rows(request: Request) -> Response:
    """GET `…/workbench/runs/{execution_id}/anomalies/{issue_id}/rows[?format=csv]`

    A ROUTE OF ITS OWN, AND THAT IS THE ARBITRAGE. Faulty rows are replayed and
    never stored (Jean, 2026-08-07), so unfolding an issue costs a read of the
    warehouse. Carried on the `runs` payload it would cost one read per run
    DISPLAYED -- two hundred of them on a Datastream that has run every night --
    for a panel almost nobody unfolds. Here, it is paid only when asked for.

    `format=csv` is the export, and it is the SAME replay: the file is written
    from the rows this call returned and from nothing else, so a CSV can never
    carry a row the screen did not show. A profile with no replayable rows has no
    file at all -- the console shows no `Download` for it, and this refuses it
    with the profile's own sentence rather than handing back an empty header.
    """
    actor = await _authorize(request)
    if isinstance(actor, Response):
        return actor
    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.dq_issue_rows import replay_issue_rows, rows_csv  # noqa: PLC0415

        with get_connection() as conn:
            payload = replay_issue_rows(
                conn,
                project_id=request.path_params["project_id"],
                datastream_id=request.path_params["datastream_id"],
                execution_id=request.path_params["execution_id"],
                issue_id=request.path_params["issue_id"],
            )
        if request.query_params.get("format") != "csv":
            return JSONResponse(payload)
        if payload.get("rows") is None:
            # NO READING, NO FILE. An export that hands back a header for a
            # replay that never ran is a file that looks like evidence.
            return JSONResponse(
                {
                    "code": str(payload.get("reason") or "rows_unavailable"),
                    "message": payload.get("message") or payload.get("rows_absence_message"),
                },
                422,
            )
        issue_id = str(payload.get("issue_id") or "")
        return Response(
            content=rows_csv(payload),
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="anomaly_{issue_id}_rows.csv"'
            },
        )
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


async def _transition_anomaly(request: Request) -> Response:
    """POST `…/workbench/anomalies/{issue_id}/transitions`

    `Mark as reviewed` had no mounted route to reach: the seven `/api/dq/*` were
    unmounted by story 49.4 (`admin_api.py#api/dq`) and the one that carried
    the word acknowledged `app.alert_firings`, a different table under the same
    word. `dq_governance.transition_issue` had zero application callers. This is
    the one, and it moves `app.dq_issues`.
    """
    actor = await _authorize(request, "member")
    if isinstance(actor, Response):
        return actor
    try:
        from datetime import date  # noqa: PLC0415

        from core.db import get_connection  # noqa: PLC0415
        from core.dq_issue_rows import InvalidIssueStatus, transition_run_issue  # noqa: PLC0415

        body = await _json_body(request)
        raw_until = str(body.get("suppressed_until") or "").strip()
        try:
            until = date.fromisoformat(raw_until) if raw_until else None
        except ValueError as exc:
            raise InvalidIssueStatus(f"suppressed_until is not a calendar day: {exc}") from exc
        with get_connection() as conn, conn.transaction():
            result = transition_run_issue(
                conn,
                project_id=request.path_params["project_id"],
                datastream_id=request.path_params["datastream_id"],
                issue_id=request.path_params["issue_id"],
                event_kind=str(body.get("event_kind") or ""),
                actor=str(actor),
                reason=(str(body.get("reason") or "").strip() or None),
                suppressed_until=until,
            )
        return JSONResponse(result)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


def _placement_scope(
    conn, *, project_id: str, datastream_id: str, plan_id: str
) -> str:
    """The connector this Datastream collects from -- once the capability allows it.

    IT LOST THE WORD `write` IN STORY 61.3, and that is not cosmetic. The same
    three checks now guard the suggestion READ: a reading that answered while the
    capability is off would be a second authority on whether the tab exists, and a
    reading that trusted the caller's `plan_id` would hand back the line labels of
    another Project's plan through this Datastream's address. A guard named for
    writes is a guard the next reader will not think to put on a read.

    THREE CHECKS, IN THIS ORDER, AND THE FIRST IS THE CAPABILITY. « Éteinte, la
    capacité n'apparaît nulle part » is not only about pixels: a write that
    succeeds while the tab it belongs to does not exist is a second authority on
    whether the capability is on. The read model refuses first, so the write does
    too, and with the same reader.

    Then `module_name`, never `config.connector_name`: the second is a display
    name, and a placement attached under a display name would hang from a campaign
    of no connector at all. Read from the database rather than taken from the
    request, because a caller that could name its own connector could reach
    another Datastream's slice from inside this Datastream's address.

    THEN THE PLAN, and story 61.2 added it for the whole family rather than for
    its own route. `plan_id` arrives from a request body or a query string on all
    THREE placement writes; without this read, a member of one Project could name
    another Project's plan from inside their own Datastream's address and reach
    its rows -- `attach_placement` scopes on the (line, campaign) parent and
    `detach_placement` on (id, plan, connector), and neither of those predicates
    knows which Project the plan belongs to. Refused as NOT FOUND, and with the
    Workbench's own sentence: a plan that exists elsewhere and a plan that never
    existed must be indistinguishable from here.
    """
    from core.plan_spend_decisions import plan_belongs_to_project  # noqa: PLC0415
    from core.project_capability_states import (  # noqa: PLC0415
        PLACEMENT_MAPPING_CAPABILITY_KEY,
        capability_is_active,
        read_capability_state,
    )

    state = read_capability_state(
        conn, project_id=project_id, capability_key=PLACEMENT_MAPPING_CAPABILITY_KEY
    )
    if not capability_is_active(state):
        raise WorkbenchNotFound("Datastream Workbench not found")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT module_name FROM app.datastreams "
            "WHERE id = %s AND project_id = %s AND archived_at IS NULL",
            (datastream_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        raise WorkbenchNotFound("Datastream Workbench not found")

    if not plan_belongs_to_project(conn, plan_id=plan_id, project_id=project_id):
        raise WorkbenchNotFound("Datastream Workbench not found")
    return str(row[0] or "")


async def _attach_placement(request: Request) -> Response:
    """POST `…/workbench/placements/attachments`

    THE GESTURE THE `Permet` LINE PROMISES. « attacher un ou plusieurs placements
    à une ligne, détacher » (`epic-61`) had no address to reach until story 61.1,
    and a control with no route behind it is the defect this epic was rejected for
    seven times.

    `member`, like every other write of this Workbench: reading a plan is a
    viewer's right, deciding which placements a line bought is not.

    SEVERAL VALUES IN ONE ACT -- amended 2026-08-18. « attacher un ou plusieurs
    placements à une ligne » is the `Permet` line, and until this amendment the
    plural had no address: the screen offered one picker and one button, so
    buying Feed, Marketplace and Search results for one campaign was three
    round trips, three re-reads of the whole tab, and three chances to stop
    half-way with the line in a state nobody chose.
    `breakdown_values` is the plural of `breakdown_value` and the two are the
    same request -- ONE transaction over the loop, so a refusal on the third
    value leaves none of the three attached. A single value is still ONE call to
    the store, which is what keeps the per-value refusals (`the connector
    declares no dimension`, `the pair carries no mapping`) exactly as they were.
    """
    actor = await _authorize(request, "member")
    if isinstance(actor, Response):
        return actor
    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.plan_line_placements import attach_placement  # noqa: PLC0415

        body = await _json_body(request)
        project_id = request.path_params["project_id"]
        datastream_id = request.path_params["datastream_id"]
        values = _placement_values(body)
        with get_connection() as conn, conn.transaction():
            connector = _placement_scope(
                conn,
                project_id=project_id,
                datastream_id=datastream_id,
                plan_id=str(body.get("plan_id") or ""),
            )
            attached = [
                attach_placement(
                    conn,
                    plan_id=str(body.get("plan_id") or ""),
                    line_key=str(body.get("line_key") or ""),
                    connector=connector,
                    campaign_ref=str(body.get("campaign_ref") or ""),
                    breakdown_dimension=str(body.get("breakdown_dimension") or ""),
                    breakdown_value=value,
                    actor=str(actor),
                )
                for value in values
            ]
        # THE SINGULAR ANSWER IS UNCHANGED. One value in, one placement out, with
        # the shape every caller written before this amendment reads; the plural
        # is the only thing that had to grow a wrapper.
        if len(values) == 1:
            return JSONResponse(attached[0], 201)
        return JSONResponse({"attached": attached, "count": len(attached)}, 201)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


def _placement_values(body: dict) -> list[str]:
    """The placement values this call is about, singular or plural, de-duplicated.

    A body may name one (`breakdown_value`) or several (`breakdown_values`), and
    naming neither is refused HERE rather than by the store: the store's refusal
    is about ONE blank value, and a caller that sent an empty list needs to be
    told the list was empty, not that a value was blank.

    Order is the caller's, and a value repeated in the list is attached once: the
    unique index would have made the second call a no-op anyway, and a count of
    three that wrote two is a confirmation that named the wrong scope.
    """
    raw = body.get("breakdown_values")
    if raw is None:
        return [str(body.get("breakdown_value") or "")]
    if not isinstance(raw, list):
        raise WorkbenchValidationError("`breakdown_values` must be a list of placement values.")
    seen: list[str] = []
    for value in raw:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.append(text)
    if not seen:
        raise WorkbenchValidationError(
            "Attaching placements requires at least one observed placement value."
        )
    return seen


async def _detach_placement(request: Request) -> Response:
    """DELETE `…/workbench/placements/attachments/{placement_id}`

    The plan travels in the query string because the store scopes the delete by
    (id, plan, connector) and not by id alone: a UUID somebody guessed must not
    reach another Project's plan through this Project's address.
    """
    actor = await _authorize(request, "member")
    if isinstance(actor, Response):
        return actor
    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.plan_line_placements import detach_placement  # noqa: PLC0415

        project_id = request.path_params["project_id"]
        datastream_id = request.path_params["datastream_id"]
        with get_connection() as conn, conn.transaction():
            connector = _placement_scope(
                conn,
                project_id=project_id,
                datastream_id=datastream_id,
                plan_id=str(request.query_params.get("plan_id") or ""),
            )
            result = detach_placement(
                conn,
                plan_id=str(request.query_params.get("plan_id") or ""),
                connector=connector,
                placement_id=request.path_params["placement_id"],
                actor=str(actor),
            )
        return JSONResponse(result)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


async def _accept_unmatched_spend(request: Request) -> Response:
    """POST `…/workbench/placements/spend-decisions`

    THE SECOND HALF OF THE `Permet` LINE, and it had no address at all.
    « sur une dépense hors plan, la rattacher **ou** l'accepter comme telle »
    (`epic-61:103-104`): re-attaching has had one since story 22.3, accepting had
    none, and a control with no route behind it is the defect this epic was
    rejected for seven times.

    `member`, like every other write of this Workbench, and the connector is read
    from `app.datastreams` rather than taken from the body -- the scope of the
    decision is `(plan_id, connector, campaign_ref)` and only its middle term is
    the caller's to state, which is why the caller does not state it.

    It moves NO money: `app.plan_unmatched_spend_decisions` carries no amount, is
    absent from `mirror_sync.py`, and `plan_vs_actual_daily.sql` keeps ventilating
    on `plan_line_mappings.status = 'active'` alone.
    """
    actor = await _authorize(request, "member")
    if isinstance(actor, Response):
        return actor
    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.plan_spend_decisions import accept_unmatched_spend  # noqa: PLC0415

        body = await _json_body(request)
        project_id = request.path_params["project_id"]
        datastream_id = request.path_params["datastream_id"]
        plan_id = str(body.get("plan_id") or "")
        with get_connection() as conn, conn.transaction():
            connector = _placement_scope(
                conn,
                project_id=project_id,
                datastream_id=datastream_id,
                plan_id=plan_id,
            )
            result = accept_unmatched_spend(
                conn,
                project_id=project_id,
                plan_id=plan_id,
                connector=connector,
                campaign_ref=str(body.get("campaign_ref") or ""),
                reason=str(body.get("reason") or ""),
                actor=str(actor),
            )
        return JSONResponse(result, 201)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


async def _read_placement_suggestions(request: Request) -> Response:
    """GET `…/workbench/placements/suggestions?plan_id=…`

    THE ADDRESS `core/plan_mapping_suggest.py` NEVER HAD. That module has been a
    384-line end-to-end reader since epic 27 with exactly one importer, its own
    test -- the sixth engine of this batch shipped without a caller. This is its
    caller, and story 61.3's first acceptance is that it exists.

    A READ, so `viewer`: seeing which campaigns resemble which plan line is a
    reading of the plan and of observed spend, and both are a viewer's right.
    Deciding is not, and the confirmation below asks for `member`.

    ON REQUEST AND NOT ON LOAD -- arbitrage A4 (b). The sweep is every line
    against every campaign of this connector over the plan's window, and the
    `Placements` tab says so instead of paying it for everybody who opens it.
    """
    actor = await _authorize(request)
    if isinstance(actor, Response):
        return actor
    try:
        from core.datastream_workbench_placements import (  # noqa: PLC0415
            read_placement_suggestions,
        )
        from core.db import get_connection  # noqa: PLC0415

        project_id = request.path_params["project_id"]
        datastream_id = request.path_params["datastream_id"]
        plan_id = str(request.query_params.get("plan_id") or "")
        with get_connection() as conn:
            connector = _placement_scope(
                conn,
                project_id=project_id,
                datastream_id=datastream_id,
                plan_id=plan_id,
            )
            payload = read_placement_suggestions(
                conn,
                project_id=project_id,
                datastream_id=datastream_id,
                connector=connector,
                plan_id=plan_id,
            )
        return JSONResponse(
            {
                "schema": "datastream_workbench.placement_suggestions.v1",
                "project_id": project_id,
                "datastream_id": datastream_id,
                "evidence": payload,
            }
        )
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


async def _confirm_placement_match(request: Request) -> Response:
    """POST `…/workbench/placements/matches`

    THE HUMAN ACT, and the only thing on this tab that creates a match. A
    suggestion is a proposal until somebody named confirms it -- `052:82-88`
    (AD-9, « no silent value fusion ») governs even an `exact` one -- so the level
    never applies itself, and nothing in the suggestion reading writes.

    The body names the plan, the line and the campaign, and NOT the level: that is
    re-derived from the engine inside the store call. A caller able to state its
    own level could write `Exact code` over a 0.89 resemblance, and the number
    beside `Name similarity` would stop meaning anything.

    `member`, like every other write of this Workbench.

    SEVERAL PAIRS IN ONE ACT -- amended 2026-08-18, and it is what makes the
    `Exact code` tier usable. A sweep over a real plan proposes forty exact-code
    matches; confirming them one dialog at a time is forty confirmations that all
    say the same thing, which is how people learn to click past a confirmation.
    `matches` is the plural of `(line_key, campaign_ref)` and the two are the
    same request: ONE transaction, so a refusal on the eleventh pair leaves none
    of the ten before it written, and the count the console named before the act
    is the count that landed or nothing at all.

    THE LEVEL IS STILL NOT SENT, on one pair or on forty. `confirm_placement_match`
    asks the engine again for each; a caller able to send a batch AND its levels
    could write `Exact code` over forty resemblances in one press.
    """
    actor = await _authorize(request, "member")
    if isinstance(actor, Response):
        return actor
    try:
        from core.datastream_workbench_placements import (  # noqa: PLC0415
            confirm_placement_match,
        )
        from core.db import get_connection  # noqa: PLC0415

        body = await _json_body(request)
        project_id = request.path_params["project_id"]
        datastream_id = request.path_params["datastream_id"]
        plan_id = str(body.get("plan_id") or "")
        pairs = _match_pairs(body)
        with get_connection() as conn, conn.transaction():
            connector = _placement_scope(
                conn,
                project_id=project_id,
                datastream_id=datastream_id,
                plan_id=plan_id,
            )
            confirmed = [
                confirm_placement_match(
                    conn,
                    connector=connector,
                    plan_id=plan_id,
                    line_key=line_key,
                    campaign_ref=campaign_ref,
                    actor=str(actor),
                )
                for line_key, campaign_ref in pairs
            ]
        if len(pairs) == 1:
            return JSONResponse(confirmed[0], 201)
        return JSONResponse({"confirmed": confirmed, "count": len(confirmed)}, 201)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


def _match_pairs(body: dict) -> list[tuple[str, str]]:
    """The `(line_key, campaign_ref)` pairs this confirmation is about.

    Singular (`line_key` + `campaign_ref`) or plural (`matches`), de-duplicated
    and in the caller's order. An empty plural is refused here rather than
    silently confirming nothing and answering `201`: a batch that wrote nothing
    and reported success is the confirmation this repository keeps finding.
    """
    raw = body.get("matches")
    if raw is None:
        return [(str(body.get("line_key") or ""), str(body.get("campaign_ref") or ""))]
    if not isinstance(raw, list):
        raise WorkbenchValidationError("`matches` must be a list of {line_key, campaign_ref}.")
    pairs: list[tuple[str, str]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise WorkbenchValidationError(
                "Each match must be an object naming a line and a campaign."
            )
        pair = (
            str(entry.get("line_key") or "").strip(),
            str(entry.get("campaign_ref") or "").strip(),
        )
        if pair[0] and pair[1] and pair not in pairs:
            pairs.append(pair)
    if not pairs:
        raise WorkbenchValidationError(
            "Confirming matches requires at least one line and campaign to confirm."
        )
    return pairs


_BASE = "/api/projects/{project_id}/datastreams/{datastream_id}/workbench"
datastream_workbench_routes = (
    [Route(_BASE, endpoint=_read_base, methods=["GET"])]
    + [
        Route(
            f"{_BASE}/{tab}",
            endpoint=_read_tab,
            methods=["GET"],
            name=f"datastream-workbench-{tab}",
        )
        for tab in TABS
    ]
    + [
        Route(f"{_BASE}/mapping/changes", endpoint=_prepare_change, methods=["POST"]),
        Route(f"{_BASE}/processing/changes", endpoint=_prepare_change, methods=["POST"]),
        # Story 71.3. Under `mapping/` because the candidate grain is derived from
        # the mapping's `mdm_target` bindings and confirmed against them: it is the
        # gesture of that tab and of no other. It declares a governed MDM version,
        # so it is a POST, and the refusal it can carry (a column not bound to the
        # MDM) is the point of confirming it here rather than in Governance blind.
        Route(
            f"{_BASE}/mapping/measurement-grains",
            endpoint=_confirm_measurement_grain,
            methods=["POST"],
        ),
        Route(
            f"{_BASE}/outputs/rollback-preparations", endpoint=_prepare_rollback, methods=["POST"]
        ),
        Route(
            f"{_BASE}/outputs/rollback-preparations/{{preparation_id}}/confirm",
            endpoint=_confirm_rollback,
            methods=["POST"],
        ),
        Route(
            f"{_BASE}/changes/{{preparation_id}}/confirm",
            endpoint=_confirm_change,
            methods=["POST"],
        ),
        # Story 59.1. The rows are under the RUN because that is where the
        # anomaly unfolds; the acknowledgement is not, because an issue outlives
        # the run that saw it last (`open_issue` moves `execution_id` with
        # `last_seen_at`).
        Route(
            f"{_BASE}/runs/{{execution_id}}/anomalies/{{issue_id}}/rows",
            endpoint=_read_anomaly_rows,
            methods=["GET"],
        ),
        Route(
            f"{_BASE}/runs/{{execution_id}}/sample",
            endpoint=_read_execution_sample,
            methods=["GET"],
        ),
        # Lot B1. NOT under `runs/`: a file source's rows are read from the file
        # that arrived, and the import that wrote them may have no candidate
        # execution at all (a no-op run creates none). Hanging it off a run would
        # ask the screen for an id the arrival does not carry.
        Route(
            f"{_BASE}/landed-file",
            endpoint=_read_landed_file,
            methods=["GET"],
        ),
        Route(
            f"{_BASE}/anomalies/{{issue_id}}/transitions",
            endpoint=_transition_anomaly,
            methods=["POST"],
        ),
        # Story 61.1. Under `placements/` because they are the gestures of that
        # tab and of no other, and their reading is refused with it the moment
        # `placement_mapping` is off.
        Route(
            f"{_BASE}/placements/attachments",
            endpoint=_attach_placement,
            methods=["POST"],
        ),
        Route(
            f"{_BASE}/placements/attachments/{{placement_id}}",
            endpoint=_detach_placement,
            methods=["DELETE"],
        ),
        # Story 61.2. Beside the attachments and not under them: accepting spend
        # as unplanned is the OPPOSITE gesture -- it says no plan line will ever
        # claim this campaign -- and hanging it off `attachments/` would read as a
        # kind of attachment.
        Route(
            f"{_BASE}/placements/spend-decisions",
            endpoint=_accept_unmatched_spend,
            methods=["POST"],
        ),
        # Story 61.3. The reading and the act, side by side and NOT one under the
        # other: a confirmation is not a sub-resource of a proposal, it is what
        # makes a proposal stop being one. Both are under `placements/` because
        # they are the gestures of that tab and of no other, and both are refused
        # the moment `placement_mapping` is off.
        Route(
            f"{_BASE}/placements/suggestions",
            endpoint=_read_placement_suggestions,
            methods=["GET"],
        ),
        Route(
            f"{_BASE}/placements/matches",
            endpoint=_confirm_placement_match,
            methods=["POST"],
        ),
    ]
)
