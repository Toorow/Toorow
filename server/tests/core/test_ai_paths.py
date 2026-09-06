"""Story 49.6 AC6: the AI Path owner refuses to guess.

These tests are written against the two ways an evidence surface lies. It can
claim something was verified when nothing was observed, and it can claim a
correct execution when the versions did not match. Both read as green.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest
from core.ai_paths import (
    FINDING_APPROVED_ALTERNATIVE,
    FINDING_FORBIDDEN_OBSERVED,
    FINDING_OUT_OF_ORDER,
    FINDING_REQUIRED_MISSING,
    FINDING_REQUIRED_OBSERVED,
    FINDING_UNVERIFIABLE,
    FINDING_VERSION_MISMATCH,
    NO_AI_PATH,
    OUTCOME_UNAVAILABLE,
    OVERLAY_NODE_TYPES,
    OVERLAY_STATE_FORBIDDEN,
    OVERLAY_STATE_MISSING,
    OVERLAY_STATE_USED,
    OVERLAY_STATE_VERSION_MISMATCH,
    VERDICT_FAIL,
    VERDICT_PASS,
    VERDICT_UNVERIFIABLE,
    AiPathError,
    _validate_trace_id,
    assess,
    graph_overlay,
)


def _step(ordinal, object_id, version=None, workspace="governance", object_type="semantic-view"):
    return {
        "ordinal": ordinal,
        "owner_workspace": workspace,
        "owner_object_type": object_type,
        "owner_object_id": object_id,
        "owner_version_id": version,
    }


def _required(object_id, version=None, alternatives=None, workspace="governance",
              object_type="semantic-view"):
    entry = {
        "owner_workspace": workspace,
        "owner_object_type": object_type,
        "owner_object_id": object_id,
    }
    if version is not None:
        entry["owner_version_id"] = version
    if alternatives is not None:
        entry["alternatives"] = alternatives
    return entry


# --- The literal ------------------------------------------------------------


def test_no_ai_path_is_the_exact_literal_story_50_1_requires():
    """Story 50.1 AC7 refuses absence, `null` and `deferred` by name. The exact
    string is the contract, so a typo here is a contract break, not cosmetics."""
    assert NO_AI_PATH == "No AI path"


def test_v2_content_preimage_adds_only_schema_and_explicit_step_detail() -> None:
    from core.ai_paths import (
        AI_PATH_CONTENT_V2,
        path_content_preimage,
    )

    steps = [
        {
            "ordinal": 0,
            "step_kind": "knowledge_read",
            "owner_workspace": "context-hub",
            "owner_object_type": "topic",
            "owner_object_id": "top_1",
            "owner_version_id": None,
            "skill_version_id": None,
            "skill_step_id": None,
            "tool_name": "search_context",
            "outcome": "succeeded",
            "detail": {"candidate_titles": ["Caf\u00e9"]},
        },
        {
            "ordinal": 1,
            "step_kind": "tool_call",
            "owner_workspace": None,
            "owner_object_type": None,
            "owner_object_id": None,
            "owner_version_id": None,
            "skill_version_id": None,
            "skill_step_id": None,
            "tool_name": "search_context",
            "outcome": "succeeded",
        },
    ]
    v1 = path_content_preimage(
        path_id="aip_1",
        outcome="succeeded",
        policy_snapshot_hash="p" * 64,
        steps=steps,
    )
    v2 = path_content_preimage(
        path_id="aip_1",
        outcome="succeeded",
        policy_snapshot_hash="p" * 64,
        steps=steps,
        content_hash_contract=AI_PATH_CONTENT_V2,
    )

    assert "schema_version" not in v1
    assert all("detail" not in step for step in v1["steps"])
    assert v2 == {
        **v1,
        "schema_version": AI_PATH_CONTENT_V2,
        "steps": [
            {**v1["steps"][0], "detail": {"candidate_titles": ["Caf\u00e9"]}},
            {**v1["steps"][1], "detail": None},
        ],
    }


def test_v2_canonicalizer_is_compact_sorted_utf8_and_detail_sensitive() -> None:
    from core.ai_paths import (
        AI_PATH_CONTENT_V2,
        canonical_json_v2,
        path_content_preimage,
    )

    assert canonical_json_v2({"z": "Caf\u00e9", "a": [2, 1]}) == (
        '{"a":[2,1],"z":"Caf\u00e9"}'
    )
    base = {
        "path_id": "aip_1",
        "outcome": "succeeded",
        "policy_snapshot_hash": "p" * 64,
        "steps": [
            {
                "ordinal": 0,
                "step_kind": "knowledge_read",
                "owner_workspace": None,
                "owner_object_type": None,
                "owner_object_id": None,
                "owner_version_id": None,
                "skill_version_id": None,
                "skill_step_id": None,
                "tool_name": "search_context",
                "outcome": "succeeded",
                "detail": {"candidate_ids": ["ctx_1"]},
            }
        ],
    }
    changed = json.loads(json.dumps(base))
    changed["steps"][0]["detail"]["candidate_ids"] = ["ctx_2"]
    v1_base = path_content_preimage(**base)
    v1_changed = path_content_preimage(**changed)
    v2_base = path_content_preimage(
        **base, content_hash_contract=AI_PATH_CONTENT_V2
    )
    v2_changed = path_content_preimage(
        **changed, content_hash_contract=AI_PATH_CONTENT_V2
    )

    from core.governance_rule_sets import content_hash

    assert content_hash(v1_base) == content_hash(v1_changed)
    assert hashlib.sha256(canonical_json_v2(v2_base).encode("utf-8")).hexdigest() != (
        hashlib.sha256(canonical_json_v2(v2_changed).encode("utf-8")).hexdigest()
    )


@pytest.mark.parametrize(
    ("contract", "expected_clock"),
    [(None, "NOW()"), ("ai-path-content.v2", "clock_timestamp()")],
)
def test_finalization_clock_matches_the_persisted_contract(
    monkeypatch, contract, expected_clock
) -> None:
    from core import ai_paths

    class Cursor:
        def __init__(self):
            self.row = None
            self.update = ""

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def execute(self, statement, params=()):
            normalized = " ".join(str(statement).split())
            if normalized.startswith("SELECT lifecycle"):
                snapshot = {"content_hash_contract": contract} if contract else {}
                self.row = (ai_paths.LIFECYCLE_RECORDING, "p" * 64, snapshot)
            elif normalized.startswith("UPDATE app.ai_paths"):
                self.update = normalized
                self.row = ("aip_1", "succeeded", datetime.now(timezone.utc), params[1])
            else:  # pragma: no cover - steps are patched below
                raise AssertionError(normalized)

        def fetchone(self):
            return self.row

    cursor = Cursor()
    conn = type("Connection", (), {"cursor": lambda self: cursor})()
    monkeypatch.setattr(ai_paths, "_load_steps", lambda *_args: [])

    ai_paths.finalize_path(
        conn,
        path_id="aip_1",
        project_id="proj_1",
        outcome="succeeded",
    )

    assert f"ended_at = {expected_clock}" in cursor.update


# --- Story 65.4: one bounded projection ------------------------------------


def _observed_path(*, lifecycle="finalized", outcome="succeeded", steps=None):
    at = datetime(2026, 8, 10, 8, 0, tzinfo=timezone.utc)
    return {
        "id": "aip_EXAMPLE",
        "lifecycle": lifecycle,
        "outcome": outcome,
        "actor": "must-not-cross@example.com",
        "w3c_trace_id": "f" * 32,
        "policy_snapshot_hash": "p" * 64,
        "assessment": {"reasoning": "must not cross"},
        "started_at": at,
        "ended_at": at,
        "steps": steps
        if steps is not None
        else [
            {
                "ordinal": 0,
                "step_kind": "skill_step",
                "outcome": "succeeded",
                "observed_at": at,
                "tool_name": "read_context",
                "owner_workspace": "context-hub",
                "owner_object_type": "context-topic",
                "owner_object_id": "ctx_EXAMPLE",
                "owner_version_id": "ctxv_EXAMPLE",
                "skill_version_id": "proc_EXAMPLE@3",
                "skill_step_id": "2",
                "skill": {"state": "resolved", "label": "Read context"},
                "evidence_record_id": "ev_EXAMPLE",
                "detail": {
                    "missing_context": ["required_but_missing", "unsafe free prose"],
                    "reasoning": "must not cross",
                },
            }
        ],
    }


def test_observed_projector_emits_only_the_strict_completed_contract(monkeypatch):
    from core import ai_paths

    monkeypatch.setattr(ai_paths, "load_path", lambda *args, **kwargs: _observed_path())
    projection = ai_paths.project_observed_ai_path(
        object(), project_id="proj_EXAMPLE", ai_path="aip_EXAMPLE"
    )

    assert projection == {
        "schema_version": "observed-ai-path.v1",
        "state": "completed",
        "path_id": "aip_EXAMPLE",
        "lifecycle": "finalized",
        "outcome": "succeeded",
        "steps": [
            {
                "ordinal": 0,
                "step_kind": "skill_step",
                "outcome": "succeeded",
                "observed_at": "2026-08-10T08:00:00+00:00",
                "tool_name": "read_context",
                "owner": {
                    "workspace": "context-hub",
                    "object_type": "context-topic",
                    "object_id": "ctx_EXAMPLE",
                    "version_id": "ctxv_EXAMPLE",
                },
                "skill": {
                    "version_id": "proc_EXAMPLE@3",
                    "step_id": "2",
                    "state": "resolved",
                    "label": "Read context",
                },
                "evidence_record_id": "ev_EXAMPLE",
                "missing_context": ["required_but_missing"],
            }
        ],
    }
    encoded = json.dumps(projection, sort_keys=True)
    for forbidden in ("actor", "trace", "policy", "assessment", "detail", "reasoning", "email"):
        assert forbidden not in encoded


def test_observed_projector_states_are_honest_and_nondisclosing(monkeypatch):
    from core import ai_paths

    assert ai_paths.project_observed_ai_path(
        object(), project_id="proj_EXAMPLE", ai_path="No AI path"
    ) == {
        "schema_version": "observed-ai-path.v1",
        "state": "human_absent",
        "literal": "No AI path",
    }

    for outcome in ("failed", "refused", "unavailable"):
        monkeypatch.setattr(
            ai_paths,
            "load_path",
            lambda *args, outcome=outcome, **kwargs: _observed_path(outcome=outcome),
        )
        failed = ai_paths.project_observed_ai_path(
            object(), project_id="proj_EXAMPLE", ai_path="aip_EXAMPLE"
        )
        assert failed["state"] == "failed"
        assert failed["outcome"] == outcome

    for lifecycle in ("recording", None):
        monkeypatch.setattr(
            ai_paths,
            "load_path",
            lambda *args, lifecycle=lifecycle, **kwargs: _observed_path(lifecycle=lifecycle),
        )
        assert ai_paths.project_observed_ai_path(
            object(), project_id="proj_EXAMPLE", ai_path="aip_FOREIGN"
        ) == {
            "schema_version": "observed-ai-path.v1",
            "state": "unavailable",
            "reason": "unavailable",
        }


def test_observed_projector_refuses_unreadable_unsafe_and_oversize_paths(monkeypatch):
    from core import ai_paths

    unavailable = {
        "schema_version": "observed-ai-path.v1",
        "state": "unavailable",
        "reason": "unavailable",
    }

    def unreadable(*args, **kwargs):
        raise ai_paths.AiPathNotFound("foreign or absent")

    monkeypatch.setattr(ai_paths, "load_path", unreadable)
    assert ai_paths.project_observed_ai_path(
        object(), project_id="proj_EXAMPLE", ai_path="aip_FOREIGN"
    ) == unavailable

    unsafe = _observed_path()
    unsafe["steps"][0]["observed_at"] = datetime(2026, 8, 10, 8, 0)
    monkeypatch.setattr(ai_paths, "load_path", lambda *args, **kwargs: unsafe)
    assert ai_paths.project_observed_ai_path(
        object(), project_id="proj_EXAMPLE", ai_path="aip_EXAMPLE"
    ) == unavailable

    oversized = _observed_path(steps=[_observed_path()["steps"][0]] * 201)
    monkeypatch.setattr(ai_paths, "load_path", lambda *args, **kwargs: oversized)
    assert ai_paths.project_observed_ai_path(
        object(), project_id="proj_EXAMPLE", ai_path="aip_EXAMPLE"
    ) == unavailable

    byte_heavy_steps = []
    for ordinal in range(200):
        step = dict(_observed_path()["steps"][0])
        step.update(
            ordinal=ordinal,
            tool_name="t" * 512,
            owner_object_type="o" * 512,
            owner_object_id="i" * 512,
            owner_version_id="v" * 512,
            evidence_record_id="e" * 512,
        )
        byte_heavy_steps.append(step)
    monkeypatch.setattr(
        ai_paths,
        "load_path",
        lambda *args, **kwargs: _observed_path(steps=byte_heavy_steps),
    )
    assert ai_paths.project_observed_ai_path(
        object(), project_id="proj_EXAMPLE", ai_path="aip_EXAMPLE"
    ) == unavailable


# --- Trace ids --------------------------------------------------------------


def test_an_all_zero_trace_id_is_refused():
    """The W3C invalid value. Stored, it would correlate every uninstrumented
    execution into one apparent trace -- worse than storing nothing."""
    with pytest.raises(AiPathError):
        _validate_trace_id("0" * 32)


@pytest.mark.parametrize("candidate", ["abc", "g" * 32, "A" * 31, ""])
def test_a_malformed_trace_id_is_refused(candidate):
    with pytest.raises(AiPathError):
        _validate_trace_id(candidate)


def test_a_valid_trace_id_is_normalized_to_lowercase():
    assert _validate_trace_id("A" * 32) == "a" * 32


def test_an_absent_trace_id_is_allowed_because_correlation_is_not_identity():
    assert _validate_trace_id(None) is None


# --- Assessment: the distinctions AC6 requires ------------------------------


def test_a_required_node_reached_at_the_pinned_version_passes():
    result = assess(
        {"required": [_required("sv_1", version="svv_1")]},
        [_step(0, "sv_1", version="svv_1")],
    )
    assert result["verdict"] == VERDICT_PASS
    assert result["findings"][0]["finding"] == FINDING_REQUIRED_OBSERVED


def test_a_required_node_never_reached_fails():
    result = assess({"required": [_required("sv_1")]}, [_step(0, "sv_other")])
    assert result["verdict"] == VERDICT_FAIL
    assert any(f["finding"] == FINDING_REQUIRED_MISSING for f in result["findings"])


def test_the_wrong_version_of_the_right_object_is_a_failure_not_a_pass():
    """The defect this whole table exists to catch: the object was reached, so a
    presence check would go green while the answer came from another version."""
    result = assess(
        {"required": [_required("sv_1", version="svv_1")]},
        [_step(0, "sv_1", version="svv_9")],
    )
    assert result["verdict"] == VERDICT_FAIL
    mismatch = [f for f in result["findings"] if f["finding"] == FINDING_VERSION_MISMATCH]
    assert mismatch[0]["expected_version"] == "svv_1"
    assert mismatch[0]["observed_version"] == "svv_9"


def test_a_step_without_a_version_is_unverifiable_never_a_pass():
    """Missing evidence and evidence of compliance are different answers. A
    boolean would merge them, and the merged value would read as compliant."""
    result = assess(
        {"required": [_required("sv_1", version="svv_1")]},
        [_step(0, "sv_1", version=None)],
    )
    assert result["verdict"] == VERDICT_UNVERIFIABLE
    assert any(f["finding"] == FINDING_UNVERIFIABLE for f in result["findings"])


def test_an_approved_alternative_is_compliance_not_a_deviation():
    result = assess(
        {"required": [_required("sv_1", alternatives=[_required("sv_alt")])]},
        [_step(0, "sv_alt")],
    )
    assert result["verdict"] == VERDICT_PASS
    assert any(f["finding"] == FINDING_APPROVED_ALTERNATIVE for f in result["findings"])


def test_a_forbidden_node_that_was_reached_fails():
    result = assess(
        {"required": [_required("sv_1")], "forbidden": [_required("sv_bad")]},
        [_step(0, "sv_1"), _step(1, "sv_bad")],
    )
    assert result["verdict"] == VERDICT_FAIL
    assert any(f["finding"] == FINDING_FORBIDDEN_OBSERVED for f in result["findings"])


def test_required_steps_taken_in_the_wrong_order_fail_when_order_is_policy():
    result = assess(
        {"required": [_required("sv_1"), _required("sv_2")], "ordered": True},
        [_step(0, "sv_2"), _step(1, "sv_1")],
    )
    assert result["verdict"] == VERDICT_FAIL
    assert any(f["finding"] == FINDING_OUT_OF_ORDER for f in result["findings"])


def test_the_same_steps_pass_when_order_is_not_policy():
    """Order is only a violation when the pinned policy said it mattered."""
    result = assess(
        {"required": [_required("sv_1"), _required("sv_2")]},
        [_step(0, "sv_2"), _step(1, "sv_1")],
    )
    assert result["verdict"] == VERDICT_PASS


# --- The absence cases ------------------------------------------------------


def test_an_execution_with_no_observed_steps_is_unverifiable():
    result = assess({"required": [_required("sv_1")]}, [])
    assert result["verdict"] == VERDICT_UNVERIFIABLE


def test_an_unavailable_outcome_is_unverifiable_even_with_steps():
    """Instrumentation that was lost mid-run cannot be judged on what survived."""
    result = assess(
        {"required": [_required("sv_1")]},
        [_step(0, "sv_1")],
        outcome=OUTCOME_UNAVAILABLE,
    )
    assert result["verdict"] == VERDICT_UNVERIFIABLE


def test_an_empty_policy_is_unverifiable_rather_than_a_pass():
    """No policy pinned is not a violation -- and it is not a pass either.

    This asserted `pass`, and the reasoning was sound as far as it went: nothing
    was violated. But `pass` claims more than "no violation" -- it claims the
    execution was judged and conformed, and with no required or forbidden node
    pinned, nothing was judged at all. The verdict vocabulary is three-valued
    precisely so those two do not merge, as this function's own docstring says.

    It was not academic. `ai_path_recorder` pins a snapshot with no expectation
    (a middleware has none to declare), so EVERY path recorded by Story 49.6 read
    `pass`, and the workbench displayed it as an assessment. Jean's arbitration
    of 2026-07-31 put relevance in this assessor rather than in a heuristic, and
    the corollary is that an unasked question answers `unverifiable`.

    The original invariant survives, asserted explicitly below: it is still not a
    failure.
    """
    verdict = assess({}, [])

    assert verdict["verdict"] == VERDICT_UNVERIFIABLE
    assert verdict["verdict"] != VERDICT_FAIL
    assert "nothing to judge" in verdict["findings"][0]["detail"]


def test_a_bare_tool_call_does_not_satisfy_a_requirement_and_does_not_excuse_it():
    """A step that reached nothing governed stays visible in the path, but it is
    not evidence that a governed node was used.

    The verdict is `fail`, not `unverifiable`, and the difference matters: the
    instrumentation demonstrably worked -- it produced a step -- and it recorded
    no access to the required node. Downgrading this to `unverifiable` would let
    any execution escape assessment by making one ungoverned call.
    """
    bare = {
        "ordinal": 0,
        "owner_workspace": None,
        "owner_object_type": None,
        "owner_object_id": None,
        "owner_version_id": None,
    }
    result = assess({"required": [_required("sv_1")]}, [bare])
    assert result["verdict"] == VERDICT_FAIL
    assert any(f["finding"] == FINDING_REQUIRED_MISSING for f in result["findings"])


# --- The Knowledge Graph overlay (AC7) --------------------------------------
#
# The overlay is a PROJECTION. Every test below asks one question in a
# different form: can it draw something the path did not observe, or fail to
# draw something the path did? Both are how a decoration becomes a second
# opinion on the evidence it decorates.


def _path(steps, snapshot, *, outcome=None, lifecycle="finalized"):
    """One loaded path, assembled exactly as `load_path` assembles it."""
    return {
        "id": "aip_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "lifecycle": lifecycle,
        "outcome": outcome,
        "steps": steps,
        "assessment": assess(snapshot, steps, outcome=outcome),
    }


def test_a_step_that_reached_a_drawable_node_decorates_it_as_used():
    overlay = graph_overlay(
        _path(
            [_step(0, "top_1", object_type="topic", workspace="context-hub")],
            {"required": [_required("top_1", object_type="topic", workspace="context-hub")]},
        )
    )
    assert overlay["nodes"] == [
        {
            "node_type": "topic",
            "node_id": "top_1",
            "owner_workspace": "context-hub",
            "ordinals": [0],
            "state": OVERLAY_STATE_USED,
        }
    ]
    assert overlay["unrepresented_steps"] == 0


def test_a_required_node_no_step_reached_is_drawn_as_missing():
    """`missing` is the only state with NO step behind it. If the overlay could
    project observed steps alone, a policy violation would render as an
    ordinary graph -- the deviation invisible on the very surface built to show
    it."""
    overlay = graph_overlay(
        _path(
            [_step(0, "top_other", object_type="topic", workspace="context-hub")],
            {"required": [_required("top_1", object_type="topic", workspace="context-hub")]},
        )
    )
    states = {(n["node_id"], n["state"]) for n in overlay["nodes"]}
    assert ("top_1", OVERLAY_STATE_MISSING) in states
    assert ("top_other", OVERLAY_STATE_USED) in states
    missing = next(n for n in overlay["nodes"] if n["node_id"] == "top_1")
    assert missing["ordinals"] == [], "nothing walked it, so it carries no walk order"


def test_the_wrong_version_outranks_used_on_the_same_node():
    """The node WAS reached, so it is `used` too. A canvas draws one state, and
    drawing the visit would hide the deviation -- the exact defect `assess`
    exists to prevent, arrived at through the decoration instead."""
    overlay = graph_overlay(
        _path(
            [_step(0, "sv_1", version="svv_9", object_type="semantic-view")],
            {"required": [_required("sv_1", version="svv_1", object_type="semantic-view")]},
        )
    )
    node = next(n for n in overlay["nodes"] if n["node_id"] == "sv_1")
    assert node["state"] == OVERLAY_STATE_VERSION_MISMATCH
    assert node["expected_version"] == "svv_1"
    assert node["observed_version"] == "svv_9"
    assert node["node_type"] == "semantic_view", "the evidence spelling resolved to the graph's"


def test_a_forbidden_node_outranks_every_other_state():
    overlay = graph_overlay(
        _path(
            [_step(0, "top_1", object_type="topic", workspace="context-hub")],
            {
                "required": [_required("top_1", object_type="topic", workspace="context-hub")],
                "forbidden": [_required("top_1", object_type="topic", workspace="context-hub")],
            },
        )
    )
    node = next(n for n in overlay["nodes"] if n["node_id"] == "top_1")
    assert node["state"] == OVERLAY_STATE_FORBIDDEN


def test_a_step_that_reached_nothing_drawable_is_counted_never_invented():
    """Migration 150 states this on the columns themselves and AC7 repeats it.
    A bare tool call and a version reference are both real steps with no node
    on this canvas; giving them one would put an object on the graph that no
    graph query can resolve."""
    overlay = graph_overlay(
        _path(
            [
                {"ordinal": 0, "step_kind": "tool_call", "owner_workspace": None,
                 "owner_object_type": None, "owner_object_id": None},
                _step(1, "svv_1", object_type="semantic-view-version"),
                _step(2, "top_1", object_type="topic", workspace="context-hub"),
            ],
            {"required": [_required("top_1", object_type="topic", workspace="context-hub")]},
        )
    )
    assert [n["node_id"] for n in overlay["nodes"]] == ["top_1"]
    assert overlay["unrepresented_steps"] == 2


def test_a_version_type_is_not_mapped_onto_the_object_it_versions():
    """The id spaces differ: a `semantic-view-version` id identifies a version,
    and `semantic_view` nodes are keyed by `app.semantic_views.id`. Mapping one
    onto the other decorates a node with an id that never identified it."""
    assert "semantic-view-version" not in OVERLAY_NODE_TYPES
    assert OVERLAY_NODE_TYPES["semantic-view"] == "semantic_view"


def test_a_node_reached_twice_appears_once_carrying_both_ordinals():
    overlay = graph_overlay(
        _path(
            [
                _step(0, "top_1", object_type="topic", workspace="context-hub"),
                _step(3, "top_1", object_type="topic", workspace="context-hub"),
            ],
            {"required": [_required("top_1", object_type="topic", workspace="context-hub")]},
        )
    )
    assert len(overlay["nodes"]) == 1
    assert overlay["nodes"][0]["ordinals"] == [0, 3]


def test_an_approved_alternative_draws_as_used_not_as_a_deviation():
    """It IS the policy being followed (`_FAILING_FINDINGS` says so). A canvas
    that flagged it would contradict the verdict printed beside it."""
    overlay = graph_overlay(
        _path(
            [_step(0, "top_alt", object_type="topic", workspace="context-hub")],
            {
                "required": [
                    _required(
                        "top_1",
                        object_type="topic",
                        workspace="context-hub",
                        alternatives=[
                            {"owner_workspace": "context-hub",
                             "owner_object_type": "topic",
                             "owner_object_id": "top_alt"}
                        ],
                    )
                ]
            },
        )
    )
    node = next(n for n in overlay["nodes"] if n["node_id"] == "top_alt")
    assert node["state"] == OVERLAY_STATE_USED
    assert overlay["verdict"] == VERDICT_PASS


def test_a_recording_path_carries_its_lifecycle_so_a_canvas_cannot_read_it_as_settled():
    """`AiPathPage` already refuses to let a `recording` path look finished. A
    second surface drawing the same evidence must be able to refuse it too, and
    without a second fetch."""
    overlay = graph_overlay(
        _path(
            [_step(0, "top_1", object_type="topic", workspace="context-hub")],
            {"required": [_required("top_1", object_type="topic", workspace="context-hub")]},
            lifecycle="recording",
        )
    )
    assert overlay["lifecycle"] == "recording"
    assert overlay["schema_version"] == "ai-path-graph-overlay.v1"


def test_an_unjudgeable_path_decorates_nothing_rather_than_drawing_a_clean_graph():
    """No observed steps returns `unverifiable` with a finding that names NO
    node. The overlay is then empty -- and an empty overlay must read as "not
    judged", never as "nothing deviated"."""
    overlay = graph_overlay(_path([], {"required": [_required("top_1", object_type="topic")]}))
    assert overlay["verdict"] == VERDICT_UNVERIFIABLE
    assert overlay["nodes"] == []
    assert overlay["unrepresented_steps"] == 0


