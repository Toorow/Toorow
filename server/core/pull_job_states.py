"""The ONE place that knows what states a pull job can be in -- story 63.6.

WHY THIS MODULE EXISTS, AND WHY IT IS ONE STOREY BELOW `core.execution_states`.
An EXECUTION is a run; a PULL JOB is one window of that run. Story 63.1 gave the
first its registry and left the second with six copies and no source, measured
2026-08-06:

  * `execution_progress.TERMINAL_JOB_STATES`   -- 3 of the 6 names
  * `scheduler._reschedule_failed_pulls`       -- 3 of the 6, twice over
  * `admin_api._VALID_JOB_STATES`              -- 5 of the 6; `superseded` was
       missing, so the only route that lists jobs answered 400 to a state the
       database holds
  * `extract_ledger._day_to_ledger_entry`      -- 4 of the 6, everything else
       falling through an `else` into `never_fetched`
  * `queue.py`                                 -- 5 constants, no `superseded`
  * `tests/core/test_queue_records_execution_progress.py` -- 3 of the 6

AND THE SIXTH COPY IS WHAT MADE STORY 63.1's GUARD BLIND. That test refuses any
statement putting a pull job into a terminal state outside the two functions that
close the run it belongs to -- by resolving the literal of the `SET` clause
against its own set of three names. `SET state = 'superseded'` was already
invisible to it, and `SET state = 'cancelled'` would have been too: a run left
non-terminal holds `uq_datastream_executions_active` and answers 409 to every
later publish AND to every following night's dispatch, forever. The guard was
green and could not see the door.

WHAT A STATE HAS TO ANSWER. Not "is it terminal" alone -- each reader asks a
different question, and the wrong grouping is how `cancelled` would have armed
story 57.8's catch-up and restarted, within the hour, the run a person had just
stopped:

  * `terminal`  -- can this window move again? Also the predicate of
    `uq_pull_jobs_active`, by complement.
  * `succeeded` -- did it land its rows? Only `done`.
  * `failed`    -- did an attempt END BADLY? `failed` and `dead_letter`, and
    nothing else: this is exactly the set `_reschedule_failed_pulls` arms one
    extra attempt on.
  * `ledger_status` -- what the day-grain extract ledger reports for a day this
    window covers.

`ATTEMPTED_JOB_STATES` is DERIVED, from `ran` and never from a hand-kept list: a
window replaced by the dedup index (`superseded`) and a window refused before it
ever started (`cancelled`) were never run, so no reader may read them as an
attempt that happened.

  * `ran` -- did a worker actually call the provider for this window? Until
    AI-307 this was exactly `succeeded or failed`, so the set was derived from
    those two. `prevented` broke that equality: the worker DID call, and the
    provider refused. It has to be in `ATTEMPTED_JOB_STATES` or
    `_reschedule_failed_pulls` would skip over the prevented window to an older
    `failed` one and re-arm a Datastream whose latest word was "not allowed" --
    an hourly retry against a grant only a human at the provider can give.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# The day-grain vocabulary of the extract ledger (story 8.3).
# ---------------------------------------------------------------------------

LEDGER_RUNNING = "running"
LEDGER_FAILED = "failed"
LEDGER_NEVER_FETCHED = "never_fetched"
#: `done` alone: the day's status is then decided by the verification verdict
#: (`ok` / `partial` / `empty`), which is a measurement of the rows, not of the
#: job. A sentinel rather than a fourth status, so the ledger keeps ONE branch
#: that reads a verdict instead of a second list of state names.
LEDGER_FROM_VERDICT = "from_verdict"


@dataclass(frozen=True)
class PullJobState:
    """One state of one window, and every question a reader may ask about it."""

    name: str
    #: Can this window still move? Its complement is `uq_pull_jobs_active`.
    terminal: bool
    #: Did it land its rows? Drives `days_done` / `rows_written` (story 63.1).
    succeeded: bool
    #: Did an ATTEMPT end badly? The set story 57.8's catch-up is armed on.
    failed: bool
    #: What the extract ledger reports for a day this window covers.
    ledger_status: str
    #: Did a worker actually call the provider for this window? DECLARED since
    #: AI-307, because `succeeded or failed` stopped being the same question the
    #: day a window could be run and refused (`prevented`).
    ran: bool = False


def _s(name, **kwargs) -> PullJobState:
    return PullJobState(name=name, **kwargs)


#: Every value `app.pull_jobs.state` may hold. KEEP THIS AND THE MIGRATIONS IN
#: STEP -- `tests/conformance/test_pull_job_state_registry.py` compares this
#: table against the CHECK constraint by reading both, so a divergence is a red
#: test rather than a production surprise.
PULL_JOB_STATES: tuple[PullJobState, ...] = (
    _s("queued", terminal=False, succeeded=False, failed=False,
       ledger_status=LEDGER_RUNNING),
    _s("running", terminal=False, succeeded=False, failed=False,
       ledger_status=LEDGER_RUNNING),
    _s("done", terminal=True, succeeded=True, failed=False,
       ledger_status=LEDGER_FROM_VERDICT, ran=True),
    _s("failed", terminal=True, succeeded=False, failed=True,
       ledger_status=LEDGER_FAILED, ran=True),
    _s("dead_letter", terminal=True, succeeded=False, failed=True,
       ledger_status=LEDGER_FAILED, ran=True),
    # Migration 022: a row REPLACED by the dedup index because a newer active job
    # covered the same window. It was never attempted, so it is not `failed` --
    # arming a retry on it would re-run a window another job already owns.
    _s("superseded", terminal=True, succeeded=False, failed=False,
       ledger_status=LEDGER_NEVER_FETCHED),
    # Story 63.6, migration 220: a window a PERSON refused before it started.
    #
    # NOT `failed`, and the measurement is what decides it: `_reschedule_failed_
    # pulls` (scheduler.py, story 57.8) reads `failed` / `dead_letter` and writes
    # `next_run_at = NOW() + interval '1 hour'`. A stop that restarts by itself
    # within the hour is the worst possible outcome of the gesture.
    #
    # NOT `superseded` either: that name already means "another job took this
    # window over", and confounding an automatic replacement with a deliberate
    # stop would make both unreadable.
    _s("cancelled", terminal=True, succeeded=False, failed=False,
       ledger_status=LEDGER_NEVER_FETCHED),
    # AI-307, migration 297: a window the SOURCE did not allow to run -- a quota
    # the provider has not granted, an allowlist the project is not on, a scope
    # still in an approval queue. The connector says so in its envelope
    # (`core.pull_envelope`), and `queue._execute_job` is the one reader.
    #
    # NOT `done`: that was the defect. A pull refused upstream was recorded
    # `done / 0 row`, indistinguishable from a pull that ran and found nothing --
    # and `0` then travelled to the verification subscriber, which filed `empty`,
    # which raises the STICKY `populate_failed` that refuses every Datastream
    # behind the authorization. One un-granted quota closed a whole credential.
    #
    # NOT `failed` / `dead_letter`: `_reschedule_failed_pulls` reads exactly
    # those two and re-arms within the hour. A grant a human has to approve will
    # not have landed an hour later; that is a crash loop, not a retry.
    #
    # NOT `cancelled`: that name means a PERSON refused the window. Nobody chose
    # here. NOT `superseded`: that means another job took the window over.
    #
    # `never_fetched` to the ledger -- the day-grain answer is that the day was
    # not fetched -- with the state travelling beside it on `job_state`, which is
    # arbitrage 7 of story 58.1 unchanged: the screen needs both words.
    _s("prevented", terminal=True, succeeded=False, failed=False,
       ledger_status=LEDGER_NEVER_FETCHED, ran=True),
)

BY_NAME: dict[str, PullJobState] = {state.name: state for state in PULL_JOB_STATES}

#: Every value the column may hold, in machine order.
JOB_STATES: tuple[str, ...] = tuple(s.name for s in PULL_JOB_STATES)
#: Exactly the predicate of `uq_pull_jobs_active`.
ACTIVE_JOB_STATES: tuple[str, ...] = tuple(
    s.name for s in PULL_JOB_STATES if not s.terminal
)
#: A window that will not move again -- what tells story 63.1 the run is over.
TERMINAL_JOB_STATES: frozenset[str] = frozenset(
    s.name for s in PULL_JOB_STATES if s.terminal
)
#: An attempt that ended badly. The catch-up sweep's set, and nothing wider.
FAILED_JOB_STATES: frozenset[str] = frozenset(
    s.name for s in PULL_JOB_STATES if s.failed
)
#: A window a worker actually ran, whatever the outcome. DERIVED from `ran`.
#:
#: It read `s.succeeded or s.failed` until AI-307, which was the same set only
#: while every window that ran either landed rows or ended badly. `prevented` is
#: neither and still ran, and reading it out of this set is not bookkeeping: it
#: is what would let `_reschedule_failed_pulls` walk past the prevented window to
#: an older `failed` one and re-arm the stream every hour against a grant only a
#: human at the provider can give.
ATTEMPTED_JOB_STATES: tuple[str, ...] = tuple(
    s.name for s in PULL_JOB_STATES if s.ran
)

# The individual names, so a single-value SQL predicate or a `state == ` reads a
# constant instead of a literal. A LIST of them is what the registry replaces;
# one name at a time is what a statement legitimately needs.
QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
DEAD_LETTER = "dead_letter"
SUPERSEDED = "superseded"
CANCELLED = "cancelled"
PREVENTED = "prevented"


_SAFE_NAME = re.compile(r"^[a-z_]+$")


def sql_literals(names) -> str:
    """A SQL value list, GENERATED from this frozen registry.

    Composed into a statement rather than bound, for the reason
    `datastream_progress_api` measured on 2026-08-05: a bound array loses a
    partial index as soon as the statement is planned generically, which is what
    a statement executed in a loop gets. Every name is re-checked against
    ``[a-z_]+`` here; none of them ever comes from a request.
    """
    ordered = sorted(names) if isinstance(names, (set, frozenset)) else list(names)
    for name in ordered:
        if not _SAFE_NAME.match(name):
            raise ValueError(f"unusable pull-job state name {name!r}")
    return ", ".join(f"'{name}'" for name in ordered)


def is_terminal(state: str | None) -> bool:
    """Can this window still move? An unknown name is NOT treated as finished.

    Fail-closed, exactly as `core.execution_states.is_terminal`: an unrecognised
    state is a window this build does not understand, and calling it finished
    would close a run that is still collecting.
    """
    entry = BY_NAME.get(str(state or "").lower())
    return bool(entry and entry.terminal)


def has_failed(state: str | None) -> bool:
    """Did this window's attempt end badly? Unknown names never arm a retry."""
    entry = BY_NAME.get(str(state or "").lower())
    return bool(entry and entry.failed)


def ledger_status(state: str | None) -> str:
    """What the extract ledger reports for a day this window covers.

    An unknown state answers `never_fetched` -- the conservative answer the
    ledger already gave, said on purpose here instead of falling out of an
    `else` that no longer knew which states it was catching.
    """
    entry = BY_NAME.get(str(state or "").lower())
    return entry.ledger_status if entry else LEDGER_NEVER_FETCHED
