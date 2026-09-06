"""Story 70.3 -- the method cascade, its four states, and its three dependencies.

These tests assert the properties the story is about, not the plumbing around
them: that ONE method resolves a row and it is the highest one that could, that
two candidates are never auto-arbitrated, that an unmatched row is RETURNED
rather than counted away, that the three added columns are null on every row that
is not matched, and that an activation refused names EVERY dependency it is
missing together with the gesture that meets it.

Everything here is offline. `test_analytics_alignment_pg.py` is what proves the
table, its CHECKs and the first-decision-stands index against a real Postgres.

No real identifier appears: `proj_EXAMPLE`, `ds_EXAMPLE_*`, `owner@example.com`.
"""

from __future__ import annotations

import pytest
from core.analytics_alignment import (
    ADDED_COLUMNS,
    ALIGNMENT_DEPENDENCIES,
    ALIGNMENT_METHOD_LABELS,
    ALIGNMENT_METHODS,
    AUTOMATIC_METHODS,
    DECISION_ACCEPTED,
    DECISION_ARBITRATED,
    DEPENDENCY_APPROVED_RELATIONSHIP,
    DEPENDENCY_COMMON_KEY,
    DEPENDENCY_CURRENCY_FX,
    ENTITY_ID_COLUMN,
    ENTITY_NAME_COLUMN,
    METHOD_COLUMN,
    METHOD_HUMAN_ARBITRATION,
    METHOD_ID_EXACT,
    METHOD_KEY_EXACT,
    METHOD_NAME_NORMALIZED,
    METHOD_NAME_PREFIX,
    MISSING_DEPENDENCY_BY_CODE,
    NAME_PREFIX_MIN_LENGTH,
    AlignedRow,
    AlignmentCurrencies,
    AlignmentDecision,
    AlignmentRefused,
    AlignmentSide,
    added_columns,
    alignment_counts,
    alignment_method_label,
    covering_key_versions,
    decisions_by_row,
    resolve_dependencies,
    run_cascade,
    unmatched_rows,
)
from core.plan_matching_states import (
    MATCHING_STATE_ACCEPTED,
    MATCHING_STATE_AMBIGUOUS,
    MATCHING_STATE_MATCHED,
    MATCHING_STATE_UNMATCHED,
    PLAN_MATCHING_STATES,
)

PROJECT = "proj_EXAMPLE"
ORG = "org_EXAMPLE"
LEFT_DS = "ds_EXAMPLE_media"
RIGHT_DS = "ds_EXAMPLE_analytics"
KEY_VERSION = "mdmckv_EXAMPLE"
ACTOR = "owner@example.com"


def _row(index: int) -> str:
    return f"row_{index}"


# ---------------------------------------------------------------------------
# The vocabulary.
# ---------------------------------------------------------------------------


def test_the_method_vocabulary_is_closed_ordered_and_ends_on_the_human() -> None:
    """Five words, in the cascade's order, with the person LAST.

    The order is the contract: the automatic stages are the four safest, and the
    arbitration that comes after them settles what they could not. A test that
    only checked membership would let the order be reshuffled -- which changes
    what every already-aligned row means.
    """
    assert ALIGNMENT_METHODS == (
        METHOD_ID_EXACT,
        METHOD_KEY_EXACT,
        METHOD_NAME_NORMALIZED,
        METHOD_NAME_PREFIX,
        METHOD_HUMAN_ARBITRATION,
    )
    assert AUTOMATIC_METHODS == ALIGNMENT_METHODS[:-1]
    assert METHOD_HUMAN_ARBITRATION not in AUTOMATIC_METHODS


def test_every_method_is_called_something_a_person_can_read() -> None:
    """No machine key reaches a screen, and no label is a promise the code breaks."""
    assert set(ALIGNMENT_METHOD_LABELS) == set(ALIGNMENT_METHODS)
    assert all(label.strip() for label in ALIGNMENT_METHOD_LABELS.values())
    # The fault story 61.3 named on the neighbouring axis: a string comparison
    # dressed as an intelligence, and a fixed pipeline dressed as a rule.
    joined = " ".join(ALIGNMENT_METHOD_LABELS.values()).lower()
    assert "ai" not in joined.split()
    assert "score" not in joined
    assert alignment_method_label("id_exact") == "Same entity id"
    assert alignment_method_label("something_nobody_declared") is None


