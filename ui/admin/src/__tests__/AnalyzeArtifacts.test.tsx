/**
 * Reports, Notebooks and Renders — Story 50.3.
 *
 * Every response below is the shape `server/core/analyze_artifacts_api.py`
 * really returns. The point of these tests is not that the screens render: it
 * is that they render the RATIFIED object model and refuse the three
 * substitutions the legacy screens made —
 *
 *   * a connector enablement toggle shown as a Report;
 *   * a missing presentation contract shown as an empty chart picker;
 *   * a legacy snapshot shown as a replayable Render.
 *
 * and that no screen creates a share, returns a bearer, or follows a latest run.
 */
import {
  FORMATTER_VERSION,
  RUNTIME_BUILD,
  THEME_VERSION,
  resolveRenderer,
  type RenderInput,
} from "@toorow/card-shell/viz";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import analyzeFeedbackTargets from "../../../cards/shell/src/viz/__tests__/fixtures/analyzeFeedbackTargets.json";

import { NotebookWorkbench, NotebooksCollection } from "../analyze-artifacts/Notebooks";
import { RenderWorkbench, RendersCollection } from "../analyze-artifacts/Renders";
import { ReportWorkbench, ReportsCollection } from "../analyze-artifacts/Reports";
import VisualizationMount from "../analyze/VisualizationMount";

const PROJECT = "proj_EXAMPLE";
const GOLDEN_RENDER_INPUT = analyzeFeedbackTargets.surfaces.console.delivery
  .render_input as unknown as RenderInput;

const CONTRACT_UNAVAILABLE = {
  available: false,
  missing: [
    {
      contract: "visualization_spec_version",
      owner_story: "50.4",
      missing_link: "app.visualization_spec_versions",
    },
    {
      contract: "renderer_and_runtime_build",
      owner_story: "50.5",
      missing_link: "app.renderer_runtime_builds",
    },
  ],
};

/**
 * WHAT THE DEPLOYMENT ACTUALLY ANSWERS TODAY, and the fixture that was missing.
 *
 * Both registries Stories 50.4/50.5 own ship, so `available` is true and the
 * banner above rendered nothing at all — while the Chart Template, ratified and
 * owned by Analyze, has no table anywhere and was still offered in the picker.
 * `unpinnable_kinds` is the separate fact the server now sends for exactly that.
 */
const CHART_TEMPLATE_UNPINNABLE = {
  kind: "visualization_template_version",
  contract: "chart_template_version",
  owner_story: "72.1",
  missing_link: "app.visualization_template_versions",
};

const CONTRACT_TODAY = {
  available: true,
  missing: [],
  pinnable_kinds: ["visualization_spec_version"],
  unpinnable_kinds: [CHART_TEMPLATE_UNPINNABLE],
};

const REPORT_COLLECTION = {
  reports: [
    {
      id: "rep_EXAMPLE",
      label: "Weekly traffic",
      description: null,
      seed_origin: "explore",
      seed: null,
      current_version_id: "repv_EXAMPLE",
      archived: false,
      created_by: "owner@example.com",
      created_at: "2026-07-31T09:00:00Z",
      updated_at: "2026-07-31T09:00:00Z",
      current_version_number: 1,
      query_spec_version_id: "qsv_EXAMPLE",
      presentation_absent: "No accepted presentation contract",
      run_count: 2,
    },
  ],
  seeds: [
    {
      module_name: "google-analytics",
      report_id: "overview_daily",
      enabled: true,
      display_order: 0,
      kind: "connector_seed",
    },
  ],
  presentation_contract: CONTRACT_UNAVAILABLE,
};

const REPORT_DETAIL = {
  ...REPORT_COLLECTION.reports[0],
  // The workbench is where the pin is offered, so it is where the contract has
  // to be read. It used to be sent to the collection only.
  presentation_contract: CONTRACT_TODAY,
  versions: [
    {
      id: "repv_EXAMPLE",
      version_number: 1,
      label: "Weekly traffic",
      query_spec_id: "qs_EXAMPLE",
      query_spec_version_id: "qsv_EXAMPLE",
      presentation: {
        kind: null,
        version_id: null,
        absent_literal: "No accepted presentation contract",
      },
      content_hash: "a".repeat(64),
      predecessor_version_id: null,
      created_by: "owner@example.com",
      created_at: "2026-07-31T09:00:00Z",
    },
  ],
  runs: [
    {
      id: "reprun_EXAMPLE",
      report_version_id: "repv_EXAMPLE",
      query_spec_version_id: "qsv_EXAMPLE",
      result_id: "qr_EXAMPLE",
      render_id: null,
      outcome: "unavailable",
      requested_as_of: null,
      resolved_as_of: null,
      actor: "owner@example.com",
      started_at: "2026-07-31T09:00:00Z",
      ended_at: "2026-07-31T09:00:01Z",
    },
  ],
};

const NOTEBOOK_DETAIL = {
  id: "nbk_EXAMPLE",
  label: "Weekly narrative",
  description: null,
  current_version_id: "nbkv_EXAMPLE2",
  archived: false,
  legacy_notebook_id: null,
  created_by: "owner@example.com",
  created_at: "2026-07-31T09:00:00Z",
  updated_at: "2026-07-31T09:10:00Z",
  current_version_number: 2,
  run_count: 1,
  last_run_at: "2026-07-31T09:05:00Z",
  last_run_outcome: "partial",
  schedule: null,
  versions: [
    {
      id: "nbkv_EXAMPLE2",
      version_number: 2,
      label: "Weekly narrative",
      content_hash: "b".repeat(64),
      predecessor_version_id: "nbkv_EXAMPLE",
      created_by: "owner@example.com",
      created_at: "2026-07-31T09:10:00Z",
      blocks: [
        {
          block_key: "intro",
          position: 1,
          block_type: "narrative",
          query_spec_version_id: null,
          report_version_id: null,
          presentation: {
            kind: null,
            version_id: null,
            absent_literal: "No accepted presentation contract",
          },
          renders: false,
          as_of_rule: "current",
          narrative: { text: "What changed" },
          content_hash: "c".repeat(64),
        },
      ],
    },
    {
      id: "nbkv_EXAMPLE",
      version_number: 1,
      label: "Weekly narrative",
      content_hash: "d".repeat(64),
      predecessor_version_id: null,
      created_by: "owner@example.com",
      created_at: "2026-07-31T09:00:00Z",
      blocks: [],
    },
  ],
  runs: [
    {
      id: "nbkrun_EXAMPLE",
      notebook_version_id: "nbkv_EXAMPLE",
      idempotency_key: "manual-1",
      dispatch_source: "manual",
      state: "terminal",
      outcome: "partial",
      requested_as_of: null,
      resolved_as_of: null,
      actor: "owner@example.com",
      accepted_at: "2026-07-31T09:05:00Z",
      terminal_at: "2026-07-31T09:05:02Z",
    },
  ],
};

const NOTEBOOK_RUN = {
  ...NOTEBOOK_DETAIL.runs[0],
  notebook_id: "nbk_EXAMPLE",
  blocks: [
    {
      block_key: "intro",
      position: 1,
      block_type: "narrative",
      query_spec_version_id: null,
      report_version_id: null,
      result_id: null,
      render_id: null,
      render_absent_literal: "No Render",
      status: "succeeded",
      limitation: null,
      resolved_as_of: null,
      started_at: "2026-07-31T09:05:00Z",
      ended_at: "2026-07-31T09:05:00Z",
    },
    {
      block_key: "traffic",
      position: 2,
      block_type: "query",
      query_spec_version_id: "qsv_EXAMPLE",
      report_version_id: null,
      result_id: "qr_EXAMPLE2",
      render_id: null,
      render_absent_literal: "No Render: no accepted presentation contract",
      status: "unavailable",
      limitation: null,
      resolved_as_of: null,
      started_at: "2026-07-31T09:05:01Z",
      ended_at: "2026-07-31T09:05:02Z",
    },
  ],
};

