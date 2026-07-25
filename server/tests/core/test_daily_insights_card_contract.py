"""Tests for server/core/daily_insights_card_contract.py (Story 35.1, A path).

Proves the agent-facing card-key contract:
  - the catalogue is derived from cards.py (every id resolves via get_template);
  - context cards are excluded from agent selection;
  - satisfiability + missing-inputs math is correct for a representative field set;
  - build_card_keys produces a `card` object that PASSES the 35.0 validator;
  - unsatisfiable / unknown / context selection fails closed with matching reason codes.

Offline (pure). ASCII-only stdout (L-3).
"""

from __future__ import annotations

from core import cards
from core.daily_insights_card_contract import (
    CARD_KEY_CONTRACT_VERSION,
    agent_card_catalog,
    build_card_keys,
    recommend_card,
)
from core.daily_insights_schema import validate_published_insight

_METRICS = {"clicks", "impressions", "conversions", "cost"}
_DIMS = {"page", "country", "device"}

# Given _METRICS/_DIMS: satisfiable = kpi, keywords, conversions, attribution;
# not satisfiable = usertypes (needs active_users/sessions/device_category); journey (needs sessions).  # noqa: E501
_SATISFIABLE = {"kpi", "keywords", "conversions", "attribution"}
_NOT_SATISFIABLE = {"usertypes", "journey"}
_CONTEXT = {"dedup", "mediaplan_pacing", "connectors"}


def test_contract_version():
    assert CARD_KEY_CONTRACT_VERSION == "1"


def test_catalogue_excludes_context_and_covers_all_non_context_templates():
    cat = agent_card_catalog(_METRICS, _DIMS)
    ids = {e["id"] for e in cat}
    assert ids == _SATISFIABLE | _NOT_SATISFIABLE
    assert ids.isdisjoint(_CONTEXT)


def test_every_catalogue_id_resolves_in_the_registry():
    for entry in agent_card_catalog(_METRICS, _DIMS):
        assert cards.get_template(entry["id"]) is not None


def test_satisfiability_and_missing_inputs():
    cat = {e["id"]: e for e in agent_card_catalog(_METRICS, _DIMS)}
    for tid in _SATISFIABLE:
        assert cat[tid]["satisfiable"] is True
        assert "missing" not in cat[tid]
    for tid in _NOT_SATISFIABLE:
        assert cat[tid]["satisfiable"] is False
        miss = cat[tid]["missing"]
        assert miss["metrics"] or miss["dimensions"]
    # journey needs 'sessions'; usertypes needs device_category dimension.
    assert "sessions" in cat["journey"]["missing"]["metrics"]
    assert "device_category" in cat["usertypes"]["missing"]["dimensions"]


def test_catalogue_is_deterministic_satisfiable_first():
    cat = agent_card_catalog(_METRICS, _DIMS)
    # All satisfiable entries precede all non-satisfiable ones.
    flags = [e["satisfiable"] for e in cat]
    assert flags == sorted(flags, reverse=True)
    assert agent_card_catalog(_METRICS, _DIMS) == cat  # stable across calls


def test_recommend_card_best_is_highest_rank_satisfiable():
    rec = recommend_card(_METRICS, _DIMS)
    # conversions (rank 30) is the highest-ranked satisfiable template for this field set.
    assert rec["best"]["id"] == "conversions"
    alt_ids = {a["id"] for a in rec["alternatives"]}
    assert "attribution" in alt_ids and "kpi" in alt_ids
    assert "usertypes" not in alt_ids  # not satisfiable


def test_recommend_card_none_when_no_metrics():
    rec = recommend_card(set(), set())
    assert rec["best"] is None
    assert rec["alternatives"] == []


def test_build_card_keys_produces_schema_valid_card():
    result = build_card_keys(
        "conversions",
        date_from="2026-07-21",
        date_to="2026-07-21",
        available_metrics=_METRICS,
        available_dimensions=_DIMS,
    )
    assert result.ok, result.message
    assert result.card == {"mode": "template", "template": "conversions"}

    # Embed the built card in a 35.0 payload and validate end-to-end.
    payload = {
        "schemaVersion": "1",
        "slot": 0,
        "insight": {
            "title": "Conversions hold while spend rises",
            "summary": "Verified conversions are flat versus the prior week.",
            "whyItMatters": "Budget increases are not translating into outcomes.",
            "confidence": "medium",
        },
        "period": {
            "dateFrom": "2026-07-21",
            "dateTo": "2026-07-21",
            "comparison": "previous_7_days",
        },
        "card": result.card,
    }
    v = validate_published_insight(
        payload,
        available_metrics=_METRICS,
        available_dimensions=_DIMS,
        available_templates=_SATISFIABLE | _NOT_SATISFIABLE,
        resolvable_evidence=set(),
        freshness_date="2026-07-21",
        has_project_access=True,
        existing_slots=set(),
    )
    assert v.ok, f"built card should pass 35.0 validator, got {v.reason_code}: {v.message}"


def test_build_card_keys_with_extra_metrics_and_refs():
    result = build_card_keys(
        "conversions",
        date_from="2026-07-14",
        date_to="2026-07-21",
        metrics=["conversions", "cost"],
        report_ref="meta/daily",
        plan_id="plan_42",
        available_metrics=_METRICS,
        available_dimensions=_DIMS,
    )
    assert result.ok
    assert result.card["metrics"] == ["conversions", "cost"]
    assert result.card["reportRef"] == "meta/daily"
    assert result.card["planId"] == "plan_42"


def test_build_card_keys_fails_closed():
    common = dict(
        date_from="2026-07-21",
        date_to="2026-07-21",
        available_metrics=_METRICS,
        available_dimensions=_DIMS,
    )
    # unknown template
    assert build_card_keys("nope", **common).reason_code == "template_unknown"
    # context-kind template is not agent-selectable
    assert build_card_keys("connectors", **common).reason_code == "template_unknown"
    # unsatisfiable template -> metric/dimension reason
    r = build_card_keys("journey", **common)
    assert r.reason_code in {"metric_unavailable", "dimension_unavailable"}
    # requested metric not available
    r2 = build_card_keys(
        "conversions",
        metrics=["ghost_metric"],
        date_from="2026-07-21",
        date_to="2026-07-21",
        available_metrics=_METRICS,
        available_dimensions=_DIMS,
    )
    assert r2.reason_code == "metric_unavailable"
