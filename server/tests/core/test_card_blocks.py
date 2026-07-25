"""Per-block DATA RESOLVER tests (Stories 9.3-9.6).

Each resolver is exercised from crafted long-format rows and asserted on BOTH shape and
MATH -- with an explicit focus on the data-correctness invariants:
  * AD-4: additive metrics summed; non-additive (average_position) impression-weighted,
    NEVER summed; CPA = SUM(cost)/SUM(conversions).
  * zero-division: CPA gauge with zero conversions -> value=null (empty state).
  * funnel pass-through rates.
  * degrade-never-raise: missing inputs -> empty payload, no exception.

Warehouse is not touched: resolvers operate on in-memory rows via a _BlockContext.
"""

from __future__ import annotations

import os

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core import cards as cards_module  # noqa: E402
from core import rollup as rollup_module  # noqa: E402


def _row(metric, dim, val, value, connector="gsc", date="2026-07-05"):
    return {
        "date": date,
        "connector": connector,
        "metric": metric,
        "breakdown_dimension": dim,
        "breakdown_value": val,
        "value": float(value),
        "pull_id": "pull_1",
        "loaded_at": f"{date}T00:00:00",
    }


def _ctx(rows, metrics, *, start="2026-07-01", end="2026-07-06", defs=None):
    """Build a _BlockContext with a real compute_rollup over the rows."""
    pull_ids = sorted({r.get("pull_id") for r in rows if r.get("pull_id")})
    rollup = rollup_module.compute_rollup(rows, metrics, start, end, "default", pull_ids)
    current, prior = rollup_module._split_periods(rows, start, end)
    return cards_module._BlockContext(
        current_rows=current,
        prior_rows=prior,
        rollup=rollup,
        metric_definitions=defs,
        resolved_metrics=metrics,
        start=start,
        end=end,
    )


# ---------------------------------------------------------------------------
# kpi_row
# ---------------------------------------------------------------------------


def test_kpi_row_shape_and_direction():
    rows = [
        _row("clicks", "page", "/a", 100),
        _row("clicks", "page", "/b", 50),
    ]
    ctx = _ctx(rows, ["clicks"], defs={"clicks": {"direction": "up_good"}})
    block = {"type": "kpi_row", "binding": {"metrics": "*"}}
    data = cards_module.resolve_block(block, ctx, "")["data"]
    assert data["metrics"][0]["metric"] == "clicks"
    # additive sum 100 + 50
    assert data["metrics"][0]["value"] == 150
    assert data["metrics"][0]["direction"] == "up_good"


# ---------------------------------------------------------------------------
# bar -- additive SUM + top-N ordering
# ---------------------------------------------------------------------------


def test_bar_sums_additive_metric_by_dimension_top_n_ordered():
    rows = [
        _row("clicks", "query", "chaussures", 10),
        _row("clicks", "query", "chaussures", 5),  # same group -> summed to 15
        _row("clicks", "query", "bottes", 40),
        _row("clicks", "query", "sandales", 2),
    ]
    ctx = _ctx(rows, ["clicks"])
    block = {"type": "bar", "binding": {"metrics": "clicks", "dimensions": ["query"]}}
    data = cards_module.resolve_block(block, ctx, "")["data"]
    assert data["dimension"] == "query"
    # sorted desc by value; chaussures summed to 15
    labels = [b["label"] for b in data["bars"]]
    values = {b["label"]: b["value"] for b in data["bars"]}
    assert labels[0] == "bottes" and values["bottes"] == 40
    assert values["chaussures"] == 15


# ---------------------------------------------------------------------------
# bar / aggregate -- NON-ADDITIVE average_position is impression-weighted, NOT summed
# ---------------------------------------------------------------------------


def test_bar_average_position_is_impression_weighted_not_summed():
    # Two pages: /a position 2 @ 100 impr, /b position 8 @ 900 impr.
    # Weighted overall would be (2*100 + 8*900)/1000 = 7.4; per-group it stays per-page.
    rows = [
        _row("average_position", "page", "/a", 2.0),
        _row("impressions", "page", "/a", 100),
        _row("average_position", "page", "/b", 8.0),
        _row("impressions", "page", "/b", 900),
    ]
    ctx = _ctx(rows, ["average_position", "impressions"])
    agg = cards_module._aggregate_by_group(ctx.current_rows, "average_position", "page")
    # Each group keeps its own weighted position (single row -> that value), NEVER summed.
    assert agg["/a"] == 2.0
    assert agg["/b"] == 8.0
    # Crucially not 10.0 (a naive sum of 2 + 8).
    assert 10.0 not in agg.values()


def test_average_position_weighted_across_days_same_group():
    # /a over two days: pos 2 @ 100 impr, pos 6 @ 300 impr -> weighted (2*100+6*300)/400 = 5.0
    rows = [
        _row("average_position", "page", "/a", 2.0, date="2026-07-04"),
        _row("impressions", "page", "/a", 100, date="2026-07-04"),
        _row("average_position", "page", "/a", 6.0, date="2026-07-05"),
        _row("impressions", "page", "/a", 300, date="2026-07-05"),
    ]
    ctx = _ctx(rows, ["average_position", "impressions"])
    agg = cards_module._aggregate_by_group(ctx.current_rows, "average_position", "page")
    assert abs(agg["/a"] - 5.0) < 1e-9  # weighted, not the simple mean 4.0


# ---------------------------------------------------------------------------
# donut -- share of additive metric; non-additive -> empty
# ---------------------------------------------------------------------------


def test_donut_share_pct_sums_to_100():
    rows = [
        _row("active_users", "device_category", "mobile", 300, connector="google-analytics"),
        _row("active_users", "device_category", "desktop", 100, connector="google-analytics"),
    ]
    ctx = _ctx(rows, ["active_users"])
    block = {
        "type": "donut",
        "binding": {"metrics": "active_users", "dimensions": ["device_category"]},
    }
    data = cards_module.resolve_block(block, ctx, "")["data"]
    assert data["total"] == 400
    pcts = {s["label"]: s["pct"] for s in data["slices"]}
    assert pcts["mobile"] == 75.0
    assert pcts["desktop"] == 25.0


def test_donut_aggregates_tail_into_autres_slice():
    """review-epic-9-backend F-7: with more than top-N sources, the remainder folds into a
    final 'Autres' slice so slice values sum to the TRUE total and pcts to ~100."""
    # 10 sources (> _DEFAULT_TOP_N=8) -> 7 named + 1 'Autres' aggregating the last 3.
    rows = []
    for i in range(10):
        rows.append(
            _row("active_users", "device_category", f"seg{i:02d}", (i + 1) * 10,
                 connector="google-analytics")
        )
    ctx = _ctx(rows, ["active_users"])
    block = {
        "type": "donut",
        "binding": {"metrics": "active_users", "dimensions": ["device_category"]},
    }
    data = cards_module.resolve_block(block, ctx, "")["data"]
    # top-N cap holds: 7 named + 1 'Autres' = 8 slices.
    assert len(data["slices"]) == cards_module._DEFAULT_TOP_N
    labels = [s["label"] for s in data["slices"]]
    assert labels[-1] == "Autres"
    # slice VALUES sum to the true total (no silent tail).
    total = sum((i + 1) * 10 for i in range(10))  # 550
    assert data["total"] == total
    assert abs(sum(s["value"] for s in data["slices"]) - total) < 1e-9
    # pcts sum to ~100.
    assert abs(sum(s["pct"] for s in data["slices"]) - 100.0) < 0.5


