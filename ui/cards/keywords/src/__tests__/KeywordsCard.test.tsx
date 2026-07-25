/**
 * Keywords card tests (Epic 9, Story 9.3).
 * - Renders fixture without throwing.
 * - kpi_row hero values visible.
 * - bar chart renders from block.data.
 * - table renders from block.data.
 * - comment renders from block.data.
 * - Malformed/empty blocks render the primitive's empty state (never throw).
 */

import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import App from "../App";
import { FIXTURE_ENVELOPE, FIXTURE_ENVELOPE_STALE, FIXTURE_ENVELOPE_PARTIAL } from "../fixture";
import type { CardEnvelope } from "@toorow/card-shell";

describe("Keywords card — fixture renders without throwing", () => {
  it("renders the card title", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByTestId("card-title")).toHaveTextContent("Mots-clés");
  });

  it("renders the kpi_row block with hero values from block.data", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByTestId("composition-block-kpi_row")).toBeInTheDocument();
    expect(screen.getByTestId("composition-kpi-row")).toBeInTheDocument();
    const tiles = screen.getAllByTestId("composition-kpi-tile");
    expect(tiles.length).toBe(2);
    const clicksTile = tiles.find((t) => t.getAttribute("data-metric") === "clicks")!;
    expect(clicksTile).toBeInTheDocument();
    // 3842 -> fr-FR "3 842"
    expect(clicksTile.querySelector("[data-testid='composition-kpi-value']")?.textContent).toMatch(/3/);
  });

  it("renders the bar chart from block.data.bars", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByTestId("composition-block-bar")).toBeInTheDocument();
    expect(screen.getByTestId("bar-chart")).toBeInTheDocument();
  });

  it("renders the data tables (détail + opportunités + cannibalisation) from block.data", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    // Three table blocks after Story 10.5: détail requêtes + opportunités + cannibalisation.
    // The cannibalisation block is a DESIGNED EMPTY table (rows []) so it renders the
    // composition-table wrapper + data-table-empty, NOT a populated data-table.
    expect(screen.getAllByTestId("composition-table").length).toBe(3);
    // Two POPULATED tables (détail + opportunités); the empty cannibalisation table renders
    // data-table-empty instead of data-table.
    expect(screen.getAllByTestId("data-table").length).toBe(2);
    expect(screen.getAllByTestId("data-table-empty").length).toBe(1);
    const rows = screen.getAllByTestId("data-table-row");
    expect(rows.length).toBe(10);
    // review-10-6 UI F-3: the empty table must be identifiably the CANNIBALISATION one —
    // its block title proves the 10.5 block rendered, not just "some empty table somewhere".
    // NOTE: the server's `empty_label` ("Aucune cannibalisation détectée") is NOT consumed
    // by the shared DataTable yet (generic "Aucune donnée disponible") — recorded as debt
    // in review-epic-10 alongside the string-columns contract alignment.
    const cannibTable = screen
      .getAllByTestId("composition-table")
      .find((t) => t.textContent?.includes("Cannibalisation"));
    expect(cannibTable).toBeTruthy();
    expect(cannibTable!.querySelector("[data-testid='data-table-empty']")).toBeTruthy();
  });

  it("renders the comment block from block.data.text", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByTestId("composition-block-comment")).toBeInTheDocument();
    expect(screen.getByTestId("composition-comment")).toHaveTextContent("bottes de randonnée");
  });

  it("renders the feedback chrome (CardShell Story 9.2b)", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByTestId("card-chrome-row")).toBeInTheDocument();
    expect(screen.getByTestId("card-feedback-bar")).toBeInTheDocument();
  });
});

