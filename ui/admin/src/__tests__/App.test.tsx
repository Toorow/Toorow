/**
 * Vitest smoke tests for the v3 ApplicationShell (Epic 42, story 42.1).
 *
 * The legacy flat 20-item French sidebar was removed in the big-bang IA v3
 * cutover. <App /> now self-provides Router > Scope > OrgTheme > Shell and
 * renders the six project workspaces (Overview / Analyze / Test / Data /
 * Governance / Context Hub) with a deep-linkable router and a TopBar scope control.
 *
 * These tests are a focused smoke of shell + routing:
 *   - the sidebar renders the six workspaces (by data-testid and English label)
 *   - Overview renders by default
 *   - the TopBar scope control shows the seeded org + project
 *   - clicking a workspace (Data) navigates and reveals its subnav (sec-data-*)
 */
import { render, screen, waitFor, within } from "@testing-library/react";
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

const PROJECT_OVERVIEW = {
  schema_version: "project-overview.v1",
  project: { id: "proj-acme", name: "Core project", organization: { id: "org-acme", name: "Acme Group" }, business_domains: [], active_configuration_version_id: null, as_of: "2026-07-29T09:00:00Z" },
  posture: {
    operational_health: { state: "unknown", explanation: "No publication evidence.", evidence_horizon: null, owner: { surface: "project", workspace: "data", section: "data-overview", global_surface: null, global_section: null, object_type: null, object_id: null, tab: null, action: null, version_id: null, evidence_id: null } },
    trust_readiness: { state: "unknown", explanation: "No test evidence.", evidence_horizon: null, owner: { surface: "project", workspace: "test", section: "regression-runs", global_surface: null, global_section: null, object_type: null, object_id: null, tab: null, action: null, version_id: null, evidence_id: null } },
    business_signals: { state: "unknown", explanation: "No outcome evidence.", evidence_horizon: null, owner: { surface: "project", workspace: "analyze", section: "explore", global_surface: null, global_section: null, object_type: null, object_id: null, tab: null, action: null, version_id: null, evidence_id: null } },
    limiting_dimension: null,
  },
  next_action: null,
  attention: { items: [], total: 0, has_more: false },
  coverage: [],
  outcomes: { status: "empty", items: [] },
  changes: { status: "empty", items: [] },
};

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
    "/api/projects/proj-acme/overview": PROJECT_OVERVIEW,
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
    "/api/connectors/available": [],
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
    expect(screen.getByTestId("ws-context-hub")).toBeInTheDocument();
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
    expect(screen.getByTestId("ws-context-hub")).toHaveTextContent("Context Hub");
  });
});

// ---------------------------------------------------------------------------
// Default landing — Overview
// ---------------------------------------------------------------------------

describe("App — default route", () => {
  it("renders the Overview page by default", async () => {
    render(<App />);

    await waitFor(() => {
      expect(
        screen.getByRole("heading", { name: "Core project", level: 1 }),
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
    expect(screen.getByRole("button", {
      name: "Switch organization or project: Acme Group, Core project",
    })).toBeInTheDocument();
  });

  it("keeps scope switching canonical and global settings outside project navigation", async () => {
    const user = userEvent.setup();
    render(<App />);

    const switchTrigger = await screen.findByRole("button", {
      name: "Switch organization or project: Acme Group, Core project",
    });
    // Story 46.4 turned this into the single global-scope menu: Getting Started,
    // Project Settings, Project Access, Organization Settings and User Account.
    const settingsTrigger = screen.getByRole("button", {
      name: "Scope and account menu",
    });

    await user.click(switchTrigger);

    const switchDialog = await screen.findByRole("dialog", {
      name: "Switch organization or project",
    });
    expect(
      within(switchDialog).getByText("Choose an Organization, then a Project."),
    ).toBeInTheDocument();
    expect(within(switchDialog).queryByText(/workspace/i)).not.toBeInTheDocument();

    await user.click(within(switchDialog).getByRole("button", { name: "Core project" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(window.location.pathname).toBe("/org/org-acme/project/proj-acme/overview/project-overview");

    await user.click(settingsTrigger);
    const settingsMenu = await screen.findByRole("menu", {
      name: "Scope and account menu",
    });
    expect(within(settingsMenu).queryByText(/workspace/i)).not.toBeInTheDocument();
    await user.click(within(settingsMenu).getByRole("menuitem", { name: "Project Settings" }));
    expect(window.location.pathname).toBe("/org/org-acme/project/proj-acme/settings/general");
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
    await waitFor(() => {
      expect(screen.getByRole("heading", { level: 1, name: "Data Overview" })).toHaveFocus();
    });

    // All six stable Data owners come from the canonical registry.
    for (const section of ["data-overview", "datastreams", "events", "sources", "imports", "connectors"]) {
      expect(screen.getByTestId(`sec-data-${section}`)).toBeInTheDocument();
    }
  });

  it("clicking Governance reveals its subnav sections", async () => {
    const user = userEvent.setup();
    render(<App />);

    await waitFor(() => {
      expect(screen.getByTestId("ws-governance")).toBeInTheDocument();
    });

    // `focus()` then `{Enter}` raced: the shell re-renders while its initial
    // reads settle, which blurs the element between the two lines, and the key
    // goes to <body>. Failed roughly one run in four. Waiting for focus to
    // actually LAND keeps the keyboard reachability this case exists to prove —
    // switching to `user.click()` would make it green by testing something else.
    const governance = screen.getByTestId("ws-governance");
    governance.focus();
    await waitFor(() => expect(governance).toHaveFocus());
    await user.keyboard("{Enter}");

    await waitFor(() => {
      expect(
        screen.getByTestId("sec-governance-semantic-model"),
      ).toBeInTheDocument();
    });
    expect(screen.getByTestId("sec-governance-master-data")).toBeInTheDocument();
    expect(screen.getByTestId("sec-governance-controls-quality")).toBeInTheDocument();
    expect(screen.getByTestId("sec-governance-evidence")).toBeInTheDocument();
    expect(screen.queryByTestId("sec-governance-mapping")).not.toBeInTheDocument();
  });
});
