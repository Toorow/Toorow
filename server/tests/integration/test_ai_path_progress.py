"""Real FastMCP/Postgres proof for Story 65.6 live AI Path progress.

The progress wire is useful only if it describes the Result-owned path produced by
``execute_analyze_query_spec``.  These tests therefore keep the real adapter,
middleware, transaction and PostgreSQL rows.  Only the warehouse runner and its
already-governed physical plan are deterministic test doubles; they are not the
owner under test.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import threading
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from core import (
    ai_path_recorder,
    ai_paths,
    query_execution,
    query_specs_api,
    warehouse,
)
from core import analyze_render_mcp as adapter
from fastmcp import Client, FastMCP
from fastmcp.client.transports import FastMCPTransport
from fastmcp.server.context import Context
from mcp import types as mcp_types
from ulid import ULID

from tests.integration.test_render_shares_postgres import RESULT_ROWS, Chain, _uid

pytestmark = [pytest.mark.anyio, pytest.mark.pg]

_SECRET = "story-65-6-feedback-secret-32-bytes"
_BANNED_EVENT_KEYS = {
    "actor",
    "arguments",
    "detail",
    "error",
    "ordinal",
    "observed_at",
    "query",
    "skill",
    "trace_id",
}


@dataclass
class ProgressCapture:
    entries: list[tuple[float, float | None, str, dict[str, Any]]] = field(
        default_factory=list
    )
    result_returned: bool = False
    late_delivery: bool = False
    noncanonical_messages: list[str] = field(default_factory=list)

    async def __call__(
        self, progress: float, total: float | None, message: str | None
    ) -> None:
        self.late_delivery = self.late_delivery or self.result_returned
        assert message is not None
        event = json.loads(message)
        canonical = json.dumps(
            event, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        if message != canonical:
            self.noncanonical_messages.append(message)
        self.entries.append((progress, total, message, event))

    @property
    def events(self) -> list[dict[str, Any]]:
        return [entry[3] for entry in self.entries]


@dataclass
class Harness:
    dsn: str
    target: FastMCP
    identities: contextvars.ContextVar[str]
    chains: tuple[Chain, Chain]
    crossings: dict[str, int]
    accepted: dict[str, list[bool]]
    fail_projects: set[str]

    async def call(
        self,
        chain: Chain,
        identity: str,
        *,
        capture: ProgressCapture | None = None,
        silent: bool = False,
        raise_on_error: bool = True,
        meta: dict[str, Any] | None = None,
    ):
        token = self.identities.set(identity)
        try:
            async with Client(FastMCPTransport(self.target)) as client:
                arguments = {
                    "project_id": chain.project_id,
                    "query_spec_version_id": chain.query_spec_version_id,
                }
                if silent:
                    assert capture is None
                    raw = await client.session.call_tool(
                        "execute_analyze_query_spec",
                        arguments,
                        progress_callback=None,
                        meta=meta,
                    )
                    result = SimpleNamespace(
                        content=raw.content,
                        structured_content=raw.structuredContent,
                        is_error=raw.isError,
                    )
                else:
                    result = await client.call_tool(
                        "execute_analyze_query_spec",
                        arguments,
                        progress_handler=capture,
                        raise_on_error=raise_on_error,
                        meta=meta,
                    )
        finally:
            self.identities.reset(token)
        if capture is not None:
            capture.result_returned = True
        return result


@pytest.fixture
def progress_harness(live_postgres, monkeypatch) -> Harness:
    dsn = live_postgres.info.dsn
    identities: contextvars.ContextVar[str] = contextvars.ContextVar(
        "story_65_6_identity", default="nobody@example.com"
    )
    identity_a = f"story-65-6-a-{ULID()}@example.com"
    identity_b = f"story-65-6-b-{ULID()}@example.com"
    chain_a = Chain(live_postgres).build()
    chain_b = Chain(live_postgres).build()
    with live_postgres.cursor() as cur:
        for chain, identity in ((chain_a, identity_a), (chain_b, identity_b)):
            cur.execute(
                "INSERT INTO app.org_members "
                "(id, org_id, identity, role, status, joined_at) "
                "VALUES (%s, %s, %s, 'owner', 'active', NOW())",
                (_uid("omem"), chain.org_id, identity),
            )
    live_postgres.commit()

    @contextlib.contextmanager
    def connection(identity):
        with psycopg.connect(dsn) as conn:
            query_specs_api.arm_access_floor(conn, identity)
            yield conn

    physical_plan = {
        "relation": "fixture_rows",
        "columns": {"day": "day", "sessions": "sessions"},
    }
    evidence = {
        "provenance": {
            "values": [
                {"member_id": "day", "source_field": "day"},
                {"member_id": "sessions", "source_field": "sessions"},
            ]
        },
        "freshness": {"output_created_at": "2026-01-01T00:00:00Z"},
        "dq_evaluation_ids": [],
        "dq_unavailable_reason": None,
    }
    crossings: dict[str, int] = {}
    accepted: dict[str, list[bool]] = {}
    fail_projects: set[str] = set()
    active_project: contextvars.ContextVar[str | None] = contextvars.ContextVar(
        "story_65_6_active_project", default=None
    )
    original_run = query_execution.run_execution
    original_semantic_crossing = query_execution._record_semantic_query_crossing
    original_required_capability_outcome = query_execution.required_capability_outcome

    def observed_run(conn, **kwargs):
        project_id = str(kwargs["project_id"])
        decisions = accepted.setdefault(project_id, [])
        token = active_project.set(project_id)
        try:
            # The production execution records the semantic-query crossing itself.
            # Extra governed observations let the same real door exercise the cap.
            for index in range(max(0, crossings.get(project_id, 1) - 1)):
                decisions.append(
                    ai_path_recorder.emit_step_sync(
                        step_kind="knowledge_read",
                        outcome="succeeded",
                        tool_name="execute_analyze_query_spec",
                        owner_workspace="context-hub",
                        owner_object_type="knowledge",
                        owner_object_id=f"ctx_{project_id}_{index}",
                        detail={"missing_context": []},
                    )
                )
            if project_id in fail_projects:
                raise RuntimeError("execution rollback tripwire")
            return original_run(conn, **kwargs)
        finally:
            active_project.reset(token)

    def observed_semantic_crossing(**fields):
        result = original_semantic_crossing(**fields)
        project_id = active_project.get()
        if project_id is not None:
            accepted.setdefault(project_id, []).append(bool(result))
        return result

    def required_capability_outcome(conn, *, project_id, spec):
        if project_id in crossings and crossings[project_id] == 0:
            return (
                "refused",
                {"reason_code": "CAPABILITY_DISABLED", "reason": "Capability disabled"},
            )
        return original_required_capability_outcome(
            conn, project_id=project_id, spec=spec
        )

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    monkeypatch.setenv("TOOROW_FEEDBACK_CONTEXT_SECRET", _SECRET)
    monkeypatch.setenv("PLATFORM_DB_URL", dsn)
    monkeypatch.setattr(adapter, "_identity", lambda: identities.get())
    monkeypatch.setattr(query_specs_api, "analyze_connection", connection)
    monkeypatch.setattr(query_execution, "resolve_physical_plan", lambda *a, **k: physical_plan)
    monkeypatch.setattr(query_execution, "capture_evidence", lambda *a, **k: evidence)
    monkeypatch.setattr(query_execution, "run_execution", observed_run)
    monkeypatch.setattr(
        query_execution, "_record_semantic_query_crossing", observed_semantic_crossing
    )
    monkeypatch.setattr(
        query_execution, "required_capability_outcome", required_capability_outcome
    )
    monkeypatch.setattr(warehouse, "_db_mode", lambda: "duckdb")
    monkeypatch.setattr(warehouse, "_query_duckdb", lambda *_a, **_k: list(RESULT_ROWS))

    target = FastMCP("story-65-6-progress")
    middleware = ai_path_recorder.build_middleware()
    assert middleware is not None
    target.add_middleware(middleware)
    adapter.register(target)
    return Harness(
        dsn=dsn,
        target=target,
        identities=identities,
        chains=(chain_a, chain_b),
        crossings=crossings,
        accepted=accepted,
        fail_projects=fail_projects,
    )


def _identity_for(chain: Chain) -> str:
    # The fixture's identities are deliberately derivable only from membership,
    # not stored on the Harness beside another Project's authority.
    with chain.conn.cursor() as cur:
        cur.execute(
            "SELECT identity FROM app.org_members WHERE org_id = %s AND status = 'active' "
            "ORDER BY joined_at DESC LIMIT 1",
            (chain.org_id,),
        )
        row = cur.fetchone()
    assert row is not None
    return str(row[0])


def _tool_error_text(result) -> str:
    return " ".join(
        str(getattr(block, "text", "")) for block in result.content
    ).lower()


def _result_ids(result) -> tuple[str, str]:
    summary = result.structured_content
    assert isinstance(summary, dict)
    return str(summary["result"]["result_id"]), str(summary["ai_path"])


def _snapshot(dsn: str, project_id: str, result) -> dict[str, Any]:
    result_id, path_id = _result_ids(result)
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT outcome, content_hash, row_count, truncated, query_spec_version_id "
                "FROM app.query_results WHERE id = %s",
                (result_id,),
            )
            result_row = cur.fetchone()
            cur.execute(
                "SELECT result_schema, manifest, rows_chunk FROM app.query_result_payloads "
                "WHERE result_id = %s",
                (result_id,),
            )
            payload_row = cur.fetchone()
        path = ai_paths.load_path(conn, path_id=path_id, project_id=project_id)
    assert result_row is not None and payload_row is not None
    steps = []
    for raw in path["steps"]:
        step = {
            key: raw.get(key)
            for key in (
                "ordinal",
                "step_kind",
                "owner_workspace",
                "owner_object_type",
                "owner_object_id",
                "owner_version_id",
                "skill_version_id",
                "skill_step_id",
                "tool_name",
                "outcome",
                "detail",
            )
        }
        evidence_id = raw.get("evidence_record_id")
        step["evidence_record_id"] = (
            "qr_NORMALIZED" if evidence_id == result_id else evidence_id
        )
        steps.append(step)
    return {
        "result": result_row,
        "payload": payload_row,
        "path": {
            "lifecycle": path["lifecycle"],
            "outcome": path["outcome"],
            "started_at": "TIMESTAMP_NORMALIZED" if path.get("started_at") else None,
            "ended_at": "TIMESTAMP_NORMALIZED" if path.get("ended_at") else None,
            "content_hash": "PATH_HASH_NORMALIZED" if path.get("content_hash") else None,
            "steps": steps,
        },
    }


def _assert_wire(capture: ProgressCapture, path_id: str) -> None:
    assert capture.events
    assert not capture.late_delivery
    assert capture.noncanonical_messages == []
    assert capture.events[0] == {
        "schema_version": "observed-ai-path-progress.v1",
        "event": "recording",
        "path_id": path_id,
        "sequence": 0,
    }
    for sequence, (progress, total, _message, event) in enumerate(capture.entries):
        assert progress == sequence + 1
        assert total is None
        assert event["path_id"] == path_id
        assert event["sequence"] == sequence
        assert not (_BANNED_EVENT_KEYS & event.keys())
        if sequence == 0:
            assert set(event) == {"schema_version", "event", "path_id", "sequence"}
            continue
        assert set(event) == {
            "schema_version",
            "event",
            "path_id",
            "sequence",
            "step",
        }
        assert event["event"] == "step_observed"
        assert not (_BANNED_EVENT_KEYS & event["step"].keys())
        assert set(event["step"]) <= {
            "step_kind",
            "outcome",
            "missing_context",
            "tool_name",
            "owner",
            "evidence_record_id",
        }
        assert {"step_kind", "outcome", "missing_context"} <= set(event["step"])


async def test_real_execute_stream_is_path_bound_ordered_and_a_final_prefix(
    progress_harness: Harness,
):
    chain = progress_harness.chains[0]
    progress_harness.crossings[chain.project_id] = 2
    capture = ProgressCapture()

    result = await progress_harness.call(
        chain, _identity_for(chain), capture=capture
    )
    result_id, path_id = _result_ids(result)
    _assert_wire(capture, path_id)
    assert [event["event"] for event in capture.events] == [
        "recording",
        "step_observed",
        "step_observed",
    ]

    with psycopg.connect(progress_harness.dsn) as conn:
        path = ai_paths.project_observed_ai_path(
            conn, project_id=chain.project_id, ai_path=path_id
        )
    assert path["state"] == "completed"
    persisted_crossings = path["steps"][:2]
    live_steps = [event["step"] for event in capture.events[1:]]
    for live, persisted in zip(live_steps, persisted_crossings, strict=True):
        assert live == {
            key: persisted[key]
            for key in live
        }
    assert path["steps"][2]["step_kind"] == "tool_call"
    assert all(step.get("evidence_record_id") in {None, result_id} for step in path["steps"])


async def test_silent_and_armed_execution_keep_identical_normalized_evidence(
    progress_harness: Harness, monkeypatch
):
    chain = progress_harness.chains[0]
    identity = _identity_for(chain)
    progress_harness.crossings[chain.project_id] = 3
    report_calls = 0
    original_report = Context.report_progress

    async def counted_report(self, *args, **kwargs):
        nonlocal report_calls
        report_calls += 1
        return await original_report(self, *args, **kwargs)

    monkeypatch.setattr(Context, "report_progress", counted_report)
    silent = await progress_harness.call(chain, identity, silent=True)
    assert report_calls == 0
    silent_snapshot = _snapshot(progress_harness.dsn, chain.project_id, silent)

    capture = ProgressCapture()
    armed = await progress_harness.call(chain, identity, capture=capture)
    _assert_wire(capture, _result_ids(armed)[1])
    armed_snapshot = _snapshot(progress_harness.dsn, chain.project_id, armed)

    assert silent_snapshot == armed_snapshot
    assert _result_ids(silent) != _result_ids(armed)


@pytest.mark.parametrize(
    ("crossing_count", "expected_persisted_steps"),
    ((0, 0), (1, 1), (64, 64), (65, 64)),
)
async def test_live_crossing_cap_matches_the_persisted_prefix(
    progress_harness: Harness,
    crossing_count: int,
    expected_persisted_steps: int,
):
    chain = progress_harness.chains[0]
    progress_harness.crossings[chain.project_id] = crossing_count
    capture = ProgressCapture()
    result = await progress_harness.call(
        chain, _identity_for(chain), capture=capture
    )
    _assert_wire(capture, _result_ids(result)[1])

    delivered_steps = len(capture.events) - 1
    if crossing_count == 0:
        assert delivered_steps == 0
    else:
        assert 1 <= delivered_steps <= expected_persisted_steps
    accepted = progress_harness.accepted[chain.project_id]
    assert len(accepted) == crossing_count
    assert accepted == sorted(accepted, reverse=True), "live delivery reopened after closing"
    assert delivered_steps <= sum(accepted) <= expected_persisted_steps
    snapshot = _snapshot(progress_harness.dsn, chain.project_id, result)
    # Persistence is independent from the bounded wire: every capped crossing
    # survives even when the 25 ms live sender closes on a slow test transport.
    assert len(snapshot["path"]["steps"]) == expected_persisted_steps + 1
    for index, event in enumerate(capture.events[1:]):
        assert event["step"]["owner"]["object_id"] == (
            snapshot["path"]["steps"][index]["owner_object_id"]
        )


async def test_two_request_scopes_do_not_cross_paths_or_foreign_authority(
    progress_harness: Harness,
):
    chain_a, chain_b = progress_harness.chains
    identity_a, identity_b = _identity_for(chain_a), _identity_for(chain_b)
    progress_harness.crossings.update({chain_a.project_id: 1, chain_b.project_id: 1})
    capture_a, capture_b = ProgressCapture(), ProgressCapture()

    result_a, result_b = await asyncio.gather(
        progress_harness.call(chain_a, identity_a, capture=capture_a),
        progress_harness.call(chain_b, identity_b, capture=capture_b),
    )
    path_a, path_b = _result_ids(result_a)[1], _result_ids(result_b)[1]
    assert path_a != path_b
    _assert_wire(capture_a, path_a)
    _assert_wire(capture_b, path_b)
    assert all(chain_b.project_id not in entry[2] for entry in capture_a.entries)
    assert all(chain_a.project_id not in entry[2] for entry in capture_b.entries)

    denied_capture = ProgressCapture()
    denied = await progress_harness.call(
        chain_b,
        identity_a,
        capture=denied_capture,
        raise_on_error=False,
    )
    assert denied.is_error
    assert "not_found" in _tool_error_text(denied)
    assert denied_capture.events == []


async def test_sequential_token_and_trace_reuse_starts_a_fresh_bound_stream(
    progress_harness: Harness,
):
    chain = progress_harness.chains[0]
    identity = _identity_for(chain)
    progress_harness.crossings[chain.project_id] = 1
    trace_id = "1234567890abcdef1234567890abcdef"
    shared_progress_token = "story-65-6-reused-progress-token"
    meta = {
        "progressToken": shared_progress_token,
        "traceparent": f"00-{trace_id}-1234567890abcdef-01",
    }
    notifications: list[mcp_types.ProgressNotificationParams] = []

    async def capture_notification(message) -> None:
        if (
            isinstance(message, mcp_types.ServerNotification)
            and isinstance(message.root, mcp_types.ProgressNotification)
        ):
            notifications.append(message.root.params)

    identity_token = progress_harness.identities.set(identity)
    try:
        # One public Client/session, and no progress callback: the MCP SDK then
        # preserves our explicit progressToken instead of replacing it with the
        # request id. The public message handler receives the actual server wire.
        async with Client(
            FastMCPTransport(progress_harness.target),
            message_handler=capture_notification,
        ) as client:
            arguments = {
                "project_id": chain.project_id,
                "query_spec_version_id": chain.query_spec_version_id,
            }
            first_raw = await client.session.call_tool(
                "execute_analyze_query_spec",
                arguments,
                progress_callback=None,
                meta=meta,
            )
            split = len(notifications)
            second_raw = await client.session.call_tool(
                "execute_analyze_query_spec",
                arguments,
                progress_callback=None,
                meta=meta,
            )
    finally:
        progress_harness.identities.reset(identity_token)

    first_path = str(first_raw.structuredContent["ai_path"])
    second_path = str(second_raw.structuredContent["ai_path"])
    first_notifications = notifications[:split]
    second_notifications = notifications[split:]

    assert first_path != second_path
    assert first_notifications and second_notifications
    assert {item.progressToken for item in notifications} == {shared_progress_token}
    for path_id, captured in (
        (first_path, first_notifications),
        (second_path, second_notifications),
    ):
        events = [json.loads(item.message) for item in captured]
        assert events[0] == {
            "schema_version": "observed-ai-path-progress.v1",
            "event": "recording",
            "path_id": path_id,
            "sequence": 0,
        }
        assert [event["sequence"] for event in events] == list(range(len(events)))
        assert {event["path_id"] for event in events} == {path_id}

    with psycopg.connect(progress_harness.dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, w3c_trace_id FROM app.ai_paths WHERE id = ANY(%s) ORDER BY id",
            ([first_path, second_path],),
        )
        rows = cur.fetchall()
    assert len(rows) == 2
    assert {row[1] for row in rows} == {trace_id}


async def test_analytical_failure_finalizes_but_exception_rolls_back_provisional_path(
    progress_harness: Harness, monkeypatch
):
    chain = progress_harness.chains[0]
    identity = _identity_for(chain)
    progress_harness.crossings[chain.project_id] = 1

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("warehouse unavailable")

    monkeypatch.setattr(warehouse, "_query_duckdb", unavailable)
    analytical_capture = ProgressCapture()
    analytical = await progress_harness.call(
        chain, identity, capture=analytical_capture
    )
    analytical_path = _result_ids(analytical)[1]
    _assert_wire(analytical_capture, analytical_path)
    with psycopg.connect(progress_harness.dsn) as conn:
        projected = ai_paths.project_observed_ai_path(
            conn, project_id=chain.project_id, ai_path=analytical_path
        )
    assert projected["state"] == "failed"
    assert projected["outcome"] == "unavailable"

    progress_harness.fail_projects.add(chain.project_id)
    rollback_capture = ProgressCapture()
    rolled_back = await progress_harness.call(
        chain,
        identity,
        capture=rollback_capture,
        raise_on_error=False,
    )
    assert rolled_back.is_error
    assert rollback_capture.events
    provisional_path = rollback_capture.events[0]["path_id"]
    with psycopg.connect(progress_harness.dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM app.ai_paths WHERE id = %s", (provisional_path,))
        assert cur.fetchone()[0] == 0
        cur.execute(
            "SELECT count(*) FROM app.query_results WHERE ai_path_id = %s",
            (provisional_path,),
        )
        assert cur.fetchone()[0] == 0
    assert all(event["event"] != "completed" for event in rollback_capture.events)
    assert all(event["event"] != "failed" for event in rollback_capture.events)


async def test_real_request_cancellation_closes_dispatcher_without_late_events(
    progress_harness: Harness, monkeypatch
):
    chain = progress_harness.chains[0]
    identity = _identity_for(chain)
    progress_harness.crossings[chain.project_id] = 1
    entered = threading.Event()
    release = threading.Event()
    original_run = query_execution.run_execution

    def blocking_run(conn, **kwargs):
        entered.set()
        assert release.wait(timeout=2.0)
        return original_run(conn, **kwargs)

    monkeypatch.setattr(query_execution, "run_execution", blocking_run)
    capture = ProgressCapture()
    task = asyncio.create_task(
        progress_harness.call(chain, identity, capture=capture)
    )
    assert await asyncio.to_thread(entered.wait, 1.0)
    assert capture.events and capture.events[0]["event"] == "recording"

    task.cancel()
    capture.result_returned = True
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.050)

    assert capture.late_delivery is False


@pytest.mark.parametrize("sink_mode", ("throw", "hang"))
async def test_report_progress_failure_cannot_hold_the_result_transaction(
    progress_harness: Harness, monkeypatch, sink_mode: str
):
    chain = progress_harness.chains[0]
    progress_harness.crossings[chain.project_id] = 1
    identity = _identity_for(chain)
    silent = await progress_harness.call(chain, identity, silent=True)
    silent_snapshot = _snapshot(progress_harness.dsn, chain.project_id, silent)

    async def broken_report(self, *args, **kwargs):
        if sink_mode == "throw":
            raise RuntimeError("notification sink failed")
        await asyncio.sleep(60)

    monkeypatch.setattr(Context, "report_progress", broken_report)
    capture = ProgressCapture()
    started = time.perf_counter()
    result = await asyncio.wait_for(
        progress_harness.call(chain, identity, capture=capture),
        timeout=1.0,
    )
    elapsed = time.perf_counter() - started

    # The whole real PG call has work beyond dispatcher cleanup, so its wall time
    # is intentionally looser than the 25 ms unit/perf gate. It still catches the
    # historical two-second wait and an un-cancelled hung task.
    assert elapsed < 1.0
    snapshot = _snapshot(progress_harness.dsn, chain.project_id, result)
    assert snapshot == silent_snapshot
    assert snapshot["result"][0] == "success"
    assert snapshot["path"]["lifecycle"] == "finalized"
