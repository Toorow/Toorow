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
        "period": "prev. wk.",
        "source_system": "<connector-name-from-row>",
        "source_field": "clicks",
        "pull_id": "pull_01JXXXXXXXXXX",
      }
    }

# AD-2: source-agnostic — no module-specific strings. source_system / source_field /
#       pull_id are read from the DATA ROWS (dynamic values), never hard-coded here.
# AD-4: non-additive metrics (average_position, ratios) are NOT naively summed. The
#       average_position value is impression-weighted over the rows; a ratio metric sums
#       its numerator and its denominator and divides ONCE (`_ratio_value`), and returns
#       None when that evidence is absent. It NEVER falls back to a mean of the rows --
#       the mean of daily CTRs is not the period CTR unless every day carries identical
#       impressions, and across connectors it is not a ratio of anything. This header
#       said the opposite until story 53.2, four months after `552ffe1` repaired the code.
# AD-9: provenance (source_system, source_field, pull_id) is assembled per metric so the
#       narrative can cite every claim.

THE ONE ROLLUP AUTHORITY. There used to be a second one -- `reports._rollup` -- and it
computed a different number for the same rows: `sums[metric] / counts[metric]`, the
unweighted mean CAV-03 was closed for. Two days of `ctr` (5/10 then 10/1000) gave 0.255
there and 0.01485 here, and it was the 0.255 that reached `build_envelope`'s
`data["metrics"]`. `reports._rollup` is now a projection of `compute_rollup` and computes
nothing of its own; a new aggregation belongs in this module or nowhere.
"""

from __future__ import annotations

from datetime import date, timedelta

# Metrics that must never be summed. average_position is impression-weighted; the
# ratio metrics (roas/ctr/cpa) arrive from semantic views already at day-grain and are
# re-aggregated from their numerator/denominator evidence, never averaged (AD-4, see
# `_ratio_value`). This constant is a generic, warehouse-vocabulary list — not a module
# name (AD-2).
#
# STORY 60.2: this is a PLATFORM DEFAULT, no longer the authority. It answers for
# the four metrics the platform ships and for nothing a client defines; a metric
# the client declared `non_additive` or `semi_additive` is added to it per Project
# by `declared_non_additive_metrics`. Four literal names decided the question for
# every custom ratio until then, and `efficiency_index` was summed.
_NON_ADDITIVE_METRICS = frozenset(
    {"average_position", "roas", "ctr", "cpa"}
)

# The French label for the comparison window used in every citation line (UX-DR10).
_PERIOD_LABEL = "prev. wk."

# Special token: group/filter a metric by the row-level ``connector`` column (cross-source),
# selecting each connector's ONE canonical breakdown partition. Mirrors cards.DIMENSION_CONNECTOR
# (re-exported there for backward-compat). Pure warehouse vocabulary, not a module name (AD-2).
DIMENSION_CONNECTOR = "connector"

#: Route statuses under which a SINGLE combined number must not be published.
#: Imported by name rather than copied so the two modules cannot drift
#: (`core.metric_reconciliation` owns the vocabulary; this is the consumer it
#: declared itself to be waiting for).
_COMBINATION_REFUSED = frozenset(
    {
        "UNRULED_OVERLAP",            # >=2 emitters, no resolved rule -- the gate
        "KEEP_SEPARATE",              # the rule says N series, never a total
        "NOT_COMBINABLE",             # non-additive without a rule
        "OVERRIDE_NOT_MATERIALIZED",  # the mart is frozen on another priority
    }
)


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


def _governed_rule(metric: str) -> str | None:
    """The metric's declared aggregation rule, or None when the dictionary is absent.

    Lazy import: `rollup` imports neither warehouse nor any mart (AD-1, enforced by
    `make check-narrative-no-raw`). `report_dictionary` is configuration, not data.
    """
    try:
        from core import report_dictionary  # noqa: PLC0415

        return report_dictionary.aggregation_rule(metric)
    except Exception:  # noqa: BLE001 -- no dictionary: caller falls back to refusal
        return None


def _ratio_value(metric: str, metric_rows: list[dict]) -> float | None:
    """Sum the numerator, sum the denominator, divide once. Refuse without evidence.

    Mirrors `core.geographic_semantics._aggregate` for the `ratio` / `weighted_ratio`
    rules, over the rows of ONE metric. Returns None -- never a mean -- when any row
    lacks its evidence or the denominator sums to zero.
    """
    rule = _governed_rule(metric)
    if rule is not None and rule not in {"ratio", "weighted_ratio"}:
        # The dictionary declares something else for this metric (e.g. `max`). Applying a
        # ratio here would be this module inventing a second opinion.
        return None

    numerator = 0.0
    denominator = 0.0
    for row in metric_rows:
        num = row.get("semantic_numerator")
        den = row.get("semantic_denominator")
        if num is None or den is None:
            return None
        try:
            numerator += float(num)
            denominator += float(den)
        except (TypeError, ValueError):
            return None
    if denominator == 0:
        return None
    return numerator / denominator


def declared_non_additive_metrics(project_id: str | None) -> frozenset[str]:
    """The metrics THIS PROJECT declared non-additive, on top of the defaults.

    Story 60.2. `_NON_ADDITIVE_METRICS` above is a PLATFORM DEFAULT, not an
    authority: it holds four literal names, and `is_ratio_name` adds a suffix
    rule. A client metric named `efficiency_index` -- a ratio by construction and
    by nothing in its name -- passes both and used to be summed across days.
    `core.metric_semantics.resolve_declared_additivity` reads what the client
    actually DECLARED, in the Semantic Model first and in `metric_definitions`
    second.

    Lazy import + fail-soft, exactly like `_governed_rule` above: this module
    stays deterministic offline, and an unreadable store degrades to the previous
    behaviour rather than to a refusal. A declaration can only ADD to the set --
    it is never allowed to make a platform ratio summable, because the reverse
    direction is the one that prints a wrong number confidently.
    """
    if not project_id:
        return frozenset()
    try:
        from core import metric_semantics  # noqa: PLC0415

        return metric_semantics.declared_non_additive(
            metric_semantics.resolve_declared_additivity(project_id)
        )
    except Exception:  # noqa: BLE001 -- no store: the platform defaults stand
        return frozenset()


def _metric_value(
    metric: str, rows: list[dict], non_additive: frozenset[str] | None = None
) -> float | None:
    """Compute the rollup value for *metric* over *rows* (additive vs non-additive).

    ``non_additive`` is the effective set for the Project being rendered:
    `_NON_ADDITIVE_METRICS` (the platform default) plus whatever the client
    declared non-additive or semi-additive. ``None`` means "no project context",
    and the platform default alone applies -- which is what every caller did
    before Story 60.2, byte for byte.

    ADDITIVE metrics (everything not in _NON_ADDITIVE_METRICS) are pinned to ONE canonical
    breakdown partition PER CONNECTOR before summing (review-epic-10 CRITICAL-A): a metric
    present under N parallel breakdown dimensions (e.g. GA4 ``sessions`` under
    device_category + country + user_type + landing_page on the same days) would otherwise
    be summed N times, inflating the KPI hero / LLM-summary total N-fold. Pinning via
    ``rows_for_dimension(..., DIMENSION_CONNECTOR)`` counts each connector's day ONCE, exactly
    like the card sparkline / donut / gauge already do.

    RATIO METRICS (`roas`, `ctr`, `cpa`) are aggregated by summing their numerator and
    denominator evidence and dividing ONCE -- not by averaging already-computed ratios.
    They used to return `sum(values) / len(values)`, the unweighted arithmetic mean over
    every day AND every connector. That is a different number wearing the same unit: the
    mean of daily CTRs is not the period CTR unless every day carries identical
    impressions, and across connectors it is not a ratio of anything. A reader deciding
    on "ROAS 3.4" had no way to see it.

    The evidence is already there and was simply unused: `warehouse` emits
    `semantic_numerator` / `semantic_denominator` alongside every ratio metric
    (`_SEMANTIC_EVIDENCE_BY_METRIC`), for the reason its own comment gives -- to
    "re-aggregate ... without averaging already-computed ratios". The rule comes from the
    governed dictionary (`report_dictionary.aggregation_rule`), so this function applies a
    declared rule rather than a second opinion, and `core.geographic_semantics._aggregate`
    stays the reference implementation of the same contract.

    FAIL-CLOSED, deliberately. When a ratio metric's evidence is absent the value is
    `None` and the metric is omitted, never silently averaged -- the posture
    `geographic_semantics` already states: *"A non-additive metric that is summed because
    its evidence was missing is a wrong number presented as a right one, which is worse
    than no number."*
    """
    if metric == "average_position":
        return weighted_avg_position(rows)

    # `is None` and not `or`: an EMPTY declared set is a real answer ("this
    # Project declared nothing extra"), and `or` would quietly widen it back to
    # the defaults -- the exact shape of bug this story is repairing elsewhere.
    effective = _NON_ADDITIVE_METRICS if non_additive is None else non_additive
    if metric in effective:
        metric_rows = [
            r for r in rows if r.get("metric") == metric and r.get("value") is not None
        ]
        if not metric_rows:
            return None
        return _ratio_value(metric, metric_rows)

    # Additive: pin to the canonical partition per connector so parallel breakdowns are not
    # double-counted, then sum. rows_for_dimension already filters to this metric only.
    partition_rows = [
        r for r in rows_for_dimension(rows, metric, DIMENSION_CONNECTOR)
        if r.get("value") is not None
    ]
    if not partition_rows:
        return None
    return sum(float(r["value"]) for r in partition_rows)


def _provenance(
    metric: str, rows: list[dict], pull_ids: list[str]
) -> tuple[str, str, str | None, list[str]]:
    """Return (source_system, source_field, pull_id, source_systems) for *metric*.

    THE DEFECT THIS SIGNATURE EXISTS TO CLOSE. ``source_system`` used to be the
    connector of the FIRST row encountered -- the loop `break`-ed on it -- while
    ``_metric_value`` sums ONE canonical partition PER CONNECTOR and then adds
    every connector together. A cross-source total was therefore printed with a
    citation naming a single source, and the citation is the whole mechanism by
    which "every claim is cited" (FR7) is supposed to hold. A reader deciding on
    "Conversions 12 340 (google-ads:conversions, pull_...)" had no way to see that
    two providers were added, possibly double-counting the same conversion.

    ``source_systems`` is now the sorted, de-duplicated list of every connector
    that actually contributed a row to this metric, and ``source_system`` is the
    honest label built from it (``a+b`` when several). ``source_field`` and
    ``pull_id`` keep their meaning; ``pull_id`` remains the most recent
    contributing pull (rolling re-pull -> newest), falling back to the
    caller-supplied ``pull_ids`` list.

    NOT FIXED HERE: whether adding those sources is legitimate at all.
    ``core.metric_reconciliation.resolve_route`` answers that -- and declares
    itself passive, with no consumer in this module. Wiring it is story 52.2. This
    change makes the addition VISIBLE; it does not make it correct.
    """
    metric_rows = [r for r in rows if r.get("metric") == metric]

    source_systems = sorted(
        {str(row["connector"]) for row in metric_rows if row.get("connector")}
    )
    source_system = "+".join(source_systems)

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

    return source_system, metric, best_pull, source_systems


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


#: How the combination question was ANSWERED for this metric. Every rollup entry
#: carries one, because "no refusal" used to cover four different situations and a
#: reader could not tell "verified and permitted" from "never asked".
CHECK_SINGLE_SOURCE = "single_source"    # one connector contributed: nothing to combine
CHECK_NOT_REQUESTED = "not_requested"    # no resolver was wired at this call site
CHECK_VERIFIED = "verified"              # the gate answered, and it permits the total
CHECK_UNAVAILABLE = "unavailable"        # the gate was asked and could not answer
CHECK_REFUSED = "refused"                # the gate answered, and it refuses the total


def combination_refusal(
    metric: str, source_systems: list[str], route_resolver
) -> tuple[str | None, str]:
    """Return ``(refusing_route_status_or_None, combination_check)``.

    PUBLIC BECAUSE A SECOND READER ASKS IT. This was private while `compute_rollup`
    was its only caller, and the mart path was then the only path that could answer
    "was this total checked". The Analyze Result lens
    (`analyze_workbench._metric_combination`) now asks the same question about the
    same metrics, and it asks THIS function rather than re-deriving the verdict from
    `_COMBINATION_REFUSED` on its own -- two readings of one gate is how the console
    and the model channel start disagreeing about whether a number was verified.
    `_combination_refusal` stays as an alias below (AI-02 convention of this module).

    ``combination_check`` is the second half of the answer and it is the point: a
    total published because the gate said yes and a total published because nobody
    asked used to be the SAME output. One is a checked number; the other is the
    CAV-02 belief itself.

    The resolver is called with the connectors this rollup actually OBSERVED
    contributing rows. `metric_reconciliation.resolve_route` unions them into its
    declared emitters, so an unreachable manifest registry can no longer answer
    DIRECT_SUM -- a permission -- about two sources it never saw.

    Fail-SOFT on the resolver itself (it is `resolve_route`, already fail-soft): a
    resolver that raises must not take the report down. The total then goes out
    UNVERIFIED, and now says so.
    """
    if route_resolver is None:
        return None, CHECK_NOT_REQUESTED
    if len(source_systems) < 2:
        return None, CHECK_SINGLE_SOURCE
    try:
        status = route_resolver(metric, tuple(source_systems))
    except Exception:  # noqa: BLE001 -- see docstring
        return None, CHECK_UNAVAILABLE
    status = getattr(status, "status", status)
    if status in _COMBINATION_REFUSED:
        return str(status), CHECK_REFUSED
    return None, CHECK_VERIFIED


def _per_source_values(
    metric: str,
    current_rows: list[dict],
    source_systems: list[str],
    non_additive: frozenset[str] | None = None,
) -> dict[str, float]:
    """Each connector's own figure, computed by the same rule as the total."""
    per_source: dict[str, float] = {}
    for connector in source_systems:
        subset = [r for r in current_rows if r.get("connector") == connector]
        value = _metric_value(metric, subset, non_additive)
        if value is not None:
            per_source[connector] = value
    return per_source


