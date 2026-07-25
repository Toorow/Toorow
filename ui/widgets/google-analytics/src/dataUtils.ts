/**
 * Pure data transformation helpers for the daily report envelope (T2.2).
 *
 * All functions are side-effect free so they are trivially unit-testable and
 * safe to call during render. Rows are the fact_daily_kpi list[dict] from the
 * envelope's data.rows.
 */

import type { Row } from "./types";

/**
 * Sum a metric per date over ONE breakdown series only (review-1-6 F-01).
 *
 * fact_daily_kpi carries TWO parallel breakdown series per metric/date
 * (device_category and country) that each total the same day: summing both
 * would double the real value. We pin one series for day totals.
 *
 * @returns Map<"YYYY-MM-DD", total>
 */
const TOTAL_SERIES_DIMENSION = "device_category";

export function aggregateByDate(rows: Row[], metric: string): Map<string, number> {
  const out = new Map<string, number>();
  for (const r of rows) {
    if (r.metric !== metric) continue;
    if (r.breakdown_dimension !== TOTAL_SERIES_DIMENSION) continue;
    out.set(r.date, (out.get(r.date) ?? 0) + (Number(r.value) || 0));
  }
  return out;
}

/**
 * Sum a metric per date for a single connector (small multiples, T5).
 *
 * @returns Map<"YYYY-MM-DD", total> for the given connector+metric.
 */
export function aggregateByDateConnector(
  rows: Row[],
  connector: string,
  metric: string,
): Map<string, number> {
  const out = new Map<string, number>();
  for (const r of rows) {
    if (r.connector !== connector || r.metric !== metric) continue;
    if (r.breakdown_dimension !== TOTAL_SERIES_DIMENSION) continue;  // review-1-6 F-01
    out.set(r.date, (out.get(r.date) ?? 0) + (Number(r.value) || 0));
  }
  return out;
}

export interface DeltaResult {
  current: number;
  previous: number;
  delta: number;
  /** Percentage change vs previous period. null when previous == 0 (undefined). */
  deltaPct: number | null;
}

/**
 * Compare the sum of the most recent half of the range vs the prior half.
 *
 * Splits the [start, end] window in two equal halves by day count and sums the
 * metric across each half. Robust to any window length (not hardcoded to 30/30).
 * deltaPct is null when the previous period total is 0 (avoids /0 → Infinity).
 */
export function computeDeltas(
  rows: Row[],
  metric: string,
  dateRange: { start: string; end: string },
): DeltaResult {
  const byDate = aggregateByDate(rows, metric);

  // Build the ordered list of dates in the range that actually have data.
  const dates = [...byDate.keys()].filter(
    (d) => d >= dateRange.start && d <= dateRange.end,
  );
  dates.sort();

  if (dates.length === 0) {
    return { current: 0, previous: 0, delta: 0, deltaPct: null };
  }

  // Split into two equal halves; the later half is "current".
  const mid = Math.floor(dates.length / 2);
  const previousDates = dates.slice(0, mid);
  const currentDates = dates.slice(mid);

  const sum = (ds: string[]) => ds.reduce((acc, d) => acc + (byDate.get(d) ?? 0), 0);
  const previous = sum(previousDates);
  const current = sum(currentDates);
  const delta = current - previous;
  const deltaPct = previous === 0 ? null : (delta / previous) * 100;

  return { current, previous, delta, deltaPct };
}

/** All rows for one exact date (day-click detail, T6). */
export function getRowsForDate(rows: Row[], date: string): Row[] {
  return rows.filter((r) => r.date === date);
}

// ---------------------------------------------------------------------------
// Story 8.11 (R5) — composite sub-dimension splits ('country>device').
// Composite rows are ordinary long-format rows using a '>' path separator in
// both breakdown_dimension and breakdown_value. No new Row shape.
// ---------------------------------------------------------------------------

/** The '>' path separator used to encode composite breakdowns. */
export const COMPOSITE_SEPARATOR = ">";

/** True when a breakdown dimension is a composite split (contains '>'). */
export function isCompositeDimension(dim: string): boolean {
  return dim.includes(COMPOSITE_SEPARATOR);
}

/**
 * Human label for a breakdown value. Composite values ('FR>mobile') render with
 * spaced separators ('FR > mobile'); single values pass through unchanged.
 */
export function breakdownLabel(breakdownValue: string): string {
  if (!breakdownValue.includes(COMPOSITE_SEPARATOR)) return breakdownValue;
  return breakdownValue
    .split(COMPOSITE_SEPARATOR)
    .map((p) => p.trim())
    .join(" > ");
}

/** Sorted list of distinct breakdown dimensions present for a metric. */
export function getBreakdownDimensions(rows: Row[], metric: string): string[] {
  const set = new Set<string>();
  for (const r of rows) {
    if (r.metric !== metric) continue;
    set.add(r.breakdown_dimension);
  }
  return [...set].sort();
}

/**
 * Sum a metric per breakdown_value for ONE breakdown dimension over the range.
 *
 * Pins a single breakdown_dimension so composite and single-dimension series are
 * never mixed (review-1-6 F-01 / Story 8.11 DESIGN §2: mixing double-counts).
 * @returns Map<breakdown_value, total> sorted-insertion not guaranteed.
 */
export function aggregateByBreakdown(
  rows: Row[],
  metric: string,
  dimension: string,
  dateRange?: { start: string; end: string },
): Map<string, number> {
  const out = new Map<string, number>();
  for (const r of rows) {
    if (r.metric !== metric) continue;
    if (r.breakdown_dimension !== dimension) continue;
    if (dateRange && (r.date < dateRange.start || r.date > dateRange.end)) continue;
    out.set(r.breakdown_value, (out.get(r.breakdown_value) ?? 0) + (Number(r.value) || 0));
  }
  return out;
}

/** Deduplicated, sorted list of connectors present in the dataset. */
export function getConnectors(rows: Row[]): string[] {
  const set = new Set<string>();
  for (const r of rows) set.add(r.connector);
  return [...set].sort();
}

/**
 * Sorted list of all dates present for a metric (shared time axis, T5.2).
 * Restricted to the range if provided.
 */
export function getDateDomain(
  rows: Row[],
  metric: string,
  dateRange?: { start: string; end: string },
): string[] {
  const set = new Set<string>();
  for (const r of rows) {
    if (r.metric !== metric) continue;
    if (dateRange && (r.date < dateRange.start || r.date > dateRange.end)) continue;
    set.add(r.date);
  }
  return [...set].sort();
}
