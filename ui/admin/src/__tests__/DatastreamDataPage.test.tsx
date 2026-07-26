import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import DatastreamData from "../shell/pages/DatastreamData";

function response(body: unknown, ok = true) {
  return { ok, status: ok ? 200 : 503, json: async () => body } as Response;
}

const emptyResponse = {
  datastream_id: "ds_real",
  project_id: "project-real",
  datastream: { id: "ds_real", name: "Actual pipeline", module_name: "meta" },
  stage: "published",
  served_stage: "processed",
  stage_note: "Published has no distinct materialisation; processed evidence was served.",
  collection_expected: true,
  materialization_available: true,
  sample_watermark: null,
  date_from: "2026-07-20",
  date_to: "2026-07-26",
  limit: 5,
  masked_fields: [],
  masked_value_count: 0,
  version_binding_available: false,
  days: [{ date: "2026-07-26", sampled_row_count: 0, rejection_count: 0, field_count: 8, rows: [] }],
};

afterEach(() => { vi.restoreAllMocks(); });

describe("DatastreamData governed samples", () => {
  it("does not substitute fictional sample rows after a failed read", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ message: "warehouse unavailable" }, false)));
    render(<DatastreamData projectId="project-real" datastreamId="ds_real" />);
    await screen.findByText(/Sample evidence unavailable/i);
    expect(screen.queryByText(/Search · Brand/i)).not.toBeInTheDocument();
    expect(screen.queryByText("18,420")).not.toBeInTheDocument();
  });

  it("requests explicit project scope and keeps the requested stage visible", async () => {
    const fetchMock = vi.fn().mockResolvedValue(response({
      ...emptyResponse,
      project_id: "project real",
      datastream_id: "ds/real",
      datastream: { ...emptyResponse.datastream, id: "ds/real" },
    }));
    vi.stubGlobal("fetch", fetchMock);
    render(<DatastreamData projectId="project real" datastreamId="ds/real" />);
    await screen.findByTestId("zero-sample-rows");
    expect(String(fetchMock.mock.calls[0][0])).toContain("/ds%2Freal/sample?");
    expect(String(fetchMock.mock.calls[0][0])).toContain("project_id=project+real");
    expect(screen.getByRole("combobox", { name: "Stage" })).toHaveValue("published");
    expect(screen.getByText(/Requested Published/i)).toBeInTheDocument();
  });

  it("distinguishes collection disabled from a missing materialisation", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response({ ...emptyResponse, collection_expected: false }))
      .mockResolvedValueOnce(response({ ...emptyResponse, materialization_available: false }));
    vi.stubGlobal("fetch", fetchMock);
    const view = render(<DatastreamData projectId="project-real" datastreamId="ds_real" />);
    await screen.findByTestId("collection-not-expected");
    view.unmount();
    render(<DatastreamData projectId="project-real" datastreamId="ds_real" />);
    await screen.findByTestId("materialization-missing");
  });

  it("labels returned N as sampled rows and never as published volume", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({
      ...emptyResponse,
      served_stage: "published",
      stage_note: null,
      sample_watermark: "2026-07-26",
      masked_fields: ["user_email"],
      masked_value_count: 2,
      days: [{ date: "2026-07-26", sampled_row_count: 2, rejection_count: 0, field_count: 2, rows: [{ metric: "m1", user_email: "[MASKED]" }, { metric: "m2", user_email: "[MASKED]" }] }],
    })));
    render(<DatastreamData projectId="project-real" datastreamId="ds_real" />);
    await screen.findByText(/First 2 eligible rows/i);
    expect(screen.getByText(/First 2 eligible rows/i).closest(".sample-day")).toHaveTextContent(/2 sampled/i);
    expect(screen.queryByText(/2 published rows/i)).not.toBeInTheDocument();
    expect(screen.getByText(/2 values masked/i)).toBeInTheDocument();
  });

  it("loads the selected stage without replacing it with served_stage", async () => {
    const fetchMock = vi.fn().mockResolvedValue(response(emptyResponse));
    vi.stubGlobal("fetch", fetchMock);
    render(<DatastreamData projectId="project-real" datastreamId="ds_real" />);
    await screen.findByTestId("zero-sample-rows");
    fireEvent.change(screen.getByRole("combobox", { name: "Stage" }), { target: { value: "mapped" } });
    fireEvent.click(screen.getByRole("button", { name: /Load sample/i }));
    await waitFor(() => expect(fetchMock.mock.calls.length).toBe(2));
    expect(String(fetchMock.mock.calls[1][0])).toContain("stage=mapped");
    expect(screen.getByRole("combobox", { name: "Stage" })).toHaveValue("mapped");
  });
});