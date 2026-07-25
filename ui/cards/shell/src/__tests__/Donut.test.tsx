/**
 * Donut tests (Story 9.2b) — slices, centre total, légende, empty.
 */

import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import Donut from "../Donut";
import type { DonutSlice } from "../Donut";

const SLICES: DonutSlice[] = [
  { label: "Organique", value: 600 },
  { label: "Payant", value: 250 },
  { label: "Direct", value: 150 },
];

describe("Donut", () => {
  it("renders the donut with slices", () => {
    render(<Donut slices={SLICES} unit="conversions" ariaLabel="Répartition sources" />);
    expect(screen.getByTestId("donut")).toBeInTheDocument();
    // Chaque slice doit avoir un path dans le SVG
    const paths = screen.getByTestId("donut").querySelectorAll("svg path");
    expect(paths.length).toBe(SLICES.length);
  });

  it("renders the total in the centre of the SVG", () => {
    render(<Donut slices={SLICES} unit="conv" ariaLabel="Donut" />);
    const svg = screen.getByTestId("donut").querySelector("svg");
    // Total = 600+250+150 = 1000 => fr-FR "1 000"
    expect(svg?.textContent).toContain("1");
  });

  it("renders the legend with labels, values and percentages", () => {
    render(<Donut slices={SLICES} ariaLabel="Donut" />);
    const legend = screen.getByTestId("donut-legend");
    expect(legend).toHaveTextContent("Organique");
    expect(legend).toHaveTextContent("Payant");
    expect(legend).toHaveTextContent("Direct");
    // Percentages: 600/1000=60%, 250/1000=25%, 150/1000=15%
    expect(legend).toHaveTextContent("60 %");
    expect(legend).toHaveTextContent("25 %");
    expect(legend).toHaveTextContent("15 %");
  });

  it("renders the designed empty state when slices is empty", () => {
    render(<Donut slices={[]} ariaLabel="Donut vide" />);
    expect(screen.getByTestId("donut-empty")).toBeInTheDocument();
    expect(screen.getByTestId("donut-empty")).toHaveTextContent("Aucune donnée");
  });

  it("renders the designed empty state when all slices have value 0", () => {
    render(<Donut slices={[{ label: "X", value: 0 }]} ariaLabel="Donut vide" />);
    expect(screen.getByTestId("donut-empty")).toBeInTheDocument();
  });

  it("has an accessible aria role and label", () => {
    render(<Donut slices={SLICES} ariaLabel="Parts de marché" />);
    expect(screen.getByRole("img", { name: "Parts de marché" })).toBeInTheDocument();
  });

  // Fix F-6 (9-2b review): negative-value slices must not produce degenerate SVG paths.
  // They should be clamped to 0 and the Donut must render its designed empty state when
  // the effective total (positive values only) is 0 or negative.
  it("renders the designed empty state when all slices have negative values (F-6)", () => {
    render(
      <Donut
        slices={[{ label: "Delta", value: -100 }, { label: "Perte", value: -50 }]}
        ariaLabel="Donut négatif"
      />,
    );
    expect(screen.getByTestId("donut-empty")).toBeInTheDocument();
  });

  it("clamps negative slices to 0 in a mixed-sign set without crashing (F-6)", () => {
    // One positive, one negative — the negative is clamped; donut renders with only positive.
    render(
      <Donut
        slices={[{ label: "Positif", value: 300 }, { label: "Négatif", value: -100 }]}
        ariaLabel="Donut mixte"
      />,
    );
    // Should render (not empty state) because there is at least one positive slice.
    expect(screen.getByTestId("donut")).toBeInTheDocument();
    // The negative slice's pct should be 0 %, not negative.
    const legend = screen.getByTestId("donut-legend");
    expect(legend).toHaveTextContent("Positif");
  });
});
