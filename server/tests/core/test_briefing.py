"""Unit tests for core.briefing.build_briefing (Story 6.7, AC7).

Tests:
  - test_build_briefing_ranks_alerts_first
  - test_build_briefing_top_3_only
  - test_build_briefing_french_headline_baisse
  - test_build_briefing_french_headline_hausse
  - test_build_briefing_context_event_cited
  - test_build_briefing_no_alerts_no_anomalies
  - test_build_briefing_empty_inputs
  - test_build_briefing_citation_format
"""

from __future__ import annotations

from core.briefing import build_briefing

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _make_firing(
    type_: str,
    metric: str = "clicks",
    connector: str = "gsc",
    observed: float = 100.0,
    threshold: float = 150.0,
    pull_ids: list | None = None,
    id_: str = "fire_test",
) -> dict:
    return {
        "id": id_,
        "type": type_,
        "metric": metric,
        "connector": connector,
        "observed_value": observed,
        "threshold": threshold,
        "pull_ids": pull_ids or ["pull_abc123"],
    }


def _make_rollup(
    metric: str = "sessions",
    connector: str = "google-analytics",
    value: float = 2100.0,
    delta: float = 378.0,
    delta_pct: str = "+18%",
    pull_id: str = "pull_JABCDEFGH",
) -> dict:
    return {
        metric: {
            "value": value,
            "delta": delta,
            "delta_pct": delta_pct,
            "period": "sem. préc.",
            "source_system": connector,
            "source_field": metric,
            "pull_id": pull_id,
        }
    }


# ---------------------------------------------------------------------------
# test_build_briefing_ranks_alerts_first (AC7.1)
# ---------------------------------------------------------------------------

def test_build_briefing_ranks_alerts_first():
    """business_threshold alert + anomaly + notable delta -> business alert ranked 1."""
    firings = [
        _make_firing("anomaly", metric="average_position", id_="fire_anomaly"),
        _make_firing("business_threshold", metric="clicks", id_="fire_biz"),
    ]
    rollup = _make_rollup(metric="sessions")

    result = build_briefing(
        project_id="proj_1",
        briefing_date="2026-07-12",
        alert_firings=firings,
        rollup=rollup,
        context_events=[],
        nightly_run_id="nrun_test",
    )

    insights = result["insights"]
    assert len(insights) >= 1
    # Business alert must be rank 1
    rank1 = insights[0]
    assert rank1["type"] == "business_alert", (
        f"Expected 'business_alert' at rank 1, got {rank1['type']}"
    )
    assert rank1["rank"] == 1


# ---------------------------------------------------------------------------
# test_build_briefing_top_3_only (AC7.2)
# ---------------------------------------------------------------------------

def test_build_briefing_top_3_only():
    """10 alert_firings -> only top 5 (MAX_INSIGHTS) in insights, target 3."""
    firings = [
        _make_firing("business_threshold", metric=f"metric_{i}", id_=f"fire_{i}")
        for i in range(10)
    ]
    result = build_briefing(
        project_id="proj_1",
        briefing_date="2026-07-12",
        alert_firings=firings,
        rollup={},
        context_events=[],
        nightly_run_id=None,
    )

    insights = result["insights"]
    assert len(insights) <= 5, (
        f"Expected at most 5 insights, got {len(insights)}"
    )


# ---------------------------------------------------------------------------
# test_build_briefing_french_headline_baisse (AC7.3)
# ---------------------------------------------------------------------------

def test_build_briefing_french_headline_baisse():
    """Metric with negative delta -> headline contains 'en baisse'."""
    # observed < threshold implies negative delta (observed - threshold < 0)
    firings = [
        _make_firing("business_threshold", metric="clicks", observed=100.0, threshold=150.0)
    ]
    result = build_briefing(
        project_id="proj_1",
        briefing_date="2026-07-12",
        alert_firings=firings,
        rollup={},
        context_events=[],
        nightly_run_id=None,
    )

    insights = result["insights"]
    assert insights, "Expected at least one insight"
    headline = insights[0]["headline"]
    assert "en baisse" in headline.lower(), (
        f"Expected 'en baisse' in headline for negative delta, got: {headline!r}"
    )


# ---------------------------------------------------------------------------
# test_build_briefing_french_headline_hausse (AC7.4)
# ---------------------------------------------------------------------------

def test_build_briefing_french_headline_hausse():
    """Positive delta -> headline contains 'en hausse'."""
    # observed > threshold => delta > 0 => "en hausse"
    firings = [
        _make_firing("business_threshold", metric="sessions", observed=300.0, threshold=150.0)
    ]
    result = build_briefing(
        project_id="proj_1",
        briefing_date="2026-07-12",
        alert_firings=firings,
        rollup={},
        context_events=[],
        nightly_run_id=None,
    )

    insights = result["insights"]
    assert insights, "Expected at least one insight"
    headline = insights[0]["headline"]
    assert "en hausse" in headline.lower(), (
        f"Expected 'en hausse' in headline for positive delta, got: {headline!r}"
    )


# ---------------------------------------------------------------------------
# test_build_briefing_context_event_cited (AC7.5)
# ---------------------------------------------------------------------------

