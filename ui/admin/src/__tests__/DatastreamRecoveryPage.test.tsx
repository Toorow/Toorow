import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import DatastreamRecovery from "../shell/pages/DatastreamRecovery";

function response(body: unknown, ok = true, status = ok ? 200 : 503) {
  return { ok, status, json: async () => body } as Response;
}
function run(id: string, state: string, interval: { from: string; to_exclusive: string } | null) {
  return {
    id,
    state,
    state_changed_at: "2026-07-22T03:04:08Z",
    row_count: state === "failed" ? 0 : 42,
    plan_version_id: "plan_1",
    mapping_version_id: "mapping_1",
    error_code: state === "failed" ? "provider_timeout" : null,
    created_at: "2026-07-22T03:00:00Z",
    created_by: "operator@example.test",
    duration_seconds: 248,
    recovery_kind: null,
    recovery_interval: interval,
    import_evidence: null,
    publication_state: state === "published" ? "current" : "unpublished",
  };
}
const model = {
  datastream_id: "ds_real",
  project_id: "project-real",
  data_project_id: "project-real",
  datastream: { id: "ds_real", name: "Actual pipeline", source_kind: "managed_feed" },
  plan_versions: [],
  mapping_versions: [],
  current_published_execution_id: "run_good",
  published_execution: { id: "run_good" },
  current_candidate: null,
  latest_execution: { id: "run_failed" },
  publication_log: [],
  recent_imports: [],
  runs: [
    run("run_failed", "failed", { from: "2026-07-22", to_exclusive: "2026-07-23" }),
    run("run_good", "published", null),
  ],
};
const preparation = {
  preparation_id: "prep_1",
  kind: "reload",
  target: { datastream_id: "ds_real", project_id: "project-real" },
  target_versions: { plan_version_id: "plan_1", mapping_version_id: "mapping_1", policy_version: "policy_1" },
  interval: { from: "2026-07-22", to_exclusive: "2026-07-23" },
  impact: { calls_provider: true, touches_published_pointer: false },
  quota: { platform_known: true, estimated_points: 17, verdict: "ok", can_proceed: true },
  lock_ref: null,
  rollback_ref: "run_good",
  reason: "Retry failed run run_failed",
  expires_in_seconds: 900,
};

afterEach(() => { vi.restoreAllMocks(); });

