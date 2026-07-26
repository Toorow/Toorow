"""Story 37.8 -- markets defined by the client.

The platform holds no opinion on what a market contains.  These tests pin the
degree of freedom (any member list is accepted), the four invariants that *are*
imposed (disjunction, non-emptiness, unique stable ids, known vocabulary), and
the fact that nothing downstream leaks a raw ISO code where a market label
belongs.
"""

from __future__ import annotations

import copy

import pytest
from core.geographic_reporting import (
    LOCAL_MARKETS,
    GeographicPosture,
    InvalidGeographicPosture,
    Market,
    bindable_markets,
    market_diff,
    markets_from_country_codes,
    normalize_geographic_posture,
    posture_from_stored_row,
    resolve_market_binding,
)
from core.geographic_semantics import (
    OTHER_MARKETS,
    UNKNOWN_MARKET,
    group_market_reporting_rows,
    market_bucket_descriptors,
)

_VOCABULARY = {"FR", "MC", "GP", "DE", "AT", "CH", "US", "CA", "BE", "NL"}


def _market(market_id: str, label: str, *codes: str) -> dict:
    return {"id": market_id, "label": label, "country_codes": list(codes)}


def _posture(*markets: dict) -> GeographicPosture:
    return normalize_geographic_posture(
        LOCAL_MARKETS, (), _VOCABULARY, local_markets=list(markets)
    )


def _row(country: object, value: float, *, metric: str = "cost", **extra: object) -> dict:
    return {
        "date": "2026-07-01",
        "connector": "example",
        "metric": metric,
        "breakdown_dimension": "country",
        "breakdown_value": country,
        "value": value,
        "pull_id": "pull-1",
        **extra,
    }


# ---------------------------------------------------------------------------
# The aggregate accepts ANY composition satisfying the invariants
# ---------------------------------------------------------------------------


def test_the_same_label_may_mean_different_things_in_two_projects() -> None:
    """Both definitions of "France" are valid; neither is a default or a correction."""

    narrow = _posture(_market("fr", "France", "FR"))
    wide = _posture(_market("fr", "France", "FR", "MC", "GP"))

    assert narrow.markets[0].country_codes == ("FR",)
    assert wide.markets[0].country_codes == ("FR", "GP", "MC")
    # The platform never widened the narrow one, nor narrowed the wide one.
    assert narrow.country_codes == ("FR",)
    assert wide.country_codes == ("FR", "GP", "MC")


def test_a_single_country_market_is_first_class_not_a_degenerate_group() -> None:
    posture = _posture(_market("fr", "France", "FR"))
    (market,) = posture.markets

    assert market.is_single_country is True
    assert market.single_country_code == "FR"
    # No group machinery, no suggested neighbour, no attached territory.
    assert market.country_codes == ("FR",)
    assert posture.country_codes == ("FR",)


def test_multi_country_market_is_accepted_without_judgement() -> None:
    posture = _posture(_market("dach", "DACH", "DE", "AT", "CH"))

    assert posture.markets[0].country_codes == ("AT", "CH", "DE")
    assert posture.markets[0].is_single_country is False


# ---------------------------------------------------------------------------
# The only invariants the platform imposes
# ---------------------------------------------------------------------------


def test_overlapping_markets_are_rejected_with_an_actionable_error() -> None:
    with pytest.raises(InvalidGeographicPosture) as exc:
        _posture(_market("fr", "France", "FR", "MC"), _market("south", "South", "MC"))

    message = str(exc.value)
    assert "MC" in message
    assert "'fr'" in message and "'south'" in message
    assert "at most one market" in message
    assert "remove it from one of them" in message


def test_empty_market_and_empty_market_list_are_rejected() -> None:
    with pytest.raises(InvalidGeographicPosture, match="at least one country code"):
        _posture(_market("fr", "France"))
    with pytest.raises(InvalidGeographicPosture, match="at least one tracked market"):
        normalize_geographic_posture(LOCAL_MARKETS, (), _VOCABULARY, local_markets=[])


def test_duplicate_market_id_is_rejected_case_insensitively() -> None:
    with pytest.raises(InvalidGeographicPosture, match="duplicate market id"):
        _posture(_market("fr", "France", "FR"), _market("FR", "Hexagone", "DE"))


def test_unknown_country_code_and_malformed_id_or_label_are_rejected() -> None:
    with pytest.raises(InvalidGeographicPosture, match="unsupported canonical country code"):
        _posture(_market("zz", "Nowhere", "ZZ"))
    with pytest.raises(InvalidGeographicPosture, match="invalid ISO alpha-2"):
        _posture(_market("fr", "France", "FRA"))
    with pytest.raises(InvalidGeographicPosture, match="invalid market id"):
        _posture(_market("fr!", "France", "FR"))
    with pytest.raises(InvalidGeographicPosture, match="non-empty 'label'"):
        _posture({"id": "fr", "label": "  ", "country_codes": ["FR"]})


