"""A run that is collecting writes where it is -- Story 63.1, epic 63.

THE TWO HALVES OF A COLLECTION WERE NEVER JOINED. Measured on preprod
2026-08-05: ``app.datastream_executions`` held 0 rows for 6 ``app.pull_jobs``.
The paths that collect (nightly dispatch, hourly dispatch, refetch) create N
pull jobs and ZERO executions; the only path that creates an execution enqueues
no pull. So this module carries both halves:

  * ``open_collection_run`` mints the execution a recurring/refetch run belongs
    to, and declares its windows;
  * ``record_window_progress`` writes where that run is, ONCE PER WINDOW;
  * ``close_collection_run_if_complete`` gives it a terminal state, so the next
    night's dispatch and every later publish are not answered 409 forever by
    ``uq_datastream_executions_active``.

THE UNIT IS THE WINDOW, NOT THE DAY. The code does not produce days: a provider
call is one round trip for a whole window (``google-ads`` builds a single GAQL
over the range, paginates internally and inserts once). Looping per day would
turn a two-year catch-up from 24 provider calls into 730, on 39 connectors.
``days_done`` is therefore DERIVED from the bounds of the finished windows, and
the screen says windows -- it does not pretend to have days it never asked for.

NOTHING IS ACCUMULATED. Every number is derived from the run's own pull jobs and
written with ``GREATEST``, so a replayed window can neither double-count nor move
a counter backwards. That is what makes the write idempotent without a ledger.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from ulid import ULID

from core import pull_job_states as _pull_job_states
from core.run_origins import stamp_origin

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# The ratified step vocabulary.
# ---------------------------------------------------------------------------
#
# docs/product-architecture/datastream-workbench-and-wizard.md: "Les quatre
# etapes d'un flux se nomment `Collect`, `Map`, `Check`, `Publish` -- alignees
# sur les etages de donnee `collected`, `mapped`, `processed`, `published`. Ni
# `Fetch`, ni `Enrich`, ni `Load` : ce sont les mots d'Adverity, pas les notres."

STEP_COLLECT = "Collect"
STEP_MAP = "Map"
STEP_CHECK = "Check"
STEP_PUBLISH = "Publish"

PROGRESS_STEPS: tuple[str, ...] = (STEP_COLLECT, STEP_MAP, STEP_CHECK, STEP_PUBLISH)

#: The projection-plan ``kind`` a collection run declares. It is what tells a
#: reader that this execution is a retrieval, not a publication candidate.
COLLECTION_PLAN_KIND = "recurring_collection"

# ---------------------------------------------------------------------------
# Pull-job states, read as "is this window still moving".
# ---------------------------------------------------------------------------
#
# STORY 63.6 GAVE THEM A REGISTRY, AND THIS MODULE HELD ONE OF THE SIX COPIES.
# It listed three names and read everything else as a window still in flight --
# so a `superseded` window (migration 022) left ``all_terminal`` False and the
# run open FOREVER, holding ``uq_datastream_executions_active`` and answering 409
# to every later publish. The names below are re-exported because callers and
# tests already read them here; their MEANING now has exactly one owner.

JOB_DONE = _pull_job_states.DONE
JOB_FAILED = _pull_job_states.FAILED
JOB_DEAD_LETTER = _pull_job_states.DEAD_LETTER
JOB_CANCELLED = _pull_job_states.CANCELLED

#: A window that will not move again. ``failed`` belongs here: nothing re-queues
#: a failed job (``_dequeue_one`` selects ``state = 'queued'`` only), so treating
#: it as still running would leave the run open forever.
TERMINAL_JOB_STATES = _pull_job_states.TERMINAL_JOB_STATES
FAILED_JOB_STATES = _pull_job_states.FAILED_JOB_STATES

#: Error code carried by a run whose windows did not all land.
ERROR_WINDOW_FAILED = "collection_window_failed"

#: Error code carried by a run a PERSON stopped. Distinct from the one above:
#: "somebody decided this had gone far enough" and "a provider call broke" are
#: two different facts, and a screen that shows the same code for both makes an
#: operator hunt for an outage that never happened.
ERROR_RUN_STOPPED = "collection_run_stopped"

#: What the refused windows carry, so a person reading a job row later knows the
#: window was not tried and not lost -- it was given back.
STOP_WINDOW_DETAIL = "stopped before this window started; the days can be re-queued"

# ---------------------------------------------------------------------------
# Stopping a run -- story 63.6.
# ---------------------------------------------------------------------------

#: The four refusals, each its own sentence. One code for all four would make a
#: console say "something went wrong" to four situations, three of which a
#: person can act on.
STOP_NOT_FOUND = "not_found"
STOP_NOT_RUNNING = "run_not_running"
STOP_NOT_A_COLLECTION_RUN = "not_a_collection_run"
STOP_FORBIDDEN = "forbidden"

STOP_REFUSALS = (
    STOP_NOT_FOUND,
    STOP_NOT_RUNNING,
    STOP_NOT_A_COLLECTION_RUN,
    STOP_FORBIDDEN,
)

#: What the answer carries, in payload order. Read by the console band AND by
#: the MCP tool, which is why it is declared once instead of being spelled out
#: at each door.
STOP_FIELDS = (
    "execution_id",
    "state",
    "windows_refused",
    "window_in_flight",
    "days_kept",
    "rows_kept",
    "stopped_at",
)


class StopRefused(Exception):
    """A stop that will not happen, carrying WHICH of the four reasons it is."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# ---------------------------------------------------------------------------
