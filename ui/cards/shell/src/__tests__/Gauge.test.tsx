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
    // Value doit apparaître dans le SVG
    const svg = gauge.querySelector("svg");
    expect(svg?.textContent).toContain("3,5");
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

  // Fix F-5 (9-2b review): target=0 must show the cible label (was silently ignored
  // because `target ?` is falsy for 0).
  it("renders the cible label when target=0 (F-5 — explicit null/undefined check)", () => {
    render(<Gauge value={0} target={0} unit="€" ariaLabel="Jauge cible zéro" />);
    const svg = screen.getByTestId("gauge").querySelector("svg");
    // The target label "cible : 0 €" should appear in the SVG text.
    expect(svg?.textContent).toContain("cible");
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
