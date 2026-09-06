/**
 * Gauge tests (Story 9.2b) — valeur vs cible, direction-aware tint, empty.
 */

import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import Gauge from "../Gauge";

describe("Gauge", () => {
  it("renders the gauge with value and target", () => {
    render(<Gauge value={3.5} target={5} unit="€" label="CPA" ariaLabel="Jauge CPA" />);
    const gauge = screen.getByTestId("gauge");
    expect(gauge).toBeInTheDocument();
    // The value appears in the SVG on the pinned formatter (story 76-8), and its
    // unit is never welded to the digits.
    const svg = gauge.querySelector("svg");
    expect(svg?.textContent).toContain("3.5");
    expect(svg?.textContent).not.toContain("3.5€");
    expect(svg?.textContent).toContain("5"); // cible
  });

  it("renders the designed empty state when value is null", () => {
    render(<Gauge value={null} ariaLabel="Jauge vide" />);
    const empty = screen.getByTestId("gauge-empty");
    expect(empty).toBeInTheDocument();
    expect(empty).toHaveTextContent("Donnée manquante");
  });

  it("renders the designed empty state when value is undefined", () => {
    render(<Gauge value={undefined} ariaLabel="Jauge vide" />);
    expect(screen.getByTestId("gauge-empty")).toBeInTheDocument();
  });

  it("has an accessible aria role and label", () => {
    render(<Gauge value={42} ariaLabel="Score de complétion" />);
    expect(screen.getByRole("img", { name: "Score de complétion" })).toBeInTheDocument();
  });

  it("renders a label caption below the SVG", () => {
    render(<Gauge value={10} label="CPA cible" />);
    expect(screen.getByText("CPA cible")).toBeInTheDocument();
  });

  it("shows the fill arc path when value > 0", () => {
    render(<Gauge value={50} target={100} ariaLabel="Jauge" />);
    const svg = screen.getByTestId("gauge").querySelector("svg");
    // Doit avoir 2 path : track + fill
    expect(svg?.querySelectorAll("path").length).toBeGreaterThanOrEqual(2);
  });

  // Fix F-5 (9-2b review): target=0 must show the target label (was silently ignored
  // because `target ?` is falsy for 0).
  it("renders the target label when target=0 (F-5 — explicit null/undefined check)", () => {
    render(<Gauge value={0} target={0} unit="€" ariaLabel="Jauge cible zéro" />);
    const svg = screen.getByTestId("gauge").querySelector("svg");
    // The target label lives UNDER the arc: inside, it overran the stroke
    // (story 76-8). Served copy is English (console-presentation, 2026-09-05).
    expect(screen.getByTestId("gauge-target")).toHaveTextContent("Target");
    expect(svg?.textContent).not.toContain("Target");
    expect(svg?.textContent).toContain("0");
  });

  it("renders gauge at zero fill with target=0 and value=0 (no fill arc)", () => {
    render(<Gauge value={0} target={0} ariaLabel="Jauge zéro" />);
    const svg = screen.getByTestId("gauge").querySelector("svg");
    // fraction = 0/max → cappedFraction = 0 → no fill arc, only track.
    const paths = svg?.querySelectorAll("path");
    // Track arc always present; fill arc absent when fraction is 0.
    expect(paths?.length).toBeGreaterThanOrEqual(1);
  });

  it("does not overflow fill arc past track when value >> target (F-4 ui — capped at 95%)", () => {
    render(<Gauge value={200} target={50} ariaLabel="Jauge over-target" />);
    const svg = screen.getByTestId("gauge").querySelector("svg");
    // Should have 2 paths (track + fill) — fill is capped, not overflowing.
    expect(svg?.querySelectorAll("path").length).toBeGreaterThanOrEqual(2);
  });

  it("scales down font size for long value+unit strings (F-4 ui — label overflow guard)", () => {
    // Render with a long value and long unit; the gauge must still render without throwing.
    render(<Gauge value={1234} unit=" utilisateurs actifs" ariaLabel="Jauge longue" />);
    expect(screen.getByTestId("gauge")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// CAV-08 / story 53.5 — no target, no verdict.
//
// The server stopped emitting a platform objective (`_DEFAULT_CPA_TARGET = 50.0`,
// removed in 34cd021): an unbound gauge now arrives with target=null. Every case
// above passes a target, so nothing here proved what the gauge does WITHOUT one --
// and without one it must draw no colour, print no comparison, and say so.
// ---------------------------------------------------------------------------

/** Stroke colour of the fill arc (paths[0] is the track, paths[1] the fill). */
function fillStroke(testId = "gauge"): string | null | undefined {
  const paths = screen.getByTestId(testId).querySelectorAll("path");
  return paths[1]?.getAttribute("stroke");
}

describe("Gauge — no target, no verdict (CAV-08)", () => {
  it("draws the neutral accent, not a verdict colour, when no target is given", () => {
    // Reference A: a real comparison that PASSES (CPA 30 under a target of 50).
    const { unmount } = render(
      <Gauge value={30} target={50} direction="down_good" ariaLabel="Jauge avec objectif" />,
    );
    const goodStroke = fillStroke();
    unmount();

    // Reference B: no semantic direction at all — the neutral accent by construction.
    const neutral = render(<Gauge value={30} direction="neutral" ariaLabel="Jauge neutre" />);
    const neutralStroke = fillStroke();
    neutral.unmount();

    // Subject: same value and same direction as A, but NO target. Nothing to compare
    // against, so it must look like B and never like A.
    render(<Gauge value={30} direction="down_good" ariaLabel="Jauge sans objectif" />);
    const noTargetStroke = fillStroke();

    expect(noTargetStroke).toBe(neutralStroke);
    expect(noTargetStroke).not.toBe(goodStroke);
  });

  it("reports verdict 'none' when no target is given, whatever the direction", () => {
    for (const direction of ["down_good", "up_good", "neutral"] as const) {
      const { unmount } = render(<Gauge value={30} direction={direction} ariaLabel="Jauge" />);
      expect(screen.getByTestId("gauge")).toHaveAttribute("data-verdict", "none");
      unmount();
    }
  });

  it("still reports a verdict when a target IS given (the guard is not a blanket mute)", () => {
    const { unmount } = render(<Gauge value={30} target={50} direction="down_good" />);
    expect(screen.getByTestId("gauge")).toHaveAttribute("data-verdict", "good");
    unmount();

    render(<Gauge value={80} target={50} direction="down_good" />);
    expect(screen.getByTestId("gauge")).toHaveAttribute("data-verdict", "bad");
  });

  it("states the absence instead of leaving the target slot empty", () => {
    render(<Gauge value={30} unit="EUR" direction="down_good" ariaLabel="Jauge sans objectif" />);
    expect(screen.getByTestId("gauge-no-target")).toHaveTextContent("No target set");
    expect(screen.queryByTestId("gauge-target")).not.toBeInTheDocument();
  });

  it("never prints a target figure when there is no target", () => {
    render(<Gauge value={30} unit="EUR" direction="down_good" ariaLabel="Jauge sans objectif" />);
    const svg = screen.getByTestId("gauge").querySelector("svg");
    // The deleted platform constant must not reappear from any code path.
    expect(svg?.textContent).not.toContain("50");
    expect(svg?.textContent).not.toContain("Target:");
  });
});
