import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {
  buildPath,
  parsePath,
  RouterProvider,
  useRoute,
  type CanonicalRoute,
} from "../shell/router";
import { WORKSPACES, defaultLens, defaultObjectTab } from "../shell/navigation";

const organizationId = "org / opaque";
const projectId = "proj/opaque ?";

function canonical(overrides: Partial<CanonicalRoute> = {}): CanonicalRoute {
  return {
    scope: "project",
    organizationId,
    projectId,
    workspace: "data",
    section: "datastreams",
    lens: null,
    objectType: null,
    objectId: null,
    tab: null,
    versionId: null,
    // Story 50.2: the Result lens evidence tail. Every parsed route carries it,
    // null when the address names no exact datum.
    evidenceId: null,
    action: null,
    query: {},
    globalSurface: null,
    globalSection: null,
    ...overrides,
  } as CanonicalRoute;
}

describe("canonical route registry", () => {
  it("contains exactly the ratified six workspaces and section counts", () => {
    expect(WORKSPACES.map(({ key, label }) => [key, label])).toEqual([
      ["overview", "Overview"],
      ["analyze", "Analyze"],
      ["test", "Test"],
      ["data", "Data"],
      ["governance", "Governance"],
      ["context-hub", "Context Hub"],
    ]);
    expect(WORKSPACES.map((workspace) => workspace.subnav.length)).toEqual([1, 4, 3, 6, 4, 3]);
    expect(WORKSPACES.flatMap((workspace) => workspace.subnav.map((item) => item.label))).not.toEqual(
      expect.arrayContaining(["Widgets", "AI Paths", "Recovery"]),
    );
  });

  it("round-trips every registered collection, on its declared default lens", () => {
    for (const workspace of WORKSPACES) {
      for (const section of workspace.subnav) {
        const lens = defaultLens(workspace.key, section.slug);
        const route = canonical({ workspace: workspace.key, section: section.slug, lens });
        const path = buildPath(route);
        expect(parsePath(path)).toEqual({ kind: "resolved", route });
      }
    }
  });
});

describe("canonical route grammar", () => {
  it.each(["ecase_EXACT", "edv_EXACT"])(
    "round-trips an Evaluation Run Cases evidence focus %s",
    (focusId) => {
      const route = canonical({
        workspace: "test",
        section: "regression-runs",
        objectType: "evaluation-run",
        objectId: "erun_EXACT",
        tab: "cases",
        versionId: focusId,
      });
      expect(parsePath(buildPath(route))).toEqual({ kind: "resolved", route });
    },
  );

  it("round-trips the organization-rooted Project Settings sections", () => {
    for (const globalSection of ["general", "capabilities", "changes"] as const) {
      const route = canonical({
        workspace: "overview",
        section: "project-overview",
        globalSurface: "project-settings",
        globalSection,
      });
      const path = buildPath(route);
      expect(path).toBe(
        `/org/${encodeURIComponent(organizationId)}/project/${encodeURIComponent(projectId)}/settings/${globalSection}`,
      );
      expect(parsePath(path)).toEqual({ kind: "resolved", route });
    }
  });

  it("resolves the bare Settings route to General and rejects unknown sections", () => {
    expect(parsePath("/org/o/project/p/settings")).toMatchObject({
      kind: "resolved",
      route: { globalSurface: "project-settings", globalSection: "general" },
    });
    expect(parsePath("/org/o/project/p/settings/not-real")).toMatchObject({
      kind: "unknown",
    });
  });
  it("round-trips opaque organization, project, object and tab identifiers", () => {
    const route = canonical({
      objectType: "datastream",
      objectId: "stream / opaque?",
      tab: "mapping",
    });
    const path = buildPath(route);

    expect(path).toBe(
      "/org/org%20%2F%20opaque/project/proj%2Fopaque%20%3F/data/datastreams/object/datastream/stream%20%2F%20opaque%3F/tab/mapping",
    );
    expect(parsePath(path)).toEqual({ kind: "resolved", route });
  });

  it("round-trips an opaque exact version, and only from the versions tab", () => {
    // `connector` declares `versions`; `datastream` does not. A version pinned
    // under a tab that contracts no history names a history that does not exist.
    const route = canonical({
      section: "connectors",
      objectType: "connector",
      objectId: "conn / opaque?",
      tab: "versions",
      versionId: "version / 7?",
    });
    const path = buildPath(route);

    expect(path).toBe(
      "/org/org%20%2F%20opaque/project/proj%2Fopaque%20%3F/data/connectors/object/connector/conn%20%2F%20opaque%3F/tab/versions/version/version%20%2F%207%3F",
    );
    expect(parsePath(path)).toEqual({ kind: "resolved", route });
    expect(() =>
      buildPath(canonical({ objectType: "datastream", objectId: "ds", tab: "mapping", versionId: "v1" })),
    ).toThrow();
  });

  it.each([
    "/not-a-route",
    "/org/o/project/p/data/not-real",
    "/org/o/project/p/data/datastreams/trailing",
    "/org/o/project/p/data/datastreams/object/datastream/ds/tab/not-real",
    "/org/o/project/p/data/datastreams/object/datastream/ds/action/not-real",
    // A version may only hang from `versions`, and only where the contract has one.
    "/org/o/project/p/data/datastreams/object/datastream/ds/tab/mapping/version/v1",
    "/org/o/project/p/data/connectors/object/connector/c/tab/overview/version/v1",
    // A lens belongs to the collection that declares it, and to no other.
    "/org/o/project/p/governance/master-data/lens/not-real",
    "/org/o/project/p/governance/master-data/lens/concepts",
    "/org/o/project/p/data/datastreams/lens/business-domains",
  ])("returns unknown without a plausible fallback for %s", (path) => {
    expect(parsePath(path)).toMatchObject({ kind: "unknown", pathname: path });
  });
});

