/**
 * The Golden Questions collection — Story 51.1 AC8.
 *
 * Every response here is the shape `server/core/golden_questions_api.py` really
 * returns. The point of these tests is not that the screen renders: it is that
 * it renders the RATIFIED object (governed pins, lifecycle, severity) and never
 * the Epic 14 benchmark record it replaced — no `topic`, no `expected_citations`
 * and above all no pass rate, because no Evaluation Run owner exists yet.
 */
import { fireEvent, render, screen } from "@testing-library/react";
import GoldenQuestions from "../shell/pages/GoldenQuestions";

const PROJECT = "proj_EXAMPLE";

const OPTIONS = {
  business_domains: [
    { id: "dom_EXAMPLE", name: "Media Performance", status: "active", latest_version_number: 3 },
  ],
  business_classifications: [
    { id: "cls_EXAMPLE", business_domain_id: "dom_EXAMPLE", name: "Paid Social" },
  ],
  semantic_view_versions: [
    {
      semantic_view_version_id: "svv_EXAMPLE",
      semantic_view_id: "sv_EXAMPLE",
      name: "Campaign Performance",
      version_number: 2,
      status: "published",
    },
  ],
  semantic_view_version_roles: ["baseline", "candidate"],
  result_types: ["scalar", "series", "breakdown", "comparison", "table", "narrative", "refusal"],
  severities: ["critical", "major", "minor"],
  lifecycles: ["draft", "active", "deprecated", "archived"],
  lifecycle_transitions: {
    draft: ["active", "archived"],
    active: ["deprecated", "archived"],
    deprecated: ["active", "archived"],
    archived: [],
  },
  assertion_types: ["value", "row_set", "ordering", "cardinality", "invariant", "empty", "degraded", "refused"],
  tolerance_kinds: ["numeric", "temporal", "set"],
  provenance_link_kinds: [
    "source",
    "pull",
    "virtual_pull",
    "mapping",
    "publication",
    "semantic_view",
    "citation",
  ],
  path_step_kinds: ["tool_call", "knowledge_read", "skill_step", "semantic_query", "data_read", "handoff"],
  path_owner_workspaces: ["data", "governance", "analyze", "context-hub", "test"],
  reference_path_roles: ["canonical", "alternative"],
  capability_rule: "a capability names a governed product or tool capability, never a connector",
};

