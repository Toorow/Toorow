/**
 * The Trace Observation Workbench — Story 51.4 AC5, AC6, AC10.
 *
 * Under test:
 *
 *   1. the five ratified lenses of `analyze-and-test.md:293` exist, in order,
 *      and each is a real address — this route used to fall through to
 *      `RouteState kind="unavailable"`;
 *   2. the `Result & Render` lens resolves the Result and states its missing
 *      half instead of filling it: no thumbnail, no placeholder, no minted
 *      render identifier, and `Unverifiable` with the owning stories named;
 *   3. an observed record is never blocking, and its timeline renders the
 *      STORED ordinal rather than a re-derived order.
 *
 * Every response is the shape `server/core/trace_observation_api.py` returns.
 */
import { render, screen, within } from "@testing-library/react";
import TraceObservationWorkbench from "../shell/pages/TraceObservationWorkbench";
import { RouterProvider } from "../shell/router";

const ORG = "org_EXAMPLE";
const PROJECT = "proj_EXAMPLE";
const PATH_ID = "aip_EXAMPLE";

const RENDER_ABSENCE = {
  state: "unverifiable",
  reason: "render_owner_not_delivered",
  owner: "Stories 50.4 / 50.5 / 50.7",
  detail:
    "The rendered artifact object does not exist and the rendering stack is not installed.",
};

const OBSERVATION = {
  ai_path_id: PATH_ID,
  project_id: PROJECT,
  lifecycle: "finalized",
  outcome: "succeeded",
  actor: "owner@example.com",
  started_at: "2026-07-21T10:00:00Z",
  ended_at: "2026-07-21T10:00:12Z",
  model_ref: "model-EXAMPLE-2026-06",
  tool_catalog_version: "a".repeat(64),
  w3c_trace_id: "b".repeat(32),
  w3c_trace_id_is_correlation_not_identity: true,
  path_evidence_state: "observed",
  lens_order: ["timeline", "context-skills", "tools", "result-render", "linked-feedback"],
  lenses: {
    timeline: {
      ordering: "stored ordinal",
      steps: [
        { ordinal: 0, observed_at: "2026-07-21T10:00:01Z", step_kind: "knowledge_read", outcome: "ok" },
        { ordinal: 1, observed_at: "2026-07-21T10:00:01Z", step_kind: "semantic_query", outcome: "ok" },
      ],
    },
    "context-skills": {
      steps: [
        {
          ordinal: 0,
          owner_workspace: "context-hub",
          owner_object_type: "context-topic",
          owner_object_id: "ctx_EXAMPLE",
          owner_version_id: "ctxv_EXAMPLE",
          skill: null,
        },
      ],
    },
    tools: {
      tool_name_is_a_label: true,
      steps: [{ ordinal: 2, tool_name: "run_query", outcome: "ok" }],
    },
    "result-render": {
      result: {
        state: "resolved",
        id: "qr_EXAMPLE",
        outcome: "succeeded",
        content_hash: "c".repeat(64),
        row_count: 12,
        truncated: false,
        owner_href: `/api/projects/${PROJECT}/analyze/results/qr_EXAMPLE`,
      },
      render: RENDER_ABSENCE,
    },
    "linked-feedback": {
      feedback: {
        state: "unverifiable",
        reason: "pinned_feedback_owner_not_delivered",
        owner: "Story 51.5",
        detail: "app.feedback carries no Result, rendered artifact or AI Path reference.",
      },
      records: [],
    },
  },
  dimensions: {
    evidence_mode: "observed_cohort",
    blocking: false,
    label: "Observed Cohort - reference window, non-blocking",
    path_quality: { verdict: "pass", findings: [] },
    render_behavior: RENDER_ABSENCE,
    mcp_app_behavior: {
      state: "unverifiable",
      reason: "mcp_app_behavior_owner_not_delivered",
      owner: "Story 50.6",
      detail: "Evaluated MCP App behaviour has no owner, so it cannot pass or fail.",
    },
  },
  proposals_suggested: [
    { member_id: null, reason_code: "result_degraded", severity_hint: "low" },
  ],
  owner_links: {
    ai_path: "Story 49.6 owns app.ai_paths and app.ai_path_steps",
    result: "Story 50.1 owns app.query_results",
    render: "Stories 50.4 / 50.5 / 50.7",
  },
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
  [/\/trace-observations\/[^/]+$/, () => response(OBSERVATION)],
];

