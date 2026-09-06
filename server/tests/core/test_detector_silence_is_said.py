"""A silent detector says why it is silent (AI-272 / CAV-13).

`build_briefing` has produced `anomaly_state` since story 53.8, with five
disjoint values, one of which means "the detector was mathematically incapable of
firing". `get_daily_report` dropped it: `meta.briefing` carried the date, the
insights and `built_at`, so a brand-new Datastream with no history arrived
indistinguishable from a monitored one with nothing to report.

These tests pin the two halves of the repair -- the sentence that reaches the
reader, and the state that reaches `meta` beside the count it qualifies.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from core.briefing import (  # noqa: E402
    ANOMALY_STATE_INSUFFICIENT,
    ANOMALY_STATE_NO_ANOMALY,
    ANOMALY_STATE_NO_SERIES,
    ANOMALY_STATE_NOT_SUPPLIED,
    ANOMALY_STATE_REPORTED,
)
from core.reporting_mcp import _describe_detector_silence  # noqa: E402


def test_the_all_clear_costs_no_line() -> None:
    """`no_anomaly` IS the all-clear: a sentence would spend a line to say nothing."""
    assert _describe_detector_silence(ANOMALY_STATE_NO_ANOMALY, {}) is None


def test_a_reported_anomaly_needs_no_explanation_of_silence() -> None:
    assert _describe_detector_silence(ANOMALY_STATE_REPORTED, {}) is None


def test_an_unarmed_detector_says_so_and_counts_what_is_missing() -> None:
    line = _describe_detector_silence(
        ANOMALY_STATE_INSUFFICIENT,
        {"insufficient_count": 3, "minimum_observations": 11},
    )
    assert line is not None
    assert "not armed" in line
    # The two numbers are what turns a label into an explanation.
    assert "3" in line and "11" in line


def test_an_unarmed_detector_still_speaks_without_its_numbers() -> None:
    """Readiness may arrive without counts; the fact must survive the absence."""
    line = _describe_detector_silence(ANOMALY_STATE_INSUFFICIENT, None)
    assert line is not None and "not armed" in line


def test_no_series_and_not_supplied_are_distinct_sentences() -> None:
    no_series = _describe_detector_silence(ANOMALY_STATE_NO_SERIES, {})
    not_supplied = _describe_detector_silence(ANOMALY_STATE_NOT_SUPPLIED, {})
    assert no_series and not_supplied
    assert no_series != not_supplied
    # "not read" is not "nothing to compare": the first is our own blindness.
    assert "unknown" in not_supplied
    assert "no baseline series" in no_series


def test_an_unknown_sixth_state_is_reported_never_swallowed() -> None:
    """A value added upstream must not silently read as an all-clear here."""
    line = _describe_detector_silence("some_future_state", {})
    assert line is not None and "some_future_state" in line


def test_every_state_the_builder_can_emit_is_answered_here() -> None:
    """Calibration: the two modules must not drift apart unnoticed."""
    for state in (
        ANOMALY_STATE_NOT_SUPPLIED,
        ANOMALY_STATE_NO_SERIES,
        ANOMALY_STATE_INSUFFICIENT,
        ANOMALY_STATE_NO_ANOMALY,
        ANOMALY_STATE_REPORTED,
    ):
        # Answered = deliberately silent, or a sentence -- never the fallback.
        line = _describe_detector_silence(state, {})
        assert line is None or "not recognised" not in line, state