def test_donut_no_autres_when_within_top_n():
    """No 'Autres' slice is added when sources <= top-N (regression)."""
    rows = [
        _row("active_users", "device_category", "mobile", 300, connector="google-analytics"),
        _row("active_users", "device_category", "desktop", 100, connector="google-analytics"),
    ]
    ctx = _ctx(rows, ["active_users"])
    block = {
        "type": "donut",
        "binding": {"metrics": "active_users", "dimensions": ["device_category"]},
    }
    data = cards_module.resolve_block(block, ctx, "")["data"]
    assert "Autres" not in [s["label"] for s in data["slices"]]


def test_donut_non_additive_metric_is_empty():
    rows = [_row("average_position", "page", "/a", 2.0)]
    ctx = _ctx(rows, ["average_position"])
    block = {"type": "donut", "binding": {"metrics": "average_position", "dimensions": ["page"]}}
    data = cards_module.resolve_block(block, ctx, "")["data"]
    assert data["slices"] == []


# ---------------------------------------------------------------------------
# gauge -- CPA = SUM(cost)/SUM(conversions); zero conversions -> null
# ---------------------------------------------------------------------------


def test_gauge_cpa_ratio_sum_cost_over_sum_conversions():
    rows = [
        _row("cost", "connector", "meta", 200, connector="meta-ads"),
        _row("conversions", "connector", "meta", 8, connector="meta-ads"),
        _row("cost", "connector", "ga", 100, connector="google-analytics"),
        _row("conversions", "connector", "ga", 2, connector="google-analytics"),
    ]
    ctx = _ctx(rows, ["cost", "conversions"])
    block = {
        "type": "gauge",
        "title": "CPA vs objectif",
        "binding": {"metrics": "cpa", "numerator": "cost", "denominator": "conversions",
                    "direction": "down_good", "unit": "EUR"},
    }
    data = cards_module.resolve_block(block, ctx, "")["data"]
    # (200 + 100) / (8 + 2) = 30.0
    assert data["value"] == 30.0
    assert data["direction"] == "down_good"
    assert data["target_source"] == "default"  # no explicit target bound


def test_gauge_zero_conversions_yields_null_value():
    rows = [
        _row("cost", "connector", "meta", 200, connector="meta-ads"),
        _row("conversions", "connector", "meta", 0, connector="meta-ads"),
    ]
    ctx = _ctx(rows, ["cost", "conversions"])
    block = {
        "type": "gauge",
        "binding": {"metrics": "cpa", "numerator": "cost", "denominator": "conversions"},
    }
    data = cards_module.resolve_block(block, ctx, "")["data"]
    assert data["value"] is None  # never divides by zero


def test_gauge_explicit_target_source():
    rows = [
        _row("cost", "connector", "meta", 100, connector="meta-ads"),
        _row("conversions", "connector", "meta", 5, connector="meta-ads"),
    ]
    ctx = _ctx(rows, ["cost", "conversions"])
    block = {
        "type": "gauge",
        "binding": {"metrics": "cpa", "numerator": "cost", "denominator": "conversions",
                    "target": 25},
    }
    data = cards_module.resolve_block(block, ctx, "")["data"]
    assert data["value"] == 20.0
    assert data["target"] == 25
    assert data["target_source"] == "binding"


# ---------------------------------------------------------------------------
# funnel -- pass-through rates
# ---------------------------------------------------------------------------


def test_funnel_pass_through_rates():
    rows = [
        _row("sessions", "device_category", "mobile", 1000, connector="google-analytics"),
        _row("active_users", "device_category", "mobile", 800, connector="google-analytics"),
        _row("conversions", "device_category", "mobile", 200, connector="google-analytics"),
    ]
    ctx = _ctx(rows, ["sessions", "active_users", "conversions"])
    block = {"type": "funnel", "binding": {"steps": ["sessions", "active_users", "conversions"]}}
    data = cards_module.resolve_block(block, ctx, "")["data"]
    steps = data["steps"]
    assert [s["label"] for s in steps] == ["sessions", "active_users", "conversions"]
    assert steps[0]["rate"] == 1.0
    assert steps[1]["rate"] == 0.8  # 800 / 1000
    assert steps[2]["rate"] == 0.25  # 200 / 800
    assert data["overall_rate"] == 0.2  # 200 / 1000


def test_funnel_degrades_when_step_metric_absent():
    # active_users absent -> funnel degrades to 2 present stages (sessions, conversions).
    rows = [
        _row("sessions", "device_category", "mobile", 1000, connector="google-analytics"),
        _row("conversions", "device_category", "mobile", 200, connector="google-analytics"),
    ]
    ctx = _ctx(rows, ["sessions", "conversions"])
    block = {"type": "funnel", "binding": {"steps": ["sessions", "active_users", "conversions"]}}
    data = cards_module.resolve_block(block, ctx, "")["data"]
    labels = [s["label"] for s in data["steps"]]
    assert labels == ["sessions", "conversions"]
    assert data["steps"][1]["rate"] == 0.2


def test_funnel_empty_when_fewer_than_two_stages():
    rows = [_row("sessions", "device_category", "mobile", 1000, connector="google-analytics")]
    ctx = _ctx(rows, ["sessions"])
    block = {"type": "funnel", "binding": {"steps": ["sessions", "active_users", "conversions"]}}
    data = cards_module.resolve_block(block, ctx, "")["data"]
    assert data["steps"] == []


# ---------------------------------------------------------------------------
# table -- per-group aggregation incl. derived CPA; AD-4 weighted position
# ---------------------------------------------------------------------------


def test_table_by_source_derives_cpa_per_group():
    rows = [
        _row("conversions", "connector", "meta", 8, connector="meta-ads"),
        _row("cost", "connector", "meta", 240, connector="meta-ads"),
        _row("conversions", "connector", "ga", 4, connector="google-analytics"),
        _row("cost", "connector", "ga", 40, connector="google-analytics"),
    ]
    ctx = _ctx(rows, ["conversions", "cost"])
    block = {
        "type": "table",
        "binding": {"metrics": ["conversions", "cost", "cpa"], "dimensions": ["connector"]},
    }
    data = cards_module.resolve_block(block, ctx, "")["data"]
    by_dim = {r["_dim"]: r for r in data["rows"]}
    assert by_dim["meta-ads"]["cpa"] == 30.0  # 240 / 8
    assert by_dim["google-analytics"]["cpa"] == 10.0  # 40 / 4
    # sorted desc by first additive metric (conversions): meta-ads (8) before ga (4)
    assert data["rows"][0]["_dim"] == "meta-ads"
    # columns include the dimension first then metrics
    keys = [c["key"] for c in data["columns"]]
    assert keys[0] == "_dim"
    assert "cpa" in keys


# ---------------------------------------------------------------------------
# Opportunités table (keywords card, review-epic-9-integration F-1).
# Easy wins: top queries by impressions where impression-weighted position > 10.
# ---------------------------------------------------------------------------

_OPPORTUNITIES_BLOCK = {
    "type": "table",
    "title": "Opportunités",
    "binding": {
        "metrics": ["impressions", "average_position"],
        "dimensions": ["query", "page"],
        "opportunities": True,
    },
}