# Pure derivation -- no database, so the arithmetic is testable on its own.
# ---------------------------------------------------------------------------


def _as_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def window_span_days(date_from: Any, date_to: Any) -> int | None:
    """Inclusive day count of ONE window, or None when its bounds are unreadable.

    A window of 2026-07-01..2026-07-31 is 31 days and ONE provider call. This is
    the only place a day count is produced, and it is produced from bounds --
    never from a loop that would ask the provider once per day.
    """
    start = _as_date(date_from)
    end = _as_date(date_to)
    if start is None or end is None:
        return None
    span = (end - start).days + 1
    return span if span >= 1 else None


def declared_days_total(windows: Iterable[Mapping[str, Any]] | None) -> int | None:
    """Days covered by every window a run declared, or None when it declared none.

    None and 0 are different answers and the screen must be able to tell them
    apart: NULL means "this run declared no window", never "zero days of work".
    """
    if not windows:
        return None
    total = 0
    counted = 0
    for window in windows:
        span = window_span_days(window.get("date_from"), window.get("date_to"))
        if span is None:
            continue
        total += span
        counted += 1
    return total if counted else None


@dataclass(frozen=True)
class CollectionProgress:
    """Where a run is, derived from the bounds and states of its own windows."""

    days_done: int
    rows_written: int | None
    day_in_progress: date | None
    windows_total: int
    windows_finished: int
    windows_failed: int
    #: Windows that ended without ever being attempted: refused by a stop (story
    #: 63.6), replaced by the dedup index (`superseded`, migration 022), or
    #: refused by the SOURCE before it could run (`prevented`, AI-307). A THIRD
    #: bucket, because none of them is a success or a failure -- counting them as
    #: failures would make `close_collection_run_if_complete` mark the whole run
    #: failed, and story 57.8 would then retry it within the hour. For
    #: `prevented` that retry would run hourly against a grant only a human at
    #: the provider can give.
    windows_stopped: int = 0

    @property
    def all_terminal(self) -> bool:
        """True when no window of this run can move again (including none at all).

        THE THIRD BUCKET IS NOT BOOKKEEPING. Before story 63.6 a window in any
        state this function did not name fell into "still in flight", so a run
        with one `superseded` window stayed non-terminal FOREVER -- holding
        `uq_datastream_executions_active` and answering 409 to every later
        publish and to every following night's dispatch. That is the exact
        catastrophe 63.1 closed, reachable through a state 63.1 did not list.
        """
        finished = self.windows_finished + self.windows_failed + self.windows_stopped
        return finished >= self.windows_total


