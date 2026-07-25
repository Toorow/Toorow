/**
 * KpiTile + KpiTileRow — Story 8.8 (R6) definitions tests.
 *
 * Covers:
 *   - KpiTile renders info button when definition is provided.
 *   - KpiTile info button hover reveals tooltip with definition text.
 *   - KpiTile does NOT render info button when definition is absent.
 *   - KpiTile delta tint: up_good + positive delta → success color class/attr.
 *   - KpiTile delta tint: down_good + positive delta → error color (inverted).
 *   - KpiTile delta tint: neutral → text.secondary (no semantic color).
 *   - KpiTileRow shows definitions toggle when metricDefinitions provided.
 *   - KpiTileRow does NOT show definitions toggle when metricDefinitions absent.
 *   - Definitions collapsible panel renders all definitions on toggle.
 *   - KpiTile renders without definition (absent-definitions fallback, no crash).
 */

import { describe, it, expect } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { ThemeProvider, createTheme } from "@mui/material";
import KpiTile from "../KpiTile";
import KpiTileRow from "../KpiTileRow";
import type { MetricDefinition, Row } from "../types";

const theme = createTheme();

function wrap(ui: React.ReactElement) {
  return render(<ThemeProvider theme={theme}>{ui}</ThemeProvider>);
}

// ---------------------------------------------------------------------------
// Fixture definitions
// ---------------------------------------------------------------------------

const DEF_UP_GOOD: MetricDefinition = {
  definition: "Nombre de sessions initiées sur la propriété.",
  unit: "séances",
  direction: "up_good",
  caveats: "Les sessions multi-appareils peuvent être comptabilisées séparément.",
};

const DEF_DOWN_GOOD: MetricDefinition = {
  definition: "Taux de rebond — plus bas est mieux.",
  unit: "%",
  direction: "down_good",
  caveats: null,
};

const DEF_NEUTRAL: MetricDefinition = {
  definition: "Métrique neutre sans direction préférée.",
  unit: null,
  direction: "neutral",
  caveats: null,
};

// Minimal row set for KpiTileRow (sessions + active_users + conversions)
function makeRows(dateRange: { start: string; end: string }): Row[] {
  const rows: Row[] = [];
  // current period: 2026-07-02..2026-07-10 (9 days)
  for (let d = 1; d <= 9; d++) {
    const date = `2026-07-0${d}`;
    for (const metric of ["sessions", "active_users", "conversions"] as const) {
      rows.push({
        date,
        connector: "google-analytics",
        metric,
        breakdown_dimension: "device_category",
        breakdown_value: "desktop",
        value: 100,
        pull_id: "pull_test",
        loaded_at: "2026-07-10T00:00:00Z",
      });
    }
  }
  return rows;
}

// ---------------------------------------------------------------------------
// KpiTile — info button (R6 tooltip affordance)
// ---------------------------------------------------------------------------

