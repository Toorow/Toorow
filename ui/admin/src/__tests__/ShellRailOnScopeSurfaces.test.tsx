/**
 * The rail, on the addresses that used to render without one.
 *
 * `resolveRailScope` is unit-tested next door, but a pure function returning a
 * Project proves nothing about a page having a menu: the shell has to MOUNT the
 * rail, and this is the assertion that was missing when Getting Started shipped
 * with no navigation. It renders the whole `<App />` at each scope address and
 * looks for the six workspaces.
 *
 * The two halves are deliberately different cases:
 *   Getting Started       carries a Project in its address
 *   Organization Settings carries none, so the rail comes from the remembered
 *                         scope — and with nothing remembered there is no rail,
 *                         which is the third test rather than a guessed Project.
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

beforeEach(() => {
  signIn();
  sessionStorage.clear();
  mockFetch({
    "/api/organizations": { organizations: [{ id: "org-acme", name: "Acme Group" }] },
    "/api/projects": { projects: [{ id: "proj-acme", name: "Core project", org_id: "org-acme" }] },
  });
});

afterEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  vi.restoreAllMocks();
});

const WORKSPACES = ["ws-overview", "ws-analyze", "ws-test", "ws-data", "ws-governance", "ws-context-hub"];

describe("the rail on the scope surfaces", () => {
  it("is mounted on Getting Started, whose address carries the Project", async () => {
    window.history.replaceState({}, "", "/org/org-acme/project/proj-acme/getting-started");
    render(<App />);

    await waitFor(() => expect(screen.getByTestId("ws-overview")).toBeInTheDocument());
    for (const testId of WORKSPACES) expect(screen.getByTestId(testId)).toBeInTheDocument();
  });

  it("is mounted on Project Settings", async () => {
    window.history.replaceState({}, "", "/org/org-acme/project/proj-acme/settings/general");
    render(<App />);

    await waitFor(() => expect(screen.getByTestId("ws-overview")).toBeInTheDocument());
  });

  it("is mounted on Organization Settings, from the remembered Project", async () => {
    sessionStorage.setItem(
      "toorow_last_project_scope",
      JSON.stringify({ organizationId: "org-acme", projectId: "proj-acme" }),
    );
    window.history.replaceState({}, "", "/org/org-acme/settings/general");
    render(<App />);

    await waitFor(() => expect(screen.getByTestId("ws-overview")).toBeInTheDocument());
  });
});