def derive_progress(jobs: Sequence[Mapping[str, Any]]) -> CollectionProgress:
    """Derive a run's progress from its pull jobs.

    ``days_done`` is the sum of the spans of the FINISHED windows -- the arbitrage
    of story 63.1, and the reason a replay changes nothing: a window that is
    already ``done`` is counted once whatever happens next.

    ``rows_written`` is None until the first window finishes. A run that has
    landed nothing yet has no row count, and 0 would be a claim it cannot make.
    """
    days_done = 0
    rows_written = 0
    windows_finished = 0
    windows_failed = 0
    windows_stopped = 0
    last_finished_day: date | None = None
    first_pending_day: date | None = None

    for job in jobs:
        state = str(job.get("state") or "")
        span = window_span_days(job.get("date_from"), job.get("date_to"))
        if state == JOB_DONE:
            windows_finished += 1
            if span is not None:
                days_done += span
            rows_written += int(job.get("row_count") or 0)
            end = _as_date(job.get("date_to"))
            if end is not None and (last_finished_day is None or end > last_finished_day):
                last_finished_day = end
        elif state in FAILED_JOB_STATES:
            windows_failed += 1
        elif _pull_job_states.is_terminal(state):
            # Terminal, and it landed nothing: refused by a stop, superseded by
            # the dedup index, or refused by the source (`prevented`, AI-307). It
            # cannot move again, and it did not fail. `rows_written` is untouched
            # on purpose -- a prevented window took no count, and adding its
            # column (NULL) as a zero is the arithmetic AI-307 exists to refuse.
            windows_stopped += 1
        else:
            start = _as_date(job.get("date_from"))
            if start is not None and (
                first_pending_day is None or start < first_pending_day
            ):
                first_pending_day = start

    # The day in progress is the first day of the window now being collected;
    # once nothing is pending it is the last day the run actually reached.
    day_in_progress = first_pending_day or last_finished_day

    return CollectionProgress(
        days_done=days_done,
        rows_written=rows_written if windows_finished else None,
        day_in_progress=day_in_progress,
        windows_total=len(jobs),
        windows_finished=windows_finished,
        windows_failed=windows_failed,
        windows_stopped=windows_stopped,
    )


# ---------------------------------------------------------------------------
# The database half.
# ---------------------------------------------------------------------------

_PROGRESS_COLUMN_NAMES = (
    "step",
    "day_in_progress",
    "days_done",
    "days_total",
    "rows_written",
    "started_at",
    "progress_updated_at",
)

_PROGRESS_COLUMNS = ", ".join(_PROGRESS_COLUMN_NAMES)

#: The same list, qualified, for the one statement that also reads the row as it
#: was BEFORE the update (``record_window_progress``).
_PROGRESS_COLUMNS_QUALIFIED = ", ".join(f"e.{name}" for name in _PROGRESS_COLUMN_NAMES)


