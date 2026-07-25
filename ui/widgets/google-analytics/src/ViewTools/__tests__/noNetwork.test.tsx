/**
 * "No network after initial render" test (Story 1.7, T7).
 *
 * Asserts that toggling granularity, connector filter, and metric switch
 * do NOT trigger any fetch() or XHR calls — View Tools are 100% in-DOM (AD-10, AC4).
 *
 * Strategy (T7.2): install global fetch + XMLHttpRequest.open spies that
 * THROW if invoked. Any network call during interactions will therefore cause
 * the test to fail with a descriptive error.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, act } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import App from "../../App";
import type { DailyReportEnvelope, Row } from "../../types";

// ---------------------------------------------------------------------------
// Minimal 30-row fixture: 3 metrics × 2 breakdowns × 5 days (1 connector).
// ---------------------------------------------------------------------------
function buildTwoConnectorRows(): Row[] {
  // review-1-7 F-02: second connector so the ConnectorFilter interaction path
  // is exercised under the no-network spies.
  const rows = buildFixtureRows();
  return [
    ...rows,
    ...rows.map((r) => ({ ...r, connector: "meta-ads", value: r.value / 2 })),
  ];
}

function buildFixtureRows(): Row[] {
  const rows: Row[] = [];
  const metrics = ["sessions", "active_users", "conversions"];
  const devices = ["desktop", "mobile"];
  for (let d = 0; d < 10; d++) {
    const date = new Date(Date.UTC(2026, 6, 1 + d)).toISOString().slice(0, 10);
    for (const metric of metrics) {
      for (const device of devices) {
        rows.push({
          date,
          connector: "google-analytics",
          metric,
          breakdown_dimension: "device_category",
          breakdown_value: device,
          value: 100 + d * 10,
          pull_id: "pull_test",
          loaded_at: "2026-07-10T00:00:00Z",
        });
      }
    }
  }
  return rows;
}

const FIXTURE_ENVELOPE: DailyReportEnvelope = {
  schema_version: "1",
  meta: {
    freshness: {
      last_pull: "2026-07-10T00:00:00Z",
      cadence_hours: 24,
      stale_since: null,
    },
    provenance: [{ source_system: "google-analytics", pull_id: "pull_test" }],
    alerts: [],
  },
  data: {
    report_profile: "standard_daily",
    date_range: { start: "2026-07-01", end: "2026-07-10" },
    connectors: ["google-analytics"],
    rows: buildFixtureRows(),
  },
};

// ---------------------------------------------------------------------------
// Spy setup: throw on any network call.
// ---------------------------------------------------------------------------
beforeEach(() => {
  vi.spyOn(globalThis, "fetch").mockImplementation(() => {
    throw new Error(
      "Unexpected network call detected — View Tools must be 100% in-DOM (AD-10, AC4)",
    );
  });
  vi.spyOn(XMLHttpRequest.prototype, "open").mockImplementation(() => {
    throw new Error(
      "Unexpected XHR from View Tools — must be 100% in-DOM (AD-10, AC4)",
    );
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("View Tools — no network calls after initial render (AC4, AD-10)", () => {
  it("renders the widget without triggering network calls", () => {
    // If fetch/XHR are called during render, this will throw.
    expect(() => {
      render(<App envelope={FIXTURE_ENVELOPE} />);
    }).not.toThrow();
  });

  it("toggling granularity to 'Semaine' triggers no network call", async () => {
    const user = userEvent.setup();
    render(<App envelope={FIXTURE_ENVELOPE} />);

    await act(async () => {
      await user.click(screen.getByRole("button", { name: "Semaine" }));
    });

    // The test passes if no error was thrown.
    // Also verify the DOM updated (some rendered content exists).
    expect(screen.getByRole("button", { name: "Semaine" })).toBeDefined();
  });

  it("toggling granularity to 'Mois' triggers no network call", async () => {
    const user = userEvent.setup();
    render(<App envelope={FIXTURE_ENVELOPE} />);

    await act(async () => {
      await user.click(screen.getByRole("button", { name: "Mois" }));
    });

    expect(screen.getByRole("button", { name: "Mois" })).toBeDefined();
  });

  it("switching metric triggers no network call", async () => {
    const user = userEvent.setup();
    render(<App envelope={FIXTURE_ENVELOPE} />);

    await act(async () => {
      await user.click(screen.getByRole("button", { name: "Utilisateurs actifs" }));
    });

    expect(screen.getByRole("button", { name: "Utilisateurs actifs" })).toBeDefined();
  });

  it("toggling a connector in the filter triggers no network call (F-02)", async () => {
    const user = userEvent.setup();
    const twoConnectorEnvelope: DailyReportEnvelope = {
      ...FIXTURE_ENVELOPE,
      data: {
        ...FIXTURE_ENVELOPE.data,
        connectors: ["google-analytics", "meta-ads"],
        rows: buildTwoConnectorRows(),
      },
    };
    render(<App envelope={twoConnectorEnvelope} />);

    // Deselect then reselect the second connector — pure in-DOM filtering.
    await act(async () => {
      await user.click(screen.getByRole("button", { name: /meta.ads/i }));
    });
    await act(async () => {
      await user.click(screen.getByRole("button", { name: /meta.ads/i }));
    });

    expect(screen.getByRole("button", { name: /meta.ads/i })).toBeDefined();
  });

  it("toggling back to 'Jour' triggers no network call", async () => {
    const user = userEvent.setup();
    render(<App envelope={FIXTURE_ENVELOPE} />);

    await act(async () => {
      await user.click(screen.getByRole("button", { name: "Semaine" }));
    });
    await act(async () => {
      await user.click(screen.getByRole("button", { name: "Jour" }));
    });

    expect(screen.getByRole("button", { name: "Jour" })).toBeDefined();
  });
});
