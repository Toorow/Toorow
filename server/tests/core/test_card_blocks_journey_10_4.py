"""Story 10.4 -- Journey card: entry-pages bar + top-pages table block tests.

Covers:
  1. Entry-pages bar: sessions by landing_page rows -> bar payload with top-5.
  2. Top-pages table: screen_page_views by page rows -> table with Part column.
  3. Part % against the TOP-N SUBTOTAL (not the day total) -- parts sum to ~100%.
  4. Connector scope: GA4 'page' rows only; GSC 'page' rows excluded (AC 4).
  5. Degrade: no landing_page/page rows -> both blocks empty; funnel + card valid (AC 5).
  6. Journey template composition: 2 new blocks present (bar after kpi_row, table after bar).
  7. build_journey_comment: cites top entry page when landing_page bar present (AC 6).
  8. build_journey_comment: unchanged drop-off line when no landing_page bar (regression).

ASCII-only (AI-03). No live mart required; warehouse mocked.
"""

from __future__ import annotations

import os

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from unittest.mock import patch  # noqa: E402

import pytest  # noqa: E402
from core import cards as cards_module  # noqa: E402
from core import narrative as narrative_module  # noqa: E402
from core import rollup as rollup_module  # noqa: E402

_GA = "google-analytics"

# ---------------------------------------------------------------------------
# Row helpers
# ---------------------------------------------------------------------------


def _row(metric, dim, val, value, connector=_GA, date="2026-07-05"):
    return {
        "date": date,
        "connector": connector,
        "metric": metric,
        "breakdown_dimension": dim,
        "breakdown_value": val,
        "value": float(value),
        "pull_id": "pull_10_4",
        "loaded_at": f"{date}T00:00:00",
    }


def _funnel_rows(date="2026-07-05"):
    """Minimal journey funnel rows (sessions + conversions)."""
    return [
        _row("sessions", "device_category", "desktop", 1000.0, date=date),
        _row("sessions", "device_category", "mobile", 600.0, date=date),
        _row("active_users", "device_category", "desktop", 800.0, date=date),
        _row("active_users", "device_category", "mobile", 450.0, date=date),
        _row("conversions", "device_category", "desktop", 50.0, date=date),
        _row("conversions", "device_category", "mobile", 30.0, date=date),
    ]


def _landing_rows(date="2026-07-05"):
    """GA4 landing_page partition rows (sessions by landing_page)."""
    return [
        _row("sessions", "landing_page", "/", 500.0, connector=_GA, date=date),
        _row("sessions", "landing_page", "/pricing", 200.0, connector=_GA, date=date),
        _row("sessions", "landing_page", "/features", 150.0, connector=_GA, date=date),
        _row("sessions", "landing_page", "/blog", 90.0, connector=_GA, date=date),
        _row("sessions", "landing_page", "/contact", 60.0, connector=_GA, date=date),
    ]


def _page_rows(date="2026-07-05"):
    """GA4 page partition rows (screen_page_views by page)."""
    return [
        _row("screen_page_views", "page", "/", 3000.0, connector=_GA, date=date),
        _row("screen_page_views", "page", "/blog", 1200.0, connector=_GA, date=date),
        _row("screen_page_views", "page", "/pricing", 800.0, connector=_GA, date=date),
        _row("screen_page_views", "page", "/features", 600.0, connector=_GA, date=date),
        _row("screen_page_views", "page", "/docs", 400.0, connector=_GA, date=date),
    ]


def _page_rows_many(date="2026-07-05", n=12):
    """GA4 page partition with MORE than the display cap (_DEFAULT_TOP_N=8) distinct
    pages, so the top-N-subtotal share (« Part ») and the display truncation can be
    distinguished (review 10.4 F-2)."""
    return [
        _row("screen_page_views", "page", f"/p{i}", float(1000 - i * 50),
             connector=_GA, date=date)
        for i in range(n)
    ]


def _gsc_page_rows(date="2026-07-05"):
    """GSC 'page' partition rows (clicks + impressions by page, connector='gsc').
    These must NOT appear in the journey top-pages block (GA4 only -- AC 4).
    """
    return [
        _row("clicks", "page", "/", 120.0, connector="gsc", date=date),
        _row("impressions", "page", "/", 4500.0, connector="gsc", date=date),
        _row("clicks", "page", "/pricing", 45.0, connector="gsc", date=date),
        _row("impressions", "page", "/pricing", 1800.0, connector="gsc", date=date),
    ]


