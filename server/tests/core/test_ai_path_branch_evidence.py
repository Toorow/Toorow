"""Story 65.7 Task 1 -- the persisted producer detail is closed or unavailable.

These tests stop at the producer boundary.  They do not project a client shape,
open an AI Path, or exercise a Result: a branch can become evidence only after
the flat value written to ``app.ai_path_steps.detail`` has one unambiguous
meaning.  A valid producer returns the complete marked detail.  Any invalid
candidate or descriptor returns the same three-key unavailable sentinel, with
none of the refused input copied into it.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from core import ai_path_recorder, ai_paths, candidate_emission, candidate_fate

PUBLIC_SCHEMA = "retrieval-branch-evidence.v1"

DETAIL_SCHEMA = "retrieval-branch-detail.v1"
PRODUCER_CONTEXT_SEARCH = "context_search"
PRODUCER_BRIEFING = "briefing_context_event"

_ARRAY_KEYS = (
    "candidate_ids",
    "candidate_kinds",
    "candidate_titles",
    "candidate_scores",
    "candidate_tiers",
    "candidate_matched",
    "candidate_ranks",
    "candidate_fates",
    "candidate_reasons",
)


def _context_descriptor(**changes):
    descriptor = {
        "mode": "lexical",
        "graph_hop_depth": 1,
        "semantic_recall": False,
        "limit": 1,
        "not_reached_enumerated": False,
        "reached_count": 2,
        "selected_count": 1,
        "rejected_count": 1,
        "tiers": {"title": 3.0, "description": 2.0, "neighbor": 1.0},
    }
    descriptor.update(changes)
    return descriptor


def _context_candidates():
    return [
        {
            "id": "ctx_Alpha-1:rev.2",
            "kind": candidate_emission.OBJECT_TYPE_TOPIC,
            "title": "  Cafe\u0301   Q2  ",
            "score": 3.0,
            "tier": "title",
            "matched": True,
            "rank": 1,
            "fate": candidate_fate.FATE_SELECTED,
            "reason": None,
        },
        {
            "id": "ctx_beta",
            "kind": candidate_emission.OBJECT_TYPE_SCHEMA_DOC,
            "title": "Beta",
            "score": 1.0,
            "tier": "neighbor",
            "matched": False,
            "rank": 2,
            "fate": candidate_fate.FATE_REJECTED,
            "reason": candidate_fate.REASON_BELOW_CUTOFF,
        },
    ]


def _briefing_descriptor(**changes):
    descriptor = {
        "mode": "exact_date_connector",
        "graph_hop_depth": 0,
        "semantic_recall": False,
        "limit": 1,
        "not_reached_enumerated": False,
        "reached_count": 1,
        "selected_count": 0,
        "rejected_count": 1,
    }
    descriptor.update(changes)
    return descriptor


def _briefing_candidates():
    return [
        {
            "id": "evt_2026-08-10",
            "kind": candidate_emission.OBJECT_TYPE_CONTEXT_EVENT,
            "title": "Launch",
            "score": None,
            "tier": None,
            "matched": None,
            "rank": 1,
            "fate": candidate_fate.FATE_REJECTED,
            "reason": candidate_fate.REASON_DATE_MISMATCH,
        }
    ]


def _build(candidates, descriptor, *, producer=PRODUCER_CONTEXT_SEARCH):
    return candidate_emission.candidate_detail(
        candidates,
        descriptor,
        producer=producer,
    )


def _unavailable(producer=PRODUCER_CONTEXT_SEARCH):
    return {
        "branch_schema_version": DETAIL_SCHEMA,
        "branch_producer": producer,
        "branch_state": "unavailable",
    }


def _retrieval_step(
    detail,
    *,
    ordinal=0,
    tool_name="search_context",
    step_kind="knowledge_read",
):
    return {
        "ordinal": ordinal,
        "step_kind": step_kind,
        "outcome": "succeeded",
        "observed_at": datetime(2026, 8, 10, 8, 0, tzinfo=timezone.utc),
        "tool_name": tool_name,
        "owner_workspace": "context-hub",
        "owner_object_type": "topic",
        "owner_object_id": "ctx_1",
        "owner_version_id": None,
        "skill_version_id": None,
        "skill_step_id": None,
        "evidence_record_id": None,
        "detail": detail,
    }


def _public_evidence(detail, *, tool_name="search_context"):
    return ai_paths.project_branch_evidence_for_steps(
        [_retrieval_step(detail, tool_name=tool_name)]
    )[0]


def test_context_search_detail_is_marked_normalized_and_aligned() -> None:
    detail = _build(_context_candidates(), _context_descriptor())

    assert detail["branch_schema_version"] == DETAIL_SCHEMA
    assert detail["branch_producer"] == PRODUCER_CONTEXT_SEARCH
    assert "branch_state" not in detail
    assert detail["retrieval_mode"] == "lexical"
    assert detail["candidate_titles"] == ["Caf\u00e9 Q2", "Beta"]
    assert detail["candidate_ranks"] == [1, 2]
    assert detail["candidate_fates"] == [
        candidate_fate.FATE_SELECTED,
        candidate_fate.FATE_REJECTED,
    ]
    assert detail["candidate_reasons"] == [
        None,
        candidate_fate.REASON_BELOW_CUTOFF,
    ]
    assert {len(detail[key]) for key in _ARRAY_KEYS} == {2}
    assert detail["candidates_listed"] == 2
    assert detail["reached_count"] == 2
    assert detail["selected_count"] + detail["rejected_count"] == 2


def test_briefing_uses_the_same_contract_with_its_own_closed_values() -> None:
    detail = _build(
        _briefing_candidates(),
        _briefing_descriptor(),
        producer=PRODUCER_BRIEFING,
    )

    assert detail["branch_schema_version"] == DETAIL_SCHEMA
    assert detail["branch_producer"] == PRODUCER_BRIEFING
    assert detail["candidate_kinds"] == [candidate_emission.OBJECT_TYPE_CONTEXT_EVENT]
    assert detail["candidate_scores"] == [None]
    assert detail["candidate_tiers"] == [None]
    assert detail["candidate_matched"] == [None]
    assert detail["candidate_reasons"] == [candidate_fate.REASON_DATE_MISMATCH]


def test_invalid_input_returns_only_the_exact_recordable_sentinel() -> None:
    candidates = _context_candidates()
    candidates[0]["title"] = "secret refused title\nwith control"

    detail = _build(candidates, _context_descriptor())

    assert detail == _unavailable()
    assert ai_path_recorder.sanitize_detail(detail) == detail
    assert "secret" not in repr(detail)


@pytest.mark.parametrize(
    "descriptor",
    [
        _context_descriptor(reached_count=3),
        _context_descriptor(selected_count=0),
        _context_descriptor(rejected_count=0),
        _context_descriptor(not_reached_enumerated=True),
        {
            key: value
            for key, value in _context_descriptor().items()
            if key != "not_reached_enumerated"
        },
    ],
)
def test_counts_and_non_enumeration_must_be_exact(descriptor) -> None:
    assert _build(_context_candidates(), descriptor) == _unavailable()


@pytest.mark.parametrize(
    ("ranks", "scores"),
    [
        ([1, 1], [3.0, 1.0]),
        ([2, 1], [3.0, 1.0]),
        ([0, 2], [3.0, 1.0]),
        ([1.5, 2], [3.0, 1.0]),
        ([True, 2], [3.0, 1.0]),
        ([1, 2], [math.nan, 1.0]),
        ([1, 2], [math.inf, 1.0]),
        ([1, 2], [-math.inf, 1.0]),
    ],
)
def test_ranks_and_scores_are_strict_and_finite(ranks, scores) -> None:
    candidates = _context_candidates()
    for candidate, rank, score in zip(candidates, ranks, scores, strict=True):
        candidate["rank"] = rank
        candidate["score"] = score
    assert _build(candidates, _context_descriptor()) == _unavailable()


@pytest.mark.parametrize(
    "candidate_id",
    ["", "bad/id", "bad id", "\u00e9vt_1", "x" * 201, "ctx_ok\u202e"],
)
def test_candidate_ids_use_the_closed_ascii_identifier_rule(candidate_id) -> None:
    candidates = _context_candidates()
    candidates[0]["id"] = candidate_id
    assert _build(candidates, _context_descriptor()) == _unavailable()


@pytest.mark.parametrize(
    "title",
    ["bad\nline", "bad\u0085control", "spoof\u202etitle", "x" * 201],
)
def test_titles_refuse_unicode_controls_and_overlength(title) -> None:
    candidates = _context_candidates()
    candidates[0]["title"] = title
    assert _build(candidates, _context_descriptor()) == _unavailable()


@pytest.mark.parametrize(
    ("kind", "producer"),
    [
        (candidate_emission.OBJECT_TYPE_CONTEXT_EVENT, PRODUCER_CONTEXT_SEARCH),
        (candidate_emission.OBJECT_TYPE_TOPIC, PRODUCER_BRIEFING),
        ("invented_kind", PRODUCER_CONTEXT_SEARCH),
    ],
)
def test_candidate_kind_is_closed_per_producer(kind, producer) -> None:
    if producer == PRODUCER_BRIEFING:
        candidates = _briefing_candidates()
        descriptor = _briefing_descriptor()
    else:
        candidates = _context_candidates()
        descriptor = _context_descriptor()
    candidates[0]["kind"] = kind
    assert _build(candidates, descriptor, producer=producer) == _unavailable(producer)


def test_v1_forbids_a_not_reached_candidate() -> None:
    candidates = _context_candidates()
    candidates[1]["fate"] = candidate_fate.FATE_NOT_REACHED
    candidates[1]["reason"] = None
    assert _build(candidates, _context_descriptor()) == _unavailable()


@pytest.mark.parametrize(
    ("fate", "reason"),
    [
        (candidate_fate.FATE_REJECTED, None),
        (candidate_fate.FATE_SELECTED, candidate_fate.REASON_BELOW_CUTOFF),
        (candidate_fate.FATE_REJECTED, candidate_fate.REASON_DATE_MISMATCH),
    ],
)
def test_context_search_fate_reason_pairs_use_its_allowlist(fate, reason) -> None:
    candidates = _context_candidates()
    candidates[1]["fate"] = fate
    candidates[1]["reason"] = reason
    assert _build(candidates, _context_descriptor()) == _unavailable()


def test_briefing_rejects_a_context_search_only_reason() -> None:
    candidates = _briefing_candidates()
    candidates[0]["reason"] = candidate_fate.REASON_ANTI_TRIGGER
    assert _build(
        candidates,
        _briefing_descriptor(),
        producer=PRODUCER_BRIEFING,
    ) == _unavailable(PRODUCER_BRIEFING)


def test_every_judged_candidate_is_validated_before_listing_is_truncated() -> None:
    candidates = []
    for rank in range(1, 26):
        candidates.append(
            {
                "id": f"ctx_{rank}",
                "kind": candidate_emission.OBJECT_TYPE_TOPIC,
                "title": f"Candidate {rank}",
                "score": float(26 - rank),
                "tier": "title",
                "matched": True,
                "rank": rank,
                "fate": (
                    candidate_fate.FATE_SELECTED
                    if rank == 1
                    else candidate_fate.FATE_REJECTED
                ),
                "reason": (
                    None if rank == 1 else candidate_fate.REASON_BELOW_CUTOFF
                ),
            }
        )
    descriptor = _context_descriptor(
        reached_count=25,
        selected_count=1,
        rejected_count=24,
    )

    valid = _build(candidates, descriptor)
    assert valid["candidates_listed"] == candidate_emission.CANDIDATE_LIST_LIMIT == 24
    assert {len(valid[key]) for key in _ARRAY_KEYS} == {24}
    assert valid["reached_count"] == 25

    invalid_tail = deepcopy(candidates)
    invalid_tail[-1]["rank"] = 24
    assert _build(invalid_tail, descriptor) == _unavailable()


def test_unknown_producer_cannot_mint_an_authoritative_sentinel() -> None:
    with pytest.raises(ValueError, match="producer"):
        _build(
            _context_candidates(),
            _context_descriptor(),
            producer="unregistered_producer",
        )


# ---------------------------------------------------------------------------
# Task 2 -- v2 identity is explicit and the real search door is fresh/final.
# ---------------------------------------------------------------------------


def _hash_steps():
    return [
        {
            "ordinal": 0,
            "step_kind": "knowledge_read",
            "owner_workspace": "context-hub",
            "owner_object_type": "topic",
            "owner_object_id": "ctx_alpha",
            "owner_version_id": None,
            "skill_version_id": None,
            "skill_step_id": None,
            "tool_name": "search_context",
            "outcome": "succeeded",
            "detail": {
                "branch_schema_version": DETAIL_SCHEMA,
                "branch_producer": PRODUCER_CONTEXT_SEARCH,
                "candidate_titles": ["Caf\u00e9"],
            },
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
            "detail": None,
        },
    ]


def _v1_preimage(steps):
    return {
        "path_id": "aip_search",
        "outcome": "succeeded",
        "policy_snapshot_hash": "p" * 64,
        "steps": [
            {
                "ordinal": step["ordinal"],
                "step_kind": step["step_kind"],
                "owner": [
                    step["owner_workspace"],
                    step["owner_object_type"],
                    step["owner_object_id"],
                    step["owner_version_id"],
                ],
                "skill": [step["skill_version_id"], step["skill_step_id"]],
                "tool_name": step["tool_name"],
                "outcome": step["outcome"],
            }
            for step in steps
        ],
    }


def _preimage(*, contract):
    return ai_paths.path_content_preimage(
        path_id="aip_search",
        outcome="succeeded",
        policy_snapshot_hash="p" * 64,
        steps=_hash_steps(),
        content_hash_contract=contract,
    )


def test_v2_preimage_is_exactly_v1_plus_schema_and_explicit_step_detail() -> None:
    steps = _hash_steps()
    expected = _v1_preimage(steps)
    expected["schema_version"] = "ai-path-content.v2"
    for projected, source in zip(expected["steps"], steps, strict=True):
        projected["detail"] = source["detail"]

    assert _preimage(contract="ai-path-content.v2") == expected
    assert expected["steps"][1]["detail"] is None


def test_v2_serializer_is_utf8_canonical_and_does_not_mutate_its_input() -> None:
    value = {"z": 0, "a": "Caf\u00e9", "nested": {"z": 2, "a": 1}}
    before = deepcopy(value)

    serialized = ai_paths.canonical_json_v2(value)

    assert serialized == '{"a":"Caf\u00e9","nested":{"a":1,"z":2},"z":0}'
    assert "\\u00e9" not in serialized
    assert value == before
    assert ai_paths.canonical_json_v2(value) == serialized


def test_v1_preimage_and_hash_remain_byte_identical() -> None:
    steps = _hash_steps()
    expected = _v1_preimage(steps)

    assert _preimage(contract=None) == expected
    expected_hash = hashlib.sha256(
        json.dumps(expected, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    assert ai_paths.content_hash(expected) == expected_hash


class _FinalizeCursor:
    def __init__(self, *, contract):
        self.contract = contract
        self.row = None
        self.updated_digest = None

    def execute(self, statement, params=()):
        normalized = " ".join(str(statement).split())
        if normalized.startswith("SELECT lifecycle"):
            self.row = (
                ai_paths.LIFECYCLE_RECORDING,
                "p" * 64,
                {"content_hash_contract": self.contract} if self.contract else {},
            )
        elif normalized.startswith("UPDATE app.ai_paths"):
            self.updated_digest = params[1]
            self.row = (
                "aip_search",
                "succeeded",
                datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc),
                self.updated_digest,
            )
        else:  # pragma: no cover - _load_steps is patched in these tests
            raise AssertionError(f"unexpected SQL: {normalized}")

    def fetchone(self):
        return self.row

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _FinalizeConnection:
    def __init__(self, *, contract):
        self.cur = _FinalizeCursor(contract=contract)

    def cursor(self):
        return self.cur


@pytest.mark.parametrize("contract", [None, "ai-path-content.v2"])
def test_finalize_selects_hash_contract_from_the_persisted_policy(
    monkeypatch, contract
) -> None:
    steps = _hash_steps()
    conn = _FinalizeConnection(contract=contract)
    monkeypatch.setattr(ai_paths, "_load_steps", lambda *_args: steps)

    ai_paths.finalize_path(
        conn,
        path_id="aip_search",
        project_id="proj_EXAMPLE",
        outcome="succeeded",
    )

    preimage = _preimage(contract=contract)
    if contract == "ai-path-content.v2":
        encoded = ai_paths.canonical_json_v2(preimage).encode("utf-8")
        expected = hashlib.sha256(encoded).hexdigest()
    else:
        expected = ai_paths.content_hash(preimage)
    assert conn.cur.updated_digest == expected


_TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
_TRACEPARENT = f"00-{_TRACE}-00f067aa0ba902b7-01"


class _ObserverConnection:
    def __init__(self, events):
        self.events = events
        self.commits = 0
        self.rollbacks = 0

    def transaction(self):
        return contextlib.nullcontext()

    def commit(self):
        self.commits += 1
        self.events.append(("commit",))

    def rollback(self):
        self.rollbacks += 1
        self.events.append(("rollback",))

    def __enter__(self):
        self.events.append(("connection_enter",))
        return self

    def __exit__(self, exc_type, _exc, _tb):
        self.events.append(("connection_exit", exc_type))
        return False


def _search_middleware_context():
    meta = {"traceparent": _TRACEPARENT}
    fastmcp_context = SimpleNamespace(
        request_context=SimpleNamespace(meta=meta)
    )
    return SimpleNamespace(
        message=SimpleNamespace(
            name="search_context",
            arguments={"project_id": "proj_EXAMPLE", "query": "pacing"},
            meta=meta,
        ),
        fastmcp_context=fastmcp_context,
    )


def _install_search_observer_fakes(monkeypatch, events, *, append_error=False):
    conn = _ObserverConnection(events)
    requested_actors = []

    def request_connection(actor, **_kwargs):
        requested_actors.append(actor)
        return conn

    monkeypatch.setattr("core.db.request_connection", request_connection)

    def generic_connection_is_forbidden():
        raise AssertionError(
            "dedicated search observation must use request_connection(actor)"
        )

    monkeypatch.setattr("core.db.get_connection", generic_connection_is_forbidden)
    monkeypatch.setattr(
        ai_path_recorder,
        "_resolve_scope_on_connection",
        lambda _conn, _arguments: ("proj_EXAMPLE", "org_EXAMPLE"),
    )

    def no_trace_path(*_args, **_kwargs):
        raise AssertionError("search_context must never look up or reuse a trace path")

    monkeypatch.setattr(ai_path_recorder, "_open_path_for", no_trace_path)
    monkeypatch.setattr("core.mcp_profiles._identity", lambda: "owner@example.com")

    def begin(_conn, **kwargs):
        events.append(("begin", deepcopy(kwargs)))
        return {
            "id": "aip_search",
            "lifecycle": ai_paths.LIFECYCLE_RECORDING,
            "started_at": datetime.now(timezone.utc),
        }

    def append(_conn, **kwargs):
        events.append(("append", deepcopy(kwargs)))
        if append_error and kwargs.get("step_kind") == "knowledge_read":
            raise RuntimeError("branch storage refused")
        return {"id": f"aps_{len(events)}", "ordinal": len(events)}

    def finalize(_conn, **kwargs):
        events.append(("finalize", deepcopy(kwargs)))
        return kwargs

    monkeypatch.setattr(ai_paths, "begin_path", begin)
    monkeypatch.setattr(ai_paths, "append_step", append)
    monkeypatch.setattr(ai_paths, "finalize_path", finalize)

    seen_pins = []

    def skill_pin(_conn, **kwargs):
        seen_pins.append(kwargs)
        events.append(("skill_pin", deepcopy(kwargs)))
        return ("proc_context@7", "2", "proc_context")

    monkeypatch.setattr(ai_path_recorder, "_skill_step_of", skill_pin)
    return conn, seen_pins, requested_actors


@pytest.mark.anyio
async def test_search_path_is_fresh_started_before_call_and_finalized_atomically(
    monkeypatch,
) -> None:
    events = []
    conn, seen_pins, requested_actors = _install_search_observer_fakes(
        monkeypatch,
        events,
    )
    middleware = ai_path_recorder.build_middleware()
    result_value = {"content": "search result stays unchanged"}

    async def call_next(_context):
        events.append(("tool_enter", datetime.now(timezone.utc)))
        await ai_path_recorder.emit_step(
            step_kind="knowledge_read",
            outcome="succeeded",
            tool_name="search_context",
            owner_workspace="context-hub",
            owner_object_type="topic",
            owner_object_id="ctx_alpha",
            detail=_unavailable(),
        )
        events.append(("tool_leave", datetime.now(timezone.utc)))
        return result_value

    result = await middleware.on_call_tool(_search_middleware_context(), call_next)

    assert result is result_value
    names = [event[0] for event in events]
    assert names == [
        "connection_enter",
        "begin",
        "skill_pin",
        "tool_enter",
        "tool_leave",
        "append",
        "append",
        "append",
        "finalize",
        "commit",
        "connection_exit",
    ]
    begin = next(event[1] for event in events if event[0] == "begin")
    assert begin["policy_snapshot"]["content_hash_contract"] == "ai-path-content.v2"
    assert begin["w3c_trace_id"] == _TRACE
    assert requested_actors == ["owner@example.com"]

    appended = [event[1] for event in events if event[0] == "append"]
    assert [step["step_kind"] for step in appended] == [
        "knowledge_read",
        "skill_step",
        "tool_call",
    ]
    assert all(step["path_id"] == "aip_search" for step in appended)
    assert appended[0]["observed_at"].tzinfo is not None
    tool_enter = next(event[1] for event in events if event[0] == "tool_enter")
    tool_leave = next(event[1] for event in events if event[0] == "tool_leave")
    assert tool_enter <= appended[0]["observed_at"] <= tool_leave
    assert appended[1]["skill_version_id"] == "proc_context@7"
    assert appended[1]["skill_step_id"] == "2"
    assert seen_pins == [
        {
            "trace_id": _TRACE,
            "tool_name": "search_context",
            "project_id": "proj_EXAMPLE",
        }
    ]
    assert names.index("skill_pin") < names.index("tool_enter")
    assert conn.commits == 1


@pytest.mark.anyio
async def test_search_observer_storage_failure_leaves_no_finalized_path(
    monkeypatch,
) -> None:
    events = []
    conn, _seen_pins, requested_actors = _install_search_observer_fakes(
        monkeypatch,
        events,
        append_error=True,
    )
    middleware = ai_path_recorder.build_middleware()
    result_value = {"content": "search result survives observer failure"}

    async def call_next(_context):
        events.append(("tool_enter", datetime.now(timezone.utc)))
        await ai_path_recorder.emit_step(
            step_kind="knowledge_read",
            outcome="succeeded",
            tool_name="search_context",
            owner_workspace="context-hub",
            owner_object_type="topic",
            owner_object_id="ctx_alpha",
            detail=_unavailable(),
        )
        events.append(("tool_leave", datetime.now(timezone.utc)))
        return result_value

    result = await middleware.on_call_tool(_search_middleware_context(), call_next)

    assert result is result_value
    names = [event[0] for event in events]
    assert names.index("begin") < names.index("tool_enter")
    assert names.index("skill_pin") < names.index("tool_enter")
    assert "append" in names
    assert "finalize" not in names
    assert requested_actors == ["owner@example.com"]
    assert conn.commits == 0
    assert conn.rollbacks == 1 or names[-1] == "connection_exit"


# ---------------------------------------------------------------------------
# Task 3 -- one closed public projector for both final evidence doors.
# ---------------------------------------------------------------------------


def _flat_context_detail(count=2, *, title="Candidate"):
    candidates = []
    for rank in range(1, count + 1):
        candidates.append(
            {
                "id": f"ctx_{rank}",
                "kind": "topic",
                "title": f"{title}{rank}",
                "score": 3.0,
                "tier": "title",
                "matched": True,
                "rank": rank,
                "fate": "selected",
                "reason": None,
            }
        )
    detail = {
        "branch_schema_version": DETAIL_SCHEMA,
        "branch_producer": PRODUCER_CONTEXT_SEARCH,
        "retrieval_mode": "lexical",
        "graph_hop_depth": 1,
        "semantic_recall": False,
        "limit": count,
        "not_reached_enumerated": False,
        "reached_count": count,
        "selected_count": count,
        "rejected_count": 0,
        "candidates_listed": count,
        "listing_truncated": False,
        "tiers_title": 3.0,
        "tiers_description": 2.0,
        "tiers_neighbor": 1.0,
    }
    for field in candidate_emission.CANDIDATE_FIELDS:
        key = "candidate_matched" if field == "matched" else f"candidate_{field}s"
        detail[key] = [candidate[field] for candidate in candidates]
    return detail


def test_public_branch_projection_has_exact_closed_shape_and_semantics():
    evidence = _public_evidence(_build(_context_candidates(), _context_descriptor()))

    assert evidence == {
        "schema_version": PUBLIC_SCHEMA,
        "state": "branches_listed",
        "walk": {
            "producer": "context_search",
            "mode": "lexical",
            "graph_hop_depth": 1,
            "semantic_recall": False,
            "selection_limit": 1,
            "judged_count": 2,
            "selected_count": 1,
            "rejected_count": 1,
            "listed_count": 2,
            "listing_truncated": False,
            "not_reached_enumerated": False,
            "tier_scale": {"title": 3.0, "description": 2.0, "neighbor": 1.0},
        },
        "branches": [
            {
                "id": "ctx_Alpha-1:rev.2",
                "kind": "topic",
                "title": "Café Q2",
                "score": 3.0,
                "tier": "title",
                "matched": True,
                "rank": 1,
                "fate": "selected",
                "reason": None,
            },
            {
                "id": "ctx_beta",
                "kind": "schema_doc",
                "title": "Beta",
                "score": 1.0,
                "tier": "neighbor",
                "matched": False,
                "rank": 2,
                "fate": "rejected",
                "reason": candidate_fate.REASON_BELOW_CUTOFF,
            },
        ],
    }
    encoded = ai_paths.canonical_json_v2(evidence)
    for forbidden in (
        "candidate_ids",
        "query",
        "tokens",
        "snippet",
        "arguments",
        "actor",
        "project",
        "trace",
        "error",
        "body",
        "reasoning",
        "detail",
    ):
        assert forbidden not in encoded


@pytest.mark.parametrize(
    ("step_kind", "tool_name", "expected"),
    [
        ("knowledge_read", "search_context", "branches_not_recorded"),
        ("knowledge_read", "briefing_context_event", "branches_not_recorded"),
        ("tool_call", "search_context", None),
        ("knowledge_read", "other_retriever", None),
        ("tool_call", "other_retriever", None),
    ],
)
def test_branch_control_is_present_exactly_for_named_retrieval_steps(
    step_kind, tool_name, expected
):
    evidence = _public_evidence(None, tool_name=tool_name) if step_kind == "knowledge_read" else (
        ai_paths.project_branch_evidence_for_steps(
            [_retrieval_step(None, tool_name=tool_name, step_kind=step_kind)]
        )[0]
    )
    assert (evidence or {}).get("state") == expected


def test_zero_judged_and_exact_unavailable_sentinel_keep_distinct_states():
    zero = _flat_context_detail(0)
    assert _public_evidence(zero) == {
        "schema_version": PUBLIC_SCHEMA,
        "state": "no_branch_judged",
        "walk": {
            "producer": "context_search",
            "mode": "lexical",
            "graph_hop_depth": 1,
            "semantic_recall": False,
            "selection_limit": 0,
            "judged_count": 0,
            "selected_count": 0,
            "rejected_count": 0,
            "listed_count": 0,
            "listing_truncated": False,
            "not_reached_enumerated": False,
            "tier_scale": {"title": 3.0, "description": 2.0, "neighbor": 1.0},
        },
        "branches": [],
    }
    assert _public_evidence(_unavailable()) == {
        "schema_version": PUBLIC_SCHEMA,
        "state": "unavailable",
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(branch_schema_version="wrong"),
        lambda d: d.update(branch_producer="briefing_context_event"),
        lambda d: d.update(candidate_ids=["bad id"]),
        lambda d: d.update(candidate_titles=["not  collapsed", "Beta"]),
        lambda d: d.update(candidate_scores=[math.inf, 1.0]),
        lambda d: d.update(candidate_ranks=[1, True]),
        lambda d: d.update(candidate_fates=["selected", "not_reached"]),
        lambda d: d.update(
            candidate_reasons=[
                candidate_fate.REASON_BELOW_CUTOFF,
                candidate_fate.REASON_BELOW_CUTOFF,
            ]
        ),
        lambda d: d.update(reached_count=3),
        lambda d: d.update(listing_truncated=True),
        lambda d: d.update(secret_body="must not cross"),
    ],
)
def test_invalid_marked_detail_fails_closed_without_downgrading_the_path(mutate):
    detail = _build(_context_candidates(), _context_descriptor())
    mutate(detail)
    before = deepcopy(detail)

    assert _public_evidence(detail) == {
        "schema_version": PUBLIC_SCHEMA,
        "state": "unavailable",
    }
    assert detail == before


def test_briefing_projection_has_null_not_zero_for_inapplicable_branch_fields():
    evidence = _public_evidence(
        _build(
            _briefing_candidates(),
            _briefing_descriptor(),
            producer=PRODUCER_BRIEFING,
        ),
        tool_name="briefing_context_event",
    )

    assert evidence["state"] == "branches_listed"
    assert evidence["walk"]["producer"] == PRODUCER_BRIEFING
    assert evidence["walk"]["tier_scale"] is None
    assert evidence["branches"][0]["score"] is None
    assert evidence["branches"][0]["tier"] is None
    assert evidence["branches"][0]["matched"] is None


def test_projector_caps_at_24_whole_ranked_rows_without_rezipping():
    evidence = _public_evidence(_flat_context_detail(25))

    assert evidence["state"] == "branches_listed"
    assert len(evidence["branches"]) == 24
    assert [branch["rank"] for branch in evidence["branches"]] == list(range(1, 25))
    assert evidence["walk"]["judged_count"] == 25
    assert evidence["walk"]["listed_count"] == 24
    assert evidence["walk"]["listing_truncated"] is True


def test_step_byte_wall_is_inclusive_and_only_downgrades_that_step(monkeypatch):
    small = _flat_context_detail(1)
    large = _flat_context_detail(24, title="X" * 190)
    large_size = len(ai_paths.canonical_json_v2(_public_evidence(large)).encode("utf-8"))
    steps = [_retrieval_step(large, ordinal=0), _retrieval_step(small, ordinal=1)]

    monkeypatch.setattr(ai_paths, "BRANCH_EVIDENCE_MAX_BYTES_PER_STEP", large_size)
    assert [item["state"] for item in ai_paths.project_branch_evidence_for_steps(steps)] == [
        "branches_listed",
        "branches_listed",
    ]
    monkeypatch.setattr(ai_paths, "BRANCH_EVIDENCE_MAX_BYTES_PER_STEP", large_size - 1)
    assert [item["state"] for item in ai_paths.project_branch_evidence_for_steps(steps)] == [
        "unavailable",
        "branches_listed",
    ]


def test_aggregate_candidate_wall_allocates_by_ordinal_then_closes_the_tail():
    steps = [
        *[_retrieval_step(_flat_context_detail(24), ordinal=ordinal) for ordinal in range(8)],
        _retrieval_step(_flat_context_detail(8), ordinal=8),
        _retrieval_step(_flat_context_detail(1), ordinal=9),
    ]
    steps.reverse()
    evidence = ai_paths.project_branch_evidence_for_steps(steps)
    by_ordinal = {step["ordinal"]: item for step, item in zip(steps, evidence)}

    assert sum(len(by_ordinal[ordinal]["branches"]) for ordinal in range(9)) == 200
    assert by_ordinal[9] == {"schema_version": PUBLIC_SCHEMA, "state": "unavailable"}


def test_aggregate_byte_wall_is_inclusive_and_closes_every_later_step(monkeypatch):
    steps = [_retrieval_step(_flat_context_detail(1), ordinal=i) for i in range(3)]
    one = _public_evidence(_flat_context_detail(1))
    size = len(ai_paths.canonical_json_v2(one).encode("utf-8"))

    monkeypatch.setattr(ai_paths, "BRANCH_EVIDENCE_MAX_BYTES_PER_PATH", size * 2)
    states = [item["state"] for item in ai_paths.project_branch_evidence_for_steps(steps)]
    assert states == ["branches_listed", "branches_listed", "unavailable"]
    monkeypatch.setattr(ai_paths, "BRANCH_EVIDENCE_MAX_BYTES_PER_PATH", size * 2 - 1)
    states = [item["state"] for item in ai_paths.project_branch_evidence_for_steps(steps)]
    assert states == ["branches_listed", "unavailable", "unavailable"]


def test_public_branch_budget_constants_are_the_story_hard_walls():
    assert ai_paths.BRANCH_EVIDENCE_MAX_CANDIDATES_PER_STEP == 24
    assert ai_paths.BRANCH_EVIDENCE_MAX_CANDIDATES_PER_PATH == 200
    assert ai_paths.BRANCH_EVIDENCE_MAX_BYTES_PER_STEP == 8_192
    assert ai_paths.BRANCH_EVIDENCE_MAX_BYTES_PER_PATH == 65_536
    assert ai_paths.OBSERVED_AI_PATH_MAX_BYTES == 262_144


def test_observed_and_authorized_wire_use_identical_public_evidence(monkeypatch):
    detail = _build(_context_candidates(), _context_descriptor())
    raw_step = _retrieval_step(detail)
    path = {
        "id": "aip_branch",
        "lifecycle": "finalized",
        "outcome": "succeeded",
        "policy_snapshot": {"content_hash_contract": ai_paths.AI_PATH_CONTENT_V2},
        "steps": [raw_step],
    }
    monkeypatch.setattr(ai_paths, "load_path", lambda *_args, **_kwargs: path)

    observed = ai_paths.project_observed_ai_path(
        object(), project_id="proj_1", ai_path="aip_branch"
    )
    wire = ai_paths.wire_steps_projection(path["steps"])

    assert ai_paths.canonical_json_v2(observed["steps"][0]["branch_evidence"]) == (
        ai_paths.canonical_json_v2(wire[0]["branch_evidence"])
    )
    assert "detail" not in wire[0]
    assert raw_step["detail"] == detail


def test_http_detail_is_ai_path_v2_and_has_no_raw_detail(monkeypatch):
    from core import ai_paths_api
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient

    detail = _build(_context_candidates(), _context_descriptor())
    path_value = {
        "id": "aip_branch",
        "lifecycle": "finalized",
        "outcome": "succeeded",
        "actor": "owner@example.com",
        "started_at": datetime(2026, 8, 10, 8, 0, tzinfo=timezone.utc),
        "ended_at": datetime(2026, 8, 10, 8, 1, tzinfo=timezone.utc),
        "policy_snapshot": {"content_hash_contract": ai_paths.AI_PATH_CONTENT_V2},
        "steps": [_retrieval_step(detail)],
        "assessment": {"verdict": "pass"},
    }

    async def authorize(_request):
        return "proj_1", "owner@example.com"

    monkeypatch.setattr(ai_paths_api, "_authorize", authorize)
    monkeypatch.setattr(
        ai_paths_api,
        "_strict_project_access",
        lambda *_args, **_kwargs: SimpleNamespace(allowed=True, org_id="org_1"),
    )
    monkeypatch.setattr(
        ai_paths_api,
        "request_connection",
        lambda _actor: contextlib.nullcontext(object()),
    )
    # The two 49-6 readers (event references, my own reactions) query the real
    # connection; this test's connection is `object()` on purpose -- it judges
    # the DETAIL projection, not the owner links. Stubbed to their empty
    # answers, exactly what a path with no events and no reactions yields.
    monkeypatch.setattr(
        ai_paths_api,
        "_compose_event_references",
        lambda _conn, *, project_id, references: [],
    )
    monkeypatch.setattr(
        ai_paths_api,
        "_my_step_reactions",
        lambda _conn, **_kwargs: {},
    )
    monkeypatch.setattr(ai_paths, "load_path", lambda *_args, **_kwargs: path_value)
    app = Starlette(
        routes=[Route("/api/projects/{project_id}/paths/{path_id}", ai_paths_api._get_ai_path)]
    )

    with patch("core.ai_paths.load_path", return_value=path_value):
        response = TestClient(app).get("/api/projects/proj_1/paths/aip_branch")

    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == "ai-path.v2"
    assert body["steps"][0]["branch_evidence"]["state"] == "branches_listed"
    assert '"detail"' not in response.text

    path_value["lifecycle"] = "recording"
    with patch("core.ai_paths.load_path", return_value=path_value):
        response = TestClient(app).get("/api/projects/proj_1/paths/aip_branch")
    assert response.status_code == 200
    assert response.json()["steps"][0]["branch_evidence"] == {
        "schema_version": PUBLIC_SCHEMA,
        "state": "unavailable",
    }
    assert (
        "Caf" not in response.text
        and candidate_fate.REASON_BELOW_CUTOFF not in response.text
    )


def test_historical_retrieval_detail_is_never_projected_as_frozen_evidence(monkeypatch):
    step = _retrieval_step(_flat_context_detail(1))
    path = {
        "id": "aip_historical",
        "lifecycle": "finalized",
        "outcome": "succeeded",
        "policy_snapshot": {},
        "steps": [step],
    }
    monkeypatch.setattr(ai_paths, "load_path", lambda *_args, **_kwargs: path)

    observed = ai_paths.project_observed_ai_path(
        object(), project_id="proj_1", ai_path="aip_historical"
    )
    wire = ai_paths.wire_steps_projection(
        path["steps"], content_hash_contract=None
    )

    expected = {"schema_version": PUBLIC_SCHEMA, "state": "branches_not_recorded"}
    assert observed["steps"][0]["branch_evidence"] == expected
    assert wire[0]["branch_evidence"] == expected
    assert "candidate_ids" not in ai_paths.canonical_json_v2(observed)


def test_marked_detail_rejects_selected_count_above_limit_and_uncanonical_mode():
    over_limit = _flat_context_detail(2)
    over_limit["limit"] = 1
    over_limit["selected_count"] = 2
    over_limit["rejected_count"] = 0
    over_limit["candidate_fates"] = ["selected", "selected"]
    over_limit["candidate_reasons"] = [None, None]
    assert _public_evidence(over_limit)["state"] == "unavailable"

    uncanonical = _flat_context_detail(1)
    uncanonical["retrieval_mode"] = " lexical  title "
    assert _public_evidence(uncanonical)["state"] == "unavailable"


def test_context_numeric_measures_are_floats_but_counts_remain_integers():
    detail = _flat_context_detail(1)
    detail["candidate_scores"] = [3]
    detail["tiers_title"] = 3
    detail["tiers_description"] = 2
    detail["tiers_neighbor"] = 1

    evidence = _public_evidence(detail)

    assert evidence["branches"][0]["score"] == 3.0
    assert isinstance(evidence["branches"][0]["score"], float)
    assert evidence["walk"]["tier_scale"] == {
        "title": 3.0,
        "description": 2.0,
        "neighbor": 1.0,
    }
    assert all(isinstance(value, float) for value in evidence["walk"]["tier_scale"].values())
    assert isinstance(evidence["walk"]["judged_count"], int)
    canonical = ai_paths.canonical_json_v2(evidence)
    assert '"score":3.0' in canonical
    assert '"tier_scale":{"description":2.0,"neighbor":1.0,"title":3.0}' in canonical


def test_http_detail_outer_step_and_byte_walls_fail_closed(monkeypatch):
    from core import ai_paths_api
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient

    base = {
        "id": "aip_wall",
        "lifecycle": "finalized",
        "outcome": "succeeded",
        "actor": "owner@example.com",
        "policy_snapshot": {"content_hash_contract": ai_paths.AI_PATH_CONTENT_V2},
        "assessment": {"verdict": "pass"},
    }

    async def authorize(_request):
        return "proj_1", "owner@example.com"

    monkeypatch.setattr(ai_paths_api, "_authorize", authorize)
    monkeypatch.setattr(
        ai_paths_api,
        "_strict_project_access",
        lambda *_args, **_kwargs: SimpleNamespace(allowed=True, org_id="org_1"),
    )
    monkeypatch.setattr(
        ai_paths_api, "request_connection", lambda _actor: contextlib.nullcontext(object())
    )
    app = Starlette(
        routes=[Route("/api/projects/{project_id}/paths/{path_id}", ai_paths_api._get_ai_path)]
    )

    with patch("core.ai_paths.load_path", return_value={**base, "steps": [{}] * 201}):
        response = TestClient(app).get("/api/projects/proj_1/paths/aip_wall")
    assert response.status_code == 503
    assert response.json()["code"] == "ai_paths_unavailable"

    oversized = {**base, "steps": [], "model_ref": "X" * 300_000}
    with patch("core.ai_paths.load_path", return_value=oversized):
        response = TestClient(app).get("/api/projects/proj_1/paths/aip_wall")
    assert response.status_code == 503
    assert "X" not in response.text


def test_http_detail_serves_the_interaction_routed_bounded_and_said(monkeypatch):
    """Round 7, B3: the route's `interaction` block -- merged rows ROUTED like own steps, the
    drawing bound SAID -- read through the route, so a route that bypasses the helper goes red."""
    import contextlib
    from datetime import timedelta
    from types import SimpleNamespace

    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from core import ai_paths_api

    t0 = datetime(2026, 8, 10, 8, 0, tzinfo=timezone.utc)
    own_steps = [
        {"ordinal": 0, "step_kind": "tool_call", "tool_name": "compose_analyze_pivot", "outcome": "succeeded", "observed_at": t0}
    ]
    sibling_steps = [
        {"ordinal": 0, "step_kind": "tool_call", "tool_name": "execute_analyze_query_spec", "outcome": "succeeded",
         "observed_at": t0 + timedelta(seconds=1), "owner_workspace": "analyze", "owner_object_type": "query-spec",
         "owner_object_id": "qs_1", "owner_version_id": "qsv_1"}
    ]
    path_value = {
        "id": "aip_own", "lifecycle": "finalized", "outcome": "succeeded", "actor": "owner@example.com",
        "started_at": t0, "ended_at": t0 + timedelta(minutes=1), "w3c_trace_id": "a" * 32,
        "policy_snapshot": {"content_hash_contract": ai_paths.AI_PATH_CONTENT_V2},
        "steps": own_steps, "assessment": {"verdict": "pass"},
    }

    async def authorize(_request):
        return "proj_1", "owner@example.com"

    monkeypatch.setattr(ai_paths_api, "_authorize", authorize)
    monkeypatch.setattr(ai_paths_api, "_strict_project_access", lambda *_a, **_k: SimpleNamespace(allowed=True, org_id="org_1"))
    monkeypatch.setattr(ai_paths_api, "request_connection", lambda _actor: contextlib.nullcontext(object()))
    monkeypatch.setattr(ai_paths_api, "_compose_event_references", lambda _conn, *, project_id, references: [])
    monkeypatch.setattr(ai_paths_api, "_my_step_reactions", lambda _conn, **_k: {})
    monkeypatch.setattr(ai_paths_api, "_steps_or_none", lambda _conn, _project_id, _path_id: sibling_steps)
    monkeypatch.setattr(ai_paths, "load_path", lambda *_a, **_k: path_value)
    monkeypatch.setattr(ai_paths, "paths_sharing_trace", lambda conn, *, project_id, trace_id, exclude: [{"path_id": "aip_exec", "state": "finalized"}])
    monkeypatch.setattr(ai_paths, "skill_coverage", lambda conn, steps: [])
    monkeypatch.setattr(ai_paths, "owner_labels", lambda conn, steps: {("query-spec", "qs_1"): "Kardinal crossing"})
    monkeypatch.setattr(ai_paths, "names_for", lambda conn, steps: {})
    monkeypatch.setattr(ai_paths_api, "INTERACTION_MAX_STEPS", 1)
    app = Starlette(routes=[Route("/api/projects/{project_id}/paths/{path_id}", ai_paths_api._get_ai_path)])
    response = TestClient(app).get("/api/projects/proj_1/paths/aip_own")
    assert response.status_code == 200, response.text
    interaction = response.json()["interaction"]
    assert interaction["truncated"] is True and interaction["total_steps"] == 2
    # The drawing keeps the END: the sibling's later row, routed, labelled, addressed.
    (row,) = interaction["steps"]
    assert row["path_id"] == "aip_exec" and row["path_step_order"] == 0 and row["step_order"] == 0
    assert row["owner_label"] == "Kardinal crossing"
    assert row["owner_reference_state"] == "governed" and row["owner_reference"]["object_id"] == "qs_1"
