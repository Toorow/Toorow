"""toorow — report pack rendering (Story 6.1, AC3; extended Story 6.3).

Renders a named, expert-designed report from the module ``reports/`` pack:
  - resolves ``"{module_name}/{report_id}"`` against the loaded-module registry,
  - resolves the date window (report.date_window.default_days when unset),
  - queries fact_daily_kpi / semantic views via the warehouse layer (AD-4 routing),
  - applies optional post-processors (Story 6.3: WoW delta, post-deploy regressions),
  - builds the dual-channel result: a ≤30-line LLM summary prefixed by the report's
    ``narrative_prompt`` (the expert 'voice'), and the canonical AD-1 envelope with
    full provenance for the widget channel (AD-1 / AD-9).

# AD-1: token-burn split — ≤30-line summary on the text channel, full dataset in
#       the structuredContent envelope.
# AD-2: core-owned, source-agnostic — the module name flows in as data, never as a
#       hard-coded branch. The renderer works for any KPI module's report pack.
# AD-9: provenance (source_system, source_field, pull_id) is mandatory in meta.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Callable

# The notes this module appends to a narrative prompt ARE narrative: they reach
# the reader through the same channel as the lines `narrative.py` composes. They
# are rendered from the one catalogue, in the reader's language
# (docs/product-architecture/analyze-and-test.md, amendment 2026-08-25).
from core.narrative_phrases import phrase

logger = logging.getLogger(__name__)

# NFR1 P1 gate: hard ceiling on the LLM summary line count (AD-1).
_MAX_LINES = 30

# Core-owned report widget URI (mirrors DAILY_REPORT_WIDGET_URI). A report binds
# to the module's declared widget_ref (manifest) so the module renders its own
# chart; core falls back to this when a module declares no widget_ref.
DEFAULT_REPORT_WIDGET_URI = "ui://core/daily-report"


class ReportNotFound(Exception):
    """Raised when the module or report id cannot be resolved."""


def find_report(loaded_modules: list, module_name: str, report_id: str) -> dict | None:
    """Return the report definition dict for ``module_name/report_id`` or None."""
    for loaded in loaded_modules:
        if loaded.name == module_name:
            for report in loaded.reports:
                if report.get("id") == report_id:
                    return report
    return None


def _find_module(loaded_modules: list, module_name: str):
    for loaded in loaded_modules:
        if loaded.name == module_name:
            return loaded
    return None


def resolve_date_window(
    report: dict, date_from: str, date_to: str, *, today: date | None = None
) -> tuple[str, str]:
    """Resolve the effective (date_from, date_to) ISO strings for a report.

    When both are empty, count back ``date_window.default_days`` from today
    (UTC). When only one is empty it is filled from the report window / today.
    """
    if today is None:
        today = datetime.now(tz=timezone.utc).date()
    default_days = int(report.get("date_window", {}).get("default_days", 30))

    end = date_to.strip() if date_to and date_to.strip() else today.isoformat()
    if date_from and date_from.strip():
        start = date_from.strip()
    else:
        end_date = date.fromisoformat(end)
        start = (end_date - timedelta(days=default_days)).isoformat()
    return start, end


def _apply_ordering(rows: list[dict], order_by: str) -> list[dict]:
    """Sort ``rows`` by the report's ``layout.order_by`` (e.g. 'date', 'cost DESC').

    Supports ``<field>`` and ``<field> DESC``/``ASC``. Falls back to a stable
    no-op when the field is absent from the rows.
    """
    if not order_by:
        return rows
    parts = order_by.split()
    field = parts[0]
    descending = len(parts) > 1 and parts[1].upper() == "DESC"

    sample = rows[0] if rows else {}
    if field not in sample:
        # order_by may name a metric; sort by value for rows of that metric.
        if field in {r.get("metric") for r in rows}:
            return sorted(
                rows,
                key=lambda r: (r.get("value") or 0) if r.get("metric") == field else 0,
                reverse=descending,
            )
        return rows

    return sorted(rows, key=lambda r: (r.get(field) is None, r.get(field)), reverse=descending)


def _rollup(
    rows: list[dict],
    report: dict,
    *,
    date_from: str = "",
    date_to: str = "",
    project_id: str = "",
    route_resolver=None,
) -> tuple[dict[str, float], dict[str, dict]]:
    """Project ``rollup.compute_rollup`` onto the envelope's flat metric map.

    THE DEFECT THIS FUNCTION EXISTS TO CLOSE, and it was inside this function. It
    computed its own aggregation -- ``sums[metric] / counts[metric]`` for anything
    ``report_dictionary.is_non_additive`` declared -- which is the unweighted mean of
    the per-day x per-connector values. That is the exact number CAV-03 was closed for
    in `552ffe1`, and closing it in `rollup.py` left it published from here: on two days
    of `ctr` (5/10 then 10/1000) this returned **0.255** while ``compute_rollup``
    returned **0.01485**, and it was 0.255 that reached ``build_envelope``'s
    ``data["metrics"]``. `test_rollup.py` forbade the number in one module while the
    other emitted it.

    It now computes NOTHING. It calls the one authority and projects the result, so a
    change to the aggregation rule cannot land on one surface and miss the other.

    Returns ``(values, not_combinable)``:

    * ``values`` -- the flat ``{metric: float}`` map the report envelope has always
      published, unchanged in shape;
    * ``not_combinable`` -- ``{metric: {status, check, per_source, source_systems}}``
      for every metric the reconciliation gate REFUSED. Those metrics are absent from
      ``values``, and this second map is why that absence does not read as "no data".
      Silently omitting a refused total would trade a wrong number for a missing one
      with no way to tell them apart.
    """
    from core import rollup as rollup_module  # noqa: PLC0415

    computed = rollup_module.compute_rollup(
        rows,
        list(report.get("metrics", [])),
        date_from,
        date_to,
        project_id,
        sorted({r.get("pull_id") for r in rows if r.get("pull_id")}),
        route_resolver=route_resolver,
    )

    values: dict[str, float] = {}
    not_combinable: dict[str, dict] = {}
    for metric, entry in computed.items():
        if entry.get("combination_refused"):
            not_combinable[metric] = {
                "status": entry["combination_refused"],
                "check": entry.get("combination_check"),
                "per_source": entry.get("per_source") or {},
                "source_systems": entry.get("source_systems") or [],
            }
            continue
        values[metric] = entry["value"]
    return values, not_combinable


def build_summary(
    report: dict,
    rows: list[dict],
    start: str,
    end: str,
    module_name: str,
    *,
    project_id: str = "",
    context_events: list[dict] | None = None,
    llm_commentary_guidelines: str | None = None,
    context_events_unavailable: dict | None = None,
) -> str:
    """Build the ≤30-line LLM summary as a deterministic what+why narrative (Story 6.4).

    The report's ``narrative_prompt`` is the first line (the expert domain 'voice',
    AD-1). The what+why body and inline citations come from the shared narrative
    builder (``narrative.build_narrative``), fed by the canonical rollup dict
    (``rollup.compute_rollup``). Every numeric claim carries a citation token (FR7);
    when no context events are present, the narrative states context missing (AD-9).
    Hard ceiling of 30 lines is enforced by the builder (NFR1).

    R6: when ``llm_commentary_guidelines`` is provided (from the merged flow.report
    override), it is appended to ``narrative_prompt`` so the LLM receives agent-set
    commentary grounding ("les définitions pour les comments du llm").

    ``project_id`` (story 53.2) is the Project the report belongs to, and it used to be
    a hardcoded ``""`` at the ``compute_rollup`` call below. That single empty string
    disabled two things at once: the reconciliation gate could not bind (a resolver
    needs a project), and ``rollup.declared_non_additive_metrics("")`` returns
    ``frozenset()`` by its own first line, so the per-Project additivity story 60.2
    delivered was dead on this path -- a client metric declared non-additive was summed
    in every report while the same metric was correctly refused on the card beside it.
    """
    from core import narrative as narrative_module  # noqa: PLC0415
    from core import rollup as rollup_module  # noqa: PLC0415

    prompt = (report.get("narrative_prompt") or "").strip() or None
    # R6: append llm_commentary_guidelines to the prompt so the LLM narrative is
    # grounded by the operator-set directives (Epic 8 R6, Story 8.8).
    #
    # Framed, not merely appended (story 53.5, CAV-18): the frame says this text is
    # an INSTRUCTION and not evidence, so a directive like "attribute drops to
    # seasonality" cannot reach the model indistinguishable from the deterministic
    # cited comment beside it. `narrative.GUIDANCE_FRAME` is shared with
    # `cards._build_summary` so the two cannot drift on the wording that carries
    # the constraint.
    _guidelines = (llm_commentary_guidelines or "").strip()
    if _guidelines:
        from core.narrative import GUIDANCE_FRAME  # noqa: PLC0415

        _sep = ("\n\n" + GUIDANCE_FRAME) if prompt else GUIDANCE_FRAME
        prompt = (prompt or "") + _sep + _guidelines

    if not rows:
        # Empty state: keep the narrative_prompt framing + explicit no-data line,
        # and still state context absence (AD-9). Built via the narrative builder so
        # the 30-line cap and structure are consistent.
        return narrative_module.build_narrative(
            project_id=project_id,
            report_id=report.get("id"),
            rollup={},
            context_events=context_events or [],
            alerts=[],
            as_of=None,
            narrative_prompt=(
                ((prompt + " ") if prompt else "") + phrase("summary_no_data")
            ).strip(),
            context_unavailable=context_events_unavailable,
        )

    from core.metric_reconciliation import route_status_resolver  # noqa: PLC0415

    metrics = list(report.get("metrics", []))
    pull_ids = sorted({r.get("pull_id") for r in rows if r.get("pull_id")})
    rollup = rollup_module.compute_rollup(
        rows, metrics, start, end, project_id, pull_ids,
        route_resolver=route_status_resolver(project_id),
    )

    return narrative_module.build_narrative(
        project_id=project_id,
        report_id=report.get("id"),
        rollup=rollup,
        context_events=context_events or [],
        alerts=[],
        as_of=None,
        narrative_prompt=prompt,
        context_unavailable=context_events_unavailable,
    )


def build_envelope(
    report: dict,
    rows: list[dict],
    module_name: str,
    start: str,
    end: str,
    project_id: str,
    *,
    trace_id: str | None = None,
    context_events: list[dict] | None = None,
    alerts: list[dict] | None = None,
    confidence: dict | None = None,
    metric_definitions: dict | None = None,
    business_context_paths: list[dict] | None = None,
) -> dict:
    """Build the canonical AD-1 envelope for a report (data channel).

    Shape (AC3):
      {schema_version, meta: {freshness, provenance, alerts[], trace_id, as_of,
       context_events, confidence}, data: {report_id, metrics: {...}, rows: [...]}}
    meta.provenance carries source_system/source_field/pull_id per AD-9.

    R6: when ``metric_definitions`` is provided (non-empty dict from the merged
    flow.report override), it is injected into ``data["metric_definitions"]`` so
    the widget's tooltip/direction-tint logic lights up (backward-compatible:
    absent when no definitions).
    """
    pull_ids = sorted({r.get("pull_id") for r in rows if r.get("pull_id")})
    loaded_ats = [str(r.get("loaded_at")) for r in rows if r.get("loaded_at")]
    last_pull = max(loaded_ats) if loaded_ats else None

    provenance = {
        "source_system": module_name,
        "source_field": "fact_daily_kpi",
        "pull_id": pull_ids[-1] if pull_ids else None,
        "pull_ids": pull_ids,
    }

    meta: dict = {
        "freshness": {
            "last_pull": last_pull,
            "cadence_hours": 24,
            # Not evaluated HERE, and that stays true: this builder does not
            # reach a database. `core.health_enrichment` is the one evaluator,
            # and since AI-273 it is called at the exit of `get_card` and of
            # `render_report`, not only from `get_daily_report` -- so the False
            # below is now a starting value that a look can overturn, rather
            # than the final word on a card frozen into a shared Render. When
            # the look does not happen (no DB, unreadable window, no connection
            # over it) it survives, and an unevaluated null must never read as
            # "evaluated, and fresh" (README.md:123, invariant 8).
            "stale_since": None,
            "stale_since_evaluated": False,
        },
        "provenance": provenance,
        "alerts": alerts or [],
        "trace_id": trace_id,
        "as_of": None,
        "context_events": [
            {
                "id": e.get("id"),
                "event_date": e.get("event_date"),
                "type": e.get("type"),
                "label": e.get("label"),
            }
            for e in (context_events or [])
        ],
    }
    if confidence is not None:
        meta["confidence"] = confidence
    if business_context_paths is not None:
        meta["business_context_paths"] = business_context_paths

    from core.metric_reconciliation import route_status_resolver  # noqa: PLC0415

    metric_values, not_combinable = _rollup(
        rows,
        report,
        date_from=start,
        date_to=end,
        project_id=project_id,
        route_resolver=route_status_resolver(project_id),
    )

    data: dict = {
        "report_id": f"{module_name}/{report.get('id')}",
        "date_range": {"start": start, "end": end},
        "connectors": [module_name],
        "metrics": metric_values,
        "rows": rows,
    }
    # A metric the reconciliation gate refused is NOT in `metrics` -- there is no
    # number to put there. It is named here with each source's own figure, so its
    # absence reads as "several sources, no rule to combine them" and not as "no
    # data" (analyze-and-test.md: "or is combined at all where no reconciliation
    # rule resolves the overlap"). Omitted entirely when nothing was refused.
    if not_combinable:
        data["metrics_not_combinable"] = not_combinable
    # R6: inject metric_definitions when non-empty so the widget's tooltips and
    # direction-tint logic light up (Epic 8, Story 8.8). Key is omitted entirely
    # when no definitions exist (backward-compatible with pre-R6 envelopes).
    if metric_definitions:
        data["metric_definitions"] = metric_definitions

    # AI-275 / CAV-17 -- CONSTRUCTED, never assembled. This was a dict literal, so
    # it walked past every assertion pinned on the constructor: the figure it
    # carries reached a reader with no word about which engine produced it, and no
    # test could tell. `ANALYTICAL_PATH_MART` is imported rather than retyped --
    # two hand-written copies of a relation name is how a disclosure starts
    # describing a path that moved.
    from core.envelope import ANALYTICAL_PATH_MART  # noqa: PLC0415
    from core.envelope import build_envelope as _build_ad1_envelope  # noqa: PLC0415

    return _build_ad1_envelope(
        meta=meta, data=data, analytical_path=ANALYTICAL_PATH_MART
    )


# ---------------------------------------------------------------------------
# Story 6.3 — Post-processor framework (AC2, AC4, T4, T5)
#
# POST_PROCESSORS maps report_id → callable applied after warehouse query.
# The callable signature: (rows, date_from, date_to, **kw) -> list[dict]
# Post-processors must NOT mutate rows in-place — they return a new list.
# AD-2: no module-specific imports; report_id is the only branch key.
# ---------------------------------------------------------------------------


def _compute_wow_position_delta(
    rows: list[dict], date_from: str, date_to: str, **_kw
) -> list[dict]:
    """Compute week-over-week position delta per page (Story 6.3, AC2, T4.2).

    Groups rows by page, computes avg_position for last 7 days (last_7d) and
    the 7 days before that (prev_7d). Adds ``delta_position`` and ``movement``
    flags to each row:
      - delta_position >= 2  → movement = "opportunity"  (rank improved)
      - delta_position <= -2 → movement = "alert"         (rank dropped)
      - |delta| < 2          → movement = "stable"

    Note: in GSC, LOWER position number = BETTER rank (position 1 = #1).
    A delta_position > 0 means rank went from e.g. 5 → 7 (worse = alert).
    A delta_position < 0 means rank went from e.g. 7 → 5 (better = opportunity).
    The AC2 spec says "hausse ≥2 rangs = opportunité, baisse ≥2 rangs = alerte"
    where "hausse" = position number decreased (rank improved).

    Pages with insufficient data (only one week's data) do not get delta.
    Post-processor returns a NEW list (no mutation of input rows).
    """
    from collections import defaultdict  # noqa: PLC0415

    # Parse the date window to identify last-7d vs prev-7d buckets.
    try:
        end_dt = date.fromisoformat(date_to)
    except (ValueError, TypeError):
        end_dt = datetime.now(tz=timezone.utc).date()

    last_7d_start = end_dt - timedelta(days=6)
    prev_7d_end = last_7d_start - timedelta(days=1)
    prev_7d_start = prev_7d_end - timedelta(days=6)

    # Group position values by page and week bucket (only from semantic view rows).
    page_last: dict[str, list[float]] = defaultdict(list)
    page_prev: dict[str, list[float]] = defaultdict(list)

    for row in rows:
        if row.get("metric") != "average_position":
            continue
        row_date_str = row.get("date") or row.get("breakdown_value") or ""
        if not row_date_str:
            continue
        try:
            row_date = date.fromisoformat(str(row_date_str)[:10])
        except (ValueError, TypeError):
            continue
        page = row.get("breakdown_value", "")
        if row.get("breakdown_dimension") == "page":
            page = row.get("breakdown_value", "")
        else:
            # For date-dimension rows the breakdown_value is the date itself;
            # page info may not be available — skip these for WoW.
            continue
        val = row.get("value")
        if val is None:
            continue
        val = float(val)
        if last_7d_start <= row_date <= end_dt:
            page_last[page].append(val)
        elif prev_7d_start <= row_date <= prev_7d_end:
            page_prev[page].append(val)

    # Compute per-page deltas.
    deltas: dict[str, float] = {}
    for page in page_last:
        if page in page_prev and page_last[page] and page_prev[page]:
            avg_last = sum(page_last[page]) / len(page_last[page])
            avg_prev = sum(page_prev[page]) / len(page_prev[page])
            # delta > 0 means rank got worse (position number increased)
            deltas[page] = round(avg_last - avg_prev, 2)

    # Build new rows list with delta/movement added to avg_position rows by page.
    result: list[dict] = []
    for row in rows:
        new_row = dict(row)
        if row.get("metric") == "average_position" and row.get("breakdown_dimension") == "page":
            page = row.get("breakdown_value", "")
            if page in deltas:
                delta = deltas[page]
                new_row["delta_position"] = delta
                # Lower delta = rank improved (position number decreased)
                if delta <= -2:
                    new_row["movement"] = "opportunity"
                elif delta >= 2:
                    new_row["movement"] = "alert"
                else:
                    new_row["movement"] = "stable"
        result.append(new_row)
    return result


def _fetch_deployment_events(
    project_id: str,
    date_from: str,
    date_to: str,
    *,
    identity: str | None = None,
) -> list[dict]:
    """The deployment/release context events of the window (Story 6.3, T5.1).

    AI-344 (context-hub.md, amendment of 2026-09-01): served by the ONE
    context-event read -- the DuckDB mirror when the deployment keeps one, else
    the record ``app.context_events`` through the caller's scoped connection --
    and filtered here to ``type IN ('deployment', 'release')``. It used to read
    the mirror alone and answer ``[]`` when there was none, so on every
    deployment without a mirror this report said "no deployment in the window"
    about windows it had never read.

    Raises ``ContextEventsUnavailable`` when neither store can serve: the
    caller says so beside the report, never "no deployment found".
    AD-2: no module-specific strings.
    """
    from core.context_events import fetch_context_events  # noqa: PLC0415

    return [
        {
            "id": e.get("id"),
            "project_id": e.get("project_id"),
            "event_date": e.get("event_date"),
            "type": e.get("type"),
            "label": e.get("label"),
        }
        for e in fetch_context_events(project_id, date_from, date_to, identity=identity)
        if e.get("type") in ("deployment", "release")
    ]

def _compute_post_deploy_regressions(
    rows: list[dict],
    date_from: str,
    date_to: str,
    project_id: str = "",
    identity: str | None = None,
    **_kw,
) -> list[dict]:
    """Compute per-event, per-page regression deltas around deployment events (T5.2).

    For each deployment/release event, compares avg position and clicks in the
    7 days before vs. 7 days after. Adds ``regression_delta_position``,
    ``regression_delta_clicks``, ``context_event_id``, ``context_event_label``
    to each row.

    When no events are found: returns rows unchanged and sets marker for the
    envelope to include ``data.context_events = []`` and a narrative note (AD-9).

    When the events could NOT be read (AI-344): rows unchanged, tagged with
    ``_deploy_events_unavailable = {reason, repair}`` so the envelope says the
    window was not read -- never ``context_events = []``.
    """
    from collections import defaultdict  # noqa: PLC0415

    from core.context_events import ContextEventsUnavailable  # noqa: PLC0415

    try:
        date.fromisoformat(date_to)
        date.fromisoformat(date_from)
    except (ValueError, TypeError):
        return rows

    try:
        events = _fetch_deployment_events(project_id, date_from, date_to, identity=identity)
    except ContextEventsUnavailable as unavailable:
        return [dict(row, _deploy_events_unavailable=unavailable.payload) for row in rows]

    if not events:
        # AD-9 explicit absence: tag each row so the caller can surface the note.
        result = []
        for row in rows:
            new_row = dict(row)
            new_row["_no_deploy_events"] = True
            result.append(new_row)
        return result

    # Build page-level daily position and clicks index.
    # Structure: {page: {date_str: {"average_position": val, "clicks": val}}}
    page_daily: dict[str, dict[str, dict[str, float]]] = defaultdict(lambda: defaultdict(dict))
    for row in rows:
        metric = row.get("metric")
        if metric not in ("average_position", "clicks"):
            continue
        row_date = str(row.get("date") or row.get("breakdown_value") or "")[:10]
        if not row_date:
            continue
        page = ""
        if row.get("breakdown_dimension") == "page":
            page = row.get("breakdown_value", "")
        if not page:
            continue
        val = row.get("value")
        if val is not None:
            page_daily[page][row_date][metric] = float(val)

    # For each event, compute pre/post window averages per page.
    # We annotate ALL existing rows for the pages involved with event metadata.
    result: list[dict] = []
    # Compute regression info per page per event.
    page_event_regressions: dict[tuple[str, str], dict] = {}
    for event in events:
        evt_date_str = event.get("event_date", "")
        if not evt_date_str:
            continue
        try:
            evt_date = date.fromisoformat(str(evt_date_str)[:10])
        except (ValueError, TypeError):
            continue
        pre_start = evt_date - timedelta(days=7)
        pre_end = evt_date - timedelta(days=1)
        post_start = evt_date + timedelta(days=1)
        post_end = evt_date + timedelta(days=7)

        for page, daily in page_daily.items():
            pre_pos, pre_clk, post_pos, post_clk = [], [], [], []
            for d_str, metrics in daily.items():
                try:
                    d = date.fromisoformat(d_str)
                except (ValueError, TypeError):
                    continue
                if pre_start <= d <= pre_end:
                    if "average_position" in metrics:
                        pre_pos.append(metrics["average_position"])
                    if "clicks" in metrics:
                        pre_clk.append(metrics["clicks"])
                elif post_start <= d <= post_end:
                    if "average_position" in metrics:
                        post_pos.append(metrics["average_position"])
                    if "clicks" in metrics:
                        post_clk.append(metrics["clicks"])

            if pre_pos or post_pos:
                avg_pre_pos = sum(pre_pos) / len(pre_pos) if pre_pos else None
                avg_post_pos = sum(post_pos) / len(post_pos) if post_pos else None
                avg_pre_clk = sum(pre_clk) / len(pre_clk) if pre_clk else None
                avg_post_clk = sum(post_clk) / len(post_clk) if post_clk else None
                delta_pos = None
                delta_clk = None
                if avg_pre_pos is not None and avg_post_pos is not None:
                    delta_pos = round(avg_post_pos - avg_pre_pos, 2)
                if avg_pre_clk is not None and avg_post_clk is not None:
                    delta_clk = round(avg_post_clk - avg_pre_clk, 2)
                page_event_regressions[(page, event["id"])] = {
                    "regression_delta_position": delta_pos,
                    "regression_delta_clicks": delta_clk,
                    "context_event_id": event.get("id"),
                    "context_event_label": event.get("label"),
                }

    # Annotate rows: for page+metric rows, add regression info from the first matching event.
    for row in rows:
        new_row = dict(row)
        if row.get("breakdown_dimension") == "page":
            page = row.get("breakdown_value", "")
            for event in events:
                key = (page, event["id"])
                if key in page_event_regressions:
                    new_row.update(page_event_regressions[key])
                    break
        result.append(new_row)
    return result


# ---------------------------------------------------------------------------
# Post-processor dispatch map (Story 6.3, T4.1).
# Keys are report ``id`` values (scoped to their module — uniqueness is assumed
# within a module). Values are callables with signature:
#   f(rows, date_from, date_to, project_id="", **kw) -> list[dict]
# AD-2: no module-specific imports in the callable implementations above.
# ---------------------------------------------------------------------------
POST_PROCESSORS: dict[str, Callable] = {
    "position_movements": _compute_wow_position_delta,
    "post_deploy_regressions": _compute_post_deploy_regressions,
}


def _load_geography_projection(project_id: str):
    """The Project's published Country meaning, or None when it has none.

    Read here rather than passed in, because the caller used to pass
    ``project_preferences``-derived posture -- the mutable field Story 48.2
    retires as an authority. Returning None is a real answer ("this Project has
    published no Country version"), and the caller must not turn it into a
    default grouping.

    A read failure is also None: a report that cannot reach Governance renders
    the retained country rows ungrouped rather than failing, and says so through
    the absent ``geography`` block. It never guesses a grouping.
    """

    try:
        from core.country_registry import load_projection  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT state
                    FROM app.project_capabilities
                    WHERE project_id = %s AND capability_key = 'country'
                    """,
                    (project_id,),
                )
                row = cur.fetchone()
            if row is None or str(row[0]) not in {"ready", "degraded"}:
                return None
            return load_projection(conn, project_id=project_id)
    except Exception:  # noqa: BLE001
        logger.warning("reports: geography projection unavailable project=%s", project_id)
        return None


def render_report(
    loaded_modules: list,
    project_id: str,
    report_id: str,
    date_from: str,
    date_to: str,
    *,
    trace_id: str | None = None,
    today: date | None = None,
    metric_definitions: dict | None = None,
    llm_commentary_guidelines: str | None = None,
    # Superseded by the published Country hierarchy version and IGNORED. Kept
    # only so the existing caller keeps type-checking until the route owner
    # drops the argument; it selects nothing, so there is no dual-read.
    geographic_posture: object | None = None,
    # AI-344: the caller the context-event read opens its scoped connection for
    # when the deployment keeps no DuckDB mirror. Without it, a report whose
    # post-processor needs the events says they were unavailable.
    identity: str | None = None,
) -> tuple[str, dict, str]:
    """Render a report pack end-to-end.

    Returns ``(summary_text, envelope, widget_uri)``.

    Raises :class:`ReportNotFound` when the module or report id is unknown.

    R6: optional ``metric_definitions`` (injected into envelope data so the
    widget tooltip/direction-tint lights up) and ``llm_commentary_guidelines``
    (appended to narrative_prompt so LLM commentary is grounded) are forwarded
    from the merged flow.report override fetched by the caller (get_report in
    main.py).  When absent (no override), behaviour is unchanged.
    """
    if "/" not in report_id:
        raise ReportNotFound(f"Report not found: {report_id}")
    module_name, _, report_def_id = report_id.partition("/")

    loaded = _find_module(loaded_modules, module_name)
    report = find_report(loaded_modules, module_name, report_def_id)
    if loaded is None or report is None:
        raise ReportNotFound(f"Report not found: {report_id}")

    start, end = resolve_date_window(report, date_from, date_to, today=today)

    from core import warehouse  # noqa: PLC0415

    # Story 6.3: pass optional connectors[] from report def for cross-source reports.
    report_connectors: list[str] | None = report.get("connectors") or None

    all_rows = warehouse.query_report(
        project_id,
        module_name,
        report.get("metrics", []),
        report.get("dimensions", []),
        start,
        end,
        connectors=report_connectors,
    )

    # G-06 fix: warehouse now returns current + prior window rows so delta/delta_pct
    # is populated. Split here; post-processors and data.rows only see current-window
    # rows; compute_rollup (inside build_summary) receives all_rows for delta computation.
    from core import rollup as _rollup_module  # noqa: PLC0415
    _current_rows, _prior_rows = _rollup_module._split_periods(all_rows, start, end)
    # Story 48.2: the split is driven by the PUBLISHED Country hierarchy
    # version, not by `project_preferences.geographic_mode`. That mutable field
    # could change between two runs of the same report with nothing recording
    # which grouping produced which number; every semantic row now names the
    # exact version that classified it.
    #
    # No published version means the Project has no governed Country meaning
    # yet. The honest answer is then to leave the country rows exactly as
    # retained -- never to invent a `global` posture and present ungrouped data
    # as if grouping had been considered.
    _geo_result = None
    _geo_projection = _load_geography_projection(project_id)
    if _geo_projection is not None:
        from core.geographic_semantics import group_geography_reporting_rows  # noqa: PLC0415

        # Story 37.9, kept: resolution reads the client's CONFIRMED conformance
        # mappings on top of the platform vocabulary, so a spelling repaired
        # once is applied at the NEXT READ with no fact rewrite. Fail-soft here
        # is deliberate and bounded: an unavailable MDM layer degrades to
        # vocabulary-only, which produces MORE Unknown -- never a guess, and
        # never a failed report.
        _country_resolver = None
        try:
            from core.geographic_conformance import make_country_resolver  # noqa: PLC0415

            _country_resolver = make_country_resolver(project_id=project_id)
        except Exception:  # noqa: BLE001
            _country_resolver = None
        _geo_result = group_geography_reporting_rows(
            _current_rows, _geo_projection, resolver=_country_resolver
        )
        _prior_geo_result = group_geography_reporting_rows(
            _prior_rows, _geo_projection, resolver=_country_resolver
        )
        _current_rows = list(_geo_result.rows)
        _prior_rows = list(_prior_geo_result.rows)
        # The dead end closes here: unresolved spellings become governed
        # 'proposed' suggestions and a dq_geography firing visible to the
        # Epic 13 monitors and get_data_quality_report.
        if _geo_result.data_quality:
            try:
                from core.geographic_conformance import (  # noqa: PLC0415
                    record_unmapped_country_evidence,
                )

                record_unmapped_country_evidence(
                    project_id,
                    _geo_result.data_quality,
                    window_date=str(end),
                    emit_firing=False,
                )
            except Exception:  # noqa: BLE001
                pass

    # rows = current-window rows (for post-processors, ordering, top_n, envelope data.rows)
    rows = _current_rows
    # Story 6.3: apply post-processor if registered for this report id (T4.3).
    # Post-processors return a NEW rows list (no in-place mutation — T4.4).
    post_proc = POST_PROCESSORS.get(report.get("id", ""))
    post_deploy_events: list[dict] | None = None
    no_deploy_events = False
    deploy_events_unavailable: dict | None = None
    if post_proc is not None:
        rows = post_proc(rows, start, end, project_id=project_id, identity=identity)
        # post_deploy_regressions tags rows with _no_deploy_events when no events
        # found, and with _deploy_events_unavailable when they could not be read
        # (AI-344) -- two different facts, said differently downstream.
        if report.get("id") == "post_deploy_regressions":
            if rows and rows[0].get("_deploy_events_unavailable"):
                deploy_events_unavailable = rows[0]["_deploy_events_unavailable"]
                rows = [
                    {k: v for k, v in r.items() if k != "_deploy_events_unavailable"}
                    for r in rows
                ]
            elif rows and rows[0].get("_no_deploy_events"):
                no_deploy_events = True
                # Strip the internal marker from rows before envelope build.
                rows = [{k: v for k, v in r.items() if k != "_no_deploy_events"} for r in rows]
                post_deploy_events = []
            else:
                # Collect deployment events for envelope context_events.
                from core.context_events import ContextEventsUnavailable  # noqa: PLC0415

                try:
                    post_deploy_events = _fetch_deployment_events(
                        project_id, start, end, identity=identity
                    )
                except ContextEventsUnavailable as unavailable:
                    deploy_events_unavailable = unavailable.payload

    order_by = report.get("layout", {}).get("order_by", "")
    rows = _apply_ordering(rows, order_by)

    top_n = report.get("layout", {}).get("top_n")
    if isinstance(top_n, int) and top_n > 0:
        rows = rows[:top_n]

    # Cross-connector scope guard (AC3, Story 6.3): when a report declares
    # multiple connectors, some connector data may be absent (disabled project).
    # When any declared cross-connector metrics are absent from results, append
    # an absence note. AD-2: check is generic (report def drives it, not module names).
    if report_connectors and len(report_connectors) > 1:
        result_metrics = {r.get("metric") for r in rows}
        absent_metrics = [m for m in report.get("metrics", []) if m not in result_metrics]
        if absent_metrics:
            report = dict(report)  # shallow copy; don't mutate the registry
            absent_note = phrase(
                "report_partial_data_note", metrics=", ".join(absent_metrics)
            )
            report["narrative_prompt"] = (
                (report.get("narrative_prompt") or "") + absent_note
            )[:200]

    # Build summary with AD-9 absence note when no deploy events found.
    if no_deploy_events:
        report = dict(report)
        report["narrative_prompt"] = (
            (report.get("narrative_prompt") or "") + phrase("report_no_deploy_events")
        )[:200]
    elif deploy_events_unavailable is not None:
        # AI-344: not "aucun déploiement" -- the window was not read.
        report = dict(report)
        report["narrative_prompt"] = (
            (report.get("narrative_prompt") or "")
            + phrase("report_deploy_events_unavailable")
        )[:200]

    # Determine connectors present in the result rows for envelope metadata.
    result_connectors = sorted(
        {r.get("connector") for r in rows if r.get("connector")}
    ) or (report_connectors or [module_name])

    # Story 6.4: feed the narrative any deployment context events collected by the
    # post_deploy_regressions processor so the why-section can cite them (AD-9).
    # G-06 fix: pass all_rows (current + prior) to build_summary so compute_rollup
    # inside can populate delta/delta_pct. data.rows in the envelope still contains
    # only current-window rows (passed as `rows`).
    summary = build_summary(
        report, _current_rows + _prior_rows, start, end, module_name,
        project_id=project_id,
        context_events=post_deploy_events or None,
        llm_commentary_guidelines=llm_commentary_guidelines,
        context_events_unavailable=deploy_events_unavailable,
    )
    envelope = build_envelope(
        report, rows, module_name, start, end, project_id, trace_id=trace_id,
        context_events=post_deploy_events,
        metric_definitions=metric_definitions,
    )
    # AI-273. `build_envelope` above ships `stale_since_evaluated: False`, and
    # until this line nothing downstream ever turned it True on this path: a
    # rendered report frozen into a shared Render said "nobody checked" forever.
    # The SAME evaluator `get_daily_report` uses, with this call's own window --
    # a second one would let a report and the card built from its rows disagree
    # about their own freshness. See `cards._evaluate_freshness` for the shape.
    try:
        from core.health_enrichment import enrich_envelope_with_health  # noqa: PLC0415

        enrich_envelope_with_health(envelope, project_id, date_from=start, date_to=end)
    except Exception:  # noqa: BLE001
        # No-op on an unreachable DB or an unreadable window: the envelope keeps
        # the builder's `False`, which is what it read before this existed.
        logger.debug("reports: freshness not evaluated for project_id=%s", project_id)

    # AI-297. `build_envelope` above takes a `confidence` parameter and this call
    # never filled it -- a socket cut and left empty since Story 4.2, so a
    # rendered report carried no completeness, no traceability and no
    # `limiting_term`. `compute_confidence` had exactly ONE caller in the
    # repository (`get_daily_report`), the same shape AI-273 found for freshness:
    # the surfaces that get frozen into a shared Render were the ones that never
    # asked.
    #
    # AFTER the health enrichment, never before, and that order is the point.
    # `stale_since` is handed over so the two halves of "how fresh is this" cannot
    # answer in opposite directions in one payload -- a set `stale_since` beside a
    # freshness term of 1.0 is the exact contradiction story 53.3 closed on the
    # daily-report path. The health verdict caps the freshness term.
    try:
        from core import confidence as confidence_module  # noqa: PLC0415

        computed = confidence_module.compute_confidence(
            project_id,
            result_connectors,
            rows=rows,
            date_from=start,
            date_to=end,
            stale_since=((envelope.get("meta") or {}).get("freshness") or {}).get("stale_since"),
        )
        if computed is not None:
            envelope.setdefault("meta", {})["confidence"] = computed
    except Exception:  # noqa: BLE001
        # Best-effort, exactly as on the daily-report path: no `confidence` key
        # rather than a number nobody could stand behind.
        logger.debug("reports: confidence not computed for project_id=%s", project_id)
    if _geo_result is not None and _geo_projection is not None:
        from core.geographic_semantics import (  # noqa: PLC0415
            geography_bucket_descriptors,
            reconciliation,
        )

        # Story 37.8, kept by 48.2: the split is expressed as governed MARKETS
        # -- stable id and operator label -- never as raw ISO codes. What 48.2
        # adds is the version that produced it and the proof that the buckets
        # add up, so a reader can check the split instead of trusting it.
        _geo_buckets = geography_bucket_descriptors(_geo_projection)
        envelope["data"]["geography"] = {
            "geography_hierarchy_version_id": _geo_projection.hierarchy_version_id,
            "vocabulary_version_id": _geo_projection.vocabulary_version_id,
            "country_partition": _geo_result.country_partition,
            "country_partition_available": any(
                row.get("breakdown_dimension") == "market" for row in rows
            ),
            "excluded_parallel_rows": _geo_result.excluded_parallel_rows,
            "buckets": _geo_buckets,
            "markets": [
                {"id": bucket["id"], "label": bucket["label"]}
                for bucket in _geo_buckets
                if bucket["kind"] in {"market", "region"}
            ],
            "bindable_market_ids": [
                bucket["id"] for bucket in _geo_buckets if bucket["bindable"]
            ],
            "rest_of_world": {
                "id": _geo_projection.rest_of_world_id,
                "label": _geo_projection.rest_of_world_label,
                "default_drill": _geo_projection.rest_of_world_drill,
                "country_codes": list(_geo_projection.rest_of_world_members()),
            },
            "reconciliation": reconciliation(_geo_result),
        }
        envelope["meta"]["alerts"].extend(_geo_result.data_quality)
    # Override connectors in data envelope when multi-connector.
    if report_connectors:
        envelope["data"]["connectors"] = result_connectors

    # AD-9: explicit absence for post_deploy_regressions with no events.
    if report.get("id") == "post_deploy_regressions" and post_deploy_events is not None:
        envelope["data"]["context_events"] = [
            {
                "id": e.get("id"),
                "event_date": e.get("event_date"),
                "type": e.get("type"),
                "label": e.get("label"),
            }
            for e in post_deploy_events
        ]
    if report.get("id") == "post_deploy_regressions" and deploy_events_unavailable is not None:
        # No `context_events` key at all: an empty list would claim the window
        # was read. The reason and the repair travel instead.
        envelope["data"]["context_events_unavailable"] = deploy_events_unavailable

    widget_uri = loaded.manifest.get("widget_ref") or DEFAULT_REPORT_WIDGET_URI
    summary = _state_the_limiting_term(summary, envelope)
    return summary, envelope, widget_uri


def _state_the_limiting_term(summary: str, envelope: dict) -> str:
    """Say, in the text channel, which term is holding this report back (AI-297).

    `compute_confidence` argues at length in its own docstring for why
    `limiting_term` exists: a single number over three different natures --
    completeness, freshness, traceability -- compensates, and `overview.md:32-34`
    refuses that collapse. « Naming the limiter is the minimum that keeps the
    number readable. »

    Nothing named it. Measured 2026-08-16: `limiting_term` appeared in
    `core/confidence.py`, where it is computed, and NOWHERE ELSE in the server --
    no summary, no screen, no widget. The disclosure that was designed to keep a
    number honest was itself unreadable, which makes the number it guards worse
    than absent: present, and trusted.

    THE TEXT CHANNEL, and not a fourth structured key. `meta.confidence` already
    carries every term for the model; what was missing is the sentence a PERSON
    reads. AD-1 splits the two channels precisely so a disclosure can be said in
    words on one and be machine-readable on the other.

    Silent when there is nothing to disclose -- no confidence computed, or no
    term known well enough to be the weakest. A line that said "limiting term:
    none" on every unmeasured report would be noise, and noise is how a real
    disclosure stops being read.
    """
    confidence = (envelope.get("meta") or {}).get("confidence")
    if not isinstance(confidence, dict):
        return summary
    limiting = confidence.get("limiting_term")
    if not limiting:
        return summary

    value = confidence.get(limiting)
    unknown = confidence.get("unknown_terms") or []
    line = phrase("report_limiting_term", term=limiting)
    if isinstance(value, (int, float)):
        line += phrase("report_limiting_term_value", value=value)
    if unknown:
        # An unknown term is why `score` is None, and a reader who sees only the
        # limiter would otherwise wonder where the overall number went.
        line += phrase(
            "report_limiting_term_unmeasured",
            terms=", ".join(str(u) for u in unknown),
        )

    lines = summary.splitlines()
    if len(lines) >= _MAX_LINES:
        # The 30-line ceiling is NFR1 and it is not negotiable for a disclosure:
        # a summary that grew past it would be truncated somewhere else, and the
        # thing dropped would be chosen by an accident of length.
        return summary
    return "\n".join([*lines, line])
