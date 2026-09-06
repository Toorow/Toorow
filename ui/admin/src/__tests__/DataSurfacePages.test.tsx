import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import DataObjectWorkbench from "../data/DataObjectWorkbench";
import DataOverview from "../shell/pages/DataOverview";
import Imports from "../shell/pages/Imports";
import Sources from "../shell/pages/Sources";

vi.mock("../shell/router", () => ({
  buildPath: (route: { tab?: string | null }) => `/tab/${route.tab ?? "overview"}`,
  useRoute: () => ({
    route: {
      scope: "project",
      organizationId: "org-1",
      projectId: "p1",
      workspace: "data",
      section: "sources",
      objectType: "source-account",
      objectId: "object-1",
      tab: "overview",
      versionId: null,
      action: null,
      globalSurface: null,
      globalSection: null,
    },
  }),
}));

function response(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) } as Response;
}

function envelope(lens: string, items: unknown[], unavailable_reasons: unknown[] = []) {
  return {
    schema_version: `data-${lens}.v1`,
    project_ref: { object_type: "project", id: "p1" },
    generated_at: "2026-07-29T10:00:00Z",
    evidence_as_of: "2026-07-29T09:00:00Z",
    items,
    unavailable_reasons,
    allowed_actions: [],
  };
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it("renders Data Overview from server-composed lens evidence without inventing healthy state", async () => {
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(response(envelope("overview", [
    {
      object_ref: { object_type: "data-lens", id: "sources" },
      lens: "sources",
      object_count: 2,
      states: { evidence: "available" },
      evidence: {},
      evidence_as_of: "2026-07-29T09:00:00Z",
      links: {},
    },
    {
      object_ref: { object_type: "data-lens", id: "imports" },
      lens: "imports",
      object_count: 0,
      states: { evidence: "unavailable" },
      evidence: {},
      evidence_as_of: null,
      links: {},
    },
  ], [{ code: "imports_evidence_empty", message: "No owned imports evidence is available." }])))));

  render(<DataOverview projectId="p1" />);

  expect(await screen.findByRole("heading", { name: "Data Overview" })).toBeInTheDocument();
  expect(screen.getByText("Source-to-publication flow")).toBeInTheDocument();
  expect(screen.getByText("imports evidence empty")).toBeInTheDocument();
  expect(screen.getAllByText("unavailable").length).toBeGreaterThan(0);
});

it("renders Imports as managed-feed evidence and opens the exact Import identity", async () => {
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(response(envelope("imports", [{
    object_ref: { object_type: "import", id: "mfl_1" },
    datastream_ref: { object_type: "datastream", id: "ds_1" },
    name: "Daily feed import",
    source_kind: "managed_feed",
    states: { lifecycle: "failed", validation: "rejected_rows", candidate: "discarded", publication: "not_current" },
    evidence: { row_count: 10, rejected_row_count: 2, feed_format: "csv", receipt: { channel: "inbound_email" } },
    evidence_as_of: "2026-07-29T09:00:00Z",
    links: {},
  }])))));
  const open = vi.fn();

  render(<Imports projectId="p1" onOpenImport={open} />);
  fireEvent.click(await screen.findByText("Daily feed import"));

  expect(open).toHaveBeenCalledWith("mfl_1");
  // The state cell says it as a person would, never as the column stores it.
  expect(screen.getByText("Rejected rows")).toBeInTheDocument();
  expect(screen.queryByText("rejected_rows")).not.toBeInTheDocument();
  expect(screen.queryByText("rejected rows")).not.toBeInTheDocument();
  expect(screen.queryByText(/media plan/i)).not.toBeInTheDocument();

  // The row carries the evidence the contract names, all of which the server
  // already composes: channel and format, the accepted/rejected split, and the
  // candidate outcome next to the publication pointer.
  expect(screen.getByText("inbound_email · csv")).toBeInTheDocument();
  expect(screen.getByText("10 / 2")).toBeInTheDocument();
  expect(screen.getByText("Discarded")).toBeInTheDocument();
  expect(screen.getByLabelText("Search Imports")).toBeInTheDocument();
  expect(screen.getByLabelText("Filter Imports by state")).toBeInTheDocument();
  expect(screen.getByLabelText("Imports from date")).toHaveAttribute("type", "date");
  expect(screen.getByLabelText("Imports to date")).toHaveAttribute("type", "date");
});

