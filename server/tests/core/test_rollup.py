"""Tests for core.rollup — Story 6.4, AC9.

compute_rollup is the canonical builder of the per-metric rollup dict consumed by
narrative.build_narrative. Covers additive sums, non-additive impression-weighted
average_position, delta-vs-prior-period, and provenance assembly.
"""

from __future__ import annotations

import os

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core.rollup import compute_rollup  # noqa: E402


def test_public_api_names_exist():
    """AI-02 guard (review-9-1 F-1): cross-module callers use the PUBLIC names.

    Importing them by their public names here makes a future rename break loudly in
    CI rather than at runtime in cards.py / reports.py. The underscore aliases remain
    for backward compat.
    """
    from core import rollup as rollup_module

    assert callable(rollup_module.split_periods)
    assert callable(rollup_module.weighted_avg_position)
    # Backward-compat aliases still resolve to the same objects.
    assert rollup_module._split_periods is rollup_module.split_periods
    assert rollup_module._weighted_avg_position is rollup_module.weighted_avg_position


def _clicks_rows(dates_and_vals, connector="gsc", pull_id="pull_abc123"):
    return [
        {
            "date": d,
            "connector": connector,
            "metric": "clicks",
            "breakdown_dimension": "date",
            "breakdown_value": d,
            "value": float(v),
            "pull_id": pull_id,
            "loaded_at": f"{d}T00:00:00",
        }
        for d, v in dates_and_vals
    ]


# ---------------------------------------------------------------------------
# AC9 test_compute_rollup_basic
# ---------------------------------------------------------------------------

def test_compute_rollup_basic():
    """7 rows of clicks → value = sum, delta vs prior 7 days, pull_id present."""
    # Current period: Jul 8-14 (7 days), 10 clicks each = 70.
    current = _clicks_rows([(f"2026-07-{8+i:02d}", 10) for i in range(7)])
    # Prior period: Jul 1-7 (7 days), 8 clicks each = 56.
    prior = _clicks_rows([(f"2026-07-{1+i:02d}", 8) for i in range(7)])

    rollup = compute_rollup(
        current + prior,
        ["clicks"],
        "2026-07-08",
        "2026-07-14",
        "default",
        ["pull_abc123"],
    )

    assert "clicks" in rollup
    entry = rollup["clicks"]
    assert entry["value"] == 70.0
    # delta = 70 - 56 = 14
    assert entry["delta"] == 14.0
    # delta_pct = 14/56 = +25%
    assert entry["delta_pct"] == "+25%"
    assert entry["source_system"] == "gsc"
    assert entry["source_field"] == "clicks"
    assert entry["pull_id"] == "pull_abc123"
    assert entry["period"] == "sem. préc."


# ---------------------------------------------------------------------------
# AC9 test_compute_rollup_non_additive
# ---------------------------------------------------------------------------

def test_compute_rollup_non_additive():
    """average_position rows + impression rows → impression-weighted value (AD-4)."""
    rows = [
        {
            "date": "2026-07-08", "connector": "gsc", "metric": "average_position",
            "breakdown_dimension": "date", "breakdown_value": "2026-07-08",
            "value": 2.0, "pull_id": "pull_p", "loaded_at": "2026-07-08T00:00:00",
        },
        {
            "date": "2026-07-08", "connector": "gsc", "metric": "impressions",
            "breakdown_dimension": "date", "breakdown_value": "2026-07-08",
            "value": 900.0, "pull_id": "pull_p", "loaded_at": "2026-07-08T00:00:00",
        },
        {
            "date": "2026-07-09", "connector": "gsc", "metric": "average_position",
            "breakdown_dimension": "date", "breakdown_value": "2026-07-09",
            "value": 10.0, "pull_id": "pull_p", "loaded_at": "2026-07-09T00:00:00",
        },
        {
            "date": "2026-07-09", "connector": "gsc", "metric": "impressions",
            "breakdown_dimension": "date", "breakdown_value": "2026-07-09",
            "value": 100.0, "pull_id": "pull_p", "loaded_at": "2026-07-09T00:00:00",
        },
    ]
    rollup = compute_rollup(
        rows,
        ["average_position"],
        "2026-07-08",
        "2026-07-09",
        "default",
        ["pull_p"],
    )
    # Impression-weighted: (2*900 + 10*100) / (900+100) = (1800+1000)/1000 = 2.8
    # A naive mean would be (2+10)/2 = 6.0 — so weighting is proven.
    assert "average_position" in rollup
    assert abs(rollup["average_position"]["value"] - 2.8) < 1e-9


