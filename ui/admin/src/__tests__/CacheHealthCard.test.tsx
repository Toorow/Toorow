/**
 * Vitest tests for CacheHealthCard (Story 19.3).
 *
 * Tests:
 *   - "disabled" state: appropriate message rendered.
 *   - "no-cache" state: "absent" message rendered.
 *   - "stale" state: warning alert rendered (never "fresh").
 *   - "fresh" state: tables + row_counts + hit_rate + age displayed.
 *   - "Rebuild" button clicked -> confirmation dialog opened.
 *   - Rebuild confirmation -> POST /api/admin/cache/rebuild called.
 *   - Button disabled when cache_enabled=false.
 *
 * Pattern: mock global.fetch (vitest), ThemeProvider adminTheme.
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ThemeProvider } from "@mui/material";
import { adminTheme } from "../theme";
import CacheHealthCard from "../cache/CacheHealthCard";

// ---------------------------------------------------------------------------
// Helper
// ---------------------------------------------------------------------------

function renderCard() {
  return render(
    <ThemeProvider theme={adminTheme}>
      <CacheHealthCard refreshIntervalMs={0} />
    </ThemeProvider>
  );
}

afterEach(() => {
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// Mock fetch helpers
// ---------------------------------------------------------------------------

function mockStatus(status: object, rebuildResult?: object) {
  vi.spyOn(globalThis, "fetch").mockImplementation((url: RequestInfo | URL, opts?: RequestInit) => {
    const urlStr = String(url);
    if (urlStr.includes("/api/admin/cache/status")) {
      return Promise.resolve(
        new Response(JSON.stringify(status), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        })
      );
    }
    if (urlStr.includes("/api/admin/cache/rebuild") && opts?.method === "POST") {
      const result = rebuildResult ?? { status: "ok", performed_by: "test-user" };
      return Promise.resolve(
        new Response(JSON.stringify(result), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        })
      );
    }
    return Promise.resolve(new Response("Not found", { status: 404 }));
  });
}

// ---------------------------------------------------------------------------
// Data fixtures
// ---------------------------------------------------------------------------

const STATUS_FRESH = {
  cache_state: "fresh",
  cache_enabled: true,
  cache_built_at: "2026-07-19T01:00:00+00:00",
  age_seconds: 7200,
  min_date: "2026-06-19",
  max_date: "2026-07-19",
  tables: ["fact_daily_kpi", "semantic_ctr"],
  row_counts: { fact_daily_kpi: 500, semantic_ctr: 120 },
  project_ids: ["proj_alpha"],
  hit_rate: 0.85,
  stats: { hit: 40, "miss-relation": 5 },
  last_rebuild_cause: null,
};

const STATUS_STALE = {
  ...STATUS_FRESH,
  cache_state: "stale",
  hit_rate: null,
};

const STATUS_NO_CACHE = {
  cache_state: "no-cache",
  cache_enabled: true,
  cache_built_at: null,
  age_seconds: null,
  min_date: null,
  max_date: null,
  tables: [],
  row_counts: {},
  project_ids: [],
  hit_rate: null,
  stats: {},
  last_rebuild_cause: null,
};

const STATUS_DISABLED = {
  cache_state: "disabled",
  cache_enabled: false,
  cache_built_at: null,
  age_seconds: null,
  min_date: null,
  max_date: null,
  tables: [],
  row_counts: {},
  project_ids: [],
  hit_rate: null,
  stats: {},
  last_rebuild_cause: null,
};

// ---------------------------------------------------------------------------
// Tests : etats
// ---------------------------------------------------------------------------

describe("CacheHealthCard - states", () => {
  it("renders the 'disabled' state with the right message", async () => {
    mockStatus(STATUS_DISABLED);
    renderCard();
    await waitFor(() => {
      expect(screen.getByTestId("cache-state-disabled")).toBeInTheDocument();
    });
    expect(screen.getByTestId("cache-state-disabled").textContent).toMatch(
      /disabled/i
    );
    // Rebuild button disabled (cache_enabled=false).
    const btn = screen.getByTestId("cache-rebuild-button");
    expect(btn).toBeDisabled();
  });

  it("renders the 'no-cache' state with the 'absent' message", async () => {
    mockStatus(STATUS_NO_CACHE);
    renderCard();
    await waitFor(() => {
      expect(screen.getByTestId("cache-state-no-cache")).toBeInTheDocument();
    });
    expect(screen.getByTestId("cache-state-no-cache").textContent).toMatch(
      /no cache|absent/i
    );
  });

  it("renders a warning for the 'stale' state (invariant c -- never a misleading 'fresh')", async () => {
    mockStatus(STATUS_STALE);
    renderCard();
    await waitFor(() => {
      expect(screen.getByTestId("cache-state-stale")).toBeInTheDocument();
    });
    const alertText = screen.getByTestId("cache-state-stale").textContent ?? "";
    // The message must mention "stale" or "bypass".
    expect(alertText.toLowerCase()).toMatch(/stale|bypass/i);
    // The state must NOT claim the cache is fresh (invariant c).
    expect(alertText.toLowerCase()).not.toMatch(/fresh/);
  });

  it("renders the metrics for the 'fresh' state (AI-56: asserted values)", async () => {
    mockStatus(STATUS_FRESH);
    renderCard();

    await waitFor(() => {
      expect(screen.getByTestId("cache-age")).toBeInTheDocument();
    });

    // Fenetre
    const window = screen.getByTestId("cache-window");
    expect(window.textContent).toMatch(/2026-06-19/);
    expect(window.textContent).toMatch(/2026-07-19/);

    // Hit rate 85 %
    const hitRate = screen.getByTestId("cache-hit-rate");
    expect(hitRate.textContent).toMatch(/85/);

    // Projets
    const projects = screen.getByTestId("cache-projects");
    expect(projects.textContent).toMatch(/proj_alpha/);

    // Tables + row counts
    expect(screen.getByText("fact_daily_kpi")).toBeInTheDocument();
    expect(screen.getByText("semantic_ctr")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Tests : rebuild
// ---------------------------------------------------------------------------

describe("CacheHealthCard - rebuild", () => {
  it("clicking 'Rebuild' opens the confirmation dialog", async () => {
    mockStatus(STATUS_FRESH);
    renderCard();

    await waitFor(() => screen.getByTestId("cache-rebuild-button"));

    const btn = screen.getByTestId("cache-rebuild-button");
    expect(btn).not.toBeDisabled();
    await userEvent.click(btn);

    // The confirmation dialog must be visible.
    await waitFor(() => {
      expect(screen.getByText(/Rebuild the cache/i)).toBeInTheDocument();
    });
  });

  it("rebuild confirmation -> POST /api/admin/cache/rebuild called", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(
      (url: RequestInfo | URL, opts?: RequestInit) => {
        const urlStr = String(url);
        if (urlStr.includes("/api/admin/cache/status")) {
          return Promise.resolve(
            new Response(JSON.stringify(STATUS_FRESH), { status: 200 })
          );
        }
        if (urlStr.includes("/api/admin/cache/rebuild") && opts?.method === "POST") {
          return Promise.resolve(
            new Response(
              JSON.stringify({ status: "ok", performed_by: "test-user" }),
              { status: 200 }
            )
          );
        }
        return Promise.resolve(new Response("Not found", { status: 404 }));
      }
    );

    renderCard();
    await waitFor(() => screen.getByTestId("cache-rebuild-button"));

    // Click "Rebuild"
    await userEvent.click(screen.getByTestId("cache-rebuild-button"));
    // Confirm in the dialog
    await waitFor(() => screen.getByTestId("cache-rebuild-confirm"));
    await userEvent.click(screen.getByTestId("cache-rebuild-confirm"));

    // Verify POST /api/admin/cache/rebuild was called.
    await waitFor(() => {
      const postCalls = fetchMock.mock.calls.filter(
        ([url, opts]) =>
          String(url).includes("/api/admin/cache/rebuild") && opts?.method === "POST"
      );
      expect(postCalls.length).toBeGreaterThan(0);
    });
  });

  it("button disabled when cache_enabled=false", async () => {
    mockStatus(STATUS_DISABLED);
    renderCard();
    await waitFor(() => screen.getByTestId("cache-rebuild-button"));
    expect(screen.getByTestId("cache-rebuild-button")).toBeDisabled();
  });
});
