/**
 * Story 49.1 — the four Governance screens, through the REAL router.
 *
 * These tests mount `ContentRouter` inside `RouterProvider` with the browser
 * address set to the route under test, so what is proven is the whole chain a
 * person exercises: address → parser → registry → screen → the next address.
 * Mocking the router out would prove that a component renders when handed the
 * right props, which is not the thing that kept failing.
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
  return {
    ok: false,
    status,
    json: async () => ({ code, message: "refused" }),
    text: async () => code,
  } as Response;
}

function collection(section: string, lens: string, overrides: Record<string, unknown> = {}) {
  return {
    schema_version: "governance-collection.v1",
    project_ref: { object_type: "project", id: PROJECT },
    organization_ref: { object_type: "organization", id: ORG },
    section,
    generated_at: "2026-07-30T09:00:00Z",
    evidence_as_of: "2026-07-30T08:00:00Z",
    lens,
    default_lens: lens,
    available_lenses: [lens],
    items: [],
    coverage: { state: "empty", returned: 0, total: 0, bound: 200 },
    unavailable_reasons: [],
    ...overrides,
  };
}

function facet(state: string, refs: unknown[] = []) {
  return { state, count: refs.length, refs, truncated: false };
}

/**
 * `summary` is the owner's OPEN facet bag: each object type puts different keys
 * in it -- `slug`, `capability_key`, `hierarchy`, `aliases`. Inferred from one
 * fixture it froze as `{ slug: string }`, so the tests that describe the
 * Hierarchy and Aliases tabs could not state what those tabs read.
 */
function governedObject(type: string, id: string, label: string, overrides: Record<string, unknown> = {}) {
  const summary: Record<string, unknown> = { slug: "commerce" };
  return {
    object_ref: {
      type,
      id,
      label,
      owner_href: { surface: "project", workspace: "governance", section: "master-data" },
    },
    scope: "organization",
    owner: { kind: "business-taxonomy", organization_id: ORG },
    lifecycle_status: "active",
    active_version_ref: { object_type: "registry-version", id: "pcv_3", state: "active" },
    selected_version_ref: null,
    available_tabs: ["overview", "hierarchy", "mappings-aliases", "used-by", "versions"],
    default_tab: "overview",
    allowed_actions: [],
    used_by: facet("available", []),
    versions: facet("available", [
      { object_type: "registry-version", id: "pcv_3", state: "active", recorded_at: "2026-07-30T08:00:00Z" },
      { object_type: "registry-version", id: "pcv_1", state: "previous", recorded_at: "2026-06-01T08:00:00Z" },
    ]),
    evidence: facet("empty"),
    summary,
    evidence_as_of: "2026-07-30T08:00:00Z",
    ...overrides,
  };
}

function objectEnvelope(section: string, type: string, detail: unknown, overrides: Record<string, unknown> = {}) {
  return {
    schema_version: "governance-object.v1",
    project_ref: { object_type: "project", id: PROJECT },
    organization_ref: { object_type: "organization", id: ORG },
    section,
    object_type: type,
    generated_at: "2026-07-30T09:00:00Z",
    evidence_as_of: "2026-07-30T08:00:00Z",
    state: detail ? "available" : "unavailable",
    object: detail,
    unavailable_reasons: [],
    ...overrides,
  };
}

/** Route the fetch mock by URL so a test states what each address answers. */
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

function open(pathname: string) {
  window.history.replaceState({}, "", pathname);
  return render(
    <RouterProvider>
      <ContentRouter />
    </RouterProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  window.history.replaceState({}, "", "/");
});

const SCREENS = [
  ["master-data", "Master Data", "business-domains"],
  ["semantic-model", "Semantic Model", "concepts"],
  ["controls-quality", "Controls & Quality", "conflicts"],
  ["evidence", "Evidence", "lineage-provenance"],
] as const;

