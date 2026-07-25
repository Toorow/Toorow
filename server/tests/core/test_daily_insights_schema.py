"""Tests for server/core/daily_insights_schema.py (Story 35.0 spike / gate).

Proves the published-insight contract + fail-closed validator:
  - the golden A-path fixture PASSES with realistic server-resolved inputs;
  - each rejection class FAILS with its expected, stable reason_code (Epic 35 §8);
  - budgets are enforced with NO silent truncation;
  - the canonical hash is deterministic.

All offline (pure validator, no DB / warehouse). ASCII-only stdout (L-3).
"""

from __future__ import annotations

import json
import os

import pytest
from core.daily_insights_schema import (
    MAX_BLOCKS_PER_CARD,
    MAX_INSIGHTS_PER_DAY,
    MAX_PAYLOAD_BYTES,
    MAX_TABLE_ROWS,
    SCHEMA_VERSION,
    canonical_payload_hash,
    validate_published_insight,
)

_FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "daily_insights")


def _load(name: str) -> dict:
    with open(os.path.join(_FIXTURES, name), encoding="utf-8") as fh:
        return json.load(fh)


def _base_kwargs() -> dict:
    """Server-resolved inputs under which the golden fixture is valid."""

    return {
        "available_metrics": {
            "clicks",
            "impressions",
            "cost",
            "conversions",
            "conversions_value",
            "cpa",
        },
        "available_dimensions": {"campaign", "country", "device", "source", "user_type"},
        "available_templates": {
            "kpi",
            "keywords",
            "conversions",
            "usertypes",
            "journey",
            "attribution",
            "dedup",
            "mediaplan_pacing",
            "connectors",
        },
        "resolvable_evidence": {"ev_1", "ev_2"},
        "freshness_date": "2026-07-21",
        "has_project_access": True,
        "existing_slots": set(),
        "expected_hash": None,
    }


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_golden_template_a_payload_passes():
    result = validate_published_insight(_load("valid_template_a.json"), **_base_kwargs())
    assert result.ok, f"expected valid, got {result.reason_code}: {result.message}"
    assert result.reason_code is None


def test_schema_version_constant_is_one():
    assert SCHEMA_VERSION == "1"


def test_vocabulary_in_lockstep_with_cards_registry():
    """Guard against silent drift: the schema must NOT diverge from cards.py's render vocab.

    (35.6 review HIGH) The schema advertises "reuses the cards.py vocabulary" -- assert it, so
    adding a block resolver or a non-additive metric to cards.py fails here until the schema
    (and its ratio/primitive gates) follow.
    """
    from core import cards as _cards
    from core.daily_insights_schema import ALLOWED_BLOCK_TYPES, NON_ADDITIVE_METRICS

    assert ALLOWED_BLOCK_TYPES == set(_cards._BLOCK_RESOLVERS) | {"comment"}
    assert NON_ADDITIVE_METRICS == _cards._NON_ADDITIVE_METRICS


# ---------------------------------------------------------------------------
# Rejection classes (payload mutations) -> stable reason_code
# ---------------------------------------------------------------------------


def _mutate(mutator) -> dict:
    payload = _load("valid_template_a.json")
    mutator(payload)
    return payload


def _set_blocks(payload, blocks, *, mode="template"):
    payload["card"]["mode"] = mode
    payload["card"]["blocks"] = blocks
    if mode == "compose":
        payload["card"].pop("template", None)


