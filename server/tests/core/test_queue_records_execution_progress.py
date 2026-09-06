"""The progress is written AT THE WORKER'S SEAM -- story 63.1.

Modelled on `test_queue_records_boundary_evidence.py`, and for the same reason:
`core.execution_progress` has its own tests, so re-testing it in isolation would
prove nothing about whether a real run writes anything. What was broken was never
the arithmetic -- it was that the two halves of a collection had never been
joined. Measured on preprod 2026-08-05: `app.datastream_executions` held 0 rows
for 6 `app.pull_jobs`.

So these tests pin the JOIN: the queue carries an `execution_id`, the dispatch
paths open a run, the worker writes at a window boundary, and a run that can no
longer move is given a terminal state.
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

from core import (
    admin_api,
    datastream_collection_api,
    datastream_runs_read,
    pull_job_states,
    queue,
    run_origins,
    scheduler,
)
from core.datastream_publication import (
    _FORWARD,
    ACTIVE_STATES,
    STATE_COLLECTED,
    STATE_LOADING,
    TERMINAL_STATES,
)

_QUEUE_SOURCE = Path(queue.__file__).read_text(encoding="utf-8")
_SCHEDULER_SOURCE = Path(scheduler.__file__).read_text(encoding="utf-8")
_ADMIN_API_SOURCE = Path(admin_api.__file__).read_text(encoding="utf-8")
_COLLECTION_SOURCE = Path(datastream_collection_api.__file__).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The queue carries the run identity.
# ---------------------------------------------------------------------------


def test_every_enqueue_pull_accepts_the_run_it_belongs_to():
    """All three implementations, or the first push after a backend flip is a TypeError."""
    for fn in (
        queue.enqueue_pull,
        queue.LocalBackend.enqueue_pull,
        queue.CloudTasksBackend.enqueue_pull,
    ):
        params = inspect.signature(fn).parameters
        assert "execution_id" in params, fn
        assert params["execution_id"].default is None, fn


def test_the_two_backends_take_the_same_keywords():
    """AI-97's class, held closed: a divergence is a TypeError at the first push.

    Not a re-statement of the test above -- that one names ONE keyword, this one
    refuses the NEXT divergence without naming it.
    """
    def _keywords(fn):
        return {
            name for name, p in inspect.signature(fn).parameters.items()
            if p.kind is inspect.Parameter.KEYWORD_ONLY and not name.startswith("_")
        }

    assert _keywords(queue.LocalBackend.enqueue_pull) == _keywords(
        queue.CloudTasksBackend.enqueue_pull
    )
    # And the module-level dispatcher passes every backend keyword on. The
    # containment is DELIBERATELY one-way: a keyword the dispatcher consumes
    # BEFORE the backend is legitimate, because the gates in front of the write
    # need arguments the row itself does not carry. `module_name` (AI-301) is the
    # first of them -- the topology gate resolves the module with it instead of
    # re-reading it on an RLS-armed connection that cannot see the row, and the
    # backend, which only writes the row, has no use for it.
    #
    # Every such keyword belongs in the set below, named, with the gate that eats
    # it. An unlisted extra keyword still fails -- that is AI-97's class, and this
    # is what keeps the exemption from becoming a hole.
    CONSUMED_BEFORE_THE_BACKEND = {"module_name"}  # _topology_scope_refusal

    backend_keywords = _keywords(queue.LocalBackend.enqueue_pull)
    dispatcher_keywords = _keywords(queue.enqueue_pull)
    assert backend_keywords <= dispatcher_keywords, (
        "the dispatcher drops a keyword the backend accepts"
    )
    assert dispatcher_keywords - backend_keywords == CONSUMED_BEFORE_THE_BACKEND, (
        "a dispatcher keyword reaches no backend and is not declared as consumed"
    )


def test_the_job_row_stores_the_execution_and_the_claim_reads_it_back():
    assert "datastream_id, execution_id)" in _QUEUE_SOURCE
    # Both claim paths -- the poller and the Cloud Tasks push -- return it, or the
    # worker holds a job whose run it cannot name.
    assert _QUEUE_SOURCE.count("requested_by, attempt_count, trace_id, datastream_id,") == 2
    assert _QUEUE_SOURCE.count("execution_id\n            \"\"\"") >= 1


def test_the_window_records_what_it_landed_at_the_boundary_that_closes_it():
    """`rows_written` derives from finished windows, so the window must store its rows."""
    source = inspect.getsource(queue._finish_job)
    assert "row_count = %s" in source
    # And the success path is the one that hands it over.
    assert "_finish_job(conn, job, DONE, row_count=row_count" in _QUEUE_SOURCE


def test_land_raw_rows_is_not_instrumented():
    """The shared write primitive of the 39 connectors stays untouched (arbitrage 5).

    A counter that moved there would tick for every connector with no measure
    behind it -- false liveness, and 39 times the cost of the thing it measures.
    """
    raw_landing = Path(queue.__file__).with_name("raw_landing.py").read_text(encoding="utf-8")
    assert "execution_progress" not in raw_landing
    assert "rows_written" not in raw_landing


# ---------------------------------------------------------------------------
# The worker writes ONCE, at the window boundary, on success AND on failure.
# ---------------------------------------------------------------------------


#: The pull-job states a window can never leave -- READ FROM THE REGISTRY.
#:
#: STORY 63.6 OPENED THIS. It was a set of three typed here, so this guard could
#: not see `SET state = 'superseded'` (migration 022) and would not have seen
#: `SET state = 'cancelled'` either: the whole point of the guard is to refuse a
#: terminal job exit that leaves its run open, and two of the states that end a
#: window were invisible to it. Green, and blind.
_TERMINAL_JOB_STATES = set(pull_job_states.TERMINAL_JOB_STATES)

#: The only functions allowed to write a terminal state onto a pull job.
#: `_finish_job` is the single boundary; `recover_stale_running_jobs` is the
#: bulk sweep, and closes the runs it dead-letters itself; `cancel_queued_jobs`
#: is story 63.6's stop, whose caller (`execution_progress.stop_collection_run`)
#: closes the run in the same transaction, BEFORE it refuses the windows.
_TERMINAL_WRITERS = {"_finish_job", "recover_stale_running_jobs", "cancel_queued_jobs"}


def _sql_strings(node: ast.AST) -> list[str]:
    """Every SQL-looking string a function executes, f-strings included.

    `_finish_job` composes its statement with an f-string, so an `ast.Constant`
    scan alone sees NOTHING -- which is how the first version of this guard
    passed while finding zero writers. `test_the_guard_is_not_vacuous` below
    exists because of that, and fails if this ever returns nothing again.
    """
    found: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            found.append(child.value)
        elif isinstance(child, ast.JoinedStr):
            # The literal halves of an f-string, joined by a placeholder marker
            # so `SET {assignments} WHERE` still reads as one statement.
            found.append(
                "<expr>".join(
                    part.value
                    for part in child.values
                    if isinstance(part, ast.Constant) and isinstance(part.value, str)
                )
            )
    return found


def _writes_a_terminal_state(sql: str) -> bool:
    """Does this statement put a pull job into a state it can never leave?

    Only the SET clause is read. Reading the whole statement made the first
    version answer on `WHERE state = 'running'` and conclude "not terminal" for
    the very sweep that dead-letters jobs -- the predicate is what a statement
    SELECTS, never what it writes.
    """
    text = " ".join(sql.split())
    if "UPDATE app.pull_jobs" not in text:
        return False
    lowered = text.lower()
    if "set " not in lowered:
        return False
    set_clause = lowered.split("set ", 1)[1].split(" where ", 1)[0]
    # An assignment list composed at runtime (an f-string). Conservative: a
    # statement whose SET clause this test cannot read has to go through the
    # boundary.
    if "<expr>" in set_clause:
        return True
    if "state" not in set_clause:
        return False
    # A literal assignment: only terminal values count, so the two re-queue
    # statements (`state = 'queued'`) are correctly left alone.
    literal = re.search(r"state\s*=\s*'([a-z_]+)'", set_clause)
    if literal is not None:
        return literal.group(1) in _TERMINAL_JOB_STATES
    # `state = %s`, or a CASE that can resolve to a terminal value.
    if re.search(r"state\s*=\s*%s", set_clause):
        return True
    return any(f"'{state}'" in set_clause for state in _TERMINAL_JOB_STATES)


def _functions_writing_a_terminal_job_state() -> set[str]:
    """Every function in queue.py that can put a pull job into a terminal state.

    Read from the AST so prose -- a comment, a docstring, or this file's own
    explanation -- never trips it: only strings a function actually executes
    count, and each is attributed to the function that owns it.
    """
    tree = ast.parse(_QUEUE_SOURCE)
    writers: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # Attribute to the INNERMOST function that owns the string.
        nested = {
            child
            for child in ast.walk(node)
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            and child is not node
        }
        owned = [
            sql
            for sql in _sql_strings(node)
            if not any(sql in _sql_strings(inner) for inner in nested)
        ]
        if any(_writes_a_terminal_state(sql) for sql in owned):
            writers.add(node.name)
    return writers


def test_the_guard_is_not_vacuous():
    """The guard must SEE the writers it allows, or it proves nothing.

    Its first version scanned only `ast.Constant` while `_finish_job` composes
    its statement with an f-string: it found ZERO terminal writers and passed,
    green and worthless. A guard that can silently observe nothing is the same
    defect as no guard, one register further along.
    """
    writers = _functions_writing_a_terminal_job_state()
    assert writers, (
        "the terminal-exit detector found NO writer at all -- it has stopped "
        "reading queue.py and is passing vacuously"
    )
    assert writers == _TERMINAL_WRITERS, (
        f"detector saw {sorted(writers)}, allow-list is {sorted(_TERMINAL_WRITERS)}"
    )


def test_a_requeue_is_not_mistaken_for_a_terminal_exit():
    """`state = 'queued'` sends the window round again; closing its run would lie."""
    assert not _writes_a_terminal_state(
        "UPDATE app.pull_jobs SET state = 'queued', error_detail = %s WHERE id = %s"
    )
    assert not _writes_a_terminal_state(
        "UPDATE app.pull_jobs SET attempt_count = %s WHERE id = %s"
    )
    for state in sorted(_TERMINAL_JOB_STATES):
        assert _writes_a_terminal_state(
            f"UPDATE app.pull_jobs SET state = '{state}', completed_at = now() WHERE id = %s"
        ), state
    # And the shape a future author is most likely to write.
    assert _writes_a_terminal_state(
        "UPDATE app.pull_jobs SET state = %s, completed_at = now() WHERE id = %s"
    )


def test_the_guard_reads_the_registry_and_not_a_copy_of_it():
    """The defect story 63.6 measured: this set was three names, typed here.

    `SET state = 'superseded'` was already invisible to the detector, and the
    stop of 63.6 would have been invisible in exactly the same way -- a terminal
    job exit that no test could see, leaving the run holding
    `uq_datastream_executions_active` and answering 409 forever.

    It names no state on purpose: the assertion is that this file has no opinion
    of its own about which states are terminal.
    """
    assert _TERMINAL_JOB_STATES == set(pull_job_states.TERMINAL_JOB_STATES)
    # And every one of them is refused outside the boundary, including the ones
    # that were invisible before.
    for state in sorted(_TERMINAL_JOB_STATES):
        assert _writes_a_terminal_state(
            f"UPDATE app.pull_jobs SET state = '{state}' WHERE id = %s"
        ), state


def test_the_state_a_stop_writes_is_terminal_and_arms_no_catch_up():
    """It must END the window, and it must not look like an attempt that failed.

    `_reschedule_failed_pulls` (story 57.8) reads `FAILED_JOB_STATES` and writes
    `next_run_at = NOW() + interval '1 hour'`. If the state a stop writes ever
    joined that set, stopping a run would restart it within the hour -- which is
    the one outcome this gesture may never have. Reddens if the classification
    moves, without naming the state of the day.
    """
    from core.execution_progress import JOB_CANCELLED

    assert pull_job_states.is_terminal(JOB_CANCELLED)
    assert JOB_CANCELLED in _TERMINAL_JOB_STATES
    assert not pull_job_states.has_failed(JOB_CANCELLED)
    assert JOB_CANCELLED not in pull_job_states.ATTEMPTED_JOB_STATES
    assert JOB_CANCELLED not in pull_job_states.ACTIVE_JOB_STATES


def test_the_stop_refuses_what_has_not_started_and_touches_nothing_running():
    """A provider call already in flight cannot be interrupted -- so it is not.

    `_execute_job` is synchronous and consults nothing between two pages; the
    only thing that takes a `running` job back is the stale sweep, at 5400 s.
    The write therefore filters on the one state a window can still be refused
    from, and the screen says so instead of promising an interruption.
    """
    source = " ".join(inspect.getsource(queue.cancel_queued_jobs).split())
    assert f"state = '{queue.QUEUED}'" in source
    # The predicate names the ONE state a window can still be refused from, and
    # no other: a filter that also matched `running` would claim an interruption
    # the worker cannot perform.
    predicate = source.split("WHERE", 1)[1].split("RETURNING", 1)[0]
    assert f"'{queue.RUNNING}'" not in predicate
    # It hands back the windows it refused, or the caller cannot name the scope.
    assert "RETURNING id, date_from, date_to" in source


def test_no_terminal_job_exit_can_bypass_the_run_closure():
    """THE guard. Story 63.1's first pass wired three of SEVEN terminal exits.

    The other four -- connection_ref deleted, no pull function registered,
    rate-limit exhausted, and the stale-job sweep -- left the execution in
    `loading`, which holds `uq_datastream_executions_active`. One provider
    outage would then have made every later publish AND every following night's
    dispatch answer 409 for that Datastream, forever: the exact catastrophe
    `collected` exists to prevent, arriving through the failure door.

    This test does not name those four. It names the RULE, so a terminal exit
    written next year reddens it without anyone remembering this paragraph.
    """
    offenders = _functions_writing_a_terminal_job_state() - _TERMINAL_WRITERS
    assert not offenders, (
        "these functions put a pull job into a terminal state without going "
        f"through _finish_job, so the run it belongs to is never closed: {sorted(offenders)}"
    )


def test_the_single_boundary_writes_the_job_then_moves_the_run():
    source = inspect.getsource(queue._finish_job)
    assert source.index("conn.commit()") < source.index("_record_window_outcome(")
    assert _QUEUE_SOURCE.count("def _finish_job(") == 1


def test_the_bulk_sweep_closes_the_runs_it_dead_letters():
    """A crashed worker must not freeze the Datastream it crashed on."""
    source = inspect.getsource(queue.recover_stale_running_jobs)
    assert "RETURNING id, state, execution_id, pull_id" in source
    assert "_record_window_outcome(" in source
    # A re-queued window is going to run again; closing its run would be a lie.
    assert "_state == DEAD_LETTER" in source


def test_the_progress_write_is_best_effort_and_never_undoes_the_pull():
    source = inspect.getsource(queue._record_window_outcome)
    assert "conn.rollback()" in source
    assert "execution_progress_failed" in source
    # A job with no run writes nothing rather than inventing one.
    assert "if not execution_id:\n        return" in source


def test_a_job_without_a_run_writes_nothing():
    calls = []

    def _fail(*args, **kwargs):  # pragma: no cover - must never be reached
        calls.append(kwargs)
        raise AssertionError("a legacy per-connection job has no run to write to")

    class _Conn:
        commit = staticmethod(_fail)
        rollback = staticmethod(_fail)

    queue._record_window_outcome(_Conn(), None, "pull_1", "tester")
    queue._record_window_outcome(_Conn(), "", "pull_1", "tester")
    assert calls == []


def test_every_terminal_exit_of_the_worker_goes_through_the_boundary():
    """Counted from the AST: `_execute_job` has NINE terminal exits, all wired.

    Six until AI-307 added the seventh -- a window the SOURCE refused, recorded
    `prevented` instead of the `done / 0 row` it used to be given -- and nine
    since 2026-08-25, when the same refusal became readable on a RAISED error as
    well as on a returned envelope: the typed handler and the untyped one each
    gained a prevented exit. Every count here is raised deliberately and never by
    reflex; this assertion exists precisely so a new terminal exit cannot be
    added without someone stating that it closes the run it belongs to.

    AND IT NO LONGER COUNTS ONE SPELLING. It matched `_finish_job(` textually, so
    moving one exit behind a helper that calls `_finish_job` itself -- which is
    what the two doors of AI-307 sharing one transport required -- read as a
    terminal exit DELETED. That is an instrument measuring how the call is
    spelled instead of whether the run is closed. The helpers are derived from
    the module: any function that reaches `_finish_job` closes the run, so
    calling it is a terminal exit.

    DIX depuis le 2026-08-30, et voici la dixieme : un job dont le Datastream ne
    lie aucun compte alors que le consentement en a plusieurs de CE connecteur.
    `_resolve_selected_account` leve `AccountSelectionAmbiguous` plutot que de
    tirer au sort (`data-path.md`, amendement du 2026-08-30), et le worker ferme
    le run en `dead_letter` : la reparation est un geste humain -- lier le compte
    sur le Datastream -- donc une nouvelle tentative ne ferait que le repeter.
    Elle passe par `_finish_job`, comme les neuf autres.
    """
    tree = ast.parse(_QUEUE_SOURCE)

    def _calls_named(node, names):
        return [
            child for child in ast.walk(node)
            if isinstance(child, ast.Call)
            and isinstance(child.func, ast.Name)
            and child.func.id in names
        ]

    helpers = {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name != "_execute_job"
        and _calls_named(node, {"_finish_job"})
    }
    execute_job = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_execute_job"
    )
    exits = _calls_named(execute_job, {"_finish_job"} | helpers)
    assert len(exits) == 10, (
        "a terminal exit of _execute_job was added or removed; every one of them "
        f"must close the run through _finish_job (found {len(exits)})"
    )


def test_the_worker_records_then_closes_in_that_order():
    """A terminal run whose last window is not counted yet would read as a loss."""
    from core.execution_progress import record_and_close

    source = inspect.getsource(record_and_close)
    assert source.index("record_window_progress(") < source.index(
        "close_collection_run_if_complete("
    )


# ---------------------------------------------------------------------------
# The dispatch paths open the run. This is the half that did not exist.
# ---------------------------------------------------------------------------


def test_the_nightly_dispatch_opens_a_run_and_binds_its_windows_to_it():
    # Story 63.7: the origin is a CONSTANT of `core.run_origins`, not the bare
    # literal this line used to read. The name is resolved from the registry
    # here too, so the day a key is renamed this guard follows it instead of
    # going quietly false.
    assert f"origin={run_origins.SCHEDULER_NIGHTLY.upper()}" in _SCHEDULER_SOURCE
    # The binding used to be typed at each call site and counted here. It now
    # happens ONCE, in the dispatch both the clock and the hand go through.
    from core import datastream_dispatch

    shared = inspect.getsource(datastream_dispatch.dispatch_windows)
    assert "_open_collection_run(" in shared
    assert "execution_id=execution_id," in shared


def test_the_hourly_dispatch_opens_one_too():
    """The defect is the class's: an hourly run is a run."""
    assert f"origin={run_origins.SCHEDULER_HOURLY.upper()}" in _SCHEDULER_SOURCE