def test_the_states_are_borrowed_and_not_respelled() -> None:
    """The four words are `plan_matching_states`', imported rather than invented."""
    assert alignment_counts(()) == {state: 0 for state in PLAN_MATCHING_STATES}
    assert set(alignment_counts(())) == {
        MATCHING_STATE_MATCHED,
        MATCHING_STATE_AMBIGUOUS,
        MATCHING_STATE_UNMATCHED,
        MATCHING_STATE_ACCEPTED,
    }


# ---------------------------------------------------------------------------
# The cascade.
# ---------------------------------------------------------------------------


def test_a_row_is_resolved_by_the_highest_method_that_could_and_by_no_other() -> None:
    """Id, key and name all agree -- the row says `id_exact`, once.

    THE FAULT THIS CLOSES: a cascade that reports every method that would have
    worked reports a confidence, not a provenance, and a reader cannot tell which
    fact the row actually rests on.
    """
    left = [AlignmentSide(row_key=_row(1), entity_id="e1", entity_key="k1", entity_name="Alpha")]
    right = [
        AlignmentSide(row_key="r1", entity_id="e1", entity_key="k1", entity_name="Alpha"),
    ]
    (aligned,) = run_cascade(left, right)
    assert aligned.state == MATCHING_STATE_MATCHED
    assert aligned.method == METHOD_ID_EXACT
    assert aligned.columns()[METHOD_COLUMN] == METHOD_ID_EXACT


@pytest.mark.parametrize(
    ("right_side", "expected"),
    [
        (
            AlignmentSide(row_key="r1", entity_key="k1", entity_name="Something else entirely"),
            METHOD_KEY_EXACT,
        ),
        (
            AlignmentSide(row_key="r1", entity_name="  ALPHA   CAMPAIGN  "),
            METHOD_NAME_NORMALIZED,
        ),
        (
            AlignmentSide(row_key="r1", entity_name="alpha campaign winter push"),
            METHOD_NAME_PREFIX,
        ),
    ],
)
def test_each_stage_of_the_cascade_resolves_on_its_own_and_names_itself(
    right_side: AlignmentSide, expected: str
) -> None:
    """One stage per situation, and the row carries the stage that answered."""
    left = [AlignmentSide(row_key=_row(1), entity_key="k1", entity_name="Alpha Campaign")]
    (aligned,) = run_cascade(left, [right_side])
    assert aligned.state == MATCHING_STATE_MATCHED
    assert aligned.method == expected


def test_two_candidates_are_ambiguous_and_the_cascade_stops_there() -> None:
    """Two names normalize the same -- `ambiguous`, and NO looser stage runs.

    Falling through to `name_prefix` would have picked between them by a weaker
    rule, which is arbitration without a person. The state exists precisely
    because nobody has arbitrated.
    """
    left = [AlignmentSide(row_key=_row(1), entity_name="Alpha Campaign")]
    right = [
        AlignmentSide(row_key="r1", entity_name="alpha campaign"),
        AlignmentSide(row_key="r2", entity_name="ALPHA-CAMPAIGN"),
    ]
    (aligned,) = run_cascade(left, right)
    assert aligned.state == MATCHING_STATE_AMBIGUOUS
    assert aligned.method == METHOD_NAME_NORMALIZED
    assert aligned.entity_id is None and aligned.entity_name is None


def test_an_ambiguous_row_names_its_candidates() -> None:
    """An ambiguity whose candidates are not carried beside it is a decoration."""
    left = [AlignmentSide(row_key=_row(1), entity_id="e1")]
    right = [
        AlignmentSide(row_key="r1", entity_id="e1"),
        AlignmentSide(row_key="r2", entity_id="e1"),
    ]
    (aligned,) = run_cascade(left, right)
    assert aligned.state == MATCHING_STATE_AMBIGUOUS
    assert aligned.candidates == ("r1", "r2")


