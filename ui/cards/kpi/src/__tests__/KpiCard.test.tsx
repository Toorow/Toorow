/**
 * KPI card tests (Epic 9, Stories 9.2 + 9.2b + 9.2c) — hero numbers, direction-aware delta,
 * sparkline, rendered comment, empty state, feedback chrome, French accents.
 * Story 9.2c: composition render path (CardComposition) + backward-compat fallback.
 */

import { describe, it, expect } from "vitest";
import { render, screen, within, fireEvent } from "@testing-library/react";
import App from "../App";
import KpiCardBody from "../KpiCardBody";
import { FIXTURE_ENVELOPE } from "../fixture";
import type { CardEnvelope } from "@toorow/card-shell";

// Backward-compat fixture without composition (exercises the KpiCardBody fallback path).
const FIXTURE_NO_COMPOSITION: CardEnvelope = {
  ...FIXTURE_ENVELOPE,
  data: {
    ...FIXTURE_ENVELOPE.data,
    composition: undefined,
  },
};

describe("KPI card App — fallback path (no composition)", () => {
  it("renders the title, question and hero numbers from the envelope (fallback)", () => {
    render(<App envelope={FIXTURE_NO_COMPOSITION} />);
    expect(screen.getByTestId("card-title")).toHaveTextContent("Synthèse KPI");
    // sessions hero value 42150 -> fr-FR "42 150"
    const tiles = screen.getAllByTestId("kpi-metric-tile");
    expect(tiles.length).toBe(3);
    const sessionsTile = tiles.find((t) => t.getAttribute("data-metric") === "sessions")!;
    expect(within(sessionsTile).getByTestId("kpi-hero-value").textContent).toMatch(/42\s?150/);
  });

  it("renders the deterministic cited comment (AD-9) via CardShell (fallback)", () => {
    render(<App envelope={FIXTURE_NO_COMPOSITION} />);
    const comment = screen.getByTestId("card-rendered-comment");
    expect(comment).toHaveTextContent("Contexte manquant pour cette période.");
    expect(comment).toHaveTextContent("google-analytics");
  });

  it("applies direction-aware delta color: positive up_good => success (green) (fallback)", () => {
    render(<App envelope={FIXTURE_NO_COMPOSITION} />);
    const sessionsTile = screen
      .getAllByTestId("kpi-metric-tile")
      .find((t) => t.getAttribute("data-metric") === "sessions")!;
    const deltaText = within(sessionsTile).getByTestId("kpi-delta-text");
    expect(deltaText).toHaveTextContent("+10.0 %");
    const color = getComputedStyle(deltaText).color;
    // theme success token (#3E9B6E — sage green, brand refresh)
    expect(color).toMatch(/rgb\(62,\s?155,\s?110\)/);
  });

  it("applies error tint for a negative up_good delta (fallback)", () => {
    render(<App envelope={FIXTURE_NO_COMPOSITION} />);
    const usersTile = screen
      .getAllByTestId("kpi-metric-tile")
      .find((t) => t.getAttribute("data-metric") === "active_users")!;
    const deltaText = within(usersTile).getByTestId("kpi-delta-text");
    expect(deltaText).toHaveTextContent("-2.0 %");
    const color = getComputedStyle(deltaText).color;
    // theme error token (#D64550 — raspberry, brand refresh)
    expect(color).toMatch(/rgb\(214,\s?69,\s?80\)/);
  });

  it("renders a sparkline per metric with >=2 points (fallback)", () => {
    render(<App envelope={FIXTURE_NO_COMPOSITION} />);
    expect(screen.getAllByTestId("sparkline").length).toBe(3);
  });

  it("renders the definitions popover from metric_definitions (R6, fallback)", () => {
    render(<App envelope={FIXTURE_NO_COMPOSITION} />);
    expect(screen.getByTestId("card-definitions-toggle")).toBeInTheDocument();
  });

  it("renders a designed empty state (never blank) when there are no metrics", () => {
    render(<KpiCardBody metrics={{}} series={{}} />);
    expect(screen.getByTestId("kpi-empty-state")).toHaveTextContent(
      "Aucune donnée disponible",
    );
  });

  it("handles a partial envelope (a metric missing its series) without crashing (fallback)", () => {
    const partial: CardEnvelope = {
      ...FIXTURE_NO_COMPOSITION,
      data: {
        ...FIXTURE_NO_COMPOSITION.data,
        series: { sessions: FIXTURE_ENVELOPE.data.series.sessions },
      },
    };
    render(<App envelope={partial} />);
    // 3 metrics still render; the ones without series show the empty sparkline.
    expect(screen.getAllByTestId("kpi-metric-tile").length).toBe(3);
    expect(screen.getAllByTestId("sparkline-empty").length).toBe(2);
  });

  it("renders the feedback chrome row from the CardShell (Story 9.2b, fallback)", () => {
    render(<App envelope={FIXTURE_NO_COMPOSITION} />);
    expect(screen.getByTestId("card-chrome-row")).toBeInTheDocument();
    expect(screen.getByTestId("card-feedback-bar")).toBeInTheDocument();
    expect(screen.getByTestId("card-export-button")).toBeInTheDocument();
  });

  it("feedback thumbs-up enables the Envoyer button (Story 9.2b, fallback)", () => {
    render(<App envelope={FIXTURE_NO_COMPOSITION} />);
    fireEvent.click(screen.getByTestId("card-feedback-thumbs-up"));
    expect(screen.getByTestId("card-feedback-submit")).not.toBeDisabled();
  });
});

// ---------------------------------------------------------------------------
// Story 9.2c: CardComposition render path (when data.composition is present)
// ---------------------------------------------------------------------------

describe("KPI card App — composition path (Story 9.2c)", () => {
  it("renders via CardComposition when data.composition is present", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByTestId("card-title")).toHaveTextContent("Synthèse KPI");
    // CardComposition renders kpi_row block with composition-kpi-tile testids
    expect(screen.getByTestId("card-composition")).toBeInTheDocument();
    expect(screen.getByTestId("composition-block-kpi_row")).toBeInTheDocument();
  });

  it("renders hero KPI values via composition path", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    const tiles = screen.getAllByTestId("composition-kpi-tile");
    expect(tiles.length).toBe(3);
    const sessionsTile = tiles.find((t) => t.getAttribute("data-metric") === "sessions")!;
    expect(within(sessionsTile).getByTestId("composition-kpi-value").textContent).toMatch(/42\s?150/);
  });

  it("renders the comment block via CardComposition", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByTestId("composition-block-comment")).toBeInTheDocument();
    expect(screen.getByTestId("composition-comment")).toBeInTheDocument();
    expect(screen.getByTestId("composition-comment")).toHaveTextContent("Sessions");
  });

  it("renders delta in composition tiles", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    const deltas = screen.getAllByTestId("composition-kpi-delta");
    const texts = deltas.map((d) => d.textContent);
    // Delta has + sign and percent sign (locale-agnostic: fr-FR uses comma, test env may differ)
    expect(texts.some((t) => t?.includes("+") && t?.includes("9") && t?.includes("%"))).toBe(true);
  });

  it("CardShell feedback chrome still works in composition mode (Story 9.2b)", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByTestId("card-chrome-row")).toBeInTheDocument();
    expect(screen.getByTestId("card-feedback-bar")).toBeInTheDocument();
  });
});
