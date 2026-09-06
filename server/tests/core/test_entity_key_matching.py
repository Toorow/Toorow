"""Every verdict a bound key occurrence can get, proven off base (Story 68.3).

WHAT THIS FILE PINS. The taxonomy and the arithmetic -- the two halves that
must never drift, and that a database cannot make more true:

  * `resolved` / `unmatched` / `ambiguous`, the `resolve_governed_node`
    vocabulary, and nothing else. Above all: the refusals. Two nodes on one
    value is a homonym, not a coin toss; a similarity win is `close`, and a
    close match is not an identity;
  * the candidates SURVIVE the refusals -- what was ranked and rejected is
    the evidence a person repairs from;
  * `same_outcome`, which decides whether a replayed pull writes a second row
    (it must not) or supersedes the first (it must, when the answer changed);
  * `compose_coverage`, where "N of M" is computed -- and `unavailable`, which
    is never allowed to render as a coverage of zero.

The storage half -- real rows, supersession, the coverage query, isolation --
lives in `tests/integration/test_entity_key_matching_pg.py`.
"""

from __future__ import annotations

from core import entity_key_matching as ekm
from core.tracked_entities import (
    REASON_AMBIGUOUS,
    REASON_BELOW_THRESHOLD,
    REASON_NEGATIVE_ALIAS,
    REASON_NO_CANDIDATE,
    REASON_RANKED,
    RELATION_CLOSE,
    RELATION_EXACT,
    RELATION_NEGATIVE,
    RELATION_NONE,
    Candidate,
    Proposal,
)


def _candidate(node_id, *, relation=RELATION_EXACT, score=1.0, method="alias"):
    return Candidate(
        node_id=node_id,
        matched_surface=node_id.replace("mdnode_", ""),
        relation=relation,
        score=score,
        method=method,
    )


# ---------------------------------------------------------------------------
# The direct alias lookup: COUNT(*)=1, never LIMIT 1.
# ---------------------------------------------------------------------------


def test_one_live_alias_resolves_and_names_its_node():
    verdict = ekm.verdict_from_exact_hits([("mdnode_a", "V-1")])
    assert verdict.verdict == ekm.VERDICT_RESOLVED
    assert verdict.node_id == "mdnode_a"
    assert verdict.reason_code == ekm.REASON_EXACT_LOOKUP
    assert verdict.confidence == 1.0


def test_two_aliases_on_one_node_are_still_one_answer():
    # The same node claiming a value twice (two namespaces, two surfaces) is
    # not an ambiguity: there is one identity to resolve to.
    verdict = ekm.verdict_from_exact_hits([("mdnode_a", "V-1"), ("mdnode_a", "v 1")])
    assert verdict.verdict == ekm.VERDICT_RESOLVED
    assert verdict.node_id == "mdnode_a"
    assert len(verdict.candidates) == 1


def test_two_nodes_on_one_value_are_ambiguous_and_name_both():
    verdict = ekm.verdict_from_exact_hits([("mdnode_a", "V-1"), ("mdnode_b", "V-1")])
    assert verdict.verdict == ekm.VERDICT_AMBIGUOUS
    # A machine never picks: the refusal names what a person must choose from.
    assert verdict.node_id is None
    assert {c["node_id"] for c in verdict.candidates} == {"mdnode_a", "mdnode_b"}


def test_no_alias_at_all_is_unmatched_and_says_why():
    verdict = ekm.verdict_from_exact_hits([])
    assert verdict.verdict == ekm.VERDICT_UNMATCHED
    assert verdict.reason_code == ekm.REASON_NO_CANDIDATE
    assert verdict.candidates == ()


# ---------------------------------------------------------------------------
# The ranking resolver: only an EXACT ranked win is an identity.
# ---------------------------------------------------------------------------


def test_a_ranked_exact_proposal_resolves():
    proposal = Proposal(
        normalized_value="v-1",
        relation=RELATION_EXACT,
        reason_code=REASON_RANKED,
        candidates=(_candidate("mdnode_a"),),
        confidence=0.99,
    )
    verdict = ekm.verdict_from_proposal(proposal)
    assert verdict.verdict == ekm.VERDICT_RESOLVED
    assert verdict.node_id == "mdnode_a"


def test_a_close_similarity_win_is_never_promoted_to_an_identity():
    proposal = Proposal(
        normalized_value="v-1",
        relation=RELATION_CLOSE,
        reason_code=REASON_RANKED,
        candidates=(_candidate("mdnode_a", relation=RELATION_CLOSE, score=0.94),),
        confidence=0.94,
    )
    verdict = ekm.verdict_from_proposal(proposal)
    # SKOS is explicit that a close match is not transitive. Promoting it here
    # is exactly the silent approximate join this story refuses.
    assert verdict.verdict == ekm.VERDICT_UNMATCHED
    assert verdict.node_id is None
    # ...and what was ranked survives, because it is the repair evidence.
    assert [c["node_id"] for c in verdict.candidates] == ["mdnode_a"]


def test_an_ambiguous_proposal_keeps_every_candidate_it_refused_to_choose():
    proposal = Proposal(
        normalized_value="v-1",
        relation=RELATION_NONE,
        reason_code=REASON_AMBIGUOUS,
        candidates=(_candidate("mdnode_a", score=0.91), _candidate("mdnode_b", score=0.90)),
        confidence=0.91,
    )
    verdict = ekm.verdict_from_proposal(proposal)
    assert verdict.verdict == ekm.VERDICT_AMBIGUOUS
    assert verdict.node_id is None
    assert len(verdict.candidates) == 2


