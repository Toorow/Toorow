"""Story 49.6 -- the AI Path owner finally has a caller.

`core.ai_paths` shipped complete (begin/append/finalize/load/list/assess) with
migration 150 behind it, and nothing called it. `context-hub.md`'s last
criterion -- "AI usage paths and their evidence cannot be inspected or
evaluated" -- therefore stayed open behind a finished implementation.

These tests hold the recorder's contract, and three of them are about what it
REFUSES to record. Evidence that invents a scope, or that disappears when an
unrelated setting is off, or that only keeps successes, is worse than a gap.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest
from core.ai_path_recorder import _trace_id_of, record_tool_call

TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
PARENT = f"00-{TRACE}-00f067aa0ba902b7-01"


# --- Trace grouping --------------------------------------------------------


def test_a_traceparent_yields_the_interaction_id_that_groups_the_calls() -> None:
    assert _trace_id_of({"traceparent": PARENT}) == TRACE
    assert _trace_id_of({"traceParent": PARENT}) == TRACE


def test_protocol_meta_model_yields_the_same_traceparent() -> None:
    class Meta:
        def model_dump(self):
            return {"progressToken": "tok-1", "traceparent": PARENT}

    assert _trace_id_of(Meta()) == TRACE


def test_hostile_metadata_access_is_non_authoritative() -> None:
    class ExplodingMapping(dict):
        def get(self, *_args, **_kwargs):
            raise RuntimeError("unreadable")

    class ExplodingModel:
        @property
        def model_dump(self):
            raise RuntimeError("unreadable")

    assert _trace_id_of(ExplodingMapping()) is None
    assert _trace_id_of(ExplodingModel()) is None


@pytest.mark.parametrize(
    "meta",
    [None, {}, {"traceparent": "not-a-header"}, {"traceparent": "00-short-x-01"}, "string"],
)
def test_an_absent_or_malformed_traceparent_groups_nothing(meta: object) -> None:
    """Guessing that two calls belong together would fabricate a connection."""
    assert _trace_id_of(meta) is None


@pytest.mark.parametrize(
    "traceparent",
    [
        f"00-{'z' * 32}-00f067aa0ba902b7-01",
        f"00-{'0' * 32}-00f067aa0ba902b7-01",
        f"00-{TRACE.upper()}-00f067aa0ba902b7-01",
        f"00-{TRACE}-{'0' * 16}-01",
    ],
)
def test_a_length_correct_but_invalid_traceparent_is_ignored(traceparent) -> None:
    assert _trace_id_of({"traceparent": traceparent}) is None


# --- What is recorded ------------------------------------------------------


class _Recorder:
    """Captures what the recorder asked core.ai_paths to write."""

    def __init__(self, *, open_path: str | None = None):
        self.begun: list[dict] = []
        self.steps: list[dict] = []
        self._open_path = open_path

    def install(self, monkeypatch, *, scope=("proj_EXAMPLE", "org_EXAMPLE")):
        import core.ai_path_recorder as mod

        class _Conn:
            def cursor(self):
                raise AssertionError("the open-path lookup must be patched in these tests")

            def commit(self):
                return None

            def transaction(self):
                """Le point de reprise que prennent les ecritures secondaires.

                Sans lui, l'ajout d'un pas sous SAVEPOINT levait un
                `AttributeError` que le recorder avalait -- et le test lisait
                << pas de pas de Skill >> la ou le code en ecrit un. Meme piege,
                et meme reparation, que le faux de `test_context_search.py`.
                """
                import contextlib

                return contextlib.nullcontext()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return None

        monkeypatch.setattr(mod, "_resolve_scope", lambda arguments: scope)
        monkeypatch.setattr(
            mod, "_open_path_for", lambda conn, **kw: self._open_path
        )
        monkeypatch.setattr("core.db.get_connection", lambda *a, **k: _Conn())

        def fake_begin(conn, **kw):
            self.begun.append(kw)
            return {"id": "aip_new"}

        def fake_append(conn, **kw):
            self.steps.append(kw)
            return {"id": "aps_1"}

        monkeypatch.setattr("core.ai_paths.begin_path", fake_begin)
        monkeypatch.setattr("core.ai_paths.append_step", fake_append)
        return self


def test_a_tool_call_becomes_a_step_of_a_new_path(monkeypatch) -> None:
    rec = _Recorder().install(monkeypatch)

    path_id = record_tool_call(
        tool_name="get_daily_report",
        arguments={"project_id": "proj_EXAMPLE"},
        meta={"traceparent": PARENT},
        outcome="succeeded",
        actor="person_1",
    )

    assert path_id == "aip_new"
    assert rec.begun[0]["w3c_trace_id"] == TRACE
    assert rec.begun[0]["actor"] == "person_1"
    # The snapshot is pinned before the run, which is what it is judged against.
    assert rec.begun[0]["policy_snapshot"] == {"recorded_by": "mcp_tool_middleware"}
    assert rec.steps[0]["tool_name"] == "get_daily_report"
    assert rec.steps[0]["step_kind"] == "tool_call"
    assert rec.steps[0]["outcome"] == "succeeded"


def test_a_second_call_on_the_same_trace_joins_the_open_path(monkeypatch) -> None:
    """An answer is several tool calls; it is ONE path with several steps."""
    rec = _Recorder(open_path="aip_open").install(monkeypatch)

    path_id = record_tool_call(
        tool_name="get_data", arguments={"project_id": "proj_EXAMPLE"},
        meta={"traceparent": PARENT}, outcome="succeeded", actor="person_1",
    )

    assert path_id == "aip_open"
    assert rec.begun == []  # no second path opened for the same interaction
    assert rec.steps[0]["path_id"] == "aip_open"


def test_a_failure_is_recorded_as_a_step_not_dropped(monkeypatch) -> None:
    """A path that only keeps its successes is not evidence of what happened."""
    rec = _Recorder().install(monkeypatch)

    record_tool_call(
        tool_name="get_data", arguments={"project_id": "proj_EXAMPLE"},
        meta=None, outcome="failed", actor="person_1",
    )

    assert rec.steps[0]["outcome"] == "failed"


def test_result_execution_opens_then_binds_then_finalizes_one_path(monkeypatch) -> None:
    """The Result receives the path id before that same transaction freezes it."""
    from core import ai_path_recorder as recorder

    events: list[tuple] = []

    monkeypatch.setattr(recorder, "current_call_trace_id", lambda: TRACE)
    monkeypatch.setattr(
        recorder,
        "_open_path_for",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Result execution paths must always be fresh")
        ),
    )
    monkeypatch.setattr(
        "core.ai_paths.begin_path",
        lambda conn, **kwargs: events.append(("begin", kwargs)) or {"id": "aip_result"},
    )
    monkeypatch.setattr(
        "core.ai_paths.append_step",
        lambda conn, **kwargs: events.append(("step", kwargs)) or {"id": "aps_result"},
    )
    monkeypatch.setattr(
        "core.ai_paths.finalize_path",
        lambda conn, **kwargs: events.append(("finalize", kwargs)) or kwargs,
    )

    def execute(path_id: str):
        events.append(("execute", path_id))
        return {"result_id": "qr_result", "outcome": "success", "ai_path": path_id}

    result = recorder.record_result_execution(
        object(),
        org_id="org_EXAMPLE",
        project_id="proj_EXAMPLE",
        actor="person_1",
        tool_name=recorder.RESULT_EXECUTION_TOOL_NAME,
        execute=execute,
    )

    assert result["ai_path"] == "aip_result"
    assert [event[0] for event in events] == ["begin", "execute", "step", "finalize"]
    assert events[1] == ("execute", "aip_result")
    assert events[2][1]["path_id"] == "aip_result"
    assert events[3][1] == {
        "path_id": "aip_result",
        "project_id": "proj_EXAMPLE",
        "outcome": "succeeded",
    }


@pytest.mark.parametrize(
    ("result_outcome", "path_outcome"),
    [
        ("success", "succeeded"),
        ("empty", "succeeded"),
        ("degraded", "succeeded"),
        ("refused", "refused"),
        ("unavailable", "unavailable"),
    ],
)
def test_delivered_analytical_outcomes_finalize_the_path(
    monkeypatch, result_outcome, path_outcome
) -> None:
    from core import ai_path_recorder as recorder

    finalized = []
    monkeypatch.setattr(recorder, "current_call_trace_id", lambda: None)
    monkeypatch.setattr(recorder, "_open_path_for", lambda *args, **kwargs: None)
    monkeypatch.setattr("core.ai_paths.begin_path", lambda *args, **kwargs: {"id": "aip_1"})
    monkeypatch.setattr("core.ai_paths.append_step", lambda *args, **kwargs: {"id": "aps_1"})
    monkeypatch.setattr(
        "core.ai_paths.finalize_path",
        lambda conn, **kwargs: finalized.append(kwargs) or kwargs,
    )

    recorder.record_result_execution(
        object(),
        org_id="org_1",
        project_id="proj_1",
        actor="person_1",
        tool_name=recorder.RESULT_EXECUTION_TOOL_NAME,
        execute=lambda path_id: {
            "result_id": "qr_1",
            "outcome": result_outcome,
            "ai_path": path_id,
        },
    )

    assert finalized[0]["outcome"] == path_outcome


def test_same_trace_different_actors_receive_fresh_result_paths(monkeypatch) -> None:
    from core import ai_path_recorder as recorder

    begun = []
    monkeypatch.setattr(recorder, "current_call_trace_id", lambda: TRACE)
    monkeypatch.setattr(
        recorder,
        "_open_path_for",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unsafe reuse")),
    )

    def begin(conn, **kwargs):
        begun.append(kwargs)
        return {"id": f"aip_{len(begun)}"}

    monkeypatch.setattr("core.ai_paths.begin_path", begin)
    monkeypatch.setattr("core.ai_paths.append_step", lambda *args, **kwargs: {})
    monkeypatch.setattr("core.ai_paths.finalize_path", lambda *args, **kwargs: {})

    paths = []
    for actor in ("alice", "bob"):
        result = recorder.record_result_execution(
            object(),
            org_id="org_1",
            project_id="proj_1",
            actor=actor,
            tool_name=recorder.RESULT_EXECUTION_TOOL_NAME,
            execute=lambda path_id: {
                "result_id": f"qr_{actor}",
                "outcome": "success",
                "ai_path": path_id,
            },
        )
        paths.append(result["ai_path"])

    assert paths == ["aip_1", "aip_2"]
    assert [row["actor"] for row in begun] == ["alice", "bob"]
    assert {row["w3c_trace_id"] for row in begun} == {TRACE}


@pytest.mark.anyio
@pytest.mark.parametrize("fails", [False, True])
async def test_transactional_result_tool_never_creates_a_second_middleware_path(
    monkeypatch, fails
) -> None:
    from types import SimpleNamespace

    from core import ai_path_recorder as recorder

    middleware = recorder.build_middleware()
    assert middleware is not None
    recorded = []
    monkeypatch.setattr(
        recorder,
        "record_tool_call",
        lambda **kwargs: recorded.append(kwargs) or "aip_second",
    )
    context = SimpleNamespace(
        message=SimpleNamespace(
            name=recorder.RESULT_EXECUTION_TOOL_NAME,
            arguments={"project_id": "proj_EXAMPLE"},
            meta={"traceparent": PARENT},
        ),
        fastmcp_context=None,
    )

    async def call_next(_context):
        if fails:
            raise RuntimeError("transaction rolled back")
        return "delivered"

    if fails:
        with pytest.raises(RuntimeError, match="transaction rolled back"):
            await middleware.on_call_tool(context, call_next)
    else:
        assert await middleware.on_call_tool(context, call_next) == "delivered"
    assert recorded == []


@pytest.mark.anyio
@pytest.mark.parametrize("fails", [False, True])
async def test_search_context_opens_before_call_and_uses_only_its_fresh_finalizer(
    monkeypatch, fails
) -> None:
    from types import SimpleNamespace

    from core import ai_path_recorder as recorder

    events: list[tuple] = []
    observation = SimpleNamespace(closed=False)
    def begin(**kwargs):
        events.append(("begin", kwargs))
        kwargs["emitter"].bind_path("aip_search")
        events.append(("bind",))
        return observation

    monkeypatch.setattr(recorder, "_begin_search_context_observation", begin)

    def finalize(current, **kwargs):
        current.closed = True
        events.append(("finalize", current, kwargs))

    monkeypatch.setattr(recorder, "_finalize_search_context_observation", finalize)
    monkeypatch.setattr(
        recorder,
        "record_tool_call",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("search_context must not reuse the generic trace path")
        ),
    )
    middleware = recorder.build_middleware()
    context = SimpleNamespace(
        message=SimpleNamespace(
            name="search_context",
            arguments={"project_id": "proj_EXAMPLE"},
            meta={"traceparent": PARENT},
        ),
        fastmcp_context=None,
    )

    async def call_next(_context):
        assert [event[0] for event in events] == ["begin", "bind"]
        events.append(("call",))
        if fails:
            raise RuntimeError("search failed")
        return "delivered"

    if fails:
        with pytest.raises(RuntimeError, match="search failed"):
            await middleware.on_call_tool(context, call_next)
    else:
        assert await middleware.on_call_tool(context, call_next) == "delivered"

    assert [event[0] for event in events] == ["begin", "bind", "call", "finalize"]
    expected = "failed" if fails else "succeeded"
    assert events[-1][2]["outcome"] == expected


class _SearchObservationConn:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0
        self.closed = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.closed += 1
        return False


@pytest.mark.parametrize("project_id", ["proj_foreign", "proj_missing"])
def test_search_scope_uses_its_request_connection_and_hides_absence_kind(
    monkeypatch, project_id
) -> None:
    """REPLACES the version that pinned `SELECT org_id` as the ONLY statement.

    2026-08-25, the `_resolve_project` arbitration. The recorder used to resolve
    the project through `core.main._resolve_project`, which opened its own
    connection: the existence question ran on a connection the actor did not
    arm, and only the `org_id` lookup below ran on the caller's. It now resolves
    BOTH on the connection this path already holds, so this cursor sees two
    statements instead of one -- and the property under test is unchanged and
    stronger: exactly ONE acquisition, armed for the actor, and a foreign project
    is indistinguishable from a missing one.
    """
    from core import ai_path_recorder as recorder

    statements: list[str] = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def execute(self, statement, params):
            statements.append(statement)
            assert "FROM app.projects" in statement
            assert params == (project_id,)

        def fetchone(self):
            # FORCE RLS makes a foreign project indistinguishable from a missing one.
            return None

    class Conn(_SearchObservationConn):
        def cursor(self):
            return Cursor()

    conn = Conn()
    requested: list[str] = []
    begun: list[dict] = []
    monkeypatch.setattr(
        "core.db.get_connection",
        lambda: (_ for _ in ()).throw(
            AssertionError("search scope must not open an unarmed connection")
        ),
    )
    monkeypatch.setattr(
        "core.db.request_connection",
        lambda actor: requested.append(actor) or conn,
    )
    monkeypatch.setattr(
        "core.ai_paths.begin_path",
        lambda _conn, **kwargs: begun.append(kwargs) or {"id": "aip_search"},
    )

    observation = recorder._begin_search_context_observation(
        arguments={"project_id": project_id},
        meta=None,
        actor="person_1",
        emitter=recorder._PathEmitter(None, None),
    )

    assert observation is None
    assert requested == ["person_1"]
    assert begun == []
    assert (conn.commits, conn.rollbacks, conn.closed) == (0, 0, 1)
    # The existence question runs on the ARMED connection too. Without this line
    # the recorder could go back to resolving it elsewhere and stay green.
    assert statements and all("app.projects" in s for s in statements)


def test_search_observation_is_fresh_atomic_and_preserves_captured_crossing_time(
    monkeypatch,
) -> None:
    from core import ai_path_recorder as recorder

    conn = _SearchObservationConn()
    begun: list[dict] = []
    appended: list[dict] = []
    finalized: list[dict] = []
    captured_at = datetime(2026, 8, 10, 9, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(
        recorder,
        "_resolve_scope_on_connection",
        lambda conn, arguments: ("proj_1", "org_1"),
    )
    monkeypatch.setattr("core.db.request_connection", lambda actor: conn)
    monkeypatch.setattr(
        recorder,
        "_open_path_for",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("dedicated search paths are never reused")
        ),
    )
    monkeypatch.setattr(
        "core.ai_paths.begin_path",
        lambda _conn, **kwargs: begun.append(kwargs) or {"id": "aip_search"},
    )
    monkeypatch.setattr(
        "core.ai_paths.append_step",
        lambda _conn, **kwargs: appended.append(kwargs) or {"id": "aps_1"},
    )
    monkeypatch.setattr(
        "core.ai_paths.finalize_path",
        lambda _conn, **kwargs: finalized.append(kwargs) or {"id": "aip_search"},
    )
    monkeypatch.setattr(
        recorder,
        "_skill_step_of",
        lambda *args, **kwargs: ("proc_1@2", "3", "proc_1"),
    )

    emitter = recorder._PathEmitter(None, None)
    observation = recorder._begin_search_context_observation(
        arguments={"project_id": "proj_1"},
        meta={"traceparent": PARENT},
        actor="person_1",
        emitter=emitter,
    )
    assert observation is not None
    assert begun[0]["policy_snapshot"]["content_hash_contract"] == "ai-path-content.v2"
    emitter.record_crossing(
        step_kind="knowledge_read",
        outcome="succeeded",
        tool_name="search_context",
        owner_workspace="context-hub",
        owner_object_type="topic",
        owner_object_id="top_1",
        observed_at=captured_at,
        detail={"branch_schema_version": "retrieval-branch-detail.v1"},
    )

    path_id = recorder._finalize_search_context_observation(
        observation,
        emitter=emitter,
        outcome="succeeded",
    )

    assert path_id == "aip_search"
    assert [step["step_kind"] for step in appended] == [
        "knowledge_read",
        "skill_step",
        "tool_call",
    ]
    assert appended[0]["observed_at"] is captured_at
    assert appended[0]["detail"]["branch_schema_version"] == "retrieval-branch-detail.v1"
    assert appended[1]["skill_version_id"] == "proc_1@2"
    assert appended[-1]["tool_name"] == "search_context"
    assert finalized == [
        {"path_id": "aip_search", "project_id": "proj_1", "outcome": "succeeded"}
    ]
    assert (conn.commits, conn.rollbacks, conn.closed) == (1, 0, 1)


def test_search_observation_storage_failure_rolls_back_and_never_finalizes(monkeypatch) -> None:
    from core import ai_path_recorder as recorder

    conn = _SearchObservationConn()
    finalized: list[dict] = []
    monkeypatch.setattr(
        recorder,
        "_resolve_scope_on_connection",
        lambda conn, arguments: ("proj_1", "org_1"),
    )
    monkeypatch.setattr("core.db.request_connection", lambda actor: conn)
    monkeypatch.setattr(
        "core.ai_paths.begin_path", lambda _conn, **kwargs: {"id": "aip_search"}
    )
    monkeypatch.setattr(
        "core.ai_paths.append_step",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("write refused")),
    )
    monkeypatch.setattr(
        "core.ai_paths.finalize_path",
        lambda _conn, **kwargs: finalized.append(kwargs),
    )
    monkeypatch.setattr(recorder, "_skill_step_of", lambda *args, **kwargs: None)
    emitter = recorder._PathEmitter(None, None)
    observation = recorder._begin_search_context_observation(
        arguments={"project_id": "proj_1"},
        meta=None,
        actor="person_1",
        emitter=emitter,
    )
    emitter.record_crossing(step_kind="knowledge_read", outcome="succeeded")

    assert recorder._finalize_search_context_observation(
        observation, emitter=emitter, outcome="succeeded"
    ) is None
    assert finalized == []
    assert (conn.commits, conn.rollbacks, conn.closed) == (0, 1, 1)


@pytest.mark.anyio
async def test_search_cancellation_rolls_back_and_closes_the_open_observation(
    monkeypatch,
) -> None:
    from types import SimpleNamespace

    from core import ai_path_recorder as recorder

    conn = _SearchObservationConn()
    finalized: list[dict] = []
    monkeypatch.setattr(
        recorder,
        "_resolve_scope_on_connection",
        lambda conn, arguments: ("proj_1", "org_1"),
    )
    monkeypatch.setattr("core.db.request_connection", lambda actor: conn)
    monkeypatch.setattr(
        "core.ai_paths.begin_path", lambda _conn, **kwargs: {"id": "aip_search"}
    )
    monkeypatch.setattr(
        "core.ai_paths.finalize_path",
        lambda _conn, **kwargs: finalized.append(kwargs),
    )
    monkeypatch.setattr(recorder, "_skill_step_of", lambda *args, **kwargs: None)
    context = SimpleNamespace(
        message=SimpleNamespace(
            name="search_context",
            arguments={"project_id": "proj_1"},
            meta={"traceparent": PARENT},
        ),
        fastmcp_context=None,
    )

    async def cancel(_context):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await recorder.build_middleware().on_call_tool(context, cancel)

    assert finalized == []
    assert (conn.commits, conn.rollbacks, conn.closed) == (0, 1, 1)


@pytest.mark.anyio
async def test_bound_search_dispatcher_skips_legacy_and_never_sends_branch_detail() -> None:
    from types import SimpleNamespace

    from core import ai_path_recorder as recorder

    class Client:
        def __init__(self):
            self.request_context = SimpleNamespace(
                meta=SimpleNamespace(progressToken="tok-search")
            )
            self.messages: list[dict] = []

        async def report_progress(self, progress, total=None, message=None):
            self.messages.append(json.loads(message))

    client = Client()
    emitter = recorder._PathEmitter(client, asyncio.get_running_loop())
    emitter.disable_legacy()
    assert emitter.bind_path("aip_search") is True
    assert await emitter.emit_legacy(level="JOB", state="started") is False
    accepted, use_legacy = emitter.record_and_route_step(
        recorder._crossing_fields(
            step_kind="knowledge_read",
            outcome="succeeded",
            tool_name="search_context",
            owner_workspace="context-hub",
            owner_object_type="topic",
            owner_object_id="ctx_1",
            owner_version_id=None,
            evidence_record_id=None,
            detail={"candidate_titles": ["must stay persisted only"]},
        )
    )
    assert (accepted, use_legacy) == (True, False)
    await emitter.finish()

    assert [message["event"] for message in client.messages] == [
        "recording",
        "step_observed",
    ]
    assert "detail" not in client.messages[-1]["step"]


# --- What is refused -------------------------------------------------------


def test_a_call_with_no_resolvable_project_records_nothing(monkeypatch) -> None:
    """A path is Project-scoped evidence; inventing a scope is worse than a gap."""
    rec = _Recorder().install(monkeypatch, scope=None)

    path_id = record_tool_call(
        tool_name="health", arguments={}, meta=None, outcome="succeeded", actor="person_1",
    )

    assert path_id is None
    assert rec.begun == [] and rec.steps == []


def test_recording_never_breaks_the_call_it_observes(monkeypatch) -> None:
    """Evidence collection that can take down the observed thing is a liability."""
    import core.ai_path_recorder as mod

    def explode(arguments):
        raise RuntimeError("the store is on fire")

    monkeypatch.setattr(mod, "_resolve_scope", explode)

    # No exception escapes, and the caller is told nothing was recorded.
    assert record_tool_call(
        tool_name="get_data", arguments={"project_id": "p"}, meta=None,
        outcome="succeeded", actor="person_1",
    ) is None


def test_a_middleware_recorded_path_is_unverifiable_never_a_pass() -> None:
    """The concrete harm behind the `assess` change, pinned at this end too.

    A middleware has no expectation to declare, so `begin_path` pins a snapshot
    with no required or forbidden node. `assess` used to return `pass` for that,
    and the Story 49.6 workbench rendered it as an assessment -- a verdict nobody
    earned, on every path this recorder wrote.

    Jean's arbitration of 2026-07-31 put relevance in the assessor, judged
    against a pinned expectation. Where none is pinned, the honest answer is that
    the question was never asked.
    """
    from core.ai_paths import assess

    verdict = assess(
        {"recorded_by": "mcp_tool_middleware"},
        [
            {
                "step_kind": "tool_call", "outcome": "succeeded", "tool_name": "get_report",
                "owner_workspace": None, "owner_object_type": None,
                "owner_object_id": None, "owner_version_id": None,
            }
        ],
        outcome="succeeded",
    )

    assert verdict["verdict"] == "unverifiable"
    assert "nothing to judge" in verdict["findings"][0]["detail"]


# --- The crossing buffer: what the database will accept ---------------------
#
# Both tests below come from ONE measured failure, on a real MCP call against a
# real Postgres on 2026-08-04: `search_context` recorded ZERO steps -- not the
# crossing, and not even the `tool_call` step that describes the call itself --
# while `health`, which crosses nothing, recorded one. The path header existed
# and was empty. Nothing in the unit suite could see it, because both halves of
# the defect only exist against a database.


def test_a_crossing_that_reached_nothing_carries_no_workspace_either():
    """`ck_ai_path_steps_owner_complete` (migration 150): "an owner reference is
    a workspace + type + id, or it is not a reference."

    `candidate_emission.emit_candidates` takes `owner_workspace` as a DEFAULT
    parameter, so it is always set, while `context_search.walk_crossings`
    correctly leaves type and id null for a walk that found nothing. That pair
    is a PARTIAL reference, and the database refuses it -- for every context
    walk that found nothing, which is the common case on a young project.
    """
    from core.ai_path_recorder import _PathEmitter

    emitter = _PathEmitter(None, None)

    emitter.record_crossing(
        step_kind="knowledge_read",
        outcome="succeeded",
        owner_workspace="context-hub",
        owner_object_type=None,
        owner_object_id=None,
    )
    crossing = emitter.drain_crossings()[0]
    assert crossing["owner_workspace"] is None
    # The rest of the crossing is untouched: it is still a real step that
    # happened, and dropping it would hide that the walk ran at all.
    assert crossing["step_kind"] == "knowledge_read"


def test_a_complete_reference_keeps_its_workspace():
    """The normalisation must not eat a reference that IS complete -- that is
    the one a graph node gets decorated from."""
    from core.ai_path_recorder import _PathEmitter

    emitter = _PathEmitter(None, None)

    emitter.record_crossing(
        step_kind="knowledge_read",
        outcome="succeeded",
        owner_workspace="context-hub",
        owner_object_type="topic",
        owner_object_id="top_1",
    )
    crossing = emitter.drain_crossings()[0]
    assert crossing["owner_workspace"] == "context-hub"
    assert crossing["owner_object_id"] == "top_1"


def test_a_swallowed_failure_is_counted_so_silence_can_be_told_from_absence():
    """The counterpart this module had no counterpart for.

    "Recording never breaks the observed" is right, and it left a broken
    recorder and an idle one producing the same thing at every layer above: an
    absent path, a `200 {"paths": []}`, a screen saying "No AI Path recorded
    yet". `context-hub.md` refuses exactly that shape for the sibling store --
    "an unavailable context store is indistinguishable from an empty one".
    """
    import core.ai_path_recorder as mod

    before = mod.recording_failures()
    mod._swallowed("crossing not recorded", "CheckViolation")
    assert mod.recording_failures() == before + 1
    # And it still does not raise: the swallow is the point.
    mod._swallowed("not recorded for tool=x", "InFailedSqlTransaction")
    assert mod.recording_failures() == before + 2


def test_the_health_envelope_carries_the_recording_failure_count():
    """A screen nobody opens is not a signal.

    The path list already carries this count, because that is where a reader
    confuses "nothing happened" with "nothing could be written". It is repeated
    on `health` because that envelope is where this deployment's other
    process-local operational facts live -- `quota` breaker states and
    `mirror_sync` lag -- and a recorder that has stopped recording is one of
    them. Without it, the only witness of a silent recorder is a log line.
    """
    import core.ai_path_recorder as mod
    from core.main import health

    before = mod.recording_failures()
    mod._swallowed("crossing not recorded", "CheckViolation")

    envelope = health(project_id="proj_EXAMPLE")
    reported = envelope["data"]["ai_path_recording"]["failures_this_instance"]
    assert reported == before + 1
    # The key names its own limitation: it counts for THIS instance, because a
    # counter that needed a write would fail exactly when recording fails.
    assert "this_instance" in str(envelope["data"]["ai_path_recording"])


# --- Story 45.7 : le pas de Skill franchi ----------------------------------


def _serve(steps, *, version=7, project_id="proj_EXAMPLE"):
    from core import skill_steps

    skill_steps.forget_served()
    skill_steps.remember_served(
        TRACE, procedure_id="proc_EXAMPLE", version_number=version, steps=steps,
        project_id=project_id,
    )


_DECLARED = [
    {"step": 1, "action": "read", "label": "Read the target", "target": "docs/"},
    {"step": 2, "action": "run", "label": "Read the report", "tool": "get_daily_report"},
]


def test_a_declared_tool_records_the_skill_step_before_the_call_that_crosses_it(
    monkeypatch,
) -> None:
    """`skill_step` etait un genre de pas que rien n'ecrivait. Il precede le
    `tool_call` parce qu'il le CONTIENT : franchir le pas 2 consiste a appeler
    cet outil."""
    rec = _Recorder().install(monkeypatch)
    monkeypatch.setattr("core.skill_steps.describes_a_step", lambda conn, **kw: True)
    _serve(_DECLARED)

    record_tool_call(
        tool_name="get_daily_report",
        arguments={"project_id": "proj_EXAMPLE"},
        meta={"traceparent": PARENT},
        outcome="succeeded",
        actor="person_1",
    )

    kinds = [step["step_kind"] for step in rec.steps]
    assert kinds == ["skill_step", "tool_call"]
    skill_step = rec.steps[0]
    assert skill_step["skill_version_id"] == "proc_EXAMPLE@7"
    assert skill_step["skill_step_id"] == "2"
    assert skill_step["owner_object_type"] == "procedure"
    assert skill_step["owner_object_id"] == "proc_EXAMPLE"
    assert skill_step["owner_workspace"] == "context-hub"


def test_a_step_the_served_version_no_longer_declares_is_not_recorded(monkeypatch) -> None:
    """La memoire de session dit ce qui a ete servi ; la base dit ce qui existe.
    Les deux sont exigees -- une trace qui epingle un pas inexistant rendrait la
    comparaison attendu/observe muette au moment ou elle compte."""
    rec = _Recorder().install(monkeypatch)
    monkeypatch.setattr("core.skill_steps.describes_a_step", lambda conn, **kw: False)
    _serve(_DECLARED)

    record_tool_call(
        tool_name="get_daily_report",
        arguments={"project_id": "proj_EXAMPLE"},
        meta={"traceparent": PARENT},
        outcome="succeeded",
        actor="person_1",
    )

    assert [step["step_kind"] for step in rec.steps] == ["tool_call"]


def test_a_call_no_skill_declared_records_only_the_call(monkeypatch) -> None:
    """Le controle negatif : rien ne change pour un agent qui n'a pris aucune
    Skill -- c'est le cas de la quasi-totalite des appels d'aujourd'hui."""
    rec = _Recorder().install(monkeypatch)
    from core import skill_steps

    skill_steps.forget_served()

    record_tool_call(
        tool_name="get_daily_report",
        arguments={"project_id": "proj_EXAMPLE"},
        meta={"traceparent": PARENT},
        outcome="succeeded",
        actor="person_1",
    )

    assert [step["step_kind"] for step in rec.steps] == ["tool_call"]