def test_an_unmatched_row_is_returned_and_counted_never_dropped() -> None:
    """It is the work. A cascade that returns only its successes hides it."""
    left = [
        AlignmentSide(row_key=_row(1), entity_id="e1"),
        AlignmentSide(row_key=_row(2), entity_id="e_nobody_has"),
    ]
    right = [AlignmentSide(row_key="r1", entity_id="e1")]
    aligned = run_cascade(left, right)
    assert len(aligned) == 2
    assert alignment_counts(aligned) == {
        MATCHING_STATE_MATCHED: 1,
        MATCHING_STATE_AMBIGUOUS: 0,
        MATCHING_STATE_UNMATCHED: 1,
        MATCHING_STATE_ACCEPTED: 0,
    }
    assert [row.row_key for row in unmatched_rows(aligned)] == [_row(2)]


def test_the_prefix_stage_has_a_declared_floor_and_a_short_name_reaches_nothing() -> None:
    """`Q3` is a prefix of half a catalogue, and a floorless stage is noise.

    The floor is a LENGTH, not a confidence, and it applies to BOTH sides: a
    left-hand name shorter than the floor cannot share enough of itself with
    anything.
    """
    assert NAME_PREFIX_MIN_LENGTH >= 2
    short = [AlignmentSide(row_key=_row(1), entity_name="Q3")]
    right = [AlignmentSide(row_key="r1", entity_name="Q3 brand awareness push")]
    (aligned,) = run_cascade(short, right)
    assert aligned.state == MATCHING_STATE_UNMATCHED

    long_enough = [AlignmentSide(row_key=_row(1), entity_name="Q3 brand awareness")]
    (aligned,) = run_cascade(long_enough, right)
    assert aligned.state == MATCHING_STATE_MATCHED
    assert aligned.method == METHOD_NAME_PREFIX


def test_a_prefix_floor_below_one_character_is_refused() -> None:
    with pytest.raises(AlignmentRefused) as excinfo:
        run_cascade([], [], prefix_min_length=0)
    assert excinfo.value.code == "prefix_floor_below_one"


# ---------------------------------------------------------------------------
# The three columns, and the one rule that governs them.
# ---------------------------------------------------------------------------


def test_the_three_columns_are_the_family_two_plus_the_method() -> None:
    assert ADDED_COLUMNS == (ENTITY_ID_COLUMN, ENTITY_NAME_COLUMN, METHOD_COLUMN)
    # Every name is derived from the one prefix, so the declaration has one home.
    assert all(name.startswith("analytics_alignment_") for name in ADDED_COLUMNS)


def test_a_matched_row_carries_the_aligned_entity_id_and_name() -> None:
    left = [AlignmentSide(row_key=_row(1), entity_id="e1")]
    right = [AlignmentSide(row_key="r1", entity_id="e1", entity_name="Alpha Campaign")]
    (aligned,) = run_cascade(left, right)
    assert aligned.columns() == {
        ENTITY_ID_COLUMN: "e1",
        ENTITY_NAME_COLUMN: "Alpha Campaign",
        METHOD_COLUMN: METHOD_ID_EXACT,
    }


def test_every_row_that_is_not_matched_carries_null_and_never_a_sentinel() -> None:
    """`0`, `""` and `Unaligned` each make an unaligned row look aligned.

    The three situations are tested together on purpose: `unmatched`, `ambiguous`
    and `accepted` are three different reasons for the same absence, and a fix
    that only covered the first would leave the other two rendering a zero.
    """
    left = [
        AlignmentSide(row_key=_row(1), entity_id="nobody_has_this"),
        AlignmentSide(row_key=_row(2), entity_id="shared"),
        AlignmentSide(row_key=_row(3), entity_id="also_nobody"),
    ]
    right = [
        AlignmentSide(row_key="r1", entity_id="shared"),
        AlignmentSide(row_key="r2", entity_id="shared"),
    ]
    decisions = [
        AlignmentDecision(
            left_row_key=_row(3),
            decision=DECISION_ACCEPTED,
            decided_by=ACTOR,
            decided_at="2026-08-25T09:00:00+00:00",
            reason="No analytics row covers this placement.",
        )
    ]
    aligned = run_cascade(left, right, decisions=decisions)
    states = {row.row_key: row.state for row in aligned}
    assert states == {
        _row(1): MATCHING_STATE_UNMATCHED,
        _row(2): MATCHING_STATE_AMBIGUOUS,
        _row(3): MATCHING_STATE_ACCEPTED,
    }
    for row in aligned:
        assert row.columns() == dict.fromkeys(ADDED_COLUMNS, None)
        assert set(row.columns()) == set(ADDED_COLUMNS)