def test_below_threshold_and_negative_alias_are_unmatched_with_their_reason():
    for reason, relation in (
        (REASON_BELOW_THRESHOLD, RELATION_NONE),
        (REASON_NEGATIVE_ALIAS, RELATION_NEGATIVE),
        (REASON_NO_CANDIDATE, RELATION_NONE),
    ):
        verdict = ekm.verdict_from_proposal(
            Proposal(normalized_value="v-1", relation=relation, reason_code=reason)
        )
        assert verdict.verdict == ekm.VERDICT_UNMATCHED
        assert verdict.reason_code == reason


def test_the_taxonomy_is_closed():
    # A fourth state would be an invention; the database CHECK says the same.
    assert set(ekm.VERDICTS) == {"resolved", "unmatched", "ambiguous"}


# ---------------------------------------------------------------------------
# Replay vs supersession.
# ---------------------------------------------------------------------------


def _resolved_verdict():
    return ekm.verdict_from_exact_hits([("mdnode_a", "V-1")])


def _row_from(verdict):
    return {
        "verdict": verdict.verdict,
        "node_id": verdict.node_id,
        "relation": verdict.relation,
        "reason_code": verdict.reason_code,
        "candidates": [dict(c) for c in verdict.candidates],
    }


def test_the_same_answer_twice_is_a_replay_not_a_new_row():
    verdict = _resolved_verdict()
    assert ekm.same_outcome(_row_from(verdict), verdict) is True


def test_a_confidence_drift_alone_is_still_the_same_outcome():
    verdict = _resolved_verdict()
    row = _row_from(verdict)
    row["confidence"] = 0.42
    # A corpus edit that moves a score without moving the answer is not a new
    # fact about this occurrence -- superseding on it would churn the history
    # every time an unrelated alias is recorded.
    assert ekm.same_outcome(row, verdict) is True


def test_a_changed_verdict_supersedes():
    row = _row_from(ekm.verdict_from_exact_hits([]))
    assert ekm.same_outcome(row, _resolved_verdict()) is False


def test_resolving_to_a_different_node_supersedes():
    row = _row_from(ekm.verdict_from_exact_hits([("mdnode_b", "V-1")]))
    assert ekm.same_outcome(row, _resolved_verdict()) is False


def test_the_same_verdict_with_new_candidates_supersedes():
    verdict = ekm.verdict_from_exact_hits([("mdnode_a", "V-1"), ("mdnode_b", "V-1")])
    row = _row_from(verdict)
    row["candidates"] = row["candidates"][:1]
    # The answer is still "ambiguous", but WHICH identities it is ambiguous
    # between changed -- and that is what a person acts on.
    assert ekm.same_outcome(row, verdict) is False


# ---------------------------------------------------------------------------
# Coverage: N of M, and the unavailable that is never a zero.
# ---------------------------------------------------------------------------


def test_coverage_counts_resolved_over_every_occurrence():
    report = ekm.compose_coverage(
        [
            ("ds_1", "video", "resolved", 7),
            ("ds_1", "video", "unmatched", 2),
            ("ds_1", "video", "ambiguous", 1),
        ]
    )
    assert report["state"] == "available"
    assert report["coverage"]["bound"] == 7
    assert report["coverage"]["eligible"] == 10
    assert report["coverage"]["by_state"] == {"resolved": 7, "unmatched": 2, "ambiguous": 1}


def test_coverage_is_reported_per_datastream_and_per_entity_type():
    report = ekm.compose_coverage(
        [
            ("ds_2", "store", "resolved", 1),
            ("ds_1", "video", "resolved", 3),
            ("ds_1", "video", "unmatched", 1),
        ]
    )
    assert [(row["datastream_id"], row["object_kind"]) for row in report["rows"]] == [
        ("ds_1", "video"),
        ("ds_2", "store"),
    ]
    assert report["rows"][0] == {
        "datastream_id": "ds_1",
        "object_kind": "video",
        "bound": 3,
        "eligible": 4,
        "by_state": {"resolved": 3, "unmatched": 1},
    }


def test_no_occurrence_at_all_is_empty_never_unavailable():
    report = ekm.compose_coverage([])
    assert report["state"] == "empty"
    assert report["coverage"]["eligible"] == 0
    assert report["unavailable_reason"] is None


def test_the_discovery_surface_actually_finds_this_reader():
    from core import object_kind_registry as registry

    # Story 68.7 probes for this module rather than importing it, so that its
    # own surface could ship before 68.3. The probe is exactly the kind of
    # guard that keeps answering "unavailable" forever once the thing it looks
    # for lands -- so the wiring itself is pinned here.
    assert registry._load_matching_coverage_reader() is ekm.matching_coverage


def test_an_unreadable_store_reports_none_and_names_the_refusal():
    report = ekm.unavailable_coverage("connection reset")
    assert report["state"] == "unavailable"
    # None, not 0: a number here would be read as a measurement.
    assert report["coverage"]["bound"] is None
    assert report["coverage"]["eligible"] is None
    assert report["unavailable_reason"]["code"] == "entity_key_verdicts_unreadable"
    assert "not a coverage of zero" in report["unavailable_reason"]["message"]
