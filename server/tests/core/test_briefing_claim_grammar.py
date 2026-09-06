"""Story 53.8 — the sentence the briefing volunteers says only what the data supports.

Three acceptance criteria live here:

  * AC1 (CAV-14) — no arrow between an observation and a threshold;
  * AC2          — a direction is never asserted without a delta;
  * AC3          — the classifier reads a key its PRODUCERS actually emit;
  * AC6          — an absent citation is not a fabricated token.

AC3's test is deliberately built FROM the producer. A hand-written dict carrying
``"type": "anomaly"`` is how this defect survived for the whole life of the
module: every producer emits ``code``, the classifier read ``type``, so every
anomaly was rendered as a business alert and ``anomalies_count`` was structurally
zero for every project every day.
"""

from __future__ import annotations

from datetime import date

from core import anomaly_alerts, narrative
from core.briefing import _build_citation, _build_headline, _direction, build_briefing

# ---------------------------------------------------------------------------
# AC2 — a direction is never asserted without a delta
# ---------------------------------------------------------------------------


def test_direction_is_empty_without_a_delta():
    assert _direction(None) == "", "A rise claimed from the absence of evidence"


def test_direction_still_reads_the_sign_when_a_delta_exists():
    assert _direction(-3.0) == "en baisse"
    assert _direction(3.0) == "en hausse"


def test_headline_has_no_direction_word_without_a_delta():
    headline = _build_headline(
        "notable_delta", "clicks", "gsc", None, None, 700.0, None
    )
    lowered = headline.lower()
    assert "en hausse" not in lowered, headline
    assert "en baisse" not in lowered, headline


# ---------------------------------------------------------------------------
# AC1 (CAV-14) — no arrow between an observation and a threshold
# ---------------------------------------------------------------------------


def test_business_threshold_headline_carries_no_arrow():
    firings = [
        {
            "code": "business_threshold",
            "metric": "clicks",
            "connector": "gsc",
            "observed_value": 700.0,
            "threshold": 1000.0,
            "pull_ids": ["pull_EXAMPLE01"],
            "firing_id": "fire_1",
        }
    ]
    result = build_briefing(
        project_id="proj_EXAMPLE",
        briefing_date="2026-07-12",
        alert_firings=firings,
        rollup={},
        context_events=[],
        nightly_run_id=None,
    )
    headline = result["insights"][0]["headline"]
    assert "→" not in headline, f"A threshold is not a 'before' value: {headline!r}"
    assert "1000" in headline, f"The threshold must still be named: {headline!r}"
    assert "700" in headline, f"The observation must still be named: {headline!r}"
    assert "seuil" in headline.lower(), (
        f"The threshold must be named AS a threshold: {headline!r}"
    )


def test_business_threshold_headline_says_which_side_was_crossed():
    below = _build_headline(
        "business_alert", "clicks", "gsc", -300.0, None, 700.0, 1000.0,
        prior_kind="threshold",
    )
    above = _build_headline(
        "business_alert", "clicks", "gsc", 300.0, None, 1300.0, 1000.0,
        prior_kind="threshold",
    )
    assert "sous le seuil" in below, below
    assert "au-dessus du seuil" in above, above
    assert "→" not in below and "→" not in above


def test_anomaly_headline_carries_no_arrow_either():
    """The baseline mean is not an earlier observation -- same class as CAV-14."""
    headline = _build_headline(
        "anomaly", "sessions", "google-analytics", 9000.0, None, 12000.0, 3000.0,
        prior_kind="baseline",
    )
    assert "→" not in headline, headline
    assert "12000" in headline and "3000" in headline, headline


def test_a_genuine_period_over_period_move_keeps_its_arrow():
    """A notable_delta compares two observations: the arrow is the right grammar."""
    result = build_briefing(
        project_id="proj_EXAMPLE",
        briefing_date="2026-07-12",
        alert_firings=[],
        rollup={
            "sessions": {
                "value": 2100.0,
                "delta": 378.0,
                "delta_pct": "+18%",
                "period": "prev. wk.",
                "source_system": "google-analytics",
                "source_field": "sessions",
                "pull_id": "pull_EXAMPLE02",
            }
        },
        context_events=[],
        nightly_run_id=None,
    )
    headline = result["insights"][0]["headline"]
    assert "→" in headline, headline
    assert "en hausse" in headline, headline


# ---------------------------------------------------------------------------
# AC3 — the classifier reads a key its producers actually emit
# ---------------------------------------------------------------------------


class _StubCursor:
    """Minimal psycopg-shaped cursor: enough for fetch_recent_anomaly_firings."""

    def __init__(self, rows: list[tuple], cols: list[str]):
        self._rows = rows
        self.description = [(c,) for c in cols]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):  # noqa: ARG002
        return None

    def fetchall(self):
        return self._rows


