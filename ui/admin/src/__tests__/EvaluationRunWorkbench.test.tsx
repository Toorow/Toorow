/**
 * The Evaluation Run Workbench — Story 51.2 AC5, AC6, AC9.
 *
 * Three claims, none cosmetic:
 *
 *   1. the five contracted tabs of `analyze-and-test.md:291` exist, in order,
 *      and each is a real address;
 *   2. the six dimensions keep six separate verdicts — no total, no rate, no
 *      cell that could hide a critical failure;
 *   3. an unresolved pin reads `Unverifiable` with its reason and its owner,
 *      never `pass`, never `fail`, never an empty cell.
 *
 * Every response is the shape `server/core/evaluation_runs_api.py` returns.
 */
import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import EvaluationRunWorkbench from "../shell/pages/EvaluationRunWorkbench";
import { RouterProvider } from "../shell/router";
import { FeedbackRegressionResolutionStatus } from "../test/FeedbackRegressionCaseActions";
import type { FeedbackRegressionResolutionReceipt } from "../test/feedbackReviewClient";
import regressionFixture from "./fixtures/feedbackRegressionExact.json";

const ORG = "org_EXAMPLE";
const PROJECT = "proj_EXAMPLE";
const RUN_ID = "erun_EXAMPLE";

const RENDER_PIN = {
  pin_family: "render",
  reason_code: "render_owner_not_delivered",
  owner: "stories 50.4 / 50.5 / 50.7 (rendered artifact, render stack not installed)",
  detail: "no rendered artifact object exists, so no Render can be pinned",
};

const OVERVIEW = {
  id: RUN_ID,
  lifecycle: "finalized",
  evidence_mode: "offline",
  run_profile_id: "erp_EXAMPLE",
  as_of: "2026-07-30",
  started_at: "2026-07-30T02:00:00Z",
  ended_at: "2026-07-30T02:04:00Z",
  content_hash: "c".repeat(64),
  question_set_fingerprint: "f".repeat(64),
  case_count: 2,
  unresolved_pins: [RENDER_PIN],
  verdict_counts: {
    semantic_correctness: { pass: 1, fail: 1, unverifiable: 0, not_applicable: 0 },
    provenance_correctness: { pass: 0, fail: 0, unverifiable: 2, not_applicable: 0 },
    context_adherence: { pass: 0, fail: 0, unverifiable: 2, not_applicable: 0 },
    path_quality: { pass: 0, fail: 0, unverifiable: 1, not_applicable: 1 },
    dq_handling: { pass: 0, fail: 0, unverifiable: 2, not_applicable: 0 },
    mcp_app_behavior: { pass: 0, fail: 0, unverifiable: 2, not_applicable: 0 },
  },
};

const CASES = {
  run_id: RUN_ID,
  cases: [
    {
      id: "ecase_EXAMPLE",
      golden_question_version_id: "gqv_EXAMPLE",
      result_id: "qr_EXAMPLE",
      ai_path: "aip_EXAMPLE",
      business_domain_id: "dom_EXAMPLE",
      business_domain_version_number: 3,
      capability_key: "spend-breakdown",
      result_type: "breakdown",
      feedback_regression: null,
      unresolved_pins: [RENDER_PIN],
      verdicts: [
        {
          verdict_id: "edv_EXAMPLE",
          dimension: "semantic_correctness",
          verdict: "pass",
          reason_code: "values_match",
          evidence_refs: {},
        },
        {
          verdict_id: null,
          dimension: "provenance_correctness",
          verdict: "unverifiable",
          reason_code: "dimension_evaluator_not_delivered",
          evidence_refs: { owner: "story 51.3 (objective dimension evaluators)" },
        },
        {
          verdict_id: null,
          dimension: "context_adherence",
          verdict: "unverifiable",
          reason_code: "path_comparison_not_delivered",
          evidence_refs: { owner: "story 51.3 (objective dimension evaluators)" },
        },
        {
          verdict_id: null,
          dimension: "path_quality",
          verdict: "unverifiable",
          reason_code: "path_comparison_not_delivered",
          evidence_refs: { owner: "story 51.3 (objective dimension evaluators)" },
        },
        {
          verdict_id: null,
          dimension: "dq_handling",
          verdict: "unverifiable",
          reason_code: "dimension_evaluator_not_delivered",
          evidence_refs: { owner: "story 51.3 (objective dimension evaluators)" },
        },
        {
          verdict_id: null,
          dimension: "mcp_app_behavior",
          verdict: "unverifiable",
          reason_code: "render_owner_not_delivered",
          evidence_refs: { owner: "stories 50.4 / 50.5 / 50.7" },
        },
      ],
    },
  ],
};