def test_compute_rollup_non_additive_not_summed():
    """average_position must never be a naive SUM (regression guard, AD-4)."""
    rows = [
        {
            "date": "2026-07-08", "connector": "gsc", "metric": "average_position",
            "breakdown_dimension": "date", "breakdown_value": "2026-07-08",
            "value": 3.0, "pull_id": "pull_p", "loaded_at": "2026-07-08T00:00:00",
        },
        {
            "date": "2026-07-09", "connector": "gsc", "metric": "average_position",
            "breakdown_dimension": "date", "breakdown_value": "2026-07-09",
            "value": 5.0, "pull_id": "pull_p", "loaded_at": "2026-07-09T00:00:00",
        },
    ]
    rollup = compute_rollup(
        rows, ["average_position"], "2026-07-08", "2026-07-09", "default", ["pull_p"]
    )
    # No impressions → simple mean (4.0), never the sum (8.0).
    assert rollup["average_position"]["value"] == 4.0


# ---------------------------------------------------------------------------
# AC9 test_compute_rollup_no_prior_data
# ---------------------------------------------------------------------------

def test_compute_rollup_no_prior_data():
    """Only current period rows → delta = None, delta_pct = None."""
    current = _clicks_rows([(f"2026-07-{8+i:02d}", 10) for i in range(7)])
    rollup = compute_rollup(
        current,
        ["clicks"],
        "2026-07-08",
        "2026-07-14",
        "default",
        ["pull_abc123"],
    )
    assert rollup["clicks"]["value"] == 70.0
    assert rollup["clicks"]["delta"] is None
    assert rollup["clicks"]["delta_pct"] is None


def test_compute_rollup_omits_metric_with_no_rows():
    """A metric with no rows in the current period is omitted from the rollup."""
    current = _clicks_rows([("2026-07-08", 10)])
    rollup = compute_rollup(
        current, ["clicks", "sessions"], "2026-07-08", "2026-07-08", "default", []
    )
    assert "clicks" in rollup
    assert "sessions" not in rollup


def test_compute_rollup_pull_id_most_recent_wins():
    """When multiple pulls contribute, the most recent (loaded_at) pull_id is cited."""
    rows = [
        {
            "date": "2026-07-08", "connector": "gsc", "metric": "clicks",
            "breakdown_dimension": "date", "breakdown_value": "2026-07-08",
            "value": 10.0, "pull_id": "pull_old", "loaded_at": "2026-07-08T00:00:00",
        },
        {
            "date": "2026-07-08", "connector": "gsc", "metric": "clicks",
            "breakdown_dimension": "date", "breakdown_value": "2026-07-08",
            "value": 12.0, "pull_id": "pull_new", "loaded_at": "2026-07-09T00:00:00",
        },
    ]
    rollup = compute_rollup(
        rows, ["clicks"], "2026-07-08", "2026-07-08", "default", []
    )
    assert rollup["clicks"]["pull_id"] == "pull_new"


# ---------------------------------------------------------------------------
# review-epic-10 CRITICAL-A: additive metric under N parallel breakdown partitions
# must be counted ONCE per connector, not N times.
# ---------------------------------------------------------------------------

def _breakdown_rows(metric, connector, dimension, dates, values_per_day, pull_id="pull_bk"):
    """Emit one row per (date, value) tuple under a single breakdown dimension.

    ``values_per_day`` maps a date -> list of (breakdown_value, value) pairs. Each pair is a
    row; the sum of the pairs' values for a day is that partition's day total (each parallel
    partition independently totals the SAME day).
    """
    out = []
    for d in dates:
        for bval, v in values_per_day[d]:
            out.append(
                {
                    "date": d,
                    "connector": connector,
                    "metric": metric,
                    "breakdown_dimension": dimension,
                    "breakdown_value": bval,
                    "value": float(v),
                    "pull_id": pull_id,
                    "loaded_at": f"{d}T00:00:00",
                }
            )
    return out


