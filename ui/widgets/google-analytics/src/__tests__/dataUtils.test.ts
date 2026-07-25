/**
 * Unit tests for dataUtils.ts (T10.1).
 *
 * NOTE: the Vitest runner is wired in Story 1.7 (this repo has no UI test runner
 * yet). These specs use the Vitest `describe/it/expect` globals so they run
 * as-is once 1.7 lands `vitest` + tsconfig `types: ["vitest/globals"]`. Until
 * then they document the expected behaviour and are covered by the standalone
 * Node smoke check (scripts/smoke.mjs) for the build path.
 */

import { describe, it, expect } from "vitest";
import {
  aggregateByDate,
  aggregateByDateConnector,
  computeDeltas,
  getRowsForDate,
  getConnectors,
} from "../dataUtils";
import type { Row } from "../types";

/** Build a 90-day fixture: 3 metrics × 3 devices × 3 countries = 810 rows/day-metric mix. */
function buildRows(): Row[] {
  const rows: Row[] = [];
  const metrics = ["sessions", "active_users", "conversions"];
  const devices = ["desktop", "mobile", "tablet"];
  const countries = ["France", "Germany", "Spain"];
  const start = new Date("2026-01-01T00:00:00Z");
  for (let d = 0; d < 90; d++) {
    const iso = new Date(start.getTime() + d * 86400000).toISOString().slice(0, 10);
    for (const metric of metrics) {
      for (const device of devices) {
        rows.push({
          date: iso,
          connector: "google-analytics",
          metric,
          breakdown_dimension: "device_category",
          breakdown_value: device,
          value: 1,
          pull_id: "pull_x",
          loaded_at: "2026-07-10T00:00:00Z",
        });
      }
      for (const country of countries) {
        rows.push({
          date: iso,
          connector: "google-analytics",
          metric,
          breakdown_dimension: "country",
          breakdown_value: country,
          value: 1,
          pull_id: "pull_x",
          loaded_at: "2026-07-10T00:00:00Z",
        });
      }
    }
  }
  return rows;
}

describe("aggregateByDate", () => {
  it("sums ONE breakdown series per date — no double-counting (review-1-6 F-01)", () => {
    const rows = buildRows();
    const byDate = aggregateByDate(rows, "sessions");
    // Only the device_category series counts: 3 devices × value 1 → 3.
    // Summing both parallel series (devices + countries) would yield 6 = 2× reality.
    expect(byDate.size).toBe(90);
    for (const total of byDate.values()) expect(total).toBe(3);
  });

  it("ignores other metrics", () => {
    const rows = buildRows();
    const byDate = aggregateByDate(rows, "conversions");
    expect([...byDate.values()].every((v) => v === 3)).toBe(true);
  });
});

describe("aggregateByDateConnector", () => {
  it("filters by connector and metric", () => {
    const rows = buildRows();
    const m = aggregateByDateConnector(rows, "google-analytics", "sessions");
    expect(m.size).toBe(90);
    const empty = aggregateByDateConnector(rows, "meta-ads", "sessions");
    expect(empty.size).toBe(0);
  });
});

describe("computeDeltas", () => {
  it("computes delta and deltaPct between the two range halves", () => {
    const rows: Row[] = [];
    // 4 days: first half total 10 (5+5), second half total 20 (10+10) → +100%.
    const perDay = [5, 5, 10, 10];
    perDay.forEach((v, i) => {
      rows.push({
        date: `2026-01-0${i + 1}`,
        connector: "google-analytics",
        metric: "sessions",
        breakdown_dimension: "device_category",
        breakdown_value: "desktop",
        value: v,
        pull_id: "p",
        loaded_at: "t",
      });
    });
    const r = computeDeltas(rows, "sessions", { start: "2026-01-01", end: "2026-01-04" });
    expect(r.previous).toBe(10);
    expect(r.current).toBe(20);
    expect(r.delta).toBe(10);
    expect(r.deltaPct).toBe(100);
  });

  it("returns null deltaPct when the previous period is 0 (no /0)", () => {
    const rows: Row[] = [
      { date: "2026-01-01", connector: "c", metric: "sessions", breakdown_dimension: "d", breakdown_value: "v", value: 0, pull_id: "p", loaded_at: "t" },
      { date: "2026-01-02", connector: "c", metric: "sessions", breakdown_dimension: "d", breakdown_value: "v", value: 50, pull_id: "p", loaded_at: "t" },
    ];
    const r = computeDeltas(rows, "sessions", { start: "2026-01-01", end: "2026-01-02" });
    expect(r.previous).toBe(0);
    expect(r.deltaPct).toBeNull();
  });

  it("handles an empty dataset", () => {
    const r = computeDeltas([], "sessions", { start: "2026-01-01", end: "2026-01-02" });
    expect(r).toEqual({ current: 0, previous: 0, delta: 0, deltaPct: null });
  });
});

describe("getRowsForDate", () => {
  it("returns only rows for the exact date", () => {
    const rows = buildRows();
    const day = getRowsForDate(rows, "2026-01-01");
    // 3 metrics × (3 devices + 3 countries) = 18 rows.
    expect(day.length).toBe(18);
    expect(day.every((r) => r.date === "2026-01-01")).toBe(true);
  });
});

describe("getConnectors", () => {
  it("dedupes and sorts", () => {
    const rows: Row[] = [
      { date: "2026-01-01", connector: "b", metric: "sessions", breakdown_dimension: "d", breakdown_value: "v", value: 1, pull_id: "p", loaded_at: "t" },
      { date: "2026-01-01", connector: "a", metric: "sessions", breakdown_dimension: "d", breakdown_value: "v", value: 1, pull_id: "p", loaded_at: "t" },
      { date: "2026-01-01", connector: "a", metric: "sessions", breakdown_dimension: "d", breakdown_value: "v", value: 1, pull_id: "p", loaded_at: "t" },
    ];
    expect(getConnectors(rows)).toEqual(["a", "b"]);
  });
});
