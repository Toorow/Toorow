/**
 * Data > Datastreams — the fleet screen.
 *
 * Rewritten with the screen (2026-08-02). The previous suite pinned a table of
 * five status words, which is what Jean asked to be thrown away. Two of its
 * assertions protected something real and are kept, sharpened:
 *
 *   - the five state axes are NOT collapsed into one word;
 *   - a failed or empty read is never dressed up as a healthy fleet.
 *
 * What is new is the other half: the screen must show what the server already
 * sends — cadence, next run, publication, exceptions — and must say plainly what
 * the read model cannot give it, rather than dropping the column.
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import DataWorkspace from "../shell/pages/DataWorkspace";
import { parsePath } from "../shell/router";
import { executionStateLabel } from "../datastreams/workbench/executionStates";
import { evidenceHrefFor } from "../datastreams/workbench/DatastreamIssueBadge";
import { connectorName } from "../ui";

/** A Datastream that needs attention is named TWICE on purpose — once in the
 *  banner and once in its row — so every query here says which one it means.
 *  A bare `getByText` matches both and fails, which is the correct outcome for
 *  an ambiguous question rather than a reason to weaken the screen. */
async function fleetRows() {
  // The page renders exactly one table; `TableScroll` puts its label on the
  // scrolling region rather than on the table, so the role alone is the stable
  // handle here.
  return within(await screen.findByRole("table"));
}

function envelope(items: unknown[], projectId = "p1") {
  return {
    schema_version: "data-datastreams.v1",
    project_ref: { object_type: "project", id: projectId },
    generated_at: "2026-07-29T10:00:00Z",
    evidence_as_of: "2026-07-29T09:00:00Z",
    items,
    unavailable_reasons: [],
    allowed_actions: [],
  };
}

/** A row with nothing but its axes — the shape the old screen was built for. */
const BARE = {
  object_ref: { object_type: "datastream", id: "ds_1" },
  name: "Revenue feed",
  source_kind: "managed_feed",
  connector_ref: null,
  states: { lifecycle: "active", validation: "blocked", publication: "unavailable", health: "unavailable", freshness: "unavailable" },
  evidence: {},
  evidence_as_of: null,
  links: {},
};

/** A row carrying the evidence the server actually emits. */
const OPERATING = {
  object_ref: { object_type: "datastream", id: "ds_2" },
  name: "Campaign performance",
  source_kind: "connector",
  connector_ref: { object_type: "connector", id: "meta-ads" },
  states: { lifecycle: "active", validation: "executable", publication: "published", health: "available", freshness: "observed" },
  evidence: {
    cadence: "nightly",
    next_run_at: "2026-07-30T06:00:00Z",
    published_at: "2026-07-29T05:00:00Z",
    published_row_count: 12840,
    validation_issues: [],
    latest_exception: null,
    mapping_version_id: "dmv_2",
    active_mapping_version: 3,
    mapping_blocking_count: 0,
    mapping_executable: true,
  },
  evidence_as_of: "2026-07-29T09:00:00Z",
  // The address `_console_href` really composes (`data_surface.py:409-412`):
  // `/object/{type}/{id}/tab/{tab}`, both literals included.
  //
  // THIS FIXTURE USED TO READ `/org/o1/project/p1/data/datastreams/ds_2/mapping`
  // and the assertion below pinned it verbatim (AI-218). That string has the
  // right root and is still refused by `parsePath` — "Unknown trailing route
  // shape", because `ds_2` sits where the literal `object` belongs. So the test
  // asserted that the Governance-gap link opens the unknown-route screen, on the
  // very screen whose 2026-08-03 repair was that class of address.
  links: { mapping: "/org/o1/project/p1/data/datastreams/object/datastream/ds_2/tab/mapping" },
};

/** Mapped, but with blocking bindings — the third answer. */
const BLOCKED_MAPPING = {
  ...OPERATING,
  object_ref: { object_type: "datastream", id: "ds_3" },
  name: "Panel extract",
  evidence: {
    ...OPERATING.evidence,
    mapping_version_id: "dmv_3",
    active_mapping_version: 4,
    mapping_blocking_count: 2,
    mapping_executable: false,
  },
};

function response(body: unknown, ok = true, status = ok ? 200 : 503): Response {
  return { ok, status, json: async () => body, text: async () => JSON.stringify(body) } as Response;
}

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it("reads the canonical fleet endpoint once, and the setup-draft list once", async () => {
  // TWO reads since 57.12 (T7): the fleet, and the drafts list that feeds the
  // resume line. No third — this test exists because an earlier screen fanned
  // out per row.
  const fetchMock = vi.fn(() => Promise.resolve(response(envelope([BARE]))));
  vi.stubGlobal("fetch", fetchMock);
  render(<DataWorkspace projectId="p1" />);
  expect((await fleetRows()).getByText("Revenue feed")).toBeInTheDocument();
  expect(fetchMock).toHaveBeenCalledWith("/api/projects/p1/datastreams", expect.objectContaining({ cache: "no-store" }));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
    "/api/projects/p1/datastream-setup-drafts",
    expect.objectContaining({ cache: "no-store" }),
  ));
  expect(fetchMock).toHaveBeenCalledTimes(2);
});

