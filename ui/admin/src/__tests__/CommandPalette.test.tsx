/**
 * The second way in.
 *
 * The rail is a tree, and a tree is the right shape for exploring and the wrong
 * one for going somewhere you can already name. Everything in this console has
 * a name — a workspace, a section, a Project, a Datastream, a settings page —
 * and until now the only way to reach any of them was to expand the right
 * branch of the right workspace.
 *
 * What is asserted here is the contract, not the decoration:
 *   - the shortcut opens it, and a VISIBLE control opens it too (a feature
 *     reachable only by a chord nobody was told about is not a feature);
 *   - what it lists comes from the registry and from the scope payload, never
 *     from a copied list;
 *   - choosing an entry navigates through the router, so the address that
 *     results is one `parsePath` resolves;
 *   - the history is offered while the box is empty, and stands aside the
 *     moment a question is typed.
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import TopBar, { resetObjectNameCache } from "../shell/TopBar";
import { resetPaletteCache } from "../shell/CommandPalette";
import { forgetRecentRoutes } from "../shell/recentRoutes";
import { RouterProvider } from "../shell/router";
import { ScopeProvider, type OrgRef } from "../shell/scope";
import { resetScopeAliases } from "../shell/scopeAliases";

const ORG_ID = "org_01EXAMPLE0000000000000000";
const PROJECT_ID = "proj_01EXAMPLE000000000000000";
const STREAM_ID = "dstr_01EXAMPLE000000000000000";
const HOME = "/org/acme/project/site-europe/overview/project-overview";

const ORGS: OrgRef[] = [{
  id: ORG_ID, name: "Acme", slug: "acme", branding: null,
  projects: [{ id: PROJECT_ID, name: "Site Europe", slug: "site-europe" }],
}];

function stubDatastreams(items: Array<{ id: string; name: string }>) {
  vi.stubGlobal("fetch", vi.fn().mockImplementation((input: unknown) =>
    Promise.resolve({
      ok: true,
      status: 200,
      json: async () => (String(input).includes("/datastreams")
        ? {
            schema_version: "data-datastreams.v1",
            project_ref: { object_type: "project", id: PROJECT_ID },
            generated_at: "2026-08-17T00:00:00Z",
            evidence_as_of: null,
            items: items.map((item) => ({
              object_ref: { object_type: "datastream", id: item.id },
              name: item.name,
              states: {},
              evidence: {},
              evidence_as_of: null,
              links: {},
            })),
            unavailable_reasons: [],
            allowed_actions: [],
          }
        : {}),
      text: async () => "{}",
    }),
  ));
}

function mount(pathname = HOME) {
  window.history.replaceState({}, "", pathname);
  return render(
    <RouterProvider>
      <ScopeProvider orgs={ORGS}><TopBar /></ScopeProvider>
    </RouterProvider>,
  );
}

/** The bar resolves its Project from the aliases, which are published in an
 *  effect through the override seam — so the palette has a scope to offer only
 *  once that has landed. Same lag `TopBarRail.test.tsx` documents. */
async function openPalette(pathname = HOME) {
  mount(pathname);
  await screen.findByText("Site Europe");
  fireEvent.keyDown(document, { key: "k", ctrlKey: true });
  return screen.getByRole("dialog", { name: /search and jump to/i });
}

beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
  resetScopeAliases();
  resetObjectNameCache();
  resetPaletteCache();
  forgetRecentRoutes();
  stubDatastreams([{ id: STREAM_ID, name: "Site Europe — GA4" }]);
});
afterEach(() => vi.restoreAllMocks());

it("opens on Ctrl+K, and closes on Escape", async () => {
  await openPalette();

  fireEvent.keyDown(document, { key: "Escape" });

  await waitFor(() =>
    expect(screen.queryByRole("dialog", { name: /search and jump to/i })).not.toBeInTheDocument());
});

it("is discoverable without the shortcut: a visible control opens the same box", async () => {
  mount();
  await screen.findByText("Site Europe");

  const trigger = screen.getByRole("button", { name: /search and jump to/i });
  // The chord is HINTED on the control, which is how the second person learns it.
  expect(trigger).toHaveAttribute("aria-keyshortcuts", "Control+K Meta+K");

  fireEvent.click(trigger);

  expect(screen.getByRole("dialog", { name: /search and jump to/i })).toBeInTheDocument();
  // The Datastream read the opening starts is awaited here rather than left to
  // settle after the test: a state update landing on an unmounted tree is what
  // React's act warning is for.
  await screen.findByRole("option", { name: /Site Europe — GA4/ });
});

it("lists the six workspaces of the registry and the sections they declare", async () => {
  const dialog = await openPalette();

  // Scoped to the dialog: the governed path in the bar behind it also says
  // "Overview", and it is a different control.
  await screen.findByRole("option", { name: /^Explore/ });
  for (const workspace of ["Overview", "Analyze", "Test", "Data", "Governance", "Context Hub"]) {
    expect(within(dialog).getByText(workspace)).toBeInTheDocument();
  }
  expect(await screen.findByRole("option", { name: /^Datastreams/ })).toBeInTheDocument();
  expect(screen.getByRole("option", { name: /^Explore/ })).toBeInTheDocument();
  expect(screen.getByRole("option", { name: /^Evidence/ })).toBeInTheDocument();
});

