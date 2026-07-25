"""toorow — shared rollup computation (Story 6.4, AC9).

Computes per-metric rollups (totals + deltas vs the previous period of the same
length) with provenance, for consumption by the narrative builder (AD-1). This is
the ONE canonical builder of the ``rollup`` dict shape that ``narrative.build_narrative``
consumes — used by BOTH ``get_daily_report`` (main.py) and ``get_report`` (reports.py),
eliminating duplicated summary-assembly logic.

Rollup dict shape (per metric)::

    {
      "clicks": {
        "value": 1245,
        "delta": 134,
        "delta_pct": "+12%",
        "period": "sem. préc.",
        "source_system": "<connector-name-from-row>",
        "source_field": "clicks",
        "pull_id": "pull_01JXXXXXXXXXX",
      }
    }

# AD-2: source-agnostic — no module-specific strings. source_system / source_field /
#       pull_id are read from the DATA ROWS (dynamic values), never hard-coded here.
# AD-4: non-additive metrics (average_position, ratios) are NOT naively summed. The
#       average_position value is impression-weighted over the rows; other non-additive
#       metrics fall back to a simple mean of their (already semantic-view) values.
# AD-9: provenance (source_system, source_field, pull_id) is assembled per metric so the
#       narrative can cite every claim.
"""

from __future__ import annotations

from datetime import date, timedelta

# Metrics that must never be summed. average_position is impression-weighted; the
# ratio metrics (roas/ctr/cpa) arrive from semantic views already at day-grain so
# a simple mean of their rows is the correct rollup here (AD-4). This constant is a
# generic, warehouse-vocabulary list — not a module name (AD-2).
_NON_ADDITIVE_METRICS = frozenset(
    {"average_position", "roas", "ctr", "cpa"}
)

# The French label for the comparison window used in every citation line (UX-DR10).
_PERIOD_LABEL = "sem. préc."

# Special token: group/filter a metric by the row-level ``connector`` column (cross-source),
# selecting each connector's ONE canonical breakdown partition. Mirrors cards.DIMENSION_CONNECTOR
# (re-exported there for backward-compat). Pure warehouse vocabulary, not a module name (AD-2).
DIMENSION_CONNECTOR = "connector"


def _row_date(row: dict) -> str | None:
    """Return the ISO date string for a fact row (``date`` or breakdown fallback)."""
    raw = row.get("date")
    if raw is None:
        # Date-dimension semantic-view rows carry the date in breakdown_value.
        if row.get("breakdown_dimension") == "date":
            raw = row.get("breakdown_value")
    if raw is None:
        return None
    return str(raw)[:10]


def split_periods(
    rows: list[dict], date_from: str, date_to: str
) -> tuple[list[dict], list[dict]]:
    """Split *rows* into (current, prior) buckets of equal length.

    Current period is ``[date_from, date_to]``. Prior period is the immediately
    preceding window of the SAME number of days. Rows outside both windows (or
    with unparseable dates) are dropped from the prior comparison.

    Public API (AI-02): cross-module callers (cards.py, main.py, reports.py) must
    call ``split_periods``. ``_split_periods`` is kept as a backward-compat alias.
    """
    try:
        start = date.fromisoformat(date_from)
        end = date.fromisoformat(date_to)
    except (ValueError, TypeError):
        # Undatable window — treat everything as current, no prior comparison.
        return rows, []

    span_days = (end - start).days + 1
    prior_end = start - timedelta(days=1)
    prior_start = prior_end - timedelta(days=span_days - 1)

    current: list[dict] = []
    prior: list[dict] = []
    for row in rows:
        ds = _row_date(row)
        if ds is None:
            # No date at all → count it in the current period (best effort).
            current.append(row)
            continue
        try:
            d = date.fromisoformat(ds)
        except (ValueError, TypeError):
            current.append(row)
            continue
        if start <= d <= end:
            current.append(row)
        elif prior_start <= d <= prior_end:
            prior.append(row)
    return current, prior