def test_a_skill_served_for_another_project_is_never_pinned_here(monkeypatch) -> None:
    """Sortie de relecture du 2026-08-05 : la memoire ne portait ni projet ni
    organisation. Une trace qui prend une Skill du projet A puis appelle un outil
    du projet B ecrivait, dans le chemin de B, une reference a la Skill de A --
    un identifiant d'un autre locataire dans le magasin de preuves."""
    rec = _Recorder().install(monkeypatch)
    monkeypatch.setattr("core.skill_steps.describes_a_step", lambda conn, **kw: True)
    _serve(_DECLARED, project_id="proj_OTHER")

    record_tool_call(
        tool_name="get_daily_report",
        arguments={"project_id": "proj_EXAMPLE"},
        meta={"traceparent": PARENT},
        outcome="succeeded",
        actor="person_1",
    )

    assert [step["step_kind"] for step in rec.steps] == ["tool_call"]


def test_two_skills_in_one_trace_do_not_pick_for_each_other(monkeypatch) -> None:
    """Le tirage au sort que le module refuse DANS une Skill ne doit pas se faire
    ENTRE deux : la seconde ecrasait la premiere, et un outil declare par les
    deux etait epingle a la derniere servie."""
    from core import skill_steps

    rec = _Recorder().install(monkeypatch)
    monkeypatch.setattr("core.skill_steps.describes_a_step", lambda conn, **kw: True)
    skill_steps.forget_served()
    shared = [{"step": 1, "action": "run", "label": "x", "tool": "get_daily_report"}]
    for procedure in ("proc_A", "proc_B"):
        skill_steps.remember_served(
            TRACE, procedure_id=procedure, version_number=1, steps=shared,
            project_id="proj_EXAMPLE",
        )

    record_tool_call(
        tool_name="get_daily_report",
        arguments={"project_id": "proj_EXAMPLE"},
        meta={"traceparent": PARENT},
        outcome="succeeded",
        actor="person_1",
    )

    assert [step["step_kind"] for step in rec.steps] == ["tool_call"]


