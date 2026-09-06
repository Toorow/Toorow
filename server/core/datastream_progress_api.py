"""Where a run is, read as often as a screen needs -- story 63.2, epic 63.

WHY A ROUTE OF ITS OWN AND NOT ANOTHER WORKBENCH TAB. This is the only route of
the product that is called IN A LOOP: story 63.3 polls it while a run moves, from
three surfaces at once. Serving it from `.../workbench/overview` would cost NINE
queries and TWO connections per poll (`_authorize` opens its own connection,
`_read_base_record` runs two queries, one of them with seven correlated
subqueries, and the overview branch adds six more), to refresh five fields --
while the eight other queries answer the same thing for the whole length of the
run. The service runs at `min-instances=0`; a poll has to cost what it reads.

WHAT IT COSTS. One connection, one guard query, one read query -- for every tick
of a run that is moving, which is every tick that repeats. The read is a probe of
`uq_datastream_executions_active`, the partial unique index that already holds
"the non-terminal execution of this Datastream": at most one row, so the run
itself is fetched with no `ORDER BY` and no `LIMIT`.

The branch that answers "nothing is running" pays for ONE more read, because it
has to say WHY it is not running -- finished well, ended badly, or never started.
That branch is the one that ENDS the poll: it runs once per run, not once per
tick, which is why the explanation lives there and not in the hot path.

THE ACTIVE STATES ARE NEVER TYPED HERE. The list is generated from
`core.execution_states` (story 63.1, the one owner) -- a typed list would be a
seventh copy to keep in step, and the six that existed before broke four
surfaces at once.

AND IT IS GENERATED INTO THE STATEMENT, NOT BOUND AS AN ARRAY. Measured
2026-08-05 on the disposable cluster, under `plan_cache_mode =
force_generic_plan` -- which is what a statement executed in a loop gets once
the driver prepares it:

    generated list   -> Index Scan using `uq_datastream_executions_active`
    `state = ANY($3)` -> `idx_datastream_executions_project_state`

The partial index is usable only when Postgres can prove the query's state list
implies the index predicate, and it cannot prove that about a parameter it has
not been given. Both forms plan identically while the statement is still custom-
planned, which is exactly why this is written down rather than left to a reader
to rediscover.

THE SCOPE IS NOT TAKEN ON TRUST. The SQL below carries `project_id` on both
tables, exactly as story 53.1 closed it on the eight MCP tools. A Datastream of
another project is `not_found`, never a payload that confirms it exists.

Until 2026-08-06 that was the ONLY thing standing between a member of project A
and a stream of project B here: `_require_datastream_role` authorized the
IDENTITY on the PROJECT and the `datastream_id` it received only reached the
audit metadata. AI-219 made the guard prove the pair. This route is the one
caller that declares the proof already made -- `pair_proven_by_read=True` at the
call site -- because it is polled every few seconds and a second statement
proving what the join below proves would take the repeating tick from two
statements to three. Measured at the ASGI seal: 1 connection, 2 statements per
tick, 3 on the tick that ends the poll
(`tests/integration/test_datastream_progress_route_cost.py`).

AND STORY 63.4 KEPT IT THERE. "How much longer" is answered from the finished
runs of this same Datastream, which is a THIRD LATERAL on `app.pull_jobs` inside
the statement that was already being executed -- not a second read. The same
measurement therefore still holds after it: one connection, two statements per
repeating tick.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from core.datastream_progress_estimate import (
    HISTORY_RUNS,
    HISTORY_WINDOWS,
    estimate_time_left,
)
from core.datastream_publication import STATE_CANCELLED
from core.execution_progress import (
    JOB_DONE,
    STOP_NOT_A_COLLECTION_RUN,
    STOP_NOT_FOUND,
    STOP_NOT_RUNNING,
    TERMINAL_JOB_STATES,
    StopRefused,
    stop_collection_run,
    window_span_days,
)
from core.execution_states import ACTIVE_STATES, BY_NAME, TERMINAL_STATES

# The payload carries `timestamptz` and `date` columns straight from the row.
# The stock `JSONResponse` raises `TypeError` inside `render()` -- AFTER the
# handler returned, and OUTSIDE its `try/except` -- which is a bare 500 no error
# branch ever sees.
from core.json_encoding import SafeJSONResponse as JSONResponse
from core.pull_job_states import DEAD_LETTER as JOB_DEAD_LETTER
from core.pull_job_states import FAILED as JOB_FAILED
from core.run_origins import origin_of

logger = logging.getLogger(__name__)

#: The payload contract 63.3 and the three surfaces of 63.5 read.
PROGRESS_SCHEMA = "datastream_progress.v1"

_SAFE_NAME = re.compile(r"^[a-z_]+$")


def _sql_literals(names) -> str:
    """A SQL value list, GENERATED from a frozen Python registry.

    Composed into the statement rather than bound, deliberately: the values come
    from a registry of identifiers, each re-checked here against `[a-z_]+`, and
    never from a request. See the module docstring for the plan measurement that
    makes the difference matter.
    """
    ordered = sorted(names) if isinstance(names, (set, frozenset)) else list(names)
    for name in ordered:
        if not _SAFE_NAME.match(name):
            raise ValueError(f"unusable state name {name!r}")
    return ", ".join(f"'{name}'" for name in ordered)


#: One query for the poll. The LEFT JOIN is what separates the two absences a
#: screen must not confuse: no `app.datastreams` row in this project is
#: `not_found`, while a row with no active execution is an honest
#: `progress: null` -- the answer that stops 63.3's poll.
#:
#: THE THREE LATERALS ARE WHAT MAKE A PLATEAU READABLE. `days_done` moves at a
#: WINDOW boundary, so a run of 24 monthly windows sits at 0/730 for as long as
#: the first window takes and then jumps to 31/730. Without the window in flight
#: and its size, a counter that has not moved for twenty minutes is
#: indistinguishable from a run that is stuck -- and 63.3 cannot invent that
#: distinction on the screen. All three probe an index of `app.pull_jobs`
#: (`idx_pull_jobs_execution`, `idx_pull_jobs_datastream_id`), and all three
#: return nothing at once when no run is active (`run.id` is NULL).
#:
#: THE THIRD ONE IS STORY 63.4, AND IT COSTS NO STATEMENT. It aggregates one
#: seconds-PER-DAY rate per FINISHED run of this Datastream, which is the only
#: sample in the database with enough observations to say anything: the largest
#: window count any path here produces is three, so the run in flight can offer
#: at most two finished windows and usually offers none. Restricting to
#: `execution_id <> run.id` is what "finished" means on this table --
#: `uq_datastream_executions_active` allows AT MOST ONE non-terminal execution
#: per Datastream, so every other run of this stream is terminal by construction.
#:
#: THE RATE IS SUMMED PER WINDOW, and both halves of the fraction cover exactly
#: the same windows. The first version took `max(completed_at) - min(started_at)`
#: over the run's `done` jobs and divided by the days of those jobs: the wall
#: time of every window that FAILED sat in the numerator while its days were
#: excluded from the denominator, so one dead_letter window inflated the rate of
#: the whole run -- and a replayed window did it again. Summing each window's own
#: duration cannot do that.
#:
#: AND THE READ IS BOUNDED BEFORE THE AGGREGATE, which is what migration 219 was
#: written for. `LIMIT {HISTORY_RUNS}` sits above a `GROUP BY`; on its own it
#: bounds the rows RETURNED and not the rows READ, so every 5-second tick
#: re-scanned and sorted every pull job the Datastream ever had (730 of them
#: after two nightly years) while the STATEMENT COUNT never moved. The inner
#: `ORDER BY j.completed_at DESC LIMIT {HISTORY_WINDOWS}` is an index scan of
#: `idx_pull_jobs_datastream_completed`, whose predicate is exactly these
#: filters. `HISTORY_WINDOWS` is derived: 10 runs of at most 3 windows.
#: The oldest run in that slice may be cut short -- which is harmless, because
#: the rate is per DAY and its two halves are cut together.
#:
#: `now()` travels with the row for the same reason: the estimate subtracts two
#: SERVER instants, never a browser epoch and a server timestamp.
PROGRESS_SQL = f"""
    SELECT d.id AS datastream_id,
           run.id AS execution_id,
           run.state,
           run.step,
           run.projection_plan_ref AS projection_plan_ref,
           run.day_in_progress,
           run.days_done,
           run.days_total,
           run.rows_written,
           run.started_at,
           run.progress_updated_at,
           run.plan_version_id,
           run.mapping_version_id,
           tally.windows_total,
           tally.windows_done,
           flight.date_from AS window_from,
           flight.date_to AS window_to,
           flight.started_at AS window_started_at,
           flight.completed_at AS window_completed_at,
           history.rates AS history_seconds_per_day,
           now() AS measured_at
    FROM app.datastreams d
    LEFT JOIN app.datastream_executions run
           ON run.datastream_id = d.id
          AND run.project_id = d.project_id
          AND run.state IN ({_sql_literals(ACTIVE_STATES)})
    LEFT JOIN LATERAL (
        SELECT count(*) AS windows_total,
               count(*) FILTER (
                   WHERE j.state IN ({_sql_literals([JOB_DONE])})
               ) AS windows_done
        FROM app.pull_jobs j
        WHERE j.execution_id = run.id
    ) tally ON TRUE
    LEFT JOIN LATERAL (
        SELECT j.date_from, j.date_to, j.started_at, j.completed_at
        FROM app.pull_jobs j
        WHERE j.execution_id = run.id
          AND j.state NOT IN ({_sql_literals(TERMINAL_JOB_STATES)})
        ORDER BY j.date_from ASC, j.id ASC
        LIMIT 1
    ) flight ON TRUE
    LEFT JOIN LATERAL (
        SELECT array_agg(past.rate) AS rates
        FROM (
            SELECT sum(recent.seconds) / nullif(sum(recent.days), 0) AS rate
            FROM (
                SELECT j.execution_id,
                       j.completed_at,
                       extract(epoch FROM (j.completed_at - j.started_at)) AS seconds,
                       (j.date_to - j.date_from + 1) AS days
                FROM app.pull_jobs j
                WHERE j.datastream_id = d.id
                  AND run.id IS NOT NULL
                  AND j.execution_id IS NOT NULL
                  AND j.execution_id <> run.id
                  AND j.state IN ({_sql_literals([JOB_DONE])})
                  AND j.started_at IS NOT NULL
                  AND j.completed_at IS NOT NULL
                ORDER BY j.completed_at DESC
                LIMIT {HISTORY_WINDOWS}
            ) recent
            GROUP BY recent.execution_id
            ORDER BY max(recent.completed_at) DESC
            LIMIT {HISTORY_RUNS}
        ) past
    ) history ON TRUE
    WHERE d.id = %s AND d.project_id = %s
