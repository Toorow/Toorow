/**
 * The read-time division, once, and exactly.
 *
 * The defect this closes was visible on a screenshot: a pivot cell printing
 * `124000000` for 124 EUR. Canonical money is micros (`core/money.py`), so the
 * bug was not in the number — it was in the reading of it.
 */

import { describe, expect, it } from "vitest";
import { formatGovernedValue, microsToUnits } from "../theme/canonicalAmount";

/** Non-breaking, written as an escape so a diff cannot hide it. */
const NBSP = " ";
const EUR = { value_type: "money", unit: "EUR" };

describe("the canonical micros read", () => {
  it("states an amount in its currency instead of its storage integer", () => {
    expect(formatGovernedValue(124_000_000, EUR)).toBe(`124.00${NBSP}EUR`);
  });

  it("keeps a sub-cent amount exactly, because micros carry six digits", () => {
    expect(microsToUnits(1)).toBe("0.000001");
    expect(microsToUnits(-2_500_000)).toBe("-2.50");
  });

  it("groups thousands so a large amount can be read at a glance", () => {
    expect(formatGovernedValue(12_345_678_000_000, EUR)).toBe(`12,345,678.00${NBSP}EUR`);
  });

  it("never invents a currency for a value the vocabulary did not type", () => {
    // An older Result, frozen before the plan carried units, reads exactly as it
    // always did: a plain number, and no currency appears from nowhere.
    expect(formatGovernedValue(124_000_000, undefined)).toBe("124,000,000");
    expect(formatGovernedValue(124_000_000, { value_type: "integer" })).toBe("124,000,000");
  });

  it("prints money with no declared currency as an amount, not as micros", () => {
    expect(formatGovernedValue(124_000_000, { value_type: "money" })).toBe("124.00");
  });

  it("returns null for an absence so the caller keeps its own words for it", () => {
    // "no data" and "0" are different business statements; this module refuses
    // to pick either.
    expect(formatGovernedValue(null, EUR)).toBeNull();
    expect(formatGovernedValue(undefined, EUR)).toBeNull();
  });

  it("shows the declared unit of a non-money measure", () => {
    expect(formatGovernedValue(3.5, { value_type: "decimal", unit: "%" })).toBe(`3.5${NBSP}%`);
  });
});