def test_compute_rollup_additive_not_inflated_by_parallel_breakdowns():
    """CRITICAL-A: sessions under 4 parallel breakdowns must NOT be summed x4.

    The LIVE smoke showed active_users hero = 888015 ~= 4 x 222361 because the metric
    exists under device_category + country + user_type + landing_page partitions that EACH
    total the same day. compute_rollup must pin to ONE canonical partition per connector and
    return the single-partition day total, not the x4 sum.
    """
    dates = ["2026-07-12", "2026-07-13", "2026-07-14"]
    # Each partition independently totals 1000/day => single-partition window total = 3000.
    # device_category: 3 devices summing to 1000/day, over all 3 days (full coverage).
    device = _breakdown_rows(
        "sessions", "ga4", "device_category", dates,
        {d: [("mobile", 600), ("desktop", 300), ("tablet", 100)] for d in dates},
    )
    # country: 2 countries summing to 1000/day, over all 3 days (full coverage).
    country = _breakdown_rows(
        "sessions", "ga4", "country", dates,
        {d: [("FR", 700), ("US", 300)] for d in dates},
    )
    # user_type: new/returning summing to 1000/day, over all 3 days (full coverage).
    user_type = _breakdown_rows(
        "sessions", "ga4", "user_type", dates,
        {d: [("new", 400), ("returning", 600)] for d in dates},
    )
    # landing_page: top-N pages summing to 1000/day, over all 3 days (full coverage).
    landing = _breakdown_rows(
        "sessions", "ga4", "landing_page", dates,
        {d: [("/", 500), ("/pricing", 300), ("/blog", 200)] for d in dates},
    )
    rows = device + country + user_type + landing

    rollup = compute_rollup(
        rows, ["sessions"], "2026-07-12", "2026-07-14", "default", ["pull_bk"]
    )
    assert "sessions" in rollup
    # ONE partition day total (1000) x 3 days = 3000 — NOT 4 x 3000 = 12000.
    assert rollup["sessions"]["value"] == 3000.0


def test_compute_rollup_prefers_higher_day_coverage_partition():
    """CRITICAL-A: a full-coverage partition wins over a sparse many-value one.

    landing_page here is a top-N partition present on only ONE day (poor day coverage). The
    device_category partition covers ALL days. The canonical selection must pick
    device_category (max distinct-day coverage), so the total reflects the full window, not
    the single landing_page day.
    """
    dates = ["2026-07-12", "2026-07-13", "2026-07-14"]
    # Full coverage: 500/day over 3 days = 1500.
    device = _breakdown_rows(
        "sessions", "ga4", "device_category", dates,
        {d: [("mobile", 300), ("desktop", 200)] for d in dates},
    )
    # Sparse: many landing pages but only ONE day present (would total only 500 for the
    # window if wrongly chosen). More ROWS than device_category, fewer distinct DAYS.
    landing = _breakdown_rows(
        "sessions", "ga4", "landing_page", ["2026-07-12"],
        {"2026-07-12": [(f"/p{i}", 100) for i in range(5)]},  # 5 rows, one day, sums 500
    )
    rows = device + landing

    rollup = compute_rollup(
        rows, ["sessions"], "2026-07-12", "2026-07-14", "default", ["pull_bk"]
    )
    # device_category chosen (3 distinct days beats landing_page's 1) => 1500, not 500.
    assert rollup["sessions"]["value"] == 1500.0


def test_compute_rollup_tie_break_lex_min_when_same_day_coverage():
    """review-10-6 F-3: the ONLY discriminating proof of the revised tie-break.

    Both partitions cover ALL days (equal day coverage). 'landing_page' has MORE rows/day
    but is top-N truncated: it sums only 300/day vs the day-complete 'country' at 1000/day.
    Under the OLD row-count tie-break landing_page won and the window undercounted
    (the live bug: active_users > sessions, rate 1.62). Lexicographic MIN must pick
    'country' ('c' < 'l') and total 3000 — a row-count regression would return 900.
    """
    dates = ["2026-07-12", "2026-07-13", "2026-07-14"]
    # country: 2 rows/day, totals the FULL day (1000) => window total 3000.
    country = _breakdown_rows(
        "sessions", "ga4", "country", dates,
        {d: [("FR", 700), ("US", 300)] for d in dates},
    )
    # landing_page: 5 rows/day (more rows), top-N bounded, sums only 300/day => 900.
    landing = _breakdown_rows(
        "sessions", "ga4", "landing_page", dates,
        {d: [(f"/p{i}", 60) for i in range(5)] for d in dates},
    )
    rows = country + landing

    rollup = compute_rollup(
        rows, ["sessions"], "2026-07-12", "2026-07-14", "default", ["pull_bk"]
    )
    assert rollup["sessions"]["value"] == 3000.0


def test_compute_rollup_single_partition_unchanged():
    """CRITICAL-A must NOT regress mono-partition fixtures: one breakdown => same sum."""
    dates = ["2026-07-12", "2026-07-13"]
    device = _breakdown_rows(
        "sessions", "ga4", "device_category", dates,
        {d: [("mobile", 600), ("desktop", 400)] for d in dates},  # 1000/day
    )
    rollup = compute_rollup(
        device, ["sessions"], "2026-07-12", "2026-07-13", "default", ["pull_bk"]
    )
    assert rollup["sessions"]["value"] == 2000.0