# --- Events leave as references, never as nodes (AC9) -----------------------


def test_an_observed_event_leaves_as_a_reference_not_as_a_graph_node():
    """AC9 says a graph "may DISPLAY an Event", and `data.md` keeps it owned by
    its Datastream. The mindmap's vocabulary is closed at ten types and carries
    none, so drawing one would mean inventing the very node type the target
    withholds."""
    overlay = graph_overlay(
        _path(
            [
                _step(0, "evt_1", object_type="context-event", workspace="context-hub"),
                _step(1, "top_1", object_type="topic", workspace="context-hub"),
            ],
            {"required": [_required("top_1", object_type="topic", workspace="context-hub")]},
        )
    )
    assert [n["node_id"] for n in overlay["nodes"]] == ["top_1"]
    assert overlay["event_references"] == [{"event_id": "evt_1", "ordinals": [0]}]


def test_an_event_step_is_not_counted_as_unrepresented():
    """It reached a REACHABLE owner. Sweeping it into `unrepresented_steps`
    would tell the reader the path touched nothing there, while the Event sits
    one deep-link away."""
    overlay = graph_overlay(
        _path(
            [_step(0, "evt_1", object_type="context-event", workspace="context-hub")],
            {"required": [_required("top_1", object_type="topic", workspace="context-hub")]},
        )
    )
    assert overlay["unrepresented_steps"] == 0
    assert len(overlay["event_references"]) == 1


