/**
 * The Analytics Explorer — story 66.7.
 *
 * Every response below is the shape the server modules of stories 66.2, 66.3,
 * 66.4/66.5 and 66.6 really return. What these tests assert is not that the
 * screen renders: it is that it renders the things a person has to be able to
 * tell apart, and refuses the four substitutions that would make it lie —
 *
 *   * an unreadable catalog shown as "nothing can be crossed";
 *   * three separate states merged into one badge;
 *   * a candidate offered as something you can run;
 *   * an empty pivot cell drawn as `0`.
 */
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";

import AnalyticsExplorer from "../analyze/explorer/AnalyticsExplorer";

const PROJECT = "proj_EXAMPLE";

function response(body: unknown, status = 200): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response;
}

function mockApi(handlers: Array<[RegExp, () => Response | Promise<Response>]>) {
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  const fetchMock = vi.fn((url: string, init?: RequestInit) => {
    calls.push({ url, init });
    for (const [pattern, produce] of handlers) {
      if (pattern.test(url)) return Promise.resolve(produce());
    }
    return Promise.resolve(response({ code: "not_found", message: "Not found" }, 404));
  });
  vi.stubGlobal("fetch", fetchMock);
  return { fetchMock, calls };
}

async function clickReadyRun() {
  const button = await screen.findByTestId("run-analysis");
  await waitFor(() => expect(button).toBeEnabled());
  fireEvent.click(button);
}

// --- the composition is made of moves --------------------------------------
//
// `visualization-and-rendering.md`, *Composition is direct manipulation, and the
// keyboard keeps parity*. Every helper below fires ordinary clicks and no drag
// event at all: if the composition ever depends on a drag, these go red, which
// is exactly what the parity clause is for.

/** Pick a field up where it is listed, then place it in a well. */
function placeInWell(fieldId: string, wellName: string) {
  fireEvent.click(screen.getByTestId(`field-${fieldId}`));
  fireEvent.click(screen.getByTestId(`well-${wellName}-place`));
}

/** Take a placed field back out. */
function removeFromWell(fieldId: string, wellName: string) {
  fireEvent.click(screen.getByTestId(`well-${wellName}-remove-${fieldId}`));
}

/** The fields a well holds, in order. */
function wellHolds(wellName: string): string[] {
  return Array.from(
    screen.getByTestId(`well-${wellName}`).querySelectorAll("[data-flip-id]"),
  ).map((node) => node.getAttribute("data-flip-id") ?? "");
}

