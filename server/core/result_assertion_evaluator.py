"""Deterministic evaluation of ``golden-question.v2`` Result assertions.

The evaluator consumes only an already-pinned immutable Result projection.  It
does not execute a query, infer expected truth, mutate a Golden Question, or
collapse independent assertions into a score.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _receipt(
    ordinal: int,
    assertion: Mapping[str, Any],
    verdict: str,
    reason_code: str,
    *,
    observed: Any = None,
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "ordinal": ordinal,
        "assertion_type": assertion.get("assertion_type"),
        "verdict": verdict,
        "reason_code": reason_code,
        "expected_hash": _hash(assertion),
        "observed_hash": _hash(observed) if observed is not None else None,
        "evidence_refs": dict(evidence or {}),
    }


def _decimal(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise InvalidOperation
    result = Decimal(str(value))
    if not result.is_finite():
        raise InvalidOperation
    return result


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError
    return parsed.astimezone(UTC)


def _matches_selector(row: Mapping[str, Any], selector: Mapping[str, Any]) -> bool:
    field = selector["field"]
    operator = selector["operator"]
    if operator == "exists":
        return field in row
    if field not in row:
        return False
    actual = row[field]
    expected = selector.get("value")
    if operator == "eq":
        return _canonical_json(actual) == _canonical_json(expected)
    if operator == "ne":
        return _canonical_json(actual) != _canonical_json(expected)
    return any(_canonical_json(actual) == _canonical_json(value) for value in expected)


def _selected_rows(
    rows: list[Mapping[str, Any]], selectors: list[Mapping[str, Any]]
) -> list[Mapping[str, Any]]:
    return [row for row in rows if all(_matches_selector(row, item) for item in selectors)]


def _numeric_match(actual: Any, expected: Any, tolerance: Mapping[str, Any]) -> bool:
    actual_value, expected_value = _decimal(actual), _decimal(expected)
    amount = _decimal(tolerance["amount"])
    difference = abs(actual_value - expected_value)
    if tolerance["mode"] == "absolute":
        return difference <= amount
    return difference <= abs(expected_value) * amount


def _temporal_match(actual: Any, expected: Any, tolerance: Mapping[str, Any]) -> bool:
    return abs((_timestamp(actual) - _timestamp(expected)).total_seconds()) <= float(
        tolerance["seconds"]
    )


def _exact_value_match(actual: Any, expected: Any) -> bool:
    if (
        not isinstance(actual, bool)
        and not isinstance(expected, bool)
        and isinstance(actual, int | float)
        and isinstance(expected, int | float)
    ):
        return _decimal(actual) == _decimal(expected)
    if isinstance(expected, str):
        try:
            expected_time = _timestamp(expected)
        except ValueError:
            pass
        else:
            return _timestamp(actual) == expected_time
    return _canonical_json(actual) == _canonical_json(expected)


def _value_verdict(assertion: Mapping[str, Any], rows: list[Mapping[str, Any]]):
    selected = _selected_rows(rows, assertion["selectors"])
    if len(selected) != 1:
        return (
            "unverifiable",
            "value_selection_not_singular",
            selected,
            {"selected_rows": len(selected)},
        )
    field = assertion["field"]
    if field not in selected[0]:
        return "unverifiable", "required_field_missing", selected, {"field": field}
    actual, expected = selected[0][field], assertion["expected"]
    tolerance = assertion["tolerance"]
    try:
        if tolerance is None:
            matched = _exact_value_match(actual, expected)
        elif tolerance["kind"] == "numeric":
            matched = _numeric_match(actual, expected, tolerance)
        else:
            matched = _temporal_match(actual, expected, tolerance)
    except InvalidOperation:
        return "unverifiable", "malformed_numeric_evidence", actual, {"field": field}
    except (TypeError, ValueError, OverflowError):
        return "unverifiable", "malformed_timestamp_evidence", actual, {"field": field}
    return (
        ("pass", "assertion_matched", actual, {"field": field})
        if matched
        else (
            "fail",
            "assertion_mismatch",
            actual,
            {"field": field},
        )
    )


def _project_rows(rows: list[Mapping[str, Any]], fields: list[str]):
    if any(any(field not in row for field in fields) for row in rows):
        raise KeyError
    return [{field: row[field] for field in fields} for row in rows]


def _ordering_atom(value: Any) -> tuple[str, Any]:
    if not isinstance(value, bool) and isinstance(value, int | float):
        return "number", _decimal(value)
    if isinstance(value, str):
        try:
            return "timestamp", _timestamp(value)
        except ValueError:
            return "string", value
    if isinstance(value, bool):
        return "boolean", value
    if value is None:
        return "null", "null"
    if isinstance(value, list):
        return "array", _canonical_json(value)
    if isinstance(value, dict):
        return "object", _canonical_json(value)
    return type(value).__name__, _canonical_json(value)


def _row_set_verdict(assertion: Mapping[str, Any], rows: list[Mapping[str, Any]]):
    selected = _selected_rows(rows, assertion["selectors"])
    try:
        projected = _project_rows(selected, assertion["fields"])
    except KeyError:
        return "unverifiable", "required_field_missing", selected, {}
    actual = Counter(_canonical_json(row) for row in projected)
    expected = Counter(_canonical_json(row) for row in assertion["expected_rows"])
    missing = sum((expected - actual).values())
    extra = sum((actual - expected).values())
    tolerance = assertion["tolerance"] or {"max_missing": 0, "max_extra": 0}
    matched = missing <= tolerance["max_missing"] and extra <= tolerance["max_extra"]
    evidence = {"selected_rows": len(projected), "missing": missing, "extra": extra}
    return (
        ("pass", "assertion_matched", projected, evidence)
        if matched
        else ("fail", "assertion_mismatch", projected, evidence)
    )


def _ordering_verdict(assertion: Mapping[str, Any], rows: list[Mapping[str, Any]]):
    selected = _selected_rows(rows, assertion["selectors"])
    if not selected:
        return "unverifiable", "selection_empty", selected, {"selected_rows": 0}
    try:
        projected = _project_rows(selected, assertion["fields"])
        keys = [
            tuple(_ordering_atom(row[field]) for field in assertion["fields"]) for row in projected
        ]
        heterogeneous = any(
            len({key[index][0] for key in keys}) > 1
            for index in range(len(assertion["fields"]))
        )
        if heterogeneous:
            return (
                "unverifiable",
                "heterogeneous_ordering_evidence",
                selected,
                {"fields": assertion["fields"]},
            )
        matched = keys == sorted(keys, reverse=assertion["operator"] == "descending")
    except (InvalidOperation, KeyError, TypeError, ValueError):
        return "unverifiable", "malformed_ordering_evidence", selected, {}
    return (
        ("pass", "assertion_matched", projected, {"selected_rows": len(projected)})
        if matched
        else ("fail", "assertion_mismatch", projected, {"selected_rows": len(projected)})
    )


def _cardinality_verdict(assertion: Mapping[str, Any], rows: list[Mapping[str, Any]]):
    actual = len(_selected_rows(rows, assertion["selectors"]))
    tolerance = assertion["tolerance"]
    try:
        matched = (
            actual == assertion["expected"]
            if tolerance is None
            else _numeric_match(actual, assertion["expected"], tolerance)
        )
    except InvalidOperation:
        return "unverifiable", "malformed_numeric_evidence", actual, {}
    return (
        ("pass", "assertion_matched", actual, {})
        if matched
        else (
            "fail",
            "assertion_mismatch",
            actual,
            {},
        )
    )


def _invariant_verdict(assertion: Mapping[str, Any], rows: list[Mapping[str, Any]]):
    selected = _selected_rows(rows, assertion["selectors"])
    if not selected:
        return "unverifiable", "selection_empty", selected, {"selected_rows": 0}
    try:
        projected = _project_rows(selected, assertion["fields"])
    except KeyError:
        return "unverifiable", "required_field_missing", selected, {}
    if assertion["operator"] == "non_null":
        matched = all(all(value is not None for value in row.values()) for row in projected)
    else:
        canonical = [_canonical_json(row) for row in projected]
        matched = len(canonical) == len(set(canonical))
    return (
        ("pass", "assertion_matched", projected, {"selected_rows": len(projected)})
        if matched
        else ("fail", "assertion_mismatch", projected, {"selected_rows": len(projected)})
    )


def evaluate_result_assertions(
    assertions: list[Mapping[str, Any]], *, result: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Evaluate validated v2 assertions without executing or mutating anything."""
    if not isinstance(result, Mapping) or result.get("truncated") is True:
        reason = (
            "result_evidence_truncated"
            if isinstance(result, Mapping)
            else "result_evidence_missing"
        )
        return [_receipt(i, item, "unverifiable", reason) for i, item in enumerate(assertions)]
    rows = result.get("rows")
    if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
        return [
            _receipt(i, item, "unverifiable", "result_rows_malformed")
            for i, item in enumerate(assertions)
        ]
    row_count = result.get("row_count")
    if (
        not isinstance(row_count, int)
        or isinstance(row_count, bool)
        or row_count < 0
        or row_count != len(rows)
    ):
        return [
            _receipt(i, item, "unverifiable", "result_rows_incomplete")
            for i, item in enumerate(assertions)
        ]
    outcome = result.get("outcome")
    receipts: list[dict[str, Any]] = []
    evaluators = {
        "value": _value_verdict,
        "row_set": _row_set_verdict,
        "ordering": _ordering_verdict,
        "cardinality": _cardinality_verdict,
        "invariant": _invariant_verdict,
    }
    for ordinal, assertion in enumerate(assertions):
        assertion_type = assertion["assertion_type"]
        if assertion_type in evaluators:
            verdict, reason, observed, evidence = evaluators[assertion_type](assertion, rows)
        elif assertion_type == "empty":
            observed = {"outcome": outcome, "rows": len(rows)}
            verdict = "pass" if outcome == "empty" and not rows else "fail"
            reason = "assertion_matched" if verdict == "pass" else "assertion_mismatch"
            evidence = {}
        else:
            observed = {"outcome": outcome}
            verdict = "pass" if outcome == assertion_type else "fail"
            reason = "assertion_matched" if verdict == "pass" else "assertion_mismatch"
            evidence = {}
        receipts.append(
            _receipt(
                ordinal,
                assertion,
                verdict,
                reason,
                observed=observed,
                evidence=evidence,
            )
        )
    return receipts