it("draws the evidence the server already sends — cadence, next run, publication", async () => {
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(response(envelope([OPERATING])))));
  render(<DataWorkspace projectId="p1" />);
  expect(await screen.findByText("Campaign performance")).toBeInTheDocument();
  // `published_row_count` and `published_at` were on the wire and undrawn.
  expect(screen.getByText(/12,840 rows/)).toBeInTheDocument();
  expect(screen.getByText("29 Jul")).toBeInTheDocument();
  // `next_run_at` likewise. The exact rendering is locale-dependent; what this
  // pins is that the cell is not "Unavailable".
  const cells = screen.getAllByRole("cell").map((cell) => cell.textContent ?? "");
  expect(cells.some((value) => /\d{2}:\d{2}|30 Jul/.test(value))).toBe(true);
});

it("keeps a blocked validation visible instead of folding it into health", async () => {
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(response(envelope([BARE])))));
  render(<DataWorkspace projectId="p1" />);
  const rows = await fleetRows();
  expect(rows.getByText("Revenue feed")).toBeInTheDocument();
  // The row has `validation: blocked` and an EMPTY issue list. An earlier draft
  // read only the issue list and showed this row as `unavailable` — the state
  // that matters most, silently gone.
  expect(rows.getByText("blocked")).toBeInTheDocument();
  // Lifecycle stays its own axis rather than being merged into one status word.
  expect(rows.getByText("active")).toBeInTheDocument();
});

it("names what needs attention before the table, not only inside it", async () => {
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(response(envelope([BARE, OPERATING])))));
  render(<DataWorkspace projectId="p1" />);
  const banner = await screen.findByTestId("fleet-attention");
  expect(banner).toHaveTextContent("Revenue feed");
  // The healthy one is not accused.
  expect(banner).not.toHaveTextContent("Campaign performance");
});

it("names what is missing in a cell instead of dropping the column", async () => {
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(response(envelope([OPERATING])))));
  render(<DataWorkspace projectId="p1" />);
  expect(await screen.findByText("Campaign performance")).toBeInTheDocument();
  // The column stays whatever the row carries: a missing column reads as a
  // column nobody wanted.
  expect(screen.getByRole("columnheader", { name: "Data type" })).toBeInTheDocument();
  // UNTIL 58.8 THIS CELL PRINTED `Unavailable`, reason "the read model carries
  // no data-type classification". `app.datastreams.data_role` has existed with
  // seven declared values and a write path since 2026-07-27, so the sentence had
  // become false. What an unclassified row says now is that NOBODY classified
  // it — which is a different statement and the true one.
  expect(screen.getByText("Not classified")).toBeInTheDocument();
  expect(screen.queryByText("Unavailable")).not.toBeInTheDocument();
});

it("says how far each Datastream is from the semantic layer, in three answers", async () => {
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(response(envelope([BARE, OPERATING, BLOCKED_MAPPING])))));
  render(<DataWorkspace projectId="p1" />);
  const rows = await fleetRows();
  // No mapping version at all: nothing is bound, nothing reaches Governance.
  expect(rows.getByText("Not mapped")).toBeInTheDocument();
  // Mapped and clean — `mdm_business_links` cannot bind a Domain to a Datastream
  // (target_type CHECK, migration 130), so the mapping IS the reach.
  expect(rows.getByText("v3")).toBeInTheDocument();
  // Mapped but blocked is a THIRD statement, not a variant of unmapped.
  expect(rows.getByText("2 blocking")).toBeInTheDocument();
});

it("links the Governance gap to the tab where it is fixed, without opening the row", async () => {
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(response(envelope([OPERATING])))));
  const open = vi.fn();
  render(<DataWorkspace projectId="p1" onOpenDatastream={open} />);
  const rows = await fleetRows();
  const link = rows.getByRole("link", { name: /v3/ });
  // The property, not the string: the address has to be one this console can
  // open. Pinning the characters is what let a refused address sit here.
  const href = link.getAttribute("href") ?? "";
  expect(parsePath(href, "")).toMatchObject({ kind: "resolved" });
  expect(href).toContain("/tab/mapping");
  // Reporting a gap and sending nobody anywhere is the raw list being replaced;
  // and the link must not also fire the row's open handler.
  fireEvent.click(link);
  expect(open).not.toHaveBeenCalled();
});