def test_opportunities_selects_high_impression_weak_position_queries():
    """Only queries with impression-weighted position > 10 appear, ordered by impressions."""
    rows = [
        # strong rank (pos 3) -> NOT an opportunity even with big impressions
        _row("average_position", "query", "brand", 3.0),
        _row("impressions", "query", "brand", 5000),
        # weak rank (pos 15) with big impressions -> the top opportunity
        _row("average_position", "query", "chaussures ete", 15.0),
        _row("impressions", "query", "chaussures ete", 3000),
        # weak rank (pos 12) with fewer impressions -> second opportunity
        _row("average_position", "query", "bottes cuir", 12.0),
        _row("impressions", "query", "bottes cuir", 800),
    ]
    ctx = _ctx(rows, ["impressions", "average_position"])
    data = cards_module.resolve_block(_OPPORTUNITIES_BLOCK, ctx, "")["data"]
    # Columns EXACTLY: Requête / Impressions / Position moyenne.
    assert [c["label"] for c in data["columns"]] == [
        "Requête", "Impressions", "Position moyenne"
    ]
    labels = [r["_dim"] for r in data["rows"]]
    assert "brand" not in labels  # strong rank excluded
    # ordered by impressions desc
    assert labels == ["chaussures ete", "bottes cuir"]
    assert data["rows"][0]["impressions"] == 3000
    assert data["rows"][0]["average_position"] == 15.0


def test_opportunities_position_is_impression_weighted():
    """The opportunity position is impression-weighted (AD-4), not a plain mean.

    'q' over two days: pos 8 @ 100 impr, pos 14 @ 900 impr.
    Weighted = (8*100 + 14*900)/1000 = 13.4 (> 10 -> opportunity).
    A plain mean would give (8+14)/2 = 11.0 -- both pass the threshold, but the DISPLAYED
    position must be the weighted 13.4, not 11.0.
    """
    rows = [
        _row("average_position", "query", "q", 8.0, date="2026-07-04"),
        _row("impressions", "query", "q", 100, date="2026-07-04"),
        _row("average_position", "query", "q", 14.0, date="2026-07-05"),
        _row("impressions", "query", "q", 900, date="2026-07-05"),
    ]
    ctx = _ctx(rows, ["impressions", "average_position"])
    data = cards_module.resolve_block(_OPPORTUNITIES_BLOCK, ctx, "")["data"]
    assert len(data["rows"]) == 1
    assert abs(data["rows"][0]["average_position"] - 13.4) < 0.01  # weighted, not mean 11.0
    assert data["rows"][0]["impressions"] == 1000


def test_opportunities_threshold_boundary_is_exclusive_at_10():
    """Position exactly 10 is NOT an opportunity (> 10 is strict); 10.01 is."""
    rows = [
        _row("average_position", "query", "exactly_10", 10.0),
        _row("impressions", "query", "exactly_10", 1000),
        _row("average_position", "query", "just_over", 10.01),
        _row("impressions", "query", "just_over", 1000),
    ]
    ctx = _ctx(rows, ["impressions", "average_position"])
    data = cards_module.resolve_block(_OPPORTUNITIES_BLOCK, ctx, "")["data"]
    labels = [r["_dim"] for r in data["rows"]]
    assert "exactly_10" not in labels  # boundary is exclusive
    assert "just_over" in labels


def test_opportunities_empty_is_designed_empty_table_never_absent():
    """No opportunity (all strong rank) -> the table still has its columns, rows=[] (never
    an absent block)."""
    rows = [
        _row("average_position", "query", "brand", 2.0),
        _row("impressions", "query", "brand", 5000),
    ]
    ctx = _ctx(rows, ["impressions", "average_position"])
    data = cards_module.resolve_block(_OPPORTUNITIES_BLOCK, ctx, "")["data"]
    assert data["rows"] == []
    assert [c["label"] for c in data["columns"]] == [
        "Requête", "Impressions", "Position moyenne"
    ]


def test_opportunities_capped_at_five():
    """Opportunities are capped at the top 5 by impressions."""
    rows = []
    for i in range(8):
        rows.append(_row("average_position", "query", f"q{i}", 12.0))
        rows.append(_row("impressions", "query", f"q{i}", (i + 1) * 100))
    ctx = _ctx(rows, ["impressions", "average_position"])
    data = cards_module.resolve_block(_OPPORTUNITIES_BLOCK, ctx, "")["data"]
    assert len(data["rows"]) == 5
    # top 5 by impressions desc: q7(800), q6(700), q5(600), q4(500), q3(400)
    assert [r["_dim"] for r in data["rows"]] == ["q7", "q6", "q5", "q4", "q3"]


def test_table_weighted_position_column_not_summed():
    rows = [
        _row("clicks", "query", "a", 10),
        _row("average_position", "query", "a", 3.0),
        _row("impressions", "query", "a", 100),
    ]
    ctx = _ctx(rows, ["clicks", "impressions", "average_position"])
    block = {
        "type": "table",
        "binding": {"metrics": ["clicks", "impressions", "average_position"],
                    "dimensions": ["query"]},
    }
    data = cards_module.resolve_block(block, ctx, "")["data"]
    row = data["rows"][0]
    assert row["average_position"] == 3.0  # weighted single row, not summed


# ---------------------------------------------------------------------------
# comment + line + degrade-never-raise
# ---------------------------------------------------------------------------


def test_comment_carries_rendered_text():
    ctx = _ctx([], ["clicks"])
    block = {"type": "comment", "binding": {"metrics": "*"}}
    data = cards_module.resolve_block(block, ctx, "Texte cite.")["data"]
    assert data["text"] == "Texte cite."


def test_sparkline_no_double_count_across_parallel_breakdowns():
    """review-epic-9-backend F-3: the sparkline sums a SINGLE canonical partition per day,
    never the naive cross-breakdown sum.

    active_users=100 for the day, present under BOTH device_category (mobile 60 + desktop
    40) AND country (FR 70 + US 30). A naive per-metric sum would give 200 for the day;
    the canonical-partition sparkline must give 100 (matching the KPI/donut/gauge totals).
    """
    rows = [
        _row("active_users", "device_category", "mobile", 60, connector="google-analytics"),
        _row("active_users", "device_category", "desktop", 40, connector="google-analytics"),
        _row("active_users", "country", "FR", 70, connector="google-analytics"),
        _row("active_users", "country", "US", 30, connector="google-analytics"),
    ]
    series = cards_module._sparkline_series(
        rows, "active_users", "2026-07-01", "2026-07-06"
    )
    assert len(series) == 1
    # single-partition day total = 100, NOT 200 (both breakdowns summed)
    assert series[0]["value"] == 100.0


def test_line_series_shape():
    rows = [
        _row("clicks", "page", "/a", 10, date="2026-07-04"),
        _row("clicks", "page", "/a", 20, date="2026-07-05"),
    ]
    ctx = _ctx(rows, ["clicks"])
    block = {"type": "line", "binding": {"metrics": "clicks"}}
    data = cards_module.resolve_block(block, ctx, "")["data"]
    assert data["series"][0]["name"] == "clicks"
    pts = data["series"][0]["points"]
    assert pts[0]["x"] == "2026-07-04" and pts[0]["y"] == 10


def test_missing_dimension_degrades_to_empty_bar():
    rows = [_row("clicks", "page", "/a", 10)]
    ctx = _ctx(rows, ["clicks"])
    # bound to a dimension not present in rows -> empty, no raise
    block = {"type": "bar", "binding": {"metrics": "clicks", "dimensions": ["query"]}}
    data = cards_module.resolve_block(block, ctx, "")["data"]
    assert data["bars"] == []


def test_unknown_block_type_returns_empty_payload():
    ctx = _ctx([], ["clicks"])
    block = {"type": "sankey", "binding": {"metrics": "*"}}
    out = cards_module.resolve_block(block, ctx, "")
    assert out["data"] == {}


# ---------------------------------------------------------------------------
# MULTI-BREAKDOWN double-count guard (review-9-3 F-2/F-3/F-4; review-9-1 F-3).
# The SAME metric under 2+ parallel breakdown dimensions for the same day/connector
# must total the SINGLE-partition sum, never N x that (each breakdown independently
# totals the day). These fixtures carry conversions/cost under BOTH device_category
# AND country for the same connector -- the exact real-world GA4/Meta shape.
# ---------------------------------------------------------------------------