def test_compute_rollup_per_connector_partition_independent():
    """CRITICAL-A: canonical partition is chosen PER connector, not globally.

    Two connectors expose ``sessions`` under DIFFERENT breakdowns. Each connector's canonical
    partition totals its own day; the grand total is the SUM across connectors (each counted
    once), never a global single-partition drop of one connector.
    """
    dates = ["2026-07-12", "2026-07-13"]
    ga4 = _breakdown_rows(
        "sessions", "ga4", "device_category", dates,
        {d: [("mobile", 600), ("desktop", 400)] for d in dates},  # 1000/day => 2000
    )
    # ga4 also carries a parallel country partition (must NOT double count within ga4).
    ga4_country = _breakdown_rows(
        "sessions", "ga4", "country", dates,
        {d: [("FR", 1000)] for d in dates},  # same 1000/day under a parallel breakdown
    )
    other = _breakdown_rows(
        "sessions", "other", "landing_page", dates,
        {d: [("/", 500)] for d in dates}, pull_id="pull_other",  # 500/day => 1000
    )
    rows = ga4 + ga4_country + other

    rollup = compute_rollup(
        rows, ["sessions"], "2026-07-12", "2026-07-13", "default", ["pull_bk"]
    )
    # ga4 canonical = 2000 (one partition, not 4000) + other = 1000 => 3000.
    assert rollup["sessions"]["value"] == 3000.0


# ---------------------------------------------------------------------------
# Story 12.4: byte-identical invariance under a governed projection dimension.
#
# The 12.4 safe projection may add ONE governed dimension as a PARALLEL series.
# It must sort so canonical_breakdown_per_connector / MIN(breakdown_dimension)
# NEVER pin it, so compute_rollup's value is BYTE-IDENTICAL before and after the
# projection (the Epic-10 CRITICAL-A xN + CRITICAL-A2 tie-break invariants hold).
# ---------------------------------------------------------------------------

from core.rollup import canonical_breakdown_per_connector  # noqa: E402


def _ga4_base_rows(dates):
    """The pre-projection GA4 fixture: full-coverage 'country' + 'device_category'."""
    country = _breakdown_rows(
        "sessions", "ga4", "country", dates,
        {d: [("FR", 700), ("US", 300)] for d in dates},  # 1000/day
    )
    device = _breakdown_rows(
        "sessions", "ga4", "device_category", dates,
        {d: [("mobile", 600), ("desktop", 400)] for d in dates},  # 1000/day
    )
    return country + device


def test_rollup_byte_identical_after_governed_projection_dimension():
    """A governed projection dimension sorting AFTER 'country' leaves the value intact."""
    dates = ["2026-07-12", "2026-07-13", "2026-07-14"]
    before = _ga4_base_rows(dates)
    rollup_before = compute_rollup(
        before, ["sessions"], "2026-07-12", "2026-07-14", "default", ["pull_bk"]
    )
    # Projection adds a governed 'zzz_projected_dim' parallel series (sorts LAST,
    # so MIN never selects it). Same 1000/day total under the new partition.
    projected = _breakdown_rows(
        "sessions", "ga4", "zzz_projected_dim", dates,
        {d: [("a", 500), ("b", 500)] for d in dates},
    )
    after = before + projected
    rollup_after = compute_rollup(
        after, ["sessions"], "2026-07-12", "2026-07-14", "default", ["pull_bk"]
    )
    # BYTE-IDENTICAL value: 1000/day x 3 = 3000, unchanged by the parallel series.
    assert rollup_before["sessions"]["value"] == 3000.0
    assert rollup_after["sessions"]["value"] == rollup_before["sessions"]["value"]


def test_canonical_partition_pin_unchanged_by_governed_projection():
    """canonical_breakdown_per_connector still pins 'country' after the projection."""
    dates = ["2026-07-12", "2026-07-13", "2026-07-14"]
    before = _ga4_base_rows(dates)
    pin_before = canonical_breakdown_per_connector(before, "sessions")
    projected = _breakdown_rows(
        "sessions", "ga4", "zzz_projected_dim", dates,
        {d: [("a", 500), ("b", 500)] for d in dates},
    )
    pin_after = canonical_breakdown_per_connector(before + projected, "sessions")
    # The pinned canonical partition is unchanged (lexicographic MIN 'country').
    assert pin_before["ga4"] == "country"
    assert pin_after["ga4"] == pin_before["ga4"]


