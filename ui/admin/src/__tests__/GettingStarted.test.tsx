import { render, screen, waitFor } from "@testing-library/react";
import GettingStarted, { type SetupJourney } from "../shell/pages/GettingStarted";

const JOURNEY: SetupJourney = {
  journey_id: "journey-1",
  organization_id: "org-real",
  project_id: "project-real",
  state: "in_progress",
  operator: "Real Operator",
  progress: { completed: 0, total: 1 },
  tasks: [
    {
      task_id: "task-1",
      safe_id_suffix: "A1",
      step_key: "source_authorization",
      title: "Authorize source",
      actor_type: "operator",
      owner: "Real Owner",
      state: "blocked",
      expires_at: null,
      handoff_method: "none",
      reminder: { mode: "none", label: "None" },
      return_condition: { kind: "binding", resource_id: "project-real" },
      return_path: "/getting-started",
      safe_scope: {
        role: "Owner",
        expected_source: "Server source",
        exposed_account: "Explicit none",
      },
      blocker: "Waiting for authorization",
      actions: [],
    },
  ],
};

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("GettingStarted authoritative states", () => {
  it("shows a recoverable error and never substitutes a demo journey", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));

    render(<GettingStarted projectId="project-real" />);

    expect(await screen.findByRole("alert")).toHaveTextContent(/offline/i);
    expect(screen.queryByText(/Acme France|Camille|Louis|Google Ads/i)).not.toBeInTheDocument();
  });

  it("renders only scope and responsibilities returned by the server", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => JOURNEY,
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<GettingStarted projectId="project-real" />);

    expect(await screen.findByRole("heading", { name: "Getting started" })).toBeInTheDocument();
    expect(screen.getAllByText("org-real").length).toBeGreaterThan(0);
    expect(screen.getAllByText("project-real").length).toBeGreaterThan(0);
    expect(screen.getByText("Real Operator’s access")).toBeInTheDocument();
    expect(screen.getByText("Server source")).toBeInTheDocument();
    expect(screen.getByText("Explicit none")).toBeInTheDocument();
    expect(screen.queryByText(/Acme France|Acquisition Europe|Camille/i)).not.toBeInTheDocument();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
  });

  it("refuses the placeholder project without calling the API", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    render(<GettingStarted projectId="default" />);

    expect(await screen.findByRole("alert")).toHaveTextContent(/select a project/i);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