const COMPARISONS = {
  run_id: RUN_ID,
  comparisons: [
    {
      id: "ecmp_EXAMPLE",
      baseline_run_id: "erun_EXAMPLEBASE",
      candidate_run_id: RUN_ID,
      comparison_kind: "semantic",
      changed_pin_families: ["semantic_view"],
      unverifiable_families: ["render"],
      held_constant_fingerprint: "d".repeat(64),
      created_by: "owner@example.com",
      created_at: "2026-07-30T03:00:00Z",
    },
  ],
};

const ENVIRONMENT = {
  run_id: RUN_ID,
  families: [
    { family: "result", state: "pinned", pinned: ["qr_EXAMPLE"], absence: null },
    { family: "render", state: "unresolved", pinned: null, absence: RENDER_PIN },
    {
      family: "semantic_view",
      state: "pinned",
      pinned: { semantic_view_id: "sv_EXAMPLE", semantic_view_version_id: "svv_EXAMPLE" },
      absence: null,
    },
  ],
  pin_fingerprints: { semantic_view: "e".repeat(64) },
  unresolved_pins: [RENDER_PIN],
};

const GATE = {
  run_id: RUN_ID,
  gate_decisions: [
    {
      id: "egd_EXAMPLE",
      comparison_id: "ecmp_EXAMPLE",
      decision: "unverifiable",
      candidate: {
        owner_workspace: "governance",
        object_type: "semantic-view",
        object_id: "sv_EXAMPLE",
        version_id: "svv_EXAMPLECANDIDATE",
      },
      coverage: { eligible: 2, evaluated: 1, missing: 1 },
      failing_dimensions: ["mcp_app_behavior"],
      decision_reason: "coverage or evidence is incomplete: 1 question(s) unevaluated",
      decided_by: "owner@example.com",
      decided_at: "2026-07-30T04:00:00Z",
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

// The tab addresses first: the bare run address must not answer `/cases`.
const HAPPY: Array<[RegExp, () => Response]> = [
  [/\/evaluation-runs\/[^/]+\/cases$/, () => response(CASES)],
  [/\/evaluation-runs\/[^/]+\/comparisons$/, () => response(COMPARISONS)],
  [/\/evaluation-runs\/[^/]+\/environment$/, () => response(ENVIRONMENT)],
  [/\/evaluation-runs\/[^/]+\/gate-decision$/, () => response(GATE)],
  [/\/evaluation-runs\/[^/]+$/, () => response(OVERVIEW)],
];

function mount(tab: string, handlers = HAPPY, onNavigateTab = vi.fn(), focusId?: string | null) {
  window.history.replaceState(
    {},
    "",
    `/org/${ORG}/project/${PROJECT}/test/regression-runs/object/evaluation-run/${RUN_ID}/tab/${tab}`,
  );
  const api = mockApi(handlers);
  const view = render(
    <RouterProvider>
      <EvaluationRunWorkbench
        projectId={PROJECT}
        runId={RUN_ID}
        tab={tab}
        focusId={focusId}
        onNavigateTab={onNavigateTab}
      />
    </RouterProvider>,
  );
  return { ...api, view, onNavigateTab };
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it("renders the five contracted tabs, in order, each as a real address", async () => {
  mount("overview");
  const nav = await screen.findByRole("navigation", { name: "Evaluation Run" });
  const links = within(nav).getAllByRole("link");
  expect(links.map((link) => link.textContent)).toEqual([
    "Overview",
    "Cases",
    "Comparisons",
    "Environment",
    "Gate Decision",
  ]);
  expect(links[1]).toHaveAttribute(
    "href",
    `/org/${ORG}/project/${PROJECT}/test/regression-runs/object/evaluation-run/${RUN_ID}/tab/cases`,
  );
});

it("keeps six dimensions and four verdicts, with no total and no rate", async () => {
  mount("overview");
  expect(await screen.findByTestId("count-semantic_correctness-pass")).toHaveTextContent("1");
  expect(screen.getByTestId("count-mcp_app_behavior-unverifiable")).toHaveTextContent("2");

  const table = screen.getByRole("region", { name: "Verdicts by dimension" });
  const headers = within(table).getAllByRole("columnheader").map((cell) => cell.textContent);
  expect(headers).toEqual(["Dimension", "Pass", "Fail", "Unverifiable", "Not applicable"]);
  // No aggregate column can be added without failing here, and no cell carries
  // a percentage that would compensate one dimension with another.
  expect(headers).not.toContain("Total");
  expect(headers).not.toContain("Score");
  expect(within(table).queryByText(/^\d+\s?%$/)).not.toBeInTheDocument();
});

it("reports the unresolved Render pin as Unverifiable with its owner", async () => {
  mount("overview");
  const pin = await screen.findByTestId("pin-render");
  expect(pin).toHaveTextContent(/Unverifiable/i);
  expect(screen.getAllByText(/render_owner_not_delivered/).length).toBeGreaterThan(0);
  expect(screen.getAllByText(/50\.4 \/ 50\.5 \/ 50\.7/).length).toBeGreaterThan(0);
});

it("gives each case six independent verdicts and never one summary", async () => {
  mount("cases");
  expect(await screen.findByTestId("verdict-ecase_EXAMPLE-semantic_correctness")).toHaveTextContent(
    "Pass",
  );
  expect(screen.getByTestId("verdict-ecase_EXAMPLE-mcp_app_behavior")).toHaveTextContent(
    "Unverifiable",
  );
  expect(screen.getByTestId("case-render-ecase_EXAMPLE")).toHaveTextContent("Unverifiable");
  // Six rows in the verdict table, one per dimension: not five, not seven.
  const verdicts = screen.getByRole("region", { name: /Verdicts of case/ });
  expect(within(verdicts).getAllByRole("row")).toHaveLength(7); // header + six
});

it.each([
  ["ecase_EXAMPLE", "Focused Evaluation Case", "Evaluation Case ecase_EXAMPLE"],
  ["edv_EXAMPLE", "Focused evaluation Verdict", "Semantic correctness Verdict edv_EXAMPLE"],
])("resolves the exact %s evidence focus inside Cases", async (focusId, title, detail) => {
  mount("cases", HAPPY, vi.fn(), focusId);
  const focus = await screen.findByTestId(`evaluation-focus-${focusId}`);
  expect(focus).toHaveTextContent(title);
  expect(focus).toHaveTextContent(detail);
});

it("evaluates and resolves the exact server-projected feedback regression case with one retry key", async () => {
  const user = userEvent.setup();
  const posts: Array<{ url: string; body: Record<string, unknown> }> = [];
  let evaluationAttempts = 0;
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      posts.push({ url, body: JSON.parse(String(init.body)) });
      if (url.endsWith("/evaluate")) {
        evaluationAttempts += 1;
        if (evaluationAttempts === 1) throw new TypeError("connection reset after evaluation");
        return response(regressionFixture.evaluation_receipt);
      }
      if (url.endsWith("/regression-resolution")) {
        return response(regressionFixture.resolution_receipt);
      }
    }
    return response({
      run_id: regressionFixture.evaluation_receipt.run_id,
      cases: [regressionFixture.evaluation_case],
    });
  }));
  const onOpenOwner = vi.fn();
  render(
    <RouterProvider>
      <EvaluationRunWorkbench
        projectId={regressionFixture.scope.project_id}
        runId={regressionFixture.evaluation_receipt.run_id}
        tab="cases"
        onNavigateTab={vi.fn()}
        onOpenOwner={onOpenOwner}
      />
    </RouterProvider>,
  );

  const evaluate = await screen.findByTestId(
    `feedback-regression-evaluate-${regressionFixture.evaluation_case.id}`,
  );
  await user.click(evaluate);
  expect(await screen.findByText("connection reset after evaluation")).toBeInTheDocument();
  await user.click(screen.getByTestId(`feedback-regression-evaluate-${regressionFixture.evaluation_case.id}`));
  expect(await screen.findByText("Trusted evaluation recorded")).toBeInTheDocument();
  await user.click(screen.getByTestId(`feedback-regression-resolve-${regressionFixture.evaluation_case.id}`));
  expect(await screen.findByText("Feedback regression resolved")).toBeInTheDocument();

  const evaluationPosts = posts.filter((entry) => entry.url.endsWith("/evaluate"));
  expect(evaluationPosts).toHaveLength(2);
  expect(evaluationPosts[1].body).toEqual(evaluationPosts[0].body);
  expect({ ...evaluationPosts[0].body, retry_key: regressionFixture.evaluation_command.retry_key })
    .toEqual(regressionFixture.evaluation_command);
  expect(posts.find((entry) => entry.url.endsWith("/regression-resolution"))?.body)
    .toEqual(regressionFixture.resolution_command);

  const labels = ["Open Evaluation Run", "Review Evaluation Case", "Review trusted Verdict"];
  for (const [index, owner] of regressionFixture.resolution_receipt.owner_links.entries()) {
    fireEvent.click(screen.getByRole("button", { name: labels[index] }));
    expect(onOpenOwner).toHaveBeenLastCalledWith(owner);
  }
});