def test_a_matched_row_with_no_method_is_refused_at_construction() -> None:
    with pytest.raises(AlignmentRefused) as excinfo:
        AlignedRow(row_key=_row(1), state=MATCHING_STATE_MATCHED, entity_id="e1")
    assert excinfo.value.code == "matched_without_method"


def test_a_row_that_is_not_matched_may_not_carry_an_entity() -> None:
    with pytest.raises(AlignmentRefused) as excinfo:
        AlignedRow(row_key=_row(1), state=MATCHING_STATE_ACCEPTED, entity_id="e1")
    assert excinfo.value.code == "unmatched_row_carries_an_entity"


# ---------------------------------------------------------------------------
# The two human acts.
# ---------------------------------------------------------------------------


def test_an_arbitration_settles_an_ambiguous_row_and_says_who_and_when() -> None:
    left = [AlignmentSide(row_key=_row(1), entity_id="shared")]
    right = [
        AlignmentSide(row_key="r1", entity_id="shared", entity_name="First"),
        AlignmentSide(row_key="r2", entity_id="shared", entity_name="Second"),
    ]
    decisions = [
        AlignmentDecision(
            left_row_key=_row(1),
            decision=DECISION_ARBITRATED,
            right_row_key="r2",
            decided_by=ACTOR,
            decided_at="2026-08-25T09:00:00+00:00",
            reason="The second one is the campaign that ran.",
        )
    ]
    (aligned,) = run_cascade(left, right, decisions=decisions)
    assert aligned.state == MATCHING_STATE_MATCHED
    assert aligned.method == METHOD_HUMAN_ARBITRATION
    assert aligned.entity_name == "Second"
    assert aligned.decided_by == ACTOR
    assert aligned.decided_at == "2026-08-25T09:00:00+00:00"


def test_an_acceptance_keeps_the_row_listed_with_who_and_when() -> None:
    """Accepting says *nothing answers this row, and we know* -- never *it does*."""
    left = [AlignmentSide(row_key=_row(1), entity_id="nobody_has_this")]
    decisions = [
        AlignmentDecision(
            left_row_key=_row(1),
            decision=DECISION_ACCEPTED,
            decided_by=ACTOR,
            decided_at="2026-08-25T09:00:00+00:00",
            reason="Brand campaign, no site tagging.",
        )
    ]
    (aligned,) = run_cascade(left, [], decisions=decisions)
    assert aligned.state == MATCHING_STATE_ACCEPTED
    assert aligned.decided_by == ACTOR and aligned.reason
    assert aligned.columns() == dict.fromkeys(ADDED_COLUMNS, None)


def test_an_arbitration_never_overwrites_a_row_an_automatic_stage_resolved() -> None:
    """`human_arbitration` is LAST, and that position is the arbitrage.

    If an `id_exact` alignment is wrong, the mapping is wrong. Letting a click
    contradict it would hide the real defect behind a decision nobody can audit.
    """
    left = [AlignmentSide(row_key=_row(1), entity_id="e1")]
    right = [
        AlignmentSide(row_key="r1", entity_id="e1", entity_name="The id match"),
        AlignmentSide(row_key="r2", entity_name="Something a person picked"),
    ]
    decisions = [
        AlignmentDecision(
            left_row_key=_row(1),
            decision=DECISION_ARBITRATED,
            right_row_key="r2",
            decided_by=ACTOR,
        )
    ]
    (aligned,) = run_cascade(left, right, decisions=decisions)
    assert aligned.method == METHOD_ID_EXACT
    assert aligned.entity_name == "The id match"


