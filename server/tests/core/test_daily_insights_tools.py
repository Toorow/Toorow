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
from core.daily_insights_schema import evidence_universe

_METRICS = {"clicks", "impressions", "conversions", "cost"}
_DIMS = {"page", "country", "device"}
_TEMPLATES = {"kpi", "keywords", "conversions", "usertypes", "journey", "attribution"}

#: La lecture de readiness du serveur pour un jour PRET. Derivee de la fonction de
#: production, pas recopiee : un litteral ici passerait la garde de `publish` meme si
#: `readiness` changeait la forme de ce qu'elle rend.
_READY = dit.readiness(insight_date="2026-07-21", freshness_date="2026-07-21")


def _payload(slot=0, template="conversions"):
    return {
        "schemaVersion": "1",
        "slot": slot,
        "insight": {"title": "t", "summary": "s", "whyItMatters": "w", "confidence": "high"},
        "period": {"dateFrom": "2026-07-21", "dateTo": "2026-07-21"},
        "card": {"mode": "template", "template": template},
        # Story 53.4 : une publication cite au moins un fait MESURE par le
        # serveur. Le ref se derivait du gabarit demande, ce qui le faisait
        # resoudre par construction -- c'est-a-dire prouver que la gate acceptait
        # une citation circulaire. Il cite maintenant une metrique de `_METRICS`.
        "evidenceRefs": ["metric:conversions"],
    }


def _ctx(**over):
    ctx = {
        "available_metrics": _METRICS,
        "available_dimensions": _DIMS,
        "available_templates": _TEMPLATES,
        # DERIVE par la fonction de production (`evidence_universe`), pas par une
        # copie de sa formule. Un ensemble vide ici ferait echouer toute
        # publication a la gate 5 et masquerait les gates suivantes, que ce
        # fichier teste.
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


# ---------------------------------------------------------------------------
# The published payload outlives the render it was made from (Epic 35 §7, L3)
#
# `app.render_snapshots` is purged at RENDER_SNAPSHOT_RETENTION_DAYS (30) and
# `daily_insights.render_snapshot_id` is `ON DELETE SET NULL` (migration 061).
# An id alone therefore buys an insight thirty days of evidence, after which the
# model-authored prose -- which no gate inspects -- is all that is left.
# ---------------------------------------------------------------------------


def test_publish_embeds_the_whole_card_beside_its_lineage():
    # A CARD, NOT A CHART. What is served is something an LLM can read, quote and add
    # to, and that a person can share: the envelope alone would keep the figures and
    # lose the card.
    rec = MagicMock(return_value="dir_5")
    card = {
        "envelope": {"schema_version": "ad-1", "data": {"rows": [{"conversions": 12}]}},
        "widgetUri": "ui://widget/conversions.html",
        "summary": "Conversions held while spend rose.",
    }
    dit.publish(
        project_id="proj_a",
        insight_date="2026-07-21",
        status="published",
        item_payloads=[_payload(0)],
        validate_ctx=_ctx(),
        conn=MagicMock(),
        render_fn=lambda p: ("rsn_9", card),
        record_run_fn=rec,
    )
    item = rec.call_args.kwargs["items"][0]
    assert item.render_snapshot_id == "rsn_9"  # lineage: the proof of origin
    frozen = item.payload["frozenCard"]
    assert frozen["envelope"] == card["envelope"]  # the figures and their provenance
    assert frozen["widgetUri"] == "ui://widget/conversions.html"  # which widget draws it
    assert frozen["summary"] == "Conversions held while spend rose."  # what an LLM transmits


def test_publish_keeps_the_envelope_when_the_snapshot_could_not_be_written():
    # Losing the lineage is not losing the evidence. The render is best-effort;
    # the numbers the prose cites are not.
    rec = MagicMock(return_value="dir_6")
    dit.publish(
        project_id="proj_a",
        insight_date="2026-07-21",
        status="published",
        item_payloads=[_payload(0)],
        validate_ctx=_ctx(),
        conn=MagicMock(),
        render_fn=lambda p: (None, {"envelope": {"data": {"rows": []}}, "summary": "s"}),
        record_run_fn=rec,
    )
    item = rec.call_args.kwargs["items"][0]
    assert item.render_snapshot_id is None
    assert item.payload["frozenCard"]["envelope"] == {"data": {"rows": []}}


def test_publish_adds_no_frozen_card_when_nothing_could_be_rendered():
    rec = MagicMock(return_value="dir_7")
    dit.publish(
        project_id="proj_a",
        insight_date="2026-07-21",
        status="published",
        item_payloads=[_payload(0)],
        validate_ctx=_ctx(),
        conn=MagicMock(),
        render_fn=lambda p: (None, None),
        record_run_fn=rec,
    )
    # An ABSENT envelope is absent, never an empty one dressed as evidence.
    assert "frozenCard" not in rec.call_args.kwargs["items"][0].payload


def test_publish_refuses_an_enriched_payload_over_budget_rather_than_truncating():
    # §8: no overrun is silently cut. A half-written envelope would still look like
    # evidence, which is worse than an absent one. The budget parameter existed from
    # 35.0 and no production caller ever passed one until the embed made it matter.
    rec = MagicMock()
    huge = {"envelope": {"data": {"rows": [{"v": "x" * 1024} for _ in range(600)]}}}
    ack = dit.publish(
        project_id="proj_a",
        insight_date="2026-07-21",
        status="published",
        item_payloads=[_payload(0)],
        validate_ctx=_ctx(),
        conn=MagicMock(),
        render_fn=lambda p: ("rsn_big", huge),
        record_run_fn=rec,
    )
    assert ack["ok"] is False
    assert ack["reasonCode"] == "payload_too_large"
    assert ack["slot"] == 0
    rec.assert_not_called()


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
        readiness_state=_READY,
        record_run_fn=rec,
    )
    assert ack["ok"] is True and ack["status"] == "no_insight"
    assert ack["publishedSlots"] == []
    assert rec.call_args.kwargs["items"] == []
    # Editorial silence on a day that WAS ready stands untouched: the server checks
    # that the agent COULD look, never whether it should have spoken.
    assert "statusCorrectedFrom" not in ack


