"""Story 37.9: the used-by guard on markets.

Deleting a market or moving a country out of one must first STATE what depends
on it. A relabel must not: bindings are on the stable id, which is exactly what
makes a relabel safe (Story 37.8).
"""

from __future__ import annotations

import pytest
from core.geographic_reporting import GeographicPosture, Market
from core.market_governance import (
    IMPACT_COUNTRY_ADDED,
    IMPACT_COUNTRY_REMOVED,
    IMPACT_MARKET_RELABELLED,
    IMPACT_MARKET_REMOVED,
    MarketBinding,
    MarketUsageBlocked,
    assert_market_change_acknowledged,
    assess_market_change,
    usage_report,
)

LOCAL = "local_markets"


def _posture(*markets: Market) -> GeographicPosture:
    return GeographicPosture(mode=LOCAL, markets=markets)


BUDGET = MarketBinding(
    market_id="hexagone",
    binding_kind="budget",
    binding_id="bud_1",
    binding_label="Q3 budget",
)
OBJECTIVE = MarketBinding(
    market_id="hexagone", binding_kind="objective", binding_id="obj_1"
)


def test_no_binding_means_no_impact() -> None:
    before = _posture(Market(id="hexagone", label="Hexagone", country_codes=("FR",)))
    after = _posture(Market(id="dach", label="DACH", country_codes=("DE",)))
    assert assess_market_change(before, after, []) == ()


def test_deleting_a_bound_market_is_reported_and_blocking() -> None:
    before = _posture(Market(id="hexagone", label="Hexagone", country_codes=("FR",)))
    after = _posture(Market(id="dach", label="DACH", country_codes=("DE",)))

    impacts = assess_market_change(before, after, [BUDGET, OBJECTIVE])
    assert [i.reason for i in impacts] == [IMPACT_MARKET_REMOVED]
    assert impacts[0].blocking is True
    # It SAYS what depends on it, by kind and id.
    kinds = {b.binding_kind for b in impacts[0].bindings}
    assert kinds == {"budget", "objective"}


def test_moving_a_country_out_of_a_bound_market_is_blocking() -> None:
    before = _posture(
        Market(id="hexagone", label="Hexagone", country_codes=("FR", "MC"))
    )
    after = _posture(Market(id="hexagone", label="Hexagone", country_codes=("FR",)))

    impacts = assess_market_change(before, after, [BUDGET])
    assert [i.reason for i in impacts] == [IMPACT_COUNTRY_REMOVED]
    assert impacts[0].detail["removed_country_codes"] == ["MC"]
    assert impacts[0].blocking is True


def test_adding_a_country_to_a_bound_market_is_blocking_too() -> None:
    """Widening a market inflates figures published under the same id."""

    before = _posture(Market(id="hexagone", label="Hexagone", country_codes=("FR",)))
    after = _posture(Market(id="hexagone", label="Hexagone", country_codes=("FR", "MC")))

    impacts = assess_market_change(before, after, [BUDGET])
    assert [i.reason for i in impacts] == [IMPACT_COUNTRY_ADDED]
    assert impacts[0].detail["added_country_codes"] == ["MC"]
    assert impacts[0].blocking is True


def test_relabelling_a_bound_market_is_reported_but_never_blocking() -> None:
    before = _posture(Market(id="hexagone", label="Hexagone", country_codes=("FR",)))
    after = _posture(Market(id="hexagone", label="France", country_codes=("FR",)))

    impacts = assess_market_change(before, after, [BUDGET])
    assert [i.reason for i in impacts] == [IMPACT_MARKET_RELABELLED]
    assert impacts[0].blocking is False
    assert usage_report(impacts)["acknowledgement_required"] is False


def test_an_unbound_market_change_needs_no_acknowledgement() -> None:
    before = _posture(Market(id="dach", label="DACH", country_codes=("DE",)))
    after = _posture(Market(id="dach", label="DACH", country_codes=("DE", "AT")))
    impacts = assess_market_change(before, after, [BUDGET])
    assert impacts == ()
    assert_market_change_acknowledged(impacts, acknowledged=False)  # must not raise


def test_unacknowledged_meaning_change_is_refused_with_its_dependents() -> None:
    before = _posture(Market(id="hexagone", label="Hexagone", country_codes=("FR", "MC")))
    after = _posture(Market(id="hexagone", label="Hexagone", country_codes=("FR",)))
    impacts = assess_market_change(before, after, [BUDGET])

    with pytest.raises(MarketUsageBlocked) as excinfo:
        assert_market_change_acknowledged(impacts, acknowledged=False)
    assert excinfo.value.impacts
    assert "hexagone" in str(excinfo.value)


def test_acknowledged_meaning_change_proceeds() -> None:
    before = _posture(Market(id="hexagone", label="Hexagone", country_codes=("FR", "MC")))
    after = _posture(Market(id="hexagone", label="Hexagone", country_codes=("FR",)))
    impacts = assess_market_change(before, after, [BUDGET])
    assert_market_change_acknowledged(impacts, acknowledged=True)  # must not raise


def test_bindings_accept_plain_dict_rows_from_the_registry() -> None:
    before = _posture(Market(id="hexagone", label="Hexagone", country_codes=("FR",)))
    after = _posture(Market(id="dach", label="DACH", country_codes=("DE",)))
    impacts = assess_market_change(
        before,
        after,
        [
            {
                "market_id": "HEXAGONE",  # ids compare case-insensitively
                "binding_kind": "saved_report",
                "binding_id": "rep_1",
            },
            {"market_id": "", "binding_kind": "x", "binding_id": "y"},  # ignored
        ],
    )
    assert len(impacts) == 1
    assert impacts[0].bindings[0].binding_kind == "saved_report"


def test_usage_report_states_the_consequence_in_words() -> None:
    before = _posture(Market(id="hexagone", label="Hexagone", country_codes=("FR",)))
    after = _posture(Market(id="dach", label="DACH", country_codes=("DE",)))
    report = usage_report(assess_market_change(before, after, [BUDGET]))
    assert report["acknowledgement_required"] is True
    assert report["blocking_impact_count"] == 1
    assert report["bound_market_ids"] == ["hexagone"]
    assert "already" in report["statement"]