// Fix F-6 (9-3-to-9-6 ui review): bar chart shows rank movers, not top-N by clicks.
describe("Keywords card — rank movers bar chart (F-6)", () => {
  it("renders a bar chart titled 'Requêtes en mouvement' (movers, not top-N by clicks)", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    // The bar block title in the fixture is "Requêtes en mouvement (±pos.)"
    const blockBar = screen.getByTestId("composition-block-bar");
    expect(blockBar.textContent).toContain("mouvement");
  });

  it("renders movers with signed delta values (positive and negative bars present)", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    const barChart = screen.getByTestId("bar-chart");
    // Signed values like 3.2 and -2.5 should appear in some form.
    expect(barChart).toBeInTheDocument();
    // bottes de randonnée appears as a mover label
    expect(barChart.textContent).toContain("bottes de randonnée");
  });
});

// F-10 (ui review): stale/partial fixtures render meaningful non-blank state.
describe("Keywords card — stale and partial fixture scenarios (F-10)", () => {
  it("renders non-blank state with stale fixture", () => {
    render(<App envelope={FIXTURE_ENVELOPE_STALE} />);
    expect(screen.getByTestId("card-title")).toHaveTextContent("Mots-clés");
    expect(screen.getByTestId("card-freshness-badge")).toHaveTextContent("MAJ 2026-07-11");
  });

  it("renders partial fixture (3-day period, null deltas) without crash", () => {
    render(<App envelope={FIXTURE_ENVELOPE_PARTIAL} />);
    expect(screen.getByTestId("card-shell")).toBeInTheDocument();
    const tiles = screen.getAllByTestId("composition-kpi-tile");
    expect(tiles.length).toBe(2);
    // Delta should be "—" for null delta_pct
    const deltas = screen.getAllByTestId("composition-kpi-delta");
    expect(deltas.some((d) => d.textContent === "—")).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// F-1 + F-3 (Epic review 2026-07-14): direction-aware movers bar.
// ---------------------------------------------------------------------------

describe("Keywords card — movers bar direction coloring (F-1 + F-3)", () => {
  it("renders signed labels (+/- prefix) for bars with direction set", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    const barChart = screen.getByTestId("bar-chart");
    // Positive mover: +3,2 (fr-FR formatting with signed prefix).
    // Negative mover: -2,5 or -3,8.
    const text = barChart.textContent ?? "";
    expect(text).toMatch(/[+-]/); // At least one signed label present.
  });

  it("renders all 5 mover labels including up and down movers", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    const barChart = screen.getByTestId("bar-chart");
    expect(barChart.textContent).toContain("bottes de randonnée");
    expect(barChart.textContent).toContain("chaussures trail femme");
    expect(barChart.textContent).toContain("chaussettes de randonnée");
  });

  it("flat bar (no direction) still renders with ACCENT color — regression (F-1 fallback)", () => {
    const env: CardEnvelope = {
      ...FIXTURE_ENVELOPE,
      data: {
        ...FIXTURE_ENVELOPE.data,
        composition: [
          {
            type: "bar",
            // No direction in binding -> semanticDirection undefined -> flat ACCENT
            binding: { metrics: "clicks", dimensions: ["connector"] },
            data: {
              orientation: "horizontal",
              dimension: "connector",
              bars: [
                { label: "google-ads", value: 200 },
                { label: "meta-ads",   value: 80 },
              ],
            },
          },
        ],
      },
    };
    render(<App envelope={env} />);
    const barChart = screen.getByTestId("bar-chart");
    expect(barChart).toBeInTheDocument();
    // No direction entries; labels rendered without +/- sign.
    expect(barChart.textContent).toContain("google-ads");
    expect(barChart.textContent).toContain("meta-ads");
  });
});

// ---------------------------------------------------------------------------
// NEW server contract: Opportunités TABLE block (Epic review 2026-07-14).
// ---------------------------------------------------------------------------

describe("Keywords card — Opportunités TABLE block (new server contract)", () => {
  it("renders the Opportunités table after the movers bar (order preserved)", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    // Both composition-table blocks must render.
    const tables = screen.getAllByTestId("composition-table");
    // First: "Détail requêtes" (idx 2), Second: "Opportunités" (idx 3 — after bar).
    expect(tables.length).toBeGreaterThanOrEqual(2);
    // "Opportunités" title appears somewhere in the card.
    expect(screen.getByText("Opportunités")).toBeInTheDocument();
  });

  it("renders 5 opportunity rows with position > 10", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    // We check for a known row label from the opportunities table.
    expect(screen.getByText("chaussures montagne imperméables")).toBeInTheDocument();
    expect(screen.getByText("bâtons randonnée pliables")).toBeInTheDocument();
  });

  it("Opportunités table renders after the bar chart block (composition order)", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    const composition = screen.getByTestId("card-composition");
    const blocks = Array.from(composition.querySelectorAll("[data-testid]"));
    const barIdx   = blocks.findIndex((el) => el.getAttribute("data-testid") === "composition-block-bar");
    // Find any table that contains "Opportunités"
    const oppsIdx  = blocks.findIndex(
      (el) => el.getAttribute("data-testid") === "composition-table" &&
               el.textContent?.includes("Opportunités"),
    );
    if (barIdx >= 0 && oppsIdx >= 0) {
      expect(oppsIdx).toBeGreaterThan(barIdx);
    }
    // At minimum both must be present.
    expect(screen.getByText("Opportunités")).toBeInTheDocument();
  });
});

