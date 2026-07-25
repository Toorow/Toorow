"""toorow — LLM-channel summary builder (Story 1.5, T3).

Generates a ≤30-line plain-text rollup summary from aggregated mart rows.

# NFR1 / CAP-3: summary is generated from metric TOTALS (aggregated rollup),
# never from serializing individual rows — full dataset stays in structuredContent.
# AD-2: source-agnostic — no module-specific strings here.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# NFR1 P1 gate: hard ceiling on summary line count.
# ---------------------------------------------------------------------------
_MAX_LINES = 30


def build_daily_report_summary(
    rows: list[dict],
    date_range: dict,
    connectors: list[str],
    project_id: str = "default",
    context_events: list[dict] | None = None,
    as_of: str | None = None,
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
        When None or empty: the summary includes an explicit "context missing"
        line (AD-9). When provided: up to 5 events are listed before the footer.
    as_of:
        Optional ISO-8601 datetime string. When set, adds an as-of provenance
        line in the header: "Données telles que connues le YYYY-MM-DD HH:MM UTC"
        (Story 4.6, AC5, AD-9 provenance, UX-DR10 French-first).

    Returns
    -------
    str
        Plain-text summary, guaranteed ≤30 lines.
    """
    start = date_range.get("start", "?")
    end = date_range.get("end", "?")

    if not rows:
        return _empty_state_summary(project_id, start, end, context_events, as_of=as_of)

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
    # Friendly metric label map (UX-DR10: French-first labels)
    # ------------------------------------------------------------------
    _METRIC_LABELS: dict[str, str] = {
        "sessions": "Sessions",
        "active_users": "Utilisateurs actifs",
        "conversions": "Conversions",
    }

    # ------------------------------------------------------------------
    # Build summary lines
    # ------------------------------------------------------------------
    lines: list[str] = []

    lines.append(f"Rapport quotidien — {start} au {end}")
    # Story 4.6 (AC5): as-of provenance line — French-first (UX-DR10 / AD-9).
    # Placed immediately after the date range line (line 2), before connectors.
    if as_of:
        from datetime import datetime, timezone  # noqa: PLC0415

        try:
            dt = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            as_of_display = dt.strftime("%Y-%m-%d %H:%M UTC")
        except ValueError:
            as_of_display = as_of
        lines.append(f"Données telles que connues le {as_of_display}")
        # review-epic-4 F-3: the as-of path reads raw source values — costs are
        # NOT currency-normalized, unlike the current view.
        lines.append("Attention : coûts en devise source (non normalisés) en vue as-of.")
    connector_display = ", ".join(connectors) if connectors else "tous"
    lines.append(f"Connecteurs : {connector_display}")
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

        for metric, label in _METRIC_LABELS.items():
            if metric in metric_totals:
                total = metric_totals[metric]
                formatted = f"{int(total):,}".replace(",", " ")  # narrow no-break space
                lines.append(f"- {label} : {formatted}")

        # Any metrics not in the known label map
        for metric, total in sorted(metric_totals.items()):
            if metric not in _METRIC_LABELS:
                formatted = f"{int(total):,}".replace(",", " ")
                lines.append(f"- {metric} : {formatted}")

        lines.append("")

    if overflow_count:
        lines.append(f"… et {overflow_count} connecteur(s) supplémentaire(s)")
        lines.append("")

    # ------------------------------------------------------------------
    # Context events section (AC6 / AD-9)
    # When events are present: list up to 5, capped to protect the 30-line
    # ceiling (_MAX_LINES enforcement below truncates overflow).
    # When absent (None or []): explicit "context missing" line (AD-9
    # hard gate HG-1 — never omit, never invent).
    # ------------------------------------------------------------------
    if context_events:
        lines.append("Evenements de contexte :")
        _MAX_CONTEXT_EVENTS = 5
        shown_events = context_events[:_MAX_CONTEXT_EVENTS]
        remaining = len(context_events) - len(shown_events)
        for evt in shown_events:
            evt_date = evt.get("event_date", "?")
            evt_type = evt.get("type", "?")
            evt_label = evt.get("label", "?")
            lines.append(f"- {evt_date} [{evt_type}] {evt_label}")
        if remaining > 0:
            lines.append(f"... et {remaining} autre(s)")
        lines.append("")
    else:
        lines.append("Contexte : aucun evenement connu pour cette periode.")
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

    lines.append(f"Fraîcheur : {freshness_display}")
    lines.append(f"Pull ID : {pull_id}")

    # ------------------------------------------------------------------
    # Hard ceiling enforcement (NFR1 P1 gate)
    # ------------------------------------------------------------------
    if len(lines) > _MAX_LINES:
        lines = lines[:_MAX_LINES - 1]
        lines.append("[tronqué — voir structuredContent pour le détail complet]")

    return "\n".join(lines)


def _empty_state_summary(
    project_id: str,
    start: str,
    end: str,
    context_events: list[dict] | None = None,
    as_of: str | None = None,
) -> str:
    """Return the empty-state message (T3.4) — always ≤7 lines.

    AD-9 / HG-1: always includes the context status line. The empty state
    short-circuits before the main body, so context events are added here
    rather than in the main path. Design decision: the empty state is kept
    self-contained and shows the same context block as the main path
    (either event list or "aucun evenement connu").
    """
    lines = [
        "Aucune donnée disponible pour la période demandée.",
        f"Projet : {project_id}",
        f"Période : {start} — {end}",
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
        lines.append(f"Données telles que connues le {as_of_display}")
        # review-epic-4 F-3: the as-of path reads raw source values — costs are
        # NOT currency-normalized, unlike the current view.
        lines.append("Attention : coûts en devise source (non normalisés) en vue as-of.")
    # AD-9 / HG-1: explicit context status — never omit, never invent.
    if context_events:
        lines.append("Evenements de contexte :")
        for evt in context_events[:5]:
            evt_date = evt.get("event_date", "?")
            evt_type = evt.get("type", "?")
            evt_label = evt.get("label", "?")
            lines.append(f"- {evt_date} [{evt_type}] {evt_label}")
    else:
        lines.append("Contexte : aucun evenement connu pour cette periode.")
    return "\n".join(lines)