def read_run_jobs(conn, execution_id: str) -> list[dict[str, Any]]:
    """Every pull job bound to *execution_id*, oldest window first."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, date_from, date_to, state, row_count
            FROM app.pull_jobs
            WHERE execution_id = %s
            ORDER BY date_from ASC, id ASC
            """,
            (execution_id,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# The span of a step -- story 58.10, migration 223.
# ---------------------------------------------------------------------------
#
# `app.datastream_executions.step` is ONE word: where the run is NOW. Migration
# 218 wrote why in its own header -- "Progress is a CURRENT STATE, not a
# history" -- and the activity track needs a history: `Collect 2 min` ->
# `Map 2 s` -> `Check 1 s` -> `Publish 1 min`. So each step gets a row with two
# ends.
#
# A SPAN OPENS WHEN THE STEP IS ENTERED, NEVER WHEN ITS RESULT IS RECORDED.
# The first cut of this story opened it in `record_window_progress`, and that
# function is called by `queue._execute_job` ONCE, at the end of the window --
# inside the same transaction that then closes the run. Postgres `NOW()` is
# `transaction_timestamp()`, so both ends carried the identical instant and a
# two-second collection measured `0.000000`. Probed on the disposable base:
# `('Collect', 18:02:41.658549, 18:02:41.658549)`. The span did not cover the
# work; it covered the bookkeeping that followed it.
#
# So `Collect` opens where the run declares it has begun -- `open_collection_run`,
# the same statement that writes migration 218's `started_at`, and for the same
# reason. `record_window_progress` keeps the CLOSE-ON-MOVE and opens only the
# step it is actually moving TO. A step with no entry point in today's code is
# left UNOPENED and the read model says it was not timed: an invented entry
# instant is a duration nobody measured.
#
# EVERY INSTANT HERE IS `clock_timestamp()`, never `NOW()`. Two writes inside
# one transaction must not share an instant, which is exactly the defect above.
#
# `ended_at IS NULL` means the step is STILL RUNNING. It never means "took no
# time" -- which is why the terminal seam below exists: a run that reached a
# terminal state has nothing still running, and a track that printed "still
# running" under a run that ended two days ago would be the same fabricated
# reading as `0 s`.


def _step_span_id() -> str:
    """House-style id, matching migration 223's CHECK."""
    return f"dsse_{ULID()}"


def open_step_span(conn, *, execution_id: str, step: str, at: Any = None) -> None:
    """Open the span of a step the run is ENTERING, at the instant it entered.

    ``at`` is that instant when the caller already holds it -- `Collect` opens
    at the run's own ``started_at``, so the span covers the collection instead
    of trailing it. Without one, the entry instant is `clock_timestamp()`: the
    moment this transition is written.

    ``ON CONFLICT DO NOTHING``, so a replayed window can neither duplicate a row
    (the UNIQUE is ``(execution_id, step)``) nor move the start of a span
    already opened. A step re-entered after it was closed keeps the span of its
    FIRST entry: the table holds one span per (run, step) by contract, and
    re-opening a closed one would report a run as still working on a step it
    finished.

    Scope comes from the execution's own row, so a caller can never bind a span
    to the wrong Project -- and ``org_id`` is read from the Datastream, which is
    where it lives (``app.datastream_executions`` carries none).
    """
    if step not in PROGRESS_STEPS:
        raise ValueError(f"unknown progress step {step!r}; expected one of {PROGRESS_STEPS}")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.datastream_execution_step_evidence
                (id, org_id, project_id, datastream_id, execution_id, step, started_at)
            SELECT %s, d.org_id, e.project_id, e.datastream_id, e.id, %s,
                   COALESCE(%s::timestamptz, clock_timestamp())
              FROM app.datastream_executions e
              JOIN app.datastreams d
                ON d.id = e.datastream_id AND d.project_id = e.project_id
             WHERE e.id = %s
            ON CONFLICT (execution_id, step) DO NOTHING
            """,
            (_step_span_id(), step, at, execution_id),
        )


def close_open_step_spans(conn, *, execution_id: str, except_step: str | None = None) -> None:
    """End every span of this run that is still open -- or all but one of them.

    Called at the two events that end a step: a MOVE to another step
    (``except_step`` is the one being entered, which must stay open) and the
    run reaching a TERMINAL state (nothing of it is still running).

    ``GREATEST(started_at, clock_timestamp())`` and not a bare clock. A stop
    opened at 10:04 whose in-flight window opens its span at 10:07 would
    otherwise write an end BEFORE the start -- migration 223's
    ``ended_at >= started_at`` raises `CheckViolation`, the caller's whole
    transaction aborts, and the `Stop` gesture answers 500 for a bookkeeping
    row. That interleaving is the one this module already documents in
    ``stop_collection_run``; it is real, and it is guarded here rather than
    hoped against.

    Idempotent: only a NULL end is ever written, so asking twice does not move a
    timestamp.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE app.datastream_execution_step_evidence
               SET ended_at = GREATEST(started_at, clock_timestamp()),
                   updated_at = clock_timestamp()
             WHERE execution_id = %s AND ended_at IS NULL
               AND (%s::text IS NULL OR step <> %s)
            """,
            (execution_id, except_step, except_step),
        )


def record_window_progress(
    conn,
    *,
    execution_id: str,
    project_id: str | None = None,
    step: str = STEP_COLLECT,
) -> dict[str, Any] | None:
    """Write where the run is. ONE statement, called at a WINDOW boundary.

    Not once per landed row and not once per page: a two-year catch-up is 24
    windows, so it is 24 writes. The counters move with ``GREATEST`` so replaying
    a window is a no-op rather than a rewind or a double count -- the caller does
    not have to know whether it is a first run or a retry.

    ``days_total`` is only ever filled in when it is still NULL: the total is
    declared once, by the dispatch that knows the windows, and a later write
    never invents one.

    AND IT WRITES NOTHING ONTO A RUN THAT IS OVER (story 63.6). A run stopped at
    10:04 whose in-flight window lands at 10:07 would otherwise move ``days_done``
    and ``rows_written`` AFTER the person was told what had been kept: a figure
    that changes after the stop is a figure the screen already reported wrongly.
    The window itself still lands -- the raw zone is append-only and nothing is
    thrown away -- but the run's account of itself is frozen at the instant it was
    closed. Returns None in that case, which is the same "nothing was written"
    the project-scope mismatch already returns.

    IT ALSO CARRIES THE STEP TRANSITION, and only a REAL one (story 58.10). The
    ``FROM`` self-join reads the row as it was BEFORE this statement -- the
    step being left -- because a window boundary on the step the run is already
    on is not a transition and must write no span at all. In production this
    function is called once per window, always with ``Collect``, so the common
    case is exactly that: nothing is written, and the ``Collect`` span opened by
    ``open_collection_run`` keeps covering the collection.

    The caller owns the transaction (this does not commit), exactly like its
    neighbours in the worker's success path.
    """
    from core.execution_states import TERMINAL_STATES  # noqa: PLC0415

    if step not in PROGRESS_STEPS:
        raise ValueError(f"unknown progress step {step!r}; expected one of {PROGRESS_STEPS}")

    progress = derive_progress(read_run_jobs(conn, execution_id))

    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE app.datastream_executions AS e
            SET step = %s,
                day_in_progress = %s,
                days_done = GREATEST(COALESCE(e.days_done, 0), %s),
                rows_written = CASE
                    WHEN %s::bigint IS NULL THEN e.rows_written
                    ELSE GREATEST(COALESCE(e.rows_written, 0), %s::bigint)
                END,
                progress_updated_at = NOW(),
                updated_at = NOW()
            FROM app.datastream_executions AS prior
            WHERE prior.id = e.id
              AND e.id = %s AND (%s::text IS NULL OR e.project_id = %s)
              AND NOT (e.state = ANY(%s))
            RETURNING prior.step AS previous_step, {_PROGRESS_COLUMNS_QUALIFIED}
            """,
            (
                step,
                progress.day_in_progress,
                progress.days_done,
                progress.rows_written,
                progress.rows_written,
                execution_id,
                project_id,
                project_id,
                list(TERMINAL_STATES),
            ),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        progress_row = dict(zip(cols, row))

    # SAME TRANSACTION AS THE UPDATE ABOVE, and only when that UPDATE actually
    # moved a row: a run that is over, or that belongs to another Project,
    # writes no progress and therefore no span either.
    #
    # A TRANSITION, or nothing. `previous is None` means no path has ever
    # declared where this run was, so this call is not a move -- it is a first
    # sighting at the END of a window, and opening a span here would time the
    # bookkeeping rather than the work. The read model says the step was not
    # timed, which is true.
    previous = progress_row.pop("previous_step", None)
    if previous and previous != step:
        close_open_step_spans(conn, execution_id=execution_id, except_step=step)
        open_step_span(conn, execution_id=execution_id, step=step)
    return progress_row


def active_versions(conn, datastream_id: str, project_id: str) -> tuple[str, str] | None:
    """The datastream's active (plan, mapping) versions, or None when it has none.

    A datastream with no active version cannot host an execution: the composite
    FKs on ``app.datastream_executions`` refuse it. Returning None here is what
    keeps that refusal a decision rather than an exception in a dispatch loop.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT current_plan_version_id, current_mapping_version_id
            FROM app.datastreams
            WHERE id = %s AND project_id = %s AND archived_at IS NULL
            """,
            (datastream_id, project_id),
        )
        row = cur.fetchone()
    if not row or not row[0] or not row[1]:
        return None
    return str(row[0]), str(row[1])