/** Is this field offered anywhere in the composition — shelf or well? */
function fieldIsOffered(fieldId: string): boolean {
  return document.querySelector(`[data-field="${fieldId}"]`) !== null;
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

const GOVERNED = {
  kind: "governed",
  authority: "governed",
  observed_coverage: "unavailable",
  execution_safety: "ready",
  left: {
    datastream_id: "ds_a",
    name: "Campaign spend",
    mapping_version_id: "dmv_a",
    published_execution_id: "dse_a",
    output_version_id: "dov_a",
    measures: [{ canonical_field_id: "mdm_spend", name: "Spend", aggregation: "sum" }],
  },
  right: {
    datastream_id: "ds_b",
    name: "Conversions",
    mapping_version_id: "dmv_b",
    published_execution_id: "dse_b",
    output_version_id: "dov_b",
    measures: [
      { canonical_field_id: "mdm_conversions", name: "Conversions", aggregation: "sum" },
    ],
  },
  common_key: {
    id: "mck_1",
    name: "Day and Campaign",
    version_id: "mckv_1",
    version_number: 1,
    components: [{ canonical_field_id: "mdm_day", canonical_name: "day" }],
  },
  key_paths: [
    {
      canonical_name: "day",
      canonical_field_id: "mdm_day",
      left_field: "date",
      right_field: "event_date",
    },
  ],
  relationship: {
    relationship_name: "spend_to_conversions",
    cardinality: "many_to_one",
    fan_out_policy: "forbid",
    bridge_dataset: null,
    view_id: "sv_1",
    view_version_id: "svv_1",
    view_version_number: 1,
    business_domain_refs: ["bd_growth"],
  },
  unlocked_measures: 3,
  analysis: "Campaign spend and Conversions, by day.",
  explore_together: {
    datastreams: [
      {
        datastream_id: "ds_a",
        mapping_version_id: "dmv_a",
        published_execution_id: "dse_a",
        output_version_id: "dov_a",
      },
      {
        datastream_id: "ds_b",
        mapping_version_id: "dmv_b",
        published_execution_id: "dse_b",
        output_version_id: "dov_b",
      },
    ],
    common_key_version_id: "mckv_1",
    view_version_id: "svv_1",
    relationship_name: "spend_to_conversions",
  },
};

const THIRD_MATCH = {
  ...GOVERNED,
  common_key: {
    id: "mck_2",
    name: "Campaign",
    version_id: "mckv_2",
    version_number: 1,
    components: [{ canonical_field_id: "mdm_campaign", canonical_name: "campaign" }],
  },
  key_paths: [{
    canonical_name: "campaign",
    canonical_field_id: "mdm_campaign",
    left_field: "campaign_id",
    right_field: "campaign_id",
  }],
  left: GOVERNED.right,
  right: {
    datastream_id: "ds_c",
    name: "Attributed revenue",
    mapping_version_id: "dmv_c",
    published_execution_id: "dse_c",
    output_version_id: "dov_c",
    measures: [{ canonical_field_id: "mdm_revenue", name: "Revenue", aggregation: "sum" }],
  },
  relationship: {
    ...GOVERNED.relationship,
    relationship_name: "conversions_to_revenue",
  },
  explore_together: {
    ...GOVERNED.explore_together,
    datastreams: [
      GOVERNED.explore_together.datastreams[1],
      {
        datastream_id: "ds_c",
        mapping_version_id: "dmv_c",
        published_execution_id: "dse_c",
        output_version_id: "dov_c",
      },
    ],
    common_key_version_id: "mckv_2",
    relationship_name: "conversions_to_revenue",
  },
  analysis: "Conversions and Attributed revenue, by day.",
};

const FOURTH_MATCH = {
  ...THIRD_MATCH,
  left: THIRD_MATCH.right,
  right: {
    datastream_id: "ds_d",
    name: "CRM pipeline",
    mapping_version_id: "dmv_d",
    published_execution_id: "dse_d",
    output_version_id: "dov_d",
    measures: [{ canonical_field_id: "mdm_pipeline", name: "Pipeline", aggregation: "sum" }],
  },
  relationship: {
    ...GOVERNED.relationship,
    relationship_name: "revenue_to_pipeline",
  },
  explore_together: {
    ...GOVERNED.explore_together,
    datastreams: [
      THIRD_MATCH.explore_together.datastreams[1],
      {
        datastream_id: "ds_d",
        mapping_version_id: "dmv_d",
        published_execution_id: "dse_d",
        output_version_id: "dov_d",
      },
    ],
    relationship_name: "revenue_to_pipeline",
  },
  analysis: "Attributed revenue and CRM pipeline, by day.",
};

const ALTERNATE_MATCH = {
  ...GOVERNED,
  relationship: { ...GOVERNED.relationship, relationship_name: "spend_to_conversions_strict" },
  explore_together: {
    ...GOVERNED.explore_together,
    relationship_name: "spend_to_conversions_strict",
  },
};

const COMPILED = {
  query_spec_id: "qs_1",
  query_spec_version_id: "qsv_1",
  content_hash: "a".repeat(64),
  plan: {
    members: [
      {
        datastream_id: "ds_a",
        measures: [{ canonical_field_id: "mdm_spend", result_field: "m_mdm_spend" }],
      },
      {
        datastream_id: "ds_b",
        measures: [
          { canonical_field_id: "mdm_conversions", result_field: "m_mdm_conversions" },
        ],
      },
    ],
  },
};

const RESULT_EVIDENCE = {
  result_id: "qr_1",
  content_hash: "a".repeat(64),
  outcome: "success",
  row_count: 2,
  truncated: false,
  schema: {
    fields: [
      { name: "k_mdm_day" },
      { name: "m_mdm_spend" },
      { name: "m_mdm_conversions" },
    ],
  },
  manifest: { outcome: "success" },
  rows: [
    { k_mdm_day: "2026-08-01", m_mdm_spend: 18, m_mdm_conversions: 4 },
    { k_mdm_day: "2026-08-02", m_mdm_spend: null, m_mdm_conversions: 2 },
  ],
};

const CANDIDATE = {
  ...GOVERNED,
  kind: "candidate_key_missing",
  authority: "needs_governance",
  execution_safety: "review_required",
  common_key: null,
  relationship: null,
  explore_together: null,
  analysis: "Campaign spend and Conversions, by campaign_id.",
  next_action: "Declare a common key over those fields, then approve the relationship.",
};

function catalog(matches: unknown[]) {
  return {
    matches,
    counts: {
      datastreams_published: 2,
      governed: matches.filter((m) => (m as { authority: string }).authority === "governed").length,
      candidates: matches.filter((m) => (m as { authority: string }).authority !== "governed").length,
      returned: matches.length,
    },
    bounds: { max_datastreams_scanned: 200, max_matches: 50, truncated: false },
    observed_coverage_state: "unavailable",
    empty_reason: matches.length ? null : { code: "no_shared_identity", message: "Nothing is shared yet." },
  };
}

const PROFILE = {
  components: [{ canonical_field_id: "mdm_day", canonical_name: "day" }],
  left: {
    datastream_id: "ds_a",
    name: "Campaign spend",
    state: "exact",
    total_rows: 3,
    null_key_rows: 0,
    distinct_keys: 1,
    duplicate_state: "exact",
    duplicated_keys: 1,
    max_rows_per_key: 3,
  },
  right: {
    datastream_id: "ds_b",
    name: "Conversions",
    state: "exact",
    total_rows: 2,
    null_key_rows: 0,
    distinct_keys: 1,
    duplicate_state: "exact",
    duplicated_keys: 1,
    max_rows_per_key: 2,
  },
  matched: { state: "exact", matched_keys: 1, left_unmatched_keys: 0, right_unmatched_keys: 0 },
  multiplication: {
    state: "exact",
    worst_case_rows_per_key: 6,
    explanation: "One key can appear 3 time(s) on Campaign spend and 2 time(s) on Conversions.",
  },
  execution_safety: "unsafe",
};
const READY_PROFILE = {
  ...PROFILE,
  profile_receipt: "signed-profile-receipt",
  multiplication: {
    state: "exact",
    worst_case_rows_per_key: 1,
    explanation: "Each shared key is unique on at least one side.",
  },
  execution_safety: "ready",
};

// ---------------------------------------------------------------------------
// The catalog, and the two absences it must never confuse
// ---------------------------------------------------------------------------

it("says an unreadable catalog is unreadable, never 'nothing can be crossed'", async () => {
  mockApi([[/analyze\/matches$/, () => response({ code: "matches_unavailable", message: "The catalog of sources could not be read." }, 503)]]);
  render(<AnalyticsExplorer projectId={PROJECT} />);

  const banner = await screen.findByTestId("catalog-error");
  expect(banner).toHaveTextContent(/could not be read/i);
  expect(banner).toHaveTextContent(/not a count of zero/i);
  expect(screen.queryByTestId("useful-matches")).toBeNull();
});

it("names the gesture when there is genuinely nothing to cross", async () => {
  mockApi([[/analyze\/matches$/, () => response(catalog([]))]]);
  render(<AnalyticsExplorer projectId={PROJECT} />);

  expect(await screen.findByText(/Nothing is shared yet\./)).toBeInTheDocument();
});

it("states the three states separately and in words", async () => {
  mockApi([[/analyze\/matches$/, () => response(catalog([GOVERNED]))]]);
  render(<AnalyticsExplorer projectId={PROJECT} />);

  const states = await screen.findByTestId("match-states");
  expect(states).toHaveTextContent("Approved to run");
  // Coverage is NOT measured at this point, and the screen says so rather than
  // leaving the reader to assume the governed badge covered it.
  expect(states).toHaveTextContent("Not measured");
  expect(states).toHaveTextContent("Safe to run");
});

it("shows a candidate with the governing gesture and no way to run it", async () => {
  mockApi([[/analyze\/matches$/, () => response(catalog([CANDIDATE]))]]);
  render(<AnalyticsExplorer projectId={PROJECT} />);

  expect(await screen.findByText(/Declare a common key/)).toBeInTheDocument();
  expect(screen.queryByTestId("inspect-match")).toBeNull();
  expect(screen.queryByTestId("run-analysis")).toBeNull();
});

// ---------------------------------------------------------------------------
// Evidence before execution
// ---------------------------------------------------------------------------

it("puts the measured multiplication in front of the person before they run", async () => {
  mockApi([
    [/analyze\/matches\/profile/, () => response({ profile: PROFILE })],
    [/analyze\/matches$/, () => response(catalog([GOVERNED]))],
  ]);
  render(<AnalyticsExplorer projectId={PROJECT} />);

  fireEvent.click(await screen.findByTestId("inspect-match"));

  const explanation = await screen.findByTestId("multiplication-explanation");
  expect(explanation).toHaveTextContent(/3 time\(s\) on Campaign spend and 2 time\(s\)/);
  expect(explanation).toHaveTextContent("Unsafe as it stands");
  expect(screen.getByTestId("run-analysis")).toBeDisabled();
  const evidence = screen.getByTestId("match-evidence");
  expect(evidence).toHaveTextContent("3 rows, 1 keys");

  fireEvent.change(screen.getByLabelText("From"), { target: { value: "2026-08-01" } });
  expect(screen.getByTestId("run-analysis")).toBeEnabled();
  expect(screen.getByText(/3 time\(s\) on Campaign spend/)).toBeInTheDocument();
});

it("does not offer a date window when the governed key has no temporal component", async () => {
  const nonTemporal = {
    ...GOVERNED,
    common_key: THIRD_MATCH.common_key,
    key_paths: THIRD_MATCH.key_paths,
    explore_together: {
      ...GOVERNED.explore_together,
      common_key_version_id: "mckv_2",
    },
  };
  mockApi([
    [/analyze\/matches\/profile/, () => response({ profile: READY_PROFILE })],
    [/analyze\/matches$/, () => response(catalog([nonTemporal]))],
  ]);
  render(<AnalyticsExplorer projectId={PROJECT} />);

  fireEvent.click(await screen.findByTestId("inspect-match"));
  await screen.findByTestId("composition");
  expect(screen.queryByLabelText("Grain")).toBeNull();
  expect(screen.queryByLabelText("From")).toBeNull();
  expect(screen.queryByLabelText("To")).toBeNull();
  expect(screen.getByLabelText("Maximum Result rows")).toBeInTheDocument();
});

it("revalidates and opens the exact match carried from a Datastream address", async () => {
  mockApi([
    [/analyze\/matches\/profile/, () => response({ profile: PROFILE })],
    [/analyze\/matches$/, () => response(catalog([GOVERNED]))],
  ]);
  render(
    <AnalyticsExplorer
      projectId={PROJECT}
      initialMatch={{
        leftDatastreamId: "ds_a",
        rightDatastreamId: "ds_b",
        commonKeyVersionId: "mckv_1",
      }}
    />,
  );

  expect(await screen.findByTestId("composition")).toBeInTheDocument();
  expect(await screen.findByTestId("match-evidence")).toBeInTheDocument();
});

// ---------------------------------------------------------------------------
// `Explore together` hands over versions, and this screen executes THOSE
//
// data.md, *Which Datastreams can usefully be crossed*: the handoff opens the
// Explorer with "the exact Datastream and Output versions that were on the
// screen, not their current heads". This screen fetches its own catalog, so
// until the pins travelled in the address it re-resolved the pair here — which
// is the one thing that amendment forbids by name.
// ---------------------------------------------------------------------------

/** The pins as the Datastream Workbench composed them, one republication ago. */
const HANDED_PINS = {
  left: {
    datastreamId: "ds_a",
    mappingVersionId: "dmv_a_AS_READ",
    outputVersionId: "dov_a_AS_READ",
    publishedExecutionId: "dse_a_AS_READ",
  },
  right: {
    datastreamId: "ds_b",
    mappingVersionId: "dmv_b",
    outputVersionId: "dov_b",
    publishedExecutionId: "dse_b",
  },
};

function runnableApi(matches: unknown[]) {
  return mockApi([
    [/analyze\/matches\/profile/, () => response({ profile: READY_PROFILE })],
    [/analyze\/matches$/, () => response(catalog(matches))],
    [/multi-source\/plans\/qsv_1\/execute$/, () => response(
      { result: { result_id: "qr_1", outcome: "success", row_count: 1 } },
      201,
    )],
    [/multi-source\/plans$/, () => response(COMPILED, 201)],
    [/results\/qr_1\/evidence$/, () => response(RESULT_EVIDENCE)],
    [/results\/qr_1\/pivot$/, () => response({ pivot: {
      result_id: "qr_1", content_hash: "a".repeat(64), row_fields: [], column_fields: [],
      value_fields: [], row_keys: [], column_keys: [], cells: [],
      bounds: { max_cells: 20000, max_row_keys: 500, max_column_keys: 100,
        rows_truncated: false, columns_truncated: false, rows_dropped: 0,
        columns_dropped: 0, cells_truncated: false },
    } })],
  ]);
}

it("executes the versions the handoff pinned, never the ones its own catalog holds", async () => {
  const { calls } = runnableApi([GOVERNED]);
  render(
    <AnalyticsExplorer
      projectId={PROJECT}
      initialMatch={{
        leftDatastreamId: "ds_a",
        rightDatastreamId: "ds_b",
        commonKeyVersionId: "mckv_1",
        pins: HANDED_PINS,
      }}
    />,
  );

  await screen.findByTestId("composition");
  await clickReadyRun();
  await waitFor(() => expect(screen.getByTestId("result-identity")).toHaveTextContent("qr_1"));

  const compileCall = calls.find((call) => call.url.endsWith("/multi-source/plans"));
  const members = JSON.parse(String(compileCall?.init?.body)).members;
  expect(members).toEqual([
    expect.objectContaining({
      datastream_id: "ds_a",
      mapping_version_id: "dmv_a_AS_READ",
      output_version_id: "dov_a_AS_READ",
      published_execution_id: "dse_a_AS_READ",
    }),
    expect.objectContaining({
      datastream_id: "ds_b",
      mapping_version_id: "dmv_b",
      output_version_id: "dov_b",
      published_execution_id: "dse_b",
    }),
  ]);
  // The catalog's own values for the left side are what a re-resolution would
  // have sent. Neither may appear anywhere in the request.
  expect(String(compileCall?.init?.body)).not.toContain("dmv_a\"");
  expect(String(compileCall?.init?.body)).not.toContain("dov_a\"");
});

it("says the pinned versions have been superseded instead of quietly following the head", async () => {
  runnableApi([GOVERNED]);
  render(
    <AnalyticsExplorer
      projectId={PROJECT}
      initialMatch={{
        leftDatastreamId: "ds_a",
        rightDatastreamId: "ds_b",
        commonKeyVersionId: "mckv_1",
        pins: HANDED_PINS,
      }}
    />,
  );

  const drift = await screen.findByTestId("pinned-versions-superseded");
  expect(drift).toHaveTextContent("Campaign spend");
  expect(drift).toHaveTextContent(/mapping version dmv_a_AS_READ was read; dmv_a is published now/);
  expect(drift).toHaveTextContent(/Output version dov_a_AS_READ was read; dov_a is published now/);
  // The right side agrees with the catalog, so it is not named as drifted.
  expect(drift).not.toHaveTextContent("Conversions");
  // And the gesture out of it is offered, not taken.
  expect(screen.getByTestId("read-current-versions")).toBeInTheDocument();
});

it("says nothing about versions when the catalog still agrees with the address", async () => {
  runnableApi([GOVERNED]);
  render(
    <AnalyticsExplorer
      projectId={PROJECT}
      initialMatch={{
        leftDatastreamId: "ds_a",
        rightDatastreamId: "ds_b",
        commonKeyVersionId: "mckv_1",
        pins: {
          left: {
            datastreamId: "ds_a",
            mappingVersionId: "dmv_a",
            outputVersionId: "dov_a",
            publishedExecutionId: "dse_a",
          },
          right: HANDED_PINS.right,
        },
      }}
    />,
  );

  await screen.findByTestId("composition");
  expect(screen.queryByTestId("pinned-versions-superseded")).toBeNull();
});

it("moves off a superseded pin only when a person asks for what is published now", async () => {
  const { calls } = runnableApi([GOVERNED]);
  render(
    <AnalyticsExplorer
      projectId={PROJECT}
      initialMatch={{
        leftDatastreamId: "ds_a",
        rightDatastreamId: "ds_b",
        commonKeyVersionId: "mckv_1",
        pins: HANDED_PINS,
      }}
    />,
  );

  fireEvent.click(await screen.findByTestId("read-current-versions"));
  await waitFor(() => expect(screen.queryByTestId("pinned-versions-superseded")).toBeNull());
  await clickReadyRun();
  await waitFor(() => expect(screen.getByTestId("result-identity")).toHaveTextContent("qr_1"));

  const compileCall = calls.find((call) => call.url.endsWith("/multi-source/plans"));
  const members = JSON.parse(String(compileCall?.init?.body)).members;
  expect(members[0]).toMatchObject({
    datastream_id: "ds_a",
    mapping_version_id: "dmv_a",
    output_version_id: "dov_a",
  });
});

it("keeps a failed measurement from reading as a measured zero", async () => {
  mockApi([
    [/analyze\/matches\/profile/, () => response({ code: "no_published_output", message: "Conversions has never produced an output." }, 422)],
    [/analyze\/matches$/, () => response(catalog([GOVERNED]))],
  ]);
  render(<AnalyticsExplorer projectId={PROJECT} />);

  fireEvent.click(await screen.findByTestId("inspect-match"));
  expect(await screen.findByText(/never produced an output/)).toBeInTheDocument();
  expect(screen.queryByTestId("match-evidence")).toBeNull();
});

// ---------------------------------------------------------------------------
// Analytical edit versus presentation edit
// ---------------------------------------------------------------------------

it("says that changing the inclusion policy produces a new Result", async () => {
  mockApi([
    [/analyze\/matches\/profile/, () => response({ profile: READY_PROFILE })],
    [/analyze\/matches$/, () => response(catalog([GOVERNED]))],
  ]);
  render(<AnalyticsExplorer projectId={PROJECT} />);
  fireEvent.click(await screen.findByTestId("inspect-match"));

  expect(await screen.findByTestId("inclusion-policy")).toBeInTheDocument();
  expect(screen.getByText(/analytical choice: changing it produces a new Result/i)).toBeInTheDocument();
});

it("compiles then executes, and never a single endpoint doing both", async () => {
  const matrix = {
    result_id: "qr_1",
    content_hash: "a".repeat(64),
    row_fields: ["k_mdm_day"],
    column_fields: [],
    value_fields: [
      { name: "m_mdm_spend", canonical_field_id: "mdm_spend", role: "measure" },
    ],
    row_keys: [["2026-08-01"]],
    column_keys: [[]],
    cells: [
      {
        row_key: ["2026-08-01"],
        column_key: [],
        values: { m_mdm_spend: { value: 18 } },
        contributing_rows: 1,
        contributing_row_indexes: [0],
      },
    ],
    row_subtotals: [
      { row_key: ["2026-08-01"], values: { m_mdm_spend: { value: 18 } } },
    ],
    column_subtotals: [{ column_key: [], values: { m_mdm_spend: { value: 18 } } }],
    grand_total: {
      policy: "both",
      by_column_key: [{ column_key: [], values: { m_mdm_spend: { value: 18 } } }],
      by_row_key: [{ row_key: ["2026-08-01"], values: { m_mdm_spend: { value: 18 } } }],
      overall: { m_mdm_spend: { value: 18 } },
    },
    bounds: {
      max_cells: 20000,
      max_row_keys: 500,
      max_column_keys: 100,
      rows_truncated: false,
      columns_truncated: false,
      rows_dropped: 0,
      columns_dropped: 0,
      cells_truncated: false,
    },
  };
  const { calls } = mockApi([
    [/analyze\/matches\/profile/, () => response({ profile: READY_PROFILE })],
    [/analyze\/matches$/, () => response(catalog([GOVERNED]))],
    [/multi-source\/plans\/qsv_1\/execute$/, () => response({ result: { result_id: "qr_1", outcome: "success", row_count: 1 } }, 201)],
    [/multi-source\/plans$/, () => response(COMPILED, 201)],
    [/results\/qr_1\/evidence$/, () => response(RESULT_EVIDENCE)],
    [/results\/qr_1\/pivot$/, () => response({ pivot: matrix })],
  ]);
  render(<AnalyticsExplorer projectId={PROJECT} />);
  fireEvent.click(await screen.findByTestId("inspect-match"));
  await clickReadyRun();

  await waitFor(() => expect(screen.getByTestId("result-identity")).toHaveTextContent("qr_1"));
  const posted = calls.filter((call) => call.init?.method === "POST").map((call) => call.url);
  expect(posted.some((url) => url.endsWith("/multi-source/plans"))).toBe(true);
  expect(posted.some((url) => url.endsWith("/execute"))).toBe(true);
  const compileCall = calls.find((call) => call.url.endsWith("/multi-source/plans"));
  const compiledRequest = JSON.parse(String(compileCall?.init?.body));
  expect(String(compileCall?.init?.body)).toContain("mdm_spend");
  expect(String(compileCall?.init?.body)).toContain("mdm_conversions");
  expect(compiledRequest.row_limit).toBe(10000);
  expect(compiledRequest.profile_receipts).toEqual(["signed-profile-receipt"]);
  expect(compiledRequest.pivot).toMatchObject({
    rows: [{ canonical_field_id: "mdm_day" }],
    values: [
      { datastream_id: "ds_a", canonical_field_id: "mdm_spend" },
      { datastream_id: "ds_b", canonical_field_id: "mdm_conversions" },
    ],
    row_sort: "asc",
  });
  const pivotCall = calls.find((call) => call.url.endsWith("/results/qr_1/pivot"));
  expect(String(pivotCall?.init?.body)).toContain("m_mdm_spend");
  expect(screen.getByText(/Row total/)).toBeInTheDocument();
  expect(screen.getByText(/Grand total.*18/)).toBeInTheDocument();
});

it("composes a governed four-source tree and sends every oriented MDM edge", async () => {
  let profileCalls = 0;
  const { calls } = mockApi([
    [/analyze\/matches\/profile/, () => response({
      profile: { ...READY_PROFILE, profile_receipt: `signed-edge-${++profileCalls}` },
    })],
    [/analyze\/matches$/, () => response(catalog([GOVERNED, THIRD_MATCH, FOURTH_MATCH]))],
    [/multi-source\/plans\/qsv_1\/execute$/, () => response({
      result: { result_id: "qr_1", outcome: "success", row_count: 2 },
    }, 201)],
    [/multi-source\/plans$/, () => response({
      ...COMPILED,
      plan: {
        members: [
          ...COMPILED.plan.members,
          { datastream_id: "ds_c", measures: [
            { canonical_field_id: "mdm_revenue", result_field: "m_mdm_revenue" },
          ] },
          { datastream_id: "ds_d", measures: [
            { canonical_field_id: "mdm_pipeline", result_field: "m_mdm_pipeline" },
          ] },
        ],
      },
    }, 201)],
    [/results\/qr_1\/evidence$/, () => response(RESULT_EVIDENCE)],
    [/results\/qr_1\/pivot$/, () => response({ pivot: {
      result_id: "qr_1", content_hash: "a".repeat(64), row_fields: ["k_mdm_day"],
      column_fields: [], value_fields: [], row_keys: [], column_keys: [], cells: [],
      bounds: { max_cells: 20000, max_row_keys: 500, max_column_keys: 100,
        rows_truncated: false, columns_truncated: false, rows_dropped: 0,
        columns_dropped: 0, cells_truncated: false },
    } })],
  ]);
  render(<AnalyticsExplorer projectId={PROJECT} />);

  fireEvent.click((await screen.findAllByTestId("inspect-match"))[0]);
  fireEvent.click(await screen.findByTestId("add-source"));
  await waitFor(() => expect(screen.getByTestId("selected-source-graph")).toHaveTextContent(
    /3 connected Datastreams.*Conversions.*Attributed revenue/s,
  ));
  expect(fieldIsOffered("mdm_campaign")).toBe(true);
  fireEvent.click(await screen.findByTestId("add-source"));
  await waitFor(() => expect(screen.getByTestId("selected-source-graph")).toHaveTextContent(
    /4 connected Datastreams.*Attributed revenue.*CRM pipeline/s,
  ));
  await clickReadyRun();

  const compileCall = calls.find((call) => call.url.endsWith("/multi-source/plans"));
  const body = JSON.parse(String(compileCall?.init?.body));
  expect(body.members.map((member: { datastream_id: string }) => member.datastream_id))
    .toEqual(["ds_a", "ds_b", "ds_c", "ds_d"]);
  expect(body.edges).toEqual([
    expect.objectContaining({ left: "ds_a", right: "ds_b", relationship_name: "spend_to_conversions" }),
    expect.objectContaining({ left: "ds_b", right: "ds_c", common_key_version_id: "mckv_2", relationship_name: "conversions_to_revenue" }),
    expect.objectContaining({ left: "ds_c", right: "ds_d", relationship_name: "revenue_to_pipeline" }),
  ]);
  expect(body.profile_receipts).toEqual(["signed-edge-1", "signed-edge-2", "signed-edge-3"]);
  expect(body.pivot.values).toContainEqual({ datastream_id: "ds_c", canonical_field_id: "mdm_revenue" });
  expect(body.pivot.values).toContainEqual({ datastream_id: "ds_d", canonical_field_id: "mdm_pipeline" });
});

it("allows only one connected-source measurement at a time", async () => {
  let profileCalls = 0;
  let resolveAddedProfile!: (value: Response) => void;
  const addedProfile = new Promise<Response>((resolve) => {
    resolveAddedProfile = resolve;
  });
  mockApi([
    [/analyze\/matches\/profile/, () => {
      profileCalls += 1;
      return profileCalls === 1
        ? response({ profile: READY_PROFILE })
        : addedProfile;
    }],
    [/analyze\/matches$/, () => response(catalog([GOVERNED, THIRD_MATCH]))],
  ]);
  render(<AnalyticsExplorer projectId={PROJECT} />);

  fireEvent.click((await screen.findAllByTestId("inspect-match"))[0]);
  const add = await screen.findByTestId("add-source");
  fireEvent.click(add);
  await waitFor(() => expect(add).toBeDisabled());
  expect(add).toHaveTextContent("Measuring the network connection");
  fireEvent.click(add);
  expect(profileCalls).toBe(2);

  await act(async () => {
    resolveAddedProfile(response({
      profile: { ...READY_PROFILE, profile_receipt: "signed-edge-2" },
    }));
  });
  await waitFor(() => expect(screen.getByTestId("selected-source-graph")).toHaveTextContent(
    "3 connected Datastreams",
  ));
  expect(profileCalls).toBe(2);
});

it("does not edit the graph before its retained primary edge is measured", async () => {
  let resolvePrimaryProfile!: (value: Response) => void;
  const primaryProfile = new Promise<Response>((resolve) => {
    resolvePrimaryProfile = resolve;
  });
  mockApi([
    [/analyze\/matches\/profile/, () => primaryProfile],
    [/analyze\/matches$/, () => response(catalog([GOVERNED, THIRD_MATCH]))],
  ]);
  render(<AnalyticsExplorer projectId={PROJECT} />);

  fireEvent.click((await screen.findAllByTestId("inspect-match"))[0]);
  const add = await screen.findByTestId("add-source");
  expect(add).toBeDisabled();

  await act(async () => resolvePrimaryProfile(response({ profile: READY_PROFILE })));
  await waitFor(() => expect(add).toBeEnabled());
});

it("keeps a competing relationship selectable instead of merging its identity", async () => {
  mockApi([
    [/analyze\/matches\/profile/, () => response({ profile: READY_PROFILE })],
    [/analyze\/matches$/, () => response(catalog([GOVERNED, ALTERNATE_MATCH]))],
  ]);
  render(<AnalyticsExplorer projectId={PROJECT} />);

  fireEvent.click((await screen.findAllByTestId("inspect-match"))[0]);
  expect((await screen.findAllByText(/spend_to_conversions_strict/)).length).toBeGreaterThan(0);
  expect(screen.getByTestId("inspect-match")).toBeEnabled();
});

it("switches one edge only after its exact alternative is freshly profiled", async () => {
  let profileCalls = 0;
  const { calls } = mockApi([
    [/analyze\/matches\/profile/, () => response({
      profile: {
        ...READY_PROFILE,
        profile_receipt: `signed-relationship-${++profileCalls}`,
      },
    })],
    [/analyze\/matches$/, () => response(catalog([GOVERNED, ALTERNATE_MATCH]))],
    [/multi-source\/plans\/qsv_1\/execute$/, () => response({
      result: { result_id: "qr_1", outcome: "success", row_count: 1 },
    }, 201)],
    [/multi-source\/plans$/, () => response(COMPILED, 201)],
    [/results\/qr_1\/evidence$/, () => response(RESULT_EVIDENCE)],
    [/results\/qr_1\/pivot$/, () => response({ pivot: {
      result_id: "qr_1", content_hash: "a".repeat(64), row_fields: [],
      column_fields: [], value_fields: [], row_keys: [], column_keys: [], cells: [],
      bounds: { max_cells: 20000, max_row_keys: 500, max_column_keys: 100,
        rows_truncated: false, columns_truncated: false, rows_dropped: 0,
        columns_dropped: 0, cells_truncated: false },
    } })],
  ]);
  render(<AnalyticsExplorer projectId={PROJECT} />);

  fireEvent.click((await screen.findAllByTestId("inspect-match"))[0]);
  const relationship = await screen.findByLabelText(
    "Relationship for Campaign spend to Conversions",
  );
  fireEvent.change(relationship, {
    target: { value: "ds_a:ds_b:mckv_1:svv_1:spend_to_conversions_strict" },
  });
  await waitFor(() => expect(screen.getByLabelText(
    "Relationship for Campaign spend to Conversions",
  )).toHaveValue("ds_a:ds_b:mckv_1:svv_1:spend_to_conversions_strict"));
  expect(profileCalls).toBe(2);

  await clickReadyRun();
  const compileCall = calls.find((call) => call.url.endsWith("/multi-source/plans"));
  const body = JSON.parse(String(compileCall?.init?.body));
  expect(body.edges[0].relationship_name).toBe("spend_to_conversions_strict");
  expect(body.profile_receipts).toEqual(["signed-relationship-2"]);
});

it("keeps the current edge when profiling its alternative is refused", async () => {
  let profileCalls = 0;
  mockApi([
    [/analyze\/matches\/profile/, () => {
      profileCalls += 1;
      return profileCalls === 1
        ? response({ profile: READY_PROFILE })
        : response({ code: "profile_unavailable", message: "Could not measure this path." }, 422);
    }],
    [/analyze\/matches$/, () => response(catalog([GOVERNED, ALTERNATE_MATCH]))],
  ]);
  render(<AnalyticsExplorer projectId={PROJECT} />);

  fireEvent.click((await screen.findAllByTestId("inspect-match"))[0]);
  const relationship = await screen.findByLabelText(
    "Relationship for Campaign spend to Conversions",
  );
  fireEvent.change(relationship, {
    target: { value: "ds_a:ds_b:mckv_1:svv_1:spend_to_conversions_strict" },
  });

  expect(await screen.findByText(/Could not measure this path/)).toBeInTheDocument();
  expect(relationship).toHaveValue("ds_a:ds_b:mckv_1:svv_1:spend_to_conversions");
});

it("removes one named added leaf and only its owned fields", async () => {
  let profileCalls = 0;
  mockApi([
    [/analyze\/matches\/profile/, () => response({
      profile: { ...READY_PROFILE, profile_receipt: `signed-edge-${++profileCalls}` },
    })],
    [/analyze\/matches$/, () => response(catalog([GOVERNED, THIRD_MATCH]))],
  ]);
  render(<AnalyticsExplorer projectId={PROJECT} />);

  fireEvent.click((await screen.findAllByTestId("inspect-match"))[0]);
  fireEvent.click(await screen.findByTestId("add-source"));
  await waitFor(() => expect(screen.getByTestId("selected-source-graph")).toHaveTextContent(
    "3 connected Datastreams",
  ));
  // The selected match seeded Rows with its only common-key component; the
  // added source brings `campaign`, and it is placed on the other axis.
  expect(wellHolds("rows")).toEqual(["mdm_day"]);
  placeInWell("mdm_campaign", "columns");
  fireEvent.change(screen.getByLabelText("Field"), { target: { value: "mdm_campaign" } });

  fireEvent.click(screen.getByTestId("remove-leaf-ds_c"));

  expect(screen.getByTestId("selected-source-graph")).toHaveTextContent("2 connected Datastreams");
  expect(screen.getByTestId("selected-source-graph")).not.toHaveTextContent("Attributed revenue");
  expect(fieldIsOffered("mdm_campaign")).toBe(false);
  expect(wellHolds("rows")).toEqual(["mdm_day"]);
  expect(wellHolds("columns")).toEqual([]);
  expect(screen.getByLabelText("Field")).toHaveValue("");
  expect(screen.queryByRole("checkbox", { name: /Revenue/ })).not.toBeInTheDocument();
});

it("promotes the retained edge with its own receipt when the root leaf is removed", async () => {
  let profileCalls = 0;
  const { calls } = mockApi([
    [/analyze\/matches\/profile/, () => response({
      profile: { ...READY_PROFILE, profile_receipt: `signed-edge-${++profileCalls}` },
    })],
    [/analyze\/matches$/, () => response(catalog([GOVERNED, THIRD_MATCH]))],
    [/multi-source\/plans\/qsv_1\/execute$/, () => response({
      result: { result_id: "qr_1", outcome: "success", row_count: 1 },
    }, 201)],
    [/multi-source\/plans$/, () => response(COMPILED, 201)],
    [/results\/qr_1\/evidence$/, () => response(RESULT_EVIDENCE)],
    [/results\/qr_1\/pivot$/, () => response({ pivot: {
      result_id: "qr_1", content_hash: "a".repeat(64), row_fields: [],
      column_fields: [], value_fields: [], row_keys: [], column_keys: [], cells: [],
      bounds: { max_cells: 20000, max_row_keys: 500, max_column_keys: 100,
        rows_truncated: false, columns_truncated: false, rows_dropped: 0,
        columns_dropped: 0, cells_truncated: false },
    } })],
  ]);
  render(<AnalyticsExplorer projectId={PROJECT} />);

  fireEvent.click((await screen.findAllByTestId("inspect-match"))[0]);
  fireEvent.click(await screen.findByTestId("add-source"));
  await screen.findByTestId("remove-leaf-ds_a");
  fireEvent.click(screen.getByTestId("remove-leaf-ds_a"));
  expect(screen.getByTestId("selected-source-graph")).toHaveTextContent(
    /2 connected Datastreams.*Conversions \(Primary\).*Attributed revenue/s,
  );

  await clickReadyRun();
  const compileCall = calls.find((call) => call.url.endsWith("/multi-source/plans"));
  const body = JSON.parse(String(compileCall?.init?.body));
  expect(body.members.map((member: { datastream_id: string }) => member.datastream_id))
    .toEqual(["ds_b", "ds_c"]);
  expect(body.edges).toEqual([
    expect.objectContaining({
      left: "ds_b", right: "ds_c", relationship_name: "conversions_to_revenue",
    }),
  ]);
  expect(body.profile_receipts).toEqual(["signed-edge-2"]);
});

it("removes fields owned only by a removed source edge", async () => {
  let profileCalls = 0;
  mockApi([
    [/analyze\/matches\/profile/, () => response({
      profile: { ...READY_PROFILE, profile_receipt: `signed-edge-${++profileCalls}` },
    })],
    [/analyze\/matches$/, () => response(catalog([GOVERNED, THIRD_MATCH]))],
  ]);
  render(<AnalyticsExplorer projectId={PROJECT} />);

  fireEvent.click((await screen.findAllByTestId("inspect-match"))[0]);
  fireEvent.click(await screen.findByTestId("add-source"));
  await waitFor(() => expect(fieldIsOffered("mdm_campaign")).toBe(true));
  removeFromWell("mdm_day", "rows");
  placeInWell("mdm_campaign", "rows");
  fireEvent.change(screen.getByLabelText("Field"), { target: { value: "mdm_campaign" } });
  fireEvent.click(screen.getByRole("button", { name: "Return to the first two sources" }));

  expect(fieldIsOffered("mdm_campaign")).toBe(false);
  // Returning to the first two sources re-seeds the axes from THEIR common
  // key, so Rows holds `day` again -- not the field the removed edge owned.
  expect(wellHolds("rows")).toEqual(["mdm_day"]);
  expect(screen.getByLabelText("Field")).toHaveValue("mdm_day");
});

it("locks graph edits while a Result execution is in flight", async () => {
  let resolveCompile!: (value: Response) => void;
  const compile = new Promise<Response>((resolve) => {
    resolveCompile = resolve;
  });
  mockApi([
    [/analyze\/matches\/profile/, () => response({ profile: READY_PROFILE })],
    [/analyze\/matches$/, () => response(catalog([GOVERNED, THIRD_MATCH]))],
    [/multi-source\/plans$/, () => compile],
  ]);
  render(<AnalyticsExplorer projectId={PROJECT} />);

  fireEvent.click((await screen.findAllByTestId("inspect-match"))[0]);
  await clickReadyRun();
  await waitFor(() => expect(screen.getByTestId("add-source")).toBeDisabled());
  expect(screen.getByTestId("cancel-analysis")).toBeInTheDocument();
  fireEvent.click(screen.getByTestId("cancel-analysis"));
  await act(async () => resolveCompile(response(COMPILED, 201)));
});

it("turns a preflight refusal into a concrete narrowing action", async () => {
  mockApi([
    [/analyze\/matches\/profile/, () => response({ profile: READY_PROFILE })],
    [/analyze\/matches$/, () => response(catalog([GOVERNED]))],
    [/multi-source\/plans$/, () => response({
      code: "unsafe_fan_out",
      message: "The observed fan-out is unsafe.",
      detail: { worst_case_rows_per_key: 12 },
    }, 422)],
  ]);
  render(<AnalyticsExplorer projectId={PROJECT} />);

  fireEvent.click(await screen.findByTestId("inspect-match"));
  await clickReadyRun();
  expect(await screen.findByTestId("run-error")).toHaveTextContent(
    "Choose another approved matching path, or narrow the date window",
  );
});

it("pins the exact business question and requested Skill into the Result plan", async () => {
  const frozenContext = {
    contract_version: "analysis-context.v1",
    semantic_view_version_id: "svv_1",
    business_domain: {
      id: "bd_growth", version_number: 4, version_id: "bd_growth:4", name: "Growth",
    },
    golden_question: {
      id: "gq_1", version_id: "gqv_3", version_number: 3,
      title: "Revenue by campaign", content_hash: "b".repeat(64),
    },
    requested_skills: [
      {
        procedure_id: "proc_paid", version_number: 7,
        version_id: "proc_paid@7", name: "Paid media investigation",
      },
      {
        procedure_id: "proc_quality", version_number: 2,
        version_id: "proc_quality@2", name: "Quality investigation",
      },
    ],
  };
  const { calls } = mockApi([
    [/context\/business-taxonomy\?project_id=/, () => response({ domains: [
      { id: "bd_growth", name: "Growth", version_number: 5 },
      { id: "bd_other", name: "Unrelated", version_number: 2 },
    ] })],
    [/golden-questions\?lifecycle=active&limit=100$/, () => response({ golden_questions: [{
      id: "gq_1", title: "Revenue by campaign", current_version_id: "gqv_3",
      current_version: {
        version_number: 3, business_domain_id: "bd_growth",
        business_domain_version_number: 4, semantic_view_version_id: "svv_1",
      },
    }] })],
    [/context\/procedures\?project_id=/, () => response({ procedures: [
      { id: "proc_paid", name: "Paid media investigation", version_number: 7 },
      { id: "proc_quality", name: "Quality investigation", version_number: 2 },
    ] })],
    [/analyze\/matches\/profile/, () => response({ profile: READY_PROFILE })],
    [/analyze\/matches$/, () => response(catalog([GOVERNED]))],
    [/multi-source\/plans\/qsv_1\/execute$/, () => response({
      result: { result_id: "qr_1", outcome: "success", row_count: 1 },
    }, 201)],
    [/multi-source\/plans$/, () => response({
      ...COMPILED, plan: { ...COMPILED.plan, analysis_context: frozenContext },
    }, 201)],
    [/results\/qr_1\/evidence$/, () => response(RESULT_EVIDENCE)],
    [/results\/qr_1\/pivot$/, () => response({ pivot: {
      result_id: "qr_1", content_hash: "a".repeat(64), row_fields: [],
      column_fields: [], value_fields: [], row_keys: [], column_keys: [], cells: [],
      bounds: { max_cells: 20000, max_row_keys: 500, max_column_keys: 100,
        rows_truncated: false, columns_truncated: false, rows_dropped: 0,
        columns_dropped: 0, cells_truncated: false },
    } })],
  ]);
  render(<AnalyticsExplorer projectId={PROJECT} />);
  fireEvent.click(await screen.findByTestId("inspect-match"));
  await waitFor(() => expect(screen.getByLabelText("Business Domain")).toHaveValue(""));
  expect(screen.queryByRole("option", { name: "Unrelated · v2" })).not.toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("Business Domain"), {
    target: { value: "bd_growth@4" },
  });
  await waitFor(() => expect(screen.getByLabelText("Golden Question")).toHaveValue(""));
  fireEvent.change(screen.getByLabelText("Golden Question"), { target: { value: "gqv_3" } });
  fireEvent.change(screen.getByLabelText("Business Domain"), { target: { value: "" } });
  expect(screen.getByLabelText("Golden Question")).toHaveValue("");
  fireEvent.change(screen.getByLabelText("Business Domain"), {
    target: { value: "bd_growth@4" },
  });
  fireEvent.change(screen.getByLabelText("Golden Question"), { target: { value: "gqv_3" } });
  fireEvent.click(screen.getByRole("checkbox", { name: /Paid media investigation/ }));
  fireEvent.click(screen.getByRole("checkbox", { name: /Quality investigation/ }));
  expect(screen.getByText("2 of 8 selected")).toBeInTheDocument();
  //  NO IDENTIFIER WHERE A NAME BELONGS. This line printed `svv_1` -- the
  //  Semantic View version ULID -- next to two things that have names. The
  //  relation has one, so the relation is what is named.
  expect(screen.getByTestId("selected-business-domain")).toHaveTextContent(
    /Growth.*v4.*spend_to_conversions relationship, Semantic View v1\./,
  );
  expect(screen.getByTestId("selected-business-domain")).not.toHaveTextContent("svv_1");
  await clickReadyRun();

  const compileCall = calls.find((call) => call.url.endsWith("/multi-source/plans"));
  expect(JSON.parse(String(compileCall?.init?.body)).analysis_context).toEqual({
    business_domain_id: "bd_growth",
    business_domain_version_number: 4,
    golden_question_version_id: "gqv_3",
    skill_version_ids: ["proc_paid@7", "proc_quality@2"],
  });
  expect(await screen.findByTestId("pinned-analysis-context")).toHaveTextContent(
    /Growth.*Revenue by campaign.*Paid media investigation.*Quality investigation/,
  );
});

