/**
 * The clock registry is reachable, and only by whoever the route admits.
 *
 * `/platform/clocks` edits a declared cadence, pushes it into Cloud Scheduler and
 * fires a clock on demand — 730 lines of screen that nothing in the console linked
 * to. Measured 2026-08-08: `grep -rln 'globalSurface: "platform"' ui/admin/src`
 * returned `router.tsx` and nothing else, so the only way in was to type the
 * address.
 *
 * The link is gated because the route is: `_enforce_platform_admin` answers 404,
 * not 403, to anyone outside `TOOROW_SUPER_ADMINS`. Three states, and the third is
 * the one that matters — unknown hides, exactly like refused, because a profile
 * that could not be read is not a promotion.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import StableSidebar from "../shell/StableSidebar";
import { RouterProvider } from "../shell/router";

const SCOPE = { organizationId: "org_EXAMPLE", projectId: "proj_EXAMPLE" };

function profileReplying(body: unknown, ok = true) {
  return vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === "string" ? input : input.toString();
    if (url.includes("/api/me/profile")) {
      return {
        ok,
        status: ok ? 200 : 404,
        json: async () => body,
      } as unknown as Response;
    }
    return { ok: true, status: 200, json: async () => ({}) } as unknown as Response;
  });
}

async function openAccountMenu() {
  render(
    <RouterProvider>
      <StableSidebar scope={SCOPE} />
    </RouterProvider>,
  );
  await userEvent.click(screen.getByLabelText("Account menu"));
}

describe("reaching the platform clocks", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    window.localStorage.clear();
    window.sessionStorage.clear();
  });

  it("offers the entry to a platform admin", async () => {
    vi.stubGlobal("fetch", profileReplying({ is_super_admin: true }));
    await openAccountMenu();
    await waitFor(() => expect(screen.getByRole("menuitem", { name: /platform clocks/i })).toBeTruthy());
  });

  it("hides it from everyone else — the route would answer 404", async () => {
    vi.stubGlobal("fetch", profileReplying({ is_super_admin: false }));
    await openAccountMenu();
    // Sign out proves the menu rendered, so the absence below is an absence and
    // not an unopened menu.
    expect(screen.getByRole("menuitem", { name: /sign out/i })).toBeTruthy();
    expect(screen.queryByRole("menuitem", { name: /platform clocks/i })).toBeNull();
  });

  it("hides it while the answer is unknown, and when the profile cannot be read", async () => {
    vi.stubGlobal("fetch", profileReplying({}, false));
    await openAccountMenu();
    expect(screen.getByRole("menuitem", { name: /sign out/i })).toBeTruthy();
    expect(screen.queryByRole("menuitem", { name: /platform clocks/i })).toBeNull();
  });

  it("reads a literal true only — a missing field is not a promotion", async () => {
    vi.stubGlobal("fetch", profileReplying({ is_super_admin: "yes" }));
    await openAccountMenu();
    expect(screen.queryByRole("menuitem", { name: /platform clocks/i })).toBeNull();
  });

  it("navigates to the platform surface, which defaults to the clocks section", async () => {
    vi.stubGlobal("fetch", profileReplying({ is_super_admin: true }));
    await openAccountMenu();
    const entry = await screen.findByRole("menuitem", { name: /platform clocks/i });
    await userEvent.click(entry);
    await waitFor(() => expect(window.location.pathname).toContain("platform"));
    expect(window.location.pathname).toContain("clocks");
  });
});