def test_the_producer_and_the_consumer_read_the_same_identifier(monkeypatch) -> None:
    """LE FINDING QUI RENDAIT LA STORY INERTE. Le producteur lisait l'id OTel,
    absent tant que `TRACING_ENABLED` est faux -- son defaut ; le consommateur
    lisait le `traceparent` du client. Les deux lisent desormais le meme champ,
    et il ne depend d'aucun reglage."""
    from core import ai_path_recorder, tracing

    # `monkeypatch.delenv`, never `os.environ.pop`: pytest restores the lever at
    # the end of the test. A bare pop decides tracing for every module collected
    # after this one, and the module that trips over it reports it as its own
    # failure (AI-291).
    monkeypatch.delenv("TRACING_ENABLED", raising=False)
    assert tracing.current_trace_id_hex() is None  # le defaut, mesure

    token = ai_path_recorder._ACTIVE_TRACE.set(
        ai_path_recorder._trace_id_of({"traceparent": PARENT})
    )
    try:
        assert ai_path_recorder.current_call_trace_id() == TRACE
    finally:
        ai_path_recorder._ACTIVE_TRACE.reset(token)


def test_a_tool_call_step_records_the_choice_it_carried(monkeypatch) -> None:
    """2026-09-05 -- Jean: « quelle solution tu as choisie ». Measured before: every
    tool_call step carried `detail: null`. The arguments' ids and short values
    travel as the step's detail, nested to depth three, lists of choices as one
    list per inner key; prose and payloads stay out."""
    rec = _Recorder().install(monkeypatch)
    record_tool_call(
        tool_name="compose_analyze_pivot",
        arguments={
            "project_id": "proj_EXAMPLE",
            "family": "table",
            "request": {
                "members": [
                    {"datastream_id": "ds_A", "measures": [{"canonical_field_id": "mdm_views", "aggregation": "sum"}]},
                    {"datastream_id": "ds_B", "measures": []},
                ],
                "pivot": {"rows": ["mdm_date"], "columns": ["mdm_channel"], "values": [{"datastream_id": "ds_A", "canonical_field_id": "mdm_views"}], "grand_total": "none"},
                "inclusion_policy": "matched_only",
            },
            "analysis": "a long prose that must never be recorded " * 3,
            "note": "x" * 500,
        },
        meta={"traceparent": PARENT},
        outcome="succeeded",
        actor="person_1",
    )
    detail = rec.steps[0]["detail"]
    assert detail["family"] == "table"
    assert detail["request.members[].datastream_id"] == ["ds_A", "ds_B"]
    assert detail["request.pivot.rows"] == ["mdm_date"] and detail["request.pivot.columns"] == ["mdm_channel"]
    assert detail["request.pivot.values[].canonical_field_id"] == ["mdm_views"]
    assert detail["request.inclusion_policy"] == "matched_only"
    assert "analysis" not in detail and "note" not in detail
    assert "project_id" not in detail  # the scope is not a choice
    assert "request.members[].measures[].canonical_field_id" not in detail  # depth three, no further


