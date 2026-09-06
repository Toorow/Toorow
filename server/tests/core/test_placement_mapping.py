"""The two columns Placement Mapping adds to an aggregation.

WHAT THIS FILE HOLDS, and it is criterion [3] of
`docs/product-architecture/capabilities/placement-mapping.md`:

    Incomplete if: "the two columns carry `0`, `""` or `Unmapped` where no plan
    line matched".

Measured at the commit this file was written against, that criterion could not be
violated because the two columns DID NOT EXIST:
`grep -rn 'plan_line_key|plan_line_label' server dbt ui/admin/src` returned two
lines, both comments in `core/analytics_alignment.py` citing them as the
precedent for its own three. So the assertions below come in pairs: the column
EXISTS and is filled where a plan line matched, and it is NULL -- not a sentinel
-- where none did. Only the second half is the ratified criterion; the first half
is what keeps it from passing over an absence.

The read-seam half reuses the doubles of
`tests/core/test_datastream_workbench_placements.py` rather than building a
second `_Connection` beside them: two fixtures for one surface is how two
readings of one surface start disagreeing.
"""

from __future__ import annotations

import pytest
from core.placement_mapping import (
    ADDED_COLUMNS,
    COLUMN_PREFIX,
    FORBIDDEN_SENTINELS,
    NULL_IS_THE_MEASUREMENT,
    PLAN_LINE_KEY_COLUMN,
    PLAN_LINE_LABEL_COLUMN,
    PlacementMappingRefused,
    add_plan_line_columns,
    added_columns,
    is_matched,
    matching_state,
    plan_line_columns,
    unmatched_columns,
)
from core.plan_matching_states import (
    MATCHING_STATE_MATCHED,
    MATCHING_STATE_UNMATCHED,
)
from core.project_capability_states import (
    CAPABILITY_ACTIVE_STATES,
    CAPABILITY_STATE_UNSET,
    CAPABILITY_STATES,
)

from tests.core.test_datastream_workbench_placements import (  # noqa: E402
    _PLAN,
    _Connection,
    _read,
)

# ---------------------------------------------------------------------------
# The two names, and the fact that they are the RATIFIED two.
# ---------------------------------------------------------------------------


def test_the_two_columns_are_the_ratified_names_and_they_are_derived() -> None:
    assert ADDED_COLUMNS == ("plan_line_key", "plan_line_label")
    # Derived from the prefix, on the patron `analytics_alignment.COLUMN_PREFIX`
    # states: a name computable from the declaration is a second place for the
    # declaration to be contradicted.
    assert PLAN_LINE_KEY_COLUMN == f"{COLUMN_PREFIX}_key"
    assert PLAN_LINE_LABEL_COLUMN == f"{COLUMN_PREFIX}_label"
    # The id column first, the name column second -- the order of the table in
    # `capabilities/placement-mapping.md`.
    assert ADDED_COLUMNS == (PLAN_LINE_KEY_COLUMN, PLAN_LINE_LABEL_COLUMN)


def test_the_three_forbidden_sentinels_are_the_three_the_document_names() -> None:
    assert FORBIDDEN_SENTINELS == ("0", "", "Unmapped")


# ---------------------------------------------------------------------------
# THE CRITERION. Null where no plan line matched -- never 0, "" or Unmapped.
# ---------------------------------------------------------------------------


def test_no_plan_line_matched_gives_two_nulls_and_nothing_else() -> None:
    columns = plan_line_columns()

    assert columns == {PLAN_LINE_KEY_COLUMN: None, PLAN_LINE_LABEL_COLUMN: None}
    # The criterion, spelled the way the document spells it.
    for name in ADDED_COLUMNS:
        assert columns[name] is None
        assert columns[name] not in FORBIDDEN_SENTINELS
    assert unmatched_columns() == columns


@pytest.mark.parametrize("absent", [None, "", "   ", "\t\n"])
def test_a_blank_line_key_is_an_absence_and_never_an_empty_string(absent) -> None:
    """`""` is unreachable in either column, and this is the line that makes it so.

    A caller handing a blank key in gets the NULL pair back, not a column
    carrying its blank: the emptiest of the three forbidden sentinels cannot be
    emitted even by a caller that supplies one.
    """
    assert plan_line_columns(line_key=absent) == unmatched_columns()


