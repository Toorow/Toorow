"""One place knows the execution states, and every layer reads it -- story 63.1.

THE DEFECT THIS FILE CLOSES. Nothing enumerated the states of an execution. Each
reader re-wrote the list as a literal, in Python, in SQL and in TSX -- six copies
that nothing kept in step. Adding `collected` (migration 218) hit all six at
once: the operations axis answered `Unknown` for every scheduled Datastream, the
Runs tab painted a finished run amber, the 30-day success rate drifted toward 0%,
and an in-flight nightly pull pushed the Workbench's primary action to "Review
candidate" for the length of the run.

Each test below RED-LIGHTS the addition of a state that some layer does not know
about. None of them names `collected`: a guard that names the state of the day is
a guard that will be silent for the next one.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest
from core import execution_states

ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS = ROOT / "infra" / "nango" / "migrations"
CONSOLE_MIRROR = (
    ROOT / "ui" / "admin" / "src" / "datastreams" / "workbench" / "executionStates.ts"
)


# ---------------------------------------------------------------------------
# The registry is total: every state answers every question.
# ---------------------------------------------------------------------------


def test_every_state_is_classified_on_every_axis() -> None:
    assert execution_states.EXECUTION_STATES, "the registry is empty"
    axes = {
        execution_states.AXIS_UNKNOWN,
        execution_states.AXIS_HEALTHY,
        execution_states.AXIS_DEGRADED,
        execution_states.AXIS_BLOCKED,
    }
    phases = {
        execution_states.PHASE_TODO,
        execution_states.PHASE_RUNNING,
        execution_states.PHASE_DONE,
        execution_states.PHASE_FAILED,
    }
    for state in execution_states.EXECUTION_STATES:
        assert state.axis in axes, state.name
        assert state.phase in phases, state.name
        # A state cannot both hold the publication lock and be finished.
        assert not (state.active and state.terminal), state.name
        # A stored state is either active or terminal -- never neither.
        if state.stored:
            assert state.active or state.terminal, state.name
        # Only a finished run can be called a success.
        if state.success:
            assert state.terminal, state.name


def test_the_derived_sets_partition_the_stored_states() -> None:
    stored = set(execution_states.STORED_STATES)
    assert set(execution_states.ACTIVE_STATES) | set(execution_states.TERMINAL_STATES) == stored
    assert not set(execution_states.ACTIVE_STATES) & set(execution_states.TERMINAL_STATES)
    assert set(execution_states.SUCCESS_STATES) <= set(execution_states.TERMINAL_STATES)


def test_only_stored_states_may_reach_sql() -> None:
    """A non-stored name in a WHERE clause can never match a row.

    The candidate LATERAL used to list `outcome_unknown`, which `state` cannot
    hold: a clause that reads like a rule and filters nothing.
    """
    for group in (
        execution_states.ACTIVE_STATES,
        execution_states.TERMINAL_STATES,
        execution_states.SUCCESS_STATES,
        execution_states.CANDIDATE_STATES,
    ):
        assert set(group) <= set(execution_states.STORED_STATES), group


# ---------------------------------------------------------------------------
# Layer 1 -- the database.
# ---------------------------------------------------------------------------


def _latest_state_check() -> str:
    """The last CHECK constraint on `state` any migration declares."""
    latest = ""
    for path in sorted(MIGRATIONS.glob("*.sql")):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(
            r"CHECK\s*\(\s*state\s+IN\s*\((?P<body>[^)]*)\)", text, re.IGNORECASE
        ):
            if "datastream_executions" in text:
                latest = match.group("body")
    return latest


def test_the_database_accepts_exactly_the_stored_states() -> None:
    body = _latest_state_check()
    assert body, "no CHECK on datastream_executions.state found in the migrations"
    declared = set(re.findall(r"'([a-z_]+)'", body))
    assert declared == set(execution_states.STORED_STATES), (
        "the migration CHECK and core.execution_states disagree: "
        f"only in SQL={sorted(declared - set(execution_states.STORED_STATES))}, "
        f"only in Python={sorted(set(execution_states.STORED_STATES) - declared)}"
    )


def test_the_active_index_predicate_is_exactly_the_active_states() -> None:
    """`uq_datastream_executions_active` is the hard structural lock.

    A terminal state that leaked into this predicate would block a Datastream
    forever; an active state missing from it would let two runs publish at once.
    """
    sql = (MIGRATIONS / "042_datastream_candidate_registry.sql").read_text(encoding="utf-8")
    block = sql.split("uq_datastream_executions_active", 1)[1].split(";", 1)[0]
    declared = set(re.findall(r"'([a-z_]+)'", block))
    assert declared == set(execution_states.ACTIVE_STATES)


# ---------------------------------------------------------------------------
# Layer 2 -- the Python readers.
# ---------------------------------------------------------------------------


_PYTHON_READERS = (
    "server/core/datastream_workbench.py",
    "server/core/datastream_publication.py",
    "server/core/datastream_first_candidate.py",
    "server/core/admin_api.py",
    # Story 58.4, added in the same change as the module: it is the first reader
    # of `uq_datastream_executions_active` written to SPEAK -- it says which run
    # holds a Datastream when a re-collection gets no run of its own. A literal
    # list of active states here would answer "nothing is running" the day a
    # state is added, about a Datastream the index is refusing a second run into.
    "server/core/datastream_active_run.py",
)


def test_no_python_reader_keeps_its_own_copy_of_the_state_list() -> None:
    """A run of three or more state names in one literal is a second truth.

    Read from the AST, so a name inside a comment or a docstring -- where naming
    the states is exactly right -- never trips it.
    """
    offenders: list[str] = []
    known = set(execution_states.BY_NAME)
    for relative in _PYTHON_READERS:
        path = ROOT / relative
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
                names = [
                    e.value for e in node.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)
                ]
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                # A SQL string carrying its own IN (...) list.
                names = re.findall(r"'([a-z_]+)'", node.value)
            hits = [n for n in names if n in known]
            if len(hits) >= 3:
                offenders.append(f"{relative}: {sorted(set(hits))}")
    assert not offenders, (
        "these readers re-declare the execution states instead of importing "
        f"core.execution_states: {offenders}"
    )


def test_the_operations_axis_never_falls_through_for_a_known_state() -> None:
    """Every state maps to a ratified axis value, including the next one added."""
    for state in execution_states.EXECUTION_STATES:
        assert execution_states.operations_axis(state.name) == state.axis
    # An absent or unknown state is Unknown -- honest, not a fabricated Healthy.
    assert execution_states.operations_axis(None) == execution_states.AXIS_UNKNOWN
    assert execution_states.operations_axis("a_state_no_build_knows") == (
        execution_states.AXIS_UNKNOWN
    )


def test_an_unknown_state_is_never_treated_as_finished() -> None:
    """Fail-closed: closing a run this build does not understand loses work."""
    assert execution_states.is_terminal("a_state_no_build_knows") is False
    assert execution_states.is_terminal(None) is False
    assert execution_states.is_active("a_state_no_build_knows") is False


# ---------------------------------------------------------------------------
# Layer 3 -- the console.
# ---------------------------------------------------------------------------


def _console_registry() -> list[dict[str, object]]:
    """Parse the TS mirror's table into plain data, without running a bundler."""
    source = CONSOLE_MIRROR.read_text(encoding="utf-8")
    body = source.split("EXECUTION_STATES: readonly ExecutionStateEntry[] = [", 1)[1]
    body = body.split("\n];", 1)[0]
    rows: list[dict[str, object]] = []
    for line in body.splitlines():
        line = line.strip().rstrip(",")
        if not line.startswith("{"):
            continue
        # Object literal -> JSON: quote the keys, the values are already valid.
        as_json = re.sub(r"(\w+):", r'"\1":', line)
        rows.append(json.loads(as_json))
    return rows