const LEGACY_NOTEBOOK = {
  id: "nb_EXAMPLE",
  title: "Legacy weekly",
  report_ref: "gsc/position_movements",
  window_rule: "last_30d",
  narrative_prompt: "summarize",
  scheduled: true,
  schedule_rule: "nightly",
  is_shared: true,
  shared_at: "2026-07-01T09:00:00Z",
  created_by: "owner@example.com",
  created_at: "2026-07-01T09:00:00Z",
  updated_at: "2026-07-20T09:00:00Z",
  classification: "legacy_mutable_definition",
  limitation:
    "one mutable report_ref/window_rule/prompt row with no version history: every Run below refers to a definition that may since have been edited in place",
  runs: [
    {
      id: "nbrun_EXAMPLE",
      executed_at: "2026-07-20T09:05:00Z",
      as_of: null,
      status: "success",
      error_message: null,
      evidence: "deferred",
      pull_id_count: 3,
      summary_snippet: "a summary",
      result_id: null,
      render_id: null,
      limitation:
        "the envelope was too large to store inline and no blob was ever written; `deferred` is shown as incomplete and is never reconstructed from current state",
      classification: "legacy_incomplete_evidence",
    },
  ],
};

const NOTEBOOK_COLLECTION = {
  notebooks: [
    {
      id: "nbk_EXAMPLE",
      label: "Weekly narrative",
      description: null,
      current_version_id: "nbkv_EXAMPLE2",
      archived: false,
      legacy_notebook_id: null,
      created_by: "owner@example.com",
      created_at: "2026-07-31T09:00:00Z",
      updated_at: "2026-07-31T09:10:00Z",
      current_version_number: 2,
      run_count: 1,
      last_run_at: "2026-07-31T09:05:00Z",
      last_run_outcome: "partial",
      schedule: null,
    },
  ],
};

const LEGACY_SNAPSHOT = {
  id: "rs_EXAMPLE",
  tool_name: "get_card",
  summary_snippet: "legacy card",
  question: null,
  identity: null,
  trace_id: null,
  created_at: "2026-07-30T09:00:00Z",
  had_widget_uri: true,
  classification: "legacy_unverifiable",
  replayable: false,
  missing_pins: [
    "result_identity",
    "retained_result_data",
    "visualization_spec_version",
    "renderer_build",
    "runtime_build",
    "theme_version",
    "formatter_version",
    "responsive_profile",
    "local_display_state",
    "evidence_manifest",
  ],
};

const RENDER_DETAIL = {
  id: "rnd_EXAMPLE",
  result_id: "qr_EXAMPLE",
  result_content_hash: "e".repeat(64),
  result_payload_retained: true,
  visualization_spec_version_id: "vsv_EXAMPLE",
  renderer_adapter: "echarts",
  renderer_build_id: "rb_EXAMPLE",
  runtime_build_id: "rt_EXAMPLE",
  theme_version: "theme_EXAMPLE",
  formatter_version: "fmt_EXAMPLE",
  responsive_profile: "desktop_wide",
  display_state: {},
  evidence_manifest: { semantic_view_version_id: "svv_EXAMPLE" },
  datum_evidence_keys: {},
  creation_surface: "explore",
  origin_kind: "explore",
  origin_report_run_id: null,
  origin_notebook_run_id: null,
  predecessor_render_id: null,
  content_hash: "f".repeat(64),
  created_by: "owner@example.com",
  created_at: "2026-07-31T09:00:00Z",
  replayable: true,
  retention_actions: [],
  sharing: {
    canonical_share_available: false,
    reason: "AD-30 fragment exchange for one Render is owned by Story 50.7",
  },
};

/**
 * The Visualization Spec version a Render pins, shaped exactly like
 * `server/core/visualization_specs.py:1793-1808`. The document is Story 50.4's
 * persisted grammar with every key present — the runtime's validator is written
 * against that, not against a convenient subset, so a trimmed fixture here would
 * pass a test the real payload fails.
 */
const VIZ_SPEC_VERSION = {
  id: "vsv_EXAMPLE",
  visualization_id: "viz_EXAMPLE",
  version_number: 1,
  query_spec_id: "qs_EXAMPLE",
  query_spec_version_id: "qsv_EXAMPLE",
  spec_contract_version: "visualization-spec.v1",
  schema_version: 1,
  family: "bar",
  content_hash: "a".repeat(64),
  predecessor_version_id: null,
  proposed_by: "owner@example.com",
  created_by: "owner@example.com",
  created_at: "2026-07-31T09:00:00Z",
  spec: {
    spec_contract_version: "visualization-spec.v1",
    schema_version: 1,
    family: "bar",
    bindings: { dimension: ["channel"], measure: ["sessions"] },
    order: { source: "result" },
    top_n: null,
    axes: {
      x: { scale: "categorical", zero_baseline: true, tick_density: "normal" },
      y: { scale: "linear", zero_baseline: true, tick_density: "normal" },
    },
    legend: { position: "right", visible: true },
    formatting: { number_style: "auto", date_style: "auto", unit_source: "semantic_view" },
    color: { role: "categorical", semantic_direction: "higher_is_better" },
    thresholds: [],
    reference_lines: [],
    annotations: [],
    interactions: {
      hover: true,
      select: true,
      zoom: false,
      legend_toggle: true,
      local_filter: false,
    },
    evidence: { datum_fields: ["channel"], mark_binding: "datum" },
    responsive: { profiles: ["console"] },
    accessibility: { summary_source: "result_manifest", table_fallback: "required" },
    labels: { override: {} },
  },
};

/** `GET /analyze/results/{id}/evidence` — the ONE path to a Result's rows. */
const RESULT_EVIDENCE = {
  result_id: "qr_EXAMPLE",
  content_hash: "e".repeat(64),
  outcome: "success",
  row_count: 2,
  truncated: false,
  schema: { fields: [{ name: "channel" }, { name: "sessions" }] },
  manifest: { semantic_view_version_id: "svv_EXAMPLE" },
  rows: [
    { channel: "organic", sessions: 1240 },
    { channel: "paid", sessions: 880 },
  ],
};

function response(body: unknown, status = 200): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response;
}