# (label, payload, extra kwargs override, expected reason_code)
def _cases():
    cases = []

    # Gate 1: schema / version / budgets
    cases.append(
        (
            "schema_version",
            _mutate(lambda p: p.update(schemaVersion="2")),
            {},
            "schema_version",
        )
    )
    cases.append(
        (
            "unknown_top_key",
            _mutate(lambda p: p.update(unexpected="x")),
            {},
            "schema_shape",
        )
    )
    cases.append(
        (
            "bad_confidence",
            _mutate(lambda p: p["insight"].update(confidence="certain")),
            {},
            "schema_shape",
        )
    )
    cases.append(
        (
            "slot_out_of_range",
            _mutate(lambda p: p.update(slot=MAX_INSIGHTS_PER_DAY)),
            {},
            "schema_shape",
        )
    )
    cases.append(
        (
            "budget_blocks",
            _mutate(
                lambda p: _set_blocks(
                    p,
                    [{"type": "kpi_row", "binding": {"metrics": "*"}}] * (MAX_BLOCKS_PER_CARD + 1),
                )
            ),
            {},
            "budget_blocks",
        )
    )

    # Gate 2: project scope + idempotency (kwargs-driven, valid payload)
    cases.append(
        (
            "project_scope",
            _load("valid_template_a.json"),
            {"has_project_access": False},
            "project_scope",
        )
    )
    cases.append(
        (
            "idempotency",
            _load("valid_template_a.json"),
            {"existing_slots": {0}},
            "idempotency",
        )
    )

    # Gate 3: mode + primitive/binding allowlist
    cases.append(
        (
            "mode_not_enabled",
            _mutate(
                lambda p: _set_blocks(
                    p, [{"type": "kpi_row", "binding": {"metrics": "*"}}], mode="compose"
                )
            ),
            {},
            "mode_not_enabled",
        )
    )
    cases.append(
        (
            "primitive_not_allowed",
            _mutate(lambda p: _set_blocks(p, [{"type": "iframe", "binding": {}}], mode="compose")),
            {"allow_compose": True},
            "primitive_not_allowed",
        )
    )
    cases.append(
        (
            "binding_not_allowed",
            _mutate(
                lambda p: _set_blocks(p, [{"type": "kpi_row", "binding": "oops"}], mode="compose")
            ),
            {"allow_compose": True},
            "binding_not_allowed",
        )
    )
    cases.append(
        (
            "budget_table_rows",
            _mutate(
                lambda p: _set_blocks(
                    p,
                    [
                        {
                            "type": "table",
                            "binding": {"dimensions": ["campaign"], "limit": MAX_TABLE_ROWS + 1},
                        }
                    ],
                    mode="compose",
                )
            ),
            {"allow_compose": True},
            "budget_table_rows",
        )
    )

    # Gate 4: template / metric / dimension availability
    cases.append(
        (
            "template_unknown",
            _mutate(lambda p: p["card"].update(template="does_not_exist")),
            {},
            "template_unknown",
        )
    )
    cases.append(
        (
            "metric_unavailable",
            _mutate(lambda p: p["card"].update(metrics=["ghost_metric"])),
            {},
            "metric_unavailable",
        )
    )
    cases.append(
        (
            "dimension_unavailable",
            _mutate(
                lambda p: _set_blocks(
                    p,
                    [
                        {
                            "type": "bar",
                            "binding": {"metrics": "clicks", "dimensions": ["ghost_dim"]},
                        }
                    ],
                    mode="compose",
                )
            ),
            {"allow_compose": True},
            "dimension_unavailable",
        )
    )

    # Gate 5: evidence resolution
    cases.append(
        (
            "evidence_unresolved",
            _mutate(lambda p: p.update(evidenceRefs=["ev_missing"])),
            {},
            "evidence_unresolved",
        )
    )

    # Gate 6: freshness / period
    cases.append(
        (
            "stale_data",
            _mutate(lambda p: p["period"].update(dateTo="2026-07-22")),
            {},
            "stale_data",
        )
    )
    cases.append(
        (
            "period_invalid",
            _mutate(lambda p: p["period"].update(dateFrom="2026-07-22", dateTo="2026-07-21")),
            {},
            "period_invalid",
        )
    )

    # Gate 7: non-additive metric must not inline a value
    cases.append(
        (
            "ratio_semantic",
            _mutate(
                lambda p: _set_blocks(
                    p,
                    [{"type": "gauge", "binding": {"metrics": "cpa", "value": 42.0}}],
                    mode="compose",
                )
            ),
            {"allow_compose": True},
            "ratio_semantic",
        )
    )

    return cases


@pytest.mark.parametrize(
    "label,payload,overrides,expected", _cases(), ids=lambda v: v if isinstance(v, str) else ""
)
def test_rejection_classes(label, payload, overrides, expected):
    kwargs = _base_kwargs()
    kwargs.update(overrides)
    result = validate_published_insight(payload, **kwargs)
    assert not result.ok, f"[{label}] expected failure, payload passed"
    assert result.reason_code == expected, (
        f"[{label}] expected reason {expected!r}, got {result.reason_code!r}: {result.message}"
    )


# ---------------------------------------------------------------------------
# Gate 1b + Gate 8: size budgets + hash
# ---------------------------------------------------------------------------


def test_spec_too_large_is_refused_not_truncated():
    payload = _load("valid_template_a.json")
    # Inflate limitations past the 64 KB agent-spec budget.
    payload["insight"]["limitations"] = ["x" * 1024] * 80
    result = validate_published_insight(payload, **_base_kwargs())
    assert not result.ok and result.reason_code == "spec_too_large"


def test_final_payload_too_large_is_refused():
    result = validate_published_insight(
        _load("valid_template_a.json"),
        final_payload_bytes=MAX_PAYLOAD_BYTES + 1,
        **_base_kwargs(),
    )
    assert not result.ok and result.reason_code == "payload_too_large"


def test_hash_mismatch_is_detected():
    payload = _load("valid_template_a.json")
    kwargs = _base_kwargs()
    kwargs["expected_hash"] = "deadbeef"
    result = validate_published_insight(payload, **kwargs)
    assert not result.ok and result.reason_code == "hash_mismatch"


def test_hash_match_passes():
    payload = _load("valid_template_a.json")
    kwargs = _base_kwargs()
    kwargs["expected_hash"] = canonical_payload_hash(payload)
    result = validate_published_insight(payload, **kwargs)
    assert result.ok, f"expected valid with matching hash, got {result.reason_code}"


def test_canonical_hash_is_deterministic_and_key_order_independent():
    payload = _load("valid_template_a.json")
    reordered = dict(reversed(list(payload.items())))
    assert canonical_payload_hash(payload) == canonical_payload_hash(reordered)


def test_all_gate_reason_codes_are_covered():
    """Guard: the parametrized suite exercises every payload-driven reason code."""

    covered = {expected for _, _, _, expected in _cases()}
    expected_min = {
        "schema_version",
        "schema_shape",
        "budget_blocks",
        "project_scope",
        "idempotency",
        "mode_not_enabled",
        "primitive_not_allowed",
        "binding_not_allowed",
        "budget_table_rows",
        "template_unknown",
        "metric_unavailable",
        "dimension_unavailable",
        "evidence_unresolved",
        "stale_data",
        "period_invalid",
        "ratio_semantic",
    }
    missing = expected_min - covered
    assert not missing, f"uncovered reason codes: {sorted(missing)}"