def test_the_console_mirror_matches_the_registry_entry_for_entry() -> None:
    """The screen may not hold a second opinion about what a state means."""
    assert _console_registry() == execution_states.as_registry_rows(), (
        "ui/admin/src/datastreams/workbench/executionStates.ts has drifted from "
        "server/core/execution_states.py -- update both, in the same change"
    )


def test_the_runs_page_reads_the_mirror_instead_of_listing_states() -> None:
    page = (
        ROOT / "ui" / "admin" / "src" / "datastreams" / "workbench" / "pages"
        / "WorkbenchRunsPage.tsx"
    ).read_text(encoding="utf-8")
    assert "EXECUTION_STATES" in page
    # What is forbidden is classifying a STATE. `phase === "failed"` compares the
    # timeline vocabulary the page owns, and `done`/`failed` belong to it; the
    # left-hand identifier is what tells the two apart, so the guard reads it
    # rather than the string -- which `failed` shares with a state name.
    quoted = {
        value
        for identifier, value in re.findall(r'(\w+)\s*===\s*"([a-z_]+)"', page)
        if "state" in identifier.lower() and value in execution_states.BY_NAME
    }
    # `running` is a phase-evidence value (`phase_state`), not an execution
    # state, and resolving it here is the page's own job.
    assert quoted <= {"running"}, (
        f"the Runs page still classifies execution states itself: {sorted(quoted)}"
    )


@pytest.mark.parametrize("state", [s.name for s in execution_states.EXECUTION_STATES])
def test_every_state_has_a_phase_in_both_languages(state: str) -> None:
    console = {row["name"]: row["phase"] for row in _console_registry()}
    assert console[state] == execution_states.phase_of(state)