"""

#: Read ONLY when nothing is running -- the path that ENDS the poll, and runs
#: once per run rather than once per tick. It is what keeps "finished well",
#: "ended badly" and "never started" three sentences instead of one silence.
#:
#: IT CARRIES WHAT THE RUN KEPT (story 63.6). A stopped run's whole promise is
#: that the days already collected stay -- the raw zone is append-only -- and
#: after the stop the run is terminal, so `PROGRESS_SQL` no longer joins it and
#: `progress` is `null`. Without `days_done` and `rows_written` here, "show what
#: was kept" is on no payload at all and the screen has nothing to say. This
#: statement runs ONCE PER RUN, on the branch that ends the poll, so two more
#: columns on a row already being read cost the repeating tick nothing --
#: `tests/integration/test_datastream_progress_route_cost.py` holds that.
IDLE_SQL = f"""
    SELECT e.id AS execution_id,
           e.state,
           e.state_changed_at AS ended_at,
           e.error_code,
           e.days_done,
           e.days_total,
           e.rows_written
    FROM app.datastream_executions e
    WHERE e.datastream_id = %s
      AND e.project_id = %s
      AND e.state IN ({_sql_literals(TERMINAL_STATES)})
    ORDER BY e.state_changed_at DESC
    LIMIT 1