describe("the four Governance collections", () => {
  it.each(SCREENS)(
    "%s mounts at its canonical route, canonicalizes to its default lens and renders one H1",
    async (section, title, defaultLens) => {
      const calls = serve([[/governance/, ok(collection(section, defaultLens))]]);
      open(`${ROOT}/governance/${section}`);

      expect(await screen.findByRole("heading", { level: 1, name: title })).toBeInTheDocument();
      expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
      // The bare address is replaced, so the address bar is shareable.
      expect(window.location.pathname).toBe(`${ROOT}/governance/${section}/lens/${defaultLens}`);
      await waitFor(() =>
        expect(calls.some((url) => url.includes(`lens=${defaultLens}`))).toBe(true),
      );
    },
  );

  it("renders every declared lens as a real, route-backed anchor", async () => {
    serve([[/governance/, ok(collection("master-data", "business-domains"))]]);
    open(`${ROOT}/governance/master-data/lens/classifications`);

    const rail = await screen.findByRole("navigation", { name: "Master Data lenses" });
    const anchors = within(rail).getAllByRole("link");
    expect(anchors.map((anchor) => anchor.textContent)).toEqual([
      "Business Domains", "Classifications", "Products", "Activities", "Registries",
      // Story 48.5: conditional in CONTENT (the server answers `unavailable`
      // when Competitors is Disabled), permanent in the ROUTE registry -- so the
      // address stays shareable and a lens never appears and disappears.
      "Competitor Registry",
    ]);
    expect(anchors.map((anchor) => anchor.getAttribute("href"))).toEqual([
      `${ROOT}/governance/master-data/lens/business-domains`,
      `${ROOT}/governance/master-data/lens/classifications`,
      `${ROOT}/governance/master-data/lens/products`,
      `${ROOT}/governance/master-data/lens/activities`,
      `${ROOT}/governance/master-data/lens/registries`,
      `${ROOT}/governance/master-data/lens/competitor-registry`,
    ]);
  });

  it("changes the address when a lens is chosen, and survives back and forward", async () => {
    const user = userEvent.setup();
    serve([
      [/lens=registries/, ok(collection("master-data", "registries"))],
      [/governance/, ok(collection("master-data", "business-domains"))],
    ]);
    open(`${ROOT}/governance/master-data/lens/business-domains`);

    await user.click(await screen.findByRole("link", { name: "Registries" }));
    expect(window.location.pathname).toBe(`${ROOT}/governance/master-data/lens/registries`);

    window.history.replaceState({}, "", `${ROOT}/governance/master-data/lens/business-domains`);
    window.dispatchEvent(new PopStateEvent("popstate"));
    await waitFor(() =>
      expect(screen.getByRole("link", { name: "Business Domains" })).toHaveAttribute("aria-current", "page"),
    );
  });

  it("opens the exact object route from a row, through a keyboard-reachable anchor", async () => {
    const user = userEvent.setup();
    serve([
      [/objects\/business-domain\/bd_1/, ok(objectEnvelope("master-data", "business-domain", governedObject("business-domain", "bd_1", "Commerce")))],
      [/governance/, ok(collection("master-data", "business-domains", {
        items: [governedObject("business-domain", "bd_1", "Commerce")],
        coverage: { state: "available", returned: 1, total: 1, bound: 200 },
      }))],
    ]);
    open(`${ROOT}/governance/master-data/lens/business-domains`);

    const row = await screen.findByRole("link", { name: "Commerce" });
    expect(row).toHaveAttribute(
      "href",
      `${ROOT}/governance/master-data/object/business-domain/bd_1/tab/overview`,
    );
    row.focus();
    await user.keyboard("{Enter}");
    await waitFor(() =>
      expect(window.location.pathname).toBe(
        `${ROOT}/governance/master-data/object/business-domain/bd_1/tab/overview`,
      ),
    );
  });

  /**
   * This test used to assert that the lens named the STORY that owned it --
   * "owned by Story 49.2, which has not been delivered". Products and Activities
   * are living lenses now, and that sentence was a deployment state shown to a
   * person who has no tracker and no gesture to make from it. The property it
   * held is real and stays: an unreadable lens says so instead of showing an
   * empty success. It is aimed at the reason the server actually produces.
   */
  it("says why an unreadable lens shows nothing, instead of a false empty success", async () => {
    serve([
      [/governance/, ok(collection("master-data", "products", {
        coverage: { state: "unavailable", returned: 0, total: null, bound: 200 },
        unavailable_reasons: [{
          code: "products_store_unreadable",
          message:
            "The Master Data store could not be read, so the products of this Project are unknown. This is not a count of zero. (OperationalError)",
        }],
      }))],
    ]);
    open(`${ROOT}/governance/master-data/lens/products`);

    expect(await screen.findByTestId("reason-products_store_unreadable")).toHaveTextContent(
      "not a count of zero",
    );
    expect(screen.getByText("No owner answers this lens yet")).toBeInTheDocument();
    expect(screen.queryByText("Nothing governed here yet")).not.toBeInTheDocument();
  });

  it("shows an empty lens its own sentence, in the empty state and not as a warning", async () => {
    serve([
      [/governance/, ok(collection("master-data", "products", {
        unavailable_reasons: [{
          code: "products_object_kind_not_declared",
          message:
            "This Project has not said what a Product is, so there is none to govern. Declare the object kind 'product' on the Datastream whose mapping identifies your products, and every one that mapping names is governed here.",
        }],
      }))],
    ]);
    open(`${ROOT}/governance/master-data/lens/products`);

    // The gesture, where the list would have been.
    expect(await screen.findByText(/Declare the object kind 'product'/)).toBeInTheDocument();
    // And not a second time, in a warning banner above a table that is fine.
    expect(screen.queryByTestId("reason-products_object_kind_not_declared")).not.toBeInTheDocument();
  });

  it("names BOTH gestures that fill Registries, rather than hiding the lens", async () => {
    serve([[/governance/, ok(collection("master-data", "registries"))]]);
    open(`${ROOT}/governance/master-data/lens/registries`);

    // Two gestures fill this lens (`governance.md`, "a registry is listed
    // whether a project capability pins it or a client declared it"). Naming
    // only the capability one sent a reader to Project Settings for something
    // no capability can give them: a client object kind has no screen at all
    // (AI-232).
    const empty = await screen.findByText(/Registries appear here in two ways/);
    expect(empty).toBeInTheDocument();
    expect(empty.textContent).toMatch(/Project Settings › Capabilities/);
    expect(empty.textContent).toMatch(/declare your own object kind/);
    // The lens itself stays in the rail: it is the inventory of where they land.
    expect(screen.getByRole("link", { name: "Registries" })).toBeInTheDocument();
  });

  it("names the workspace that owns what Governance only projects", async () => {
    serve([[/governance/, ok(collection("semantic-model", "concepts"))]]);
    open(`${ROOT}/governance/semantic-model/lens/concepts`);

    const link = await screen.findByRole("link", { name: "Physical field mapping" });
    expect(link).toHaveAttribute("href", `${ROOT}/data/datastreams`);
  });

  it("asks its owner once instead of fanning out and joining in the browser", async () => {
    const calls = serve([[/governance/, ok(collection("evidence", "versions-approvals", {
      items: [governedObject("object-version", "pconf_1", "Orders mapping approval")],
      coverage: { state: "available", returned: 1, total: 1, bound: 200 },
    }))]]);
    open(`${ROOT}/governance/evidence/lens/versions-approvals`);
    await screen.findByRole("link", { name: "Orders mapping approval" });

    // Counts, coverage and used-by come from the owner, composed once. A second
    // request here would mean the browser is doing the authoritative join.
    expect(calls).toHaveLength(1);
    expect(calls[0]).toContain("/governance/evidence?lens=versions-approvals");
  });

  it("refuses a response for another Project instead of rendering it", async () => {
    serve([[/governance/, ok(collection("master-data", "business-domains", {
      project_ref: { object_type: "project", id: "proj_OTHER" },
    }))]]);
    open(`${ROOT}/governance/master-data/lens/business-domains`);

    expect(await screen.findByText(/does not match the address that was opened/)).toBeInTheDocument();
  });

  it.each([
    [403, "This project route cannot be opened"],
    [404, "This address is not registered"],
  ])("turns %i into its own route state, never into an empty collection", async (status, heading) => {
    serve([[/governance/, fail(status, status === 403 ? "denied" : "not_found")]]);
    open(`${ROOT}/governance/evidence/lens/audit-activity`);

    expect(await screen.findByRole("heading", { level: 1, name: heading })).toBeInTheDocument();
  });

  it("leaves an unregistered lens Unknown rather than repairing it to the default", async () => {
    serve([[/governance/, ok(collection("master-data", "business-domains"))]]);
    open(`${ROOT}/governance/master-data/lens/not-real`);

    expect(await screen.findByRole("heading", { level: 1, name: "This address is not registered" })).toBeInTheDocument();
    expect(window.location.pathname).toBe(`${ROOT}/governance/master-data/lens/not-real`);
  });
});