def test_an_event_step_naming_no_event_is_unrepresented_not_a_blank_reference():
    """A reference with no id points at nothing. Emitting one would put an
    "unavailable" row on the screen for a step that named no Event at all --
    two different facts wearing the same word."""
    overlay = graph_overlay(
        _path(
            [
                {"ordinal": 0, "step_kind": "knowledge_read", "owner_workspace": "context-hub",
                 "owner_object_type": "context-event", "owner_object_id": None},
            ],
            {"required": [_required("top_1", object_type="topic", workspace="context-hub")]},
        )
    )
    assert overlay["event_references"] == []
    assert overlay["unrepresented_steps"] == 1


def test_the_same_event_reached_twice_is_one_reference_with_both_ordinals():
    overlay = graph_overlay(
        _path(
            [
                _step(0, "evt_1", object_type="context-event", workspace="context-hub"),
                _step(2, "evt_1", object_type="context-event", workspace="context-hub"),
            ],
            {"required": [_required("top_1", object_type="topic", workspace="context-hub")]},
        )
    )
    assert overlay["event_references"] == [{"event_id": "evt_1", "ordinals": [0, 2]}]


def test_the_projection_resolves_no_event_itself():
    """`graph_overlay` returns ids and walk order and nothing else. The moment
    it returned a Datastream or a route, Context Hub would have become the
    Event's second owner through a helper rather than through a table."""
    overlay = graph_overlay(
        _path(
            [_step(0, "evt_1", object_type="context-event", workspace="context-hub")],
            {"required": [_required("top_1", object_type="topic", workspace="context-hub")]},
        )
    )
    assert set(overlay["event_references"][0]) == {"event_id", "ordinals"}