it("opens a row through the supplied canonical navigation callback", async () => {
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(response(envelope([BARE])))));
  const open = vi.fn();
  render(<DataWorkspace projectId="p1" onOpenDatastream={open} />);
  // Explicitly the name INSIDE the table: the banner carries it too, and a row
  // that opens from the banner would be a different contract.
  fireEvent.click((await fleetRows()).getByText("Revenue feed"));
  expect(open).toHaveBeenCalledWith("ds_1");
});

it("offers Add Datastream only when the caller owns the route", async () => {
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(response(envelope([BARE])))));
  const add = vi.fn();
  const view = render(<DataWorkspace projectId="p1" onAddDatastream={add} />);
  fireEvent.click(await screen.findByRole("button", { name: /Add Datastream/i }));
  expect(add).toHaveBeenCalledOnce();
  // Without the callback the button is absent rather than present and inert.
  view.rerender(<DataWorkspace projectId="p1" />);
  expect(screen.queryByRole("button", { name: /Add Datastream/i })).not.toBeInTheDocument();
});

/**
 * Story 63.5 — the fleet says which Datastreams are collecting, and pays
 * nothing to find out.
 *
 * `evidence.latest_candidate_state` has been on the wire for every row since
 * this envelope existed and no version of this screen read it. What the rows
 * must NOT do is poll: one `useDatastreamProgress` per row is 40 × 17_280
 * requests a night at forty Datastreams and forty timers, on a service that
 * scales to zero — which is why `toHaveBeenCalledTimes(1)` above stays exactly
 * as it is. The list says a collection is running; the Workbench says where it
 * has got to.
 */
const COLLECTING = {
  ...OPERATING,
  object_ref: { object_type: "datastream", id: "ds_4" },
  name: "Nightly retrieval",
  evidence: { ...OPERATING.evidence, latest_candidate_state: "loading" },
};

/** The same flux after its run ended. The active lock is free. */
const FINISHED = {
  ...OPERATING,
  object_ref: { object_type: "datastream", id: "ds_5" },
  name: "Weekly rollup",
  evidence: { ...OPERATING.evidence, latest_candidate_state: "collected" },
};

it("marks the row of a Datastream that is collecting, and only that one", async () => {
  const fetchMock = vi.fn(() => Promise.resolve(response(envelope([COLLECTING, FINISHED, OPERATING]))));
  vi.stubGlobal("fetch", fetchMock);
  render(<DataWorkspace projectId="p1" />);
  const rows = await fleetRows();

  const collecting = rows.getAllByRole("row").find((row) => row.textContent?.includes("Nightly retrieval"))!;
  const finished = rows.getAllByRole("row").find((row) => row.textContent?.includes("Weekly rollup"))!;
  // The registry's label, never the wire value and never a literal typed here.
  expect(within(collecting).getByText("Loading")).toBeInTheDocument();
  // A terminal state is not a collection in flight — and the cell says so
  // rather than being left blank, which reads as a column nobody filled in.
  expect(within(finished).getByText("Not collecting")).toBeInTheDocument();
  // A row whose payload carries no run state at all is in the same case.
  const noState = rows.getAllByRole("row").find((row) => row.textContent?.includes("Campaign performance"))!;
  expect(within(noState).getByText("Not collecting")).toBeInTheDocument();

  // THE COST, pinned: the fleet endpoint is read once and nothing is fanned
  // out per row — the progress address included, whatever the number of rows.
  // The second call is the ONE setup-drafts list of the resume line (57.12).
  expect(fetchMock).toHaveBeenCalledTimes(2);
  expect(fetchMock).toHaveBeenCalledWith(
    "/api/projects/p1/datastreams",
    expect.objectContaining({ cache: "no-store" }),
  );
});

it("does not turn failed or empty reads into healthy fleet claims", async () => {
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(response({ code: "unavailable" }, false, 503))));
  const view = render(<DataWorkspace projectId="p1" />);
  const failure = (await screen.findByRole("alert")).textContent ?? "";
  expect(failure).toMatch(/No fleet has been substituted/i);
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(response(envelope([], "p2")))));
  view.rerender(<DataWorkspace projectId="p2" />);
  expect(await screen.findByText(/No Datastreams/i)).toBeInTheDocument();
  expect(screen.queryByText("Healthy")).not.toBeInTheDocument();
  expect(screen.queryByTestId("fleet-attention")).not.toBeInTheDocument();
  // TWO TEXTS, and the point is that they are not each other's. "This Project
  // has none" and "the list could not be read" send a person to two different
  // places, and a screen that says one when the other is true sends them to the
  // wrong one.
  const empty = screen.getByText(/No Datastreams/i).textContent ?? "";
  expect(empty).not.toMatch(/No fleet has been substituted/i);
  expect(failure).not.toMatch(/No Datastreams/i);
});

