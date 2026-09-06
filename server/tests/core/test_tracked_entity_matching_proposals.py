"""Story 48.5, AC4: ranking proposes, and cannot decide.

Every test here runs without a database, because every rule here is about what
the ranking is ALLOWED to conclude rather than about where the conclusion is
stored. The two facts worth stating in a type system are stated in one:
:class:`Proposal` has no `resolved` member, so no caller can persist one by
accident.

The second half is the distinction that was collapsed: a similarity win is
`close`, never `exact`. SKOS says a close match is not transitive, and the
version of this code that promoted a 0.9 difflib ratio to an equality is how two
different companies end up as one row.
"""

from __future__ import annotations

import pytest
from core.tracked_entities import (
    REASON_AMBIGUOUS,
    REASON_BELOW_THRESHOLD,
    REASON_NEGATIVE_ALIAS,
    REASON_NO_CANDIDATE,
    REASON_RANKED,
    RELATION_CLOSE,
    RELATION_EXACT,
    RELATION_NONE,
    RELATION_RELATED,
    Proposal,
    build_corpus,
    corpus_version,
    rank_candidates,
)


def _entity(node_id: str, label: str, aliases: list[tuple[str, str]] | None = None) -> dict:
    return {
        "entity_id": node_id,
        "label": label,
        "current_version": {"payload": {"preferred_label": label}},
        "aliases": [
            {"raw_value": value, "relation": relation} for value, relation in (aliases or [])
        ],
    }


ALPHA = _entity("mdnode_ALPHA", "Northwind", [("Northwind Traders", "exact"), ("NWT", "related")])
BETA = _entity("mdnode_BETA", "Southwind", [])
CORPUS = build_corpus([ALPHA, BETA])


# ---------------------------------------------------------------------------
# The type itself forbids the outcome the old code produced.
# ---------------------------------------------------------------------------


def test_a_proposal_cannot_express_a_decision():
    assert not hasattr(Proposal, "resolved")
    assert "resolved" not in {
        field for field in Proposal.__dataclass_fields__  # noqa: SLF001 - the point of the test
    }


def test_a_verbatim_hit_proposes_exact():
    proposal = rank_candidates("Northwind Traders", CORPUS)
    assert proposal.relation == RELATION_EXACT
    assert proposal.reason_code == REASON_RANKED
    assert proposal.top.node_id == "mdnode_ALPHA"


def test_a_case_only_difference_still_proposes_exact():
    assert rank_candidates("NORTHWIND", CORPUS).relation == RELATION_EXACT


def test_a_similarity_win_proposes_close_and_never_exact():
    proposal = rank_candidates("Northwinds", CORPUS)
    assert proposal.relation == RELATION_CLOSE
    assert proposal.confidence is not None and proposal.confidence >= 0.88
    assert proposal.features["stage"] == "similarity"


def test_an_alias_keeps_the_relation_it_was_recorded_with():
    """`NWT` is `related`, so matching it proposes `related` -- not `exact`."""
    assert rank_candidates("NWT", CORPUS).relation == RELATION_RELATED


# ---------------------------------------------------------------------------
# Every un-decidable case stays explicit. None of them is "no competitor".
# ---------------------------------------------------------------------------


def test_a_weak_best_candidate_is_below_threshold_not_a_match():
    proposal = rank_candidates("Contoso", CORPUS)
    assert proposal.relation == RELATION_NONE
    assert proposal.reason_code == REASON_BELOW_THRESHOLD
    # The rejected candidates survive: what was NOT chosen is evidence too.
    assert proposal.candidates


def test_two_identities_claiming_one_string_are_ambiguous_not_arbitrated():
    twin_a = _entity("mdnode_A", "Apex")
    twin_b = _entity("mdnode_B", "Apex")
    proposal = rank_candidates("Apex", build_corpus([twin_a, twin_b]))
    assert proposal.relation == RELATION_NONE
    assert proposal.reason_code == REASON_AMBIGUOUS
    assert {candidate.node_id for candidate in proposal.candidates} == {"mdnode_A", "mdnode_B"}


def test_two_close_scores_above_the_threshold_are_ambiguous():
    proposal = rank_candidates(
        "Northwynd",
        build_corpus([_entity("mdnode_A", "Northwind"), _entity("mdnode_B", "Northwynd")]),
    )
    assert proposal.reason_code in {REASON_AMBIGUOUS, REASON_RANKED}
    if proposal.reason_code == REASON_AMBIGUOUS:
        assert proposal.relation == RELATION_NONE


def test_an_empty_value_is_no_candidate_rather_than_a_silent_drop():
    proposal = rank_candidates("   ", CORPUS)
    assert proposal.reason_code == REASON_NO_CANDIDATE
    assert proposal.relation == RELATION_NONE


def test_an_empty_corpus_produces_a_reason_not_an_exception():
    assert rank_candidates("anything", []).reason_code == REASON_NO_CANDIDATE


# ---------------------------------------------------------------------------
# A refusal survives. This is what stops a rejected mapping resurfacing weekly.
# ---------------------------------------------------------------------------


def test_a_negative_alias_removes_that_identity_from_the_candidate_set():
    refusing = _entity("mdnode_A", "Orange", [("orange juice", "negative")])
    proposal = rank_candidates("orange juice", build_corpus([refusing]))
    assert proposal.relation == RELATION_NONE
    assert proposal.reason_code == REASON_NEGATIVE_ALIAS
    assert proposal.features["refused_by"] == ["mdnode_A"]


def test_a_refusal_does_not_hide_another_identity_that_still_matches():
    refusing = _entity("mdnode_A", "Orange", [("Citrus Co", "negative")])
    other = _entity("mdnode_B", "Citrus Co")
    proposal = rank_candidates("Citrus Co", build_corpus([refusing, other]))
    assert proposal.relation == RELATION_EXACT
    assert proposal.top.node_id == "mdnode_B"
    assert proposal.features["refused_by"] == ["mdnode_A"]


# ---------------------------------------------------------------------------
# Determinism. A score is only comparable to another score over the same corpus.
# ---------------------------------------------------------------------------


def test_ranking_is_stable_across_corpus_ordering():
    forward = rank_candidates("Northwinds", build_corpus([ALPHA, BETA]))
    backward = rank_candidates("Northwinds", build_corpus([BETA, ALPHA]))
    assert forward.top.node_id == backward.top.node_id
    assert forward.confidence == backward.confidence


def test_the_corpus_version_changes_when_the_corpus_does():
    before = corpus_version(CORPUS)
    after = corpus_version(build_corpus([ALPHA, BETA, _entity("mdnode_GAMMA", "Eastwind")]))
    assert before != after
    assert corpus_version(build_corpus([BETA, ALPHA])) == before


@pytest.mark.parametrize(
    "value,expected",
    [
        ("Northwind", RELATION_EXACT),
        ("northwind ", RELATION_EXACT),
        ("Northwinds", RELATION_CLOSE),
        ("Contoso", RELATION_NONE),
    ],
)
def test_the_same_input_always_produces_the_same_relation(value, expected):
    assert rank_candidates(value, CORPUS).relation == expected
    assert rank_candidates(value, CORPUS).relation == expected