def test_no_preset_or_default_market_is_ever_produced() -> None:
    """A Global project holds no market at all, and nothing is seeded."""

    posture = normalize_geographic_posture("global", ["FR"], _VOCABULARY)

    assert posture.markets == ()
    assert posture.country_codes == ()
    assert market_bucket_descriptors(posture) == []


# ---------------------------------------------------------------------------
# Backward compatibility: flat codes read as single-country markets
# ---------------------------------------------------------------------------


def test_flat_country_codes_read_as_equivalent_single_country_markets() -> None:
    legacy = normalize_geographic_posture(LOCAL_MARKETS, ["fr", "DE"], _VOCABULARY)

    assert legacy.markets == (
        Market(id="DE", label="DE", country_codes=("DE",)),
        Market(id="FR", label="FR", country_codes=("FR",)),
    )
    assert legacy.country_codes == ("DE", "FR")
    assert markets_from_country_codes(["DE", "FR"]) == legacy.markets


def test_stored_legacy_row_reads_as_markets_with_unchanged_reporting_output() -> None:
    rows = [_row("FR", 10), _row("Germany", 20), _row("US", 30), _row("Atlantis", 4)]

    legacy = posture_from_stored_row(LOCAL_MARKETS, ["FR", "DE"], None)
    migrated = posture_from_stored_row(
        LOCAL_MARKETS,
        ["FR", "DE"],
        [_market("FR", "FR", "FR"), _market("DE", "DE", "DE")],
    )

    before = group_market_reporting_rows(rows, legacy)
    after = group_market_reporting_rows(rows, migrated)

    assert [dict(row) for row in before.rows] == [dict(row) for row in after.rows]
    assert {row["breakdown_value"] for row in before.rows} == {
        "FR",
        "DE",
        OTHER_MARKETS,
        UNKNOWN_MARKET,
    }
    # Historical payload field kept for pre-37.8 readers.
    tracked = {row["breakdown_value"]: row for row in before.rows}
    assert tracked["FR"]["market_code"] == "FR"
    assert tracked["FR"]["market_label"] == "FR"


def test_stored_market_rows_win_over_the_derived_flat_column() -> None:
    posture = posture_from_stored_row(
        LOCAL_MARKETS,
        ["FR", "MC"],
        [_market("fr", "France", "FR", "MC")],
    )

    assert [market.id for market in posture.markets] == ["fr"]
    assert posture.country_codes == ("FR", "MC")


# ---------------------------------------------------------------------------
# Semantic grouping over multi-country markets
# ---------------------------------------------------------------------------


def test_multi_country_market_aggregates_into_one_bucket_and_reconciles_exactly() -> None:
    posture = _posture(
        _market("fr", "France", "FR", "MC"),
        _market("dach", "DACH", "DE", "AT", "CH"),
    )
    rows = [
        _row("FR", 10),
        _row("MC", 5),
        _row("Germany", 20),
        _row("AT", 2),
        _row("US", 30),
        _row("CA", 7),
        _row("Atlantis", 4),
        _row(None, 6),
    ]
    original = copy.deepcopy(rows)

    result = group_market_reporting_rows(rows, posture)

    # Facts are never rewritten.
    assert rows == original
    values = {row["breakdown_value"]: row["value"] for row in result.rows}
    assert values == {
        "fr": 15.0,
        "dach": 22.0,
        OTHER_MARKETS: 37.0,
        UNKNOWN_MARKET: 10.0,
    }
    # Additive reconciliation is EXACT: markets + Other + Unknown = canonical total.
    assert sum(values.values()) == sum(float(row["value"]) for row in rows)

    grouped = {row["breakdown_value"]: row for row in result.rows}
    assert grouped["fr"]["market_label"] == "France"
    assert grouped["fr"]["market_id"] == "fr"
    assert grouped["fr"]["market_country_codes"] == ["FR", "MC"]
    # A multi-country market has no single code to expose.
    assert grouped["fr"]["market_code"] is None
    assert grouped["dach"]["market_kind"] == "tracked"
    assert grouped[UNKNOWN_MARKET]["market_kind"] == "unknown"
    assert grouped[OTHER_MARKETS]["market_kind"] == "other"
    assert {item["raw_value"] for item in result.data_quality} == {"Atlantis", None}


def test_unmapped_values_never_fall_into_other_markets() -> None:
    posture = _posture(_market("fr", "France", "FR"))

    result = group_market_reporting_rows([_row("Atlantis", 4)], posture)

    assert result.rows[0]["breakdown_value"] == UNKNOWN_MARKET
    assert result.rows[0]["market_kind"] == "unknown"


