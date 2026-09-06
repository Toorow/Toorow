import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import FeedbackReviewWorkbench from "../shell/pages/FeedbackReviewWorkbench";
import { RouterProvider } from "../shell/router";
import exactFixture from "./fixtures/feedbackReviewExact.json";
import regressionFixture from "./fixtures/feedbackRegressionExact.json";

const ORG = "org_EXAMPLE";
const PROJECT = exactFixture.collection.project_id;
const ANNOTATION = exactFixture.detail;
const FEEDBACK_ID = ANNOTATION.id;
const REVIEW_VERSION = ANNOTATION.review.versions[0];
const AUTOMATED_VERDICT = ANNOTATION.automated_verdicts.items[0];
const REVIEW_RECEIPT = {
  ...exactFixture.review_receipt,
  review_version_id: "fbrv_AFTER_FIXTURE",
  version_number: exactFixture.review_receipt.version_number + 1,
  predecessor_version_id: ANNOTATION.review.current_version_id,
};

function response(body: unknown, status = 200): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response;
}

function mockApi(implementation?: (url: string, init?: RequestInit) => Promise<Response>) {
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    if (implementation) return implementation(url, init);
    if (init?.method === "GET") return response(ANNOTATION);
    return response({ code: "not_found", message: "Not found" }, 404);
  });
  vi.stubGlobal("fetch", fetchMock);
  return { fetchMock, calls };
}

function mount(
  tab: string,
  implementation?: (url: string, init?: RequestInit) => Promise<Response>,
) {
  window.history.replaceState(
    {},
    "",
    `/org/${ORG}/project/${PROJECT}/test/widget-feedback/object/feedback-review/${FEEDBACK_ID}/tab/${tab}`,
  );
  const api = mockApi(implementation);
  const view = render(
    <RouterProvider>
      <FeedbackReviewWorkbench
        projectId={PROJECT}
        feedbackId={FEEDBACK_ID}
        tab={tab}
        onNavigateTab={vi.fn()}
      />
    </RouterProvider>,
  );
  return { ...api, view };
}

