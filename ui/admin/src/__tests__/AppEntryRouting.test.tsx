/**
 * Entry-routing tests — finding F-011.
 *
 * Before F-011 the two onboarding screens were reachable only by typing their URL, so a
 * signed-in user with no organization fell straight into the shell. <App /> now dispatches
 * on the scope contract (shell/scope.tsx):
 *   state "loading" | "error" | "empty" | "ready"
 * and an invitation in the URL wins ahead of any scope fetch.
 *
 * The scope module is mocked here so each of the five branches can be exercised
 * deterministically — the module itself is owned by F-010 and is not touched.
 */
import { render, screen, waitFor } from "@testing-library/react";
import App from "../App";

// ---------------------------------------------------------------------------
// Scope seam — a mocked ScopeProvider/useScope driven by `scope.state`.
// `scope.reads` counts useScope() calls, which proves the invitation and the
// explicit /create-org route never depend on the scope.
// ---------------------------------------------------------------------------

const scope = vi.hoisted(() => ({
  state: "ready" as "loading" | "error" | "empty" | "ready",
  reads: 0,
}));

vi.mock("../shell/scope", () => {
  const project = { id: "default", name: "Default project" };
  const org = { id: "org-acme", name: "Acme Group", branding: null, projects: [project] };
  return {
    ScopeProvider: ({ children }: { children: React.ReactNode }) => children,
    useScope: () => {
      scope.reads += 1;
      return { state: scope.state, org, orgs: [org], activeProject: project };
    },
  };
});

// The shell itself is out of scope for entry routing — stand it in.
vi.mock("../shell/ApplicationShell", () => ({
  default: () => <div data-testid="app-shell" />,
}));

/** A syntactically valid, unexpired ID token so the real AuthGate lets us through. */
function signIn(): void {
  const payload = btoa(JSON.stringify({ exp: Math.floor(Date.now() / 1000) + 3600 }));
  localStorage.setItem("api_token", `header.${payload}.signature`);
}

beforeEach(() => {
  scope.state = "ready";
  scope.reads = 0;
  signIn();
  window.history.replaceState({}, "", "/");
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({}) }),
  );
});

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// 1. An invitation in the URL wins, without waiting on the scope
// ---------------------------------------------------------------------------

describe("App entry routing — invitation", () => {
  it("renders JoinOrg for /invite#invite=<bearer> and never reads the scope", async () => {
    window.history.replaceState({}, "", "/invite#invite=test-bearer");

    render(<App />);

    expect(await screen.findByRole("dialog")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByRole("heading", { level: 1 }).textContent).toMatch(/invitation/i);
    });
    expect(screen.queryByTestId("app-shell")).not.toBeInTheDocument();
    expect(scope.reads).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// 2. No organization -> the welcome + create-organization surface
// ---------------------------------------------------------------------------

describe("App entry routing — state \"empty\"", () => {
  it("welcomes the user and offers organization creation", async () => {
    scope.state = "empty";

    render(<App />);

    expect(
      await screen.findByRole("heading", { level: 1, name: "Welcome to toorow" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { level: 2, name: "Create your organization" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create organization" })).toBeInTheDocument();
    // Nowhere to go back to: no cancel affordance on the entry surface.
    expect(screen.queryByRole("button", { name: "Cancel" })).not.toBeInTheDocument();
    expect(screen.queryByTestId("app-shell")).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// 3. Loading -> a sober labelled wait, never a blank page and never the shell
// ---------------------------------------------------------------------------

describe("App entry routing — state \"loading\"", () => {
  it("renders a loading surface instead of the shell", async () => {
    scope.state = "loading";

    render(<App />);

    expect(
      await screen.findByRole("heading", { level: 1, name: "Loading your workspace" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveAttribute("aria-busy", "true");
    expect(screen.queryByTestId("app-shell")).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// 4. Error -> an explicit failure with a retry, never a fabricated shell
// ---------------------------------------------------------------------------

describe("App entry routing — state \"error\"", () => {
  it("states the failure and offers a retry", async () => {
    scope.state = "error";

    render(<App />);

    expect(
      await screen.findByRole("heading", { level: 1, name: "We could not load your workspace" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
    expect(screen.queryByTestId("app-shell")).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// 5. Ready -> the project shell, as before
// ---------------------------------------------------------------------------

describe("App entry routing — state \"ready\"", () => {
  it("renders the project shell", async () => {
    scope.state = "ready";

    render(<App />);

    expect(await screen.findByTestId("app-shell")).toBeInTheDocument();
    expect(screen.queryByText("Welcome to toorow")).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// The explicit routes keep working (and stay scope-independent)
// ---------------------------------------------------------------------------

describe("App entry routing — explicit onboarding routes", () => {
  it("/create-org renders CreateOrg without the first-login welcome", async () => {
    window.history.replaceState({}, "", "/create-org");

    render(<App />);

    expect(
      await screen.findByRole("heading", { level: 1, name: "Create organization" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("Welcome to toorow")).not.toBeInTheDocument();
    // A deliberate route: cancelling back to the console makes sense here.
    expect(screen.getByRole("button", { name: "Cancel" })).toBeInTheDocument();
    expect(scope.reads).toBe(0);
  });

  it("/onboarding renders CreateOrg too", async () => {
    window.history.replaceState({}, "", "/onboarding");

    render(<App />);

    expect(
      await screen.findByRole("heading", { level: 1, name: "Create organization" }),
    ).toBeInTheDocument();
  });
});
