/**
 * Story 60.4 — the scope filter of the three lenses that carry a transformation
 * family, and the fourth family named with its owner.
 *
 * WHAT THIS FILE DOES NOT TEST, and the reason is the story: there is no
 * `Transformations` lens and no `Transformations` tab. `governance.md:37-38`
 * ratifies "exactly four stable Level 2 screens … never as additional permanent
 * navigation", and `governance.md:43,84,200` names five Semantic Model lenses,
 * none of them called `Transformations`. The proof that no sixth lens appeared
 * is asserted elsewhere and deliberately not duplicated here:
 * `Router.test.tsx:157-196` (eighteen lenses), `GovernanceScreens.test.tsx:516-529`
 * (the five exact labels) and `test_governance_read_model.py:300-301,750,937`.
 *
 * These tests mount `ContentRouter` through the REAL router, so what is proven
 * is the address → parser → screen chain: a filter that does not survive being
 * copied out of the address bar is not a view, it is a mood
 * (`GovernanceCollection.tsx:4-7`).
 */
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import ContentRouter from "../shell/ContentRouter";
import { RouterProvider } from "../shell/router";

const ORG = "org_EXAMPLE";
const PROJECT = "proj_EXAMPLE";
const ROOT = `/org/${ORG}/project/${PROJECT}`;

function ok(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) } as Response;
}

function fail(status: number, code: string): Response {
  return { ok: false, status, json: async () => ({ code, message: "refused" }), text: async () => code } as Response;
}

function facet(state: string, count = 0) {
  return { state, count, refs: [], truncated: false };
}

function item(type: string, id: string, label: string, scope: string) {
  return {
    object_ref: {
      type,
      id,
      label,
      owner_href: { surface: "project", workspace: "governance", section: "semantic-model" },
    },
    scope,
    owner: { kind: type },
    lifecycle_status: "active",
    active_version_ref: null,
    selected_version_ref: null,
    available_tabs: ["overview", "used-by"],
    default_tab: "overview",
    allowed_actions: [],
    used_by: facet("available", 2),
    versions: facet("unavailable"),
    evidence: facet("unavailable"),
    summary: {},
    evidence_as_of: "2026-08-09T08:00:00Z",
  };
}

function collection(lens: string, items: unknown[], overrides: Record<string, unknown> = {}) {
  return {
    schema_version: "governance-collection.v1",
    project_ref: { object_type: "project", id: PROJECT },
    organization_ref: { object_type: "organization", id: ORG },
    section: "semantic-model",
    generated_at: "2026-08-09T09:00:00Z",
    evidence_as_of: "2026-08-09T08:00:00Z",
    lens,
    default_lens: "concepts",
    available_lenses: [lens],
    items,
    coverage: { state: items.length ? "available" : "empty", returned: items.length, total: items.length, bound: 200 },
    unavailable_reasons: [],
    ...overrides,
  };
}

function serve(routes: Array<[RegExp, Response | (() => Response)]>) {
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string) => {
      calls.push(url);
      for (const [pattern, answer] of routes) {
        if (pattern.test(url)) return Promise.resolve(typeof answer === "function" ? answer() : answer);
      }
      return Promise.resolve(fail(404, "not_found"));
    }),
  );
  return calls;
}

function open(address: string) {
  const index = address.indexOf("?");
  window.history.replaceState({}, "", address);
  return { render: render(<RouterProvider><ContentRouter /></RouterProvider>), search: index === -1 ? "" : address.slice(index) };
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  window.history.replaceState({}, "", "/");
});

const VALUE_TABLES = [
  item("value-mapping-table", "vmt_1", "Campaign to product line", "project"),
  item("value-mapping-table", "vmt_2", "Agency naming", "organization"),
];

const CLEANUP_RULES = [
  item("cleanup-rule", "clr_1", "Drop test campaigns", "project"),
  item("cleanup-rule", "clr_2", "Strip UTM tail", "project"),
];