def _ctx(rows, metrics, *, start="2026-07-01", end="2026-07-06"):
    pull_ids = sorted({r.get("pull_id") for r in rows if r.get("pull_id")})
    roll = rollup_module.compute_rollup(rows, metrics, start, end, "default", pull_ids)
    current, prior = rollup_module.split_periods(rows, start, end)
    return cards_module._BlockContext(
        current_rows=current,
        prior_rows=prior,
        rollup=roll,
        metric_definitions=None,
        resolved_metrics=metrics,
        start=start,
        end=end,
    )


# ---------------------------------------------------------------------------
# 1. Entry-pages bar block
# ---------------------------------------------------------------------------


class TestEntryPagesBar:
    """AC 1: bar block bound to sessions/landing_page, top 5, GA4-scoped."""

    def _bar_block(self):
        tpl = cards_module.get_template("journey")
        return next(
            b for b in tpl.composition
            if b.get("type") == "bar" and b.get("title") == "Pages d'entrée"
        )

    def test_bar_block_present_in_journey_composition(self):
        """AC 1: journey template has a bar block titled 'Pages d'entree'."""
        tpl = cards_module.get_template("journey")
        bar_blocks = [b for b in tpl.composition if b.get("type") == "bar"]
        assert bar_blocks, "No bar block in journey composition"
        titles = [b.get("title", "") for b in bar_blocks]
        assert any("Pages d" in t for t in titles), (
            f"No 'Pages d...' bar found; titles={titles}"
        )

    def test_bar_block_binding_sessions_landing_page(self):
        """AC 1: bar block is bound to sessions/landing_page with top_n=5."""
        block = self._bar_block()
        binding = block.get("binding") or {}
        assert binding.get("metrics") == "sessions"
        assert "landing_page" in (binding.get("dimensions") or [])
        assert binding.get("top_n") == 5
        assert binding.get("connector_scope") == "google-analytics"

    def test_bar_resolves_landing_page_rows(self):
        """AC 1: bar block yields bars from landing_page rows sorted by sessions."""
        rows = _funnel_rows() + _landing_rows()
        ctx = _ctx(rows, ["sessions", "conversions", "screen_page_views"])
        block = self._bar_block()
        resolved = cards_module.resolve_block(block, ctx, "")
        data = resolved.get("data") or {}
        bars = data.get("bars") or []
        assert bars, "Expected non-empty bars from landing_page rows"
        # Top bar should be '/' with 500 sessions
        assert bars[0]["label"] == "/"
        assert bars[0]["value"] == pytest.approx(500.0)
        # At most top_n=5 bars
        assert len(bars) <= 5

    def test_bar_empty_when_no_landing_page_rows(self):
        """AC 5: no landing_page rows -> bar data is empty (designed empty state)."""
        rows = _funnel_rows()  # no landing_page rows
        ctx = _ctx(rows, ["sessions", "conversions"])
        block = self._bar_block()
        resolved = cards_module.resolve_block(block, ctx, "")
        data = resolved.get("data") or {}
        bars = data.get("bars") or []
        assert bars == [], f"Expected empty bars without landing_page rows, got {bars}"


# ---------------------------------------------------------------------------
# 2. Top-pages table block
# ---------------------------------------------------------------------------