async function fillRegressionFixtureForm() {
  await screen.findByLabelText(/^Business Domain version/);
  fireEvent.change(screen.getByLabelText(/^Title/), { target: { value: regressionFixture.create_command.title } });
  fireEvent.change(screen.getByLabelText(/^Owner\*/), { target: { value: regressionFixture.create_command.owner } });
  fireEvent.change(screen.getByLabelText(/^Reproduction reason/), { target: { value: regressionFixture.create_command.reproduction_reason } });
  fireEvent.change(screen.getByLabelText(/^Business question/), { target: { value: regressionFixture.create_command.question } });
  fireEvent.click(screen.getByRole("button", { name: "Add assertion" }));
  fireEvent.change(screen.getByLabelText(/^Assertion 1 type/), { target: { value: "cardinality" } });
  fireEvent.change(screen.getByLabelText(/^Assertion 1 expected cardinality/), { target: { value: "3" } });
  fireEvent.change(screen.getByLabelText("Requirement for semantic_view"), { target: { value: "required" } });
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it("renders the five ratified tabs, in order, each as a real address", async () => {
  mount("feedback");
  const nav = await screen.findByRole("navigation", { name: "Feedback Review" });
  const links = within(nav).getAllByRole("link");
  expect(links.map((link) => link.textContent)).toEqual([
    "Feedback",
    "Result & Render",
    "AI Path",
    "Classification",
    "Resolution",
  ]);
  expect(links[3]).toHaveAttribute(
    "href",
    `/org/${ORG}/project/${PROJECT}/test/widget-feedback/object/feedback-review/${FEEDBACK_ID}/tab/classification`,
  );
});

it("shows the exact observed surface, path target and retained Render tuple", async () => {
  const first = mount("feedback");
  expect(await screen.findByText(ANNOTATION.observed_surface)).toBeInTheDocument();
  expect(screen.getByText(String(ANNOTATION.interaction_ref))).toBeInTheDocument();
  first.view.unmount();

  mount("result-render");
  const renderEvidence = await screen.findByRole("region", { name: "Pinned Render and build versions" });
  expect(within(renderEvidence).getByText(String(ANNOTATION.render?.render_id))).toBeInTheDocument();
  expect(
    within(renderEvidence).getByText(String(ANNOTATION.render?.renderer_build_id)),
  ).toBeInTheDocument();
  expect(screen.getByText(`Path step · ordinal ${ANNOTATION.target?.ordinal}`)).toBeInTheDocument();
});

it("reports the complete fixture snapshot and refuses completeness when a visible pin is absent", async () => {
  const complete = mount("feedback");
  expect(await screen.findByTestId("no-divergence")).toHaveTextContent(
    "The versions visible on screen match the versions this annotation pins",
  );
  expect(screen.getByText(new RegExp(ANNOTATION.visible_versions_hash!))).toBeInTheDocument();
  expect(screen.queryByTestId("visible-versions-incomplete")).not.toBeInTheDocument();
  complete.view.unmount();

  const incomplete = {
    ...ANNOTATION,
    visible_versions: { ...ANNOTATION.visible_versions, query_spec_version_id: null },
  };
  mount("feedback", async () => response(incomplete));
  expect(await screen.findByTestId("visible-versions-incomplete")).toHaveTextContent(
    "Visible-version snapshot incomplete",
  );
  expect(screen.getByText(/query_spec_version_id/)).toBeInTheDocument();
  expect(screen.queryByTestId("no-divergence")).not.toBeInTheDocument();
});

it("keeps user polarity, human review and automated verdict separately named", async () => {
  mount("classification");
  const human = await screen.findByTestId(`human-verdict-${REVIEW_VERSION.id}`);
  const automated = screen.getByTestId(
    `automated-verdict-${AUTOMATED_VERDICT.case_id}-${AUTOMATED_VERDICT.dimension}`,
  );
  expect(human).toHaveTextContent("Fail");
  expect(human).toHaveTextContent("Human judgement");
  expect(automated).toHaveTextContent("Fail");
  expect(automated).toHaveTextContent("Automated evaluation");
  expect(screen.getByRole("region", { name: "Automated verdicts" })).toBeInTheDocument();
  expect(human).not.toContainElement(automated);
});

it("distinguishes available-but-empty automated evidence from an unavailable service", async () => {
  const emptyAutomated = {
    ...ANNOTATION,
    automated_verdicts: { state: "available", reason: null, items: [], truncated: false },
  };
  mount("classification", async () => response(emptyAutomated));
  expect(await screen.findByText("No compatible automated verdict")).toBeInTheDocument();
  expect(screen.getByText(/Exact evidence is available/i)).toBeInTheDocument();
  expect(screen.queryByTestId("automated-verdicts-unavailable")).not.toBeInTheDocument();
});

it("authors the server-generated v2 regression fixture without copying frozen evidence", async () => {
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  const regressionAnnotation = {
    ...ANNOTATION,
    id: regressionFixture.scope.feedback_id,
    review: {
      ...ANNOTATION.review,
      current_version_id: regressionFixture.draft.review.version_id,
    },
  };
  const options = {
    business_domains: [], business_classifications: [], semantic_view_versions: [],
    semantic_view_version_roles: [], result_types: [], severities: [], lifecycles: [],
    lifecycle_transitions: {}, assertion_types: [], tolerance_kinds: [],
    provenance_link_kinds: ["semantic_view"], path_step_kinds: [],
    path_owner_workspaces: [], reference_path_roles: [], capability_rule: "",
  };
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    if (url.endsWith("/regression-draft")) return response(regressionFixture.draft);
    if (url.endsWith("/golden-questions/options")) return response(options);
    if (url.endsWith("/regression-cases") && init?.method === "POST") {
      return response(regressionFixture.create_receipt, 201);
    }
    return response(regressionAnnotation);
  }));
  const onOpenOwner = vi.fn();
  render(
    <RouterProvider>
      <FeedbackReviewWorkbench
        projectId={regressionFixture.scope.project_id}
        feedbackId={regressionFixture.scope.feedback_id}
        tab="resolution"
        onNavigateTab={vi.fn()}
        onOpenOwner={onOpenOwner}
      />
    </RouterProvider>,
  );

  const frozen = await screen.findByRole("region", { name: "Frozen promotion evidence" });
  expect(frozen).toHaveTextContent(regressionFixture.draft.frozen.result.content_hash);
  expect(frozen).toHaveTextContent(regressionFixture.draft.frozen.classification_hash);
  expect(screen.getByLabelText(/^Business Domain version/)).toHaveValue(
    `${regressionFixture.create_command.selected_domain.domain_id}:${regressionFixture.create_command.selected_domain.version_number}`,
  );
  await fillRegressionFixtureForm();
  fireEvent.click(screen.getByTestId("feedback-regression-create"));

  const ownerButton = await screen.findByRole("button", { name: "Open Golden Question Definition" });
  const posted = JSON.parse(String(calls.find((call) => call.url.endsWith("/regression-cases"))?.init?.body));
  expect({ ...posted, retry_key: regressionFixture.create_command.retry_key }).toEqual(regressionFixture.create_command);
  expect(posted).not.toHaveProperty("result_id");
  expect(posted).not.toHaveProperty("classification_hash");
  expect(posted.expected_ai_path).not.toEqual(regressionFixture.draft.frozen.ai_path);
  fireEvent.click(ownerButton);
  expect(onOpenOwner).toHaveBeenCalledWith(regressionFixture.create_receipt.owner_links[0]);
});

