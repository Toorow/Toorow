/**
 * Story 76-8 — `card-conversions` on 2026-09-05: « 30EUR » inside the gauge,
 * the code welded to the digits, printed over « No target set », itself printed
 * over the block title repeated a second time under the arc.
 */

import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import App from "../App";
import { FIXTURE_ENVELOPE } from "../fixture";

describe("card-conversions — an amount is never bare, never welded (76-8)", () => {
  it("separates the currency from the digits inside the gauge", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    const gaugeText = screen.getByTestId("gauge").textContent ?? "";
    expect(gaugeText).not.toMatch(/\d(EUR|USD|GBP)/);
    // Either the locale's symbol, or the code set off from the number.
    expect(gaugeText).toMatch(/[€$£]\s?\d|\d\s\w{3}/);
  });

  it("does not ask the same question twice under the arc", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    const block = screen.getByTestId("composition-block-gauge");
    // The block title is rendered once, as the overline above the gauge. The
    // gauge no longer repeats it as its own caption — which is what printed
    // « CPA vs objectif » a second time, across the arc.
    const title = "CPA vs objectif";
    // The svg `<title>` is the accessible name, not visible text — it is removed
    // before counting what a reader actually sees.
    const visible = block.cloneNode(true) as HTMLElement;
    visible.querySelectorAll("svg title").forEach((n) => n.remove());
    const occurrences = (visible.textContent ?? "").split(title).length - 1;
    expect(occurrences).toBe(1);
  });

  it("states the missing objective, in the shell's served language", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByTestId("gauge-no-target")).toHaveTextContent("No target set");
  });
});

describe("card-conversions — one label, asked once (76-8)", () => {
  it("drops a unit that only restates the metric, keeps one that changes the reading", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    const labels = screen
      .getAllByTestId("composition-kpi-tile")
      .map((t) => t.textContent ?? "")
      .join(" ");
    expect(labels).not.toMatch(/CONVERSIONS \(CONVERSIONS\)/i);
    // `EUR` changes how the number reads, so it stays.
    expect(labels).toMatch(/\(EUR\)/);
  });
});
