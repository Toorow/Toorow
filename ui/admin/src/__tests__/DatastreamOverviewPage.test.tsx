import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import DatastreamOverview from "../shell/pages/DatastreamOverview";

function response(body: unknown, ok = true) {
  return { ok, status: ok ? 200 : 503, json: async () => body } as Response;
}

afterEach(() => { vi.restoreAllMocks(); });

const baseModel = {
  datastream_id: "ds_real",
  project_id: "project-real",
  datastream: { id: "ds_real", name: "Actual pipeline", module_name: "meta", next_run_at: null },
  plan_versions: [],
  mapping_versions: [],
  current_published_execution_id: null,
  published_execution: null,
  current_candidate: null,
  latest_execution: null,
  publication_log: [],
  recent_imports: [],
};

describe("DatastreamOverview authoritative states", () => {
  it("does not substitute healthy mockup evidence when the read fails", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ message: "temporarily unavailable" }, false)));
    render(<DatastreamOverview projectId="project-real" datastreamId="ds_real" />);
    await screen.findByText(/Health evidence unavailable/i);
    expect(screen.queryByText(/^Healthy$/i)).not.toBeInTheDocument();
    expect(screen.queryByText("18,420")).not.toBeInTheDocument();
    expect(screen.queryByText(/22 Jul 2026/i)).not.toBeInTheDocument();
  });

  it("renders no history as unknown, never healthy", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(baseModel)));
    render(<DatastreamOverview projectId="project-real" datastreamId="ds_real" />);
    await screen.findByTestId("no-publication-history");
    expect(screen.getByTestId("no-publication-history")).toHaveTextContent(/No publication history/i);
    expect(screen.queryByText(/^Healthy$/i)).not.toBeInTheDocument();
  });

  it("keeps a last-known-good publication visible beside a newer failure", async () => {
    const onNavigateTab = vi.fn();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({
      ...baseModel,
      current_published_execution_id: "pub_1",
      published_execution: { id: "pub_1", state: "published", row_count: 42, state_changed_at: "2026-07-20T03:00:00Z", mapping_version_id: "map_1" },
      latest_execution: { id: "candidate_2", state: "failed", error_code: "provider_timeout", state_changed_at: "2026-07-21T03:00:00Z" },
    })));
    render(<DatastreamOverview projectId="project-real" datastreamId="ds_real" onNavigateTab={onNavigateTab} />);
    await screen.findByTestId("last-known-good-limitation");
    expect(screen.getByText("42")).toBeInTheDocument();
    expect(screen.getAllByText(/Latest processing failed/i).length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("link", { name: /Inspect the responsible run/i }));
    expect(onNavigateTab).toHaveBeenCalledWith("runs");
  });

  it("requests the explicit route project and encoded Datastream id", async () => {
    const fetchMock = vi.fn().mockResolvedValue(response(baseModel));
    vi.stubGlobal("fetch", fetchMock);
    render(<DatastreamOverview projectId="project real" datastreamId="ds/real" />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(String(fetchMock.mock.calls[0][0])).toContain("/ds%2Freal/read-model?project_id=project+real");
  });
});