/**
 * THE FILTERS AND THE PAGER, on the collection that carries every control.
 *
 * Every assertion below covers a control that was DRAWN and did nothing, or a
 * way back that was never offered. Imports is where the WHOLE contract can be
 * read — search, two dates, two menus, the pager and the count — which is why
 * it is asserted here; `PAGED_LENSES` (`server/core/data_surface.py`) now holds
 * `sources` and `events` beside it, and the footer those two draw is proven
 * below on Sources.
 */
const IMPORT_ROW = {
  object_ref: { object_type: "import", id: "mfl_1" },
  datastream_ref: { object_type: "datastream", id: "ds_1" },
  name: "Daily feed import",
  states: { lifecycle: "published" },
  evidence: {},
  evidence_as_of: "2026-07-29T09:00:00Z",
  links: {},
};

function importsPage(overrides: Record<string, unknown> = {}) {
  return {
    ...envelope("imports", [IMPORT_ROW]),
    total: 2,
    bound: 1,
    next_cursor: "1",
    applied_filters: {},
    filter_options: { states: ["published"], datastreams: ["ds_1", "ds_2"] },
    ...overrides,
  };
}

it("re-reads the collection when the Datastream filter changes", async () => {
  // The dead filter. `getDataSurface` has always sent `datastream=`; the effect
  // that decides WHEN to send it did not list it, so picking a Datastream
  // repainted the select and asked the server nothing.
  const fetchMock = vi.fn(() => Promise.resolve(response(importsPage())));
  vi.stubGlobal("fetch", fetchMock);

  render(<Imports projectId="p1" />);
  await screen.findByText("Daily feed import");
  fireEvent.change(screen.getByLabelText("Filter Imports by Datastream"), { target: { value: "ds_2" } });

  await waitFor(() => expect(fetchMock).toHaveBeenLastCalledWith(
    expect.stringContaining("datastream=ds_2"),
    expect.objectContaining({ cache: "no-store" }),
  ));
  // ...and the rows a person was reading are still there while the answer is
  // being fetched: a refetch is not a first load.
  expect(screen.getByText("Daily feed import")).toBeInTheDocument();
});

it("walks back a page, and offers no way back from the first", async () => {
  const fetchMock = vi.fn(() => Promise.resolve(response(importsPage())));
  vi.stubGlobal("fetch", fetchMock);

  render(<Imports projectId="p1" />);
  await screen.findByText("Daily feed import");
  expect(screen.getByRole("button", { name: "Previous page" })).toBeDisabled();

  fireEvent.click(screen.getByRole("button", { name: "Next page" }));
  await waitFor(() => expect(fetchMock).toHaveBeenLastCalledWith(expect.stringContaining("cursor=1"), expect.anything()));
  expect(screen.getByRole("button", { name: "Previous page" })).toBeEnabled();

  // Back to the page that was left — the cursor it was read with, not a reset.
  fireEvent.click(screen.getByRole("button", { name: "Previous page" }));
  await waitFor(() => expect(fetchMock).toHaveBeenLastCalledWith(expect.not.stringContaining("cursor="), expect.anything()));
  expect(screen.getByRole("button", { name: "Previous page" })).toBeDisabled();
});

it("counts the collection in a sentence, without the query's own ceiling word", async () => {
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(response(importsPage()))));

  render(<Imports projectId="p1" />);

  expect(await screen.findByText("Showing 1 of 2 Imports")).toBeInTheDocument();
  // `bound` is the server's page size. It answered no question a reader has.
  expect(screen.queryByText(/bound/i)).toBeNull();
});

it("waits for the typing to stop before re-reading, and keeps the table meanwhile", async () => {
  const fetchMock = vi.fn(() => Promise.resolve(response(importsPage())));
  vi.stubGlobal("fetch", fetchMock);

  render(<Imports projectId="p1" />);
  await screen.findByText("Daily feed import");
  const box = screen.getByLabelText("Search Imports");
  fireEvent.change(box, { target: { value: "s" } });
  fireEvent.change(box, { target: { value: "sh" } });
  fireEvent.change(box, { target: { value: "sho" } });

  // Three characters used to be three reads, and three tables replaced by
  // "Loading Imports…".
  expect(fetchMock).toHaveBeenCalledTimes(1);
  expect(screen.getByText("Daily feed import")).toBeInTheDocument();
  await waitFor(() => expect(fetchMock).toHaveBeenLastCalledWith(expect.stringContaining("q=sho"), expect.anything()));
  expect(fetchMock).toHaveBeenCalledTimes(2);
});