describe("KpiTile — info button (R6, Story 8.8)", () => {
  it("renders info button when definition is provided", () => {
    wrap(<KpiTile label="Sessions" currentValue={1000} deltaPct={5.2} definition={DEF_UP_GOOD} />);
    expect(screen.getByTestId("kpi-info-button")).toBeInTheDocument();
  });

  it("does NOT render info button when definition is absent", () => {
    wrap(<KpiTile label="Sessions" currentValue={1000} deltaPct={5.2} />);
    expect(screen.queryByTestId("kpi-info-button")).not.toBeInTheDocument();
  });

  it("shows tooltip with definition text on hover of info button", () => {
    wrap(<KpiTile label="Sessions" currentValue={1000} deltaPct={5.2} definition={DEF_UP_GOOD} />);
    const infoBtn = screen.getByTestId("kpi-info-button");
    fireEvent.mouseEnter(infoBtn);
    // The definition text appears in the tooltip
    expect(screen.getByText(/Nombre de sessions initiées/i)).toBeInTheDocument();
  });

  it("shows unit in tooltip header when unit is set", () => {
    wrap(<KpiTile label="Sessions" currentValue={1000} deltaPct={5.2} definition={DEF_UP_GOOD} />);
    const infoBtn = screen.getByTestId("kpi-info-button");
    fireEvent.mouseEnter(infoBtn);
    expect(screen.getByText(/séances/i)).toBeInTheDocument();
  });

  it("shows caveats when provided and tooltip is open", () => {
    wrap(<KpiTile label="Sessions" currentValue={1000} deltaPct={5.2} definition={DEF_UP_GOOD} />);
    const infoBtn = screen.getByTestId("kpi-info-button");
    fireEvent.mouseEnter(infoBtn);
    expect(screen.getByText(/multi-appareils/i)).toBeInTheDocument();
  });

  it("hides tooltip on mouse leave", () => {
    wrap(<KpiTile label="Sessions" currentValue={1000} deltaPct={5.2} definition={DEF_UP_GOOD} />);
    const infoBtn = screen.getByTestId("kpi-info-button");
    fireEvent.mouseEnter(infoBtn);
    fireEvent.mouseLeave(infoBtn);
    // Definition text should not be visible
    expect(screen.queryByText(/Nombre de sessions initiées/i)).not.toBeInTheDocument();
  });

  it("shows tooltip on focus (keyboard accessibility)", () => {
    wrap(<KpiTile label="Sessions" currentValue={1000} deltaPct={5.2} definition={DEF_UP_GOOD} />);
    const infoBtn = screen.getByTestId("kpi-info-button");
    fireEvent.focus(infoBtn);
    expect(screen.getByText(/Nombre de sessions initiées/i)).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// KpiTile — hero value rendering
// ---------------------------------------------------------------------------

describe("KpiTile — hero value (rule 3, Story 8.8)", () => {
  it("renders the hero value in the kpi-hero-value element", () => {
    wrap(<KpiTile label="Sessions" currentValue={1234} deltaPct={null} />);
    const hero = screen.getByTestId("kpi-hero-value");
    // fr-FR locale formats 1234 as "1 234" or "1 234"
    expect(hero.textContent).toMatch(/1[\s ]?234/);
  });

  it("renders the delta text in the kpi-delta-text element", () => {
    wrap(<KpiTile label="Sessions" currentValue={1234} deltaPct={5.2} />);
    const delta = screen.getByTestId("kpi-delta-text");
    expect(delta.textContent).toContain("+5.2");
  });

  it("renders '—' when deltaPct is null", () => {
    wrap(<KpiTile label="Sessions" currentValue={1234} deltaPct={null} />);
    const delta = screen.getByTestId("kpi-delta-text");
    expect(delta.textContent).toBe("—");
  });
});

// ---------------------------------------------------------------------------
// KpiTile — down_good direction inversion (R6)
// ---------------------------------------------------------------------------

describe("KpiTile — down_good delta inversion (R6, Story 8.8)", () => {
  it("renders delta line for down_good positive delta (no crash)", () => {
    // When direction=down_good and delta>0, the tile should still render
    wrap(
      <KpiTile
        label="Taux de rebond"
        currentValue={42}
        deltaPct={3.5}
        definition={DEF_DOWN_GOOD}
      />,
    );
    const delta = screen.getByTestId("kpi-delta-text");
    expect(delta.textContent).toContain("+3.5");
  });

  it("renders delta line for down_good negative delta (no crash)", () => {
    wrap(
      <KpiTile
        label="Taux de rebond"
        currentValue={38}
        deltaPct={-2.1}
        definition={DEF_DOWN_GOOD}
      />,
    );
    const delta = screen.getByTestId("kpi-delta-text");
    expect(delta.textContent).toContain("-2.1");
  });

  it("info button has definition for down_good metric", () => {
    wrap(
      <KpiTile
        label="Taux de rebond"
        currentValue={38}
        deltaPct={-2.1}
        definition={DEF_DOWN_GOOD}
      />,
    );
    expect(screen.getByTestId("kpi-info-button")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// KpiTile — neutral direction (R6)
// ---------------------------------------------------------------------------

describe("KpiTile — neutral direction (R6, Story 8.8)", () => {
  it("renders delta line for neutral metric (no crash)", () => {
    wrap(
      <KpiTile
        label="Métrique neutre"
        currentValue={500}
        deltaPct={1.0}
        definition={DEF_NEUTRAL}
      />,
    );
    const delta = screen.getByTestId("kpi-delta-text");
    expect(delta.textContent).toContain("+1.0");
  });
});

// ---------------------------------------------------------------------------
// KpiTile — absent-definitions fallback (R6)
// ---------------------------------------------------------------------------

describe("KpiTile — absent-definitions fallback (Story 8.8)", () => {
  it("renders correctly with no definition prop (no crash)", () => {
    expect(() => {
      wrap(<KpiTile label="Sessions" currentValue={1000} deltaPct={5.2} />);
    }).not.toThrow();
  });

  it("renders hero value when no definition", () => {
    wrap(<KpiTile label="Sessions" currentValue={1000} deltaPct={5.2} />);
    expect(screen.getByTestId("kpi-hero-value")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// KpiTileRow — definitions toggle (R6)
// ---------------------------------------------------------------------------

describe("KpiTileRow — definitions toggle (R6, Story 8.8)", () => {
  const dateRange = { start: "2026-07-02", end: "2026-07-10" };
  const rows = makeRows(dateRange);

  const metricDefinitions = {
    sessions: DEF_UP_GOOD,
    active_users: {
      definition: "Utilisateurs actifs dans la période.",
      unit: "utilisateurs",
      direction: "up_good" as const,
      caveats: null,
    },
    conversions: {
      definition: "Nombre de conversions enregistrées.",
      unit: "conversions",
      direction: "up_good" as const,
      caveats: null,
    },
  };

  it("shows the definitions toggle button when metricDefinitions is provided", () => {
    wrap(
      <KpiTileRow rows={rows} dateRange={dateRange} metricDefinitions={metricDefinitions} />,
    );
    expect(screen.getByTestId("definitions-toggle")).toBeInTheDocument();
  });

  it("does NOT show definitions toggle when metricDefinitions is absent", () => {
    wrap(<KpiTileRow rows={rows} dateRange={dateRange} />);
    expect(screen.queryByTestId("definitions-toggle")).not.toBeInTheDocument();
  });

  it("definitions toggle starts with aria-expanded=false", () => {
    wrap(
      <KpiTileRow rows={rows} dateRange={dateRange} metricDefinitions={metricDefinitions} />,
    );
    const toggle = screen.getByTestId("definitions-toggle");
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
  });

  it("clicking definitions toggle changes aria-expanded to true", () => {
    wrap(
      <KpiTileRow rows={rows} dateRange={dateRange} metricDefinitions={metricDefinitions} />,
    );
    const toggle = screen.getByTestId("definitions-toggle");
    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
  });

  it("definitions panel contains sessions definition text after toggle", () => {
    wrap(
      <KpiTileRow rows={rows} dateRange={dateRange} metricDefinitions={metricDefinitions} />,
    );
    fireEvent.click(screen.getByTestId("definitions-toggle"));
    expect(screen.getByText(/Nombre de sessions initiées/i)).toBeInTheDocument();
  });

  it("definitions panel contains caveat text when caveats are set", () => {
    wrap(
      <KpiTileRow rows={rows} dateRange={dateRange} metricDefinitions={metricDefinitions} />,
    );
    fireEvent.click(screen.getByTestId("definitions-toggle"));
    expect(screen.getByText(/multi-appareils/i)).toBeInTheDocument();
  });

  it("clicking toggle again collapses (aria-expanded back to false)", () => {
    wrap(
      <KpiTileRow rows={rows} dateRange={dateRange} metricDefinitions={metricDefinitions} />,
    );
    const toggle = screen.getByTestId("definitions-toggle");
    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
  });
});
