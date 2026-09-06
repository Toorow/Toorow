"""`get_ai_path`: the model reads the observed AI Paths of a Project, its own included.

`mcp-tool-surface.md`, amendment of 2026-09-05 -- Jean: « tu devrais pouvoir
analyser ton propre AI path ». Measured before it: 147 paths on the reference
project, 111 of them `recording` (one per tool call the harness made), and no
MCP read able to list or open one -- only `render_analyze_result` walked the
path behind a finalized Result. Composition and refusal only; the store has its
own pg tests (`test_ai_paths*`). Doubles as in `test_semantic_model_mcp_door.py`.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from fastmcp.exceptions import ToolError

PROJECT = "proj_x"


def _tool(name: str = "get_ai_path"):
    import core.evaluation_mcp as module
    import core.mcp_profiles as profiles

    captured: dict[str, object] = {}
    declarations: dict[str, dict] = {}

    def _record(_mcp, handler, **kwargs):
        captured[handler.__name__] = handler
        declarations[handler.__name__] = kwargs
        return handler

    original = profiles.register_profiled
    profiles.register_profiled = _record
    try:
        module.register(object())
    finally:
        profiles.register_profiled = original
    return captured[name], declarations[name]


@pytest.fixture()
def a_member(monkeypatch):
    """A caller who holds the Project; the connection is a double."""
    conn = MagicMock()

    @contextmanager
    def _opens(_identity):
        yield conn

    monkeypatch.setattr("core.mcp_scope.caller_identity", lambda: "person_member")
    monkeypatch.setattr("core.mcp_scope.refuse_unless_project_scope", lambda *_a, **_k: None)
    monkeypatch.setattr("core.db.request_connection", _opens)
    return conn


def _code(exc: ToolError) -> str:
    return json.loads(str(exc))["code"]


def _step(n: int, tool: str = "propose_shared_identities") -> dict:
    return {
        "id": f"aips_{n}",
        "ordinal": n,
        "step_kind": "tool_call",
        "owner_workspace": "governance",
        "owner_object_type": "semantic-view",
        "owner_object_id": "sv_1",
        "owner_version_id": "svv_1",
        "skill_version_id": None,
        "skill_step_id": None,
        "tool_name": tool,
        "outcome": "succeeded",
        "evidence_record_id": None,
        "observed_at": datetime(2026, 9, 5, 3, 20, n % 60, tzinfo=UTC),
        "detail": {"family": "table", "request.pivot.rows": ["mdm_date"], "anything": "x" * 200},
    }


def _path(steps: int, lifecycle: str = "recording") -> dict:
    return {
        "id": "aip_1",
        "project_id": PROJECT,
        "lifecycle": lifecycle,
        "outcome": None if lifecycle == "recording" else "succeeded",
        "actor": "person_member",
        "w3c_trace_id": "a" * 32,
        "started_at": datetime(2026, 9, 5, 3, 20, 0, tzinfo=UTC),
        "ended_at": None,
        "policy_snapshot": {},
        "steps": [_step(n) for n in range(1, steps + 1)],
        "assessment": {"verdict": "unverifiable", "findings": [{"finding": "evidence_unverifiable", "detail": "y" * 5000}]},
    }


def test_the_tool_is_an_insights_read():
    _handler, declared = _tool()
    assert (declared["profile"], declared["effect"], declared["confirmation_mode"]) == ("insights", "read", "none")


def test_the_list_is_one_line_per_path_with_its_tools(a_member, monkeypatch):
    monkeypatch.setattr(
        "core.ai_paths.list_paths",
        lambda conn, *, project_id, limit=50, cursor=None: [
            {"id": "aip_2", "lifecycle": "recording", "outcome": None, "actor": "person_member",
             "started_at": datetime(2026, 9, 5, 3, 21, tzinfo=UTC), "ended_at": None, "w3c_trace_id": "b" * 32},
            {"id": "aip_1", "lifecycle": "finalized", "outcome": "succeeded", "actor": "person_member",
             "started_at": datetime(2026, 9, 5, 3, 20, tzinfo=UTC), "ended_at": None, "w3c_trace_id": "a" * 32},
        ],
    )
    monkeypatch.setattr(
        "core.ai_paths.steps_digest",
        lambda conn, *, path_ids: {
            "aip_2": {"steps": 3, "tools": ["propose_shared_identities", "publish_shared_identity"]},
            "aip_1": {"steps": 2, "tools": ["execute_analyze_query_spec"]},
        },
    )
    handler, _ = _tool()
    result = handler(project_id=PROJECT)
    data = result.structured_content["data"]
    assert [p["path_id"] for p in data["paths"]] == ["aip_2", "aip_1"]
    assert data["paths"][0]["tools"] == ["propose_shared_identities", "publish_shared_identity"]
    assert data["paths"][0]["state"] == "recording" and data["paths"][1]["outcome"] == "succeeded"
    assert "2 latest AI Path(s)" in result.content[0].text and "1 still recording" in result.content[0].text


def test_one_path_pages_its_steps_inside_the_model_channel(a_member, monkeypatch):
    from core import model_channel

    monkeypatch.setattr("core.ai_paths.load_path", lambda conn, *, path_id, project_id, **_kwargs: _path(57))
    monkeypatch.setattr(
        "core.ai_paths.paths_sharing_trace",
        lambda conn, *, project_id, trace_id, exclude=None: [{"path_id": "aip_exec", "state": "finalized", "outcome": "succeeded"}],
    )
    monkeypatch.setattr("core.ai_paths.load_steps", lambda conn, *, path_id, project_id, **_kwargs: _path(57)["steps"])
    seen_steps: list[int] = []

    def _coverage(conn, steps):
        seen_steps.append(len(steps))
        return [{"skill_name": "s", "skill_version": "proc_1@6", "prescribed": 9, "crossed": ["2", "6"], "skipped": ["3"], "unobservable": ["1"], "steps": []}]

    monkeypatch.setattr("core.ai_paths.skill_coverage", _coverage)
    handler, _ = _tool()

    first = handler(project_id=PROJECT, path_id="aip_1")
    # Coverage is read over the interaction: this path's 57 steps plus the sibling's (the same double answers 57 again).
    assert seen_steps == [114]
    assert first.structured_content["data"]["judgement"]["skill_coverage"][0]["crossed"] == ["2", "6"]
    visible, _app = model_channel.partition_envelope(first.structured_content, tool_name="get_ai_path")
    model_channel.enforce_model_channel("get_ai_path", first.content, visible)
    data = visible["data"]
    assert isinstance(data["steps"], list), "the steps were withheld from the model"
    assert data["steps_total"] == 57 and len(data["steps"]) == 10 and data["truncated"] is True
    assert data["state"] == "recording"
    assert data["steps"][0] == {
        "n": 1, "kind": "tool_call", "level": "TOOL", "tool": "propose_shared_identities",
        "outcome": "succeeded", "owner": "governance/semantic-view/sv_1@svv_1",
        "chose": {"family": "table", "request.pivot.rows": ["mdm_date"], "anything": "x" * 80},
    }
    assert "x" * 200 not in json.dumps(data)  # the stored detail is bounded before it travels
    judgement = data["judgement"]
    assert judgement["policy"]["verdict"] == "unverifiable"
    assert judgement["skills"] == [] and judgement["context_consulted"] == []
    assert judgement["reading"].startswith("no Skill taken, no governed context consulted")
    assert data["same_interaction"] == [{"path_id": "aip_exec", "state": "finalized", "outcome": "succeeded"}]
    assert "still recording" in first.content[0].text and "1 other path(s) share this interaction" in first.content[0].text

    last = handler(project_id=PROJECT, path_id="aip_1", offset=50)
    assert last.structured_content["data"]["steps"][-1]["n"] == 57
    assert last.structured_content["data"]["truncated"] is False


def test_a_result_run_by_a_person_says_no_ai_path_and_a_model_result_opens_its_path(a_member, monkeypatch):
    cursor = a_member.cursor.return_value.__enter__.return_value
    handler, _ = _tool()

    cursor.fetchone.return_value = (None, "No AI path")
    human = handler(project_id=PROJECT, result_id="qr_human")
    assert human.structured_content["data"] == {"result_id": "qr_human", "state": "human_absent", "literal": "No AI path"}

    cursor.fetchone.return_value = ("aip_1", None)
    monkeypatch.setattr("core.ai_paths.load_path", lambda conn, *, path_id, project_id, **_kwargs: _path(2, "finalized"))
    monkeypatch.setattr("core.ai_paths.paths_sharing_trace", lambda conn, *, project_id, trace_id, exclude=None: [])
    model = handler(project_id=PROJECT, result_id="qr_model")
    assert model.structured_content["data"]["path_id"] == "aip_1"
    assert model.structured_content["data"]["result_id"] == "qr_model"
    assert "finalized, succeeded" in model.content[0].text

    cursor.fetchone.return_value = None
    with pytest.raises(ToolError) as missing:
        handler(project_id=PROJECT, result_id="qr_nobody")
    assert _code(missing.value) == "not_found"


def test_an_unknown_path_is_not_found_and_never_named(a_member, monkeypatch):
    from core.ai_paths import AiPathNotFound

    def _raise(conn, *, path_id, project_id, **_kwargs):
        raise AiPathNotFound("AI Path not found")

    monkeypatch.setattr("core.ai_paths.load_path", _raise)
    handler, _ = _tool()
    with pytest.raises(ToolError) as unknown:
        handler(project_id=PROJECT, path_id="aip_foreign")
    assert _code(unknown.value) == "not_found"
    assert "aip_foreign" not in str(unknown.value)


def test_the_skill_mode_reads_what_the_walks_say_about_a_skill(a_member, monkeypatch):  # noqa: F811
    """Iteration 3: the model reads its own habit on a Skill before walking again."""
    monkeypatch.setattr(
        "core.ai_paths.skill_walk_stats",
        lambda conn, *, project_id, procedure_id, limit=20: {
            "procedure_id": procedure_id, "walks": 3, "window": "last 3 walk(s)", "verdicts": {"pass": 1, "fail": 2}, "versions": {"proc_1@6": 3},
            "steps": [{"step": "1", "label": "Run", "tool": "execute_analyze_query_spec", "crossed": 3, "skipped": 0, "unobservable": 0},
                      {"step": "2", "label": "Read the Result and its evidence before saying anything about it.", "tool": "analyze_result", "crossed": 1, "skipped": 2, "unobservable": 0}],
            "most_skipped": [{"step": "2", "label": "Read the Result and its evidence before saying anything about it.", "tool": "analyze_result", "crossed": 1, "skipped": 2, "unobservable": 0}],
            "recent": [{"path_id": "aip_a", "verdict": "pass", "skipped": []}],
        },
    )
    handler, _ = _tool()
    result = handler(project_id=PROJECT, skill="proc_1")
    data = result.structured_content["data"]
    assert data["walks"] == 3 and data["verdicts"] == {"pass": 1, "fail": 2}
    assert data["most_skipped"] == ["step 2 (2x): Read the Result and its evidence before saying anything abou"]  # label cut at 60 chars
    assert "Most skipped: step 2 (2x)" in result.content[0].text
    from core import model_channel

    visible, _app = model_channel.partition_envelope(result.structured_content, tool_name="get_ai_path")
    model_channel.enforce_model_channel("get_ai_path", result.content, visible)


def _skill_step(n: int, step: str, tool: str) -> dict:
    return {
        **_step(n, tool), "step_kind": "skill_step", "skill_version_id": "proc_1@6", "skill_step_id": step,
        "owner_workspace": "context-hub", "owner_object_type": "procedure", "owner_object_id": "proc_1", "owner_version_id": None,
    }


def test_the_judgement_reads_the_skills_and_the_context_over_the_interaction(a_member, monkeypatch):
    """Round 8, R8-B1: a step crossed on a SIBLING reaches `judgement.skills[].steps_crossed`, and the
    sibling's context call reaches `context_consulted` -- the verdict's own evidence, read once."""
    own = _path(2)
    own["steps"] = [_skill_step(1, "2", "analyze_result"), _step(2, "analyze_result")]
    sibling_steps = [_skill_step(0, "1", "execute_analyze_query_spec"), {**_step(2, "search_context"), "step_kind": "knowledge_read", "owner_workspace": "context-hub", "owner_object_type": "knowledge", "owner_object_id": "kn_1"}]
    reads = {"index": 0, "steps": 0}

    def _index(conn, *, project_id, trace_id, exclude=None):
        reads["index"] += 1
        return [{"path_id": "aip_exec", "state": "finalized", "outcome": "succeeded"}]

    def _steps(conn, *, path_id, project_id):
        reads["steps"] += 1
        return sibling_steps

    monkeypatch.setattr("core.ai_paths.load_path", lambda conn, *, path_id, project_id, **_kwargs: own)
    monkeypatch.setattr("core.ai_paths.paths_sharing_trace", _index)
    monkeypatch.setattr("core.ai_paths.load_steps", _steps)
    monkeypatch.setattr("core.ai_paths.skill_coverage", lambda conn, steps: [])
    handler, _ = _tool()
    judgement = handler(project_id=PROJECT, path_id="aip_1").structured_content["data"]["judgement"]
    assert judgement["skills"] == [{"skill_version": "proc_1@6", "steps_crossed": ["1", "2"]}]
    assert any(c.get("tool") == "search_context" for c in judgement["context_consulted"])
    # ONE read of the sibling index and ONE of the sibling's steps for the whole answer.
    assert reads == {"index": 1, "steps": 1}


def test_an_unreadable_sibling_narrows_the_mcp_reading_and_never_breaks_it(a_member, monkeypatch):
    """Round 8, R8-B2: the sibling INDEX is guarded -- the model keeps the path, its steps and its
    verdict (the index guard short-circuits, so `load_steps` is never reached here; the widening
    guard has its own test below, round 9 F3)."""
    def _boom(conn, **_kwargs):
        raise RuntimeError("sibling index unreadable")

    monkeypatch.setattr("core.ai_paths.load_path", lambda conn, *, path_id, project_id, **_kwargs: _path(3, "finalized"))
    monkeypatch.setattr("core.ai_paths.paths_sharing_trace", _boom)
    monkeypatch.setattr("core.ai_paths.load_steps", _boom)
    monkeypatch.setattr("core.ai_paths.skill_coverage", lambda conn, steps: [])
    handler, _ = _tool()
    data = handler(project_id=PROJECT, path_id="aip_1").structured_content["data"]
    assert data["state"] == "finalized" and data["steps_total"] == 3 and data["same_interaction"] == []
    assert data["judgement"]["policy"]["verdict"] == "unverifiable"


def test_an_unreadable_widening_narrows_the_mcp_reading_to_the_path(a_member, monkeypatch):
    """Round 9, F3: the WIDENING itself raising (not one sibling inside it) is caught -- the read answers."""
    def _boom(conn, **_kwargs):
        raise RuntimeError("widening unreadable")

    monkeypatch.setattr("core.ai_paths.load_path", lambda conn, *, path_id, project_id, **_kwargs: _path(3, "finalized"))
    monkeypatch.setattr("core.ai_paths.paths_sharing_trace", lambda conn, *, project_id, trace_id, exclude=None: [{"path_id": "aip_exec", "state": "finalized", "outcome": "succeeded"}])
    monkeypatch.setattr("core.ai_paths.interaction_steps", _boom)
    monkeypatch.setattr("core.ai_paths.skill_coverage", lambda conn, steps: [])
    handler, _ = _tool()
    data = handler(project_id=PROJECT, path_id="aip_1").structured_content["data"]
    assert data["state"] == "finalized" and data["steps_total"] == 3 and len(data["same_interaction"]) == 1
    assert data["judgement"]["policy"]["verdict"] == "unverifiable"


def test_the_mcp_reading_widens_once_and_the_loader_does_not(a_member, monkeypatch):
    """Round 9, F1: the ratchet on the single read -- the loader is asked NOT to widen, and the
    interaction is read exactly once for the whole answer."""
    from core import ai_paths

    asked: list[dict] = []
    widenings = {"n": 0}

    def _load(conn, *, path_id, project_id, **kwargs):
        asked.append(kwargs)
        return _path(3, "finalized")

    def _widen(conn, *, project_id, path, limit=4, siblings=None):
        widenings["n"] += 1
        return [dict(step, path_id="aip_1") for step in path["steps"]]

    monkeypatch.setattr(ai_paths, "load_path", _load)
    monkeypatch.setattr(ai_paths, "paths_sharing_trace", lambda conn, *, project_id, trace_id, exclude=None: [])
    monkeypatch.setattr(ai_paths, "interaction_steps", _widen)
    monkeypatch.setattr(ai_paths, "skill_coverage", lambda conn, steps: [])
    handler, _ = _tool()
    handler(project_id=PROJECT, path_id="aip_1")
    assert asked == [{"assess_over_interaction": False}]
    assert widenings == {"n": 1}


def test_the_summary_says_the_bound_when_more_siblings_share_the_trace_than_the_reading_spans(a_member, monkeypatch):
    """Round 9, F4: the index lists up to 10 siblings, the reading spans 4 -- said when it bites."""
    monkeypatch.setattr("core.ai_paths.load_path", lambda conn, *, path_id, project_id, **_kwargs: _path(2, "finalized"))
    monkeypatch.setattr("core.ai_paths.paths_sharing_trace", lambda conn, *, project_id, trace_id, exclude=None: [{"path_id": f"aip_{i}", "state": "finalized", "outcome": "succeeded"} for i in range(6)])
    read: list[str] = []
    monkeypatch.setattr("core.ai_paths.load_steps", lambda conn, *, path_id, project_id: read.append(path_id) or [])
    monkeypatch.setattr("core.ai_paths.skill_coverage", lambda conn, steps: [])
    handler, _ = _tool()
    result = handler(project_id=PROJECT, path_id="aip_1")
    text = result.content[0].text
    from core.ai_paths import INTERACTION_SIBLING_LIMIT

    # The sentence, the structured bound and the READ agree (round 10, N2): the sentence is not a string.
    assert f"6 other path(s) share this interaction's trace. The reading spans the first {INTERACTION_SIBLING_LIMIT} of them." in text
    data = result.structured_content["data"]
    assert data["siblings_total"] == 6 and data["siblings_read"] == INTERACTION_SIBLING_LIMIT
    assert read == [f"aip_{i}" for i in range(INTERACTION_SIBLING_LIMIT)]