describe("the shared governed-object workbench", () => {
  const TYPES = [
    ["master-data", "business-domain", "overview"],
    ["master-data", "master-data-object", "overview"],
    ["master-data", "registry", "overview"],
    ["semantic-model", "semantic-concept", "definition"],
    ["semantic-model", "semantic-view", "definition"],
    ["controls-quality", "control-case", "evidence"],
    ["controls-quality", "dq-monitor", "overview"],
    ["controls-quality", "rule-set", "overview"],
    ["evidence", "evidence-trace", "overview"],
    ["evidence", "object-version", "overview"],
    ["evidence", "audit-event", "overview"],
  ] as const;

  it.each(TYPES)("direct-loads %s/%s on its declared default tab", async (section, type, defaultTab) => {
    serve([[/objects\//, ok(objectEnvelope(section, type, governedObject(type, "obj_1", "Object one", {
      available_tabs: [defaultTab],
      default_tab: defaultTab,
    })))]]);
    open(`${ROOT}/governance/${section}/object/${type}/obj_1`);

    expect(await screen.findByRole("heading", { level: 1, name: "Object one" })).toBeInTheDocument();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(window.location.pathname).toBe(`${ROOT}/governance/${section}/object/${type}/obj_1/tab/${defaultTab}`);
  });

  it("shows a labelled breadcrumb, the identity, the owner, the status and the version", async () => {
    serve([[/objects\//, ok(objectEnvelope("master-data", "business-domain", governedObject("business-domain", "bd_1", "Commerce")))]]);
    open(`${ROOT}/governance/master-data/object/business-domain/bd_1/tab/overview`);

    const breadcrumb = await screen.findByRole("navigation", { name: "breadcrumb" });
    expect(within(breadcrumb).getByRole("link", { name: "Master Data" })).toHaveAttribute(
      "href",
      `${ROOT}/governance/master-data/lens/business-domains`,
    );
    expect(within(breadcrumb).getByText("Commerce")).toHaveAttribute("aria-current", "page");
    expect(screen.getByText("Business Domain · bd_1")).toBeInTheDocument();
    expect(screen.getAllByText("active").length).toBeGreaterThan(0);
    expect(screen.getAllByText("pcv_3").length).toBeGreaterThan(0);
  });

  /**
   * The screen has to be able to act on the object it opened. Until 2026-08-30
   * this Overview was `Object.entries(summary)` and the only console caller of
   * the identity commands was the Context Hub, so a person who came here to
   * rename a Business Domain was sent to another screen for it.
   */
  it("mounts the identity commands on the Overview of the object it opened", async () => {
    serve([[/objects\//, ok(objectEnvelope("master-data", "business-domain", governedObject("business-domain", "bd_1", "Commerce")))]]);
    open(`${ROOT}/governance/master-data/object/business-domain/bd_1/tab/overview`);

    expect(await screen.findByTestId("master-data-rename")).toBeInTheDocument();
    expect(screen.getByTestId("master-data-archive")).toBeInTheDocument();
    // Typed, not dumped: the owner's storage keys are gone from the Overview.
    expect(screen.getByText("Short code")).toBeInTheDocument();
    expect(screen.queryByText("Slug")).toBeNull();
  });

  it("offers restore, and only restore, on an archived identity", async () => {
    serve([[/objects\//, ok(objectEnvelope("master-data", "business-domain", governedObject("business-domain", "bd_1", "Commerce", {
      lifecycle_status: "archived",
    })))]]);
    open(`${ROOT}/governance/master-data/object/business-domain/bd_1/tab/overview`);

    expect(await screen.findByTestId("master-data-restore")).toBeInTheDocument();
    expect(screen.queryByTestId("master-data-archive")).toBeNull();
  });

  it("keeps Used by, Versions and Evidence three different answers", async () => {
    const user = userEvent.setup();
    serve([[/objects\//, ok(objectEnvelope("master-data", "business-domain", governedObject("business-domain", "bd_1", "Commerce", {
      used_by: { state: "unavailable", count: 0, refs: [], truncated: false },
      evidence: { state: "empty", count: 0, refs: [], truncated: false },
    })))]]);
    open(`${ROOT}/governance/master-data/object/business-domain/bd_1/tab/used-by`);

    // Unavailable is not zero, and the copy says which one it is.
    expect(await screen.findByText("No owner answers Used by")).toBeInTheDocument();

    await user.click(screen.getByRole("link", { name: "Versions" }));
    await waitFor(() => expect(screen.getByRole("link", { name: "pcv_1" })).toBeInTheDocument());
    expect(screen.getByRole("link", { name: "pcv_1" })).toHaveAttribute(
      "href",
      `${ROOT}/governance/master-data/object/business-domain/bd_1/tab/versions/version/pcv_1`,
    );
  });

  it("opens the delivered Master Data Hierarchy tab and states rootedness as an answer", async () => {
    // This test used to assert "Hierarchy is not delivered" and name Story 49.2.
    // That placeholder was honest until 49.2 shipped MasterDataTabs.tsx; keeping
    // the assertion would now pin the placeholder rather than the product.
    const detail = governedObject("business-domain", "bd_1", "Commerce");
    detail.summary = {
      ...detail.summary,
      hierarchy: {
        state: "available",
        is_root: true,
        ancestors: [],
        children: [{ kind: "master-data-object", id: "cls_1", label: "Retail", status: "active" }],
        reason: null,
      },
    };
    serve([[/objects\//, ok(objectEnvelope("master-data", "business-domain", detail))]]);
    open(`${ROOT}/governance/master-data/object/business-domain/bd_1/tab/hierarchy`);

    // A Domain IS a root, so "no parent" is stated as an answer, not an absence.
    expect(await screen.findByText(/This object is a root/)).toBeInTheDocument();
    expect(screen.getByText("Retail")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Hierarchy" })).toBeInTheDocument();
  });

  it("mounts the dedicated Country workspace on the Country registry hierarchy tab", async () => {
    const detail = governedObject("registry", "reg_country", "Country registry", {
      summary: { slug: "country", capability_key: "country" },
    });
    const calls = serve([
      [
        /governance\/master-data\/country$/,
        ok({
          state: "preset_required",
          registry: null,
          presets: [],
          vocabulary: [],
          nodes: [],
          draft: null,
          current: null,
          versions: [],
          used_by: [],
        }),
      ],
      [
        /objects\/registry\/reg_country/,
        ok(objectEnvelope("master-data", "registry", detail)),
      ],
    ]);

    open(
      `${ROOT}/governance/master-data/object/registry/reg_country/tab/hierarchy`,
    );

    expect(
      await screen.findByRole("heading", {
        name: "Choose a qualified starting point",
      }),
    ).toBeInTheDocument();
    expect(
      calls.some((url) => url.endsWith("/governance/master-data/country")),
    ).toBe(true);
  });

  it("reports a missing alias store rather than an empty alias table", async () => {
    // The distinction the whole tab exists for: a Business Domain has no alias
    // store at all, and an empty table there would read as "no aliases".
    const detail = governedObject("business-domain", "bd_1", "Commerce");
    detail.summary = {
      ...detail.summary,
      aliases: {
        state: "unavailable",
        rows: [],
        reason: {
          code: "master_data_alias_store_absent",
          message: "No alias store exists for this object type.",
        },
      },
    };
    serve([[/objects\//, ok(objectEnvelope("master-data", "business-domain", detail))]]);
    open(`${ROOT}/governance/master-data/object/business-domain/bd_1/tab/mappings-aliases`);

    expect(await screen.findByText("The alias store could not be read")).toBeInTheDocument();
    expect(screen.getByText(/No alias store exists/)).toBeInTheDocument();
    expect(screen.queryByText("No alias recorded")).not.toBeInTheDocument();
  });

  it("opens the exact Epic 48 pinned version and says plainly that it is superseded", async () => {
    const detail = governedObject("registry", "reg_1", "Country registry", {
      selected_version_ref: { object_type: "registry-version", id: "pcv_1", state: "previous" },
    });
    serve([[/versions\/pcv_1/, ok(objectEnvelope("master-data", "registry", detail, {
      schema_version: "governance-version.v1",
      requested_version_id: "pcv_1",
      version_state: "stale",
    }))]]);
    open(`${ROOT}/governance/master-data/object/registry/reg_1/tab/versions/version/pcv_1`);

    const banner = await screen.findByTestId("selected-version-banner");
    expect(banner).toHaveTextContent("no longer current");
    expect(banner).toHaveTextContent("pcv_1");
    // The current version is named, and it is NOT what was opened.
    expect(banner).not.toHaveTextContent("pcv_3 is the active");
    expect(window.location.pathname).toBe(
      `${ROOT}/governance/master-data/object/registry/reg_1/tab/versions/version/pcv_1`,
    );
  });

  it("keeps an unknown identifier Unknown rather than opening a neighbour", async () => {
    serve([[/objects\//, fail(404, "not_found")]]);
    open(`${ROOT}/governance/master-data/object/business-domain/bd_absent/tab/overview`);

    expect(await screen.findByRole("heading", { level: 1, name: "This address is not registered" })).toBeInTheDocument();
    expect(screen.getByText(/Nothing similar has been opened in its place/)).toBeInTheDocument();
  });

  it("reports an undelivered owner as Unavailable, distinctly from Unknown", async () => {
    serve([[/objects\//, ok(objectEnvelope("controls-quality", "dq-monitor", null, {
      unavailable_reasons: [{
        code: "data_quality_owner_not_delivered",
        message: "The governed DQ Monitor object is owned by Story 49.4, which has not been delivered.",
      }],
    }))]]);
    open(`${ROOT}/governance/controls-quality/object/dq-monitor/dq_1/tab/overview`);

    expect(await screen.findByText(/Story 49.4/)).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(
      "This route is registered, but its workbench does not exist yet",
    );
  });

  it("drops the pinned version when leaving the versions tab rather than building an unknown route", async () => {
    const detail = governedObject("registry", "reg_1", "Country registry", {
      selected_version_ref: { object_type: "registry-version", id: "pcv_1", state: "previous" },
    });
    serve([[/objects\//, ok(objectEnvelope("master-data", "registry", detail, { version_state: "stale" }))]]);
    open(`${ROOT}/governance/master-data/object/registry/reg_1/tab/versions/version/pcv_1`);

    const overview = await screen.findByRole("link", { name: "Overview" });
    expect(overview).toHaveAttribute(
      "href",
      `${ROOT}/governance/master-data/object/registry/reg_1/tab/overview`,
    );
  });
});

describe("the Value Tables lens (story 60.1)", () => {
  it("is reachable from governance/semantic-model as a route-backed anchor", async () => {
    serve([[/governance/, ok(collection("semantic-model", "concepts"))]]);
    open(`${ROOT}/governance/semantic-model/lens/concepts`);

    const rail = await screen.findByRole("navigation", { name: "Semantic Model lenses" });
    const anchors = within(rail).getAllByRole("link");
    expect(anchors.map((anchor) => anchor.textContent)).toEqual([
      "Concepts",
      "Semantic Views",
      "Mapping Coverage",
      // Story 60.1. Named `Value Tables` and NOT `Transformations`: a lens
      // carries one object type, and the tab that will group three families
      // belongs to story 60.4.
      "Value Tables",
      // Story 60.3, and the same reasoning one lens further: a cleanup rule
      // removes rows at read, which is not what a lookup table does. The word is
      // NOT `Filter` — that one is taken twice by living code.
      "Cleanup Rules",
      // Lot A1, issue #68: the MDM canonical vocabulary. Its own lens because a
      // lens carries one object type, and a canonical field is not a Concept.
      "Canonical Fields",
      // Added to the rail on 2026-08-16 -- "the LOWER declaring store, visible
      // at last" -- and this list was not told. `resolve_declared_additivity`
      // reads that store on every render to decide whether a metric may be
      // summed across two days, so the lens is not decorative; the red was this
      // expectation, not the navigation.
      "Metric Definitions",
    ]);
    expect(anchors[3].getAttribute("href")).toBe(
      `${ROOT}/governance/semantic-model/lens/value-tables`,
    );
    expect(anchors[4].getAttribute("href")).toBe(
      `${ROOT}/governance/semantic-model/lens/cleanup-rules`,
    );
  });

  it("renders the library editor on that lens, and asks the server for it", async () => {
    const calls = serve([
      [/value-mapping-tables/, ok({ tables: [] })],
      [/governance/, ok(collection("semantic-model", "value-tables"))],
    ]);
    open(`${ROOT}/governance/semantic-model/lens/value-tables`);

    expect(await screen.findByTestId("transformations-library")).toBeInTheDocument();
    await waitFor(() =>
      expect(
        calls.some((url) => url.includes(`/api/projects/${PROJECT}/value-mapping-tables`)),
      ).toBe(true),
    );
  });

  it("says which owner failed instead of an empty list of tables", async () => {
    serve([
      [/value-mapping-tables/, fail(503, "unavailable")],
      [
        /governance/,
        ok(
          collection("semantic-model", "value-tables", {
            coverage: { state: "unavailable", returned: 0, total: null, bound: 200 },
            unavailable_reasons: [
              {
                code: "value_tables_store_unreadable",
                message:
                  "The transformations library could not be read, so the value mapping tables of this Project are unknown. This is not a count of zero.",
              },
            ],
          }),
        ),
      ],
    ]);
    open(`${ROOT}/governance/semantic-model/lens/value-tables`);

    expect(await screen.findByTestId("reason-value_tables_store_unreadable")).toHaveTextContent(
      "not a count of zero",
    );
    expect(screen.getByText("No owner answers this lens yet")).toBeInTheDocument();
  });
});

/**
 * Story 60.4. The rail assertion above is deliberately UNTOUCHED: those five
 * exact labels are what proves no sixth lens called `Transformations` appeared,
 * which is this story's principal decision rather than its omission.
 */
describe("the scope narrowing is an address (story 60.4)", () => {
  it("survives canonicalization, so reopening the shared link reopens the same view", async () => {
    const items = [
      governedObject("value-mapping-table", "vmt_1", "Campaign to product line", { scope: "project" }),
      governedObject("value-mapping-table", "vmt_2", "Agency naming", { scope: "organization" }),
    ];
    const calls = serve([
      [/value-mapping-tables/, ok({ tables: [] })],
      [
        /governance/,
        ok(
          collection("semantic-model", "value-tables", {
            items,
            coverage: { state: "available", returned: 2, total: 2, bound: 200 },
          }),
        ),
      ],
    ]);
    open(`${ROOT}/governance/semantic-model/lens/value-tables?scope=project`);

    expect(await screen.findByText("Campaign to product line")).toBeInTheDocument();
    expect(screen.queryByText("Agency naming")).not.toBeInTheDocument();
    // The provider replaces the address with its canonical form on every
    // resolution. A parameter the registry did not declare would be dropped
    // right here, and the filter would die on the first refresh — which is the
    // whole difference between a view and a mood.
    expect(window.location.search).toBe("?scope=project");
    // And it is applied in the browser: the collection route knows nine
    // Evidence keys and refuses everything else.
    await waitFor(() => expect(calls.some((url) => url.includes("lens=value-tables"))).toBe(true));
    expect(calls.some((url) => url.includes("scope="))).toBe(false);
  });
});