class TestTopPagesTable:
    """AC 2: table block bound to screen_page_views/page with Part column."""

    def _table_block(self):
        tpl = cards_module.get_template("journey")
        return next(
            b for b in tpl.composition
            if b.get("type") == "table" and b.get("title") == "Pages les plus vues"
        )

    def test_table_block_present_in_journey_composition(self):
        """AC 2: journey template has a table block titled 'Pages les plus vues'."""
        tpl = cards_module.get_template("journey")
        table_blocks = [b for b in tpl.composition if b.get("type") == "table"]
        assert table_blocks, "No table block in journey composition"
        titles = [b.get("title", "") for b in table_blocks]
        assert any("Pages les plus vues" in t for t in titles), (
            f"'Pages les plus vues' not found; titles={titles}"
        )

    def test_table_block_binding_screen_page_views_page(self):
        """AC 2: table block is bound to screen_page_views/page with subtotal_share."""
        block = self._table_block()
        binding = block.get("binding") or {}
        assert binding.get("metrics") == "screen_page_views"
        assert "page" in (binding.get("dimensions") or [])
        assert binding.get("subtotal_share") is True
        assert binding.get("connector_scope") == "google-analytics"

    def test_table_resolves_page_rows_with_part_column(self):
        """AC 2: table block yields rows with Page, Vues, Part columns."""
        rows = _funnel_rows() + _page_rows()
        ctx = _ctx(rows, ["sessions", "conversions", "screen_page_views"])
        block = self._table_block()
        resolved = cards_module.resolve_block(block, ctx, "")
        data = resolved.get("data") or {}
        columns = data.get("columns") or []
        rows_out = data.get("rows") or []
        col_labels = [c.get("label") for c in columns]
        assert "Page" in col_labels, f"'Page' column missing; columns={col_labels}"
        assert "Vues" in col_labels, f"'Vues' column missing; columns={col_labels}"
        assert "Part" in col_labels, f"'Part' column missing; columns={col_labels}"
        assert rows_out, "Expected non-empty rows from page rows"
        # Top row should be '/' with 3000 views
        assert rows_out[0]["_dim"] == "/"
        assert rows_out[0]["screen_page_views"] == pytest.approx(3000.0)

    def test_table_empty_when_no_page_rows(self):
        """AC 5: no page rows -> table has empty rows list."""
        rows = _funnel_rows()
        ctx = _ctx(rows, ["sessions", "conversions"])
        block = self._table_block()
        resolved = cards_module.resolve_block(block, ctx, "")
        data = resolved.get("data") or {}
        rows_out = data.get("rows") or []
        assert rows_out == [], f"Expected empty rows without page rows, got {rows_out}"


# ---------------------------------------------------------------------------
# 3. Part % denominator: TOP-N SUBTOTAL (AC 3, the critical invariant)
# ---------------------------------------------------------------------------


