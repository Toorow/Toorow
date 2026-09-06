/**
 * AI-347 — the Presentation tab EDITS the template document, and saves a
 * successor version without leaving the workbench.
 *
 * What is asserted is not that a form renders:
 *
 *   * the act happens here — a successor version is posted to
 *     `POST /chart-templates/{id}/versions` and the screen says so. Neutralise
 *     that route and this test goes red;
 *   * a server refusal (the 72.2 grammar's own 422) is read back as the GESTURE
 *     that repairs it, anchored on the well it is about, and the JSON pointer is
 *     never printed;
 *   * a seed says it is about to become project-owned BEFORE the gesture, not
 *     after it;
 *   * choosing a family REDUCES the next question: only that family's wells are
 *     offered, and a well it does not declare is not on the screen;
 *   * with no family to write with, the empty state names the gesture instead of
 *     showing a form nothing could be saved from.
 *
 * Every response below is the shape `server/core/chart_templates_api.py` really
 * returns, and the vocabulary is `visualization_templates.template_vocabulary()`.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import ChartTemplateWorkbench from "../analyze/templates/ChartTemplateWorkbench";
import { NO_FAMILY_TO_WRITE_WITH } from "../analyze/templates/TemplateDocumentEditor";

const PROJECT = "proj_EXAMPLE";

const DOCUMENT = {
  spec_contract_version: "chart-template.v1",
  schema_version: 1,
  family: "bar",
  answers_question: "How does one measure compare across a few categories?",
  requires: {
    measure: { min: 1, max: 1 },
    dimension: { min: 1, max: 1, max_cardinality: 12 },
  },
};

/** `bar` and `kpi`, exactly as the shipped registry declares them. */
const FAMILY_CATALOGUE = [
  {
    id: "kpi",
    label: "KPI",
    description: "One governed measure as a single figure, with its disclosures.",
    wells: [
      {
        name: "measure",
        label: "Measure",
        accepts: ["measure"],
        required: true,
        max_members: 1,
        max_cardinality: null,
        available: true,
        unavailable_reason: null,
        unavailable_owner: null,
      },
    ],
  },
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
      {
        name: "dimension",
        label: "Dimension",
        accepts: ["dimension"],
        required: true,
        max_members: 1,
        max_cardinality: 50,
        available: true,
        unavailable_reason: null,
        unavailable_owner: null,
      },
      {
        name: "time",
        label: "Time",
        accepts: ["time"],
        required: false,
        max_members: 1,
        max_cardinality: null,
        available: false,
        unavailable_reason:
          "Time and classification roles are not yet carried by the compiled Semantic View, so this group cannot be populated. The semantic compiler owns that field.",
        unavailable_owner: "Semantic compiler (Epics 47 / 49)",
      },
      {
        name: "series",
        label: "Series",
        accepts: ["dimension"],
        required: false,
        max_members: 1,
        max_cardinality: 8,
        available: true,
        unavailable_reason: null,
        unavailable_owner: null,
      },
    ],
  },
];

const VOCABULARY = {
  contract_version: "chart-template.v1",
  schema_version: 1,
  max_bytes: 32768,
  wells: [
    { name: "measure", label: "Measure" },
    { name: "dimension", label: "Dimension" },
    { name: "time", label: "Time" },
    { name: "series", label: "Series" },
  ],
  roles: ["measure", "dimension", "time", "classification"],
  available_roles: ["dimension", "measure"],
  families: ["kpi", "bar"],
  family_catalogue: FAMILY_CATALOGUE,
  deferred_families: [],
  responsive_profiles: ["console", "share"],
  document_keys: [
    "spec_contract_version",
    "schema_version",
    "family",
    "requires",
    "axes",
    "legend",
    "formatting",
    "color",
    "interactions",
    "evidence",
    "responsive",
    "accessibility",
    "labels",
    "answers_question",
  ],
  subtracted_from_visualization_spec: { bindings: "requires", annotations: "evidence" },
  derived_from: "visualization-spec.v1",
};

const DETAIL = {
  id: "vtpl_EXAMPLE",
  label: "Spend by channel",
  description: null,
  answers_question: DOCUMENT.answers_question,
  family: "bar",
  family_label: "Bar",
  requires: [
    { well: "measure", well_label: "Measure", accepts: ["measure"], min: 1, max_cardinality: null },
    {
      well: "dimension",
      well_label: "Dimension",
      accepts: ["dimension"],
      min: 1,
      max_cardinality: 12,
    },
  ],
  requires_sentence: "one measure in Measure, one dimension in Dimension",
  readable: true,
  unreadable_reason: null,
  content_hash: "b".repeat(64),
  seed_origin: "project",
  seed: null,
  origin_label: "This Project",
  current_version_id: "vtv_EXAMPLE",
  version_count: 1,
  archived: false,
  archived_at: null,
  created_by: "owner@example.com",
  created_at: "2026-09-01T09:00:00Z",
  updated_at: "2026-09-01T09:00:00Z",
  versions: [
    {
      id: "vtv_EXAMPLE",
      version_number: 1,
      family: "bar",
      spec_contract_version: "chart-template.v1",
      schema_version: 1,
      document: DOCUMENT,
      content_hash: "b".repeat(64),
      predecessor_version_id: null,
      proposed_by: "person",
      created_by: "owner@example.com",
      created_at: "2026-09-01T09:00:00Z",
    },
  ],
  used_by: { reports: [], visualizations: [] },
  results: [],
  verdict: null,
  result_id: null,
};

