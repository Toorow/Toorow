/**
 * Story 76-8 — `card-journey` on 2026-09-05 printed, between two funnel steps:
 *
 *     ▲ 3 %  (−260 223 , −97 %)
 *
 * an arrow pointing UP over a 97 % collapse, one percentage that was the
 * through-rate and a second, in a parenthesis, that was the drop. Three numbers
 * for one fact, and the arrow said the opposite of it.
 *
 * The ratified convention (arbitrage 5): ARROW + SIGNED PERCENT + the word that
 * says what it is compared to, the passage rate kept behind it in grey, and a
 * legend under the funnel that says what the arrows mean.
 */

import { describe, expect, it } from "vitest";
import { render, screen, within } from "@testing-library/react";

import App from "../App";
import { FIXTURE_ENVELOPE } from "../fixture";

describe("card-journey — a funnel delta reads in one go (76-8)", () => {
  it("points the arrow at the direction the volume actually went", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    const variation = screen.getByTestId("funnel-step-variation-1");
    const text = variation.textContent ?? "";
    // Sessions -> conversions is a collapse, so the arrow points down…
    expect(text).toContain("▼");
    expect(text).not.toContain("▲");
    // …and the percentage carries the sign of that collapse.
    expect(text).toMatch(/-\d+\.\d%/);
    expect(text).toContain("vs previous step");
  });

  it("keeps the passage rate, discreetly, instead of a second bare percentage", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByTestId("funnel-passage-rate-1")).toHaveTextContent(/\d% passed/);
  });

  it("legends the arrows ONCE, in the card footer, never under the block", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getAllByTestId("variation-legend")).toHaveLength(1);
    const legend = within(screen.getByTestId("card-footer")).getByTestId("variation-legend");
    expect(legend).toHaveTextContent("▲ up");
    expect(legend).toHaveTextContent("▼ down");
    expect(legend).toHaveTextContent("Green: favourable");
  });

  it("uses one minus sign and one decimal convention across the whole step row", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    const row = screen.getByTestId("funnel-through-rate-1");
    const text = row.textContent ?? "";
    // The minus glyph depends on ICU; what must NOT depend on it is that there
    // is only one of them in the row. Two glyphs are two conventions, and that
    // was the case while a hand-written U+2212 sat beside the hyphen the
    // formatter renders.
    const signGlyphs = new Set((text.match(/[-−]/g) ?? []));
    expect(signGlyphs.size).toBeLessThanOrEqual(1);
    // No decimal comma: the pinned formatter groups with a comma and separates
    // decimals with a point, and a card shows one convention.
    expect(text).not.toMatch(/\d,\d(?!\d\d)/);
  });
});

describe("card-journey — a metric label is a word, not a token (76-8)", () => {
  it("names screen_page_views instead of printing the wire token", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    const labels = screen
      .getAllByTestId("composition-kpi-tile")
      .map((t) => t.textContent ?? "")
      .join(" ");
    expect(labels).not.toContain("SCREEN_PAGE_VIEWS");
    expect(labels).not.toContain("screen_page_views");
    expect(labels).toMatch(/Pages vues/i);
    // …and no label repeats itself inside its own unit parenthesis.
    expect(labels).not.toMatch(/SESSIONS \(SÉANCES\)/i);
    expect(labels).not.toMatch(/CONVERSIONS \(CONVERSIONS\)/i);
  });
});