it("retries the identical promotion command after a transport failure", async () => {
  const posts: Record<string, unknown>[] = [];
  let attempts = 0;
  const regressionAnnotation = {
    ...ANNOTATION,
    id: regressionFixture.scope.feedback_id,
    review: { ...ANNOTATION.review, current_version_id: regressionFixture.draft.review.version_id },
  };
  const options = {
    business_domains: [], business_classifications: [], semantic_view_versions: [],
    semantic_view_version_roles: [], result_types: [], severities: [], lifecycles: [],
    lifecycle_transitions: {}, assertion_types: [], tolerance_kinds: [],
    provenance_link_kinds: ["semantic_view"], path_step_kinds: [],
    path_owner_workspaces: [], reference_path_roles: [], capability_rule: "",
  };
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (url.endsWith("/regression-draft")) return response(regressionFixture.draft);
    if (url.endsWith("/golden-questions/options")) return response(options);
    if (url.endsWith("/regression-cases")) {
      posts.push(JSON.parse(String(init?.body)));
      attempts += 1;
      if (attempts === 1) throw new TypeError("connection reset");
      return response(regressionFixture.create_receipt, 201);
    }
    return response(regressionAnnotation);
  }));
  render(
    <RouterProvider>
      <FeedbackReviewWorkbench
        projectId={regressionFixture.scope.project_id}
        feedbackId={regressionFixture.scope.feedback_id}
        tab="resolution"
        onNavigateTab={vi.fn()}
      />
    </RouterProvider>,
  );
  await fillRegressionFixtureForm();
  fireEvent.click(screen.getByTestId("feedback-regression-create"));
  expect(await screen.findByTestId("feedback-regression-refusal")).toHaveTextContent("connection reset");
  fireEvent.click(screen.getByTestId("feedback-regression-create"));
  await screen.findByText("Regression case created");
  expect(posts).toHaveLength(2);
  expect(posts[1]).toEqual(posts[0]);
});

it("continues append-only review history from the returned cursor", async () => {
  const truncated = {
    ...ANNOTATION,
    review: { ...ANNOTATION.review, versions_truncated: true, versions_next_cursor: "1" },
  };
  const older = {
    ...REVIEW_VERSION,
    id: "review-v0",
    version_number: 0,
    predecessor_version_id: null,
  };
  const { calls } = mount("classification", async (url) => {
    if (url.includes("/reviews?")) {
      return response({ feedback_id: FEEDBACK_ID, versions: [older], truncated: false, next_cursor: null });
    }
    return response(truncated);
  });
  fireEvent.click(await screen.findByRole("button", { name: "Load earlier review versions" }));
  expect(await screen.findByText("v0")).toBeInTheDocument();
  expect(calls.some((call) => call.url.includes("/reviews?") && call.url.includes("cursor=1"))).toBe(true);
  expect(screen.queryByRole("button", { name: "Load earlier review versions" })).not.toBeInTheDocument();
});

it("ignores a history continuation after a save refresh replaces its cursor", async () => {
  let resolveHistory: ((value: Response) => void) | undefined;
  let historySignal: AbortSignal | null = null;
  let detailReads = 0;
  const first = {
    ...ANNOTATION,
    review: { ...ANNOTATION.review, versions_truncated: true, versions_next_cursor: "old-cursor" },
  };
  const refreshed = {
    ...ANNOTATION,
    review: {
      ...ANNOTATION.review,
      current_version_id: "review-v2",
      versions: [{ ...REVIEW_VERSION, id: "review-v2", version_number: 2 }, REVIEW_VERSION],
      versions_truncated: true,
      versions_next_cursor: "new-cursor",
    },
  };
  const staleOlder = { ...REVIEW_VERSION, id: "review-stale", version_number: 0 };
  const { view } = mount("classification", async (url, init) => {
    if (url.includes("/reviews?")) {
      historySignal = init?.signal ?? null;
      return new Promise<Response>((resolve) => { resolveHistory = resolve; });
    }
    if (init?.method === "POST") return response(REVIEW_RECEIPT, 201);
    detailReads += 1;
    return response(detailReads === 1 ? first : refreshed);
  });

  fireEvent.click(await screen.findByRole("button", { name: "Load earlier review versions" }));
  await waitFor(() => expect(historySignal).not.toBeNull());
  view.rerender(
    <RouterProvider>
      <FeedbackReviewWorkbench
        projectId={PROJECT}
        feedbackId={FEEDBACK_ID}
        tab="resolution"
        onNavigateTab={vi.fn()}
      />
    </RouterProvider>,
  );
  fireEvent.click(screen.getByTestId("feedback-review-save"));
  await waitFor(() => expect(historySignal?.aborted).toBe(true));
  expect(await screen.findByText(/Review version 2 appended/i)).toBeInTheDocument();
  view.rerender(
    <RouterProvider>
      <FeedbackReviewWorkbench
        projectId={PROJECT}
        feedbackId={FEEDBACK_ID}
        tab="classification"
        onNavigateTab={vi.fn()}
      />
    </RouterProvider>,
  );
  expect(await screen.findByText("v2")).toBeInTheDocument();

  resolveHistory?.(response({
    feedback_id: FEEDBACK_ID,
    versions: [staleOlder],
    truncated: false,
    next_cursor: null,
  }));
  await waitFor(() => expect(screen.queryByText("v0")).not.toBeInTheDocument());
  expect(screen.getByRole("button", { name: "Load earlier review versions" })).toBeInTheDocument();
});