def test_the_refetch_endpoint_opens_one_too():
    # AD-40 (2026-08-12): the re-collection left `admin_api.py` with the rest of
    # the collection family. The invariant follows the CODE, not the path.
    assert f"origin={run_origins.REFETCH.upper()}" in _COLLECTION_SOURCE
    assert "execution_id=execution_id," in _COLLECTION_SOURCE


def test_the_three_dispatch_origins_are_declared_and_mintable():
    """A dispatch may not name an origin the registry refuses to stamp.

    `open_collection_run` goes through `stamp_origin`, which raises on a key no
    build knows AND on a verb with no engine -- so a dispatch naming either would
    fail at run time, on the nightly path, where nobody is watching.
    """
    for key in (run_origins.SCHEDULER_NIGHTLY, run_origins.SCHEDULER_HOURLY,
                run_origins.REFETCH):
        assert key in run_origins.MINTABLE_ORIGINS
        assert run_origins.reads_provider_windows(key), (
            f"{key} declares no provider window, yet it dispatches pull jobs"
        )


def test_every_dispatch_path_closes_the_run_it_opened():
    """Left open, a run holds uq_datastream_executions_active and answers 409 forever."""
    from core import datastream_dispatch

    assert "_close_collection_run(" in inspect.getsource(
        datastream_dispatch.dispatch_windows
    )
    assert "_close_refetch_run(execution_id, subject)" in _COLLECTION_SOURCE