def test_choices_of_is_total_and_bounded() -> None:
    from core.ai_path_recorder import CHOICE_VALUE_MAX_CHARS, DETAIL_MAX_LIST_ITEMS, choices_of

    assert choices_of(None) is None and choices_of({}) is None
    assert choices_of({"ids": [f"id_{i}" for i in range(60)]})["ids"] == [f"id_{i}" for i in range(DETAIL_MAX_LIST_ITEMS)]
    assert choices_of({"name": "y" * (CHOICE_VALUE_MAX_CHARS + 1)}) is None
    assert choices_of({"flag": True, "limit": 12, "query": "which pages convert"}) == {"flag": True, "limit": 12, "query": "which pages convert"}


def test_a_tool_call_step_reaches_the_object_its_arguments_name(monkeypatch) -> None:
    """2026-09-05: « Reached: nothing governed » on a call that executed a plan
    version. The step's owner is the most specific object the arguments name;
    a version id is resolved to its parent object once, on the connection."""
    from unittest.mock import MagicMock

    rec = _Recorder().install(monkeypatch)
    conn = MagicMock()
    conn.__enter__.return_value = conn  # `with get_connection() as conn` hands back this double
    conn.cursor.return_value.__enter__.return_value.fetchone.return_value = ("qs_parent",)
    conn.transaction.return_value.__enter__.return_value = None
    monkeypatch.setattr("core.db.get_connection", lambda *a, **k: conn)
    record_tool_call(
        tool_name="execute_analyze_query_spec",
        arguments={"project_id": "proj_EXAMPLE", "query_spec_version_id": "qsv_1"},
        meta={"traceparent": PARENT},
        outcome="succeeded",
        actor="person_1",
    )
    step = rec.steps[-1]
    assert (step["owner_workspace"], step["owner_object_type"]) == ("analyze", "query-spec")
    assert (step["owner_object_id"], step["owner_version_id"]) == ("qs_parent", "qsv_1")