def test_a_matched_row_carries_the_stable_key_and_the_active_version_label() -> None:
    columns = plan_line_columns(line_key="line_display_q3", label="Display Q3")

    assert columns == {
        PLAN_LINE_KEY_COLUMN: "line_display_q3",
        PLAN_LINE_LABEL_COLUMN: "Display Q3",
    }
    assert is_matched(columns) is True


def test_a_matched_line_with_no_label_carries_the_key_and_an_absent_name() -> None:
    """Asymmetric on purpose, and the docstring of the module says why.

    A plan imported without labels must not report every one of its lines as
    unmatched spend: the identity is what decides the match, so a key with no
    label is a match whose name is absent.
    """
    columns = plan_line_columns(line_key="line_display_q3", label=None)

    assert columns[PLAN_LINE_KEY_COLUMN] == "line_display_q3"
    assert columns[PLAN_LINE_LABEL_COLUMN] is None
    assert is_matched(columns) is True
    assert matching_state(columns) == MATCHING_STATE_MATCHED


def test_a_label_without_a_key_is_refused() -> None:
    with pytest.raises(PlacementMappingRefused) as raised:
        plan_line_columns(label="Display Q3")

    assert raised.value.code == "label_without_a_plan_line"


def test_the_null_pair_is_the_fourth_matching_state_in_the_borrowed_vocabulary() -> None:
    # `placement-mapping.md`: "Rows carrying null on both columns are exactly the
    # fourth matching state". The word is BORROWED from `plan_matching_states`
    # and not respelled here.
    assert matching_state(unmatched_columns()) == MATCHING_STATE_UNMATCHED
    assert is_matched(unmatched_columns()) is False


def test_the_reason_is_stated_once_and_names_no_sentinel() -> None:
    assert "plan line" in NULL_IS_THE_MEASUREMENT
    # It explains an absence; it must not teach a reader a placeholder word.
    assert "Unmapped" not in NULL_IS_THE_MEASUREMENT


# ---------------------------------------------------------------------------
# The projection over a whole aggregation.
# ---------------------------------------------------------------------------


def test_add_plan_line_columns_fills_the_matched_rows_and_nulls_the_rest() -> None:
    rows = [
        {"campaign_ref": "camp_EXAMPLE_1", "line_key": "line_display_q3", "spend": 10},
        {"campaign_ref": "camp_EXAMPLE_9", "spend": 4210.5},
        {"campaign_ref": "camp_EXAMPLE_7", "line_key": "", "spend": 3},
    ]

    projected = add_plan_line_columns(rows, labels={"line_display_q3": "Display Q3"})

    assert projected[0][PLAN_LINE_KEY_COLUMN] == "line_display_q3"
    assert projected[0][PLAN_LINE_LABEL_COLUMN] == "Display Q3"
    for row in projected[1:]:
        assert row[PLAN_LINE_KEY_COLUMN] is None
        assert row[PLAN_LINE_LABEL_COLUMN] is None
    # Every row keeps what it came in with -- the projection ADDS columns, it
    # does not replace a reading.
    assert [row["campaign_ref"] for row in projected] == [
        "camp_EXAMPLE_1",
        "camp_EXAMPLE_9",
        "camp_EXAMPLE_7",
    ]


def test_add_plan_line_columns_never_mutates_the_rows_it_was_given() -> None:
    rows = [{"campaign_ref": "camp_EXAMPLE_1", "line_key": "line_display_q3"}]

    add_plan_line_columns(rows, labels={"line_display_q3": "Display Q3"})

    # An aggregation read twice -- once with the capability on and once off --
    # must not be contaminated by the first read.
    assert set(rows[0]) == {"campaign_ref", "line_key"}