function mount(tab: string, handlers = HAPPY) {
  window.history.replaceState(
    {},
    "",
    `/org/${ORG}/project/${PROJECT}/test/regression-runs/object/trace-observation/${PATH_ID}/tab/${tab}`,
  );
  const api = mockApi(handlers);
  const view = render(
    <RouterProvider>
      <TraceObservationWorkbench
        projectId={PROJECT}
        aiPathId={PATH_ID}
        tab={tab}
        onNavigateTab={vi.fn()}
      />
    </RouterProvider>,
  );
  return { ...api, view };
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it("renders the five ratified lenses, in order, each as a real address", async () => {
  mount("timeline");
  const nav = await screen.findByRole("navigation", { name: "Trace Observation" });
  const links = within(nav).getAllByRole("link");
  expect(links.map((link) => link.textContent)).toEqual([
    "Timeline",
    "Context & Skills",
    "Tools",
    "Result & Render",
    "Linked Feedback",
  ]);
  expect(links[3]).toHaveAttribute(
    "href",
    `/org/${ORG}/project/${PROJECT}/test/regression-runs/object/trace-observation/${PATH_ID}/tab/result-render`,
  );
});

it("orders the timeline by the stored ordinal, never by timestamp", async () => {
  mount("timeline");
  expect(await screen.findByText(/Ordered by stored ordinal/i)).toBeInTheDocument();
  const table = screen.getByRole("region", { name: "Observed steps" });
  const rows = within(table).getAllByRole("row").slice(1);
  // Both steps share a timestamp; the recorded order is what is shown.
  expect(rows[0]).toHaveTextContent("knowledge_read");
  expect(rows[1]).toHaveTextContent("semantic_query");
});

it("resolves the Result and leaves the Render panel empty, naming its owner", async () => {
  mount("result-render");
  expect(await screen.findByText("qr_EXAMPLE")).toBeInTheDocument();

  const absence = screen.getByTestId("render-absence");
  expect(absence).toHaveTextContent(/Unverifiable/i);
  expect(absence).toHaveTextContent(/render_owner_not_delivered/);
  expect(absence).toHaveTextContent(/50\.4 \/ 50\.5 \/ 50\.7/);
  // Nothing was drawn in its place and no identifier was minted for it.
  expect(within(absence).queryByRole("img")).not.toBeInTheDocument();
  expect(absence.textContent).not.toMatch(/rnd_|render_[0-9A-Z]{10,}/);
});

it("states the linked-feedback absence rather than matching a trace id", async () => {
  mount("linked-feedback");
  const absence = await screen.findByTestId("linked-feedback-absence");
  expect(absence).toHaveTextContent(/pinned_feedback_owner_not_delivered/);
  expect(screen.getByText(/correlation as identity/i)).toBeInTheDocument();
});

it("keeps the observation non-blocking and its three dimensions separate", async () => {
  mount("timeline");
  expect(await screen.findByTestId("observed-non-blocking")).toHaveTextContent(/non-blocking/i);
  expect(screen.getByTestId("dimension-path_quality")).toHaveTextContent("Pass");
  expect(screen.getByTestId("dimension-render_behavior")).toHaveTextContent("Unverifiable");
  expect(screen.getByTestId("dimension-mcp_app_behavior")).toHaveTextContent("Unverifiable");
  // A suggestion, and it says so: it can never block until reproduced offline.
  expect(screen.getByText(/until reproduced offline/i)).toBeInTheDocument();
});

it("says a tool name is a label and joins nothing on it", async () => {
  mount("tools");
  expect(await screen.findByText(/recorded label/i)).toBeInTheDocument();
  expect(screen.getByText("run_query")).toBeInTheDocument();
});

it("answers a foreign, denied or absent observation with one envelope", async () => {
  mount("timeline", []);
  expect(await screen.findByText(/answer identically on purpose/i)).toBeInTheDocument();
});