def _dual_breakdown_conversions(*, connector="google-analytics"):
    """conversions=100 and cost=1000 for a day, each present under TWO parallel
    breakdowns (device_category totals 100 across mobile+desktop; country totals 100
    across FR+US). A single-partition total is 100 conversions / 1000 cost."""
    return [
        # device_category partition (sums to 100 conversions / 1000 cost)
        _row("conversions", "device_category", "mobile", 60, connector=connector),
        _row("conversions", "device_category", "desktop", 40, connector=connector),
        _row("cost", "device_category", "mobile", 600, connector=connector),
        _row("cost", "device_category", "desktop", 400, connector=connector),
        # country partition -- SAME 100 conversions / 1000 cost, different slicing
        _row("conversions", "country", "FR", 70, connector=connector),
        _row("conversions", "country", "US", 30, connector=connector),
        _row("cost", "country", "FR", 700, connector=connector),
        _row("cost", "country", "US", 300, connector=connector),
    ]


def test_sum_metric_connector_single_partition_no_double_count():
    rows = _dual_breakdown_conversions()
    ctx = _ctx(rows, ["conversions", "cost"])
    # Must equal the single-partition total (100), NOT 200 (both breakdowns summed).
    assert cards_module._sum_metric(ctx.current_rows, "conversions",
                                    cards_module.DIMENSION_CONNECTOR) == 100.0
    assert cards_module._sum_metric(ctx.current_rows, "cost",
                                    cards_module.DIMENSION_CONNECTOR) == 1000.0


def test_gauge_cpa_no_double_count_across_parallel_breakdowns():
    rows = _dual_breakdown_conversions()
    ctx = _ctx(rows, ["conversions", "cost"])
    block = {
        "type": "gauge",
        "binding": {"metrics": "cpa", "numerator": "cost", "denominator": "conversions",
                    "direction": "down_good", "unit": "EUR"},
    }
    data = cards_module.resolve_block(block, ctx, "")["data"]
    # CPA = 1000 / 100 = 10.0 (NOT 2000/200 which also = 10 -- but here numerator and
    # denominator would each double, so guard against a mis-partition returning e.g. 5.0).
    assert data["value"] == 10.0


def test_donut_no_double_count_across_parallel_breakdowns():
    rows = _dual_breakdown_conversions()
    ctx = _ctx(rows, ["conversions", "cost"])
    block = {
        "type": "donut",
        "binding": {"metrics": "conversions", "dimensions": ["connector"]},
    }
    data = cards_module.resolve_block(block, ctx, "")["data"]
    # ONE connector slice totalling 100 -- not 200.
    assert data["total"] == 100.0
    assert len(data["slices"]) == 1
    assert data["slices"][0]["value"] == 100.0


def test_table_by_source_no_double_count_across_parallel_breakdowns():
    rows = _dual_breakdown_conversions()
    ctx = _ctx(rows, ["conversions", "cost"])
    block = {
        "type": "table",
        "binding": {"metrics": ["conversions", "cost", "cpa"], "dimensions": ["connector"]},
    }
    data = cards_module.resolve_block(block, ctx, "")["data"]
    row = {r["_dim"]: r for r in data["rows"]}["google-analytics"]
    assert row["conversions"] == 100.0
    assert row["cost"] == 1000.0
    assert row["cpa"] == 10.0


def test_funnel_no_double_count_across_parallel_breakdowns():
    # sessions=1000 and conversions=100, each under device_category AND country.
    rows = [
        _row("sessions", "device_category", "mobile", 600, connector="google-analytics"),
        _row("sessions", "device_category", "desktop", 400, connector="google-analytics"),
        _row("sessions", "country", "FR", 700, connector="google-analytics"),
        _row("sessions", "country", "US", 300, connector="google-analytics"),
        _row("conversions", "device_category", "mobile", 60, connector="google-analytics"),
        _row("conversions", "device_category", "desktop", 40, connector="google-analytics"),
        _row("conversions", "country", "FR", 70, connector="google-analytics"),
        _row("conversions", "country", "US", 30, connector="google-analytics"),
    ]
    ctx = _ctx(rows, ["sessions", "conversions"])
    block = {"type": "funnel", "binding": {"steps": ["sessions", "conversions"]}}
    data = cards_module.resolve_block(block, ctx, "")["data"]
    steps = {s["label"]: s for s in data["steps"]}
    assert steps["sessions"]["value"] == 1000.0  # not 2000
    assert steps["conversions"]["value"] == 100.0  # not 200
    # rate stays a valid fraction, never a doubled-then-halved artifact
    assert steps["conversions"]["rate"] == 0.1
    assert data["overall_rate"] == 0.1


def test_canonical_partition_prefers_day_coverage_over_row_count():
    """review-epic-9-backend F-2: the canonical breakdown is chosen by DISTINCT-DAY
    coverage FIRST, not raw row count.

    device_category: 3 devices over 10 full days = 30 rows, covering 10 unique days.
    country: 30 countries over 2 days = 60 rows, covering only 2 unique days.
    Row-count would (wrongly) pick country (60 > 30) and undercount the window to 2 days.
    Day-coverage must pick device_category (10 days > 2 days) so the total covers the
    whole window.
    """
    rows = []
    # device_category: 10 days, each day active_users totals 100 across 3 devices.
    for day in range(1, 11):
        d = f"2026-07-{day:02d}"
        rows.append(_row("active_users", "device_category", "mobile", 50, date=d,
                         connector="google-analytics"))
        rows.append(_row("active_users", "device_category", "desktop", 30, date=d,
                         connector="google-analytics"))
        rows.append(_row("active_users", "device_category", "tablet", 20, date=d,
                         connector="google-analytics"))
    # country: only 2 days, but 30 countries/day -> 60 rows (more rows, fewer days).
    for d in ("2026-07-01", "2026-07-02"):
        for i in range(30):
            rows.append(_row("active_users", "country", f"C{i:02d}", 100 / 30, date=d,
                             connector="google-analytics"))

    canonical = cards_module._canonical_breakdown_per_connector(rows, "active_users")
    assert canonical["google-analytics"] == "device_category"
    # And the connector-grouped total covers all 10 days (1000), not the 2-day country sum.
    ctx = _ctx(rows, ["active_users"], start="2026-07-01", end="2026-07-11")
    total = cards_module._sum_metric(
        ctx.current_rows, "active_users", cards_module.DIMENSION_CONNECTOR
    )
    assert total == 1000.0  # 10 days x 100 (device partition), never the 2-day country slice


# ---------------------------------------------------------------------------
# Story 10.2: user_type donut -- FR labels + degrade (AC 1, 2, 3)
# ---------------------------------------------------------------------------


def _user_type_rows(connector="google-analytics", metric="active_users", date="2026-07-01"):
    """Three user_type breakdown rows (new/returning/unknown) — seed split 38/60/2 %."""
    return [
        _row(metric, "user_type", "new",       380.0, connector=connector, date=date),
        _row(metric, "user_type", "returning", 600.0, connector=connector, date=date),
        _row(metric, "user_type", "unknown",    20.0, connector=connector, date=date),
    ]


