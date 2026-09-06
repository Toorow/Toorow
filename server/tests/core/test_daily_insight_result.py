"""AI-294 -- the Query Spec behind the card, offline seams.

What publishing an insight now produces is a governed Result, and this file
proves the seams that need no database: the derivation refusals that fire
before any SQL (each NAMED, never mute), the ack block, and the publish wiring
-- lineage lands on the item, a refusal lands as a named reason, and a refusal
NEVER blocks publication. The full chain against real Postgres rows lives in
`tests/integration/test_daily_insight_result_pg.py`.

ASCII-only stdout (L-3).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from core import daily_insight_result as dir_mod
from core import daily_insights_tools as dit
from core.daily_insights_schema import evidence_universe

_METRICS = {"clicks", "impressions", "conversions", "cost"}
_DIMS = {"page", "country", "device"}
_TEMPLATES = {"kpi", "keywords", "conversions", "usertypes", "journey", "attribution"}


def _payload(slot=0, template="conversions"):
    return {
        "schemaVersion": "1",
        "slot": slot,
        "insight": {"title": "t", "summary": "s", "whyItMatters": "w", "confidence": "high"},
        "period": {"dateFrom": "2026-07-21", "dateTo": "2026-07-21"},
        "card": {"mode": "template", "template": template, "metrics": ["conversions"]},
        "evidenceRefs": ["metric:conversions"],
    }


def _ctx(**over):
    ctx = {
        "available_metrics": _METRICS,
        "available_dimensions": _DIMS,
        "available_templates": _TEMPLATES,
        "resolvable_evidence": evidence_universe(
            available_metrics=_METRICS, available_dimensions=_DIMS
        ),
        "freshness_date": "2026-07-21",
        "has_project_access": True,
        "existing_slots": set(),
    }
    ctx.update(over)
    return ctx


# ---------------------------------------------------------------------------
# Derivation refusals that fire before any SQL. `conn=None` is deliberate:
# if one of these ever reached the database, the test would crash rather than
# silently pass with a mock that swallows the query.
# ---------------------------------------------------------------------------


def test_a_report_backed_card_is_refused_by_name():
    payload = _payload()
    payload["card"] = {"mode": "template", "reportRef": "google_ads/summary"}
    with pytest.raises(dir_mod.InsightResultUnavailable) as err:
        dir_mod.derive_insight_query_spec(None, project_id="proj_x", payload=payload)
    assert err.value.missing_link == dir_mod.REPORT_BACKED_CARD


def test_a_plan_context_card_is_refused_by_name():
    payload = _payload()
    payload["card"] = {"mode": "template", "template": "mediaplan_pacing", "planId": "mp_1"}
    with pytest.raises(dir_mod.InsightResultUnavailable) as err:
        dir_mod.derive_insight_query_spec(None, project_id="proj_x", payload=payload)
    assert err.value.missing_link == dir_mod.CONTEXT_CARD


def test_a_card_naming_no_metric_is_refused_by_name():
    payload = _payload()
    payload["card"] = {"mode": "template", "template": "kpi"}
    with pytest.raises(dir_mod.InsightResultUnavailable) as err:
        dir_mod.derive_insight_query_spec(None, project_id="proj_x", payload=payload)
    assert err.value.missing_link == dir_mod.CARD_NAMES_NO_METRIC


def test_the_refusal_carries_a_reason_a_surface_can_store():
    exc = dir_mod.InsightResultUnavailable("some_link", "the sentence")
    assert exc.as_reason() == {
        "result_unavailable_reason": "the sentence",
        "missing_link": "some_link",
    }


# ---------------------------------------------------------------------------
# The ack block (pure).
# ---------------------------------------------------------------------------


def test_lineage_ack_names_the_result_when_one_exists():
    ack = dir_mod.lineage_ack(
        {"query_spec_version_id": "qsv_1", "result_id": "qr_1", "outcome": "success"}, 0
    )
    assert ack == {
        "slot": 0,
        "resultId": "qr_1",
        "querySpecVersionId": "qsv_1",
        "outcome": "success",
    }


def test_lineage_ack_names_the_missing_link_when_none_exists():
    ack = dir_mod.lineage_ack(
        {"result_unavailable_reason": "no view", "missing_link": "no_semantic_view"}, 2
    )
    assert ack["resultId"] is None
    assert ack["resultUnavailableReason"] == "no view"
    assert ack["missingLink"] == "no_semantic_view"


# ---------------------------------------------------------------------------
# Publish wiring: the lineage rides the item and the ack; a refusal never
# blocks; an absent result_fn changes nothing (back-compat for callers that
# do not produce Results).
# ---------------------------------------------------------------------------


def test_publish_stores_the_result_lineage_on_the_item_and_in_the_ack():
    rec = MagicMock(return_value="dir_1")
    ack = dit.publish(
        project_id="proj_a",
        insight_date="2026-07-21",
        status="published",
        item_payloads=[_payload(0)],
        validate_ctx=_ctx(),
        conn=MagicMock(),
        identity="user_1",
        record_run_fn=rec,
        result_fn=lambda p: {
            "query_spec_version_id": "qsv_9",
            "result_id": "qr_9",
            "outcome": "success",
        },
    )
    assert ack["ok"] is True
    item = rec.call_args.kwargs["items"][0]
    assert item.query_spec_version_id == "qsv_9"
    assert item.result_id == "qr_9"
    assert item.result_unavailable_reason is None
    assert ack["results"] == [
        {"slot": 0, "resultId": "qr_9", "querySpecVersionId": "qsv_9", "outcome": "success"}
    ]


def test_a_derivation_refusal_never_blocks_publication_and_is_stored_named():
    rec = MagicMock(return_value="dir_1")
    ack = dit.publish(
        project_id="proj_a",
        insight_date="2026-07-21",
        status="published",
        item_payloads=[_payload(0)],
        validate_ctx=_ctx(),
        conn=MagicMock(),
        identity="user_1",
        record_run_fn=rec,
        result_fn=lambda p: {
            "result_unavailable_reason": "No published Semantic View covers this card.",
            "missing_link": dir_mod.NO_SEMANTIC_VIEW,
        },
    )
    assert ack["ok"] is True and ack["publishedSlots"] == [0]
    item = rec.call_args.kwargs["items"][0]
    assert item.result_id is None
    assert item.query_spec_version_id is None
    assert item.result_unavailable_reason == "No published Semantic View covers this card."
    assert ack["results"][0]["resultUnavailableReason"].startswith("No published Semantic View")


def test_without_a_result_fn_the_item_carries_no_lineage_and_the_ack_no_results_key():
    rec = MagicMock(return_value="dir_1")
    ack = dit.publish(
        project_id="proj_a",
        insight_date="2026-07-21",
        status="published",
        item_payloads=[_payload(0)],
        validate_ctx=_ctx(),
        conn=MagicMock(),
        identity="user_1",
        record_run_fn=rec,
    )
    assert ack["ok"] is True
    item = rec.call_args.kwargs["items"][0]
    assert item.result_id is None and item.result_unavailable_reason is None
    assert "results" not in ack


def test_the_result_fn_receives_the_enriched_payload_not_the_raw_one():
    """The Result must stand behind the exact artifact persisted, authorship included."""
    seen: list[dict] = []

    def result_fn(p):
        seen.append(p)
        return {"result_unavailable_reason": "x", "missing_link": "y"}

    dit.publish(
        project_id="proj_a",
        insight_date="2026-07-21",
        status="published",
        item_payloads=[_payload(0)],
        validate_ctx=_ctx(),
        conn=MagicMock(),
        identity="user_1",
        record_run_fn=MagicMock(return_value="dir_1"),
        result_fn=result_fn,
    )
    assert len(seen) == 1 and "authorship" in seen[0]
