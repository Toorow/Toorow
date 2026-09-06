"""The generic Master Data invariants, proven without a database.

Every rule here is one Story 48.2 acceptance criterion stated as executable
evidence. They live in this file rather than in a Country test because the
model is generic: a Country test that proved them would prove them for Country
only, and the next capability would re-derive its own.
"""

from __future__ import annotations

from datetime import date

import pytest
from core.master_data import (
    DraftContent,
    MasterDataConflict,
    MasterDataError,
    Membership,
    UsedByReference,
    content_hash,
    effective_parent,
    membership_from_mapping,
    resolve_ancestry,
    validate_hierarchy,
)

FR, RE, GF, DE = "FR", "RE", "GF", "DE"
FRANCE, DACH, EMEA, ROW = "mdnode_A", "mdnode_B", "mdnode_C", "mdnode_D"
NODES = (FRANCE, DACH, EMEA, ROW)
VALUES = frozenset({FR, RE, GF, DE})


def _edge(
    parent: str, *, node: str | None = None, value: str | None = None, **kw: object
) -> Membership:
    return Membership(parent_node_id=parent, child_node_id=node, child_value=value, **kw)


# ---------------------------------------------------------------------------
# AC3 -- membership shape.
# ---------------------------------------------------------------------------


def test_a_membership_names_exactly_one_child() -> None:
    with pytest.raises(MasterDataError):
        Membership(parent_node_id=FRANCE)
    with pytest.raises(MasterDataError):
        Membership(parent_node_id=FRANCE, child_node_id=DACH, child_value=FR)


def test_a_node_cannot_contain_itself() -> None:
    with pytest.raises(MasterDataConflict):
        Membership(parent_node_id=FRANCE, child_node_id=FRANCE)


def test_an_effective_range_ends_after_it_starts() -> None:
    with pytest.raises(MasterDataError):
        Membership(
            parent_node_id=FRANCE,
            child_value=FR,
            effective_from=date(2026, 7, 1),
            effective_to=date(2026, 7, 1),
        )


def test_membership_from_mapping_reads_iso_dates_and_ignores_blank_values() -> None:
    edge = membership_from_mapping(
        {
            "parent_node_id": FRANCE,
            "child_value": " FR ",
            "child_node_id": "",
            "effective_from": "2026-01-01",
            "effective_to": "2026-12-31",
            "display_order": "3",
        }
    )
    assert edge.child_value == "FR"
    assert edge.child_node_id is None
    assert edge.effective_from == date(2026, 1, 1)
    assert edge.display_order == 3


# ---------------------------------------------------------------------------
# AC3 -- "at most one effective Market and one effective Region for a given
# hierarchy version and date". This is the invariant the additive
# reconciliation of AC5 depends on: a country counted twice cannot reconcile.
# ---------------------------------------------------------------------------


def test_country_to_market_to_region_is_a_valid_hierarchy() -> None:
    outcome = validate_hierarchy(
        [
            _edge(FRANCE, value=FR),
            _edge(FRANCE, value=RE),
            _edge(FRANCE, value=GF),
            _edge(DACH, value=DE),
            _edge(EMEA, node=FRANCE),
            _edge(EMEA, node=DACH),
        ],
        known_node_ids=NODES,
        known_values=VALUES,
    )
    assert outcome.ok, outcome.errors
    assert EMEA in outcome.roots
    assert outcome.depth == 3


def test_a_region_may_hold_a_country_directly_when_nothing_overlaps() -> None:
    outcome = validate_hierarchy(
        [_edge(FRANCE, value=FR), _edge(EMEA, node=FRANCE), _edge(EMEA, value=DE)],
        known_node_ids=NODES,
        known_values=VALUES,
    )
    assert outcome.ok, outcome.errors


def test_two_effective_parents_for_one_country_fail_closed() -> None:
    outcome = validate_hierarchy(
        [_edge(FRANCE, value=FR), _edge(DACH, value=FR)],
        known_node_ids=NODES,
        known_values=VALUES,
    )
    assert not outcome.ok
    assert any("two effective parents" in error for error in outcome.errors)


def test_the_same_country_may_move_between_markets_over_disjoint_dates() -> None:
    outcome = validate_hierarchy(
        [
            _edge(FRANCE, value=FR, effective_to=date(2026, 7, 1)),
            _edge(DACH, value=FR, effective_from=date(2026, 7, 1)),
        ],
        known_node_ids=NODES,
        known_values=VALUES,
    )
    assert outcome.ok, outcome.errors


def test_overlapping_dates_for_one_country_still_fail_closed() -> None:
    outcome = validate_hierarchy(
        [
            _edge(FRANCE, value=FR, effective_to=date(2026, 8, 1)),
            _edge(DACH, value=FR, effective_from=date(2026, 7, 1)),
        ],
        known_node_ids=NODES,
        known_values=VALUES,
    )
    assert not outcome.ok