# ---------------------------------------------------------------------------
# Le pas de Skill RESOLU par le chargeur -- Story 45.7, AC1/AC2
# ---------------------------------------------------------------------------


_HEADER_COLUMNS = 15
_SEQUENCE = (
    'name: demo\ndescription: "d"\n'
    "steps:\n"
    "  - step: 1\n    action: read\n    label: Read the spec\n    target: docs/a.md\n"
    "  - step: 2\n    action: run\n    label: Run it\n    command: make test\n"
)


class _PathCursor:
    """Repond aux TROIS instructions que `load_path` emet, et a elles seules."""

    def __init__(self, steps, versions):
        self._steps = steps
        self._versions = versions
        self._rows = []
        self._one = None

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def execute(self, sql, params=None):
        flat = " ".join(sql.split())
        if "FROM app.ai_paths" in flat:
            self._one = ("path_1", "org_1", "proj_EXAMPLE", "recording", None,
                         None, "actor", None, None, None, None, None, {}, None, None)
            assert len(self._one) == _HEADER_COLUMNS
        elif "FROM app.ai_path_steps" in flat:
            self._rows = list(self._steps)
        elif "FROM app.procedures_versions" in flat:
            self._one = self._versions.get((params[0], params[1]))
        else:  # pragma: no cover -- an unexpected statement must be visible
            raise AssertionError(f"unexpected SQL: {flat}")

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._one


class _PathConn:
    def __init__(self, steps, versions):
        self._steps = steps
        self._versions = versions

    def cursor(self):
        return _PathCursor(self._steps, self._versions)


def _step_row(ordinal, *, skill_version_id=None, skill_step_id=None):
    """Les 14 colonnes de `_STEP_COLUMNS`, dans l'ordre."""
    return (
        f"aps_{ordinal}", ordinal, "tool_call", "context-hub", None, None, None,
        skill_version_id, skill_step_id, "search_context", "ok", None, None, None,
    )


def test_the_loader_resolves_a_pinned_step_to_what_that_version_declared():
    """REJECT 45.7 du 2026-08-05 : `skill_steps.resolve()` n'avait AUCUN lecteur
    sur une surface de chemin -- `ai_paths_api` rendait le `skill_version_id`
    brut et `trace_observation` le couple brut, ni intitule ni action. Les deux
    surfaces passent par `load_path` : la resolution est posee la, une fois,
    plutot que deux fois en aval."""
    from core.ai_paths import load_path, wire_step_projection

    conn = _PathConn(
        [_step_row(0, skill_version_id="proc_1@3", skill_step_id="2"), _step_row(1)],
        {("proc_1", 3): (_SEQUENCE, "demo")},
    )
    path = load_path(conn, path_id="path_1", project_id="proj_EXAMPLE")
    pinned, bare = path["steps"]

    assert pinned["skill"]["state"] == "resolved"
    assert pinned["skill"]["label"] == "Run it"
    assert pinned["skill"]["action"] == "run"
    # Un pas sans epingle ne porte pas de dictionnaire vide : il ne porte rien.
    assert bare.get("skill") is None

    # Et la forme de fil des DEUX lecteurs porte le couple ENTIER : une moitie
    # de couple ne se resout pas, et la projection n'en portait qu'une.
    wire = wire_step_projection(pinned)
    assert wire["skill_version_id"] == "proc_1@3"
    assert wire["skill_step_id"] == "2"
    assert wire["skill"]["label"] == "Run it"


def test_the_loader_keeps_reading_a_path_whose_pin_no_longer_resolves():
    """`unknown_version` / `unavailable`, jamais une sequence vide : << on n'a
    pas pu lire >> n'est pas << il n'y a rien >>. Et une resolution qui echoue
    n'emporte pas la lecture de la trace."""
    from core.ai_paths import load_path

    conn = _PathConn(
        [_step_row(0, skill_version_id="proc_1@99", skill_step_id="2")],
        {("proc_1", 3): (_SEQUENCE, "demo")},
    )
    path = load_path(conn, path_id="path_1", project_id="proj_EXAMPLE")
    assert path["steps"][0]["skill"]["state"] == "unknown_version"
    assert path["steps"][0]["skill"]["skill_version_id"] == "proc_1@99"


# ---------------------------------------------------------------------------
# Story 65.6 -- the provisional step is a strict subset of final evidence.
# ---------------------------------------------------------------------------


def test_live_step_projection_is_closed_and_has_no_persisted_identity():
    from core.ai_paths import project_observed_ai_path_progress_step

    projected = project_observed_ai_path_progress_step(
        {
            "step_kind": "knowledge_read",
            "outcome": "succeeded",
            "tool_name": "search_context",
            "owner_workspace": "context-hub",
            "owner_object_type": "topic",
            "owner_object_id": "ctx_1",
            "owner_version_id": "ctxv_1",
            "evidence_record_id": "ev_1",
            "detail": {
                "missing_context": ["not_recorded", "unsafe", "not_recorded"],
                "candidate_ids": ["ctx_1", "ctx_2"],
                "reasoning": "never wire this",
            },
        }
    )

    assert projected == {
        "step_kind": "knowledge_read",
        "outcome": "succeeded",
        "missing_context": ["not_recorded"],
        "tool_name": "search_context",
        "owner": {
            "workspace": "context-hub",
            "object_type": "topic",
            "object_id": "ctx_1",
            "version_id": "ctxv_1",
        },
        "evidence_record_id": "ev_1",
    }
    assert not ({"ordinal", "observed_at", "skill", "detail"} & projected.keys())


@pytest.mark.parametrize(
    "step",
    [
        {"step_kind": "unknown", "outcome": "succeeded"},
        {"step_kind": "knowledge_read", "outcome": "unknown"},
        {
            "step_kind": "knowledge_read",
            "outcome": "succeeded",
            "tool_name": "x" * 513,
        },
        {
            "step_kind": "knowledge_read",
            "outcome": "succeeded",
            "owner_workspace": "context-hub",
            "owner_object_type": "topic",
        },
        {
            "step_kind": "knowledge_read",
            "outcome": "succeeded",
            "ordinal": 7,
        },
    ],
)
def test_live_step_projection_refuses_unknown_oversized_or_partial_fields(step):
    from core.ai_paths import project_observed_ai_path_progress_step

    with pytest.raises(ValueError):
        project_observed_ai_path_progress_step(step)


def test_skill_coverage_says_crossed_skipped_and_unobservable_per_prescribed_step(monkeypatch) -> None:
    """2026-09-05: the path marked the steps it crossed and said nothing of the
    others. The served version's whole sequence is read back; a step with a tool
    the walk never called is `skipped`, a read of a target is `unobservable`."""
    from core import ai_paths

    monkeypatch.setattr(
        ai_paths,
        "_prescribed_steps",
        lambda conn, *, procedure_id, version_number: (
            "youtube-video-analyst",
            [
                {"step": "1", "label": "Read the catalogue", "tool": None, "action": "read"},
                {"step": "2", "label": "Run the pinned query", "tool": "execute_analyze_query_spec", "action": "analyze"},
                {"step": "3", "label": "Read the Result", "tool": "analyze_result", "action": "read"},
                {"step": "6", "label": "Mount the answer", "tool": "render_analyze_result", "action": "suggest"},
            ],
        ),
    )
    steps = [
        {"step_kind": "skill_step", "skill_version_id": "proc_1@6", "skill_step_id": "2", "tool_name": "execute_analyze_query_spec"},
        {"step_kind": "tool_call", "tool_name": "execute_analyze_query_spec"},
        {"step_kind": "skill_step", "skill_version_id": "proc_1@6", "skill_step_id": 6, "tool_name": "render_analyze_result"},
    ]
    coverage = ai_paths.skill_coverage(object(), steps)
    assert len(coverage) == 1
    entry = coverage[0]
    assert entry["skill_name"] == "youtube-video-analyst" and entry["prescribed"] == 4
    assert entry["crossed"] == ["2", "6"] and entry["skipped"] == ["3"] and entry["unobservable"] == ["1"]
    assert [line["state"] for line in entry["steps"]] == ["unobservable", "crossed", "skipped", "crossed"]
    assert ai_paths.skill_coverage(object(), [{"step_kind": "tool_call", "tool_name": "x"}]) == []


