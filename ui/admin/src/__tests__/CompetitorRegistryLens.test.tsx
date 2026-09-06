/**
 * Story 48.5 — the Competitor Registry lens, through the REAL router.
 *
 * This file inherits the useful cases from `Competitors.test.tsx`, which tested
 * a page mounted at no route: a Project that shows nothing must say WHY, and a
 * scope change must never leave the previous Project's identities on screen.
 * Those two invariants survive the deletion of that page; they now run against
 * the workbench a person can actually reach.
 *
 * The rest is what the orphan page could not test because it did not exist: the
 * entity x Datastream matrix, its keyboard traversal, and the list
 * representation that keeps the same data readable when a grid cannot be.
 */
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import ContentRouter from "../shell/ContentRouter";
import { RouterProvider } from "../shell/router";

const ORG = "org_EXAMPLE";
const PROJECT = "proj_EXAMPLE";
const LENS = `/org/${ORG}/project/${PROJECT}/governance/master-data/lens/competitor-registry`;

const NORTHWIND = "mdnode_NORTHWIND";
const SOUTHWIND = "mdnode_SOUTHWIND";
const DS_ADS = "ds_ADS";
const DS_CLUBS = "ds_CLUBS";

function ok(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response;
}

function facet(state: string, refs: unknown[] = []) {
  return { state, count: refs.length, refs, truncated: false };
}

function entity(
  id: string,
  label: string,
  role: string,
  matrix: Array<Record<string, unknown>>,
  representations: Array<Record<string, unknown>> = [],
) {
  return {
    object_ref: {
      type: "tracked-entity",
      id,
      label,
      owner_href: { surface: "project", workspace: "governance", section: "master-data" },
    },
    scope: "organization",
    owner: { kind: "tracked-entity-registry", organization_id: ORG, project_role: role },
    lifecycle_status: "active",
    active_version_ref: { object_type: "object-version", id: `mdver_${id}`, state: "current" },
    selected_version_ref: null,
    available_tabs: ["overview", "representations", "coverage", "used-by", "versions"],
    default_tab: "overview",
    allowed_actions: [],
    used_by: facet("available", matrix.map((cell) => cell.datastream_ref)),
    versions: facet("available", []),
    evidence: facet(representations.length ? "available" : "empty", representations),
    summary: {
      entity_kind: "organization",
      project_role: role,
      aliases: [],
      representations,
      matrix,
      published_count: matrix.filter((cell) => cell.state === "published").length,
      bound_count: matrix.length,
    },
    evidence_as_of: "2026-07-30T08:00:00Z",
  };
}

function cell(datastreamId: string, state: string, extra: Record<string, unknown> = {}) {
  return {
    datastream_ref: { object_type: "datastream", id: datastreamId },
    state,
    direction: "collect",
    report_id: "snapshot",
    exception_reason_code: null,
    exception_reason: null,
    ...extra,
  };
}

function collection(items: unknown[], overrides: Record<string, unknown> = {}) {
  return {
    schema_version: "governance-collection.v1",
    project_ref: { object_type: "project", id: PROJECT },
    organization_ref: { object_type: "organization", id: ORG },
    section: "master-data",
    generated_at: "2026-07-30T09:00:00Z",
    evidence_as_of: "2026-07-30T08:00:00Z",
    lens: "competitor-registry",
    default_lens: "business-domains",
    available_lenses: ["business-domains", "competitor-registry"],
    items,
    coverage: { state: items.length ? "available" : "empty", returned: items.length, total: items.length, bound: 200 },
    unavailable_reasons: [],
    ...overrides,
  };
}

function serve(answer: Response | (() => Response)) {
  vi.stubGlobal(
    "fetch",
    vi.fn(() => Promise.resolve(typeof answer === "function" ? answer() : answer)),
  );
}

function open(pathname: string) {
  window.history.replaceState({}, "", pathname);
  return render(
    <RouterProvider>
      <ContentRouter />
    </RouterProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  window.history.replaceState({}, "", "/");
});

const POPULATED = collection([
  entity(NORTHWIND, "Northwind", "competitor", [cell(DS_ADS, "published"), cell(DS_CLUBS, "candidate")], [
    { connector: "example-source", account_scope: null, external_id: "42", external_label: "Northwind", version: 1 },
  ]),
  entity(SOUTHWIND, "Southwind", "own", [
    cell(DS_ADS, "excluded", {
      exception_reason_code: "not_measured_here",
      exception_reason: "This account does not run competitor campaigns.",
    }),
  ]),
]);

// ---------------------------------------------------------------------------
// Inherited from the deleted orphan page: a blank screen must say why.
// ---------------------------------------------------------------------------

test("a Project with Competitors disabled reads a reason, never an empty registry", async () => {
  serve(
    ok(
      collection([], {
        coverage: { state: "unavailable", returned: 0, total: 0, bound: 200 },
        unavailable_reasons: [
          {
            code: "competitors_capability_disabled",
            message:
              "Competitors is not enabled for this Project, so no tracked identity, role or binding exists to show.",
          },
        ],
      }),
    ),
  );

  open(LENS);

  expect(await screen.findByTestId("reason-competitors_capability_disabled")).toHaveTextContent(
    "not enabled for this Project",
  );
  // The distinction the orphan page existed to make: "nothing here" and "we did
  // not look" are different sentences, and only one of them is a claim.
  expect(screen.queryByTestId("entity-matrix-grid")).not.toBeInTheDocument();
});

