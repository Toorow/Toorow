"""toorow — Morning briefing builder (Story 6.7, AC2, AC3).

Pure function — no warehouse queries, no Postgres calls, no LLM calls.

The briefing builder receives pre-fetched data from the caller
(_run_due_briefings in scheduler.py) and returns the insights JSONB dict
(AC2 shape) ready for storage in app.morning_briefings.

This is the canonical implementation of the AD-1 "precomputed briefing"
pattern: nightly scheduler builds it once, get_daily_report serves it via
ONE SELECT from app.morning_briefings (zero warehouse calls on the hot path).

Step order (Story 6.7, Dev Notes):
    1. dispatch_nightly  (data pulls)
    2. _run_alert_check  (business thresholds)
    3. _run_anomaly_alert_check (anomalies)
    4. run_due_notebooks (scheduled notebooks)
    5. run_due_briefings ← this module is called by step 5 (always last)
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# French metric labels (UX-DR10 — French-first headlines).
# ---------------------------------------------------------------------------

METRIC_LABELS_FR: dict[str, str] = {
    "clicks": "Clics",
    "impressions": "Impressions",
    "average_position": "Position moyenne",
    "sessions": "Sessions",
    "active_users": "Utilisateurs actifs",
    "conversions": "Conversions",
    "cost": "Coût",
}


def _metric_label(metric: str) -> str:
    """Return the French label for *metric*, falling back to metric name as-is."""
    return METRIC_LABELS_FR.get(metric, metric)


def _direction(delta: float | None) -> str:
    """Return 'en baisse' or 'en hausse' based on the sign of *delta*."""
    if delta is not None and delta < 0:
        return "en baisse"
    return "en hausse"


def _format_value(value: float | None) -> str:
    """Return a human-readable string for a metric value."""
    if value is None:
        return "N/A"
    if value == int(value):
        return str(int(value))
    return f"{value:.1f}"


def _build_headline(
    insight_type: str,
    metric: str,
    connector: str,
    delta: float | None,
    delta_pct: str | None,
    value_curr: float | None,
    value_prev: float | None,
) -> str:
    """Build a French headline for one insight (AC3 template rules)."""
    label = _metric_label(metric)
    direction = _direction(delta)
    curr_str = _format_value(value_curr)
    prev_str = _format_value(value_prev)

    # Compute absolute pct if not provided
    if delta_pct is not None:
        pct_str = delta_pct.lstrip("+-").rstrip("%")
        try:
            pct_num = float(pct_str)
        except (ValueError, TypeError):
            pct_num = None
    else:
        pct_num = None

    if insight_type == "business_alert":
        if pct_num is not None:
            return (
                f"{label} {connector.upper()} {direction} de {pct_num:.0f} %"
                f" ({prev_str} → {curr_str})"
            )
        return f"{label} {connector.upper()} {direction} ({prev_str} → {curr_str})"
    elif insight_type == "anomaly":
        return f"Anomalie: {label} {direction} ({prev_str} → {curr_str})"
    else:  # notable_delta
        if pct_num is not None:
            return (
                f"{label} {connector.upper()} {direction} de {pct_num:.0f} %"
                f" ({prev_str} → {curr_str})"
            )
        return f"{label} {connector.upper()} {direction} ({prev_str} → {curr_str})"


def _build_citation(connector: str, metric: str, pull_ids: list[str]) -> str:
    """Build provenance citation in (connector:metric, pull_id) format (AC2 / AD-9)."""
    pull_id = pull_ids[0] if pull_ids else "unknown"
    return f"({connector}:{metric}, {pull_id})"


def _find_context_event(
    context_events: list[dict],
    briefing_date: str,
    window_days: int = 7,
) -> dict | None:
    """Return the most recent context_event within *window_days* of *briefing_date*, or None."""
    if not context_events:
        return None
    try:
        from datetime import date  # noqa: PLC0415

        target = date.fromisoformat(briefing_date)
    except (ValueError, TypeError):
        return None

    best: dict | None = None
    for evt in context_events:
        evt_date_raw = evt.get("event_date") or evt.get("date")
        if not evt_date_raw:
            continue
        try:
            evt_date = date.fromisoformat(str(evt_date_raw)[:10])
        except (ValueError, TypeError):
            continue
        diff = abs((target - evt_date).days)
        if diff <= window_days:
            if best is None:
                best = evt
            else:
                # prefer closer to briefing_date
                best_date_raw = best.get("event_date") or best.get("date")
                best_date = date.fromisoformat(str(best_date_raw)[:10])
                if abs((target - evt_date).days) < abs((target - best_date).days):
                    best = evt
    return best


def build_briefing(
    project_id: str,
    briefing_date: str,
    alert_firings: list[dict],
    rollup: dict,
    context_events: list[dict],
    nightly_run_id: str | None,
) -> dict:
    """Build the insights JSONB dict for one project's morning briefing (AC2 shape).

    Returns the insights JSONB dict (AC2 shape).
    Pure Python — no warehouse queries, no Postgres calls, no LLM calls.
    Inputs are all pre-fetched by the caller.

    Args:
        project_id:      Project identifier.
        briefing_date:   ISO date string (today in project tz).
        alert_firings:   Rows from app.alert_firings for the nightly window
                         (type != 'meta_alert').
        rollup:          From compute_rollup() for the project's default report.
        context_events:  From app.context_events for the last 7 days.
        nightly_run_id:  Identifier of the nightly run that produced this briefing.

    Returns:
        insights JSONB dict conforming to AC2 schema.
    """
    t0 = time.monotonic()

    # ------------------------------------------------------------------
    # Step 1: Classify alert_firings into business_alerts vs anomalies.
    # business_alerts: type == 'business_threshold'
    # anomalies: type == 'anomaly'
    # Both ranked by abs(delta_magnitude) = abs(observed_value - threshold) or
    # abs(observed_value) as a proxy when threshold is not meaningful.
    # ------------------------------------------------------------------
    business_alert_candidates: list[dict] = []
    anomaly_candidates: list[dict] = []

    for firing in alert_firings:
        ftype = (firing.get("type") or "").lower()
        if ftype == "meta_alert":
            continue  # always skip meta_alerts
        elif ftype == "anomaly":
            anomaly_candidates.append(firing)
        else:
            # business_threshold or any other non-meta type
            business_alert_candidates.append(firing)

    def _firing_magnitude(firing: dict) -> float:
        """Compute a sortable magnitude for ranking within type (highest first)."""
        obs = firing.get("observed_value")
        thr = firing.get("threshold")
        try:
            obs_f = float(obs) if obs is not None else 0.0
            thr_f = float(thr) if thr is not None else 0.0
            delta = obs_f - thr_f
            return abs(delta) if abs(delta) > 0 else abs(obs_f)
        except (TypeError, ValueError):
            return 0.0

    # Sort each group by magnitude desc
    business_alert_candidates.sort(key=_firing_magnitude, reverse=True)
    anomaly_candidates.sort(key=_firing_magnitude, reverse=True)

    # ------------------------------------------------------------------
    # Step 2: Build notable_delta candidates from rollup.
    # These are metrics that have a delta but did NOT already fire an alert.
    # ------------------------------------------------------------------
    alerted_metrics: set[str] = {
        (f.get("metric") or "") for f in (business_alert_candidates + anomaly_candidates)
    }

    notable_delta_candidates: list[dict] = []
    for metric, data in rollup.items():
        if metric in alerted_metrics:
            continue
        delta = data.get("delta")
        if delta is None:
            continue
        notable_delta_candidates.append(
            {
                "_metric": metric,
                "_data": data,
                "_magnitude": abs(float(delta)) if delta is not None else 0.0,
            }
        )
    notable_delta_candidates.sort(key=lambda x: x["_magnitude"], reverse=True)

    # ------------------------------------------------------------------
    # Step 3: Assemble ranked insights — business_alerts first,
    #         then anomalies, then notable_deltas. Max 5, target 3.
    # ------------------------------------------------------------------
    insights: list[dict] = []
    rank = 1
    MAX_INSIGHTS = 5

    # -- Business alerts --
    for firing in business_alert_candidates:
        if rank > MAX_INSIGHTS:
            break
        metric = firing.get("metric") or ""
        connector = firing.get("connector") or firing.get("source_system") or ""
        obs_value = firing.get("observed_value")
        threshold = firing.get("threshold")
        pull_ids = firing.get("pull_ids") or []
        if isinstance(pull_ids, str):
            import json as _json  # noqa: PLC0415
            try:
                pull_ids = _json.loads(pull_ids)
            except Exception:
                pull_ids = [pull_ids]
        firing_id = firing.get("id") or firing.get("firing_id")

        try:
            obs_f = float(obs_value) if obs_value is not None else None
            thr_f = float(threshold) if threshold is not None else None
            delta = (obs_f - thr_f) if (obs_f is not None and thr_f is not None) else None
        except (TypeError, ValueError):
            obs_f = None
            thr_f = None
            delta = None

        headline = _build_headline(
            "business_alert", metric, connector,
            delta, None, obs_f, thr_f
        )
        citation = _build_citation(connector, metric, pull_ids)

        insights.append(
            {
                "rank": rank,
                "type": "business_alert",
                "metric": metric,
                "connector": connector,
                "headline": headline,
                "citation": citation,
                "context_event_id": None,
                "context_event_label": None,
                "alert_id": firing_id,
                "pull_ids": pull_ids,
            }
        )
        rank += 1

    # -- Anomalies --
    for firing in anomaly_candidates:
        if rank > MAX_INSIGHTS:
            break
        metric = firing.get("metric") or ""
        connector = firing.get("connector") or firing.get("source_system") or ""
        obs_value = firing.get("observed_value")
        threshold = firing.get("threshold")
        pull_ids = firing.get("pull_ids") or []
        if isinstance(pull_ids, str):
            import json as _json  # noqa: PLC0415
            try:
                pull_ids = _json.loads(pull_ids)
            except Exception:
                pull_ids = [pull_ids]
        firing_id = firing.get("id") or firing.get("firing_id")

        try:
            obs_f = float(obs_value) if obs_value is not None else None
            thr_f = float(threshold) if threshold is not None else None
            delta = (obs_f - thr_f) if (obs_f is not None and thr_f is not None) else None
        except (TypeError, ValueError):
            obs_f = None
            thr_f = None
            delta = None

        # Find nearby context event for anomaly linkage (AC3 T3.5)
        ctx_evt = _find_context_event(context_events, briefing_date)
        ctx_id = (ctx_evt.get("id") if ctx_evt else None)
        ctx_label = (ctx_evt.get("label") if ctx_evt else None)

        headline = _build_headline(
            "anomaly", metric, connector,
            delta, None, obs_f, thr_f
        )
        citation = _build_citation(connector, metric, pull_ids)

        insights.append(
            {
                "rank": rank,
                "type": "anomaly",
                "metric": metric,
                "connector": connector,
                "headline": headline,
                "citation": citation,
                "context_event_id": ctx_id,
                "context_event_label": ctx_label,
                "alert_id": firing_id,
                "pull_ids": pull_ids,
            }
        )
        rank += 1

    # -- Notable deltas --
    for nd in notable_delta_candidates:
        if rank > MAX_INSIGHTS:
            break
        metric = nd["_metric"]
        data = nd["_data"]
        connector = data.get("source_system") or ""
        pull_id = data.get("pull_id")
        pull_ids = [pull_id] if pull_id else []
        value = data.get("value")
        delta = data.get("delta")
        delta_pct = data.get("delta_pct")
        prior = (float(value) - float(delta)) if (value is not None and delta is not None) else None

        try:
            curr_f = float(value) if value is not None else None
            prev_f = float(prior) if prior is not None else None
            delta_f = float(delta) if delta is not None else None
        except (TypeError, ValueError):
            curr_f = None
            prev_f = None
            delta_f = None

        headline = _build_headline(
            "notable_delta", metric, connector,
            delta_f, delta_pct, curr_f, prev_f
        )
        citation = _build_citation(connector, metric, pull_ids)

        insights.append(
            {
                "rank": rank,
                "type": "notable_delta",
                "metric": metric,
                "connector": connector,
                "headline": headline,
                "citation": citation,
                "context_event_id": None,
                "context_event_label": None,
                "alert_id": None,
                "pull_ids": pull_ids,
            }
        )
        rank += 1

    # ------------------------------------------------------------------
    # Step 4: Build the final insights JSONB envelope (AC2 shape).
    # ------------------------------------------------------------------
    build_duration_ms = round((time.monotonic() - t0) * 1000)
    alerts_count = len(business_alert_candidates)
    anomalies_count = len(anomaly_candidates)

    return {
        "version": 1,
        "briefing_date": briefing_date,
        "insights": insights,
        "alerts_count": alerts_count,
        "anomalies_count": anomalies_count,
        "build_duration_ms": build_duration_ms,
    }
