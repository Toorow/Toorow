"""Story 70.4 -- the prorata ventilation engine, proved without a connection.

The engine is pure, so every one of the story's criteria is provable directly:
the weights of a key sum to exactly one, the assertion RAISES rather than rounds,
the split is reversible to the cent, a key whose declared volume is absent or
zero is not split and is COUNTED, the provenance names the declared volume, and
the observed columns and the ventilated ones are never the same column.

`test_analytics_ventilation_pg.py` proves the half this file cannot: that the
database accepts the weights, refuses the shapes the model forbids, and gives
back a run whose stored weights still conserve.

Fixtures are generic -- `key_*`, `entity_*`, `owner@example.com`. No real
identifier, no client, no domain.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from core.analytics_alignment import (
    ENTITY_ID_COLUMN,
    ENTITY_NAME_COLUMN,
    METHOD_COLUMN,
    METHOD_ID_EXACT,
    AlignedRow,
    AlignmentRefused,
)
from core.analytics_ventilation import (
    REFUSAL_VOLUME_IS_NEGATIVE,
    REFUSAL_VOLUME_IS_ZERO,
    REFUSAL_VOLUME_NOT_DECLARED,
    RESIDUAL_COLUMN,
    VENTILATION_COLUMNS,
    VOLUME_COLUMN,
    VOLUME_TOTAL_COLUMN,
    VOLUME_VALUE_COLUMN,
    VOLUME_VERSION_COLUMN,
    WEIGHT_COLUMN,
    VentilatedRow,
    VentilationBasis,
    VentilationCandidate,
    VentilationGroup,
    VentilationRefused,
    VentilationShare,
    assert_stored_weights_conserve,
    assert_weights_sum_to_one,
    reassemble,
    refuse_mixed_columns,
    ventilate,
    ventilated_metric_column,
    ventilated_rows,
    ventilation_columns,
    ventilation_counts,
)
from core.plan_matching_states import MATCHING_STATE_AMBIGUOUS, MATCHING_STATE_UNMATCHED

DAY = date(2026, 8, 25)
BASIS = VentilationBasis(volume_name="clicks", declared_version="v1")
ACTIVE = "ready"


def _group(key: str, *volumes: object, day: object = DAY) -> VentilationGroup:
    """One coarse key with N candidates, named `entity_a`, `entity_b`, ..."""
    return VentilationGroup.of(
        key,
        day,
        [
            VentilationCandidate(row_key=f"entity_{chr(ord('a') + index)}", volume=volume)
            for index, volume in enumerate(volumes)
        ],
    )


def _run(*groups: VentilationGroup, state: str = ACTIVE):
    return ventilate(list(groups), basis=BASIS, capability_state=state)


# ---------------------------------------------------------------------------
# The weights sum to one, EXACTLY, and the assertion is what says so.
# ---------------------------------------------------------------------------


def test_the_weights_of_a_key_sum_to_exactly_one_as_a_decimal() -> None:
    outcome = _run(_group("key_1", 10, 30, 60))
    weights = [share.weight for share in outcome.shares]
    assert all(isinstance(weight, Decimal) for weight in weights)
    assert sum(weights) == Decimal(1)
    assert weights == [Decimal("0.1"), Decimal("0.3"), Decimal("0.6")]


def test_a_split_no_finite_decimal_can_write_still_sums_to_exactly_one() -> None:
    """Three equal candidates. 1/3 has no finite decimal, and the engine still conserves.

    Quantizing all three would give 0.999999999999999999, and an engine that
    refuses thirds is not an engine. The residual lands on ONE named carrier and
    the sum stays exact.
    """
    outcome = _run(_group("key_1", 1, 1, 1))
    weights = [share.weight for share in outcome.shares]
    assert sum(weights) == Decimal(1)
    assert sum(1 for share in outcome.shares if share.carries_residual) == 1


def test_the_residual_carrier_is_the_largest_volume_and_it_is_deterministic() -> None:
    first = _run(_group("key_1", 1, 2, 4)).shares
    again = _run(_group("key_1", 1, 2, 4)).shares
    carrier = [share.right_row_key for share in first if share.carries_residual]
    assert carrier == ["entity_c"]  # the largest volume
    assert [s.right_row_key for s in first if s.carries_residual] == [
        s.right_row_key for s in again if s.carries_residual
    ]


def test_a_tie_on_the_volume_is_broken_by_the_lowest_row_key() -> None:
    shares = _run(_group("key_1", 5, 5)).shares
    assert [share.right_row_key for share in shares if share.carries_residual] == [
        "entity_a"
    ]


def test_a_weight_set_that_deviates_RAISES_and_is_never_rounded_into_agreement() -> None:
    """The invariant is an assertion, not a correction. Zero tolerance, Decimal."""
    with pytest.raises(VentilationRefused) as excinfo:
        assert_weights_sum_to_one([Decimal("0.5"), Decimal("0.4")])
    assert excinfo.value.code == "weights_do_not_sum_to_one"
    assert "0.9" in excinfo.value.message

    # One unit in the last place is still a deviation, and still raises.
    with pytest.raises(VentilationRefused):
        assert_weights_sum_to_one(
            [Decimal("0.5"), Decimal("0.499999999999999999")]
        )


def test_a_stored_run_that_drifted_is_caught_on_the_way_BACK_OUT() -> None:
    """The check an assertion at computation time cannot perform.

    A run written half, a row a cascade erased, a column with the wrong scale: all
    three are invisible to the engine that computed the weights and visible to
    the one that re-reads them.
    """
    drifted = [
        VentilationShare(
            alignment_key="key_1",
            day=DAY.isoformat(),
            right_row_key=row_key,
            weight=weight,
            volume=Decimal(1),
            volume_total=Decimal(3),
            volume_name="clicks",
            volume_version="v1",
        )
        for row_key, weight in (("entity_a", Decimal("0.5")), ("entity_b", Decimal("0.4")))
    ]
    with pytest.raises(VentilationRefused) as excinfo:
        assert_stored_weights_conserve(drifted)
    assert excinfo.value.code == "weights_do_not_sum_to_one"


def test_a_ventilation_refusal_is_an_alignment_refusal() -> None:
    """One gesture for the person who runs it, so one exception family to guard."""
    assert issubclass(VentilationRefused, AlignmentRefused)


# ---------------------------------------------------------------------------
# Reversibility. Weight x observed re-gives the observed, to the cent.
# ---------------------------------------------------------------------------


def test_the_split_re_gives_the_observed_figure_exactly() -> None:
    outcome = _run(_group("key_1", 7, 11, 13))
    observed = {"conversions": Decimal("1234.56"), "revenue_micros": Decimal("987654321")}
    back = reassemble(outcome.shares, observed)
    assert back["conversions"] == Decimal("1234.56")
    assert back["revenue_micros"] == Decimal("987654321")
    # And to the cent, said the way a client would check it.
    assert back["conversions"].quantize(Decimal("0.01")) == Decimal("1234.56")


def test_the_split_conserves_on_a_ratio_no_decimal_can_write() -> None:
    outcome = _run(_group("key_1", 1, 1, 1))
    observed = {"conversions": Decimal("100.00")}
    assert reassemble(outcome.shares, observed)["conversions"] == Decimal("100.00")


def test_the_split_conserves_over_many_candidates_and_an_awkward_total() -> None:
    outcome = _run(_group("key_1", *range(1, 18)))
    observed = {"spend_micros": Decimal("1000000000001")}
    assert reassemble(outcome.shares, observed)["spend_micros"] == Decimal("1000000000001")


def test_the_observed_figure_is_never_overwritten_by_the_split() -> None:
    observed = {"conversions": Decimal("300")}
    outcome = _run(_group("key_1", 1, 2))
    for share in outcome.shares:
        share.ventilate(observed)
    assert observed == {"conversions": Decimal("300")}


def test_a_share_carries_everything_needed_to_recompute_its_own_weight() -> None:
    share = _run(_group("key_1", 25, 75)).shares[0]
    assert share.volume / share.volume_total == share.weight


# ---------------------------------------------------------------------------
# The refusal. Absent or zero is NOT ventilated, and it is counted.
# ---------------------------------------------------------------------------


def test_a_key_whose_declared_volume_is_absent_is_not_ventilated_and_is_counted() -> None:
    outcome = _run(_group("key_1", 10, None), _group("key_2", 1, 3))
    assert [share.alignment_key for share in outcome.shares] == ["key_2", "key_2"]
    assert [refusal.code for refusal in outcome.refusals] == [REFUSAL_VOLUME_NOT_DECLARED]
    counts = ventilation_counts(outcome)
    assert counts[REFUSAL_VOLUME_NOT_DECLARED] == 1
    assert counts["refused_keys"] == 1
    assert counts["ventilated_keys"] == 1


def test_a_key_whose_declared_volume_is_zero_is_not_split_in_equal_parts() -> None:
    """Equal parts is the answer this story refuses by name."""
    outcome = _run(_group("key_1", 0, 0, 0))
    assert outcome.shares == ()
    assert [refusal.code for refusal in outcome.refusals] == [REFUSAL_VOLUME_IS_ZERO]
    assert "equal parts" in outcome.refusals[0].reason


def test_a_negative_volume_is_refused_rather_than_treated_as_a_share() -> None:
    outcome = _run(_group("key_1", 10, -1))
    assert outcome.shares == ()
    assert [refusal.code for refusal in outcome.refusals] == [REFUSAL_VOLUME_IS_NEGATIVE]


def test_one_missing_volume_refuses_the_WHOLE_key_and_not_just_that_candidate() -> None:
    """Dropping an entity from the denominator hands its share to its neighbours."""
    outcome = _run(_group("key_1", 10, 20, None))
    assert outcome.shares == ()
    assert outcome.refusals[0].candidates == ("entity_a", "entity_b", "entity_c")


def test_a_candidate_that_measured_zero_still_receives_a_share_of_zero() -> None:
    """`None` and `0` are different answers: nobody measured, versus measured nothing."""
    outcome = _run(_group("key_1", 0, 4))
    assert outcome.refusals == ()
    assert {s.right_row_key: s.weight for s in outcome.shares} == {
        "entity_a": Decimal(0),
        "entity_b": Decimal(1),
    }


def test_one_entity_offered_twice_for_a_key_is_refused() -> None:
    """Two shares of one split would be a run the store rejects half of."""
    group = VentilationGroup.of(
        "key_1",
        DAY,
        [
            VentilationCandidate(row_key="entity_a", volume=1),
            VentilationCandidate(row_key="entity_a", volume=3),
        ],
    )
    with pytest.raises(VentilationRefused) as excinfo:
        ventilate([group], basis=BASIS, capability_state=ACTIVE)
    assert excinfo.value.code == "candidate_named_twice"


def test_every_refusal_code_is_counted_at_zero_rather_than_absent() -> None:
    counts = ventilation_counts(_run(_group("key_1", 1, 1)))
    assert counts[REFUSAL_VOLUME_NOT_DECLARED] == 0
    assert counts[REFUSAL_VOLUME_IS_ZERO] == 0
    assert counts[REFUSAL_VOLUME_IS_NEGATIVE] == 0


def test_every_refusal_names_the_gesture_that_lifts_it() -> None:
    outcome = _run(
        _group("key_1", 10, None), _group("key_2", 0, 0), _group("key_3", 1, -2)
    )
    assert len(outcome.refusals) == 3
    for refusal in outcome.refusals:
        assert refusal.gesture.strip()
        assert refusal.reason.strip()


# ---------------------------------------------------------------------------
# The declared volume. No default, ever, and it is named on every share.
# ---------------------------------------------------------------------------


def test_a_basis_with_no_volume_is_refused_at_declaration() -> None:
    with pytest.raises(VentilationRefused) as excinfo:
        VentilationBasis(volume_name="   ", declared_version="v1")
    assert excinfo.value.code == REFUSAL_VOLUME_NOT_DECLARED


def test_a_declared_volume_carries_the_version_that_published_it() -> None:
    with pytest.raises(VentilationRefused) as excinfo:
        VentilationBasis(volume_name="clicks", declared_version="")
    assert excinfo.value.code == "basis_without_a_version"


def test_there_is_no_default_volume_anywhere_in_the_signature() -> None:
    """`ventilate` cannot be called without saying what it is proportional to."""
    with pytest.raises(TypeError):
        ventilate([_group("key_1", 1, 1)], capability_state=ACTIVE)  # type: ignore[call-arg]


def test_every_ventilated_row_names_the_declared_volume_in_its_provenance() -> None:
    share = _run(_group("key_1", 3, 1)).shares[0]
    columns = share.columns()
    assert columns[VOLUME_COLUMN] == "clicks"
    assert columns[VOLUME_VERSION_COLUMN] == "v1"
    assert columns[VOLUME_VALUE_COLUMN] == Decimal(3)
    assert columns[VOLUME_TOTAL_COLUMN] == Decimal(4)
    assert columns[WEIGHT_COLUMN] == Decimal("0.75")
    assert columns[RESIDUAL_COLUMN] is True
    # And the provenance carries no metric: a weight is not a figure.
    assert set(columns) == set(VENTILATION_COLUMNS)


# ---------------------------------------------------------------------------
# The central refusal: observed and ventilated are DIFFERENT columns.
# ---------------------------------------------------------------------------


def test_a_ventilated_metric_gets_a_column_of_its_own() -> None:
    assert ventilated_metric_column("conversions") == "conversions_ventilated"


def test_a_share_of_a_share_is_refused_at_the_moment_the_name_is_derived() -> None:
    with pytest.raises(VentilationRefused) as excinfo:
        ventilated_metric_column("conversions_ventilated")
    assert excinfo.value.code == "metric_already_ventilated"


def test_one_name_emitted_twice_is_refused_as_a_mixed_column() -> None:
    refuse_mixed_columns(["conversions", "conversions_ventilated"])  # the separation working
    with pytest.raises(VentilationRefused) as excinfo:
        refuse_mixed_columns(["conversions", "conversions"])
    assert excinfo.value.code == "observed_and_ventilated_in_one_column"


def test_the_two_blocks_of_a_ventilated_row_never_share_a_column() -> None:
    row = VentilatedRow(
        aligned=AlignedRow(row_key="key_1", state=MATCHING_STATE_AMBIGUOUS, method="name_prefix"),
        observed={"conversions": Decimal("100")},
        share=_run(_group("key_1", 1, 3)).shares[1],
    )
    observed, split = row.observed_columns(), row.ventilated_columns()
    assert set(observed) & set(split) == set()
    # The observed metric is NULL on a ventilated row: the observed figure belongs
    # to the coarse key, and repeating it on each of N rows would multiply it.
    assert observed["conversions"] is None
    assert split["conversions_ventilated"] == Decimal("75")
    assert row.columns()["conversions_ventilated"] == Decimal("75")


def test_a_reader_who_takes_only_the_observed_block_still_gets_a_true_total() -> None:
    """The story's central criterion, as arithmetic.

    Two coarse keys: one the cascade matched one-to-one (observed, attributed),
    one it left ambiguous and the engine split across two entities. Summing the
    OBSERVED column gives the matched figure alone -- never the split added to it.
    """
    matched = AlignedRow(
        row_key="key_1",
        state="matched",
        method=METHOD_ID_EXACT,
        entity_id="entity_x",
        entity_name="Entity X",
    )
    ambiguous = AlignedRow(
        row_key="key_2",
        state=MATCHING_STATE_AMBIGUOUS,
        method="name_prefix",
        candidates=("entity_a", "entity_b"),
    )
    outcome = _run(_group("key_2", 1, 3))
    rows = ventilated_rows(
        [matched, ambiguous],
        outcome=outcome,
        observed_by_key={
            "key_1": {"conversions": Decimal("10")},
            "key_2": {"conversions": Decimal("100")},
        },
        day=DAY,
    )
    assert len(rows) == 3  # one matched row, two ventilated ones

    observed_total = sum(
        row.observed_columns()["conversions"] or Decimal(0) for row in rows
    )
    ventilated_total = sum(
        row.ventilated_columns()["conversions_ventilated"] or Decimal(0) for row in rows
    )
    assert observed_total == Decimal("10")
    assert ventilated_total == Decimal("100")
    # The two are additive only because they are separate. Nothing sums them here,
    # and nothing in the engine ever does.
    assert observed_total + ventilated_total == Decimal("110")


def test_the_three_identity_columns_of_story_70_3_still_travel_untouched() -> None:
    row = VentilatedRow(
        aligned=AlignedRow(
            row_key="key_1",
            state="matched",
            method=METHOD_ID_EXACT,
            entity_id="entity_x",
            entity_name="Entity X",
        ),
        observed={"conversions": Decimal("10")},
    )
    columns = row.observed_columns()
    assert columns[ENTITY_ID_COLUMN] == "entity_x"
    assert columns[ENTITY_NAME_COLUMN] == "Entity X"
    assert columns[METHOD_COLUMN] == METHOD_ID_EXACT
    assert columns["conversions"] == Decimal("10")


def test_a_row_the_cascade_matched_is_never_ventilated() -> None:
    """Splitting an attributed figure would replace a fact with an estimate."""
    matched = AlignedRow(
        row_key="key_1",
        state="matched",
        method=METHOD_ID_EXACT,
        entity_id="entity_x",
        entity_name="Entity X",
    )
    with pytest.raises(VentilationRefused) as excinfo:
        ventilated_rows(
            [matched],
            outcome=_run(_group("key_1", 1, 3)),
            observed_by_key={"key_1": {"conversions": Decimal("100")}},
            day=DAY,
        )
    assert excinfo.value.code == "matched_row_was_ventilated"


def test_a_row_nothing_split_keeps_the_ventilation_columns_at_null() -> None:
    """The names stay, the values are `None`: a table whose columns come and go is not one."""
    row = VentilatedRow(
        aligned=AlignedRow(row_key="key_9", state=MATCHING_STATE_UNMATCHED),
        observed={"conversions": Decimal("5")},
    )
    split = row.ventilated_columns()
    assert set(VENTILATION_COLUMNS) <= set(split)
    assert all(split[name] is None for name in VENTILATION_COLUMNS)
    assert split["conversions_ventilated"] is None


# ---------------------------------------------------------------------------
# Off splits nothing, and that is checkable.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["disabled", "draft", "blocked", "unset", ""])
def test_the_capability_being_off_ventilates_nothing(state: str) -> None:
    outcome = _run(_group("key_1", 1, 3), state=state)
    assert outcome.ran is False
    assert outcome.shares == ()
    assert outcome.refusals == ()
    assert ventilation_counts(outcome)["ventilated_rows"] == 0


@pytest.mark.parametrize("state", ["disabled", "draft", "blocked", "unset", ""])
def test_the_capability_being_off_names_no_ventilated_column(state: str) -> None:
    assert ventilation_columns(state, ["conversions"]) == ()


@pytest.mark.parametrize("state", ["ready", "degraded"])
def test_the_capability_being_on_names_the_provenance_and_the_split_metrics(
    state: str,
) -> None:
    columns = ventilation_columns(state, ["conversions", "revenue_micros"])
    assert set(VENTILATION_COLUMNS) <= set(columns)
    assert "conversions_ventilated" in columns
    assert "revenue_micros_ventilated" in columns
    # And no observed metric name leaks into the ventilated block.
    assert "conversions" not in columns


def test_a_run_that_did_not_happen_is_not_a_run_where_everything_was_refused() -> None:
    off = _run(_group("key_1", 0, 0), state="disabled")
    on = _run(_group("key_1", 0, 0), state=ACTIVE)
    assert off.ran is False and off.refusals == ()
    assert on.ran is True and len(on.refusals) == 1