class _StubConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


def _anomaly_firings_from_the_producer() -> list[dict]:
    cols = [
        "firing_id",
        "observed_value",
        "expected_value",
        "fired_at",
        "window_date",
        "severity",
        "metric",
    ]
    rows = [
        (
            "fire_EXAMPLE01",
            12000.0,
            3000.0,
            "2026-07-13T02:00:00+00:00",
            date(2026, 7, 12),
            "error",
            "sessions",
        )
    ]
    conn = _StubConn(_StubCursor(rows, cols))
    return anomaly_alerts.fetch_recent_anomaly_firings("proj_EXAMPLE", conn)


def test_the_producer_does_not_emit_the_key_the_classifier_used_to_read():
    """The bite of AC3: if this ever fails, the test stopped proving anything."""
    firings = _anomaly_firings_from_the_producer()
    assert firings, "producer returned nothing"
    assert "type" not in firings[0], (
        "The producer now emits 'type' -- rewrite this test, it no longer "
        "proves the classifier reads the producer's key"
    )
    assert firings[0]["code"] == "anomaly"


def test_a_producer_built_anomaly_is_classified_as_an_anomaly():
    firings = _anomaly_firings_from_the_producer()
    result = build_briefing(
        project_id="proj_EXAMPLE",
        briefing_date="2026-07-13",
        alert_firings=firings,
        rollup={},
        context_events=[],
        nightly_run_id=None,
    )
    assert result["insights"], "no insight built from a real producer firing"
    assert result["insights"][0]["type"] == "anomaly", result["insights"][0]
    assert result["anomalies_count"] == 1, result
    assert result["alerts_count"] == 0, result


def test_the_producer_carries_the_claims_own_date():
    """CAV-15 needs it: without window_date no event can ever be scoped."""
    firings = _anomaly_firings_from_the_producer()
    assert firings[0]["window_date"] == "2026-07-12"


def test_mediaplan_firings_are_not_rendered_as_business_alerts():
    firings = [
        {
            "code": "mediaplan_pace_overrun",
            "metric": "mediaplan_pace_overrun:plan_EXAMPLE",
            "observed_value": 1200.0,
            "threshold": 1000.0,
            "firing_id": "fire_2",
        }
    ]
    result = build_briefing(
        project_id="proj_EXAMPLE",
        briefing_date="2026-07-12",
        alert_firings=firings,
        rollup={},
        context_events=[],
        nightly_run_id=None,
    )
    assert result["insights"][0]["type"] == "mediaplan_alert", result["insights"][0]
    assert result["alerts_count"] == 0


def test_meta_alerts_are_still_skipped_after_the_classifier_change():
    firings = [
        {"code": "meta_alert", "metric": "scheduler_health", "firing_id": "fire_meta"},
        {"type": "meta_alert", "metric": "scheduler_health_2", "firing_id": "fire_meta2"},
        {
            "code": "business_threshold",
            "metric": "clicks",
            "connector": "gsc",
            "observed_value": 700.0,
            "threshold": 1000.0,
            "firing_id": "fire_biz",
        },
    ]
    result = build_briefing(
        project_id="proj_EXAMPLE",
        briefing_date="2026-07-12",
        alert_firings=firings,
        rollup={},
        context_events=[],
        nightly_run_id=None,
    )
    assert len(result["insights"]) == 1
    assert result["insights"][0]["metric"] == "clicks"


# ---------------------------------------------------------------------------
# AC6 — an absent citation is not a fabricated token
# ---------------------------------------------------------------------------


def test_absent_provenance_is_not_the_token_unknown():
    citation = _build_citation("google-analytics", "sessions", [])
    assert "unknown" not in citation, citation


def test_the_absence_marker_comes_from_narrative():
    citation = _build_citation("google-analytics", "sessions", [])
    expected = narrative._metric_citation(
        {"source_system": "google-analytics", "source_field": "sessions", "pull_id": None}
    )
    assert citation == expected, (citation, expected)


def test_present_provenance_keeps_the_connector_metric_pull_shape():
    citation = _build_citation("gsc", "clicks", ["pull_EXAMPLE01"])
    assert citation == "(gsc:clicks, pull_EXAMPLE01)", citation


def test_an_anomaly_from_the_producer_cites_an_absence_not_unknown():
    """pull_ids=[] is the DESIGNED state of an anomaly firing (derived signal)."""
    firings = _anomaly_firings_from_the_producer()
    result = build_briefing(
        project_id="proj_EXAMPLE",
        briefing_date="2026-07-13",
        alert_firings=firings,
        rollup={},
        context_events=[],
        nightly_run_id=None,
    )
    citation = result["insights"][0]["citation"]
    assert "unknown" not in citation, citation
    assert citation == narrative._metric_citation({}), citation
