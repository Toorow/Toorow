/**
 * AC17 — the Chart Template is reachable, and the two ways in are addresses.
 *
 * `python scripts/object_coverage_audit.py` answered
 * `ratified objects reaching no surface : 1 — Chart Template` for thirteen
 * months. That script reads `navigation.ts`; this test reads the same registry
 * from the other side and proves the declarations RESOLVE — a lens nothing
 * dispatches and an object type nothing mounts would still satisfy the audit.
 *
 * Against the REAL registry on purpose. A synthetic contract would prove the
 * router's grammar (`AnalyzeRoutes.test.tsx` already does) and nothing at all
 * about whether this object was declared.
 */
import { findObjectContract, sectionLenses } from "../shell/navigation";
import { buildPath, parsePath } from "../shell/router";

/** The address prefix every workspace route hangs from. */
const SCOPE = "/org/org_EXAMPLE/project/proj_EXAMPLE";

it("declares `templates` as a lens of Reports, and a bare Reports address still opens Reports", () => {
  const lenses = sectionLenses("analyze", "reports").map((lens) => lens.slug);
  expect(lenses).toContain("templates");
  // The FIRST lens is the declared default: adding a fourth must not change
  // which screen a bare `/analyze/reports` opens.
  expect(lenses[0]).toBe("reports");
});

it("declares the Chart Template object with the four tabs the ratified table names", () => {
  const contract = findObjectContract("analyze", "reports", "chart-template");
  expect(contract).toBeTruthy();
  expect(contract?.tabs).toEqual(["overview", "presentation", "compatibility", "versions"]);
  expect(contract?.defaultTab).toBe("overview");
});

it("resolves the lens address, and round-trips it", () => {
  const route = parsePath(`${SCOPE}/analyze/reports/lens/templates`);
  expect(route.kind).toBe("resolved");
  if (route.kind !== "resolved") return;
  expect(route.route.section).toBe("reports");
  expect(route.route.lens).toBe("templates");
  expect(buildPath(route.route)).toBe(`${SCOPE}/analyze/reports/lens/templates`);
});

it("resolves the workbench address on each of its four tabs", () => {
  for (const tab of ["overview", "presentation", "compatibility", "versions"]) {
    const parsed = parsePath(`${SCOPE}/analyze/reports/object/chart-template/vtpl_EXAMPLE/tab/${tab}`);
    expect(parsed.kind).toBe("resolved");
    if (parsed.kind !== "resolved") continue;
    expect(parsed.route.objectType).toBe("chart-template");
    expect(parsed.route.objectId).toBe("vtpl_EXAMPLE");
    expect(parsed.route.tab).toBe(tab);
  }
});

it("declares no version tail, because no row of the Versions tab opens one yet", () => {
  const contract = findObjectContract("analyze", "reports", "chart-template") as
    | { versionTabs?: string[] }
    | undefined;
  // Declaring `versionTabs` would make `/version/{id}` a resolvable address that
  // `objectSurfaces.tsx` then refuses on arrival — an address whose only purpose
  // is to say no.
  expect(contract?.versionTabs).toBeUndefined();
});

it("keeps ONE spelling of the noun in the address grammar", () => {
  // Decision D1 of epic 72 collapsed `Visualization Template` / `Widget
  // template` / nothing-in-the-glossary into `Chart Template`. A second spelling
  // in the address grammar would reopen exactly what D1 closed.
  expect(findObjectContract("analyze", "reports", "visualization-template")).toBeFalsy();
});

it("carries the Result the list is judged against in the address", () => {
  const parsed = parsePath(`${SCOPE}/analyze/reports/lens/templates`, "?result_id=qr_EXAMPLE");
  expect(parsed.kind).toBe("resolved");
  if (parsed.kind !== "resolved") return;
  // Declared in the section's query contract, so it survives the parse. A
  // parameter nobody declares is dropped, and the shared link would open the
  // same list judged against nothing.
  expect(parsed.route.query?.result_id).toBe("qr_EXAMPLE");
});

it("refuses a Result pin that follows a moving target", () => {
  const parsed = parsePath(`${SCOPE}/analyze/reports/lens/templates`, "?result_id=latest");
  expect(parsed.kind).toBe("resolved");
  if (parsed.kind !== "resolved") return;
  expect(parsed.route.query?.result_id).toBeUndefined();
});