def test_the_first_decision_on_a_row_stands() -> None:
    """A second click never rewrites who decided and when."""
    first = AlignmentDecision(
        left_row_key=_row(1), decision=DECISION_ACCEPTED, decided_by="first@example.com"
    )
    second = AlignmentDecision(
        left_row_key=_row(1), decision=DECISION_ACCEPTED, decided_by="second@example.com"
    )
    assert decisions_by_row([first, second])[_row(1)].decided_by == "first@example.com"


def test_an_arbitration_naming_a_row_nobody_publishes_is_refused() -> None:
    left = [AlignmentSide(row_key=_row(1))]
    decisions = [
        AlignmentDecision(
            left_row_key=_row(1),
            decision=DECISION_ARBITRATED,
            right_row_key="r_that_does_not_exist",
            decided_by=ACTOR,
        )
    ]
    with pytest.raises(AlignmentRefused) as excinfo:
        run_cascade(left, [], decisions=decisions)
    assert excinfo.value.code == "arbitration_names_an_absent_row"


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        (
            {"left_row_key": "row_1", "decision": DECISION_ARBITRATED, "decided_by": ACTOR},
            "arbitration_names_nothing",
        ),
        (
            {
                "left_row_key": "row_1",
                "decision": DECISION_ACCEPTED,
                "right_row_key": "r1",
                "decided_by": ACTOR,
            },
            "acceptance_names_a_side",
        ),
        (
            {"left_row_key": "row_1", "decision": DECISION_ACCEPTED, "decided_by": "  "},
            "decision_without_an_author",
        ),
    ],
)
def test_a_decision_that_states_nothing_is_refused(kwargs: dict, code: str) -> None:
    with pytest.raises(AlignmentRefused) as excinfo:
        AlignmentDecision(**kwargs)
    assert excinfo.value.code == code


# ---------------------------------------------------------------------------
# Money: refused, never warned.
# ---------------------------------------------------------------------------


def test_two_unconverted_currencies_are_refused_and_the_refusal_names_the_gesture() -> None:
    with pytest.raises(AlignmentRefused) as excinfo:
        run_cascade(
            [AlignmentSide(row_key=_row(1), entity_id="e1")],
            [AlignmentSide(row_key="r1", entity_id="e1")],
            currencies=AlignmentCurrencies(left="USD", right="EUR", reporting="EUR"),
        )
    assert excinfo.value.code == "unconverted_currency"
    assert "USD" in excinfo.value.message
    assert "FX rate batch" in excinfo.value.message


def test_an_unresolved_reporting_currency_is_refused() -> None:
    with pytest.raises(AlignmentRefused) as excinfo:
        run_cascade([], [], currencies=AlignmentCurrencies(left="EUR", right="EUR", reporting=""))
    assert excinfo.value.code == "reporting_currency_unresolved"


def test_a_side_that_publishes_no_currency_at_all_is_refused() -> None:
    with pytest.raises(AlignmentRefused) as excinfo:
        run_cascade([], [], currencies=AlignmentCurrencies(left="EUR", right=None, reporting="EUR"))
    assert excinfo.value.code == "native_currency_missing"


def test_a_run_that_states_no_currency_makes_no_claim_about_money() -> None:
    """The three columns carry no amount, so an identity run is not a money run."""
    (aligned,) = run_cascade(
        [AlignmentSide(row_key=_row(1), entity_id="e1")],
        [AlignmentSide(row_key="r1", entity_id="e1")],
        currencies=None,
    )
    assert aligned.state == MATCHING_STATE_MATCHED


# ---------------------------------------------------------------------------
# The switch: off adds nothing.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["disabled", "draft", "blocked", "unset"])
def test_no_column_is_added_while_the_capability_is_not_active(state: str) -> None:
    assert added_columns(state) == ()


@pytest.mark.parametrize("state", ["ready", "degraded"])
def test_the_three_columns_are_added_on_the_two_active_states(state: str) -> None:
    """`degraded` shows the capability AND says it is degraded -- it does not hide it."""
    assert added_columns(state) == ADDED_COLUMNS


