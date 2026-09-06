"""Story 65.9 -- deterministic Golden Question v2 Result comparison."""

from __future__ import annotations

import pytest
from core.result_assertion_evaluator import evaluate_result_assertions


def _evaluate(assertions, *, rows=None, outcome="success", truncated=False):
    projected_rows = rows if rows is not None else []
    return evaluate_result_assertions(
        assertions,
        result={
            "outcome": outcome,
            "rows": projected_rows,
            "row_count": len(projected_rows),
            "truncated": truncated,
        },
    )


def test_value_uses_decimal_and_relative_tolerance_without_binary_float_drift():
    result = _evaluate(
        [
            {
                "assertion_type": "value",
                "selectors": [{"field": "market", "operator": "eq", "value": "FR"}],
                "field": "spend",
                "operator": "equals",
                "expected": 0.3,
                "tolerance": {"kind": "numeric", "mode": "relative", "amount": 0.001},
            }
        ],
        rows=[{"market": "FR", "spend": 0.1 + 0.2}],
    )[0]
    assert result["verdict"] == "pass"
    assert result["reason_code"] == "assertion_matched"


def test_exact_numeric_and_timestamp_values_use_canonical_value_semantics():
    numeric = {
        "assertion_type": "value",
        "selectors": [],
        "field": "amount",
        "operator": "equals",
        "expected": 1,
        "tolerance": None,
    }
    timestamp = {**numeric, "field": "at", "expected": "2026-08-11T12:00:00Z"}
    assert _evaluate([numeric], rows=[{"amount": 1.0}])[0]["verdict"] == "pass"
    assert (
        _evaluate([timestamp], rows=[{"at": "2026-08-11T14:00:00+02:00"}])[0]["verdict"] == "pass"
    )


def test_timestamp_value_requires_timezone_and_normalizes_to_utc():
    assertion = {
        "assertion_type": "value",
        "selectors": [],
        "field": "at",
        "operator": "equals",
        "expected": "2026-08-11T10:00:00Z",
        "tolerance": {"kind": "temporal", "seconds": 0},
    }
    assert (
        _evaluate([assertion], rows=[{"at": "2026-08-11T12:00:00+02:00"}])[0]["verdict"] == "pass"
    )
    assert _evaluate([assertion], rows=[{"at": "2026-08-11T10:00:00"}])[0] == {
        **_evaluate([assertion], rows=[{"at": "2026-08-11T10:00:00"}])[0],
        "verdict": "unverifiable",
        "reason_code": "malformed_timestamp_evidence",
    }


def test_row_set_compares_canonical_selected_rows_with_explicit_set_tolerance():
    assertion = {
        "assertion_type": "row_set",
        "selectors": [{"field": "kind", "operator": "eq", "value": "paid"}],
        "fields": ["market", "spend"],
        "operator": "equals",
        "expected_rows": [
            {"spend": 10, "market": "FR"},
            {"market": "DE", "spend": 20},
        ],
        "tolerance": {"kind": "set", "max_missing": 0, "max_extra": 1},
    }
    result = _evaluate(
        [assertion],
        rows=[
            {"kind": "paid", "market": "DE", "spend": 20},
            {"kind": "paid", "market": "FR", "spend": 10},
            {"kind": "paid", "market": "ES", "spend": 5},
            {"kind": "organic", "market": "US", "spend": 0},
        ],
    )[0]
    assert result["verdict"] == "pass"
    assert result["evidence_refs"]["missing"] == 0
    assert result["evidence_refs"]["extra"] == 1


def test_missing_malformed_or_truncated_result_evidence_is_unverifiable():
    assertion = {
        "assertion_type": "cardinality",
        "selectors": [],
        "operator": "equals",
        "expected": 1,
        "tolerance": None,
    }
    assert _evaluate([assertion], rows=None, truncated=True)[0]["verdict"] == "unverifiable"
    assert (
        evaluate_result_assertions([assertion], result={"outcome": "success"})[0]["verdict"]
        == "unverifiable"
    )
    incomplete = evaluate_result_assertions(
        [assertion],
        result={"outcome": "success", "rows": [], "row_count": 1, "truncated": False},
    )[0]
    assert incomplete["verdict"] == "unverifiable"
    assert incomplete["reason_code"] == "result_rows_incomplete"


def test_ordering_and_invariant_require_non_empty_selected_evidence():
    assertions = [
        {
            "assertion_type": "ordering",
            "selectors": [],
            "fields": ["rank"],
            "operator": "ascending",
            "tolerance": None,
        },
        {
            "assertion_type": "invariant",
            "selectors": [],
            "fields": ["market"],
            "operator": "unique",
            "tolerance": None,
        },
    ]
    receipts = _evaluate(assertions, rows=[])
    assert [item["verdict"] for item in receipts] == ["unverifiable", "unverifiable"]
    assert [item["reason_code"] for item in receipts] == ["selection_empty", "selection_empty"]


def test_ordering_cardinality_invariant_and_result_states_are_independent():
    assertions = [
        {
            "assertion_type": "ordering",
            "selectors": [],
            "fields": ["rank"],
            "operator": "ascending",
            "tolerance": None,
        },
        {
            "assertion_type": "cardinality",
            "selectors": [],
            "operator": "equals",
            "expected": 2,
            "tolerance": None,
        },
        {
            "assertion_type": "invariant",
            "selectors": [],
            "fields": ["market"],
            "operator": "unique",
            "tolerance": None,
        },
        {"assertion_type": "degraded", "selectors": [], "operator": "is", "tolerance": None},
    ]
    verdicts = _evaluate(
        assertions,
        rows=[{"rank": 1, "market": "FR"}, {"rank": 2, "market": "DE"}],
        outcome="degraded",
    )
    assert [entry["verdict"] for entry in verdicts] == ["pass", "pass", "pass", "pass"]


def test_ordering_compares_numbers_as_decimals_and_timestamps_in_utc():
    numeric = {
        "assertion_type": "ordering",
        "selectors": [],
        "fields": ["amount"],
        "operator": "ascending",
        "tolerance": None,
    }
    temporal = {**numeric, "fields": ["at"]}
    assert _evaluate([numeric], rows=[{"amount": 2}, {"amount": 10}])[0]["verdict"] == "pass"
    assert (
        _evaluate(
            [temporal],
            rows=[
                {"at": "2026-08-11T09:00:00Z"},
                {"at": "2026-08-11T12:00:00+02:00"},
            ],
        )[0]["verdict"]
        == "pass"
    )


@pytest.mark.parametrize(
    "rows",
    [
        [{"value": 2}, {"value": "10"}],
        [{"value": "2026-08-11T09:00:00Z"}, {"value": "not-a-timestamp"}],
        [{"value": {"rank": 1}}, {"value": [2]}],
    ],
)
def test_ordering_refuses_heterogeneous_comparable_classes(rows):
    assertion = {
        "assertion_type": "ordering",
        "selectors": [],
        "fields": ["value"],
        "operator": "ascending",
        "tolerance": None,
    }
    receipt = _evaluate([assertion], rows=rows)[0]
    assert receipt["verdict"] == "unverifiable"
    assert receipt["reason_code"] == "heterogeneous_ordering_evidence"


def test_empty_assertion_requires_the_explicit_empty_result_outcome():
    assertion = {
        "assertion_type": "empty",
        "selectors": [],
        "operator": "is",
        "tolerance": None,
    }
    assert _evaluate([assertion], rows=[], outcome="empty")[0]["verdict"] == "pass"
    assert _evaluate([assertion], rows=[], outcome="success")[0]["verdict"] == "fail"
