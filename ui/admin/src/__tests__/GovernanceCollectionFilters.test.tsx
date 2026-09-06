/**
 * The Governance collections are FILTERABLE, and the filter is an address.
 *
 * Every one of the four sections draws the same instant search bar, and three of
 * them declared no query state at all — so `buildPath` threw
 * "This section declares no query state" (`router.tsx:246`) the moment anyone
 * pressed Enter in it. Master Data was the worst of the three: it is the only
 * SERVER-PAGED section, so its Search button, its scope selector, its
 * `First page` and its `Next page` all called `buildPath` and all threw. The
 * screen could be read and could not be narrowed.
 *
 * These tests mount `ContentRouter` through the REAL router, so what is proven
 * is the chain a person exercises: keystroke → handler → `buildPath` → address →
 * parser → screen. A narrowing that does not survive being copied out of the
 * address bar is not a view, it is a mood (`GovernanceCollection.tsx:4-7`).
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import ContentRouter from "../shell/ContentRouter";
import { RouterProvider } from "../shell/router";

const ORG = "org_EXAMPLE";
const PROJECT = "proj_EXAMPLE";
const ROOT = `/org/${ORG}/project/${PROJECT}`;
const SEARCH_PLACEHOLDER = "🔍 Instant Search (concepts, views, scope...)";

function ok(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) } as Response;
}

function fail(status: number, code: string): Response {
  return { ok: false, status, json: async () => ({ code, message: "refused" }), text: async () => code } as Response;
}

function facet(state: string, count = 0) {
  return { state, count, refs: [], truncated: false };
}

/** The default tab has to be one the object contract DECLARES: `objectHref`
 *  builds a real address for every row, and `buildPath` refuses a tab the
 *  contract never named (`router.tsx:272`). */
const DEFAULT_TAB: Record<string, string> = {
  "semantic-concept": "definition",
};

function item(section: string, type: string, id: string, label: string, scope: string) {
  const defaultTab = DEFAULT_TAB[type] ?? "overview";
  return {
    object_ref: {
      type,
      id,
      label,
      owner_href: { surface: "project", workspace: "governance", section },
    },
    scope,
    owner: { kind: type },
    lifecycle_status: "active",
    active_version_ref: null,
    selected_version_ref: null,
    available_tabs: [defaultTab, "used-by"],
    default_tab: defaultTab,
    allowed_actions: [],
    used_by: facet("available", 1),
    versions: facet("unavailable"),
    evidence: facet("unavailable"),
    summary: {},
    evidence_as_of: "2026-08-17T08:00:00Z",
  };
}

function collection(
  section: string,
  lens: string,
  items: unknown[],
  overrides: Record<string, unknown> = {},
) {
  return {
    schema_version: "governance-collection.v1",
    project_ref: { object_type: "project", id: PROJECT },
    organization_ref: { object_type: "organization", id: ORG },
    section,
    generated_at: "2026-08-17T09:00:00Z",
    evidence_as_of: "2026-08-17T08:00:00Z",
    lens,
    default_lens: lens,
    available_lenses: [lens],
    items,
    coverage: {
      state: items.length ? "available" : "empty",
      returned: items.length,
      total: items.length,
      bound: 200,
      index_state: "complete",
      adapters: [],
    },
    unavailable_reasons: [],
    next_cursor: null,
    applied_filters: {},
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
  window.history.replaceState({}, "", address);
  return render(<RouterProvider><ContentRouter /></RouterProvider>);
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  window.history.replaceState({}, "", "/");
});

const DOMAINS = [
  item("master-data", "business-domain", "bd_1", "Commerce", "project"),
  item("master-data", "business-domain", "bd_2", "Finance", "organization"),
];

describe("Master Data — the section that pages on the server can be narrowed at all", () => {
  it("turns a typed term and the Search button into an address instead of throwing", async () => {
    serve([[/governance/, ok(collection("master-data", "business-domains", DOMAINS, {
      filter_options: { scopes: ["organization", "project"] },
    }))]]);
    open(`${ROOT}/governance/master-data/lens/business-domains`);

    fireEvent.change(await screen.findByPlaceholderText(SEARCH_PLACEHOLDER), {
      target: { value: "commerce" },
    });
    // Until 2026-08-17 this click raised "This section declares no query state".
    fireEvent.click(screen.getByRole("button", { name: "Search" }));

    await waitFor(() => expect(window.location.search).toBe("?q=commerce"));
    expect(window.location.pathname).toBe(`${ROOT}/governance/master-data/lens/business-domains`);
  });

  it("carries the term AND the scope, and asks the server for both", async () => {
    const calls = serve([[/governance/, ok(collection("master-data", "business-domains", DOMAINS, {
      filter_options: { scopes: ["organization", "project"] },
    }))]]);
    open(`${ROOT}/governance/master-data/lens/business-domains?scope=project`);

    fireEvent.change(await screen.findByPlaceholderText(SEARCH_PLACEHOLDER), {
      target: { value: "commerce" },
    });
    fireEvent.keyDown(screen.getByPlaceholderText(SEARCH_PLACEHOLDER), { key: "Enter" });

    await waitFor(() => expect(window.location.search).toBe("?q=commerce&scope=project"));
    // Master Data is the ONE section whose search is the server's:
    // `governance_read_model.py:2889` allows exactly {cursor, limit, q, scope}.
    await waitFor(() =>
      expect(calls.some((url) => url.includes("q=commerce") && url.includes("scope=project"))).toBe(true),
    );
  });

  it("writes the chosen scope into the address, keeping the term already typed", async () => {
    serve([[/governance/, ok(collection("master-data", "business-domains", DOMAINS, {
      filter_options: { scopes: ["organization", "project"] },
    }))]]);
    open(`${ROOT}/governance/master-data/lens/business-domains?q=commerce`);

    const selector = await screen.findByTestId("scope-filter");
    expect(within(selector).getAllByRole("option").map((option) => option.textContent)).toEqual([
      "Any scope",
      "organization",
      "project",
    ]);
    fireEvent.change(selector, { target: { value: "organization" } });

    await waitFor(() => expect(window.location.search).toBe("?q=commerce&scope=organization"));
  });

  it("pages, and comes back to the first page without losing the narrowing", async () => {
    serve([[/governance/, ok(collection("master-data", "business-domains", DOMAINS, {
      filter_options: { scopes: ["project"] },
      next_cursor: "50",
      coverage: { state: "available", returned: 2, total: 120, bound: 200, index_state: "complete", adapters: [] },
    }))]]);
    open(`${ROOT}/governance/master-data/lens/business-domains?q=commerce`);

    fireEvent.click(await screen.findByRole("button", { name: "Next page" }));
    // `limit` travels with the cursor: it is part of the page the cursor names.
    await waitFor(() => expect(window.location.search).toBe("?q=commerce&cursor=50&limit=50"));

    fireEvent.click(await screen.findByRole("button", { name: "First page" }));
    await waitFor(() => expect(window.location.search).toBe("?q=commerce"));
  });
});

