/**
 * LineChart tests (Story 9.2b) — render avec data, empty state, aria.
 */

import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import LineChart from "../LineChart";
import type { LineChartSeries } from "../LineChart";

const SERIES_1: LineChartSeries[] = [
  {
    label: "Sessions",
    points: [
      { index: "2026-07-01", value: 100 },
      { index: "2026-07-02", value: 120 },
      { index: "2026-07-03", value: 90 },
    ],
  },
];

const SERIES_2: LineChartSeries[] = [
  ...SERIES_1,
  {
    label: "Conversions",
    points: [
      { index: "2026-07-01", value: 10 },
      { index: "2026-07-02", value: 15 },
      { index: "2026-07-03", value: 8 },
    ],
  },
];

describe("LineChart", () => {
  it("renders the SVG chart with one series", () => {
    render(<LineChart series={SERIES_1} ariaLabel="Tendance sessions" />);
    const chart = screen.getByTestId("line-chart");
    expect(chart).toBeInTheDocument();
    const svg = chart.querySelector("svg");
    expect(svg).toHaveAttribute("aria-label", "Tendance sessions");
    // Une polyline doit être présente
    expect(svg?.querySelector("polyline")).toBeInTheDocument();
  });

  it("renders a legend when >1 series", () => {
    render(<LineChart series={SERIES_2} />);
    const legend = screen.getByTestId("line-chart-legend");
    expect(legend).toHaveTextContent("Sessions");
    expect(legend).toHaveTextContent("Conversions");
  });

  it("does NOT render a legend for a single series", () => {
    render(<LineChart series={SERIES_1} />);
    expect(screen.queryByTestId("line-chart-legend")).not.toBeInTheDocument();
  });

  it("renders the designed empty state when series is empty", () => {
    render(<LineChart series={[]} />);
    const empty = screen.getByTestId("line-chart-empty");
    expect(empty).toBeInTheDocument();
    expect(empty).toHaveTextContent("Aucune donnée disponible");
  });

  it("renders the designed empty state when all series have <2 points", () => {
    render(
      <LineChart
        series={[{ label: "X", points: [{ index: "2026-07-01", value: 5 }] }]}
      />,
    );
    expect(screen.getByTestId("line-chart-empty")).toBeInTheDocument();
  });

  it("has an accessible aria-label on the SVG", () => {
    render(<LineChart series={SERIES_1} ariaLabel="Courbe de trafic" />);
    expect(screen.getByRole("img", { name: "Courbe de trafic" })).toBeInTheDocument();
  });

  // Fix F-4 (9-2b review): NaN values must trigger the designed empty state rather than
  // producing invisible degenerate SVG paths.
  it("renders the designed empty state when series points contain NaN values (F-4)", () => {
    render(
      <LineChart
        series={[{
          label: "NaN série",
          points: [
            { index: "2026-07-01", value: NaN },
            { index: "2026-07-02", value: NaN },
            { index: "2026-07-03", value: NaN },
          ],
        }]}
      />,
    );
    expect(screen.getByTestId("line-chart-empty")).toBeInTheDocument();
    expect(screen.getByTestId("line-chart-empty")).toHaveTextContent("Aucune donnée disponible");
  });

  it("renders only finite points and ignores NaN points in a mixed series", () => {
    render(
      <LineChart
        series={[{
          label: "Mixed",
          points: [
            { index: "2026-07-01", value: 100 },
            { index: "2026-07-02", value: NaN },
            { index: "2026-07-03", value: 120 },
          ],
        }]}
      />,
    );
    // Two finite points remain → chart renders (not empty state).
    const chart = screen.getByTestId("line-chart");
    expect(chart).toBeInTheDocument();
    expect(chart.querySelector("polyline")).toBeInTheDocument();
  });

  it("renders empty state when only one finite point remains after NaN filtering", () => {
    render(
      <LineChart
        series={[{
          label: "OnePoint",
          points: [
            { index: "2026-07-01", value: 100 },
            { index: "2026-07-02", value: NaN },
          ],
        }]}
      />,
    );
    expect(screen.getByTestId("line-chart-empty")).toBeInTheDocument();
  });
});

describe("les repères datés — une sortie n'est pas une mesure", () => {
  const series = [
    {
      label: "views",
      points: [
        { index: "2026-07-13", value: 200 },
        { index: "2026-07-14", value: 210 },
        { index: "2026-07-15", value: 1201 },
        { index: "2026-07-16", value: 480 },
      ],
    },
  ];

  it("pose le repère sur le jour de l'axe, avec son titre lisible", () => {
    render(
      <LineChart
        series={series}
        markers={[{ index: "2026-07-15", label: "Pique-nique végan" }]}
      />,
    );

    const marker = document.querySelector('[data-marker="2026-07-15"]');
    expect(marker).toBeTruthy();
    // Le titre voyage avec le repère : c'est ce qu'une personne lit, jamais l'id.
    expect(marker?.querySelector("title")?.textContent).toBe("Pique-nique végan");
  });

  it("ne dessine pas un repère dont la date n'est sur aucun point tracé", () => {
    // Le poser demanderait d'inventer une position sur l'axe.
    render(
      <LineChart series={series} markers={[{ index: "2025-01-01", label: "Vieille sortie" }]} />,
    );

    expect(document.querySelector('[data-marker="2025-01-01"]')).toBeNull();
  });

  it("reste la même courbe sans repère — la propriété est additive", () => {
    const { container: withoutMarkers } = render(<LineChart series={series} />);
    const polylines = withoutMarkers.querySelectorAll("polyline");

    expect(polylines.length).toBe(1);
    expect(withoutMarkers.querySelector("[data-marker]")).toBeNull();
  });
});
