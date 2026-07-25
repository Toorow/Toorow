/**
 * User types card tests (Epic 9, Story 9.5 / 10.2).
 * - Renders fixture without throwing.
 * - kpi_row, donut(user_type LEAD), donut(device), bar, table, comment all render.
 * - Story 10.2: user_type donut renders as LEAD with FR labels (Nouveaux/Fidèles/Indéterminés).
 * - Story 10.2: empty state when user_type block has no slices (degrade path, AC 3).
 * - Empty/malformed blocks render the primitive's designed empty state.
 */

import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import App from "../App";
import { FIXTURE_ENVELOPE } from "../fixture";
import type { CardEnvelope } from "@toorow/card-shell";


describe("User types card — fixture renders without throwing", () => {
  it("renders the card title", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByTestId("card-title")).toHaveTextContent("Types d'utilisateurs");
  });

  it("renders kpi_row with active_users and sessions from block.data", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    const tiles = screen.getAllByTestId("composition-kpi-tile");
    expect(tiles.length).toBe(2);
    const usersTile = tiles.find((t) => t.getAttribute("data-metric") === "active_users")!;
    expect(usersTile).toBeInTheDocument();
    // Real payload (post rollup fix): active_users = 221102 -> fr-FR "221 102".
    expect(usersTile.querySelector("[data-testid='composition-kpi-value']")?.textContent).toMatch(/221/);
  });

  // Story 10.2: user_type donut leads the card with FR labels (AC 1+2).
  // The fixture has two donuts: user_type (lead) then device_category (below).
  it("renders the new/returning donut as LEAD block with FR labels (Story 10.2)", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    const donuts = screen.getAllByTestId("composition-block-donut");
    // First donut block is the user_type lead.
    expect(donuts.length).toBeGreaterThanOrEqual(2);
    // The first donut legend must contain FR user_type labels.
    const firstLegend = donuts[0].querySelector("[data-testid='donut-legend']");
    expect(firstLegend).toBeTruthy();
    expect(firstLegend?.textContent).toContain("Fidèles");
    expect(firstLegend?.textContent).toContain("Nouveaux");
    expect(firstLegend?.textContent).toContain("Indéterminés");
  });

  it("renders donut chart for the device split as SECONDARY block (Story 10.2)", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    const donuts = screen.getAllByTestId("composition-block-donut");
    expect(donuts.length).toBeGreaterThanOrEqual(2);
    // Second donut block is the device split. device_category values are raw
    // (no FR label map — only user_type gets one), so labels are lowercase.
    const secondLegend = donuts[1].querySelector("[data-testid='donut-legend']");
    expect(secondLegend).toBeTruthy();
    expect(secondLegend?.textContent).toContain("mobile");
    expect(secondLegend?.textContent).toContain("desktop");
  });

  it("renders bar chart (country split) from block.data.bars", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    const chart = screen.getByTestId("bar-chart");
    expect(chart).toBeInTheDocument();
    // Real payload: country values are raw ISO codes (FR, DE, ...).
    expect(chart.textContent).toContain("FR");
  });

  it("renders table with device rows from block.data", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByTestId("data-table")).toBeInTheDocument();
    expect(screen.getAllByTestId("data-table-row").length).toBe(3);
  });

  it("renders comment from block.data.text", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    // Story 10.2: the comment now leads with the dominant user_type segment.
    expect(screen.getByTestId("composition-comment")).toHaveTextContent("Fidèles");
  });

  it("renders feedback chrome", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByTestId("card-feedback-bar")).toBeInTheDocument();
  });
});


describe("User types card — Story 10.2 degrade: empty user_type block (AC 3)", () => {
  /**
   * When the user_type_daily datastream is off (no user_type rows in the mart),
   * the server returns an empty user_type donut (slices: []). The widget must:
   *   - render donut-empty for the user_type block (designed empty state).
   *   - still render the device donut normally below it.
   */
  it("renders donut-empty for user_type block when slices are absent (degrade path)", () => {
    const env: CardEnvelope = {
      ...FIXTURE_ENVELOPE,
      data: {
        ...FIXTURE_ENVELOPE.data,
        composition: [
          // user_type donut: empty (datastream off / not yet pulled).
          {
            type: "donut",
            binding: { metrics: "active_users", dimensions: ["user_type"] },
            title: "Nouveaux vs fidèles",
            data: { total: 0, dimension: null, slices: [] },
          },
          // Device donut: present (required dimension, always rendered).
          {
            type: "donut",
            binding: { metrics: "active_users", dimensions: ["device_category"] },
            title: "Utilisateurs par appareil",
            data: {
              total: 31820,
              dimension: "device_category",
              slices: [
                { label: "Mobile",  value: 19092, pct: 60.0 },
                { label: "Desktop", value: 9546,  pct: 30.0 },
              ],
            },
          },
        ],
      },
    };
    render(<App envelope={env} />);

    // The user_type donut renders its designed empty state.
    expect(screen.getByTestId("donut-empty")).toBeInTheDocument();
    // The device donut renders normally with its legend.
    const donuts = screen.getAllByTestId("composition-block-donut");
    expect(donuts.length).toBe(2);
    const deviceLegend = donuts[1].querySelector("[data-testid='donut-legend']");
    expect(deviceLegend).toBeTruthy();
    expect(deviceLegend?.textContent).toContain("Mobile");
  });
});


describe("User types card — empty/malformed block.data renders empty state", () => {
  it("empty donut slices -> donut-empty", () => {
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

  it("empty bar.bars -> bar-chart-empty", () => {
    const env: CardEnvelope = {
      ...FIXTURE_ENVELOPE,
      data: {
        ...FIXTURE_ENVELOPE.data,
        composition: [
          { type: "bar", binding: {}, data: { orientation: "horizontal", dimension: null, bars: [] } },
        ],
      },
    };
    render(<App envelope={env} />);
    expect(screen.getByTestId("bar-chart-empty")).toBeInTheDocument();
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

  it("block with no data -> primitive empty state", () => {
    const env: CardEnvelope = {
      ...FIXTURE_ENVELOPE,
      data: {
        ...FIXTURE_ENVELOPE.data,
        composition: [
          { type: "funnel", binding: {} },
        ],
      },
    };
    render(<App envelope={env} />);
    expect(screen.getByTestId("funnel-empty")).toBeInTheDocument();
  });
});
