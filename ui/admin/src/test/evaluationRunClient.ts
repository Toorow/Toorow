/**
 * The ONE client boundary between ui/admin and the Story 51.2 / 51.3 Evaluation
 * Run capability.
 *
 * Everything below is a typed call to a route that exists in
 * `server/core/evaluation_runs_api.py`. Nothing is computed on this side: the
 * verdict counts, the coverage figures, the unresolved pins and the comparison
 * drift all arrive already decided by `core.evaluation_runs`, which is the only
 * module allowed to decide them. A number derived in the browser would be a
 * second evaluator, and two evaluators disagree the first time either changes.
 *
 * Three shapes this file will not describe, because the server does not return
 * them and inventing a field is how a console starts asking for one:
 *
 *   - a run score, pass rate or trust figure. There is no aggregate column in
 *     the schema and no aggregate key in any payload (`analyze-and-test.md`
 *     `:336-337`). `verdict_counts` is counts per (dimension, verdict) with
 *     every verdict listed, which is what states its own denominator.
 *   - a Render identity. Stories 50.4 / 50.5 / 50.7 own the rendered artifact
 *     and none is delivered. The `render` pin arrives as an entry of
 *     `unresolved_pins`, and `mcp_app_behavior` arrives as `unverifiable`.
 *     This file mints no render identifier and sends none.
 *   - a baseline that moves. A baseline exists only through an explicit
 *     approval; `GET /run-profiles/{id}/baseline` answers `null` when nobody
 *     approved one, and `null` is rendered as "none approved", never as the
 *     most recent run.
 *
 * Every call goes through `apiGet`, so the bearer header cannot be forgotten and
 * a failure arrives as a typed `ApiError` carrying the server's envelope rather
 * than as a silent fallback to invented rows.
 */
import { apiGet } from "../lib/apiFetch";
import type { Verdict } from "./testEvidence";

const base = (projectId: string) => `/api/projects/${encodeURIComponent(projectId)}/test`;

/**
 * The six dimensions of `analyze-and-test.md:319-326`, in document order.
 *
 * This is the ratified document vocabulary, not a catalogue of governed rows:
 * it changes when the architecture document changes, and the server refuses a
 * seventh by name (`core.evaluation_runs.DIMENSIONS`). Holding it here lets the
 * workbench render a dimension the run never recorded as an explicit gap rather
 * than omitting the row, which would read as "nothing to report".
 */
export const EVALUATION_DIMENSIONS = [
  "semantic_correctness",
  "provenance_correctness",
  "context_adherence",
  "path_quality",
  "dq_handling",
  "mcp_app_behavior",
] as const;

/** The four verdicts, in the order the counts table renders them. */
export const EVALUATION_VERDICTS: readonly Verdict[] = [
  "pass",
  "fail",
  "unverifiable",
  "not_applicable",
];

/** The two evidence modes a run can carry. `user_feedback` is not one of them. */
export const EVIDENCE_MODES = ["offline", "observed_cohort"] as const;

// ---------------------------------------------------------------------------
// What the server returns.
// ---------------------------------------------------------------------------

/** One honest absence: which pin, why, and the story that owns it. */
export interface UnresolvedPin {
  pin_family: string;
  reason_code: string;
  owner: string;
  detail: string;
}

export interface EvaluationRunSummary {
  id: string;
  lifecycle: string;
  evidence_mode: string;
  run_profile: string;
  as_of: string | null;
  started_at: string | null;
  ended_at: string | null;
  question_set_fingerprint: string | null;
  unresolved_pin_count: number;
  case_count: number;
}

export interface RunProfile {
  id: string;
  name: string;
  description: string | null;
  evidence_mode: string;
  created_at: string | null;
}

export interface Baseline {
  id: string;
  run_id: string;
  approved_by: string;
  approval_reason: string;
  approved_at: string | null;
  compared_versions: Record<string, unknown>;
}

/** Counts per (dimension, verdict). No total, no ratio — by construction. */
export type VerdictCounts = Record<string, Record<string, number>>;