def test_owner_from_choices_prefers_the_most_specific_object_and_never_guesses() -> None:
    from unittest.mock import MagicMock

    from core.ai_path_recorder import owner_from_choices

    conn = MagicMock()
    # A Result outranks the plan it came from; no lookup is needed for it.
    owner = owner_from_choices(conn, {"result_id": "qr_1", "query_spec_version_id": "qsv_1"})
    assert owner == {"owner_workspace": "analyze", "owner_object_type": "result", "owner_object_id": "qr_1", "owner_version_id": None}
    # An intent's canonical field one level down is found; a carrier's datastream is not preferred to it.
    owner = owner_from_choices(conn, {"intent": {"canonical_field_id": "mdm_1", "carriers": [{"datastream_id": "ds_1", "column": "views"}]}})
    assert owner["owner_object_type"] == "canonical-field" and owner["owner_object_id"] == "mdm_1"
    # A version whose parent cannot be resolved leaves NO owner rather than a guessed one.
    conn.cursor.return_value.__enter__.return_value.fetchone.return_value = None
    conn.transaction.return_value.__enter__.return_value = None
    assert owner_from_choices(conn, {"semantic_view_version_id": "svv_ghost"}) is None
    assert owner_from_choices(conn, {"project_id": "proj_x"}) is None


