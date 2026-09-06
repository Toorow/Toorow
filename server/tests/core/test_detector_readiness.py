"""Story 53.8 / CAV-13 — the detector's silence is a state, and it is disclosed.

AC8 — the minimum-observation number is DERIVED from the configured threshold,
never a literal. A hardcoded 11 becomes a lie the day someone edits the env var,
and the disclosure would keep claiming it was measured.

AC9 — the payload separates ``insufficient observations (n/N)`` from
``no anomaly``. The two must be distinguishable by a consumer without heuristics.
"""

from __future__ import annotations

import math
import os
from datetime import date
from unittest.mock import MagicMock, patch

from core import anomaly_alerts
from core.briefing import build_briefing

# ---------------------------------------------------------------------------
# AC8 — the number is derived
# ---------------------------------------------------------------------------


def test_minimum_observations_at_the_default_threshold():
    assert anomaly_alerts.minimum_observations_for_threshold(3.0) == 11


def test_minimum_observations_at_the_error_severity():
    assert anomaly_alerts.minimum_observations_for_threshold(5.0) == 27


def test_the_minimum_moves_with_the_configured_threshold():
    """The bite of AC8: a literal would not move."""
    with patch.dict(os.environ, {"ANOMALY_Z_THRESHOLD": "3.0"}):
        low = anomaly_alerts.minimum_observations_for_threshold()
    with patch.dict(os.environ, {"ANOMALY_Z_THRESHOLD": "5.0"}):
        high = anomaly_alerts.minimum_observations_for_threshold()
    assert (low, high) == (11, 27), (low, high)


def test_the_minimum_is_the_smallest_n_that_can_reach_the_threshold():
    """(n-1)/sqrt(n) is the exact bound; check both sides of it, not just one."""
    for threshold in (1.5, 2.0, 3.0, 4.0, 5.0, 7.5):
        n = anomaly_alerts.minimum_observations_for_threshold(threshold)
        assert (n - 1) / math.sqrt(n) >= threshold, (threshold, n)
        assert (n - 2) / math.sqrt(n - 1) < threshold, (
            f"{n} is not the SMALLEST n reaching {threshold}"
        )


def test_an_unparseable_threshold_falls_back_to_the_documented_default():
    with patch.dict(os.environ, {"ANOMALY_Z_THRESHOLD": "not-a-number"}):
        assert anomaly_alerts.anomaly_z_threshold() == 3.0
        assert anomaly_alerts.minimum_observations_for_threshold() == 11


# ---------------------------------------------------------------------------
# The reader over metric_baselines — module posture: never raises
# ---------------------------------------------------------------------------


def _duck(rows):
    conn = MagicMock()
    res = MagicMock()
    res.fetchall = MagicMock(return_value=rows)
    conn.execute = MagicMock(return_value=res)
    return conn


def test_readiness_marks_a_short_series_as_not_armed():
    conn = _duck([("proj_EXAMPLE", "google-analytics", "sessions", 5)])
    with patch.dict(os.environ, {"ANOMALY_Z_THRESHOLD": "3.0"}):
        rows = anomaly_alerts.fetch_detector_readiness(
            project_id="proj_EXAMPLE", evaluation_date=date(2026, 7, 12), conn=conn
        )
    assert len(rows) == 1
    assert rows[0]["observations"] == 5
    assert rows[0]["minimum_observations"] == 11
    assert rows[0]["armed"] is False


def test_readiness_marks_a_long_series_as_armed():
    conn = _duck([("proj_EXAMPLE", "google-analytics", "sessions", 30)])
    with patch.dict(os.environ, {"ANOMALY_Z_THRESHOLD": "3.0"}):
        rows = anomaly_alerts.fetch_detector_readiness(
            project_id="proj_EXAMPLE", evaluation_date=date(2026, 7, 12), conn=conn
        )
    assert rows[0]["armed"] is True


def test_readiness_never_raises():
    conn = MagicMock()
    conn.execute = MagicMock(side_effect=RuntimeError("mart absent"))
    rows = anomaly_alerts.fetch_detector_readiness(
        project_id="proj_EXAMPLE", evaluation_date=date(2026, 7, 12), conn=conn
    )
    assert rows == []


# ---------------------------------------------------------------------------
# AC9 — the payload separates the two silences
# ---------------------------------------------------------------------------