it("aborts and ignores an in-flight append when the Project scope changes", async () => {
  let resolvePost: ((value: Response) => void) | undefined;
  let postSignal: AbortSignal | null = null;
  const scoped = { ...ANNOTATION, comment: "Different Project feedback" };
  const { view } = mount("resolution", async (url, init) => {
    if (init?.method === "POST") {
      postSignal = init.signal ?? null;
      return new Promise<Response>((resolve) => { resolvePost = resolve; });
    }
    return response(url.includes("proj_OTHER") ? scoped : ANNOTATION);
  });
  await screen.findByTestId("feedback-review-save");
  fireEvent.change(screen.getByLabelText(/^Reason/), { target: { value: "scope-bound draft" } });
  fireEvent.click(screen.getByTestId("feedback-review-save"));
  await waitFor(() => expect(postSignal).not.toBeNull());

  view.rerender(
    <RouterProvider>
      <FeedbackReviewWorkbench
        projectId="proj_OTHER"
        feedbackId={FEEDBACK_ID}
        tab="resolution"
        onNavigateTab={vi.fn()}
      />
    </RouterProvider>,
  );
  expect(await screen.findByText("Different Project feedback")).toBeInTheDocument();
  expect((postSignal as AbortSignal | null)?.aborted).toBe(true);
  expect(screen.getByLabelText(/^Reason/)).toHaveValue("");
  resolvePost?.(response(REVIEW_RECEIPT, 201));
  await Promise.resolve();
  expect(screen.queryByText(/Review version 2 appended/i)).not.toBeInTheDocument();
});

it("posts the closed v1 command with the current head and one retry key", async () => {
  const { calls } = mount("resolution", async (_url, init) => {
    if (init?.method === "GET") return response(ANNOTATION);
    return response({ code: "unavailable", message: "temporary outage" }, 503);
  });
  await screen.findByTestId("feedback-review-save");
  fireEvent.change(screen.getByLabelText(/^Review state/), { target: { value: "triaged" } });
  fireEvent.change(screen.getByLabelText(/^Affected dimension/), {
    target: { value: "semantic_correctness" },
  });
  fireEvent.change(screen.getByLabelText(/^Human verdict/), { target: { value: "fail" } });
  fireEvent.change(screen.getByLabelText(/^Severity/), { target: { value: "critical" } });
  fireEvent.change(screen.getByLabelText(/^Reason/), { target: { value: "  exact mismatch  " } });

  fireEvent.click(screen.getByTestId("feedback-review-save"));
  expect(await screen.findByText(/temporary outage/i)).toBeInTheDocument();
  await waitFor(() => expect(screen.getByTestId("feedback-review-save")).toBeEnabled());
  fireEvent.click(screen.getByTestId("feedback-review-save"));
  await screen.findByText(/temporary outage/i);

  const posts = calls.filter((call) => call.init?.method === "POST");
  const first = JSON.parse(String(posts[0]?.init?.body)) as Record<string, unknown>;
  const second = JSON.parse(String(posts[1]?.init?.body)) as Record<string, unknown>;
  expect(first).toEqual({
    schema_version: "feedback-review-command.v1",
    expected_head: ANNOTATION.review.current_version_id,
    retry_key: expect.any(String),
    state: "triaged",
    affected_dimension: "semantic_correctness",
    human_verdict: "fail",
    severity: "critical",
    reason: "exact mismatch",
  });
  expect(second.retry_key).toBe(first.retry_key);
});

