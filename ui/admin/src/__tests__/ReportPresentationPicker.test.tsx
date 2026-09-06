/**
 * AC21 — a Report pins a presentation version that EXISTS, chosen from a list.
 *
 * Two defects closed here, and both had been true for a while:
 *
 *   1. the tab asked for an "Exact version identifier" in a free-text box with a
 *      `vsv_…` placeholder. `app.is_exact_pin` (migration 154:70-75) refuses six
 *      literal words and accepts every other string, and
 *      `analysis_report_versions.presentation_version_id` has no foreign key — so
 *      a typed identifier was a pin the database would store and no read could
 *      ever resolve. The commit that left the box there said why:
 *      *"aucun endpoint de liste n'existe, un picker aurait invente une route"*.
 *      The route exists now.
 *
 *   2. the panel announced the Chart Template as unavailable and named story 72.1
 *      as its owner — after 72.1 landed. `visualization-and-rendering.md` makes
 *      that a failure condition in as many words: "the Reports screen announces
 *      as absent an object the deployment ships, or offers for pinning an object
 *      it does not ship". Nothing is asserted by the screen either way: it
 *      renders what the server's own probe reports.
 */
import { fireEvent, render, screen } from "@testing-library/react";

import { ReportWorkbench } from "../analyze-artifacts/Reports";

const PROJECT = "proj_EXAMPLE";

const BASE_VERSION = {
  id: "repv_EXAMPLE",
  version_number: 1,
  label: "Weekly traffic",
  query_spec_id: "qs_EXAMPLE",
  query_spec_version_id: "qsv_EXAMPLE",
  presentation: { kind: null, version_id: null, absent_literal: "No presentation pinned" },
  content_hash: "a".repeat(64),
  predecessor_version_id: null,
  created_by: "owner@example.com",
  created_at: "2026-09-01T09:00:00Z",
};

const DETAIL = {
  id: "rep_EXAMPLE",
  label: "Weekly traffic",
  description: null,
  seed_origin: "explore",
  seed: null,
  current_version_id: "repv_EXAMPLE",
  archived: false,
  archived_at: null,
  created_by: "owner@example.com",
  created_at: "2026-09-01T09:00:00Z",
  updated_at: "2026-09-01T09:00:00Z",
  current_version_number: 1,
  query_spec_version_id: "qsv_EXAMPLE",
  presentation_absent: "No presentation pinned",
  run_count: 0,
  versions: [BASE_VERSION],
  runs: [],
  // BOTH registries ship: the deployment names nothing unpinnable. This is what
  // `render_contract_state` actually answers since migration 333.
  presentation_contract: {
    available: true,
    missing: [],
    pinnable_kinds: ["visualization_spec_version", "visualization_template_version"],
    unpinnable_kinds: [],
  },
  pinnable_presentation_versions: {
    visualization_spec_version: [
      {
        version_id: "vsv_EXAMPLE",
        label: "Sessions by channel",
        version_number: 2,
        family: "bar",
        content_hash: "c".repeat(64),
        created_at: "2026-09-01T09:00:00Z",
        kind_noun: "Visualization Spec version",
      },
    ],
    visualization_template_version: [
      {
        version_id: "vtv_EXAMPLE",
        label: "Spend by channel",
        version_number: 1,
        family: "bar",
        content_hash: "d".repeat(64),
        created_at: "2026-09-01T09:00:00Z",
        kind_noun: "Chart Template version",
      },
    ],
  },
};

function response(body: unknown, status = 200): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response;
}