"""

#: What the `progress` object carries, in payload order.
#:
#: `days_done` / `days_total` are DAYS, covered by the windows the run declared
#: and by those it has finished -- true at every instant, and derived from the
#: window bounds rather than counted by a per-day loop. `windows_done` /
#: `windows_total` and `window_in_progress` are what let a screen say WHY the day
#: count is standing still.
#:
#: `estimate` (story 63.4) is COMPUTED HERE and never on a screen: the MCP reads
#: this payload too, and a number derived in the console would be a second answer
#: to "how much longer" that the tools could not see. It always carries a
#: sentence -- including when it declines to estimate.
PROGRESS_FIELDS = (
    "execution_id",
    "state",
    # Story 63.7. WHY this treatment is running, derived from
    # `projection_plan_ref.origin` and resolved against `core.run_origins`. ONE
    # field leaves the plan: the plan itself carries the compiled projection,
    # the retained-execution reference and the recovery scope, none of which is
    # a screen's business -- and this is the one route called in a loop.
    #
    # It is on the payload for the four paths that do not collect, too. Their
    # `windows_*` and `estimate` are silent by construction (the three LATERALs
    # find no `app.pull_jobs` row), and without the origin a person reads a
    # treatment in flight with no days, no fraction and no reason.
    "origin",
    "step",
    "day_in_progress",
    "days_done",
    "days_total",
    "windows_done",
    "windows_total",
    "window_in_progress",
    "rows_written",
    "started_at",
    "progress_updated_at",
    "plan_version_id",
    "mapping_version_id",
    "estimate",
    # WHY A RUN THAT HAS NOT STARTED IS NOT STARTING -- Jean, 2026-08-12.
    #
    # A candidate is minted at `created` and a `candidate_materialization` job is
    # what opens it. When that job dies, the execution stays at `created` for
    # ever and every surface says « this run has not started », which is true and
    # useless: it HAS been attempted, three times, and it failed. Measured the
    # same day: 17 of 17 such jobs in `dead_letter`, none ever `done`, the oldest
    # sitting for thirteen hours while the band showed a spinner.
    #
    # So the payload carries the job's own verdict, and only when there IS one to
    # carry. `None` on a run whose job is queued or running, and `None` on a run
    # that never had a job -- neither is a failure, and a key that appeared for
    # both would be read as one.
    "materialization",
)

#: The sentences an absence may say. Never one silence for all of them.
IDLE_NEVER_RAN = "never_ran"
IDLE_LAST_RUN_SUCCEEDED = "last_run_succeeded"
IDLE_LAST_RUN_FAILED = "last_run_failed"
#: Story 63.6. A run somebody STOPPED is not a run that failed: reporting it
#: under `last_run_failed` would fire the failure sentences on all three
#: surfaces of 63.5 and send an operator hunting for an outage nobody had.
IDLE_LAST_RUN_STOPPED = "last_run_stopped"

IDLE_REASONS = (
    IDLE_NEVER_RAN,
    IDLE_LAST_RUN_SUCCEEDED,
    IDLE_LAST_RUN_FAILED,
    IDLE_LAST_RUN_STOPPED,
)

#: What an `idle` object carries, in payload order.
IDLE_FIELDS = (
    "reason",
    "execution_id",
    "state",
    "ended_at",
    "error_code",
    "days_done",
    "days_total",
    "rows_written",
)


class DatastreamNotFound(LookupError):
    """No Datastream with this id in this project -- or in another one."""


def _window_in_progress(record: dict[str, Any]) -> dict[str, Any] | None:
    """The window being collected, with its SIZE and its clock -- or None.

    The size is what explains the plateau: a 31-day window is ONE provider call,
    so the day count cannot move until it lands. `window_span_days` is story
    63.1's own arithmetic, reused rather than restated.

    `started_at` / `completed_at` are the only per-window chronometer the product
    has (migration 006). `started_at` is what lets story 63.4 see that a run is
    DECELERATING -- a window that has already outrun what this Datastream usually
    spends on its days -- and `completed_at` is `null` for as long as the window
    is in flight, which is what makes "in flight" a fact on the row rather than
    an inference from the state list.
    """
    date_from = record.get("window_from")
    date_to = record.get("window_to")
    if date_from is None or date_to is None:
        return None
    return {
        "date_from": date_from,
        "date_to": date_to,
        "days": window_span_days(date_from, date_to),
        "started_at": record.get("window_started_at"),
        "completed_at": record.get("window_completed_at"),
    }


def read_active_progress(
    conn,
    *,
    project_id: str,
    datastream_id: str,
) -> dict[str, Any] | None:
    """Where the non-terminal run of this Datastream is, or None when none is.

    None means "nothing is moving". WHY it is not moving is a separate question,
    answered by :func:`read_idle_reason` on that branch only -- so the polled
    path stays one index probe and the branch that ends the poll is the one that
    pays for an explanation.

    A column story 63.1 has not written yet comes back as `None` and is rendered
    as `null`. It is never turned into a `0`: a run that has landed nothing has
    no row count, and `0` would be a measurement it never made. `windows_total`
    follows the same rule -- a run with no window bound to it declared none, it
    did not declare zero.
    """
    with conn.cursor() as cur:
        cur.execute(PROGRESS_SQL, (datastream_id, project_id))
        row = cur.fetchone()
        if row is None:
            raise DatastreamNotFound(datastream_id)
        columns = [desc[0] for desc in cur.description]
    record = dict(zip(columns, row))
    if record.get("execution_id") is None:
        return None

    # Story 63.7: one derived field, and the plan does not travel. `origin_of`
    # answers None for every execution minted before this story -- an absence
    # the screen states, never one it guesses its way out of.
    record["origin"] = origin_of(record.pop("projection_plan_ref", None))
    record["window_in_progress"] = _window_in_progress(record)
    if not record.get("windows_total"):
        record["windows_total"] = None
        record["windows_done"] = None

    # Story 63.4. Derived from the row that was already read -- the rates of this
    # Datastream's finished runs travelled with it -- so the estimate costs no
    # statement, and it is computed on the SERVER so that the screen and the MCP
    # cannot hold two opinions about how much longer this run has.
    window = record["window_in_progress"] or {}
    record["estimate"] = estimate_time_left(
        days_done=record.get("days_done"),
        days_total=record.get("days_total"),
        window_days=window.get("days"),
        window_started_at=window.get("started_at"),
        samples=record.get("history_seconds_per_day"),
        measured_at=record.get("measured_at"),
    )
    return {field: record.get(field) for field in PROGRESS_FIELDS}


#: The job that opens a candidate, and what it says when it did not.
#:
#: Read on ONE branch and one only -- a run that has not started. A run in flight
#: was opened by definition, so asking about its opener would be a statement per
#: tick for an answer nobody reads.
_MATERIALIZATION_SQL = """
    SELECT state, error_code, attempt_count, enqueued_at, started_at, completed_at
    FROM app.datastream_activation_jobs
    WHERE execution_id = %(execution_id)s
      AND project_id = %(project_id)s
      AND kind = 'candidate_materialization'
    ORDER BY enqueued_at DESC
    LIMIT 1