def test_no_dispatch_path_queues_a_pull_on_its_own():
    """The guard that replaces three counted call sites -- 2026-08-12.

    A path that calls `enqueue_pull` itself skips the gates AND the run line, and
    that is exactly what the manual door did: no gate, no execution, so a stopped
    Datastream ran and its run appeared on no screen. Measured the day it was
    repaired: **0** rows of `app.pull_jobs` carried an `execution_id`.
    """
    from core import datastream_dispatch

    for owner in (
        scheduler._dispatch_nightly_datastreams,
        scheduler._dispatch_hourly_datastreams,
        datastream_collection_api._run_datastream,
    ):
        source = inspect.getsource(owner)
        assert "dispatch_windows(" in source, f"{owner.__name__} must dispatch by call"
        assert "enqueue_pull(" not in source, (
            f"{owner.__name__} queues a pull on its own, so it passes neither the "
            "gates nor the run line"
        )
    assert "enqueue_pull(" in inspect.getsource(datastream_dispatch.dispatch_windows)


def test_a_run_that_cannot_be_opened_never_blocks_the_pull():
    """The pull is the point; the progress line is the instrumentation."""
    for source in (
        inspect.getsource(scheduler._open_collection_run),
        inspect.getsource(datastream_collection_api._open_refetch_run),
    ):
        assert "return None" in source
        assert "raise" not in source


