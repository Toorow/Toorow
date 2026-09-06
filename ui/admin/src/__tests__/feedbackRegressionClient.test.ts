import {
  createFeedbackRegressionCase,
  evaluateFeedbackRegressionCase,
  feedbackRegressionCreatePayload,
  fetchFeedbackRegressionDraft,
  resolveFeedbackRegression,
  type FeedbackRegressionCreateCommand,
} from "../test/feedbackReviewClient";
import { emptyDefinitionDraft, emptyV2AssertionDraft } from "../test/goldenQuestionClient";

const PROJECT = "proj_EXAMPLE";
const FEEDBACK = "fba_EXAMPLE";

function response(body: unknown, status = 200): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

it("uses the project-scoped promotion read and closed create command without extra authority", async () => {
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    return response({ schema_version: "feedback-regression-draft.v1", state: "available" });
  }));
  await fetchFeedbackRegressionDraft(PROJECT, FEEDBACK);

  const command: FeedbackRegressionCreateCommand = {
    schema_version: "feedback-regression-create.v1",
    expected_review_version_id: "fbrv_EXAMPLE",
    retry_key: "retry-create",
    title: "Spend regression",
    owner: "owner@example.com",
    reproduction_reason: "The reviewed mismatch is reproducible.",
    selected_domain: { domain_id: "dom_EXAMPLE", version_number: 2 },
    question: "What is spend?",
    time_boundary: {},
    expected_result: [{
      assertion_type: "cardinality",
      selectors: [],
      operator: "equals",
      expected: 3,
      tolerance: null,
    }],
    required_provenance: [{ link_kind: "semantic_view", required: true }],
    expected_ai_path: { grammar_version: 1, required_nodes: [] },
  };
  await createFeedbackRegressionCase(PROJECT, FEEDBACK, command);

  expect(calls[0]?.url).toBe(`/api/projects/${PROJECT}/test/feedback/${FEEDBACK}/regression-draft`);
  expect(calls[1]?.url).toBe(`/api/projects/${PROJECT}/test/feedback/${FEEDBACK}/regression-cases`);
  expect(JSON.parse(String(calls[1]?.init?.body))).toEqual(command);
  expect(JSON.parse(String(calls[1]?.init?.body))).not.toHaveProperty("result_id");
  expect(JSON.parse(String(calls[1]?.init?.body))).not.toHaveProperty("classification");
});

it("sends only the retry key for evaluation and only the two server-owned links for resolution", async () => {
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    return response({});
  }));

  await evaluateFeedbackRegressionCase(PROJECT, "ecase_EXAMPLE", "retry-evaluate");
  await resolveFeedbackRegression(PROJECT, FEEDBACK, "frc_EXAMPLE", "ecase_EXAMPLE");

  expect(JSON.parse(String(calls[0]?.init?.body))).toEqual({
    schema_version: "evaluation-request.v1",
    retry_key: "retry-evaluate",
  });
  expect(JSON.parse(String(calls[1]?.init?.body))).toEqual({
    schema_version: "feedback-regression-resolve.v1",
    regression_case_id: "frc_EXAMPLE",
    evaluation_case_id: "ecase_EXAMPLE",
  });
});

it("projects only authored v2 truth and never accepts observed pins as create inputs", () => {
  const definition = emptyDefinitionDraft(["semantic_view"]);
  definition.contract_version = "golden-question.v2";
  definition.question = "What is spend?";
  definition.v2_assertions = [{
    ...emptyV2AssertionDraft("cardinality"),
    expected_json: "3",
  }];
  definition.provenance[0] = {
    link_kind: "semantic_view",
    state: "required",
    expected_ref: "svv_EXAMPLE",
  };

  const payload = feedbackRegressionCreatePayload(
    {
      schema_version: "feedback-regression-create.v1",
      expected_review_version_id: "fbrv_EXAMPLE",
    },
    "retry-create",
    {
      title: "  Spend regression  ",
      owner: "  owner@example.com ",
      reproduction_reason: "  Reviewed mismatch. ",
      selected_domain: { domain_id: "dom_EXAMPLE", version_number: 2 },
      definition,
    },
  );

  expect(payload).toEqual({
    schema_version: "feedback-regression-create.v1",
    expected_review_version_id: "fbrv_EXAMPLE",
    retry_key: "retry-create",
    title: "Spend regression",
    owner: "owner@example.com",
    reproduction_reason: "Reviewed mismatch.",
    selected_domain: { domain_id: "dom_EXAMPLE", version_number: 2 },
    question: "What is spend?",
    time_boundary: {},
    expected_result: [{
      assertion_type: "cardinality",
      selectors: [],
      expected: 3,
      operator: "equals",
      tolerance: null,
    }],
    required_provenance: [{
      link_kind: "semantic_view",
      required: true,
      expected_ref: "svv_EXAMPLE",
    }],
    expected_ai_path: { grammar_version: 1, required_nodes: [] },
  });
  expect(payload).not.toHaveProperty("result_id");
  expect(payload).not.toHaveProperty("classification_hash");
  expect(payload).not.toHaveProperty("observed_ai_path");
});