def weighted_avg_position(rows: list[dict]) -> float | None:
    """Impression-weighted average position over *rows* (AD-4).

    Weights each ``average_position`` row by the ``impressions`` value for the
    same (date, breakdown) key when available; falls back to a simple mean when
    no impression data is present.

    Public API (AI-02): cross-module callers (cards.py, reports.py) must call
    ``weighted_avg_position``. ``_weighted_avg_position`` is a backward-compat alias.
    """
    # Build an impressions lookup keyed by (date, breakdown_dimension, breakdown_value).
    imp_by_key: dict[tuple, float] = {}
    for row in rows:
        if row.get("metric") != "impressions":
            continue
        key = (
            _row_date(row),
            row.get("breakdown_dimension"),
            row.get("breakdown_value"),
        )
        val = row.get("value")
        if val is not None:
            imp_by_key[key] = imp_by_key.get(key, 0.0) + float(val)

    num = 0.0
    weight = 0.0
    plain_vals: list[float] = []
    for row in rows:
        if row.get("metric") != "average_position":
            continue
        val = row.get("value")
        if val is None:
            continue
        val = float(val)
        plain_vals.append(val)
        key = (
            _row_date(row),
            row.get("breakdown_dimension"),
            row.get("breakdown_value"),
        )
        w = imp_by_key.get(key)
        if w:
            num += val * w
            weight += w

    if weight > 0:
        return num / weight
    if plain_vals:
        return sum(plain_vals) / len(plain_vals)
    return None


def _neg_name(name: str | None) -> tuple:
    """Sort helper: invert a string for ``max`` so the LEXICOGRAPHICALLY SMALLEST name wins
    a (days, rows) tie. ``max`` picks the largest tuple; we negate each code point so a
    smaller name yields a larger inverted key. Deterministic and dependency-free.
    """
    return tuple(-ord(c) for c in (name or ""))


def canonical_breakdown_per_connector(
    rows: list[dict], metric: str
) -> dict[str, str | None]:
    """Pick ONE canonical breakdown_dimension PER connector for *metric* (AD-4).

    In the mart a metric can appear under SEVERAL parallel breakdown dimensions for the
    same connector and day (e.g. ``conversions`` under ``device_category`` AND ``country``
    on 2026-07-05), each of which independently totals the day for THAT connector. Summing
    across all of them inflates that connector's total by the number of parallel breakdowns
    (review-9-3 F-2/F-3/F-4, review-epic-10 CRITICAL-A). To total a metric across the
    row-level ``connector`` column we pin EACH connector to a single canonical breakdown
    dimension.

    Selection is per-connector and deterministic, ordered by (review-epic-9-backend F-2,
    tie-break revised by review-epic-10):
      1. max DISTINCT-DAY coverage (the breakdown that covers the most unique dates) --
         a sparse-but-many-values partition (e.g. 30 countries over 7 days) must never
         beat a date-COMPLETE partition (3 devices over 30 days) and undercount the
         window. Day coverage keeps the canonical total covering the full window.
      2. then LEXICOGRAPHIC MIN dimension name -- the same tie-break DIRECTION as the dbt
         marts' MIN(breakdown_dimension), so Python totals and mart totals pin the same
         partition ('country' for GA4/GSC). Review-epic-10: the previous ROW-COUNT
         tie-break wrongly selected high-cardinality TOP-N-BOUNDED partitions
         (landing_page, 30 pages/day) whose sums are PARTIAL by design -- the funnel
         then showed active_users > sessions (rate 1.62). Row count is NOT a proxy
         for "totals the day"; lexicographic MIN among full-coverage partitions is.

    Python<->dbt alignment caveat (review-10-6 F-4): the dbt marts apply
    MIN(breakdown_dimension) PER (date, connector, metric) with no day-coverage notion,
    while this function applies day-coverage first over the WHOLE window. Both agree under
    the standing assumption that a connector's date-complete canonical partition
    ('country') also has maximal day coverage -- true today because all of a connector's
    partitions land from the same daily pull. If the canonical partition ever had a day gap
    that a top-N partition covers, Python would pin the top-N partition for the window and
    diverge from the per-day dbt MIN (tracked in deferred-work; candidate fix: exclude
    top-N-bounded partitions from canonical selection).

    The value is ``None`` for a connector whose metric rows carry no breakdown dimension at
    all (a bare day-total) -- callers then keep every such row (one implicit partition).
    Choosing per connector (not globally) is essential: different connectors legitimately
    expose the metric under different breakdowns, so a single global partition would DROP a
    connector.

    PURE row logic (no warehouse import) -- lives here so BOTH ``compute_rollup`` (additive
    totals) and cards.py resolvers (sparkline/donut/gauge/funnel) pin to the SAME partition
    (AD-1: rollup/narrative never import warehouse). Re-exported from cards.py under the
    legacy ``_canonical_breakdown_per_connector`` name.
    """
    # Per connector -> per breakdown: distinct-day coverage.
    days: dict[str, dict[str | None, set[str]]] = {}
    for r in rows:
        if r.get("metric") != metric:
            continue
        conn = str(r.get("connector") or "")
        bd = r.get("breakdown_dimension")
        d = str(r.get("date") or "")[:10]
        days.setdefault(conn, {}).setdefault(bd, set()).add(d)

    canonical: dict[str, str | None] = {}
    for conn, bd_days in days.items():
        present = [bd for bd in bd_days if bd]
        if not present:
            canonical[conn] = None  # only bare day-totals -> single implicit partition
            continue
        # Order: (distinct-days desc, name asc) -> coverage-guarded lexicographic MIN,
        # mirroring the dbt marts' MIN(breakdown_dimension) rule (review-epic-10).
        canonical[conn] = max(
            present,
            key=lambda bd: (len(bd_days[bd]), _neg_name(bd)),
        )
    return canonical