class TestPartSubtotalDenominator:
    """AC 3: 'Part' column is computed against the SUM of the returned page rows
    (top-N subtotal), NOT the connector daily total. Parts must sum to ~100%."""

    def _table_block(self):
        tpl = cards_module.get_template("journey")
        return next(
            b for b in tpl.composition
            if b.get("type") == "table" and b.get("title") == "Pages les plus vues"
        )

    def test_part_pct_denominator_is_top_n_subtotal_not_day_total(self):
        """AC 3 (critical): « Part » = share of the TOP-N SUBTOTAL, NOT the connector
        daily total.

        When the page pool fits inside the display cap (<= _DEFAULT_TOP_N), all rows are
        shown so the displayed parts sum to ~100%. If the denominator were the connector
        daily total, they would sum to < 100% (the top-N page rows do not sum to the day
        total -- the long tail is dropped). Here the 5 seeded pages are all displayed.
        """
        # 5 page rows: total screen_page_views = 3000+1200+800+600+400 = 6000
        rows = _funnel_rows() + _page_rows()
        ctx = _ctx(rows, ["sessions", "conversions", "screen_page_views"])
        block = self._table_block()
        resolved = cards_module.resolve_block(block, ctx, "")
        data = resolved.get("data") or {}
        rows_out = data.get("rows") or []
        assert rows_out, "Expected non-empty page rows"
        total_pct = sum(r.get("part_pct") or 0.0 for r in rows_out)
        # 5 pages <= display cap -> all shown -> parts sum to ~100%.
        assert abs(total_pct - 100.0) < 1.0, (
            f"Part percentages do not sum to ~100% when the pool fits the display cap "
            f"(sum={total_pct:.2f}%). Denominator must be the top-N subtotal, "
            f"not the connector daily total. rows={rows_out}"
        )

    def test_displayed_parts_below_100_when_pool_exceeds_display_cap(self):
        """AC 3 / review 10.4 F-2: « Part » is a share of the FULL top-N subtotal (all
        aggregated pages), not of the displayed subset. When the pool has MORE than the
        display cap, only the top rows are shown and their parts sum to < 100% -- the
        remaining top-N pages are the real tail. Each displayed part still equals
        value / full_subtotal."""
        rows = _funnel_rows() + _page_rows_many(n=12)
        ctx = _ctx(rows, ["sessions", "conversions", "screen_page_views"])
        block = self._table_block()
        resolved = cards_module.resolve_block(block, ctx, "")
        data = resolved.get("data") or {}
        rows_out = data.get("rows") or []
        subtotal = data.get("subtotal", 0.0)
        # subtotal covers ALL 12 pages, not just the displayed rows.
        assert subtotal == pytest.approx(sum(1000.0 - i * 50 for i in range(12)))
        # display is capped -> fewer rows than the pool.
        assert 0 < len(rows_out) < 12
        # displayed parts sum to < 100% (the tail beyond the cap is real).
        total_pct = sum(r.get("part_pct") or 0.0 for r in rows_out)
        assert total_pct < 100.0, (
            f"Displayed parts should sum to < 100% when the pool exceeds the display cap "
            f"(sum={total_pct:.2f}%) -- part is a share of the full top-N subtotal."
        )
        # each displayed part is still value / full_subtotal.
        for r in rows_out:
            expected = round((r["screen_page_views"] / subtotal) * 100.0, 1)
            assert abs(r["part_pct"] - expected) < 0.05

    def test_subtotal_field_in_payload(self):
        """AC 3: the table payload carries 'subtotal' (the denominator used)."""
        rows = _funnel_rows() + _page_rows()
        ctx = _ctx(rows, ["sessions", "conversions", "screen_page_views"])
        block = self._table_block()
        resolved = cards_module.resolve_block(block, ctx, "")
        data = resolved.get("data") or {}
        assert "subtotal" in data, "Expected 'subtotal' key in table data payload"
        subtotal = data["subtotal"]
        assert subtotal == pytest.approx(6000.0), (
            f"Subtotal should be SUM of all page rows (6000); got {subtotal}"
        )

    def test_part_pct_individual_values_correct(self):
        """AC 3: individual Part values match value/subtotal*100."""
        rows = _funnel_rows() + _page_rows()
        ctx = _ctx(rows, ["sessions", "conversions", "screen_page_views"])
        block = self._table_block()
        resolved = cards_module.resolve_block(block, ctx, "")
        data = resolved.get("data") or {}
        rows_out = data.get("rows") or []
        subtotal = data.get("subtotal", 0.0)
        for r in rows_out:
            expected_pct = round((r["screen_page_views"] / subtotal) * 100.0, 1)
            assert abs(r["part_pct"] - expected_pct) < 0.05, (
                f"Part for {r['_dim']}: got {r['part_pct']}, expected {expected_pct}"
            )


# ---------------------------------------------------------------------------
# 4. Connector scope: GA4 page rows only; GSC 'page' rows excluded (AC 4)
# ---------------------------------------------------------------------------