export interface RunOverview {
  id: string;
  lifecycle: string;
  evidence_mode: string;
  run_profile_id: string;
  as_of: string | null;
  started_at: string | null;
  ended_at: string | null;
  content_hash: string | null;
  question_set_fingerprint: string | null;
  case_count: number;
  unresolved_pins: UnresolvedPin[];
  verdict_counts: VerdictCounts;
}

export interface CaseVerdict {
  verdict_id: string | null;
  dimension: string;
  verdict: string;
  reason_code: string;
  evidence_refs: Record<string, unknown>;
}

export interface RunCase {
  id: string;
  golden_question_version_id: string;
  result_id: string | null;
  /** An exact path id, the literal `No AI path`, or null when evidence is missing. */
  ai_path: string | null;
  business_domain_id: string | null;
  business_domain_version_number: number | null;
  capability_key: string | null;
  result_type: string | null;
  /** Present only when the server joined this exact frozen case to a feedback
   * promotion. These ids are the authority for resolve; the UI never derives
   * either one from the Golden Question, Result or route. */
  feedback_regression: {
    feedback_id: string;
    regression_case_id: string;
  } | null;
  unresolved_pins: UnresolvedPin[];
  verdicts: CaseVerdict[];
}

export interface RunComparison {
  id: string;
  baseline_run_id: string;
  candidate_run_id: string;
  comparison_kind: string;
  changed_pin_families: string[];
  unverifiable_families: string[];
  held_constant_fingerprint: string | null;
  created_by: string | null;
  created_at: string | null;
}

/** One pin family: resolved to an exact identity, or a declared, owned absence. */
export interface EnvironmentFamily {
  family: string;
  state: "pinned" | "unresolved";
  pinned: unknown;
  absence: UnresolvedPin | null;
}

export interface RunEnvironment {
  run_id: string;
  families: EnvironmentFamily[];
  pin_fingerprints: Record<string, string>;
  unresolved_pins: UnresolvedPin[];
}

export interface GateCoverage {
  eligible: number;
  evaluated: number;
  missing: number;
}

export interface GateDecision {
  id: string;
  comparison_id: string;
  decision: string;
  candidate: {
    owner_workspace: string;
    object_type: string;
    object_id: string;
    version_id: string;
  };
  coverage: GateCoverage;
  failing_dimensions: string[];
  decision_reason: string;
  decided_by: string;
  decided_at: string | null;
}

// ---------------------------------------------------------------------------
// The routes.
// ---------------------------------------------------------------------------

export function listEvaluationRuns(
  projectId: string,
  evidenceMode?: string,
  init?: RequestInit,
): Promise<{ evaluation_runs: EvaluationRunSummary[] }> {
  const query = evidenceMode ? `?evidence_mode=${encodeURIComponent(evidenceMode)}` : "";
  return apiGet<{ evaluation_runs: EvaluationRunSummary[] }>(
    `${base(projectId)}/evaluation-runs${query}`,
    init,
  );
}

export function listRunProfiles(
  projectId: string,
  init?: RequestInit,
): Promise<{ run_profiles: RunProfile[] }> {
  return apiGet<{ run_profiles: RunProfile[] }>(`${base(projectId)}/run-profiles`, init);
}

/** `{baseline: null}` means nobody approved one. It never means "the last run". */
export function fetchActiveBaseline(
  projectId: string,
  runProfileId: string,
  init?: RequestInit,
): Promise<{ baseline: Baseline | null }> {
  return apiGet<{ baseline: Baseline | null }>(
    `${base(projectId)}/run-profiles/${encodeURIComponent(runProfileId)}/baseline`,
    init,
  );
}

export function fetchRunOverview(
  projectId: string,
  runId: string,
  init?: RequestInit,
): Promise<RunOverview> {
  return apiGet<RunOverview>(
    `${base(projectId)}/evaluation-runs/${encodeURIComponent(runId)}`,
    init,
  );
}