def test_non_additive_rule_applies_after_market_grouping() -> None:
    posture = _posture(_market("fr", "France", "FR", "MC"))
    rows = [
        _row("FR", 0.5, metric="ctr", semantic_numerator=10, semantic_denominator=20),
        _row("MC", 0.1, metric="ctr", semantic_numerator=10, semantic_denominator=100),
    ]

    result = group_market_reporting_rows(rows, posture)

    assert len(result.rows) == 1
    assert result.rows[0]["market_id"] == "fr"
    # 20/120, not the mean of the two per-country ratios.
    assert result.rows[0]["value"] == pytest.approx(20 / 120)
    assert result.rows[0]["semantic_aggregation_rule"] == "ratio"


def test_regrouping_reclassifies_retained_rows_without_rewriting_them() -> None:
    rows = [_row("FR", 10), _row("MC", 5)]
    original = copy.deepcopy(rows)

    split = group_market_reporting_rows(
        rows, _posture(_market("fr", "France", "FR"), _market("mc", "Monaco", "MC"))
    )
    merged = group_market_reporting_rows(rows, _posture(_market("fr", "France", "FR", "MC")))

    assert {row["breakdown_value"]: row["value"] for row in split.rows} == {
        "fr": 10.0,
        "mc": 5.0,
    }
    assert {row["breakdown_value"]: row["value"] for row in merged.rows} == {"fr": 15.0}
    # Same retained facts, no provider pull, no rewrite.
    assert rows == original


# ---------------------------------------------------------------------------
# Bucket descriptors and bindings
# ---------------------------------------------------------------------------


def test_bucket_descriptors_carry_id_label_and_member_codes() -> None:
    posture = _posture(_market("fr", "France", "FR", "MC"))

    descriptors = market_bucket_descriptors(posture)

    assert descriptors[0] == {
        "id": "fr",
        "label": "France",
        "kind": "tracked",
        "country_codes": ["FR", "MC"],
        "country_code": None,
        "bindable": True,
    }
    assert [item["kind"] for item in descriptors] == ["tracked", "other", "unknown"]
    assert [item["bindable"] for item in descriptors] == [True, False, False]


def test_binding_is_on_the_market_id_and_survives_a_relabel() -> None:
    before = _posture(_market("fr", "France", "FR", "MC"))
    after = _posture(_market("fr", "France metropolitaine + Monaco", "FR", "MC"))

    bound = resolve_market_binding(before, "fr")
    still_bound = resolve_market_binding(after, "fr")

    assert bound.id == still_bound.id == "fr"
    assert bound.label != still_bound.label
    assert still_bound.country_codes == ("FR", "MC")


def test_other_markets_and_unknown_are_never_bindable() -> None:
    posture = _posture(_market("hexagone", "France", "FR", "MC"))

    assert [market.id for market in bindable_markets(posture)] == ["hexagone"]
    for bucket_id in (OTHER_MARKETS, UNKNOWN_MARKET):
        with pytest.raises(InvalidGeographicPosture, match="not a budgetable market"):
            resolve_market_binding(posture, bucket_id)
    with pytest.raises(InvalidGeographicPosture, match="unknown market id"):
        # A raw country code is not a binding handle: bindings are on market ids.
        resolve_market_binding(posture, "FR")


def test_bindable_markets_is_empty_in_global_mode() -> None:
    assert bindable_markets(GeographicPosture()) == ()


# ---------------------------------------------------------------------------
# Audit diff
# ---------------------------------------------------------------------------


def test_market_diff_reports_markets_not_only_codes() -> None:
    previous = _posture(_market("fr", "France", "FR"), _market("bnl", "Benelux", "BE", "NL"))
    target = _posture(
        _market("fr", "Hexagone + Monaco", "FR", "MC"),
        _market("na", "North America", "US", "CA"),
    )

    diff = market_diff(previous, target)

    assert [item["id"] for item in diff["added_markets"]] == ["na"]
    assert [item["id"] for item in diff["removed_markets"]] == ["bnl"]
    assert diff["relabelled_markets"] == [
        {"id": "fr", "previous_label": "France", "new_label": "Hexagone + Monaco"}
    ]
    assert diff["recomposed_markets"] == [
        {
            "id": "fr",
            "label": "Hexagone + Monaco",
            "added_country_codes": ["MC"],
            "removed_country_codes": [],
        }
    ]
    assert diff["added_country_codes"] == ["CA", "MC", "US"]
    assert diff["removed_country_codes"] == ["BE", "NL"]


def test_market_diff_reports_a_country_moved_between_markets() -> None:
    previous = _posture(_market("fr", "France", "FR", "MC"), _market("dach", "DACH", "DE"))
    target = _posture(_market("fr", "France", "FR"), _market("dach", "DACH", "DE", "MC"))

    diff = market_diff(previous, target)

    assert diff["moved_country_codes"] == [
        {"country_code": "MC", "previous_market_id": "fr", "new_market_id": "dach"}
    ]
    # A pure regroup changes no tracked country: nothing to extract or backfill.
    assert diff["added_country_codes"] == []
    assert diff["removed_country_codes"] == []
