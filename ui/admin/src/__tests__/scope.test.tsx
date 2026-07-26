/**
 * Vitest tests for ScopeProvider / useScope — finding F-010.
 *
 * The regression under guard: the provider used to fall back on a hard-coded
 * SEED_ORGS ("Toorow Core" / "Default project") whenever GET /api/organizations
 * failed, so an unauthenticated 401 rendered a console full of an organization
 * the user does not own. There is no fallback any more; the outcome is reported
 * through `state`.
 *
 * Coverage — the four states of the contract:
 *   - "loading" while the requests are in flight
 *   - "error"   on 401 (and NEVER a fabricated org), on 500, on network failure
 *   - "empty"   on 200 with zero organizations
 *   - "ready"   on 200 with at least one organization (org + activeProject set)
 *   - the requests carry Authorization: Bearer <localStorage.api_token>
 *   - reload() re-runs the fetch
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider } from "../shell/router";
import { ScopeProvider, useScope, type OrgRef } from "../shell/scope";

/** The seed the provider must no longer be able to invent. */
const FORBIDDEN_ORG_NAME = "Toorow Core";

/** Probe that renders the whole scope value as text so every field is assertable. */
function ScopeProbe() {
  const { state, org, orgs, activeProject, reload } = useScope();
  return (
    <div>
      <span data-testid="state">{state}</span>
      <span data-testid="org">{org ? org.name : "—"}</span>
      <span data-testid="orgs">{orgs.length}</span>
      <span data-testid="project">{activeProject ? activeProject.name : "—"}</span>
      <button type="button" onClick={reload}>
        Retry
      </button>
    </div>
  );
}

function renderScope(orgsOverride?: OrgRef[]) {
  return render(
    <RouterProvider defaultProject="default">
      <ScopeProvider orgs={orgsOverride}>
        <ScopeProbe />
      </ScopeProvider>
    </RouterProvider>
  );
}

/** Response double: `status` decides ok, `body` is what .json() resolves to. */
function resp(status: number, body: unknown) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

function stubFetch(orgStatus: number, orgBody: unknown, projStatus = 200, projBody: unknown = { projects: [] }) {
  const mock = vi.fn((url: string) =>
    Promise.resolve(
      String(url).includes("/api/organizations") ? resp(orgStatus, orgBody) : resp(projStatus, projBody)
    )
  );
  vi.stubGlobal("fetch", mock);
  return mock;
}

const ONE_ORG = {
  organizations: [{ id: "org_acme", name: "Acme Media", brand_primary: "#123456" }],
};
const ONE_PROJECT = { projects: [{ id: "proj_a", name: "Acme Growth", org_id: "org_acme" }] };

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  localStorage.clear();
});