def test_usertypes_user_type_donut_present_labels_fr():
    """AC 1+2: donut bound to user_type renders FR labels (Nouveaux/Fideles/Indetermines)
    and slice pcts sum to ~100."""
    rows = _user_type_rows()
    ctx = _ctx(rows, ["active_users"])
    block = {
        "type": "donut",
        "title": "Nouveaux vs fideles",
        "binding": {"metrics": "active_users", "dimensions": ["user_type"]},
    }
    data = cards_module.resolve_block(block, ctx, "")["data"]

    assert data["total"] == 1000.0
    assert data["dimension"] == "user_type"
    labels = {s["label"] for s in data["slices"]}
    assert labels == {"Nouveaux", "Fidèles", "Indéterminés"}, (
        f"Expected FR labels but got: {labels}"
    )
    pct_sum = sum(s["pct"] for s in data["slices"])
    assert abs(pct_sum - 100.0) < 0.5, f"pcts should sum to ~100 but got {pct_sum}"
    # Check individual slices match the seed split.
    by_label = {s["label"]: s for s in data["slices"]}
    assert by_label["Fidèles"]["pct"] == 60.0
    assert by_label["Nouveaux"]["pct"] == 38.0
    assert by_label["Indéterminés"]["pct"] == 2.0


def test_usertypes_user_type_donut_unknown_raw_value_falls_back_to_string():
    """AC 2 (AD-9): an unrecognised breakdown_value is passed through as the raw string,
    never blank, never dropped."""
    rows = [
        _row("active_users", "user_type", "new",     600.0, connector="google-analytics"),
        _row("active_users", "user_type", "mystery",  400.0, connector="google-analytics"),
    ]
    ctx = _ctx(rows, ["active_users"])
    block = {
        "type": "donut",
        "binding": {"metrics": "active_users", "dimensions": ["user_type"]},
    }
    data = cards_module.resolve_block(block, ctx, "")["data"]
    labels = {s["label"] for s in data["slices"]}
    assert "Nouveaux" in labels   # "new" -> FR label
    assert "mystery" in labels    # unknown raw value passes through unchanged


def test_usertypes_degrades_without_user_type_rows():
    """AC 3 (degrade): when NO user_type rows are present in the mart, the user_type
    donut block resolves to an empty payload (no slices) — the card is still valid and
    the device donut still renders with its own rows."""
    device_rows = [
        _row("active_users", "device_category", "desktop", 800.0, connector="google-analytics"),
        _row("active_users", "device_category", "mobile",  200.0, connector="google-analytics"),
    ]
    ctx = _ctx(device_rows, ["active_users"])

    # user_type donut must degrade to empty — never raise.
    user_type_block = {
        "type": "donut",
        "title": "Nouveaux vs fideles",
        "binding": {"metrics": "active_users", "dimensions": ["user_type"]},
    }
    ut_data = cards_module.resolve_block(user_type_block, ctx, "")["data"]
    assert ut_data["slices"] == [], "user_type donut must be empty when no user_type rows"
    assert ut_data["dimension"] is None
    assert ut_data["total"] == 0.0

    # Device donut must still render normally (not affected by the absent user_type dim).
    device_block = {
        "type": "donut",
        "binding": {"metrics": "active_users", "dimensions": ["device_category"]},
    }
    dev_data = cards_module.resolve_block(device_block, ctx, "")["data"]
    assert dev_data["dimension"] == "device_category"
    assert len(dev_data["slices"]) == 2
    assert dev_data["total"] == 1000.0


def test_device_donut_labels_unchanged_by_user_type_label_map():
    """Regression: device_category values must NOT be translated through _USER_TYPE_LABELS.
    The label map is strictly bounded to dimension='user_type'."""
    rows = [
        _row("active_users", "device_category", "new",      300.0, connector="google-analytics"),
        _row("active_users", "device_category", "returning", 700.0, connector="google-analytics"),
    ]
    ctx = _ctx(rows, ["active_users"])
    block = {
        "type": "donut",
        "binding": {"metrics": "active_users", "dimensions": ["device_category"]},
    }
    data = cards_module.resolve_block(block, ctx, "")["data"]
    labels = {s["label"] for s in data["slices"]}
    # Raw strings preserved — must NOT be translated to "Nouveaux"/"Fideles".
    assert "new" in labels
    assert "returning" in labels
    assert "Nouveaux" not in labels
    assert "Fidèles" not in labels


def test_canonical_partition_tie_break_lex_min_after_day_coverage():
    """When two breakdowns cover the SAME days, the LEXICOGRAPHIC MIN name wins.

    Row count is deliberately NOT a criterion (review-epic-10: row count wrongly selected
    high-cardinality top-N-bounded partitions whose sums are partial by design). Here
    'country' also happens to carry more rows than 'device' — intentional: the test
    documents that the outcome comes from 'country' < 'device', not from row count.
    """
    rows = []
    # Both breakdowns cover the same 2 days. 'country' has 3 rows/day, 'device' has 2.
    for d in ("2026-07-01", "2026-07-02"):
        rows.append(_row("clicks", "device", "mobile", 10, date=d))
        rows.append(_row("clicks", "device", "desktop", 10, date=d))
        rows.append(_row("clicks", "country", "FR", 7, date=d))
        rows.append(_row("clicks", "country", "US", 7, date=d))
        rows.append(_row("clicks", "country", "DE", 6, date=d))
    canonical = cards_module._canonical_breakdown_per_connector(rows, "clicks")
    # Same day coverage (2 days); lexicographic MIN tie-break (review-epic-10) -> country wins.
    assert canonical["gsc"] == "country"


def test_canonical_partition_high_row_count_lex_greater_loses():
    """Discriminating case for the revised tie-break (review-10-6 F-3): a full-coverage but
    TOP-N-TRUNCATED partition ('landing_page', many rows, partial sums) must LOSE to the
    date-complete canonical partition ('country') at equal day coverage — under the old
    row-count rule 'landing_page' won and the funnel undercounted (active_users > sessions).
    """
    rows = []
    for d in ("2026-07-01", "2026-07-02"):
        # 'country': 2 rows/day, totals the full day (1000).
        rows.append(_row("sessions", "country", "FR", 600, date=d,
                         connector="google-analytics"))
        rows.append(_row("sessions", "country", "US", 400, date=d,
                         connector="google-analytics"))
        # 'landing_page': 5 rows/day (higher row count) but top-N bounded — sums only 300.
        for i in range(5):
            rows.append(_row("sessions", "landing_page", f"/p{i}", 60, date=d,
                             connector="google-analytics"))
    canonical = cards_module._canonical_breakdown_per_connector(rows, "sessions")
    assert canonical["google-analytics"] == "country"


def test_multiday_multibreakdown_gauge_cpa_no_inflation():
    """AI-45 lesson: multi-DAY + multi-breakdown must still total a single partition."""
    rows = []
    for d in ("2026-07-02", "2026-07-03", "2026-07-04"):
        rows += [
            _row("conversions", "device_category", "mobile", 20, date=d,
                 connector="google-analytics"),
            _row("conversions", "country", "FR", 20, date=d, connector="google-analytics"),
            _row("cost", "device_category", "mobile", 200, date=d,
                 connector="google-analytics"),
            _row("cost", "country", "FR", 200, date=d, connector="google-analytics"),
        ]
    ctx = _ctx(rows, ["conversions", "cost"], start="2026-07-01", end="2026-07-06")
    block = {
        "type": "gauge",
        "binding": {"metrics": "cpa", "numerator": "cost", "denominator": "conversions"},
    }
    data = cards_module.resolve_block(block, ctx, "")["data"]
    # 3 days x 20 conversions (one partition) = 60; 3 x 200 cost = 600 -> CPA 10.0.
    assert data["value"] == 10.0


# ---------------------------------------------------------------------------
# Ratio-of-sums for ctr/roas (review-9-2b F-8): NOT a mean of per-row ratios.
# ---------------------------------------------------------------------------