"""

#: The two states worth interrupting a person for. `queued` and `running` are the
#: job doing its work, and `done` is a run that opened -- none of the three is a
#: reason to say anything the state badge does not already say.
#:
#: READ FROM THE REGISTRY, NEVER RE-TYPED. `core.pull_job_states` owns the queue
#: vocabulary, and this module's own conformance test refuses a state name typed
#: into it -- rightly, and it caught this line written by hand. These are JOB
#: states, a different vocabulary from the EXECUTION states of
#: `core.execution_states`, which happens to share the word `failed`; taking them
#: from the owner is what keeps the two from being confused for one.
_MATERIALIZATION_STUCK = (JOB_FAILED, JOB_DEAD_LETTER)


def read_materialization(
    conn, *, project_id: str, execution_id: str
) -> dict[str, Any] | None:
    """What became of the job that was supposed to open this run.

    `None` when the job is fine, when it is still working, and when there is
    none -- three different facts, and not one of them is something to report.
    A screen that spoke on all three would be noise on every healthy Datastream.

    Never raises. This annotates a read that must answer; a failure to explain a
    silence must not become a second silence.
    """
    if not execution_id or not project_id:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                _MATERIALIZATION_SQL,
                {"execution_id": execution_id, "project_id": project_id},
            )
            row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001 -- instrumentation, never a blocker
        logger.warning(
            "datastream_progress: materialization_read_failed execution=%s: %s",
            execution_id,
            exc,
        )
        return None
    if row is None or str(row[0]) not in _MATERIALIZATION_STUCK:
        return None
    ended = row[5]
    return {
        "state": str(row[0]),
        # The code VERBATIM. Composing a sentence per code here would be a second
        # vocabulary to keep in step with the queue, and an unknown code would
        # then render as nothing at all -- which is the silence this exists to
        # remove. The screen frames it; it does not translate it.
        "error_code": row[1],
        "attempt_count": row[2],
        "last_attempt_at": ended.isoformat() if hasattr(ended, "isoformat") else ended,
    }


def read_idle_reason(
    conn,
    *,
    project_id: str,
    datastream_id: str,
) -> dict[str, Any]:
    """Why nothing is running -- three distinct sentences, never one silence.

    A poll that stops has to be able to say what it stopped on: a run that
    finished well, a run that ended badly, or a flux that has never started. The
    ratified surface requires empty and broken to read differently, and an
    envelope that answers `progress: null` to all three cannot honour it.

    Which terminal states count as a success is not decided here: the registry
    (`core.execution_states`) decides, and the exact `state` travels with the
    reason so `cancelled` is never flattened into `failed` on a screen.

    AND A RUN SOMEBODY STOPPED IS ITS OWN SENTENCE (story 63.6). It used to fall
    under `last_run_failed` with everything else that is terminal and not a
    success, so a deliberate stop reached every reader -- the MCP included --
    under the name of a failure. It carries WHAT WAS KEPT, because after the stop
    this is the only payload that still has it: the run is terminal, so
    `PROGRESS_SQL` no longer returns it.
    """
    with conn.cursor() as cur:
        cur.execute(IDLE_SQL, (datastream_id, project_id))
        row = cur.fetchone()
        if row is None:
            return {
                **{field: None for field in IDLE_FIELDS},
                "reason": IDLE_NEVER_RAN,
            }
        columns = [desc[0] for desc in cur.description]
    record = dict(zip(columns, row))
    state = str(record.get("state") or "")
    entry = BY_NAME.get(state)
    if state == STATE_CANCELLED:
        record["reason"] = IDLE_LAST_RUN_STOPPED
    elif entry and entry.success:
        record["reason"] = IDLE_LAST_RUN_SUCCEEDED
    else:
        record["reason"] = IDLE_LAST_RUN_FAILED
    return {field: record.get(field) for field in IDLE_FIELDS}


def compose_payload(
    *,
    project_id: str,
    datastream_id: str,
    progress: dict[str, Any] | None,
    idle: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The envelope, always -- never a bare `null` on the wire.

    Story 63.3 has three polls in flight at once; a response that does not say
    which flux it answers cannot be routed to the right one.

    `idle` is the counterpart of `progress`: exactly one of the two is set. A
    `progress: null` on its own is the same body for a run that succeeded, a run
    that failed and a flux that never started -- three different things a screen
    is required to say differently.
    """
    return {
        "schema": PROGRESS_SCHEMA,
        "project_id": project_id,
        "datastream_id": datastream_id,
        "progress": progress,
        "idle": idle if progress is None else None,
    }


