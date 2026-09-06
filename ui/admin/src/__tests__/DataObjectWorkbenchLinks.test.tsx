/**
 * THE DATA WORKBENCH LEADS SOMEWHERE — and only where its own router goes.
 *
 * Three fields of the Data envelope were declared, validated on the way in and
 * read by no screen: `links` (`dataSurface.ts`), `allowed_actions`, and the
 * owner references `datastream_ref` / `connector_ref`, which reached the page as
 * opaque strings in an evidence row. So every Data workbench was a leaf: four
 * tabs of facts and no way to the object that owns them.
 *
 * WHY THE REAL ROUTER, AND NOT THE MOCK `DataSurfacePages.test.tsx` USES. That
 * file stubs `buildPath` down to `/tab/{tab}`, which is exactly right for the
 * question it asks (does the tab band call back) and useless for this one: an
 * address is only worth rendering if `parsePath` resolves it, and a stubbed
 * builder cannot be refused. Every case below mounts `RouterProvider` over a
 * real address and asks the router about every anchor the document offers —
 * the property `ComposedAddressesResolve.test.tsx` holds for the class.
 *
 * AND THE ACTIONS ARE ASKED THE OTHER WAY ROUND. `_ACTIONS` in
 * `server/core/data_surface.py` can send five values; four have a surface that
 * performs them and `import.create` has none — an Import is a file arriving.
 * The case that matters is therefore the one that asserts NOTHING is drawn:
 * a button that exists to do nothing is the read-only theatre this workbench
 * was rebuilt against.
 */
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import DataObjectWorkbench from "../data/DataObjectWorkbench";
import { RouterProvider, parsePath } from "../shell/router";

const ORG = "org_01EXAMPLE0000000000000000";
const PROJECT = "proj_01EXAMPLE000000000000000";

const TABS = {
  sources: [
    { key: "overview", label: "Overview" },
    { key: "accounts", label: "Accounts" },
    { key: "health", label: "Health" },
    { key: "used-by", label: "Used by" },
  ],
  imports: [
    { key: "overview", label: "Overview" },
    { key: "raw-evidence", label: "Raw evidence" },
    { key: "validation", label: "Validation" },
    { key: "publication", label: "Publication" },
  ],
  events: [
    { key: "overview", label: "Overview" },
    { key: "source-mapping", label: "Source mapping" },
    { key: "collection", label: "Collection" },
    { key: "usage", label: "Usage" },
  ],
  connectors: [
    { key: "overview", label: "Overview" },
    { key: "capabilities", label: "Capabilities" },
    { key: "coverage", label: "Coverage" },
    { key: "versions", label: "Versions" },
  ],
} as const;

const SECTION: Record<keyof typeof TABS, string> = {
  sources: "sources",
  imports: "imports",
  events: "events",
  connectors: "connectors",
};

const OBJECT_TYPE: Record<keyof typeof TABS, string> = {
  sources: "source-account",
  imports: "import",
  events: "event-configuration",
  connectors: "connector",
};

function envelope(lens: string, item: Record<string, unknown>, allowed_actions: string[] = []) {
  return {
    schema_version: `data-${lens}.v1`,
    project_ref: { object_type: "project", id: PROJECT },
    generated_at: "2026-08-17T10:00:00Z",
    evidence_as_of: "2026-08-17T09:00:00Z",
    items: [item],
    unavailable_reasons: [],
    allowed_actions,
  };
}

function stub(body: unknown, ok = true) {
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve({
    ok,
    status: ok ? 200 : 503,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response)));
}

/** Every address the rendered document offers, judged by the router itself. */
function unresolvable(): string[] {
  return Array.from(document.querySelectorAll("a[href]"))
    .map((anchor) => anchor.getAttribute("href") ?? "")
    .filter((href) => href.startsWith("/") && !href.startsWith("/api/"))
    .filter((href) => {
      const index = href.indexOf("?");
      const pathname = index === -1 ? href : href.slice(0, index);
      const search = index === -1 ? "" : href.slice(index);
      return parsePath(pathname, search).kind !== "resolved";
    });
}

