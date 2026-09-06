/**
 * Readable addresses, and the promise that no old one breaks.
 *
 * `/org/org_01KYJ0NP…/project/proj_01KYJ0NP…/overview/project-overview` and
 * `/org/acme/project/site-europe/overview/project-overview` name the same thing.
 * Only one can be read, typed or dictated.
 *
 * The migration is only safe because both slugs are IMMUTABLE and the server
 * enforces it (`slug_immutable` on the org PATCH; "id and slug are immutable"
 * for projects), so a slug is exactly as stable an address as a ULID.
 */
import { parsePath, buildPath } from "../shell/router";
import { registerScopeAliases, resetScopeAliases } from "../shell/scopeAliases";

const ORG_ID = "org_01EXAMPLE0000000000000000";
const PROJECT_ID = "proj_01EXAMPLE000000000000000";

beforeEach(() => resetScopeAliases());

function resolved(pathname: string) {
  const result = parsePath(pathname);
  if (result.kind !== "resolved") throw new Error(`${pathname} -> ${result.kind}`);
  return result.route;
}

it("keeps carrying the id in the route, whichever form the address used", () => {
  registerScopeAliases([{ id: ORG_ID, slug: "acme", projects: [{ id: PROJECT_ID, slug: "site-europe" }] }]);

  const bySlug = resolved("/org/acme/project/site-europe/overview/project-overview");
  const byId = resolved(`/org/${ORG_ID}/project/${PROJECT_ID}/overview/project-overview`);

  expect(bySlug.organizationId).toBe(ORG_ID);
  expect(bySlug.projectId).toBe(PROJECT_ID);
  // The whole containment claim: nothing downstream of the router can tell
  // which form the person arrived with.
  expect(bySlug).toEqual(byId);
});

it("writes the readable address once the slugs are known", () => {
  registerScopeAliases([{ id: ORG_ID, slug: "acme", projects: [{ id: PROJECT_ID, slug: "site-europe" }] }]);
  const route = resolved(`/org/${ORG_ID}/project/${PROJECT_ID}/data/datastreams`);
  expect(buildPath(route)).toBe("/org/acme/project/site-europe/data/datastreams");
});

it("AN ADDRESS ALREADY SHARED NEVER BREAKS — a ULID resolves with no aliases at all", () => {
  // The registry is empty here on purpose: this is the state of every first
  // paint, before the scope has loaded, and the state of any address whose
  // organization this person cannot see.
  const route = resolved(`/org/${ORG_ID}/project/${PROJECT_ID}/data/datastreams`);
  expect(route.organizationId).toBe(ORG_ID);
  expect(route.projectId).toBe(PROJECT_ID);
  expect(buildPath(route)).toBe(`/org/${ORG_ID}/project/${PROJECT_ID}/data/datastreams`);
});

it("falls back per segment, so one known slug does not hide an unknown one", () => {
  registerScopeAliases([{ id: ORG_ID, slug: "acme", projects: [] }]);
  const route = resolved(`/org/acme/project/${PROJECT_ID}/data/datastreams`);
  expect(route.projectId).toBe(PROJECT_ID);
  expect(buildPath(route)).toBe(`/org/acme/project/${PROJECT_ID}/data/datastreams`);
});

it("ignores a slug equal to the id, which would teach nothing", () => {
  registerScopeAliases([{ id: ORG_ID, slug: ORG_ID, projects: [] }]);
  expect(buildPath(resolved(`/org/${ORG_ID}/settings/general`)))
    .toBe(`/org/${ORG_ID}/settings/general`);
});
