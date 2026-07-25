"""toorow — token/latency metrics + conversions dedup (Story 1.5; extended 5.1).

Logs structured P1 gate measurement records to stdout, and — from Story 5.1 —
surfaces the same P1 gate data as span attributes when tracing is enabled (AC7).

Story 5.1 (AI-27 decomposition) additionally hosts the conversions dedup rule and
its declarative priority loader, extracted verbatim from ``core.main``:
  - ``apply_conversions_dedup`` (Rule P — priority-source dedup)
  - ``load_metric_source_priorities`` + the ``_METRIC_PRIORITIES`` cache
``main.py`` re-exports ``_apply_conversions_dedup``, ``_load_metric_source_priorities``,
and ``_METRIC_PRIORITIES`` for backward compatibility with existing imports/tests.

# NFR1 P1 gate: target ~500 tokens LLM channel vs multi-MB widget payload.
# NFR2: target ~30s latency.
# AD-2: source-agnostic — no module-specific strings here (priorities are read
#       from the declarative dbt seed CSV, never hardcoded).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def log_tool_metrics(
    tool_name: str,
    summary_text: str,
    payload_bytes: int,
    latency_ms: int,
) -> dict:
    """Log a structured P1 gate metrics record to stdout/trace.

    Token count is estimated from the summary text using a word-count
    heuristic (no LLM API call required — T5.2).

    Parameters
    ----------
    tool_name:
        The MCP tool name (e.g. ``"get_daily_report"``).
    summary_text:
        The plain-text summary sent to the LLM channel.
    payload_bytes:
        Byte size of the JSON-serialized structuredContent envelope.
    latency_ms:
        End-to-end tool latency in milliseconds.

    Returns
    -------
    dict
        The structured record that was logged (useful for testing).
    """
    # NFR1 P1 gate: target ~500 tokens LLM channel vs multi-MB widget payload.
    # NFR2: target ~30s latency.
    summary_lines = len(summary_text.splitlines())

    # T5.2: rough token estimate — word count × 1.3 (no LLM API call needed)
    word_count = len(summary_text.split())
    summary_token_estimate = int(word_count * 1.3)

    record = {
        "event": "tool_metrics",
        "tool": tool_name,
        "summary_lines": summary_lines,
        "summary_token_estimate": summary_token_estimate,
        "payload_bytes": payload_bytes,
        "latency_ms": latency_ms,
    }

    logger.info(json.dumps(record))

    # Story 5.1 (AC7): surface P1 gate data as attributes on the current tool span
    # (created by tracing.TracingMiddleware) without duplicating the measurement
    # logic. Best-effort — a no-op when tracing is disabled and never raises.
    try:
        from core import tracing  # noqa: PLC0415

        tracing.record_current_span_attributes(
            {
                "tool.summary_lines": summary_lines,
                "tool.summary_token_estimate": summary_token_estimate,
                "tool.payload_bytes": payload_bytes,
                "tool.latency_ms": latency_ms,
            }
        )
    except Exception:  # noqa: BLE001 -- tracing must never break metrics logging
        pass

    return record


# ---------------------------------------------------------------------------
# AD-4-conversions: Rule P — priority-source dedup for cross-source conversions.
# Story 3.7 (extracted from main.py in Story 5.1 / AI-27).
#
# When multiple connectors report the same canonical metric ('conversions'), only
# the priority-source row is used in cross-source totals. The dbt semantic view
# (dbt/models/marts/cross_source_conversions.sql) is the canonical definition; this
# function enforces the same rule at runtime in get_daily_report so the LLM summary
# is never inflated. Per-source rows in fact_daily_kpi are NEVER modified (HG-1).
#
# AD-2 compliance: connector names are DECLARATIVE — they live in the seed CSV at
# dbt/seeds/metric_source_priority.csv, not hardcoded here. This Python code is
# generic; it reads whatever priorities the seed declares.
# ---------------------------------------------------------------------------

_METRIC_SOURCE_PRIORITY_CSV = (
    Path(__file__).parent.parent.parent / "dbt" / "seeds" / "metric_source_priority.csv"
)


def load_metric_source_priorities() -> dict[str, list[str]]:
    """Load metric→connector priority lists from the declarative seed CSV.

    Returns a dict mapping metric name to ordered list of connectors (priority-1 first).
    The CSV lives at dbt/seeds/metric_source_priority.csv and is the single source
    of truth for dedup priority — no connector names are hardcoded in this module.

    Falls back to an empty dict if the CSV is absent (graceful degradation:
    all connectors treated as equal priority, first-seen wins).
    """
    import csv  # noqa: PLC0415

    result: dict[str, list[str]] = {}
    if not _METRIC_SOURCE_PRIORITY_CSV.exists():
        logger.warning(
            "metric_source_priority.csv not found at %s — dedup priority unavailable",
            _METRIC_SOURCE_PRIORITY_CSV,
        )
        return result

    with _METRIC_SOURCE_PRIORITY_CSV.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        rows_by_metric: dict[str, list[tuple[int, str]]] = {}
        for csv_row in reader:
            metric = csv_row.get("metric", "").strip()
            connector = csv_row.get("connector", "").strip()
            try:
                priority = int(csv_row.get("priority", "99"))
            except ValueError:
                priority = 99
            if metric and connector:
                rows_by_metric.setdefault(metric, []).append((priority, connector))

        for metric, entries in rows_by_metric.items():
            entries.sort(key=lambda t: t[0])
            result[metric] = [c for _, c in entries]

    return result


# Loaded once at import time; the seed CSV is the authoritative source.
_METRIC_PRIORITIES: dict[str, list[str]] = load_metric_source_priorities()


def apply_conversions_dedup(rows: list[dict]) -> list[dict]:
    """Apply Rule P dedup to conversions rows for cross-source totals.

    Keeps ALL conversions rows of the highest-priority connector for each
    (project_id, date) key (every breakdown row -- review-3-7 F-2). All
    non-conversions rows pass through unchanged.
    Per-source data is never deleted from fact_daily_kpi (HG-1); this function
    only filters which rows enter the cross-source aggregation in get_daily_report.

    Priority order is loaded from dbt/seeds/metric_source_priority.csv (AD-2:
    no connector names hardcoded in this module — declarative config in dbt seed).
    """
    conv_rows = [r for r in rows if r.get("metric") == "conversions"]
    other_rows = [r for r in rows if r.get("metric") != "conversions"]

    if not conv_rows:
        return rows

    priority_list = _METRIC_PRIORITIES.get("conversions", [])

    def _rank(connector: str) -> int:
        try:
            return priority_list.index(connector)
        except ValueError:
            return 99

    # review-3-7 F-1/F-2: two-pass -- find the winning CONNECTOR per
    # (project_id, date), then keep ALL of that connector's breakdown rows
    # (a single retained row under-counted multi-breakdown days by ~75%).
    winner: dict[tuple, str] = {}
    for row in conv_rows:
        key = (row.get("project_id"), row.get("date"))
        connector = row.get("connector", "")
        if key not in winner or _rank(connector) < _rank(winner[key]):
            winner[key] = connector

    kept = [
        row
        for row in conv_rows
        if winner.get((row.get("project_id"), row.get("date"))) == row.get("connector", "")
    ]
    return other_rows + kept