def compute_rollup(
    rows: list[dict],
    metrics: list[str],
    date_from: str,
    date_to: str,
    project_id: str,
    pull_ids: list[str],
    *,
    route_resolver=None,
) -> dict:
    """Compute the per-metric rollup dict consumed by ``narrative.build_narrative``.

    Returns ``{metric: {value, delta, delta_pct, period, source_system,
    source_field, pull_id}}`` for each metric present in *rows*. Delta is computed
    vs the previous period of the same length (see :func:`_split_periods`).
    Non-additive metrics (``average_position``) use an impression-weighted value
    (AD-4). Provenance (source_system/source_field/pull_id) is read from the data
    rows (AD-9), never hard-coded (AD-2).

    Metrics with no rows in the current period are omitted from the result.

    ``route_resolver`` — WHETHER SEVERAL SOURCES MAY BE ADDED AT ALL.
    A callable ``(metric, observed_sources) -> status``, in practice the one
    ``core.metric_reconciliation.route_status_resolver(project_id)`` returns,
    consulted ONLY when two or more connectors contributed rows for that metric.
    The observed sources travel with the question because the gate's own emitter
    enumeration is fail-soft and answers ``DIRECT_SUM`` -- a permission -- when it
    is unreachable.
    ``resolve_route`` has always been able to answer this — it returns
    ``UNRULED_OVERLAP`` when several sources emit the same metric with no resolved
    rule — but it declared itself *"STRICTLY PASSIVE … no existing consumer
    (rollup.py, cards.py, the dbt marts) is touched"*, so nothing ever asked. Two
    sources double-counting the same conversion were simply added.

    On a refusing status the metric keeps its entry but carries **no combined
    value**: ``value`` is None, ``combination_refused`` names the status and
    ``per_source`` gives each connector's own figure. A total nobody may compute is
    not replaced by a plausible one.

    Injected rather than imported so this module keeps its AD-1 posture (no
    warehouse, no mart, no DB) and stays deterministic offline. ``None`` still
    publishes the total, but it now labels it ``combination_check ==
    "not_requested"`` instead of leaving it indistinguishable from a checked one.
    """
    current_rows, prior_rows = split_periods(rows, date_from, date_to)

    # Story 60.2: the platform default UNION what this Project declared. Read
    # ONCE per report, not once per metric -- and resolved here rather than
    # inside `_metric_value` so that function stays pure and offline-testable.
    non_additive = _NON_ADDITIVE_METRICS | declared_non_additive_metrics(project_id)

    rollup: dict[str, dict] = {}
    for metric in metrics:
        value = _metric_value(metric, current_rows, non_additive)
        if value is None:
            continue
        prior = _metric_value(metric, prior_rows, non_additive)
        delta, delta_pct = _format_delta_pct(value, prior)
        source_system, source_field, pull_id, source_systems = _provenance(
            metric, current_rows, pull_ids
        )
        refusal, check = combination_refusal(metric, source_systems, route_resolver)
        if refusal is not None:
            rollup[metric] = {
                "value": None,
                "combination_refused": refusal,
                "combination_check": check,
                "per_source": _per_source_values(
                    metric, current_rows, source_systems, non_additive
                ),
                "delta": None,
                "delta_pct": None,
                "period": _PERIOD_LABEL,
                "source_system": source_system,
                "source_field": source_field,
                "pull_id": pull_id,
                "source_systems": source_systems,
                "source_count": len(source_systems),
            }
            continue
        rollup[metric] = {
            "value": value,
            "delta": delta,
            "delta_pct": delta_pct,
            "period": _PERIOD_LABEL,
            "source_system": source_system,
            "source_field": source_field,
            "pull_id": pull_id,
            # Every connector that contributed a row to this value, and how many.
            # `_metric_value` adds them together; without these two keys the sum
            # was indistinguishable from a single-source figure.
            "source_systems": source_systems,
            "source_count": len(source_systems),
            # And WHETHER anyone checked that adding them is legitimate.
            "combination_check": check,
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
_combination_refusal = combination_refusal
