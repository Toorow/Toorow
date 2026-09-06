"""One place knows the states of a pull job, and every layer reads it -- 63.6.

THE DEFECT THIS FILE CLOSES, AND WHY IT IS ONE STOREY BELOW STORY 63.1's. The
states of an EXECUTION got their registry in 63.1. The states of a pull JOB never
did: six copies, no source, measured 2026-08-06 --

  * `execution_progress.TERMINAL_JOB_STATES`      3 of 6 names
  * `test_queue_records_execution_progress.py`    3 of 6 names
  * `scheduler._reschedule_failed_pulls`          3 of 6 names
  * `admin_api._VALID_JOB_STATES`                 5 of 6 names (`superseded`
                                                  absent -> a state the database
                                                  holds is answered 400)
  * `extract_ledger._day_to_ledger_entry`         4 of 6 names
  * `queue.py`                                    5 constants, no `superseded`

AND THAT ABSENCE MADE STORY 63.1's GUARD BLIND. `_writes_a_terminal_state`
resolves the literal of a `SET` clause against ITS OWN copy of three names, so
`UPDATE app.pull_jobs SET state = 'cancelled'` was neither detected nor refused
by the guard whose entire purpose is to refuse a terminal job exit that does not
close its run. A run left open holds `uq_datastream_executions_active` and
answers 409 to every later publish and to every following night's dispatch,
forever -- the exact catastrophe 63.1 closed, reachable through a door its guard
could not see.

So: declare a job state ONCE, in `server/core/pull_job_states.py`, and let each
test below RED-LIGHT the layer that does not read it. None of them names
`cancelled`: a guard that names the state of the day is a guard that will be
silent for the next one.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from core import (
    jobs_api,  # AD-43 : le handler vit chez son sujet
    pull_job_states,
)

ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS = ROOT / "infra" / "nango" / "migrations"


# ---------------------------------------------------------------------------
# The registry is total: every state answers every question.
# ---------------------------------------------------------------------------


def test_every_state_is_classified_on_every_axis() -> None:
    assert pull_job_states.PULL_JOB_STATES, "the registry is empty"
    ledger_values = {
        pull_job_states.LEDGER_RUNNING,
        pull_job_states.LEDGER_FAILED,
        pull_job_states.LEDGER_NEVER_FETCHED,
        pull_job_states.LEDGER_FROM_VERDICT,
    }
    for state in pull_job_states.PULL_JOB_STATES:
        assert state.ledger_status in ledger_values, state.name
        # A window cannot have landed AND failed.
        assert not (state.succeeded and state.failed), state.name
        # An outcome is something only a finished window can have.
        if state.succeeded or state.failed:
            assert state.terminal, state.name
        # A window still moving cannot be reported as a fetched day.
        if not state.terminal:
            assert state.ledger_status == pull_job_states.LEDGER_RUNNING, state.name


def test_the_derived_sets_partition_the_states() -> None:
    every = set(pull_job_states.JOB_STATES)
    assert set(pull_job_states.ACTIVE_JOB_STATES) | set(
        pull_job_states.TERMINAL_JOB_STATES
    ) == every
    assert not set(pull_job_states.ACTIVE_JOB_STATES) & set(
        pull_job_states.TERMINAL_JOB_STATES
    )
    assert set(pull_job_states.FAILED_JOB_STATES) <= set(
        pull_job_states.TERMINAL_JOB_STATES
    )
    # `attempted` is still DERIVED -- from `ran`, since AI-307. A window replaced
    # by the dedup index and a window refused before it started were never run, and
    # the retry sweep of story 57.8 must not read either of them as a run that
    # happened.
    #
    # IT USED TO BE DERIVED FROM `succeeded or failed`, and that was the same set
    # only while every window that ran either landed rows or ended badly.
    # `prevented` is neither and still ran: the provider was called, and it
    # refused. Reading it OUT of this set is not bookkeeping -- the sweep takes the
    # LATEST attempted window per Datastream, so an invisible prevented window
    # would let it find an older `failed` one and re-arm the stream every hour
    # against a grant only a human at the provider can give.
    assert set(pull_job_states.ATTEMPTED_JOB_STATES) == {
        s.name for s in pull_job_states.PULL_JOB_STATES if s.ran
    }
    # And `ran` is a SUPERSET of the old derivation, never a different question.
    assert {
        s.name for s in pull_job_states.PULL_JOB_STATES if s.succeeded or s.failed
    } <= set(pull_job_states.ATTEMPTED_JOB_STATES)
    # A window that never moved cannot have run.
    for state in pull_job_states.PULL_JOB_STATES:
        if state.ran:
            assert state.terminal, state.name
    assert set(pull_job_states.FAILED_JOB_STATES) <= set(
        pull_job_states.ATTEMPTED_JOB_STATES
    )


def test_an_unknown_state_is_never_treated_as_finished() -> None:
    """Fail-closed, exactly as the execution registry is.

    Closing a run whose windows this build does not understand would mark work
    finished that is still moving.
    """
    assert pull_job_states.is_terminal("a_state_no_build_knows") is False
    assert pull_job_states.is_terminal(None) is False
    assert pull_job_states.ledger_status("a_state_no_build_knows") == (
        pull_job_states.LEDGER_NEVER_FETCHED
    )


# ---------------------------------------------------------------------------
# Layer 1 -- the database.
# ---------------------------------------------------------------------------


#: The NAMED constraint, never "the last CHECK in a file that mentions pull
#: jobs": migration 218 alters `app.pull_jobs` and declares a CHECK on
#: `app.datastream_executions.state` in the same file, and reading by proximity
#: made this sweep compare the job registry against the EXECUTION states.
_PULL_JOB_CHECK = re.compile(
    r"pull_jobs_state_check\s+CHECK\s*\(\s*state\s+IN\s*\((?P<body>[^)]*)\)",
    re.IGNORECASE,
)


def _latest_state_check() -> str:
    """The last CHECK constraint on `app.pull_jobs.state` any migration declares."""
    latest = ""
    for path in sorted(MIGRATIONS.glob("*.sql")):
        text = path.read_text(encoding="utf-8")
        for match in _PULL_JOB_CHECK.finditer(text):
            latest = match.group("body")
    return latest


def test_the_database_accepts_exactly_the_registered_states() -> None:
    body = _latest_state_check()
    assert body, "no CHECK on pull_jobs.state found in the migrations"
    declared = set(re.findall(r"'([a-z_]+)'", body))
    assert declared == set(pull_job_states.JOB_STATES), (
        "the migration CHECK and core.pull_job_states disagree: "
        f"only in SQL={sorted(declared - set(pull_job_states.JOB_STATES))}, "
        f"only in Python={sorted(set(pull_job_states.JOB_STATES) - declared)}"
    )


def test_the_active_index_predicate_is_exactly_the_active_states() -> None:
    """`uq_pull_jobs_active` is what stops the same window being pulled twice.

    A terminal state leaking into this predicate would refuse a legitimate
    re-queue of a window that was stopped; an active state missing from it would
    let two workers pull the same days at once.
    """
    sql = (MIGRATIONS / "022_pull_jobs_dedup_index.sql").read_text(encoding="utf-8")
    block = sql.split("CREATE UNIQUE INDEX uq_pull_jobs_active", 1)[1].split(";", 1)[0]
    declared = set(re.findall(r"'([a-z_]+)'", block))
    assert declared == set(pull_job_states.ACTIVE_JOB_STATES)


# ---------------------------------------------------------------------------
# Layer 2 -- the Python readers. The six copies, held closed.
# ---------------------------------------------------------------------------


#: A FIXED LIST, and that is its known weakness: a module written tomorrow is
#: invisible to it until somebody adds the line. Story 58.1 added the seventh
#: entry in the same change as the module, because a guard that does not read a
#: reader is green about nothing at all.
_PYTHON_READERS = (
    "server/core/queue.py",
    "server/core/execution_progress.py",
    "server/core/scheduler.py",
    # AD-43, 2026-08-13 : les trois routes `/api/jobs` ont quitte `admin_api.py`.
    # Une entree epinglee a l'ancien chemin ne serait pas rouge -- elle lirait un
    # fichier qui ne contient plus de vocabulaire d'etat, et se tairait.
    "server/core/jobs_api.py",
    "server/core/extract_ledger.py",
    "server/core/datastream_progress_api.py",
    # Story 58.1: the day-grain read. It publishes `job_state` on every row, so
    # it is exactly the shape of reader that would grow its own list of names.
    "server/core/datastream_daily_breakdown_api.py",
    # Story 58.3, ADDED IN THE SAME CHANGE AS THE MODULE -- which is the whole
    # discipline of this list. It reads the raw and staging relations of one
    # Datastream and reasons about which pull landed a row, so a copy of the state
    # vocabulary is exactly the shape of drift it could grow.
    "server/core/collected_mapped_reader.py",
    # Story 58.4, same discipline and same commit as the module. It reads which
    # RUN holds a Datastream, one layer above the pull windows -- and a reader
    # that explains why a re-collection owns no run is one keystroke from
    # explaining it with a copy of the window vocabulary too.
    "server/core/datastream_active_run.py",
)


#: A SQL membership list, and ONLY a membership list.
#:
#: `state = 'running' ... WHERE state = 'queued'` is a state MACHINE writing one
#: transition; it names two states because that is what a transition is, and
#: rewriting it through a registry would obscure it. What this guard is after is
#: a reader holding its own copy of the SET -- which in SQL is always an `IN`.
_SQL_MEMBERSHIP = re.compile(r"\bIN\s*\(([^)]*)\)", re.IGNORECASE)


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """The identity of every docstring Constant, so prose never trips the sweep.

    Naming the states in a docstring is exactly right, and the execution-state
    twin claims this exemption without implementing it -- `ast.walk` reaches a
    docstring like any other constant. Here it is implemented.
    """
    marked: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            if isinstance(first.value.value, str):
                marked.add(id(first.value))
    return marked


def _relisted_states(relative: str) -> list[str]:
    """Every literal in one file that re-lists three or more job state names.

    Read from the AST, so a name inside a comment or a docstring -- where naming
    the states is exactly right -- never trips it. Counted on DISTINCT names: a
    statement that mentions `queued` twice mentions one state, not two.
    """
    known = set(pull_job_states.BY_NAME)
    found: list[str] = []
    tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
    docstrings = _docstring_nodes(tree)
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            names = [
                e.value
                for e in node.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)
            ]
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstrings:
                continue
            for group in _SQL_MEMBERSHIP.findall(node.value):
                names.extend(re.findall(r"'([a-z_]+)'", group))
        hits = {n for n in names if n in known}
        if len(hits) >= 3:
            found.append(f"{relative}: {sorted(hits)}")
    return found


def test_no_python_reader_keeps_its_own_copy_of_the_job_state_list() -> None:
    """A run of three or more state names in one literal is a second truth."""
    offenders = [
        entry for relative in _PYTHON_READERS for entry in _relisted_states(relative)
    ]
    assert not offenders, (
        "these readers re-declare the pull-job states instead of importing "
        f"core.pull_job_states: {offenders}"
    )


def test_the_guard_is_not_vacuous() -> None:
    """It must SEE a re-listing when there is one, or it proves nothing.

    Story 63.1's own guard passed while observing zero writers. A sweep that can
    silently match nothing is the same defect as no sweep, one register further
    along -- so this one is shown a file that DOES re-list, and must find it.
    """
    fixture = "server/tests/conformance/test_pull_job_state_registry.py"
    assert _relisted_states(fixture), (
        "the re-listing detector found nothing in a file that provably re-lists "
        "the states -- it has stopped reading and is passing vacuously"
    )


#: The deliberate re-listing `test_the_guard_is_not_vacuous` is shown. It is a
#: test fixture and never a reader: nothing imports it, and it is exactly the
#: shape the sweep above exists to refuse.
_VACUITY_FIXTURE = ("queued", "running", "done")


def test_every_reader_that_needs_a_set_gets_it_from_the_registry() -> None:
    """The named sites of the measurement, each importing instead of listing."""
    for relative, symbol in (
        ("server/core/queue.py", "pull_job_states"),
        ("server/core/execution_progress.py", "pull_job_states"),
        ("server/core/scheduler.py", "pull_job_states"),
        ("server/core/jobs_api.py", "pull_job_states"),
        ("server/core/extract_ledger.py", "pull_job_states"),
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert symbol in source, f"{relative} does not read the registry"


def test_the_admin_filter_accepts_every_state_the_database_can_hold() -> None:
    """`superseded` was refused with a 400 by the only route that lists jobs.

    A state the CHECK accepts and an API rejects is a row nobody can ask about.
    """
    assert set(jobs_api.VALID_JOB_STATES) == set(pull_job_states.JOB_STATES)


# ---------------------------------------------------------------------------
# Layer 3 -- the console. Story 58.2.
#
# THE LAYER THAT WAS MISSING, AND THE PATTERN THAT ALREADY EXISTED.
# `test_execution_state_registry.py` has compared the TypeScript mirror of the
# EXECUTION states against its Python registry, entry for entry, since story
# 63.1. The pull-job states had no such layer -- so story 58.2 could add a table
# of seven job-state labels and a table of six extract verdicts to the console
# with nothing tying either to `core.pull_job_states`, and a state added to the
# CHECK constraint would ship green and mute: the screen would render the raw
# machine word, or a `Record` lookup would land on `undefined`.
#
# NOTHING IS RE-LISTED HERE. The expected sets are DERIVED -- the job states from
# the registry, the day statuses by running the ledger's own converter over every
# (state x verdict) pair it can be given. A guard that restated the six words
# would be the seventh copy.
# ---------------------------------------------------------------------------


CONSOLE_VOCABULARY = ROOT / "ui" / "admin" / "src" / "ui" / "CoverageBars.tsx"


def _ts_record_keys(source: str, name: str) -> list[str]:
    """The keys of one `Record` literal in a TypeScript file, in order."""
    body = source.split(f"export const {name}", 1)
    assert len(body) == 2, f"{name} is not exported from {CONSOLE_VOCABULARY.name}"
    body = body[1].split("= {", 1)[1].split("\n};", 1)[0]
    return re.findall(r"^\s{2}([a-z_]+)\s*:", body, re.MULTILINE)


def _ts_union_members(source: str, name: str) -> set[str]:
    """The members of an exported string-literal union type."""
    body = source.split(f"export type {name} =", 1)
    assert len(body) == 2, f"{name} is not exported from {CONSOLE_VOCABULARY.name}"
    return set(re.findall(r'"([a-z_]+)"', body[1].split(";", 1)[0]))


def _ledger_statuses() -> set[str]:
    """Every status the ledger can put on a day, taken from the ledger itself.

    Run over every registered state and every verdict the verification table
    holds, plus the no-pull case. Derived, so a state or a verdict added
    tomorrow enters this set without anyone remembering to type it here.
    """
    from datetime import date

    from core.extract_ledger import _day_to_ledger_entry

    day = date(2026, 7, 10)
    statuses = {_day_to_ledger_entry(day, None)["status"]}
    for state in pull_job_states.PULL_JOB_STATES:
        for verdict in (None, "ok", "partial", "empty", "failed"):
            pull = {
                "pull_id": "pull_1",
                "state": state.name,
                "date_from": day,
                "date_to": day,
                "verdict": verdict,
                "actual_rows": 1,
                "expected_rows": 1,
                "completeness_ratio": 1.0,
                "completed_at": None,
                "datastream_id": "ds_1",
                "execution_id": None,
                "error_detail": None,
            }
            statuses.add(_day_to_ledger_entry(day, pull, datastream_id="ds_1")["status"])
    return statuses


def test_the_console_names_exactly_the_seven_job_states() -> None:
    """`JOB_STATE_LABEL` is what a screen shows instead of `dead_letter`.

    An eighth state would render as its raw machine word through the fallback,
    or as `undefined` on any reader that trusts the `Record` -- and the console
    is where "a window a person stopped" is told apart from "a day nobody asked
    for", which is the whole reason story 58.1 published `job_state` at all.
    """
    source = CONSOLE_VOCABULARY.read_text(encoding="utf-8")
    labels = _ts_record_keys(source, "JOB_STATE_LABEL")
    assert set(labels) == set(pull_job_states.JOB_STATES), (
        "ui/admin/src/ui/CoverageBars.tsx has drifted from core.pull_job_states: "
        f"only in the console={sorted(set(labels) - set(pull_job_states.JOB_STATES))}, "
        f"only in Python={sorted(set(pull_job_states.JOB_STATES) - set(labels))}"
    )
    # The union type is the same list a second time inside that file; the two
    # cannot be allowed to disagree either.
    assert _ts_union_members(source, "JobState") == set(pull_job_states.JOB_STATES)
    # Every label is a SENTENCE, never the machine word passed through: a screen
    # that prints `dead_letter` has a table for nothing.
    for name in labels:
        rendered = re.search(rf"^\s{{2}}{name}:\s*\"([^\"]+)\"", source, re.MULTILINE)
        assert rendered and rendered.group(1) != name, name


def test_the_console_names_exactly_the_day_statuses_the_ledger_emits() -> None:
    """`EXTRACT_STATUS_LABEL` and `CoverageStatus`, against the ledger's output.

    `CoverageStrip.tsx` maps any status it does not recognise onto
    `never_fetched`: survivable on a strip of sixty marks, a LIE on a grid row
    that carries a date and a word -- it would report "never requested" about a
    day that was collected. The set is derived from the ledger, so a seventh
    status cannot arrive without this test naming it.
    """
    source = CONSOLE_VOCABULARY.read_text(encoding="utf-8")
    emitted = _ledger_statuses()
    assert set(_ts_record_keys(source, "EXTRACT_STATUS_LABEL")) == emitted, (
        "the console's extract vocabulary and the extract ledger disagree: "
        f"only in the console="
        f"{sorted(set(_ts_record_keys(source, 'EXTRACT_STATUS_LABEL')) - emitted)}, "
        f"only in the ledger="
        f"{sorted(emitted - set(_ts_record_keys(source, 'EXTRACT_STATUS_LABEL')))}"
    )
    assert _ts_union_members(source, "CoverageStatus") == emitted
    # And every one of them has a tone, or a pill renders with no colour at all.
    assert set(_ts_record_keys(source, "EXTRACT_STATUS_TONE")) == emitted


def test_the_console_layer_is_not_vacuous() -> None:
    """It must SEE a drift when there is one, or it proves nothing.

    The parser is shown a table it is NOT reading -- one key renamed -- and must
    report the difference. Story 63.1's guard passed while observing zero
    writers; this file already holds that lesson for the Python layer, and this
    is the same lesson one storey up.
    """
    source = CONSOLE_VOCABULARY.read_text(encoding="utf-8")
    drifted = source.replace("  dead_letter:", "  dead_letters:", 1)
    assert set(_ts_record_keys(drifted, "JOB_STATE_LABEL")) != set(
        pull_job_states.JOB_STATES
    ), "the console parser reads nothing: a renamed key went undetected"
    # And it really did read seven keys, rather than matching an empty set twice.
    assert len(_ts_record_keys(source, "JOB_STATE_LABEL")) == len(
        pull_job_states.JOB_STATES
    )


def test_the_ledger_maps_every_state_and_invents_none() -> None:
    """Every state a day can be found in reaches a ledger status ON PURPOSE.

    Before the registry, four names were listed and everything else fell into
    `never_fetched` through an `else` -- so a state added to the CHECK silently
    reported days as never fetched.
    """
    for state in pull_job_states.PULL_JOB_STATES:
        assert pull_job_states.ledger_status(state.name) == state.ledger_status
    # Only the state that LANDED rows defers to the verification verdict.
    from_verdict = [
        s.name
        for s in pull_job_states.PULL_JOB_STATES
        if s.ledger_status == pull_job_states.LEDGER_FROM_VERDICT
    ]
    assert from_verdict == [
        s.name for s in pull_job_states.PULL_JOB_STATES if s.succeeded
    ]