def test_ctr_aggregates_as_ratio_of_sums_not_mean():
    # /a: 10 clicks / 1000 impr (ctr 1%); /b: 90 clicks / 100 impr (ctr 90%).
    # Mean-of-rows would give (0.01+0.9)/2 = 45.5%; ratio-of-sums = 100/1100 = 9.09%.
    rows = [
        _row("clicks", "query", "a", 10),
        _row("impressions", "query", "a", 1000),
        _row("ctr", "query", "a", 0.01),
        _row("clicks", "query", "b", 90),
        _row("impressions", "query", "b", 100),
        _row("ctr", "query", "b", 0.90),
    ]
    ctx = _ctx(rows, ["clicks", "impressions", "ctr"])
    # Aggregate ctr by connector (single group) -> ratio-of-sums.
    agg = cards_module._aggregate_by_group(
        ctx.current_rows, "ctr", cards_module.DIMENSION_CONNECTOR
    )
    val = next(iter(agg.values()))
    assert abs(val - (100.0 / 1100.0)) < 1e-9  # ratio-of-sums, not the 0.455 mean


def test_gauge_cpa_prior_period_delta():
    """Prior-period CPA delta on the gauge block (review-9-3 F-5)."""
    # current window 2026-07-01..06: cost 300 / conv 10 -> CPA 30.
    # prior window 2026-06-25..30: cost 400 / conv 10 -> CPA 40. delta = -10.
    rows = [
        _row("cost", "device_category", "mobile", 300, date="2026-07-03",
             connector="google-analytics"),
        _row("conversions", "device_category", "mobile", 10, date="2026-07-03",
             connector="google-analytics"),
        _row("cost", "device_category", "mobile", 400, date="2026-06-27",
             connector="google-analytics"),
        _row("conversions", "device_category", "mobile", 10, date="2026-06-27",
             connector="google-analytics"),
    ]
    ctx = _ctx(rows, ["cost", "conversions"], start="2026-07-01", end="2026-07-06")
    block = {
        "type": "gauge",
        "binding": {"metrics": "cpa", "numerator": "cost", "denominator": "conversions"},
    }
    data = cards_module.resolve_block(block, ctx, "")["data"]
    assert data["value"] == 30.0
    assert data["prior_value"] == 40.0
    assert data["delta"] == -10.0
    assert data["delta_pct"] == -25.0


def test_gauge_cpa_empty_prior_delta_is_null_not_zero():
    """Empty prior window -> delta/delta_pct are None (not 0) so 'no comparison' shows."""
    rows = [
        _row("cost", "device_category", "mobile", 300, date="2026-07-03",
             connector="google-analytics"),
        _row("conversions", "device_category", "mobile", 10, date="2026-07-03",
             connector="google-analytics"),
    ]
    ctx = _ctx(rows, ["cost", "conversions"], start="2026-07-01", end="2026-07-06")
    block = {
        "type": "gauge",
        "binding": {"metrics": "cpa", "numerator": "cost", "denominator": "conversions"},
    }
    data = cards_module.resolve_block(block, ctx, "")["data"]
    assert data["value"] == 30.0
    assert data["prior_value"] is None
    assert data["delta"] is None
    assert data["delta_pct"] is None


# ---------------------------------------------------------------------------
# bar MOVERS (story 9.3) -- keywords rank-delta path
#
# Invariants:
#   * delta = prior_position - current_position (positive = rank gained/improved)
#   * impression-weighted (AD-4): never plain AVG
#   * |delta| >= 2 filter applied; queries in only one period are skipped
#   * sorted desc by |delta|, capped at _DEFAULT_TOP_N
#   * empty prior -> partial-state payload (bars=[]), no raise
# ---------------------------------------------------------------------------


def _pos_row(query, position, impressions, date="2026-07-05", connector="gsc"):
    """Convenience builder: one average_position + one impressions row for a query."""
    return [
        _row("average_position", "query", query, position, connector=connector, date=date),
        _row("impressions", "query", query, impressions, connector=connector, date=date),
    ]


def _movers_ctx(current_rows, prior_rows, *, start="2026-07-01", end="2026-07-06"):
    """Build a _BlockContext with explicit current/prior split (no recompute needed)."""
    # Combine for rollup (minimal; movers resolver reads ctx.current_rows/prior_rows directly).
    all_rows = current_rows + prior_rows
    pull_ids = sorted({r.get("pull_id") for r in all_rows if r.get("pull_id")})
    rollup = rollup_module.compute_rollup(
        all_rows, ["average_position", "impressions"], start, end, "default", pull_ids
    )
    return cards_module._BlockContext(
        current_rows=current_rows,
        prior_rows=prior_rows,
        rollup=rollup,
        metric_definitions=None,
        resolved_metrics=["average_position", "impressions"],
        start=start,
        end=end,
    )


_MOVERS_BLOCK = {
    "type": "bar",
    "title": "Requêtes en mouvement (±pos.)",
    "binding": {
        "metrics": "average_position",
        "dimensions": ["query", "page"],
        "movers": True,
        "direction": "down_good",
    },
}


def test_bar_movers_basic_delta_sign_and_filter():
    """Queries with |delta|>=2 appear; those below threshold are excluded."""
    # query "bottes": prior pos 6, current pos 3 -> delta +3 (improved)
    # query "sandales": prior pos 2, current pos 4 -> delta -2 (degraded, exactly at threshold)
    # query "chaussettes": prior pos 5, current pos 4.5 -> delta +0.5 (below threshold -> excluded)
    current = (
        _pos_row("bottes", 3.0, 500, date="2026-07-03")
        + _pos_row("sandales", 4.0, 200, date="2026-07-03")
        + _pos_row("chaussettes", 4.5, 100, date="2026-07-03")
    )
    prior = (
        _pos_row("bottes", 6.0, 400, date="2026-06-25")
        + _pos_row("sandales", 2.0, 150, date="2026-06-25")
        + _pos_row("chaussettes", 5.0, 90, date="2026-06-25")
    )
    ctx = _movers_ctx(current, prior)
    data = cards_module.resolve_block(_MOVERS_BLOCK, ctx, "")["data"]
    labels = [b["label"] for b in data["bars"]]
    # chaussettes excluded (|delta| 0.5 < 2)
    assert "chaussettes" not in labels
    assert "bottes" in labels
    assert "sandales" in labels
    # bottes: delta = 6 - 3 = +3.0 (improved -> "up")
    bottes = next(b for b in data["bars"] if b["label"] == "bottes")
    assert bottes["value"] == 3.0
    assert bottes["direction"] == "up"
    # sandales: delta = 2 - 4 = -2.0 (degraded -> "down")
    sandales = next(b for b in data["bars"] if b["label"] == "sandales")
    assert sandales["value"] == -2.0
    assert sandales["direction"] == "down"


def test_bar_movers_threshold_applied_on_rounded_delta():
    """review-epic-9-backend F-8: the |delta| >= 2 filter is applied on the ROUNDED delta
    so the displayed value and the filter agree.

    'included' has a raw delta of 1.996 which rounds to 2.00 -> must PASS (displayed 2,0).
    'excluded' has a raw delta of 1.994 which rounds to 1.99 -> must be filtered.
    """
    current = (
        _pos_row("included", 3.004, 100, date="2026-07-03")
        + _pos_row("excluded", 3.006, 100, date="2026-07-03")
    )
    prior = (
        _pos_row("included", 5.0, 100, date="2026-06-25")   # delta 1.996 -> round 2.00
        + _pos_row("excluded", 5.0, 100, date="2026-06-25")  # delta 1.994 -> round 1.99
    )
    ctx = _movers_ctx(current, prior)
    data = cards_module.resolve_block(_MOVERS_BLOCK, ctx, "")["data"]
    labels = {b["label"]: b["value"] for b in data["bars"]}
    assert "included" in labels
    assert labels["included"] == 2.0  # the displayed value equals the threshold
    assert "excluded" not in labels  # rounds to 1.99 < 2.0 -> filtered