# ---------------------------------------------------------------------------
# `no_insight` is the one status the server does not take on faith
#
# `proactive-assertions.md` ("Incomplete if"): a surface must not report the
# equivalent of `no anomaly` where the honest answer is `insufficient
# observations`. Here the pair is `no_insight` (nothing was worth saying) versus
# `blocked` (nobody could look). `readiness()` separates them; `publish` used to
# record whichever one the agent declared.
# ---------------------------------------------------------------------------


def test_publish_no_insight_on_a_blocked_day_is_recorded_as_blocked():
    rec = MagicMock(return_value="dir_3")
    blocked = dit.readiness(insight_date="2026-07-21", freshness_date="2026-07-19")
    ack = dit.publish(
        project_id="proj_a",
        insight_date="2026-07-21",
        status="no_insight",
        item_payloads=[],
        validate_ctx=_ctx(),
        conn=MagicMock(),
        readiness_state=blocked,
        record_run_fn=rec,
    )
    assert ack["ok"] is True
    assert ack["status"] == "blocked"
    # Corrected out loud, so the task can log it rather than believe it published a
    # quiet day.
    assert ack["statusCorrectedFrom"] == "no_insight"
    coverage = rec.call_args.kwargs["coverage"]
    assert coverage["reason_code"] == dit.DATA_NOT_READY
    # The server's OWN measured reason travels to the row, not a generic label.
    assert "behind target" in coverage["reason"]
    # What the agent believed is kept beside it -- it is itself worth reading.
    assert coverage["declared_status"] == "no_insight"
    assert rec.call_args.kwargs["status"] == "blocked"


def test_publish_no_insight_keeps_an_existing_coverage_manifest():
    rec = MagicMock(return_value="dir_4")
    blocked = dit.readiness(insight_date="2026-07-21", freshness_date=None)
    dit.publish(
        project_id="proj_a",
        insight_date="2026-07-21",
        status="no_insight",
        item_payloads=[],
        validate_ctx=_ctx(),
        conn=MagicMock(),
        coverage={"datastreams_seen": 4},
        readiness_state=blocked,
        record_run_fn=rec,
    )
    coverage = rec.call_args.kwargs["coverage"]
    assert coverage["datastreams_seen"] == 4
    assert coverage["reason_code"] == dit.DATA_NOT_READY


def test_publish_no_insight_refused_when_readiness_is_unresolved():
    rec = MagicMock()
    ack = dit.publish(
        project_id="proj_a",
        insight_date="2026-07-21",
        status="no_insight",
        item_payloads=[],
        validate_ctx=_ctx(),
        conn=MagicMock(),
        readiness_state=None,
        record_run_fn=rec,
    )
    assert ack["ok"] is False
    assert ack["reasonCode"] == dit.READINESS_UNRESOLVED
    assert ack["fieldPath"] == "status"
    rec.assert_not_called()  # no durable claim about the day on an unverified reading


def test_publish_blocked_and_failed_stay_the_caller_s_to_declare():
    # `blocked` and `failed` assert an INABILITY, which is the one thing the host is
    # entitled to report about itself. Only `no_insight` asserts something about the
    # DAY, so only it is checked -- and neither needs a readiness reading to be written.
    for status in ("blocked", "failed"):
        rec = MagicMock(return_value=f"dir_{status}")
        ack = dit.publish(
            project_id="proj_a",
            insight_date="2026-07-21",
            status=status,
            item_payloads=[],
            validate_ctx=_ctx(),
            conn=MagicMock(),
            readiness_state=None,
            record_run_fn=rec,
        )
        assert ack["ok"] is True and ack["status"] == status
        assert rec.call_args.kwargs["status"] == status