it("clears view-owned pins when another governed match is inspected", async () => {
  const other = {
    ...GOVERNED,
    analysis: "Campaign spend and Conversions, by another governed view.",
    right: {
      ...GOVERNED.right,
      datastream_id: "ds_c",
      mapping_version_id: "dmv_c",
      published_execution_id: "dse_c",
      output_version_id: "dov_c",
    },
    relationship: {
      ...GOVERNED.relationship,
      view_id: "sv_2",
      view_version_id: "svv_2",
      business_domain_refs: ["bd_other"],
    },
    explore_together: {
      ...GOVERNED.explore_together,
      datastreams: [
        GOVERNED.explore_together.datastreams[0],
        {
          datastream_id: "ds_c",
          mapping_version_id: "dmv_c",
          published_execution_id: "dse_c",
          output_version_id: "dov_c",
        },
      ],
      view_version_id: "svv_2",
      relationship_name: "other_relationship",
    },
  };
  mockApi([
    [/context\/business-taxonomy\?project_id=/, () => response({ domains: [
      { id: "bd_growth", name: "Growth", version_number: 4 },
      { id: "bd_other", name: "Other", version_number: 1 },
    ] })],
    [/golden-questions\?lifecycle=active&limit=100$/, () => response({
      golden_questions: [{
        id: "gq_1", title: "Revenue by campaign", current_version_id: "gqv_3",
        current_version: {
          version_number: 3, business_domain_id: "bd_growth",
          business_domain_version_number: 4, semantic_view_version_id: "svv_1",
        },
      }],
    })],
    [/context\/procedures\?project_id=/, () => response({ procedures: [] })],
    [/analyze\/matches\/profile/, () => response({ profile: READY_PROFILE })],
    [/analyze\/matches$/, () => response(catalog([GOVERNED, other]))],
  ]);
  render(<AnalyticsExplorer projectId={PROJECT} />);

  fireEvent.click((await screen.findAllByTestId("inspect-match"))[0]);
  await waitFor(() => expect(screen.getByLabelText("Business Domain")).toBeInTheDocument());
  fireEvent.change(screen.getByLabelText("Business Domain"), {
    target: { value: "bd_growth@4" },
  });
  fireEvent.change(screen.getByLabelText("Golden Question"), { target: { value: "gqv_3" } });

  fireEvent.click(screen.getByTestId("inspect-match"));

  expect(screen.getByLabelText("Business Domain")).toHaveValue("");
  expect(screen.getByLabelText("Golden Question")).toHaveValue("");
  await waitFor(() => expect(screen.getByTestId("run-analysis")).toBeEnabled());
  expect(screen.getByRole("option", { name: "Other · v1" })).toBeInTheDocument();
  expect(screen.queryByRole("option", { name: "Growth · v4" })).not.toBeInTheDocument();
});

