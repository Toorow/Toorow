"""AI Path closure and immutability, PROVEN ON ROWS (Opus review, round 3).

Three rules that a mocked cursor could not exercise:

1. `_walk_completed_by` excludes the row just appended for the crossing being
   judged -- delete that exclusion and no walk ever closes (finding 8, rounds 2
   and 3: the mock harness returned the rows it was told and never ran the SQL).
2. A step's BEFORE INSERT trigger reads the path under FOR KEY SHARE (migration
   351): an append that started while a finalizer holds the path WAITS, then is
   refused -- the interleaving `FOR UPDATE` in the reader alone left open.
3. `previous_walks` decides one walk per trace IN SQL: a trace with more than a
   hundred pinned paths hides no other trace.

Requires TEST_POSTGRES_DSN (disposable base, `python scripts/disposable_postgres.py up`).
"""

from __future__ import annotations

import os
import threading
import uuid

import psycopg
import pytest
from core import skill_steps
from core.ai_path_recorder import _walk_completed_by
from core.ai_paths import (
    LIFECYCLE_FINALIZED,
    append_step,
    begin_path,
    finalize_path,
    load_path,
    previous_walks,
)

from tests.integration.test_datastream_change_engine_pg import (
    conn,  # noqa: F401 -- the suite's fixture
)

requires_postgres = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set -- run `python scripts/disposable_postgres.py up`",
)

ACTOR = "person_EXAMPLE"


def _scope(connection) -> tuple[str, str]:
    org_id = f"org_{uuid.uuid4().hex[:12]}"
    project_id = f"proj_{uuid.uuid4().hex[:12]}"
    with connection.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id,name,slug,created_by) VALUES (%s,%s,%s,'ai-path-closure-harness')",
            (org_id, org_id, org_id),
        )
        cur.execute(
            "INSERT INTO app.projects (id,name,slug,org_id,created_by,status) VALUES (%s,%s,%s,%s,'ai-path-closure-harness','active')",
            (project_id, project_id, project_id, org_id),
        )
    return org_id, project_id


def _path(connection, *, org_id: str, project_id: str, trace_id: str | None) -> str:
    return begin_path(
        connection, org_id=org_id, project_id=project_id, actor=ACTOR, w3c_trace_id=trace_id
    )["id"]


def _cross(
    connection, *, path_id: str, project_id: str, version: str, step: str, tool: str
) -> None:
    append_step(
        connection,
        path_id=path_id,
        project_id=project_id,
        step_kind="skill_step",
        outcome="succeeded",
        owner_workspace="context-hub",
        owner_object_type="procedure",
        owner_object_id=version.split("@", 1)[0],
        skill_version_id=version,
        skill_step_id=step,
        tool_name=tool,
    )


@requires_postgres
def test_the_closure_excludes_the_row_of_the_crossing_it_judges(conn) -> None:  # noqa: F811
    """Rule 1. The recorder appends the crossing's row BEFORE asking whether it completed the walk."""
    org_id, project_id = _scope(conn)
    trace = uuid.uuid4().hex
    path_id = _path(conn, org_id=org_id, project_id=project_id, trace_id=trace)
    skill_steps.forget_served()
    skill_steps.remember_served(
        trace,
        procedure_id="proc_x",
        version_number=2,
        project_id=project_id,
        steps=[
            {"step": 1, "tool": "execute_analyze_query_spec", "required": True},
            {"step": 2, "tool": "analyze_result", "required": True},
        ],
    )
    try:
        _cross(
            conn,
            path_id=path_id,
            project_id=project_id,
            version="proc_x@2",
            step="1",
            tool="execute_analyze_query_spec",
        )
        # Step 1 alone: the walk is not complete, whichever step is judged.
        assert (
            _walk_completed_by(
                conn,
                path_id=path_id,
                project_id=project_id,
                trace_id=trace,
                crossed_now=("proc_x@2", "1"),
            )
            is False
        )
        _cross(
            conn,
            path_id=path_id,
            project_id=project_id,
            version="proc_x@2",
            step="2",
            tool="analyze_result",
        )
        # Step 2's row is in the table when the recorder asks -- and it is THIS crossing that completes the walk.
        assert (
            _walk_completed_by(
                conn,
                path_id=path_id,
                project_id=project_id,
                trace_id=trace,
                crossed_now=("proc_x@2", "2"),
            )
            is True
        )
        # A retry of step 1 after both were crossed completes nothing.
        _cross(
            conn,
            path_id=path_id,
            project_id=project_id,
            version="proc_x@2",
            step="1",
            tool="execute_analyze_query_spec",
        )
        assert (
            _walk_completed_by(
                conn,
                path_id=path_id,
                project_id=project_id,
                trace_id=trace,
                crossed_now=("proc_x@2", "1"),
            )
            is False
        )
        # The crossing on a LATER path of the same trace is judged over the interaction.
        later = _path(conn, org_id=org_id, project_id=project_id, trace_id=trace)
        _cross(
            conn,
            path_id=later,
            project_id=project_id,
            version="proc_x@2",
            step="2",
            tool="analyze_result",
        )
        assert (
            _walk_completed_by(
                conn,
                path_id=later,
                project_id=project_id,
                trace_id=trace,
                crossed_now=("proc_x@2", "2"),
            )
            is False
        )
    finally:
        skill_steps.forget_served()


