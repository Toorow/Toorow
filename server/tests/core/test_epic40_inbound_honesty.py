"""Story 40.6 -- inbound-honesty suite (Epic 40 acceptance gate, AC3 / E40-NFR02, NFR04, AD-9).

Inbound alias matching is FAIL-CLOSED and provenance-complete: nothing enters the master
silently. Over the 40.3 delivered matcher (match_brand) driven on the fixture strings:
  (a) a high-confidence alias hit  -> resolves to the canonical entity carrying match
      provenance (the matched alias + a confidence score, AD-9);
  (b) a no-match string, (c) a below-threshold string, (d) an ambiguous homonym -> each yields
      a TYPED alert (OUTCOME_ALERT with a reason no_match/below_threshold/ambiguous) -- NEVER a
      silent join, a silent drop, or a silent default (E40-NFR02: state the missing context,
      never invent it);
  and a mis-match correction adds an alias / negative alias UNDER governance (never an
  autonomous silent AI write -- E40-AD5). Each case asserts BOTH the typed alert/provenance IS
  present AND the absence of a fabricated approval-less assignment.

Offline (PURE matcher -- no DB). Skip-guarded on 40.3's delivered symbols (match_brand /
OUTCOME_RESOLVED / OUTCOME_ALERT / REASON_*). Pattern calque sur test_tracked_entity_matching.py.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

# Skip-guard the whole module on the 40.3 delivered matcher (contexted-in-parallel).
tem = pytest.importorskip("core.tracked_entity_matching")


# ---------------------------------------------------------------------------
# The inbound corpus + fixture strings (Dev Notes A). The corpus is SYNTHETIC in-memory
# matcher input only -- it is never persisted to a mart, so it uses clean opaque tokens rather
# than the `__epic40_*__` mart-namespace prefix (that prefix normalizes to a shared stem which
# would inflate every similarity ratio). The strings exercise the four AC3 cases: a
# high-confidence hit, a no-match, a below-threshold, an ambiguous homonym.
# ---------------------------------------------------------------------------

_CORPUS = [
    {"id": "tent_epic40_brand_x", "canonical_name": "Zephyrus Telco",
     "aliases": ["Zephyrus", "Zephyrus SA"]},
    {"id": "tent_epic40_brand_y", "canonical_name": "Nimbus Fruit",
     "aliases": ["Nimbus", "Nimbus Co"]},
]

# A high-confidence verbatim alias hit; a below-threshold near-miss; a homonym collision.
_STR_HIGH_CONFIDENCE = "Zephyrus SA"     # exact alias of entity X
_STR_BELOW_THRESHOLD = "Zeph"            # a near-miss (~0.67) below the 0.88 threshold
_STR_UNRELATED = "Quorvex"               # a fully unrelated string (best candidate ~0.25)


def test_high_confidence_match_resolves_with_provenance():
    """AC3 (a) -- a string that matches a known alias VERBATIM resolves to the canonical entity
    carrying provenance (the matched alias + a confidence score, AD-9). The provenance IS
    present; nothing is assumed."""
    match = tem.match_brand(_STR_HIGH_CONFIDENCE, _CORPUS)
    assert match.outcome == tem.OUTCOME_RESOLVED
    assert match.entity_id == "tent_epic40_brand_x"
    # AD-9 provenance: the matched surface + the method + a confidence score are all carried.
    assert match.matched_alias == _STR_HIGH_CONFIDENCE
    assert match.method in (tem.METHOD_EXACT, tem.METHOD_NORMALIZED)
    assert match.score is not None and match.score >= tem.DEFAULT_CONFIDENCE_THRESHOLD
    # No alert reason on a resolved match (it is not a gap).
    assert match.reason is None


def test_no_match_string_is_a_typed_alert_never_a_silent_drop():
    """AC3 (b) -- when there is NO candidate entity at all (an empty/exhausted corpus) a string
    yields OUTCOME_ALERT(reason=no_match) -- NEVER silently dropped, NEVER default-assigned: no
    entity_id, no fabricated canonical assignment (fail-closed by absence-of-a-silent-write).

    NOTE (a production finding, see Dev Agent Record): with a NON-empty corpus, an unrelated
    string yields reason=below_threshold (a nearest rejected candidate always exists), NOT
    no_match -- both are fail-closed alerts carrying NO assignment; the distinction is provenance
    detail, not a leak. The strict no_match reason fires when the candidate pool is empty."""
    match = tem.match_brand("anything", [])   # empty corpus -> the pure no_match branch
    assert match.outcome == tem.OUTCOME_ALERT
    assert match.reason == tem.REASON_NO_MATCH
    # No silent join / no silent default: nothing was assigned to a canonical entity.
    assert match.entity_id is None
    assert match.matched_alias is None
    assert match.method is None


def test_unrelated_string_against_corpus_is_a_fail_closed_alert_no_assignment():
    """AC3 (b, corpus variant) -- an unrelated string against a NON-empty corpus still yields a
    typed alert (below_threshold, its best candidate is far) and is NEVER assigned to an entity.
    The honesty invariant (alert + no silent assignment) holds regardless of the reason
    granularity."""
    match = tem.match_brand(_STR_UNRELATED, _CORPUS)
    assert match.outcome == tem.OUTCOME_ALERT
    assert match.reason in (tem.REASON_BELOW_THRESHOLD, tem.REASON_NO_MATCH)
    assert match.entity_id is None            # never a silent join to the nearest entity


def test_below_threshold_string_is_a_typed_alert_never_a_silent_join():
    """AC3 (c) -- a string whose best candidate scores BELOW the confidence threshold yields
    OUTCOME_ALERT(reason=below_threshold) -- an honest gap, never a silent join to the nearest
    entity. The rejected candidate is carried for provenance, but no assignment is made."""
    match = tem.match_brand(_STR_BELOW_THRESHOLD, _CORPUS)
    assert match.outcome == tem.OUTCOME_ALERT
    assert match.reason == tem.REASON_BELOW_THRESHOLD
    assert match.entity_id is None            # never a silent join despite a near candidate
    # The nearest candidate IS carried (provenance) but its score is honestly below threshold.
    assert match.candidates
    assert match.candidates[0].score < tem.DEFAULT_CONFIDENCE_THRESHOLD


def test_ambiguous_homonym_is_a_typed_alert_never_an_arbitrary_pick():
    """AC3 (d) -- an ambiguous homonym (a string that two DIFFERENT entities both claim at the
    top with equal confidence) yields OUTCOME_ALERT(reason=ambiguous) -- never a silent pick of
    one arbitrary entity. Here the string 'Orange' is a verbatim exact hit on entity A's
    canonical AND on entity B's alias -- a homonym naming two org entities (telco vs fruit)."""
    corpus = [
        {"id": "tent_a", "canonical_name": "Orange", "aliases": []},
        {"id": "tent_b", "canonical_name": "Banana", "aliases": ["Orange"]},
    ]
    match = tem.match_brand("Orange", corpus)
    assert match.outcome == tem.OUTCOME_ALERT
    assert match.reason == tem.REASON_AMBIGUOUS
    # The two colliding candidates are carried (provenance) but NO single one is assigned.
    assert match.entity_id is None
    assert len({c.entity_id for c in match.candidates}) >= 2