/**
 * Story 58.8 — the fleet says what each stream COLLECTS, not only that it is.
 *
 * Every fixture below is a shape the read model really produces, and the three
 * `Collects` cases are the three the database is measurably in: a current plan
 * with a grain, a current plan without one, and no current plan at all (1012 of
 * 1258 Datastreams on the disposable cluster).
 */
const GRAINED = {
  object_ref: { object_type: "datastream", id: "ds_10" },
  name: "Meta daily",
  source_kind: "connector_pull",
  connector_ref: { object_type: "connector", id: "meta-ads" },
  states: { lifecycle: "active", validation: "executable", publication: "published", health: "available" },
  evidence: {
    cadence: "nightly",
    data_role: "Performance",
    source_account_id: "sacct_EXAMPLE",
    source_account_label: "Example brand account",
    active_plan_version: 4,
    plan_grain: ["Date", "Campaign"],
    plan_metric_count: 2,
    plan_dimension_count: 3,
    latest_candidate_state: "failed",
    latest_run_at: "2026-08-06T23:10:00Z",
  },
  evidence_as_of: "2026-08-07T09:00:00Z",
  links: {},
};

/** A plan is current and selects no grain — 44 of 246 on the cluster. */
const PLAN_WITHOUT_GRAIN = {
  ...GRAINED,
  object_ref: { object_type: "datastream", id: "ds_11" },
  name: "Search console",
  connector_ref: { object_type: "connector", id: "google-ads" },
  evidence: {
    ...GRAINED.evidence,
    data_role: null,
    source_account_id: "sacct_EXAMPLE",
    source_account_label: null,
    plan_grain: [],
    plan_metric_count: 1,
    plan_dimension_count: 0,
    latest_candidate_state: "a_state_this_build_does_not_know",
  },
};

/** No plan version is current, so there is nothing to collect yet. */
const NO_PLAN = {
  ...GRAINED,
  object_ref: { object_type: "datastream", id: "ds_12" },
  name: "Uploaded plan",
  source_kind: "managed_feed",
  connector_ref: null,
  evidence: {
    cadence: "manual",
    data_role: null,
    source_account_id: null,
    source_account_label: null,
    active_plan_version: null,
    plan_grain: null,
    plan_metric_count: null,
    plan_dimension_count: null,
    latest_candidate_state: null,
    latest_run_at: null,
  },
};

function rowNamed(rows: ReturnType<typeof within>, name: string): HTMLElement {
  return rows.getAllByRole("row").find((row: HTMLElement) => row.textContent?.includes(name))!;
}

it("carries the connector's mark AND its name in the Datastream cell", async () => {
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(response(envelope([GRAINED])))));
  render(<DataWorkspace projectId="p1" />);
  const rows = await fleetRows();
  const row = rowNamed(rows, "Meta daily");
  // The name comes from the managed registry, never typed here: a screen must
  // not be able to show one vendor's mark beside another's label.
  expect(within(row).getByText(connectorName("meta-ads"))).toBeInTheDocument();
  expect(connectorName("meta-ads")).not.toBe("meta-ads");
  // The mark itself resolves through the same registry entry.
  expect(row.querySelector("img")?.getAttribute("src")).toContain("meta");
});

it("refuses a Destination column, because BigQuery is the only warehouse", async () => {
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(response(envelope([GRAINED])))));
  render(<DataWorkspace projectId="p1" />);
  const rows = await fleetRows();
  const headings = rows.getAllByRole("columnheader").map((cell) => cell.textContent ?? "");
  expect(headings.length).toBe(13);
  // `evidence.destination_policy` is on the wire for every row and must stay
  // undrawn: a column whose value never varies carries nothing, and the
  // amendment of 2026-08-05 says so in as many words.
  expect(headings.some((name) => /destination/i.test(name))).toBe(false);
});

it("says what a stream collects in three different sentences, never one dash", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(() => Promise.resolve(response(envelope([GRAINED, PLAN_WITHOUT_GRAIN, NO_PLAN])))),
  );
  render(<DataWorkspace projectId="p1" />);
  const rows = await fleetRows();

  // 1. A current plan with a grain: the grain itself, and what it pulls at it.
  const grained = within(rowNamed(rows, "Meta daily"));
  expect(grained.getByText("Date · Campaign")).toBeInTheDocument();
  expect(grained.getByText("2 metrics · 3 dimensions")).toBeInTheDocument();

  // 2. A current plan with NO grain is not the same statement, and the count of
  //    dimensions is 0 — which is why no cell here prints a zero.
  const flat = within(rowNamed(rows, "Search console"));
  expect(flat.getByText("No grain selected")).toBeInTheDocument();
  expect(flat.getByText("1 metric")).toBeInTheDocument();

  // 3. No current plan at all: nothing is selected, and the reason is not that
  //    the grain is unknown.
  const bare = within(rowNamed(rows, "Uploaded plan"));
  expect(bare.getByText("No current plan")).toBeInTheDocument();
  expect(bare.queryByText("No grain selected")).not.toBeInTheDocument();

  // No `0` anywhere in the table, in any cell.
  const cells = rows.getAllByRole("cell").map((cell) => cell.textContent?.trim() ?? "");
  expect(cells.some((value) => value === "0" || value === "—")).toBe(false);
});