describe("the other three sections narrow in the browser, and say so in the address", () => {
  it("Semantic Model carries the term without sending it to a server that refuses it", async () => {
    const calls = serve([
      [/value-mapping-tables/, ok({ tables: [] })],
      [/governance/, ok(collection("semantic-model", "concepts", [
        item("semantic-model", "semantic-concept", "sc_1", "Gross Revenue", "project"),
        item("semantic-model", "semantic-concept", "sc_2", "Margin", "project"),
      ]))],
    ]);
    open(`${ROOT}/governance/semantic-model/lens/concepts`);

    fireEvent.change(await screen.findByPlaceholderText(SEARCH_PLACEHOLDER), {
      target: { value: "margin" },
    });
    fireEvent.keyDown(screen.getByPlaceholderText(SEARCH_PLACEHOLDER), { key: "Enter" });

    await waitFor(() => expect(window.location.search).toBe("?q=margin"));
    // `governance_read_model.py:2940-2943` raises "accepts no query parameters"
    // for every section outside Evidence and Master Data.
    expect(calls.some((url) => url.includes("q="))).toBe(false);
    expect(await screen.findByText("Margin")).toBeInTheDocument();
    expect(screen.queryByText("Gross Revenue")).not.toBeInTheDocument();
  });

  it("Controls & Quality carries the term, which used to throw on Enter", async () => {
    serve([[/governance/, ok(collection("controls-quality", "rule-sets", [
      item("controls-quality", "rule-set", "rs_1", "FX ladder", "project"),
    ]))]]);
    open(`${ROOT}/governance/controls-quality/lens/rule-sets`);

    fireEvent.change(await screen.findByPlaceholderText(SEARCH_PLACEHOLDER), {
      target: { value: "ladder" },
    });
    fireEvent.keyDown(screen.getByPlaceholderText(SEARCH_PLACEHOLDER), { key: "Enter" });

    await waitFor(() => expect(window.location.search).toBe("?q=ladder"));
  });

  it("Evidence carries the term in the address and NEVER in the request", async () => {
    const calls = serve([[/governance/, ok(collection("evidence", "lineage-provenance", [
      item("evidence", "evidence-trace", "ev_1", "Datastream execution", "project"),
    ], {
      coverage: { state: "available", returned: 1, total: 1, bound: 200, index_state: "complete", adapters: [] },
    }))]]);
    open(`${ROOT}/governance/evidence/lens/lineage-provenance`);

    fireEvent.change(await screen.findByPlaceholderText(SEARCH_PLACEHOLDER), {
      target: { value: "execution" },
    });
    fireEvent.keyDown(screen.getByPlaceholderText(SEARCH_PLACEHOLDER), { key: "Enter" });

    await waitFor(() => expect(window.location.search).toBe("?q=execution"));
    // `normalize_filters` (`evidence_index.py:1823-1831`) refuses every key
    // outside `ALLOWED_FILTERS`, so an Evidence request carrying `q` is a 400 —
    // not a filtered list. The narrowing is applied over the page already
    // returned, and the address is what makes it shareable.
    await waitFor(() => expect(calls.some((url) => url.includes("lens=lineage-provenance"))).toBe(true));
    expect(calls.some((url) => url.includes("q="))).toBe(false);
  });
});

describe("one empty state per empty list", () => {
  it("lets a lens that owns its empty case say it alone", async () => {
    serve([
      [/projects\/[^/]+\/cleanup-rules/, ok({ rules: [] })],
      [/governance/, ok(collection("semantic-model", "cleanup-rules", []))],
    ]);
    open(`${ROOT}/governance/semantic-model/lens/cleanup-rules`);

    expect(await screen.findByText("No cleanup rule on this Project yet.")).toBeInTheDocument();
    // The generic sentence names no gesture, and a person reads the last one
    // they see.
    expect(screen.queryByText("Nothing governed here yet")).not.toBeInTheDocument();
  });

  it("keeps the generic state when it is the only way back from a narrowing", async () => {
    serve([
      [/projects\/[^/]+\/cleanup-rules/, ok({ rules: [] })],
      [/governance/, ok(collection("semantic-model", "cleanup-rules", []))],
    ]);
    open(`${ROOT}/governance/semantic-model/lens/cleanup-rules?scope=organization`);

    // Not a duplicate: it names WHICH narrowing hid the objects and how to undo
    // it, and no panel below is filtered by this screen's scope.
    expect(await screen.findByText("No object of this lens has scope organization")).toBeInTheDocument();
  });
});
