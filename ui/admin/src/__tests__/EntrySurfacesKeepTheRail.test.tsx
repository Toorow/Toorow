/**
 * The rail, on the three surfaces that render BEFORE a project scope exists.
 *
 * `page-structure.md §A.7.1` settled it on 2026-08-04 — *"en plus y a pas le
 * menu"*: the rail stays on every surface, because leaving a page is not the
 * same act as being in it. It enumerated the five scope surfaces that existed
 * then; `/create-org`, `/onboarding`, `/invite` and `/setup` were still
 * rendering outside the shell with neither rail nor scope dialog, which is the
 * same defect reached by a third route.
 *
 * The two halves are deliberately different cases, exactly as
 * `ShellRailOnScopeSurfaces` splits them:
 *   a Project remembered  -> the rail is mounted, from `lastProjectScope()`
 *   nothing remembered    -> no rail, and the surface's own form is the only
 *                            gesture there is (the 2026-08-31 amendment). Never
 *                            a guessed Project.
 */
import { render, screen, waitFor } from "@testing-library/react";
import App from "../App";

vi.mock("../shell/AuthGate", () => ({
  default: ({ children }: { children: React.ReactNode }) => children,
}));

function mockFetch(responseMap: Record<string, unknown> = {}) {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation((url: string) => {
      for (const [key, data] of Object.entries(responseMap)) {
        if (url.includes(key)) {
          return Promise.resolve({ ok: true, status: 200, json: async () => data });
        }
      }
      return Promise.resolve({ ok: true, status: 200, json: async () => ({}) });
    }),
  );
}

function signIn(): void {
  const payload = btoa(JSON.stringify({ exp: Math.floor(Date.now() / 1000) + 3600 }));
  localStorage.setItem("api_token", `header.${payload}.signature`);
}

function rememberProject(): void {
  sessionStorage.setItem(
    "toorow_last_project_scope",
    JSON.stringify({ organizationId: "org-acme", projectId: "proj-acme" }),
  );
}

beforeEach(() => {
  signIn();
  sessionStorage.clear();
  mockFetch({
    "/api/entry-state": { state: "hosted_entry_ready" },
    "/api/organizations": { organizations: [{ id: "org-acme", name: "Acme Group" }] },
    "/api/projects": { projects: [{ id: "proj-acme", name: "Core project", org_id: "org-acme" }] },
  });
});

afterEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  window.history.replaceState({}, "", "/");
  vi.restoreAllMocks();
});

const WORKSPACES = ["ws-overview", "ws-analyze", "ws-test", "ws-data", "ws-governance", "ws-context-hub"];

describe("the rail on the entry surfaces", () => {
  it("is mounted on /create-org, from the remembered Project", async () => {
    rememberProject();
    window.history.replaceState({}, "", "/create-org");
    render(<App />);

    await waitFor(() => expect(screen.getByTestId("ws-overview")).toBeInTheDocument());
    for (const testId of WORKSPACES) expect(screen.getByTestId(testId)).toBeInTheDocument();
    // The page itself is untouched: the rail is a way OUT, not a replacement.
    expect(
      await screen.findByRole("heading", { level: 1, name: "Create organization" }),
    ).toBeInTheDocument();
  });

  it("is mounted on /onboarding too — one rule, not one route", async () => {
    rememberProject();
    window.history.replaceState({}, "", "/onboarding");
    render(<App />);

    await waitFor(() => expect(screen.getByTestId("ws-overview")).toBeInTheDocument());
  });

  it("is mounted on /invite, and the invitation still never reads the scope", async () => {
    rememberProject();
    window.history.replaceState({}, "", "/invite#invite=test-bearer");
    render(<App />);

    await waitFor(() => expect(screen.getByTestId("ws-overview")).toBeInTheDocument());
    for (const testId of WORKSPACES) expect(screen.getByTestId(testId)).toBeInTheDocument();
    const calls = (fetch as unknown as { mock: { calls: unknown[][] } }).mock.calls;
    const asked = calls.map((call) => String(call[0]));
    expect(asked.some((url) => url.includes("/api/invitations/exchange"))).toBe(true);
    expect(asked.some((url) => url.includes("/api/organizations"))).toBe(false);
  });

  it("is mounted on /setup", async () => {
    rememberProject();
    window.history.replaceState({}, "", "/setup");
    render(<App />);

    await waitFor(() => expect(screen.getByTestId("ws-overview")).toBeInTheDocument());
  });

  it("names no Project when none is remembered — the entry form is the gesture", async () => {
    window.history.replaceState({}, "", "/create-org");
    render(<App />);

    expect(
      await screen.findByRole("heading", { level: 1, name: "Create organization" }),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("ws-overview")).not.toBeInTheDocument();
  });
});
