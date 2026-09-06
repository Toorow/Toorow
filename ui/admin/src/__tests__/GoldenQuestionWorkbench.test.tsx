/**
 * The Golden Question Workbench — Story 51.1 AC8 and AC11.
 *
 * Two claims are under test and neither is cosmetic:
 *
 *   1. the five contracted tabs exist, in the contracted order, and each is a
 *      real address (`analyze-and-test.md:290`);
 *   2. every dimension whose owner has not been delivered reads `Unverifiable`
 *      with its reason code and its owner story — never `pass`, never `fail`,
 *      never a placeholder that looks like evidence.
 *
 * The responses are the shapes `server/core/golden_questions_api.py` returns.
 */
import { fireEvent, render, screen, within } from "@testing-library/react";
import GoldenQuestionWorkbench from "../shell/pages/GoldenQuestionWorkbench";
import { RouterProvider } from "../shell/router";

const ORG = "org_EXAMPLE";
const PROJECT = "proj_EXAMPLE";
const QUESTION_ID = "gq_EXAMPLE";

const OPTIONS = {
  business_domains: [
    { id: "dom_EXAMPLE", name: "Media Performance", status: "active", latest_version_number: 3 },
  ],
  business_classifications: [],
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

const VERSION = {
  id: "gqv_EXAMPLE",
  golden_question_id: QUESTION_ID,
  version_number: 1,
  business_domain_id: "dom_EXAMPLE",
  business_domain_version_number: 3,
  business_classification_id: null,
  semantic_view_id: "sv_EXAMPLE",
  semantic_view_version_id: "svv_EXAMPLE",
  semantic_view_version_role: "baseline",
  question: "What did paid media cost by market last complete month?",
  time_boundary: { as_of: "2026-06-30" },
  expected_result: [
    { assertion_type: "value", tolerance: { kind: "numeric", value: 0.01 } },
    { assertion_type: "cardinality", tolerance: null },
  ],
  required_provenance: [
    { link_kind: "source", required: true },
    { link_kind: "citation", required: true },
  ],
  expected_ai_path: {
    grammar_version: 1,
    required_nodes: [
      { key: "analyze/semantic-view/sv_EXAMPLE", step_kind: "semantic_query" },
    ],
  },
  result_type: "breakdown",
  capability_tags: ["spend-breakdown"],
  severity: "critical",
  expected_render_ref: null,
  content_hash: "a".repeat(64),
  predecessor_version_id: null,
  created_by: "owner@example.com",
  created_at: "2026-07-31T09:00:00Z",
  reference_paths: [],
};

const DETAIL = {
  id: QUESTION_ID,
  title: "Paid media spend by market",
  owner: "owner@example.com",
  lifecycle: "active",
  current_version_id: VERSION.id,
  created_by: "owner@example.com",
  created_at: "2026-07-31T09:00:00Z",
  updated_at: "2026-07-31T09:00:00Z",
  lifecycle_transitions: ["deprecated", "archived"],
  current_version: VERSION,
  versions: [VERSION],
};

const COVERAGE = {
  golden_question_id: QUESTION_ID,
  pinned: {
    current_version_id: VERSION.id,
    business_domain: { id: "dom_EXAMPLE", version_number: 3, classification_id: null },
    semantic_view: { id: "sv_EXAMPLE", version_id: "svv_EXAMPLE", role: "baseline" },
    result_type: "breakdown",
    severity: "critical",
    capability_tags: ["spend-breakdown"],
    reference_path_count: 0,
    expected_assertion_count: 2,
    required_provenance_count: 2,
    expected_required_node_count: 1,
  },
  semantic_view_version: { status: "published" },
  dimensions: [
    {
      dimension: "render",
      verdict: "unverifiable",
      reason_code: "render_owner_not_delivered",
      owner_story: "50.4",
      message: "the rendered artifact does not exist",
    },
    {
      dimension: "mcp_app_behavior",
      verdict: "unverifiable",
      reason_code: "mcp_app_evaluation_not_delivered",
      owner_story: "50.6",
      message: "evaluated MCP App behaviour does not exist",
    },
    {
      dimension: "run_coverage",
      verdict: "unverifiable",
      reason_code: "evaluation_run_owner_not_delivered",
      owner_story: "51.2",
      message: "no Evaluation Run owner exists",
    },
    {
      dimension: "observed_ai_path_coverage",
      verdict: "unverifiable",
      reason_code: "observed_path_evidence_absent",
      owner_story: "49.6",
      message: "no caller records an observed AI Path yet",
    },
  ],
};

function response(body: unknown, status = 200): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response;
}

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

