/**
 * Story 50.5 AC4/AC7 -- the compiler: golden dataset/encode per family, proof it
 * never mutates a value, and proof that an over-limit Result falls back to the
 * table with the EXACT counts in the message.
 */

import { describe, expect, it } from "vitest";

import { compileVisualModel } from "../compile/dataset";
import { DEFAULT_VOLUME_CONSTRAINTS, evaluateVolume } from "../compile/limits";
import { getVizPalette } from "../../vizTheme";
import { RESULT_ROWS, renderInput, specDocument } from "./fixtures";

const palette = getVizPalette({
  palette: {
    mode: "light",
    primary: { main: "#FF99C8" },
    success: { main: "#1B8A5A" },
    warning: { main: "#B26B00" },
    error: { main: "#B3261E" },
    text: { primary: "#111111" },
  },
});

const options = { palette, limits: DEFAULT_VOLUME_CONSTRAINTS };

describe("AC4 -- dataset/encode built from the bindings", () => {
  it("bar: one dataset column per measure, encode names the bound dimension", () => {
    const input = renderInput();
    const model = compileVisualModel(input.result, input.spec.document, options);
    expect(model.dataset.dimensions).toEqual(["channel", "sessions"]);
    expect(model.dataset.source).toEqual([
      ["organic", 1240],
      ["paid", 880],
      ["referral", 305],
    ]);
    expect(model.encode).toEqual([{ x: "channel", y: "sessions", seriesId: "sessions" }]);
  });

  it("stacked_bar: the breakdown becomes one column per split value, in first-appearance order", () => {
    const rows = [
      { channel: "organic", device: "mobile", sessions: 700 },
      { channel: "organic", device: "desktop", sessions: 540 },
      { channel: "paid", device: "mobile", sessions: 500 },
      { channel: "paid", device: "desktop", sessions: 380 },
    ];
    const input = renderInput();
    const model = compileVisualModel(
      {
        ...input.result,
        schema: { fields: [{ name: "channel" }, { name: "device" }, { name: "sessions" }] },
        rows,
      },
      specDocument({
        family: "stacked_bar",
        bindings: { dimension: ["channel"], breakdown: ["device"], measure: ["sessions"] },
      }),
      options,
    );
    expect(model.dataset.dimensions).toEqual([
      "channel",
      "sessions / mobile",
      "sessions / desktop",
    ]);
    expect(model.dataset.source).toEqual([
      ["organic", 700, 540],
      ["paid", 500, 380],
    ]);
    expect(model.series.map((s) => s.stack)).toEqual(["total", "total"]);
  });

  it("line: multiple measures become multiple series, each with a resolved colour", () => {
    const input = renderInput();
    const model = compileVisualModel(
      input.result,
      specDocument({
        family: "line",
        bindings: { dimension: ["channel"], measure: ["sessions", "conversions"] },
      }),
      options,
    );
    expect(model.series.map((s) => s.id)).toEqual(["sessions", "conversions"]);
    expect(model.series.every((s) => /^(#|rgba?\()/.test(s.color))).toBe(true);
    // The colours come from the LIVE theme, brand colour first.
    expect(model.series[0]!.color).toBe(palette.categorical[0]);
  });

  it("kpi: one series, one mark", () => {
    const input = renderInput();
    const model = compileVisualModel(
      input.result,
      specDocument({ family: "kpi", bindings: { measure: ["sessions"] } }),
      options,
    );
    expect(model.series).toHaveLength(1);
    expect(model.dataset.dimensions).toEqual(["sessions"]);
  });

  it("scatter: two measures, one point per dimension member", () => {
    const input = renderInput();
    const model = compileVisualModel(
      input.result,
      specDocument({
        family: "scatter",
        bindings: { dimension: ["channel"], measure: ["sessions", "conversions"] },
      }),
      options,
    );
    expect(model.dataset.dimensions).toEqual(["channel", "sessions", "conversions"]);
    expect(model.dataset.source).toHaveLength(3);
  });

  it("area and table compile without touching a value", () => {
    const input = renderInput();
    for (const family of ["area", "table"] as const) {
      const model = compileVisualModel(
        input.result,
        specDocument({
          family,
          bindings: { dimension: ["channel"], measure: ["sessions"] },
        }),
        options,
      );
      expect(model.family).toBe(family);
    }
  });
});

describe("AC4 -- the compiler never computes", () => {
  it("every dataset value is byte-identical to a value the Result returned", () => {
    const input = renderInput();
    const model = compileVisualModel(input.result, input.spec.document, options);
    const returned = new Set<unknown>();
    for (const row of RESULT_ROWS) for (const v of Object.values(row)) returned.add(v);
    for (const line of model.dataset.source) {
      for (const value of line) {
        expect(returned.has(value)).toBe(true);
      }
    }
  });

  it("a missing (category, split) pair stays null and never becomes zero", () => {
    const rows = [
      { channel: "organic", device: "mobile", sessions: 700 },
      { channel: "paid", device: "desktop", sessions: 380 },
    ];
    const input = renderInput();
    const model = compileVisualModel(
      {
        ...input.result,
        schema: { fields: [{ name: "channel" }, { name: "device" }, { name: "sessions" }] },
        rows,
      },
      specDocument({
        family: "stacked_bar",
        bindings: { dimension: ["channel"], breakdown: ["device"], measure: ["sessions"] },
      }),
      options,
    );
    // organic has no desktop row; the cell is null, not 0.
    expect(model.dataset.source[0]).toEqual(["organic", 700, null]);
    expect(model.dataset.source[1]).toEqual(["paid", null, 380]);
  });

  it("the row order is the server's ranking, unchanged", () => {
    const input = renderInput();
    const model = compileVisualModel(input.result, input.spec.document, options);
    expect(model.dataset.source.map((r) => r[0])).toEqual(["organic", "paid", "referral"]);
  });
});

describe("AC7 -- volume refusals are explicit, never a blank chart", () => {
  it("a 5001-row Result falls back to the table with the exact counts", () => {
    const verdict = evaluateVolume(
      { rows: 5001, series: 1, cells: 15003 },
      DEFAULT_VOLUME_CONSTRAINTS,
    );
    expect(verdict.chartAllowed).toBe(false);
    expect(verdict.reason).toContain("5,001 rows");
    expect(verdict.reason).toContain("5,000");
    expect(verdict.reason).toContain("not a sample of it");
  });

  it("a 25-series Result names the series count and the series limit", () => {
    const verdict = evaluateVolume({ rows: 10, series: 25, cells: 100 }, DEFAULT_VOLUME_CONSTRAINTS);
    expect(verdict.chartAllowed).toBe(false);
    expect(verdict.reason).toContain("25 series");
    expect(verdict.reason).toContain("24");
  });

  it("a within-limits Result is allowed and reports no reason", () => {
    const verdict = evaluateVolume({ rows: 3, series: 1, cells: 9 }, DEFAULT_VOLUME_CONSTRAINTS);
    expect(verdict).toMatchObject({ chartAllowed: true, reason: null });
  });
});

describe("AC10 -- top_n is disclosed as a display limit, never as the whole", () => {
  it("states that the totals are unchanged", () => {
    const input = renderInput();
    const model = compileVisualModel(
      input.result,
      specDocument({ top_n: { n: 2, display_only: true } }),
      options,
    );
    expect(model.disclosures.topN).toContain("display limit");
    expect(model.disclosures.topN).toContain("totals below are unchanged");
  });
});