it("puts the last run's mark and its instant in the same cell", async () => {
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(response(envelope([GRAINED, NO_PLAN])))));
  render(<DataWorkspace projectId="p1" />);
  const rows = await fleetRows();

  const cells = within(rowNamed(rows, "Meta daily")).getAllByRole("cell");
  // The label is the registry's, and the instant is whatever the reader's
  // locale makes of it — a clock when the run was today, a date otherwise.
  // Pinning the characters of the date would pin the machine's timezone.
  // The day is `\d{1,2}`: since 76-1 the console has ONE date spelling
  // (`ui/Timestamp#formatDate`, day numeric), so the 2nd of a month reads
  // `2 Sep` and not `02 Sep`.
  const together = cells.filter((cell) =>
    new RegExp(`^${executionStateLabel("failed")}\\s*(\\d{2}:\\d{2}|\\d{1,2} [A-Za-z]{3})$`).test(
      (cell.textContent ?? "").replace(/\s+/g, " ").trim(),
    ),
  );
  // ONE cell holds both. A state with no instant is five minutes or five months
  // old and reads the same, which is what the fleet did before 58.8.
  expect(together).toHaveLength(1);

  // A row with no run instant says so rather than leaving the cell blank.
  expect(within(rowNamed(rows, "Uploaded plan")).getByText("No run recorded")).toBeInTheDocument();
});

it("renders a run state this build does not know through the registry, as Unknown", async () => {
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(response(envelope([PLAN_WITHOUT_GRAIN])))));
  render(<DataWorkspace projectId="p1" />);
  const rows = await fleetRows();
  // `executionStateLabel` answers `Unknown` for a name the registry does not
  // hold, and the list must not have an opinion of its own about run states.
  expect(executionStateLabel("a_state_this_build_does_not_know")).toBe("Unknown");
  expect(within(rowNamed(rows, "Search console")).getByText("Unknown")).toBeInTheDocument();
});

it("filters the fleet by source without asking the server a second question", async () => {
  const fetchMock = vi.fn(() => Promise.resolve(response(envelope([GRAINED, PLAN_WITHOUT_GRAIN]))));
  vi.stubGlobal("fetch", fetchMock);
  render(<DataWorkspace projectId="p1" />);
  const rows = await fleetRows();
  // Header row plus two Datastreams.
  expect(rows.getAllByRole("row")).toHaveLength(3);

  const filter = screen.getByLabelText("Filter by source");
  fireEvent.change(filter, { target: { value: "google-ads" } });

  const filtered = await fleetRows();
  expect(filtered.getAllByRole("row")).toHaveLength(2);
  expect(filtered.queryByText("Meta daily")).not.toBeInTheDocument();
  expect(filtered.getByText("Search console")).toBeInTheDocument();

  // THE COST: the filter is applied to the envelope already loaded. The fleet
  // route is shared by six lenses and carries no query parameters; giving one
  // lens a parameter would be a change to all six. The second call is the ONE
  // setup-drafts list of the resume line (57.12).
  expect(fetchMock).toHaveBeenCalledTimes(2);
});

/**
 * Story 59.2 — the `Issues` column carries its count, and the three things a
 * count is not.
 *
 * WHAT THIS CASE USED TO ASSERT, and why it could not stay. It pinned the literal
 * "No monitor has run" on every row. That sentence was TRUE on 2026-08-07, when
 * `app.dq_monitors` was 0 on every database; stories 59.3 and 59.4 published two
 * monitors against a real preprod Datastream on 2026-08-08, and the same words
 * became a measurably false claim about an existing row — the exact defect
 * `epic-59:114` refuses, turned against the screen.
 *
 * The four fixtures below are the four states the server can send, and every one
 * of them is asserted separately: collapsing any two is what the column is
 * being rewritten to stop doing.
 */
const UNWATCHED = {
  ...GRAINED,
  object_ref: { object_type: "datastream", id: "ds_20" },
  name: "Unwatched flux",
  evidence: {
    ...GRAINED.evidence,
    open_issues: {
      monitored: false,
      count: 0,
      by_severity: { blocking: 0, degrading: 0, informational: 0 },
      highest_severity: null,
    },
  },
};

/** Preprod's real row since 2026-08-08: monitors, and nothing open. */
const WATCHED_CLEAN = {
  ...GRAINED,
  object_ref: { object_type: "datastream", id: "ds_21" },
  name: "Watched flux",
  evidence: {
    ...GRAINED.evidence,
    open_issues: {
      monitored: true,
      count: 0,
      by_severity: { blocking: 0, degrading: 0, informational: 0 },
      highest_severity: null,
    },
  },
};

