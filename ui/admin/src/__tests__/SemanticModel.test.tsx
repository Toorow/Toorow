/**
 * Story 49.3 — the Semantic Model workbenches and the Explore handoff, through
 * the REAL router.
 *
 * These mount `ContentRouter` inside `RouterProvider` with the browser address
 * set to the route under test, so what is proven is the chain a person walks:
 * address → parser → registry → screen → the next address. What kept failing in
 * this repository was never "does the component render given the right props".
 *
 * What they hold in place:
 *
 *   - an unreadable Data owner shows Unavailable, never 0 / 0;
 *   - a refused metric/dimension pair carries the reason it was refused;
 *   - an exact version pin survives a refresh and the back button;
 *   - half a pin is not honoured, and `latest` is refused outright;
 *   - a superseded pin is shown as pinned, never swapped for the current one.
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import ContentRouter from "../shell/ContentRouter";
import { RouterProvider, buildPath, parsePath, type CanonicalRoute } from "../shell/router";
import { validateSectionQuery } from "../shell/navigation";

const ORG = "org_EXAMPLE";
const PROJECT = "proj_EXAMPLE";
const ROOT = `/org/${ORG}/project/${PROJECT}`;
const VIEW = "sv_01EXAMPLE0000000000000000";
const VERSION = "svv_01EXAMPLE0000000000000000";
const CONCEPT = "mdm_01EXAMPLE0000000000000000";

function ok(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) } as Response;
}

function fail(status: number, code: string): Response {
  return { ok: false, status, json: async () => ({ code, message: "refused" }), text: async () => code } as Response;
}

function facet(state: string, refs: unknown[] = []) {
  return { state, count: refs.length, refs, truncated: false };
}

function bindingRow(overrides: Record<string, unknown> = {}) {
  return {
    concept_id: CONCEPT,
    concept_version_id: null,
    concept_name: "revenue",
    datastream_id: "ds_1",
    datastream_name: "Meta Ads daily",
    mapping_version_id: "dmv_ACTIVE",
    mapping_version_number: 4,
    source_field_id: "revenue_micros",
    source_field_path: "report.revenue_micros",
    state: "active",
    confidence: 0.92,
    publication_ref: null,
    fingerprints: {},
    freshness: {},
    blocking_refs: [],
    provenance: {},
    owner_href: {
      surface: "project",
      workspace: "data",
      section: "datastreams",
      object_type: "datastream",
      object_id: "ds_1",
      tab: "mapping",
    },
    ...overrides,
  };
}

function sourceBindings(overrides: Record<string, unknown> = {}) {
  return {
    state: "available",
    rows: [bindingRow()],
    coverage: {
      bound: 1,
      eligible: 2,
      unmapped_datastreams: 0,
      by_state: { active: 1 },
      returned: 1,
      total: 1,
      truncated: false,
    },
    unavailable_reason: null,
    ...overrides,
  };
}

function conceptDetail(summary: Record<string, unknown> = {}) {
  return {
    object_ref: {
      type: "semantic-concept",
      id: CONCEPT,
      label: "Revenue",
      owner_href: { surface: "project", workspace: "governance", section: "semantic-model" },
    },
    scope: "project",
    owner: { kind: "semantic-model", project_id: PROJECT },
    lifecycle_status: "published",
    active_version_ref: { object_type: "semantic-concept-version", id: "scv_1", state: "published" },
    selected_version_ref: null,
    available_tabs: ["definition", "semantics", "source-bindings", "used-by", "versions"],
    default_tab: "definition",
    allowed_actions: [],
    used_by: facet("empty"),
    versions: facet("available", [
      { object_type: "semantic-concept-version", id: "scv_1", state: "published", recorded_at: "2026-07-30T08:00:00Z" },
    ]),
    evidence: facet("empty"),
    summary: {
      concept_kind: "metric",
      value_type: "money",
      aggregation: "sum",
      additivity_class: "additive",
      non_additive_dimensions: [],
      expression: { op: "source_measure", concept: "revenue" },
      unspecified: [],
      source_bindings: sourceBindings(),
      ...summary,
    },
    evidence_as_of: "2026-07-30T08:00:00Z",
  };
}

function viewDetail(summary: Record<string, unknown> = {}, overrides: Record<string, unknown> = {}) {
  return {
    object_ref: {
      type: "semantic-view",
      id: VIEW,
      label: "Marketing core",
      owner_href: { surface: "project", workspace: "governance", section: "semantic-model" },
    },
    scope: "project",
    owner: { kind: "semantic-model", project_id: PROJECT },
    lifecycle_status: "published",
    active_version_ref: { object_type: "semantic-view-version", id: VERSION, state: "published" },
    selected_version_ref: null,
    available_tabs: ["definition", "metrics-dimensions", "source-bindings", "used-by", "versions"],
    default_tab: "definition",
    allowed_actions: ["explore-data"],
    used_by: facet("unavailable"),
    versions: facet("available", [
      { object_type: "semantic-view-version", id: VERSION, state: "published", recorded_at: "2026-07-30T08:00:00Z" },
    ]),
    evidence: facet("empty"),
    summary: {
      metric_count: 1,
      dimension_count: 2,
      queryable_pairs: 1,
      refused_pairs: 1,
      source_bindings: sourceBindings(),
      explore_handoff: { semantic_view_id: VIEW, semantic_view_version_id: VERSION },
      compiled_matrix: {
        compiler_version: "semantic-compiler.v1",
        metrics: [{ concept_id: "sc_rev", version_id: "scv_rev", label: "Revenue" }],
        dimensions: [
          { concept_id: "sc_country", version_id: "scv_country", label: "Country" },
          { concept_id: "sc_tag", version_id: "scv_tag", label: "Tag" },
        ],
        cells: [
          { metric_id: "sc_rev", metric_version_id: "scv_rev", dimension_id: "sc_country", dimension_version_id: "scv_country", queryable: true, join_path: [] },
          {
            metric_id: "sc_rev",
            metric_version_id: "scv_rev",
            dimension_id: "sc_tag",
            dimension_version_id: "scv_tag",
            queryable: false,
            refusal: { code: "fan_out_forbidden", message: "The measure would be counted once per matching row." },
          },
        ],
        summary: { metrics: 1, dimensions: 2, pairs: 2, accepted: 1, refused: 1 },
      },
      relationships: [],
      ...summary,
    },
    evidence_as_of: "2026-07-30T08:00:00Z",
    ...overrides,
  };
}

function objectEnvelope(type: string, detail: unknown, overrides: Record<string, unknown> = {}) {
  return {
    schema_version: "governance-object.v1",
    project_ref: { object_type: "project", id: PROJECT },
    organization_ref: { object_type: "organization", id: ORG },
    section: "semantic-model",
    object_type: type,
    generated_at: "2026-07-30T09:00:00Z",
    evidence_as_of: "2026-07-30T08:00:00Z",
    state: detail ? "available" : "unavailable",
    object: detail,
    unavailable_reasons: [],
    ...overrides,
  };
}

function serve(routes: Array<[RegExp, Response | (() => Response)]>) {
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string) => {
      for (const [pattern, answer] of routes) {
        if (pattern.test(url)) return Promise.resolve(typeof answer === "function" ? answer() : answer);
      }
      return Promise.resolve(fail(404, "not_found"));
    }),
  );
}

function open(address: string) {
  window.history.replaceState({}, "", address);
  return render(
    <RouterProvider>
      <ContentRouter />
    </RouterProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

// ---------------------------------------------------------------------------
// The query contract, before any screen
// ---------------------------------------------------------------------------

describe("the Explore query contract", () => {
  it("requires both Semantic View parameters together", () => {
    const half = validateSectionQuery("analyze", "explore", { semantic_view_id: VIEW });
    expect(half.query).toEqual({});
    expect(half.rejected[0].code).toBe("incomplete_group");

    const whole = validateSectionQuery("analyze", "explore", {
      semantic_view_id: VIEW,
      semantic_view_version_id: VERSION,
    });
    expect(whole.query).toEqual({ semantic_view_id: VIEW, semantic_view_version_id: VERSION });
    expect(whole.rejected).toEqual([]);
  });

  it("refuses `latest` outright rather than resolving it", () => {
    const result = validateSectionQuery("analyze", "explore", {
      semantic_view_id: VIEW,
      semantic_view_version_id: "latest",
    });
    expect(result.query).toEqual({});
    expect(result.rejected.map((entry) => entry.code)).toContain("forbidden_value");
  });

  it("drops a parameter no section declared instead of carrying it", () => {
    const result = validateSectionQuery("analyze", "reports", { semantic_view_id: VIEW });
    expect(result.query).toEqual({});
    expect(result.rejected[0].code).toBe("undeclared_parameter");
  });

  it("round-trips an exact pin through parse and build", () => {
    const address = `${ROOT}/analyze/explore?semantic_view_id=${VIEW}&semantic_view_version_id=${VERSION}`;
    const parsed = parsePath(`${ROOT}/analyze/explore`, `?semantic_view_id=${VIEW}&semantic_view_version_id=${VERSION}`);
    expect(parsed.kind).toBe("resolved");
    if (parsed.kind !== "resolved") return;
    expect(parsed.route.query).toEqual({ semantic_view_id: VIEW, semantic_view_version_id: VERSION });
    expect(buildPath(parsed.route)).toBe(address);
  });

  it("refuses to BUILD an address from query state the contract rejects", () => {
    const parsed = parsePath(`${ROOT}/analyze/explore`);
    expect(parsed.kind).toBe("resolved");
    if (parsed.kind !== "resolved") return;
    const broken = { ...parsed.route, query: { semantic_view_id: VIEW } } as CanonicalRoute;
    expect(() => buildPath(broken)).toThrow(/query state/i);
  });

  it("carries no query state into a section that declares none", () => {
    const parsed = parsePath(`${ROOT}/analyze/reports`, `?semantic_view_id=${VIEW}`);
    expect(parsed.kind).toBe("resolved");
    if (parsed.kind !== "resolved") return;
    expect(parsed.route.query).toEqual({});
    // 2026-08-01 : ce test s'appelle « carries no QUERY STATE », et c'est ce
    // qu'il doit garder. Son assertion epinglait en plus le chemin EXACT, donc
    // la lentille par defaut ajoutee a la section `reports` (`49cffe82`) le
    // faisait rougir pour une raison qui n'est pas la sienne -- meme classe
    // qu'AI-103 : le nom promet une chose, l'assertion en epingle une autre.
    // On asserte donc l'invariant annonce : aucun `?…` ne survit a l'aller-retour.
    // Le chemin de base reste verifie, la lentille eventuelle est laissee au
    // test qui la possede.
    const built = buildPath(parsed.route);
    expect(built).toContain(`${ROOT}/analyze/reports`);
    expect(built).not.toContain("?");
    expect(built).not.toContain(VIEW);
  });
});

// ---------------------------------------------------------------------------
// Source Bindings
// ---------------------------------------------------------------------------

describe("Source Bindings", () => {
  it("shows the active mapping version and links to its Data owner", async () => {
    serve([[/semantic-model\/objects\/semantic-concept/, ok(objectEnvelope("semantic-concept", conceptDetail()))]]);
    open(`${ROOT}/governance/semantic-model/object/semantic-concept/${CONCEPT}/tab/source-bindings`);
    expect(await screen.findByText("dmv_ACTIVE (v4)")).toBeInTheDocument();
    expect(screen.getByText("1 / 2")).toBeInTheDocument();
    const owner = screen.getByRole("link", { name: /Data · Mapping/ });
    expect(owner).toHaveAttribute("href", `${ROOT}/data/datastreams/object/datastream/ds_1/tab/mapping`);
  });

  it("shows Unavailable, never a zero, when the Data owner could not be read", async () => {
    serve([
      [
        /semantic-model\/objects\/semantic-concept/,
        ok(
          objectEnvelope(
            "semantic-concept",
            conceptDetail({
              source_bindings: sourceBindings({
                state: "unavailable",
                rows: [],
                coverage: {
                  bound: null,
                  eligible: null,
                  unmapped_datastreams: null,
                  by_state: {},
                  returned: 0,
                  total: 0,
                  truncated: false,
                },
                unavailable_reason: { code: "mapping_owner_unreadable", message: "This is not a coverage of zero." },
              }),
            }),
          ),
        ),
      ],
    ]);
    open(`${ROOT}/governance/semantic-model/object/semantic-concept/${CONCEPT}/tab/source-bindings`);
    expect(await screen.findByText("Unavailable")).toBeInTheDocument();
    expect(screen.queryByText("0 / 0")).not.toBeInTheDocument();
    // Said in both the headline hint and the block: neither place may show a number.
    expect(screen.getAllByText(/not a coverage of zero/i).length).toBeGreaterThan(0);
  });

  it("shows a genuinely empty answer differently from an unreadable one", async () => {
    serve([
      [
        /semantic-model\/objects\/semantic-concept/,
        ok(
          objectEnvelope(
            "semantic-concept",
            conceptDetail({
              source_bindings: sourceBindings({
                state: "empty",
                rows: [],
                coverage: {
                  bound: 0,
                  eligible: 3,
                  unmapped_datastreams: 0,
                  by_state: {},
                  returned: 0,
                  total: 0,
                  truncated: false,
                },
              }),
            }),
          ),
        ),
      ],
    ]);
    open(`${ROOT}/governance/semantic-model/object/semantic-concept/${CONCEPT}/tab/source-bindings`);
    expect(await screen.findByText("Nothing is bound yet")).toBeInTheDocument();
    expect(screen.getByText("0 / 3")).toBeInTheDocument();
  });

  // -------------------------------------------------------------------------
  // Story 53.7 — the fraction counts ONE population, and the population it
  // leaves out is on the screen next to it.
  // -------------------------------------------------------------------------

  it("never shows the fraction alone while Datastreams publish no mapping", async () => {
    // The exact shape the server composes for a concept workbench: one field
    // bound, eight Datastreams publishing no mapping version at all. Both sides
    // of the fraction count (Datastream, field) pairs, so those eight contribute
    // to neither and it reads 1 / 1 — a finished Project, to any reader. The
    // compensating count exists in the payload and reached no screen.
    serve([
      [
        /semantic-model\/objects\/semantic-concept/,
        ok(
          objectEnvelope(
            "semantic-concept",
            conceptDetail({
              source_bindings: sourceBindings({
                coverage: {
                  bound: 1,
                  eligible: 1,
                  unmapped_datastreams: 8,
                  by_state: { active: 1, unavailable: 8 },
                  returned: 9,
                  total: 9,
                  truncated: false,
                },
              }),
            }),
          ),
        ),
      ],
    ]);
    open(`${ROOT}/governance/semantic-model/object/semantic-concept/${CONCEPT}/tab/source-bindings`);
    expect(await screen.findByText("1 / 1")).toBeInTheDocument();
    // The number, named, beside it — not buried in a tooltip and not implied.
    expect(screen.getByText("Datastreams with no published mapping")).toBeInTheDocument();
    expect(screen.getByText("8")).toBeInTheDocument();
    // And the fraction stops claiming to describe the Project while they sit there.
    expect(screen.getByText(/not the coverage of the Project/i)).toBeInTheDocument();
  });

  it("lets a complete mapping read as complete, with nothing hedging it", async () => {
    serve([
      [
        /semantic-model\/objects\/semantic-concept/,
        ok(
          objectEnvelope(
            "semantic-concept",
            conceptDetail({
              source_bindings: sourceBindings({
                coverage: {
                  bound: 2,
                  eligible: 2,
                  unmapped_datastreams: 0,
                  by_state: { active: 2 },
                  returned: 2,
                  total: 2,
                  truncated: false,
                },
              }),
            }),
          ),
        ),
      ],
    ]);
    open(`${ROOT}/governance/semantic-model/object/semantic-concept/${CONCEPT}/tab/source-bindings`);
    expect(await screen.findByText("2 / 2")).toBeInTheDocument();
    expect(screen.queryByText("Datastreams with no published mapping")).not.toBeInTheDocument();
    expect(screen.queryByText(/not the coverage of the Project/i)).not.toBeInTheDocument();
  });

  it("reports an unread unmapped count as unknown, never as none", async () => {
    serve([
      [
        /semantic-model\/objects\/semantic-concept/,
        ok(
          objectEnvelope(
            "semantic-concept",
            conceptDetail({
              source_bindings: sourceBindings({
                coverage: {
                  bound: 1,
                  eligible: 1,
                  unmapped_datastreams: null,
                  by_state: { active: 1 },
                  returned: 1,
                  total: 1,
                  truncated: false,
                },
              }),
            }),
          ),
        ),
      ],
    ]);
    open(`${ROOT}/governance/semantic-model/object/semantic-concept/${CONCEPT}/tab/source-bindings`);
    expect(await screen.findByText("Unknown")).toBeInTheDocument();
    expect(screen.getByText(/not the coverage of the Project/i)).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Semantics
// ---------------------------------------------------------------------------

describe("Semantics", () => {
  it("renders the formula as an accessible list and names the aggregation", async () => {
    serve([[/semantic-model\/objects\/semantic-concept/, ok(objectEnvelope("semantic-concept", conceptDetail()))]]);
    open(`${ROOT}/governance/semantic-model/object/semantic-concept/${CONCEPT}/tab/semantics`);
    expect(await screen.findByText("Mapped source measure of revenue")).toBeInTheDocument();
    expect(screen.getByText("Sum")).toBeInTheDocument();
  });

  it("warns that a formula still referring to Concepts by name cannot be published", async () => {
    serve([
      [
        /semantic-model\/objects\/semantic-concept/,
        ok(
          objectEnvelope(
            "semantic-concept",
            conceptDetail({
              expression: {
                op: "ratio",
                zero_denominator: "null",
                numerator: { op: "concept_name", name: "clicks" },
                denominator: { op: "concept_name", name: "impressions" },
              },
            }),
          ),
        ),
      ],
    ]);
    open(`${ROOT}/governance/semantic-model/object/semantic-concept/${CONCEPT}/tab/semantics`);
    expect(await screen.findByText(/still refers to Concepts by name/i)).toBeInTheDocument();
    expect(screen.getByText("Unresolved reference by name: clicks")).toBeInTheDocument();
  });

  it("names the dimensions a metric must not be summed across", async () => {
    serve([
      [
        /semantic-model\/objects\/semantic-concept/,
        ok(
          objectEnvelope(
            "semantic-concept",
            conceptDetail({ non_additive_dimensions: ["country"], additivity_class: "semi_additive" }),
          ),
        ),
      ],
    ]);
    open(`${ROOT}/governance/semantic-model/object/semantic-concept/${CONCEPT}/tab/semantics`);
    expect(await screen.findByText(/must not be summed across every dimension/i)).toBeInTheDocument();
  });

  // -------------------------------------------------------------------------
  // Story 60.2 — three labels for three stored classes, and exact dependencies
  //
  // The epic plan names "Additive / Non-Additive Ratio / Non-Additive Snapshot".
  // Those are CASES; the schema stores three CLASSES, and this repository already
  // carries three additivity vocabularies. A fourth is the defect, so the screen
  // shows the three it stores, spelled for a person.
  // -------------------------------------------------------------------------

  it.each([
    ["additive", "Additive"],
    ["semi_additive", "Semi-additive"],
    ["non_additive", "Non-additive"],
  ])("shows the declared class %s as %s", async (stored, shown) => {
    serve([
      [
        /semantic-model\/objects\/semantic-concept/,
        ok(
          objectEnvelope(
            "semantic-concept",
            conceptDetail({
              additivity_class: stored,
              non_additive_dimensions: stored === "semi_additive" ? ["date"] : [],
            }),
          ),
        ),
      ],
    ]);
    open(`${ROOT}/governance/semantic-model/object/semantic-concept/${CONCEPT}/tab/semantics`);
    expect(await screen.findByText(shown)).toBeInTheDocument();
  });

  it("says Unavailable rather than inventing a fourth class when none is stored", async () => {
    serve([
      [
        /semantic-model\/objects\/semantic-concept/,
        ok(objectEnvelope("semantic-concept", conceptDetail({ additivity_class: null }))),
      ],
    ]);
    open(`${ROOT}/governance/semantic-model/object/semantic-concept/${CONCEPT}/tab/semantics`);
    expect(await screen.findByText("Unavailable")).toBeInTheDocument();
    expect(screen.queryByText("Additive")).not.toBeInTheDocument();
  });

  it("lists the EXACT Concept versions a formula pins", async () => {
    serve([
      [
        /semantic-model\/objects\/semantic-concept/,
        ok(
          objectEnvelope(
            "semantic-concept",
            conceptDetail({
              expression: {
                op: "ratio",
                zero_denominator: "null",
                numerator: { op: "concept_ref", concept_id: "sc_NUM", version_id: "scv_NUM" },
                denominator: { op: "concept_ref", concept_id: "sc_DEN", version_id: "scv_DEN" },
              },
            }),
          ),
        ),
      ],
    ]);
    open(`${ROOT}/governance/semantic-model/object/semantic-concept/${CONCEPT}/tab/semantics`);
    expect(await screen.findByText("Depends on")).toBeInTheDocument();
    // Both ids, always: a reference without a version follows `latest` and is
    // not a reference.
    expect(screen.getAllByText("scv_NUM").length).toBeGreaterThan(0);
    expect(screen.getAllByText("scv_DEN").length).toBeGreaterThan(0);
    expect(screen.getAllByText("sc_NUM").length).toBeGreaterThan(0);
  });

  // -------------------------------------------------------------------------
  // A pinned dependency OPENS, and the address is built by the router.
  //
  // The two columns were raw `ObjectId` values: "what does this metric depend
  // on, at which version" is the question asked before an edit, and the answer
  // was an identity to copy out and paste into a search. Concatenating the
  // address instead would be a second grammar nothing validates — the defect
  // `ownerPath` exists to prevent — so the proof is that what comes out of the
  // screen goes back through `parsePath` and resolves.
  // -------------------------------------------------------------------------

  it("opens each pinned dependency through an address the router built", async () => {
    serve([
      [
        /semantic-model\/objects\/semantic-concept/,
        ok(
          objectEnvelope(
            "semantic-concept",
            conceptDetail({
              expression: {
                op: "ratio",
                zero_denominator: "null",
                numerator: { op: "concept_ref", concept_id: "sc_NUM", version_id: "scv_NUM" },
                denominator: { op: "concept_ref", concept_id: "sc_DEN", version_id: "scv_DEN" },
              },
            }),
          ),
        ),
      ],
    ]);
    open(`${ROOT}/governance/semantic-model/object/semantic-concept/${CONCEPT}/tab/semantics`);
    await screen.findByText("Depends on");

    const concept = screen.getByRole("link", { name: "sc_NUM" });
    expect(concept).toHaveAttribute(
      "href",
      `${ROOT}/governance/semantic-model/object/semantic-concept/sc_NUM/tab/definition`,
    );

    // The VERSION column addresses the exact pin, and it can only hang from the
    // versions tab — the one tab this contract declares as version-bearing.
    const version = screen.getByRole("link", { name: "scv_NUM" });
    expect(version).toHaveAttribute(
      "href",
      `${ROOT}/governance/semantic-model/object/semantic-concept/sc_NUM/tab/versions/version/scv_NUM`,
    );

    // Built by the router means parseable by the router. A hand-assembled
    // address can be well-formed and still land on the unknown-route screen.
    const parsedConcept = parsePath(concept.getAttribute("href") as string);
    expect(parsedConcept.kind).toBe("resolved");
    const parsedVersion = parsePath(version.getAttribute("href") as string);
    expect(parsedVersion.kind).toBe("resolved");
    if (parsedVersion.kind !== "resolved") return;
    expect(parsedVersion.route.objectId).toBe("sc_NUM");
    expect(parsedVersion.route.versionId).toBe("scv_NUM");
  });

  it("renders the depth of a nested formula as indentation, not as text", async () => {
    // It used to prepend `" ".repeat(depth)` to the role: characters, not
    // layout. Nothing aligned to them, a screen reader read them out, and they
    // vanished the moment the label wrapped.
    serve([
      [
        /semantic-model\/objects\/semantic-concept/,
        ok(
          objectEnvelope(
            "semantic-concept",
            conceptDetail({
              expression: {
                op: "ratio",
                zero_denominator: "null",
                numerator: { op: "concept_ref", concept_id: "sc_NUM", version_id: "scv_NUM" },
                denominator: { op: "concept_ref", concept_id: "sc_DEN", version_id: "scv_DEN" },
              },
            }),
          ),
        ),
      ],
    ]);
    open(`${ROOT}/governance/semantic-model/object/semantic-concept/${CONCEPT}/tab/semantics`);
    // Scoped to the formula: the "Depends on" table below names the same roles.
    const formula = within(
      await screen.findByRole("region", { name: "Formula dependency list" }),
    );
    const root = formula.getByText("Result").closest("td") as HTMLElement;
    const numerator = formula.getByText("Numerator").closest("td") as HTMLElement;

    expect(root.dataset.depth).toBe("0");
    expect(numerator.dataset.depth).toBe("1");
    // The depth reaches CSS as a measurement, and the padding is derived from it
    // through the spacing scale — no literal spacing, no class per level.
    expect(root.style.getPropertyValue("--formula-depth")).toBe("0");
    expect(numerator.style.getPropertyValue("--formula-depth")).toBe("1");
    expect(numerator.className).toContain("ps-[calc(var(--formula-depth)*var(--spacing)*4)]");
    // And the role is the only thing left in the cell's text.
    expect(numerator.textContent).toBe("Numerator");
  });

  it("says a formula pins nothing rather than showing an empty table", async () => {
    serve([[/semantic-model\/objects\/semantic-concept/, ok(objectEnvelope("semantic-concept", conceptDetail()))]]);
    open(`${ROOT}/governance/semantic-model/object/semantic-concept/${CONCEPT}/tab/semantics`);
    expect(await screen.findByText("This formula pins no other Concept")).toBeInTheDocument();
  });

  it("shows the zero-denominator policy a ratio declared", async () => {
    serve([
      [
        /semantic-model\/objects\/semantic-concept/,
        ok(
          objectEnvelope(
            "semantic-concept",
            conceptDetail({
              expression: {
                op: "ratio",
                zero_denominator: "error",
                numerator: { op: "concept_ref", concept_id: "sc_NUM", version_id: "scv_NUM" },
                denominator: { op: "concept_ref", concept_id: "sc_DEN", version_id: "scv_DEN" },
              },
            }),
          ),
        ),
      ],
    ]);
    open(`${ROOT}/governance/semantic-model/object/semantic-concept/${CONCEPT}/tab/semantics`);
    expect(await screen.findByText(/zero denominator becomes error/i)).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Metrics & Dimensions
// ---------------------------------------------------------------------------

describe("Metrics and Dimensions", () => {
  it("shows accepted and refused pairs separately, with the refusal reason", async () => {
    serve([[/semantic-model\/objects\/semantic-view/, ok(objectEnvelope("semantic-view", viewDetail()))]]);
    open(`${ROOT}/governance/semantic-model/object/semantic-view/${VIEW}/tab/metrics-dimensions`);
    expect(await screen.findByText("Same dataset")).toBeInTheDocument();
    // The refusal is named, not just "not queryable".
    expect(screen.getByText("Fan out forbidden")).toBeInTheDocument();
  });

  it("shows the exact common-key version pinned by each relationship", async () => {
    serve([[
      /semantic-model\/objects\/semantic-view/,
      ok(objectEnvelope("semantic-view", viewDetail({
        relationships: [{
          name: "spend_to_conversions",
          from_dataset: "campaign_spend",
          to_dataset: "conversions",
          from_columns: ["day", "campaign_id"],
          to_columns: ["day", "campaign_id"],
          cardinality_type: "many_to_one",
          fan_out_policy: "forbid",
          bridge_dataset: null,
          mdm_common_key_version_id: "mckv_CAMPAIGN_DAY_3",
        }],
      }))),
    ]]);
    open(`${ROOT}/governance/semantic-model/object/semantic-view/${VIEW}/tab/metrics-dimensions`);

    const relationships = within(await screen.findByRole("region", {
      name: "Declared relationships",
    }));
    expect(relationships.getByText("mckv_CAMPAIGN_DAY_3")).toBeInTheDocument();
    expect(relationships.getByText("Many to one")).toBeInTheDocument();
    expect(relationships.getByText("Forbid")).toBeInTheDocument();
  });

  it("says a version was not compiled rather than showing an empty matrix", async () => {
    serve([
      [
        /semantic-model\/objects\/semantic-view/,
        ok(objectEnvelope("semantic-view", viewDetail({ compiled_matrix: {} }))),
      ],
    ]);
    open(`${ROOT}/governance/semantic-model/object/semantic-view/${VIEW}/tab/metrics-dimensions`);
    expect(await screen.findByText("This version has not been compiled")).toBeInTheDocument();
  });

  it("distinguishes an unreadable compiler output from an uncompiled version", async () => {
    serve([
      [
        /semantic-model\/objects\/semantic-view/,
        ok(objectEnvelope("semantic-view", viewDetail({ compiled_matrix: null }))),
      ],
    ]);
    open(`${ROOT}/governance/semantic-model/object/semantic-view/${VIEW}/tab/metrics-dimensions`);
    expect(await screen.findByText("The compiler output could not be read")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// The handoff, end to end
// ---------------------------------------------------------------------------

describe("the Explore handoff", () => {
  it("opens Analyze on the exact pinned version and puts it in the address", async () => {
    serve([
      [/semantic-model\/objects\/semantic-view/, ok(objectEnvelope("semantic-view", viewDetail()))],
    ]);
    open(`${ROOT}/governance/semantic-model/object/semantic-view/${VIEW}/tab/definition`);
    const button = await screen.findByTestId("explore-data");
    await userEvent.click(button);
    await waitFor(() => {
      expect(window.location.pathname).toBe(`${ROOT}/analyze/explore`);
    });
    expect(window.location.search).toContain(`semantic_view_id=${VIEW}`);
    expect(window.location.search).toContain(`semantic_view_version_id=${VERSION}`);
  });

  it("offers no Explore button when the server declared no such action", async () => {
    serve([
      [
        /semantic-model\/objects\/semantic-view/,
        ok(
          objectEnvelope(
            "semantic-view",
            viewDetail({ explore_handoff: null, queryable_pairs: 0 }, { allowed_actions: [] }),
          ),
        ),
      ],
    ]);
    open(`${ROOT}/governance/semantic-model/object/semantic-view/${VIEW}/tab/definition`);
    // The label appears in the breadcrumb, the heading and the object header.
    await screen.findAllByText("Marketing core");
    expect(screen.queryByTestId("explore-data")).not.toBeInTheDocument();
  });

  it("resolves the pinned version in Explore and says the pin is current", async () => {
    serve([
      [
        new RegExp(`semantic-view/${VIEW}/versions/${VERSION}`),
        ok(objectEnvelope("semantic-view", viewDetail(), { schema_version: "governance-version.v1", version_state: "current", requested_version_id: VERSION })),
      ],
    ]);
    open(`${ROOT}/analyze/explore?semantic_view_id=${VIEW}&semantic_view_version_id=${VERSION}`);
    const banner = await screen.findByTestId("explore-version-banner");
    expect(banner).toHaveTextContent(/is the published version/i);
  });

  it("keeps showing a superseded pin instead of swapping in the current version", async () => {
    serve([
      [
        new RegExp(`semantic-view/${VIEW}/versions/${VERSION}`),
        ok(objectEnvelope("semantic-view", viewDetail(), { schema_version: "governance-version.v1", version_state: "stale", requested_version_id: VERSION })),
      ],
    ]);
    open(`${ROOT}/analyze/explore?semantic_view_id=${VIEW}&semantic_view_version_id=${VERSION}`);
    const banner = await screen.findByTestId("explore-version-banner");
    expect(banner).toHaveTextContent(/no longer current/i);
    expect(banner).toHaveTextContent(/has not been substituted/i);
  });

  it("does not honour half a pin", async () => {
    serve([]);
    open(`${ROOT}/analyze/explore?semantic_view_id=${VIEW}`);
    expect(await screen.findByText("Sources you can cross")).toBeInTheDocument();
    expect(screen.queryByText("No Semantic View is pinned")).not.toBeInTheDocument();
    await waitFor(() => {
      expect(window.location.search).toBe("");
    });
  });

  it("keeps the pin across a refresh of the same address", async () => {
    serve([
      [
        new RegExp(`semantic-view/${VIEW}/versions/${VERSION}`),
        ok(objectEnvelope("semantic-view", viewDetail(), { schema_version: "governance-version.v1", version_state: "current", requested_version_id: VERSION })),
      ],
    ]);
    const address = `${ROOT}/analyze/explore?semantic_view_id=${VIEW}&semantic_view_version_id=${VERSION}`;
    const first = open(address);
    await screen.findByTestId("explore-version-banner");
    first.unmount();
    // A refresh is a fresh mount on the same browser address.
    open(address);
    expect(await screen.findByTestId("explore-version-banner")).toBeInTheDocument();
    expect(window.location.search).toContain(VERSION);
  });

  it("offers the query door but executes nothing until the user asks", async () => {
    // Story 50.1 Task 6 replaced the "Querying is not built yet" placeholder with
    // a real door. The assertion that survives the change is the one that still
    // matters: MOUNTING a screen must never run a query. An address that executes
    // on arrival would create an immutable Result — and a cost — for anyone who
    // merely opened a shared link.
    serve([
      [
        new RegExp(`semantic-view/${VIEW}/versions/${VERSION}`),
        ok(objectEnvelope("semantic-view", viewDetail(), { schema_version: "governance-version.v1", version_state: "current", requested_version_id: VERSION })),
      ],
      [
        // Story 50.2 widened the option read from `query-options` to
        // `query-facets`: the same compiled artifact, composed with the governed
        // controls the request also needs. The property under test is unchanged.
        /analyze\/query-facets/,
        ok({
          schema_version: "analyze-query-facets.v1",
          project_id: PROJECT,
          semantic_view_id: VIEW,
          semantic_view_version_id: VERSION,
          semantic_view_label: "Search performance",
          semantic_view_version_number: 1,
          status: "published",
          executable: true,
          measures: [{ concept_id: "sc_clicks", version_id: "scv_clicks", label: "Clicks", owner_ref: null }],
          dimensions: [{ concept_id: "sc_date", version_id: "scv_date", label: "Date", owner_ref: null }],
          pairs: [{ measure_id: "sc_clicks", dimension_id: "sc_date", queryable: true }],
          time: {
            members: [{ concept_id: "sc_date", version_id: "scv_date", label: "Date", allowed_grains: ["day"] }],
            grains: ["day"],
            grains_unavailable_reason: null,
            comparisons: ["none", "previous_period", "previous_year"],
            as_of_supported: true,
            reporting_boundary_supported: true,
          },
          sort: { directions: ["asc", "desc"] },
          filter_operators: ["eq", "in"],
          limits: {
            default_row_limit: 10000,
            max_row_limit: 100000,
            max_measures: 50,
            max_dimensions: 20,
            max_filters: 100,
          },
          classification_facets: [],
          classification_facets_unavailable: [],
          unavailable_reasons: [],
          owner_ref: null,
        }),
      ],
    ]);
    open(`${ROOT}/analyze/explore?semantic_view_id=${VIEW}&semantic_view_version_id=${VERSION}`);
    await screen.findByTestId("explore-version-banner");
    expect(await screen.findByTestId("explore-query-door")).toBeInTheDocument();
    // Every call made so far is a READ; nothing posts without an explicit action.
    const calls = (globalThis.fetch as unknown as { mock: { calls: unknown[][] } }).mock.calls;
    for (const [, init] of calls) {
      const method = (init as RequestInit | undefined)?.method ?? "GET";
      expect(method).toBe("GET");
    }
  });
});

describe("Metrics and Dimensions — search and the refused list", () => {
  it("narrowing shows its own counts and never moves the server's totals", async () => {
    serve([[/semantic-model\/objects\/semantic-view/, ok(objectEnvelope("semantic-view", viewDetail()))]]);
    open(`${ROOT}/governance/semantic-model/object/semantic-view/${VIEW}/tab/metrics-dimensions`);
    await screen.findByText("Same dataset");

    fireEvent.change(screen.getByRole("textbox", { name: "Search metrics and dimensions" }), {
      target: { value: "zzz-nothing" },
    });
    // A second line counts what is SHOWN; the headline totals stay the server's.
    expect(screen.getByTestId("matrix-filter-count")).toHaveTextContent(/Showing 0 of/);
  });

  it("lists every refused pair with its reason, reachable without a pointer hover", async () => {
    serve([[/semantic-model\/objects\/semantic-view/, ok(objectEnvelope("semantic-view", viewDetail()))]]);
    open(`${ROOT}/governance/semantic-model/object/semantic-view/${VIEW}/tab/metrics-dimensions`);
    await screen.findByText("Same dataset");

    fireEvent.click(screen.getByTestId("refused-pairs-trigger"));
    const list = screen.getByTestId("refused-pairs-list");
    // The same fact the tooltip holds, now readable on touch.
    expect(within(list).getByText(/Fan out forbidden/)).toBeInTheDocument();
  });
});