def evaluate_required_provenance(
    requirements: list[Mapping[str, Any]], *, manifest: Mapping[str, Any]
) -> dict[str, Any]:
    """Evaluate closed requirements only against frozen Result manifest links."""
    if not isinstance(manifest, Mapping):
        return {"verdict": "unverifiable", "reason_code": "provenance_links_malformed"}
    provenance = manifest.get("provenance")
    if not isinstance(provenance, Mapping):
        return {"verdict": "unverifiable", "reason_code": "provenance_links_malformed"}
    links = provenance.get("links")
    if links is None:
        candidates = {
            "source": provenance.get("source_system") or provenance.get("datastream_id"),
            "pull": provenance.get("pull_id"),
            "virtual_pull": provenance.get("virtual_pull_id"),
            "mapping": provenance.get("mapping_version_id"),
            "publication": provenance.get("publication_log_id"),
            "semantic_view": manifest.get("semantic_view_version_id"),
        }
        links = [
            {"link_kind": kind, "ref": str(reference)}
            for kind, reference in candidates.items()
            if reference is not None
        ]
        citations = manifest.get("citations")
        if isinstance(citations, list):
            links.extend(
                {"link_kind": "citation", "ref": str(reference)}
                for reference in citations
                if isinstance(reference, str) and reference
            )
    if not isinstance(links, list) or any(not isinstance(link, Mapping) for link in links):
        return {"verdict": "unverifiable", "reason_code": "provenance_links_malformed"}
    missing: list[dict[str, Any]] = []
    for requirement in requirements:
        if not requirement["required"]:
            continue
        matches = [link for link in links if link.get("link_kind") == requirement["link_kind"]]
        if "expected_ref" in requirement:
            matches = [link for link in matches if link.get("ref") == requirement["expected_ref"]]
        if not matches:
            missing.append(dict(requirement))
    return {
        "verdict": "pass" if not missing else "fail",
        "reason_code": "provenance_matched" if not missing else "provenance_missing",
        "missing": missing,
    }