# ---------------------------------------------------------------------------
# The terminal state that makes the join safe.
# ---------------------------------------------------------------------------


def test_a_collected_run_is_terminal_and_blocks_nothing():
    assert STATE_COLLECTED == "collected"
    assert STATE_COLLECTED in TERMINAL_STATES
    assert STATE_COLLECTED not in ACTIVE_STATES
    assert _FORWARD[STATE_COLLECTED] == frozenset()
    assert STATE_COLLECTED in _FORWARD[STATE_LOADING]


def test_a_collected_run_is_not_offered_as_a_candidate_to_review():
    """And the probe reads the registry, so the NEXT state is right too.

    AD-40 (2026-08-12): the probe left `admin_api.py` for the runs read model it
    always was. The assertion follows the SQL, and naming its module is what
    makes the next move break this test instead of quietly emptying it.
    """
    from core.execution_states import CANDIDATE_STATES

    runs_read = Path(datastream_runs_read.__file__).read_text(encoding="utf-8")
    assert "collected" not in CANDIDATE_STATES
    assert "AND state = ANY(%s)" in runs_read
    assert "list(_execution_states.CANDIDATE_STATES)" in runs_read


def test_the_frozen_evidence_tables_are_not_touched():
    """`append_phase_evidence` / `append_stage_evidence` refuse a second write."""
    from core import execution_progress

    source = Path(execution_progress.__file__).read_text(encoding="utf-8")
    assert "phase_evidence" not in source
    assert "stage_evidence" not in source