class TestConnectorScopeGA4:
    """AC 4: the top-pages table reads ONLY connector='google-analytics' rows.
    GSC 'page' rows (connector='gsc') must never appear in the output.
    """

    def _table_block(self):
        tpl = cards_module.get_template("journey")
        return next(
            b for b in tpl.composition
            if b.get("type") == "table" and b.get("title") == "Pages les plus vues"
        )

    def _bar_block(self):
        tpl = cards_module.get_template("journey")
        return next(
            b for b in tpl.composition
            if b.get("type") == "bar" and b.get("title") == "Pages d'entrée"
        )

    def test_top_pages_table_excludes_gsc_page_rows(self):
        """AC 4: when BOTH GA4 and GSC 'page' rows are present, the table shows GA4 only.

        GSC page rows carry clicks/impressions (metric screen_page_views does not exist
        in GSC). The connector_scope binding ensures only GA4 rows are aggregated, so
        the output rows are exclusively from connector='google-analytics'.
        """
        rows = _funnel_rows() + _page_rows() + _gsc_page_rows()
        ctx = _ctx(rows, ["sessions", "conversions", "screen_page_views"])
        block = self._table_block()
        resolved = cards_module.resolve_block(block, ctx, "")
        data = resolved.get("data") or {}
        rows_out = data.get("rows") or []
        assert rows_out, "Expected GA4 page rows in the output"
        # All output rows must have come from screen_page_views (GA4-only metric).
        # GA4 page rows: ['/', '/blog', '/pricing', '/features', '/docs']
        output_pages = {r["_dim"] for r in rows_out}
        ga4_pages = {"/", "/blog", "/pricing", "/features", "/docs"}
        assert output_pages.issubset(ga4_pages), (
            f"Some pages are not from GA4: {output_pages - ga4_pages}"
        )
        # The GSC metric 'clicks'/'impressions' must not appear in the columns.
        col_keys = {c.get("key") for c in (data.get("columns") or [])}
        assert "clicks" not in col_keys, "GSC 'clicks' column must not appear in top-pages table"
        assert "impressions" not in col_keys, (
            "GSC 'impressions' column must not appear in top-pages table"
        )

    def test_top_pages_table_with_only_gsc_rows_is_empty(self):
        """AC 4: with ONLY GSC page rows, the table is empty (no GA4 screen_page_views)."""
        rows = _funnel_rows() + _gsc_page_rows()
        ctx = _ctx(rows, ["sessions", "conversions"])
        block = self._table_block()
        resolved = cards_module.resolve_block(block, ctx, "")
        data = resolved.get("data") or {}
        rows_out = data.get("rows") or []
        assert rows_out == [], (
            f"Table should be empty when only GSC page rows present; got {rows_out}"
        )

    def test_entry_bar_excludes_gsc_rows(self):
        """AC 4: the entry bar only uses GA4 landing_page rows (no GSC contamination)."""
        # Add some GSC 'landing_page' rows to confirm they would not be picked up.
        gsc_landing = [
            _row("clicks", "landing_page", "/gsc-only", 50.0, connector="gsc"),
        ]
        rows = _funnel_rows() + _landing_rows() + gsc_landing
        ctx = _ctx(rows, ["sessions", "conversions", "screen_page_views"])
        block = self._bar_block()
        resolved = cards_module.resolve_block(block, ctx, "")
        data = resolved.get("data") or {}
        bars = data.get("bars") or []
        bar_labels = {b["label"] for b in bars}
        assert "/gsc-only" not in bar_labels, (
            "GSC landing_page row appeared in the GA4-scoped entry bar"
        )


# ---------------------------------------------------------------------------
# 5. Degrade: no pages rows -> both blocks empty, card valid, funnel intact (AC 5)
# ---------------------------------------------------------------------------


class TestDegrade:
    """AC 5: when pages datastreams are off (no landing_page/page rows),
    both new blocks render their empty state and the card remains fully valid.
    """

    def test_card_renders_without_page_rows(self):
        """AC 5: get_card with journey template and no page rows must not raise."""
        rows = _funnel_rows()
        with patch("core.warehouse.query_daily_report", return_value=rows):
            try:
                _s, env, _uri = cards_module.get_card(
                    [], "default", template="journey",
                    metrics=["sessions", "conversions"],
                    date_from="2026-07-01", date_to="2026-07-06",
                )
            except Exception as exc:
                pytest.fail(f"get_card raised without page rows: {exc}")
        composition = env["data"]["composition"]
        assert composition, "Composition must not be empty"

    def test_funnel_intact_without_page_rows(self):
        """AC 5: funnel block is still resolved correctly when no page rows are present."""
        rows = _funnel_rows()
        with patch("core.warehouse.query_daily_report", return_value=rows):
            _s, env, _uri = cards_module.get_card(
                [], "default", template="journey",
                metrics=["sessions", "conversions"],
                date_from="2026-07-01", date_to="2026-07-06",
            )
        composition = env["data"]["composition"]
        funnel_blocks = [b for b in composition if b.get("type") == "funnel"]
        assert funnel_blocks, "Funnel block missing from composition"
        funnel_data = funnel_blocks[0].get("data") or {}
        steps = funnel_data.get("steps") or []
        assert steps, "Funnel must have steps from sessions/conversions rows"

    def test_bar_empty_without_page_rows(self):
        """AC 5: entry bar block is empty (bars=[]) when no landing_page rows."""
        rows = _funnel_rows()
        ctx = _ctx(rows, ["sessions", "conversions"])
        tpl = cards_module.get_template("journey")
        bar_block = next(
            b for b in tpl.composition
            if b.get("type") == "bar" and b.get("title") == "Pages d'entrée"
        )
        resolved = cards_module.resolve_block(bar_block, ctx, "")
        data = resolved.get("data") or {}
        assert (data.get("bars") or []) == []

    def test_table_empty_without_page_rows(self):
        """AC 5: top-pages table block is empty (rows=[]) when no page rows."""
        rows = _funnel_rows()
        ctx = _ctx(rows, ["sessions", "conversions"])
        tpl = cards_module.get_template("journey")
        table_block = next(
            b for b in tpl.composition
            if b.get("type") == "table" and b.get("title") == "Pages les plus vues"
        )
        resolved = cards_module.resolve_block(table_block, ctx, "")
        data = resolved.get("data") or {}
        assert (data.get("rows") or []) == []