def rows_for_dimension(
    rows: list[dict], metric: str, dimension: str
) -> list[dict]:
    """Filter *rows* to a single metric AND breakdown dimension.

    The special token ``connector`` selects, FOR EACH CONNECTOR, the metric's rows under
    that connector's ONE canonical breakdown dimension (the most-populated partition) so a
    metric present under several parallel breakdowns is NOT double-counted when it is later
    grouped by the row-level connector column (review-9-3 F-3, review-epic-10 CRITICAL-A).
    A connector whose metric rows carry no breakdown dimension keeps every row (a single
    implicit partition).

    Re-exported from cards.py under the legacy ``_rows_for_dimension`` name.
    """
    out: list[dict] = []
    if dimension == DIMENSION_CONNECTOR:
        canonical = canonical_breakdown_per_connector(rows, metric)
        for r in rows:
            if r.get("metric") != metric:
                continue
            conn = str(r.get("connector") or "")
            want = canonical.get(conn)
            if want is None or r.get("breakdown_dimension") == want:
                out.append(r)
        return out
    for r in rows:
        if r.get("metric") != metric:
            continue
        if r.get("breakdown_dimension") == dimension:
            out.append(r)
    return out


def _metric_value(metric: str, rows: list[dict]) -> float | None:
    """Compute the rollup value for *metric* over *rows* (additive vs non-additive).

    ADDITIVE metrics (everything not in _NON_ADDITIVE_METRICS) are pinned to ONE canonical
    breakdown partition PER CONNECTOR before summing (review-epic-10 CRITICAL-A): a metric
    present under N parallel breakdown dimensions (e.g. GA4 ``sessions`` under
    device_category + country + user_type + landing_page on the same days) would otherwise
    be summed N times, inflating the KPI hero / LLM-summary total N-fold. Pinning via
    ``rows_for_dimension(..., DIMENSION_CONNECTOR)`` counts each connector's day ONCE, exactly
    like the card sparkline / donut / gauge already do. Non-additive metrics (average_position
    impression-weighted; ratio metrics mean) are NOT re-partitioned -- their aggregation is
    already correct over the full row set (AD-4).
    """
    if metric == "average_position":
        return weighted_avg_position(rows)

    if metric in _NON_ADDITIVE_METRICS:
        metric_rows = [
            r for r in rows if r.get("metric") == metric and r.get("value") is not None
        ]
        if not metric_rows:
            return None
        vals = [float(r["value"]) for r in metric_rows]
        return sum(vals) / len(vals) if vals else None

    # Additive: pin to the canonical partition per connector so parallel breakdowns are not
    # double-counted, then sum. rows_for_dimension already filters to this metric only.
    partition_rows = [
        r for r in rows_for_dimension(rows, metric, DIMENSION_CONNECTOR)
        if r.get("value") is not None
    ]
    if not partition_rows:
        return None
    return sum(float(r["value"]) for r in partition_rows)


