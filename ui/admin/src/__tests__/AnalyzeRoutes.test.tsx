/**
 * Story 50.2 AC5/AC12 — the Analyze address grammar.
 *
 * The registry itself (`shell/navigation.ts`) is written by three sessions at
 * the time this landed, so these tests exercise the ROUTER against a synthetic
 * Analyze contract rather than against whatever the shared file happens to hold
 * this minute. What is proved here is the grammar the router owns:
 *
 *   - a version tail hangs only from a tab the contract declares version-bearing;
 *   - an evidence tail hangs only from a lens the contract declares
 *     evidence-bearing;
 *   - the two are never interchangeable, and a mismatch stays Unknown rather
 *     than being repaired by dropping the tail.
 *
 * The last point is the one that matters most. Dropping an unrecognised tail
 * would open a DIFFERENT object under the address someone shared — the Result
 * instead of the datum, the Query Spec head instead of the pinned version — and
 * it would do it silently.
 */
import { describe, expect, it, vi } from "vitest";

const ANALYZE_CONTRACTS: Record<string, unknown> = {
  "query-spec": {
    type: "query-spec",
    tabs: ["query"],
    defaultTab: "query",
    versionTabs: ["query"],
  },
  result: {
    type: "result",
    tabs: ["view", "data", "definitions", "quality", "provenance", "ai-path"],
    defaultTab: "view",
    evidenceTabs: ["view", "data", "definitions", "quality", "provenance", "ai-path"],
  },
};

vi.mock("../shell/navigation", async () => {
  const actual = await vi.importActual<typeof import("../shell/navigation")>(
    "../shell/navigation",
  );
  const forAnalyze = (workspace: string, section: string, objectType: string) =>
    workspace === "analyze" && section === "explore" ? ANALYZE_CONTRACTS[objectType] : undefined;
  return {
    ...actual,
    findObjectContract: (workspace: string, section: string, objectType: string) =>
      forAnalyze(workspace, section, objectType)
      ?? actual.findObjectContract(workspace, section, objectType),
    defaultObjectTab: (workspace: string, section: string, objectType: string) =>
      (forAnalyze(workspace, section, objectType) as { defaultTab?: string } | undefined)
        ?.defaultTab
      ?? actual.defaultObjectTab(workspace, section, objectType),
  };
});

const { buildPath, parsePath } = await import("../shell/router");

const ROOT = "/org/org_EXAMPLE/project/proj_EXAMPLE/analyze/explore";
const PIN = "?semantic_view_id=sv_EXAMPLE&semantic_view_version_id=svv_EXAMPLE";

function analyzeRoute(rest: Record<string, unknown> = {}) {
  return {
    scope: "project",
    organizationId: "org_EXAMPLE",
    projectId: "proj_EXAMPLE",
    workspace: "analyze",
    section: "explore",
    lens: null,
    objectType: null,
    objectId: null,
    tab: null,
    versionId: null,
    evidenceId: null,
    action: null,
    query: {},
    globalSurface: null,
    globalSection: null,
    ...rest,
  } as Parameters<typeof buildPath>[0];
}