function mount(
  lens: keyof typeof TABS,
  objectId: string,
  tab: string,
) {
  window.history.replaceState(
    {},
    "",
    `/org/${ORG}/project/${PROJECT}/data/${SECTION[lens]}/object/${OBJECT_TYPE[lens]}/${objectId}/tab/${tab}`,
  );
  return render(
    <RouterProvider>
      <DataObjectWorkbench
        projectId={PROJECT}
        lens={lens}
        objectId={objectId}
        tab={tab}
        tabs={TABS[lens]}
        onNavigateTab={vi.fn()}
      />
    </RouterProvider>,
  );
}

/** The block that carries what this object opens, once the read has landed. */
async function opens() {
  return within(await screen.findByTestId("data-object-destinations"));
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("what a Data object names, it can open", () => {
  it("opens the Datastream an Import belongs to, at an address the router resolves", async () => {
    stub(envelope("imports", {
      object_ref: { object_type: "import", id: "mfl_1" },
      datastream_ref: { object_type: "datastream", id: "dstr_1" },
      name: "Weekly spend import",
      states: { lifecycle: "published" },
      evidence: {},
      evidence_as_of: "2026-08-17T09:00:00Z",
      links: {},
    }));

    mount("imports", "mfl_1", "overview");

    const block = await opens();
    expect(block.getByRole("link", { name: "Open the Datastream" })).toHaveAttribute(
      "href",
      `/org/${ORG}/project/${PROJECT}/data/datastreams/object/datastream/dstr_1/tab/overview`,
    );
    expect(unresolvable()).toEqual([]);
  });

  it("renders a server link that is not one of this object's own tabs, and drops the ones that are", async () => {
    // `_console_link` composes one address per tab of the contract, and the tab
    // band above already opens every one of them. A second copy would be
    // navigation dressed as evidence — so the panel carries the OTHER key and
    // only that one.
    stub(envelope("imports", {
      object_ref: { object_type: "import", id: "mfl_1" },
      name: "Weekly spend import",
      states: { lifecycle: "published" },
      evidence: {},
      evidence_as_of: "2026-08-17T09:00:00Z",
      links: {
        overview: `/org/${ORG}/project/${PROJECT}/data/imports/object/import/mfl_1/tab/overview`,
        raw_evidence: `/org/${ORG}/project/${PROJECT}/data/imports/object/import/mfl_1/tab/raw-evidence`,
        governance: `/org/${ORG}/project/${PROJECT}/governance/master-data`,
        // The shape this repository has paid for five times: an address written
        // in a second grammar, refused by `parsePath` on its first segment. It
        // is dropped, never repaired into a guess.
        legacy: `/project/${PROJECT}/data/imports`,
      },
    }));

    mount("imports", "mfl_1", "overview");

    const block = await opens();
    expect(block.getByRole("link", { name: "Governance" })).toHaveAttribute(
      "href",
      `/org/${ORG}/project/${PROJECT}/governance/master-data`,
    );
    expect(block.queryByRole("link", { name: "Overview" })).toBeNull();
    expect(block.queryByRole("link", { name: "Raw evidence" })).toBeNull();
    expect(block.queryByRole("link", { name: "Legacy" })).toBeNull();
    expect(document.body.innerHTML).not.toContain(`/project/${PROJECT}/data/imports"`);
    expect(unresolvable()).toEqual([]);
  });

  it("does not link a Connector to itself", async () => {
    stub(envelope("connectors", {
      object_ref: { object_type: "connector", id: "google-analytics" },
      connector_id: "google-analytics",
      states: { activation: "installed" },
      evidence: { contract: { auth_kind: "oauth2" } },
      evidence_as_of: "2026-08-17T09:00:00Z",
      links: {},
    }, ["connector.inspect"]));

    mount("connectors", "google-analytics", "capabilities");

    const block = await opens();
    expect(block.queryByRole("link", { name: "Open the Connector" })).toBeNull();
    expect(block.getByRole("link", { name: "Open the Connector catalog" })).toHaveAttribute(
      "href",
      `/org/${ORG}/project/${PROJECT}/data/connectors`,
    );
  });
});

describe("an action is offered only where something performs it", () => {
  it("draws no control for import.create, because nothing in this console creates an Import", async () => {
    // MEASURED, not assumed: the only console file that posts anywhere near it
    // posts to `/imports/preview`, which by construction does not import. The
    // envelope declares the action all the same, and the honest answer to a
    // declared gesture with no executor is silence.
    stub(envelope("imports", {
      object_ref: { object_type: "import", id: "mfl_1" },
      datastream_ref: { object_type: "datastream", id: "dstr_1" },
      name: "Weekly spend import",
      states: { lifecycle: "published" },
      evidence: {},
      evidence_as_of: "2026-08-17T09:00:00Z",
      links: {},
    }, ["import.create"]));

    mount("imports", "mfl_1", "overview");

    const block = await opens();
    // The owner reference is still there — this is not "the panel disappeared".
    expect(block.getByRole("link", { name: "Open the Datastream" })).toBeInTheDocument();
    expect(block.getAllByRole("link")).toHaveLength(1);
    expect(screen.queryByRole("button", { name: /import/i })).toBeNull();
  });

  it("offers arming an event stream on the Datastream that owns it", async () => {
    stub(envelope("events", {
      object_ref: { object_type: "event-configuration", id: "evc_1" },
      datastream_ref: { object_type: "datastream", id: "dstr_1" },
      name: "Campaign launches",
      states: { lifecycle: "active" },
      evidence: { collection_policy: { cadence: "daily" } },
      evidence_as_of: "2026-08-17T09:00:00Z",
      links: {},
    }, ["event-configuration.create"]));

    mount("events", "evc_1", "collection");

    const block = await opens();
    // `EventStreamPanel` is mounted by `WorkbenchOutputsPage`, so the address is
    // that Datastream's Outputs tab and not a collection screen.
    expect(block.getByRole("link", { name: "Arm an event stream" })).toHaveAttribute(
      "href",
      `/org/${ORG}/project/${PROJECT}/data/datastreams/object/datastream/dstr_1/tab/outputs`,
    );
    expect(unresolvable()).toEqual([]);
  });

  it("offers no arming gesture when the Event Configuration names no Datastream", async () => {
    // The executor needs a Datastream to run on. Without one there is no address
    // to give, so the gesture is absent rather than dead.
    stub(envelope("events", {
      object_ref: { object_type: "event-configuration", id: "evc_1" },
      name: "Campaign launches",
      states: { lifecycle: "active" },
      evidence: { collection_policy: { cadence: "daily" } },
      evidence_as_of: "2026-08-17T09:00:00Z",
      links: {},
    }, ["event-configuration.create"]));

    mount("events", "evc_1", "collection");

    await screen.findByText("Campaign launches");
    expect(screen.queryByRole("link", { name: "Arm an event stream" })).toBeNull();
    expect(screen.queryByTestId("data-object-destinations")).toBeNull();
  });
});

describe("the Source Account answers its own two questions", () => {
  const account = (evidence: Record<string, unknown>) => envelope("sources", {
    object_ref: { object_type: "source-account", id: "sacct_1" },
    label: "North America analytics",
    connector_ref: { object_type: "connector", id: "google-analytics" },
    connection_ref: { object_type: "connection", id: "conn_1" },
    authorization_ref: { object_type: "source-authorization", owner_scope: "organization", kind: "oauth2" },
    discovered_for_connector: "google-analytics",
    states: { availability: "available", authorization: "stale", usage: "used" },
    evidence,
    evidence_as_of: "2026-08-17T09:00:00Z",
    links: {},
  });

  it("says what the used-by count is, and where the list of Datastreams lives", async () => {
    // The tab used to render `Datastream count · 2` and stop. A number with no
    // way to the objects it counts is the dead end this workbench is being
    // rebuilt against.
    stub(account({ used_by_count: 2 }));

    mount("sources", "sacct_1", "used-by");

    const note = within(await screen.findByTestId("data-tab-note"));
    expect(note.getByText("2 Datastreams read this Source Account")).toBeInTheDocument();
    expect(note.getByRole("link", { name: "Open Datastreams" })).toHaveAttribute(
      "href",
      `/org/${ORG}/project/${PROJECT}/data/datastreams`,
    );
    expect(unresolvable()).toEqual([]);
  });

  it("names the gesture that would fill an empty used-by, instead of a zero", async () => {
    stub(account({ used_by_count: 0 }));

    mount("sources", "sacct_1", "used-by");

    const note = within(await screen.findByTestId("data-tab-note"));
    expect(note.getByText("No Datastream reads this Source Account")).toBeInTheDocument();
    expect(note.getByRole("link", { name: "Add a Datastream" })).toHaveAttribute(
      "href",
      `/org/${ORG}/project/${PROJECT}/data/datastreams/action/create`,
    );
  });

  it("gives the Accounts tab a reading of its own, and names who owns the rest", async () => {
    // It returned `{ source_account, connector }` — the two fields `overview`
    // already returns — so two tabs of four showed one reading.
    stub(account({ discovered_at: "2026-08-01T08:00:00Z", used_by_count: 2 }));

    mount("sources", "sacct_1", "accounts");

    expect(await screen.findByText("Authorization kind")).toBeInTheDocument();
    expect(screen.getByText("oauth2")).toBeInTheDocument();
    expect(screen.getByText("Discovered for connector")).toBeInTheDocument();
    expect(screen.getByText("Authorization owner")).toBeInTheDocument();
    const note = within(screen.getByTestId("data-tab-note"));
    expect(note.getByText("One account, and its siblings are on Sources")).toBeInTheDocument();
    expect(note.getByRole("link", { name: "Open Sources" })).toHaveAttribute(
      "href",
      `/org/${ORG}/project/${PROJECT}/data/sources`,
    );
  });
});

describe("the words on the panel are the reader's", () => {
  it("heads a state axis with a written word, never the stored one", async () => {
    stub(envelope("connectors", {
      object_ref: { object_type: "connector", id: "google-analytics" },
      connector_id: "google-analytics",
      states: { installation: "ready", coverage: "unused" },
      evidence: { contract: { auth_kind: "oauth2" } },
      evidence_as_of: "2026-08-17T09:00:00Z",
      links: {},
    }));

    mount("connectors", "google-analytics", "capabilities");

    // Asked of the STATE PANEL: `Coverage` is also a tab of this contract, so a
    // document-wide query would pass on the tab band alone.
    const states = within(await screen.findByTestId("data-object-states"));
    expect(states.getByText("Installation")).toBeInTheDocument();
    expect(states.getByText("Coverage")).toBeInTheDocument();
    expect(states.queryByText("installation")).toBeNull();
    expect(states.queryByText("coverage")).toBeNull();
    // The VALUE keeps its own sentence, from the same shared map.
    expect(states.getByText("Not used yet")).toBeInTheDocument();
  });

  it("gives an empty tab the vocabulary of THAT tab", async () => {
    stub(envelope("connectors", {
      object_ref: { object_type: "connector", id: "google-analytics" },
      connector_id: "google-analytics",
      states: { activation: "unavailable" },
      evidence: { contract: {} },
      evidence_as_of: null,
      links: {},
    }));

    mount("connectors", "google-analytics", "capabilities");

    expect(await screen.findByText("This Connector published no contract")).toBeInTheDocument();
    expect(screen.getByText(/come from the installed Connector itself/)).toBeInTheDocument();
    // The one sentence sixteen tabs shared.
    expect(screen.queryByText("Evidence unavailable")).toBeNull();
  });

  it("gives a different empty tab a different sentence", async () => {
    // Two mounts, because a single one passes just as well against a screen
    // that still holds one sentence for every tab.
    stub(envelope("events", {
      object_ref: { object_type: "event-configuration", id: "evc_1" },
      name: "Campaign launches",
      states: { lifecycle: "unavailable" },
      evidence: { collection_policy: {} },
      evidence_as_of: null,
      links: {},
    }));

    mount("events", "evc_1", "collection");

    expect(await screen.findByText("No collection policy is pinned here")).toBeInTheDocument();
    expect(screen.queryByText("This Connector published no contract")).toBeNull();
  });
});

describe("a failed read is not a dead end", () => {
  it("offers the collection beside Retry", async () => {
    stub({ nothing: true }, false);

    mount("imports", "mfl_1", "overview");

    await waitFor(() => expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument());
    expect(screen.getByRole("link", { name: "Back to Imports" })).toHaveAttribute(
      "href",
      `/org/${ORG}/project/${PROJECT}/data/imports`,
    );
    expect(unresolvable()).toEqual([]);
  });
});
