/**
 * Conversions card tests (Epic 9, Story 9.4).
 * - Renders fixture without throwing.
 * - kpi_row, donut, gauge, table, comment all render from block.data.
 * - Empty/malformed blocks render the primitive's designed empty state.
 */

import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import App from "../App";
import { FIXTURE_ENVELOPE } from "../fixture";
import type { CardEnvelope } from "@toorow/card-shell";

describe("Conversions card — fixture renders without throwing", () => {
  it("renders the card title", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByTestId("card-title")).toHaveTextContent("Conversions");
  });

  it("renders kpi_row with conversions and cost from block.data", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    const tiles = screen.getAllByTestId("composition-kpi-tile");
    expect(tiles.length).toBe(2);
    const convTile = tiles.find((t) => t.getAttribute("data-metric") === "conversions")!;
    expect(convTile).toBeInTheDocument();
    expect(convTile.querySelector("[data-testid='composition-kpi-value']")?.textContent).toMatch(/320/);
  });

  it("renders donut chart (by source) from block.data.slices", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByTestId("composition-block-donut")).toBeInTheDocument();
    expect(screen.getByTestId("donut")).toBeInTheDocument();
    expect(screen.getByTestId("donut-legend")).toBeInTheDocument();
  });

  it("renders gauge (CPA vs target) from block.data", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByTestId("composition-block-gauge")).toBeInTheDocument();
    expect(screen.getByTestId("gauge")).toBeInTheDocument();
  });

  // -------------------------------------------------------------------------
  // CAV-08 / story 53.5 — the fixture must not resurrect the deleted constant.
  //
  // main.tsx renders FIXTURE_ENVELOPE whenever no envelope is injected, so what
  // this fixture declares IS what an un-injected card shows. It carried
  // target: 50.0 / target_source: "default" -- the platform objective the server
  // deleted in 34cd021 -- and with value 30 / down_good the gauge painted a green
  // verdict against a number nobody chose.
  // -------------------------------------------------------------------------
  it("declares no objective in the fixture (the platform constant is gone)", () => {
    const gauge = FIXTURE_ENVELOPE.data.composition?.find((b) => b.type === "gauge");
    expect(gauge).toBeDefined();
    expect(gauge!.data!.target).toBeNull();
    expect(gauge!.data!.target_source).toBe("unset");
  });

  it("renders no verdict and states the absence of an objective", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByTestId("composition-block-gauge")).toHaveAttribute("data-target-state", "unset");
    expect(screen.getByTestId("gauge")).toHaveAttribute("data-verdict", "none");
    expect(screen.getByTestId("gauge-no-target")).toBeInTheDocument();
  });

  it("prints no target figure anywhere in the gauge", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    const svg = screen.getByTestId("gauge").querySelector("svg");
    expect(svg?.textContent).toContain("30"); // the measured value is still served
    expect(svg?.textContent).not.toContain("50"); // the objective nobody chose is not
  });

  it("does not reassert the deleted objective in the rendered comment", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    const comment = screen.getByTestId("composition-comment");
    expect(comment).not.toHaveTextContent("objectif de 50");
    expect(comment).toHaveTextContent("Aucun objectif de CPA");
  });

  it("renders table with source rows from block.data (including Autres row)", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByTestId("data-table")).toBeInTheDocument();
    // Fixture now includes 4 rows: google-ads, meta-ads, organic, Autres.
    expect(screen.getAllByTestId("data-table-row").length).toBe(4);
  });

  it("renders comment from block.data.text", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByTestId("composition-comment")).toHaveTextContent("Google Ads");
  });

  it("renders feedback chrome", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByTestId("card-feedback-bar")).toBeInTheDocument();
  });
});

describe("Conversions card — empty/malformed block.data renders empty state", () => {
  it("null gauge value -> gauge-empty (zero conversions edge case)", () => {
    const env: CardEnvelope = {
      ...FIXTURE_ENVELOPE,
      data: {
        ...FIXTURE_ENVELOPE.data,
        composition: [
          {
            type: "gauge",
            binding: {},
            data: { value: null, target: 50, target_source: "binding", unit: "EUR", direction: "down_good", label: "CPA" },
          },
        ],
      },
    };
    render(<App envelope={env} />);
    expect(screen.getByTestId("gauge-empty")).toBeInTheDocument();
  });

  it("empty donut slices -> donut-empty (no source data)", () => {
    const env: CardEnvelope = {
      ...FIXTURE_ENVELOPE,
      data: {
        ...FIXTURE_ENVELOPE.data,
        composition: [
          { type: "donut", binding: {}, data: { total: 0, dimension: null, slices: [] } },
        ],
      },
    };
    render(<App envelope={env} />);
    expect(screen.getByTestId("donut-empty")).toBeInTheDocument();
  });

  it("empty table rows -> data-table-empty", () => {
    const env: CardEnvelope = {
      ...FIXTURE_ENVELOPE,
      data: {
        ...FIXTURE_ENVELOPE.data,
        composition: [
          { type: "table", binding: {}, data: { columns: [], rows: [] } },
        ],
      },
    };
    render(<App envelope={env} />);
    expect(screen.getByTestId("data-table-empty")).toBeInTheDocument();
  });

  it("empty kpi_row metrics -> composition-kpi-row-empty", () => {
    const env: CardEnvelope = {
      ...FIXTURE_ENVELOPE,
      data: {
        ...FIXTURE_ENVELOPE.data,
        composition: [
          { type: "kpi_row", binding: {}, data: { metrics: [] } },
        ],
      },
    };
    render(<App envelope={env} />);
    expect(screen.getByTestId("composition-kpi-row-empty")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// NEW server contract: "Autres" residual slice in donut (Epic review 2026-07-14).
// ---------------------------------------------------------------------------

describe("Conversions card — 'Autres' slice in donut (new server contract)", () => {
  it("renders 'Autres' in the donut legend when the slice is present in fixture", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    const legend = screen.getByTestId("donut-legend");
    expect(legend).toBeInTheDocument();
    // The "Autres" label must appear in the legend.
    expect(legend.textContent).toContain("Autres");
  });

  it("donut with 4 slices (including Autres) renders without crash", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByTestId("donut")).toBeInTheDocument();
    // Donut itself must not be in empty state.
    expect(screen.queryByTestId("donut-empty")).not.toBeInTheDocument();
  });
});