describe("DatastreamRecovery real evidence and governed recovery", () => {
  it("reads exact scope, keeps executions without ledger and shows real LKG", async () => {
    const fetchMock = vi.fn().mockResolvedValue(response(model));
    vi.stubGlobal("fetch", fetchMock);
    render(<DatastreamRecovery projectId="project-real" datastreamId="ds_real" />);

    await screen.findByTestId("run-run_failed");
    expect(String(fetchMock.mock.calls[0][0])).toContain("/api/datastreams/ds_real/read-model?project_id=project-real");
    expect(screen.getByTestId("run-run_failed")).toHaveTextContent("No ledger enrichment");
    expect(screen.getByText(/Published execution/).parentElement).toHaveTextContent("run_good");
    expect(screen.queryByText(/Campaign performance|Meta Ads|Healthy/)).not.toBeInTheDocument();
  });

  it("prepares, reviews and confirms only the exact server interval", async () => {
    const confirmResult = {
      preparation_id: "prep_1",
      operation_id: "op_1",
      trace_id: "a".repeat(32),
      outcome: "succeeded",
      replayed: true,
      result: { candidate_execution_id: "run_retry" },
    };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response(model))
      .mockResolvedValueOnce(response(preparation))
      .mockResolvedValueOnce(response(confirmResult));
    vi.stubGlobal("fetch", fetchMock);
    render(<DatastreamRecovery projectId="project-real" datastreamId="ds_real" />);

    fireEvent.click(await screen.findByRole("button", { name: "Prepare exact retry" }));
    await screen.findByText("Review immutable proposal");
    const prepareBody = JSON.parse(String(fetchMock.mock.calls[1][1]?.body));
    expect(prepareBody).toEqual({
      project_id: "project-real",
      kind: "reload",
      date_from: "2026-07-22",
      date_to_exclusive: "2026-07-23",
      reason: "Retry failed run run_failed",
    });
    expect(prepareBody).not.toHaveProperty("estimated_points");
    expect(prepareBody).not.toHaveProperty("trace_id");
    expect(screen.getByText(/17 points - ok/)).toBeInTheDocument();
    expect(screen.getByText(/touches_published_pointer/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Confirm exact recovery" }));
    await screen.findByTestId("recovery-result");
    const confirmBody = JSON.parse(String(fetchMock.mock.calls[2][1]?.body));
    expect(confirmBody).toEqual({ project_id: "project-real", preparation_id: "prep_1" });
    expect(confirmBody).not.toHaveProperty("trace_id");
    expect(screen.getByTestId("recovery-result")).toHaveTextContent("Succeeded");
    expect(screen.getByTestId("recovery-result")).toHaveTextContent("Yes");
    expect(screen.getByTestId("recovery-result")).toHaveTextContent("op_1");
  });

  it("accepts a shared route project only when the preparation pins the owner project", async () => {
    const sharedModel = { ...model, project_id: "project-shared", data_project_id: "project-owner" };
    const sharedPreparation = {
      ...preparation,
      target: { ...preparation.target, project_id: "project-owner" },
    };
    vi.stubGlobal("fetch", vi.fn()
      .mockResolvedValueOnce(response(sharedModel))
      .mockResolvedValueOnce(response(sharedPreparation)));
    render(<DatastreamRecovery projectId="project-shared" datastreamId="ds_real" />);
    fireEvent.click(await screen.findByRole("button", { name: "Prepare exact retry" }));
    expect(await screen.findByText("Review immutable proposal")).toBeInTheDocument();
  });

  it("keeps governed 4xx refusals deterministic and rejects malformed proposals", async () => {
    const malformedFetch = vi.fn()
      .mockResolvedValueOnce(response(model))
      .mockResolvedValueOnce(response({ ...preparation, quota: undefined }));
    vi.stubGlobal("fetch", malformedFetch);
    const malformed = render(<DatastreamRecovery projectId="project-real" datastreamId="ds_real" />);
    fireEvent.click(await screen.findByRole("button", { name: "Prepare exact retry" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/contract was invalid/i);
    malformed.unmount();

    const refusalFetch = vi.fn()
      .mockResolvedValueOnce(response(model))
      .mockResolvedValueOnce(response(preparation))
      .mockResolvedValueOnce(response({ code: "stale_preparation", message: "Proposal expired" }, false, 422));
    vi.stubGlobal("fetch", refusalFetch);
    render(<DatastreamRecovery projectId="project-real" datastreamId="ds_real" />);
    fireEvent.click(await screen.findByRole("button", { name: "Prepare exact retry" }));
    fireEvent.click(await screen.findByRole("button", { name: "Confirm exact recovery" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Proposal expired");
    expect(screen.queryByTestId("outcome-unknown")).not.toBeInTheDocument();
  });

  it("does not claim success when the durable trace is absent", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response(model))
      .mockResolvedValueOnce(response(preparation))
      .mockResolvedValueOnce(response({
        preparation_id: "prep_1",
        operation_id: "op_1",
        trace_id: null,
        outcome: "succeeded",
        replayed: false,
        result: {},
      }));
    vi.stubGlobal("fetch", fetchMock);
    render(<DatastreamRecovery projectId="project-real" datastreamId="ds_real" />);
    fireEvent.click(await screen.findByRole("button", { name: "Prepare exact retry" }));
    fireEvent.click(await screen.findByRole("button", { name: "Confirm exact recovery" }));
    expect(await screen.findByTestId("outcome-unknown")).toHaveTextContent(/response was invalid/i);
  });

  it("blocks failed runs without exact intervals and marks lost confirmation outcome unknown", async () => {
    const noInterval = { ...model, runs: [run("run_failed", "failed", null)] };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(noInterval)));
    const first = render(<DatastreamRecovery projectId="project-real" datastreamId="ds_real" />);
    expect(await screen.findByRole("button", { name: "Prepare exact retry" })).toBeDisabled();
    first.unmount();

    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response(model))
      .mockResolvedValueOnce(response(preparation))
      .mockRejectedValueOnce(new Error("connection lost"));
    vi.stubGlobal("fetch", fetchMock);
    render(<DatastreamRecovery projectId="project-real" datastreamId="ds_real" />);
    fireEvent.click(await screen.findByRole("button", { name: "Prepare exact retry" }));
    fireEvent.click(await screen.findByRole("button", { name: "Confirm exact recovery" }));
    expect(await screen.findByTestId("outcome-unknown")).toHaveTextContent(/connection lost/i);
  });

  it("renders loading, empty, denied and canonical URL tabs honestly", async () => {
    let resolveRead!: (value: Response) => void;
    vi.stubGlobal("fetch", vi.fn().mockReturnValue(new Promise<Response>((resolve) => { resolveRead = resolve; })));
    const loading = render(<DatastreamRecovery projectId="project-real" datastreamId="ds_real" />);
    expect(screen.getByRole("status")).toHaveTextContent("Loading run evidence");
    resolveRead(response({ ...model, runs: [] }));
    await screen.findByTestId("runs-empty");
    loading.unmount();

    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ message: "scope denied" }, false, 403)));
    const onNavigateTab = vi.fn();
    render(<DatastreamRecovery projectId="project-real" datastreamId="ds_real" onNavigateTab={onNavigateTab} />);
    await screen.findByText("Run access unavailable");
    fireEvent.click(screen.getByRole("link", { name: "Overview" }));
    expect(onNavigateTab).toHaveBeenCalledWith("overview");
    await waitFor(() => expect(screen.getByRole("link", { name: "Data" })).toHaveAttribute("href", "/p/project-real/data/datastreams/o/datastream/ds_real/data"));
  });
});