const DEGRADING = {
  ...GRAINED,
  object_ref: { object_type: "datastream", id: "ds_22" },
  name: "Degrading flux",
  evidence: {
    ...GRAINED.evidence,
    open_issues: {
      monitored: true,
      count: 1,
      by_severity: { blocking: 0, degrading: 1, informational: 0 },
      highest_severity: "degrading",
    },
  },
  links: { runs: "/org/o1/project/p1/data/datastreams/object/datastream/ds_22/tab/runs" },
};

const BLOCKING = {
  ...GRAINED,
  object_ref: { object_type: "datastream", id: "ds_23" },
  name: "Blocking flux",
  evidence: {
    ...GRAINED.evidence,
    open_issues: {
      monitored: true,
      count: 3,
      by_severity: { blocking: 2, degrading: 1, informational: 0 },
      highest_severity: "blocking",
    },
  },
  links: { runs: "/org/o1/project/p1/data/datastreams/object/datastream/ds_23/tab/runs" },
};

it("counts the open issues of a row, with the severity the server measured", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(() => Promise.resolve(response(envelope([DEGRADING, BLOCKING])))),
  );
  render(<DataWorkspace projectId="p1" />);
  const rows = await fleetRows();
  expect(rows.getByRole("columnheader", { name: "Issues" })).toBeInTheDocument();

  // Warning and critical — the two of `epic-59:116` that need a count at all.
  expect(within(rowNamed(rows, "Degrading flux")).getByText("1 open issue · degrading"))
    .toBeInTheDocument();
  expect(within(rowNamed(rows, "Blocking flux")).getByText("3 open issues · blocking"))
    .toBeInTheDocument();
  // The severity word is the SERVER's. No screen-side synonym, and no invented
  // scale: `blocking` outranks the `degrading` this row also carries, and the
  // ranking was applied before the payload was sent.
  expect(rows.queryByText(/critical|major|minor/i)).not.toBeInTheDocument();
});

it("separates no monitor, nothing found, and a count that could not be read", async () => {
  vi.stubGlobal(
    "fetch",
    // `GRAINED` carries no `open_issues` key at all — the fourth state.
    vi.fn(() => Promise.resolve(response(envelope([UNWATCHED, WATCHED_CLEAN, GRAINED])))),
  );
  render(<DataWorkspace projectId="p1" />);
  const rows = await fleetRows();

  // 1. Nothing has looked at this flux.
  expect(within(rowNamed(rows, "Unwatched flux")).getByText("No monitor watches this"))
    .toBeInTheDocument();
  // 2. Something looked and found nothing — preprod's own row, and a DIFFERENT
  //    statement from the first. Folding them is what `epic-59:114` refuses.
  expect(within(rowNamed(rows, "Watched flux")).getByText("No open issue")).toBeInTheDocument();
  // 3. The key is absent: the count could not be read. Never a `0`, and never
  //    the clean bill of health of state 2.
  expect(within(rowNamed(rows, "Meta daily")).getByText("Issue count unavailable"))
    .toBeInTheDocument();

  // NO ZERO ANYWHERE, in any cell, in any of the three states.
  const cells = rows.getAllByRole("cell").map((cell) => cell.textContent?.trim() ?? "");
  expect(cells.some((value) => value === "0" || value === "—")).toBe(false);
  expect(rows.queryByText(/0 open issue/)).not.toBeInTheDocument();
});

it("sends the badge to the runs of that Datastream, through the router's own answer", async () => {
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(response(envelope([BLOCKING, DEGRADING])))));
  const open = vi.fn();
  render(<DataWorkspace projectId="p1" onOpenDatastream={open} />);
  const rows = await fleetRows();

  const link = within(rowNamed(rows, "Blocking flux")).getByRole("link", { name: /3 open issues/ });
  const href = link.getAttribute("href") ?? "";
  // The property, not the characters: an address this console cannot open is a
  // control that looks live and lands on the unknown-route screen.
  expect(parsePath(href, "")).toMatchObject({ kind: "resolved" });
  expect(href).toContain("/tab/runs");
  // Opening the anomaly must not also open the row underneath it.
  fireEvent.click(link);
  expect(open).not.toHaveBeenCalled();
});

it("names the count without a link when the server sent no address for the runs", async () => {
  // `_console_link` returns nothing when the Project has no organization
  // (`data_surface.py`), so `links` is `{}` — and a badge with no address stays
  // a badge rather than becoming a dead anchor.
  const NO_LINK = { ...BLOCKING, object_ref: { object_type: "datastream", id: "ds_24" }, links: {} };
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(response(envelope([NO_LINK])))));
  render(<DataWorkspace projectId="p1" />);
  const rows = await fleetRows();

  expect(rows.getByText("3 open issues · blocking")).toBeInTheDocument();
  expect(rows.queryByRole("link", { name: /open issues/ })).not.toBeInTheDocument();
});