describe("ScopeProvider states", () => {
  it("is loading while the requests are in flight", () => {
    // A fetch that never settles keeps the provider in its initial state.
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => {})));
    renderScope();
    expect(screen.getByTestId("state")).toHaveTextContent("loading");
    expect(screen.getByTestId("org")).toHaveTextContent("—");
    expect(screen.getByTestId("project")).toHaveTextContent("—");
  });

  it("reports error on 401 and never fabricates an organization", async () => {
    stubFetch(401, { code: "unauthenticated", message: "Missing bearer token" }, 401, {
      code: "unauthenticated",
    });
    renderScope();
    await waitFor(() => expect(screen.getByTestId("state")).toHaveTextContent("error"));
    expect(screen.getByTestId("orgs")).toHaveTextContent("0");
    expect(screen.getByTestId("org")).toHaveTextContent("—");
    expect(screen.getByTestId("project")).toHaveTextContent("—");
    expect(screen.queryByText(FORBIDDEN_ORG_NAME)).toBeNull();
    expect(screen.queryByText("Default project")).toBeNull();
  });

  it("reports error on 500", async () => {
    stubFetch(500, { code: "internal" }, 500, { code: "internal" });
    renderScope();
    await waitFor(() => expect(screen.getByTestId("state")).toHaveTextContent("error"));
    expect(screen.getByTestId("orgs")).toHaveTextContent("0");
  });

  it("reports error when the network is unreachable", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new TypeError("Failed to fetch"))));
    renderScope();
    await waitFor(() => expect(screen.getByTestId("state")).toHaveTextContent("error"));
    expect(screen.getByTestId("org")).toHaveTextContent("—");
  });

  it("reports empty when the call succeeds with no organization", async () => {
    stubFetch(200, { organizations: [] });
    renderScope();
    await waitFor(() => expect(screen.getByTestId("state")).toHaveTextContent("empty"));
    expect(screen.getByTestId("orgs")).toHaveTextContent("0");
    expect(screen.getByTestId("org")).toHaveTextContent("—");
    expect(screen.queryByText(FORBIDDEN_ORG_NAME)).toBeNull();
  });

  it("does not expose inert migration seed scopes as a user workspace", async () => {
    stubFetch(
      200,
      {
        organizations: [
          { id: "org_default", name: "Default organization" },
          { id: "org_integ-test-project", name: "Integration fixture" },
        ],
      },
      200,
      {
        projects: [
          { id: "default", name: "Default", org_id: "org_default" },
          {
            id: "integ-test-project",
            name: "Integration Test Project",
            org_id: "org_integ-test-project",
          },
        ],
      },
    );
    renderScope();

    await waitFor(() => expect(screen.getByTestId("state")).toHaveTextContent("empty"));
    expect(screen.getByTestId("orgs")).toHaveTextContent("0");
    expect(screen.getByTestId("project")).toHaveTextContent("—");
  });

  it("requires a real project while preserving an existing zero-project organization", async () => {
    stubFetch(200, ONE_ORG, 200, { projects: [] });
    renderScope();
    await waitFor(() =>
      expect(screen.getByTestId("state")).toHaveTextContent("project_required")
    );
    expect(screen.getByTestId("org")).toHaveTextContent("Acme Media");
    expect(screen.getByTestId("project")).toHaveTextContent("—");
    expect(screen.queryByText(FORBIDDEN_ORG_NAME)).toBeNull();
  });

  it("reports ready with the org and active project resolved", async () => {
    stubFetch(200, ONE_ORG, 200, ONE_PROJECT);
    renderScope();
    await waitFor(() => expect(screen.getByTestId("state")).toHaveTextContent("ready"));
    expect(screen.getByTestId("org")).toHaveTextContent("Acme Media");
    expect(screen.getByTestId("project")).toHaveTextContent("Acme Growth");
    expect(screen.getByTestId("orgs")).toHaveTextContent("1");
  });

  it("is ready immediately from the orgs override without any fetch", () => {
    const mock = vi.fn();
    vi.stubGlobal("fetch", mock);
    renderScope([
      { id: "o1", name: "Override Org", branding: null, projects: [{ id: "p1", name: "P1" }] },
    ]);
    expect(screen.getByTestId("state")).toHaveTextContent("ready");
    expect(screen.getByTestId("org")).toHaveTextContent("Override Org");
    expect(mock).not.toHaveBeenCalled();
  });
});

describe("ScopeProvider transport", () => {
  it("sends the bearer token on both calls", async () => {
    localStorage.setItem("api_token", "tok-123");
    const mock = stubFetch(200, ONE_ORG, 200, ONE_PROJECT);
    renderScope();
    await waitFor(() => expect(screen.getByTestId("state")).toHaveTextContent("ready"));
    const urls = mock.mock.calls.map((c) => String(c[0]));
    expect(urls).toContain("/api/organizations");
    expect(urls).toContain("/api/projects");
    for (const call of mock.mock.calls) {
      const init = (call as unknown as [string, RequestInit])[1];
      const headers = new Headers(init.headers);
      expect(headers.get("Authorization")).toBe("Bearer tok-123");
    }
  });

  it("reload() re-runs the fetch and can recover from error to ready", async () => {
    let attempt = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        attempt += 1;
        // First round (both calls) fails with 401, subsequent rounds succeed.
        if (attempt <= 2) return Promise.resolve(resp(401, { code: "unauthenticated" }));
        return Promise.resolve(
          String(url).includes("/api/organizations") ? resp(200, ONE_ORG) : resp(200, ONE_PROJECT)
        );
      })
    );
    renderScope();
    await waitFor(() => expect(screen.getByTestId("state")).toHaveTextContent("error"));
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(screen.getByTestId("state")).toHaveTextContent("ready"));
    expect(screen.getByTestId("org")).toHaveTextContent("Acme Media");
  });
});
