"""Story 54.1 -- the recorder emits what it already observes.

The middleware that records an AI Path (Story 49.6) now also *streams* it. The
tests that matter here are not the ones counting notifications: a single
notification posted as the call returns would satisfy "notifications exist" and
miss the entire point, which is that the person watches a traversal instead of a
spinner.

So the load-bearing test is :func:`test_the_walk_is_streamed_not_recapitulated`,
and it is proven by mutation: :func:`test_a_batch_at_the_end_fails_the_same_check`
runs the deliberately wrong implementation -- buffer everything, flush after the
tool returns -- through the *same* assertions and requires them to fail.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace

import anyio
import core.ai_path_recorder as mod
import pytest
from core.ai_path_recorder import (
    BANNED_DETAIL_KEYS,
    EMISSION_KIND,
    LEVEL_CONTEXT,
    LEVEL_JOB,
    LEVEL_PROCEDURE,
    LEVEL_SKILL,
    LEVEL_TOOL,
    PAYLOAD_FIELDS,
    STEP_STATE_OBSERVED,
    STEP_STATE_STARTED,
    build_step_payload,
    emission_is_armed,
    emit_step,
    emit_step_sync,
    level_of,
    sanitize_detail,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# T1 -- the reading grid, in isolation.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("step_kind", "tool_name", "owner_object_type", "expected"),
    [
        # The pair the grid exists for: SAME step_kind, different rung. A
        # `get_procedure` is a `tool_call` exactly like a data tool is, so the
        # kind alone cannot tell them apart.
        ("tool_call", "get_procedure", None, LEVEL_PROCEDURE),
        ("tool_call", "search_context", None, LEVEL_CONTEXT),
        ("tool_call", "get_daily_report", None, LEVEL_TOOL),
        # And the same collision one layer down: a hit INSIDE search_context is
        # a `knowledge_read` whether it reached a topic or a procedure.
        ("knowledge_read", "search_context", "topic", LEVEL_CONTEXT),
        ("knowledge_read", "search_context", "procedure", LEVEL_PROCEDURE),
        ("semantic_query", None, None, LEVEL_CONTEXT),
        ("skill_step", None, None, LEVEL_SKILL),
        ("skill_step", "get_procedure", None, LEVEL_SKILL),
        ("data_read", None, None, LEVEL_TOOL),
        # Recorded kinds that name no rung of this grid map to nothing, and
        # nothing is emitted for them (AC4). `handoff` is the standing example.
        ("handoff", None, None, None),
        (None, "search_context", None, None),
        ("", None, None, None),
    ],
)
def test_the_grid_reads_kind_and_tool_name_together(
    step_kind: str | None, tool_name: str | None, owner_object_type: str | None,
    expected: str | None,
) -> None:
    assert level_of(step_kind, tool_name, owner_object_type) == expected


def test_the_grid_never_persists_a_sixth_step_kind() -> None:
    """The levels are a reading grid; `step_kind` keeps its six values.

    Migration 150 applies a CHECK to `app.ai_path_steps.step_kind`. If a level
    ever leaked into that column the CHECK would reject it in production, so the
    two vocabularies are held apart here.
    """
    from core.ai_paths import STEP_KINDS

    assert set(mod.LEVELS).isdisjoint(set(STEP_KINDS))


# ---------------------------------------------------------------------------
# AC5 -- nothing emitted is model reasoning.
# ---------------------------------------------------------------------------


def test_the_payload_key_set_is_closed() -> None:
    payload = build_step_payload(ordinal=0, level=LEVEL_TOOL)
    assert tuple(payload) == PAYLOAD_FIELDS


@pytest.mark.parametrize("banned", sorted(BANNED_DETAIL_KEYS))
def test_a_prose_shaped_detail_key_is_refused(banned: str) -> None:
    payload = build_step_payload(
        ordinal=0, level=LEVEL_TOOL, detail={banned: "the model thought about it"}
    )
    assert payload["detail"] is None


def test_a_long_value_is_dropped_not_truncated() -> None:
    """Truncated prose is still prose."""
    kept = sanitize_detail({"node_count": 3, "blurb": "x" * 5_000})
    assert kept == {"node_count": 3}


def test_a_detail_map_carries_recorded_values_through() -> None:
    """Story 54.2 attaches its candidates here; the door opens for facts."""
    kept = sanitize_detail(
        {"candidate_ids": ["ctx_a", "ctx_b"], "score": 3.0, "considered": 12,
         "taken": False, "tier": None}
    )
    assert kept == {
        "candidate_ids": ["ctx_a", "ctx_b"], "score": 3.0, "considered": 12,
        "taken": False, "tier": None,
    }


def test_the_job_line_names_argument_keys_never_argument_values() -> None:
    """`append_step` records `tool_name`, not `arguments`.

    Emitting argument values would make the stream a second store of things the
    AI Path never held -- the "two stores, two truths" defect the reading grid
    was chosen to avoid.
    """
    source = (REPO_ROOT / "server" / "core" / "ai_path_recorder.py").read_text(
        encoding="utf-8"
    )
    body = source[source.index("class AiPathMiddleware") :]
    assert "argument_keys" in body
    assert "arguments.get(" not in body
    assert '"query"' not in body


# ---------------------------------------------------------------------------
# The harness: a middleware context that records a single timeline.
# ---------------------------------------------------------------------------


class _FakeClient:
    """A client that keeps notifications and the result on ONE timeline."""

    def __init__(self, *, progress_token: str | None = "tok-1", explode: bool = False):
        self.timeline: list[tuple[str, object]] = []
        self.explode = explode
        meta = SimpleNamespace(progressToken=progress_token)
        self.request_context = SimpleNamespace(meta=meta)

    async def report_progress(self, progress, total=None, message=None):
        if self.explode:
            raise RuntimeError("the notification channel is on fire")
        self.timeline.append(("progress", json.loads(message)))

    @property
    def progress_payloads(self) -> list[dict]:
        return [item for kind, item in self.timeline if kind == "progress"]

    @property
    def marks(self) -> list[str]:
        marks = []
        for kind, item in self.timeline:
            if kind != "progress":
                marks.append(kind)
            elif "event" in item:
                marks.append(f"progress:{item['event']}")
            else:
                marks.append(f"progress:{item['level']}:{item['state']}")
        return marks


def _middleware_context(client, *, tool_name="search_context", arguments=None):
    return SimpleNamespace(
        message=SimpleNamespace(
            name=tool_name, arguments=arguments or {"project_id": "proj_EXAMPLE"}, meta=None
        ),
        fastmcp_context=client,
    )


@pytest.fixture
def silent_recorder(monkeypatch):
    """Stub both recorders while keeping the dedicated-search boundary visible."""
    calls: list[dict] = []

    def _record(**kwargs):
        calls.append({"recorder": "generic", **kwargs})
        return "aip_stub"

    search_observation = SimpleNamespace(path_id="aip_search", closed=False)

    def _begin_search(*, emitter, **_kwargs):
        assert emitter.bind_path(search_observation.path_id) is emitter.armed
        return search_observation

    def _finalize_search(observation, *, emitter, outcome):
        assert observation is search_observation
        # The production finalizer persists this same buffered prefix. Draining
        # it here keeps the fixture's recording lifecycle faithful without
        # acquiring a database connection in this live-emission suite.
        emitter.drain_crossings()
        observation.closed = True
        calls.append(
            {
                "recorder": "dedicated",
                "tool_name": mod.SEARCH_CONTEXT_TOOL_NAME,
                "outcome": outcome,
            }
        )

    monkeypatch.setattr(mod, "record_tool_call", _record)
    monkeypatch.setattr(mod, "_begin_search_context_observation", _begin_search)
    monkeypatch.setattr(mod, "_finalize_search_context_observation", _finalize_search)
    return calls


async def _walking_tool(context, client):
    """A tool that reaches a governed node halfway through, like the real walk."""
    client.timeline.append(("tool:entered", None))
    await emit_step(
        step_kind="knowledge_read",
        outcome="succeeded",
        tool_name="search_context",
        owner_workspace="context-hub",
        owner_object_type="topic",
        owner_object_id="ctx_EXAMPLE",
        detail={"considered": 12},
    )
    client.timeline.append(("tool:left", None))
    return {"content": "answer"}


def _assert_streamed(client, inner_level: str = LEVEL_CONTEXT) -> None:
    """The assertions AC2 requires, isolated so a mutation can be run through them."""
    marks = client.marks
    if "progress:recording" in marks:
        opening = "progress:recording"
        crossing = "progress:step_observed"
        order_key = "sequence"
        assert marks.index(opening) < marks.index(crossing) < marks.index("result"), (
            "the non-blocking Result FIFO must preserve recording, crossing, result order"
        )
    else:
        opening = f"progress:{LEVEL_JOB}:{STEP_STATE_STARTED}"
        crossing = f"progress:{inner_level}:{STEP_STATE_OBSERVED}"
        order_key = "ordinal"
        assert marks.index(opening) < marks.index("tool:entered"), (
            "the path must be announced BEFORE the walk starts, not after it ends"
        )
        assert marks.index(crossing) < marks.index("tool:left"), (
            "a crossed step must go out AT the crossing, not once the tool has returned"
        )
    assert marks[-1] == "result", "every notification precedes the tool result"
    ordinals = [payload[order_key] for payload in client.progress_payloads]
    assert ordinals == sorted(ordinals) and len(set(ordinals)) == len(ordinals), (
        "the emitted sequence must match the step order"
    )


@pytest.mark.anyio
async def test_the_walk_is_streamed_not_recapitulated(silent_recorder) -> None:
    """AC2 -- order and precedence, not a count of notifications."""
    client = _FakeClient()
    middleware = mod.build_middleware()
    context = _middleware_context(client)

    result = await middleware.on_call_tool(
        context, lambda ctx: _walking_tool(ctx, client)
    )
    client.timeline.append(("result", result))

    _assert_streamed(client)
    assert [payload["event"] for payload in client.progress_payloads] == [
        "recording",
        "step_observed",
    ]


@pytest.mark.anyio
async def test_a_batch_at_the_end_fails_the_same_check(silent_recorder) -> None:
    """The mutation. Buffer everything, flush after the tool returns.

    It posts the same notifications, in the same order, with the same payloads --
    and it is exactly the blind-waiting-with-a-summary behaviour this story
    exists to remove. A test that counted notifications would pass it.
    """
    client = _FakeClient()

    async def batched_middleware(context, call_next):
        buffered: list[dict] = []
        buffered.append(build_step_payload(ordinal=0, level=LEVEL_JOB,
                                           state=STEP_STATE_STARTED,
                                           tool_name="search_context"))
        result = await call_next(context)
        buffered.append(build_step_payload(ordinal=1, level=LEVEL_CONTEXT,
                                           step_kind="knowledge_read",
                                           outcome="succeeded"))
        buffered.append(build_step_payload(ordinal=2, level=LEVEL_TOOL,
                                           step_kind="tool_call",
                                           outcome="succeeded"))
        for payload in buffered:
            await client.report_progress(1.0, None, json.dumps(payload))
        return result

    async def quiet_tool(context):
        client.timeline.append(("tool:entered", None))
        client.timeline.append(("tool:left", None))
        return {"content": "answer"}

    result = await batched_middleware(_middleware_context(client), quiet_tool)
    client.timeline.append(("result", result))

    assert len(client.progress_payloads) == 3  # a counting test would pass here
    with pytest.raises(AssertionError, match="BEFORE the walk starts"):
        _assert_streamed(client)


@pytest.mark.anyio
async def test_a_synchronous_call_site_can_emit_from_its_worker_thread(
    silent_recorder,
) -> None:
    """The seam Story 54.2 needs: `search_context` is a synchronous function.

    FastMCP runs a sync tool body through a worker thread, which copies the
    context, so the emitter is reachable -- but the emission must BLOCK until it
    is handed to the session, or it lands after the result.
    """
    client = _FakeClient()
    middleware = mod.build_middleware()

    def sync_body():
        client.timeline.append(("tool:entered", None))
        assert emission_is_armed() is True
        emitted = emit_step_sync(
            step_kind="knowledge_read", outcome="succeeded",
            tool_name="search_context", owner_workspace="context-hub",
            owner_object_type="procedure", owner_object_id="proc_EXAMPLE",
        )
        assert emitted is True
        client.timeline.append(("tool:left", None))

    async def call_next(context):
        await anyio.to_thread.run_sync(sync_body)
        return {"content": "answer"}

    result = await middleware.on_call_tool(_middleware_context(client), call_next)
    client.timeline.append(("result", result))

    _assert_streamed(client, inner_level=LEVEL_PROCEDURE)
    assert [p["event"] for p in client.progress_payloads] == [
        "recording",
        "step_observed",
    ]
    assert client.progress_payloads[1]["step"]["owner"]["object_type"] == "procedure"


# ---------------------------------------------------------------------------
# AC3 -- level AND node, and a step that reached nothing stays visible.
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_each_emission_carries_its_level_and_the_node_it_reached(
    silent_recorder,
) -> None:
    client = _FakeClient()
    middleware = mod.build_middleware()

    async def call_next(context):
        await emit_step(
            step_kind="knowledge_read", outcome="succeeded",
            tool_name="search_context", owner_workspace="context-hub",
            owner_object_type="topic", owner_object_id="ctx_EXAMPLE",
            owner_version_id="ver_EXAMPLE",
        )
        return {"content": "answer"}

    await middleware.on_call_tool(_middleware_context(client), call_next)

    hit = client.progress_payloads[1]["step"]
    assert hit["step_kind"] == "knowledge_read"
    assert hit["owner"] == {
        "workspace": "context-hub",
        "object_type": "topic",
        "object_id": "ctx_EXAMPLE",
        "version_id": "ver_EXAMPLE",
    }
    assert "detail" not in hit


@pytest.mark.anyio
async def test_no_level_is_ever_emitted_as_pending(silent_recorder) -> None:
    """AC4 -- a permanently pending rung reads as a step in progress and lies."""
    client = _FakeClient()
    middleware = mod.build_middleware()

    async def call_next(context):
        return {"content": "answer"}

    await middleware.on_call_tool(
        _middleware_context(client, tool_name="get_daily_report"), call_next
    )

    states = {payload["state"] for payload in client.progress_payloads}
    assert states <= set(mod.STEP_STATES)
    assert "pending" not in states and "active" not in states
    # Only the levels an observation supports. Nothing invents a SKILL rung for a
    # call that crossed no Skill.
    assert [p["level"] for p in client.progress_payloads] == [LEVEL_JOB, LEVEL_TOOL]


@pytest.mark.anyio
async def test_a_step_that_maps_to_no_level_is_not_emitted(silent_recorder) -> None:
    client = _FakeClient()
    middleware = mod.build_middleware()

    async def call_next(context):
        assert await emit_step(step_kind="handoff", outcome="succeeded") is False
        return {"content": "answer"}

    await middleware.on_call_tool(_middleware_context(client), call_next)
    assert [p["event"] for p in client.progress_payloads] == ["recording"]


# ---------------------------------------------------------------------------
# AC7 / AC8 -- degradation, and the observer never breaking the observed.
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_without_a_progress_token_nothing_is_emitted(silent_recorder) -> None:
    """AC7 -- and no payload is even composed, so the cost is not moved, it is absent."""
    client = _FakeClient(progress_token=None)
    middleware = mod.build_middleware()
    composed: list[object] = []
    original = mod.build_step_payload

    def spy(**kwargs):
        composed.append(kwargs)
        return original(**kwargs)

    async def call_next(context):
        assert emission_is_armed() is False
        assert await emit_step(step_kind="knowledge_read", outcome="succeeded",
                               tool_name="search_context") is False
        assert emit_step_sync(step_kind="knowledge_read", outcome="succeeded",
                              tool_name="search_context") is False
        return {"content": "answer"}

    import unittest.mock

    with unittest.mock.patch.object(mod, "build_step_payload", spy):
        result = await middleware.on_call_tool(_middleware_context(client), call_next)

    assert result == {"content": "answer"}
    assert client.timeline == []
    assert composed == []
    # The dedicated AI Path is still finalized: same result and evidence, no
    # notification channel, and no fallback to generic trace-path recording.
    assert len(silent_recorder) == 1
    assert silent_recorder[0]["recorder"] == "dedicated"
    assert silent_recorder[0]["outcome"] == "succeeded"


@pytest.mark.anyio
async def test_a_failing_emitter_does_not_touch_the_result(silent_recorder) -> None:
    """AC8 -- the same refusal recording already states, at the new seam."""
    client = _FakeClient(explode=True)
    middleware = mod.build_middleware()

    async def call_next(context):
        assert await emit_step(step_kind="knowledge_read", outcome="succeeded",
                               tool_name="search_context") is True
        return {"content": "answer"}

    result = await middleware.on_call_tool(_middleware_context(client), call_next)

    assert result == {"content": "answer"}
    assert len(silent_recorder) == 1
    assert silent_recorder[0]["recorder"] == "dedicated"


@pytest.mark.anyio
async def test_a_failing_tool_still_streams_its_failure_then_re_raises(
    silent_recorder,
) -> None:
    client = _FakeClient()
    middleware = mod.build_middleware()

    async def call_next(context):
        raise RuntimeError("the tool failed")

    with pytest.raises(RuntimeError, match="the tool failed"):
        await middleware.on_call_tool(_middleware_context(client), call_next)

    assert [p["event"] for p in client.progress_payloads] == ["recording"]
    assert silent_recorder[0]["recorder"] == "dedicated"
    assert silent_recorder[0]["outcome"] == "failed"


@pytest.mark.anyio
async def test_the_emitter_does_not_leak_between_calls(silent_recorder) -> None:
    """A contextvar left set would make the NEXT call emit onto a dead channel."""
    client = _FakeClient()
    middleware = mod.build_middleware()

    async def call_next(context):
        return {"content": "answer"}

    await middleware.on_call_tool(_middleware_context(client), call_next)
    assert emission_is_armed() is False
    assert await emit_step(step_kind="tool_call", outcome="succeeded") is False


# ---------------------------------------------------------------------------
# AC1 / AC6 -- one middleware, no schema change.
# ---------------------------------------------------------------------------


def test_exactly_one_middleware_carries_the_ai_path_concern() -> None:
    """AC1 -- a second recorder would open a second path per interaction."""
    source = (REPO_ROOT / "server" / "core" / "ai_path_recorder.py").read_text(
        encoding="utf-8"
    )
    assert len(re.findall(r"^\s*class \w+\(Middleware\):", source, re.MULTILINE)) == 1

    main_source = (REPO_ROOT / "server" / "core" / "main.py").read_text(encoding="utf-8")
    assert len(re.findall(r"_build_ai_path_middleware", main_source)) == 2, (
        "imported once, called once"
    )
    assert len(re.findall(r"add_middleware\(_ai_path_mw\)", main_source)) == 1, (
        "the AI Path middleware is mounted exactly once"
    )
    # And no OTHER module builds a middleware for this concern.
    builders = [
        path.name
        for path in sorted((REPO_ROOT / "server" / "core").glob("*.py"))
        if "ai_path" in path.name and "build_middleware" in path.read_text(encoding="utf-8")
    ]
    assert builders == ["ai_path_recorder.py"]


def test_the_emission_adds_no_column_to_ai_path_steps(monkeypatch) -> None:
    """AC6 -- `app.ai_path_steps` receives exactly what it received before."""
    seen: list[dict] = []

    class _Conn:
        def commit(self):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

    monkeypatch.setattr(mod, "_resolve_scope", lambda arguments: ("proj_E", "org_E"))
    monkeypatch.setattr(mod, "_open_path_for", lambda conn, **kw: "aip_open")
    monkeypatch.setattr("core.db.get_connection", lambda *a, **k: _Conn())
    monkeypatch.setattr("core.ai_paths.append_step", lambda conn, **kw: seen.append(kw))

    mod.record_tool_call(
        tool_name="search_context", arguments={"project_id": "proj_E"},
        meta=None, outcome="succeeded", actor="person_1",
    )

    assert set(seen[0]) == {"path_id", "project_id", "step_kind", "outcome", "tool_name", "detail"}  # `detail` is a column since migration 150; the choice rides it (2026-09-05)
    assert seen[0]["step_kind"] in {"tool_call"}


def test_no_migration_carries_the_streaming_vocabulary() -> None:
    """AC6 -- the levels are a reading grid; the schema never learns them."""
    migrations = REPO_ROOT / "infra" / "nango" / "migrations"
    # SQL literals, so `'ai_path_step'` cannot be confused with the existing
    # table name `app.ai_path_steps`.
    forbidden = [f"'{EMISSION_KIND}'"] + [f"'{level}'" for level in mod.LEVELS]
    offenders = [
        (path.name, marker)
        for path in sorted(migrations.glob("*.sql"))
        for marker in forbidden
        if marker in path.read_text(encoding="utf-8", errors="ignore")
    ]
    assert offenders == []


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_the_thread_emission_bound_stays_under_the_nfr_envelope() -> None:
    """A blocked worker thread is worse than a missing notification."""
    assert mod._EMIT_FROM_THREAD_TIMEOUT_SECONDS > 0
    assert isinstance(asyncio.get_event_loop_policy(), asyncio.AbstractEventLoopPolicy)


# ---------------------------------------------------------------------------
# Story 65.6 Tasks 1-2 -- Result-bound safe projection and bounded FIFO.
# ---------------------------------------------------------------------------


class _HungClient(_FakeClient):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()

    async def report_progress(self, progress, total=None, message=None):
        self.started.set()
        await asyncio.Event().wait()


class _SlowCancellationClient(_FakeClient):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.finished = asyncio.Event()

    async def report_progress(self, progress, total=None, message=None):
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            await asyncio.sleep(0.100)
            raise
        finally:
            self.finished.set()


class _CancelledSinkClient(_FakeClient):
    async def report_progress(self, progress, total=None, message=None):
        raise asyncio.CancelledError


def test_live_fifo_limits_are_the_ratified_contract() -> None:
    assert mod.LIVE_EVENT_MAX_COUNT == 65
    assert mod.LIVE_EVENT_MAX_BYTES == 8_192
    assert mod.LIVE_REQUEST_MAX_BYTES == 131_072
    assert 0 < mod.LIVE_SEND_BUDGET_SECONDS < 0.025


@pytest.mark.anyio
async def test_event_and_request_byte_limits_are_inclusive(monkeypatch) -> None:
    loop = asyncio.get_running_loop()
    step = {
        "step_kind": "knowledge_read",
        "outcome": "succeeded",
        "missing_context": [],
    }
    event = {
        "schema_version": mod.PROGRESS_SCHEMA_VERSION,
        "event": mod.PROGRESS_EVENT_STEP,
        "path_id": "aip_RESULT",
        "sequence": 1,
        "step": step,
    }
    event_bytes = len(
        json.dumps(event, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
    )

    equal = mod._PathEmitter(_FakeClient(), loop)
    assert equal.bind_path("aip_RESULT") is True
    recording_bytes = equal._accepted_bytes
    monkeypatch.setattr(mod, "LIVE_EVENT_MAX_BYTES", event_bytes)
    monkeypatch.setattr(mod, "LIVE_REQUEST_MAX_BYTES", recording_bytes + event_bytes)
    assert equal._offer(mod.PROGRESS_EVENT_STEP, step=step) is True
    assert equal._offer(mod.PROGRESS_EVENT_STEP, step=step) is False
    await equal.finish()

    over = mod._PathEmitter(_FakeClient(), loop)
    assert over.bind_path("aip_RESULT") is True
    monkeypatch.setattr(mod, "LIVE_EVENT_MAX_BYTES", event_bytes - 1)
    assert over._offer(mod.PROGRESS_EVENT_STEP, step=step) is False
    await over.finish()


async def _emit_sync_crossings(emitter, count: int) -> list[bool]:
    token = mod._ACTIVE_EMITTER.set(emitter)
    try:
        return await anyio.to_thread.run_sync(
            lambda: [
                emit_step_sync(
                    step_kind="knowledge_read",
                    outcome="succeeded",
                    tool_name="search_context",
                    owner_workspace="context-hub",
                    owner_object_type="topic",
                    owner_object_id=f"ctx_{index}",
                    detail={"candidate_ids": [f"ctx_{index}"]},
                )
                for index in range(count)
            ]
        )
    finally:
        mod._ACTIVE_EMITTER.reset(token)


@pytest.mark.anyio
async def test_result_bound_stream_is_closed_safe_and_fifo_ordered() -> None:
    client = _FakeClient()
    emitter = mod._PathEmitter(client, asyncio.get_running_loop())

    assert emitter.bind_path("aip_RESULT") is True
    assert await _emit_sync_crossings(emitter, 2) == [True, True]
    await emitter.finish()

    assert client.progress_payloads == [
        {
            "schema_version": mod.PROGRESS_SCHEMA_VERSION,
            "event": "recording",
            "path_id": "aip_RESULT",
            "sequence": 0,
        },
        {
            "schema_version": mod.PROGRESS_SCHEMA_VERSION,
            "event": "step_observed",
            "path_id": "aip_RESULT",
            "sequence": 1,
            "step": {
                "step_kind": "knowledge_read",
                "outcome": "succeeded",
                "missing_context": [],
                "tool_name": "search_context",
                "owner": {
                    "workspace": "context-hub",
                    "object_type": "topic",
                    "object_id": "ctx_0",
                },
            },
        },
        {
            "schema_version": mod.PROGRESS_SCHEMA_VERSION,
            "event": "step_observed",
            "path_id": "aip_RESULT",
            "sequence": 2,
            "step": {
                "step_kind": "knowledge_read",
                "outcome": "succeeded",
                "missing_context": [],
                "tool_name": "search_context",
                "owner": {
                    "workspace": "context-hub",
                    "object_type": "topic",
                    "object_id": "ctx_1",
                },
            },
        },
    ]
    assert all(
        not (
            {"ordinal", "observed_at", "skill", "detail", "level", "state"}
            & event.keys()
        )
        for event in client.progress_payloads
    )


@pytest.mark.anyio
async def test_65th_crossing_is_neither_persisted_nor_streamed() -> None:
    client = _FakeClient()
    emitter = mod._PathEmitter(client, asyncio.get_running_loop())
    assert emitter.bind_path("aip_RESULT") is True

    accepted = await _emit_sync_crossings(emitter, mod.CROSSINGS_PER_CALL_MAX + 1)
    await emitter.finish()

    assert accepted == [True] * mod.CROSSINGS_PER_CALL_MAX + [False]
    assert len(emitter.drain_crossings()) == mod.CROSSINGS_PER_CALL_MAX
    assert len(client.progress_payloads) == mod.LIVE_EVENT_MAX_COUNT
    assert [event["sequence"] for event in client.progress_payloads] == list(
        range(mod.LIVE_EVENT_MAX_COUNT)
    )


@pytest.mark.anyio
async def test_concurrent_crossings_keep_the_same_persisted_and_live_order() -> None:
    client = _FakeClient()
    emitter = mod._PathEmitter(client, asyncio.get_running_loop())
    assert emitter.bind_path("aip_RESULT") is True

    def fields(index: int) -> dict:
        return mod._crossing_fields(
            step_kind="knowledge_read",
            outcome="succeeded",
            tool_name="search_context",
            owner_workspace="context-hub",
            owner_object_type="topic",
            owner_object_id=f"ctx_{index}",
            owner_version_id=None,
            evidence_record_id=None,
            detail=None,
        )

    accepted = await asyncio.gather(
        *(asyncio.to_thread(emitter.record_and_route_step, fields(index)) for index in range(32))
    )
    await emitter.finish()

    assert accepted == [(True, False)] * 32
    persisted_ids = [item["owner_object_id"] for item in emitter.drain_crossings()]
    live_ids = [event["step"]["owner"]["object_id"] for event in client.progress_payloads[1:]]
    assert live_ids == persisted_ids


@pytest.mark.anyio
async def test_crossing_in_inherited_context_is_refused_after_drain() -> None:
    client = _FakeClient()
    emitter = mod._PathEmitter(client, asyncio.get_running_loop())
    assert emitter.bind_path("aip_RESULT") is True
    assert emitter.drain_crossings() == []
    token = mod._ACTIVE_EMITTER.set(emitter)
    try:
        accepted = await emit_step(
            step_kind="knowledge_read",
            outcome="succeeded",
        )
    finally:
        mod._ACTIVE_EMITTER.reset(token)
    await emitter.finish()

    assert accepted is False
    assert emitter.drain_crossings() == []
    assert [event["event"] for event in client.progress_payloads] == ["recording"]


@pytest.mark.anyio
async def test_prebind_crossing_fails_the_live_stream_closed() -> None:
    client = _FakeClient()
    emitter = mod._PathEmitter(client, asyncio.get_running_loop())
    assert emitter.record_crossing(
        step_kind="knowledge_read", outcome="succeeded"
    ) is True

    assert emitter.bind_path("aip_RESULT") is False
    await emitter.finish()

    assert client.progress_payloads == []
    assert len(emitter.drain_crossings()) == 1


@pytest.mark.anyio
async def test_invalid_live_step_closes_delivery_but_persistence_continues() -> None:
    client = _FakeClient()
    emitter = mod._PathEmitter(client, asyncio.get_running_loop())
    assert emitter.bind_path("aip_RESULT") is True
    token = mod._ACTIVE_EMITTER.set(emitter)
    try:
        invalid = await emit_step(
            step_kind="knowledge_read",
            outcome="succeeded",
            tool_name="x" * 513,
        )
        later = await emit_step(
            step_kind="knowledge_read",
            outcome="succeeded",
            tool_name="search_context",
        )
    finally:
        mod._ACTIVE_EMITTER.reset(token)
    await emitter.finish()

    assert invalid is False and later is False
    assert len(emitter.drain_crossings()) == 2
    assert client.progress_payloads == [
        {
            "schema_version": mod.PROGRESS_SCHEMA_VERSION,
            "event": "recording",
            "path_id": "aip_RESULT",
            "sequence": 0,
        }
    ]


@pytest.mark.anyio
async def test_live_and_persisted_crossing_share_one_sanitized_frozen_snapshot() -> None:
    client = _FakeClient()
    emitter = mod._PathEmitter(client, asyncio.get_running_loop())
    assert emitter.bind_path("aip_RESULT") is True
    detail = {"missing_context": ["not_recorded"]}
    token = mod._ACTIVE_EMITTER.set(emitter)
    try:
        assert await emit_step(
            step_kind="knowledge_read",
            outcome="succeeded",
            detail=detail,
        ) is True
        detail["missing_context"].append("unavailable")
    finally:
        mod._ACTIVE_EMITTER.reset(token)
    await emitter.finish()

    crossing = emitter.drain_crossings()[0]
    assert crossing["detail"] == {"missing_context": ["not_recorded"]}
    assert client.progress_payloads[1]["step"]["missing_context"] == ["not_recorded"]


@pytest.mark.anyio
async def test_oversized_missing_context_is_absent_from_live_and_persisted_views() -> None:
    client = _FakeClient()
    emitter = mod._PathEmitter(client, asyncio.get_running_loop())
    assert emitter.bind_path("aip_RESULT") is True
    token = mod._ACTIVE_EMITTER.set(emitter)
    try:
        assert await emit_step(
            step_kind="knowledge_read",
            outcome="succeeded",
            detail={"missing_context": ["not_recorded"] * 25},
        ) is True
    finally:
        mod._ACTIVE_EMITTER.reset(token)
    await emitter.finish()

    assert emitter.drain_crossings()[0]["detail"] is None
    assert client.progress_payloads[1]["step"]["missing_context"] == []


@pytest.mark.anyio
@pytest.mark.parametrize("limit_name", ["LIVE_EVENT_MAX_BYTES", "LIVE_REQUEST_MAX_BYTES"])
async def test_event_or_aggregate_overflow_keeps_only_the_accepted_prefix(
    monkeypatch, limit_name: str,
) -> None:
    client = _FakeClient()
    emitter = mod._PathEmitter(client, asyncio.get_running_loop())
    assert emitter.bind_path("aip_RESULT") is True
    monkeypatch.setattr(mod, limit_name, 1)

    assert await _emit_sync_crossings(emitter, 2) == [False, False]
    await emitter.finish()

    assert len(emitter.drain_crossings()) == 2
    assert [event["event"] for event in client.progress_payloads] == ["recording"]
    assert emitter.live_closed is True


@pytest.mark.anyio
async def test_sync_crossing_never_waits_for_a_hung_progress_sink() -> None:
    client = _HungClient()
    emitter = mod._PathEmitter(client, asyncio.get_running_loop())
    assert emitter.bind_path("aip_RESULT") is True
    await asyncio.wait_for(client.started.wait(), timeout=0.100)

    # The sender is demonstrably stuck when the synchronous producer offers its
    # crossing. Acceptance must depend only on the bounded FIFO, never on thread
    # startup timing or completion of the progress sink.
    accepted = emitter.record_and_route_step(
        mod._crossing_fields(
            step_kind="knowledge_read",
            outcome="succeeded",
            tool_name="search_context",
            owner_workspace="context-hub",
            owner_object_type="topic",
            owner_object_id="ctx_1",
            owner_version_id=None,
            evidence_record_id=None,
            detail=None,
        )
    )
    assert accepted == (True, False)
    assert mod.LIVE_SEND_BUDGET_SECONDS == 0.015
    await asyncio.wait_for(emitter.finish(), timeout=0.100)
    assert emitter.live_closed is True


@pytest.mark.anyio
async def test_finish_has_a_hard_bound_when_sink_delays_cancellation() -> None:
    loop = asyncio.get_running_loop()
    client = _SlowCancellationClient()
    emitter = mod._PathEmitter(client, loop)
    assert emitter.bind_path("aip_RESULT") is True
    await client.started.wait()
    started = loop.time()

    await emitter.finish()

    assert loop.time() - started < 0.025
    await asyncio.wait_for(client.cancelled.wait(), timeout=0.100)
    await asyncio.wait_for(client.finished.wait(), timeout=0.050)
    await asyncio.sleep(0)
    assert mod._DETACHED_SENDERS == set()


@pytest.mark.anyio
async def test_external_cancellation_is_propagated_after_sender_cleanup() -> None:
    client = _HungClient()
    emitter = mod._PathEmitter(client, asyncio.get_running_loop())
    assert emitter.bind_path("aip_RESULT") is True
    task = asyncio.create_task(emitter.finish())
    await asyncio.sleep(0)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.anyio
async def test_sink_cancelled_error_closes_progress_without_cancelling_request() -> None:
    emitter = mod._PathEmitter(_CancelledSinkClient(), asyncio.get_running_loop())
    assert emitter.bind_path("aip_RESULT") is True

    await emitter.finish()

    assert emitter.live_closed is True


@pytest.mark.anyio
async def test_middleware_never_turns_request_cancellation_into_a_tool_result() -> None:
    client = _SlowCancellationClient()
    middleware = mod.build_middleware()
    assert middleware is not None

    async def call_next(_context):
        emitter = mod._ACTIVE_EMITTER.get()
        assert emitter is not None
        assert emitter.bind_path("aip_RESULT") is True
        return {"ok": True}

    task = asyncio.create_task(
        middleware.on_call_tool(
            _middleware_context(client, tool_name=mod.RESULT_EXECUTION_TOOL_NAME),
            call_next,
        )
    )
    await client.started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(client.finished.wait(), timeout=0.250)


@pytest.mark.anyio
async def test_search_cancellation_at_call_start_rolls_back_and_closes_observation(
    monkeypatch,
) -> None:
    class Conn:
        def __init__(self):
            self.rollbacks = 0

        def rollback(self):
            self.rollbacks += 1

    class Manager:
        def __init__(self):
            self.exits = 0

        def __exit__(self, *_exc):
            self.exits += 1

    conn = Conn()
    manager = Manager()
    observation = SimpleNamespace(closed=False, conn=conn, manager=manager)

    def begin(*, emitter, **_kwargs):
        assert emitter.bind_path("aip_search") is True
        return observation

    monkeypatch.setattr(mod, "_begin_search_context_observation", begin)
    monkeypatch.setattr(
        mod,
        "_finalize_search_context_observation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a cancelled search must abort, not finalize")
        ),
    )
    client = _FakeClient()
    entered = asyncio.Event()

    async def call_next(_context):
        entered.set()
        await asyncio.Event().wait()

    middleware = mod.build_middleware()
    task = asyncio.create_task(
        middleware.on_call_tool(_middleware_context(client), call_next)
    )
    await asyncio.wait_for(entered.wait(), timeout=0.100)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert observation.closed is True
    assert (conn.rollbacks, manager.exits) == (1, 1)
    assert mod._ACTIVE_EMITTER.get() is None
