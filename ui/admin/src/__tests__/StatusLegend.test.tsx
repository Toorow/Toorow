/**
 * THE KEY IS THE SAME DRAWING AS THE THING IT EXPLAINS.
 *
 * `console-presentation.md` §3 asks every screen showing three tones or more to
 * mount a `StatusLegend`. A legend earns that mount only if its mark is a
 * miniature of the mark on the screen — `CoverageBars.tsx` l.510-518 records what
 * happens otherwise (Jean, 2026-07-29: "not the same color que la légende").
 *
 * So this file asserts the two things a reader would notice if they broke, and
 * nothing about the copy, which belongs to the caller:
 *
 *   1. ONE ROW PER TONE GIVEN, in the scale's order — never the caller's, or two
 *      screens listing the same three tones would list them two ways.
 *   2. THE SHAPES. Warning is a diamond and neutral is an open dotted ring, the
 *      two rules `Status` carries, because they are what survives greyscale and
 *      colour blindness. Asserted on the class, since a rotation and a border
 *      style have no accessible name to query.
 *
 * And one thing about the source rather than the render: NO COLOUR LITERAL. The
 * component reads `HALO`; a hex or a `text-success` written here would be the
 * seventh scale this whole epic exists to remove.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { StatusLegend, type StatusLegendEntry } from "../ui/StatusLegend";

const THREE: StatusLegendEntry[] = [
  { tone: "warning", label: "Stale", meaning: "collected, but older than its freshness window" },
  { tone: "success", label: "Fresh", meaning: "collected inside its freshness window" },
  { tone: "neutral", label: "Never requested", meaning: "no collection was ever asked for" },
];

describe("StatusLegend lists what this screen's marks mean", () => {
  it("renders one row per tone given", () => {
    render(<StatusLegend entries={THREE} />);
    const rows = within(screen.getByTestId("status-legend")).getAllByRole("listitem");
    expect(rows).toHaveLength(3);
    expect(screen.getByText("Stale")).toBeInTheDocument();
    expect(screen.getByText(/older than its freshness window/)).toBeInTheDocument();
  });

  it("orders them by the tone scale, not by the order the caller passed", () => {
    render(<StatusLegend entries={THREE} />);
    const rows = within(screen.getByTestId("status-legend")).getAllByRole("listitem");
    // TONES = neutral, success, warning, error, info.
    expect(rows.map((row) => row.getAttribute("data-tone"))).toEqual([
      "neutral",
      "success",
      "warning",
    ]);
  });

  it("draws the warning as a diamond and the neutral as an open dotted ring", () => {
    render(<StatusLegend entries={THREE} />);
    const rows = within(screen.getByTestId("status-legend")).getAllByRole("listitem");
    const markOf = (tone: string) =>
      rows.find((row) => row.getAttribute("data-tone") === tone)!.querySelector("span[aria-hidden]")!;
    expect(markOf("warning").className).toContain("rotate-45");
    expect(markOf("neutral").className).toContain("border-dotted");
    expect(markOf("neutral").className).toContain("bg-transparent");
    // And success is neither: a plain filled dot.
    expect(markOf("success").className).not.toContain("rotate-45");
    expect(markOf("success").className).not.toContain("border-dotted");
  });

  it("names itself for a screen reader, and takes the caller's name when given", () => {
    const { rerender } = render(<StatusLegend entries={THREE} />);
    expect(screen.getByRole("list", { name: "What these marks mean" })).toBeInTheDocument();
    rerender(<StatusLegend entries={THREE} label="What a run's mark means" />);
    expect(screen.getByRole("list", { name: "What a run's mark means" })).toBeInTheDocument();
  });

  it("renders nothing for fewer than two tones — a key of one is a caption", () => {
    const { container } = render(<StatusLegend entries={[THREE[0]]} />);
    expect(container).toBeEmptyDOMElement();
    const empty = render(<StatusLegend entries={[]} />);
    expect(empty.container).toBeEmptyDOMElement();
  });
});

describe("the legend holds no colour of its own", () => {
  it("names no tone class and no hex in its source", () => {
    const source = readFileSync(resolve(__dirname, "../ui/StatusLegend.tsx"), "utf-8")
      .replace(/\/\*[\s\S]*?\*\//g, "")
      .replace(/\/\/.*/g, "");
    expect(source).not.toMatch(/#[0-9a-fA-F]{3,8}\b/);
    expect(source).not.toMatch(/\b(?:text|bg|border)-(?:success|warning|error|info|primary)\b/);
    // It reads the one map instead.
    expect(source).toContain("HALO[tone]");
  });
});
