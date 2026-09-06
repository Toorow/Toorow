/**
 * Regression Runs — the Level 2 collection (Stories 51.2 AC9 and 51.4 AC9).
 *
 * Every response here is the shape `server/core/evaluation_runs_api.py` and
 * `server/core/trace_observation_api.py` really return. What is under test is
 * not that the screen renders, but that it renders the two evidence modes as
 * TWO things:
 *
 *   - `Offline` and `Observed Cohort` are separately labelled sections;
 *   - no figure is computed across them, and the Epic 14 pass rate is gone;
 *   - an approved baseline is the only thing that reads as a baseline — a
 *     profile without one says "None approved", never "the latest run";
 *   - an observed cohort says `Non-blocking` and its members' Render pin says
 *     `Unverifiable`.
 */
import { fireEvent, render, screen, within } from "@testing-library/react";
import RegressionRuns from "../shell/pages/RegressionRuns";

const PROJECT = "proj_EXAMPLE";

const OFFLINE_RUN = {
  id: "erun_EXAMPLEOFFLINE",
  lifecycle: "finalized",
  evidence_mode: "offline",
  run_profile: "Nightly semantic",
  as_of: "2026-07-30",
  started_at: "2026-07-30T02:00:00Z",
  ended_at: "2026-07-30T02:04:00Z",
  question_set_fingerprint: "f".repeat(64),
  unresolved_pin_count: 1,
  case_count: 4,
};

const PROFILES = {
  run_profiles: [
    {
      id: "erp_EXAMPLEOFFLINE",
      name: "Nightly semantic",
      description: "",
      evidence_mode: "offline",
      created_at: "2026-07-20T09:00:00Z",
    },
    {
      id: "erp_EXAMPLEOBSERVED",
      name: "Console traffic",
      description: "",
      evidence_mode: "observed_cohort",
      created_at: "2026-07-20T09:00:00Z",
    },
  ],
};

const COHORT = {
  id: "oc_EXAMPLE",
  label: "Console, last complete week",
  window_start: "2026-07-20T00:00:00Z",
  window_end: "2026-07-27T00:00:00Z",
  resolved_at: "2026-07-27T08:00:00Z",
  member_count: 2,
  filter_hash: "a".repeat(64),
  evidence_mode: "observed_cohort",
  evidence_label: "Observed Cohort - reference window, non-blocking",
  blocking: false,
};

const COHORT_DETAIL = {
  ...COHORT,
  content_hash: "b".repeat(64),
  created_by: "owner@example.com",
  version_filters: { window_start: COHORT.window_start, window_end: COHORT.window_end },
  members: [
    {
      id: "ocm_EXAMPLE",
      ai_path_id: "aip_EXAMPLE",
      query_result_id: "qr_EXAMPLE",
      path_evidence_state: "observed",
      render_evidence_state: "unverifiable",
      render: {
        state: "unverifiable",
        reason: "render_owner_not_delivered",
        owner: "Stories 50.4 / 50.5 / 50.7",
        detail: "The rendered artifact object does not exist.",
      },
      observed_at: "2026-07-21T10:00:00Z",
      assessment: { verdict: "pass", findings: [] },
    },
  ],
};

// The adherence measure, in the shape `core.adherence.adherence_overview`
// really returns. A trace-identified session and a wall-clock inference are two
// rows here because they are two kinds of evidence, never one figure — and the
// payload carries NO top-level `adherent` / `share_adherent` for exactly that
// reason (`analyze-and-test.md:1330`).
//
// The numbers are chosen so that every pooled figure would be a string that
// appears nowhere else: pooling the two bases gives "4 of 6", pooling
// `get_report` across them gives "3 of 4". The guard below forbids both by name,
// which is what makes it a test on the FIGURE rather than on the labels.
const ADHERENCE = {
  project_id: PROJECT,
  window: { days: 30, from: "2026-07-01T00:00:00Z", to: "2026-07-31T00:00:00Z" },
  observations: 6,
  first_observation: "2026-07-02T09:00:00Z",
  last_observation: "2026-07-30T17:00:00Z",
  by_data_tool: [
    {
      data_tool: "get_card",
      basis: "observed_session" as const,
      observations: 2,
      adherent: 1,
      not_adherent: 1,
      share_adherent: 0.5,
      means: "the context call and the data query carried the same trace from the host, so they are known to be one exchange",
    },
    {
      data_tool: "get_report",
      basis: "inferred_window" as const,
      observations: 1,
      adherent: 1,
      not_adherent: 0,
      share_adherent: 1,
      means: "no trace was sent, so the two calls were treated as one exchange because the same person made them in the same project within a few minutes",
    },
    {
      data_tool: "get_report",
      basis: "observed_session" as const,
      observations: 3,
      adherent: 2,
      not_adherent: 1,
      share_adherent: 0.6667,
      means: "the context call and the data query carried the same trace from the host, so they are known to be one exchange",
    },
  ],
  by_basis: [
    {
      basis: "inferred_window" as const,
      observations: 1,
      adherent: 1,
      not_adherent: 0,
      share_adherent: 1,
      means: "no trace was sent, so the two calls were treated as one exchange because the same person made them in the same project within a few minutes",
    },
    {
      basis: "observed_session" as const,
      observations: 5,
      adherent: 3,
      not_adherent: 2,
      share_adherent: 0.6,
      means: "the context call and the data query carried the same trace from the host, so they are known to be one exchange",
    },
  ],
  context_tools: [{ context_tool: "search_context", observations: 4 }],
  measured_data_tools: ["get_daily_report", "get_report", "get_card"],
  measured_context_tools: ["search_context", "get_procedure", "resolve_business_path"],
  empty_state: null,
};

