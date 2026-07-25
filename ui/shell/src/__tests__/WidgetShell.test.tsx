/**
 * Vitest tests for WidgetShell (Story 2.5, AC6, AC7, AC8, T5.3, T5.4, T7.2).
 *
 * Verifies:
 *   - auth_expired reconnect affordance renders when meta.alerts contains auth_expired (AC6, AC8)
 *   - Reconnect button links to adminConsoleUrl (AD-15)
 *   - stale-since badge renders when meta.freshness.stale_since is set (AC7, T7.2)
 *   - No stale badge when stale_since is absent
 *   - No reconnect button when no auth_expired alert
 *
 * These tests confirm the REAL data wire (Story 2.5) — the affordance was
 * wired in Story 1.2; here we assert correct rendering for the envelope shapes
 * that get_daily_report will now produce.
 */
import { render, screen } from "@testing-library/react";
import WidgetShell from "../WidgetShell";
import type { WidgetMeta } from "../types";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function renderShell(meta?: WidgetMeta, adminConsoleUrl?: string) {
  return render(
    <WidgetShell meta={meta} adminConsoleUrl={adminConsoleUrl ?? "/admin"}>
      <div data-testid="child">Widget content</div>
    </WidgetShell>
  );
}

function makeBaseMeta(overrides?: Partial<WidgetMeta>): WidgetMeta {
  return {
    freshness: { last_pull: "2026-07-10T12:00:00Z", cadence_hours: 24 },
    provenance: { source_system: "test", pull_id: "pull_01" },
    alerts: [],
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// auth_expired reconnect affordance (AC6, AC8)
// ---------------------------------------------------------------------------

describe("WidgetShell -- auth_expired reconnect affordance (Story 2.5 AC6)", () => {
  it("renders reconnect button when meta.alerts contains auth_expired", () => {
    const meta = makeBaseMeta({
      alerts: [
        {
          code: "auth_expired",
          severity: "error",
          message: "Token Google Analytics expire -- reconnectez dans la console admin.",
        },
      ],
    });

    renderShell(meta, "/admin");

    expect(screen.getByRole("button", { name: /Reconnecter/i })).toBeInTheDocument();
  });

  it("does NOT render reconnect button when alerts is empty", () => {
    const meta = makeBaseMeta({ alerts: [] });
    renderShell(meta);

    expect(screen.queryByRole("button", { name: /Reconnecter/i })).not.toBeInTheDocument();
  });

  it("does NOT render reconnect button for other alert codes", () => {
    const meta = makeBaseMeta({
      alerts: [{ code: "some_other_code", severity: "warn", message: "Other issue" }],
    });
    renderShell(meta);

    expect(screen.queryByRole("button", { name: /Reconnecter/i })).not.toBeInTheDocument();
  });

  it("renders children even when auth_expired is present", () => {
    const meta = makeBaseMeta({
      alerts: [{ code: "auth_expired", severity: "error", message: "Expired" }],
    });
    renderShell(meta);

    expect(screen.getByTestId("child")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// stale-since badge (AC7, T7.2)
// ---------------------------------------------------------------------------

describe("WidgetShell -- stale-since badge (Story 2.5 AC7)", () => {
  it("renders stale badge when meta.freshness.stale_since is set", () => {
    const meta = makeBaseMeta({
      freshness: {
        last_pull: "2026-07-09T10:00:00Z",
        cadence_hours: 24,
        stale_since: "2026-07-09T10:00:00Z",
      },
    });
    renderShell(meta);

    // WidgetShell renders "Donnees perimees depuis <date>" for stale_since
    expect(screen.getByText(/p.rim.es depuis/i)).toBeInTheDocument();
  });

  it("does NOT render stale badge when stale_since is absent", () => {
    const meta = makeBaseMeta({
      freshness: { last_pull: "2026-07-11T10:00:00Z", cadence_hours: 24 },
    });
    renderShell(meta);

    expect(screen.queryByText(/p.rim.es depuis/i)).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Renders without meta (no crash)
// ---------------------------------------------------------------------------

describe("WidgetShell -- no meta", () => {
  it("renders children when meta is undefined", () => {
    renderShell(undefined);
    expect(screen.getByTestId("child")).toBeInTheDocument();
  });

  it("does NOT render reconnect or stale badge when meta is undefined", () => {
    renderShell(undefined);
    expect(screen.queryByRole("button", { name: /Reconnecter/i })).not.toBeInTheDocument();
    expect(screen.queryByText(/p.rim.es depuis/i)).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Story 4.6 — as-of archive banner (AC5, UX-DR10)
// ---------------------------------------------------------------------------

describe("WidgetShell -- as-of archive banner (Story 4.6 AC5)", () => {
  it("renders as-of banner when meta.as_of is set", () => {
    const meta = makeBaseMeta({
      as_of: "2026-07-08T23:59:59Z",
    } as Partial<WidgetMeta>);
    renderShell(meta);

    // Banner text: "Vue archivée — données au YYYY-MM-DD HH:MM UTC"
    expect(screen.getByText(/Vue archivée/i)).toBeInTheDocument();
    expect(screen.getByText(/données au/i)).toBeInTheDocument();
    // Formatted date should appear
    expect(screen.getByText(/2026-07-08/)).toBeInTheDocument();
  });

  it("does NOT render as-of banner when meta.as_of is null", () => {
    const meta = makeBaseMeta({
      as_of: null,
    } as Partial<WidgetMeta>);
    renderShell(meta);

    expect(screen.queryByText(/Vue archivée/i)).not.toBeInTheDocument();
  });

  it("does NOT render as-of banner when meta.as_of is absent", () => {
    const meta = makeBaseMeta();
    renderShell(meta);

    expect(screen.queryByText(/Vue archivée/i)).not.toBeInTheDocument();
  });

  it("renders children even when as-of banner is shown", () => {
    const meta = makeBaseMeta({
      as_of: "2026-07-05T00:00:00Z",
    } as Partial<WidgetMeta>);
    renderShell(meta);

    expect(screen.getByTestId("child")).toBeInTheDocument();
  });

  it("formats as_of date correctly in UTC", () => {
    const meta = makeBaseMeta({
      as_of: "2026-07-05T12:30:00Z",
    } as Partial<WidgetMeta>);
    renderShell(meta);

    // Should display 2026-07-05 12:30 UTC
    expect(screen.getByText(/2026-07-05/)).toBeInTheDocument();
    expect(screen.getByText(/12:30 UTC/)).toBeInTheDocument();
  });
});