def test_build_briefing_context_event_cited():
    """Anomaly with matching context_event -> context_event_id in insight."""
    firings = [
        _make_firing("anomaly", metric="average_position", id_="fire_anom")
    ]
    context_events = [
        {
            "id": "evt_01JZZ",
            "event_date": "2026-07-12",
            "type": "deployment",
            "label": "Déploiement v2.5.0",
        }
    ]
    result = build_briefing(
        project_id="proj_1",
        briefing_date="2026-07-12",
        alert_firings=firings,
        rollup={},
        context_events=context_events,
        nightly_run_id=None,
    )

    insights = result["insights"]
    anomaly_insights = [i for i in insights if i["type"] == "anomaly"]
    assert anomaly_insights, "Expected at least one anomaly insight"
    anomaly = anomaly_insights[0]
    assert anomaly["context_event_id"] == "evt_01JZZ", (
        f"Expected context_event_id='evt_01JZZ', got {anomaly['context_event_id']}"
    )
    assert anomaly["context_event_label"] == "Déploiement v2.5.0", (
        f"Expected context_event_label set, got {anomaly['context_event_label']}"
    )


# ---------------------------------------------------------------------------
# test_build_briefing_no_alerts_no_anomalies (AC7.6)
# ---------------------------------------------------------------------------

def test_build_briefing_no_alerts_no_anomalies():
    """Only notable_delta -> rank 1 insight is type 'notable_delta'."""
    rollup = _make_rollup(metric="sessions", delta=378.0, delta_pct="+18%")

    result = build_briefing(
        project_id="proj_1",
        briefing_date="2026-07-12",
        alert_firings=[],
        rollup=rollup,
        context_events=[],
        nightly_run_id=None,
    )

    insights = result["insights"]
    assert insights, "Expected at least one insight from rollup"
    assert insights[0]["type"] == "notable_delta", (
        f"Expected 'notable_delta' at rank 1 when no alerts/anomalies, got {insights[0]['type']}"
    )
    assert insights[0]["rank"] == 1


# ---------------------------------------------------------------------------
# test_build_briefing_empty_inputs (AC7.7)
# ---------------------------------------------------------------------------

def test_build_briefing_empty_inputs():
    """No alerts, no rollup data -> insights=[], alerts_count=0."""
    result = build_briefing(
        project_id="proj_1",
        briefing_date="2026-07-12",
        alert_firings=[],
        rollup={},
        context_events=[],
        nightly_run_id=None,
    )

    assert result["insights"] == [], (
        f"Expected empty insights list, got {result['insights']}"
    )
    assert result["alerts_count"] == 0, (
        f"Expected alerts_count=0, got {result['alerts_count']}"
    )
    assert result["anomalies_count"] == 0, (
        f"Expected anomalies_count=0, got {result['anomalies_count']}"
    )
    assert result["version"] == 1
    assert result["briefing_date"] == "2026-07-12"


# ---------------------------------------------------------------------------
# test_build_briefing_citation_format (AC7.8)
# ---------------------------------------------------------------------------

def test_build_briefing_citation_format():
    """All insights carry citation in '(source:field, pull_id)' format."""
    firings = [
        _make_firing("business_threshold", metric="clicks", connector="gsc",
                     pull_ids=["pull_XXXXXXXXXXX"])
    ]
    rollup = _make_rollup(metric="sessions", connector="google-analytics",
                          pull_id="pull_JABCDEFGH")

    result = build_briefing(
        project_id="proj_1",
        briefing_date="2026-07-12",
        alert_firings=firings,
        rollup=rollup,
        context_events=[],
        nightly_run_id=None,
    )

    insights = result["insights"]
    assert insights, "Expected insights"
    for insight in insights:
        citation = insight.get("citation", "")
        assert citation.startswith("(") and citation.endswith(")"), (
            f"Citation must be wrapped in parentheses: {citation!r}"
        )
        assert ":" in citation, (
            f"Citation must contain 'source:field' separator: {citation!r}"
        )
        assert ", " in citation, (
            f"Citation must contain ', pull_id' part: {citation!r}"
        )


# ---------------------------------------------------------------------------
# Additional correctness tests
# ---------------------------------------------------------------------------

def test_build_briefing_meta_alerts_skipped():
    """meta_alert type firings must never appear in insights."""
    firings = [
        _make_firing("meta_alert", metric="scheduler_health"),
        _make_firing("business_threshold", metric="clicks"),
    ]
    result = build_briefing(
        project_id="proj_1",
        briefing_date="2026-07-12",
        alert_firings=firings,
        rollup={},
        context_events=[],
        nightly_run_id=None,
    )

    for insight in result["insights"]:
        assert insight["metric"] != "scheduler_health", (
            "meta_alert metrics must not appear in insights"
        )
    # Only the business_threshold should appear
    assert len(result["insights"]) == 1
    assert result["insights"][0]["type"] == "business_alert"


def test_build_briefing_result_shape():
    """Result must have all required AC2 keys."""
    result = build_briefing(
        project_id="proj_1",
        briefing_date="2026-07-12",
        alert_firings=[],
        rollup={},
        context_events=[],
        nightly_run_id=None,
    )

    required_keys = {"version", "briefing_date", "insights", "alerts_count",
                     "anomalies_count", "build_duration_ms"}
    assert required_keys.issubset(set(result.keys())), (
        f"Missing keys in result: {required_keys - set(result.keys())}"
    )
