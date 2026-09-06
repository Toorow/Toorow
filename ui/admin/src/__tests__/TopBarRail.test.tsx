/**
 * The governed path is read by a person, so it has to be readable.
 *
 * It carried two natures and drew them as one: `route.objectType` is a WORD
 * (`source-account`) and was rendered in the monospace face `DESIGN.md:96`
 * reserves for immutable ids, dashes included; `route.objectId` is an id and was
 * rendered at full length — on this product's ULIDs, thirty characters in the
 * middle of a breadcrumb.
 *
 * Nothing asserted any of it, which is why it drifted.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import TopBar, { resetObjectNameCache } from "../shell/TopBar";
import { RouterProvider } from "../shell/router";
import { ScopeProvider, type OrgRef } from "../shell/scope";
import { resetScopeAliases } from "../shell/scopeAliases";

const ORG_ID = "org_01EXAMPLE0000000000000000";
const PROJECT_ID = "proj_01EXAMPLE000000000000000";
const OBJECT_ID = "sacc_01EXAMPLE000000000000000";
const STREAM_ID = "dstr_01EXAMPLE000000000000000";

const ORGS: OrgRef[] = [{
  id: ORG_ID, name: "Acme", slug: "acme", branding: null,
  projects: [{ id: PROJECT_ID, name: "Site Europe", slug: "site-europe" }],
}];

function open(pathname: string) {
  window.history.replaceState({}, "", pathname);
  return render(
    <RouterProvider>
      <ScopeProvider orgs={ORGS}><TopBar /></ScopeProvider>
    </RouterProvider>,
  );
}

beforeEach(() => {
  resetScopeAliases();
  resetObjectNameCache();
});
afterEach(() => vi.restoreAllMocks());

it("says the object TYPE in words, not as a slug in the id typeface", () => {
  open(`/org/acme/project/site-europe/data/sources/object/source-account/${OBJECT_ID}/tab/overview`);

  const crumb = screen.getByText("Source Account");
  expect(crumb).toBeInTheDocument();
  // A noun is not an identifier: mono here is what made `source-account` read
  // like a hash.
  expect(crumb.className).not.toContain("font-mono");
});

it("keeps the identifier reachable without letting it fill the bar", () => {
  open(`/org/acme/project/site-europe/data/sources/object/source-account/${OBJECT_ID}/tab/overview`);

  const id = screen.getByTitle(OBJECT_ID);
  // Demoted, not hidden: the full value is one hover away, and Governance needs
  // the exact pointer.
  expect(id).toHaveTextContent(OBJECT_ID);
  expect(id.className).toContain("font-mono");
  expect(id.className).toContain("truncate");
});

it("shows the organization and project by NAME, never by id", async () => {
  open("/org/acme/project/site-europe/overview/project-overview");

  // Awaited on purpose. Through the override seam the aliases are published in
  // an effect, so the FIRST parse of a slug address cannot resolve it yet and
  // the scope control has no active project to name. The production path
  // registers before its state lands, so this lag is the test seam's, not the
  // shell's — but asserting synchronously here would hide it rather than say it.
  expect(await screen.findByText("Acme")).toBeInTheDocument();
  expect(screen.getByText("Site Europe")).toBeInTheDocument();
  expect(screen.queryByText(ORG_ID)).not.toBeInTheDocument();
  expect(screen.queryByText(PROJECT_ID)).not.toBeInTheDocument();
});

/* ------------------------------------------------------------------------
 * A PATH THAT CANNOT BE WALKED IS A CAPTION.
 *
 * Every crumb was a `<span>`: the one gesture a breadcrumb exists for — going
 * back up a level — was nowhere on an object page. Leaving a Datastream meant
 * finding it again in the rail.
 * ------------------------------------------------------------------------ */

const OBJECT_ROUTE =
  `/org/acme/project/site-europe/data/datastreams/object/datastream/${STREAM_ID}/tab/overview`;

it("walks back up: the workspace crumb opens its first section", async () => {
  open(OBJECT_ROUTE);

  fireEvent.click(await screen.findByRole("button", { name: "Data" }));

  expect(window.location.pathname).toBe("/org/acme/project/site-europe/data/data-overview");
});

it("walks back up: the section crumb reopens its collection, without the object", async () => {
  open(OBJECT_ROUTE);

  fireEvent.click(await screen.findByRole("button", { name: "Datastreams" }));

  expect(window.location.pathname).toBe("/org/acme/project/site-europe/data/datastreams");
});

it("leaves the crumb of the page one is on unclickable, and says it is the page", () => {
  open(OBJECT_ROUTE);

  const here = screen.getByTitle(STREAM_ID);
  expect(here).toHaveAttribute("aria-current", "page");
  expect(here.tagName).toBe("SPAN");
});

/* ------------------------------------------------------------------------
 * The rail resolves a Datastream to its NAME two centimetres lower down. The
 * crumb above it showed a ULID cut to twelve characters — the same object,
 * named on one side of the screen and unnamed on the other.
 * ------------------------------------------------------------------------ */

function stubDatastreams(items: Array<{ id: string; name: string }>) {
  const fetchMock = vi.fn().mockImplementation((input: unknown) =>
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
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

it("names the object in the path, and keeps its exact id one hover away", async () => {
  stubDatastreams([{ id: STREAM_ID, name: "Site Europe — GA4" }]);
  open(OBJECT_ROUTE);

  const named = await screen.findByText("Site Europe — GA4");
  expect(named).toHaveAttribute("title", STREAM_ID);
  // The ULID is no longer WORDS on the screen — it is the pointer behind them.
  expect(named.className).not.toContain("font-mono");
  expect(screen.queryByText(STREAM_ID)).not.toBeInTheDocument();
});

it("keeps the id rather than inventing a name when the read gives nothing", async () => {
  stubDatastreams([]);
  open(OBJECT_ROUTE);

  await waitFor(() => expect(screen.getByTitle(STREAM_ID)).toHaveTextContent(STREAM_ID));
  expect(screen.getByTitle(STREAM_ID).className).toContain("font-mono");
});

it("does not read the collection again once it has been read", async () => {
  const fetchMock = stubDatastreams([{ id: STREAM_ID, name: "Site Europe — GA4" }]);
  const reads = () =>
    fetchMock.mock.calls.filter((call) => String(call[0]).includes("/datastreams")).length;
  const first = open(OBJECT_ROUTE);
  await screen.findByText("Site Europe — GA4");
  const afterFirst = reads();
  first.unmount();

  open(OBJECT_ROUTE);
  await screen.findByText("Site Europe — GA4");

  // A breadcrumb that re-read its collection on every mount would put a request
  // behind every navigation of the screen underneath it.
  expect(reads()).toBe(afterFirst);
});