it("an empty FILTERED list says the filters hide the rows, and names the gesture", async () => {
  let body: unknown = importsPage();
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(response(body))));

  render(<Imports projectId="p1" />);
  await screen.findByText("Daily feed import");
  body = importsPage({ items: [], total: 0, next_cursor: null, applied_filters: { state: "published" } });
  fireEvent.change(screen.getByLabelText("Filter Imports by state"), { target: { value: "published" } });

  // The screen's own empty sentence sends a person off to import a file. Here
  // the files exist and the filter hides them, so that sentence would be a lie.
  expect(await screen.findByText("No Imports match the filters you set")).toBeInTheDocument();
  expect(screen.queryByText(/No file has been imported/)).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
  await waitFor(() => expect(screen.getByLabelText("Filter Imports by state")).toHaveValue(""));
});

const SOURCE_ROW = {
  object_ref: { object_type: "source-account", id: "sacct_1" },
  connector_ref: { object_type: "connector", id: "google" },
  label: "Alpha property",
  authorization_ref: { object_type: "source-authorization", owner_scope: "organization" },
  states: { availability: "available", authorization: "ok", freshness: "observed", usage: "used" },
  evidence: { used_by_count: 2 },
  evidence_as_of: "2026-08-16T09:00:00Z",
  links: {},
};

it("lights the Sources footer the moment the envelope carries the page facts", async () => {
  // The pager on this page has been wired since it was written and could never
  // appear: `DataCollectionLayout` renders it only when the envelope carries
  // `total` or `next_cursor`, and `_compose_collection` composed neither for
  // `sources`. No client change closed this — the server now serves them.
  const fetchMock = vi.fn(() => Promise.resolve(response({
    ...envelope("sources", [SOURCE_ROW]),
    total: 2,
    bound: 1,
    next_cursor: "1",
    applied_filters: {},
    filter_options: { states: ["available", "ok"], datastreams: [] },
  })));
  vi.stubGlobal("fetch", fetchMock);

  render(<Sources projectId="p1" />);

  expect(await screen.findByText("Alpha property")).toBeInTheDocument();
  expect(screen.getByText("Showing 1 of 2 Sources")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Previous page" })).toBeDisabled();

  fireEvent.click(screen.getByRole("button", { name: "Next page" }));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
    expect.stringContaining("cursor=1"),
    expect.objectContaining({ cache: "no-store" }),
  ));
  expect(screen.getByRole("button", { name: "Previous page" })).toBeEnabled();
});

it("shows no pager at all on an envelope that carries no page facts", async () => {
  // The other half of the same rule: a lens the server does not page must not
  // draw a control it cannot honour.
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(response(envelope("sources", [SOURCE_ROW])))));

  render(<Sources projectId="p1" />);

  expect(await screen.findByText("Alpha property")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Next page" })).toBeNull();
  expect(screen.queryByText(/^Showing /)).toBeNull();
});

const WORKBENCHES = [
  {
    lens: "sources" as const,
    schema: "sources",
    objectType: "source-account",
    tabs: ["Overview", "Accounts", "Health", "Used by"],
  },
  {
    lens: "imports" as const,
    schema: "imports",
    objectType: "import",
    tabs: ["Overview", "Raw evidence", "Validation", "Publication"],
  },
  {
    lens: "events" as const,
    schema: "events",
    objectType: "event-configuration",
    tabs: ["Overview", "Source mapping", "Collection", "Usage"],
  },
  {
    lens: "connectors" as const,
    schema: "connectors",
    objectType: "connector",
    tabs: ["Overview", "Capabilities", "Coverage", "Versions"],
  },
] as const;

