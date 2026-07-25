/**
 * Unit tests for aggregation.ts (Story 1.7, T5.7).
 *
 * Covers:
 *  - filterRows with 3 connectors, filter to 1
 *  - aggregateRows("week"): 7 consecutive rows → 1 week row with sum
 *  - aggregateRows("month"): 31 rows across two months → 2 rows with sums
 *  - aggregateRows("day"): passthrough — same rows, same values
 *  - Non-additive metric guard: throws for "average_position"
 */

import { describe, it, expect } from "vitest";
import {
  filterRows,
  aggregateRows,
  isoWeekStart,
  isoWeekNumber,
  monthStart,
} from "../aggregation";
import type { Row } from "../../types";

function makeRow(
  date: string,
  connector: string,
  metric: string,
  value: number,
): Row {
  return {
    date,
    connector,
    metric,
    breakdown_dimension: "device_category",
    breakdown_value: "desktop",
    value,
    pull_id: "pull_test",
    loaded_at: "2026-07-10T00:00:00Z",
  };
}

// ---------------------------------------------------------------------------
// filterRows
// ---------------------------------------------------------------------------

describe("filterRows", () => {
  it("returns only rows for the selected connectors", () => {
    const rows: Row[] = [
      makeRow("2026-01-01", "google-analytics", "sessions", 100),
      makeRow("2026-01-01", "meta-ads", "sessions", 50),
      makeRow("2026-01-01", "google-search-console", "sessions", 30),
    ];
    const result = filterRows(rows, ["google-analytics"]);
    expect(result).toHaveLength(1);
    expect(result[0].connector).toBe("google-analytics");
  });

  it("returns all rows when all connectors are selected", () => {
    const rows: Row[] = [
      makeRow("2026-01-01", "google-analytics", "sessions", 100),
      makeRow("2026-01-01", "meta-ads", "sessions", 50),
      makeRow("2026-01-01", "google-search-console", "sessions", 30),
    ];
    const result = filterRows(rows, ["google-analytics", "meta-ads", "google-search-console"]);
    expect(result).toHaveLength(3);
  });

  it("returns empty array when no connectors match", () => {
    const rows: Row[] = [makeRow("2026-01-01", "google-analytics", "sessions", 100)];
    expect(filterRows(rows, ["meta-ads"])).toHaveLength(0);
  });

  it("returns empty array for empty input", () => {
    expect(filterRows([], ["google-analytics"])).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// aggregateRows("day") — passthrough
// ---------------------------------------------------------------------------

describe("aggregateRows('day')", () => {
  it("passes through rows unchanged (same count, same values)", () => {
    const rows: Row[] = [
      makeRow("2026-06-30", "google-analytics", "sessions", 100),
      makeRow("2026-07-01", "google-analytics", "sessions", 200),
      makeRow("2026-07-01", "google-analytics", "active_users", 150),
    ];
    const result = aggregateRows(rows, "day");
    expect(result).toHaveLength(3);
    // Values are preserved
    const sessions = result.filter((r) => r.metric === "sessions");
    expect(sessions.find((r) => r.date === "2026-06-30")?.value).toBe(100);
    expect(sessions.find((r) => r.date === "2026-07-01")?.value).toBe(200);
    // period_label equals the date for day granularity
    for (const r of result) {
      expect(r.period_label).toBe(r.date);
    }
  });

  it("returns empty array for empty input", () => {
    expect(aggregateRows([], "day")).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// aggregateRows("week")
// ---------------------------------------------------------------------------

describe("aggregateRows('week')", () => {
  it("aggregates 7 consecutive day-grain rows into 1 week row with the sum", () => {
    // ISO week 28, 2026: Mon 2026-07-06 … Sun 2026-07-12
    const values = [10, 20, 30, 40, 50, 60, 70]; // sum = 280
    const rows: Row[] = values.map((v, i) => {
      const d = new Date(Date.UTC(2026, 6, 6 + i)); // 2026-07-06 … 2026-07-12
      return makeRow(d.toISOString().slice(0, 10), "google-analytics", "sessions", v);
    });

    const result = aggregateRows(rows, "week");
    expect(result).toHaveLength(1);
    expect(result[0].value).toBe(280);
    // Week start = Monday 2026-07-06
    expect(result[0].date).toBe("2026-07-06");
    // Period label references week number 28
    expect(result[0].period_label).toMatch(/Sem\. 28/);
  });

  it("separates rows from different ISO weeks", () => {
    // Week 27: 2026-06-29 (Mon) … 2026-07-05 (Sun)
    // Week 28: 2026-07-06 (Mon) …
    const rows: Row[] = [
      makeRow("2026-07-05", "google-analytics", "sessions", 100), // week 27
      makeRow("2026-07-06", "google-analytics", "sessions", 200), // week 28
    ];
    const result = aggregateRows(rows, "week");
    expect(result).toHaveLength(2);
    const w27 = result.find((r) => r.date === "2026-06-29");
    const w28 = result.find((r) => r.date === "2026-07-06");
    expect(w27?.value).toBe(100);
    expect(w28?.value).toBe(200);
  });
});

// ---------------------------------------------------------------------------
// aggregateRows("month")
// ---------------------------------------------------------------------------

describe("aggregateRows('month')", () => {
  it("aggregates 31 day rows across two calendar months → 2 rows with correct sums", () => {
    // January 2026: 31 days (value=1 each), February 2026: 28 days (value=2 each)
    // But we only pass 10 days from each month for test brevity, adjusted sums.
    const janRows: Row[] = [];
    const febRows: Row[] = [];

    for (let d = 1; d <= 20; d++) {
      janRows.push(makeRow(`2026-01-${String(d).padStart(2, "0")}`, "ga", "sessions", 1));
    }
    for (let d = 1; d <= 11; d++) {
      febRows.push(makeRow(`2026-02-${String(d).padStart(2, "0")}`, "ga", "sessions", 2));
    }

    const result = aggregateRows([...janRows, ...febRows], "month");
    expect(result).toHaveLength(2);

    const jan = result.find((r) => r.date === "2026-01-01");
    const feb = result.find((r) => r.date === "2026-02-01");

    expect(jan?.value).toBe(20); // 20 days × value=1
    expect(feb?.value).toBe(22); // 11 days × value=2

    expect(jan?.period_label).toMatch(/Janv\. 2026/);
    expect(feb?.period_label).toMatch(/Févr\. 2026/);
  });

  it("handles a full 31-day January with correct sum", () => {
    const rows: Row[] = [];
    for (let d = 1; d <= 31; d++) {
      rows.push(makeRow(`2026-01-${String(d).padStart(2, "0")}`, "ga", "sessions", 10));
    }
    const result = aggregateRows(rows, "month");
    expect(result).toHaveLength(1);
    expect(result[0].value).toBe(310); // 31 × 10
  });
});

// ---------------------------------------------------------------------------
// Non-additive metric guard (AD-4)
// ---------------------------------------------------------------------------

describe("aggregateRows — non-additive guard (AD-4)", () => {
  it("throws a descriptive error when a non-additive metric is encountered", () => {
    const rows: Row[] = [makeRow("2026-01-01", "ga", "average_position", 3.5)];
    expect(() => aggregateRows(rows, "week")).toThrowError(/average_position/);
    expect(() => aggregateRows(rows, "week")).toThrowError(/AD-4/);
  });

  it("throws for 'day' granularity too (guard runs before passthrough)", () => {
    const rows: Row[] = [makeRow("2026-01-01", "ga", "average_position", 3.5)];
    expect(() => aggregateRows(rows, "day")).toThrowError(/average_position/);
  });
});

// ---------------------------------------------------------------------------
// isoWeekStart / isoWeekNumber helpers
// ---------------------------------------------------------------------------

describe("isoWeekStart", () => {
  it("returns Monday for a Wednesday date", () => {
    const d = new Date(Date.UTC(2026, 6, 8)); // Wed 2026-07-08
    expect(isoWeekStart(d)).toBe("2026-07-06"); // Mon
  });

  it("returns the same date for Monday", () => {
    const d = new Date(Date.UTC(2026, 6, 6)); // Mon 2026-07-06
    expect(isoWeekStart(d)).toBe("2026-07-06");
  });

  it("returns the previous Monday for Sunday", () => {
    const d = new Date(Date.UTC(2026, 6, 12)); // Sun 2026-07-12
    expect(isoWeekStart(d)).toBe("2026-07-06");
  });
});

describe("isoWeekNumber", () => {
  it("returns 28 for 2026-07-06 (week 28)", () => {
    const d = new Date(Date.UTC(2026, 6, 6));
    expect(isoWeekNumber(d)).toBe(28);
  });
});

describe("monthStart", () => {
  it("returns YYYY-MM-01", () => {
    expect(monthStart("2026-07-15")).toBe("2026-07-01");
    expect(monthStart("2026-01-01")).toBe("2026-01-01");
    expect(monthStart("2026-12-31")).toBe("2026-12-01");
  });
});
