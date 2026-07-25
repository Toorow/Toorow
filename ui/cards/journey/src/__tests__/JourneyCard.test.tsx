/**
 * Journey card tests (Epic 9, Story 9.6).
 * - Renders fixture without throwing.
 * - funnel, kpi_row, comment all render from block.data.
 * - Pass-through rate indicators visible.
 * - Empty/malformed blocks render the primitive's designed empty state.
 */

import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import App from "../App";
import { FIXTURE_ENVELOPE, FIXTURE_ENVELOPE_STALE, FIXTURE_ENVELOPE_PARTIAL } from "../fixture";
import type { CardEnvelope } from "@toorow/card-shell";

describe("Journey card — fixture renders without throwing", () => {
  it("renders the card title", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByTestId("card-title")).toHaveTextContent("Parcours utilisateur");
  });

  it("renders the funnel block with its stages from block.data.steps", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByTestId("composition-block-funnel")).toBeInTheDocument();
    expect(screen.getByTestId("funnel")).toBeInTheDocument();
    // Real payload: 2 steps (sessions -> conversions) -> 1 through-rate indicator.
    const throughRates = screen.getAllByTestId(/funnel-through-rate/);
    expect(throughRates.length).toBe(1);
  });

  it("renders pass-through rates between funnel stages", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    // Rate from sessions -> active_users is ~75.5% -> rounded to 75 or 76%
    const rate1 = screen.getByTestId("funnel-through-rate-1");
    expect(rate1.textContent).toMatch(/%/);
  });

  it("renders kpi_row with sessions and conversions from block.data", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    const tiles = screen.getAllByTestId("composition-kpi-tile");
    // Real payload: kpi_row binds "*" -> sessions, conversions, screen_page_views.
    expect(tiles.length).toBe(3);
    const sessionsTile = tiles.find((t) => t.getAttribute("data-metric") === "sessions")!;
    expect(sessionsTile).toBeInTheDocument();
    // Real payload (post rollup fix): sessions = 268981 -> fr-FR "268 981".
    expect(sessionsTile.querySelector("[data-testid='composition-kpi-value']")?.textContent).toMatch(/268/);
  });

  it("renders comment from block.data.text", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByTestId("composition-comment")).toHaveTextContent("Décroché principal");
  });

  it("renders feedback chrome", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByTestId("card-feedback-bar")).toBeInTheDocument();
  });
});

// Fix F-3 (9-3-to-9-6 ui review): overall_rate must render as NumberHero headline.
describe("Journey card — overall_rate NumberHero (F-3)", () => {
  it("renders the overall conversion rate hero (3.3 %) from fixture overall_rate=0.0326", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    const hero = screen.getByTestId("funnel-overall-rate");
    expect(hero).toBeInTheDocument();
    expect(hero).toHaveTextContent("Taux de conversion global");
    // 3.0 % in fr-FR
    const value = screen.getByTestId("funnel-overall-rate-value");
    expect(value.textContent).toMatch(/3/);
    expect(value.textContent).toMatch(/%/);
  });

  it("does NOT render the rate hero when overall_rate is null (partial fixture)", () => {
    render(<App envelope={FIXTURE_ENVELOPE_PARTIAL} />);
    expect(screen.queryByTestId("funnel-overall-rate")).not.toBeInTheDocument();
  });
});

// F-10 (ui review): stale/partial fixtures — card renders meaningful non-blank state.
describe("Journey card — stale and partial fixture scenarios (F-10)", () => {
  it("renders non-blank state with stale fixture (freshness badge visible)", () => {
    render(<App envelope={FIXTURE_ENVELOPE_STALE} />);
    expect(screen.getByTestId("card-shell")).toBeInTheDocument();
    expect(screen.getByTestId("card-title")).toHaveTextContent("Parcours utilisateur");
    // Freshness badge from MAJ 2026-07-11
    expect(screen.getByTestId("card-freshness-badge")).toHaveTextContent("MAJ 2026-07-11");
  });

  it("renders partial fixture without crash (single funnel step, null overall_rate)", () => {
    render(<App envelope={FIXTURE_ENVELOPE_PARTIAL} />);
    // The funnel with 1 step renders but without a through-rate indicator.
    expect(screen.queryByTestId("funnel-through-rate-1")).not.toBeInTheDocument();
    expect(screen.queryByTestId("funnel-overall-rate")).not.toBeInTheDocument();
  });
});

describe("Journey card — empty/malformed block.data renders empty state", () => {
  it("empty funnel steps -> funnel-empty state", () => {
    const env: CardEnvelope = {
      ...FIXTURE_ENVELOPE,
      data: {
        ...FIXTURE_ENVELOPE.data,
        composition: [
          { type: "funnel", binding: {}, data: { steps: [], overall_rate: null } },
        ],
      },
    };
    render(<App envelope={env} />);
    expect(screen.getByTestId("funnel-empty")).toBeInTheDocument();
  });

  it("funnel block with no data -> funnel-empty state", () => {
    const env: CardEnvelope = {
      ...FIXTURE_ENVELOPE,
      data: {
        ...FIXTURE_ENVELOPE.data,
        composition: [
          { type: "funnel", binding: { steps: ["sessions", "conversions"] } },
        ],
      },
    };
    render(<App envelope={env} />);
    expect(screen.getByTestId("funnel-empty")).toBeInTheDocument();
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

  it("empty comment text -> composition-comment-empty state", () => {
    const env: CardEnvelope = {
      ...FIXTURE_ENVELOPE,
      data: {
        ...FIXTURE_ENVELOPE.data,
        rendered_comment: "",
        composition: [
          { type: "comment", binding: {}, data: { text: "" } },
        ],
      },
    };
    render(<App envelope={env} />);
    expect(screen.getByTestId("composition-comment-empty")).toBeInTheDocument();
  });

  it("unknown block type -> composition-unknown-block", () => {
    const env: CardEnvelope = {
      ...FIXTURE_ENVELOPE,
      data: {
        ...FIXTURE_ENVELOPE.data,
        composition: [
          { type: "unknown_block" as never, binding: {} },
        ],
      },
    };
    render(<App envelope={env} />);
    expect(screen.getByTestId("composition-unknown-block")).toBeInTheDocument();
  });
});