# ---------------------------------------------------------------------------
# 6. Journey template composition order (AC 1/2: bar after kpi_row, table after bar)
# ---------------------------------------------------------------------------


def test_journey_composition_order():
    """AC 1/2: check that bar(Pages d'entree) comes after kpi_row and before comment,
    and table(Pages les plus vues) comes after bar and before comment.
    """
    tpl = cards_module.get_template("journey")
    types = [b.get("type") for b in tpl.composition]
    kpi_idx = types.index("kpi_row")
    bar_idx = next(
        i for i, b in enumerate(tpl.composition)
        if b.get("type") == "bar" and b.get("title") == "Pages d'entrée"
    )
    table_idx = next(
        i for i, b in enumerate(tpl.composition)
        if b.get("type") == "table" and b.get("title") == "Pages les plus vues"
    )
    comment_idx = len(types) - 1  # comment is last
    assert types[comment_idx] == "comment", "Last block must be comment"
    assert kpi_idx < bar_idx < table_idx < comment_idx, (
        f"Expected order: funnel < kpi_row < bar < table < comment; "
        f"got kpi={kpi_idx}, bar={bar_idx}, table={table_idx}, comment={comment_idx}"
    )


def test_journey_optional_dimensions_includes_landing_page_and_page():
    """AC 1/2: journey template optional_dimensions includes landing_page and page."""
    tpl = cards_module.get_template("journey")
    assert "landing_page" in tpl.optional_dimensions, (
        "landing_page not in optional_dimensions"
    )
    assert "page" in tpl.optional_dimensions, "page not in optional_dimensions"


def test_journey_required_dimensions_still_empty():
    """AC 5: required_dimensions must stay () so the card is servable without pages."""
    tpl = cards_module.get_template("journey")
    assert tpl.required_dimensions == (), (
        f"required_dimensions changed; expected () got {tpl.required_dimensions!r}"
    )


# ---------------------------------------------------------------------------
# 7. build_journey_comment: cites top entry page when landing_page bar present
# ---------------------------------------------------------------------------


