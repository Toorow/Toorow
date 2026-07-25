/**
 * Unit tests for Story 8.11 composite-split helpers in dataUtils.
 *
 * Locks in:
 *  - isCompositeDimension / breakdownLabel parsing ('FR>mobile' → 'FR > mobile')
 *  - getBreakdownDimensions surfaces composite dims
 *  - aggregateByBreakdown pins ONE dimension so composite and single series are
 *    never mixed (no double count — DESIGN §2): composite total == single-dim total.
 */

import { describe, it, expect } from "vitest";
import {
  isCompositeDimension,
  breakdownLabel,
  getBreakdownDimensions,
  aggregateByBreakdown,
} from "../dataUtils";
import type { Row } from "../types";

function row(dim: string, val: string, value: number, metric = "sessions"): Row {
  return {
    date: "2026-04-11",
    connector: "google-analytics",
    metric,
    breakdown_dimension: dim,
    breakdown_value: val,
    value,
    pull_id: "pull_c1",
    loaded_at: "2026-04-11T00:00:00Z",
  };
}

describe("isCompositeDimension", () => {
  it("detects the '>' separator", () => {
    expect(isCompositeDimension("country>device")).toBe(true);
    expect(isCompositeDimension("country")).toBe(false);
    expect(isCompositeDimension("device_category")).toBe(false);
  });
});

describe("breakdownLabel", () => {
  it("renders composite values with spaced separators", () => {
    expect(breakdownLabel("FR>mobile")).toBe("FR > mobile");
  });
  it("passes single values through unchanged", () => {
    expect(breakdownLabel("FR")).toBe("FR");
    expect(breakdownLabel("desktop")).toBe("desktop");
  });
});

describe("getBreakdownDimensions", () => {
  it("returns sorted distinct dimensions incl. composite for a metric", () => {
    const rows = [
      row("country", "FR", 10),
      row("device_category", "mobile", 10),
      row("country>device", "FR>mobile", 5),
      row("country", "DE", 5, "conversions"), // different metric — excluded
    ];
    expect(getBreakdownDimensions(rows, "sessions")).toEqual([
      "country",
      "country>device",
      "device_category",
    ]);
  });
});

describe("aggregateByBreakdown (no double count)", () => {
  const rows = [
    // country series: FR=60, DE=40  → day total 100
    row("country", "FR", 60),
    row("country", "DE", 40),
    // composite series (country>device): sums to the SAME day total 100
    row("country>device", "FR>desktop", 35),
    row("country>device", "FR>mobile", 25),
    row("country>device", "DE>desktop", 25),
    row("country>device", "DE>mobile", 15),
  ];

  it("sums a single dimension without mixing in the composite series", () => {
    const country = aggregateByBreakdown(rows, "sessions", "country");
    expect(country.get("FR")).toBe(60);
    expect(country.get("DE")).toBe(40);
    // total counted exactly once
    expect([...country.values()].reduce((a, b) => a + b, 0)).toBe(100);
  });

  it("composite series totals equal the single-dim day total (reconciliation)", () => {
    const composite = aggregateByBreakdown(rows, "sessions", "country>device");
    expect(composite.get("FR>desktop")).toBe(35);
    expect([...composite.values()].reduce((a, b) => a + b, 0)).toBe(100);
  });
});