it("refreshes a stale head and rotates the retry key before another append", async () => {
  let getCount = 0;
  const staleHead = { ...ANNOTATION, review: { ...ANNOTATION.review, current_version_id: "review-v2" } };
  const { calls } = mount("resolution", async (_url, init) => {
    if (init?.method === "GET") {
      getCount += 1;
      return response(getCount === 1 ? ANNOTATION : staleHead);
    }
    const postCount = calls.filter((call) => call.init?.method === "POST").length;
    return postCount === 1
      ? response({ code: "stale_head", message: "review head changed" }, 409)
      : response(REVIEW_RECEIPT, 201);
  });
  await screen.findByTestId("feedback-review-save");
  fireEvent.click(screen.getByTestId("feedback-review-save"));
  expect(await screen.findByText(/current review head was refreshed/i)).toBeInTheDocument();
  await waitFor(() => expect(screen.getByTestId("feedback-review-save")).toBeEnabled());
  fireEvent.click(screen.getByTestId("feedback-review-save"));
  expect(await screen.findByText(/Review version 2 appended/i)).toBeInTheDocument();

  const posts = calls.filter((call) => call.init?.method === "POST");
  const first = JSON.parse(String(posts[0]?.init?.body)) as Record<string, unknown>;
  const second = JSON.parse(String(posts[1]?.init?.body)) as Record<string, unknown>;
  expect(first.expected_head).toBe(ANNOTATION.review.current_version_id);
  expect(second.expected_head).toBe("review-v2");
  expect(second.retry_key).not.toBe(first.retry_key);
  expect(calls.filter((call) => !call.url.includes("regression-draft")).map((call) => call.init?.method)).toEqual(["GET", "POST", "GET", "POST", "GET"]);
});

it("acknowledges the immutable receipt and then reloads the detail", async () => {
  let getCount = 0;
  const updated = {
    ...ANNOTATION,
    blocking_use: "detail reloaded after receipt",
    review: {
      current_state: "resolved",
      current_version_id: "review-v2",
      versions: [
        {
          ...REVIEW_VERSION,
          id: "review-v2",
          version_number: 2,
          review_state: "resolved",
          predecessor_version_id: "review-v1",
        },
        REVIEW_VERSION,
      ],
    },
  };
  const { calls } = mount("resolution", async (_url, init) => {
    if (init?.method === "POST") return response(REVIEW_RECEIPT, 201);
    getCount += 1;
    return response(getCount === 1 ? ANNOTATION : updated);
  });
  await screen.findByTestId("feedback-review-save");

  fireEvent.click(screen.getByTestId("feedback-review-save"));

  expect(await screen.findByText(/Review version 2 appended/i)).toBeInTheDocument();
  expect(await screen.findByText("detail reloaded after receipt")).toBeInTheDocument();
  expect(calls.filter((call) => !call.url.includes("regression-draft")).map((call) => call.init?.method)).toEqual(["GET", "POST", "GET"]);
});

it("rotates the retry key after a receipt even when the detail refresh fails", async () => {
  let getCount = 0;
  const { calls } = mount("resolution", async (_url, init) => {
    if (init?.method === "POST") return response(REVIEW_RECEIPT, 201);
    getCount += 1;
    return getCount === 1
      ? response(ANNOTATION)
      : response({ code: "unavailable", message: "refresh unavailable" }, 503);
  });
  await screen.findByTestId("feedback-review-save");

  fireEvent.click(screen.getByTestId("feedback-review-save"));

  expect(await screen.findByText(/Review version 2 appended/i)).toBeInTheDocument();
  expect(
    screen.getByText(/review was appended, but the detail could not be refreshed/i),
  ).toBeInTheDocument();
  await waitFor(() => expect(screen.getByTestId("feedback-review-save")).toBeEnabled());
  fireEvent.click(screen.getByTestId("feedback-review-save"));
  await waitFor(() => expect(calls.filter((call) => call.init?.method === "POST")).toHaveLength(2));

  const posts = calls.filter((call) => call.init?.method === "POST");
  const first = JSON.parse(String(posts[0]?.init?.body)) as Record<string, unknown>;
  const second = JSON.parse(String(posts[1]?.init?.body)) as Record<string, unknown>;
  expect(second.retry_key).not.toBe(first.retry_key);
});

it("answers a foreign, denied or absent annotation with one envelope", async () => {
  mount("feedback", async () => response({ code: "not_found", message: "Not found" }, 404));
  expect(await screen.findByText(/answer identically on purpose/i)).toBeInTheDocument();
});
