"""The ONE place that knows what states an execution can be in -- story 63.1.

WHY THIS MODULE EXISTS. Before it, no single place enumerated them. Every reader
re-wrote the list as a literal, in three languages:

  * Python  -- `datastream_workbench._operations_axis`, `datastream_first_candidate`
  * SQL     -- the candidate LATERAL and the 30-day success rate in
               `datastream_workbench`, the candidate probe in `admin_api`,
               the CHECK and the partial unique index in the migrations
  * TSX     -- `WorkbenchRunsPage.phaseState`

Adding `collected` (migration 218) hit SIX of those copies at once: the
operations axis answered `Unknown` for every scheduled Datastream, the Runs tab
painted a finished run amber, the 30-day success rate drifted toward 0%, and an
in-flight nightly pull pushed the Workbench's primary action to "Review
candidate" for the whole run. None of those readers was wrong on its own -- the
absence of a source of truth was.

So: declare a state ONCE, here, with everything a reader needs to answer without
guessing, and let `tests/conformance/test_execution_state_registry.py` refuse a
state that any layer does not know about.

TWO FAMILIES, AND THE DIFFERENCE MATTERS. `stored=True` states are the values
`app.datastream_executions.state` may hold -- they and only they may appear in
SQL. The rest are RECOVERY VERDICTS (`outcome_unknown`) and reported run
outcomes (`succeeded`, `partial`, `drifted`, `blocked`) that reach a Python
reader through other paths. The old candidate LATERAL listed `outcome_unknown`
in SQL, where it could never match a row; generating the SQL from `stored` drops
that clause rather than carrying it forward.
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# The four operations-axis values the ratified surface allows.
# ---------------------------------------------------------------------------
#
# docs/product-architecture/datastream-workbench-and-wizard.md, "State model":
# `Unknown`, `Healthy`, `Degraded`, `Stale`, `Blocked`. `Stale` is derived from
# freshness evidence, never from a state, so no state maps to it here.

AXIS_UNKNOWN = "Unknown"
AXIS_HEALTHY = "Healthy"
AXIS_DEGRADED = "Degraded"
AXIS_BLOCKED = "Blocked"

# The four values the console's phase timeline can paint.
PHASE_TODO = "todo"
PHASE_RUNNING = "running"
PHASE_DONE = "done"
PHASE_FAILED = "failed"


@dataclass(frozen=True)
class ExecutionState:
    """One state, and every question a reader is allowed to ask about it."""

    name: str
    #: May `app.datastream_executions.state` hold this value? Only these reach SQL.
    stored: bool
    #: Does it block a concurrent publication (the `uq_datastream_executions_active`
    #: predicate)? Exactly the non-terminal stored states.
    active: bool
    #: Can it still move?
    terminal: bool
    #: Was work finished successfully? Drives the 30-day success rate.
    success: bool
    #: Is it a candidate a person is expected to review?
    candidate: bool
    #: What the Operations axis says when this is the latest TERMINAL run.
    axis: str
    #: What the console's timeline paints.
    phase: str


def _s(name, **kwargs) -> ExecutionState:
    return ExecutionState(name=name, **kwargs)


#: The nine stored states plus the five reported outcomes, in machine order.
#: KEEP THE MIGRATION AND THIS TABLE IN STEP -- the conformance test compares
#: `STORED_STATES` against the CHECK constraint in the migrations, by reading
#: both, so a divergence is a red test rather than a production surprise.
EXECUTION_STATES: tuple[ExecutionState, ...] = (
    _s("created", stored=True, active=True, terminal=False, success=False,
       candidate=True, axis=AXIS_UNKNOWN, phase=PHASE_TODO),
    _s("loading", stored=True, active=True, terminal=False, success=False,
       candidate=True, axis=AXIS_UNKNOWN, phase=PHASE_RUNNING),
    _s("validating", stored=True, active=True, terminal=False, success=False,
       candidate=True, axis=AXIS_UNKNOWN, phase=PHASE_RUNNING),
    _s("ready", stored=True, active=True, terminal=False, success=False,
       candidate=True, axis=AXIS_HEALTHY, phase=PHASE_RUNNING),
    _s("publishing", stored=True, active=True, terminal=False, success=False,
       candidate=True, axis=AXIS_UNKNOWN, phase=PHASE_RUNNING),
    _s("published", stored=True, active=False, terminal=True, success=True,
       candidate=False, axis=AXIS_HEALTHY, phase=PHASE_DONE),
    # Story 63.1: a run that pulled its windows and published nothing -- what a
    # recurring retrieval is today. Terminal, successful, never a candidate to
    # review, and it blocks nothing.
    _s("collected", stored=True, active=False, terminal=True, success=True,
       candidate=False, axis=AXIS_HEALTHY, phase=PHASE_DONE),
    _s("failed", stored=True, active=False, terminal=True, success=False,
       candidate=False, axis=AXIS_BLOCKED, phase=PHASE_FAILED),
    _s("cancelled", stored=True, active=False, terminal=True, success=False,
       candidate=False, axis=AXIS_DEGRADED, phase=PHASE_FAILED),
    # Reported outcomes. Never stored in `state`; they reach a Python reader
    # through the recovery verdict and the run-outcome vocabulary of the surface
    # document, and the console has to paint them.
    _s("succeeded", stored=False, active=False, terminal=True, success=True,
       candidate=False, axis=AXIS_HEALTHY, phase=PHASE_DONE),
    _s("outcome_unknown", stored=False, active=False, terminal=True, success=False,
       candidate=False, axis=AXIS_BLOCKED, phase=PHASE_FAILED),
    _s("blocked", stored=False, active=False, terminal=True, success=False,
       candidate=False, axis=AXIS_BLOCKED, phase=PHASE_FAILED),
    _s("partial", stored=False, active=False, terminal=True, success=False,
       candidate=False, axis=AXIS_DEGRADED, phase=PHASE_FAILED),
    _s("drifted", stored=False, active=False, terminal=True, success=False,
       candidate=False, axis=AXIS_DEGRADED, phase=PHASE_FAILED),
)

BY_NAME: dict[str, ExecutionState] = {state.name: state for state in EXECUTION_STATES}

#: Every value `app.datastream_executions.state` may hold. The ONLY tuple that
#: may reach SQL -- a non-stored name in a WHERE clause can never match a row.
STORED_STATES: tuple[str, ...] = tuple(s.name for s in EXECUTION_STATES if s.stored)
ACTIVE_STATES: tuple[str, ...] = tuple(s.name for s in EXECUTION_STATES if s.active)
TERMINAL_STATES: tuple[str, ...] = tuple(
    s.name for s in EXECUTION_STATES if s.stored and s.terminal
)
SUCCESS_STATES: tuple[str, ...] = tuple(
    s.name for s in EXECUTION_STATES if s.stored and s.success
)
#: The states a run must be in to be offered as a candidate awaiting review.
CANDIDATE_STATES: tuple[str, ...] = tuple(
    s.name for s in EXECUTION_STATES if s.stored and s.candidate
)
#: A run that ENDED BADLY, and is therefore recoverable. Includes the reported
#: outcomes, because a recovery drawer reads whatever the run reported -- not
#: only what the column can hold.
ADVERSE_STATES: tuple[str, ...] = tuple(
    s.name for s in EXECUTION_STATES if s.terminal and not s.success
)


def is_terminal(state: str | None) -> bool:
    """Can this state still move? An unknown name is treated as NOT terminal.

    Fail-closed: an unrecognised state is something in flight that this build
    does not understand, and treating it as finished would let a caller close a
    run that is still working.
    """
    entry = BY_NAME.get(str(state or "").lower())
    return bool(entry and entry.terminal)


def is_active(state: str | None) -> bool:
    """Does this state hold the one-non-terminal-execution-per-datastream lock?"""
    entry = BY_NAME.get(str(state or "").lower())
    return bool(entry and entry.active)


def operations_axis(state: str | None) -> str:
    """The Operations axis this state implies, or `Unknown` for an absent one."""
    entry = BY_NAME.get(str(state or "").lower())
    return entry.axis if entry else AXIS_UNKNOWN


def phase_of(state: str | None) -> str:
    """What the console's timeline paints for this state."""
    entry = BY_NAME.get(str(state or "").lower())
    return entry.phase if entry else PHASE_TODO


def as_registry_rows() -> list[dict[str, object]]:
    """The whole table as plain data -- what the console mirror is compared to."""
    return [
        {
            "name": s.name,
            "stored": s.stored,
            "active": s.active,
            "terminal": s.terminal,
            "success": s.success,
            "candidate": s.candidate,
            "axis": s.axis,
            "phase": s.phase,
        }
        for s in EXECUTION_STATES
    ]
