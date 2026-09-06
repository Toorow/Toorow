/**
 * Story 76-8 — the daily report converges on the card's chrome.
 *
 * The v5-01 hierarchy (dominant value, discrete variation) was already there;
 * what was missing was everything a card says AROUND the figure: when it was
 * refreshed, and where it comes from. « 40 467 sessions » with neither a date
 * nor a source is a number nobody can act on, and the cards have said both
 * since Story 9.1.
 */

import { describe, expect, it } from "vitest";
import { render, screen, within } from "@testing-library/react";

import App from "../App";
import CalendarHeatmap from "../CalendarHeatmap";
import { FIXTURE_ENVELOPE } from "../fixture";
import { formatValue } from "../format";

describe("daily report chrome — freshness and provenance (76-8)", () => {
  it("says WHEN the figures were refreshed, in the card's own wording", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    const badge = screen.getByTestId("widget-freshness-badge");
    expect(badge.textContent).toMatch(/^Updated \d{4}-\d{2}-\d{2}$/);
  });

  it("shows the source in double — human label, then the token in monospace", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    const provenance = screen.getByTestId("widget-provenance");
    // The human label, capitalised from the slug, and the slug itself beside it.
    expect(provenance.textContent).toContain("Google Analytics");
    expect(provenance.textContent).toContain("google-analytics");
    // A raw token alone is the defect; the label must come first.
    expect(provenance.textContent!.indexOf("Google Analytics")).toBeLessThan(
      provenance.textContent!.indexOf("google-analytics"),
    );
  });

  it("names the run rather than printing its identifier bare", () => {
    const withPull = {
      ...FIXTURE_ENVELOPE,
      meta: {
        ...FIXTURE_ENVELOPE.meta,
        provenance: [{ source_system: "google-analytics", pull_id: "pull_FIXTURE0000000000" }],
      },
    };
    render(<App envelope={withPull} />);
    const provenance = screen.getByTestId("widget-provenance");
    expect(provenance.textContent).toContain("Run");
    expect(provenance.textContent).toContain("pull_FIXTURE0000000000");
  });
});

describe("heatmap intensity legend (76-8)", () => {
  const VALUES = new Map<string, number>([
    ["2026-07-01", 10],
    ["2026-07-02", 250],
    ["2026-07-03", 900],
  ]);

  it("names both ends of the ramp and the metric it measures", () => {
    render(
      <CalendarHeatmap
        values={VALUES}
        dateRange={{ start: "2026-07-01", end: "2026-07-03" }}
        onDayClick={() => {}}
        metricLabel="Active Users"
      />,
    );
    const legend = screen.getByTestId("heatmap-intensity-legend");
    expect(legend).toHaveTextContent("Active Users per day");
    expect(legend).toHaveTextContent("0");
    // The upper bound is the observed maximum, on the pinned formatter.
    expect(legend).toHaveTextContent(formatValue(900));
    // The event ring is the only non-accent mark on the grid, so it is legended.
    expect(legend).toHaveTextContent("context event");
  });

  it("says the absence rather than a fake maximum when nothing was measured", () => {
    render(
      <CalendarHeatmap
        values={new Map()}
        dateRange={{ start: "2026-07-01", end: "2026-07-03" }}
        onDayClick={() => {}}
        metricLabel="Active Users"
      />,
    );
    expect(screen.getByTestId("heatmap-intensity-legend")).toHaveTextContent("—");
  });
});

describe("the daily report legends the colour of its deltas (76-8)", () => {
  it("states the convention ONCE, in the widget's own footer", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    // The tiles painted `-8.6 %`, `-7.0 %` and `-9.6 %` red with no legend at
    // all: the same defect as the cards' bars, outside the shell.
    const legends = screen.getAllByTestId("widget-variation-legend");
    expect(legends).toHaveLength(1);
    expect(legends[0]).toHaveTextContent("Green: favourable");
    expect(legends[0]).toHaveTextContent("red: unfavourable");
    expect(legends[0]).toHaveTextContent("vs the previous period");
  });

  it("names the metrics it governs, from the declared definitions", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    const legend = screen.getByTestId("widget-variation-legend");
    expect(legend.textContent).toMatch(/rising is favourable/);
  });
});

describe("one number convention on the whole report (76-8)", () => {
  it("groups the hero value and signs the delta through the same module", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    const tiles = screen.getAllByTestId("kpi-hero-value");
    expect(tiles.length).toBeGreaterThan(0);
    for (const tile of tiles) {
      // The pinned locale groups with a comma; a space or a dot would mean a
      // second convention slipped back in.
      expect(tile.textContent).not.toMatch(/\d[\s  ]\d/);
    }
    const deltas = screen.getAllByTestId("kpi-delta-text");
    for (const delta of deltas) {
      expect(delta.textContent).toMatch(/^([+-]?\d+\.\d%|—)$/);
    }
  });

  it("keeps the dominant value dominant — the v5-01 hierarchy", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    const line = screen.getAllByTestId("kpi-delta-line")[0];
    expect(within(line).getByTestId("kpi-delta-text")).toBeInTheDocument();
    expect(line.textContent).toContain("vs période préc.");
  });
});
