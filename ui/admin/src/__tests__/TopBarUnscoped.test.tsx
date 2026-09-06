/**
 * THE ROOM WITH NO DOOR.
 *
 * On `/account/profile` in a fresh session — the address a person lands on
 * after signing in without a remembered Project — the console offered no way
 * back into any Project at all:
 *
 *   - the left rail renders nothing without a Project
 *     (`page-structure.md` A.7.1: "renders only when there is one");
 *   - the scope switcher was gated on `org && activeProject`, and `useScope()`
 *     returns NEITHER on an account address (`scope.tsx:249-257`);
 *   - the gear menu gates its three Project entries on `activeProject` and
 *     Organization Settings on `org`, so one entry survived: "User Account",
 *     the page already being looked at.
 *
 * `lastProjectScope()` rescues the case where the person navigated here from a
 * Project. It rescues nothing on the first page of a session, which is exactly
 * when it happens — so these cases start from an EMPTY session storage.
 *
 * What the switcher needs is not a current Project. It is a LIST of them to
 * offer, and `orgs` carries that on every surface.
 */
import { fireEvent, render, screen } from "@testing-library/react";
import TopBar, { resetObjectNameCache } from "../shell/TopBar";
import { RouterProvider } from "../shell/router";
import { ScopeProvider, type OrgRef } from "../shell/scope";
import { resetScopeAliases } from "../shell/scopeAliases";

const ORG_ID = "org_01EXAMPLE0000000000000000";
const PROJECT_ID = "proj_01EXAMPLE000000000000000";

const ORGS: OrgRef[] = [{
  id: ORG_ID, name: "Acme", slug: "acme", branding: null,
  projects: [{ id: PROJECT_ID, name: "Site Europe", slug: "site-europe" }],
}];

function open(pathname: string, orgs: OrgRef[] = ORGS) {
  window.history.replaceState({}, "", pathname);
  return render(
    <RouterProvider>
      <ScopeProvider orgs={orgs}><TopBar /></ScopeProvider>
    </RouterProvider>,
  );
}

beforeEach(() => {
  // A FRESH session: nothing remembered, which is the case that was broken.
  sessionStorage.clear();
  resetScopeAliases();
  resetObjectNameCache();
});

it("keeps the switcher on a surface that carries no Project", () => {
  open("/account/profile");

  expect(
    screen.getByRole("button", { name: /switch organization or project/i }),
  ).toBeInTheDocument();
});

it("offers the Projects themselves, and opening one leaves the account surface", () => {
  open("/account/profile");

  fireEvent.click(screen.getByRole("button", { name: /switch organization or project/i }));
  const dialog = screen.getByRole("dialog", { name: /switch organization or project/i });
  expect(dialog).toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "Site Europe" }));

  expect(window.location.pathname).toBe("/org/acme/project/site-europe/overview/project-overview");
});

it("names the way back in from the gear menu, which is the only control left", () => {
  open("/account/profile");

  fireEvent.click(screen.getByRole("button", { name: /scope and account menu/i }));
  const entry = screen.getByRole("menuitem", { name: /open a project/i });
  fireEvent.click(entry);

  // The SAME dialog the switcher opens — one gesture, declared twice because
  // the menu is where a stranded person looks.
  expect(screen.getByRole("dialog", { name: /switch organization or project/i })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Site Europe" })).toBeInTheDocument();
});

it("does not repeat the offer once a Project is in hand", () => {
  open("/org/acme/project/site-europe/overview/project-overview");

  fireEvent.click(screen.getByRole("button", { name: /scope and account menu/i }));

  // The switcher beside this menu already names the Project. Asking the same
  // question twice is asking it once too often.
  expect(screen.queryByRole("menuitem", { name: /open a project/i })).not.toBeInTheDocument();
  expect(screen.getByRole("menuitem", { name: /project settings/i })).toBeInTheDocument();
});

it("offers no switch at all when the payload holds no Project", () => {
  open("/account/profile", [{ id: ORG_ID, name: "Acme", slug: "acme", branding: null, projects: [] }]);

  // An honest absence: there is nothing to switch TO, and a control that opens
  // an empty list would be a door onto a wall.
  expect(
    screen.queryByRole("button", { name: /switch organization or project/i }),
  ).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /scope and account menu/i }));
  expect(screen.queryByRole("menuitem", { name: /open a project/i })).not.toBeInTheDocument();
});

// ---------------------------------------------------------------------------
// The dialog filters (page-structure.md §G.3.3): the list is already loaded,
// so narrowing it is pure — and the empty result names the term.
// ---------------------------------------------------------------------------

const TWO_ORGS: OrgRef[] = [
  ...ORGS,
  {
    id: "org_01EXAMPLE0000000000000001", name: "Borealis", slug: "borealis", branding: null,
    projects: [{ id: "proj_01EXAMPLE000000000000001", name: "Nord app", slug: "nord-app" }],
  } as OrgRef,
];

it("narrows the dialog by name, keeping a matching org's whole project list", () => {
  open("/account/profile", TWO_ORGS);
  fireEvent.click(screen.getByRole("button", { name: /switch organization or project/i }));

  fireEvent.change(screen.getByRole("textbox", { name: /filter organizations and projects/i }), {
    target: { value: "acme" },
  });

  // The org matched, so its project survives even though "acme" is not in it.
  expect(screen.getByRole("button", { name: "Site Europe" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Nord app" })).toBeNull();
});

it("keeps only matching projects under a non-matching org, and clears on reopen", () => {
  open("/account/profile", TWO_ORGS);
  fireEvent.click(screen.getByRole("button", { name: /switch organization or project/i }));
  fireEvent.change(screen.getByRole("textbox", { name: /filter organizations and projects/i }), {
    target: { value: "nord" },
  });
  expect(screen.getByRole("button", { name: "Nord app" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Site Europe" })).toBeNull();

  // Close and reopen: last week's filter must not read as a list that shrank.
  fireEvent.click(screen.getByRole("button", { name: /switch organization or project/i }));
  fireEvent.click(screen.getByRole("button", { name: /switch organization or project/i }));
  expect(screen.getByRole("button", { name: "Site Europe" })).toBeInTheDocument();
});

it("a narrowing that reaches nothing names the term", () => {
  open("/account/profile", TWO_ORGS);
  fireEvent.click(screen.getByRole("button", { name: /switch organization or project/i }));
  fireEvent.change(screen.getByRole("textbox", { name: /filter organizations and projects/i }), {
    target: { value: "shopify" },
  });

  expect(screen.getByTestId("scope-filter-empty")).toHaveTextContent("shopify");
  expect(screen.queryByRole("button", { name: "Site Europe" })).toBeNull();
});
