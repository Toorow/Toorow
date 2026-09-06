"""The confidence of a published insight is MEASURED, not declared.

`proactive-assertions.md` ("Incomplete if"): *a confidence level is declared by
the author of the claim rather than derived*. Story 53.4 made the declaration
visible; this proves the derivation that replaces it, and -- as much -- proves
the three ways it refuses to invent one.

WHAT WOULD MAKE THESE TESTS WORTHLESS. A derivation that quietly returns a
default level when it cannot measure passes every "it returns a reading" test
ever written. So four of the cases below assert the ABSENCE of a level:
unreadable period, no refs, refs that resolve against nothing, and a term that
cannot be taken. Each one must read `unmeasurable`, and `unmeasurable` is not a
member of the level scale.
"""

from __future__ import annotations

import pytest
from core.insight_confidence import (
    READING_LEVELS,
    READING_UNMEASURABLE,
    derive_insight_confidence,
    term_reading,
)


def _rows(**over):
    base = dict(
        date="2026-07-20",
        loaded_at="2026-07-20T06:00:00+00:00",
        connector="google-ads",
        pull_id="pull_EXAMPLE",
        metric="conversions",
        breakdown_dimension=None,
    )
    base.update(over)
    return base


def _payload(refs=("metric:conversions",), date_from="2026-07-20", date_to="2026-07-20"):
    return {
        "insight": {"title": "t", "summary": "s", "confidence": "high"},
        "period": {"dateFrom": date_from, "dateTo": date_to},
        "evidenceRefs": list(refs),
    }


def test_a_fully_backed_insight_reads_high_and_names_its_terms():
    block = derive_insight_confidence(_payload(), rows=[_rows()])

    assert block["reading"] == "high"
    assert block["terms"] == {"completeness": 1.0, "freshness": 1.0, "provenance": 1.0}
    assert block["citedMembers"] == ["metric:conversions"]
    assert block["unresolvedRefs"] == []
    # The weakest term is named even when all three agree: a reader who sees only
    # a word cannot tell which question the reading answers.
    assert block["limitingTerm"] in block["terms"]


def test_the_reading_is_the_limiting_term_and_never_a_blend():
    """The sibling clause -- *a single score merges evidence of different natures*
    -- binds this derivation too.

    Two of three terms perfect and one at 0.5 must read `low`. A product would
    read 0.5, an average 0.83; both let completeness buy back traceability.
    """
    rows = [_rows(), _rows(pull_id=None)]
    block = derive_insight_confidence(_payload(), rows=rows)

    assert block["terms"]["completeness"] == 1.0
    assert block["terms"]["freshness"] == 1.0
    assert block["terms"]["provenance"] == 0.5
    assert block["reading"] == "low"
    assert block["limitingTerm"] == "provenance"


def test_a_partly_backed_citation_lowers_completeness_and_names_the_gap():
    payload = _payload(refs=("metric:conversions", "metric:revenue", "dimension:device"))
    block = derive_insight_confidence(payload, rows=[_rows()])

    assert block["terms"]["completeness"] == round(1 / 3, 4)
    assert block["reading"] == "low"
    assert block["limitingTerm"] == "completeness"
    assert block["unresolvedRefs"] == ["dimension:device", "metric:revenue"]


def test_rows_outside_the_insights_own_period_do_not_support_it():
    """The run reads a seven-day lookback; the insight names ONE day.

    Measuring the claim over the lookback would let a member the server measured
    last Tuesday back a claim about today.
    """
    block = derive_insight_confidence(
        _payload(date_from="2026-07-20", date_to="2026-07-20"),
        rows=[_rows(date="2026-07-14")],
    )

    assert block["reading"] == READING_UNMEASURABLE
    assert block["unresolvedRefs"] == ["metric:conversions"]


def test_a_dimension_ref_resolves_on_the_breakdown_column():
    block = derive_insight_confidence(
        _payload(refs=("dimension:device",)),
        rows=[_rows(metric=None, breakdown_dimension="device")],
    )

    assert block["reading"] == "high"
    assert block["citedMembers"] == ["dimension:device"]


@pytest.mark.parametrize(
    "payload, rows",
    [
        pytest.param(_payload(date_from="", date_to=""), [_rows()], id="no_period"),
        pytest.param(_payload(refs=()), [_rows()], id="no_refs"),
        pytest.param(_payload(), [], id="no_rows"),
        pytest.param(_payload(refs=("nonsense",)), [_rows()], id="malformed_ref"),
        pytest.param(
            _payload(),
            [_rows(loaded_at=None)],
            id="unknown_freshness",
        ),
    ],
)
def test_nothing_measurable_yields_no_level_at_all(payload, rows):
    """THE ONE THAT MATTERS. Every path that cannot measure returns
    `unmeasurable`, and `unmeasurable` is not a level.

    A default of `medium` here would reproduce the exact object this module was
    written to remove: a confidence word nobody stands behind, now wearing the
    server's authority instead of the model's.
    """
    block = derive_insight_confidence(payload, rows=rows)

    assert block["reading"] == READING_UNMEASURABLE
    assert block["reading"] not in READING_LEVELS
    assert block["reason"], "an unmeasurable reading always says why"
    # The shape never changes, so no surface has to branch on a missing key to
    # discover it is looking at an unmeasured claim.
    assert set(block) >= {"terms", "limitingTerm", "unknownTerms", "citedMembers"}


def test_the_declared_word_is_never_read_by_the_derivation():
    """The model may declare anything; the measurement does not move."""
    low = derive_insight_confidence(_payload(), rows=[_rows()])
    shouting = _payload()
    shouting["insight"]["confidence"] = "low"
    assert derive_insight_confidence(shouting, rows=[_rows()]) == low


def test_the_band_boundaries_are_inclusive_and_unknown_is_not_a_band():
    assert term_reading(1.0) == "high"
    assert term_reading(0.9) == "high"
    assert term_reading(0.6) == "medium"
    assert term_reading(0.59) == "low"
    assert term_reading(0.0) == "low"
    assert term_reading(None) == READING_UNMEASURABLE