it("freezes the labelled time window and row bound into the executed plan", async () => {
  const { calls } = mockApi([
    [/analyze\/matches\/profile/, () => response({ profile: READY_PROFILE })],
    [/analyze\/matches$/, () => response(catalog([GOVERNED]))],
    [/multi-source\/plans\/qsv_1\/execute$/, () => response({ result: { result_id: "qr_1", outcome: "success", row_count: 1 } }, 201)],
    [/multi-source\/plans$/, () => response(COMPILED, 201)],
    [/results\/qr_1\/evidence$/, () => response(RESULT_EVIDENCE)],
    [/results\/qr_1\/pivot$/, () => response({ pivot: {
      result_id: "qr_1", content_hash: "a".repeat(64), row_fields: ["k_mdm_day"],
      column_fields: [], value_fields: [], row_keys: [], column_keys: [], cells: [],
      bounds: { max_cells: 20000, max_row_keys: 500, max_column_keys: 100,
        rows_truncated: false, columns_truncated: false, rows_dropped: 0,
        columns_dropped: 0, cells_truncated: false },
    } })],
  ]);
  render(<AnalyticsExplorer projectId={PROJECT} />);
  fireEvent.click(await screen.findByTestId("inspect-match"));
  fireEvent.change(screen.getByLabelText("From"), { target: { value: "2026-08-01" } });
  fireEvent.change(screen.getByLabelText("To"), { target: { value: "2026-08-31" } });
  fireEvent.change(screen.getByLabelText("Maximum Result rows"), { target: { value: "5000" } });
  await clickReadyRun();

  const compileCall = calls.find((call) => call.url.endsWith("/multi-source/plans"));
  const body = JSON.parse(String(compileCall?.init?.body));
  expect(body.row_limit).toBe(5000);
  expect(body.profile_receipts).toEqual(["signed-profile-receipt"]);
  expect(body.filters.filter((entry: { stage: string }) => entry.stage === "pre_aggregation"))
    .toEqual([
      { stage: "pre_aggregation", datastream_id: "ds_a", canonical_field_id: "mdm_day", operator: "gte", value: "2026-08-01" },
      { stage: "pre_aggregation", datastream_id: "ds_a", canonical_field_id: "mdm_day", operator: "lte", value: "2026-08-31" },
      { stage: "pre_aggregation", datastream_id: "ds_b", canonical_field_id: "mdm_day", operator: "gte", value: "2026-08-01" },
      { stage: "pre_aggregation", datastream_id: "ds_b", canonical_field_id: "mdm_day", operator: "lte", value: "2026-08-31" },
    ]);
});

