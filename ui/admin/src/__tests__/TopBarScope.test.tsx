/**
 * What the gear menu offers, on an address that carries no Project.
 *
 * `useScope().activeProject` is null on every scope surface, and the menu read
 * it directly: on Organization Settings and User Account, three of its five
 * entries silently disappeared — Getting Started, Project Settings, Project
 * Access — and the switcher collapsed to the word "Organization". Jean,
 * 2026-08-04, looking at the two that were left: *"pourquoi le menu est pas
 * pareil"*.
 *
 * The name of the remembered Project is resolved from the real scope payload,
 * never rebuilt from its id — so a Project that has left the payload (access
 * lost, archived) drops out of the menu instead of being offered.
 */
import { describe, expect, it } from "vitest";
import { resolveBarScope } from "../shell/TopBar";
import type { OrgRef, ProjectRef } from "../shell/scope";

const PROJECT: ProjectRef = { id: "proj_A", name: "Acme site" } as ProjectRef;
const OTHER: ProjectRef = { id: "proj_B", name: "Acme app" } as ProjectRef;
const ORG: OrgRef = { id: "org_A", name: "Acme", branding: null, projects: [PROJECT, OTHER] } as OrgRef;

describe("the bar's scope", () => {
  it("is the routed Project whenever the address carries one", () => {
    expect(resolveBarScope(ORG, [ORG], OTHER, { organizationId: "org_A", projectId: "proj_A" }))
      .toEqual({ org: ORG, project: OTHER });
  });

  it("keeps the last Project — with its real name — on an address that carries none", () => {
    // Organization Settings: an Organization, no Project.
    expect(resolveBarScope(ORG, [ORG], null, { organizationId: "org_A", projectId: "proj_A" }))
      .toEqual({ org: ORG, project: PROJECT });
    // User Account: neither. The Organization is recovered from the payload.
    expect(resolveBarScope(null, [ORG], null, { organizationId: "org_A", projectId: "proj_A" }))
      .toEqual({ org: ORG, project: PROJECT });
  });

  it("offers no Project rather than one the scope payload no longer carries", () => {
    expect(resolveBarScope(ORG, [ORG], null, { organizationId: "org_A", projectId: "proj_GONE" }))
      .toEqual({ org: ORG, project: null });
    expect(resolveBarScope(ORG, [ORG], null, null)).toEqual({ org: ORG, project: null });
  });
});
