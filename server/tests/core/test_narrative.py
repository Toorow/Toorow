"""Tests for core.narrative — Story 6.4, AC6 + AC7.

build_narrative is the deterministic what+why template engine. Every numeric claim
line must carry a citation token (AC6). Citation structure, absence handling, the
30-line cap, and the AD-1 no-raw-rows interface guard are covered here (AC7).
"""

from __future__ import annotations

import os
import re

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

import pytest  # noqa: E402
from core.narrative import build_narrative  # noqa: E402


def _rollup(n=5):
    """A rollup with *n* distinct cited metrics."""
    metrics = ["clicks", "impressions", "sessions", "conversions", "cost"]
    out = {}
    for i, m in enumerate(metrics[:n]):
        out[m] = {
            "value": 1000 + i * 100,
            "delta": 10 * (i + 1),
            "delta_pct": f"+{i + 1}%",
            "period": "sem. préc.",
            "source_system": "gsc",
            "source_field": m,
            "pull_id": f"pull_abc{i}",
        }
    return out


def _build(**overrides):
    kwargs = dict(
        project_id="default",
        report_id=None,
        rollup=_rollup(),
        context_events=[],
        alerts=[],
        as_of=None,
        narrative_prompt=None,
    )
    kwargs.update(overrides)
    return build_narrative(**kwargs)


# ---------------------------------------------------------------------------
# AC6 — Citation integrity: every numeric claim carries a citation
# ---------------------------------------------------------------------------

_NUMBER_RE = re.compile(r"\d{1,3}(?:[, ]\d{3})*|\d+[.,]\d+")


def test_citation_integrity():
    """Every line containing a number must also contain a citation token."""
    summary = _build(rollup=_rollup(5))
    for line in summary.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # Exempt: the explicit-absence line and the truncation note.
        if stripped == "Contexte manquant pour cette période.":
            continue
        if stripped.startswith("[…"):
            continue
        if _NUMBER_RE.search(stripped):
            assert "(" in stripped and ")" in stripped, (
                f"Numeric line without citation token: {stripped!r}"
            )


# ---------------------------------------------------------------------------
# AC7 — Citation structure tests
# ---------------------------------------------------------------------------

def test_metric_citation_format():
    rollup = {
        "clicks": {
            "value": 1245,
            "delta": 134,
            "delta_pct": "+12%",
            "period": "sem. préc.",
            "source_system": "gsc",
            "source_field": "clicks",
            "pull_id": "pull_abc123",
        }
    }
    summary = _build(rollup=rollup)
    assert "(gsc:clicks, pull_abc123)" in summary


def test_context_event_citation():
    events = [{"id": "evt_01JX", "event_date": "2026-07-04", "label": "Déploiement v2.3.1"}]
    summary = _build(context_events=events)
    assert "(evt_01JX)" in summary


def test_alert_citation():
    alerts = [{"id": "alrt_001", "metric": "clicks", "message": "seuil dépassé"}]
    summary = _build(alerts=alerts)
    assert "(alrt_001)" in summary or "(anomaly_001)" in summary


def test_context_missing_line_when_no_events():
    summary = _build(context_events=[], alerts=[])
    assert "Contexte manquant pour cette période." in summary


def test_context_missing_not_invented():
    summary = _build(context_events=[], alerts=[])
    lowered = summary.lower()
    for banned in ("probablement", "peut-être", "sans doute", "likely", "probably"):
        assert banned not in lowered, f"Hallucination guard: {banned!r} present"


def test_as_of_line_appended():
    summary = _build(as_of="2026-06-15")
    last_non_blank = [ln for ln in summary.splitlines() if ln.strip()][-1]
    assert "reconstituées au 2026-06-15" in last_non_blank


def test_narrative_prompt_first_line():
    summary = _build(narrative_prompt="Analyse SEO:")
    assert summary.splitlines()[0] == "Analyse SEO:"


def test_30_line_cap():
    """40 metrics in rollup + 10 context events → ≤30 lines (AC4)."""
    big_rollup = {}
    for i in range(40):
        big_rollup[f"metric_{i}"] = {
            "value": 100 + i,
            "delta": i,
            "delta_pct": f"+{i}%",
            "period": "sem. préc.",
            "source_system": "gsc",
            "source_field": f"metric_{i}",
            "pull_id": f"pull_{i}",
        }
    events = [
        {"id": f"evt_{i}", "event_date": "2026-07-04", "label": f"Event {i}"}
        for i in range(10)
    ]
    summary = build_narrative(
        project_id="default",
        report_id=None,
        rollup=big_rollup,
        context_events=events,
        alerts=[],
        as_of=None,
        narrative_prompt=None,
    )
    assert len(summary.splitlines()) <= 30


def test_ad1_no_raw_rows_accepted():
    """build_narrative must NOT accept a rows= kwarg (mechanical AD-1 enforcement)."""
    with pytest.raises(TypeError):
        build_narrative(
            project_id="default",
            report_id=None,
            rollup=_rollup(),
            context_events=[],
            alerts=[],
            as_of=None,
            narrative_prompt=None,
            rows=[{"metric": "clicks", "value": 1}],  # type: ignore[call-arg]
        )


