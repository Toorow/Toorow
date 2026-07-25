/**
 * Pure aggregation utilities for View Tools (Story 1.7, T5).
 *
 * No React, no side-effects — pure functions that can be called on every
 * render without risk (and trivially unit-tested with Vitest).
 *
 * AD-4: only additive metrics are stored as plain values; ratios and
 * non-additive source metrics carry a declared aggregation rule and the
 * semantic layer is the only place that applies it — they are never summed
 * or re-averaged downstream.
 */

import type { Row } from "../types";

/** The three granularity levels available in the View Tools toggle. */
export type Granularity = "day" | "week" | "month";

/** The ADDITIVE metrics present in P0/P1 data. Non-additive metrics throw. */
const ADDITIVE_METRICS = new Set(["sessions", "active_users", "conversions"]);

/**
 * AD-4: only additive metrics are stored; summing day-grain rows is correct
 * for these metrics. Non-additive metrics (e.g. average_position) would
 * require a declared aggregation rule from the semantic layer — do not sum
 * them here.
 */
function assertAdditive(metric: string): void {
  if (!ADDITIVE_METRICS.has(metric)) {
    throw new Error(
      `AD-4 violation: metric "${metric}" is not in the additive set ` +
        `(${[...ADDITIVE_METRICS].join(", ")}). ` +
        "Non-additive metrics must not be summed across day-grain rows — " +
        "they require a semantic-layer aggregation rule.",
    );
  }
}

// ---------------------------------------------------------------------------
// ISO week helpers (no external dep — keeps bundle small).
// ---------------------------------------------------------------------------

/**
 * Returns the ISO week number (1–53) for a UTC date string "YYYY-MM-DD".
 * ISO weeks start on Monday.
 */
export function isoWeekNumber(date: Date): number {
  // Algorithm: shift to Thursday of the same ISO week, then count weeks from
  // Jan 1 of that Thursday's year.
  const d = new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate()));
  // Thursday in current week (ISO 8601: week 1 contains the year's first Thursday)
  d.setUTCDate(d.getUTCDate() + 4 - (d.getUTCDay() || 7));
  const yearStart = new Date(Date.UTC(d.getUTCFullYear(), 0, 1));
  return Math.ceil(((d.getTime() - yearStart.getTime()) / 86400000 + 1) / 7);
}

/**
 * Returns the Monday (start of the ISO week) for a given UTC date, as
 * "YYYY-MM-DD".
 */
export function isoWeekStart(date: Date): string {
  const d = new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate()));
  const dow = d.getUTCDay() || 7; // Monday=1 … Sunday=7
  d.setUTCDate(d.getUTCDate() - dow + 1);
  return d.toISOString().slice(0, 10);
}

/**
 * Calendar month start ("YYYY-MM-01") for a date string, in Europe/Paris
 * timezone (consistent with AD-6 project default).
 *
 * For the purpose of month-boundary splitting we use simple UTC month
 * extraction plus fixed-offset correction (+1h/+2h for CET/CEST). Since
 * we receive date strings already at day grain (not timestamps) from the
 * warehouse, the Paris calendar date matches the ISO date in the rows except
 * during the ambiguous hour around midnight — a P0/P1 non-issue.
 * We therefore use the date string directly as the month boundary key.
 */
export function monthStart(dateStr: string): string {
  return dateStr.slice(0, 7) + "-01"; // "YYYY-MM-01"
}

/**
 * French period_label for week/month views.
 *
 * Week:  "Sem. 28"
 * Month: "Juil. 2026"
 */
const MONTHS_FR_SHORT = [
  "Janv.", "Févr.", "Mars", "Avr.", "Mai", "Juin",
  "Juil.", "Août", "Sept.", "Oct.", "Nov.", "Déc.",
];

export function periodLabel(periodStart: string, granularity: "week" | "month"): string {
  const d = new Date(periodStart + "T00:00:00Z");
  if (granularity === "week") {
    return `Sem. ${isoWeekNumber(d)}`;
  }
  // month
  return `${MONTHS_FR_SHORT[d.getUTCMonth()]} ${d.getUTCFullYear()}`;
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * T5.2 — Filter rows to only those whose connector field is in the given list.
 */
export function filterRows(rows: Row[], connectors: string[]): Row[] {
  const set = new Set(connectors);
  return rows.filter((r) => set.has(r.connector));
}

/**
 * AggregatedRow — an output row from aggregateRows().
 * For day granularity it mirrors the input Row exactly (passthrough).
 * For week/month it carries the period_label and the period's start date.
 */
export interface AggregatedRow {
  date: string; // period start date (ISO-8601)
  period_label: string; // e.g. "Sem. 28" or "Juil. 2026" (French)
  connector: string;
  metric: string;
  breakdown_dimension: string;
  breakdown_value: string;
  value: number;
  /** Original pull_id(s) — first seen in the period (informational only). */
  pull_id: string;
  loaded_at: string;
}

/**
 * T5.3 — Aggregate day-grain rows to the requested granularity.
 *
 * For "day": returns the original rows repackaged as AggregatedRow (period_label = date).
 * For "week"/"month": groups by (period_start, connector, metric, breakdown_dimension,
 * breakdown_value) and SUMS value across the window (AD-4: additive metrics only).
 *
 * T5.4 — Non-additive guard: throws a descriptive error if any row's metric is
 * not in the additive set.
 */
export function aggregateRows(rows: Row[], granularity: Granularity): AggregatedRow[] {
  if (rows.length === 0) return [];

  // Validate ALL metrics upfront to fail-fast rather than silently mis-aggregate.
  const metricsPresent = new Set(rows.map((r) => r.metric));
  for (const m of metricsPresent) {
    assertAdditive(m);
  }

  if (granularity === "day") {
    // Passthrough: wrap each Row as AggregatedRow.
    return rows.map((r) => ({
      date: r.date,
      period_label: r.date,
      connector: r.connector,
      metric: r.metric,
      breakdown_dimension: r.breakdown_dimension,
      breakdown_value: r.breakdown_value,
      value: r.value,
      pull_id: r.pull_id,
      loaded_at: r.loaded_at,
    }));
  }

  // Build a composite key map: periodStart|connector|metric|bdDim|bdVal → AggregatedRow
  const key = (r: Row, period: string): string =>
    `${period}|${r.connector}|${r.metric}|${r.breakdown_dimension}|${r.breakdown_value}`;

  const acc = new Map<string, AggregatedRow>();

  for (const r of rows) {
    const d = new Date(r.date + "T00:00:00Z");
    const periodStart =
      granularity === "week" ? isoWeekStart(d) : monthStart(r.date);
    const k = key(r, periodStart);

    const existing = acc.get(k);
    if (existing) {
      existing.value += r.value;
    } else {
      acc.set(k, {
        date: periodStart,
        period_label: periodLabel(periodStart, granularity),
        connector: r.connector,
        metric: r.metric,
        breakdown_dimension: r.breakdown_dimension,
        breakdown_value: r.breakdown_value,
        value: r.value,
        pull_id: r.pull_id,
        loaded_at: r.loaded_at,
      });
    }
  }

  return [...acc.values()].sort((a, b) => a.date.localeCompare(b.date));
}