it("shows the generated incompatible trusted pass as unresolved with its exact owner", () => {
  const onOpenOwner = vi.fn();
  render(
    <FeedbackRegressionResolutionStatus
      runCaseId={regressionFixture.incompatible_resolution_receipt.evaluation_case_id}
      receipt={regressionFixture.incompatible_resolution_receipt as FeedbackRegressionResolutionReceipt}
      onOpenOwner={onOpenOwner}
    />,
  );
  expect(screen.getByText("Feedback remains unresolved")).toBeInTheDocument();
  expect(screen.getByText("trusted_pass_incompatible")).toBeInTheDocument();
  expect(screen.queryByText("Feedback regression resolved")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /Evaluate regression case again/i })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /Retry trusted resolution/i })).not.toBeInTheDocument();
  expect(screen.getByText(/Create a new offline Evaluation Case/i)).toBeInTheDocument();

  const [runOwner, caseOwner, verdictOwner] = regressionFixture.incompatible_resolution_receipt.owner_links;
  fireEvent.click(screen.getByRole("button", { name: "Open Evaluation Run" }));
  fireEvent.click(screen.getByRole("button", { name: "Review Evaluation Case" }));
  fireEvent.click(screen.getByRole("button", { name: "Review trusted Verdict" }));
  expect(onOpenOwner).toHaveBeenNthCalledWith(1, runOwner);
  expect(onOpenOwner).toHaveBeenNthCalledWith(2, caseOwner);
  expect(onOpenOwner).toHaveBeenNthCalledWith(3, verdictOwner);
});