def test_a_path_opened_under_a_served_skill_pins_its_expectation(monkeypatch) -> None:
    """2026-09-05: every path pinned `{recorded_by}` and read `unverifiable`; a trace that
    took a Skill pins the served version's tool-bearing steps, required ones flagged."""
    from core import skill_steps

    skill_steps.forget_served()
    rec = _Recorder().install(monkeypatch)
    skill_steps.remember_served(
        TRACE, procedure_id="proc_x", version_number=3, project_id="proj_EXAMPLE",
        steps=[
            {"step": 1, "tool": "execute_analyze_query_spec", "required": True},
            {"step": 2, "target": "the catalogue"},
            {"step": 3, "tool": "render_analyze_result"},
        ],
    )
    try:
        record_tool_call(
            tool_name="discover_analyze_matches", arguments={"project_id": "proj_EXAMPLE"},
            meta={"traceparent": PARENT}, outcome="succeeded", actor="person_1",
        )
    finally:
        skill_steps.forget_served()
    snapshot = rec.begun[0]["policy_snapshot"]
    assert snapshot["recorded_by"] == "mcp_tool_middleware"
    assert snapshot["expected_skill_steps"] == [
        {"skill_version": "proc_x@3", "step": "1", "tool": "execute_analyze_query_spec", "required": True},
        {"skill_version": "proc_x@3", "step": "3", "tool": "render_analyze_result", "required": False},
    ]
    assert snapshot["ordered_skill_steps"] is True


