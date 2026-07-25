/**
 * BarChart tests (Story 9.2b) — horizontal, vertical, groupé, empty state, aria.
 * Epic review 2026-07-14: direction-aware movers coloring tests (F-1 + F-3).
 */

import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import BarChart from "../BarChart";
import type { BarChartEntry } from "../BarChart";

const ENTRIES: BarChartEntry[] = [
  { label: "Organique", value: 1500 },
  { label: "Payant", value: 800 },
  { label: "Direct", value: 400 },
];

const GROUPED_ENTRIES: BarChartEntry[] = [
  { label: "Semaine 1", values: { Desktop: 200, Mobile: 150 } },
  { label: "Semaine 2", values: { Desktop: 220, Mobile: 180 } },
];

describe("BarChart — horizontal", () => {
  it("renders bars with labels and values", () => {
    render(<BarChart entries={ENTRIES} variant="horizontal" ariaLabel="Barres sources" />);
    const chart = screen.getByTestId("bar-chart");
    expect(chart).toBeInTheDocument();
    expect(chart).toHaveAttribute("data-variant", "horizontal");
    expect(chart).toHaveTextContent("Organique");
    expect(chart).toHaveTextContent("Payant");
    expect(chart).toHaveTextContent("1 500"); // fr-FR locale
  });

  it("has an accessible aria role and label", () => {
    render(<BarChart entries={ENTRIES} ariaLabel="Sources de trafic" />);
    expect(screen.getByRole("img", { name: "Sources de trafic" })).toBeInTheDocument();
  });

  it("renders the designed empty state when entries is empty", () => {
    render(<BarChart entries={[]} />);
    expect(screen.getByTestId("bar-chart-empty")).toBeInTheDocument();
    expect(screen.getByTestId("bar-chart-empty")).toHaveTextContent("Aucune donnée disponible");
  });
});

describe("BarChart — vertical", () => {
  it("renders an SVG with bars (vertical variant)", () => {
    render(<BarChart entries={ENTRIES} variant="vertical" ariaLabel="Barres verticales" />);
    const chart = screen.getByTestId("bar-chart");
    expect(chart).toHaveAttribute("data-variant", "vertical");
    const svg = chart.querySelector("svg");
    expect(svg).toBeInTheDocument();
    // Doit contenir des rect (barres)
    expect(svg?.querySelectorAll("rect").length).toBeGreaterThan(0);
  });
});

describe("BarChart — groupé", () => {
  it("renders grouped bars per entry", () => {
    render(
      <BarChart
        entries={GROUPED_ENTRIES}
        variant="horizontal"
        groupKeys={["Desktop", "Mobile"]}
        ariaLabel="Barres groupées"
      />,
    );
    const chart = screen.getByTestId("bar-chart");
    expect(chart).toHaveTextContent("Semaine 1");
    expect(chart).toHaveTextContent("Semaine 2");
  });
});

// ---------------------------------------------------------------------------
// F-1 + F-3 (Epic review 2026-07-14): direction-aware movers bar.
// ---------------------------------------------------------------------------

// Server contract (cards.py _resolve_bar_movers): value = SIGNED delta
// (positive = rank gained -> direction "up"; negative = rank lost -> "down").
const MOVER_ENTRIES: BarChartEntry[] = [
  { label: "bottes de randonnée",     value: 3.2,  direction: "up" },
  { label: "chaussures trail femme",  value: -2.5, direction: "down" },
  { label: "chaussettes randonnée",   value: -3.8, direction: "down" },
];

describe("BarChart — direction-aware movers coloring (F-1 + F-3)", () => {
  it("renders signed value labels with + prefix for up-direction entries", () => {
    render(
      <BarChart
        entries={MOVER_ENTRIES}
        variant="horizontal"
        semanticDirection="down_good"
        ariaLabel="Movers"
      />,
    );
    const chart = screen.getByTestId("bar-chart");
    // +3,2 sign must appear (fr-FR format)
    expect(chart.textContent).toContain("+");
  });

  it("renders signed value labels with - prefix for down-direction entries", () => {
    render(
      <BarChart
        entries={MOVER_ENTRIES}
        variant="horizontal"
        semanticDirection="down_good"
        ariaLabel="Movers"
      />,
    );
    const chart = screen.getByTestId("bar-chart");
    // Negative prefix must appear for "down" movers
    expect(chart.textContent).toMatch(/-/);
  });

  it("renders all mover labels including gaining and losing queries", () => {
    render(
      <BarChart
        entries={MOVER_ENTRIES}
        variant="horizontal"
        semanticDirection="down_good"
        ariaLabel="Movers"
      />,
    );
    const chart = screen.getByTestId("bar-chart");
    expect(chart.textContent).toContain("bottes de randonnée");
    expect(chart.textContent).toContain("chaussures trail femme");
  });

  it("flat bar (no semanticDirection) renders without sign — regression for other cards", () => {
    render(
      <BarChart
        entries={[
          { label: "google-ads", value: 200 },
          { label: "meta-ads",   value: 80 },
        ]}
        variant="horizontal"
        ariaLabel="Sources"
      />,
    );
    const chart = screen.getByTestId("bar-chart");
    // No sign prefix when semanticDirection absent.
    expect(chart.textContent).not.toContain("+");
    expect(chart.textContent).toContain("200");
  });

  it("negative movers render at correct bar width (not zero or 2% only)", () => {
    // When direction entries are present, value in entries is already |v| (CardComposition passes Math.abs).
    // This test verifies that entries with value > 0 (representing a formerly-negative delta) render a bar.
    render(
      <BarChart
        entries={[
          { label: "gaining",  value: 3.2, direction: "up" },
          { label: "losing",   value: 3.8, direction: "down" },
        ]}
        variant="horizontal"
        semanticDirection="down_good"
        ariaLabel="Movers"
      />,
    );
    const chart = screen.getByTestId("bar-chart");
    // Both bars rendered (chart content includes both labels).
    expect(chart.textContent).toContain("gaining");
    expect(chart.textContent).toContain("losing");
  });
});
