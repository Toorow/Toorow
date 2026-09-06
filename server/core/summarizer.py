"""toorow — LLM-channel summary builder (Story 1.5, T3).

Generates a ≤30-line plain-text rollup summary from aggregated mart rows.

# NFR1 / CAP-3: summary is generated from metric TOTALS (aggregated rollup),
# never from serializing individual rows — full dataset stays in structuredContent.
# AD-2: source-agnostic — no module-specific strings here.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# THE SUMMARY IS A RECIT, so its sentences are not spelled here: they are
# rendered from the one catalogue, in the reader's language
# (docs/product-architecture/analyze-and-test.md, amendment 2026-08-25, from
# Jean's arbitration of 2026-08-22). This module keeps the FORM -- which totals,
# in which order, with which provenance footer.
# ---------------------------------------------------------------------------
from core.narrative import context_absence_line
from core.narrative_phrases import metric_label, phrase

# ---------------------------------------------------------------------------
# NFR1 P1 gate: hard ceiling on summary line count.
# ---------------------------------------------------------------------------
_MAX_LINES = 30

#: The metrics this summary names FIRST, in this order. Identifiers, not words:
#: the words come from `metric_label`. AD-2 -- warehouse vocabulary, no module
#: name. Any other metric present in the rollup follows, under its own key.
_SUMMARY_METRICS = ("sessions", "active_users", "conversions")


def build_daily_report_summary(
    rows: list[dict],
    date_range: dict,
    connectors: list[str],
    project_id: str = "default",
    context_events: list[dict] | None = None,
    as_of: str | None = None,
    context_events_unavailable: dict | None = None,
) -> str:
    """Generate a ≤30-line plain-text summary from aggregated rollup data.

    The summary is built from metric totals computed over *rows*. Individual
    row data is never serialized into the summary (AD-1 / NFR1).

    Parameters
    ----------
    rows:
        Raw fact_daily_kpi rows from the warehouse layer.
    date_range:
        Dict with ``start`` and ``end`` ISO-8601 date strings.
    connectors:
        List of connector names included in the query.
    project_id:
        Project identifier (for empty-state messaging).
    context_events:
        Optional list of context event dicts from app.context_events.
        When None or empty: the summary includes an explicit "contexte manquant"
        line (AD-9). When provided: up to 5 events are listed before the footer.
    as_of:
        Optional ISO-8601 datetime string. When set, adds an as-of provenance
        line in the header: "Données telles que connues le YYYY-MM-DD HH:MM UTC"
        (Story 4.6, AC5, AD-9 provenance, UX-DR10 French-first).
    context_events_unavailable:
        The ``{reason, repair}`` of ``ContextEventsUnavailable`` when NO store
        could serve the window (AI-350). ``context_events`` is then ``[]`` for a
        reason that is not emptiness, and the context line says the window was
        not read -- the catalogue's ``context_unavailable``, via the one composer
        ``narrative.context_absence_line``, never a wording of this module's own.
        ``None`` means the window WAS read, which is the only state that may be
        described as calm.

    Returns
    -------
    str
        Plain-text summary, guaranteed ≤30 lines.
    """
    start = date_range.get("start", "?")
    end = date_range.get("end", "?")

    if not rows:
        return _empty_state_summary(
            project_id,
            start,
            end,
            context_events,
            as_of=as_of,
            context_events_unavailable=context_events_unavailable,
        )

    # ------------------------------------------------------------------
    # Aggregate rollup: totals per (connector, metric)
    # ------------------------------------------------------------------
    connector_metric_totals: dict[str, dict[str, float]] = {}
    freshness_by_connector: dict[str, str] = {}
    pull_id_by_connector: dict[str, str] = {}

    for row in rows:
        connector = row.get("connector", "unknown")
        metric = row.get("metric", "unknown")
        value = row.get("value") or 0
        loaded_at = row.get("loaded_at") or ""
        pull_id = row.get("pull_id") or ""

        if connector not in connector_metric_totals:
            connector_metric_totals[connector] = {}
        connector_metric_totals[connector][metric] = (
            connector_metric_totals[connector].get(metric, 0) + float(value)
        )

        # Track latest freshness / pull_id per connector
        if loaded_at > freshness_by_connector.get(connector, ""):
            freshness_by_connector[connector] = str(loaded_at)
        if pull_id > pull_id_by_connector.get(connector, ""):
            pull_id_by_connector[connector] = pull_id

    # ------------------------------------------------------------------
    # Build summary lines
    # ------------------------------------------------------------------
    lines: list[str] = []

    lines.append(phrase("summary_daily_report_header", start=start, end=end))
    # Story 4.6 (AC5): as-of provenance line (AD-9). Placed immediately after the
    # date range line (line 2), before connectors. The authority formerly named
    # here -- UX-DR10 "French-first" -- lives in no ratified document and was
    # replaced by the arbitration of 2026-08-22.
    if as_of:
        from datetime import datetime, timezone  # noqa: PLC0415

        try:
            dt = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            as_of_display = dt.strftime("%Y-%m-%d %H:%M UTC")
        except ValueError:
            as_of_display = as_of
        lines.append(phrase("summary_as_of_known", as_of=as_of_display))
        # review-epic-4 F-3: the as-of path reads raw source values — costs are
        # NOT currency-normalized, unlike the current view.
        lines.append(phrase("summary_as_of_source_currency"))
    connector_display = (
        ", ".join(connectors) if connectors else phrase("summary_connectors_all")
    )
    lines.append(phrase("summary_connectors", connectors=connector_display))
    lines.append("")

    # Cap connector sections to keep ≤30 lines
    # Each connector block = display_name + metrics + blank ≈ 5 lines.
    # 30 lines − 3 header − 2 footer = 25 usable → ~5 connectors safely.
    # If more, truncate with a note.
    shown_connectors = sorted(connector_metric_totals.keys())
    max_connectors = 5
    overflow_count = max(0, len(shown_connectors) - max_connectors)
    if overflow_count:
        shown_connectors = shown_connectors[:max_connectors]

    for connector in shown_connectors:
        metric_totals = connector_metric_totals[connector]
        lines.append(connector)

        for metric in _SUMMARY_METRICS:
            if metric in metric_totals:
                total = metric_totals[metric]
                formatted = f"{int(total):,}".replace(",", " ")  # narrow no-break space
                lines.append(
                    phrase(
                        "summary_metric_line",
                        label=metric_label(metric),
                        value=formatted,
                    )
                )

        # A metric this summary does not lead with keeps its own key: naming a
        # measure nobody declared would invent a name.
        for metric, total in sorted(metric_totals.items()):
            if metric not in _SUMMARY_METRICS:
                formatted = f"{int(total):,}".replace(",", " ")
                lines.append(
                    phrase("summary_metric_line", label=metric, value=formatted)
                )

        lines.append("")

    if overflow_count:
        lines.append(phrase("summary_connectors_overflow", count=overflow_count))
        lines.append("")

    # ------------------------------------------------------------------
    # Context events section (AC6 / AD-9)
    # When events are present: list up to 5, capped to protect the 30-line
    # ceiling (_MAX_LINES enforcement below truncates overflow).
    # When absent (None or []): explicit "context missing" line (AD-9
    # hard gate HG-1 — never omit, never invent).
    # ------------------------------------------------------------------
    if context_events:
        lines.append(phrase("summary_context_events_heading"))
        _MAX_CONTEXT_EVENTS = 5
        shown_events = context_events[:_MAX_CONTEXT_EVENTS]
        remaining = len(context_events) - len(shown_events)
        for evt in shown_events:
            evt_date = evt.get("event_date", "?")
            evt_type = evt.get("type", "?")
            evt_label = evt.get("label", "?")
            lines.append(
                phrase(
                    "summary_context_event_item",
                    date=evt_date,
                    type=evt_type,
                    label=evt_label,
                )
            )
        if remaining > 0:
            lines.append(phrase("summary_context_events_overflow", count=remaining))
        lines.append("")
    else:
        # AI-350: read-and-empty, or never read -- one fork, one composer.
        lines.append(
            context_absence_line(
                context_events_unavailable,
                read_and_empty=phrase("summary_context_none"),
            )
        )
        lines.append("")

    # ------------------------------------------------------------------
    # Footer: freshness + pull_id (use first connector's values as summary)
    # ------------------------------------------------------------------
    primary_connector = shown_connectors[0] if shown_connectors else ""
    freshness = freshness_by_connector.get(primary_connector, "N/A")
    pull_id = pull_id_by_connector.get(primary_connector, "N/A")

    if freshness and freshness != "N/A":
        # Trim to date part for readability if it's a full timestamp
        freshness_display = str(freshness)[:10] if len(str(freshness)) > 10 else str(freshness)
    else:
        freshness_display = "N/A"

    lines.append(phrase("summary_freshness", value=freshness_display))
    lines.append(phrase("summary_pull_id", pull_id=pull_id))

    # ------------------------------------------------------------------
    # Hard ceiling enforcement (NFR1 P1 gate)
    # ------------------------------------------------------------------
    if len(lines) > _MAX_LINES:
        lines = lines[:_MAX_LINES - 1]
        lines.append(phrase("summary_truncated"))

    return "\n".join(lines)


def _empty_state_summary(
    project_id: str,
    start: str,
    end: str,
    context_events: list[dict] | None = None,
    as_of: str | None = None,
    context_events_unavailable: dict | None = None,
) -> str:
    """Return the empty-state message (T3.4) — always ≤7 lines.

    AD-9 / HG-1: always includes the context status line. The empty state
    short-circuits before the main body, so context events are added here
    rather than in the main path. Design decision: the empty state is kept
    self-contained and shows the same context block as the main path
    (either event list, "aucun evenement connu", or -- AI-350 -- the catalogue's
    ``context_unavailable`` when no store could serve the window).

    THIS IS THE BRANCH AI-350 REPAIRED. It is the branch `get_daily_report` takes
    when the warehouse holds no row for the window, and it was the only reader of
    context events that had no channel for the unread state: measured 2026-09-01
    on disposable Postgres, a project whose record held 5 live events was told
    "Contexte : aucun événement connu pour cette période" on the model channel
    while `meta.context_events_unavailable` on the SAME response said the events
    were never read.
    """
    lines = [
        phrase("summary_no_data"),
        phrase("summary_project", project_id=project_id),
        phrase("summary_period", start=start, end=end),
    ]
    # Story 4.6 (AC5): as-of provenance in empty state too.
    if as_of:
        from datetime import datetime, timezone  # noqa: PLC0415

        try:
            dt = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            as_of_display = dt.strftime("%Y-%m-%d %H:%M UTC")
        except ValueError:
            as_of_display = as_of
        lines.append(phrase("summary_as_of_known", as_of=as_of_display))
        # review-epic-4 F-3: the as-of path reads raw source values — costs are
        # NOT currency-normalized, unlike the current view.
        lines.append(phrase("summary_as_of_source_currency"))
    # AD-9 / HG-1: explicit context status — never omit, never invent.
    if context_events:
        lines.append(phrase("summary_context_events_heading"))
        for evt in context_events[:5]:
            lines.append(
                phrase(
                    "summary_context_event_item",
                    date=evt.get("event_date", "?"),
                    type=evt.get("type", "?"),
                    label=evt.get("label", "?"),
                )
            )
    else:
        lines.append(
            context_absence_line(
                context_events_unavailable,
                read_and_empty=phrase("summary_context_none"),
            )
        )
    return "\n".join(lines)