describe("the scope selector offers what the lens actually loaded", () => {
  it("derives its options from the items, and never from a hardcoded list", async () => {
    serve([
      [/value-mapping-tables/, ok({ tables: [] })],
      [/governance/, ok(collection("value-tables", VALUE_TABLES))],
    ]);
    open(`${ROOT}/governance/semantic-model/lens/value-tables`);

    const selector = await screen.findByTestId("scope-filter");
    const options = within(selector).getAllByRole("option").map((option) => option.textContent);
    // `project` and `organization` are the two scopes a value table can carry
    // (`governance_read_model.py:1687`); `platform` is refused by that store and
    // is therefore absent here because no item carries it — not because a list
    // in this file left it out.
    expect(options).toEqual(["Any scope", "organization", "project"]);
  });

  it("offers no choice when every object of the lens carries the same scope", async () => {
    serve([
      [/projects\/[^/]+\/cleanup-rules/, ok({ rules: [] })],
      [/governance/, ok(collection("cleanup-rules", CLEANUP_RULES))],
    ]);
    open(`${ROOT}/governance/semantic-model/lens/cleanup-rules`);

    // A cleanup rule is `project` in the read model, hardcoded
    // (`governance_read_model.py:1777`). A selector with one option is a control
    // that cannot change anything.
    expect(await screen.findByText("Drop test campaigns")).toBeInTheDocument();
    expect(screen.queryByTestId("scope-filter")).not.toBeInTheDocument();
  });

  it("does not draw a filter above an envelope it could not read", async () => {
    serve([[/governance/, fail(500, "unavailable")]]);
    open(`${ROOT}/governance/semantic-model/lens/value-tables`);

    expect(await screen.findByText(/No substitute content has been shown/)).toBeInTheDocument();
    // A filter above a nothing reads as "no object matches", which is a
    // different sentence from "this lens could not be read".
    expect(screen.queryByTestId("scope-filter")).not.toBeInTheDocument();
  });

  it("keeps the control when the address scope is the ONLY option left", async () => {
    // The hole the test below did not cover, and the one that bit: it loads a
    // lens that carries OTHER objects, so `scopeOptions` had two entries and the
    // selector was drawn for a reason that had nothing to do with the address.
    // With an EMPTY lens the options are exactly `[scope]` -- one option -- and
    // `showScopeFilter = scopeOptions.length > 1` removed the control from under
    // the very sentence telling the person to use it.
    serve([
      [/projects\/[^/]+\/cleanup-rules/, ok({ rules: [] })],
      [/governance/, ok(collection("cleanup-rules", []))],
    ]);
    open(`${ROOT}/governance/semantic-model/lens/cleanup-rules?scope=organization`);

    expect(
      await screen.findByText("No object of this lens has scope organization"),
    ).toBeInTheDocument();
    const selector = await screen.findByTestId("scope-filter");
    expect(within(selector).getAllByRole("option").map((option) => option.textContent)).toEqual([
      "Any scope",
      "organization",
    ]);
  });

  it("keeps the control that clears a scope the lens has no object for", async () => {
    serve([
      [/projects\/[^/]+\/cleanup-rules/, ok({ rules: [] })],
      [/governance/, ok(collection("cleanup-rules", CLEANUP_RULES))],
    ]);
    open(`${ROOT}/governance/semantic-model/lens/cleanup-rules?scope=organization`);

    // The filter is in the ADDRESS, so it can arrive from a shared link on a
    // lens that offers no choice. Hiding the selector there would leave a person
    // in front of an empty table with nothing to undo it with.
    const selector = await screen.findByTestId("scope-filter");
    expect(within(selector).getAllByRole("option").map((option) => option.textContent)).toEqual([
      "Any scope",
      "organization",
      "project",
    ]);
  });
});

describe("three sentences, and none of them is the other", () => {
  it("says a filter matched nothing, not that the lens is empty", async () => {
    serve([
      [/projects\/[^/]+\/cleanup-rules/, ok({ rules: [] })],
      [/governance/, ok(collection("cleanup-rules", CLEANUP_RULES))],
    ]);
    open(`${ROOT}/governance/semantic-model/lens/cleanup-rules?scope=organization`);

    expect(await screen.findByText("No object of this lens has scope organization")).toBeInTheDocument();
    expect(screen.queryByText("Nothing governed here yet")).not.toBeInTheDocument();
  });

  it("says a lens holds nothing ONCE, in the sentence that names the gesture", async () => {
    serve([
      [/projects\/[^/]+\/cleanup-rules/, ok({ rules: [] })],
      [/governance/, ok(collection("cleanup-rules", []))],
    ]);
    open(`${ROOT}/governance/semantic-model/lens/cleanup-rules`);

    // The lens owns its empty case (`CleanupRules.tsx:237-238`) and it names who
    // fills the list. The generic "Nothing governed here yet" rendered
    // underneath it until 2026-08-17: two empty states for one empty list, and a
    // person reads the last one they see — the one that names no gesture.
    expect(await screen.findByText("No cleanup rule on this Project yet.")).toBeInTheDocument();
    expect(screen.queryByText("Nothing governed here yet")).not.toBeInTheDocument();
    expect(screen.queryByTestId("scope-filter")).not.toBeInTheDocument();
  });
});