describe("Governance route contracts (Story 49.1)", () => {
  const governance = WORKSPACES.find((workspace) => workspace.key === "governance")!;

  it("declares exactly the four permanent screens, in order", () => {
    expect(governance.subnav.map((section) => [section.slug, section.label])).toEqual([
      ["master-data", "Master Data"],
      ["semantic-model", "Semantic Model"],
      ["controls-quality", "Controls & Quality"],
      ["evidence", "Evidence"],
    ]);
  });

  it("declares exactly the twenty registered lenses and round-trips every one", () => {
    const registered = governance.subnav.flatMap((section) =>
      (section.lenses ?? []).map((lens) => `${section.slug}/${lens.slug}`),
    );
    expect(registered).toEqual([
      "master-data/business-domains",
      "master-data/classifications",
      "master-data/products",
      "master-data/activities",
      "master-data/registries",
      // Story 48.5. Conditional in CONTENT, permanent in the ROUTE registry:
      // a lens that appeared and disappeared would make a shared address break
      // depending on who opens it.
      "master-data/competitor-registry",
      "semantic-model/concepts",
      "semantic-model/semantic-views",
      "semantic-model/mapping-coverage",
      // Stories 60.1 and 60.3. This list is the reason they are here: it is the
      // ONLY place that fails when a lens is registered in `navigation.ts` and
      // nowhere else, and it went red for two commits because neither story's
      // test plan named this file.
      "semantic-model/value-tables",
      "semantic-model/cleanup-rules",
      // Lot A1, issue #68. The vocabulary six modules validate bindings against
      // had no lens and no screen, so no address could point at it.
      "semantic-model/canonical-fields",
      // Commit d98c2a4b (2026-08-16). `app.metric_definitions` decides whether a
      // metric may be summed across two days, and no lens listed it. Its own
      // lens because a lens carries ONE object type. This contract went red for
      // the third time here because that commit's test plan named the server
      // read model and `tsc`, and not this file.
      "semantic-model/metric-definitions",
      "controls-quality/conflicts",
      "controls-quality/reconciliation",
      "controls-quality/data-quality",
      "controls-quality/rule-sets",
      "evidence/lineage-provenance",
      "evidence/versions-approvals",
      "evidence/audit-activity",
    ]);
    expect(registered).toHaveLength(20);

    for (const section of governance.subnav) {
      for (const lens of section.lenses ?? []) {
        const route = canonical({ workspace: "governance", section: section.slug, lens: lens.slug });
        expect(parsePath(buildPath(route))).toEqual({ kind: "resolved", route });
      }
    }
  });

  it("canonicalizes a bare collection address to the declared default lens", () => {
    for (const section of governance.subnav) {
      const expected = section.lenses![0].slug;
      const result = parsePath(`/org/o/project/p/governance/${section.slug}`);
      expect(result).toMatchObject({ kind: "resolved", route: { lens: expected } });
      // The canonical form the provider replaces the address with.
      expect(buildPath((result as { route: CanonicalRoute }).route)).toBe(
        `/org/o/project/p/governance/${section.slug}/lens/${expected}`,
      );
    }
  });

  it("registers sixteen object types with their exact tabs and declared default", () => {
    const contracts = governance.subnav.flatMap((section) =>
      section.objects.map((object) => [section.slug, object.type, object.defaultTab, object.tabs]),
    );
    expect(contracts).toHaveLength(16);
    expect(contracts).toEqual([
      ["master-data", "business-domain", "overview", ["overview", "hierarchy", "mappings-aliases", "used-by", "versions"]],
      ["master-data", "master-data-object", "overview", ["overview", "hierarchy", "mappings-aliases", "used-by", "versions"]],
      ["master-data", "registry", "overview", ["overview", "hierarchy", "mappings-aliases", "used-by", "versions"]],
      ["master-data", "tracked-entity", "overview", ["overview", "representations", "coverage", "used-by", "versions"]],
      ["semantic-model", "semantic-concept", "definition", ["definition", "semantics", "source-bindings", "used-by", "versions"]],
      ["semantic-model", "semantic-view", "definition", ["definition", "metrics-dimensions", "source-bindings", "used-by", "versions"]],
      // Three tabs since story 60.5: migration 242 gave both families an
      // immutable ledger, so the `versions` tab now opens on rows that exist.
      ["semantic-model", "value-mapping-table", "overview", ["overview", "used-by", "versions"]],
      ["semantic-model", "cleanup-rule", "overview", ["overview", "used-by", "versions"]],
      // Canonical fields expose their definition and the independently tested
      // source-to-canonical lineage. They still have no version ledger.
      ["semantic-model", "canonical-field", "definition", ["definition", "lineage"]],
      // Commit d98c2a4b. ONE tab for the same reason: an upsert store with an
      // audit table beside it has no version ledger, and nothing records which
      // renders read a given definition, so `versions` and `used-by` would both
      // promise an answer nothing writes.
      ["semantic-model", "metric-definition", "definition", ["definition"]],
      ["controls-quality", "control-case", "evidence",["evidence", "candidate-change", "impact", "decision-history"]],
      ["controls-quality", "dq-monitor", "overview", ["overview", "coverage", "history", "issues"]],
      ["controls-quality", "rule-set", "overview", ["overview", "rules", "effective-dates", "approvals-exceptions", "versions"]],
      ["evidence", "evidence-trace", "overview", ["overview", "lineage", "provenance"]],
      ["evidence", "object-version", "overview", ["overview", "diff", "approvals", "used-by"]],
      ["evidence", "audit-event", "overview", ["overview"]],
    ]);
  });

  it("round-trips every registered tab of every registered object type", () => {
    for (const section of governance.subnav) {
      for (const object of section.objects) {
        for (const tab of object.tabs ?? []) {
          const route = canonical({
            workspace: "governance",
            section: section.slug,
            objectType: object.type,
            objectId: "obj / opaque?",
            tab,
          });
          expect(parsePath(buildPath(route))).toEqual({ kind: "resolved", route });
        }
      }
    }
  });

  it("canonicalizes a bare object address to the contract's default tab", () => {
    for (const section of governance.subnav) {
      for (const object of section.objects) {
        const result = parsePath(
          `/org/o/project/p/governance/${section.slug}/object/${object.type}/obj_1`,
        );
        expect(result).toMatchObject({
          kind: "resolved",
          route: { tab: defaultObjectTab("governance", section.slug, object.type) },
        });
      }
    }
  });

  it("accepts an exact version only where the contract declares a versions tab", () => {
    const versioned = governance.subnav.flatMap((section) =>
      section.objects
        .filter((object) => object.tabs?.includes("versions"))
        .map((object) => [section.slug, object.type] as const),
    );
    expect(versioned.map(([, type]) => type)).toEqual([
      "business-domain",
      "master-data-object",
      "registry",
      "tracked-entity",
      "semantic-concept",
      "semantic-view",
      // Story 60.5: both families of transformation rules got an immutable
      // ledger (migration 242), so both now accept an exact version.
      "value-mapping-table",
      "cleanup-rule",
      "rule-set",
    ]);

    for (const [section, type] of versioned) {
      const path = `/org/o/project/p/governance/${section}/object/${type}/obj_1/tab/versions/version/ver_1`;
      expect(parsePath(path)).toMatchObject({
        kind: "resolved",
        route: { section, objectType: type, tab: "versions", versionId: "ver_1" },
      });
    }

    // `control-case`, `dq-monitor`, `evidence-trace`, `object-version` and
    // `audit-event` declare no `versions` tab; pinning one is Unknown.
    for (const [section, type] of [
      ["controls-quality", "control-case"],
      ["controls-quality", "dq-monitor"],
      ["evidence", "evidence-trace"],
      ["evidence", "object-version"],
      ["evidence", "audit-event"],
    ] as const) {
      const path = `/org/o/project/p/governance/${section}/object/${type}/obj_1/tab/versions/version/ver_1`;
      expect(parsePath(path)).toMatchObject({ kind: "unknown" });
    }
  });

  it("carries the lens across a Project switch and drops it when an object opens", () => {
    // A lens is a property of the collection, not of the Project: switching
    // Project keeps the person on the same lens of the same screen.
    const start = canonical({ workspace: "governance", section: "evidence", lens: "audit-activity" });
    window.history.replaceState({}, "", buildPath(start));
    let seen = "";
    function Probe() {
      const { result, navigate } = useRoute();
      seen = result.kind === "resolved" ? buildPath(result.route) : result.kind;
      return (
        <div>
          <button type="button" onClick={() => navigate({ organizationId: "org_2", projectId: "project_2" })}>Switch</button>
          <button type="button" onClick={() => navigate({ objectType: "audit-event", objectId: "audit_1" })}>Open object</button>
        </div>
      );
    }
    const view = render(<RouterProvider><Probe /></RouterProvider>);
    expect(seen).toBe(buildPath(start));

    act(() => { view.container.querySelectorAll("button")[0].click(); });
    expect(window.location.pathname).toBe("/org/org_2/project/project_2/governance/evidence/lens/audit-activity");

    act(() => { view.container.querySelectorAll("button")[1].click(); });
    expect(window.location.pathname).toBe(
      "/org/org_2/project/project_2/governance/evidence/object/audit-event/audit_1/tab/overview",
    );
    view.unmount();
  });

  it("makes the Epic 48 pinned owner reference a resolvable address", () => {
    // `governance_owner_reference(capability_key, object_type=…, version_id=…)`
    // emits section + object + tab="versions" + version. Before this story the
    // router rejected every one of them.
    for (const [section, type] of [
      ["master-data", "registry"],
      ["controls-quality", "rule-set"],
    ] as const) {
      const route = canonical({
        workspace: "governance",
        section,
        objectType: type,
        objectId: "cap_object_1",
        tab: "versions",
        versionId: "pcv_1",
      });
      const path = buildPath(route);
      expect(path).toBe(
        `/org/${encodeURIComponent(organizationId)}/project/${encodeURIComponent(projectId)}/governance/${section}/object/${type}/cap_object_1/tab/versions/version/pcv_1`,
      );
      expect(parsePath(path)).toEqual({ kind: "resolved", route });
    }
  });
});