def _skill_policy(*, required=(1, 2), expected=(3,)):
    return {
        "recorded_by": "mcp_tool_middleware",
        "ordered_skill_steps": True,
        "expected_skill_steps": [
            *[{"skill_version": "proc_x@3", "step": str(n), "tool": f"tool_{n}", "required": True} for n in required],
            *[{"skill_version": "proc_x@3", "step": str(n), "tool": f"tool_{n}", "required": False} for n in expected],
        ],
    }


def _crossed(*pairs):
    return [
        {"ordinal": ordinal, "step_kind": "skill_step", "skill_version_id": "proc_x@3", "skill_step_id": str(step),
         "tool_name": f"tool_{step}", "outcome": "succeeded"}
        for ordinal, step in pairs
    ]


def test_the_skills_required_steps_crossed_in_order_pass_and_a_skipped_one_fails() -> None:
    """2026-09-05: the Skill is the expectation. Judged on `skill_step` rows, never on tool names."""
    from core import ai_paths

    verdict = ai_paths.assess(_skill_policy(), _crossed((0, 1), (1, 2)), outcome="succeeded")
    assert verdict["verdict"] == "pass"
    kinds = [f["finding"] for f in verdict["findings"]]
    assert kinds.count("required_skill_step_crossed") == 2 and "expected_skill_step_skipped" in kinds

    skipped = ai_paths.assess(_skill_policy(), _crossed((0, 1)), outcome="succeeded")
    assert skipped["verdict"] == "fail"
    assert {"finding": "required_skill_step_skipped", "skill_step": "proc_x@3 step 2", "tool": "tool_2"} in skipped["findings"]

    disorder = ai_paths.assess(_skill_policy(), _crossed((0, 2), (1, 1)), outcome="succeeded")
    assert disorder["verdict"] == "fail" and any(f["finding"] == "skill_steps_out_of_order" for f in disorder["findings"])

    # A tool call with the right name but outside the Skill (no skill_step row) does not count as
    # a crossing -- and with NO crossing at all the walk is unverifiable, not failed (round 2, finding 3).
    impostor = [{"ordinal": 0, "step_kind": "tool_call", "tool_name": "tool_1", "outcome": "succeeded"}]
    impostor_verdict = ai_paths.assess(_skill_policy(required=(1,), expected=()), impostor, outcome="succeeded")
    assert impostor_verdict["verdict"] == "unverifiable"
    assert any("no step crossing was observed" in str(f.get("detail")) for f in impostor_verdict["findings"])
    # One crossing observed, another required step outside the Skill: that one IS a skip.
    half = impostor + _crossed((1, 2))
    assert ai_paths.assess(_skill_policy(required=(1, 2), expected=()), half, outcome="succeeded")["verdict"] == "fail"


def test_a_skill_without_required_steps_is_reported_not_judged() -> None:
    from core import ai_paths

    verdict = ai_paths.assess(_skill_policy(required=(), expected=(1, 2)), _crossed((0, 1)), outcome="succeeded")
    assert verdict["verdict"] == "unverifiable"
    assert any("required: true" in str(f.get("detail")) for f in verdict["findings"])
    assert any(f["finding"] == "expected_skill_step_skipped" and f["skill_step"] == "proc_x@3 step 2" for f in verdict["findings"])


def test_interaction_steps_never_calls_the_assessing_loader(monkeypatch) -> None:
    """Opus review of efe127b7, finding 1: `load_path` widened through `interaction_steps`,
    which called `load_path` on every sibling -- a cycle measured at 997 SQL statements
    for two paths and no termination for three. Siblings are read with `load_steps`."""
    from core import ai_paths

    calls = {"load_path": 0, "load_steps": 0}

    def _load_path(conn, *, path_id, project_id):
        calls["load_path"] += 1
        raise AssertionError("interaction_steps must not call load_path")

    def _load_steps(conn, *, path_id, project_id):
        calls["load_steps"] += 1
        return [{"ordinal": 0, "step_kind": "tool_call", "tool_name": f"t_{path_id}", "outcome": "succeeded"}]

    monkeypatch.setattr(ai_paths, "load_path", _load_path)
    monkeypatch.setattr(ai_paths, "load_steps", _load_steps)
    monkeypatch.setattr(
        ai_paths, "paths_sharing_trace",
        lambda conn, *, project_id, trace_id, exclude=None: [{"path_id": "aip_b", "state": "finalized", "outcome": "succeeded"},
                                                              {"path_id": "aip_c", "state": "recording", "outcome": None}],
    )
    steps = ai_paths.interaction_steps(object(), project_id="p", path={"id": "aip_a", "w3c_trace_id": "t" * 32, "steps": [{"ordinal": 0}]})
    assert calls == {"load_path": 0, "load_steps": 2}
    assert [s.get("tool_name") for s in steps] == [None, "t_aip_b", "t_aip_c"]


def test_no_observed_step_under_a_skill_policy_is_unverifiable_not_a_fail() -> None:
    """Finding 3: a recorder that wrote nothing must not read as « the model skipped every step »."""
    from core import ai_paths

    verdict = ai_paths.assess(_skill_policy(), [], outcome="succeeded")
    assert verdict["verdict"] == "unverifiable"
    assert not any(f["finding"] == "required_skill_step_skipped" for f in verdict["findings"])


def test_the_order_of_required_steps_is_the_moment_observed_not_the_per_path_ordinal() -> None:
    """Finding 4: the reading spans paths and each path allocates ordinals from 0."""
    from datetime import UTC, datetime

    from core import ai_paths

    steps = [
        # step 1 crossed on the interaction path at ordinal 3, at 10:00
        {"ordinal": 3, "step_kind": "skill_step", "skill_version_id": "proc_x@3", "skill_step_id": "1", "tool_name": "tool_1",
         "outcome": "succeeded", "observed_at": datetime(2026, 9, 5, 10, 0, tzinfo=UTC)},
        # step 2 crossed on the Result's own path at ordinal 1, at 10:01
        {"ordinal": 1, "step_kind": "skill_step", "skill_version_id": "proc_x@3", "skill_step_id": "2", "tool_name": "tool_2",
         "outcome": "succeeded", "observed_at": datetime(2026, 9, 5, 10, 1, tzinfo=UTC)},
    ]
    verdict = ai_paths.assess(_skill_policy(required=(1, 2), expected=()), steps, outcome="succeeded")
    assert verdict["verdict"] == "pass", verdict
    reversed_moments = [dict(steps[0], observed_at=datetime(2026, 9, 5, 10, 2, tzinfo=UTC)), steps[1]]
    assert ai_paths.assess(_skill_policy(required=(1, 2), expected=()), reversed_moments, outcome="succeeded")["verdict"] == "fail"


def test_order_is_judged_per_skill_and_on_datetimes() -> None:
    """Round 2 residuals: a Skill citing another interleaves two sequences, each in order;
    and moments compare as datetimes, so a DST fall-back does not sort backwards."""
    from datetime import UTC, datetime, timedelta, timezone

    from core import ai_paths

    policy = {
        "recorded_by": "mcp_tool_middleware",
        "ordered_skill_steps": True,
        "expected_skill_steps": [
            {"skill_version": "A@1", "step": "1", "tool": "a1", "required": True},
            {"skill_version": "A@1", "step": "2", "tool": "a2", "required": True},
            {"skill_version": "B@1", "step": "1", "tool": "b1", "required": True},
        ],
    }
    t0 = datetime(2026, 9, 5, 10, 0, tzinfo=UTC)
    interleaved = [
        {"ordinal": 0, "step_kind": "skill_step", "skill_version_id": "A@1", "skill_step_id": "1", "outcome": "succeeded", "observed_at": t0},
        {"ordinal": 1, "step_kind": "skill_step", "skill_version_id": "B@1", "skill_step_id": "1", "outcome": "succeeded", "observed_at": t0 + timedelta(minutes=1)},
        {"ordinal": 2, "step_kind": "skill_step", "skill_version_id": "A@1", "skill_step_id": "2", "outcome": "succeeded", "observed_at": t0 + timedelta(minutes=2)},
    ]
    assert ai_paths.assess(policy, interleaved, outcome="succeeded")["verdict"] == "pass"
    # 02:59 at UTC+2 is EARLIER than 02:01 at UTC+1 (a fall-back): string order says the opposite.
    fall_back = [
        {"ordinal": 0, "step_kind": "skill_step", "skill_version_id": "A@1", "skill_step_id": "1", "outcome": "succeeded",
         "observed_at": datetime(2026, 10, 25, 2, 59, tzinfo=timezone(timedelta(hours=2)))},
        {"ordinal": 1, "step_kind": "skill_step", "skill_version_id": "A@1", "skill_step_id": "2", "outcome": "succeeded",
         "observed_at": datetime(2026, 10, 25, 2, 1, tzinfo=timezone(timedelta(hours=1)))},
    ]
    two_step = {**policy, "expected_skill_steps": policy["expected_skill_steps"][:2]}
    assert ai_paths.assess(two_step, fall_back, outcome="succeeded")["verdict"] == "pass"