@requires_postgres
def test_an_append_that_started_under_a_finalizer_waits_and_is_refused() -> None:
    """Rule 2 (migration 351). Two connections, the finalizer first: the appending
    transaction's trigger must WAIT for the path lock, re-read `finalized`, and refuse."""
    dsn = os.environ["TEST_POSTGRES_DSN"]
    assert "supabase" not in dsn.lower() and "pooler" not in dsn.lower()
    finalizer = psycopg.connect(dsn, connect_timeout=5)
    appender = psycopg.connect(dsn, connect_timeout=5)
    outcome: dict[str, object] = {}
    try:
        org_id, project_id = _scope(finalizer)
        path_id = _path(finalizer, org_id=org_id, project_id=project_id, trace_id=None)
        append_step(
            finalizer,
            path_id=path_id,
            project_id=project_id,
            step_kind="tool_call",
            outcome="succeeded",
            tool_name="t0",
        )
        finalizer.commit()  # the path exists for both connections

        with finalizer.cursor() as cur:  # the finalizer takes the path, as finalize_path does first
            cur.execute(
                "SELECT lifecycle FROM app.ai_paths WHERE id = %s AND project_id = %s FOR UPDATE",
                (path_id, project_id),
            )
        started = threading.Event()

        def _append() -> None:
            started.set()
            try:
                append_step(
                    appender,
                    path_id=path_id,
                    project_id=project_id,
                    step_kind="tool_call",
                    outcome="succeeded",
                    tool_name="late",
                )
                appender.commit()
                outcome["result"] = "appended"
            except Exception as exc:  # noqa: BLE001
                appender.rollback()
                outcome["result"] = "refused"
                outcome["sqlstate"] = getattr(exc, "sqlstate", None)

        worker = threading.Thread(target=_append, daemon=True)
        worker.start()
        started.wait(5)
        worker.join(1.0)
        # The append is WAITING on the finalizer's lock -- not done, not refused yet.
        assert worker.is_alive(), f"the append did not wait for the finalizer: {outcome}"
        finalize_path(finalizer, path_id=path_id, project_id=project_id, outcome="succeeded")
        finalizer.commit()
        worker.join(10)
        assert not worker.is_alive()
        assert outcome == {"result": "refused", "sqlstate": "23000"}, outcome
        path = load_path(
            finalizer, path_id=path_id, project_id=project_id, assess_over_interaction=False
        )
        assert path["lifecycle"] == LIFECYCLE_FINALIZED
        assert [s.get("tool_name") for s in path["steps"]] == ["t0"]
    finally:
        for connection in (finalizer, appender):
            try:
                connection.rollback()
            finally:
                connection.close()


@requires_postgres
def test_one_chatty_trace_hides_no_other_trace_from_previous_walks(conn) -> None:  # noqa: F811
    """Rule 3. 101 pinned paths on one trace, one older path on another: both traces are walks."""
    org_id, project_id = _scope(conn)
    lone_trace = uuid.uuid4().hex
    lone = _path(conn, org_id=org_id, project_id=project_id, trace_id=lone_trace)
    _cross(
        conn,
        path_id=lone,
        project_id=project_id,
        version="proc_y@1",
        step="1",
        tool="execute_analyze_query_spec",
    )
    chatty_trace = uuid.uuid4().hex
    for _ in range(101):
        path_id = _path(conn, org_id=org_id, project_id=project_id, trace_id=chatty_trace)
        _cross(
            conn,
            path_id=path_id,
            project_id=project_id,
            version="proc_y@1",
            step="1",
            tool="execute_analyze_query_spec",
        )
    walks = previous_walks(conn, project_id=project_id, procedure_id="proc_y", limit=5)
    assert len(walks) == 2
    assert lone in {
        w["path_id"] for w in walks
    }  # started_at is the transaction time: order among equals is arbitrary
    # The prefix is a range, not a pattern: `proc_y` reads neither `proc_z`, `proc_` nor `proc`.
    assert previous_walks(conn, project_id=project_id, procedure_id="proc_z", limit=5) == []
    assert previous_walks(conn, project_id=project_id, procedure_id="proc_", limit=5) == []
    assert previous_walks(conn, project_id=project_id, procedure_id="proc", limit=5) == []


@requires_postgres
def test_the_owner_object_types_the_emitters_write_are_all_storable(conn) -> None:  # noqa: F811
    """AI-376. Every candidate kind, mapped by the writing boundary, lands in the column; the
    Event step then reaches the overlay's Event branch (a reference, never a drawn node)."""
    from core import candidate_emission
    from core.ai_paths import OVERLAY_EVENT_OBJECT_TYPE, graph_overlay, load_path

    org_id, project_id = _scope(conn)
    path_id = _path(conn, org_id=org_id, project_id=project_id, trace_id=None)
    for kind in ("topic", "procedure", "schema_doc", "context_event"):
        append_step(
            conn, path_id=path_id, project_id=project_id, step_kind="knowledge_read", outcome="succeeded",
            owner_workspace="context-hub", owner_object_type=candidate_emission.owner_object_type_for(kind),
            owner_object_id=f"{kind}_1", tool_name="search_context",
        )
    path = load_path(conn, path_id=path_id, project_id=project_id, assess_over_interaction=False)
    assert [s.get("owner_object_type") for s in path["steps"]] == ["topic", "procedure", "schema-doc", OVERLAY_EVENT_OBJECT_TYPE]
    overlay = graph_overlay(path)
    assert [ref["event_id"] for ref in overlay["event_references"]] == ["context_event_1"]
