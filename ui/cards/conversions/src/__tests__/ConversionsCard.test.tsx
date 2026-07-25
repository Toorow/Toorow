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
            data: { value: null, target: 50, target_source: "default", unit: "EUR", direction: "down_good", label: "CPA" },
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