class TestJourneyCommentWithEntryPage:
    """AC 6: build_journey_comment cites the top entry page when the bar is present."""

    def _block_data_with_landing(self):
        rows = _funnel_rows() + _landing_rows()
        ctx = _ctx(rows, ["sessions", "conversions"])
        tpl = cards_module.get_template("journey")
        block_data: dict = {}
        for block in tpl.composition:
            if block.get("type") == "comment":
                continue
            resolved = cards_module.resolve_block(block, ctx, "")
            btype = resolved.get("type")
            if btype and btype not in block_data:
                block_data[btype] = resolved.get("data") or {}
        return block_data

    def _rollup(self, rows):
        return rollup_module.compute_rollup(
            rows, ["sessions", "conversions"], "2026-07-01", "2026-07-06",
            "default", ["pull_10_4"]
        )

    def test_cites_top_entry_page(self):
        """AC 6: line 2 cites the first entry page name and session count."""
        rows = _funnel_rows() + _landing_rows()
        bd = self._block_data_with_landing()
        roll = self._rollup(rows)
        comment = narrative_module.build_journey_comment(
            block_data=bd, rollup=roll, context_events=[], pull_ids=["pull_10_4"]
        )
        # The top landing page is '/' with 500 sessions
        assert "/" in comment, f"Top entry page '/' not found in comment: {comment!r}"
        assert "Premi" in comment, (
            f"'Premiere page d'entree' phrase not found in comment: {comment!r}"
        )
        # 500 < 1000 -> no thousands separator inserted by _format_number.
        assert "500" in comment, (
            f"Session count for top page not cited: {comment!r}"
        )

    def test_comment_still_cites_dropoff_on_line_1(self):
        """AC 6: line 1 still cites the biggest drop-off (regression guard)."""
        rows = _funnel_rows() + _landing_rows()
        bd = self._block_data_with_landing()
        roll = self._rollup(rows)
        comment = narrative_module.build_journey_comment(
            block_data=bd, rollup=roll, context_events=[], pull_ids=["pull_10_4"]
        )
        assert "Décroché" in comment or "taux de passage" in comment, (
            f"Drop-off line not found in comment: {comment!r}"
        )

    def test_comment_at_most_3_lines(self):
        """AC 6: comment stays <= 3 lines with landing_page bar."""
        rows = _funnel_rows() + _landing_rows()
        bd = self._block_data_with_landing()
        roll = self._rollup(rows)
        comment = narrative_module.build_journey_comment(
            block_data=bd, rollup=roll, context_events=[], pull_ids=["pull_10_4"]
        )
        assert len(comment.splitlines()) <= 3

    def test_comment_no_landing_falls_back_to_overall_rate(self):
        """AC 6 regression: when no landing_page bar, line 2 falls back to overall rate."""
        rows = _funnel_rows()
        ctx = _ctx(rows, ["sessions", "conversions"])
        tpl = cards_module.get_template("journey")
        block_data: dict = {}
        for block in tpl.composition:
            if block.get("type") == "comment":
                continue
            resolved = cards_module.resolve_block(block, ctx, "")
            btype = resolved.get("type")
            if btype and btype not in block_data:
                block_data[btype] = resolved.get("data") or {}
        roll = self._rollup(rows)
        comment = narrative_module.build_journey_comment(
            block_data=block_data, rollup=roll, context_events=[], pull_ids=["pull_10_4"]
        )
        # No entry bar -> falls back to overall rate or conversions
        assert "Première page" not in comment, (
            "Should NOT cite entry page when no landing_page bar"
        )
        # Should still cite conversion rate or conversions total
        assert "conversion" in comment.lower() or "%" in comment, (
            f"Expected conversion rate fallback; got: {comment!r}"
        )

    def test_comment_contains_citation_token(self):
        """AC 6: the comment always carries a citation token."""
        rows = _funnel_rows() + _landing_rows()
        bd = self._block_data_with_landing()
        roll = self._rollup(rows)
        comment = narrative_module.build_journey_comment(
            block_data=bd, rollup=roll, context_events=[], pull_ids=["pull_10_4"]
        )
        assert "(" in comment and ")" in comment, (
            f"Citation token missing from comment: {comment!r}"
        )

    def test_no_context_events_emits_contexte_manquant(self):
        """AD-9: when no context events, line 3 emits 'Contexte manquant'."""
        rows = _funnel_rows() + _landing_rows()
        bd = self._block_data_with_landing()
        roll = self._rollup(rows)
        comment = narrative_module.build_journey_comment(
            block_data=bd, rollup=roll, context_events=[], pull_ids=["pull_10_4"]
        )
        assert "Contexte manquant" in comment, (
            f"Expected 'Contexte manquant'; got: {comment!r}"
        )


# ---------------------------------------------------------------------------
# 8. Summary line count (AD-1: <= 30 lines)
# ---------------------------------------------------------------------------


def test_journey_card_summary_at_most_30_lines():
    """AD-1: get_card summary for the journey card stays <= 30 lines with pages."""
    rows = _funnel_rows() + _landing_rows() + _page_rows()
    with patch("core.warehouse.query_daily_report", return_value=rows):
        summary, _env, _uri = cards_module.get_card(
            [], "default", template="journey",
            metrics=["sessions", "conversions", "screen_page_views"],
            date_from="2026-07-01", date_to="2026-07-06",
        )
    assert len(summary.splitlines()) <= 30, (
        f"Summary exceeds 30 lines: {len(summary.splitlines())}"
    )