test("an enabled Project with no association shows an empty state, not a matrix", async () => {
  serve(ok(collection([])));
  open(LENS);
  expect(await screen.findByText(/Nothing governed here yet/i)).toBeInTheDocument();
  expect(screen.queryByTestId("entity-matrix-grid")).not.toBeInTheDocument();
});

// ---------------------------------------------------------------------------
// The matrix itself.
// ---------------------------------------------------------------------------

test("the matrix shows one row per entity and one column per bound Datastream", async () => {
  serve(ok(POPULATED));
  open(LENS);

  const grid = await screen.findByTestId("entity-matrix-grid");
  expect(within(grid).getByRole("rowheader", { name: "Northwind" })).toBeInTheDocument();
  expect(within(grid).getByRole("rowheader", { name: "Southwind" })).toBeInTheDocument();
  expect(within(grid).getByRole("columnheader", { name: DS_ADS })).toBeInTheDocument();
  expect(within(grid).getByRole("columnheader", { name: DS_CLUBS })).toBeInTheDocument();
});

test("a candidate binding is never rendered as a published one", async () => {
  serve(ok(POPULATED));
  open(LENS);

  const grid = await screen.findByTestId("entity-matrix-grid");
  expect(
    within(grid).getByRole("button", { name: `Northwind, ${DS_ADS}: Published` }),
  ).toBeInTheDocument();
  expect(
    within(grid).getByRole("button", { name: `Northwind, ${DS_CLUBS}: Candidate` }),
  ).toBeInTheDocument();
});

test("an exclusion states its reason inside the accessible name", async () => {
  serve(ok(POPULATED));
  open(LENS);

  const grid = await screen.findByTestId("entity-matrix-grid");
  expect(
    within(grid).getByRole("button", {
      name: `Southwind, ${DS_ADS}: Excluded. This account does not run competitor campaigns.`,
    }),
  ).toBeInTheDocument();
});

test("an unbound pair is Not bound rather than blank", async () => {
  serve(ok(POPULATED));
  open(LENS);

  const grid = await screen.findByTestId("entity-matrix-grid");
  expect(
    within(grid).getByRole("button", { name: `Southwind, ${DS_CLUBS}: Not bound` }),
  ).toBeInTheDocument();
});

test("the arrow keys move between cells and the focused cell explains itself", async () => {
  const user = userEvent.setup();
  serve(ok(POPULATED));
  open(LENS);

  const grid = await screen.findByTestId("entity-matrix-grid");
  const first = within(grid).getByRole("button", { name: `Northwind, ${DS_ADS}: Published` });
  first.focus();
  expect(first).toHaveFocus();

  await user.keyboard("{ArrowRight}");
  await waitFor(() =>
    expect(
      within(grid).getByRole("button", { name: `Northwind, ${DS_CLUBS}: Candidate` }),
    ).toHaveFocus(),
  );

  const detail = await screen.findByTestId("entity-matrix-detail");
  expect(detail).toHaveTextContent("Northwind");
  expect(detail).toHaveTextContent("Candidate");
  // Stated on every focused cell, because it is the confusion this whole
  // capability is designed to prevent.
  expect(detail).toHaveTextContent("does not authorize metric comparison");
});

test("the same data is reachable as a list when a grid will not do", async () => {
  const user = userEvent.setup();
  serve(ok(POPULATED));
  open(LENS);

  await screen.findByTestId("entity-matrix-grid");
  await user.click(screen.getByRole("button", { name: "Show as list" }));

  const list = await screen.findByTestId("entity-matrix-list");
  expect(within(list).getByText("Northwind")).toBeInTheDocument();
  expect(within(list).getByText(`${DS_ADS} — Published`)).toBeInTheDocument();
  expect(screen.queryByTestId("entity-matrix-grid")).not.toBeInTheDocument();
});

test("filtering by role hides the other roles without claiming they are absent", async () => {
  const user = userEvent.setup();
  serve(ok(POPULATED));
  open(LENS);

  await screen.findByTestId("entity-matrix-grid");
  await user.selectOptions(screen.getByLabelText("Filter by Project role"), "own");

  const grid = screen.getByTestId("entity-matrix-grid");
  expect(within(grid).getByRole("rowheader", { name: "Southwind" })).toBeInTheDocument();
  expect(within(grid).queryByRole("rowheader", { name: "Northwind" })).not.toBeInTheDocument();
});

// ---------------------------------------------------------------------------
// Inherited: a scope change never leaves the previous Project on screen.
// ---------------------------------------------------------------------------

test("a Project switch never shows the previous Project's identities", async () => {
  let body: unknown = POPULATED;
  serve(() => ok(body));

  const view = open(LENS);
  expect(await screen.findByRole("rowheader", { name: "Northwind" })).toBeInTheDocument();

  view.unmount();
  body = collection([]);
  window.history.replaceState({}, "", `/org/${ORG}/project/proj_OTHER/governance/master-data/lens/competitor-registry`);
  render(
    <RouterProvider>
      <ContentRouter />
    </RouterProvider>,
  );

  await waitFor(() => expect(screen.queryByRole("rowheader", { name: "Northwind" })).not.toBeInTheDocument());
});