def _error(exc: Exception) -> JSONResponse:
    if isinstance(exc, DatastreamNotFound):
        return JSONResponse({"code": "not_found", "message": "Datastream not found"}, 404)
    # An unknown exception leaves a trace, or the 503 is a wall: the Cloud Run
    # log otherwise carries the HTTP line and nothing else. `exc_info=exc` and
    # not `logger.exception()`, which only traces the CURRENT exception.
    logger.error("datastream_progress: unmapped_error %s", type(exc).__name__, exc_info=exc)
    return JSONResponse(
        {"code": "unavailable", "message": "Datastream progress is unavailable"}, 503
    )


async def _read_progress(request: Request) -> Response:
    """GET /api/projects/{project_id}/datastreams/{datastream_id}/progress."""
    from core.admin_api import _check_auth, _require_datastream_role  # noqa: PLC0415

    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Authentication required"}, 401)

    project_id = request.path_params["project_id"]
    datastream_id = request.path_params["datastream_id"]
    try:
        from core.db import get_connection  # noqa: PLC0415

        # ONE connection for the guard AND the read. The Workbench pays two per
        # call; a route polled every few seconds may not.
        with get_connection() as conn:
            denied = _require_datastream_role(
                project_id,
                identity,
                "viewer",
                conn,
                datastream_id=datastream_id,
                # AI-219 made the shared guard prove the stream is in the project.
                # It is the right default and it is the wrong bill HERE: this
                # route is polled every few seconds and `PROGRESS_SQL` already
                # joins `d.id = %s AND d.project_id = %s`, returning the same
                # non-disclosing 404. Claiming the pair is not skipping the
                # proof -- the conformance sweep re-derives this reader's SQL and
                # fails if the claim ever stops being true.
                pair_proven_by_read=True,
            )
            if denied is not None:
                return denied
            progress = read_active_progress(
                conn, project_id=project_id, datastream_id=datastream_id
            )
            # ONE EXTRA STATEMENT, AND ONLY WHERE IT ANSWERS SOMETHING. A run
            # that has already started was opened, so its opener has nothing to
            # explain; asking anyway would put a statement on every tick of every
            # healthy collection.
            if progress is not None and not progress.get("started_at"):
                progress["materialization"] = read_materialization(
                    conn,
                    project_id=project_id,
                    execution_id=str(progress.get("execution_id") or ""),
                )
            # ONLY on the branch that ends the poll. While a run moves, the tick
            # stays exactly one index probe; the explanation is read once, when
            # the answer is "nothing is running".
            idle = (
                None
                if progress is not None
                else read_idle_reason(
                    conn, project_id=project_id, datastream_id=datastream_id
                )
            )
    except Exception as exc:  # noqa: BLE001
        return _error(exc)

    return JSONResponse(
        compose_payload(
            project_id=project_id,
            datastream_id=datastream_id,
            progress=progress,
            idle=idle,
        )
    )


