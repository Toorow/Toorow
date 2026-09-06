import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import GettingStarted, { type SetupJourney } from "../shell/pages/GettingStarted";

const PROJECT_ID = "proj_01KYJ0NPEXAMPLEULID000000";

const JOURNEY: SetupJourney = {
  schema_version: "getting-started.v2",
  project: { id: PROJECT_ID, name: "Kardinal Media", organization: { id: "org-1", name: "Kardinal" } },
  materialization: { state: "materialized", action: null, journal_behind: false, explanation: "The journey is recorded." },
  journey: { id: "journey-1", state: "active", progress: { completed: 1, total: 2, percent: 50 }, next_task_id: "task-2" },
  tasks: [
    { id: "task-1", step_key: "project_foundation", title: "Confirm Project foundation", state: "completed", owner: { label: "Project settings" }, actions: [] },
    { id: "task-2", step_key: "source", title: "Authorize a Source", state: "blocked", owner: { identity: "owner@example.com" }, blocker: "Source evidence is unavailable", actions: ["prepare_handoff"] },
  ],
  history: [{ event_id: "event-1", task_id: "task-1", event_type: "completed", actor: "owner@example.com", occurred_at: "2026-07-29T10:00:00Z" }],
  readiness: { version: "ready-v1", components: ["project_foundation", "source", "datastream", "governance", "first_value"], project_foundation: "ready", source: "blocked", datastream: "blocked", governance: "blocked", first_value: "blocked" },
};

/** A Project whose journey was never created: the bootstrap left the GET on
 *  2026-08-17, so this state is now reachable and must name its gesture. */
const NOT_STARTED: SetupJourney = {
  ...JOURNEY,
  materialization: { state: "pending", action: "start_journey", journal_behind: false, explanation: "This Project's setup journey has not been created yet." },
  journey: { id: null, state: "not_started", progress: { completed: 0, total: 0, percent: 0 }, next_task_id: null },
  tasks: [],
  history: [],
};

afterEach(() => vi.unstubAllGlobals());

describe("Getting Started authoritative journey", () => {
  it("renders server readiness, next work, owner and retained history", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => JOURNEY }));
    render(<GettingStarted projectId={PROJECT_ID} />);
    expect(await screen.findByText("Authorize a Source")).toBeInTheDocument();
    expect(screen.getByText("Owner: owner@example.com")).toBeInTheDocument();
    expect(screen.getByText("Readiness version: ready-v1")).toBeInTheDocument();
    expect(screen.getAllByText("completed").length).toBeGreaterThan(0);
  });

  it("preserves an explicit unavailable state instead of demo data", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
    render(<GettingStarted projectId={PROJECT_ID} />);
    expect(await screen.findByText(/No task was marked complete/i)).toBeInTheDocument();
    expect(screen.queryByText("Authorize a Source")).not.toBeInTheDocument();
  });

  it("never prints the raw Project identifier, loaded or not", async () => {
    // THE MEASURED DEFECT. The eyebrow read "Project coordination · proj_01K…"
    // because the page passed `scopeLabel={projectId}`. It names the Project now,
    // and while the envelope is in flight it says "Project" -- never an id.
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => JOURNEY }));
    const { container } = render(<GettingStarted projectId={PROJECT_ID} />);
    expect(container.textContent).not.toContain(PROJECT_ID);
    expect(await screen.findByText(/Kardinal \/ Kardinal Media/)).toBeInTheDocument();
    expect(container.textContent).not.toContain(PROJECT_ID);
  });

  it("shows every readiness component the server names, Governance included", async () => {
    // Four hardcoded spans hid the fifth component, so the step that capped every
    // journey at 4/5 was not even visible on the surface that owns it.
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => JOURNEY }));
    render(<GettingStarted projectId={PROJECT_ID} />);
    expect(await screen.findByText("Governance: blocked")).toBeInTheDocument();
    expect(screen.getByText("First value: blocked")).toBeInTheDocument();
  });

  it("names the gesture that creates a journey instead of creating one on sight", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({ ok: true, status: 200, json: async () => NOT_STARTED })
      .mockResolvedValueOnce({ ok: true, status: 200, json: async () => JOURNEY })
      .mockResolvedValueOnce({ ok: true, status: 200, json: async () => JOURNEY });
    vi.stubGlobal("fetch", fetchMock);
    render(<GettingStarted projectId={PROJECT_ID} />);

    expect(await screen.findByText("This Project has no setup journey yet")).toBeInTheDocument();
    expect(screen.getByText("No steps are recorded yet")).toBeInTheDocument();
    // The FIRST call is a GET: opening the page creates nothing.
    expect(fetchMock.mock.calls[0][1]?.method ?? "GET").toBe("GET");

    await userEvent.click(screen.getByRole("button", { name: "Start setup journey" }));
    const write = fetchMock.mock.calls.find((call) => call[1]?.method === "POST");
    expect(write).toBeDefined();
    expect(String(write![0])).toContain("/getting-started/journey");
  });
});
