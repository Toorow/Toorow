"""Seam tests for server/core/daily_insights_tools.py (Epic 35, Story 35.2, AI-56).

Module-level seams with injected deps (no live warehouse/DB): readiness states,
capability catalogue, preview (validate, no persistence), and publish (validate-all
fail-closed then atomic record_run). A registration assertion (tools exposed on the
FastMCP app) lives in test_daily_insights_tools_registration.

ASCII-only stdout (L-3).
"""

from __future__ import annotations

from unittest.mock import MagicMock

from core import daily_insights_tools as dit

_METRICS = {"clicks", "impressions", "conversions", "cost"}
_DIMS = {"page", "country", "device"}
_TEMPLATES = {"kpi", "keywords", "conversions", "usertypes", "journey", "attribution"}


def _payload(slot=0, template="conversions"):
    return {
        "schemaVersion": "1",
        "slot": slot,
        "insight": {"title": "t", "summary": "s", "whyItMatters": "w", "confidence": "high"},
        "period": {"dateFrom": "2026-07-21", "dateTo": "2026-07-21"},
        "card": {"mode": "template", "template": template},
    }


def _ctx(**over):
    ctx = {
        "available_metrics": _METRICS,
        "available_dimensions": _DIMS,
        "available_templates": _TEMPLATES,
        "resolvable_evidence": set(),
        "freshness_date": "2026-07-21",
        "has_project_access": True,
        "existing_slots": set(),
    }
    ctx.update(over)
    return ctx


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------


def test_readiness_ready():
    r = dit.readiness(insight_date="2026-07-21", freshness_date="2026-07-21")
    assert r["status"] == "ready" and r["reasons"] == []


def test_readiness_blocked_stale():
    r = dit.readiness(insight_date="2026-07-21", freshness_date="2026-07-20")
    assert r["status"] == "blocked" and any("behind target" in x for x in r["reasons"])


def test_readiness_blocked_no_freshness_or_dq():
    r = dit.readiness(insight_date="2026-07-21", freshness_date=None, dq_blocking=["gap in cost"])
    assert r["status"] == "blocked"
    assert any("no resolved freshness" in x for x in r["reasons"])
    assert any("data quality" in x for x in r["reasons"])


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------


def test_capabilities_shape():
    caps = dit.capabilities(available_metrics=_METRICS, available_dimensions=_DIMS)
    assert caps["contractVersion"] == "1" and caps["schemaVersion"] == "1"
    assert caps["metrics"] == sorted(_METRICS)
    ids = {e["id"] for e in caps["catalog"]}
    assert "conversions" in ids and "dedup" not in ids  # context card excluded
    assert caps["recommended"]["best"]["id"] == "conversions"


# ---------------------------------------------------------------------------
# Preview -- validate, never persist
# ---------------------------------------------------------------------------


def test_preview_ok():
    out = dit.preview(payload=_payload(), **_ctx())
    assert out["ok"] is True and out["reasonCode"] is None


def test_preview_reports_reason_without_persisting():
    out = dit.preview(payload=_payload(template="does_not_exist"), **_ctx())
    assert out["ok"] is False and out["reasonCode"] == "template_unknown"


# ---------------------------------------------------------------------------
# Publish -- validate-all fail-closed then atomic record_run
# ---------------------------------------------------------------------------


def test_publish_success_calls_record_run_with_items():
    rec = MagicMock(return_value="dir_1")
    conn = MagicMock()
    ack = dit.publish(
        project_id="proj_a",
        insight_date="2026-07-21",
        status="published",
        item_payloads=[_payload(0), _payload(1)],
        validate_ctx=_ctx(),
        conn=conn,
        render_snapshot_ids={0: "rsn_0"},
        identity="user_1",
        record_run_fn=rec,
    )
    assert ack["ok"] is True and ack["runId"] == "dir_1"
    assert ack["publishedSlots"] == [0, 1]
    kwargs = rec.call_args.kwargs
    assert kwargs["status"] == "published"
    assert kwargs["contract_version"] == "1"
    items = kwargs["items"]
    assert [it.slot for it in items] == [0, 1]
    assert items[0].render_snapshot_id == "rsn_0"


def test_publish_fail_closed_persists_nothing():
    rec = MagicMock()
    ack = dit.publish(
        project_id="proj_a",
        insight_date="2026-07-21",
        status="published",
        item_payloads=[_payload(0), _payload(1, template="does_not_exist")],
        validate_ctx=_ctx(),
        conn=MagicMock(),
        record_run_fn=rec,
    )
    assert ack["ok"] is False and ack["reasonCode"] == "template_unknown"
    assert ack["slot"] == 1
    rec.assert_not_called()  # all-or-nothing: no partial run


def test_publish_no_insight_records_zero_items():
    rec = MagicMock(return_value="dir_2")
    ack = dit.publish(
        project_id="proj_a",
        insight_date="2026-07-21",
        status="no_insight",
        item_payloads=[],
        validate_ctx=_ctx(),
        conn=MagicMock(),
        record_run_fn=rec,
    )
    assert ack["ok"] is True and ack["status"] == "no_insight"
    assert ack["publishedSlots"] == []
    assert rec.call_args.kwargs["items"] == []
