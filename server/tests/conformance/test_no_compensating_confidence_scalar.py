"""No surface merges the confidence terms back into one number.

`docs/product-architecture/proactive-assertions.md`, "Incomplete if":

    a single score merges evidence of different natures

and the rule it restates, `overview.md:32-34`: *"Business signals, operational
health and trust/readiness remain visually and semantically separate; they never
collapse into one compensating score."*

WHAT THIS GUARD IS FOR. `core.confidence` shipped
`score = completeness * freshness * provenance`, rounded into a `"score"` key
that travelled to the proactive surface through `meta.confidence`
(`reporting_mcp`, `reports`) and `envelope.build_canonical_envelope`. Removing it
once is a commit; keeping it removed is this file. A scalar over three natures is
the kind of thing that comes back as a convenience -- "the widget needs one
number for the gauge" -- and it comes back looking reasonable.

WHY THE GUARD IS A BEHAVIOUR AND NOT A GREP. A text search for `"score"` in
`core/confidence.py` proves the word is absent, not that the object is: the same
product under the name `overall`, `trust` or `index` would pass it. So the two
producers are CALLED, and every numeric value they return is checked against the
products and averages the three terms could form. `memory: an instrument must
not measure its own copy` -- the assertion runs against the real functions, with
no reimplementation of the rule inside the test.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from core.confidence import compute_confidence
from core.insight_confidence import derive_insight_confidence

#: Deliberately NOT round numbers, and deliberately all different: a product and
#: an average of three distinct values are two distinct numbers, so a scalar
#: built either way is caught, and no term can coincide with a combination of the
#: other two by accident.
_COMPLETENESS = 0.8
_PROVENANCE = 0.5


def _forbidden_combinations(terms: dict[str, float]) -> set[float]:
    """Every scalar the three terms could be collapsed into, to 4 decimals."""
    values = [v for v in terms.values() if v is not None]
    product = 1.0
    for value in values:
        product *= value
    return {
        round(product, 4),
        round(sum(values) / len(values), 4),
        round(min(values) * max(values), 4),
    }


def _numbers(payload) -> list[float]:
    """Every float a returned block carries, at any depth."""
    found: list[float] = []
    stack = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
        elif isinstance(node, bool):
            continue
        elif isinstance(node, (int, float)):
            found.append(float(node))
    return found


def _confidence_with(completeness: float, rows: list[dict]) -> dict:
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone = MagicMock(return_value=(completeness,))
    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    conn.cursor = MagicMock(return_value=cur)

    @contextmanager
    def _ctx(c):
        yield c

    with patch("core.db.get_connection", return_value=_ctx(conn)):
        return compute_confidence(
            "proj_EXAMPLE", ["google-ads"], rows=rows, date_to="2026-07-20"
        )


def test_the_report_confidence_carries_no_scalar_over_its_three_terms():
    rows = [
        {"date": "2026-07-20", "loaded_at": "2026-07-20T06:00:00+00:00", "pull_id": "p"},
        {"date": "2026-07-20", "loaded_at": "2026-07-20T06:00:00+00:00", "pull_id": None},
    ]
    block = _confidence_with(_COMPLETENESS, rows)

    assert block is not None
    assert "score" not in block, (
        "`score` is the compensating scalar `proactive-assertions.md` refuses; "
        "the three terms and `limiting_term` are what a reader gets"
    )
    terms = {
        "completeness": block["completeness"],
        "freshness": block["freshness"],
        "provenance": block["provenance"],
    }
    assert terms["completeness"] == _COMPLETENESS
    assert terms["provenance"] == _PROVENANCE
    forbidden = _forbidden_combinations(terms) - set(terms.values())
    assert not (forbidden & set(_numbers(block))), (
        "a value equal to a product or an average of the three terms reappeared "
        f"in the confidence block: {sorted(forbidden & set(_numbers(block)))}"
    )
    # The only summary the three support, and it is a NAME, not a number.
    assert block["limiting_term"] == "provenance"


def test_the_insight_confidence_carries_no_scalar_over_its_three_terms():
    payload = {
        "insight": {"title": "t", "summary": "s", "confidence": "high"},
        "period": {"dateFrom": "2026-07-20", "dateTo": "2026-07-20"},
        "evidenceRefs": ["metric:conversions", "metric:revenue"],
    }
    rows = [
        {
            "date": "2026-07-20",
            "loaded_at": "2026-07-20T06:00:00+00:00",
            "connector": "google-ads",
            "pull_id": "p",
            "metric": "conversions",
        },
        {
            "date": "2026-07-20",
            "loaded_at": "2026-07-20T06:00:00+00:00",
            "connector": "google-ads",
            "pull_id": None,
            "metric": "conversions",
        },
    ]
    block = derive_insight_confidence(payload, rows=rows)

    terms = block["terms"]
    assert terms["completeness"] == 0.5
    assert terms["provenance"] == 0.5
    forbidden = _forbidden_combinations(terms) - set(terms.values())
    assert not (forbidden & set(_numbers(block))), (
        "the insight reading collapsed its terms into one number: "
        f"{sorted(forbidden & set(_numbers(block)))}"
    )
    # The reading is a BAND OF ONE TERM, and the term is named.
    assert block["reading"] == "low"
    assert block["limitingTerm"] in terms