function HistoryProbe() {
  const { result, navigate } = useRoute();
  return (
    <div>
      <output data-testid="route">{result.kind === "resolved" ? buildPath(result.route) : result.kind}</output>
      <button
        type="button"
        onClick={() =>
          navigate({
            organizationId: "org_2",
            projectId: "project_2",
            workspace: "overview",
            section: "project-overview",
            objectType: null,
            objectId: null,
            tab: null,
            versionId: null,
            action: null,
          })
        }
      >
        Switch project
      </button>
    </div>
  );
}

describe("native history transport", () => {
  it("preserves deep links across back/forward and intentionally clears object state on project switch", async () => {
    const user = userEvent.setup();
    const first = buildPath(canonical({ objectType: "datastream", objectId: "ds_1", tab: "mapping" }));
    window.history.replaceState({}, "", first);
    render(
      <RouterProvider>
        <HistoryProbe />
      </RouterProvider>,
    );

    await user.click(screen.getByRole("button", { name: "Switch project" }));
    expect(window.location.pathname).toBe("/org/org_2/project/project_2/overview/project-overview");

    act(() => {
      window.history.replaceState({}, "", first);
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    expect(screen.getByTestId("route")).toHaveTextContent(first);

    act(() => {
      window.history.replaceState({}, "", "/org/org_2/project/project_2/overview/project-overview");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    expect(screen.getByTestId("route")).toHaveTextContent(
      "/org/org_2/project/project_2/overview/project-overview",
    );
  });
});

/**
 * Story 50.4 AC12 -- the Visualization Builder is a ROUTE-STABLE workbench.
 *
 * These were absent: `grep -rn "visualization" src/__tests__/Router.test.tsx
 * src/__tests__/ContentRouter.test.tsx` returned nothing, so the object contract
 * registered in `navigation.ts` had no regression at all. A Builder whose address
 * silently stops round-tripping is a Builder whose "send me what you are looking
 * at" is a lie, and nothing would have said so.
 */
describe("Visualization Builder route contract (Story 50.4 AC12)", () => {
  const explore = WORKSPACES.find((workspace) => workspace.key === "analyze")!.subnav.find(
    (section) => section.slug === "explore",
  )!;
  const contract = explore.objects.find((object) => object.type === "visualization")!;

  it("declares build and versions, with build as the default tab", () => {
    expect(contract).toBeDefined();
    expect(contract.tabs).toEqual(["build", "versions"]);
    expect(defaultObjectTab("analyze", "explore", "visualization")).toBe("build");
  });

  it("round-trips both declared tabs", () => {
    for (const tab of contract.tabs ?? []) {
      const route = canonical({
        workspace: "analyze",
        section: "explore",
        objectType: "visualization",
        objectId: "vis / opaque?",
        tab,
      });
      expect(parsePath(buildPath(route))).toEqual({ kind: "resolved", route });
    }
  });

  it("canonicalizes a bare Builder address to build", () => {
    expect(
      parsePath("/org/o/project/p/analyze/explore/object/visualization/vis_1"),
    ).toMatchObject({ kind: "resolved", route: { objectType: "visualization", tab: "build" } });
  });

  it("keeps an unknown tab Unknown instead of falling back to build", () => {
    // A wrong address that renders the right screen is a wrong address nobody
    // can see. `preview` is not a declared tab, so it resolves to nothing.
    expect(
      parsePath("/org/o/project/p/analyze/explore/object/visualization/vis_1/tab/preview"),
    ).toMatchObject({ kind: "unknown" });
  });

  it("accepts an exact version tail on the declared version-bearing tabs", () => {
    for (const tab of contract.tabs ?? []) {
      expect(
        parsePath(
          `/org/o/project/p/analyze/explore/object/visualization/vis_1/tab/${tab}/version/vsv_1`,
        ),
      ).toMatchObject({
        kind: "resolved",
        route: { objectType: "visualization", tab, versionId: "vsv_1" },
      });
    }
  });

  it("carries the optional result_id pin through a round trip, and refuses `latest`", () => {
    const route = canonical({
      workspace: "analyze",
      section: "explore",
      objectType: "visualization",
      objectId: "vis_1",
      tab: "build",
      query: { result_id: "qr_1" },
    });
    const path = buildPath(route);
    expect(path).toContain("result_id=qr_1");
    expect(parsePath(path.split("?")[0], `?${path.split("?")[1]}`)).toEqual({
      kind: "resolved",
      route,
    });

    // `latest` follows whatever becomes current, which is not what anyone shares.
    // The contract forbids the value, so the parsed address carries NO pin rather
    // than a pin that means "whichever".
    const dropped = parsePath(
      "/org/o/project/p/analyze/explore/object/visualization/vis_1/tab/build",
      "?result_id=latest",
    );
    expect(dropped).toMatchObject({ kind: "resolved", route: { query: {} } });
  });

  it("clears the Visualization and its Result pin on a Project switch", () => {
    const start = canonical({
      workspace: "analyze",
      section: "explore",
      objectType: "visualization",
      objectId: "vis_1",
      tab: "build",
      query: { result_id: "qr_1" },
    });
    window.history.replaceState({}, "", buildPath(start));
    let seen = "";
    function Probe() {
      const { result, navigate } = useRoute();
      seen = result.kind === "resolved" ? buildPath(result.route) : result.kind;
      return (
        <button onClick={() => navigate({ organizationId: "org_2", projectId: "project_2" })}>
          Switch project
        </button>
      );
    }
    render(
      <RouterProvider>
        <Probe />
      </RouterProvider>,
    );
    act(() => {
      screen.getByRole("button", { name: "Switch project" }).click();
    });
    expect(seen).not.toContain("visualization");
    expect(seen).not.toContain("result_id");
  });
});