it("reads the data role off the row, and names the absence when nobody set one", async () => {
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(response(envelope([GRAINED, NO_PLAN])))));
  render(<DataWorkspace projectId="p1" />);
  const rows = await fleetRows();
  // One of the seven values `core.datastreams.DATA_ROLES` declares.
  expect(within(rowNamed(rows, "Meta daily")).getByText("Performance")).toBeInTheDocument();
  expect(within(rowNamed(rows, "Uploaded plan")).getByText("Not classified")).toBeInTheDocument();
});

it("separates a stream with no source account from an account with no name", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(() => Promise.resolve(response(envelope([GRAINED, PLAN_WITHOUT_GRAIN, NO_PLAN])))),
  );
  render(<DataWorkspace projectId="p1" />);
  const rows = await fleetRows();

  // Named: the provider's label, or the account's own external id.
  expect(within(rowNamed(rows, "Meta daily")).getByText("Example brand account")).toBeInTheDocument();
  // TWO ABSENCES, TWO SENTENCES. The stream names an account and the
  // authorization exposes no name for it — repaired on the Source.
  expect(within(rowNamed(rows, "Search console")).getByText("Account not named")).toBeInTheDocument();
  // The stream names no account at all — repaired on the Datastream. Collapsing
  // these two sends a person to the wrong screen.
  expect(within(rowNamed(rows, "Uploaded plan")).getByText("No source account")).toBeInTheDocument();
});

/** 57.12, T7 — the way back into a setup draft.
 *
 *  The only door used to be the sessionStorage key the "Set up source access"
 *  handoff writes: leave the wizard by any other path and the work was
 *  invisible. The server lists the drafts (`GET …/datastream-setup-drafts`);
 *  the screen now offers the resume through the SAME bridge — the key is
 *  written, the create route is taken, `DatastreamCreate` reads the key into
 *  `resumeDraftId`. One channel, or two would disagree about which draft is
 *  open. */

/** The exact wire shape of `_draft_payload`, minus what the list never sends
 *  (no `updated_at` — ordered by, never selected). */
const RESUMABLE_DRAFT = {
  // Was `exited` -- the ONE place in the whole repository that ever assigned
  // that state, and it was a hand-written object, never a database write.
  // Migration 284 retired it; `draft` is what a resumable draft really carries.
  draft_ref: "dsd_9", project_ref: "p1", state: "draft",
  current_revision_ref: "dsdr_2", current_revision: 2, current_proposal_ref: null,
  first_incomplete_section: "configure", invalidation_causes: [],
  resume_href: "", idempotent_replay: false,
};

/** Routes the drafts list by URL; every other call gets the fleet envelope. */
function fetchWithDrafts(draftsBody: unknown, ok = true, status = ok ? 200 : 503) {
  return vi.fn((url: string) => Promise.resolve(
    url.includes("/datastream-setup-drafts") ? response(draftsBody, ok, status) : response(envelope([BARE])),
  ));
}