/** One fetch mock that answers by path, the way the real server does. */
function mockApi(handlers: Array<[RegExp, () => Response]>) {
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

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

// ---------------------------------------------------------------------------
// Reports
// ---------------------------------------------------------------------------

it("reads nothing without an exact Project scope", () => {
  const { fetchMock } = mockApi([]);
  render(<ReportsCollection />);
  expect(screen.getByText(/No project selected/i)).toBeInTheDocument();
  expect(fetchMock).not.toHaveBeenCalled();
});

it("keeps connector seeds and configured Reports in two separate lists", async () => {
  const { calls } = mockApi([[/\/analyze\/reports$/, () => response(REPORT_COLLECTION)]]);
  render(<ReportsCollection projectId={PROJECT} />);

  expect(await screen.findByText("Weekly traffic")).toBeInTheDocument();
  // The two panels are distinct objects, and the seed is never called a Report.
  expect(screen.getByText("Configured Reports")).toBeInTheDocument();
  expect(screen.getByText("Connector report seeds")).toBeInTheDocument();
  expect(screen.getByText("overview_daily")).toBeInTheDocument();
  expect(calls.map((call) => call.url)).toEqual([`/api/projects/${PROJECT}/analyze/reports`]);
});

it("states the missing presentation contract instead of an empty chart picker", async () => {
  mockApi([[/\/analyze\/reports$/, () => response(REPORT_COLLECTION)]]);
  render(<ReportsCollection projectId={PROJECT} />);

  expect(await screen.findByText(/is not available in this deployment/i)).toBeInTheDocument();
  expect(screen.getByText(/app\.visualization_spec_versions/)).toBeInTheDocument();
  expect(screen.getByText(/app\.renderer_runtime_builds/)).toBeInTheDocument();
  expect(screen.getByText(/Story 50\.4/)).toBeInTheDocument();
  expect(screen.getByText(/Story 50\.5/)).toBeInTheDocument();
});

it("names the Chart Template as the object with no table, on a deployment whose replay contract ships", async () => {
  /*
   *  THE BANNER THAT COULD NOT SAY THE ONE TRUE THING. `ContractUnavailable`
   *  listed the two registries Stories 50.4/50.5 own; both ship, so it returned
   *  null — and the only ratified object with no table anywhere was the only one
   *  the list of missing contracts never mentioned. Reddens if the banner goes
   *  back to keying on `available` alone.
   */
  mockApi([
    [/\/analyze\/reports$/, () => response({ ...REPORT_COLLECTION, presentation_contract: CONTRACT_TODAY })],
  ]);
  render(<ReportsCollection projectId={PROJECT} />);

  expect(await screen.findByText(/cannot use every presentation kind/i)).toBeInTheDocument();
  expect(screen.getByText(/app\.visualization_template_versions/)).toBeInTheDocument();
  expect(screen.getByText(/Story 72\.1/)).toBeInTheDocument();
  // And it does not invent a missing replay contract that is not missing.
  expect(screen.queryByText(/app\.visualization_spec_versions/)).not.toBeInTheDocument();
});

it("does not announce a delivered object as absent, and does not offer a pin the server refuses", async () => {
  /*
   *  TWO DEFECTS ON ONE PANEL, both measured 2026-08-31.
   *
   *  (1) The absence banner said "No Visualization Template version and no
   *      materialized Visualization Spec version exists in this deployment" —
   *      and the Spec exists: it has a table, story 66.x materialises it, and
   *      the picker beside the sentence pins one.
   *  (2) The picker offered `Visualization Template version` with nothing behind
   *      it. The server refuses that kind (story 72.1 owns the registry), so
   *      every use of the control was a guaranteed refusal.
   */
  mockApi([[/\/analyze\/reports\/rep_EXAMPLE$/, () => response(REPORT_DETAIL)]]);
  render(<ReportWorkbench collectionHref="/analyze/reports" projectId={PROJECT} reportId="rep_EXAMPLE" tab="presentation" />);

  await screen.findByText(/This Report version references no presentation/i);
  expect(screen.queryByText(/no materialized Visualization Spec/i)).not.toBeInTheDocument();
  expect(screen.getByText(/A Chart Template version cannot be pinned here yet/i)).toBeInTheDocument();

  const template = screen.getByRole("option", { name: /Chart Template version/i });
  expect(template).toBeDisabled();
  expect(template.textContent).toMatch(/Story 72\.1/);
  // The kind that IS served stays offered, and stays selectable.
  expect(screen.getByRole("option", { name: "Visualization Spec version" })).not.toBeDisabled();
  // The retired spelling is gone from the screen: one noun, everywhere.
  expect(screen.queryByText(/Visualization Template/i)).not.toBeInTheDocument();
});

it("shows the five ratified Report tabs and refuses an unknown one", async () => {
  mockApi([[/\/analyze\/reports\/rep_EXAMPLE$/, () => response(REPORT_DETAIL)]]);
  const { rerender } = render(
    <ReportWorkbench collectionHref="/analyze/reports"
      projectId={PROJECT}
      reportId="rep_EXAMPLE"
      tab="overview"
      tabHref={(next) => `/analyze/reports/rep_EXAMPLE/tab/${next}`}
    />,
  );
  await screen.findByRole("navigation", { name: "Report" });
  for (const label of ["Overview", "Query", "Presentation", "Runs", "Versions"]) {
    expect(screen.getByRole("link", { name: label })).toBeInTheDocument();
  }
  rerender(
    <ReportWorkbench collectionHref="/analyze/reports"
      projectId={PROJECT}
      reportId="rep_EXAMPLE"
      tab="widgets"
      tabHref={(next) => `/analyze/reports/rep_EXAMPLE/tab/${next}`}
    />,
  );
  expect(await screen.findByText("Unknown tab")).toBeInTheDocument();
});

it("shows each Report run as its own Result, and no Render", async () => {
  mockApi([[/\/analyze\/reports\/rep_EXAMPLE$/, () => response(REPORT_DETAIL)]]);
  render(<ReportWorkbench collectionHref="/analyze/reports" projectId={PROJECT} reportId="rep_EXAMPLE" tab="runs" />);

  expect(await screen.findByText("qr_EXAMPLE")).toBeInTheDocument();
  expect(screen.getByText("No Render")).toBeInTheDocument();
  // THE SENTENCE, NOT THE WIRE TOKEN (76-2 review): these badges printed
  // `run.status` / `block.status` raw, which §4 forbids.
  expect(screen.getByText("Unavailable")).toBeInTheDocument();
});

it("runs a Report version through the server and reloads its evidence", async () => {
  const { calls } = mockApi([
    [/\/analyze\/report-versions\/repv_EXAMPLE\/run$/, () =>
      response({ run_id: "reprun_2", result_id: "qr_2", outcome: "unavailable" }, 202)],
    [/\/analyze\/reports\/rep_EXAMPLE$/, () => response(REPORT_DETAIL)],
  ]);
  render(<ReportWorkbench collectionHref="/analyze/reports" projectId={PROJECT} reportId="rep_EXAMPLE" tab="query" />);

  fireEvent.click(await screen.findByRole("button", { name: /Run this version/i }));
  await waitFor(() =>
    expect(calls.some((call) => call.url.endsWith("/report-versions/repv_EXAMPLE/run"))).toBe(
      true,
    ),
  );
});

it("creates a new immutable Report version for an exact presentation pin", async () => {
  const { calls } = mockApi([
    [/\/analyze\/reports\/rep_EXAMPLE\/versions$/, () => response({ id: "repv_2", version_number: 2 }, 201)],
    [
      /\/analyze\/reports\/rep_EXAMPLE$/,
      // Story 72.5, AC21: the pin is CHOSEN from the versions the server lists,
      // never typed. `app.is_exact_pin` refuses six literal words and accepts
      // every other string, and the column carries no foreign key — a typed
      // identifier was a pin the database would store and no read could resolve.
      () =>
        response({
          ...REPORT_DETAIL,
          pinnable_presentation_versions: {
            visualization_spec_version: [
              {
                version_id: "vsv_EXACT",
                label: "Sessions by channel",
                version_number: 1,
                family: "bar",
                content_hash: "e".repeat(64),
                created_at: "2026-09-01T09:00:00Z",
                kind_noun: "Visualization Spec version",
              },
            ],
          },
        }),
    ],
  ]);
  render(<ReportWorkbench collectionHref="/analyze/reports" projectId={PROJECT} reportId="rep_EXAMPLE" tab="presentation" />);

  fireEvent.change(await screen.findByLabelText("Version to pin"), {
    target: { value: "vsv_EXACT" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Save as new version" }));

  await waitFor(() => expect(calls.some((call) => call.url.endsWith("/versions"))).toBe(true));
  const create = calls.find((call) => call.url.endsWith("/versions"))!;
  expect(JSON.parse(String(create.init?.body))).toEqual({
    query_spec_version_id: "qsv_EXAMPLE",
    presentation: { kind: "visualization_spec_version", version_id: "vsv_EXACT" },
  });
});

// ---------------------------------------------------------------------------
// Notebooks
// ---------------------------------------------------------------------------

it("never falls back to a project id when none is given", () => {
  const { fetchMock } = mockApi([]);
  render(<NotebooksCollection />);
  expect(screen.getByText(/No project selected/i)).toBeInTheDocument();
  // The legacy panel fell back to `default` here and showed another workspace.
  expect(fetchMock).not.toHaveBeenCalled();
});

it("composes a Notebook from server-owned current Report versions", async () => {
  const opened = vi.fn();
  let notebookCalls = 0;
  const { calls } = mockApi([
    [/\/analyze\/notebooks\/legacy$/, () => response({ notebooks: [] })],
    [/\/analyze\/notebooks$/, () => notebookCalls++ === 0
      ? response({ notebooks: [] })
      : response({ notebook_id: "nbk_NEW", id: "nbkv_NEW", version_number: 1 }, 201)],
    [/\/analyze\/reports$/, () => response(REPORT_COLLECTION)],
  ]);
  render(<NotebooksCollection projectId={PROJECT} onOpenNotebook={opened} />);

  fireEvent.click(await screen.findByRole("button", { name: "Compose a Notebook" }));
  fireEvent.change(screen.getByLabelText("Notebook name"), { target: { value: "Weekly board" } });
  fireEvent.change(screen.getByLabelText("Add a current Report version"), { target: { value: "rep_EXAMPLE" } });
  fireEvent.click(screen.getByRole("button", { name: "Add block" }));
  fireEvent.click(screen.getByRole("button", { name: "Create Notebook" }));

  await waitFor(() => expect(opened).toHaveBeenCalledWith("nbk_NEW"));
  const create = calls.find((call) => call.url.endsWith("/notebooks") && call.init?.method === "POST")!;
  expect(JSON.parse(String(create.init?.body))).toMatchObject({
    label: "Weekly board",
    blocks: [{ block_key: "report-1", block_type: "report", report_version_id: "repv_EXAMPLE" }],
  });
});

// Composition is direct manipulation, and the keyboard keeps parity
// (`visualization-and-rendering.md:488`). The two paths are proven to produce
// the SAME wire payload, because a drag layer that moves blocks differently
// from the keyboard is two products wearing one screen.
const TWO_REPORTS = {
  ...REPORT_COLLECTION,
  reports: [
    REPORT_COLLECTION.reports[0],
    {
      ...REPORT_COLLECTION.reports[0],
      id: "rep_SECOND",
      label: "Monthly revenue",
      current_version_id: "repv_SECOND",
    },
  ],
};

/** Open the composer and add both Report versions, in listed order. */
async function composeTwoBlocks() {
  const opened = vi.fn();
  let notebookCalls = 0;
  const { calls } = mockApi([
    [/\/analyze\/notebooks\/legacy$/, () => response({ notebooks: [] })],
    [/\/analyze\/notebooks$/, () => notebookCalls++ === 0
      ? response({ notebooks: [] })
      : response({ notebook_id: "nbk_NEW", id: "nbkv_NEW", version_number: 1 }, 201)],
    [/\/analyze\/reports$/, () => response(TWO_REPORTS)],
  ]);
  render(<NotebooksCollection projectId={PROJECT} onOpenNotebook={opened} />);
  fireEvent.click(await screen.findByRole("button", { name: "Compose a Notebook" }));
  fireEvent.change(screen.getByLabelText("Notebook name"), { target: { value: "Weekly board" } });
  for (const id of ["rep_EXAMPLE", "rep_SECOND"]) {
    fireEvent.change(screen.getByLabelText("Add a current Report version"), { target: { value: id } });
    fireEvent.click(screen.getByRole("button", { name: "Add block" }));
  }
  const chip = (label: string) =>
    screen.getByRole("button", { name: new RegExp(`^${label}, block`) });
  const submit = async () => {
    fireEvent.click(screen.getByRole("button", { name: "Create Notebook" }));
    await waitFor(() => expect(opened).toHaveBeenCalledWith("nbk_NEW"));
    const create = calls.find((call) => call.url.endsWith("/notebooks") && call.init?.method === "POST")!;
    return JSON.parse(String(create.init?.body)).blocks;
  };
  return { chip, submit };
}

/** The order both paths must reach: the second block moved in front of the
 *  first, keys rebuilt from position — which is what the server reads. */
const REORDERED = [
  { block_key: "report-1", block_type: "report", report_version_id: "repv_SECOND" },
  { block_key: "report-2", block_type: "report", report_version_id: "repv_EXAMPLE" },
];

it("reorders composition blocks by pointer, dropping at a position", async () => {
  const { chip, submit } = await composeTwoBlocks();
  const second = chip("Monthly revenue");
  const firstRow = chip("Weekly traffic").closest("li")!;

  fireEvent.dragStart(second, { dataTransfer: { setData: vi.fn(), effectAllowed: "" } });
  fireEvent.dragOver(firstRow, { dataTransfer: { dropEffect: "" } });
  fireEvent.drop(firstRow, { dataTransfer: { dropEffect: "" } });

  expect(await submit()).toMatchObject(REORDERED);
});

it("reorders them from the keyboard to the same payload, on the same elements", async () => {
  const { chip, submit } = await composeTwoBlocks();

  // Pick it up — the same gesture Enter performs on the chip — and the well
  // offers the move control beside it.
  fireEvent.click(chip("Monthly revenue"));
  fireEvent.click(screen.getByRole("button", { name: "Move Monthly revenue earlier in Composition" }));

  expect(await submit()).toMatchObject(REORDERED);
});

it("removes a block from the well it sits in", async () => {
  const { chip, submit } = await composeTwoBlocks();
  fireEvent.click(screen.getByRole("button", { name: "Remove Weekly traffic from Composition" }));
  expect(chip("Monthly revenue")).toBeInTheDocument();
  expect(await submit()).toMatchObject([
    { block_key: "report-1", block_type: "report", report_version_id: "repv_SECOND" },
  ]);
});

it("composes on the Content tab too, appending a version through the route that exists", async () => {
  // `analyze-and-test.md` gives this tab the content and the Versions tab says
  // "Saving content appends a version" — but nothing on the tab could save.
  // The route was mounted server-side (`analyze_artifacts_api.py:592-596`) with
  // no caller in the console.
  let detailCalls = 0;
  const { calls } = mockApi([
    [/\/analyze\/notebooks\/nbk_EXAMPLE\/versions$/, () => response({ id: "nbkv_EXAMPLE3", version_number: 3 }, 201)],
    [/\/analyze\/notebooks\/nbk_EXAMPLE$/, () => { detailCalls += 1; return response(NOTEBOOK_DETAIL); }],
    [/\/analyze\/reports$/, () => response(TWO_REPORTS)],
  ]);
  render(<NotebookWorkbench collectionHref="/analyze/notebooks" projectId={PROJECT} notebookId="nbk_EXAMPLE" tab="content" />);

  fireEvent.click(await screen.findByRole("button", { name: "Edit content" }));
  // Nothing is asked of the Reports route until the editor is opened.
  await screen.findByLabelText("Add a current Report version");
  fireEvent.change(screen.getByLabelText("Add a current Report version"), { target: { value: "rep_EXAMPLE" } });
  fireEvent.click(screen.getByRole("button", { name: "Add block" }));
  fireEvent.click(screen.getByRole("button", { name: "Save as a new version" }));

  await waitFor(() =>
    expect(calls.some((call) => call.url.endsWith("/notebooks/nbk_EXAMPLE/versions"))).toBe(true),
  );
  const append = calls.find((call) => call.url.endsWith("/versions"))!;
  expect(JSON.parse(String(append.init?.body))).toMatchObject({
    // The existing block keeps its own key: its earlier Runs name it, and a
    // narrative block renamed `report-1` would be a lie about what it is.
    blocks: [
      { block_key: "intro", block_type: "narrative", narrative: { text: "What changed" } },
      { block_key: "report-2", block_type: "report", report_version_id: "repv_EXAMPLE" },
    ],
  });
  // The head advances: the detail is read again rather than patched in place.
  await waitFor(() => expect(detailCalls).toBeGreaterThan(1));
});

it("shows the three ratified Notebook tabs and version-pinned blocks", async () => {
  mockApi([[/\/analyze\/notebooks\/nbk_EXAMPLE$/, () => response(NOTEBOOK_DETAIL)]]);
  render(<NotebookWorkbench collectionHref="/analyze/notebooks"
      projectId={PROJECT}
      notebookId="nbk_EXAMPLE"
      tab="content"
      tabHref={(next) => `/analyze/notebooks/nbk_EXAMPLE/tab/${next}`}
    />,
  );

  await screen.findByRole("navigation", { name: "Notebook" });
  for (const label of ["Content", "Runs", "Versions"]) {
    expect(screen.getByRole("link", { name: label })).toBeInTheDocument();
  }
  expect(screen.getByText("intro")).toBeInTheDocument();
  expect(screen.getByText(/Composition v2/)).toBeInTheDocument();
});

it("pins every Run to its exact composition version, never to the current one", async () => {
  mockApi([[/\/analyze\/notebooks\/nbk_EXAMPLE$/, () => response(NOTEBOOK_DETAIL)]]);
  render(<NotebookWorkbench collectionHref="/analyze/notebooks" projectId={PROJECT} notebookId="nbk_EXAMPLE" tab="runs" />);

  // The head is on v2; the Run is on v1 and says so. A screen that showed the
  // head's version here would make an old Run unexplainable.
  expect(await screen.findByText("nbkv_EXAMPLE")).toBeInTheDocument();
  expect(screen.queryByText("nbkv_EXAMPLE2")).not.toBeInTheDocument();
});

it("shows per-block Result and the exact no-Render literal", async () => {
  mockApi([
    [/\/analyze\/notebooks\/nbk_EXAMPLE$/, () => response(NOTEBOOK_DETAIL)],
    [/\/analyze\/notebook-runs\/nbkrun_EXAMPLE$/, () => response(NOTEBOOK_RUN)],
  ]);
  render(<NotebookWorkbench collectionHref="/analyze/notebooks" projectId={PROJECT} notebookId="nbk_EXAMPLE" tab="runs" />);

  fireEvent.click(await screen.findByRole("button", { name: "Inspect" }));
  expect(await screen.findByText("qr_EXAMPLE2")).toBeInTheDocument();
  expect(screen.getByText("No Render")).toBeInTheDocument();
  expect(
    screen.getByText("No Render: no accepted presentation contract"),
  ).toBeInTheDocument();
  // A failed or unavailable block stays inspectable rather than hiding the run.
  // THE SENTENCE, NOT THE WIRE TOKEN (76-2 review): these badges printed
  // `run.status` / `block.status` raw, which §4 forbids.
  expect(screen.getByText("Unavailable")).toBeInTheDocument();
});

it("sends an idempotency key with every manual run", async () => {
  const { calls } = mockApi([
    [/\/analyze\/notebooks\/nbk_EXAMPLE\/runs$/, () =>
      response({ run_id: "nbkrun_2", idempotent_replay: false }, 202)],
    [/\/analyze\/notebooks\/nbk_EXAMPLE$/, () => response(NOTEBOOK_DETAIL)],
  ]);
  render(<NotebookWorkbench collectionHref="/analyze/notebooks" projectId={PROJECT} notebookId="nbk_EXAMPLE" tab="content" />);

  fireEvent.click(await screen.findByRole("button", { name: /Run this Notebook/i }));
  await waitFor(() => {
    const run = calls.find((call) => call.url.endsWith("/notebooks/nbk_EXAMPLE/runs"));
    expect(run).toBeDefined();
    const body = JSON.parse(String(run?.init?.body ?? "{}"));
    expect(body.idempotency_key).toMatch(/^manual-/);
    expect(body.dispatch_source).toBe("manual");
  });
});

it("keeps legacy Notebooks readable, in their own list, with their limitations", async () => {
  // AC12. `NotebooksPanel.tsx` was the only surface where these rows were
  // readable and it is mounted nowhere now, which made the requirement false
  // without anyone repealing it.
  const { calls } = mockApi([
    [/\/analyze\/notebooks\/legacy$/, () => response({ notebooks: [LEGACY_NOTEBOOK] })],
    [/\/analyze\/notebooks$/, () => response(NOTEBOOK_COLLECTION)],
  ]);
  render(<NotebooksCollection projectId={PROJECT} />);

  expect(await screen.findByText("Legacy Notebooks")).toBeInTheDocument();
  expect(screen.getByText("Legacy weekly")).toBeInTheDocument();
  expect(screen.getByText("Legacy, not versioned")).toBeInTheDocument();
  // Its ACTUAL evidence, named: this Run's envelope was never written.
  expect(screen.getByText("Deferred — never written")).toBeInTheDocument();
  expect(screen.getByText(/never reconstructed from current state/)).toBeInTheDocument();
  // The two families are two lists, each with its own heading. A merged one would
  // call a legacy row a Notebook.
  expect(screen.getAllByText("Notebooks").length).toBeGreaterThan(0);
  expect(screen.getByText("Weekly narrative")).toBeInTheDocument();
  expect(calls.some((call) => call.url.endsWith("/analyze/notebooks/legacy"))).toBe(true);
});

it("reports that a legacy share exists without ever emitting its token", async () => {
  mockApi([
    [/\/analyze\/notebooks\/legacy$/, () => response({ notebooks: [LEGACY_NOTEBOOK] })],
    [/\/analyze\/notebooks$/, () => response(NOTEBOOK_COLLECTION)],
  ]);
  const { container } = render(<NotebooksCollection projectId={PROJECT} />);

  expect(await screen.findByText("Legacy share exists")).toBeInTheDocument();
  // The server never sends the token; nothing on screen can leak one.
  expect(container.textContent).not.toMatch(/tok_|share_token|bearer/i);
});

it("says who dispatches a schedule, and whether anything has ever fired", async () => {
  const scheduled = {
    ...NOTEBOOK_DETAIL,
    schedule: {
      recurrence: "daily",
      timezone: "UTC",
      enabled: true,
      next_due_at: "2026-08-01T00:00:00Z",
      last_dispatched_at: null,
      last_run_id: null,
      last_dispatch_note: null,
      dispatcher: "server/core/scheduler.py::_run_due_notebooks",
    },
  };
  mockApi([[/\/analyze\/notebooks\/nbk_EXAMPLE$/, () => response(scheduled)]]);
  render(<NotebookWorkbench collectionHref="/analyze/notebooks" projectId={PROJECT} notebookId="nbk_EXAMPLE" tab="content" />);

  // "Enabled: Yes" on its own is what this panel used to say while nothing in the
  // repository read the schedule at all.
  expect(await screen.findByText("No scheduled Run yet")).toBeInTheDocument();
  expect(
    screen.getByText("server/core/scheduler.py::_run_due_notebooks"),
  ).toBeInTheDocument();
});

it("names the exact scheduled Run once dispatch has fired", async () => {
  const dispatched = {
    ...NOTEBOOK_DETAIL,
    schedule: {
      recurrence: "daily",
      timezone: "UTC",
      enabled: true,
      next_due_at: "2026-08-01T00:00:00Z",
      last_dispatched_at: "2026-07-31T02:00:00Z",
      last_run_id: "nbkrun_SCHEDULED",
      last_dispatch_note: null,
      dispatcher: "server/core/scheduler.py::_run_due_notebooks",
    },
  };
  mockApi([[/\/analyze\/notebooks\/nbk_EXAMPLE$/, () => response(dispatched)]]);
  render(<NotebookWorkbench collectionHref="/analyze/notebooks" projectId={PROJECT} notebookId="nbk_EXAMPLE" tab="content" />);

  expect(await screen.findByText("Dispatch is connected")).toBeInTheDocument();
  expect(screen.getByText("nbkrun_SCHEDULED")).toBeInTheDocument();
});

it("shows a refused dispatch as a refusal, never as an enabled schedule", async () => {
  const failing = {
    ...NOTEBOOK_DETAIL,
    schedule: {
      recurrence: "daily",
      timezone: "UTC",
      enabled: true,
      next_due_at: "2026-08-01T00:00:00Z",
      last_dispatched_at: null,
      last_run_id: null,
      last_dispatch_note: "ArtifactRefused: this Notebook has no version to run",
      dispatcher: "server/core/scheduler.py::_run_due_notebooks",
    },
  };
  mockApi([[/\/analyze\/notebooks\/nbk_EXAMPLE$/, () => response(failing)]]);
  render(<NotebookWorkbench collectionHref="/analyze/notebooks" projectId={PROJECT} notebookId="nbk_EXAMPLE" tab="content" />);

  expect(await screen.findByText("The last dispatch was refused")).toBeInTheDocument();
  expect(screen.getByText(/no version to run/)).toBeInTheDocument();
});

// ---------------------------------------------------------------------------
// Renders
// ---------------------------------------------------------------------------

it("separates canonical Renders from legacy snapshots and never offers replay", async () => {
  mockApi([
    [/\/analyze\/renders\/legacy$/, () => response({ snapshots: [LEGACY_SNAPSHOT] })],
    [/\/analyze\/renders$/, () =>
      response({ renders: [], next_cursor: null, contract: CONTRACT_UNAVAILABLE })],
  ]);
  render(<RendersCollection projectId={PROJECT} />);

  expect(await screen.findByText("Canonical Renders")).toBeInTheDocument();
  expect(screen.getByText("Legacy snapshots")).toBeInTheDocument();
  expect(screen.getByText("rs_EXAMPLE")).toBeInTheDocument();
  expect(screen.getByText("Legacy, unverifiable")).toBeInTheDocument();
  expect(screen.getByText("10 of 10")).toBeInTheDocument();
  // The legacy gallery opened a widget window and retried postMessage. Nothing
  // on this screen offers to open one.
  expect(screen.queryByRole("button", { name: /open widget|replay/i })).not.toBeInTheDocument();
});

// ---------------------------------------------------------------------------
// Legacy share links. These four assertions come from `RenderSharingLegacy.test.tsx`
// (Story 50.7), deleted with `RenderGalleryPage.tsx` on 2026-08-04: that screen
// was the only place the capability existed and NOTHING MOUNTED IT, so a repair
// landed four days earlier had never reached a reader. Re-pinned here, on the
// screen a route actually reaches.
// ---------------------------------------------------------------------------

/** A row exactly as the retired-but-surviving listing returns it: no token. */
const LEGACY_SHARE = {
  id: "rss_old",
  snapshot_id: "rs_EXAMPLE",
  shared_at: "2026-07-21T14:32:00Z",
  shared_by: "analyst@example.com",
  revoked_at: null,
};

async function openLegacyLinks(shares: unknown[]) {
  const mocked = mockApi([
    [/\/analyze\/renders\/legacy$/, () => response({ snapshots: [LEGACY_SNAPSHOT] })],
    [/\/analyze\/renders$/, () =>
      response({ renders: [], next_cursor: null, contract: CONTRACT_UNAVAILABLE })],
    [/\/api\/rendus\/snapshots\/rs_EXAMPLE\/shares\?/, () =>
      response({ shares, legacy: true })],
    [/\/api\/rendus\/shares\/rss_old\?/, () => response({}, 204)],
  ]);
  render(<RendersCollection projectId={PROJECT} />);
  fireEvent.click(await screen.findByRole("button", { name: "Manage links" }));
  return mocked;
}

it("shows a legacy grant's audit facts and nothing that could rebuild it", async () => {
  await openLegacyLinks([LEGACY_SHARE]);

  expect(await screen.findByText("Live, never expires")).toBeInTheDocument();
  expect(screen.getByText("analyst@example.com")).toBeInTheDocument();

  // Nothing that IS, or could rebuild, a live public grant.
  expect(document.body.innerHTML).not.toContain("share_token");
  expect(document.body.innerHTML).not.toContain("/api/rendus/shared/");
  expect(document.querySelector("a[href*='shared']")).toBeNull();
  // Creation is retired here — 410 Gone on the server. No affordance, at all.
  expect(screen.queryByRole("button", { name: /Create share link/i })).toBeNull();
});

it("points the reader at the replacement instead of leaving a dead panel", async () => {
  await openLegacyLinks([LEGACY_SHARE]);

  const copy = await screen.findByText(/Sharing tab/i);
  // The replacement's three properties are stated, so a reader learns what
  // changed rather than that something disappeared.
  expect(copy.textContent).toMatch(/expires/i);
  expect(copy.textContent).toMatch(/revocable/i);
  expect(copy.textContent).toMatch(/shown once/i);
});

it("still revokes an existing grant, behind a confirmation", async () => {
  // A retirement that also removed revocation would strand the very grants it
  // was meant to close. And revocation is irreversible FOR THE RECIPIENT, so the
  // first click must not be the one that sends it (the Epic 46 class).
  const { calls } = await openLegacyLinks([LEGACY_SHARE]);

  fireEvent.click(await screen.findByRole("button", { name: "Revoke" }));
  expect(calls.every((c) => !c.url.includes("/api/rendus/shares/"))).toBe(true);

  fireEvent.click(await screen.findByRole("button", { name: "Revoke it" }));
  await waitFor(() =>
    expect(
      calls.some(
        (c) =>
          c.url === `/api/rendus/shares/rss_old?project_id=${PROJECT}` &&
          c.init?.method === "DELETE",
      ),
    ).toBe(true),
  );
});

it("offers no revocation on a link that is already revoked", async () => {
  await openLegacyLinks([{ ...LEGACY_SHARE, revoked_at: "2026-07-25T10:00:00Z" }]);

  expect(await screen.findByText("Revoked")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Revoke" })).toBeNull();
});

it("says a legacy snapshot was never shared rather than showing an empty table", async () => {
  await openLegacyLinks([]);

  expect(await screen.findByText(/This snapshot was never shared/i)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Revoke" })).toBeNull();
});

it("says why no canonical Render can exist yet, without fabricating one", async () => {
  mockApi([
    [/\/analyze\/renders\/legacy$/, () => response({ snapshots: [] })],
    [/\/analyze\/renders$/, () =>
      response({ renders: [], next_cursor: null, contract: CONTRACT_UNAVAILABLE })],
  ]);
  render(<RendersCollection projectId={PROJECT} />);

  expect(await screen.findByText(/No canonical Render yet/i)).toBeInTheDocument();
  expect(screen.getByText(/until the replay contract above lands/i)).toBeInTheDocument();
});

it("shows the three ratified Render tabs and the ten replay pins", async () => {
  mockApi([[/\/analyze\/renders\/rnd_EXAMPLE$/, () => response(RENDER_DETAIL)]]);
  render(<RenderWorkbench collectionHref="/analyze/renders"
      projectId={PROJECT}
      renderId="rnd_EXAMPLE"
      tab="evidence"
      tabHref={(next) => `/analyze/renders/rnd_EXAMPLE/tab/${next}`}
    />,
  );

  await screen.findByRole("navigation", { name: "Render" });
  for (const label of ["Result", "Evidence", "Sharing"]) {
    expect(screen.getByRole("link", { name: label })).toBeInTheDocument();
  }
  expect(screen.getByText("vsv_EXAMPLE")).toBeInTheDocument();
  expect(screen.getByText("rt_EXAMPLE")).toBeInTheDocument();
  expect(screen.getByText("desktop_wide")).toBeInTheDocument();
});

// ---------------------------------------------------------------------------
// The preserved visual. Story 50.5's `VisualizationMount` was built, tested and
// mounted by NOTHING until 2026-08-04 — thirteen green tests over a component no
// reader could reach. These four are the ones a reader can fail.
// ---------------------------------------------------------------------------

/** A Render this deploy CAN replay: its two build pins are the ones running. */
function replayableRender() {
  const resolution = resolveRenderer("bar", 1, "console");
  return {
    ...RENDER_DETAIL,
    renderer_build_id: resolution.kind === "renderer" ? resolution.renderer.build : "",
    runtime_build_id: RUNTIME_BUILD,
    theme_version: THEME_VERSION,
    formatter_version: FORMATTER_VERSION,
    responsive_profile: "console",
  };
}

it("draws the preserved visual in page, from the Spec version the Render pins", async () => {
  const { calls } = mockApi([
    [/\/analyze\/renders\/rnd_EXAMPLE$/, () => response(replayableRender())],
    [/\/analyze\/visualization-spec-versions\/vsv_EXAMPLE$/, () => response(VIZ_SPEC_VERSION)],
    [/\/analyze\/results\/qr_EXAMPLE\/evidence\?render_id=rnd_EXAMPLE$/, () => response(RESULT_EVIDENCE)],
  ]);
  const { container } = render(
    <RenderWorkbench collectionHref="/analyze/renders" projectId={PROJECT} renderId="rnd_EXAMPLE" tab="result" />,
  );

  await waitFor(() =>
    expect(container.querySelector("[data-viz-runtime]")).not.toBeNull(),
  );
  // The runtime drew the family the SPEC names — not a default picked here.
  await waitFor(() =>
    expect(container.querySelector('[data-viz-family="bar"]')).not.toBeNull(),
  );
  expect(container.querySelector('[data-viz-chart-drawn="true"]')).not.toBeNull();
  // The spec is RESOLVED from the pinned id. A screen that accepted a spec from
  // anywhere else could draw a different picture over the same frozen Result.
  expect(calls.some((c) => c.url.includes("/visualization-spec-versions/vsv_EXAMPLE"))).toBe(true);
  // One Result path, and it is the Story 50.1 retrieval route.
  expect(calls.some((c) => c.url.includes("/results/qr_EXAMPLE/evidence"))).toBe(true);
  // The legacy gallery opened a second window for this. Nothing here does.
  expect(screen.queryByRole("button", { name: /open widget/i })).toBeNull();
});

it("submits replay feedback with the context signed for the retained Render", async () => {
  const surface = analyzeFeedbackTargets.surfaces.console;
  const interaction = surface.interactions.visualization;
  const input = surface.delivery.render_input as unknown as RenderInput;
  const feedbackRequests: Array<Record<string, unknown>> = [];
  mockApi([
    [/\/analyze\/results\/qr_FIXTURE\/evidence\?render_id=rnd_FIXTURE$/, () => response(surface.delivery.evidence)],
    [/\/test\/feedback$/, () => response(interaction.receipt)],
  ]);
  const mockedFetch = globalThis.fetch;
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (/\/test\/feedback$/.test(url)) {
      feedbackRequests.push(JSON.parse(String(init?.body)) as Record<string, unknown>);
    }
    return mockedFetch(url, init);
  }));
  render(
    <VisualizationMount
      projectId="proj_FIXTURE"
      resultId={input.result.result_id}
      renderId="rnd_FIXTURE"
      expectedResultContentHash={input.result.content_hash}
      spec={input.spec}
      pins={input.pins}
      profile="console"
      display={input.display}
    />,
  );
  fireEvent.click(await screen.findByRole("button", { name: "Target feedback at row 2, sessions" }));
  fireEvent.click(screen.getByRole("button", { name: "Helpful" }));
  fireEvent.click(screen.getByRole("button", { name: "Submit feedback" }));
  await screen.findByText("Feedback recorded.");
  expect(feedbackRequests).toHaveLength(1);
  expect(feedbackRequests[0]).toMatchObject({
    context: interaction.context,
    target: interaction.target,
    polarity: "positive",
  });
  expect(surface.delivery.evidence.result_id).toBe(interaction.authority.result_id);
  expect(input.result.content_hash).toBe(interaction.authority.result_content_hash);
  expect(interaction.authority.renderer_build_id)
    .toBe(analyzeFeedbackTargets.pins.renderer_build_id);
  expect(analyzeFeedbackTargets.stored.find(
    (row) => row.surface === "console" && row.render_id === "rnd_FIXTURE",
  )?.target).toEqual(interaction.target);
});

it("drives the real Console adapter from the same FastMCP golden as MCP and Share", async () => {
  const surface = analyzeFeedbackTargets.surfaces.console;
  const authority = surface.interactions.visualization.authority;
  const input = structuredClone(GOLDEN_RENDER_INPUT);
  const renderDetail = {
    ...replayableRender(),
    id: authority.render_id,
    result_id: input.result.result_id,
    result_content_hash: input.result.content_hash,
    visualization_spec_version_id: input.spec.visualization_spec_version_id,
    renderer_build_id: input.pins.renderer_build,
    runtime_build_id: input.pins.runtime_build,
    theme_version: input.pins.theme_version,
    formatter_version: input.pins.formatter_version,
    responsive_profile: "console",
    display_state: input.display,
  };
  const specVersion = {
    ...VIZ_SPEC_VERSION,
    id: input.spec.visualization_spec_version_id,
    spec_contract_version: input.spec.spec_contract_version,
    schema_version: input.spec.schema_version,
    family: input.spec.document.family,
    spec: input.spec.document,
  };
  mockApi([
    [/\/analyze\/renders\/rnd_FIXTURE$/, () => response(renderDetail)],
    [/\/analyze\/visualization-spec-versions\/vsv_FIXTURE$/, () => response(specVersion)],
    [/\/analyze\/results\/qr_FIXTURE\/evidence\?render_id=rnd_FIXTURE$/, () => response(surface.delivery.evidence)],
  ]);
  const { container } = render(
    <RenderWorkbench collectionHref="/analyze/renders" projectId="proj_FIXTURE" renderId="rnd_FIXTURE" tab="result" />,
  );

  await waitFor(() =>
    expect(container.querySelectorAll("[data-viz-row-key]")).toHaveLength(3),
  );
  expect(screen.getByText("Jul 28, 2026")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Target feedback at row 2, sessions" }))
    .toHaveTextContent("1,877");
  expect(renderDetail.result_content_hash).toBe(authority.result_content_hash);
  expect(renderDetail.runtime_build_id).toBe(authority.runtime_build_id);
  expect(surface.delivery.evidence.feedback_context)
    .toEqual(surface.interactions.visualization.context);
});

it("uses the Result outcome returned by the server instead of inferring success from rows", async () => {
  mockApi([
    [/\/analyze\/renders\/rnd_EXAMPLE$/, () => response(replayableRender())],
    [/\/analyze\/visualization-spec-versions\/vsv_EXAMPLE$/, () => response(VIZ_SPEC_VERSION)],
    [
      /\/analyze\/results\/qr_EXAMPLE\/evidence\?render_id=rnd_EXAMPLE$/,
      () => response({ ...RESULT_EVIDENCE, outcome: "degraded" }),
    ],
  ]);
  const { container } = render(
    <RenderWorkbench collectionHref="/analyze/renders" projectId={PROJECT} renderId="rnd_EXAMPLE" tab="result" />,
  );

  await waitFor(() =>
    expect(container.querySelector('[data-viz-state="degraded"]')).not.toBeNull(),
  );
  expect(container.querySelector("[data-viz-table-fallback]")).not.toBeNull();
});

it("preserves the server row count and truncation state in the runtime input", async () => {
  mockApi([
    [/\/analyze\/renders\/rnd_EXAMPLE$/, () => response(replayableRender())],
    [/\/analyze\/visualization-spec-versions\/vsv_EXAMPLE$/, () => response(VIZ_SPEC_VERSION)],
    [
      /\/analyze\/results\/qr_EXAMPLE\/evidence\?render_id=rnd_EXAMPLE$/,
      () => response({ ...RESULT_EVIDENCE, row_count: 7, truncated: true }),
    ],
  ]);
  const { container } = render(
    <RenderWorkbench collectionHref="/analyze/renders" projectId={PROJECT} renderId="rnd_EXAMPLE" tab="result" />,
  );

  await waitFor(() =>
    expect(container.querySelector('[data-viz-state="truncated"]')).not.toBeNull(),
  );
  expect(screen.getByText(/truncated slice of this answer/i)).toBeInTheDocument();
});

it("refuses replay when the fetched Result hash differs from the frozen Render", async () => {
  mockApi([
    [/\/analyze\/renders\/rnd_EXAMPLE$/, () => response(replayableRender())],
    [/\/analyze\/visualization-spec-versions\/vsv_EXAMPLE$/, () => response(VIZ_SPEC_VERSION)],
    [
      /\/analyze\/results\/qr_EXAMPLE\/evidence\?render_id=rnd_EXAMPLE$/,
      () => response({ ...RESULT_EVIDENCE, content_hash: "0".repeat(64) }),
    ],
  ]);
  const { container } = render(
    <RenderWorkbench collectionHref="/analyze/renders" projectId={PROJECT} renderId="rnd_EXAMPLE" tab="result" />,
  );

  const refusal = await waitFor(() => {
    const found = container.querySelector('[data-viz-state="refused"]');
    expect(found).not.toBeNull();
    return found!;
  });
  expect(refusal.textContent).toContain("no longer matches this frozen Render");
  expect(refusal.textContent).toContain("create a new Render");
  expect(container.querySelector("[data-viz-runtime]")).toBeNull();
});

it("passes all four frozen build pins instead of replacing theme and formatter", async () => {
  const placeholderPins = {
    ...replayableRender(),
    theme_version: "latest",
    formatter_version: "current",
  };
  mockApi([
    [/\/analyze\/renders\/rnd_EXAMPLE$/, () => response(placeholderPins)],
    [/\/analyze\/visualization-spec-versions\/vsv_EXAMPLE$/, () => response(VIZ_SPEC_VERSION)],
    [/\/analyze\/results\/qr_EXAMPLE\/evidence\?render_id=rnd_EXAMPLE$/, () => response(RESULT_EVIDENCE)],
  ]);
  const { container } = render(
    <RenderWorkbench collectionHref="/analyze/renders" projectId={PROJECT} renderId="rnd_EXAMPLE" tab="result" />,
  );

  const refusal = await waitFor(() => {
    const found = container.querySelector('[data-viz-state="refused"]');
    expect(found).not.toBeNull();
    return found!;
  });
  expect(refusal.textContent).toContain("pins.theme_version");
  expect(refusal.textContent).toContain('"latest"');
  expect(refusal.textContent).toContain("pins.formatter_version");
  expect(refusal.textContent).toContain('"current"');
});

it("seeds the runtime with the Render's stored display state", async () => {
  mockApi([
    [
      /\/analyze\/renders\/rnd_EXAMPLE$/,
      () => response({ ...replayableRender(), display_state: { legendHidden: ["sessions"] } }),
    ],
    [/\/analyze\/visualization-spec-versions\/vsv_EXAMPLE$/, () => response(VIZ_SPEC_VERSION)],
    [/\/analyze\/results\/qr_EXAMPLE\/evidence\?render_id=rnd_EXAMPLE$/, () => response(RESULT_EVIDENCE)],
  ]);
  const { container } = render(
    <RenderWorkbench collectionHref="/analyze/renders" projectId={PROJECT} renderId="rnd_EXAMPLE" tab="result" />,
  );

  await waitFor(() =>
    expect(
      container.querySelector('[data-viz-legend-toggle="sessions"]'),
    ).not.toBeNull(),
  );
  expect(
    container.querySelector('[data-viz-legend-toggle="sessions"]'),
  ).toHaveAttribute("aria-pressed", "true");
});

it("refuses a Render pinning a renderer build this deploy does not have, and names both", async () => {
  // `RENDER_DETAIL` pins `rb_EXAMPLE`, which this page is not running. Redrawing
  // anyway would show THIS build's picture under a frozen Render's name — the
  // exact divergence Epic 50 exists to remove. The refusal names the pinned
  // build AND the running one, so the reader knows what to deploy.
  mockApi([
    [/\/analyze\/renders\/rnd_EXAMPLE$/, () => response(RENDER_DETAIL)],
    [/\/analyze\/visualization-spec-versions\/vsv_EXAMPLE$/, () => response(VIZ_SPEC_VERSION)],
    [/\/analyze\/results\/qr_EXAMPLE\/evidence\?render_id=rnd_EXAMPLE$/, () => response(RESULT_EVIDENCE)],
  ]);
  const { container } = render(
    <RenderWorkbench collectionHref="/analyze/renders" projectId={PROJECT} renderId="rnd_EXAMPLE" tab="result" />,
  );

  const panel = await waitFor(() => {
    const found = container.querySelector('[data-viz-state="refused"]');
    expect(found).not.toBeNull();
    return found!;
  });
  expect(panel.textContent).toContain("rb_EXAMPLE");
  const running = resolveRenderer("bar", 1, "console");
  expect(panel.textContent).toContain(
    running.kind === "renderer" ? running.renderer.build : "",
  );
  expect(container.querySelector('[data-viz-chart-drawn="true"]')).toBeNull();
});

it("refuses a Render whose RUNTIME build is not the one running, and names it", async () => {
  // The renderer pin is valid here, so validation passes and the SECOND gate —
  // `checkBuildIdentity` — is the one that fires. Two distinct refusals, because
  // "this renderer no longer exists" and "this page is a different runtime" are
  // two different things to deploy your way out of.
  const otherRuntime = { ...replayableRender(), runtime_build_id: "rt_EXAMPLE" };
  mockApi([
    [/\/analyze\/renders\/rnd_EXAMPLE$/, () => response(otherRuntime)],
    [/\/analyze\/visualization-spec-versions\/vsv_EXAMPLE$/, () => response(VIZ_SPEC_VERSION)],
    [/\/analyze\/results\/qr_EXAMPLE\/evidence\?render_id=rnd_EXAMPLE$/, () => response(RESULT_EVIDENCE)],
  ]);
  const { container } = render(
    <RenderWorkbench collectionHref="/analyze/renders" projectId={PROJECT} renderId="rnd_EXAMPLE" tab="result" />,
  );

  const panel = await waitFor(() => {
    const found = container.querySelector('[data-viz-state="refused"]');
    expect(found).not.toBeNull();
    return found!;
  });
  expect(panel.textContent).toContain("rt_EXAMPLE");
  expect(panel.textContent).toContain(RUNTIME_BUILD);
  expect(container.querySelector('[data-viz-chart-drawn="true"]')).toBeNull();
});

it("states the layout substitution rather than silently drawing another one", async () => {
  // `responsive_profile` is stored as free TEXT (`154_reports_notebooks_renders.sql:411`
  // constrains it to a non-placeholder, not to the enum), so a value the runtime
  // does not carry can genuinely be pinned. It is SAID, per `responsive.ts`.
  mockApi([
    [/\/analyze\/renders\/rnd_EXAMPLE$/, () => response(RENDER_DETAIL)],
    [/\/analyze\/visualization-spec-versions\/vsv_EXAMPLE$/, () => response(VIZ_SPEC_VERSION)],
    [/\/analyze\/results\/qr_EXAMPLE\/evidence\?render_id=rnd_EXAMPLE$/, () => response(RESULT_EVIDENCE)],
  ]);
  render(<RenderWorkbench collectionHref="/analyze/renders" projectId={PROJECT} renderId="rnd_EXAMPLE" tab="result" />);

  expect(
    await screen.findByText(/This Render pins a layout this build does not have/i),
  ).toBeInTheDocument();
  expect(screen.getByText(/"desktop_wide" is not a responsive profile/i)).toBeInTheDocument();
});

it("refuses to draw a Render whose runtime material was retired, and says so", async () => {
  const retired = {
    ...RENDER_DETAIL,
    replayable: false,
    retention_actions: [
      {
        action: "runtime_unavailable",
        reason: "renderer build removed from the registry",
        policy_ref: "visualization-and-rendering.md:318",
        actor: "platform",
        created_at: "2026-08-01T00:00:00Z",
      },
    ],
  };
  const { calls } = mockApi([
    [/\/analyze\/renders\/rnd_EXAMPLE$/, () => response(retired)],
    [/\/analyze\/visualization-spec-versions\//, () => response(VIZ_SPEC_VERSION)],
    [/\/evidence$/, () => response(RESULT_EVIDENCE)],
  ]);
  const { container } = render(
    <RenderWorkbench collectionHref="/analyze/renders" projectId={PROJECT} renderId="rnd_EXAMPLE" tab="result" />,
  );

  expect(await screen.findByText(/This Render cannot be replayed/i)).toBeInTheDocument();
  expect(container.querySelector("[data-viz-runtime]")).toBeNull();
  // It does not merely hide the canvas: it never asks for the material either.
  expect(calls.every((c) => !c.url.includes("visualization-spec-versions"))).toBe(true);
  expect(calls.every((c) => !c.url.includes("/evidence"))).toBe(true);
});

it("says the rows are gone rather than drawing an empty chart, when the payload was not retained", async () => {
  const dropped = { ...RENDER_DETAIL, result_payload_retained: false };
  const { calls } = mockApi([
    [/\/analyze\/renders\/rnd_EXAMPLE$/, () => response(dropped)],
    [/\/analyze\/visualization-spec-versions\//, () => response(VIZ_SPEC_VERSION)],
    [/\/evidence$/, () => response(RESULT_EVIDENCE)],
  ]);
  const { container } = render(
    <RenderWorkbench collectionHref="/analyze/renders" projectId={PROJECT} renderId="rnd_EXAMPLE" tab="result" />,
  );

  expect(await screen.findByText(/The Result payload was not retained/i)).toBeInTheDocument();
  expect(container.querySelector("[data-viz-runtime]")).toBeNull();
  expect(calls.every((c) => !c.url.includes("/evidence"))).toBe(true);
});

it("names the missing Spec version instead of falling back to another spec", async () => {
  const { calls } = mockApi([
    [/\/analyze\/renders\/rnd_EXAMPLE$/, () => response(RENDER_DETAIL)],
    [/\/evidence$/, () => response(RESULT_EVIDENCE)],
  ]);
  const { container } = render(
    <RenderWorkbench collectionHref="/analyze/renders" projectId={PROJECT} renderId="rnd_EXAMPLE" tab="result" />,
  );

  expect(
    await screen.findByText(/Visualization Spec version not found/i),
  ).toBeInTheDocument();
  expect(screen.getByText(/vsv_EXAMPLE/)).toBeInTheDocument();
  expect(container.querySelector("[data-viz-runtime]")).toBeNull();
  // A missing spec must not become a request for rows nobody can draw.
  expect(calls.every((c) => !c.url.includes("/evidence"))).toBe(true);
});

it("offers no sharing action when the server says the contract is unavailable", async () => {
  /** Story 50.7 delivered the Sharing surface (`RenderSharing.tsx`), so this test
   *  no longer pins "sharing does not exist" -- it pins the state the server can
   *  still report: a Render whose replay contract is absent gets the reason and NO
   *  create affordance, rather than a button that could only fail. The Sharing
   *  surface's own behaviour is `RenderSharingTab.test.tsx`. */
  const { calls } = mockApi([
    [/\/analyze\/renders\/rnd_EXAMPLE$/, () => response(RENDER_DETAIL)],
  ]);
  render(<RenderWorkbench collectionHref="/analyze/renders" projectId={PROJECT} renderId="rnd_EXAMPLE" tab="sharing" />);

  expect(
    await screen.findByText(/External sharing is not available for this Render/i),
  ).toBeInTheDocument();
  expect(screen.getByText(/owned by Story 50\.7/i)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /share|copy link|create link/i })).toBeNull();
  expect(calls.every((call) => !call.url.includes("share"))).toBe(true);
});