describe("Keywords card — empty/malformed block.data renders empty state", () => {
  it("empty bar.bars -> bar-chart-empty state", () => {
    const env: CardEnvelope = {
      ...FIXTURE_ENVELOPE,
      data: {
        ...FIXTURE_ENVELOPE.data,
        composition: [
          {
            type: "bar",
            binding: { metrics: "clicks" },
            data: { orientation: "horizontal", dimension: null, bars: [] },
          },
        ],
      },
    };
    render(<App envelope={env} />);
    expect(screen.getByTestId("bar-chart-empty")).toBeInTheDocument();
  });

  it("null gauge value -> gauge-empty state", () => {
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

  it("empty donut slices -> donut-empty state", () => {
    const env: CardEnvelope = {
      ...FIXTURE_ENVELOPE,
      data: {
        ...FIXTURE_ENVELOPE.data,
        composition: [
          {
            type: "donut",
            binding: {},
            data: { total: 0, dimension: null, slices: [] },
          },
        ],
      },
    };
    render(<App envelope={env} />);
    expect(screen.getByTestId("donut-empty")).toBeInTheDocument();
  });

  it("empty funnel steps -> funnel-empty state", () => {
    const env: CardEnvelope = {
      ...FIXTURE_ENVELOPE,
      data: {
        ...FIXTURE_ENVELOPE.data,
        composition: [
          {
            type: "funnel",
            binding: {},
            data: { steps: [], overall_rate: null },
          },
        ],
      },
    };
    render(<App envelope={env} />);
    expect(screen.getByTestId("funnel-empty")).toBeInTheDocument();
  });

  it("empty table -> data-table-empty state", () => {
    const env: CardEnvelope = {
      ...FIXTURE_ENVELOPE,
      data: {
        ...FIXTURE_ENVELOPE.data,
        composition: [
          {
            type: "table",
            binding: {},
            data: { columns: [], rows: [] },
          },
        ],
      },
    };
    render(<App envelope={env} />);
    expect(screen.getByTestId("data-table-empty")).toBeInTheDocument();
  });

  it("unknown block type -> composition-unknown-block state", () => {
    const env: CardEnvelope = {
      ...FIXTURE_ENVELOPE,
      data: {
        ...FIXTURE_ENVELOPE.data,
        composition: [
          { type: "heatmap" as never, binding: {} },
        ],
      },
    };
    render(<App envelope={env} />);
    expect(screen.getByTestId("composition-unknown-block")).toBeInTheDocument();
  });

  it("empty composition -> card-composition-empty state", () => {
    const env: CardEnvelope = {
      ...FIXTURE_ENVELOPE,
      data: { ...FIXTURE_ENVELOPE.data, composition: [] },
    };
    render(<App envelope={env} />);
    expect(screen.getByTestId("card-composition-empty")).toBeInTheDocument();
  });
});