def test_previous_walks_scopes_the_project_escapes_the_prefix_and_dedupes_before_the_bound(monkeypatch) -> None:
    """Round 2, finding 5: the read drives from the Project's pinned steps with an escaped
    prefix (`_` is a LIKE wildcard), and one chatty trace cannot hide the others."""
    from datetime import UTC, datetime
    from unittest.mock import MagicMock

    from core import ai_paths

    conn = MagicMock()
    t0 = datetime(2026, 9, 5, 10, 0, tzinfo=UTC)
    rows = [(f"aip_{i}", "a" * 32, t0) for i in range(30)] + [("aip_other", "b" * 32, t0), ("aip_lone", None, t0)]
    conn.cursor.return_value.__enter__.return_value.fetchall.return_value = rows
    monkeypatch.setattr(ai_paths, "load_path", lambda conn, *, path_id, project_id, assess_over_interaction=True: {"id": path_id, "w3c_trace_id": None, "steps": [], "policy_snapshot": {}, "outcome": "succeeded"})
    monkeypatch.setattr(ai_paths, "interaction_steps", lambda conn, *, project_id, path, limit=4: [])
    monkeypatch.setattr(ai_paths, "skill_coverage", lambda conn, steps: [])
    walks = ai_paths.previous_walks(conn, project_id="p", procedure_id="proc_x_1", limit=5)
    assert [w["path_id"] for w in walks] == ["aip_0", "aip_other", "aip_lone"]
    sql, params = conn.cursor.return_value.__enter__.return_value.execute.call_args[0]
    # Round 3: one walk per trace is decided IN SQL (DISTINCT ON), before the bound --
    # a trace with 101 pinned paths can no longer hide every other trace behind LIMIT 100.
    # And no LIKE: under row-level security `~~` is not leakproof and never reached the
    # index; the prefix is a range on the pattern-ops operators, `_` is no wildcard.
    assert "s.project_id = %s" in sql and "DISTINCT ON (COALESCE(p.w3c_trace_id, p.id))" in sql
    assert "~>=~ %s" in sql and "~<~ %s" in sql and "LIKE" not in sql
    assert "LIMIT %s" in sql and "LIMIT 100" not in sql
    assert params == ("p", "proc_x_1@", "proc_x_1@:", 5)


def test_skill_walk_stats_counts_each_prescribed_step_across_the_last_walks(monkeypatch) -> None:
    """Iteration 3: the Skill's page reads how often each step was crossed or skipped."""
    from core import ai_paths

    walks = [
        {"path_id": "aip_1", "started_at": "2026-09-05T10:00:00+00:00", "verdict": "pass", "skill_version": "proc_x@2", "crossed": ["1", "2"], "skipped": [],
         "steps": [{"step": "1", "label": "Run", "tool": "execute_analyze_query_spec", "state": "crossed"}, {"step": "2", "label": "Read", "tool": "analyze_result", "state": "crossed"}, {"step": "3", "label": "Look", "tool": None, "state": "unobservable"}]},
        {"path_id": "aip_2", "started_at": "2026-09-05T09:00:00+00:00", "verdict": "fail", "skill_version": "proc_x@2", "crossed": ["1"], "skipped": ["2"],
         "steps": [{"step": "1", "label": "Run", "tool": "execute_analyze_query_spec", "state": "crossed"}, {"step": "2", "label": "Read", "tool": "analyze_result", "state": "skipped"}, {"step": "3", "label": "Look", "tool": None, "state": "unobservable"}]},
        {"path_id": "aip_3", "started_at": "2026-09-05T08:00:00+00:00", "verdict": "fail", "skill_version": "proc_x@1", "crossed": ["1"], "skipped": ["2"],
         "steps": [{"step": "1", "label": "Run", "tool": "execute_analyze_query_spec", "state": "crossed"}, {"step": "2", "label": "Read", "tool": "analyze_result", "state": "skipped"}]},
    ]
    seen = {}

    def _walks(conn, *, project_id, procedure_id, limit=3):
        seen["limit"] = limit
        return walks

    monkeypatch.setattr(ai_paths, "previous_walks", _walks)
    stats = ai_paths.skill_walk_stats(object(), project_id="p", procedure_id="proc_x")
    assert seen["limit"] == 20
    assert stats["walks"] == 3 and stats["verdicts"] == {"pass": 1, "fail": 2} and stats["versions"] == {"proc_x@2": 2, "proc_x@1": 1}
    by_step = {e["step"]: e for e in stats["steps"]}
    assert by_step["1"]["crossed"] == 3 and by_step["2"]["skipped"] == 2 and by_step["3"]["unobservable"] == 2
    assert [e["step"] for e in stats["most_skipped"]] == ["2"]
    assert [w["path_id"] for w in stats["recent"]] == ["aip_1", "aip_2", "aip_3"]
    assert ai_paths.skill_walk_stats(object(), project_id="p", procedure_id="proc_none")["walks"] == 3  # the double answers the same


def test_a_crossing_of_another_skill_leaves_this_one_unverifiable() -> None:
    """Round 3 residual: the guard reads the PINNED versions, not any crossing at all."""
    from core import ai_paths

    other = [{"ordinal": 0, "step_kind": "skill_step", "skill_version_id": "proc_other@1", "skill_step_id": "1", "outcome": "succeeded"}]
    verdict = ai_paths.assess(_skill_policy(required=(1,), expected=()), other, outcome="succeeded")
    assert verdict["verdict"] == "unverifiable"
    mine = other + _crossed((1, 1))
    assert ai_paths.assess(_skill_policy(required=(1,), expected=()), mine, outcome="succeeded")["verdict"] == "pass"


def test_labels_are_made_safe_for_the_decoder_or_absent() -> None:
    """Round 3, N2: a Skill named with a trailing space or a newline must not erase the whole path."""
    from core import ai_paths

    assert ai_paths._safe_label("Kardinal crossing ") == "Kardinal crossing"
    assert ai_paths._safe_label("line\none\ttwo") == "line one two"
    assert ai_paths._safe_label("\x00\x1f") is None
    assert ai_paths._safe_label(None) is None
    long = "a" * 119 + " " + "b" * 40
    safe = ai_paths._safe_label(long)
    assert safe == "a" * 119 and len(safe) <= ai_paths.LABEL_MAX_CHARS


def test_choices_skip_long_keys_count_what_is_left_out_and_hold_a_byte_budget() -> None:
    """Round 3, N2/N3/N5: keys the decoder refuses are skipped; the `…` entry counts EVERY key left
    out; under `max_bytes` the projection stops before the wall instead of tripping it."""
    import json

    from core import ai_paths

    detail = {"k" * 81: "payload", "nested": {"a": 1}, **{f"key_{i}": f"v{i}" for i in range(15)}}
    chose = ai_paths.project_choices(detail)
    assert "k" * 81 not in chose and "nested" not in chose
    assert len([k for k in chose if k != "…"]) == ai_paths.CHOSE_MAX_KEYS
    # 5 overflowed + 1 key too long + 1 value the wire cannot carry: ALL counted (round 4, F3).
    assert chose["…"] == "7 more"
    assert ai_paths.project_choices({"k" * 81: "payload"}) == {"…": "1 more"}
    big = {f"list_{i}": ["x" * 80] * 12 for i in range(10)}
    bounded = ai_paths.project_choices(big, max_bytes=ai_paths.CHOSE_OBSERVED_MAX_BYTES)
    kept = {k: v for k, v in bounded.items() if k != "…"}
    assert 0 < len(json.dumps(kept, ensure_ascii=False).encode()) <= ai_paths.CHOSE_OBSERVED_MAX_BYTES
    assert all(1 <= len(v) < 12 for v in kept.values())  # lists shortened before being left out
    assert bounded["…"] == f"{10 - len(kept)} more"
    assert ai_paths.project_choices(big) is not None and "…" not in ai_paths.project_choices(big)


def test_the_interaction_merges_into_one_timeline_ordered_by_the_moment() -> None:
    """Round 3, N1 (deciding): steps of several paths become ONE sequence -- the merged rank is the
    ordinal the renderer orders by, continuous across paths, ids unique, and a DST fall-back keeps
    the order that happened (moments compare as datetimes, never as strings)."""
    from datetime import UTC, datetime, timedelta, timezone

    from core.ai_paths_api import merge_interaction_steps

    t0 = datetime(2026, 9, 5, 10, 0, tzinfo=UTC)

    def raw(path_id, ordinal, at, tool):
        return {"path_id": path_id, "ordinal": ordinal, "observed_at": at, "step_kind": "tool_call", "tool_name": tool, "outcome": "succeeded"}

    steps = [
        raw("B", 0, t0 + timedelta(seconds=3), "b0"),
        raw("B", 1, t0 + timedelta(seconds=4), "b1"),
        raw("A", 0, t0, "a0"),
        raw("A", 1, t0 + timedelta(seconds=1), "a1"),
        raw("A", 2, t0 + timedelta(seconds=2), "a2"),
    ]
    merged = merge_interaction_steps(steps, {})
    assert [m["tool_name"] for m in merged] == ["a0", "a1", "a2", "b0", "b1"]
    assert [m["step_order"] for m in merged] == [0, 1, 2, 3, 4]
    assert [m["path_step_order"] for m in merged] == [0, 1, 2, 0, 1]
    assert len({m["id"] for m in merged}) == 5 and merged[3]["id"] == "B#0"
    fall_back = [
        raw("A", 1, datetime(2026, 10, 25, 2, 1, tzinfo=timezone(timedelta(hours=1))), "later"),
        raw("A", 0, datetime(2026, 10, 25, 2, 59, tzinfo=timezone(timedelta(hours=2))), "earlier"),
    ]
    assert [m["tool_name"] for m in merge_interaction_steps(fall_back, {})] == ["earlier", "later"]


