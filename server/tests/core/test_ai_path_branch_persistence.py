"""A judged branch outlives the person watching (migration 176).

Before this, `emit_candidates` returned early unless a `progressToken` was
present, so an unwatched walk assembled nothing and stored nothing, and a later
reader of the same path could only say `branch count unknown`. These tests pin
the two halves of the repair:

  * an UNWATCHED crossing is still recorded -- the buffer does not depend on a
    live channel;
  * the recorded crossings land as their own steps, in order, BEFORE the
    `tool_call` step that describes the call, each carrying its `detail`.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from core import ai_path_recorder as mod
from core import ai_paths
from core.ai_path_recorder import (
    emission_is_armed,
    emit_step_sync,
    observation_is_recorded,
)

BRANCHES = {
    "candidate_ids": ["ctx_alpha", "ctx_beta"],
    "candidate_fates": ["selected", "rejected"],
    "candidate_scores": [0.91, 0.14],
    "retrieval_mode": "lexical",
    "selected_count": 1,
    "rejected_count": 1,
}


class _Cur:
    def execute(self, *_a, **_k):
        return None

    def fetchone(self):
        return ("org_EXAMPLE",)

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


class _Conn:
    def cursor(self):
        return _Cur()

    def commit(self):
        return None

    def transaction(self):
        """Le point de reprise que prend l'ecriture d'un croisement.

        LE ROUGE QUE CE FAUX PRODUISAIT ETAIT LE SIEN. La garde SAVEPOINT existe
        dans `record_tool_call` depuis le 2026-08-04 (bd1ee493) ; ce faux ne l'a
        jamais fournie, donc l'ajout d'un croisement levait un `AttributeError`
        que le recorder avale -- et le test lisait << aucun croisement >> la ou
        le code en ecrit un. Le test etait rouge et decrivait un defaut qui
        n'existait pas.

        Meme piege, meme reparation, dans `test_context_search.py` (record_fates)
        et `test_ai_path_recorder.py` (le pas de Skill).
        """
        import contextlib

        return contextlib.nullcontext()

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


@pytest.fixture
def recorded(monkeypatch):
    """Capture what `record_tool_call` appends, without a database."""
    steps: list[dict] = []

    monkeypatch.setattr(mod, "_resolve_scope", lambda _args: ("proj_EXAMPLE", "org_EXAMPLE"))
    monkeypatch.setattr(mod, "_open_path_for", lambda *_a, **_k: "aip_EXAMPLE")
    monkeypatch.setattr("core.db.get_connection", lambda *_a, **_k: _Conn())
    monkeypatch.setattr(ai_paths, "begin_path", lambda *_a, **_k: {"id": "aip_EXAMPLE"})

    def _append(_conn, **kwargs):
        steps.append(kwargs)
        return {"id": f"aps_{len(steps)}", "ordinal": len(steps) - 1}

    monkeypatch.setattr(ai_paths, "append_step", _append)
    return steps


def _emitter(*, progress_token):
    context = SimpleNamespace(
        request_context=SimpleNamespace(meta=SimpleNamespace(progressToken=progress_token))
    )
    return mod._PathEmitter(context, None)


def test_an_unwatched_crossing_is_still_recorded(monkeypatch):
    emitter = _emitter(progress_token=None)
    token = mod._ACTIVE_EMITTER.set(emitter)
    try:
        assert emission_is_armed() is False  # nobody is watching...
        assert observation_is_recorded() is True  # ...and it is kept anyway
        streamed = emit_step_sync(
            step_kind="knowledge_read",
            outcome="succeeded",
            tool_name="search_context",
            owner_object_type="topic",
            detail=BRANCHES,
        )
    finally:
        mod._ACTIVE_EMITTER.reset(token)

    assert streamed is False, "an unwatched crossing must not claim to have gone out"
    crossings = emitter.drain_crossings()
    assert len(crossings) == 1
    assert crossings[0]["detail"] == BRANCHES


def test_draining_is_once_so_a_retry_cannot_double_the_path():
    emitter = _emitter(progress_token=None)
    token = mod._ACTIVE_EMITTER.set(emitter)
    try:
        emit_step_sync(step_kind="knowledge_read", outcome="succeeded", detail=BRANCHES)
    finally:
        mod._ACTIVE_EMITTER.reset(token)

    assert len(emitter.drain_crossings()) == 1
    assert emitter.drain_crossings() == []


def test_a_runaway_walk_cannot_grow_the_path_without_bound():
    emitter = _emitter(progress_token=None)
    for _ in range(mod.CROSSINGS_PER_CALL_MAX + 25):
        emitter.record_crossing(step_kind="knowledge_read", outcome="succeeded")
    assert len(emitter.drain_crossings()) == mod.CROSSINGS_PER_CALL_MAX


def test_crossings_are_appended_before_the_tool_call_step(recorded):
    emitter = _emitter(progress_token=None)
    token = mod._ACTIVE_EMITTER.set(emitter)
    try:
        emit_step_sync(
            step_kind="knowledge_read",
            outcome="succeeded",
            tool_name="search_context",
            owner_object_type="topic",
            detail=BRANCHES,
        )
        path_id = mod.record_tool_call(
            tool_name="search_context",
            arguments={"project_id": "proj_EXAMPLE"},
            meta=None,
            outcome="succeeded",
            actor="owner@example.com",
        )
    finally:
        mod._ACTIVE_EMITTER.reset(token)

    assert path_id == "aip_EXAMPLE"
    assert [step["step_kind"] for step in recorded] == ["knowledge_read", "tool_call"]
    assert recorded[0]["detail"] == BRANCHES
    # The call itself judged nothing; only the crossing did.
    assert recorded[1].get("detail") is None


def test_a_crossing_that_cannot_be_appended_does_not_lose_the_call(recorded, monkeypatch):
    """One bad crossing is not the walk: the tool_call step must still land."""
    emitter = _emitter(progress_token=None)
    original = ai_paths.append_step

    def _explode_on_crossings(conn, **kwargs):
        if kwargs.get("step_kind") == "knowledge_read":
            raise ValueError("this crossing is malformed")
        return original(conn, **kwargs)

    monkeypatch.setattr(ai_paths, "append_step", _explode_on_crossings)

    token = mod._ACTIVE_EMITTER.set(emitter)
    try:
        emit_step_sync(step_kind="knowledge_read", outcome="succeeded", detail=BRANCHES)
        path_id = mod.record_tool_call(
            tool_name="search_context",
            arguments={"project_id": "proj_EXAMPLE"},
            meta=None,
            outcome="succeeded",
            actor="owner@example.com",
        )
    finally:
        mod._ACTIVE_EMITTER.reset(token)

    assert path_id == "aip_EXAMPLE"
    assert [step["step_kind"] for step in recorded] == ["tool_call"]


def test_an_empty_detail_is_stored_as_null_not_as_a_zero():
    """`{}` would decode to `no_branch_judged`; absence must stay absence."""
    assert ai_paths._detail_json(None, mod.sanitize_detail) is None
    assert ai_paths._detail_json({}, mod.sanitize_detail) is None
    # A map whose every key is refused is an absence too, not an empty listing.
    assert ai_paths._detail_json({"rationale": "x"}, mod.sanitize_detail) is None
    assert ai_paths._detail_json(BRANCHES, mod.sanitize_detail) is not None