const HAPPY: Array<[RegExp, () => Response]> = [
  [/\/golden-questions\/options$/, () => response(OPTIONS)],
  [/\/coverage$/, () => response(COVERAGE)],
  [new RegExp(`/golden-questions/${QUESTION_ID}$`), () => response(DETAIL)],
];

function mount(tab: string, onNavigateTab = vi.fn(), versionId: string | null = null) {
  window.history.replaceState(
    {},
    "",
    `/org/${ORG}/project/${PROJECT}/test/golden-questions/object/golden-question/${QUESTION_ID}/tab/${tab}`,
  );
  return render(
    <RouterProvider>
      <GoldenQuestionWorkbench
        projectId={PROJECT}
        goldenQuestionId={QUESTION_ID}
        versionId={versionId}
        tab={tab}
        onNavigateTab={onNavigateTab}
      />
    </RouterProvider>,
  );
}

it("reads a pinned immutable version address without offering to rewrite it", async () => {
  mockApi(HAPPY);
  mount("definition", vi.fn(), VERSION.id);
  expect(await screen.findByText("Pinned immutable Golden Question version")).toBeInTheDocument();
  expect(screen.getByText(VERSION.id)).toBeInTheDocument();
  expect(screen.getByLabelText(/^Business question/)).toHaveValue(VERSION.question);
  expect(screen.queryByTestId("golden-question-save")).not.toBeInTheDocument();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it("carries exactly the five contracted tabs, each as a real address", async () => {
  mockApi(HAPPY);
  mount("definition");
  const nav = await screen.findByRole("navigation", { name: "Golden Question" });
  const labels = within(nav)
    .getAllByRole("link")
    .map((link) => link.textContent);
  expect(labels).toEqual([
    "Definition",
    "Expected Result",
    "Expected AI Path",
    "Coverage",
    "Versions",
  ]);
  expect(within(nav).getByRole("link", { name: "Coverage" })).toHaveAttribute(
    "href",
    `/org/${ORG}/project/${PROJECT}/test/golden-questions/object/golden-question/${QUESTION_ID}/tab/coverage`,
  );
});

it("edits the stored version through the governed pickers the server served", async () => {
  mockApi(HAPPY);
  mount("definition");
  // `Field` renders the required marker inside the label, so the accessible
  // name carries a trailing asterisk.
  expect(await screen.findByLabelText(/^Business question/)).toHaveValue(VERSION.question);
  expect(screen.getByLabelText(/^Business Domain\*/)).toHaveValue("dom_EXAMPLE");
  expect(screen.getByLabelText(/^Business Domain version/)).toHaveValue("3");
  expect(screen.getByLabelText(/^Semantic View version/)).toHaveValue("svv_EXAMPLE");
  expect(screen.getByLabelText(/^Exercised as/)).toHaveValue("baseline");
  expect(screen.getByLabelText(/^Result type/)).toHaveValue("breakdown");
  expect(screen.getByLabelText(/^Severity/)).toHaveValue("critical");
  expect(screen.getByLabelText(/^Capability tags/)).toHaveValue("spend-breakdown");
});

it("reads a stored assertion back with its explicit tolerance", async () => {
  mockApi(HAPPY);
  mount("expected-result");
  expect(await screen.findByLabelText(/^Assertion type 1/)).toHaveValue("value");
  expect(screen.getByLabelText(/^Tolerance 1/)).toHaveValue("numeric");
  expect(screen.getByLabelText(/^Tolerance value 1/)).toHaveValue("0.01");
  // A declared `null` tolerance is `exact`, and it is not the same statement as
  // an undeclared one — the second is refused by the server.
  expect(screen.getByLabelText(/^Tolerance 2/)).toHaveValue("exact");
  expect(screen.getByLabelText(/^Requirement for citation/)).toHaveValue("required");
  expect(screen.getByLabelText(/^Requirement for mapping/)).toHaveValue("absent");
});

it("names every expected-path node in the observed vocabulary", async () => {
  mockApi(HAPPY);
  mount("expected-ai-path");
  expect(await screen.findByLabelText(/^Step kind 1/)).toHaveValue("semantic_query");
  expect(screen.getByLabelText(/^Owner workspace 1/)).toHaveValue("analyze");
  expect(screen.getByText(/This is a pattern, not a trace/i)).toBeInTheDocument();
});

it("reports every undelivered owner as Unverifiable, with its reason and story", async () => {
  mockApi(HAPPY);
  mount("coverage");
  expect(await screen.findByTestId("verdict-render")).toHaveTextContent("Unverifiable");
  expect(screen.getByTestId("verdict-mcp_app_behavior")).toHaveTextContent("Unverifiable");
  expect(screen.getByTestId("verdict-run_coverage")).toHaveTextContent("Unverifiable");
  expect(screen.getByTestId("verdict-observed_ai_path_coverage")).toHaveTextContent("Unverifiable");

  expect(screen.getByText("render_owner_not_delivered")).toBeInTheDocument();
  expect(screen.getByText("evaluation_run_owner_not_delivered")).toBeInTheDocument();
  expect(screen.getByText("Story 51.2")).toBeInTheDocument();
  expect(screen.getByText("Story 49.6")).toBeInTheDocument();

  // Nothing on this tab may read as a result.
  expect(screen.queryByText(/^Pass$/i)).not.toBeInTheDocument();
  expect(screen.queryByText(/^Fail$/i)).not.toBeInTheDocument();
});

it("lists the immutable history and shows the Render pin as Unverifiable", async () => {
  mockApi(HAPPY);
  mount("versions");
  expect(await screen.findByText("a".repeat(64))).toBeInTheDocument();
  expect(screen.getByText("None (first version)")).toBeInTheDocument();
  expect(screen.getByText(/refuses to change a stored one/i)).toBeInTheDocument();
  expect(screen.getByText("Unverifiable")).toBeInTheDocument();
});

it("saves as a NEW version and never rewrites the one it read", async () => {
  const { calls } = mockApi(HAPPY);
  mount("definition");
  await screen.findByLabelText(/^Business question/);

  const fetchMock = globalThis.fetch as unknown as ReturnType<typeof vi.fn>;
  fetchMock.mockImplementationOnce((url: string, init?: RequestInit) => {
    calls.push({ url, init });
    return Promise.resolve(
      response(
        {
          golden_question_id: QUESTION_ID,
          version_id: "gqv_EXAMPLE_NEXT",
          version_number: 2,
          predecessor_version_id: VERSION.id,
          content_hash: "b".repeat(64),
          created_at: "2026-07-31T10:00:00Z",
        },
        201,
      ),
    );
  });
  fireEvent.click(screen.getByTestId("golden-question-save"));

  expect(await screen.findByText(/Version 2 created/i)).toBeInTheDocument();
  const post = calls.find((call) => call.init?.method === "POST");
  expect(post?.url).toBe(
    `/api/projects/${PROJECT}/test/golden-questions/${QUESTION_ID}/versions`,
  );
  const body = JSON.parse(String(post?.init?.body)) as { definition: Record<string, unknown> };
  expect(body.definition).not.toHaveProperty("expected_render_ref");
  expect(body.definition.result_type).toBe("breakdown");
});

it("shows the server's refusal by subject instead of rewriting it", async () => {
  const { calls } = mockApi(HAPPY);
  mount("expected-result");
  await screen.findByLabelText(/^Assertion type 1/);

  const fetchMock = globalThis.fetch as unknown as ReturnType<typeof vi.fn>;
  fetchMock.mockImplementationOnce((url: string, init?: RequestInit) => {
    calls.push({ url, init });
    return Promise.resolve(
      response(
        {
          code: "invalid_golden_question",
          message: "the Golden Question was refused on 1 point(s)",
          refusals: [
            {
              code: "connector_name_is_not_a_capability",
              message: "`shopify` is a connector, not a governed capability",
              subject: "capability_tags[0]",
            },
          ],
        },
        422,
      ),
    );
  });
  fireEvent.click(screen.getByTestId("golden-question-save"));

  const refusal = await screen.findByTestId("golden-question-refusal");
  expect(refusal).toHaveTextContent("capability_tags[0]");
  expect(refusal).toHaveTextContent(/is a connector, not a governed capability/i);
});

it("answers foreign, denied and absent identically", async () => {
  mockApi([[/\/golden-questions/, () => response({ code: "not_found", message: "Not found" }, 404)]]);
  mount("definition");
  expect(await screen.findByText(/answer identically on purpose/i)).toBeInTheDocument();
});