it("freezes and renders one server-owned previous-period comparison", async () => {
  const comparison = {
    contract_version: "period-comparison.v1",
    kind: "previous_period",
    canonical_field_id: "mdm_day",
    period_field: "k_comparison_period",
    current: { start: "2026-08-01", end: "2026-08-07" },
    baseline: { start: "2026-07-25", end: "2026-07-31" },
  };
  const { calls } = mockApi([
    [/analyze\/matches\/profile/, () => response({ profile: READY_PROFILE })],
    [/analyze\/matches$/, () => response(catalog([GOVERNED]))],
    [/multi-source\/plans\/qsv_1\/execute$/, () => response({
      result: { result_id: "qr_1", outcome: "success", row_count: 2 },
    }, 201)],
    [/multi-source\/plans$/, () => response({
      ...COMPILED,
      plan: { ...COMPILED.plan, comparison },
    }, 201)],
    [/results\/qr_1\/evidence$/, () => response(RESULT_EVIDENCE)],
    [/results\/qr_1\/pivot$/, () => response({ pivot: {
      result_id: "qr_1",
      content_hash: "a".repeat(64),
      row_fields: [],
      column_fields: ["k_comparison_period"],
      value_fields: [{
        name: "m_mdm_spend",
        canonical_field_id: "mdm_spend",
        role: "measure",
        aggregation: "sum",
        datastream_id: "ds_a",
      }],
      row_keys: [[]],
      column_keys: [["baseline"], ["current"]],
      cells: [],
      comparison: {
        ...comparison,
        deltas: [{
          row_key: [],
          column_key: [],
          value_field: "m_mdm_spend",
          current: { value: 100 },
          baseline: { value: 80 },
          absolute_delta: { value: 20 },
          relative_delta: { value: 0.25 },
        }],
      },
      bounds: {
        max_cells: 20000,
        max_row_keys: 500,
        max_column_keys: 100,
        rows_truncated: false,
        columns_truncated: false,
        rows_dropped: 0,
        columns_dropped: 0,
        cells_truncated: false,
      },
    } })],
  ]);
  render(<AnalyticsExplorer projectId={PROJECT} />);
  fireEvent.click(await screen.findByTestId("inspect-match"));

  expect(screen.getByLabelText("Source comparison")).toBeDisabled();
  fireEvent.change(screen.getByLabelText("From"), { target: { value: "2026-08-01" } });
  fireEvent.change(screen.getByLabelText("To"), { target: { value: "2026-08-07" } });
  fireEvent.change(screen.getByLabelText("Source comparison"), {
    target: { value: "previous_period" },
  });

  expect(wellHolds("rows")).toEqual([]);
  expect(Array.from((screen.getByLabelText("Field") as HTMLSelectElement).options)
    .map((option) => option.textContent)).not.toContain("day");
  expect(screen.getByRole("checkbox", { name: "Row subtotals" })).toBeDisabled();
  expect(screen.getByLabelText("Grand totals")).toBeDisabled();
  await clickReadyRun();

  const compileCall = calls.find((call) => call.url.endsWith("/multi-source/plans"));
  expect(JSON.parse(String(compileCall?.init?.body)).comparison).toBe("previous_period");
  const periodComparison = await screen.findByTestId("period-comparison");
  expect(periodComparison).toHaveTextContent(
    /2026-08-01 to 2026-08-07.*2026-07-25 to 2026-07-31/,
  );
  // `25.00 %` and not `25%`: since 76-1 `formatPercent` holds the digits FIXED
  // (`console-presentation.md` §2 — `0` must read `0.0 %`, never blank), so a
  // column of relative changes lines up on its decimal point. The cap of two
  // decimals is the one this call site always had.
  expect(periodComparison).toHaveTextContent(/25\.00\s*%/);

  fireEvent.change(screen.getByLabelText("Source comparison"), {
    target: { value: "none" },
  });
  expect(screen.queryByTestId("result-identity")).not.toBeInTheDocument();
});