def test_a_mapping_naming_a_line_the_active_version_dropped_keeps_its_key() -> None:
    """A stale mapping is a match with an absent name, not an unmatched row.

    `set_line_mappings` replaces a line's whole set and a re-imported plan can
    drop a line; the mapping row survives it. Reporting such a row as unmatched
    would move spend somebody DID plan into the pile of spend nobody planned.
    """
    projected = add_plan_line_columns(
        [{"line_key": "line_retired"}], labels={"line_display_q3": "Display Q3"}
    )

    assert projected[0][PLAN_LINE_KEY_COLUMN] == "line_retired"
    assert projected[0][PLAN_LINE_LABEL_COLUMN] is None


# ---------------------------------------------------------------------------
# The gate. Off adds NOTHING, and that is checkable.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", sorted(CAPABILITY_ACTIVE_STATES))
def test_an_active_capability_adds_both_columns(state) -> None:
    assert added_columns(state) == ADDED_COLUMNS


@pytest.mark.parametrize(
    "state",
    sorted(set(CAPABILITY_STATES) - set(CAPABILITY_ACTIVE_STATES))
    + [CAPABILITY_STATE_UNSET, None, ""],
)
def test_an_inactive_capability_adds_no_column_at_all(state) -> None:
    # « Éteinte, la capacité n'apparaît nulle part -- ni onglet, ni panneau, ni
    # colonne. »
    assert added_columns(state) == ()


# ---------------------------------------------------------------------------
# The read seam: the aggregation the `Placements` tab draws CARRIES the columns.
# ---------------------------------------------------------------------------


def test_the_aggregation_carries_both_columns_on_a_matched_campaign() -> None:
    evidence, _unmapped_fn, _observed_fn = _read(_Connection(state="ready"), plan_id=_PLAN)

    assert evidence["added_columns"] == list(ADDED_COLUMNS)
    assert evidence["added_columns_reason"] == NULL_IS_THE_MEASUREMENT

    matched = [line for line in evidence["lines"] if line["campaigns"]]
    assert matched, "the fixture must carry one matched line, or this proves nothing"
    campaign = matched[0]["campaigns"][0]
    assert campaign[PLAN_LINE_KEY_COLUMN] == matched[0]["line_key"]
    assert campaign[PLAN_LINE_LABEL_COLUMN] == matched[0]["label"]


def test_the_out_of_plan_rows_carry_two_nulls_and_never_a_sentinel() -> None:
    evidence, _unmapped_fn, _observed_fn = _read(_Connection(state="ready"), plan_id=_PLAN)

    rows = evidence["unmapped"]["rows"]
    assert rows, "the fixture must carry unmatched spend, or this proves nothing"
    for row in rows:
        # THE CRITERION, on the rows it is about.
        assert row[PLAN_LINE_KEY_COLUMN] is None
        assert row[PLAN_LINE_LABEL_COLUMN] is None
        for name in ADDED_COLUMNS:
            assert row[name] not in ("0", "", "Unmapped")


def test_an_accepted_row_still_carries_two_nulls() -> None:
    """Accepting says *this spend was not planned, and we know*.

    Filling a plan line in on a row somebody accepted would say the opposite --
    the same fault `analytics_alignment.AlignedRow.columns` refuses for its own
    `accepted` rows.
    """
    from tests.core.test_datastream_workbench_placements import _DECISIONS

    evidence, _unmapped_fn, _observed_fn = _read(
        _Connection(state="ready", decisions=_DECISIONS), plan_id=_PLAN
    )

    decided = [row for row in evidence["unmapped"]["rows"] if row["decision"]]
    assert decided, "the fixture must carry one acceptance, or this proves nothing"
    for row in decided:
        assert row[PLAN_LINE_KEY_COLUMN] is None
        assert row[PLAN_LINE_LABEL_COLUMN] is None


@pytest.mark.parametrize("state", ["disabled", "draft", "blocked", None])
def test_an_inactive_capability_names_neither_column_anywhere(state) -> None:
    import json

    evidence, _unmapped_fn, _observed_fn = _read(_Connection(state=state))

    assert evidence["added_columns"] == []
    # OFF NAMES NOTHING: neither column name appears anywhere in the payload,
    # which is the check `analytics_alignment_read` makes of its own three.
    payload = json.dumps(evidence, default=str)
    for name in ADDED_COLUMNS:
        assert name not in payload