it("reports a comparison's unverifiable families rather than calling them unchanged", async () => {
  mount("comparisons");
  expect(await screen.findByTestId("unverifiable-ecmp_EXAMPLE")).toHaveTextContent(
    /Unverifiable: render/i,
  );
  expect(screen.getByText("semantic_view")).toBeInTheDocument();
});

it("separates a pinned family from a declared, owned absence", async () => {
  mount("environment");
  expect(await screen.findByTestId("family-semantic_view")).toHaveTextContent("Pinned");
  expect(screen.getByTestId("family-render")).toHaveTextContent("Unverifiable");
});

it("shows a Gate Decision with its coverage denominator and never a bare verdict", async () => {
  mount("gate-decision");
  expect(await screen.findByTestId("gate-egd_EXAMPLE")).toHaveTextContent("Unverifiable");
  expect(screen.getByText("Eligible questions")).toBeInTheDocument();
  expect(screen.getByText(/1 question\(s\) unevaluated/)).toBeInTheDocument();
  expect(screen.getByText(/Test writes evidence; the owner decides/i)).toBeInTheDocument();
});

it("answers a foreign, denied or absent run with one indistinguishable envelope", async () => {
  mount("overview", []);
  expect(await screen.findByText(/answer identically on purpose/i)).toBeInTheDocument();
});

it("names the cause when the run cannot be read and shows nothing in its place", async () => {
  mount("overview", [
    [
      /\/evaluation-runs\//,
      () => response({ code: "unavailable", message: "the evaluation service is unreachable" }, 503),
    ],
  ]);
  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent(/the evaluation service is unreachable/i);
  expect(screen.queryByText("Verdicts by dimension")).not.toBeInTheDocument();
});
