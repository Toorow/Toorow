"""Ce que la plateforme s appelle a elle-meme -- jamais une personne.

AD-43, 2026-08-13. Neuf routes `/internal/...` : les tics d horloge (nocturne,
horaire, DQ, sante, rapprochement des files et des horloges, vidage de l outbox)
et les deux executants pousses par Cloud Tasks. Elles ne portent PAS la meme
autorisation que le reste -- `_authorize_internal` accepte un secret partage ou
un jeton OIDC, la ou toute autre porte exige une identite humaine.

C est exactement pourquoi elles meritaient leur fichier : melangees aux routes
de personnes, la difference d autorisation se lisait comme un oubli.
"""

from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger("core.admin_api")

# --- le joint qui reste dans admin_api -----------------------------------
async def _authorize_internal(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import."""
    from core.admin_api import _authorize_internal as _impl  # noqa: PLC0415
    return await _impl(*args, **kwargs)

# --- les handlers, deplaces TELS QUELS -----------------------------------

async def _run_dq_monitors_internal(request: Request) -> Response:
    """POST /internal/scheduler/run-dq-monitors -- Cloud Scheduler trigger.

    THE SIX DQ MONITORS HAD NO PUSH TRIGGER. They ran in a daemon thread inside
    this process, and this service is deployed `--min-instances=0` without
    `--no-cpu-throttling`: between two requests the instance is throttled, and at
    zero instances it does not exist. So the loop had nowhere to run, and the
    only other way to reach the monitors was `POST /api/dq/evaluate` -- a
    rate-limited human action. Every automatic quality alert on the platform was
    therefore never emitted, including the arrival monitor that tells an operator
    a delivered file did not come (AI-113).

    `min-instances=0` is not negotiable -- it is the cost posture -- so the
    answer is the one its six siblings already take: a dumb, frequent external
    tick that asks "what is due?" and re-reads the ledger. This endpoint is that
    tick's target.

    SCOPE. Without `project_id` every project is evaluated, which is what a
    platform tick wants; with one, only that project, which is what a targeted
    re-check wants. `run_dq_monitors` never raises and honours
    DQ_MONITORS_ENABLED, so a deployment that has not turned the monitors on
    answers 200 with zeros rather than failing a scheduled call forever.

    Returns the monitor summary so the tick's own logs carry what happened,
    rather than a bare 200 that proves only that something answered.
    """
    err = await _authorize_internal(request)
    if err is not None:
        return err

    project_id = (request.query_params.get("project_id") or "").strip() or None

    from core.dq_monitors import run_dq_monitors  # noqa: PLC0415

    try:
        summary = run_dq_monitors(project_id=project_id)
    except Exception as exc:  # noqa: BLE001 -- documented as never-raising; trust nothing
        logger.exception("admin_api: run_dq_monitors failed: %s", exc)
        return JSONResponse(
            {"code": "dq_monitors_failed", "message": "DQ monitor sweep raised"},
            status_code=503,
        )
    return JSONResponse(summary, status_code=200)

async def _dispatch_nightly_internal(request: Request) -> Response:
    """POST /internal/scheduler/dispatch-nightly -- Cloud Scheduler trigger (Story 3.4, AC3).

    Auth via ``_authorize_internal``: OIDC, or the shared secret, or a human on
    the platform allow-list (AI-127). The three sentences that used to stand
    here were each false by the time they were read -- "Phase B only, 404 for
    local backend" (story 56.5 removed that gate), "auth via _check_auth"
    (it is the fallback branch, not the gate), and a reference to
    ``_check_internal_auth``, which had no caller at all.

    No google-cloud-scheduler import at runtime (HG-1).

    Response (200): {"ran": "nightly_steps", "date": "..."}
    """
    err = await _authorize_internal(request)
    if err is not None:
        return err

    # Story 56.5: the QUEUE_BACKEND gate is GONE. It answered 404 unless the push
    # backend was on, which coupled two independent things -- WHO carries the
    # clock, and HOW a job is delivered. Cloud Scheduler is the right trigger
    # whatever the queue backend is, and the 404 made the endpoint untestable in
    # exactly the mode a deployment starts in.
    #
    # AI-166 (2026-08-18, measured on the 02:00 Paris fire): this endpoint used
    # to call bare `dispatch_nightly()` while its hourly sibling calls
    # `run_hourly_steps` -- so every OTHER nightly step (dbt_per_project, alert
    # checks, DQ monitors, notebooks, briefings) was only reachable from the
    # in-process clock loop, which a request-scoped container never runs. The
    # steps had NEVER run in production over HTTP. `run_nightly_steps` includes
    # dispatch_nightly as its first step, isolates every step (AI-32), and holds
    # the advisory lock against double-fire -- a Scheduler retry lands on
    # `nightly_skipped` instead of a second run.
    from datetime import date as _date  # noqa: PLC0415

    from core.scheduler import run_nightly_steps  # noqa: PLC0415

    today = _date.today()
    run_nightly_steps(today)
    return JSONResponse({"ran": "nightly_steps", "date": today.isoformat()})

async def _execute_pull_internal(request: Request) -> Response:
    """POST /internal/worker/execute-pull/{job_id} -- Cloud Tasks push target (AD-36).

    THE ROUTE THE TASKS ALREADY POINTED AT. ``CloudTasksBackend`` has built this
    exact URL since story 3.2 (``queue.py``: ``f"{worker_url}/internal/worker/
    execute-pull/{job_id}"``) and nothing served it, so flipping QUEUE_BACKEND
    would have produced a 404 answered by retries, forever. Measured 2026-07-31
    (AI-97): ``grep -rn execute-pull server`` returned the URL construction and a
    test, never a route.

    THE STATUS CODE IS THE CONTRACT, because Cloud Tasks reads it as an
    instruction rather than as information:

      * 200 -- terminal. Executed (done / failed / dead_letter), or the job was
        ALREADY terminal, or it is unknown. The task stops.
      * 429 -- quota-blocked. ``_execute_job`` returned the job to 'queued'
        without spending an attempt, so the task must come back later. This is
        the one case where a retry is the right answer.
      * 409 -- another attempt holds the claim. The task stops; a genuinely
        stuck claim is released by ``recover_stale_running_jobs``, not by
        piling a second execution on top of the first.

    A REDELIVERED TASK MUST NOT RE-PULL. Cloud Tasks is at-least-once, so the
    same job_id can arrive twice. ``claim_job_by_id`` refuses anything that is
    not 'queued', which makes the second delivery a no-op rather than a second
    pull of the same window -- and the append-only + supersede-by-pull_id
    contract (AD-7) would hide the duplicate rather than reveal it, so the
    refusal has to happen HERE.

    A failing pull is NOT a failing task: ``_execute_job`` records the failure on
    the job row and returns True. Answering 5xx would make Cloud Tasks re-run a
    job the ledger already marked failed, on top of its own retry policy.
    """
    err = await _authorize_internal(request)
    if err is not None:
        return err

    job_id = (request.path_params.get("job_id") or "").strip()
    if not job_id:
        return JSONResponse(
            {"code": "missing_id", "message": "job_id is required"},
            status_code=400,
        )

    from core.queue import (  # noqa: PLC0415
        QUEUED,
        RUNNING,
        claim_job_by_id,
        execute_claimed_job,
        get_job_status,
    )

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            job = claim_job_by_id(conn, job_id)
    except Exception as exc:
        logger.error("admin_api: execute_pull claim_error job=%s: %s", job_id, exc)
        # The ledger could not be read. That is transient by nature, and losing
        # the task would lose the operation -- so this is a retry, not a drop.
        return JSONResponse(
            {"code": "claim_failed", "message": "Cannot claim the job"},
            status_code=503,
        )

    if job is None:
        # Not claimable. WHICH not-claimable decides what the task does next, so
        # the state is read rather than assumed.
        status = get_job_status(job_id)
        if status is None:
            logger.warning("admin_api: execute_pull unknown job_id=%s", job_id)
            return JSONResponse(
                {"code": "unknown_job", "message": "No such job", "job_id": job_id},
                status_code=200,
            )
        state = status.get("state")
        if state == RUNNING:
            return JSONResponse(
                {"code": "already_running", "job_id": job_id, "state": state},
                status_code=409,
            )
        if state == QUEUED:
            # CONTENDED, not terminal. The claim uses FOR UPDATE SKIP LOCKED, so a
            # row another delivery is claiming right now is SKIPPED -- and that
            # transaction's move to 'running' is not visible here until it
            # commits. Reading 'queued' after a failed claim therefore means
            # "someone else has it", never "it is finished". Reporting it as
            # terminal put a state of 'queued' under a code saying the opposite,
            # which is what an operator would read while debugging.
            # Either way this task stops -- the other delivery owns the work, and
            # if that transaction rolls back the reconciliation sweep re-issues it.
            return JSONResponse(
                {"code": "claim_contended", "job_id": job_id, "state": state},
                status_code=409,
            )
        return JSONResponse(
            {"code": "already_terminal", "job_id": job_id, "state": state},
            status_code=200,
        )

    try:
        executed = execute_claimed_job(job)
    except Exception as exc:  # noqa: BLE001 -- a crash must not strand the claim
        logger.exception("admin_api: execute_pull failed job=%s: %s", job_id, exc)
        # The job stays 'running' and is recovered by the stale-claim sweep; the
        # task retries so the work is not silently dropped.
        return JSONResponse(
            {"code": "execution_error", "message": "Job execution raised", "job_id": job_id},
            status_code=503,
        )

    if not executed:
        return JSONResponse(
            {"code": "quota_blocked", "job_id": job_id, "state": QUEUED},
            status_code=429,
        )

    final = get_job_status(job_id) or {}
    return JSONResponse(
        {"job_id": job_id, "state": final.get("state"), "executed": True},
        status_code=200,
    )

async def _execute_activation_internal(request: Request) -> Response:
    """POST /internal/worker/execute-activation/{job_id} -- push target (story 56.3).

    THE QUEUE NOBODY REMEMBERED. ``_worker_loop`` drains TWO queues: pull jobs,
    then -- only when the first is empty -- the setup-preview and
    candidate-materialisation work that the Datastream wizard depends on. Its
    only drainer was that loop, so flipping QUEUE_BACKEND without this route
    would have stopped materialising candidates altogether and hung the wizard,
    in silence (AI-98). It is also the link that leaves 42 Datastreams in
    'draft' today: at --min-instances=0 the loop only advances while a request
    happens to be in flight.

    Same status contract as the pull target, with one difference that comes from
    the queue itself: an activation job RETRIES IN PLACE ('failed' is claimable
    again under the attempt ceiling), so a failure that has attempts left is a
    429 -- come back -- while a dead-lettered one is 200.
    """
    err = await _authorize_internal(request)
    if err is not None:
        return err

    job_id = (request.path_params.get("job_id") or "").strip()
    if not job_id:
        return JSONResponse(
            {"code": "missing_id", "message": "job_id is required"},
            status_code=400,
        )

    from core.queue import (  # noqa: PLC0415
        claim_activation_job_by_id,
        execute_claimed_activation_job,
        get_activation_job_state,
    )

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            job = claim_activation_job_by_id(conn, job_id)
    except Exception as exc:
        logger.error("admin_api: execute_activation claim_error job=%s: %s", job_id, exc)
        return JSONResponse(
            {"code": "claim_failed", "message": "Cannot claim the job"},
            status_code=503,
        )

    if job is None:
        state = get_activation_job_state(job_id)
        if state is None:
            logger.warning("admin_api: execute_activation unknown job_id=%s", job_id)
            return JSONResponse(
                {"code": "unknown_job", "message": "No such job", "job_id": job_id},
                status_code=200,
            )
        if state == "running":
            return JSONResponse(
                {"code": "already_running", "job_id": job_id, "state": state},
                status_code=409,
            )
        if state in ("queued", "failed"):
            # Same contention as the pull target: these ARE the claimable states,
            # so seeing one after a failed claim means the row was locked by
            # another delivery -- or that its attempt ceiling is reached, which
            # the claim also refuses. Neither is "finished".
            return JSONResponse(
                {"code": "claim_contended_or_exhausted", "job_id": job_id, "state": state},
                status_code=409,
            )
        # 'done' or 'dead_letter': terminal.
        return JSONResponse(
            {"code": "already_terminal", "job_id": job_id, "state": state},
            status_code=200,
        )

    try:
        execute_claimed_activation_job(job)
    except Exception as exc:  # noqa: BLE001 -- the claim must not be stranded
        logger.exception("admin_api: execute_activation failed job=%s: %s", job_id, exc)
        return JSONResponse(
            {"code": "execution_error", "message": "Job execution raised", "job_id": job_id},
            status_code=503,
        )

    # The outcome lives on the row: the worker records 'done', 'failed' (retryable)
    # or 'dead_letter' (terminal) rather than raising.
    state = get_activation_job_state(job_id)
    if state == "failed":
        return JSONResponse(
            {"code": "retryable_failure", "job_id": job_id, "state": state},
            status_code=429,
        )
    return JSONResponse(
        {"job_id": job_id, "state": state, "executed": True},
        status_code=200,
    )

async def _reconcile_queues_internal(request: Request) -> Response:
    """POST /internal/scheduler/reconcile-queues -- the ledger commands (story 56.4).

    Cloud Scheduler target. Re-dispatches work the LEDGER shows pending and no
    live task is serving, in both queues.

    IT RE-DISPATCHES, IT NEVER EXECUTES. That is the line between a
    reconciliation and a second worker, and AD-36 draws it explicitly: the day
    this endpoint runs a job itself, one invocation carries N jobs and the
    readability the whole migration buys is gone.
    """
    err = await _authorize_internal(request)
    if err is not None:
        return err

    from core.queue import reconcile_pending_tasks  # noqa: PLC0415

    try:
        result = reconcile_pending_tasks()
    except Exception as exc:  # noqa: BLE001 -- a periodic job must not 500 loudly forever
        logger.exception("admin_api: reconcile_queues failed: %s", exc)
        return JSONResponse(
            {"code": "reconcile_failed", "message": "Reconciliation raised"},
            status_code=503,
        )
    return JSONResponse(result, status_code=200)

async def _reconcile_clocks_internal(request: Request) -> Response:
    """POST /internal/scheduler/reconcile-clocks -- the clock that watches the clocks.

    AI-117. `app.platform_clocks` holds two halves: what this deployment
    DECLARES each clock to be, and what Cloud Scheduler was last OBSERVED to
    hold. Nothing refreshed the second half. `/api/platform/clocks` observes on
    read, but only while somebody has the screen open -- so between two visits
    the stored observation aged with no upper bound, and an `observed_at` from
    last week renders exactly like one from a minute ago. A registry built to
    make drift visible was itself drifting in silence.

    IT OBSERVES AND RECORDS. IT NEVER APPLIES. Re-imposing the declared value
    here would destroy the only evidence that a human edited a job by hand, so a
    drift stays a drift until somebody NAMES the clock through
    `/api/platform/clocks/{name}/apply`.

    200 EVEN WHEN GCP COULD NOT BE READ. An unreachable Scheduler API is already
    recorded per clock as the verdict `unknown` WITH its reason -- never
    `in_sync`. Answering 503 as well would turn one missing credential into a
    permanently failing scheduled job. 503 is reserved for the reconciliation
    itself raising with NOTHING recorded.
    """
    err = await _authorize_internal(request)
    if err is not None:
        return err

    from core.db import get_connection  # noqa: PLC0415
    from core.platform_clocks import reconcile  # noqa: PLC0415

    try:
        with get_connection() as conn:
            result = reconcile(conn)
    except Exception as exc:  # noqa: BLE001 -- a periodic job must not 500 forever
        logger.exception("admin_api: reconcile_clocks failed: %s", exc)
        return JSONResponse(
            {
                "code": "clock_reconcile_failed",
                "message": "Platform clock reconciliation raised",
            },
            status_code=503,
        )

    return JSONResponse(
        {
            "observation_ok": bool(result.get("observation_ok")),
            "observation_error": result.get("observation_error"),
            "counts": result.get("counts") or {},
            "verdicts": [
                {
                    "clock_name": entry.get("clock_name"),
                    "job_id": entry.get("job_id"),
                    "verdict": entry.get("verdict"),
                }
                for entry in (result.get("verdicts") or [])
            ],
            # Stated in the payload, not only in a docstring: whoever reads this
            # must be able to see that nothing was corrected on their behalf.
            "drift_is_reported_not_repaired": True,
        },
        status_code=200,
    )

async def _dispatch_hourly_internal(request: Request) -> Response:
    """POST /internal/scheduler/dispatch-hourly -- Cloud Scheduler trigger (story 56.5).

    The nightly loop also carried an HOURLY branch (`run_hourly_steps`), which no
    endpoint exposed: moving the clock out of the process without this would have
    silently dropped every hourly datastream.
    """
    err = await _authorize_internal(request)
    if err is not None:
        return err

    from core.scheduler import run_hourly_steps  # noqa: PLC0415

    try:
        run_hourly_steps()
    except Exception as exc:  # noqa: BLE001 -- a scheduled run reports, never 500s blindly
        logger.exception("admin_api: dispatch_hourly failed: %s", exc)
        return JSONResponse(
            {"code": "hourly_failed", "message": "Hourly steps raised"},
            status_code=503,
        )
    return JSONResponse({"ran": "hourly"}, status_code=200)

async def _poll_health_internal(request: Request) -> Response:
    """POST /internal/scheduler/poll-health -- the daily sweep (story 56.6).

    The backstop, not the mechanism. Health is refreshed at the event that
    changes it; this catches what happens outside our flows -- a token revoked
    at the provider, a scope withdrawn -- which nothing here can observe.
    """
    err = await _authorize_internal(request)
    if err is not None:
        return err

    from core.health_poller import _run_one_poll_cycle  # noqa: PLC0415

    try:
        polled = _run_one_poll_cycle()
    except Exception as exc:  # noqa: BLE001
        logger.exception("admin_api: poll_health failed: %s", exc)
        return JSONResponse(
            {"code": "poll_failed", "message": "Health poll raised"}, status_code=503
        )
    return JSONResponse({"polled": polled}, status_code=200)

async def _drain_outbox_internal(request: Request) -> Response:
    """POST /internal/scheduler/drain-outbox -- publish pending facts (story 56.7).

    The outbox row is written in the SAME transaction as the mutation it
    describes, so a fact can never disagree with the state it reports. This
    endpoint is what finally takes those rows OUT: `app.operation_outbox` has
    carried state/attempts/retry_at/delivered_at since migration 060, and
    `dispatch_outbox_event` never had a production caller -- every fact this
    product has ever recorded is still sitting there, undelivered.
    """
    err = await _authorize_internal(request)
    if err is not None:
        return err

    from core.events import drain_outbox  # noqa: PLC0415

    try:
        result = drain_outbox()
    except Exception as exc:  # noqa: BLE001
        logger.exception("admin_api: drain_outbox failed: %s", exc)
        return JSONResponse(
            {"code": "drain_failed", "message": "Outbox drain raised"}, status_code=503
        )
    return JSONResponse(result, status_code=200)


# --- LES ROUTES, dans l'ordre exact ou elles etaient declarees ------------
#
# Starlette resout dans l'ordre de declaration ; chaque collection est epissee
# a la position que ses routes occupaient. La preuve est un dump avant/apres.

INTERNAL_ROUTES_1 = [
    # Story 3.4 (AC3): Cloud Scheduler dispatch stub (Phase B, QUEUE_BACKEND=cloud_tasks)
    Route(
        "/internal/scheduler/run-dq-monitors",
        endpoint=_run_dq_monitors_internal,
        methods=["POST"],
    ),
    Route(
        "/internal/scheduler/dispatch-nightly",
        endpoint=_dispatch_nightly_internal,
        methods=["POST"],
    ),
    # Story 56.2 (AD-36): the Cloud Tasks push target. CloudTasksBackend has
    # addressed this exact path since story 3.2 and nothing served it, so the
    # push backend could only ever produce a 404 answered by retries.
    Route(
        "/internal/worker/execute-pull/{job_id}",
        endpoint=_execute_pull_internal,
        methods=["POST"],
    ),
    # Story 56.3 (AD-36): the SECOND queue. Its only drainer was the daemon
    # loop, so a push deployment without this route would stop materialising
    # candidates and hang the Datastream wizard, in silence.
    Route(
        "/internal/worker/execute-activation/{job_id}",
        endpoint=_execute_activation_internal,
        methods=["POST"],
    ),
    # Story 56.4: the ledger commands -- re-dispatch what is pending with no
    # live task. Re-dispatches, never executes.
    Route(
        "/internal/scheduler/reconcile-queues",
        endpoint=_reconcile_queues_internal,
        methods=["POST"],
    ),
    # AI-117: the clock that watches the clocks. Without it the OBSERVED half
    # of app.platform_clocks is refreshed only while a human has the screen
    # open, so a week-old observation renders exactly like a fresh one.
    Route(
        "/internal/scheduler/reconcile-clocks",
        endpoint=_reconcile_clocks_internal,
        methods=["POST"],
    ),
    # Story 56.5: the hourly branch of the loop had no endpoint at all.
    Route(
        "/internal/scheduler/dispatch-hourly",
        endpoint=_dispatch_hourly_internal,
        methods=["POST"],
    ),
    # Story 56.6: the daily health sweep -- the backstop behind the
    # event-driven refresh, not the mechanism.
    Route(
        "/internal/scheduler/poll-health",
        endpoint=_poll_health_internal,
        methods=["POST"],
    ),
    # Story 56.7: take the facts OUT of the outbox they have never left.
    Route(
        "/internal/scheduler/drain-outbox",
        endpoint=_drain_outbox_internal,
        methods=["POST"],
    ),
]