it("freezes the Filters well into the Result plan before pivoting it", async () => {
  const { calls } = mockApi([
    [/analyze\/matches\/profile/, () => response({ profile: READY_PROFILE })],
    [/analyze\/matches$/, () => response(catalog([GOVERNED]))],
    [/multi-source\/plans\/qsv_1\/execute$/, () => response({ result: { result_id: "qr_1", outcome: "success", row_count: 1 } }, 201)],
    [/multi-source\/plans$/, () => response(COMPILED, 201)],
    [/results\/qr_1\/evidence$/, () => response(RESULT_EVIDENCE)],
    [/results\/qr_1\/pivot$/, () => response({ pivot: {
      result_id: "qr_1", content_hash: "a".repeat(64), row_fields: ["k_mdm_day"],
      column_fields: [], value_fields: [], row_keys: [], column_keys: [], cells: [],
      bounds: { max_cells: 20000, max_row_keys: 500, max_column_keys: 100,
        rows_truncated: false, columns_truncated: false, rows_dropped: 0,
        columns_dropped: 0, cells_truncated: false },
    } })],
  ]);
  render(<AnalyticsExplorer projectId={PROJECT} />);
  fireEvent.click(await screen.findByTestId("inspect-match"));
  fireEvent.change(screen.getByLabelText("Exact value"), { target: { value: "2026-08-01" } });
  await clickReadyRun();

  await waitFor(() => expect(calls.some((call) => call.url.endsWith("/multi-source/plans"))).toBe(true));
  const compileCall = calls.find((call) => call.url.endsWith("/multi-source/plans"));
  expect(JSON.parse(String(compileCall?.init?.body)).filters).toEqual([{
    stage: "post_aggregation",
    canonical_field_id: "mdm_day",
    operator: "eq",
    value: "2026-08-01",
  }]);
});

it("aborts an in-flight analytical request and never exposes a partial Result", async () => {
  let compileSignal: AbortSignal | undefined;
  const fetchMock = vi.fn((url: string, init?: RequestInit) => {
    if (/analyze\/matches\/profile/.test(url)) {
      return Promise.resolve(response({ profile: READY_PROFILE }));
    }
    if (/analyze\/matches$/.test(url)) {
      return Promise.resolve(response(catalog([GOVERNED])));
    }
    if (/multi-source\/plans$/.test(url)) {
      compileSignal = init?.signal ?? undefined;
      return new Promise<Response>((_resolve, reject) => {
        compileSignal?.addEventListener("abort", () => {
          reject(new DOMException("Aborted", "AbortError"));
        });
      });
    }
    return Promise.resolve(response({ code: "not_found", message: "Not found" }, 404));
  });
  vi.stubGlobal("fetch", fetchMock);
  render(<AnalyticsExplorer projectId={PROJECT} />);
  fireEvent.click(await screen.findByTestId("inspect-match"));
  await clickReadyRun();

  fireEvent.click(await screen.findByTestId("cancel-analysis"));

  await waitFor(() => expect(compileSignal?.aborted).toBe(true));
  expect(screen.getByTestId("run-error")).toHaveTextContent(/cancelled/i);
  expect(screen.queryByTestId("preview")).toBeNull();
  expect(screen.getByTestId("run-analysis")).toBeEnabled();
});

it("keeps the Result on screen when a projection is refused", async () => {
  mockApi([
    [/analyze\/matches\/profile/, () => response({ profile: READY_PROFILE })],
    [/analyze\/matches$/, () => response(catalog([GOVERNED]))],
    [/multi-source\/plans\/qsv_1\/execute$/, () => response({ result: { result_id: "qr_1", outcome: "success", row_count: 1 } }, 201)],
    [/multi-source\/plans$/, () => response(COMPILED, 201)],
    [/results\/qr_1\/evidence$/, () => response(RESULT_EVIDENCE)],
    [/results\/qr_1\/pivot$/, () => response({ code: "pivot_unavailable", message: "The pivot is unavailable." }, 422)],
  ]);
  render(<AnalyticsExplorer projectId={PROJECT} />);
  fireEvent.click(await screen.findByTestId("inspect-match"));
  await clickReadyRun();
  await waitFor(() => expect(screen.getByTestId("result-identity")).toBeInTheDocument());

  await waitFor(() => expect(screen.getByTestId("run-error")).toHaveTextContent(/unavailable/));
  // The last useful evidence is still there: a refusal never blanks the answer.
  expect(screen.getByTestId("result-identity")).toHaveTextContent("qr_1");
});

// ---------------------------------------------------------------------------
// The pivot is drawn, never computed
// ---------------------------------------------------------------------------

it("draws an empty cell as 'no data' and never as zero", async () => {
  const matrix = {
    result_id: "qr_1",
    content_hash: "a".repeat(64),
    row_fields: ["k_campaign"],
    column_fields: ["k_day"],
    value_fields: [
      { name: "m_mdm_spend", canonical_field_id: "mdm_spend", role: "measure", aggregation: "sum", datastream_id: "ds_a" },
      { name: "m_mdm_conversions", canonical_field_id: "mdm_conversions", role: "measure", aggregation: "sum", datastream_id: "ds_b" },
    ],
    row_keys: [["A"], ["B"]],
    column_keys: [["2026-08-01"]],
    cells: [
      { row_key: ["A"], column_key: ["2026-08-01"], values: { m_mdm_spend: { value: 18 }, m_mdm_conversions: { value: 4 } }, contributing_rows: 1, contributing_row_indexes: [0] },
      { row_key: ["B"], column_key: ["2026-08-01"], values: { m_mdm_spend: { value: null, absent_reason: "no_contributing_row" }, m_mdm_conversions: { value: 2 } }, contributing_rows: 0, contributing_row_indexes: [] },
    ],
    bounds: {
      max_cells: 20000, max_row_keys: 500, max_column_keys: 100,
      rows_truncated: false, columns_truncated: false, rows_dropped: 0, columns_dropped: 0, cells_truncated: false,
    },
  };
  mockApi([
    [/analyze\/matches\/profile/, () => response({ profile: READY_PROFILE })],
    [/analyze\/matches$/, () => response(catalog([GOVERNED]))],
    [/multi-source\/plans\/qsv_1\/execute$/, () => response({ result: { result_id: "qr_1", outcome: "success", row_count: 2 } }, 201)],
    [/multi-source\/plans$/, () => response(COMPILED, 201)],
    [/results\/qr_1\/evidence$/, () => response(RESULT_EVIDENCE)],
    [/results\/qr_1\/pivot$/, () => response({ pivot: matrix })],
  ]);
  render(<AnalyticsExplorer projectId={PROJECT} />);
  fireEvent.click(await screen.findByTestId("inspect-match"));
  await clickReadyRun();
  await waitFor(() => expect(screen.getByTestId("result-identity")).toBeInTheDocument());

  const scroller = await screen.findByTestId("pivot-scroller");
  expect(scroller).toHaveTextContent("18");
  expect(scroller).toHaveTextContent("Conversions");
  expect(scroller).toHaveTextContent("no data");
  expect(scroller.textContent).not.toMatch(/\b0\b/);
});