def test_a_trace_without_a_skill_pins_the_bare_policy(monkeypatch) -> None:
    from core import skill_steps

    skill_steps.forget_served()
    rec = _Recorder().install(monkeypatch)
    record_tool_call(
        tool_name="get_daily_report", arguments={"project_id": "proj_EXAMPLE"},
        meta={"traceparent": PARENT}, outcome="succeeded", actor="person_1",
    )
    assert rec.begun[0]["policy_snapshot"] == {"recorded_by": "mcp_tool_middleware"}


def test_a_walk_is_fulfilled_over_the_interaction_not_one_path() -> None:
    """Finding 2: the Skill's first required step records on the Result's own path; read on
    one path the walk never closed. The crossed steps are read over the trace."""
    from unittest.mock import MagicMock

    from core import skill_steps
    from core.ai_path_recorder import _walk_fulfilled

    skill_steps.forget_served()
    skill_steps.remember_served(
        "t" * 32, procedure_id="proc_x", version_number=1, project_id="p",
        steps=[{"step": 1, "tool": "execute_analyze_query_spec", "required": True},
               {"step": 2, "tool": "analyze_result", "required": True},
               {"step": 3, "tool": "render_analyze_result"}],
    )
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    try:
        cursor.fetchall.return_value = [("proc_x@1", "1"), ("proc_x@1", "2")]
        assert _walk_fulfilled(conn, path_id="aip_interaction", project_id="p", trace_id="t" * 32) is True
        sql = cursor.execute.call_args[0][0]
        assert "w3c_trace_id" in sql and "JOIN app.ai_paths" in sql
        cursor.fetchall.return_value = [("proc_x@1", "1")]
        assert _walk_fulfilled(conn, path_id="aip_interaction", project_id="p", trace_id="t" * 32) is False
        assert _walk_fulfilled(conn, path_id="aip_interaction", project_id="p", trace_id=None) is False
    finally:
        skill_steps.forget_served()