describe("the filter is an address, and it never leaves the browser", () => {
  it("applies the scope carried by the address and asks the server nothing about it", async () => {
    const calls = serve([
      [/value-mapping-tables/, ok({ tables: [] })],
      [/governance/, ok(collection("value-tables", VALUE_TABLES))],
    ]);
    open(`${ROOT}/governance/semantic-model/lens/value-tables?scope=organization`);

    expect(await screen.findByText("Agency naming")).toBeInTheDocument();
    expect(screen.queryByText("Campaign to product line")).not.toBeInTheDocument();
    // `_COLLECTION_QUERY_KEYS` (`governance_surface_api.py:85-95`) knows nine
    // Evidence keys and refuses everything else, and
    // `governance_read_model.py:2274-2275` raises for any parameter outside
    // Evidence. A scope sent to the server would be a 400 on every one of these
    // lenses.
    await waitFor(() => expect(calls.some((url) => url.includes("lens=value-tables"))).toBe(true));
    expect(calls.some((url) => url.includes("scope="))).toBe(false);
  });

  it("writes the chosen scope into the address so the view can be sent to someone", async () => {
    const user = userEvent.setup();
    serve([
      [/value-mapping-tables/, ok({ tables: [] })],
      [/governance/, ok(collection("value-tables", VALUE_TABLES))],
    ]);
    open(`${ROOT}/governance/semantic-model/lens/value-tables`);

    await user.selectOptions(await screen.findByTestId("scope-filter"), "project");
    await waitFor(() => expect(window.location.search).toBe("?scope=project"));
    expect(window.location.pathname).toBe(`${ROOT}/governance/semantic-model/lens/value-tables`);
    expect(screen.queryByText("Agency naming")).not.toBeInTheDocument();
  });

  it("drops the scope when the lens changes, because a scope of another list is not this one's", async () => {
    const user = userEvent.setup();
    serve([
      [/value-mapping-tables/, ok({ tables: [] })],
      [/projects\/[^/]+\/cleanup-rules/, ok({ rules: [] })],
      [/lens=cleanup-rules/, ok(collection("cleanup-rules", CLEANUP_RULES))],
      [/governance/, ok(collection("value-tables", VALUE_TABLES))],
    ]);
    open(`${ROOT}/governance/semantic-model/lens/value-tables?scope=organization`);

    await user.click(await screen.findByRole("link", { name: "Cleanup Rules" }));
    await waitFor(() => expect(window.location.pathname).toBe(`${ROOT}/governance/semantic-model/lens/cleanup-rules`));
    expect(window.location.search).toBe("");
  });
});

describe("the fourth family is named with its owner (story 60.6)", () => {
  it("names the Workbench columns, the tab that edits them, and refuses to edit them here", async () => {
    serve([
      [/value-mapping-tables/, ok({ tables: [] })],
      [/governance/, ok(collection("value-tables", VALUE_TABLES))],
    ]);
    open(`${ROOT}/governance/semantic-model/lens/value-tables`);

    const note = await screen.findByTestId("owned-elsewhere-excluded-joined-and-split-columns");
    expect(note).toHaveTextContent("Excluded, joined and split columns");
    // The tab is NAMED from the route registry, so this sentence cannot outlive
    // the tab it points at.
    expect(note).toHaveTextContent("Mapping tab");
    expect(note).toHaveTextContent(/not edited (here|in Governance)/i);
    // And the link goes to the collection that LISTS the objects carrying that
    // tab -- the deepest address that exists without a Datastream identifier,
    // which this envelope does not carry.
    expect(within(note).getAllByRole("link")[0]).toHaveAttribute("href", `${ROOT}/data/datastreams`);
  });
});
