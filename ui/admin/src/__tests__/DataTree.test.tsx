import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import DataTree from "../shell/DataTree";
import { RouterProvider } from "../shell/router";

function envelope(items: unknown[]) {
  return {
    schema_version: "data-datastreams.v1",
    project_ref: { object_type: "project", id: "proj_test" },
    generated_at: "2026-07-29T10:00:00Z",
    evidence_as_of: "2026-07-29T09:00:00Z",
    items,
    unavailable_reasons: [],
    allowed_actions: [],
  };
}

function item(id: string, name: string, publication: string) {
  return {
    object_ref: { object_type: "datastream", id },
    name,
    source_kind: "connector_pull",
    connector_ref: { object_type: "connector", id: "generic" },
    states: { lifecycle: "active", validation: "executable", publication, health: "unavailable", freshness: "observed" },
    evidence: {},
    evidence_as_of: "2026-07-29T09:00:00Z",
    links: {},
  };
}

function renderTree() {
  window.history.replaceState({}, "", "/org/org-test/project/proj_test/data/datastreams");
  return render(<RouterProvider><DataTree /></RouterProvider>);
}

function response(body: unknown, ok = true, status = ok ? 200 : 503): Response {
  return { ok, status, json: async () => body, text: async () => JSON.stringify(body) } as Response;
}

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it("reads the canonical Project Datastream envelope and no legacy join", async () => {
  const fetchMock = vi.fn(() => Promise.resolve(response(envelope([
    item("ds_001", "Paid data", "published"),
    item("ds_002", "Web data", "failed"),
  ]))));
  vi.stubGlobal("fetch", fetchMock);
  renderTree();
  expect(await screen.findByTestId("stream-ds_001")).toHaveTextContent("Paid data");
  expect(fetchMock).toHaveBeenCalledWith(
    "/api/projects/proj_test/datastreams",
    expect.objectContaining({ cache: "no-store" }),
  );
  expect(screen.getByTestId("stream-ds_002").querySelector('[title="Needs attention"]')).not.toBeNull();
  expect(screen.getByTestId("stream-ds_001").querySelector('[title="Needs attention"]')).toBeNull();
});

it("does not infer that unavailable health needs attention", async () => {
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(response(envelope([item("ds_001", "Unknown health", "unavailable")])))));
  renderTree();
  expect(await screen.findByTestId("stream-ds_001")).toBeInTheDocument();
  expect(screen.getByTestId("stream-ds_001").querySelector('[title="Needs attention"]')).toBeNull();
});

it("renders honest error and empty states", async () => {
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(response({ code: "unavailable" }, false, 503))
    .mockResolvedValueOnce(response(envelope([])));
  vi.stubGlobal("fetch", fetchMock);
  const user = userEvent.setup();
  renderTree();
  expect(await screen.findByTestId("tree-error")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Retry" }));
  expect(await screen.findByTestId("tree-empty")).toHaveTextContent(/No Datastream yet/i);
  expect(screen.queryByLabelText("Find Datastreams")).not.toBeInTheDocument();
});

it("opens the exact canonical Datastream object route", async () => {
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(response(envelope([item("ds_001", "Paid data", "published")])))));
  const user = userEvent.setup();
  renderTree();
  await user.click(await screen.findByTestId("stream-ds_001"));
  await waitFor(() => expect(window.location.pathname).toBe(
    "/org/org-test/project/proj_test/data/datastreams/object/datastream/ds_001/tab/overview",
  ));
});