describe("the Analyze workbench address grammar", () => {
  it("keeps the exact Semantic View pin on the collection address", () => {
    const result = parsePath(ROOT, PIN);
    expect(result.kind).toBe("resolved");
    if (result.kind !== "resolved") return;
    expect(result.route.query).toEqual({
      semantic_view_id: "sv_EXAMPLE",
      semantic_view_version_id: "svv_EXAMPLE",
    });
  });

  it("drops half a pin rather than guessing the version", () => {
    const result = parsePath(ROOT, "?semantic_view_id=sv_EXAMPLE");
    expect(result.kind).toBe("resolved");
    if (result.kind !== "resolved") return;
    // A Semantic View without its version follows whatever becomes current,
    // which is not what anyone shares.
    expect(result.route.query).toEqual({});
  });

  it("round-trips a Query Spec version under its only declared tab", () => {
    const path = `${ROOT}/object/query-spec/qs_EXAMPLE/tab/query/version/qsv_EXAMPLE`;
    const result = parsePath(path);
    expect(result.kind).toBe("resolved");
    if (result.kind !== "resolved") return;
    expect(result.route.tab).toBe("query");
    expect(result.route.versionId).toBe("qsv_EXAMPLE");
    expect(buildPath(result.route)).toBe(path);
  });

  it("canonicalizes a bare Query Spec address to its declared default tab", () => {
    const result = parsePath(`${ROOT}/object/query-spec/qs_EXAMPLE`);
    expect(result.kind).toBe("resolved");
    if (result.kind !== "resolved") return;
    expect(result.route.tab).toBe("query");
  });

  it("round-trips every declared Result lens", () => {
    for (const lens of ["view", "data", "definitions", "quality", "provenance", "ai-path"]) {
      const path = `${ROOT}/object/result/qr_EXAMPLE/tab/${lens}`;
      const result = parsePath(path);
      expect(result.kind).toBe("resolved");
      if (result.kind !== "resolved") return;
      expect(result.route.tab).toBe(lens);
      expect(buildPath(result.route)).toBe(path);
    }
  });

  it("canonicalizes a bare Result address to `view`", () => {
    const result = parsePath(`${ROOT}/object/result/qr_EXAMPLE`);
    expect(result.kind).toBe("resolved");
    if (result.kind !== "resolved") return;
    expect(result.route.tab).toBe("view");
  });

  it("round-trips an exact evidence target on a Result lens, pin included", () => {
    const path = `${ROOT}/object/result/qr_EXAMPLE/tab/data/evidence/qr_EXAMPLE%3A0%3Aclicks${PIN}`;
    const result = parsePath(
      `${ROOT}/object/result/qr_EXAMPLE/tab/data/evidence/qr_EXAMPLE%3A0%3Aclicks`,
      PIN,
    );
    expect(result.kind).toBe("resolved");
    if (result.kind !== "resolved") return;
    expect(result.route.evidenceId).toBe("qr_EXAMPLE:0:clicks");
    expect(buildPath(result.route)).toBe(path);
  });

  it.each([
    // A version tail on a Result lens: the Result IS the version, and a
    // `/version/` tail here would name a history the contract never declared.
    `${ROOT}/object/result/qr_EXAMPLE/tab/data/version/v1`,
    // An evidence tail on the Query tab: a Query Spec has no datum.
    `${ROOT}/object/query-spec/qs_EXAMPLE/tab/query/evidence/e1`,
    // A lens nobody declared.
    `${ROOT}/object/result/qr_EXAMPLE/tab/summary`,
    // A tab nobody declared on the Query Spec.
    `${ROOT}/object/query-spec/qs_EXAMPLE/tab/results`,
    // An object type Explore does not own.
    `${ROOT}/object/render/rnd_EXAMPLE/tab/view`,
  ])("keeps %s Unknown instead of repairing it", (path) => {
    expect(parsePath(path).kind).toBe("unknown");
  });

  it("refuses to BUILD an address the contract does not declare", () => {
    expect(() =>
      buildPath(analyzeRoute({ objectType: "result", objectId: "qr_1", tab: "data", versionId: "v1" })),
    ).toThrow(/versions tab/);
    expect(() =>
      buildPath(
        analyzeRoute({ objectType: "query-spec", objectId: "qs_1", tab: "query", evidenceId: "e1" }),
      ),
    ).toThrow(/evidence-bearing tab/);
    expect(() =>
      buildPath(
        analyzeRoute({
          objectType: "result",
          objectId: "qr_1",
          tab: "view",
          versionId: "v1",
          evidenceId: "e1",
        }),
      ),
    ).toThrow();
  });

  it("still hangs a Governance version from `versions` and nowhere else", () => {
    // The historical rule survives the generalization: a contract that declares
    // no `versionTabs` keeps `versions` as its only version-bearing tab.
    const path =
      "/org/o/project/p/governance/semantic-model/object/semantic-view/sv_1/tab/versions/version/svv_1";
    const result = parsePath(path);
    expect(result.kind).toBe("resolved");
    expect(
      parsePath(
        "/org/o/project/p/governance/semantic-model/object/semantic-view/sv_1/tab/definition/version/svv_1",
      ).kind,
    ).toBe("unknown");
  });
});