def open_collection_run(
    conn,
    *,
    datastream_id: str,
    project_id: str,
    plan_version_id: str | None = None,
    mapping_version_id: str | None = None,
    windows: Sequence[Mapping[str, Any]],
    actor: str,
    idempotency_key: str,
    origin: str,
) -> dict[str, Any] | None:
    """Mint the execution a collection run belongs to, and declare its windows.

    ``origin`` is a key of ``core.run_origins`` (story 63.7) and is stamped onto
    the projection plan, never written as a free string: it is what the progress
    route reads to say WHY this collection is running, and an invented value
    would reach a screen as an unexplained treatment.

    Returns the execution record, or None when no run could be opened -- a
    concurrent non-terminal execution already holds the datastream, or the
    datastream has no usable plan/mapping version. None is not an error the
    caller has to handle: the pulls are still enqueued and still land, the run
    simply has no progress row, which is the honest state rather than a
    fabricated one.

    The caller owns the transaction.
    """
    from core.datastream_publication import (  # noqa: PLC0415
        STATE_CREATED,
        STATE_LOADING,
        PublicationError,
        advance_state,
        create_execution,
    )

    if not plan_version_id or not mapping_version_id:
        resolved = active_versions(conn, datastream_id, project_id)
        if resolved is None:
            return None
        plan_version_id, mapping_version_id = resolved

    # Story 63.7: the origin is STAMPED from the registry, never written as the
    # literal each of the three call sites used to pass. The stamp is what
    # refuses a key no build knows -- and what refuses an origin whose execution
    # nothing would ever advance, which is the state that holds
    # `uq_datastream_executions_active` and blocks every following night.
    projection_plan = stamp_origin(
        {
            "executable": True,
            "kind": COLLECTION_PLAN_KIND,
            # ONE place a reader looks for "which windows did this run declare".
            "windows": [
                {"date_from": str(w.get("date_from")), "date_to": str(w.get("date_to"))}
                for w in windows
            ],
        },
        origin,
    )

    try:
        execution = create_execution(
            datastream_id,
            project_id,
            plan_version_id,
            mapping_version_id,
            projection_plan,
            actor,
            idempotency_key,
            conn,
        )
        execution_id = str(execution["id"])
        if execution.get("state") == STATE_CREATED:
            advance_state(
                execution_id,
                STATE_CREATED,
                STATE_LOADING,
                actor,
                conn,
                project_id=project_id,
            )
    except PublicationError as exc:
        # ConcurrentExecutionActive is the expected one: something is already
        # running for this datastream. Never a raise -- a dispatch that refused
        # to enqueue because it could not open a progress row would trade the
        # collection itself for its instrumentation.
        logger.info(
            "execution_progress: collection_run_not_opened ds=%s code=%s",
            datastream_id,
            getattr(exc, "code", type(exc).__name__),
        )
        return None

    total = declared_days_total(windows)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE app.datastream_executions
            SET step = %s,
                days_total = %s,
                days_done = COALESCE(days_done, 0),
                started_at = COALESCE(started_at, NOW()),
                progress_updated_at = NOW(),
                updated_at = NOW()
            WHERE id = %s AND project_id = %s
            RETURNING {_PROGRESS_COLUMNS}
            """,
            (STEP_COLLECT, total, execution_id, project_id),
        )
        row = cur.fetchone()
        if row is not None:
            cols = [d[0] for d in cur.description]
            execution = {**execution, **dict(zip(cols, row))}

    # STORY 58.10: `Collect` IS ENTERED HERE, and the span opens at the run's own
    # `started_at` -- the very instant this statement just wrote. Opening it at
    # the window boundary instead (where the first cut of this story put it)
    # timed the bookkeeping that follows a collection rather than the collection:
    # `record_and_close` runs in ONE transaction, `NOW()` is
    # `transaction_timestamp()`, and a two-second pull measured `0.000000`.
    if execution.get("started_at") is not None:
        open_step_span(
            conn,
            execution_id=execution_id,
            step=STEP_COLLECT,
            at=execution.get("started_at"),
        )
    return execution


def close_collection_run_if_complete(
    conn,
    *,
    execution_id: str,
    project_id: str | None = None,
    actor: str = "scheduler",
) -> str | None:
    """Give the run a terminal state once no window of it can move again.

    Returns the state it was moved to, or None when it is still running (or was
    already terminal).

    THIS IS NOT OPTIONAL BOOKKEEPING. ``uq_datastream_executions_active`` allows
    one non-terminal execution per datastream, so a collection run left open
    would answer 409 to every later publish AND to the next night's dispatch,
    forever. ``collected`` (migration 218) is the terminal state of a run that
    pulled its windows and published nothing -- which is what a recurring
    retrieval is today.

    The last progress written is KEPT on a failed run: what was collected before
    the failure is a fact, and blanking it would make a partial run and a run
    that never started read as the same absence.
    """
    from core.datastream_publication import (  # noqa: PLC0415
        STATE_COLLECTED,
        STATE_FAILED,
        TERMINAL_STATES,
        PublicationError,
        advance_state,
    )

    with conn.cursor() as cur:
        cur.execute(
            "SELECT state, project_id FROM app.datastream_executions WHERE id = %s",
            (execution_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    current_state, row_project_id = row[0], row[1]
    if current_state in TERMINAL_STATES:
        return None
    if project_id is not None and row_project_id != project_id:
        return None

    progress = derive_progress(read_run_jobs(conn, execution_id))
    if not progress.all_terminal:
        return None

    target = STATE_FAILED if progress.windows_failed else STATE_COLLECTED
    try:
        advance_state(
            execution_id,
            current_state,
            target,
            actor,
            conn,
            project_id=row_project_id,
            error_code=ERROR_WINDOW_FAILED if target == STATE_FAILED else None,
            error_detail=(
                f"{progress.windows_failed} of {progress.windows_total} windows failed"
                if target == STATE_FAILED
                else None
            ),
        )
    except PublicationError as exc:
        logger.info(
            "execution_progress: close_refused execution=%s code=%s",
            execution_id,
            getattr(exc, "code", type(exc).__name__),
        )
        return None
    # The spans are NOT closed here: this function reaches its terminal state
    # through `datastream_publication.advance_state`, which closes them, so a
    # second call would be a second writer on the same rows.
    #
    # That statement was true of THIS path only when it was written, and story
    # 58.10 was rejected twice for claiming it more widely: `commit_publication`,
    # `reconcile_execution`, `_reconcile_fail_closed`,
    # `begin_managed_file_promotion` and `datastream_activation` each set a state
    # with their own UPDATE. AI-223 converted all of them, so it now holds for
    # every terminal transition, and
    # `tests/conformance/test_one_state_machine_for_a_run.py` keeps it holding.
    # The READ side still backs it: `_mark_step_spans` derives "still running"
    # from the run's state, so a span left open by a crash between the state
    # write and the close reads as unmeasured rather than as working.
    return target


def _stop_summary(conn, execution_id: str) -> dict[str, Any]:
    """What the run kept, and what is still in flight, at the instant of the stop.

    Everything here is DERIVED from rows, so asking twice answers the same thing:
    the refused windows are counted by their state, never by how many rows one
    ``UPDATE`` happened to touch.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT count(*) AS windows_refused
            FROM app.pull_jobs
            WHERE execution_id = %s AND state = '{_pull_job_states.CANCELLED}'
            """,
            (execution_id,),
        )
        row = cur.fetchone()
        refused = int(row[0]) if row else 0

        cur.execute(
            f"""
            SELECT date_from, date_to
            FROM app.pull_jobs
            WHERE execution_id = %s AND state = '{_pull_job_states.RUNNING}'
            ORDER BY date_from ASC, id ASC
            LIMIT 1
            """,
            (execution_id,),
        )
        flight = cur.fetchone()

    return {
        "windows_refused": refused,
        "window_in_flight": (
            None if flight is None else {"date_from": flight[0], "date_to": flight[1]}
        ),
    }


def stop_collection_run(
    conn,
    *,
    execution_id: str,
    project_id: str,
    actor: str,
) -> dict[str, Any]:
    """Stop a collection run: close it, and refuse the windows it never started.

    WHAT IT CAN HONESTLY REACH. `queue._execute_job` calls the provider
    synchronously and consults nothing between two pages; the only mechanism that
    takes a `running` job back is the stale sweep, at 5400 s by default. So a
    provider call already in flight is NOT interrupted -- it finishes, its rows
    land, and the answer names that window so the screen can say so. Promising an
    interruption would manufacture a false expectation in somebody watching a
    counter refuse to stop.

    THE ORDER IS THE SAFETY. The run is moved to its terminal state FIRST, then
    its queued windows are refused. A window landing in between calls
    `record_and_close`, which reads the run: with the run already terminal the
    progress write is refused and the closure is a no-op, so the numbers a person
    was just shown cannot move afterwards.

    WHAT IT REFUSES, and each with its own code, because a console cannot offer
    three different repairs behind one message:

      * `not_found`            -- no such run in this project. The envelope of an
        absent run, never a refusal that confirms it exists somewhere else.
      * `run_not_running`      -- it already ended. Asked again on a run this
        function itself stopped, it answers the same body instead.
      * `not_a_collection_run` -- its plan is not a retrieval.
        `managed_file_dispatch` reads `{"failed", "cancelled"}` off an execution
        and marks the dispatch FAILED, so stopping a publication candidate here
        would report a dispatch failure nobody caused.
      * `forbidden`            -- the caller's role. Raised at the door, not here:
        whoever may spend the quota may stop spending it.

    The caller owns the transaction. Returns :data:`STOP_FIELDS`.
    """
    from core.datastream_publication import (  # noqa: PLC0415
        STATE_CANCELLED,
        PublicationError,
        advance_state,
    )
    from core.execution_states import TERMINAL_STATES  # noqa: PLC0415
    from core.queue import cancel_queued_jobs  # noqa: PLC0415

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT state,
                   projection_plan_ref->>'kind' AS plan_kind,
                   days_done,
                   rows_written,
                   state_changed_at AS stopped_at,
                   datastream_id
            FROM app.datastream_executions
            WHERE id = %s AND project_id = %s
            """,
            (execution_id, project_id),
        )
        row = cur.fetchone()
        columns = [desc[0] for desc in cur.description] if cur.description else []
    if row is None:
        raise StopRefused(STOP_NOT_FOUND)
    record = dict(zip(columns, row))

    if record.get("plan_kind") != COLLECTION_PLAN_KIND:
        raise StopRefused(STOP_NOT_A_COLLECTION_RUN)

    state = record.get("state")
    already_stopped = state == STATE_CANCELLED
    if not already_stopped and state in TERMINAL_STATES:
        raise StopRefused(STOP_NOT_RUNNING)

    if not already_stopped:
        try:
            advance_state(
                execution_id,
                state,
                STATE_CANCELLED,
                actor,
                conn,
                project_id=project_id,
                error_code=ERROR_RUN_STOPPED,
                error_detail=f"stopped by {actor}",
            )
        except PublicationError as exc:
            # `publishing` is not cancellable: once the atomic commit is under
            # way there is no safe stop, it resolves to published or failed.
            logger.info(
                "execution_progress: stop_refused execution=%s code=%s",
                execution_id,
                getattr(exc, "code", type(exc).__name__),
            )
            raise StopRefused(STOP_NOT_RUNNING) from exc
        cancel_queued_jobs(
            conn, execution_id=execution_id, reason=STOP_WINDOW_DETAIL
        )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT state, state_changed_at FROM app.datastream_executions "
                "WHERE id = %s",
                (execution_id,),
            )
            moved = cur.fetchone()
        if moved is not None:
            record["state"], record["stopped_at"] = moved[0], moved[1]
        else:  # pragma: no cover - the row was read one statement earlier
            record["state"] = STATE_CANCELLED

    days_done = record.get("days_done")
    return {
        "execution_id": execution_id,
        "state": record.get("state"),
        # `days_done` is written as 0 by `open_collection_run` before any window
        # lands, so a bare 0 here would claim a measurement the run never made.
        # A finished window covers at least one day, so 0 means exactly "none
        # finished" -- the same rule 63.1 applies to `rows_written`.
        "days_kept": days_done if days_done else None,
        "rows_kept": record.get("rows_written"),
        "stopped_at": record.get("stopped_at"),
        **_stop_summary(conn, execution_id),
    }


def record_and_close(
    conn,
    *,
    execution_id: str,
    project_id: str | None = None,
    actor: str = "scheduler",
    step: str = STEP_COLLECT,
) -> dict[str, Any]:
    """One window boundary: write the progress, then close the run if it is over.

    The single entry point the queue worker calls, so the ORDER is fixed in one
    place -- progress first, terminal state second. The reverse order would let a
    reader see a terminal run whose last window is not counted yet.
    """
    progress = record_window_progress(
        conn, execution_id=execution_id, project_id=project_id, step=step
    )
    closed = close_collection_run_if_complete(
        conn, execution_id=execution_id, project_id=project_id, actor=actor
    )
    return {"progress": progress, "closed_state": closed}