const ADHERENCE_EMPTY = {
  ...ADHERENCE,
  observations: 0,
  first_observation: null,
  last_observation: null,
  by_data_tool: [],
  by_basis: [],
  context_tools: [],
  empty_state: {
    headline: "Nothing recorded yet.",
    detail:
      "Adherence is measured when someone asks this project a data question from an assistant -- a daily report, a report or a card -- and it says whether the governed context was consulted first.",
    next_step: "Connect this project to an assistant and ask it one question, then come back here.",
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

// Literal segments first, exactly as the server declares its routes: the
// baseline address must not be answered by the run-profiles collection.
const HAPPY: Array<[RegExp, () => Response]> = [
  [/\/context-adherence$/, () => response(ADHERENCE)],
  [/\/run-profiles\/[^/]+\/baseline$/, () => response({ baseline: null })],
  [/\/run-profiles$/, () => response(PROFILES)],
  [/\/evaluation-runs\?evidence_mode=offline$/, () => response({ evaluation_runs: [OFFLINE_RUN] })],
  [/\/observed-cohorts\/[^/?]+$/, () => response(COHORT_DETAIL)],
  [/\/observed-cohorts$/, () => response({ cohorts: [COHORT] })],
];

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it("reads nothing without an exact Project scope", () => {
  const { fetchMock } = mockApi([]);
  render(<RegressionRuns />);
  expect(screen.getByText(/Evaluation evidence is Project-scoped/i)).toBeInTheDocument();
  expect(fetchMock).not.toHaveBeenCalled();
});

it("keeps Offline and Observed Cohort as two sections and reports no merged score", async () => {
  const { calls } = mockApi(HAPPY);
  render(<RegressionRuns projectId={PROJECT} />);

  expect(await screen.findByRole("heading", { name: "Offline" })).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "Observed Cohort" })).toBeInTheDocument();
  expect(await screen.findByText(OFFLINE_RUN.id)).toBeInTheDocument();
  expect(await screen.findByText(COHORT.label)).toBeInTheDocument();

  // The Epic 14 merge, in its two on-screen forms: a rate over a mixed list and
  // a "latest score". Neither survives, and no cell renders a bare percentage.
  expect(
    screen.queryByRole("columnheader", { name: /pass rate|precision|latest score/i }),
  ).not.toBeInTheDocument();
  expect(screen.queryByText(/^\d+\s?%$/)).not.toBeInTheDocument();
  expect(screen.getByText(/never merged into a trust score/i)).toBeInTheDocument();

  // Two independent reads. A single request feeding both sections is how a
  // figure ends up computed across evidence modes.
  expect(calls.map((call) => call.url)).toEqual(
    expect.arrayContaining([
      `/api/projects/${PROJECT}/test/evaluation-runs?evidence_mode=offline`,
      `/api/projects/${PROJECT}/test/observed-cohorts`,
    ]),
  );
});

it("says a profile has no approved baseline instead of naming its latest run", async () => {
  mockApi(HAPPY);
  render(<RegressionRuns projectId={PROJECT} />);
  const cell = await screen.findByTestId("baseline-erp_EXAMPLEOFFLINE");
  expect(cell).toHaveTextContent(/None approved/i);
  // The finalized run exists, and it is NOT presented as the baseline.
  expect(
    within(cell).queryByText(OFFLINE_RUN.id),
  ).not.toBeInTheDocument();
});

it("marks an observed cohort non-blocking and its Render pin Unverifiable", async () => {
  mockApi(HAPPY);
  const onOpenTraceObservation = vi.fn();
  render(
    <RegressionRuns projectId={PROJECT} onOpenTraceObservation={onOpenTraceObservation} />,
  );

  expect(await screen.findByTestId(`blocking-${COHORT.id}`)).toHaveTextContent(/Non-blocking/i);

  fireEvent.click(await screen.findByRole("button", { name: /Show observations/i }));
  expect(await screen.findByTestId("render-ocm_EXAMPLE")).toHaveTextContent(/Unverifiable/i);

  fireEvent.click(screen.getByRole("button", { name: "aip_EXAMPLE" }));
  expect(onOpenTraceObservation).toHaveBeenCalledWith("aip_EXAMPLE");
});

