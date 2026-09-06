/**
 * Which Project the rail points at, on every kind of address.
 *
 * Five surfaces rendered with NO navigation at all: the shell read
 * `README.md:77` — *"Global scope surfaces sit outside project navigation"* —
 * as "no navigation", so Project Settings, Project Access, Organization
 * Settings, User Account and Getting Started had nothing to click. Jean amended
 * it on 2026-08-04: the rail stays.
 *
 * The rail still has to name a Project. Three of the five carry one in the
 * address; the other two do not, and the rail follows the one the person was
 * last in rather than picking.
 */
import { describe, expect, it } from "vitest";
import { resolveRailScope } from "../shell/ApplicationShell";

const REMEMBERED = { organizationId: "org_LAST", projectId: "proj_LAST" };

describe("the rail's Project", () => {
  it("is the one in the address whenever the address carries one", () => {
    expect(
      resolveRailScope({ organizationId: "org_A", projectId: "proj_A" }, REMEMBERED),
    ).toEqual({ organizationId: "org_A", projectId: "proj_A" });
  });

  it("falls back to the last visited Project on an address that carries none", () => {
    // Organization Settings: an Organization, no Project.
    expect(resolveRailScope({ organizationId: "org_A", projectId: null }, REMEMBERED)).toEqual(REMEMBERED);
    // User Account: neither.
    expect(resolveRailScope({ organizationId: null, projectId: null }, REMEMBERED)).toEqual(REMEMBERED);
  });

  it("is null — no rail — rather than a guessed Project", () => {
    expect(resolveRailScope({ organizationId: null, projectId: null }, null)).toBeNull();
    expect(resolveRailScope({ organizationId: "org_A", projectId: null }, null)).toBeNull();
  });
});
