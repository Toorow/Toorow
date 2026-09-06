"""Story 37.8 -- markets defined by the client.

The platform holds no opinion on what a market contains.  These tests pin the
degree of freedom (any member list is accepted), the four invariants that *are*
imposed (disjunction, non-emptiness, unique stable ids, known vocabulary), and
the fact that nothing downstream leaks a raw ISO code where a market label
belongs.
"""

from __future__ import annotations

import pytest
from core.geographic_reporting import (
    LOCAL_MARKETS,
    GeographicPosture,
    InvalidGeographicPosture,
    Market,
    market_diff,
    markets_from_country_codes,
    normalize_geographic_posture,
    posture_from_stored_row,
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



def test_stored_market_rows_win_over_the_derived_flat_column() -> None:
    posture = posture_from_stored_row(
        LOCAL_MARKETS,
        ["FR", "MC"],
        [_market("fr", "France", "FR", "MC")],
    )

    assert [market.id for market in posture.markets] == ["fr"]
    assert posture.country_codes == ("FR", "MC")


# ---------------------------------------------------------------------------
# Semantic grouping moved out of this file in Story 48.2.
#
# The tests that lived here drove `group_market_reporting_rows(rows, posture)`
# -- grouping against a mutable preference. That contract is gone: grouping now
# runs against a published hierarchy version. The properties they proved were
# not dropped, they were re-proven against the model that replaced them:
#
#   multi-country market aggregates into one bucket   test_geographic_semantics
#   exact additive reconciliation                     test_geographic_semantics
#   an unmapped value never falls into the catch-all  test_geographic_semantics
#   a non-additive rule applies after grouping        test_geographic_semantics
#   regrouping reclassifies without rewriting facts   test_geographic_semantics
#   descriptors carry id and label                    test_country_registry
#   the catch-all and Unknown are never bindable      test_country_registry
#
# What stays here is what this file is actually about: the client-defined
# posture aggregate, its four invariants and its diff.
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
