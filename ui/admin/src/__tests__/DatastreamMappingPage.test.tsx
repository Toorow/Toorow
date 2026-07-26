import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import DatastreamMapping from "../shell/pages/DatastreamMapping";

function response(body: unknown, ok = true, status = ok ? 200 : 503) {
  return { ok, status, json: async () => body } as Response;
}
function version(id: string, versionNumber: number, fieldId: string) {
  return {
    id,
    datastream_id: "ds_real",
    project_id: "project-real",
    version_number: versionNumber,
    plan_version_id: "plan_1",
    executable: true,
    blocking_count: 0,
    ossie_spec_version: "0.1.1",
    created_at: `2026-07-2${versionNumber}T10:00:00Z`,
    mapping_payload: {
      mapping_contract_version: "1",
      source_schema_hash: "a".repeat(64),
      plan_version_id: "plan_1",
      grain: ["event_day"],
      ambiguities: [],
      fields: [{
        field_id: fieldId,
        physical_type: "decimal",
        profile: { nullable: false, unique: false, cardinality_signal: "medium", sample_values: ["SENSITIVE_SAMPLE"], confidence: 0.91 },
        suggestion: { semantic_role: "measure_spend", aggregation: "sum", non_additive: false, currency: "EUR", sensitivity: "financial", status: "suggested", evidence: ["capability field catalog"] },
        binding: { canonical_target: "cost", mdm_target: null, status: "confirmed", blocking_reason: null, confirmed_by: "operator@example.test", confirmed_reason: "Verified contract" },
      }],
    },
    ossie_projection: { ossie_spec_version: "0.1.1", semantic_model: { metrics: [fieldId] } },
  };
}
const model = {
  datastream_id: "ds_real",
  project_id: "project-real",
  datastream: { id: "ds_real", name: "Actual pipeline", module_name: "meta" },
  mapping_versions: [version("map_candidate", 2, "candidate_cost"), version("map_published", 1, "published_cost")],
  published_execution: { id: "exec_1", mapping_version_id: "map_published" },
};

afterEach(() => { vi.restoreAllMocks(); });

describe("DatastreamMapping governed read-only evidence", () => {
  it("uses apiFetch scope and distinguishes published mapping from the latest candidate", async () => {
    const fetchMock = vi.fn().mockResolvedValue(response(model));
    vi.stubGlobal("fetch", fetchMock);
    render(<DatastreamMapping projectId="project-real" datastreamId="ds_real" />);

    await screen.findAllByText("published_cost");
    expect(String(fetchMock.mock.calls[0][0])).toContain("/api/datastreams/ds_real/read-model?project_id=project-real");
    expect(screen.getByLabelText("Evidence version")).toHaveValue("map_published");
    expect(screen.getByText("Latest candidate").parentElement).toHaveTextContent("v2");
    expect(screen.queryByText("candidate_cost")).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Evidence version"), { target: { value: "map_candidate" } });
    expect((await screen.findAllByText("candidate_cost")).length).toBeGreaterThan(0);
  });

  it("renders persisted profile, binding and Ossie evidence without mock values", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(model)));
    render(<DatastreamMapping projectId="project-real" datastreamId="ds_real" />);

    await screen.findAllByText("published_cost");
    expect(screen.getAllByText(/91% confidence/i).length).toBeGreaterThan(0);
    expect(screen.queryByText("SENSITIVE_SAMPLE")).not.toBeInTheDocument();
    expect(screen.getAllByText(/measure_spend/i).length).toBeGreaterThan(0);
    expect(screen.getByText(/operator@example\.test/i)).toBeInTheDocument();
    expect(screen.getByText(/capability field catalog/i)).toBeInTheDocument();
    expect(screen.getByText(/"ossie_spec_version": "0\.1\.1"/i)).toBeInTheDocument();
    expect(screen.queryByText(/Campaign performance/i)).not.toBeInTheDocument();
  });

  it("keeps loading and denied states explicit", async () => {
    let resolveRead!: (value: Response) => void;
    vi.stubGlobal("fetch", vi.fn().mockReturnValue(new Promise<Response>((resolve) => { resolveRead = resolve; })));
    const loading = render(<DatastreamMapping projectId="project-real" datastreamId="ds_real" />);
    expect(screen.getByRole("status")).toHaveTextContent(/Loading mapping evidence/);
    resolveRead(response({ ...model, mapping_versions: [], published_execution: null }));
    await screen.findByTestId("mapping-empty");
    loading.unmount();

    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ message: "scope denied" }, false, 403)));
    render(<DatastreamMapping projectId="project-real" datastreamId="ds_real" />);
    expect(await screen.findByText("Mapping access unavailable")).toBeInTheDocument();
    expect(screen.queryByText("published_cost")).not.toBeInTheDocument();
  });
  it("shows explicit empty and failed states without fallback fields", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response({ ...model, mapping_versions: [], published_execution: null }))
      .mockResolvedValueOnce(response({ message: "temporarily unavailable" }, false));
    vi.stubGlobal("fetch", fetchMock);
    const first = render(<DatastreamMapping projectId="project-real" datastreamId="ds_real" />);
    await screen.findByTestId("mapping-empty");
    expect(screen.queryByText("spend")).not.toBeInTheDocument();
    first.unmount();
    render(<DatastreamMapping projectId="project-real" datastreamId="ds_real" />);
    await screen.findByText("Mapping evidence unavailable");
    expect(screen.queryByText("spend")).not.toBeInTheDocument();
  });

  it("rejects a wrong-scope success response", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ ...model, project_id: "project-other" })));
    render(<DatastreamMapping projectId="project-real" datastreamId="ds_real" />);
    await screen.findByText("Mapping evidence unavailable");
    expect(screen.queryByText("published_cost")).not.toBeInTheDocument();
  });

  it("keeps mutations disabled and routes local tabs canonically", async () => {
    const onNavigateTab = vi.fn();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(model)));
    render(<DatastreamMapping projectId="project-real" datastreamId="ds_real" onNavigateTab={onNavigateTab} />);
    await screen.findAllByText("published_cost");

    expect(screen.getByRole("button", { name: /Publish mapping unavailable/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /Confirm suggestion unavailable/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /Change binding unavailable/i })).toBeDisabled();
    fireEvent.click(screen.getByRole("link", { name: "Overview" }));
    expect(onNavigateTab).toHaveBeenCalledWith("overview");
    await waitFor(() => expect(screen.getByRole("link", { name: "Data" })).toHaveAttribute("href", "/p/project-real/data/datastreams/o/datastream/ds_real/data"));
  });
});