#: The HTTP status each refusal answers with. `not_found` and `forbidden` are the
#: envelopes the rest of this surface already uses; `run_not_running` and
#: `not_a_collection_run` are 409, because the request was well formed and the
#: STATE of the thing is what refuses it.
_STOP_STATUS = {
    STOP_NOT_FOUND: 404,
    STOP_NOT_RUNNING: 409,
    STOP_NOT_A_COLLECTION_RUN: 409,
}

#: The role that may stop a run. THE SAME ONE THAT MAY START ONE -- the refetch
#: endpoint requires `member` (`datastream_collection_api._refetch_datastream`). No new role and
#: no asymmetry: whoever may spend the provider quota may stop spending it.
STOP_MINIMUM_ROLE = "member"


async def _stop_run(request: Request) -> Response:
    """POST /api/projects/{p}/datastreams/{d}/runs/{execution_id}/stop.

    THE SCOPE IS PROVEN, NOT CLAIMED. Unlike the polled read beside it, this is a
    WRITE reached by a click: it does not claim `pair_proven_by_read`, so the
    shared guard of AI-219 proves the stream belongs to the project, and the
    statement below proves the run belongs to that stream. A run of another
    project answers the envelope of an absent run, never one that confirms it
    exists somewhere else.
    """
    from core.admin_api import _check_auth, _require_datastream_role  # noqa: PLC0415

    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Authentication required"}, 401)

    project_id = request.path_params["project_id"]
    datastream_id = request.path_params["datastream_id"]
    execution_id = request.path_params["execution_id"]

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            denied = _require_datastream_role(
                project_id, identity, STOP_MINIMUM_ROLE, conn,
                datastream_id=datastream_id,
            )
            if denied is not None:
                return denied
            # The role guard proved (stream, project); this proves (run, stream).
            # Both are needed: a run of project A and a stream of project A do
            # not make that run this stream's.
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT 1 FROM app.datastream_executions
                    WHERE id = %s AND project_id = %s AND datastream_id = %s
                    """,
                    (execution_id, project_id, datastream_id),
                )
                if cur.fetchone() is None:
                    return JSONResponse(
                        {"code": STOP_NOT_FOUND, "message": "Run not found"}, 404
                    )
            answer = stop_collection_run(
                conn,
                execution_id=execution_id,
                project_id=project_id,
                actor=identity or "anonymous",
            )
            conn.commit()
    except StopRefused as exc:
        return JSONResponse(
            {"code": exc.code, "message": _STOP_MESSAGES[exc.code]},
            _STOP_STATUS[exc.code],
        )
    except Exception as exc:  # noqa: BLE001
        return _error(exc)

    return JSONResponse({"schema": STOP_SCHEMA, "project_id": project_id,
                         "datastream_id": datastream_id, **answer})


#: One English sentence per refusal. A console shows the SERVER's words here:
#: three of the four are situations a person can act on, and "something went
#: wrong" is actionable for none of them.
_STOP_MESSAGES = {
    STOP_NOT_FOUND: "Run not found",
    STOP_NOT_RUNNING: "This run has already ended, so there is nothing to stop.",
    STOP_NOT_A_COLLECTION_RUN: (
        "This run is not a collection, so it cannot be stopped here."
    ),
}

#: The envelope of the stop answer, so a reader can tell it from a progress one.
STOP_SCHEMA = "datastream_run_stop.v1"

PROGRESS_ROUTE_PATH = "/api/projects/{project_id}/datastreams/{datastream_id}/progress"
STOP_ROUTE_PATH = (
    "/api/projects/{project_id}/datastreams/{datastream_id}"
    "/runs/{execution_id}/stop"
)

datastream_progress_routes = [
    Route(
        PROGRESS_ROUTE_PATH,
        endpoint=_read_progress,
        methods=["GET"],
        name="datastream-progress",
    ),
    Route(
        STOP_ROUTE_PATH,
        endpoint=_stop_run,
        methods=["POST"],
        name="datastream-run-stop",
    ),
]