# ---------------------------------------------------------------------------
# The failure door, walked end to end. A guard that only reads source would
# have let the first version of this story pass.
# ---------------------------------------------------------------------------


class _RecordingConn:
    """A connection that records every statement and never talks to Postgres."""

    def __init__(self, ref_row=None):
        self.statements: list[str] = []
        self.commits = 0
        self._ref_row = ref_row

    def cursor(self):
        return _RecordingCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _RecordingCursor:
    def __init__(self, owner):
        self._owner = owner
        self.description = None
        self._row = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._owner.statements.append(" ".join(str(sql).split()))
        self._row = self._owner._ref_row

    def fetchone(self):
        return self._row

    def fetchall(self):
        return []


def _walk_execute_job(monkeypatch, *, execution_id):
    """Run `_execute_job` down its `connection_ref` deleted exit, for real."""
    conn = _RecordingConn(ref_row=None)
    monkeypatch.setattr(queue, "_resolve_connection_ref", lambda *a, **k: None)
    monkeypatch.setattr(queue, "_resolve_datastream_module", lambda *a, **k: "generic")
    monkeypatch.setattr(queue, "write_audit_row", lambda **k: None, raising=False)

    from contextlib import contextmanager

    @contextmanager
    def _fake_get_connection():
        yield conn

    monkeypatch.setattr("core.db.get_connection", _fake_get_connection)

    closed: list[tuple] = []
    monkeypatch.setattr(
        queue,
        "_record_window_outcome",
        lambda c, exec_id, pull_id, actor: closed.append((exec_id, actor)),
    )

    queue._execute_job(
        {
            "id": "job_1",
            "pull_id": "pull_1",
            "connection_ref_id": "cref_1",
            "date_from": "2026-07-01",
            "date_to": "2026-07-31",
            "requested_by": "tester",
            "attempt_count": 0,
            "trace_id": None,
            "datastream_id": "ds_1",
            "execution_id": execution_id,
        }
    )
    return conn, closed


def test_a_window_that_dead_letters_still_closes_its_run(monkeypatch):
    """The failure door, walked. A deleted connection_ref dead-letters the job.

    Before the repair this exit wrote `dead_letter` and returned, leaving the
    execution in `loading` -- which holds `uq_datastream_executions_active` and
    would answer 409 to every later publish AND to the next night's dispatch.
    """
    conn, closed = _walk_execute_job(monkeypatch, execution_id="dse_run_1")

    terminal_writes = [
        s for s in conn.statements
        if "UPDATE app.pull_jobs" in s and "state = %s" in s
    ]
    assert terminal_writes, f"the job was never marked terminal: {conn.statements}"
    assert closed == [("dse_run_1", "tester")], (
        "the job reached a terminal state and its run was never closed"
    )


def test_the_same_exit_writes_nothing_when_the_job_carries_no_run(monkeypatch):
    """A legacy per-connection job has no run to close, and none is invented."""
    conn, closed = _walk_execute_job(monkeypatch, execution_id=None)
    assert any("UPDATE app.pull_jobs" in s for s in conn.statements)
    assert closed == [(None, "tester")]