def test_projection_dim_sorting_before_canonical_WOULD_repin_and_inflate():
    """DISCRIMINATING negative: a governed dim sorting BEFORE 'country' re-pins.

    This is the exact hazard the compiler's governed_dim_shadows_canonical gate
    prevents (server/core/datastream_projection.py). If a governed projection
    dimension whose breakdown name sorts lexically at/before the connector's
    canonical ('aaa_dim' < 'country') is ALLOWED into fact_daily_kpi as a parallel
    series, canonical_breakdown_per_connector RE-PINS it (day-coverage ties,
    lexicographic MIN picks 'aaa_dim') and, if its per-day sum differs, the
    additive hero total shifts -- the Epic-10 xN failure mode. This test PROVES
    the rollup layer would mis-pin such a series, which is why the compiler MUST
    reject it upstream (proven in test_datastream_projection.py). The benign
    'zzz_projected_dim' case above (sorts last) never triggers this.
    """
    dates = ["2026-07-12", "2026-07-13", "2026-07-14"]
    before = _ga4_base_rows(dates)  # canonical pins 'country', total 3000
    assert canonical_breakdown_per_connector(before, "sessions")["ga4"] == "country"

    # A governed dim projected as 'aaa_dim' (sorts BEFORE 'country'). Give it a
    # DIFFERENT per-day sum (250/day) to show the total would shift if re-pinned.
    shadowing = _breakdown_rows(
        "sessions", "ga4", "aaa_dim", dates,
        {d: [("x", 150), ("y", 100)] for d in dates},  # 250/day, full coverage
    )
    after = before + shadowing
    # The rollup layer, given the shadowing series, RE-PINS to 'aaa_dim'...
    assert canonical_breakdown_per_connector(after, "sessions")["ga4"] == "aaa_dim"
    # ...and the additive total is corrupted (750, not the true 3000) -- exactly
    # the inflation/shift the compiler gate forbids by rejecting the projection.
    rollup_after = compute_rollup(
        after, ["sessions"], "2026-07-12", "2026-07-14", "default", ["pull_bk"]
    )
    rollup_before = compute_rollup(
        before, ["sessions"], "2026-07-12", "2026-07-14", "default", ["pull_bk"]
    )
    assert rollup_before["sessions"]["value"] == 3000.0
    assert rollup_after["sessions"]["value"] != rollup_before["sessions"]["value"]


# ---------------------------------------------------------------------------
# Story 12.5: byte-identical invariance across an ATOMIC PUBLICATION POINTER SWAP.
#
# The 12.5 publish is a POINTER SWAP over already-validated data -- it promotes a
# compiled candidate, it does NOT recompute metrics and it introduces NO new
# fact_daily_kpi partition. So compute_rollup's value and the canonical-partition
# pin are BYTE-IDENTICAL before and after a publish, provided the published
# candidate carries only the governed projection (the Epic-10 xN / CRITICAL-A2
# invariants 12.4 closed still hold). This test simulates the pointer swap by
# re-running the rollup over the SAME published fact rows (a pointer swap does not
# change fact_daily_kpi in v1 -- 12.5 owns only the Postgres pointer) and proves
# no double-count / re-pin sneaks in.
# ---------------------------------------------------------------------------


def test_rollup_byte_identical_across_publication_pointer_swap():
    dates = ["2026-07-12", "2026-07-13", "2026-07-14"]
    published_v1 = _ga4_base_rows(dates)  # the currently-published candidate
    rollup_v1 = compute_rollup(
        published_v1, ["sessions"], "2026-07-12", "2026-07-14", "default", ["pull_bk"]
    )
    pin_v1 = canonical_breakdown_per_connector(published_v1, "sessions")

    # A new candidate execution publishes: it carries the SAME governed projection
    # (same canonical 'country' partition, same day totals) -- the pointer swap
    # promotes it without recomputation and without a new partition MIN would re-pin.
    published_v2 = _ga4_base_rows(dates)
    rollup_v2 = compute_rollup(
        published_v2, ["sessions"], "2026-07-12", "2026-07-14", "default", ["pull_bk"]
    )
    pin_v2 = canonical_breakdown_per_connector(published_v2, "sessions")

    # BYTE-IDENTICAL: value and canonical pin unchanged by the pointer swap.
    assert rollup_v2["sessions"]["value"] == rollup_v1["sessions"]["value"] == 3000.0
    assert pin_v2["ga4"] == pin_v1["ga4"] == "country"
