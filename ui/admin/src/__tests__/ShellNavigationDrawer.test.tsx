/**
 * BELOW `lg` THE PRODUCT HAD NO NAVIGATION.
 *
 * The rail is `hidden … lg:flex` (`StableSidebar.tsx`) and nothing replaced it:
 * on a tablet, or on a laptop with the window at half width, there were no
 * workspaces, no sections, no account menu and no way out of the page except
 * the browser's own back button. `ShellRailOnScopeSurfaces.test.tsx` proves the
 * rail is MOUNTED on the scope surfaces; nothing proved a person who cannot see
 * it has any navigation at all.
 *
 * The drawer renders the SAME component as the rail — `SidebarBody`, one
 * declaration of the six workspaces — so a section added to the registry cannot
 * appear in one host and not the other. That is what the first case asserts by
 * reading the rail's own test ids out of the drawer.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import TopBar, { resetObjectNameCache } from "../shell/TopBar";
import { resetPaletteCache } from "../shell/CommandPalette";
import { forgetRecentRoutes } from "../shell/recentRoutes";
import { RouterProvider } from "../shell/router";
import { ScopeProvider, type OrgRef } from "../shell/scope";
import { resetScopeAliases } from "../shell/scopeAliases";

const ORG_ID = "org_01EXAMPLE0000000000000000";
const PROJECT_ID = "proj_01EXAMPLE000000000000000";
const HOME = "/org/acme/project/site-europe/overview/project-overview";

const ORGS: OrgRef[] = [{
  id: ORG_ID, name: "Acme", slug: "acme", branding: null,
  projects: [{ id: PROJECT_ID, name: "Site Europe", slug: "site-europe" }],
}];

/** The bar mounts on its own here: the rail is `hidden lg:flex` and jsdom
 *  applies no media query, so a shell-wide render would put the rail's test ids
 *  on screen twice and prove nothing about the drawer. */
function mount(pathname = HOME) {
  window.history.replaceState({}, "", pathname);
  return render(
    <RouterProvider>
      <ScopeProvider orgs={ORGS}><TopBar /></ScopeProvider>
    </RouterProvider>,
  );
}

beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
  resetScopeAliases();
  resetObjectNameCache();
  resetPaletteCache();
  forgetRecentRoutes();
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
    ok: true, status: 200, json: async () => ({}), text: async () => "{}",
  }));
});
afterEach(() => vi.restoreAllMocks());

it("opens the same six workspaces the rail declares", async () => {
  mount();
  await screen.findByText("Site Europe");

  fireEvent.click(screen.getByRole("button", { name: /open navigation/i }));

  for (const key of ["overview", "analyze", "test", "data", "governance", "context-hub"]) {
    expect(await screen.findByTestId(`ws-${key}`)).toBeInTheDocument();
  }
});

it("navigates, and closes behind itself", async () => {
  mount();
  await screen.findByText("Site Europe");
  fireEvent.click(screen.getByRole("button", { name: /open navigation/i }));
  fireEvent.click(await screen.findByTestId("ws-data"));

  expect(window.location.pathname).toBe("/org/acme/project/site-europe/data/data-overview");
  // A drawer left open over the page it just opened hides the answer it was
  // asked for.
  await waitFor(() => expect(screen.queryByTestId("ws-data")).not.toBeInTheDocument());
});

it("is not offered when there is no Project to navigate", async () => {
  // A fresh session on the account surface: nothing remembered, so the rail has
  // no Project either. A hamburger opening an empty panel would be a door onto
  // a wall — the switcher is the control that belongs here, and it is there.
  mount("/account/profile");

  expect(screen.queryByRole("button", { name: /open navigation/i })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: /switch organization or project/i })).toBeInTheDocument();
});
