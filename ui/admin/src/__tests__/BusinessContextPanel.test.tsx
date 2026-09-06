import { fireEvent, render, screen } from "@testing-library/react";
import BusinessContextPanel from "../BusinessContextPanel";

const navigate = vi.fn();
const ROUTE = {
  scope: "project",
  organizationId: "org-1",
  projectId: "p1",
  workspace: "data",
  section: "events",
  lens: null,
  objectType: null,
  objectId: null,
  tab: null,
  versionId: null,
  action: null,
  query: {},
  globalSurface: null,
  globalSection: null,
};

// The panel now BUILDS an address, so the router it will be resolved by is the
// one that composes it. Mocked here rather than provided, because a real
// `RouterProvider` would parse the test runner's own location.
vi.mock("../shell/router", () => ({
  buildPath: (route: { workspace: string; section: string }) => `/org/org-1/project/p1/${route.workspace}/${route.section}`,
  parsePath: () => ({ kind: "resolved" }),
  useRoute: () => ({ route: ROUTE, navigate }),
}));

function envelope(items: unknown[]) {
  return {
    schema_version: "data-events.v1",
    project_ref: { object_type: "project", id: "p1" },
    generated_at: "2026-07-29T10:00:00Z",
    evidence_as_of: "2026-07-29T09:00:00Z",
    items,
    unavailable_reasons: [],
    allowed_actions: ["event-configuration.create"],
  };
}

const CONFIGURATION = {
  object_ref: { object_type: "event-configuration", id: "ecfg_1" },
  datastream_ref: { object_type: "datastream", id: "ds_1" },
  name: "Launch observations",
  states: { lifecycle: "active", review: "active", binding: "pinned" },
  evidence: { source_mapping: { source_ref: "report:event" } },
  evidence_as_of: "2026-07-29T09:00:00Z",
  links: {},
};

function response(body: unknown, ok = true, status = ok ? 200 : 503): Response {
  return { ok, status, json: async () => body, text: async () => JSON.stringify(body) } as Response;
}

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); navigate.mockReset(); });

it("renders Datastream-owned Event Configurations instead of timeline observations", async () => {
  const fetchMock = vi.fn(() => Promise.resolve(response(envelope([CONFIGURATION]))));
  vi.stubGlobal("fetch", fetchMock);
  render(<BusinessContextPanel projectId="p1" />);
  expect(await screen.findByText("Launch observations")).toBeInTheDocument();
  expect(screen.getByText("ds_1")).toBeInTheDocument();
  expect(fetchMock).toHaveBeenCalledWith("/api/projects/p1/event-configurations", expect.objectContaining({ cache: "no-store" }));
  expect(screen.queryByRole("form", { name: /Add context event/i })).not.toBeInTheDocument();
});

it("opens the exact Event Configuration owner", async () => {
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(response(envelope([CONFIGURATION])))));
  const open = vi.fn();
  render(<BusinessContextPanel projectId="p1" onOpenEventConfiguration={open} />);
  fireEvent.click(await screen.findByText("Launch observations"));
  expect(open).toHaveBeenCalledWith("ecfg_1");
});

it("names the gesture that fills the empty list, and makes it reachable from here", async () => {
  // The sentence sent a person to a Datastream and the screen offered no way to
  // one: a dead end that read as guidance.
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(response(envelope([])))));
  render(<BusinessContextPanel projectId="p1" />);

  const link = await screen.findByRole("link", { name: "Open Datastreams" });
  expect(link).toHaveAttribute("href", "/org/org-1/project/p1/data/datastreams");
  fireEvent.click(link);
  expect(navigate).toHaveBeenCalledWith(expect.objectContaining({ workspace: "data", section: "datastreams" }));
});

it("does not substitute Business Timeline rows on malformed evidence", async () => {
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(response({ events: [{ label: "Legacy launch" }] }))));
  render(<BusinessContextPanel projectId="p1" />);
  // TWO INDEPENDENT READERS live on this surface since 2026-08-17: the
  // Datastream-owned Event Configurations above, and the manual annotations
  // below. This stub answers the same malformed body to both, so both refuse it
  // — `findByRole` singular would now fail on the SECOND honest refusal, which
  // is why this asserts the sentence rather than the count.
  const alerts = await screen.findAllByRole("alert");
  expect(alerts.some((alert) => /malformed/i.test(alert.textContent ?? ""))).toBe(true);
  // The point of the case is unchanged: neither reader invents a row out of a
  // shape it could not read.
  expect(screen.queryByText("Legacy launch")).not.toBeInTheDocument();
});