def _briefing(readiness=None, alert_firings=None):
    kwargs = dict(
        project_id="proj_EXAMPLE",
        briefing_date="2026-07-13",
        alert_firings=alert_firings or [],
        rollup={},
        context_events=[],
        nightly_run_id=None,
    )
    if readiness is not None:
        kwargs["detector_readiness"] = readiness
    return build_briefing(**kwargs)


def test_a_five_observation_series_is_insufficient_not_quiet():
    result = _briefing(
        readiness=[
            {
                "project_id": "proj_EXAMPLE",
                "connector": "google-analytics",
                "metric": "sessions",
                "observations": 5,
                "minimum_observations": 11,
                "armed": False,
            }
        ]
    )
    assert result["anomaly_state"] == "insufficient_observations", result["anomaly_state"]
    readiness = result["detector_readiness"]
    assert readiness["insufficient_count"] == 1
    assert readiness["armed_count"] == 0
    assert readiness["minimum_observations"] == 11
    entry = readiness["insufficient"][0]
    assert entry["observations"] == 5
    assert entry["minimum_observations"] == 11
    assert entry["label"] == "insufficient observations (5/11)"
    # ... and it is NOT the quiet briefing.
    assert result["anomaly_state"] != "no_anomaly"


def test_an_armed_series_with_nothing_to_report_is_no_anomaly():
    result = _briefing(
        readiness=[
            {
                "project_id": "proj_EXAMPLE",
                "connector": "google-analytics",
                "metric": "sessions",
                "observations": 30,
                "minimum_observations": 11,
                "armed": True,
            }
        ]
    )
    assert result["anomaly_state"] == "no_anomaly", result["anomaly_state"]
    assert result["detector_readiness"]["insufficient_count"] == 0
    assert result["detector_readiness"]["armed_count"] == 1


def test_the_two_states_are_distinguishable_without_heuristics():
    quiet = _briefing(
        readiness=[{"connector": "c", "metric": "m", "observations": 30,
                    "minimum_observations": 11, "armed": True}]
    )
    mute = _briefing(
        readiness=[{"connector": "c", "metric": "m", "observations": 5,
                    "minimum_observations": 11, "armed": False}]
    )
    assert quiet["anomalies_count"] == mute["anomalies_count"] == 0, (
        "both look identical on the old field -- that IS the caveat"
    )
    assert quiet["anomaly_state"] != mute["anomaly_state"]


def test_an_absent_readiness_input_is_not_supplied_never_no_anomaly():
    """AC9 boundary: scheduler.py is out of this story's file scope."""
    result = _briefing()
    assert result["anomaly_state"] == "not_supplied", result["anomaly_state"]
    assert result["detector_readiness"]["state"] == "not_supplied"
    assert result["detector_readiness"]["minimum_observations"] is None


def test_an_empty_readiness_list_is_no_series_not_no_anomaly():
    result = _briefing(readiness=[])
    assert result["anomaly_state"] == "no_series", result["anomaly_state"]


def test_a_fired_anomaly_reports_anomalies_not_a_silence():
    firings = [
        {
            "code": "anomaly",
            "metric": "sessions",
            "connector": "google-analytics",
            "observed_value": 12000.0,
            "expected_value": 3000.0,
            "window_date": "2026-07-12",
            "firing_id": "fire_1",
            "pull_ids": [],
        }
    ]
    result = _briefing(
        readiness=[{"connector": "google-analytics", "metric": "sessions",
                    "observations": 30, "minimum_observations": 11, "armed": True}],
        alert_firings=firings,
    )
    assert result["anomaly_state"] == "anomalies_reported"
    assert result["anomalies_count"] == 1


def test_a_partially_armed_project_still_names_what_is_mute():
    result = _briefing(
        readiness=[
            {"connector": "google-analytics", "metric": "sessions",
             "observations": 30, "minimum_observations": 11, "armed": True},
            {"connector": "meta-ads", "metric": "cost",
             "observations": 4, "minimum_observations": 11, "armed": False},
        ]
    )
    assert result["anomaly_state"] == "no_anomaly"
    readiness = result["detector_readiness"]
    assert readiness["armed_count"] == 1
    assert readiness["insufficient_count"] == 1
    assert readiness["insufficient"][0]["label"] == "insufficient observations (4/11)"
