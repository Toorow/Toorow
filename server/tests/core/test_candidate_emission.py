"""The branches of the walk are EMITTED, and the reason stays cited data (Story 54.2).

Half A tasks T1/T2/T6 proved the walk *keeps* its dropped candidates. This file
covers what happens to them afterwards:

* **T3 / AC1, AC5** -- they ride the ``CONTEXT`` (and ``PROCEDURE``) crossing of
  Story 54.1's seam, each with its node, score, tier, ``matched`` and fate, and
  the mode / depth / cap of the walk travel on the SAME payload. A count of
  candidates without a statement of what produced them is the misleading half of
  the sentence, so the shape refuses to be built without it.
* **T4 / AC6, AC7** -- the reason crosses the text channel assembled from
  enumerated constants, and the AD-9 frame that separates it from model prose is
  `narrative.GUIDANCE_FRAME`, imported. Re-typing its wording fails a test here.
  No string this code composes asserts a cause.
* **T5 / AC8** -- Half B, delivered: Story 53.8 closed the pairing this half
  would explain (`608a8ae6`, verified in `briefing._find_context_event`: the
  claim's own date, exactly, and a declared platform that contradicts the
  connector disqualifies). So the business-event pairing now reports its branches
  too, with the same vocabulary and on the same seam.

Two refusals proven rather than asserted:

* nothing goes OUT when the client sent no ``progressToken``. It is now composed
  in that case, and that is migration 176: the crossings are persisted, so an
  unwatched walk must judge exactly as much as a watched one. Nothing is composed
  when no call is in flight at all -- there, no store and no client is waiting;
* the emission rides ONE seam. `core.candidate_emission` never touches
  ``report_progress`` itself, and a test reads its source to say so.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from types import SimpleNamespace

import anyio
import core.ai_path_recorder as recorder
import pytest
from core import anomaly_alerts, briefing, candidate_emission, candidate_fate, context_search
from core.ai_path_recorder import LEVEL_CONTEXT, LEVEL_PROCEDURE
from core.narrative import GUIDANCE_FRAME

# AI-317: a fake cursor that recognises SQL by text names its whole inventory and
# raises on a statement it was never taught.
from tests.support.statement_router import StatementInventory, UnknownStatement

# ---------------------------------------------------------------------------
# Harness: an armed channel, and a synchronous call site reaching it.
# ---------------------------------------------------------------------------


class _FakeClient:
    """Keeps every notification this call posted, decoded."""

    def __init__(self, *, progress_token: str | None = "tok-1") -> None:
        self.payloads: list[dict] = []
        self.request_context = SimpleNamespace(
            meta=SimpleNamespace(progressToken=progress_token)
        )

    async def report_progress(self, progress, total=None, message=None):
        self.payloads.append(json.loads(message))


async def _in_armed_call(client: _FakeClient, fn):
    """Run *fn* the way FastMCP runs a synchronous tool body: on a worker thread.

    `emit_step_sync` exists because `search_context` is synchronous; running the
    call site on the event loop thread would prove a path production never takes.
    """
    loop = asyncio.get_running_loop()
    emitter = recorder._PathEmitter(client, loop)
    token = recorder._ACTIVE_EMITTER.set(emitter)
    try:
        return await anyio.to_thread.run_sync(fn)
    finally:
        recorder._ACTIVE_EMITTER.reset(token)


def _armed(fn, *, progress_token: str | None = "tok-1") -> tuple[object, _FakeClient]:
    client = _FakeClient(progress_token=progress_token)
    result = anyio.run(_in_armed_call, client, fn)
    return result, client


def _hit(node_id: str, kind: str, score: float, tier: str, *, matched: bool = True,
         project_id: str | None = "proj_EXAMPLE") -> context_search.ContextHit:
    return context_search.ContextHit(
        id=node_id, kind=kind, title=f"Title of {node_id}", snippet="body",
        score=score, tier=tier, project_id=project_id, matched=matched,
    )


def _walk(limit: int = 2) -> context_search.ContextWalk:
    """Three reached candidates, two kept: one procedure, two topics."""
    return context_search.ContextWalk(
        query="pacing",
        project_id="proj_EXAMPLE",
        limit=limit,
        ranked=[
            _hit("ctx_top", "topic", context_search.TIER_TITLE, "title"),
            _hit("prc_one", "procedure", context_search.TIER_DESCRIPTION, "description"),
            _hit("ctx_hop", "topic", context_search.TIER_NEIGHBOR, "neighbor", matched=False),
        ],
    )


# ---------------------------------------------------------------------------
# T3 / AC1 -- every candidate travels, with its node and its fate
# ---------------------------------------------------------------------------


def test_each_candidate_travels_with_its_node_score_tier_matched_and_fate() -> None:
    walk = _walk()
    sent, client = _armed(lambda: context_search.emit_walk(walk))

    assert sent == 2, "a procedure crossing and a context crossing"
    listed = {
        node_id: index
        for payload in client.payloads
        for index, node_id in enumerate(payload["detail"]["candidate_ids"])
    }
    assert set(listed) == {"ctx_top", "prc_one", "ctx_hop"}

    context_payload = next(p for p in client.payloads if p["level"] == LEVEL_CONTEXT)
    assert context_payload["tool_name"] == candidate_emission.TOOL_SEARCH_CONTEXT
    detail = context_payload["detail"]
    assert detail["candidate_kinds"] == ["topic", "topic"]
    assert detail["candidate_titles"] == ["Title of ctx_top", "Title of ctx_hop"]
    assert detail["candidate_scores"] == [context_search.TIER_TITLE, context_search.TIER_NEIGHBOR]
    assert detail["candidate_tiers"] == ["title", "neighbor"]
    # `matched` keeps its meaning: reached directly vs reached by the graph hop.
    assert detail["candidate_matched"] == [True, False]
    assert detail["candidate_ranks"] == [1, 3]
    assert detail["candidate_fates"] == [
        candidate_fate.FATE_SELECTED,
        candidate_fate.FATE_REJECTED,
    ]
    assert detail["candidate_reasons"] == [None, candidate_fate.REASON_BELOW_CUTOFF]


def test_a_procedure_candidate_is_a_different_rung_than_a_context_one() -> None:
    """`level_of` reads the OBJECT TYPE, not the step kind -- both are knowledge reads."""
    _sent, client = _armed(lambda: context_search.emit_walk(_walk()))
    levels = [p["level"] for p in client.payloads]
    assert levels == [LEVEL_PROCEDURE, LEVEL_CONTEXT], (
        "the crossings must arrive in the order the reading grid puts them"
    )
    procedure_payload = client.payloads[0]
    assert procedure_payload["owner_object_type"] == candidate_emission.OBJECT_TYPE_PROCEDURE
    assert procedure_payload["owner_object_id"] == "prc_one"
    assert procedure_payload["detail"]["candidate_ids"] == ["prc_one"]


def test_a_crossing_that_kept_nothing_names_no_owner() -> None:
    """An owner invented for a crossing that kept nothing is a node nobody reached."""
    walk = _walk(limit=0)
    _sent, client = _armed(lambda: context_search.emit_walk(walk))
    for payload in client.payloads:
        assert payload["owner_object_id"] is None
        assert payload["detail"]["selected_count"] == 0
        assert set(payload["detail"]["candidate_fates"]) == {candidate_fate.FATE_REJECTED}


def test_a_walk_that_reached_nothing_still_crosses() -> None:
    """An absent rung is indistinguishable from "it never looked"."""
    empty = context_search.ContextWalk(
        query="nothing", project_id="proj_EXAMPLE", limit=20, ranked=[]
    )
    sent, client = _armed(lambda: context_search.emit_walk(empty))
    assert sent == 1
    payload = client.payloads[0]
    assert payload["level"] == LEVEL_CONTEXT
    assert payload["owner_object_id"] is None
    assert payload["detail"]["reached_count"] == 0
    assert payload["detail"]["candidates_listed"] == 0


def test_the_counts_of_two_crossings_never_double_count_a_candidate() -> None:
    walk = _walk()
    _sent, client = _armed(lambda: context_search.emit_walk(walk))
    assert sum(p["detail"]["reached_count"] for p in client.payloads) == len(walk.ranked)
    assert sum(p["detail"]["selected_count"] for p in client.payloads) == len(walk.selected)


# ---------------------------------------------------------------------------
# T3 / AC5 -- the mode and the depth are FIELDS, on the same crossing
# ---------------------------------------------------------------------------


def test_the_mode_and_the_depth_ride_the_same_payload_as_the_candidates() -> None:
    _sent, client = _armed(lambda: context_search.emit_walk(_walk()))
    for payload in client.payloads:
        detail = payload["detail"]
        assert detail["retrieval_mode"] == context_search.RETRIEVAL_MODE == "lexical"
        assert detail["graph_hop_depth"] == context_search.GRAPH_HOP_DEPTH == 1
        assert detail["semantic_recall"] is False
        assert detail["not_reached_enumerated"] is False
        assert detail["limit"] == 2
        # The tier scale travels too: scores with nothing to read them against
        # are numbers a surface can rank but cannot explain.
        assert detail["tiers_title"] == context_search.TIER_TITLE
        assert detail["tiers_neighbor"] == context_search.TIER_NEIGHBOR
        # ... and it is the SAME payload the candidates are on, not a sibling.
        assert detail["candidate_ids"]
        assert detail["branch_schema_version"] == candidate_emission.BRANCH_SCHEMA_VERSION
        assert detail["branch_producer"] == candidate_emission.PRODUCER_CONTEXT_SEARCH
        assert detail["listing_truncated"] is False


def test_candidates_cannot_be_built_without_the_descriptor_that_produced_them() -> None:
    """"20 candidates evaluated" over a lexical one-hop walk is the lie to prevent."""
    assert candidate_emission.candidate_detail(
        [{"id": "ctx_a"}],
        {},
        producer=candidate_emission.PRODUCER_CONTEXT_SEARCH,
    ) == {
        "branch_schema_version": candidate_emission.BRANCH_SCHEMA_VERSION,
        "branch_producer": candidate_emission.PRODUCER_CONTEXT_SEARCH,
        "branch_state": candidate_emission.BRANCH_STATE_UNAVAILABLE,
    }


def test_the_producer_contract_is_closed_and_invalid_input_fails_to_a_sentinel() -> None:
    walk = _walk()
    candidates = [candidate.as_dict() for candidate in walk.candidates]
    descriptor = walk.retrieval_descriptor()

    invalid_cases = [
        (
            candidate_emission.PRODUCER_CONTEXT_SEARCH,
            [{**candidates[0], "score": float("inf")}],
            {**descriptor, "reached_count": 1, "selected_count": 1, "rejected_count": 0},
        ),
        (
            candidate_emission.PRODUCER_CONTEXT_SEARCH,
            [{**candidates[0], "rank": True}],
            {**descriptor, "reached_count": 1, "selected_count": 1, "rejected_count": 0},
        ),
        (
            candidate_emission.PRODUCER_CONTEXT_SEARCH,
            [{**candidates[0], "fate": candidate_fate.FATE_NOT_REACHED}],
            {**descriptor, "reached_count": 1, "selected_count": 0, "rejected_count": 0},
        ),
        (
            candidate_emission.PRODUCER_CONTEXT_SEARCH,
            [{**candidates[0], "kind": candidate_emission.OBJECT_TYPE_CONTEXT_EVENT}],
            {**descriptor, "reached_count": 1, "selected_count": 1, "rejected_count": 0},
        ),
        (
            candidate_emission.PRODUCER_CONTEXT_SEARCH,
            [
                {**candidates[0], "rank": 2},
                {**candidates[1], "rank": 2},
            ],
            {**descriptor, "reached_count": 2, "selected_count": 2, "rejected_count": 0},
        ),
        (
            candidate_emission.PRODUCER_CONTEXT_SEARCH,
            candidates,
            {**descriptor, "selected_count": 99},
        ),
        (
            candidate_emission.PRODUCER_CONTEXT_SEARCH,
            candidates,
            {**descriptor, "not_reached_enumerated": True},
        ),
    ]

    for producer, invalid_candidates, invalid_descriptor in invalid_cases:
        detail = candidate_emission.candidate_detail(
            invalid_candidates,
            invalid_descriptor,
            producer=producer,
        )
        assert detail == {
            "branch_schema_version": candidate_emission.BRANCH_SCHEMA_VERSION,
            "branch_producer": producer,
            "branch_state": candidate_emission.BRANCH_STATE_UNAVAILABLE,
        }

    with pytest.raises(ValueError, match="producer"):
        candidate_emission.candidate_detail(
            candidates,
            descriptor,
            producer="unknown",
        )


def test_titles_are_normalized_once_and_control_characters_fail_closed() -> None:
    walk = _walk()
    candidate = walk.candidates[0].as_dict()
    descriptor = {**walk.retrieval_descriptor(), "reached_count": 1,
                  "selected_count": 1, "rejected_count": 0}

    detail = candidate_emission.candidate_detail(
        [{**candidate, "title": "  Cafe\u0301   pricing  "}],
        descriptor,
        producer=candidate_emission.PRODUCER_CONTEXT_SEARCH,
    )
    assert detail["candidate_titles"] == ["Caf\u00e9 pricing"]

    unavailable = candidate_emission.candidate_detail(
        [{**candidate, "title": "safe\u202eunsafe"}],
        descriptor,
        producer=candidate_emission.PRODUCER_CONTEXT_SEARCH,
    )
    assert unavailable["branch_state"] == candidate_emission.BRANCH_STATE_UNAVAILABLE


def test_retrieval_mode_is_normalized_and_invalid_modes_fail_to_the_sentinel() -> None:
    walk = _walk()
    candidates = [candidate.as_dict() for candidate in walk.candidates]
    descriptor = walk.retrieval_descriptor()

    normalized = candidate_emission.candidate_detail(
        candidates,
        {**descriptor, "mode": "  lexical   ranked  "},
        producer=candidate_emission.PRODUCER_CONTEXT_SEARCH,
    )
    assert normalized["retrieval_mode"] == "lexical ranked"

    sentinel = {
        "branch_schema_version": candidate_emission.BRANCH_SCHEMA_VERSION,
        "branch_producer": candidate_emission.PRODUCER_CONTEXT_SEARCH,
        "branch_state": candidate_emission.BRANCH_STATE_UNAVAILABLE,
    }
    for mode in ("   ", "x" * 201, "lexical\u202eunsafe"):
        assert candidate_emission.candidate_detail(
            candidates,
            {**descriptor, "mode": mode},
            producer=candidate_emission.PRODUCER_CONTEXT_SEARCH,
        ) == sentinel


def test_the_crossing_survives_the_seams_sanitizer_untouched() -> None:
    """The shape must pass `sanitize_detail` whole -- a dropped list is a silent lie."""
    walk = _walk()
    built = candidate_emission.candidate_detail(
        [c.as_dict() for c in walk.candidates],
        walk.retrieval_descriptor(),
        producer=candidate_emission.PRODUCER_CONTEXT_SEARCH,
    )
    kept = recorder.sanitize_detail(built)
    assert kept is not None
    assert sorted(kept) == sorted(built), "the seam dropped a key of the crossing"
    assert len(built) <= recorder.DETAIL_MAX_KEYS


def test_a_long_node_name_makes_the_whole_branch_detail_unavailable() -> None:
    walk = context_search.ContextWalk(
        query="q", project_id="p", limit=5,
        ranked=[
            context_search.ContextHit(
                id="ctx_long", kind="topic", title="N" * 5_000, snippet="",
                score=3.0, tier="title", project_id="p", matched=True,
            ),
            _hit("ctx_short", "topic", 3.0, "title"),
        ],
    )
    detail = candidate_emission.candidate_detail(
        [c.as_dict() for c in walk.candidates],
        walk.retrieval_descriptor(),
        producer=candidate_emission.PRODUCER_CONTEXT_SEARCH,
    )
    assert detail == {
        "branch_schema_version": candidate_emission.BRANCH_SCHEMA_VERSION,
        "branch_producer": candidate_emission.PRODUCER_CONTEXT_SEARCH,
        "branch_state": candidate_emission.BRANCH_STATE_UNAVAILABLE,
    }


def test_a_listing_longer_than_the_seam_allows_says_so_instead_of_vanishing() -> None:
    ranked = [_hit(f"ctx_{i:03d}", "topic", 3.0, "title") for i in range(40)]
    walk = context_search.ContextWalk(query="q", project_id="p", limit=20, ranked=ranked)
    detail = candidate_emission.candidate_detail(
        [c.as_dict() for c in walk.candidates],
        walk.retrieval_descriptor(),
        producer=candidate_emission.PRODUCER_CONTEXT_SEARCH,
    )
    assert detail["candidates_listed"] == candidate_emission.CANDIDATE_LIST_LIMIT
    assert detail["reached_count"] == 40, "the count stays exact when the listing is cut"
    assert recorder.sanitize_detail(detail)["candidate_ids"], "the list must survive the seam"


# ---------------------------------------------------------------------------
# One seam, and silence without a progressToken
# ---------------------------------------------------------------------------


def _spy_on_composition(monkeypatch) -> list[dict]:
    composed: list[dict] = []
    real = candidate_emission.candidate_detail
    monkeypatch.setattr(
        candidate_emission,
        "candidate_detail",
        lambda *a, **k: composed.append(real(*a, **k)) or composed[-1],
    )
    return composed


def test_without_a_progress_token_the_walk_is_recorded_but_not_sent(monkeypatch) -> None:
    """Migration 176 moved this boundary, so the old assertion is restated, not kept.

    Until the crossings were persisted, "nobody will receive it" and "no
    progressToken" were the same sentence, and building the payload without a
    watcher was pure cost. They are no longer the same sentence: the store
    receives it. An unwatched walk that composed nothing recorded nothing, and a
    later reader could only say `branch count unknown` -- which is exactly the
    state Story 55.2 had to invent a fourth value for.

    What must still hold is that nothing goes OUT: no notification, no channel.
    """
    composed = _spy_on_composition(monkeypatch)
    sent, client = _armed(lambda: context_search.emit_walk(_walk()), progress_token=None)

    assert sent == 0, "an unwatched crossing must not claim to have gone out"
    assert client.payloads == [], "no progressToken means no notification"
    assert composed, "the candidates must still be composed, because they are kept"


def test_nothing_is_composed_when_no_call_is_in_flight(monkeypatch) -> None:
    """The cost guard that survives: outside a tool call there is nothing to keep.

    No emitter means no path will be written, so composing the payload buys
    nothing at all -- this is the case the original assertion was really
    protecting, and it is now the only one.
    """
    composed = _spy_on_composition(monkeypatch)

    assert context_search.emit_walk(_walk()) == 0
    assert composed == [], "a payload no store and no client will receive must not be built"


def test_the_emission_rides_story_54_1s_seam_and_not_a_second_one() -> None:
    source = inspect.getsource(candidate_emission)
    assert "emit_step_sync" in source
    for forbidden in ("report_progress", "notifications/progress", ".progressToken"):
        assert forbidden not in source, (
            f"{forbidden!r} here would be a second emission channel; "
            "Story 54.1 owns the only one"
        )


def test_an_emitter_that_explodes_never_breaks_the_walk(monkeypatch) -> None:
    monkeypatch.setattr(
        candidate_emission,
        "emit_step_sync",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("channel on fire")),
    )
    sent, _client = _armed(lambda: context_search.emit_walk(_walk()))
    assert sent == 0


# ---------------------------------------------------------------------------
# The wiring is real: `search_context` itself crosses
# ---------------------------------------------------------------------------


# THE SEVEN STATEMENTS `search_context` MAKES over a corpus that holds only
# topics. Six of them used to be answered by the same silent `else []` -- a
# corpus with no procedure and a corpus whose procedure query MOVED were
# indistinguishable, and the second is a broken test that reads as a green one
# (AI-317). Naming them is what tells the two apart.
_CONTEXT_SEARCH = StatementInventory(
    "_TopicsOnlyCursor",
    fates_write="insert into app.context_candidate_fates",
    fates_read="from app.context_candidate_fates",
    topic_search=("from app.context_topics", "order by"),
    topic_hydrate=("from app.context_topics", "id = any("),
    procedure_search="from app.procedures",
    schema_context="from app.schema_context",
    graph="from app.context_graph",
)


class _TopicsOnlyCursor:
    """A corpus that holds topics and nothing else -- and SAYS which is which."""

    def __init__(self, topics: list[tuple]) -> None:
        self._topics = topics
        self._rows: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql: str, params=None) -> None:
        statement = _CONTEXT_SEARCH.match(sql)
        if statement == "topic_search":
            self._rows = list(self._topics)
        elif statement == "topic_hydrate":
            # The ids the caller asks to hydrate, resolved against the corpus --
            # answering `[]` here made a hydration failure look like an empty
            # corpus, which is the same lie one level down.
            wanted = {
                value
                for item in (params or ())
                for value in (item if isinstance(item, (list, tuple, set)) else [item])
                if isinstance(value, str)
            }
            self._rows = [topic for topic in self._topics if topic[0] in wanted]
        else:
            # Genuinely empty: this corpus declares no procedure, no schema doc,
            # no graph edge, and keeps no candidate fate.
            self._rows = []

    def fetchall(self):
        return list(self._rows)


class _TopicsOnlyConn:
    def __init__(self, topics: list[tuple]) -> None:
        self._topics = topics

    def cursor(self):
        return _TopicsOnlyCursor(self._topics)


def _corpus(n: int = 3) -> _TopicsOnlyConn:
    return _TopicsOnlyConn(
        [(f"ctx_{i:02d}", "proj_EXAMPLE", f"pacing {i}", "body") for i in range(n)]
    )


def test_the_corpus_fake_refuses_a_statement_the_walk_never_declared() -> None:
    """AI-317: an empty answer must mean an empty corpus, never a moved query."""
    cursor = _TopicsOnlyCursor([])
    with pytest.raises(UnknownStatement) as raised:
        cursor.execute("SELECT id FROM app.context_entities WHERE project_id = %s")
    assert "app.context_entities" in str(raised.value)
    assert "procedure_search" in str(raised.value)


def test_search_context_walk_crosses_at_the_crossing() -> None:
    conn = _corpus(3)
    walk, client = _armed(
        lambda: context_search.search_context_walk(
            conn, query="pacing", project_id="proj_EXAMPLE", limit=2
        )
    )
    assert len(walk.ranked) == 3
    assert [p["level"] for p in client.payloads] == [LEVEL_CONTEXT]
    detail = client.payloads[0]["detail"]
    assert detail["reached_count"] == 3
    assert detail["rejected_count"] == 1
    assert detail["candidate_reasons"][-1] == candidate_fate.REASON_BELOW_CUTOFF


def test_the_result_is_identical_armed_or_silent() -> None:
    """NFR2: an uninstrumented host must be indistinguishable from today."""
    silent = context_search.search_context(
        _corpus(3), query="pacing", project_id="proj_EXAMPLE", limit=2
    )
    armed, client = _armed(
        lambda: context_search.search_context(
            _corpus(3), query="pacing", project_id="proj_EXAMPLE", limit=2
        )
    )
    assert [h.as_dict() for h in armed] == [h.as_dict() for h in silent]
    assert client.payloads, "the armed run must still have emitted"


# ---------------------------------------------------------------------------
# T4 / AC6 -- the reason is cited data, on the far side of ONE AD-9 frame
# ---------------------------------------------------------------------------


def test_the_frame_is_imported_never_retyped() -> None:
    """The import IS the test (Story 53.5's discipline, same failure mode)."""
    source = inspect.getsource(candidate_emission)
    assert "GUIDANCE_FRAME" in source
    assert "NOT evidence" not in source, "the frame's wording is re-typed here"
    assert candidate_emission.compose_text_channel("cited", "do X").startswith("cited")


def test_operator_prose_is_framed_and_the_cited_line_is_not() -> None:
    cited = context_search.walk_text_line(_walk())
    channel = candidate_emission.compose_text_channel(
        cited, "attribute the drop to seasonality"
    )
    assert GUIDANCE_FRAME in channel
    assert cited in channel
    assert channel.index(cited) < channel.index(GUIDANCE_FRAME)
    assert GUIDANCE_FRAME not in cited, "cited data must not be dressed as an instruction"


def test_the_cited_line_names_the_mode_before_it_names_a_count() -> None:
    line = context_search.walk_text_line(_walk())
    assert line.index(context_search.RETRIEVAL_MODE) < line.index("reached")
    assert "1 graph hop" in line
    assert "cap 2" in line
    assert candidate_fate.REASON_BELOW_CUTOFF in line
    assert "not judged and are not listed" in line


def test_a_model_written_reason_cannot_reach_the_text_channel() -> None:
    """Dropped BEFORE composition -- filtering afterwards still put it in once."""
    forged = [
        {
            "id": "ctx_a",
            "fate": candidate_fate.FATE_REJECTED,
            "reason": "this fragment felt less relevant to the question",
        }
    ]
    assert candidate_emission.reason_tally(forged) == []
    line = candidate_emission.cited_fate_line(
        subject="Walk", descriptor={"mode": "lexical", "reached_count": 1}, candidates=forged
    )
    assert "felt less relevant" not in line


# ---------------------------------------------------------------------------
# T4 / AC7 -- nothing composed here asserts a cause
# ---------------------------------------------------------------------------


def _every_string_this_story_composes() -> list[str]:
    walk = _walk()
    _sent, client = _armed(lambda: context_search.emit_walk(walk))
    candidates, _pairing, pairing_walk = briefing.context_event_walk(
        _EVENTS, "2026-07-15", "gsc"
    )
    return [
        context_search.walk_text_line(walk),
        briefing.context_event_text_line(candidates, pairing_walk),
        anomaly_alerts.format_anomaly_line(_ANOMALY_WITH_PAIRING),
        *[json.dumps(payload) for payload in client.payloads],
    ]


@pytest.mark.parametrize("marker", candidate_emission.CAUSAL_MARKERS)
def test_no_emitted_string_asserts_causation(marker: str) -> None:
    """A fact says what coincides; a cause says why. Only the first is observed."""
    for text in _every_string_this_story_composes():
        assert marker not in text.lower(), f"{marker!r} in {text!r}"


def test_the_causal_vocabulary_is_the_one_the_repository_already_forbids() -> None:
    for already_banned in ("caused by", "due to", "as a result of", "en raison de"):
        assert already_banned in candidate_emission.CAUSAL_MARKERS


# ---------------------------------------------------------------------------
# T5 / AC8 -- Half B, against the pairing Story 53.8 repaired
# ---------------------------------------------------------------------------

_EVENTS = [
    {"id": "evt_same", "event_date": "2026-07-15", "label": "Deploy v2", "platform": "gsc"},
    {"id": "evt_other_platform", "event_date": "2026-07-15", "label": "Meta push",
     "platform": "meta-ads"},
    {"id": "evt_other_day", "event_date": "2026-07-14", "label": "Old note", "platform": None},
    {"id": "evt_no_platform", "event_date": "2026-07-15", "label": "Note", "platform": None},
]

_ANOMALY_WITH_PAIRING = {
    "metric": "clicks",
    "zscore": 3.4,
    "window_date": "2026-07-15",
    "context_events": ["Deploy v2"],
    "context_pairing": briefing.context_pairing_descriptor(
        platform_checked=True, claim_date="2026-07-15"
    ),
}


def test_story_53_8_closed_the_pairing_this_half_explains() -> None:
    """AC8's precondition, verified in the CODE and not inherited from a note.

    Half B is delivered only because the selection it describes is the repaired
    one: the claim's OWN date, exactly, and a declared platform that contradicts
    the claim's connector disqualifies the event. If either regressed, this half
    would be an exact explanation of a wrong choice and must not ship.
    """
    near, _ = briefing._find_context_event(_EVENTS, "2026-07-16", "gsc")
    assert near is None, "a D-1 event must not attach: proximity is not a relationship"
    on_day, pairing = briefing._find_context_event(_EVENTS, "2026-07-15", "gsc")
    assert on_day["id"] == "evt_same"
    assert briefing.CONTEXT_BASIS_PLATFORM in pairing["basis"]
    foreign, _ = briefing._find_context_event(
        [_EVENTS[1]], "2026-07-15", "google-search-console"
    )
    assert foreign is None, "a declared foreign platform must disqualify"


def test_the_pairing_reports_every_branch_it_judged() -> None:
    candidates, _pairing, walk = briefing.context_event_walk(_EVENTS, "2026-07-15", "gsc")
    by_id = {c["id"]: c for c in candidates}
    assert by_id["evt_same"]["fate"] == candidate_fate.FATE_SELECTED
    assert by_id["evt_same"]["reason"] is None
    assert by_id["evt_no_platform"]["reason"] == candidate_fate.REASON_BELOW_CUTOFF
    assert by_id["evt_other_platform"]["reason"] == candidate_fate.REASON_CONNECTOR_MISMATCH
    assert by_id["evt_other_day"]["reason"] == candidate_fate.REASON_DATE_MISMATCH
    assert walk["reached_count"] == 4
    assert walk["selected_count"] == 1
    assert walk["not_reached_enumerated"] is False
    assert walk["mode"] == briefing.CONTEXT_PAIRING_MODE


def test_metric_is_unscoped_while_no_event_names_one() -> None:
    """The events here carry no metric, so nothing about metric was compared.

    This was ``test_metric_mismatch_is_declared_and_produced_by_nobody`` until
    2026-08-30: migration 322 gave ``app.context_events`` a nullable ``metric``,
    so the reason became producible. What has NOT changed, and is what this test
    now holds, is that an event naming no metric is never rejected for one -- it
    is about every metric, and the payload says the dimension went unchecked.
    """
    assert candidate_fate.REASON_METRIC_MISMATCH in candidate_fate.CONTEXT_EVENT_REASONS
    candidates, _pairing, walk = briefing.context_event_walk(
        _EVENTS, "2026-07-15", "gsc", "clicks"
    )
    reasons = {c["reason"] for c in candidates if c["reason"]}
    assert reasons <= set(briefing.CONTEXT_EVENT_PRODUCIBLE_REASONS)
    assert candidate_fate.REASON_METRIC_MISMATCH not in reasons
    # ... and the payload NAMES the dimension it could not check.
    assert briefing.CONTEXT_DIM_METRIC in walk["unscoped_dimensions"]
    assert briefing.CONTEXT_BASIS_METRIC not in walk["basis"]


def test_an_event_about_another_metric_is_rejected_by_name() -> None:
    """Migration 322 -- the reason exists because a column now supports it."""
    events = [
        *_EVENTS,
        {"id": "evt_cost", "event_date": "2026-07-15", "label": "Bid raised",
         "platform": "gsc", "metric": "cost"},
    ]
    candidates, pairing, walk = briefing.context_event_walk(
        events, "2026-07-15", "gsc", "clicks"
    )
    by_id = {c["id"]: c for c in candidates}
    assert by_id["evt_cost"]["fate"] == candidate_fate.FATE_REJECTED
    assert by_id["evt_cost"]["reason"] == candidate_fate.REASON_METRIC_MISMATCH
    # The kept event named no metric, so the dimension is still reported
    # unchecked: admissible is not the same as verified.
    assert briefing.CONTEXT_DIM_METRIC in pairing["unscoped_dimensions"]
    assert walk["reached_count"] == len(events)


def test_an_event_about_this_metric_takes_the_dimension_out_of_unscoped() -> None:
    events = [
        {"id": "evt_clicks", "event_date": "2026-07-15", "label": "Deploy v2",
         "platform": "gsc", "metric": "clicks"},
        {"id": "evt_wide", "event_date": "2026-07-15", "label": "Outage", "platform": "gsc"},
    ]
    candidates, pairing, walk = briefing.context_event_walk(
        events, "2026-07-15", "gsc", "clicks"
    )
    by_id = {c["id"]: c for c in candidates}
    # The event whose dimensions were actually COMPARED wins the one-event cut.
    assert by_id["evt_clicks"]["fate"] == candidate_fate.FATE_SELECTED
    assert by_id["evt_clicks"]["metric_checked"] is True
    assert by_id["evt_wide"]["reason"] == candidate_fate.REASON_BELOW_CUTOFF
    assert briefing.CONTEXT_BASIS_METRIC in pairing["basis"]
    assert briefing.CONTEXT_DIM_METRIC not in pairing["unscoped_dimensions"]
    assert briefing.CONTEXT_BASIS_METRIC in walk["basis"]


def test_a_claim_with_no_date_judges_nothing_rather_than_rejecting_everything() -> None:
    candidates, pairing, walk = briefing.context_event_walk(_EVENTS, None, "gsc")
    assert (candidates, pairing, walk) == ([], None, None)


def test_the_survivor_is_read_off_the_same_judgement_not_a_second_scan() -> None:
    source = inspect.getsource(briefing._find_context_event)
    assert "context_event_walk" in source
    assert "fromisoformat" not in source, "a second date scan is a second result set"


def test_the_pairing_crosses_on_the_same_seam() -> None:
    candidates, _pairing, walk = briefing.context_event_walk(_EVENTS, "2026-07-15", "gsc")
    sent, client = _armed(lambda: briefing.emit_context_event_walk(candidates, walk))
    assert sent is True
    payload = client.payloads[0]
    assert payload["level"] == LEVEL_CONTEXT
    # The column's word, mapped at the writing boundary (AI-376): the kind is `context_event`, the owner type `context-event`.
    assert payload["owner_object_type"] == candidate_emission.owner_object_type_for(candidate_emission.OBJECT_TYPE_CONTEXT_EVENT) == "context-event"
    assert payload["owner_object_id"] == "evt_same"
    detail = payload["detail"]
    assert payload["tool_name"] == candidate_emission.TOOL_BRIEFING_CONTEXT_EVENT
    assert detail["branch_schema_version"] == candidate_emission.BRANCH_SCHEMA_VERSION
    assert detail["branch_producer"] == candidate_emission.PRODUCER_BRIEFING_CONTEXT_EVENT
    assert detail["candidate_ids"] == [c["id"] for c in candidates]
    assert detail["candidate_fates"][0] == candidate_fate.FATE_SELECTED
    # A pairing does not score and walks no graph: `None` means NOT APPLICABLE,
    # and a zero there would invent a measurement nobody took.
    assert detail["candidate_scores"] == [None] * len(candidates)
    assert detail["graph_hop_depth"] == 0


def test_the_briefing_payload_carries_the_branches_and_what_the_pairing_is() -> None:
    firings = [
        {
            "type": "anomaly",
            "metric": "clicks",
            "connector": "gsc",
            "observed_value": 700.0,
            "expected_value": 1000.0,
            "window_date": "2026-07-15",
            "firing_id": "fire_1",
            "pull_ids": [],
        }
    ]
    result = briefing.build_briefing(
        project_id="proj_EXAMPLE",
        briefing_date="2026-07-16",
        alert_firings=firings,
        rollup={},
        context_events=_EVENTS,
        nightly_run_id="run_1",
    )
    anomaly = next(i for i in result["insights"] if i["type"] == "anomaly")
    assert anomaly["context_event_id"] == "evt_same"
    ids = [c["id"] for c in anomaly["context_event_candidates"]]
    assert ids == ["evt_same", "evt_no_platform", "evt_other_platform", "evt_other_day"]
    assert anomaly["context_event_retrieval"]["mode"] == briefing.CONTEXT_PAIRING_MODE
    # The raw event never rides the payload: a path is trace evidence, not a
    # second store of the content it points at (`context-hub.md:40-50`).
    assert all(not key.startswith("_") for key in anomaly["context_event_candidates"][0])


def test_the_other_insight_types_keep_the_same_shape() -> None:
    firings = [
        {
            "code": "threshold_breach",
            "metric": "clicks",
            "connector": "gsc",
            "observed_value": 700.0,
            "threshold": 1000.0,
            "window_date": "2026-07-15",
            "firing_id": "fire_2",
            "pull_ids": [],
        }
    ]
    result = briefing.build_briefing(
        project_id="proj_EXAMPLE",
        briefing_date="2026-07-16",
        alert_firings=firings,
        rollup={},
        context_events=_EVENTS,
        nightly_run_id="run_1",
    )
    for insight in result["insights"]:
        assert "context_event_candidates" in insight
        assert "context_event_retrieval" in insight


# ---------------------------------------------------------------------------
# T5 / AC6 -- the other half of the class says the same thing, in the same words
# ---------------------------------------------------------------------------


def test_the_anomaly_line_carries_the_basis_as_cited_data() -> None:
    line = anomaly_alerts.format_anomaly_line(_ANOMALY_WITH_PAIRING)
    assert '"Deploy v2"' in line
    assert briefing.CONTEXT_BASIS_EXACT_DATE in line
    assert briefing.CONTEXT_BASIS_PLATFORM in line
    assert f"Not scoped: {briefing.CONTEXT_DIM_METRIC}" in line
    assert briefing.CONTEXT_PAIRING_MODE in line


def test_the_evaluator_side_never_claims_rejections_it_never_fetched() -> None:
    """Its filter runs INSIDE the SQL: a non-qualifying event is never seen.

    The same refusal Half A applies to `out_of_scope` -- AD-5 filters in the
    query, so another Project's row is never a candidate and reporting it as
    rejected would invent a judgement AND leak its existence.
    """
    descriptor = anomaly_alerts.pairing_walk_descriptor(
        _ANOMALY_WITH_PAIRING["context_pairing"], 1
    )
    assert descriptor["selected_count"] == 1
    assert "rejected_count" not in descriptor
    assert "reached_count" not in descriptor
    line = anomaly_alerts.format_anomaly_line(_ANOMALY_WITH_PAIRING)
    assert "rejected" not in line


def test_an_anomaly_without_a_pairing_is_unchanged() -> None:
    line = anomaly_alerts.format_anomaly_line(
        {"metric": "clicks", "zscore": -3.5, "window_date": "2026-07-15",
         "context_events": []}
    )
    assert line == "⚠️ Anomalie : clicks (z=-3.5) le 2026-07-15 -- Contexte manquant."


def test_the_two_halves_use_one_vocabulary_for_one_pairing() -> None:
    """Repairing/describing one side only is how this class survives."""
    _cands, _pairing, walk = briefing.context_event_walk(_EVENTS, "2026-07-15", "gsc")
    evaluator = anomaly_alerts.pairing_walk_descriptor(
        _ANOMALY_WITH_PAIRING["context_pairing"], 1
    )
    assert walk["basis"] == evaluator["basis"]
    assert walk["mode"] == evaluator["mode"]
    assert walk["not_reached_enumerated"] == evaluator["not_reached_enumerated"] is False


# ---------------------------------------------------------------------------
# Nothing new is persisted
# ---------------------------------------------------------------------------


def test_the_crossing_uses_a_step_kind_that_already_exists() -> None:
    from core.ai_paths import STEP_KINDS

    assert candidate_emission.STEP_KIND_KNOWLEDGE_READ in STEP_KINDS
    assert candidate_emission.OWNER_WORKSPACE_CONTEXT_HUB in __import__(
        "core.ai_paths", fromlist=["OWNER_WORKSPACES"]
    ).OWNER_WORKSPACES


def test_the_owner_object_type_written_is_the_columns_vocabulary(monkeypatch) -> None:
    """AI-376: a candidate kind with an underscore reaches the column as its hyphenated owner
    type; the kinds the column already accepts pass through unchanged."""
    from core import candidate_emission
    from core.ai_paths import OVERLAY_EVENT_OBJECT_TYPE

    assert candidate_emission.owner_object_type_for("context_event") == OVERLAY_EVENT_OBJECT_TYPE == "context-event"
    assert candidate_emission.owner_object_type_for("schema_doc") == "schema-doc"
    assert candidate_emission.owner_object_type_for("topic") == "topic"
    assert candidate_emission.owner_object_type_for("procedure") == "procedure"
    assert candidate_emission.owner_object_type_for(None) is None
    import re

    column = re.compile(r"^[a-z][a-z0-9-]{2,60}$")
    for kind in ("topic", "procedure", "schema_doc", "context_event"):
        assert column.match(candidate_emission.owner_object_type_for(kind)), kind

    written: list[dict] = []
    monkeypatch.setattr(candidate_emission, "observation_is_recorded", lambda: True)
    monkeypatch.setattr(candidate_emission, "emit_step_sync", lambda **kwargs: written.append(kwargs) or True)
    monkeypatch.setattr(candidate_emission, "candidate_detail", lambda candidates, descriptor, producer: {"producer": producer})
    assert candidate_emission.emit_candidates(
        candidates=[], descriptor={}, producer=candidate_emission.PRODUCER_BRIEFING_CONTEXT_EVENT,
        tool_name=candidate_emission.TOOL_BRIEFING_CONTEXT_EVENT,
        owner_object_type=candidate_emission.OBJECT_TYPE_CONTEXT_EVENT, owner_object_id="cev_1",
    )
    assert written[0]["owner_object_type"] == "context-event" and written[0]["owner_object_id"] == "cev_1"