def test_a_cycle_fails_closed() -> None:
    outcome = validate_hierarchy(
        [_edge(EMEA, node=FRANCE), _edge(FRANCE, node=DACH), _edge(DACH, node=EMEA)],
        known_node_ids=NODES,
        known_values=VALUES,
    )
    assert not outcome.ok
    assert "the hierarchy contains a cycle" in outcome.errors


def test_a_value_outside_the_pinned_vocabulary_fails_closed() -> None:
    outcome = validate_hierarchy(
        [_edge(FRANCE, value="XX")], known_node_ids=NODES, known_values=VALUES
    )
    assert not outcome.ok
    assert any("outside the pinned vocabulary" in error for error in outcome.errors)


def test_an_unknown_node_fails_closed() -> None:
    outcome = validate_hierarchy(
        [_edge("mdnode_MISSING", value=FR)], known_node_ids=NODES, known_values=VALUES
    )
    assert not outcome.ok
    assert any("unknown parent node" in error for error in outcome.errors)


# ---------------------------------------------------------------------------
# Effective resolution: what a read actually asks of the hierarchy.
# ---------------------------------------------------------------------------


def test_effective_parent_respects_the_as_of_date() -> None:
    edges = [
        _edge(FRANCE, value=FR, effective_to=date(2026, 7, 1)),
        _edge(DACH, value=FR, effective_from=date(2026, 7, 1)),
    ]
    assert effective_parent(edges, ("value", FR), date(2026, 6, 30)) == FRANCE
    assert effective_parent(edges, ("value", FR), date(2026, 7, 1)) == DACH


def test_an_unassigned_country_has_no_effective_parent() -> None:
    assert effective_parent([_edge(FRANCE, value=FR)], ("value", DE), date(2026, 7, 1)) is None


def test_resolve_ancestry_walks_market_to_region() -> None:
    edges = [_edge(EMEA, node=FRANCE), _edge(FRANCE, value=FR)]
    assert resolve_ancestry(edges, FRANCE, date(2026, 7, 1)) == (FRANCE, EMEA)


# ---------------------------------------------------------------------------
# Content addressing: the same meaning hashes the same way, and a reorder is
# not a change. Story 48.1 pins proposals by hash, so an unstable digest would
# invalidate every proposal on every read.
# ---------------------------------------------------------------------------


def test_draft_digest_ignores_membership_order() -> None:
    first = DraftContent(memberships=(_edge(FRANCE, value=FR), _edge(DACH, value=DE)))
    second = DraftContent(memberships=(_edge(DACH, value=DE), _edge(FRANCE, value=FR)))
    assert first.digest(vocabulary_version_id="v1") == second.digest(vocabulary_version_id="v1")


def test_draft_digest_changes_with_the_pinned_vocabulary() -> None:
    content = DraftContent(memberships=(_edge(FRANCE, value=FR),))
    assert content.digest(vocabulary_version_id="v1") != content.digest(vocabulary_version_id="v2")


def test_draft_digest_changes_with_the_rest_of_world_policy() -> None:
    edges = (_edge(FRANCE, value=FR),)
    aggregated = DraftContent(memberships=edges, payload={"rest_of_world": {"drill": "aggregate"}})
    drilled = DraftContent(memberships=edges, payload={"rest_of_world": {"drill": "country"}})
    assert aggregated.digest(vocabulary_version_id="v1") != drilled.digest(
        vocabulary_version_id="v1"
    )


def test_content_hash_is_stable_across_key_order() -> None:
    assert content_hash({"a": 1, "b": 2}) == content_hash({"b": 2, "a": 1})


# ---------------------------------------------------------------------------
# AC8 -- used-by references carry what a confirmation must display.
# ---------------------------------------------------------------------------


def test_used_by_reference_states_its_consumer_and_pinned_version() -> None:
    reference = UsedByReference(
        node_id=FRANCE,
        consumer_kind="budget",
        consumer_id="bud_EXAMPLE",
        consumer_label="FY26 France",
        consumer_version_id="ver_1",
        hierarchy_version_id="mdver_1",
    )
    assert reference.as_dict() == {
        "node_id": FRANCE,
        "consumer_kind": "budget",
        "consumer_id": "bud_EXAMPLE",
        "consumer_label": "FY26 France",
        "consumer_version_id": "ver_1",
        "hierarchy_version_id": "mdver_1",
        # Story 49.2 AC7: the owner workspace and the evidence time travel with
        # every reference. They are READ-side facts -- no writer supplies them --
        # so a reference built by hand carries them as the honest absence.
        "workspace": None,
        "recorded_at": None,
    }