it.each(WORKBENCHES)("uses the shared exact tab contract for $objectType", async ({ lens, schema, objectType, tabs }) => {
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(response(envelope(schema, [{
    object_ref: { object_type: objectType, id: "object-1" },
    name: "Owned object",
    states: { lifecycle: "unavailable" },
    evidence: {},
    evidence_as_of: null,
    links: {},
  }])))));
  const navigate = vi.fn();
  const tabContract = tabs.map((label) => ({ key: label.toLowerCase().replaceAll(" ", "-"), label }));

  render(
    <DataObjectWorkbench
      projectId="p1"
      lens={lens}
      objectId="object-1"
      tab="overview"
      tabs={tabContract}
      onNavigateTab={navigate}
    />,
  );

  expect(await screen.findByText("Owned object")).toBeInTheDocument();
  for (const label of tabs) expect(screen.getByRole("link", { name: label })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("link", { name: tabs[1] }));
  expect(navigate).toHaveBeenCalledWith(tabContract[1].key);
});
it("renders structured tab evidence as named fields, never as a JSON blob", async () => {
  // The Connector `Capabilities` tab is the worst case: its evidence is the
  // whole normalized contract. It used to reach the screen through
  // `JSON.stringify(value, null, 2)` inside a `<pre>`, which is a debug view,
  // not the field-by-field tab the acceptance criteria describe.
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(response(envelope("connectors", [{
    object_ref: { object_type: "connector", id: "generic" },
    name: "Generic",
    connector_id: "generic",
    states: { activation: "installed" },
    evidence: {
      contract: {
        auth_kind: "oauth",
        supported_grains: ["day", "week"],
        history_limit_days: 90,
        quota: { daily_calls: 1000, burst: 20 },
      },
    },
    evidence_as_of: "2026-07-29T09:00:00Z",
    links: {},
  }])))));

  render(
    <DataObjectWorkbench
      projectId="p1"
      lens="connectors"
      objectId="generic"
      tab="capabilities"
      tabs={[{ key: "overview", label: "Overview" }, { key: "capabilities", label: "Capabilities" }]}
      onNavigateTab={vi.fn()}
    />,
  );

  // Each contract field is its own labelled row...
  expect(await screen.findByText("Auth kind")).toBeInTheDocument();
  expect(screen.getByText("oauth")).toBeInTheDocument();
  expect(screen.getByText("History limit days")).toBeInTheDocument();
  expect(screen.getByText("90")).toBeInTheDocument();
  // ...lists read as their members...
  expect(screen.getByText("day, week")).toBeInTheDocument();
  // ...and one nested level is expanded rather than dumped.
  expect(screen.getByText("Quota · Daily calls")).toBeInTheDocument();
  expect(screen.getByText("1000")).toBeInTheDocument();
  // Nothing on the page is raw JSON.
  expect(screen.queryByText(/^\{/)).not.toBeInTheDocument();
  expect(document.querySelector("pre")).toBeNull();
});

/**
 * `state_counts` — the server computed it and this screen threw it away.
 *
 * `_compose_overview` (`server/core/data_surface.py:947`) counts every object's
 * state ON EACH AXIS. Data Overview read `object_count` alone, so "12
 * Datastreams" was the whole answer: eleven published and one failed looked
 * exactly like twelve never run. `screens/expectations.json` names the promise
 * — "afficher la repartition des etats, pas seulement un compteur" — and the
 * generated page listed it MANQUE, step 5 of the founding journey.
 *
 * It is the README's most frequent class, `route` without `calls`: the server
 * computes, the screen throws it away. Which is why the assertion below is on
 * the BREAKDOWN and not on the count — a test that checked the count would have
 * passed for the whole time the defect existed.
 */
it("shows the per-axis state breakdown the server sends, not only a counter", async () => {
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(response(envelope("overview", [
    {
      object_ref: { object_type: "data-lens", id: "datastreams" },
      lens: "datastreams",
      object_count: 12,
      states: { evidence: "available" },
      evidence: {
        state_counts: {
          publication: { published: 11, failed: 1 },
          collection: { collecting: 12 },
        },
      },
      evidence_as_of: "2026-07-29T09:00:00Z",
      links: {},
    },
  ])))));

  render(<DataOverview projectId="p1" />);

  const stage = await screen.findByTestId("data-overview-breakdown-datastreams");
  // Both axes, each on its own line: Data Overview's contract is that
  // independent axes never collapse into one compensating score.
  expect(stage.textContent).toContain("publication");
  expect(stage.textContent).toContain("collection");
  // And the split itself — the eleven and the one that the counter hid.
  expect(stage.textContent).toContain("11");
  expect(stage.textContent).toContain("published");
  expect(stage.textContent).toContain("1");
  expect(stage.textContent).toContain("failed");
});

it("says a lens reported no breakdown, rather than letting absent read as fine", async () => {
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(response(envelope("overview", [
    {
      object_ref: { object_type: "data-lens", id: "datastreams" },
      lens: "datastreams",
      object_count: 4,
      states: { evidence: "available" },
      evidence: {},
      evidence_as_of: "2026-07-29T09:00:00Z",
      links: {},
    },
  ])))));

  render(<DataOverview projectId="p1" />);

  // Four objects and no breakdown is NOT four healthy objects. Rendering
  // nothing here would let the reader complete the sentence themselves.
  expect(await screen.findByTestId("data-overview-breakdown-absent-datastreams")).toBeInTheDocument();
  expect(screen.queryByTestId("data-overview-breakdown-datastreams")).toBeNull();
});