it("names the cause when one section fails and keeps the other readable", async () => {
  mockApi([
    [/\/run-profiles\/[^/]+\/baseline$/, () => response({ baseline: null })],
    [/\/run-profiles$/, () => response(PROFILES)],
    [
      /\/evaluation-runs\?evidence_mode=offline$/,
      () => response({ code: "unavailable", message: "the evaluation service is unreachable" }, 503),
    ],
    [/\/observed-cohorts$/, () => response({ cohorts: [COHORT] })],
  ]);
  render(<RegressionRuns projectId={PROJECT} />);

  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent(/the evaluation service is unreachable/i);
  expect(alert).toHaveTextContent(/No run, cohort or figure has been fabricated/i);
  // The other evidence mode is unaffected, because nothing is shared.
  expect(await screen.findByText(COHORT.label)).toBeInTheDocument();
});

it("answers a foreign, denied or absent Project with one indistinguishable envelope", async () => {
  mockApi([]);
  render(<RegressionRuns projectId={PROJECT} />);
  expect((await screen.findAllByText(/answer identically on purpose/i)).length).toBeGreaterThan(0);
});

// ---------------------------------------------------------------------------
// Story 67.15 A — the measure that had no reader, read here.
// ---------------------------------------------------------------------------

it("reads context adherence beside the evaluations, with every denominator shown", async () => {
  const { calls } = mockApi(HAPPY);
  render(<RegressionRuns projectId={PROJECT} />);

  expect(await screen.findByRole("heading", { name: "Context adherence" })).toBeInTheDocument();

  // The denominator travels with the count. A bare percentage is exactly how a
  // share over five observations starts reading like a share over five hundred.
  expect(await screen.findByText("3 of 5")).toBeInTheDocument();
  // Every bucket states its own denominator, and the two tables slice the SAME
  // observations, so neither can be read as an addition to the other.
  expect(screen.getAllByText("2 of 3")).toHaveLength(1);
  expect(screen.queryByText(/^\d+\s?%$/)).not.toBeInTheDocument();

  // A fourth independent read: no state is shared with the two evidence modes.
  expect(calls.map((call) => call.url)).toEqual(
    expect.arrayContaining([`/api/projects/${PROJECT}/test/context-adherence`]),
  );
});

it("never pools an inferred session with an observed one", async () => {
  mockApi(HAPPY);
  render(<RegressionRuns projectId={PROJECT} />);

  // THE FIGURE, NOT THE LABELS. This guard used to assert only that both basis
  // labels rendered — which they already did, on a screen whose first row was
  // "All questions" carrying the server's pooled total. A guard that measures
  // its own weaker version proves nothing, so this one names the two figures
  // that can only exist by pooling and refuses them:
  //   "4 of 6" — the two bases summed into one headline;
  //   "3 of 4" — `get_report` summed across its observed and inferred rows.
  // Put the "All questions" row back, or drop the basis split out of
  // `by_data_tool`, and one of these appears.
  expect((await screen.findAllByText("Observed exchange")).length).toBeGreaterThan(0);
  expect(screen.queryByText("4 of 6")).not.toBeInTheDocument();
  expect(screen.queryByText("3 of 4")).not.toBeInTheDocument();
  expect(screen.queryByText("All questions")).not.toBeInTheDocument();

  // What DOES render: the same observations, split, each with its own basis and
  // its own denominator.
  expect(screen.getByText("3 of 5")).toBeInTheDocument(); // observed exchange total
  expect(screen.getByText("2 of 3")).toBeInTheDocument(); // get_report, observed
  expect(screen.getByText("1 of 2")).toBeInTheDocument(); // get_card, observed
  expect(screen.getAllByText("1 of 1")).toHaveLength(2); // inferred: total and get_report

  // Every data-question row is badged with the basis it stands on — there is no
  // basis-free row left to read as "all evidence".
  expect(screen.getAllByText("Observed exchange")).toHaveLength(3);
  expect(screen.getAllByText("Inferred")).toHaveLength(2);

  // Each basis says what it proves, so a reader never has to deduce it.
  expect(screen.getAllByText(/known to be one exchange/i).length).toBeGreaterThan(0);
  expect(screen.getAllByText(/within a few minutes/i).length).toBeGreaterThan(0);
});

it("says why adherence is empty and names the gesture that fills it", async () => {
  mockApi([
    [/\/context-adherence$/, () => response(ADHERENCE_EMPTY)],
    ...HAPPY.filter(([pattern]) => pattern.source !== /\/context-adherence$/.source),
  ]);
  render(<RegressionRuns projectId={PROJECT} />);

  expect(await screen.findByText("Nothing recorded yet.")).toBeInTheDocument();
  expect(screen.getByText(/ask it one question/i)).toBeInTheDocument();
  // An empty measure is not a zero. A "0 %" here would read as "nobody ever
  // consults context", which is a different and false claim.
  expect(screen.queryByText("0 of 0")).not.toBeInTheDocument();
});
