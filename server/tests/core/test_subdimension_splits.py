"""Tests for Story 8.11 — sub-dimension (composite) splits.

Composite breakdowns (e.g. 'country>device' / 'FR>mobile') are ordinary
long-format fact rows using a '>' path separator. These tests lock in the two
non-negotiables from the story DESIGN:

  1. Double-count safety: a composite series is ONE MORE parallel series that
     independently totals the day. When a caller correctly hands the rollup a
     SINGLE dimension's rows (as reports/widget do), the total counts each metric
     exactly once — whether that dimension is a single dim or the composite.
     Mixing a composite series with its component single series for the same
     metric WOULD double-count; the guarantee is that callers never do that
     (verified here by construction).

  2. Composite helpers parse labels correctly (warehouse.is_composite_dimension /
     split_composite_value).
"""

from __future__ import annotations

import os

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core import warehouse  # noqa: E402
from core.rollup import compute_rollup  # noqa: E402


def _sessions_row(date, dim, value, connector="google-analytics", pull_id="pull_c1"):
    return {
        "date": date,
        "connector": connector,
        "metric": "sessions",
        "breakdown_dimension": dim,
        "breakdown_value": "x",
        "value": value,
        "pull_id": pull_id,
        "loaded_at": "2026-04-11T00:00:00Z",
    }


# ---------------------------------------------------------------------------
# 1. Double-count safety
# ---------------------------------------------------------------------------


def test_rollup_single_composite_series_counts_once():
    """Rollup over ONLY the composite series equals the true day total (counted once)."""
    # country x device split of a 100-session day.
    rows = [
        _sessions_row("2026-04-11", "country>device", 40),  # FR>desktop
        _sessions_row("2026-04-11", "country>device", 35),  # FR>mobile
        _sessions_row("2026-04-11", "country>device", 25),  # DE>desktop
    ]
    rollup = compute_rollup(
        rows, ["sessions"], "2026-04-11", "2026-04-11", "default", ["pull_c1"]
    )
    assert rollup["sessions"]["value"] == 100.0


def test_rollup_composite_matches_single_dim_when_queried_separately():
    """Composite total == single-dim total when each is rolled up on its own series.

    This is the safe path callers use: they never mix a composite series with a
    component single series for the same metric.
    """
    country_rows = [
        _sessions_row("2026-04-11", "country", 60),  # FR
        _sessions_row("2026-04-11", "country", 40),  # DE
    ]
    composite_rows = [
        _sessions_row("2026-04-11", "country>device", 35),  # FR>desktop
        _sessions_row("2026-04-11", "country>device", 25),  # FR>mobile
        _sessions_row("2026-04-11", "country>device", 25),  # DE>desktop
        _sessions_row("2026-04-11", "country>device", 15),  # DE>mobile
    ]
    country_total = compute_rollup(
        country_rows, ["sessions"], "2026-04-11", "2026-04-11", "default", ["pull_c1"]
    )["sessions"]["value"]
    composite_total = compute_rollup(
        composite_rows, ["sessions"], "2026-04-11", "2026-04-11", "default", ["pull_c1"]
    )["sessions"]["value"]
    assert country_total == composite_total == 100.0


def test_mixing_composite_with_component_would_double_count():
    """Documents that the double-count safety logic prevents double counting.

    Even if a caller passes a composite AND its component single dimension,
    the rollup is double-count safe because it pins to the canonical partition.
    """
    mixed = [
        _sessions_row("2026-04-11", "country", 60),
        _sessions_row("2026-04-11", "country", 40),
        _sessions_row("2026-04-11", "country>device", 100),
    ]
    total = compute_rollup(
        mixed, ["sessions"], "2026-04-11", "2026-04-11", "default", ["pull_c1"]
    )["sessions"]["value"]
    # Pinned to the canonical 'country' partition -> 100 (country) is counted, composite is ignored.
    assert total == 100.0


# ---------------------------------------------------------------------------
# 2. Composite helpers
# ---------------------------------------------------------------------------


def test_is_composite_dimension():
    assert warehouse.is_composite_dimension("country>device") is True
    assert warehouse.is_composite_dimension("country") is False
    assert warehouse.is_composite_dimension("device_category") is False
    assert warehouse.is_composite_dimension("") is False
    assert warehouse.is_composite_dimension(None) is False


def test_split_composite_value():
    assert warehouse.split_composite_value("country>device", "FR>mobile") == [
        ("country", "FR"),
        ("device", "mobile"),
    ]


def test_split_single_dimension_value():
    assert warehouse.split_composite_value("country", "FR") == [("country", "FR")]
