import { buildPath, parsePath, type CanonicalRoute } from "../shell/router";

const cases: Array<[string, CanonicalRoute["scope"], string | null, string | null]> = [
  ["/org/org-1/project/proj-1/access/people", "project", "org-1", "proj-1"],
  ["/org/org-1/project/proj-1/getting-started", "project", "org-1", "proj-1"],
  ["/org/org-1/settings/members", "organization", "org-1", null],
  ["/account/profile", "account", null, null],
];

describe("global scope routes", () => {
  it.each(cases)("round-trips %s without fabricated scope", (path, scope, organizationId, projectId) => {
    const parsed = parsePath(path);
    expect(parsed.kind).toBe("resolved");
    if (parsed.kind !== "resolved") return;
    expect(parsed.route).toMatchObject({ scope, organizationId, projectId });
    expect(buildPath(parsed.route)).toBe(path);
  });

  it.each([
    ["/org/org-1/project/proj-1/access", "/org/org-1/project/proj-1/access/people"],
    ["/org/org-1/settings", "/org/org-1/settings/general"],
    ["/account", "/account/profile"],
  ])("normalizes bare surface %s", (bare, canonical) => {
    const parsed = parsePath(bare);
    expect(parsed.kind).toBe("resolved");
    if (parsed.kind === "resolved") expect(buildPath(parsed.route)).toBe(canonical);
  });
});