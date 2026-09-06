/**
 * The cross-surface invariant of the Test workspace: **the three evidence modes
 * never collapse into one score** (`analyze-and-test.md:210-216`, Story 51.2
 * AC9, Story 51.4 AC9, Story 51.5 AC8).
 *
 * This file used to test the Epic 14 vestiges of the three Test screens — a
 * `precision_pct` over `/api/eval/runs`, a `positivePct` over `/api/feedback`,
 * and a benchmark question row. Every one of those contracts is retired: they
 * were the on-screen form of the merge the document forbids, and asserting them
 * would now pin the defect in place.
 *
 * What survives is the invariant they violated, asserted across the two
 * surfaces at once, plus the per-screen suites:
 *
 *   - `RegressionRuns.test.tsx`, `EvaluationRunWorkbench.test.tsx`,
 *     `TraceObservationWorkbench.test.tsx` (offline and observed evidence);
 *   - `WidgetFeedback.test.tsx`, `FeedbackReviewWorkbench.test.tsx` (user
 *     annotations);
 *   - `GoldenQuestions.test.tsx`, `GoldenQuestionWorkbench.test.tsx` (Story
 *     51.1), which replaced this file's Golden Questions block.
 */
import { render, screen } from "@testing-library/react";
import RegressionRuns from "../shell/pages/RegressionRuns";
import WidgetFeedback from "../shell/pages/WidgetFeedback";

const PROJECT = "proj_EXAMPLE";

function response(body: unknown, status = 200): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response;
}

function mockApi(handlers: Array<[RegExp, () => Response]>) {
  const calls: string[] = [];
  const fetchMock = vi.fn((url: string) => {
    calls.push(url);
    for (const [pattern, produce] of handlers) {
      if (pattern.test(url)) return Promise.resolve(produce());
    }
    return Promise.resolve(response({ code: "not_found", message: "Not found" }, 404));
  });
  vi.stubGlobal("fetch", fetchMock);
  return { fetchMock, calls };
}

const OFFLINE_RUN = {
  id: "erun_EXAMPLE",
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

const AGGREGATES = {
  project_id: PROJECT,
  version_filters: {},
  // LES NOMS DU FIL, PAS CEUX D'AVANT. `feedback-aggregate.v1` porte
  // `eligible_interactions` / `annotated_interactions` ; la fixture disait
  // encore `*_results`, donc elle décrivait une réponse qu'aucun serveur
  // n'envoie — et un écran qui lit le bon champ y lisait `undefined`.
  scope: {
    eligible_interactions: 40,
    annotated_interactions: 6,
    annotations: 7,
    coverage: 0.15,
    coverage_state: "stated",
    coverage_reason: null,
  },
  axes: {},
  evidence_lenses: {},
  blocking_use: "Prioritizes investigation; never proves correctness or regression alone.",
};

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("Test workspace evidence modes", () => {
  it("keeps offline and observed evidence modes visibly distinct and never merged", async () => {
    mockApi([
      [/\/run-profiles\/[^/]+\/baseline$/, () => response({ baseline: null })],
      [/\/run-profiles$/, () => response({ run_profiles: [] })],
      [
        /\/evaluation-runs\?evidence_mode=offline$/,
        () => response({ evaluation_runs: [OFFLINE_RUN] }),
      ],
      [/\/observed-cohorts$/, () => response({ cohorts: [COHORT] })],
    ]);
    render(<RegressionRuns projectId={PROJECT} />);

    // Two sections, two headings, two counts that are never added together.
    expect(await screen.findByRole("heading", { name: "Offline" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Observed Cohort" })).toBeInTheDocument();
    expect(screen.getByText(/Reproducible, eligible to block/i)).toBeInTheDocument();
    expect(screen.getByText(/Reference windows, never blocking/i)).toBeInTheDocument();

    // Not one merged figure survives. The assertion is on what the screen
    // MEASURES — its columns and its cells — not on its prose, which says the
    // words "pass rate" precisely in order to state that there is none.
    expect(screen.queryByText(/^\d+\s?%$/)).not.toBeInTheDocument();
    expect(
      screen.queryByRole("columnheader", { name: /pass rate|latest score|precision|score/i }),
    ).not.toBeInTheDocument();
    expect(screen.getByText(/never merged into a trust score/i)).toBeInTheDocument();
  });

  it("never presents user feedback as a correctness figure", async () => {
    // `(\?|$)` ET PAS `$` SEUL. `WidgetFeedback` demande ses agrégats avec
    // `{ limit: 200 }` et ses négatifs critiques avec `{ limit: 50 }`, donc les
    // deux URL portent une query : `$` ne matchait plus, `mockApi` rendait 404,
    // et l'écran affichait honnêtement « This collection was not opened » — un
    // 404 est indistinguable d'un refus, par construction. Le test lisait cet
    // état comme l'absence de sa phrase. Le préfixe reste épinglé exactement :
    // c'est la query qui est tolérée, pas la route.
    mockApi([
      [/\/feedback\/aggregates(\?|$)/, () => response(AGGREGATES)],
      [
        /\/feedback\/critical-negatives(\?|$)/,
        () => response({ project_id: PROJECT, items: [], seed_target_owner_story: "51.1" }),
      ],
      [/\/feedback(\?|$)/, () => response({ project_id: PROJECT, limit: 50, offset: 0, items: [] })],
    ]);
    render(<WidgetFeedback projectId={PROJECT} />);

    expect(await screen.findByText(/never proves correctness or regression alone/i)).toBeInTheDocument();
    // The denominator travels with the count, and no bare percentage exists.
    expect(screen.getByText("6 / 40")).toBeInTheDocument();
    expect(screen.queryByText(/^\d+\s?%$/)).not.toBeInTheDocument();
  });

  it("reads the pinned Test routes and never the retired Epic 14 ones", async () => {
    const { calls } = mockApi([
      [/\/run-profiles\/[^/]+\/baseline$/, () => response({ baseline: null })],
      [/\/run-profiles$/, () => response({ run_profiles: [] })],
      [/\/evaluation-runs\?evidence_mode=offline$/, () => response({ evaluation_runs: [] })],
      [/\/observed-cohorts$/, () => response({ cohorts: [] })],
    ]);
    render(<RegressionRuns projectId={PROJECT} />);
    await screen.findByRole("heading", { name: "Offline" });

    // `/api/eval/runs` is the shallow legacy score row; `/api/feedback` is the
    // unpinned annotation. Neither is read, aliased or fallen back to.
    expect(calls.some((url) => url.startsWith("/api/eval/"))).toBe(false);
    expect(calls.some((url) => url === "/api/feedback" || url.startsWith("/api/feedback?"))).toBe(
      false,
    );
    expect(calls.every((url) => url.startsWith(`/api/projects/${PROJECT}/test/`))).toBe(true);
  });
});