it("offers the way back into a setup in progress, through the one resume channel", async () => {
  sessionStorage.removeItem("datastream-setup-return:p1");
  vi.stubGlobal("fetch", fetchWithDrafts({ drafts: [RESUMABLE_DRAFT] }));
  const onAddDatastream = vi.fn();
  render(<DataWorkspace projectId="p1" onAddDatastream={onAddDatastream} />);

  expect(await screen.findByText("A Datastream setup is in progress")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Resume" }));

  // The handoff bridge, not a second channel: the same sessionStorage key
  // `DatastreamCreate` reads into `resumeDraftId`, and the same create route.
  expect(sessionStorage.getItem("datastream-setup-return:p1")).toBe("dsd_9");
  expect(onAddDatastream).toHaveBeenCalledOnce();
  sessionStorage.removeItem("datastream-setup-return:p1");
});

/** The other end of the same line (AI-336, ratified by Jean 2026-08-31).
 *
 *  A setup in progress offered exactly one gesture — resume — so the only way to
 *  be rid of one was to finish it. It ends here too, because this is one of the
 *  two places a draft is visible at all. The server writes `archived`; the line
 *  then goes, because it reads `state === "draft"`. */
it("ends the setup in progress, and stops offering a bridge to it", async () => {
  sessionStorage.setItem("datastream-setup-return:p1", "dsd_9");
  const fetchMock = fetchWithDrafts({ drafts: [RESUMABLE_DRAFT] });
  vi.stubGlobal("fetch", fetchMock);
  render(<DataWorkspace projectId="p1" onAddDatastream={vi.fn()} />);

  expect(await screen.findByText("A Datastream setup is in progress")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Discard" }));

  // What is lost, in the operator's words. NO evidence rows: this screen has
  // never read what the draft holds — the list payload carries no
  // `operator_input` — so printing a section id from it would be the database's
  // vocabulary standing in for the person's.
  const dialog = await screen.findByTestId("datastream-setup-discard");
  expect(dialog).toHaveTextContent(/No Datastream was created from this setup/);
  expect(dialog).not.toHaveTextContent(/archiv/i);
  expect(screen.getByTestId("datastream-setup-discard-cancel")).toHaveTextContent("Keep the setup");

  fireEvent.click(screen.getByTestId("datastream-setup-discard-confirm"));

  // ONE route, and the verb the surface already uses for a soft archive.
  await waitFor(() => expect((fetchMock.mock.calls as unknown as Array<[string, RequestInit?]>).some(
    ([url, init]) => url.includes("/datastream-setup-drafts/dsd_9") && init?.method === "DELETE",
  )).toBe(true));

  // The line goes, and so does the bridge — a stale key would send the next
  // entry back into a draft the server refuses every write on.
  await waitFor(() => expect(
    screen.queryByText("A Datastream setup is in progress"),
  ).not.toBeInTheDocument());
  expect(sessionStorage.getItem("datastream-setup-return:p1")).toBeNull();
  expect(screen.getByRole("button", { name: "+ Add Datastream" })).toBeInTheDocument();
});

it("keeps the setup, and says why, when the discard is refused", async () => {
  sessionStorage.setItem("datastream-setup-return:p1", "dsd_9");
  vi.stubGlobal("fetch", vi.fn((url: string, init?: RequestInit) => Promise.resolve(
    init?.method === "DELETE"
      ? response(
        {
          code: "conflict",
          message: "This setup already created a Datastream. Archive the Datastream from its "
            + "own screen; the draft that created it is kept as its record.",
        },
        false,
        409,
      )
      : url.includes("/datastream-setup-drafts")
        ? response({ drafts: [RESUMABLE_DRAFT] })
        : response(envelope([BARE])),
  )));
  render(<DataWorkspace projectId="p1" onAddDatastream={vi.fn()} />);

  expect(await screen.findByText("A Datastream setup is in progress")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Discard" }));
  fireEvent.click(await screen.findByTestId("datastream-setup-discard-confirm"));

  // A refusal is SHOWN. A line that quietly stays after a gesture that claimed
  // to remove it is the defect this covers.
  expect(await screen.findByText(/Archive the Datastream from its own screen/)).toBeInTheDocument();
  expect(screen.getByText("A Datastream setup is in progress")).toBeInTheDocument();
  expect(sessionStorage.getItem("datastream-setup-return:p1")).toBe("dsd_9");
  sessionStorage.removeItem("datastream-setup-return:p1");
});

it("draws no resume line when nothing is resumable, and none when the read fails", async () => {
  // No resumable draft: `materialized` is terminal (migration 134's CHECK).
  vi.stubGlobal("fetch", fetchWithDrafts({ drafts: [{ ...RESUMABLE_DRAFT, state: "materialized" }] }));
  const view = render(<DataWorkspace projectId="p1" onAddDatastream={vi.fn()} />);
  expect((await fleetRows()).getByText("Revenue feed")).toBeInTheDocument();
  expect(screen.queryByText("A Datastream setup is in progress")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "+ Add Datastream" })).toBeInTheDocument();

  // A failed list read is silent BY DESIGN: the Add button stays and no ghost
  // line renders — an error banner for an auxiliary read would warn about a
  // screen that works.
  vi.unstubAllGlobals();
  view.unmount();
  vi.stubGlobal("fetch", fetchWithDrafts({}, false));
  render(<DataWorkspace projectId="p1" onAddDatastream={vi.fn()} />);
  expect((await fleetRows()).getByText("Revenue feed")).toBeInTheDocument();
  expect(screen.queryByText("A Datastream setup is in progress")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "+ Add Datastream" })).toBeInTheDocument();
});

it("opens a faulty run at an evidence address the router itself built, never a concatenation", () => {
  // data.md [10]/[38], found 2026-08-30: the badge appended `/evidence/{id}` to the
  // server's runs address by string, and the composed-address sweep only saw
  // keys ending in lower-case `href`. The router builds the tail, and refuses
  // to build one on an address it cannot read.
  const runs = "/org/org_1/project/project_1/data/datastreams/object/datastream/ds_1/tab/runs";
  const evidence = evidenceHrefFor(runs, "dse_01EXAMPLE");
  expect(evidence).toBe(`${runs}/evidence/dse_01EXAMPLE`);
  expect(parsePath(evidence).kind).toBe("resolved");
  // An address the router refuses gets no tail at all: the runs address stands.
  expect(evidenceHrefFor("/nowhere/at/all", "dse_01EXAMPLE")).toBe("/nowhere/at/all");
});