def _closure_harness(monkeypatch, *, crossed_before: list[tuple[str, str]]):
    """A trace that took a Skill with two required steps; the recorder's connection answers
    `crossed_before` for the interaction's earlier crossings; finalize is recorded, never run."""
    from unittest.mock import MagicMock

    from core import ai_path_recorder, skill_steps

    skill_steps.forget_served()
    rec = _Recorder().install(monkeypatch)
    skill_steps.remember_served(
        TRACE, procedure_id="proc_x", version_number=2, project_id="proj_EXAMPLE",
        steps=[{"step": 1, "tool": "execute_analyze_query_spec", "required": True},
               {"step": 2, "tool": "analyze_result", "required": True},
               {"step": 3, "tool": "render_analyze_result"}],
    )
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.cursor.return_value.__enter__.return_value.fetchall.return_value = crossed_before
    conn.cursor.return_value.__enter__.return_value.fetchone.return_value = (0,)
    conn.transaction.return_value.__enter__.return_value = None
    monkeypatch.setattr("core.db.get_connection", lambda *a, **k: conn)
    monkeypatch.setattr(ai_path_recorder, "owner_from_choices", lambda conn, arguments: None)
    finalized: list[dict] = []
    monkeypatch.setattr("core.ai_paths.finalize_path", lambda conn, *, path_id, project_id, outcome: finalized.append({"path_id": path_id, "outcome": outcome}) or {"id": path_id})
    return rec, finalized


def _crossing(monkeypatch, step: str):
    from core import ai_path_recorder

    monkeypatch.setattr(ai_path_recorder, "_skill_step_of", lambda conn, *, trace_id, tool_name, project_id=None: ("proc_x@2", step, "proc_x"))


def test_the_call_that_crosses_the_last_missing_required_step_closes_the_walk(monkeypatch) -> None:
    """Round 2, finding 8: a closure held by a test that dies under mutation."""
    from core import skill_steps

    rec, finalized = _closure_harness(monkeypatch, crossed_before=[("proc_x@2", "1")])
    _crossing(monkeypatch, "2")
    try:
        record_tool_call(tool_name="analyze_result", arguments={"project_id": "proj_EXAMPLE", "result_id": "qr_1"},
                         meta={"traceparent": PARENT}, outcome="succeeded", actor="person_1")
    finally:
        skill_steps.forget_served()
    assert finalized == [{"path_id": "aip_new", "outcome": "succeeded"}]


def test_a_crossing_after_the_contract_was_kept_closes_nothing(monkeypatch) -> None:
    """The interaction already crossed both required steps: a retry of step 1 completes nothing."""
    from core import skill_steps

    _rec, finalized = _closure_harness(monkeypatch, crossed_before=[("proc_x@2", "1"), ("proc_x@2", "2")])
    _crossing(monkeypatch, "1")
    try:
        record_tool_call(tool_name="execute_analyze_query_spec", arguments={"project_id": "proj_EXAMPLE", "query_spec_version_id": "qsv_1"},
                         meta={"traceparent": PARENT}, outcome="succeeded", actor="person_1")
    finally:
        skill_steps.forget_served()
    assert finalized == []


def test_a_crossing_of_an_optional_step_or_no_step_closes_nothing(monkeypatch) -> None:
    from core import ai_path_recorder, skill_steps

    _rec, finalized = _closure_harness(monkeypatch, crossed_before=[("proc_x@2", "1"), ("proc_x@2", "2")])
    _crossing(monkeypatch, "3")  # step 3 is expected, not required
    try:
        record_tool_call(tool_name="render_analyze_result", arguments={"project_id": "proj_EXAMPLE", "result_id": "qr_1"},
                         meta={"traceparent": PARENT}, outcome="succeeded", actor="person_1")
        monkeypatch.setattr(ai_path_recorder, "_skill_step_of", lambda conn, *, trace_id, tool_name, project_id=None: None)
        record_tool_call(tool_name="get_ai_path", arguments={"project_id": "proj_EXAMPLE", "limit": 5},
                         meta={"traceparent": PARENT}, outcome="succeeded", actor="person_1")
    finally:
        skill_steps.forget_served()
    assert finalized == []