it("records pivot page history only after a page succeeds", async () => {
  const pivotOffsets: Array<number | undefined> = [];
  let pivotCall = 0;
  const matrixAt = (rowOffset: number, nextRowOffset: number | null) => ({
    result_id: "qr_1",
    content_hash: "a".repeat(64),
    row_fields: ["k_campaign"],
    column_fields: [],
    value_fields: [
      {
        name: "m_mdm_spend",
        canonical_field_id: "mdm_spend",
        role: "measure",
        aggregation: "sum",
        datastream_id: "ds_a",
      },
    ],
    row_keys: [[`page-${rowOffset}`]],
    column_keys: [[]],
    cells: [
      {
        row_key: [`page-${rowOffset}`],
        column_key: [],
        values: { m_mdm_spend: { value: rowOffset + 1 } },
        contributing_rows: 1,
        contributing_row_indexes: [rowOffset],
      },
    ],
    bounds: {
      row_offset: rowOffset,
      column_offset: 0,
      next_row_offset: nextRowOffset,
      next_column_offset: null,
      rows_truncated: nextRowOffset !== null,
      columns_truncated: false,
    },
  });
  const fetchMock = vi.fn((url: string, init?: RequestInit) => {
    if (/analyze\/matches\/profile/.test(url)) {
      return Promise.resolve(response({ profile: READY_PROFILE }));
    }
    if (/analyze\/matches$/.test(url)) return Promise.resolve(response(catalog([GOVERNED])));
    if (/multi-source\/plans$/.test(url)) return Promise.resolve(response(COMPILED, 201));
    if (/multi-source\/plans\/qsv_1\/execute$/.test(url)) {
      return Promise.resolve(
        response({ result: { result_id: "qr_1", outcome: "success", row_count: 6 } }, 201),
      );
    }
    if (/results\/qr_1\/evidence$/.test(url)) {
      return Promise.resolve(response(RESULT_EVIDENCE));
    }
    if (/results\/qr_1\/pivot$/.test(url)) {
      const body = JSON.parse(String(init?.body)) as { row_offset?: number };
      pivotOffsets.push(body.row_offset);
      pivotCall += 1;
      if (pivotCall === 3) {
        return Promise.resolve(
          response({ code: "pivot_unavailable", message: "Temporary projection failure." }, 422),
        );
      }
      const offset = body.row_offset ?? 0;
      const next = offset === 0 ? 2 : offset === 2 ? 4 : null;
      return Promise.resolve(response({ pivot: matrixAt(offset, next) }));
    }
    return Promise.resolve(response({ code: "not_found", message: "Not found" }, 404));
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AnalyticsExplorer projectId={PROJECT} />);
  fireEvent.click(await screen.findByTestId("inspect-match"));
  await clickReadyRun();
  fireEvent.click(await screen.findByRole("button", { name: "Next rows" }));
  await waitFor(() => expect(screen.getByText("page-2")).toBeInTheDocument());
  fireEvent.click(screen.getByRole("button", { name: "Next rows" }));
  await screen.findByText("Temporary projection failure.");
  fireEvent.click(screen.getByRole("button", { name: "Next rows" }));
  await waitFor(() => expect(screen.getByText("page-4")).toBeInTheDocument());
  fireEvent.click(screen.getByRole("button", { name: "Previous rows" }));
  await waitFor(() => expect(screen.getByText("page-2")).toBeInTheDocument());
  fireEvent.click(screen.getByRole("button", { name: "Previous rows" }));
  await waitFor(() => expect(screen.getByText("page-0")).toBeInTheDocument());

  expect(pivotOffsets).toEqual([undefined, 2, 4, 4, 2, 0]);
});

// ---------------------------------------------------------------------------
// Story 66.10 AC 9 — the console pages with the SIGNED continuation.
//
// The bare offset is a position and asserts nothing about which matrix it is a
// position IN: replaying `row_offset: 2` under another filter served a page of
// a different matrix, silently. The server mints a token per axis; a console
// that kept sending the integer would leave the guard unused on the only path
// a person actually walks.
// ---------------------------------------------------------------------------

it("pages with the server's continuation, forward and back", async () => {
  const sent: Array<{ cursor?: string; row_offset?: number }> = [];
  const matrixAt = (rowOffset: number, next: number | null) => ({
    result_id: "qr_1",
    content_hash: "a".repeat(64),
    row_fields: ["k_campaign"],
    column_fields: [],
    value_fields: [
      {
        name: "m_mdm_spend",
        canonical_field_id: "mdm_spend",
        role: "measure",
        aggregation: "sum",
        datastream_id: "ds_a",
      },
    ],
    row_keys: [[`page-${rowOffset}`]],
    column_keys: [[]],
    cells: [
      {
        row_key: [`page-${rowOffset}`],
        column_key: [],
        values: { m_mdm_spend: { value: rowOffset + 1 } },
        contributing_rows: 1,
        contributing_row_indexes: [rowOffset],
      },
    ],
    bounds: {
      row_offset: rowOffset,
      column_offset: 0,
      next_row_offset: next,
      next_column_offset: null,
      rows_truncated: next !== null,
      columns_truncated: false,
      cursor: `token-at-${rowOffset}`,
      next_row_cursor: next === null ? null : `token-at-${next}`,
      next_column_cursor: null,
    },
  });

  const fetchMock = vi.fn((url: string, init?: RequestInit) => {
    if (/analyze\/matches\/profile/.test(url)) {
      return Promise.resolve(response({ profile: READY_PROFILE }));
    }
    if (/analyze\/matches$/.test(url)) return Promise.resolve(response(catalog([GOVERNED])));
    if (/multi-source\/plans$/.test(url)) return Promise.resolve(response(COMPILED, 201));
    if (/multi-source\/plans\/qsv_1\/execute$/.test(url)) {
      return Promise.resolve(
        response({ result: { result_id: "qr_1", outcome: "success", row_count: 6 } }, 201),
      );
    }
    if (/results\/qr_1\/evidence$/.test(url)) return Promise.resolve(response(RESULT_EVIDENCE));
    if (/results\/qr_1\/pivot$/.test(url)) {
      const body = JSON.parse(String(init?.body)) as { cursor?: string; row_offset?: number };
      sent.push({ cursor: body.cursor, row_offset: body.row_offset });
      const offset = body.cursor ? Number(body.cursor.replace("token-at-", "")) : 0;
      return Promise.resolve(response({ pivot: matrixAt(offset, offset === 0 ? 2 : null) }));
    }
    return Promise.resolve(response({ code: "not_found", message: "Not found" }, 404));
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AnalyticsExplorer projectId={PROJECT} />);
  fireEvent.click(await screen.findByTestId("inspect-match"));
  await clickReadyRun();

  fireEvent.click(await screen.findByRole("button", { name: "Next rows" }));
  await waitFor(() => expect(screen.getByText("page-2")).toBeInTheDocument());
  fireEvent.click(screen.getByRole("button", { name: "Previous rows" }));
  await waitFor(() => expect(screen.getByText("page-0")).toBeInTheDocument());

  // The first page asks for no page at all; every move afterwards travels as a
  // token, and NEVER as the bare integer beside it.
  expect(sent).toEqual([
    { cursor: undefined, row_offset: undefined },
    { cursor: "token-at-2", row_offset: undefined },
    { cursor: "token-at-0", row_offset: undefined },
  ]);
});

// ---------------------------------------------------------------------------
// `visualization-and-rendering.md`, *Composition is direct manipulation, and
// the keyboard keeps parity* (2026-08-15). Two edits, two consequences, and the
// screen has to say which one it just made.
// ---------------------------------------------------------------------------

const PIVOTED = {
  result_id: "qr_1",
  content_hash: "a".repeat(64),
  row_fields: ["k_mdm_day"],
  column_fields: [],
  value_fields: [],
  row_keys: [],
  column_keys: [],
  cells: [],
  bounds: {
    max_cells: 20000, max_row_keys: 500, max_column_keys: 100,
    rows_truncated: false, columns_truncated: false, rows_dropped: 0,
    columns_dropped: 0, cells_truncated: false,
  },
};

function pivotableApi() {
  return mockApi([
    [/analyze\/matches\/profile/, () => response({ profile: READY_PROFILE })],
    [/analyze\/matches$/, () => response(catalog([GOVERNED]))],
    [/multi-source\/plans\/qsv_1\/execute$/, () => response({
      result: { result_id: "qr_1", outcome: "success", row_count: 1 },
    }, 201)],
    [/multi-source\/plans$/, () => response(COMPILED, 201)],
    [/results\/qr_1\/evidence$/, () => response(RESULT_EVIDENCE)],
    [/results\/qr_1\/pivot$/, () => response({ pivot: PIVOTED })],
  ]);
}

it("re-projects the same Result when a placed field moves between the axes", async () => {
  const { calls } = pivotableApi();
  render(<AnalyticsExplorer projectId={PROJECT} />);
  fireEvent.click(await screen.findByTestId("inspect-match"));
  await clickReadyRun();
  await waitFor(() => expect(screen.getByTestId("pivot-axis-bar")).toBeInTheDocument());

  const compilesBefore = calls.filter((call) => call.url.endsWith("/multi-source/plans")).length;
  const pivotsBefore = calls.filter((call) => call.url.endsWith("/pivot")).length;

  // Rows -> Columns, from the axis bar of the table itself.
  fireEvent.click(screen.getByTestId("axis-rows-field-mdm_day"));
  fireEvent.click(screen.getByTestId("axis-columns-place"));

  await waitFor(() => expect(
    calls.filter((call) => call.url.endsWith("/pivot")).length,
  ).toBe(pivotsBefore + 1));
  // NOT a new query: the Result was not re-executed and no plan was compiled.
  expect(calls.filter((call) => call.url.endsWith("/multi-source/plans")).length)
    .toBe(compilesBefore);
  const reprojection = calls.filter((call) => call.url.endsWith("/pivot")).at(-1);
  expect(reprojection?.url).toContain("/results/qr_1/pivot");
  expect(JSON.parse(String(reprojection?.init?.body))).toMatchObject({
    rows: [],
    columns: ["k_mdm_day"],
  });
  // And nothing says the composition drifted, because it did not.
  expect(screen.queryByTestId("composition-stale")).toBeNull();
});

it("says a Result is owed rather than repainting figures, when a member changes", async () => {
  const { calls } = pivotableApi();
  render(<AnalyticsExplorer projectId={PROJECT} />);
  fireEvent.click(await screen.findByTestId("inspect-match"));
  await clickReadyRun();
  await waitFor(() => expect(screen.getByTestId("pivot-axis-bar")).toBeInTheDocument());

  const pivotsBefore = calls.filter((call) => call.url.endsWith("/pivot")).length;
  // Taking a measure out is an analytical edit: it changes what was computed.
  fireEvent.click(screen.getByTestId("well-values-remove-ds_a:mdm_spend"));

  expect(screen.getByTestId("composition-stale")).toHaveTextContent(
    /have not been recomputed|Run the analysis/,
  );
  expect(calls.filter((call) => call.url.endsWith("/pivot")).length).toBe(pivotsBefore);
  // The Result on screen is untouched, and still names itself.
  expect(screen.getByTestId("result-identity")).toBeInTheDocument();
});

it("re-projects the same Result when a dimension is DRAGGED between the axes", async () => {
  // The pointer path of the same move the click test above makes — dragstart on
  // the chip that heads Rows, drop on the Columns well. jsdom has no
  // DataTransfer, so the gesture carries a minimal literal one.
  const { calls } = pivotableApi();
  const dataTransfer = { setData: vi.fn(), effectAllowed: "", dropEffect: "" };
  render(<AnalyticsExplorer projectId={PROJECT} />);
  fireEvent.click(await screen.findByTestId("inspect-match"));
  await clickReadyRun();
  await waitFor(() => expect(screen.getByTestId("pivot-axis-bar")).toBeInTheDocument());

  const compilesBefore = calls.filter((call) => call.url.endsWith("/multi-source/plans")).length;
  const pivotsBefore = calls.filter((call) => call.url.endsWith("/pivot")).length;

  fireEvent.dragStart(screen.getByTestId("axis-rows-field-mdm_day"), { dataTransfer });
  fireEvent.dragOver(screen.getByTestId("axis-columns"), { dataTransfer });
  fireEvent.drop(screen.getByTestId("axis-columns"), { dataTransfer });

  await waitFor(() => expect(
    calls.filter((call) => call.url.endsWith("/pivot")).length,
  ).toBe(pivotsBefore + 1));
  // A presentation move, not a new query.
  expect(calls.filter((call) => call.url.endsWith("/multi-source/plans")).length)
    .toBe(compilesBefore);
  expect(JSON.parse(String(
    calls.filter((call) => call.url.endsWith("/pivot")).at(-1)?.init?.body,
  ))).toMatchObject({ rows: [], columns: ["k_mdm_day"] });
});

it("says a Result is owed when a measure is DRAGGED back to the shelf", async () => {
  const { calls } = pivotableApi();
  const dataTransfer = { setData: vi.fn(), effectAllowed: "", dropEffect: "" };
  render(<AnalyticsExplorer projectId={PROJECT} />);
  fireEvent.click(await screen.findByTestId("inspect-match"));
  await clickReadyRun();
  await waitFor(() => expect(screen.getByTestId("pivot-axis-bar")).toBeInTheDocument());

  const pivotsBefore = calls.filter((call) => call.url.endsWith("/pivot")).length;
  // Dragging a bound measure onto the shelf unbinds it — an analytical edit.
  fireEvent.dragStart(screen.getByTestId("well-values-field-ds_a:mdm_spend"), { dataTransfer });
  fireEvent.dragOver(screen.getByTestId("composition-shelf-drop"), { dataTransfer });
  fireEvent.drop(screen.getByTestId("composition-shelf-drop"), { dataTransfer });

  expect(screen.getByTestId("composition-stale")).toBeInTheDocument();
  // Nothing was recomputed, and the field is back on the shelf.
  expect(calls.filter((call) => call.url.endsWith("/pivot")).length).toBe(pivotsBefore);
  expect(screen.getByTestId("field-ds_a:mdm_spend")).toBeInTheDocument();
});

function truncatedPivot(reason: "axis_cap" | "cell_cap" | "byte_budget") {
  return {
    ...PIVOTED,
    bounds: {
      ...PIVOTED.bounds,
      rows_truncated: true,
      rows_dropped: 454,
      cells_truncated: reason === "cell_cap",
      truncation_reason: reason,
    },
  };
}

async function renderTruncated(reason: "axis_cap" | "cell_cap" | "byte_budget") {
  mockApi([
    [/analyze\/matches\/profile/, () => response({ profile: READY_PROFILE })],
    [/analyze\/matches$/, () => response(catalog([GOVERNED]))],
    [/multi-source\/plans\/qsv_1\/execute$/, () => response({
      result: { result_id: "qr_1", outcome: "success", row_count: 1 },
    }, 201)],
    [/multi-source\/plans$/, () => response(COMPILED, 201)],
    [/results\/qr_1\/evidence$/, () => response(RESULT_EVIDENCE)],
    [/results\/qr_1\/pivot$/, () => response({ pivot: truncatedPivot(reason) })],
  ]);
  render(<AnalyticsExplorer projectId={PROJECT} />);
  fireEvent.click(await screen.findByTestId("inspect-match"));
  await clickReadyRun();
  return screen.findByTestId("pivot-truncated");
}

// Story 66.6. Le bandeau offrait UN geste pour trois causes. Mesure au plafond
// de cellules exactement : c'est le budget en octets qui mord, et un filtre
// n'est pas ce qui rend les lignes suivantes -- le pageur si. Envoyer quelqu'un
// filtrer une page simplement PLEINE lui fait retrecir une analyse qui avait la
// bonne taille.
it("tells a full page from a too-large matrix, and names a different gesture for each", async () => {
  expect(await renderTruncated("byte_budget")).toHaveTextContent(
    "The page is full, not the analysis: use the pager to see the rest.",
  );
});

it("sends a too-large matrix to remove a dimension, not to filter", async () => {
  expect(await renderTruncated("cell_cap")).toHaveTextContent(
    "Remove a dimension, or split the analysis.",
  );
});

it("keeps the filter gesture for the axis cap, which a filter does repair", async () => {
  expect(await renderTruncated("axis_cap")).toHaveTextContent(
    "Narrow the analysis with a filter to see them.",
  );
});

it("refuses a measure in an axis well, in the words of the role it takes", async () => {
  pivotableApi();
  render(<AnalyticsExplorer projectId={PROJECT} />);
  fireEvent.click(await screen.findByTestId("inspect-match"));
  await screen.findByTestId("composition-shelf");

  fireEvent.click(screen.getByTestId("well-values-field-ds_a:mdm_spend"));
  expect(screen.queryByTestId("well-rows-place")).toBeNull();
  expect(screen.getByTestId("well-rows-refusal")).toHaveTextContent(
    "Rows takes dimension, and Spend is a measure.",
  );
});


// ---------------------------------------------------------------------------
// Arbitration of 2026-08-22 — the person inspecting sees what the inspection
// cost, and NO EMPTY LINE.
//
// The three figures were computed, aggregated, signed into the receipt and
// served, and drawn nowhere. On a billed engine the inspection itself is money
// the customer spends; a figure the product measures and never shows is a figure
// it did not measure, from where the person stands.
// ---------------------------------------------------------------------------

it("says what the check cost on a billed engine", async () => {
  mockApi([
    [/analyze\/matches\/profile/, () => response({
      profile: {
        ...READY_PROFILE,
        cost: {
          engine: "bigquery",
          warehouse_jobs_issued: 3,
          billed_bytes: 1_258_291,
          billed_bytes_state: "exact",
          elapsed_ms: 412,
          elapsed_ms_state: "exact",
        },
      },
    })],
    [/analyze\/matches$/, () => response(catalog([GOVERNED]))],
  ]);
  render(<AnalyticsExplorer projectId={PROJECT} />);
  fireEvent.click(await screen.findByTestId("inspect-match"));

  const line = await screen.findByTestId("profile-cost");
  expect(line).toHaveTextContent("3 warehouse jobs");
  expect(line).toHaveTextContent("412 ms");
  expect(line).toHaveTextContent("1.2 MB billed");
});

it("prints no billed line at all on an engine that bills nothing", async () => {
  // `not_applicable` is ABSENT, never `0` and never the words. DuckDB reads a
  // local file: `0` would be a measurement nobody made, and a row that says
  // nothing teaches a reader to skip the block.
  mockApi([
    [/analyze\/matches\/profile/, () => response({
      profile: {
        ...READY_PROFILE,
        cost: {
          engine: "duckdb",
          warehouse_jobs_issued: 3,
          billed_bytes: null,
          billed_bytes_state: "not_applicable",
          elapsed_ms: 12,
          elapsed_ms_state: "exact",
        },
      },
    })],
    [/analyze\/matches$/, () => response(catalog([GOVERNED]))],
  ]);
  render(<AnalyticsExplorer projectId={PROJECT} />);
  fireEvent.click(await screen.findByTestId("inspect-match"));

  const line = await screen.findByTestId("profile-cost");
  expect(line).toHaveTextContent("3 warehouse jobs");
  expect(line).toHaveTextContent("12 ms");
  expect(line.textContent).not.toMatch(/billed|not applicable|0 B/i);
});

it("says a reused profile measured its cost when the receipt was minted", async () => {
  mockApi([
    [/analyze\/matches\/profile/, () => response({
      profile: {
        ...READY_PROFILE,
        cost: {
          engine: "bigquery",
          warehouse_jobs_issued: 3,
          billed_bytes: 1024,
          billed_bytes_state: "exact",
          elapsed_ms: 400,
          elapsed_ms_state: "exact",
          reused_from_receipt: true,
          warehouse_jobs_issued_now: 0,
        },
      },
    })],
    [/analyze\/matches$/, () => response(catalog([GOVERNED]))],
  ]);
  render(<AnalyticsExplorer projectId={PROJECT} />);
  fireEvent.click(await screen.findByTestId("inspect-match"));

  // Handing the minted figures back unlabelled reads as "this costs three jobs
  // every time".
  expect(await screen.findByTestId("profile-cost")).toHaveTextContent(
    "when it was measured",
  );
});

it("draws nothing when the server served no cost block", async () => {
  mockApi([
    [/analyze\/matches\/profile/, () => response({ profile: READY_PROFILE })],
    [/analyze\/matches$/, () => response(catalog([GOVERNED]))],
  ]);
  render(<AnalyticsExplorer projectId={PROJECT} />);
  fireEvent.click(await screen.findByTestId("inspect-match"));
  await screen.findByTestId("match-evidence");
  expect(screen.queryByTestId("profile-cost")).toBeNull();
});