const QUESTION = {
  id: "gq_EXAMPLE",
  title: "Paid media spend by market, last complete month",
  owner: "owner@example.com",
  lifecycle: "active",
  current_version_id: "gqv_EXAMPLE",
  created_by: "owner@example.com",
  created_at: "2026-07-31T09:00:00Z",
  updated_at: "2026-07-31T09:00:00Z",
  current_version: {
    version_number: 1,
    business_domain_id: "dom_EXAMPLE",
    business_domain_version_number: 3,
    semantic_view_id: "sv_EXAMPLE",
    semantic_view_version_id: "svv_EXAMPLE",
    result_type: "breakdown",
    severity: "critical",
    capability_tags: ["spend-breakdown"],
  },
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

it("refuses to read anything without an exact Project scope", () => {
  const { fetchMock } = mockApi([]);
  render(<GoldenQuestions />);
  // 76-5: the screen's private "Select a Project" banner became the console's
  // one `NoScope`, which names the switcher instead of repeating it.
  expect(screen.getByText("No project selected")).toBeInTheDocument();
  expect(screen.getByText(/Choose a project to see its Golden Questions/i)).toBeInTheDocument();
  expect(fetchMock).not.toHaveBeenCalled();
});

it("renders the ratified product object and no benchmark verdict", async () => {
  const { calls } = mockApi([
    [/\/golden-questions\/options$/, () => response(OPTIONS)],
    [/\/golden-questions$/, () => response({ golden_questions: [QUESTION] })],
  ]);
  render(<GoldenQuestions projectId={PROJECT} />);

  expect(await screen.findByText(QUESTION.title)).toBeInTheDocument();
  // 76-5: the Business Domain column printed the ULID IN the position of the
  // name. The name is on the wire (`options.business_domains`) and the id is
  // beside it, as `console-presentation.md` §4 asks.
  expect(screen.getByText("Media Performance v3")).toBeInTheDocument();
  expect(screen.getByTitle("Business Domain: dom_EXAMPLE")).toHaveTextContent("dom_EXAMPLE");
  expect(screen.getByText("svv_EXAMPLE")).toBeInTheDocument();
  // 76-5: `result_type` was the wire token raw in a cell. `wireWord` is the
  // console's one repair for a stored word whose word IS the token.
  expect(screen.getByText("Breakdown")).toBeInTheDocument();
  expect(screen.getByText("Critical")).toBeInTheDocument();

  // The Epic 14 vestige columns are gone, and so is the invented pass rate: no
  // column reports a verdict and no cell renders a percentage.
  expect(
    screen.queryByRole("columnheader", {
      name: /pass rate|topic|expected citations|last result/i,
    }),
  ).not.toBeInTheDocument();
  expect(screen.queryByText(/^\d+\s?%$/)).not.toBeInTheDocument();
  expect(screen.getByText(/Evaluation Run does not exist yet, so this collection shows no pass rate/i)).toBeInTheDocument();

  expect(calls.map((call) => call.url)).toEqual(
    expect.arrayContaining([
      `/api/projects/${PROJECT}/test/golden-questions`,
      `/api/projects/${PROJECT}/test/golden-questions/options`,
    ]),
  );
});

it("states an empty collection honestly and never borrows the evaluation corpus", async () => {
  mockApi([
    [/\/golden-questions\/options$/, () => response(OPTIONS)],
    [/\/golden-questions$/, () => response({ golden_questions: [] })],
  ]);
  render(<GoldenQuestions projectId={PROJECT} />);
  expect(await screen.findByText(/No Golden Question in this Project/i)).toBeInTheDocument();
  expect(screen.getByText(/test code, not product knowledge/i)).toBeInTheDocument();
});

it("names the cause when the collection cannot be read, and fabricates nothing", async () => {
  mockApi([
    [/\/golden-questions\/options$/, () => response(OPTIONS)],
    [
      /\/golden-questions$/,
      () => response({ code: "unavailable", message: "the evaluation service is unreachable" }, 503),
    ],
  ]);
  render(<GoldenQuestions projectId={PROJECT} />);
  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent(/the evaluation service is unreachable/i);
  expect(alert).toHaveTextContent(/No question has been fabricated/i);
  expect(screen.queryByText(QUESTION.title)).not.toBeInTheDocument();
});

it("answers a foreign, denied or absent Project with one indistinguishable envelope", async () => {
  mockApi([[/\/golden-questions/, () => response({ code: "not_found", message: "Not found" }, 404)]]);
  render(<GoldenQuestions projectId={PROJECT} />);
  // 76-5: the two answers stay ONE answer -- what the block gained is a way out.
  expect(await screen.findByText("Project not found")).toBeInTheDocument();
  expect(screen.getByText(/project switcher at the top of the screen/i)).toBeInTheDocument();
});

it("shows the server's structured refusal and sends no Render pin", async () => {
  const refusal = {
    code: "invalid_golden_question",
    message: "the Golden Question was refused on 2 point(s)",
    refusals: [
      {
        code: "missing_tolerance",
        message: "every assertion declares a tolerance; declare null for exact",
        subject: "expected_result[0]",
      },
      {
        code: "missing_field",
        message: "a Golden Question version must pin a Business Domain",
        subject: "business_domain_id",
      },
    ],
  };
  const { calls } = mockApi([
    [/\/golden-questions\/options$/, () => response(OPTIONS)],
    [/\/golden-questions$/, () => response({ golden_questions: [] })],
  ]);
  render(<GoldenQuestions projectId={PROJECT} />);
  // Both the page action and the empty state offer it; either opens the form.
  const openers = await screen.findAllByRole("button", { name: /New Golden Question/i });
  fireEvent.click(openers[0]);

  // The create POST is refused: the draft is deliberately incomplete.
  const fetchMock = globalThis.fetch as unknown as ReturnType<typeof vi.fn>;
  fetchMock.mockImplementationOnce((url: string, init?: RequestInit) => {
    calls.push({ url, init });
    return Promise.resolve(response(refusal, 422));
  });
  fireEvent.click(screen.getByTestId("golden-question-create-save"));

  expect(await screen.findByTestId("golden-question-refusal")).toHaveTextContent(
    /expected_result\[0\]/,
  );
  expect(screen.getByTestId("golden-question-refusal")).toHaveTextContent(/business_domain_id/);

  const post = calls.find((call) => call.init?.method === "POST");
  expect(post?.url).toBe(`/api/projects/${PROJECT}/test/golden-questions`);
  const body = JSON.parse(String(post?.init?.body)) as {
    definition: Record<string, unknown> & { expected_result: Record<string, unknown>[] };
  };
  // Render does not exist: the console never mints or sends a pin for it.
  expect(body.definition).not.toHaveProperty("expected_render_ref");
  // An undeclared tolerance is OMITTED, so the server refuses it by name rather
  // than the console quietly choosing `exact` on the author's behalf.
  expect(body.definition.expected_result[0]).not.toHaveProperty("tolerance");
});