# ---------------------------------------------------------------------------
# The dependencies.
# ---------------------------------------------------------------------------


def test_every_declared_dependency_has_a_reason_and_a_gesture() -> None:
    """A refusal that names a cause and no act sends a person looking for a control."""
    assert ALIGNMENT_DEPENDENCIES == (
        DEPENDENCY_COMMON_KEY,
        DEPENDENCY_APPROVED_RELATIONSHIP,
        DEPENDENCY_CURRENCY_FX,
    )
    assert set(MISSING_DEPENDENCY_BY_CODE) == set(ALIGNMENT_DEPENDENCIES)
    for code, missing in MISSING_DEPENDENCY_BY_CODE.items():
        assert missing.code == code
        assert missing.reason.strip() and missing.gesture.strip()


def test_a_key_covers_a_pair_only_when_both_sides_implement_every_component() -> None:
    coverage = {
        "state": "available",
        "components": [
            {"implemented_by": [{"datastream_id": LEFT_DS}, {"datastream_id": RIGHT_DS}]},
            {"implemented_by": [{"datastream_id": LEFT_DS}]},
        ],
    }
    assert not covering_key_versions(
        coverage, left_datastream_id=LEFT_DS, right_datastream_id=RIGHT_DS
    )
    coverage["components"][1]["implemented_by"].append({"datastream_id": RIGHT_DS})
    assert covering_key_versions(
        coverage, left_datastream_id=LEFT_DS, right_datastream_id=RIGHT_DS
    )


def test_a_key_that_declares_no_component_covers_nothing() -> None:
    """An identity that declares nothing is not an identity."""
    assert not covering_key_versions(
        {"state": "available", "components": []},
        left_datastream_id=LEFT_DS,
        right_datastream_id=RIGHT_DS,
    )


class _StubConn:
    """A connection nothing here reads. Every collaborator is patched by name."""


def _patch_dependencies(
    monkeypatch,
    *,
    keys: list[dict],
    coverage: dict,
    pinned: dict,
    fx_state: str,
) -> None:
    import core.analytics_alignment as module
    import core.datastream_matches as matches
    import core.mdm_common_keys as common_keys

    monkeypatch.setattr(common_keys, "list_common_keys", lambda conn, *, project_id: keys)
    monkeypatch.setattr(
        common_keys, "mapping_coverage", lambda conn, *, project_id, components: coverage
    )
    monkeypatch.setattr(matches, "executable_key_versions", lambda conn, project_id: pinned)
    monkeypatch.setattr(
        module, "read_capability_state", lambda conn, *, project_id, capability_key: fx_state
    )


_ACTIVE_KEY = [
    {
        "id": "mdmck_EXAMPLE",
        "status": "active",
        "current_version": {"id": KEY_VERSION, "components": [{"canonical_field_id": "cf_day"}]},
    }
]
_BOTH_SIDES = {
    "state": "available",
    "components": [
        {"implemented_by": [{"datastream_id": LEFT_DS}, {"datastream_id": RIGHT_DS}]}
    ],
}
_APPROVED = {
    KEY_VERSION: [
        {
            "relationship_name": "media_to_analytics",
            "left_datastream_id": LEFT_DS,
            "right_datastream_id": RIGHT_DS,
            "view_version_id": "svv_EXAMPLE",
        }
    ]
}


def test_an_activation_refused_names_every_dependency_it_is_missing(monkeypatch) -> None:
    """Every one, not the first.

    A refusal that reveals its blockers one at a time makes a person fix, retry,
    and be refused again -- which is how a dependency chain becomes a guessing
    game.
    """
    _patch_dependencies(
        monkeypatch, keys=[], coverage={"state": "available", "components": []},
        pinned={}, fx_state="disabled",
    )
    result = resolve_dependencies(
        _StubConn(), project_id=PROJECT, left_datastream_id=LEFT_DS, right_datastream_id=RIGHT_DS
    )
    assert not result.satisfied
    assert [item.code for item in result.missing] == [
        DEPENDENCY_COMMON_KEY,
        DEPENDENCY_CURRENCY_FX,
    ]
    for item in result.missing:
        assert item.gesture.strip()