def test_the_recorders_silence_does_not_silence_a_forbidden_touch() -> None:
    """Round 4, F1: a walk that crossed another Skill AND touched a forbidden object is a
    deviation (fail), not missing evidence -- the guard reports and falls through."""
    from core import ai_paths

    snapshot = {
        "recorded_by": "mcp_tool_middleware",
        "expected_skill_steps": [{"skill_version": "proc_v1@1", "step": "1", "required": True, "tool": "tool_a"}],
        "forbidden": [{"owner_workspace": "data", "owner_object_type": "dataset", "owner_object_id": "ds_SECRET"}],
    }
    steps = [
        {"ordinal": 0, "step_kind": "skill_step", "skill_version_id": "proc_v2@1", "skill_step_id": "1", "outcome": "succeeded"},
        {"ordinal": 1, "step_kind": "tool_call", "outcome": "succeeded", "owner_workspace": "data", "owner_object_type": "dataset", "owner_object_id": "ds_SECRET"},
    ]
    verdict = ai_paths.assess(snapshot, steps, outcome="succeeded")
    kinds = [f["finding"] for f in verdict["findings"]]
    assert verdict["verdict"] == "fail"
    assert "observed_but_forbidden" in kinds and ai_paths.FINDING_UNVERIFIABLE in kinds
    # Without the forbidden touch the same silence is unverifiable, said once.
    silent = ai_paths.assess({k: v for k, v in snapshot.items() if k != "forbidden"}, steps[:1], outcome="succeeded")
    assert silent["verdict"] == "unverifiable"
    assert [f["finding"] for f in silent["findings"]].count(ai_paths.FINDING_UNVERIFIABLE) == 1


def test_a_procedure_id_with_the_version_separator_reads_no_walk() -> None:
    """Round 4, F6: `proc_x@1` as a procedure id would be conflated with a version of `proc_x`."""
    from unittest.mock import MagicMock

    from core import ai_paths

    conn = MagicMock()
    assert ai_paths.previous_walks(conn, project_id="p", procedure_id="proc_x@1", limit=5) == []
    assert ai_paths.previous_walks(conn, project_id="p", procedure_id="", limit=5) == []
    conn.cursor.assert_not_called()


def test_the_detail_read_judges_the_interaction_only_for_a_skills_walk() -> None:
    """Round 5, B2: a plain node policy is judged on the path's own steps -- a sibling's
    forbidden touch does not fail this path; a Skill policy is judged over the interaction."""
    from core.ai_paths_api import assessment_for_read

    own = [{"ordinal": 0, "step_kind": "tool_call", "outcome": "succeeded", "owner_workspace": "data", "owner_object_type": "dataset", "owner_object_id": "ds_GOOD", "path_id": "A"}]
    sibling = [{"ordinal": 0, "step_kind": "tool_call", "outcome": "succeeded", "owner_workspace": "data", "owner_object_type": "dataset", "owner_object_id": "ds_SECRET", "path_id": "B"}]
    plain = {
        "id": "A", "w3c_trace_id": "t" * 32, "outcome": "succeeded",
        "policy_snapshot": {"required": [{"owner_workspace": "data", "owner_object_type": "dataset", "owner_object_id": "ds_GOOD"}],
                            "forbidden": [{"owner_workspace": "data", "owner_object_type": "dataset", "owner_object_id": "ds_SECRET"}]},
        "assessment": {"verdict": "pass", "findings": []},
    }
    assert assessment_for_read(plain, own + sibling) == {"verdict": "pass", "findings": []}
    skill = {**plain, "policy_snapshot": {"expected_skill_steps": [{"skill_version": "proc_x@1", "step": "1", "required": True, "tool": "t"}]}}
    crossed_on_sibling = [{"ordinal": 0, "step_kind": "skill_step", "skill_version_id": "proc_x@1", "skill_step_id": "1", "outcome": "succeeded", "path_id": "B"}]
    assert assessment_for_read(skill, own + crossed_on_sibling)["verdict"] == "pass"
    assert assessment_for_read({**skill, "w3c_trace_id": None}, own + crossed_on_sibling) == {"verdict": "pass", "findings": []}


def test_the_interaction_is_bounded_once_in_the_order_of_the_moment() -> None:
    """Round 5/6: every row reaches the reading in the order of the moment; only the drawing is bounded."""
    from datetime import UTC, datetime, timedelta

    from core.ai_paths_api import INTERACTION_MAX_STEPS, ordered_interaction_steps

    t0 = datetime(2026, 9, 5, 10, 0, tzinfo=UTC)
    rows = [{"path_id": "B", "ordinal": i, "observed_at": t0 + timedelta(seconds=2 * i + 1)} for i in range(150)]
    rows += [{"path_id": "A", "ordinal": i, "observed_at": t0 + timedelta(seconds=2 * i)} for i in range(150)]
    ordered = ordered_interaction_steps(rows)
    # Every row is judged (a clamp dropped the closing crossing, round 6); the order is the moment.
    assert len(ordered) == 300 and INTERACTION_MAX_STEPS == 200
    assert [r["path_id"] for r in ordered[:4]] == ["A", "B", "A", "B"]
    assert ordered[-1]["observed_at"] == t0 + timedelta(seconds=299)
    # Only the drawing is bounded, and it is contiguous after the bound.
    from core.ai_paths_api import merge_interaction_steps

    drawn = merge_interaction_steps([{**r, "step_kind": "tool_call", "outcome": "succeeded"} for r in rows], {})
    assert len(drawn) == INTERACTION_MAX_STEPS and [d["step_order"] for d in drawn] == list(range(INTERACTION_MAX_STEPS))
    # The drawing keeps the END of the walk (round 7, B2): the last row drawn is the last row that happened.
    assert drawn[-1]["observed_at"] == (t0 + timedelta(seconds=299)).isoformat()
    assert drawn[0]["observed_at"] == (t0 + timedelta(seconds=100)).isoformat()


def test_under_the_recorders_silence_a_missing_required_node_is_not_a_failure() -> None:
    """Round 5, finding 1: absence is not evidence when the record is declared untrustworthy;
    a forbidden touch (positive evidence) still fails."""
    from core import ai_paths

    snapshot = {
        "recorded_by": "mcp_tool_middleware",
        "expected_skill_steps": [{"skill_version": "proc_A@1", "step": "1", "required": True, "tool": "t"}],
        "required": [{"owner_workspace": "data", "owner_object_type": "dataset", "owner_object_id": "ds_R"}],
    }
    other = [{"ordinal": 0, "step_kind": "skill_step", "skill_version_id": "proc_B@3", "skill_step_id": "1", "outcome": "succeeded",
              "owner_workspace": "data", "owner_object_type": "dataset", "owner_object_id": "ds_OTHER"}]
    verdict = ai_paths.assess(snapshot, other, outcome="succeeded")
    assert verdict["verdict"] == "unverifiable"
    assert "required_but_missing" not in [f["finding"] for f in verdict["findings"]]
    # The same silence with the required node PRESENT stays unverifiable, never pass.
    present = other + [{"ordinal": 1, "step_kind": "tool_call", "outcome": "succeeded", "owner_workspace": "data", "owner_object_type": "dataset", "owner_object_id": "ds_R"}]
    assert ai_paths.assess(snapshot, present, outcome="succeeded")["verdict"] == "unverifiable"


def test_node_order_is_judged_on_the_moment_across_the_paths_of_an_interaction() -> None:
    """Round 6, B5: ds-ONE reached first in time on a sibling (ordinal 7), ds-TWO second on the own
    path (ordinal 1) -- the prescribed order was respected; per-path ordinals said the opposite."""
    from datetime import UTC, datetime, timedelta

    from core import ai_paths

    t0 = datetime(2026, 9, 5, 10, 0, tzinfo=UTC)
    snapshot = {
        "recorded_by": "mcp_tool_middleware",
        "ordered": True,
        "required": [
            {"owner_workspace": "data", "owner_object_type": "dataset", "owner_object_id": "ds_ONE"},
            {"owner_workspace": "data", "owner_object_type": "dataset", "owner_object_id": "ds_TWO"},
        ],
    }
    steps = [
        {"ordinal": 1, "step_kind": "tool_call", "outcome": "succeeded", "observed_at": t0 + timedelta(seconds=10),
         "owner_workspace": "data", "owner_object_type": "dataset", "owner_object_id": "ds_TWO"},
        {"ordinal": 7, "step_kind": "tool_call", "outcome": "succeeded", "observed_at": t0 + timedelta(seconds=1),
         "owner_workspace": "data", "owner_object_type": "dataset", "owner_object_id": "ds_ONE"},
    ]
    verdict = ai_paths.assess(snapshot, steps, outcome="succeeded")
    assert verdict["verdict"] == "pass", verdict
    reversed_in_time = [dict(steps[0], observed_at=t0), dict(steps[1], observed_at=t0 + timedelta(seconds=5))]
    out = ai_paths.assess(snapshot, reversed_in_time, outcome="succeeded")
    assert out["verdict"] == "fail"
    assert {"finding": "out_of_order", "observed_ordinals": [7, 1]} in out["findings"]


