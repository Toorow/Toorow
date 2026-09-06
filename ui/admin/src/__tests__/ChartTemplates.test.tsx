/**
 * Story 72.5 — the Chart Template lens, its workbench, and the two rules that
 * make the surface honest.
 *
 * Every response below is the shape `server/core/chart_templates_api.py` really
 * returns. What is asserted is not that the screens render:
 *
 *   * AC18 — the THREE emptinesses are three distinct screens, each naming the
 *     gesture that fills it, and none of them names a deployment state or a
 *     table name;
 *   * the incompatible rows do NOT disappear when a Result narrows the list —
 *     each says which predicate is missing, in the SERVER's words;
 *   * AC19 — applying a template happens on this screen and produces a
 *     Visualization Spec version of the Project;
 *   * AC22 — a Result offers a compatible template BEFORE the fallback, and the
 *     fallback is the named case.
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import ChartTemplates, {
  NO_COMPATIBLE_TEMPLATE,
  NO_PROJECT_TEMPLATE,
  NO_SEED_AVAILABLE,
} from "../analyze/templates/ChartTemplates";
import ChartTemplateWorkbench, {
  archiveConsequence,
} from "../analyze/templates/ChartTemplateWorkbench";
import StartingPointChoice from "../analyze/builder/StartingPointChoice";
import { FALLBACK_REASON } from "../analyze/builder/seedVisualization";

const PROJECT = "proj_EXAMPLE";

const REQUIRES = [
  {
    well: "measure",
    well_label: "Measure",
    accepts: ["measure"],
    min: 1,
    max_cardinality: null,
  },
  {
    well: "dimension",
    well_label: "Dimension",
    accepts: ["dimension"],
    min: 1,
    max_cardinality: 12,
  },
];

const TEMPLATE_ROW = {
  id: "vtpl_EXAMPLE",
  label: "Spend by channel",
  description: null,
  answers_question: "Where did the spend go?",
  family: "bar",
  family_label: "Bar",
  requires: REQUIRES,
  requires_sentence:
    "one dimension with at most 12 distinct values in Dimension, one measure in Measure",
  readable: true,
  unreadable_reason: null,
  content_hash: "b".repeat(64),
  seed_origin: "platform_seed",
  seed: null,
  origin_label: "Shipped with toorow",
  current_version_id: "vtv_EXAMPLE",
  version_count: 1,
  archived: false,
  archived_at: null,
  created_by: "owner@example.com",
  created_at: "2026-09-01T09:00:00Z",
  updated_at: "2026-09-01T09:00:00Z",
};

const RESULT_CHOICE = {
  result_id: "qr_EXAMPLE",
  outcome: "success",
  row_count: 2,
  created_at: "2026-09-01T09:00:00Z",
  question: "Spend by channel, last 30 days",
};

const EMPTY_COLLECTION = {
  templates: [],
  available_seeds: [],
  unusable_seeds: [],
  connected_modules: [],
  results: [],
  result_id: null,
  compatible_count: null,
};

function response(body: unknown, status = 200): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response;
}

function mockApi(handlers: Array<[RegExp, (url: string) => Response]>) {
  const calls: string[] = [];
  const fetchMock = vi.fn((url: string) => {
    calls.push(url);
    for (const [pattern, produce] of handlers) {
      if (pattern.test(url)) return Promise.resolve(produce(url));
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
// AC18 — three emptinesses, three sentences.
// ---------------------------------------------------------------------------

it("names the gesture for a Project with no Chart Template, and for a Project with no seed", async () => {
  mockApi([[/\/analyze\/chart-templates/, () => response(EMPTY_COLLECTION)]]);
  render(<ChartTemplates projectId={PROJECT} />);

  expect(await screen.findByText(NO_PROJECT_TEMPLATE.title)).toBeInTheDocument();
  expect(screen.getByText(NO_SEED_AVAILABLE.title)).toBeInTheDocument();
  // They are two different sentences on the same screen at the same time. A
  // single "nothing here" would have collapsed two facts a person acts on
  // differently.
  expect(NO_PROJECT_TEMPLATE.title).not.toBe(NO_SEED_AVAILABLE.title);
});

it("names no deployment state and no table in any of its three empty states", () => {
  const forbidden = [
    /migration/i,
    /app\./,
    /_id\b/,
    /deployed/i,
    /not (yet )?(built|delivered|landed)/i,
    /story \d/i,
  ];
  for (const state of [NO_PROJECT_TEMPLATE, NO_SEED_AVAILABLE, NO_COMPATIBLE_TEMPLATE]) {
    const text = `${state.title} ${state.description}`;
    for (const pattern of forbidden) {
      expect(text).not.toMatch(pattern);
    }
  }
});

it("says nothing fits the open Result, and still shows every template with its missing predicate", async () => {
  const incompatible = {
    ...TEMPLATE_ROW,
    verdict: {
      state: "incompatible",
      template_version_id: "vtv_EXAMPLE",
      result_id: "qr_EXAMPLE",
      family: "bar",
      answers_question: "Where did the spend go?",
      unmet: [
        {
          code: "missing_role",
          message: "this template needs one measure in Measure, and this Result carries none",
          subject: "/requires/measure/min",
          remedy: "Ask the question again in Explore with one measure.",
        },
      ],
      unreadable: null,
    },
  };
  mockApi([
    [
      /\/analyze\/chart-templates/,
      () =>
        response({
          ...EMPTY_COLLECTION,
          templates: [incompatible],
          results: [RESULT_CHOICE],
          result_id: "qr_EXAMPLE",
          compatible_count: 0,
        }),
    ],
  ]);
  render(<ChartTemplates projectId={PROJECT} resultId="qr_EXAMPLE" />);

  expect(await screen.findByText(NO_COMPATIBLE_TEMPLATE.title)).toBeInTheDocument();
  // The row is STILL THERE, and it says why. A template that vanished would be
  // a catalogue silently rewriting itself.
  expect(screen.getByText("Where did the spend go?")).toBeInTheDocument();
  expect(
    screen.getByText(/this template needs one measure in Measure/),
  ).toBeInTheDocument();
});

it("prints the requirement sentence the SERVER composed, never one of its own", async () => {
  mockApi([
    [/\/analyze\/chart-templates/, () => response({ ...EMPTY_COLLECTION, templates: [TEMPLATE_ROW] })],
  ]);
  render(<ChartTemplates projectId={PROJECT} />);

  expect(await screen.findByText(TEMPLATE_ROW.requires_sentence)).toBeInTheDocument();
  // The provenance is the server's label too — never the stored token.
  expect(screen.getByText("Shipped with toorow")).toBeInTheDocument();
  expect(screen.queryByText("platform_seed")).not.toBeInTheDocument();
});

// ---------------------------------------------------------------------------
// AC19 — the act happens here.
// ---------------------------------------------------------------------------

const DETAIL = {
  ...TEMPLATE_ROW,
  versions: [
    {
      id: "vtv_EXAMPLE",
      version_number: 1,
      family: "bar",
      spec_contract_version: "chart-template.v1",
      schema_version: 1,
      document: {},
      content_hash: "b".repeat(64),
      predecessor_version_id: null,
      proposed_by: "person",
      created_by: "owner@example.com",
      created_at: "2026-09-01T09:00:00Z",
    },
  ],
  used_by: { reports: [], visualizations: [] },
  results: [RESULT_CHOICE],
  verdict: {
    state: "compatible",
    template_version_id: "vtv_EXAMPLE",
    result_id: "qr_EXAMPLE",
    family: "bar",
    answers_question: "Where did the spend go?",
    unmet: [],
    unreadable: null,
  },
  result_id: "qr_EXAMPLE",
};

const VOCABULARY = {
  contract_version: "chart-template.v1",
  schema_version: 1,
  max_bytes: 32768,
  wells: [{ name: "measure", label: "Measure" }],
  roles: ["measure", "dimension", "time", "classification"],
  available_roles: ["dimension", "measure"],
  families: ["table", "bar"],
  // Each family with its own wells: what the Presentation tab edits against.
  // `ChartTemplateEditor.test.tsx` is where that tab is asserted.
  family_catalogue: [
    {
      id: "bar",
      label: "Bar",
      description: "A measure compared across the members of one dimension.",
      wells: [
        {
          name: "measure",
          label: "Measure",
          accepts: ["measure"],
          required: true,
          max_members: 3,
          max_cardinality: null,
          available: true,
          unavailable_reason: null,
          unavailable_owner: null,
        },
      ],
    },
  ],
  deferred_families: [],
  responsive_profiles: ["console"],
  document_keys: ["family", "requires"],
  subtracted_from_visualization_spec: { bindings: "requires" },
  derived_from: "visualization-spec.v1",
};

it("applies the template to the chosen Result without leaving the screen", async () => {
  const applied = {
    visualization_id: "vis_EXAMPLE",
    id: "vsv_EXAMPLE",
    version_number: 1,
    content_hash: "c".repeat(64),
    created_at: "2026-09-01T09:05:00Z",
    family: "bar",
    query_spec_id: "qs_EXAMPLE",
    query_spec_version_id: "qsv_EXAMPLE",
    proposed_by: "person",
    spec: { spec_contract_version: "visualization-spec.v1", schema_version: 1, family: "bar" },
    materialized_from_template_version_id: "vtv_EXAMPLE",
    verdict: DETAIL.verdict,
    result_id: "qr_EXAMPLE",
  };
  const { calls } = mockApi([
    [/chart-template-versions\/[^/]+\/apply/, () => response(applied, 201)],
    [/chart-template-vocabulary/, () => response(VOCABULARY)],
    [/\/analyze\/chart-templates\//, () => response(DETAIL)],
    // The preview mounts the shared runtime, which retrieves the Result through
    // the ONE retrieval route. Answering 404 keeps this test about the act.
    [/\/results\//, () => response({ code: "not_found", message: "" }, 404)],
  ]);

  render(
    <ChartTemplateWorkbench collectionHref="/analyze/reports" projectId={PROJECT} templateId="vtpl_EXAMPLE" tab="compatibility" />,
  );

  const picker = await screen.findByTestId("chart-template-compatibility-result");
  fireEvent.change(picker, { target: { value: "qr_EXAMPLE" } });
  const apply = await screen.findByTestId("chart-template-apply");
  await waitFor(() => expect(apply).not.toBeDisabled());
  fireEvent.click(apply);

  expect(
    await screen.findByText(/A Visualization of this Project was created/),
  ).toBeInTheDocument();
  const applyCall = calls.find((url) => url.includes("/apply"));
  expect(applyCall).toContain("vtv_EXAMPLE");
});

it("refuses to offer the act when the verdict is not compatible", async () => {
  const unavailable = {
    ...DETAIL,
    verdict: {
      state: "unavailable",
      template_version_id: "vtv_EXAMPLE",
      result_id: "qr_EXAMPLE",
      family: null,
      answers_question: "",
      unmet: [],
      unreadable: {
        code: "result_unreadable",
        message: "this Result does not resolve in this Project, so what it offers cannot be read",
        subject: null,
        remedy: "Choose a Result of this Project.",
      },
    },
  };
  mockApi([
    [/chart-template-vocabulary/, () => response(VOCABULARY)],
    [/\/analyze\/chart-templates\//, () => response(unavailable)],
  ]);

  render(
    <ChartTemplateWorkbench collectionHref="/analyze/reports" projectId={PROJECT} templateId="vtpl_EXAMPLE" tab="compatibility" />,
  );

  // "could not be judged" is NOT "does not fit": the two are different states
  // and a person acts on them differently.
  expect(await screen.findByText(/This pair could not be judged/)).toBeInTheDocument();
  expect(screen.getByTestId("chart-template-apply")).toBeDisabled();
});

it("declares the four tabs the ratified table declares, in its order", async () => {
  mockApi([
    [/chart-template-vocabulary/, () => response(VOCABULARY)],
    [/\/analyze\/chart-templates\//, () => response(DETAIL)],
  ]);
  render(<ChartTemplateWorkbench collectionHref="/analyze/reports" projectId={PROJECT} templateId="vtpl_EXAMPLE" tab="overview" />);

  await screen.findAllByText("Overview");
  for (const label of ["Overview", "Presentation", "Compatibility", "Versions"]) {
    expect(screen.getAllByText(label).length).toBeGreaterThan(0);
  }
});

it("renders the well vocabulary the SERVER serves, and states its absence rather than inventing one", async () => {
  mockApi([
    [/chart-template-vocabulary/, () => response({ code: "unavailable", message: "" }, 503)],
    [/\/analyze\/chart-templates\//, () => response(DETAIL)],
  ]);
  render(
    <ChartTemplateWorkbench collectionHref="/analyze/reports" projectId={PROJECT} templateId="vtpl_EXAMPLE" tab="presentation" />,
  );

  expect(
    await screen.findByText(/The document vocabulary could not be read/),
  ).toBeInTheDocument();
});

// ---------------------------------------------------------------------------
// AC22 — the choice comes before the fallback.
// ---------------------------------------------------------------------------

it("offers a compatible Chart Template before anything falls back to a table", async () => {
  const compatible = {
    ...TEMPLATE_ROW,
    verdict: { ...DETAIL.verdict },
  };
  mockApi([
    [
      /\/analyze\/chart-templates/,
      () =>
        response({
          ...EMPTY_COLLECTION,
          templates: [compatible],
          result_id: "qr_EXAMPLE",
          compatible_count: 1,
        }),
    ],
  ]);
  const started = vi.fn();
  render(
    <StartingPointChoice
      projectId={PROJECT}
      resultId="qr_EXAMPLE"
      busy={false}
      onStartFromTemplate={started}
    />,
  );

  const select = await screen.findByTestId("starting-point-choice-select");
  fireEvent.change(select, { target: { value: "vtv_EXAMPLE" } });
  fireEvent.click(screen.getByTestId("starting-point-choice-start"));
  expect(started).toHaveBeenCalledWith("vtv_EXAMPLE");
});

it("names the fallback instead of taking it silently when nothing fits", async () => {
  mockApi([
    [
      /\/analyze\/chart-templates/,
      () => response({ ...EMPTY_COLLECTION, result_id: "qr_EXAMPLE", compatible_count: 0 }),
    ],
  ]);
  render(
    <StartingPointChoice
      projectId={PROJECT}
      resultId="qr_EXAMPLE"
      busy={false}
      onStartFromTemplate={vi.fn()}
    />,
  );

  // `Status` renders its `title` for the assistive layer; the sentence a person
  // reads is the child, and it is the named fallback reason itself.
  expect(await screen.findByText(new RegExp(FALLBACK_REASON.slice(0, 48)))).toBeInTheDocument();
});

it("says nobody knows rather than nothing fits when the catalogue cannot be read", async () => {
  mockApi([[/\/analyze\/chart-templates/, () => response({ code: "error", message: "" }, 500)]]);
  render(
    <StartingPointChoice
      projectId={PROJECT}
      resultId="qr_EXAMPLE"
      busy={false}
      onStartFromTemplate={vi.fn()}
    />,
  );

  expect(
    await screen.findByText(/Which starting points fit this Result is unknown/),
  ).toBeInTheDocument();
});

it("asks nothing when there is no exact Result to judge against", () => {
  const { fetchMock } = mockApi([]);
  render(
    <StartingPointChoice
      projectId={PROJECT}
      resultId={null}
      busy={false}
      onStartFromTemplate={vi.fn()}
    />,
  );
  expect(fetchMock).not.toHaveBeenCalled();
});

// ---------------------------------------------------------------------------
// AI-351 — archive and restore, from the screen that names them.
//
// `append_chart_template_version` has refused an archived head since story 72.5
// with the remedy "List archived templates and restore this one first" — two
// gestures no route carried and no control offered. What is asserted here is
// that the gestures REACH the server, that the consequence is named BEFORE the
// archive, and that nothing about an archived template reads as a deletion.
// ---------------------------------------------------------------------------

const CONSUMED_DETAIL = {
  ...DETAIL,
  used_by: {
    reports: [
      { report_id: "rep_EXAMPLE", label: "Weekly spend", report_version_id: "rv_EXAMPLE" },
      { report_id: "rep_OTHER", label: "Channel mix", report_version_id: "rv_OTHER" },
    ],
    visualizations: [
      { visualization_id: "viz_EXAMPLE", visualization_spec_version_id: "vsv_EXAMPLE" },
    ],
  },
};

const ARCHIVED_DETAIL = {
  ...DETAIL,
  archived: true,
  archived_at: "2026-09-01T10:00:00Z",
};

it("archives from the workbench, and the confirmation counts what already uses it", async () => {
  const { calls } = mockApi([
    [/chart-template-vocabulary/, () => response(VOCABULARY)],
    [/\/archive$/, () => response({ template_id: "vtpl_EXAMPLE", archived: true, archived_at: "2026-09-01T10:00:00Z", unchanged: false })],
    [/\/analyze\/chart-templates\//, () => response(CONSUMED_DETAIL)],
  ]);

  render(<ChartTemplateWorkbench collectionHref="/analyze/reports" projectId={PROJECT} templateId="vtpl_EXAMPLE" tab="overview" />);

  const archive = await screen.findByTestId("chart-template-archive");
  // THE CONSEQUENCE IS ON THE SCREEN BEFORE THE CLICK, counted from `used_by`
  // and not from a guess: two Report versions and one Visualization.
  expect(
    screen.getByText(/2 Report versions and 1 Visualization already use this template/),
  ).toBeInTheDocument();
  fireEvent.click(archive);

  // And again in the confirmation, with what SURVIVES named beside it.
  const dialog = await screen.findByTestId("chart-template-archive-confirm");
  expect(dialog).toHaveTextContent(/a pin already made goes on resolving/);

  fireEvent.click(within(dialog).getByText("Archive it", { selector: "button" }));

  await waitFor(() =>
    expect(calls.some((url) => url.endsWith("/chart-templates/vtpl_EXAMPLE/archive"))).toBe(true),
  );
});

it("names how many consumers the archive would affect, and never as a deletion", () => {
  const sentence = archiveConsequence(CONSUMED_DETAIL as never);
  expect(sentence).toMatch(/2 Report versions and 1 Visualization/);
  // Singular and plural are both said correctly: "1 Report version", never "1
  // Report versions" — the count is read, so its grammar is too.
  expect(
    archiveConsequence({
      ...DETAIL,
      used_by: {
        reports: [{ report_id: "r", label: "l", report_version_id: "rv" }],
        visualizations: [],
      },
    } as never),
  ).toMatch(/1 Report version already uses|1 Report version /);
  // NOTHING ANYWHERE CALLS IT A DELETION. The database refuses to delete a head
  // whose versions are evidence, and the sentence must not promise otherwise.
  for (const text of [
    sentence,
    archiveConsequence(DETAIL as never),
  ]) {
    expect(text).not.toMatch(/delete|remove|erase/i);
  }
});

it("says an archived template keeps its versions, and offers the way back", async () => {
  const { calls } = mockApi([
    [/chart-template-vocabulary/, () => response(VOCABULARY)],
    [/\/restore$/, () => response({ template_id: "vtpl_EXAMPLE", archived: false, archived_at: null, unchanged: false })],
    [/\/analyze\/chart-templates\//, () => response(ARCHIVED_DETAIL)],
  ]);

  render(<ChartTemplateWorkbench collectionHref="/analyze/reports" projectId={PROJECT} templateId="vtpl_EXAMPLE" tab="overview" />);

  const banner = await screen.findByTestId("chart-template-archived-banner");
  expect(banner).toHaveTextContent(/Nothing was deleted/);
  // The versions are still LISTED, not hidden behind the state.
  expect(screen.getByText("1")).toBeInTheDocument();

  fireEvent.click(screen.getByTestId("chart-template-restore"));
  await waitFor(() =>
    expect(calls.some((url) => url.endsWith("/chart-templates/vtpl_EXAMPLE/restore"))).toBe(true),
  );
});

it("does not offer to compose a version an archived head would refuse", async () => {
  mockApi([
    [/chart-template-vocabulary/, () => response(VOCABULARY)],
    [/\/analyze\/chart-templates\//, () => response(ARCHIVED_DETAIL)],
  ]);

  render(
    <ChartTemplateWorkbench collectionHref="/analyze/reports" projectId={PROJECT} templateId="vtpl_EXAMPLE" tab="presentation" />,
  );

  expect(await screen.findByTestId("chart-template-presentation-archived")).toBeInTheDocument();
  // The editor is not rendered at all: a document composed here would be refused
  // by `append_chart_template_version` after the work, not before it.
  expect(screen.queryByTestId("chart-template-save")).not.toBeInTheDocument();
});

it("lists archived templates on request, and marks each one on its own row", async () => {
  const archivedRow = { ...TEMPLATE_ROW, id: "vtpl_ARCHIVED", archived: true, archived_at: "2026-09-01T10:00:00Z" };
  const { calls } = mockApi([
    [
      /include_archived=true/,
      () => response({ ...EMPTY_COLLECTION, templates: [TEMPLATE_ROW, archivedRow] }),
    ],
    [/\/analyze\/chart-templates/, () => response({ ...EMPTY_COLLECTION, templates: [TEMPLATE_ROW] })],
  ]);

  render(<ChartTemplates projectId={PROJECT} />);
  await screen.findByText(TEMPLATE_ROW.requires_sentence);
  // Before the gesture, the archived one is simply not there.
  expect(screen.queryByText("Archived")).not.toBeInTheDocument();

  fireEvent.click(screen.getByTestId("chart-templates-show-archived"));

  await waitFor(() => expect(screen.getByText("Archived")).toBeInTheDocument());
  expect(calls.some((url) => url.includes("include_archived=true"))).toBe(true);
  expect(screen.getByText(/1 archived template is listed below/)).toBeInTheDocument();
});