def test_a_covered_key_with_no_published_relationship_is_not_executable(monkeypatch) -> None:
    """A key without an approved relationship remains non-executable."""
    _patch_dependencies(
        monkeypatch, keys=_ACTIVE_KEY, coverage=_BOTH_SIDES, pinned={}, fx_state="ready"
    )
    result = resolve_dependencies(
        _StubConn(), project_id=PROJECT, left_datastream_id=LEFT_DS, right_datastream_id=RIGHT_DS
    )
    assert [item.code for item in result.missing] == [DEPENDENCY_APPROVED_RELATIONSHIP]
    assert result.common_key_version_ids == (KEY_VERSION,)


def test_a_relationship_between_two_other_datastreams_does_not_authorize_this_pair(
    monkeypatch,
) -> None:
    """The relationship names its two sides, and this pair is not them."""
    elsewhere = {
        KEY_VERSION: [
            {
                "relationship_name": "somewhere_else",
                "left_datastream_id": LEFT_DS,
                "right_datastream_id": "ds_EXAMPLE_third",
                "view_version_id": "svv_EXAMPLE",
            }
        ]
    }
    _patch_dependencies(
        monkeypatch, keys=_ACTIVE_KEY, coverage=_BOTH_SIDES, pinned=elsewhere, fx_state="ready"
    )
    result = resolve_dependencies(
        _StubConn(), project_id=PROJECT, left_datastream_id=LEFT_DS, right_datastream_id=RIGHT_DS
    )
    assert [item.code for item in result.missing] == [DEPENDENCY_APPROVED_RELATIONSHIP]


def test_all_three_satisfied_is_satisfied_and_pins_the_exact_key_version(monkeypatch) -> None:
    _patch_dependencies(
        monkeypatch, keys=_ACTIVE_KEY, coverage=_BOTH_SIDES, pinned=_APPROVED, fx_state="ready"
    )
    result = resolve_dependencies(
        _StubConn(), project_id=PROJECT, left_datastream_id=LEFT_DS, right_datastream_id=RIGHT_DS
    )
    assert result.satisfied
    assert result.relationships[0]["common_key_version_id"] == KEY_VERSION
    assert result.as_dict()["missing"] == []


def test_a_degraded_currency_fx_still_satisfies_the_money_dependency(monkeypatch) -> None:
    """`degraded` shows the capability and says so; hiding evidence is worse."""
    _patch_dependencies(
        monkeypatch, keys=_ACTIVE_KEY, coverage=_BOTH_SIDES, pinned=_APPROVED, fx_state="degraded"
    )
    result = resolve_dependencies(
        _StubConn(), project_id=PROJECT, left_datastream_id=LEFT_DS, right_datastream_id=RIGHT_DS
    )
    assert result.satisfied


def test_a_coverage_that_could_not_be_read_is_not_read_as_uncovered(monkeypatch) -> None:
    """"I could not look" and "nobody binds it" are different answers."""
    _patch_dependencies(
        monkeypatch,
        keys=_ACTIVE_KEY,
        coverage={"state": "unavailable", "components": []},
        pinned=_APPROVED,
        fx_state="ready",
    )
    result = resolve_dependencies(
        _StubConn(), project_id=PROJECT, left_datastream_id=LEFT_DS, right_datastream_id=RIGHT_DS
    )
    # No key is claimed to cover the pair, so the FIRST dependency is the one
    # named -- never the relationship, whose gesture nobody could take yet.
    assert [item.code for item in result.missing] == [DEPENDENCY_COMMON_KEY]


def test_a_datastream_aligned_with_itself_is_not_a_pair() -> None:
    with pytest.raises(AlignmentRefused) as excinfo:
        resolve_dependencies(
            _StubConn(),
            project_id=PROJECT,
            left_datastream_id=LEFT_DS,
            right_datastream_id=LEFT_DS,
        )
    assert excinfo.value.code == "one_datastream_is_not_a_pair"