function mockApi(handlers: Array<[RegExp, (url: string, init?: RequestInit) => Response]>) {
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  const fetchMock = vi.fn((url: string, init?: RequestInit) => {
    calls.push({ url, init });
    for (const [pattern, produce] of handlers) {
      if (pattern.test(url)) return Promise.resolve(produce(url, init));
    }
    return Promise.resolve(response({ code: "not_found", message: "Not found" }, 404));
  });
  vi.stubGlobal("fetch", fetchMock);
  return { fetchMock, calls };
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it("offers a list of versions that resolve, and no box to type an identifier into", async () => {
  mockApi([[/\/analyze\/reports\/rep_EXAMPLE$/, () => response(DETAIL)]]);
  render(<ReportWorkbench collectionHref="/analyze/reports" projectId={PROJECT} reportId="rep_EXAMPLE" tab="presentation" />);

  const picker = await screen.findByTestId("report-presentation-version");
  expect(picker.tagName).toBe("SELECT");
  expect(screen.getByText(/Sessions by channel/)).toBeInTheDocument();
  // The free-text box is gone. A pin nobody verified is what it produced.
  expect(screen.queryByPlaceholderText(/vsv_/)).not.toBeInTheDocument();
});

it("pins the version the person chose, exactly", async () => {
  const { calls } = mockApi([
    [/\/analyze\/reports\/rep_EXAMPLE\/versions$/, () => response({ id: "repv_2" }, 201)],
    [/\/analyze\/reports\/rep_EXAMPLE$/, () => response(DETAIL)],
  ]);
  render(<ReportWorkbench collectionHref="/analyze/reports" projectId={PROJECT} reportId="rep_EXAMPLE" tab="presentation" />);

  fireEvent.change(await screen.findByTestId("report-presentation-version"), {
    target: { value: "vsv_EXAMPLE" },
  });
  fireEvent.click(screen.getByRole("button", { name: /Save as new version/ }));

  const write = calls.find((call) => call.url.endsWith("/versions"));
  expect(write).toBeTruthy();
  expect(JSON.parse(String(write?.init?.body))).toEqual({
    query_spec_version_id: "qsv_EXAMPLE",
    presentation: { kind: "visualization_spec_version", version_id: "vsv_EXAMPLE" },
  });
});

it("no longer says a Chart Template version cannot be pinned once the deployment ships one", async () => {
  mockApi([[/\/analyze\/reports\/rep_EXAMPLE$/, () => response(DETAIL)]]);
  render(<ReportWorkbench collectionHref="/analyze/reports" projectId={PROJECT} reportId="rep_EXAMPLE" tab="presentation" />);

  await screen.findByTestId("report-presentation-version");
  expect(screen.queryByText(/cannot be pinned here yet/)).not.toBeInTheDocument();
  const option = screen.getByRole("option", { name: "Chart Template version" });
  expect(option).not.toBeDisabled();
});

it("switching the kind drops the version chosen for the other kind", async () => {
  mockApi([[/\/analyze\/reports\/rep_EXAMPLE$/, () => response(DETAIL)]]);
  render(<ReportWorkbench collectionHref="/analyze/reports" projectId={PROJECT} reportId="rep_EXAMPLE" tab="presentation" />);

  const versionPicker = await screen.findByTestId("report-presentation-version");
  fireEvent.change(versionPicker, { target: { value: "vsv_EXAMPLE" } });
  fireEvent.change(screen.getByRole("combobox", { name: /Presentation kind/ }), {
    target: { value: "visualization_template_version" },
  });
  // A version id belongs to one kind. Carrying it across would compose a pin of
  // kind A on an identifier of kind B, resolvable by nothing.
  expect((await screen.findByTestId("report-presentation-version")).getAttribute("value")).not.toBe(
    "vsv_EXAMPLE",
  );
  expect(screen.getByText(/Spend by channel/)).toBeInTheDocument();
});

it("says the Project has none of a kind rather than opening a text box", async () => {
  mockApi([
    [
      /\/analyze\/reports\/rep_EXAMPLE$/,
      () =>
        response({
          ...DETAIL,
          pinnable_presentation_versions: {
            visualization_spec_version: [],
            visualization_template_version: [],
          },
        }),
    ],
  ]);
  render(<ReportWorkbench collectionHref="/analyze/reports" projectId={PROJECT} reportId="rep_EXAMPLE" tab="presentation" />);

  expect(
    await screen.findByText(/This Project has no Visualization Spec version/),
  ).toBeInTheDocument();
  expect(screen.queryByTestId("report-presentation-version")).not.toBeInTheDocument();
});

// ---------------------------------------------------------------------------
// AI-351 — an existing pin on an ARCHIVED Chart Template.
//
// The two facts are separate and both have to be on the screen: the pin still
// RESOLVES (archiving is a date on the head, and deletes nothing), and the
// template is no longer OFFERED for a new pin. A screen with only the first
// shows a retired template as if it were live; a screen with only the second
// makes a person think their Report broke.
// ---------------------------------------------------------------------------

const PINNED_TO_ARCHIVED = {
  ...DETAIL,
  presentation_absent: null,
  versions: [
    {
      ...BASE_VERSION,
      presentation: {
        kind: "visualization_template_version",
        version_id: "vtv_ARCHIVED",
        absent_literal: null,
        archived: true,
      },
    },
  ],
  //  The archived head is absent from the OFFER — that exclusion is
  //  `list_pinnable_presentation_versions`', measured against
  //  `t.archived_at IS NULL`, and proven in PostgreSQL. What this fixture
  //  reproduces is the shape the screen receives because of it.
  pinnable_presentation_versions: {
    ...DETAIL.pinnable_presentation_versions,
    visualization_template_version: [],
  },
};

it("says the pinned Chart Template is archived, and still shows the pin it resolves", async () => {
  mockApi([[/\/analyze\/reports\/rep_EXAMPLE$/, () => response(PINNED_TO_ARCHIVED)]]);
  render(<ReportWorkbench collectionHref="/analyze/reports" projectId={PROJECT} reportId="rep_EXAMPLE" tab="presentation" />);

  const said = await screen.findByTestId("report-presentation-archived");
  expect(said).toHaveTextContent(/resolves it exactly as before/);
  // The pin itself is still displayed. It was not hidden, replaced or emptied.
  expect(screen.getByText("vtv_ARCHIVED")).toBeInTheDocument();
});

it("does not offer an archived Chart Template for a NEW pin, and says which gesture brings it back", async () => {
  mockApi([[/\/analyze\/reports\/rep_EXAMPLE$/, () => response(PINNED_TO_ARCHIVED)]]);
  render(<ReportWorkbench collectionHref="/analyze/reports" projectId={PROJECT} reportId="rep_EXAMPLE" tab="presentation" />);

  await screen.findByTestId("report-presentation-archived");
  fireEvent.change(screen.getByDisplayValue("Visualization Spec version"), {
    target: { value: "visualization_template_version" },
  });

  // No picker for this kind: the one template of this Project is archived, so
  // there is nothing that could be pinned — stated as this Project having none,
  // never as a deployment that cannot resolve the kind.
  expect(screen.queryByTestId("report-presentation-version")).not.toBeInTheDocument();
  expect(screen.getByText(/This Project has no Chart Template version/)).toBeInTheDocument();
  // The pin that exists is untouched by any of that.
  expect(screen.getByText("vtv_ARCHIVED")).toBeInTheDocument();
});

it("says nothing about archiving for a pin that is not a Chart Template version", async () => {
  mockApi([
    [
      /\/analyze\/reports\/rep_EXAMPLE$/,
      () =>
        response({
          ...DETAIL,
          presentation_absent: null,
          versions: [
            {
              ...BASE_VERSION,
              presentation: {
                kind: "visualization_spec_version",
                version_id: "vsv_EXAMPLE",
                absent_literal: null,
                //  `null`, never `false`: the server measured nothing here, and a
                //  screen that read `false` as "live" would be asserting a
                //  liveness nobody checked.
                archived: null,
              },
            },
          ],
        }),
    ],
  ]);
  render(<ReportWorkbench collectionHref="/analyze/reports" projectId={PROJECT} reportId="rep_EXAMPLE" tab="presentation" />);

  expect(await screen.findByText("vsv_EXAMPLE")).toBeInTheDocument();
  expect(screen.queryByTestId("report-presentation-archived")).not.toBeInTheDocument();
});
