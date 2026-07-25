/**
 * GA widget App smoke tests (review-global-gaps — Widget smoke tests).
 *
 * Verifies:
 *   1. Fixture envelope renders KPI tiles + heatmap without throwing.
 *   2. Malformed envelope (missing rows/data) hits the error boundary and
 *      renders the French fallback message, OR renders an empty-state gracefully.
 *
 * No-network discipline: WidgetShell does NOT fetch. html-to-image is mocked
 * via vi.mock so toPng does not attempt any DOM-to-canvas work in jsdom.
 *
 * The ErrorBoundary is tested directly with a component that throws, since
 * jsdom + React 19 error handling behaves differently from the browser.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import App from "../App";
import { ErrorBoundary } from "../ErrorBoundary";
import { FIXTURE_ENVELOPE } from "../fixture";
import type { DailyReportEnvelope } from "../types";

// ---------------------------------------------------------------------------
// Mock html-to-image (used by ExportButton inside WidgetShell)
// so toPng never runs inside jsdom.
// ---------------------------------------------------------------------------
vi.mock("html-to-image", () => ({
  toPng: vi.fn().mockResolvedValue("data:image/png;base64,fake"),
}));

// ---------------------------------------------------------------------------
// Minimal fixture with only a few rows (fast test)
// ---------------------------------------------------------------------------
const SMOKE_ENVELOPE: DailyReportEnvelope = {
  schema_version: "1",
  meta: {
    freshness: { last_pull: "2026-07-10T00:00:00Z", cadence_hours: 24 },
    provenance: [{ source_system: "google-analytics", pull_id: "pull_smoke" }],
    alerts: [],
  },
  data: {
    report_profile: "standard_daily",
    date_range: { start: "2026-07-01", end: "2026-07-10" },
    connectors: ["google-analytics"],
    rows: [
      {
        date: "2026-07-01",
        connector: "google-analytics",
        metric: "sessions",
        breakdown_dimension: "device_category",
        breakdown_value: "desktop",
        value: 1000,
        pull_id: "pull_smoke",
        loaded_at: "2026-07-10T00:00:00Z",
      },
      {
        date: "2026-07-01",
        connector: "google-analytics",
        metric: "active_users",
        breakdown_dimension: "device_category",
        breakdown_value: "desktop",
        value: 700,
        pull_id: "pull_smoke",
        loaded_at: "2026-07-10T00:00:00Z",
      },
      {
        date: "2026-07-02",
        connector: "google-analytics",
        metric: "sessions",
        breakdown_dimension: "device_category",
        breakdown_value: "desktop",
        value: 1100,
        pull_id: "pull_smoke",
        loaded_at: "2026-07-10T00:00:00Z",
      },
    ],
  },
};

// ---------------------------------------------------------------------------
// Smoke test 1: fixture envelope renders KPI tiles + heatmap without throwing
// ---------------------------------------------------------------------------

describe("GA widget App — smoke render with fixture", () => {
  it("renders without throwing using the built-in FIXTURE_ENVELOPE", () => {
    expect(() => {
      render(<App envelope={FIXTURE_ENVELOPE} />);
    }).not.toThrow();
  });

  it("renders 'Rapport quotidien' heading", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByText("Rapport quotidien")).toBeInTheDocument();
  });

  it("renders KPI tiles (sessions metric present in data)", () => {
    render(<App envelope={SMOKE_ENVELOPE} />);
    // KpiTileRow renders tile labels — at least one metric-related tile should appear.
    // The heading "Rapport quotidien" confirms the App tree mounted.
    expect(screen.getByText("Rapport quotidien")).toBeInTheDocument();
    // KPI tile for sessions should appear somewhere in the document.
    // We check by looking for "Sessions" or "sessions" text in tiles.
    const sessionsElements = screen.queryAllByText(/sessions/i);
    expect(sessionsElements.length).toBeGreaterThan(0);
  });

  it("renders CalendarHeatmap (SVG present in document)", () => {
    render(<App envelope={SMOKE_ENVELOPE} />);
    // CalendarHeatmap renders an SVG — confirm at least one is in the DOM.
    const svgs = document.querySelectorAll("svg");
    expect(svgs.length).toBeGreaterThan(0);
  });
});

// ---------------------------------------------------------------------------
// Smoke test 2: malformed envelope — missing rows — shows empty state,
// not a white screen
// ---------------------------------------------------------------------------

describe("GA widget App — malformed envelope (empty/missing rows)", () => {
  it("renders without throwing when rows is empty", () => {
    const emptyEnvelope: DailyReportEnvelope = {
      ...SMOKE_ENVELOPE,
      data: { ...SMOKE_ENVELOPE.data, rows: [] },
    };
    expect(() => {
      render(<App envelope={emptyEnvelope} />);
    }).not.toThrow();
  });

  it("shows empty state message when rows is empty", () => {
    const emptyEnvelope: DailyReportEnvelope = {
      ...SMOKE_ENVELOPE,
      data: { ...SMOKE_ENVELOPE.data, rows: [] },
    };
    render(<App envelope={emptyEnvelope} />);
    expect(
      screen.getByText(/Aucune donnée pour la sélection actuelle/i)
    ).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Smoke test 3: ErrorBoundary renders French fallback when child throws
// ---------------------------------------------------------------------------

describe("ErrorBoundary — French fallback on render error", () => {
  // Suppress React error output during this test
  const originalConsoleError = console.error;
  beforeEach(() => {
    console.error = vi.fn();
  });
  afterEach(() => {
    console.error = originalConsoleError;
  });

  it("renders French fallback when a child component throws", () => {
    function Bomb() {
      throw new Error("Intentional test error");
    }

    render(
      <ErrorBoundary>
        <Bomb />
      </ErrorBoundary>
    );

    expect(
      screen.getByText(
        "Une erreur est survenue lors de l'affichage du rapport."
      )
    ).toBeInTheDocument();
  });

  it("renders children normally when no error occurs", () => {
    render(
      <ErrorBoundary>
        <div data-testid="child-ok">OK</div>
      </ErrorBoundary>
    );
    expect(screen.getByTestId("child-ok")).toBeInTheDocument();
  });
});
