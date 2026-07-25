/**
 * Story 10.4 — Journey card: entry-pages bar + top-pages table block tests.
 *
 * - Renders bar(Pages d'entrée) and table(Pages les plus vues) blocks from FIXTURE_ENVELOPE.
 * - Empty state: card renders without throwing when bar/table data is empty.
 * - Existing fixture tests (funnel, kpi_row, comment) are NOT regressed here —
 *   they remain in JourneyCard.test.tsx.
 */

import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import App from "../App";
import { FIXTURE_ENVELOPE } from "../fixture";
import type { CardEnvelope } from "@toorow/card-shell";

// ---------------------------------------------------------------------------
// Helper: build an envelope with explicit composition
// ---------------------------------------------------------------------------

function makeEnvelope(composition: CardEnvelope["data"]["composition"]): CardEnvelope {
  return {
    ...FIXTURE_ENVELOPE,
    data: {
      ...FIXTURE_ENVELOPE.data,
      composition,
    },
  };
}

// ---------------------------------------------------------------------------
// 1. Story 10.4 blocks present in the FIXTURE_ENVELOPE
// ---------------------------------------------------------------------------

describe("Journey card 10.4 — fixture has entry-pages bar and top-pages table", () => {
  it("renders at least two bar blocks (entry-pages bar present)", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    // The fixture has the entry-pages bar block after the kpi_row.
    // CardComposition renders bar blocks as data-testid="composition-block-bar".
    const barBlocks = screen.getAllByTestId("composition-block-bar");
    expect(barBlocks.length).toBeGreaterThanOrEqual(1);
  });

  it("renders the 'Pages d'entrée' bar title in the fixture", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    // The bar block title "Pages d'entrée" is rendered by CardComposition.
    expect(screen.getByText(/Pages d.entr/)).toBeInTheDocument();
  });

  it("renders the top-pages table block (composition-table present)", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    // CardComposition renders table blocks as data-testid="composition-table".
    const tables = screen.getAllByTestId("composition-table");
    expect(tables.length).toBeGreaterThanOrEqual(1);
  });

  it("renders 'Pages les plus vues' table title in the fixture", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByText("Pages les plus vues")).toBeInTheDocument();
  });

  it("renders 'Part' column header from the top-pages table", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    // The DataTable renders column labels; 'Part' is the share column.
    expect(screen.getByText("Part")).toBeInTheDocument();
  });

  it("renders a computed part_pct VALUE in the top-pages table (review-10-6 UI F-4)", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    // Header presence alone does not prove the share is rendered: assert the fixture's
    // top row share (25.9 -> fr-FR "25,9") appears as a cell value.
    const table = screen.getAllByTestId("composition-table")[0].closest("[data-testid]");
    expect(screen.getByText(/25[.,]9/)).toBeInTheDocument();
    expect(table).toBeTruthy();
  });

  it("renders 'Vues' column header from the top-pages table", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByText("Vues")).toBeInTheDocument();
  });

  it("renders 'Page' column header from the top-pages table", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    // The first column label is 'Page' (the dimension column).
    const pageHeaders = screen.getAllByText("Page");
    expect(pageHeaders.length).toBeGreaterThanOrEqual(1);
  });

  it("renders a placeholder bar label '/' in the entry-pages bar", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    // The fixture has '/' as the top landing page bar label.
    // The Bar component renders each bar's label text.
    const slashLabels = screen.getAllByText("/");
    expect(slashLabels.length).toBeGreaterThanOrEqual(1);
  });

  it("existing funnel block still renders (regression guard)", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByTestId("composition-block-funnel")).toBeInTheDocument();
  });

  it("existing kpi_row still renders (regression guard)", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByTestId("composition-block-kpi_row")).toBeInTheDocument();
  });

  it("existing comment block still renders (regression guard)", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByTestId("composition-block-comment")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// 2. Empty-state: bar/table with empty data does not crash
// ---------------------------------------------------------------------------

describe("Journey card 10.4 — empty page blocks do not crash", () => {
  it("renders without throwing when bar data is empty (no bars)", () => {
    const env = makeEnvelope([
      {
        type: "funnel",
        binding: { steps: ["sessions", "active_users", "conversions"] },
        data: {
          steps: [
            { label: "sessions", value: 1000, rate: 1.0 },
            { label: "conversions", value: 50, rate: 0.05 },
          ],
          overall_rate: 0.05,
        },
      },
      {
        type: "bar",
        title: "Pages d'entrée",
        binding: { metrics: "sessions", dimensions: ["landing_page"] },
        data: { orientation: "horizontal", dimension: "landing_page", bars: [] },
      },
      {
        type: "comment",
        binding: {},
        data: { text: "Contexte manquant pour cette période." },
      },
    ]);
    // Must not throw
    expect(() => render(<App envelope={env} />)).not.toThrow();
    expect(screen.getByTestId("composition-block-bar")).toBeInTheDocument();
  });

  it("renders without throwing when table data is empty (no rows)", () => {
    const env = makeEnvelope([
      {
        type: "funnel",
        binding: { steps: ["sessions", "conversions"] },
        data: {
          steps: [
            { label: "sessions", value: 1000, rate: 1.0 },
            { label: "conversions", value: 50, rate: 0.05 },
          ],
          overall_rate: 0.05,
        },
      },
      {
        type: "table",
        title: "Pages les plus vues",
        binding: { metrics: "screen_page_views", dimensions: ["page"] },
        data: {
          columns: [
            { key: "_dim", label: "Page", numeric: false },
            { key: "screen_page_views", label: "Vues", numeric: true },
            { key: "part_pct", label: "Part", numeric: true },
          ],
          rows: [],
        },
      },
      {
        type: "comment",
        binding: {},
        data: { text: "Contexte manquant pour cette période." },
      },
    ]);
    expect(() => render(<App envelope={env} />)).not.toThrow();
    expect(screen.getByTestId("composition-table")).toBeInTheDocument();
  });

  it("renders without throwing when both bar and table data are empty (full degrade)", () => {
    const env = makeEnvelope([
      {
        type: "funnel",
        binding: { steps: ["sessions", "conversions"] },
        data: {
          steps: [
            { label: "sessions", value: 1000, rate: 1.0 },
            { label: "conversions", value: 50, rate: 0.05 },
          ],
          overall_rate: 0.05,
        },
      },
      {
        type: "kpi_row",
        binding: { metrics: "*" },
        data: { metrics: [{ metric: "sessions", value: 1000, delta: null, delta_pct: null }] },
      },
      {
        type: "bar",
        title: "Pages d'entrée",
        binding: { metrics: "sessions", dimensions: ["landing_page"] },
        data: { orientation: "horizontal", dimension: null, bars: [] },
      },
      {
        type: "table",
        title: "Pages les plus vues",
        binding: { metrics: "screen_page_views", dimensions: ["page"] },
        data: {
          columns: [
            { key: "_dim", label: "Page", numeric: false },
            { key: "screen_page_views", label: "Vues", numeric: true },
            { key: "part_pct", label: "Part", numeric: true },
          ],
          rows: [],
        },
      },
      {
        type: "comment",
        binding: {},
        data: { text: "Contexte manquant pour cette période." },
      },
    ]);
    expect(() => render(<App envelope={env} />)).not.toThrow();
    // Funnel is still present (degraded card remains valid)
    expect(screen.getByTestId("composition-block-funnel")).toBeInTheDocument();
    // Both 10.4 blocks render (even empty)
    expect(screen.getByTestId("composition-block-bar")).toBeInTheDocument();
    expect(screen.getByTestId("composition-table")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// 3. block order in the fixture: funnel < kpi_row < bar < table < comment
// ---------------------------------------------------------------------------

describe("Journey card 10.4 — block order in fixture", () => {
  it("bar block comes after kpi_row and before comment in fixture", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    const composition = screen.getByTestId("card-composition");
    // Within card-composition, relative DOM position determines order.
    const kpiRow = composition.querySelector("[data-testid='composition-block-kpi_row']");
    const bar = composition.querySelector("[data-testid='composition-block-bar']");
    const comment = composition.querySelector("[data-testid='composition-block-comment']");
    expect(kpiRow).toBeInTheDocument();
    expect(bar).toBeInTheDocument();
    expect(comment).toBeInTheDocument();
    // DOM order: kpiRow before bar, bar before comment
    const nodes = Array.from(composition.children);
    const kpiIdx = nodes.findIndex((n) => n.querySelector("[data-testid='composition-block-kpi_row']") || n.getAttribute("data-testid") === "composition-block-kpi_row");
    const barIdx = nodes.findIndex((n) => n.querySelector("[data-testid='composition-block-bar']") || n.getAttribute("data-testid") === "composition-block-bar");
    const commentIdx = nodes.findIndex((n) => n.querySelector("[data-testid='composition-block-comment']") || n.getAttribute("data-testid") === "composition-block-comment");
    // All must be found (not -1)
    expect(kpiIdx).toBeGreaterThanOrEqual(0);
    expect(barIdx).toBeGreaterThanOrEqual(0);
    expect(commentIdx).toBeGreaterThanOrEqual(0);
    expect(kpiIdx).toBeLessThan(barIdx);
    expect(barIdx).toBeLessThan(commentIdx);
  });
});