export function fetchRunCases(
  projectId: string,
  runId: string,
  init?: RequestInit,
): Promise<{ run_id: string; cases: RunCase[] }> {
  return apiGet<{ run_id: string; cases: RunCase[] }>(
    `${base(projectId)}/evaluation-runs/${encodeURIComponent(runId)}/cases`,
    init,
  );
}

export function fetchRunComparisons(
  projectId: string,
  runId: string,
  init?: RequestInit,
): Promise<{ run_id: string; comparisons: RunComparison[] }> {
  return apiGet<{ run_id: string; comparisons: RunComparison[] }>(
    `${base(projectId)}/evaluation-runs/${encodeURIComponent(runId)}/comparisons`,
    init,
  );
}

export function fetchRunEnvironment(
  projectId: string,
  runId: string,
  init?: RequestInit,
): Promise<RunEnvironment> {
  return apiGet<RunEnvironment>(
    `${base(projectId)}/evaluation-runs/${encodeURIComponent(runId)}/environment`,
    init,
  );
}

export function fetchRunGateDecisions(
  projectId: string,
  runId: string,
  init?: RequestInit,
): Promise<{ run_id: string; gate_decisions: GateDecision[] }> {
  return apiGet<{ run_id: string; gate_decisions: GateDecision[] }>(
    `${base(projectId)}/evaluation-runs/${encodeURIComponent(runId)}/gate-decision`,
    init,
  );
}

// ---------------------------------------------------------------------------
// Context adherence — the measure that had no reader.
//
// `app.query_adherence` has been written since migration 034 and read by
// nothing: no API, no screen, no tool. It answers ONE question — when this
// Project was asked a data question, had the governed context been consulted
// first — over an explicit window.
//
// It is not the `context_adherence` DIMENSION of a run, and this file keeps the
// two apart on purpose: a dimension judges one pinned execution against one
// question, this is the ambient rate over a period. Merging them would make a
// project-wide average read as a verdict on a case.
//
// Nothing here is computed in the browser. `share_adherent` arrives beside the
// count it came from, which is what lets a share over four observations be told
// apart from a share over four hundred.
// ---------------------------------------------------------------------------

/** How the two calls were known to belong to the same exchange. Never merged. */
export type AdherenceBasis = "observed_session" | "inferred_window";

export interface AdherenceBucket {
  observations: number;
  adherent: number;
  not_adherent: number;
  /** `null` when the denominator is zero. Never rendered as 0 %. */
  share_adherent: number | null;
  /** The basis every counted bucket stands on. There is no basis-free bucket. */
  basis: AdherenceBasis;
  /** What that basis proves, in words, so no reader has to deduce it. */
  means: string;
}

/**
 * NOTE THAT THIS DOES NOT EXTEND `AdherenceBucket`, and that is the point.
 * It used to, so the overview carried a top-level `adherent` / `share_adherent`
 * summed across both bases, and the screen rendered it as the "All questions"
 * row — the first figure a reader met was one inference merged into the
 * measures. `analyze-and-test.md:1330` requires the two to be "reported apart
 * and never merged", so the totals now live in `by_basis` alone.
 *
 * `observations` stays: it counts measurements taken and carries no verdict, so
 * no share can be built from it.
 */
export interface AdherenceOverview {
  project_id: string;
  window: { days: number; from: string; to: string };
  observations: number;
  first_observation: string | null;
  last_observation: string | null;
  /** One bucket per data tool AND basis. Never one bucket per data tool. */
  by_data_tool: (AdherenceBucket & { data_tool: string })[];
  by_basis: AdherenceBucket[];
  context_tools: { context_tool: string; observations: number }[];
  measured_data_tools: string[];
  measured_context_tools: string[];
  /** Present only when nothing was measured. It says why, and names the gesture. */
  empty_state: { headline: string; detail: string; next_step: string } | null;
}

export function fetchContextAdherence(
  projectId: string,
  days?: number,
  init?: RequestInit,
): Promise<AdherenceOverview> {
  const query = days ? `?days=${encodeURIComponent(String(days))}` : "";
  return apiGet<AdherenceOverview>(`${base(projectId)}/context-adherence${query}`, init);
}