const APPENDED = {
  template_id: "vtpl_EXAMPLE",
  version_id: "vtv_SUCCESSOR",
  version_number: 2,
  content_hash: "c".repeat(64),
  family: "bar",
  predecessor_version_id: "vtv_EXAMPLE",
  seed_origin: "project",
  became_project_owned: false,
};

function response(body: unknown, status = 200): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response;
}

type Handler = [RegExp, (url: string, init?: RequestInit) => Response];

function mockApi(handlers: Handler[]) {
  const calls: { url: string; init?: RequestInit }[] = [];
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

/** The reads the Presentation tab needs, plus whatever the case under test adds. */
function reads(vocabulary: unknown = VOCABULARY, detail: unknown = DETAIL): Handler[] {
  return [
    [/chart-template-vocabulary/, () => response(vocabulary)],
    [/\/analyze\/chart-templates\//, () => response(detail)],
  ];
}

function openPresentation() {
  return render(
    <ChartTemplateWorkbench collectionHref="/analyze/reports" projectId={PROJECT} templateId="vtpl_EXAMPLE" tab="presentation" />,
  );
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

// ---------------------------------------------------------------------------
// The act happens here.
// ---------------------------------------------------------------------------

it("saves a successor version without leaving the workbench", async () => {
  const { calls } = mockApi([
    [/\/chart-templates\/[^/]+\/versions$/, () => response(APPENDED, 201)],
    ...reads(),
  ]);
  openPresentation();

  const question = await screen.findByTestId("chart-template-question");
  fireEvent.change(question, { target: { value: "Which channel moved the most?" } });
  fireEvent.click(screen.getByTestId("chart-template-save"));

  expect(await screen.findByText(/A successor version was saved/)).toBeInTheDocument();

  const posted = calls.find((call) => call.url.endsWith("/versions"));
  expect(posted).toBeDefined();
  expect(posted?.init?.method).toBe("POST");
  const body = JSON.parse(String(posted?.init?.body)) as { document: Record<string, unknown> };
  //  The edited question travels, the contract travels, and the predicates
  //  travel as the wells of the chosen family — never a member, never a Result.
  expect(body.document.answers_question).toBe("Which channel moved the most?");
  expect(body.document.spec_contract_version).toBe("chart-template.v1");
  expect(body.document.family).toBe("bar");
  expect(body.document.requires).toMatchObject({ measure: { min: 1 }, dimension: { min: 1 } });
  expect(body.document).not.toHaveProperty("bindings");
  expect(body.document).not.toHaveProperty("result_id");
});

it("re-reads the template from the server after saving, rather than patching its own copy", async () => {
  let detailReads = 0;
  mockApi([
    [/\/chart-templates\/[^/]+\/versions$/, () => response(APPENDED, 201)],
    [/chart-template-vocabulary/, () => response(VOCABULARY)],
    [
      /\/analyze\/chart-templates\//,
      () => {
        detailReads += 1;
        return response(DETAIL);
      },
    ],
  ]);
  openPresentation();

  await screen.findByTestId("chart-template-question");
  await waitFor(() => expect(detailReads).toBe(1));
  fireEvent.click(screen.getByTestId("chart-template-save"));

  await screen.findByText(/A successor version was saved/);
  await waitFor(() => expect(detailReads).toBe(2));
});

// ---------------------------------------------------------------------------
// A refusal names the gesture.
// ---------------------------------------------------------------------------

it("reads a server refusal back as the gesture that repairs it, anchored on the well", async () => {
  mockApi([
    [
      /\/chart-templates\/[^/]+\/versions$/,
      () =>
        response(
          {
            code: "chart_template_refused",
            message: "the template was refused on 1 point(s)",
            refusals: [
              {
                code: "cardinality_over_limit",
                message:
                  "the Dimension well of the Bar family reads at most 50 distinct values; this template allows 400",
                subject: "/requires/dimension/max_cardinality",
                remedy: "Allow at most 50, or choose a family that reads more.",
              },
            ],
          },
          422,
        ),
    ],
    ...reads(),
  ]);
  openPresentation();

  await screen.findByTestId("chart-template-question");
  fireEvent.click(screen.getByTestId("chart-template-save"));

  const refusal = await screen.findByTestId("chart-template-editor-refusal");
  //  The GESTURE, not the cause. And it is attached to the well it is about, in
  //  the well's own name.
  expect(refusal).toHaveTextContent("Allow at most 50, or choose a family that reads more.");
  expect(refusal).toHaveTextContent("Dimension");
  //  The pointer is how the server anchors a refusal; it is not a sentence.
  expect(refusal).not.toHaveTextContent("/requires/");
  expect(screen.queryByTestId("chart-template-editor-saved")).not.toBeInTheDocument();
});

// ---------------------------------------------------------------------------
// A seed says what the gesture does to it, before the gesture.
// ---------------------------------------------------------------------------

it("says a seed becomes project-owned BEFORE the gesture, not after it", async () => {
  const seeded = {
    ...DETAIL,
    seed_origin: "connector_seed",
    seed: { module_name: "example-connector", template_id: "spend_by_channel" },
    origin_label: "Seeded by example-connector",
  };
  mockApi(reads(VOCABULARY, seeded));
  openPresentation();

  const notice = await screen.findByTestId("chart-template-seed-notice");
  expect(notice).toHaveTextContent(/makes it owned by this Project/);
  //  And the sentence is on the screen while the button is still unpressed.
  expect(screen.getByTestId("chart-template-save")).toBeEnabled();
});

it("says nothing about ownership on a template this Project already owns", async () => {
  mockApi(reads());
  openPresentation();

  await screen.findByTestId("chart-template-question");
  expect(screen.queryByTestId("chart-template-seed-notice")).not.toBeInTheDocument();
});

// ---------------------------------------------------------------------------
// Each question reduces the next.
// ---------------------------------------------------------------------------

it("offers the wells of the chosen family, and none the family does not declare", async () => {
  mockApi(reads());
  openPresentation();

  //  `bar` is chosen: its own wells are on the screen.
  expect(await screen.findByTestId("chart-template-well-series")).toBeInTheDocument();
  expect(screen.getByTestId("chart-template-min-dimension")).toBeInTheDocument();

  //  Switching to `kpi` drops every well `kpi` does not declare, rather than
  //  leaving a requirement the server would refuse a moment later.
  fireEvent.change(screen.getByTestId("chart-template-family"), { target: { value: "kpi" } });
  await waitFor(() =>
    expect(screen.queryByTestId("chart-template-well-series")).not.toBeInTheDocument(),
  );
  expect(screen.queryByTestId("chart-template-min-dimension")).not.toBeInTheDocument();
  expect(screen.getByTestId("chart-template-min-measure")).toBeInTheDocument();
});

it("offers a cardinality bound only where the family carries one", async () => {
  mockApi(reads());
  openPresentation();

  //  Dimension reads at most 50 distinct values on `bar`; Measure carries no
  //  cardinality risk, and a bound there is a point the server refuses.
  expect(await screen.findByTestId("chart-template-cardinality-dimension")).toBeInTheDocument();
  expect(screen.queryByTestId("chart-template-cardinality-measure")).not.toBeInTheDocument();
});

it("names why a well cannot be required, and who owns making it available", async () => {
  mockApi(reads());
  openPresentation();

  expect(
    await screen.findByText(/Time and classification roles are not yet carried/),
  ).toBeInTheDocument();
  expect(screen.getByText(/Semantic compiler/)).toBeInTheDocument();
});

it("does not ask the question the family answers: no family select before the question", async () => {
  const blank = {
    ...DETAIL,
    versions: [{ ...DETAIL.versions[0], document: {} }],
  };
  mockApi(reads(VOCABULARY, blank));
  openPresentation();

  await screen.findByTestId("chart-template-question");
  //  The next question does not appear until this one is answered.
  expect(screen.queryByTestId("chart-template-family")).not.toBeInTheDocument();
  fireEvent.change(screen.getByTestId("chart-template-question"), {
    target: { value: "Where did the spend go?" },
  });
  expect(await screen.findByTestId("chart-template-family")).toBeInTheDocument();
  //  And nothing can be saved until a family says how the answer is shown.
  expect(screen.getByTestId("chart-template-save")).toBeDisabled();
});

// ---------------------------------------------------------------------------
// The empty vocabulary says why, and names the gesture.
// ---------------------------------------------------------------------------

it("names the gesture when there is no visual family to write with", async () => {
  mockApi(reads({ ...VOCABULARY, families: [], family_catalogue: [] }));
  openPresentation();

  expect(await screen.findByText(NO_FAMILY_TO_WRITE_WITH.title)).toBeInTheDocument();
  //  No form nothing could be saved from.
  expect(screen.queryByTestId("chart-template-save")).not.toBeInTheDocument();
  //  And no deployment state, no table name, no story number.
  for (const forbidden of [/migration/i, /app\./, /_id\b/, /deployed/i, /story \d/i]) {
    expect(`${NO_FAMILY_TO_WRITE_WITH.title} ${NO_FAMILY_TO_WRITE_WITH.description}`).not.toMatch(
      forbidden,
    );
  }
});