it("narrows to what was typed, without asking the server again", async () => {
  await openPalette();
  await screen.findByRole("option", { name: /^Explore/ });
  const readsBefore = (fetch as unknown as { mock: { calls: unknown[] } }).mock.calls.length;

  fireEvent.change(screen.getByRole("combobox"), { target: { value: "explore" } });

  expect(screen.getByRole("option", { name: /^Explore/ })).toBeInTheDocument();
  expect(screen.queryByRole("option", { name: /^Datastreams/ })).not.toBeInTheDocument();
  // Six keystrokes are not six requests. The collection was read once, on open.
  expect((fetch as unknown as { mock: { calls: unknown[] } }).mock.calls.length).toBe(readsBefore);
});

it("opens the highlighted destination on Enter, through the router", async () => {
  await openPalette();
  await screen.findByRole("option", { name: /^Datastreams/ });

  const box = screen.getByRole("combobox");
  fireEvent.change(box, { target: { value: "datastreams" } });
  fireEvent.keyDown(box, { key: "Enter" });

  expect(window.location.pathname).toBe("/org/acme/project/site-europe/data/datastreams");
});

it("walks the list with the arrows and opens what is highlighted", async () => {
  await openPalette();
  await screen.findByRole("option", { name: /^Datastreams/ });

  const box = screen.getByRole("combobox");
  fireEvent.change(box, { target: { value: "project overview" } });
  const options = screen.getAllByRole("option");
  expect(options[0]).toHaveAttribute("aria-selected", "true");

  fireEvent.keyDown(box, { key: "ArrowDown" });
  fireEvent.keyDown(box, { key: "ArrowUp" });
  fireEvent.keyDown(box, { key: "Enter" });

  expect(window.location.pathname).toBe(HOME);
});

it("offers the Datastreams of the Project by name, from the envelope the rail reads", async () => {
  await openPalette();

  fireEvent.click(await screen.findByRole("option", { name: /Site Europe — GA4/ }));

  expect(window.location.pathname).toBe(
    `/org/acme/project/site-europe/data/datastreams/object/datastream/${STREAM_ID}/tab/overview`,
  );
  // And the bar behind it names the same object — the crumb read that the
  // arrival triggers, awaited rather than left in flight.
  await screen.findByTitle(STREAM_ID);
});

it("offers the Projects of the payload, and the scope surfaces", async () => {
  await openPalette();

  expect(await screen.findByRole("option", { name: /Project Settings/ })).toBeInTheDocument();
  expect(screen.getByRole("option", { name: /Organization Settings/ })).toBeInTheDocument();
  expect(screen.getByRole("option", { name: /User Account/ })).toBeInTheDocument();
  expect(screen.getByRole("option", { name: /Getting Started/ })).toBeInTheDocument();

  fireEvent.click(screen.getByRole("option", { name: /User Account/ }));

  expect(window.location.pathname).toBe("/account/profile");
});

/* ------------------------------------------------------------------------
 * The history. It answers "take me back", which is a different question from
 * "find me X" — so it is offered while the box is empty and stands aside as
 * soon as one is typed.
 * ---------------------------------------------------------------------- */

it("puts what was opened recently at the top, and drops it once a question is typed", async () => {
  localStorage.setItem("toorow_recent_routes", JSON.stringify([
    { path: "/org/acme/project/site-europe/governance/evidence", label: "Governance › Evidence" },
  ]));

  await openPalette();
  await screen.findByRole("option", { name: /Site Europe — GA4/ });

  const first = screen.getAllByRole("option")[0];
  expect(first).toHaveTextContent("Governance › Evidence");
  expect(screen.getByText("Recent")).toBeInTheDocument();

  fireEvent.change(screen.getByRole("combobox"), { target: { value: "evidence" } });

  expect(screen.queryByText("Governance › Evidence")).not.toBeInTheDocument();
  expect(screen.getByRole("option", { name: /^Evidence/ })).toBeInTheDocument();
});

it("reopens a recent address through the router, never as a hand-built link", async () => {
  localStorage.setItem("toorow_recent_routes", JSON.stringify([
    { path: "/org/acme/project/site-europe/governance/evidence", label: "Governance › Evidence" },
  ]));

  await openPalette();
  fireEvent.click(screen.getByRole("option", { name: /Governance › Evidence/ }));

  // The router CANONICALIZES what it is handed — Evidence declares a default
  // lens — which is exactly why the entry is reopened through `parsePath` and
  // `navigate` rather than by assigning the stored string to the address bar.
  expect(window.location.pathname).toBe(
    "/org/acme/project/site-europe/governance/evidence/lens/lineage-provenance",
  );
});

it("records the address being looked at, with the words the bar drew", async () => {
  mount("/org/acme/project/site-europe/data/datastreams");

  await waitFor(() => {
    const stored = JSON.parse(localStorage.getItem("toorow_recent_routes") ?? "[]");
    expect(stored[0]).toEqual({
      path: "/org/acme/project/site-europe/data/datastreams",
      label: "Data › Datastreams",
    });
  });
});

it("never offers the page being looked at as somewhere to go", async () => {
  await openPalette("/org/acme/project/site-europe/governance/evidence");
  await screen.findByRole("option", { name: /Site Europe — GA4/ });

  // It IS in the history — it was just recorded — and it is not in the list:
  // an entry that reopens the page you are on is a control that does nothing.
  expect(screen.queryByRole("option", { name: /Governance › Evidence/ })).not.toBeInTheDocument();
});
