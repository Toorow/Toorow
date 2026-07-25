/**
 * Tests for CalendarHeatmap context event markers (Story 4.4, AC7).
 *
 * Covers:
 *  - No markers rendered when contextEvents is empty.
 *  - Marker circle appears for each event date when contextEvents is non-empty.
 *  - Tooltip on marker shows label and type.
 *  - Multiple events on the same date are shown in a single marker tooltip.
 *
 * AD-11: markers are in-DOM only — no external fetch. Data arrives in the envelope.
 */

import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import CalendarHeatmap, { type ContextEvent } from "../CalendarHeatmap";

// Minimal theme stub — MUI useTheme() requires a ThemeProvider.
import { ThemeProvider, createTheme } from "@mui/material/styles";

function renderHeatmap(contextEvents: ContextEvent[] = []) {
  const theme = createTheme();
  const values = new Map<string, number>([
    ["2026-07-01", 100],
    ["2026-07-02", 200],
    ["2026-07-10", 50],
  ]);
  return render(
    <ThemeProvider theme={theme}>
      <CalendarHeatmap
        values={values}
        dateRange={{ start: "2026-07-01", end: "2026-07-31" }}
        onDayClick={() => {}}
        metricLabel="Sessions"
        contextEvents={contextEvents}
      />
    </ThemeProvider>,
  );
}

describe("CalendarHeatmap — context event markers", () => {
  it("renders no marker circles when contextEvents is empty", () => {
    renderHeatmap([]);
    // No <circle> elements with event aria-labels should be present.
    const circles = document.querySelectorAll("circle");
    expect(circles.length).toBe(0);
  });

  it("renders no marker circles when contextEvents is not provided", () => {
    const theme = createTheme();
    const values = new Map<string, number>([["2026-07-01", 100]]);
    render(
      <ThemeProvider theme={theme}>
        <CalendarHeatmap
          values={values}
          dateRange={{ start: "2026-07-01", end: "2026-07-31" }}
          onDayClick={() => {}}
          metricLabel="Sessions"
        />
      </ThemeProvider>,
    );
    const circles = document.querySelectorAll("circle");
    expect(circles.length).toBe(0);
  });

  it("renders a marker circle for each event date in contextEvents", () => {
    const events: ContextEvent[] = [
      { id: "evt_1", event_date: "2026-07-01", type: "business", label: "Lancement" },
      { id: "evt_2", event_date: "2026-07-10", type: "incident", label: "Panne" },
    ];
    renderHeatmap(events);
    // Two distinct dates with events -> two marker circles.
    const circles = document.querySelectorAll("circle");
    expect(circles.length).toBe(2);
  });

  it("marker circle has aria-label containing the event label and type", () => {
    const events: ContextEvent[] = [
      { id: "evt_1", event_date: "2026-07-02", type: "deployment", label: "Deploy v2.0" },
    ];
    renderHeatmap(events);
    const circle = document.querySelector("circle");
    expect(circle).not.toBeNull();
    const ariaLabel = circle!.getAttribute("aria-label") ?? "";
    expect(ariaLabel).toContain("Deploy v2.0");
    expect(ariaLabel).toContain("deployment");
  });

  it("marker tooltip <title> contains label and type", () => {
    const events: ContextEvent[] = [
      { id: "evt_3", event_date: "2026-07-01", type: "campaign", label: "Promo Ete" },
    ];
    renderHeatmap(events);
    // The <title> element inside the circle is the SVG tooltip.
    const titles = document.querySelectorAll("circle > title");
    expect(titles.length).toBeGreaterThan(0);
    expect(titles[0].textContent).toContain("Promo Ete");
    expect(titles[0].textContent).toContain("campaign");
  });

  it("multiple events on the same date are combined in one marker", () => {
    const events: ContextEvent[] = [
      { id: "evt_4", event_date: "2026-07-01", type: "business", label: "Event A" },
      { id: "evt_5", event_date: "2026-07-01", type: "incident", label: "Event B" },
    ];
    renderHeatmap(events);
    // One date -> one marker circle (not two).
    const circles = document.querySelectorAll("circle");
    expect(circles.length).toBe(1);
    const titleEl = circles[0].querySelector("title");
    expect(titleEl?.textContent).toContain("Event A");
    expect(titleEl?.textContent).toContain("Event B");
  });
});