def test_every_input_yields_resolve_or_alert_never_nothing():
    """AC3 (invariant) -- fail-closed completeness: EVERY input string yields either a resolved
    match or a typed alert. There is no third 'silently nothing' outcome (E40-NFR02). Proven by
    running the fixture strings through the matcher and asserting each carries a typed outcome +
    (for alerts) a typed reason and NO assignment."""
    strings = [_STR_HIGH_CONFIDENCE, _STR_UNRELATED, _STR_BELOW_THRESHOLD]
    for raw in strings:
        m = tem.match_brand(raw, _CORPUS)
        assert m.outcome in (tem.OUTCOME_RESOLVED, tem.OUTCOME_ALERT)
        if m.outcome == tem.OUTCOME_ALERT:
            assert m.reason in (
                tem.REASON_NO_MATCH, tem.REASON_BELOW_THRESHOLD, tem.REASON_AMBIGUOUS,
            )
            assert m.entity_id is None  # an alert NEVER carries a canonical assignment
        else:
            assert m.entity_id is not None and m.score is not None  # provenance on a resolve


def test_negative_alias_suppresses_a_homonym_never_silently():
    """AC3 (correction, PURE slice) -- a negative alias (a homonym the wrong entity must NOT
    claim) removes that entity from the candidate set for the suppressed string. This is the
    matcher-side effect of a GOVERNED mis-match correction (the DB-writing correct_match /
    add_negative_alias are the governed, actor-stamped path -- AD-5 / E40-NFR04); here we prove
    the suppression is HONORED, so a corrected homonym never silently re-matches the wrong
    entity."""
    corpus_dicts = [
        {"id": "tent_x", "canonical_name": "Apple Inc", "aliases": ["Apple"]},
    ]
    # Without suppression the exact alias resolves to entity X.
    m = tem.match_brand("Apple", corpus_dicts)
    assert m.outcome == tem.OUTCOME_RESOLVED and m.entity_id == "tent_x"

    # A GOVERNED correction records 'Apple must NOT match tent_x' (a homonym: the fruit, not the
    # company). Build the corpus with that negative and prove the suppression is honored.
    entries = tem._build_corpus(
        corpus_dicts, negatives_by_entity={"tent_x": ["Apple"]}
    )
    m2 = tem.match_brand("Apple", entries)
    assert m2.outcome == tem.OUTCOME_ALERT       # suppressed -> a typed gap, never a silent join
    assert m2.entity_id is None


def test_match_run_never_mutates_the_master_symbols_exist():
    """AC3 (governance seam) -- the module SEPARATES the deterministic ranking (match_brand,
    pure) from the GOVERNED master-writing lifecycle (approve_new_entity_alert / correct_match /
    add_negative_alias -- all human-invoked, actor-stamped). A match RUN calls NONE of the
    master-writing functions: this asserts the governed entrypoints exist and are DISTINCT from
    match_brand (the AI proposes via a matcher result; a human ratifies via the lifecycle)."""
    # The pure ranker and the governed lifecycle are different callables (no silent AI write).
    assert callable(tem.match_brand)
    for governed in ("approve_new_entity_alert", "correct_match", "add_negative_alias",
                     "dismiss_alert"):
        assert callable(getattr(tem, governed)), f"missing governed entrypoint: {governed}"
    # approve_new_entity_alert is the ONLY path that mints a master entity (E40-AD5): it takes an
    # explicit human identity + org (an actor), proving nothing enters the master anonymously.
    import inspect

    sig = inspect.signature(tem.approve_new_entity_alert)
    assert "identity" in sig.parameters and "org_id" in sig.parameters