def _provenance(metric: str, rows: list[dict], pull_ids: list[str]) -> tuple[str, str, str | None]:
    """Return (source_system, source_field, pull_id) for *metric* from the data rows.

    source_system is the connector name carried on the metric's rows (dynamic
    value, AD-2). source_field is the metric name (the warehouse field). pull_id is
    the most recent pull_id contributing to the metric (rolling re-pull → newest),
    falling back to the caller-supplied ``pull_ids`` list.
    """
    metric_rows = [r for r in rows if r.get("metric") == metric]

    source_system = ""
    for row in metric_rows:
        conn = row.get("connector")
        if conn:
            source_system = str(conn)
            break

    # Most recent pull_id for this metric: prefer the row with the greatest
    # loaded_at; fall back to the max pull_id string, then to the caller's list.
    best_pull: str | None = None
    best_loaded = ""
    for row in metric_rows:
        pid = row.get("pull_id")
        if not pid:
            continue
        loaded = str(row.get("loaded_at") or "")
        if best_pull is None or loaded > best_loaded or (
            loaded == best_loaded and str(pid) > str(best_pull)
        ):
            best_pull = str(pid)
            best_loaded = loaded

    if best_pull is None and pull_ids:
        best_pull = pull_ids[-1]

    return source_system, metric, best_pull


def _format_delta_pct(value: float | None, prior: float | None) -> tuple[float | None, str | None]:
    """Return (delta, delta_pct_str) for value vs prior. None-safe.

    delta is ``value - prior`` (None if either side missing). delta_pct is a signed
    percentage string like ``"+12%"`` / ``"-5%"`` (None when prior is missing or 0).
    """
    if value is None or prior is None:
        return None, None
    delta = value - prior
    if prior == 0:
        return delta, None
    pct = (delta / abs(prior)) * 100.0
    sign = "+" if pct >= 0 else "-"
    return delta, f"{sign}{abs(pct):.0f}%"


def compute_rollup(
    rows: list[dict],
    metrics: list[str],
    date_from: str,
    date_to: str,
    project_id: str,
    pull_ids: list[str],
) -> dict:
    """Compute the per-metric rollup dict consumed by ``narrative.build_narrative``.

    Returns ``{metric: {value, delta, delta_pct, period, source_system,
    source_field, pull_id}}`` for each metric present in *rows*. Delta is computed
    vs the previous period of the same length (see :func:`_split_periods`).
    Non-additive metrics (``average_position``) use an impression-weighted value
    (AD-4). Provenance (source_system/source_field/pull_id) is read from the data
    rows (AD-9), never hard-coded (AD-2).

    Metrics with no rows in the current period are omitted from the result.
    """
    current_rows, prior_rows = split_periods(rows, date_from, date_to)

    rollup: dict[str, dict] = {}
    for metric in metrics:
        value = _metric_value(metric, current_rows)
        if value is None:
            continue
        prior = _metric_value(metric, prior_rows)
        delta, delta_pct = _format_delta_pct(value, prior)
        source_system, source_field, pull_id = _provenance(metric, current_rows, pull_ids)
        rollup[metric] = {
            "value": value,
            "delta": delta,
            "delta_pct": delta_pct,
            "period": _PERIOD_LABEL,
            "source_system": source_system,
            "source_field": source_field,
            "pull_id": pull_id,
        }
    return rollup


# ---------------------------------------------------------------------------
# Backward-compat private aliases (AI-02). Existing callers imported the
# underscore-prefixed names before they were promoted to public API; keep the
# aliases so no existing caller / test breaks. New cross-module callers must use
# the public ``split_periods`` / ``weighted_avg_position`` names.
# ---------------------------------------------------------------------------
_split_periods = split_periods
_weighted_avg_position = weighted_avg_position