def test_bar_movers_sorted_by_abs_delta_desc():
    """Results are ordered by |delta| descending, not by raw value."""
    current = (
        _pos_row("a", 1.0, 100, date="2026-07-03")   # delta = prior(8) - 1 = +7
        + _pos_row("b", 3.0, 100, date="2026-07-03")   # delta = prior(7) - 3 = +4
        + _pos_row("c", 10.0, 100, date="2026-07-03")  # delta = prior(2) - 10 = -8
    )
    prior = (
        _pos_row("a", 8.0, 100, date="2026-06-25")
        + _pos_row("b", 7.0, 100, date="2026-06-25")
        + _pos_row("c", 2.0, 100, date="2026-06-25")
    )
    ctx = _movers_ctx(current, prior)
    data = cards_module.resolve_block(_MOVERS_BLOCK, ctx, "")["data"]
    abs_deltas = [abs(b["value"]) for b in data["bars"]]
    assert abs_deltas == sorted(abs_deltas, reverse=True)
    # c has |delta|=8 (the biggest), must be first
    assert data["bars"][0]["label"] == "c"


def test_bar_movers_impression_weighted_not_plain_avg():
    """Movers delta uses impression-weighted average_position (AD-4), not plain AVG.

    Query "bottes": current period has two days with different impression counts.
      day1: pos 2.0 @ 900 impr -> weight 900
      day2: pos 8.0 @ 100 impr -> weight 100
      weighted current = (2*900 + 8*100) / 1000 = 2.6

    Prior period: one day pos 8.0 @ 500 impr -> prior = 8.0
    delta (weighted) = 8.0 - 2.6 = 5.4

    A plain-AVG resolver would give current = (2+8)/2 = 5.0 -> delta = 3.0.
    This test asserts the weighted value (5.4), not the plain-avg value (3.0).
    """
    current = [
        _row("average_position", "query", "bottes", 2.0, date="2026-07-02"),
        _row("impressions",       "query", "bottes", 900, date="2026-07-02"),
        _row("average_position", "query", "bottes", 8.0, date="2026-07-03"),
        _row("impressions",       "query", "bottes", 100, date="2026-07-03"),
    ]
    prior = [
        _row("average_position", "query", "bottes", 8.0, date="2026-06-25"),
        _row("impressions",       "query", "bottes", 500, date="2026-06-25"),
    ]
    ctx = _movers_ctx(current, prior)
    data = cards_module.resolve_block(_MOVERS_BLOCK, ctx, "")["data"]
    assert len(data["bars"]) == 1
    bottes = data["bars"][0]
    # weighted delta = 8.0 - 2.6 = 5.4; plain-avg would give 3.0
    assert abs(bottes["value"] - 5.4) < 0.01, f"Expected ~5.4 (weighted), got {bottes['value']}"
    assert bottes["direction"] == "up"


def test_bar_movers_single_period_query_skipped():
    """A query present in only one period must NOT appear in movers (no fake delta)."""
    current = _pos_row("bottes", 3.0, 500, date="2026-07-03")
    # "trail" is only in current, not in prior -> must be skipped
    current += _pos_row("trail", 5.0, 200, date="2026-07-03")
    prior = _pos_row("bottes", 9.0, 400, date="2026-06-25")
    # "old" is only in prior, not in current -> must be skipped
    prior += _pos_row("old", 2.0, 300, date="2026-06-25")
    ctx = _movers_ctx(current, prior)
    data = cards_module.resolve_block(_MOVERS_BLOCK, ctx, "")["data"]
    labels = [b["label"] for b in data["bars"]]
    assert "trail" not in labels, "current-only query must not appear in movers"
    assert "old" not in labels, "prior-only query must not appear in movers"
    assert "bottes" in labels  # |delta| = 9-3=6 -> included


def test_bar_movers_empty_prior_returns_partial_state():
    """No prior-period data -> bars=[] with partial=True (designed empty state, no raise)."""
    current = _pos_row("bottes", 3.0, 500, date="2026-07-03")
    ctx = _movers_ctx(current, [])
    data = cards_module.resolve_block(_MOVERS_BLOCK, ctx, "")["data"]
    assert data["bars"] == []
    assert data.get("partial") is True


def test_bar_movers_no_query_reaches_threshold_returns_empty_bars():
    """All deltas < 2 -> empty bars (designed empty state), no raise."""
    current = _pos_row("bottes", 3.0, 500, date="2026-07-03")
    # prior pos 3.5 -> delta = 0.5 (below threshold)
    prior = _pos_row("bottes", 3.5, 400, date="2026-06-25")
    ctx = _movers_ctx(current, prior)
    data = cards_module.resolve_block(_MOVERS_BLOCK, ctx, "")["data"]
    assert data["bars"] == []


def test_bar_movers_capped_at_top_n():
    """Movers are capped at _DEFAULT_TOP_N (8) ordered by |delta| desc."""
    current_rows = []
    prior_rows = []
    for i in range(12):
        q = f"query_{i:02d}"
        current_rows += _pos_row(q, float(i + 1), 100, date="2026-07-03")
        prior_rows += _pos_row(q, float(i + 1) + 3.0, 100, date="2026-06-25")
    ctx = _movers_ctx(current_rows, prior_rows)
    data = cards_module.resolve_block(_MOVERS_BLOCK, ctx, "")["data"]
    assert len(data["bars"]) <= cards_module._DEFAULT_TOP_N


def test_bar_movers_multi_breakdown_no_double_count():
    """Multi-breakdown rows for the same query are not double-counted (review-9-3 F-4).

    Each query has average_position rows under BOTH 'query' AND 'page' breakdown
    dimensions for the same day. The movers resolver pins to the 'query' dimension
    (first present in binding.dimensions) and must not inflate the impression weight.
    """
    # "bottes" appears under breakdown_dimension=query (50 impr) AND page (50 impr).
    # The resolver should use only the query-dimension rows -> 50 impr weight.
    current = [
        _row("average_position", "query", "bottes", 3.0, date="2026-07-03"),
        _row("impressions",       "query", "bottes", 50, date="2026-07-03"),
        _row("average_position", "page",  "/shoes",  3.0, date="2026-07-03"),
        _row("impressions",       "page",  "/shoes",  50, date="2026-07-03"),
    ]
    prior = [
        _row("average_position", "query", "bottes", 7.0, date="2026-06-25"),
        _row("impressions",       "query", "bottes", 40, date="2026-06-25"),
    ]
    ctx = _movers_ctx(current, prior)
    data = cards_module.resolve_block(_MOVERS_BLOCK, ctx, "")["data"]
    # delta = 7 - 3 = +4.0 (based purely on the query-dimension partition)
    assert len(data["bars"]) == 1
    assert data["bars"][0]["label"] == "bottes"
    assert abs(data["bars"][0]["value"] - 4.0) < 0.01


def test_bar_movers_multi_day_range():
    """Multi-day current and prior periods aggregate correctly (AI-45)."""
    # "bottes": 3 current days each with pos 3.0 @ 100 impr -> weighted current = 3.0
    # "bottes": 3 prior days each with pos 8.0 @ 100 impr -> weighted prior = 8.0
    # delta = 8.0 - 3.0 = +5.0
    current_rows = []
    prior_rows = []
    for d in ("2026-07-02", "2026-07-03", "2026-07-04"):
        current_rows += _pos_row("bottes", 3.0, 100, date=d)
    for d in ("2026-06-25", "2026-06-26", "2026-06-27"):
        prior_rows += _pos_row("bottes", 8.0, 100, date=d)
    ctx = _movers_ctx(current_rows, prior_rows)
    data = cards_module.resolve_block(_MOVERS_BLOCK, ctx, "")["data"]
    assert len(data["bars"]) == 1
    assert abs(data["bars"][0]["value"] - 5.0) < 0.01
    assert data["bars"][0]["direction"] == "up"