def test_the_merged_rows_are_routed_like_own_steps() -> None:
    """Round 6, F1: the server composes the owner reference of every merged row (B3 guarded)."""
    from datetime import UTC, datetime

    from core.ai_paths_api import routed_interaction_rows

    rows = routed_interaction_rows(
        [
            {"path_id": "A", "ordinal": 0, "observed_at": datetime(2026, 9, 5, 10, 0, tzinfo=UTC), "step_kind": "tool_call", "outcome": "succeeded",
             "owner_workspace": "analyze", "owner_object_type": "query-spec", "owner_object_id": "qs_1", "owner_version_id": "qsv_1"},
            {"path_id": "B", "ordinal": 0, "observed_at": datetime(2026, 9, 5, 10, 0, 1, tzinfo=UTC), "step_kind": "tool_call", "outcome": "succeeded"},
        ],
        {},
        {},
    )
    assert rows[0]["owner_reference"] and rows[0]["owner_reference"].get("object_id") == "qs_1"
    assert rows[0]["owner_reference_state"] == "governed"
    assert rows[1]["owner_reference"] is None and rows[1]["id"] == "B#0"


def test_the_assessment_owns_its_order_so_two_callers_read_one_verdict() -> None:
    """Round 7, B1: step 1 crossed on the sibling first (t=1) and retried on the own path (t=30),
    step 2 crossed on the own path (t=10) -- the walk respected the order. Handed own-then-siblings
    or by the moment, the verdict is the same: pass."""
    from datetime import UTC, datetime, timedelta

    from core import ai_paths

    t0 = datetime(2026, 9, 5, 10, 0, tzinfo=UTC)
    policy = {
        "recorded_by": "mcp_tool_middleware",
        "ordered_skill_steps": True,
        "expected_skill_steps": [
            {"skill_version": "proc_x@1", "step": "1", "tool": "a", "required": True},
            {"skill_version": "proc_x@1", "step": "2", "tool": "b", "required": True},
        ],
    }
    own = [
        {"path_id": "A", "ordinal": 0, "step_kind": "skill_step", "skill_version_id": "proc_x@1", "skill_step_id": "2", "outcome": "succeeded", "observed_at": t0 + timedelta(seconds=10)},
        {"path_id": "A", "ordinal": 1, "step_kind": "skill_step", "skill_version_id": "proc_x@1", "skill_step_id": "1", "outcome": "succeeded", "observed_at": t0 + timedelta(seconds=30)},
    ]
    sibling = [
        {"path_id": "B", "ordinal": 0, "step_kind": "skill_step", "skill_version_id": "proc_x@1", "skill_step_id": "1", "outcome": "succeeded", "observed_at": t0 + timedelta(seconds=1)},
    ]
    by_caller = ai_paths.assess(policy, own + sibling, outcome="succeeded")
    by_moment = ai_paths.assess(policy, ai_paths.ordered_by_moment(own + sibling), outcome="succeeded")
    assert by_caller["verdict"] == by_moment["verdict"] == "pass", (by_caller, by_moment)
    # And a node policy: the first row of a node decides its version -- in the order it happened.
    node_policy = {"recorded_by": "mcp_tool_middleware", "required": [{"owner_workspace": "data", "owner_object_type": "dataset", "owner_object_id": "ds_R", "owner_version_id": "v1"}]}
    rows = [
        {"path_id": "A", "ordinal": 0, "step_kind": "tool_call", "outcome": "succeeded", "observed_at": t0 + timedelta(seconds=9), "owner_workspace": "data", "owner_object_type": "dataset", "owner_object_id": "ds_R", "owner_version_id": "v2"},
        {"path_id": "B", "ordinal": 0, "step_kind": "tool_call", "outcome": "succeeded", "observed_at": t0, "owner_workspace": "data", "owner_object_type": "dataset", "owner_object_id": "ds_R", "owner_version_id": "v1"},
    ]
    assert ai_paths.assess(node_policy, rows, outcome="succeeded")["verdict"] == ai_paths.assess(node_policy, list(reversed(rows)), outcome="succeeded")["verdict"] == "pass"


def test_interaction_steps_hands_rows_in_the_order_they_happened_and_tagged_with_their_path(monkeypatch) -> None:
    """Round 7, B1: the loader's interaction is the same sequence the detail read orders."""
    from datetime import UTC, datetime, timedelta

    from core import ai_paths

    t0 = datetime(2026, 9, 5, 10, 0, tzinfo=UTC)
    monkeypatch.setattr(ai_paths, "paths_sharing_trace", lambda conn, *, project_id, trace_id, exclude: [{"path_id": "B"}])
    monkeypatch.setattr(ai_paths, "load_steps", lambda conn, *, path_id, project_id: [{"ordinal": 0, "observed_at": t0 + timedelta(seconds=1), "tool_name": "b0"}])
    path = {"id": "A", "w3c_trace_id": "t" * 32, "steps": [{"ordinal": 0, "observed_at": t0, "tool_name": "a0"}, {"ordinal": 1, "observed_at": t0 + timedelta(seconds=2), "tool_name": "a1"}]}
    steps = ai_paths.interaction_steps(object(), project_id="p", path=path)
    assert [(s["tool_name"], s["path_id"]) for s in steps] == [("a0", "A"), ("b0", "B"), ("a1", "A")]


def test_previous_walks_judges_a_plain_policy_on_the_paths_own_steps(monkeypatch) -> None:
    """Round 10, N1: a walk whose snapshot pins no Skill keeps the loader's verdict -- a sibling's
    forbidden touch does not fail it here when it fails it nowhere else."""
    from datetime import UTC, datetime
    from unittest.mock import MagicMock

    from core import ai_paths

    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchall.return_value = [("aip_1", "a" * 32, datetime(2026, 9, 5, 10, 0, tzinfo=UTC))]
    own = {"id": "aip_1", "w3c_trace_id": "a" * 32, "outcome": "succeeded", "steps": [],
           "policy_snapshot": {"forbidden": [{"owner_workspace": "data", "owner_object_type": "dataset", "owner_object_id": "ds_SECRET"}]},
           "assessment": {"verdict": "pass", "findings": []}}
    monkeypatch.setattr(ai_paths, "load_path", lambda conn, *, path_id, project_id, assess_over_interaction=True: dict(own))
    monkeypatch.setattr(ai_paths, "interaction_steps", lambda conn, *, project_id, path, limit=4, siblings=None: [
        {"ordinal": 0, "step_kind": "tool_call", "outcome": "succeeded", "path_id": "aip_sib", "owner_workspace": "data", "owner_object_type": "dataset", "owner_object_id": "ds_SECRET"}])
    monkeypatch.setattr(ai_paths, "skill_coverage", lambda conn, steps: [])
    (walk,) = ai_paths.previous_walks(conn, project_id="p", procedure_id="proc_x", limit=5)
    assert walk["verdict"] == "pass"
    # With a Skill pinned, the same walk IS judged over the interaction.
    own["policy_snapshot"] = {"expected_skill_steps": [{"skill_version": "proc_x@1", "step": "1", "required": True, "tool": "t"}]}
    (walk,) = ai_paths.previous_walks(conn, project_id="p", procedure_id="proc_x", limit=5)
    assert walk["verdict"] == "unverifiable"


def test_every_owner_object_type_the_recorder_can_write_is_a_word_the_column_holds() -> None:
    """AI-376, the class (Opus F1): the writer refuses what the column refuses, and every
    constant object type the recorder or the emitters can hand it passes."""
    from unittest.mock import MagicMock

    import pytest

    from core import ai_path_recorder, ai_paths, candidate_emission

    written = {entry[2] for entry in ai_path_recorder._OWNER_ARGUMENTS}
    written |= {"procedure", "semantic-view-version", ai_paths.OVERLAY_EVENT_OBJECT_TYPE}
    written |= {candidate_emission.owner_object_type_for(k) for k in ("topic", "procedure", "schema_doc", "context_event")}
    for object_type in sorted(written):
        assert ai_paths.OWNER_OBJECT_TYPE_PATTERN.match(object_type), object_type
    with pytest.raises(ai_paths.AiPathError, match="owner_object_type"):
        ai_paths.append_step(MagicMock(), path_id="aip_1", project_id="p", step_kind="tool_call", outcome="succeeded",
                             owner_workspace="context-hub", owner_object_type="context_event", owner_object_id="cev_1")


def test_an_expected_node_written_in_the_detail_vocabulary_matches_the_stored_word() -> None:
    """AI-376, F2: an author writing `schema_doc` addresses the stored `schema-doc`."""
    from core import ai_paths
    from core.expected_ai_path import node_key

    policy = {"recorded_by": "mcp_tool_middleware", "required": [{"owner_workspace": "context-hub", "owner_object_type": "schema_doc", "owner_object_id": "doc_1"}]}
    observed = [{"ordinal": 0, "step_kind": "knowledge_read", "outcome": "succeeded", "owner_workspace": "context-hub", "owner_object_type": "schema-doc", "owner_object_id": "doc_1"}]
    assert ai_paths.assess(policy, observed, outcome="succeeded")["verdict"] == "pass"
    assert node_key({"owner_workspace": "context-hub", "owner_object_type": "context_event", "owner_object_id": "cev_1"}) == "context-hub/context-event/cev_1"
