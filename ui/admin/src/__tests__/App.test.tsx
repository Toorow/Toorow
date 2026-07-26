/**
 * Vitest smoke tests for the v3 ApplicationShell (Epic 42, story 42.1).
 *
 * The legacy flat 20-item French sidebar was removed in the big-bang IA v3
 * cutover. <App /> now self-provides Router > Scope > OrgTheme > Shell and
 * renders the six project workspaces (Overview / Analyze / Test / Data /
 * Governance / Context) with a deep-linkable router and a TopBar scope control.
 *
 * These tests are a focused smoke of shell + routing:
 *   - the sidebar renders the six workspaces (by data-testid and English label)
 *   - Overview renders by default
 *   - the TopBar scope control shows the seeded org + project
 *   - clicking a workspace (Data) navigates and reveals its subnav (sec-data-*)
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import App from "../App";

vi.mock("../shell/AuthGate", () => ({
  default: ({ children }: { children: React.ReactNode }) => children,
}));

// ---------------------------------------------------------------------------
// Mock fetch globally so no panel crashes on mount
// ---------------------------------------------------------------------------

function mockFetch(responseMap: Record<string, unknown> = {}) {
  const fetchMock = vi.fn().mockImplementation((url: string) => {
    for (const [key, data] of Object.entries(responseMap)) {
      if (url.includes(key)) {
        return Promise.resolve({
          ok: true,
          status: 200,
          json: async () => data,
        });
      }
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => ({}) });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

/** A syntactically valid, unexpired ID token so AuthGate renders the app, not sign-in. */
function signIn(): void {
  const payload = btoa(
    JSON.stringify({ exp: Math.floor(Date.now() / 1000) + 3600 }),
  );
  localStorage.setItem("api_token", `header.${payload}.signature`);
}

beforeEach(() => {
  signIn();
  // The router normalizes a bare "/" into the first real project overview. Start clean.
  window.history.replaceState({}, "", "/");

  mockFetch({
    "/api/overview": { fleet: [], breakers: [] },
    "/api/connections": { connections: [] },
    // A real membership: <App /> only renders the shell when the scope resolves to
    // state "ready" (F-011 entry routing). An empty list is the new-user case and
    // routes to the welcome/create-organization surface instead — covered by
    // AppEntryRouting.test.tsx.
    "/api/organizations": {
      organizations: [{ id: "org-acme", name: "Acme Group" }],
    },
    "/api/projects": {
      projects: [
        { id: "proj-acme", name: "Core project", org_id: "org-acme" },
      ],
    },
    "/api/datamodel/fields": [],
    "/api/reports/available": [],
    "/api/notebooks": [],
    "/api/context-events": { events: [] },
    "/api/modules/available": [],
    "/api/jobs": { jobs: [] },
    "/api/health": { data: { status: "ok", quota: [], mirror_sync: null } },
    "/api/cards/templates": [],
    "/api/admin/cache/status": {
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
    },
  });
});

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// Sidebar structure — the six project workspaces
// ---------------------------------------------------------------------------

describe("App — v3 sidebar renders the six workspaces", () => {
  it("renders each workspace by data-testid", async () => {
    render(<App />);

    await waitFor(() => {
      expect(screen.getByTestId("ws-overview")).toBeInTheDocument();
    });
    expect(screen.getByTestId("ws-analyze")).toBeInTheDocument();
    expect(screen.getByTestId("ws-test")).toBeInTheDocument();
    expect(screen.getByTestId("ws-data")).toBeInTheDocument();
    expect(screen.getByTestId("ws-governance")).toBeInTheDocument();
    expect(screen.getByTestId("ws-context")).toBeInTheDocument();
  });

  it("renders the workspaces by their visible English labels", async () => {
    render(<App />);

    await waitFor(() => {
      expect(screen.getByTestId("ws-overview")).toHaveTextContent("Overview");
    });
    expect(screen.getByTestId("ws-analyze")).toHaveTextContent("Analyze");
    expect(screen.getByTestId("ws-test")).toHaveTextContent("Test");
    expect(screen.getByTestId("ws-data")).toHaveTextContent("Data");
    expect(screen.getByTestId("ws-governance")).toHaveTextContent("Governance");
    expect(screen.getByTestId("ws-context")).toHaveTextContent("Context");
  });
});

// ---------------------------------------------------------------------------
// Default landing — Overview
// ---------------------------------------------------------------------------

describe("App — default route", () => {
  it("renders the Overview page by default", async () => {
    render(<App />);

    await waitFor(() => {
      // OverviewV3 page header <h1>Overview</h1>
      expect(
        screen.getByRole("heading", { name: "Overview", level: 1 }),
      ).toBeInTheDocument();
    });
  });

  it("marks the Overview workspace active by default", async () => {
    render(<App />);

    await waitFor(() => {
      expect(screen.getByTestId("ws-overview")).toHaveAttribute(
        "aria-current",
        "page",
      );
    });
  });
});

// ---------------------------------------------------------------------------
// TopBar scope control
// ---------------------------------------------------------------------------

describe("App — TopBar scope control", () => {
  it("renders the organization and project returned by the API", async () => {
    render(<App />);

    await waitFor(() => {
      expect(screen.getByText("Acme Group")).toBeInTheDocument();
    });
    expect(screen.getByText("Core project")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Navigation — clicking a workspace reveals its subnav
// ---------------------------------------------------------------------------

describe("App — workspace navigation", () => {
  it("clicking Data activates it and reveals the data subnav", async () => {
    const user = userEvent.setup();
    render(<App />);

    await waitFor(() => {
      expect(screen.getByTestId("ws-data")).toBeInTheDocument();
    });

    await user.click(screen.getByTestId("ws-data"));

    await waitFor(() => {
      expect(screen.getByTestId("ws-data")).toHaveAttribute(
        "aria-current",
        "page",
      );
    });

    // The Data workspace expands its stable section anchors (sec-data-*).
    await waitFor(() => {
      expect(screen.getByTestId("sec-data-sources")).toBeInTheDocument();
    });
    expect(screen.getByTestId("sec-data-imports")).toBeInTheDocument();
    expect(screen.getByTestId("sec-data-modules")).toBeInTheDocument();
  });

  it("clicking Governance reveals its subnav sections", async () => {
    const user = userEvent.setup();
    render(<App />);

    await waitFor(() => {
      expect(screen.getByTestId("ws-governance")).toBeInTheDocument();
    });

    await user.click(screen.getByTestId("ws-governance"));

    await waitFor(() => {
      expect(
        screen.getByTestId("sec-governance-semantic-model"),
      ).toBeInTheDocument();
    });
    expect(screen.getByTestId("sec-governance-mapping")).toBeInTheDocument();
    expect(
      screen.getByTestId("sec-governance-data-quality"),
    ).toBeInTheDocument();
  });
});