def test_bar_non_movers_binding_uses_generic_top_n_path():
    """A bar block without movers=True still uses the generic top-N path (unchanged)."""
    rows = [
        _row("clicks", "query", "bottes", 40),
        _row("clicks", "query", "trail", 10),
    ]
    ctx = _ctx(rows, ["clicks"])
    block = {"type": "bar", "binding": {"metrics": "clicks", "dimensions": ["query"]}}
    data = cards_module.resolve_block(block, ctx, "")["data"]
    assert data["bars"][0]["label"] == "bottes"  # top by clicks, not a delta


# ---------------------------------------------------------------------------
# Cannibalisation table (keywords card, Story 10.5).
# Reads the JOINT query>page composite grain (breakdown_dimension='query>page',
# breakdown_value='<query>>​<page>') -- NOT the marginal query/page rows.
# ---------------------------------------------------------------------------

_CANNIB_BLOCK = {
    "type": "table",
    "title": "Cannibalisation",
    "binding": {
        "metrics": ["impressions", "average_position"],
        "dimensions": ["query", "page"],
        "cannibalisation": True,
    },
}


def _qp_rows(query, page, impressions, position, *, date="2026-07-05"):
    """A composite query>page pair: impressions + average_position rows co-located."""
    val = f"{query}>{page}"
    return [
        {
            "date": date, "connector": "gsc", "metric": "impressions",
            "breakdown_dimension": "query>page", "breakdown_value": val,
            "value": float(impressions), "pull_id": "pull_1",
            "loaded_at": f"{date}T00:00:00",
        },
        {
            "date": date, "connector": "gsc", "metric": "average_position",
            "breakdown_dimension": "query>page", "breakdown_value": val,
            "value": float(position), "pull_id": "pull_1",
            "loaded_at": f"{date}T00:00:00",
        },
    ]


def _cannib_ctx(rows, *, config=None):
    ctx = _ctx(rows, ["impressions", "average_position"])
    ctx.config = config or {}
    return ctx


def test_cannib_net_case():
    """50/50 split with position gap >= 2 -> query flagged; both pages emitted."""
    rows = []
    for d in ("2026-07-03", "2026-07-04", "2026-07-05"):
        rows += _qp_rows("chaussures", "/sport/", 500, 4.5, date=d)
        rows += _qp_rows("chaussures", "/running/", 500, 8.5, date=d)
    data = cards_module.resolve_block(_CANNIB_BLOCK, _cannib_ctx(rows), "")["data"]
    assert data["columns"] == ["Requête", "Page", "Part (%)", "Position moy."]
    assert data["empty_label"] == "Aucune cannibalisation détectée"
    queries = {r["Requête"] for r in data["rows"]}
    assert "chaussures" in queries
    pages = {r["Page"] for r in data["rows"]}
    assert pages == {"/sport/", "/running/"}
    # Shares ~50/50 (each 500/1000).
    shares = {r["Page"]: r["Part (%)"] for r in data["rows"]}
    assert shares["/sport/"] == 50.0
    assert shares["/running/"] == 50.0
    # Weighted position per page preserved (single value per page).
    positions = {r["Page"]: r["Position moy."] for r in data["rows"]}
    assert positions["/sport/"] == 4.5
    assert positions["/running/"] == 8.5


def test_cannib_negative_case():
    """95/5 split -> the minority page is below the 20% share threshold -> NOT flagged."""
    rows = []
    for d in ("2026-07-03", "2026-07-04", "2026-07-05"):
        rows += _qp_rows("brand", "/home/", 950, 2.0, date=d)
        rows += _qp_rows("brand", "/other/", 50, 9.0, date=d)
    data = cards_module.resolve_block(_CANNIB_BLOCK, _cannib_ctx(rows), "")["data"]
    assert data["rows"] == []  # designed empty; block present, never absent


def test_cannib_threshold_override():
    """Raising cannib_min_share_pct to 0.30 makes a 25/75 split no longer flagged."""
    rows = []
    for d in ("2026-07-03", "2026-07-04", "2026-07-05"):
        rows += _qp_rows("q", "/a/", 250, 4.0, date=d)   # 25% share
        rows += _qp_rows("q", "/b/", 750, 9.0, date=d)   # 75% share
    # Default (0.20): /a/ 25% >= 20% -> both qualify -> flagged.
    data_default = cards_module.resolve_block(_CANNIB_BLOCK, _cannib_ctx(rows), "")["data"]
    assert {r["Page"] for r in data_default["rows"]} == {"/a/", "/b/"}
    # Override 0.30: /a/ 25% < 30% -> only one qualifying page -> NOT flagged.
    ctx_over = _cannib_ctx(rows, config={"cannib_min_share_pct": 0.30})
    data_over = cards_module.resolve_block(_CANNIB_BLOCK, ctx_over, "")["data"]
    assert data_over["rows"] == []


def test_cannib_position_gap_threshold_override():
    """Raising cannib_min_position_gap suppresses a small-gap split (gap-driven path)."""
    rows = []
    for d in ("2026-07-03", "2026-07-04", "2026-07-05"):
        rows += _qp_rows("q", "/a/", 500, 4.0, date=d)
        rows += _qp_rows("q", "/b/", 500, 5.0, date=d)  # gap = 1.0
    # Default gap 2.0: gap 1.0 < 2.0 -> not flagged even at 50/50.
    data_default = cards_module.resolve_block(_CANNIB_BLOCK, _cannib_ctx(rows), "")["data"]
    assert data_default["rows"] == []
    # Lower the gap to 0.5 -> now flagged.
    ctx_over = _cannib_ctx(rows, config={"cannib_min_position_gap": 0.5})
    data_over = cards_module.resolve_block(_CANNIB_BLOCK, ctx_over, "")["data"]
    assert {r["Page"] for r in data_over["rows"]} == {"/a/", "/b/"}


def test_cannib_empty_state_no_query_rows():
    """No query>page rows (datastream off) -> empty block, no exception (dégradé propre)."""
    # Only marginal page rows present (the pre-10.5 world).
    rows = [
        _row("impressions", "page", "/a", 1000),
        _row("clicks", "page", "/a", 50),
    ]
    ctx = _cannib_ctx(rows)
    data = cards_module.resolve_block(_CANNIB_BLOCK, ctx, "")["data"]
    assert data["rows"] == []
    assert data["empty_label"] == "Aucune cannibalisation détectée"
    assert data["columns"] == ["Requête", "Page", "Part (%)", "Position moy."]


def test_cannib_reads_joint_grain_not_marginals():
    """Guard: marginal query + page rows (no join) must NEVER flag cannibalisation.

    This is the whole design point -- marginal breakdown rows cannot express which page
    belongs to which query, so the detector (which reads the query>page composite) sees
    nothing and returns empty even when both marginal dimensions are richly populated.
    """
    rows = [
        # marginal query rows (summed over pages) -- no page association
        _row("impressions", "query", "chaussures", 1000),
        _row("average_position", "query", "chaussures", 6.0),
        # marginal page rows (summed over queries) -- no query association
        _row("impressions", "page", "/sport/", 550),
        _row("impressions", "page", "/running/", 450),
        _row("average_position", "page", "/sport/", 4.5),
        _row("average_position", "page", "/running/", 8.5),
    ]
    ctx = _cannib_ctx(rows)
    flagged = cards_module._detect_cannibalisation(ctx)
    assert flagged == []  # no query>page composite -> nothing to detect