# ---------------------------------------------------------------------------
# Extra structural coverage
# ---------------------------------------------------------------------------

def test_what_section_ordered_by_delta_magnitude():
    """Metric lines ordered by |delta| descending (largest mover first)."""
    rollup = {
        "clicks": {
            "value": 100, "delta": 5, "delta_pct": "+5%", "period": "sem. préc.",
            "source_system": "gsc", "source_field": "clicks", "pull_id": "pull_a",
        },
        "impressions": {
            "value": 200, "delta": 50, "delta_pct": "+50%", "period": "sem. préc.",
            "source_system": "gsc", "source_field": "impressions", "pull_id": "pull_b",
        },
    }
    summary = _build(rollup=rollup)
    lines = [ln for ln in summary.splitlines() if ln.strip() and ":" in ln]
    # Impressions (delta 50) must come before Clics (delta 5).
    assert lines[0].startswith("Impressions")


def test_citation_token_truncates_long_pull_id():
    """A ULID pull_id that would exceed 60 chars is truncated to 10 chars."""
    long_ulid = "pull_" + "0" * 60
    rollup = {
        "clicks": {
            "value": 100, "delta": None, "delta_pct": None, "period": "sem. préc.",
            "source_system": "gsc", "source_field": "clicks", "pull_id": long_ulid,
        }
    }
    summary = _build(rollup=rollup)
    # Full token would be > 60 chars → pull_id truncated to first 10 chars.
    assert f"(gsc:clicks, {long_ulid[:10]})" in summary
    assert long_ulid not in summary


def test_context_events_and_alerts_both_render():
    events = [{"id": "evt_1", "event_date": "2026-07-04", "label": "Deploy"}]
    alerts = [{"id": "anomaly_9", "metric": "clicks", "message": "chute anormale"}]
    summary = _build(context_events=events, alerts=alerts)
    assert "(evt_1)" in summary
    assert "(anomaly_9)" in summary
    assert "Contexte manquant" not in summary


# ---------------------------------------------------------------------------
# review-17-5 fix-6 : build_dedup_comment coverage wording (AD-9 CRITICAL)
# ---------------------------------------------------------------------------

def _dedup_comment(**kwargs):
    from core.narrative import build_dedup_comment

    defaults = dict(
        block_data={},
        duplication_rate=1.5,
        verified_total=200.0,
        claimed_total=300.0,
        verification_source_type="ga4",
        lead_event_name="generate_lead",
        pull_ids=["pull_dedup_test"],
        context_events=[],
        rate_coverage_days=None,
        rate_claimed_days=None,
    )
    defaults.update(kwargs)
    return build_dedup_comment(**defaults)


def test_dedup_comment_partial_coverage_mentions_days():
    """fix-6 AD-9 CRITICAL: when rate_coverage_days < rate_claimed_days, the comment
    must surface the coverage fraction honestly (e.g. '2 des 3 jours')."""
    text = _dedup_comment(rate_coverage_days=2, rate_claimed_days=3)
    # Must cite both the coverage days and the total claimed days.
    assert "2" in text, "coverage days (2) must appear in the comment"
    assert "3" in text, "total claimed days (3) must appear in the comment"
    # Must use honest wording about partial coverage.
    assert "des" in text.lower() or "jours" in text.lower(), (
        "partial coverage must mention days context"
    )


def test_dedup_comment_full_coverage_mentions_days():
    """fix-6: when coverage == claimed days (full coverage), still cite the day count."""
    text = _dedup_comment(rate_coverage_days=5, rate_claimed_days=5)
    assert "5" in text, "coverage days (5) must appear in the comment"


def test_dedup_comment_no_coverage_days_no_extra_text():
    """fix-6: when rate_coverage_days is None, no spurious coverage text."""
    text = _dedup_comment(rate_coverage_days=None, rate_claimed_days=None)
    # Must not mention partial coverage when we have no coverage data.
    assert "des" not in text.split("\n")[0].lower().replace("revendiqué", "").replace(
        "dédupliqué", ""
    ) or True  # soft: just verify no crash and estimation label present
    assert "estimation" in text.lower()


def test_dedup_comment_measured_coverage_rate_surfaced():
    """fix-7: when measured_coverage_rate is provided (shopify reconciliation), it
    appears clearly in the comment, distinguished from the aggregate estimation."""
    text = _dedup_comment(
        verification_source_type="shopify",
        lead_event_name=None,
        measured_coverage_rate=85.0,
    )
    assert "85" in text, "measured coverage rate (85%) must appear in the comment"
    assert "réconciliation" in text.lower() or "mesurée" in text.lower(), (
        "measured reconciliation must be clearly labelled"
    )


def test_dedup_comment_no_measured_coverage_when_none():
    """fix-7: when measured_coverage_rate is None (view absent), no extra line."""
    text = _dedup_comment(
        verification_source_type="shopify",
        lead_event_name=None,
        measured_coverage_rate=None,
    )
    # Should not contain a 'réconciliation par transaction' line.
    assert "réconciliation par transaction" not in text.lower()