describe("the exact console targets Analyze produces", () => {
  it("Open in Toorow carries organization, Project, pin, Result, lens and datum", async () => {
    const { openInToorowTarget } = await import("../analyze/analyzeTargets");
    const target = openInToorowTarget(
      {
        organizationId: "org_EXAMPLE",
        projectId: "proj_EXAMPLE",
        semanticViewId: "sv_EXAMPLE",
        semanticViewVersionId: "svv_EXAMPLE",
        businessDomainId: "bd_EXAMPLE",
      },
      "qr_EXAMPLE",
      "quality",
      "qr_EXAMPLE:3:clicks",
    );
    expect(target).toContain("/org/org_EXAMPLE/project/proj_EXAMPLE/analyze/explore");
    expect(target).toContain("/object/result/qr_EXAMPLE/tab/quality/evidence/");
    expect(target).toContain("semantic_view_version_id=svv_EXAMPLE");
    expect(target).toContain("business_domain_id=bd_EXAMPLE");

    // The round trip is what makes it a shareable address rather than a string.
    const [pathname, search] = target.split("?");
    const parsed = parsePath(pathname, `?${search}`);
    expect(parsed.kind).toBe("resolved");
    if (parsed.kind !== "resolved") return;
    expect(parsed.route.objectId).toBe("qr_EXAMPLE");
    expect(parsed.route.tab).toBe("quality");
    expect(parsed.route.evidenceId).toBe("qr_EXAMPLE:3:clicks");
    expect(buildPath(parsed.route)).toBe(target);
  });

  it("omits half a pin from the target rather than shipping an address that lies", async () => {
    const { resultTarget } = await import("../analyze/analyzeTargets");
    const target = resultTarget(
      { organizationId: "org_EXAMPLE", projectId: "proj_EXAMPLE", semanticViewId: "sv_EXAMPLE" },
      "qr_EXAMPLE",
    );
    expect(target).not.toContain("semantic_view_id");
  });

  it("resolves the section of an owner reference that carries only its triple", async () => {
    // An AI Path step records `owner_workspace`/`owner_object_type`/
    // `owner_object_id` and nothing else (migration 150). The server used to
    // stamp `section: "knowledge-library"` on all of them, which made every step
    // that was not a Context Topic unreachable — procedures and skills are filed
    // under `skills-registry`, and three more workspaces are permitted.
    const { ownerTarget, sectionForObjectType } = await import("../analyze/analyzeTargets");
    const cases: [string, string, string][] = [
      ["context-hub", "context-topic", "knowledge-library"],
      ["context-hub", "context-procedure", "skills-registry"],
      ["context-hub", "skill", "skills-registry"],
      ["context-hub", "ai-path", "knowledge-graph"],
      ["governance", "semantic-view", "semantic-model"],
      ["data", "datastream", "datastreams"],
      ["test", "golden-question", "golden-questions"],
    ];
    for (const [workspace, objectType, section] of cases) {
      expect(sectionForObjectType(workspace, objectType)).toBe(section);
      const target = ownerTarget("org_EXAMPLE", "proj_EXAMPLE", {
        workspace,
        section: null,
        object_type: objectType,
        object_id: "obj_EXAMPLE",
        tab: null,
        version_id: null,
      });
      expect(target).toContain(`/${workspace}/${section}/object/${objectType}/obj_EXAMPLE`);
    }
  });

  it("refuses to resolve a section it cannot decide, rather than picking one", async () => {
    const { sectionForObjectType, ownerTarget } = await import("../analyze/analyzeTargets");
    expect(sectionForObjectType("context-hub", "not-an-object")).toBeNull();
    expect(sectionForObjectType("not-a-workspace", "context-topic")).toBeNull();
    expect(sectionForObjectType("context-hub", null)).toBeNull();
    // And the address is refused rather than aimed at a plausible neighbour.
    expect(
      ownerTarget("org_EXAMPLE", "proj_EXAMPLE", {
        workspace: "context-hub",
        section: null,
        object_type: "not-an-object",
        object_id: "obj_EXAMPLE",
        tab: null,
        version_id: null,
      }),
    ).toBeNull();
  });

  it("returns null for an owner reference the router cannot build", async () => {
    const { ownerTarget } = await import("../analyze/analyzeTargets");
    expect(
      ownerTarget("org_EXAMPLE", "proj_EXAMPLE", {
        workspace: "governance",
        section: "semantic-model",
        object_type: "not-an-object",
        object_id: "x",
        tab: null,
        version_id: null,
      }),
    ).toBeNull();
    // A dead link that looks live is worse than no link.
    expect(ownerTarget("org_EXAMPLE", "proj_EXAMPLE", null)).toBeNull();
  });
});
