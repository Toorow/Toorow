/**
 * Story 76-8 — what `card-keywords` looked like on 2026-09-05, and must not again.
 *
 * The capture of that day showed, in ONE card: « CLICS (CLICS) » and
 * « IMPRESSIONS (IMPRESSIONS) » (the label asked twice), « +8.8 % » with a
 * decimal point beside « 2,4 » with a decimal comma (two conventions), and a
 * table header reading « CLICSIMPRESSIONSPOSITION MOY. » (three headers with no
 * gutter). Each of the three is asserted here on the real fixture.
 */

import { describe, expect, it } from "vitest";
import { render, screen, within } from "@testing-library/react";

import App from "../App";
import { FIXTURE_ENVELOPE } from "../fixture";

function renderCard() {
  render(<App envelope={FIXTURE_ENVELOPE} />);
}

describe("card-keywords — one label, asked once (76-8)", () => {
  it("never repeats the metric name in its own unit parenthesis", () => {
    renderCard();
    const tiles = screen.getAllByTestId("composition-kpi-tile");
    const labels = tiles.map((t) => t.textContent ?? "");
    expect(labels.join(" ")).not.toMatch(/CLICS \(CLICS\)/i);
    expect(labels.join(" ")).not.toMatch(/IMPRESSIONS \(IMPRESSIONS\)/i);
    // The label itself is still there — the parenthesis is what went.
    expect(labels.join(" ")).toMatch(/Clics/i);
  });
});

describe("card-keywords — one decimal convention (76-8)", () => {
  it("groups values and signs deltas through the same pinned formatter", () => {
    renderCard();
    const card = screen.getByTestId("card-shell");
    const text = card.textContent ?? "";
    // The hero value 3842 is grouped by the pinned locale…
    expect(text).toContain("3,842");
    // …and the delta carries a decimal POINT, the same convention.
    expect(text).toMatch(/[+-]\d+\.\d%/);
    // A decimal comma anywhere in a NUMBER would be the second convention.
    // (The narrative prose is a different object and is not scanned here.)
    const numbers = screen.getAllByTestId("composition-kpi-value").map((n) => n.textContent ?? "");
    for (const n of numbers) {
      expect(n).not.toMatch(/\d,\d(?!\d\d)/); // `2,4` is a comma DECIMAL, `3,842` a group
    }
  });

  it("prints table cell numbers on the same convention as the hero", () => {
    renderCard();
    const cells = screen.getAllByTestId("cell-impressions");
    expect(cells[0].textContent).toBe("18,000");
  });
});

describe("card-keywords — the colour of a variation is legended (76-8)", () => {
  it("keeps the sign on a negative mover — `-3.8`, never `+3.8`", () => {
    renderCard();
    const bars = screen.getByTestId("composition-block-bar");
    // ICU renders the minus as a hyphen on one engine and as U+2212 on another:
    // what is tested is the SIGN, not the glyph, so it is normalised first. That
    // there is only one glyph per card is guaranteed elsewhere — one formatter
    // writes them all.
    const text = (bars.textContent ?? "").replace(/−/g, "-");
    // The fixture carries `-3.8` for « chaussettes de randonnée »: the bar is
    // sized by |value|, the LABEL keeps the sign.
    expect(text).toContain("-3.8");
    expect(text).toContain("+3.2");
    expect(text).not.toContain("+3.8");
  });

  it("paints -3.8 favourable and +3.2 unfavourable under `down_good`", () => {
    renderCard();
    // THE FINDING OF ROUND 2. The bars used to colour from `entry.direction`
    // alone, so `+3.2` came out green and `-3.8` red under a legend saying the
    // opposite. The verdict now comes from the SIGNED change and the binding.
    const rows = screen.getAllByTestId("bar-chart-row");
    const byLabel = new Map(
      rows.map((r) => [r.getAttribute("data-label"), r.getAttribute("data-verdict")]),
    );
    expect(byLabel.get("chaussettes de randonnée")).toBe("favourable");
    expect(byLabel.get("bottes de randonnée")).toBe("unfavourable");
  });

  it("states both conventions once, in the card footer", () => {
    renderCard();
    // ONE placement (arbitrage 5): not under each block.
    expect(screen.getAllByTestId("variation-legend")).toHaveLength(1);
    const legend = within(screen.getByTestId("card-footer")).getByTestId("variation-legend");
    expect(legend).toHaveTextContent("Green: favourable");
    expect(legend).toHaveTextContent("red: unfavourable");
    // Clicks rise favourably; average position falls favourably. One sentence
    // for both would be false for half the figures on the card.
    expect(legend).toHaveTextContent("rising is favourable for Clics");
    expect(legend.textContent).toMatch(/falling is favourable for/);
    expect(legend).toHaveAttribute("data-direction", "mixed");
  });
});

describe("card-keywords — table headers do not touch (76-8)", () => {
  it("puts a gutter between columns, header and body alike", () => {
    renderCard();
    const header = screen.getAllByTestId("data-table-header")[0];
    const gap = getComputedStyle(header).columnGap;
    expect(gap).not.toBe("");
    expect(gap).not.toBe("0px");
    expect(gap).not.toBe("normal");
  });

  it("keeps every column header as its own cell", () => {
    renderCard();
    const header = screen.getAllByTestId("data-table-header")[0];
    const headers = Array.from(header.querySelectorAll('[role="columnheader"]'));
    expect(headers.length).toBeGreaterThan(2);
    for (const h of headers) {
      expect((h.textContent ?? "").trim().length).toBeGreaterThan(0);
    }
  });
});
