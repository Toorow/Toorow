/**
 * The ONE client boundary between ui/admin and the Story 51.4 observed-evidence
 * capability: Observed Cohorts and the Trace Observation.
 *
 * Every call maps to a route in `server/core/trace_observation_api.py`. What is
 * NOT here is the point of the file:
 *
 *   - no approve, promote, baseline, gate or blocking call. An Observed Cohort
 *     is a reference window (`analyze-and-test.md:357-358`); the server has no
 *     such route and the schema has no such column, so this client has no such
 *     function. The absence is the enforcement.
 *   - no Render identity. The rendered artifact belongs to Stories 50.4 / 50.5 /
 *     50.7 and none is delivered; the `result-render` lens carries a stated
 *     absence, and this file mints nothing to fill it.
 *   - no percentage. Every aggregate entry arrives with `numerator`,
 *     `denominator`, `coverage` and `version_filters` together, and the screen
 *     renders all four. A ratio computed here would drop the denominator on the
 *     way to the eye.
 */
import { apiGet } from "../lib/apiFetch";

const base = (projectId: string) => `/api/projects/${encodeURIComponent(projectId)}/test`;

/**
 * The five ratified lenses of `analyze-and-test.md:293`, in the exact slugs the
 * server keys its `lenses` object with (`core.trace_observation.LENS_*`). The
 * screen renders `lens_order` from the payload; this list exists so a tab can be
 * addressed before the payload has arrived.
 */
export const TRACE_OBSERVATION_LENSES = [
  { key: "timeline", label: "Timeline" },
  { key: "context-skills", label: "Context & Skills" },
  { key: "tools", label: "Tools" },
  { key: "result-render", label: "Result & Render" },
  { key: "linked-feedback", label: "Linked Feedback" },
] as const;

// ---------------------------------------------------------------------------
// What the server returns.
// ---------------------------------------------------------------------------

/** A declared absence: its state, why, and the story that owns it. */
export interface Absence {
  state: string;
  reason: string | null;
  owner?: string;
  owner_stories?: string[];
  detail?: string | null;
}

export interface CohortSummary {
  id: string;
  label: string | null;
  window_start: string | null;
  window_end: string | null;
  resolved_at: string | null;
  member_count: number;
  filter_hash: string | null;
  evidence_mode: string;
  evidence_label: string;
  blocking: boolean;
}

export interface PathFinding {
  finding: string;
  detail?: string;
  [key: string]: unknown;
}

export interface CohortMember {
  id: string;
  ai_path_id: string;
  query_result_id: string | null;
  path_evidence_state: string;
  render_evidence_state: string;
  render: Absence;
  observed_at: string | null;
  assessment: { verdict: string; findings: PathFinding[] };
}

export interface CohortDetail extends CohortSummary {
  content_hash: string | null;
  created_by: string | null;
  version_filters: Record<string, unknown>;
  members: CohortMember[];
}

/** Every aggregate entry carries all four fields, always. */
export interface AggregateEntry {
  key: unknown;
  numerator: number;
  denominator: number;
  coverage: Record<string, number>;
  version_filters: Record<string, unknown>;
}

export interface CohortAggregates {
  cohort_id: string;
  label: string;
  evidence_mode: string;
  blocking: boolean;
  denominator: number;
  coverage: Record<string, number>;
  version_filters: Record<string, unknown>;
  by_required_node: AggregateEntry[];
  by_skill: AggregateEntry[];
  by_tool: AggregateEntry[];
  by_semantic_view_version: AggregateEntry[];
  by_result_outcome: AggregateEntry[];
  by_path_verdict: AggregateEntry[];
  unavailable_axes: Array<{ axis: string; state: string; reason: string }>;
}

export interface TimelineStep {
  ordinal: number | null;
  observed_at: string | null;
  step_kind: string | null;
  outcome: string | null;
}

export interface ContextSkillStep {
  ordinal: number | null;
  owner_workspace: string | null;
  owner_object_type: string | null;
  owner_object_id: string | null;
  owner_version_id: string | null;
  /** Both halves or neither: half a Skill pin resolves to the wrong step. */
  skill: { skill_version_id: string; skill_step_id: string } | null;
}

export interface ToolStep {
  ordinal: number | null;
  tool_name: string | null;
  outcome: string | null;
}

export interface ResultLens {
  result:
    | { state: "absent"; reason: string }
    | {
        state: "resolved";
        id: string;
        outcome: string | null;
        content_hash: string | null;
        row_count: number | null;
        truncated: boolean | null;
        owner_href: string;
      };
  render: Absence;
}

export interface TraceObservation {
  ai_path_id: string;
  project_id: string;
  lifecycle: string | null;
  outcome: string | null;
  actor: string | null;
  started_at: string | null;
  ended_at: string | null;
  model_ref: string | null;
  tool_catalog_version: string | null;
  w3c_trace_id: string | null;
  w3c_trace_id_is_correlation_not_identity: boolean;
  path_evidence_state: string;
  lens_order: string[];
  lenses: {
    timeline: { ordering: string; steps: TimelineStep[] };
    "context-skills": { steps: ContextSkillStep[] };
    tools: { tool_name_is_a_label: boolean; steps: ToolStep[] };
    "result-render": ResultLens;
    "linked-feedback": { feedback: Absence; records: unknown[] };
  };
  dimensions: {
    evidence_mode: string;
    blocking: boolean;
    label: string;
    path_quality: { verdict: string; findings: PathFinding[] };
    render_behavior: Absence;
    mcp_app_behavior: Absence;
  };
  proposals_suggested: Array<{ member_id: string | null; reason_code: string; severity_hint: string }>;
  owner_links: Record<string, string>;
}

// ---------------------------------------------------------------------------
// The routes.
// ---------------------------------------------------------------------------

export function listObservedCohorts(
  projectId: string,
  init?: RequestInit,
): Promise<{ cohorts: CohortSummary[] }> {
  return apiGet<{ cohorts: CohortSummary[] }>(`${base(projectId)}/observed-cohorts`, init);
}

export function fetchObservedCohort(
  projectId: string,
  cohortId: string,
  init?: RequestInit,
): Promise<CohortDetail> {
  return apiGet<CohortDetail>(
    `${base(projectId)}/observed-cohorts/${encodeURIComponent(cohortId)}`,
    init,
  );
}

export function fetchCohortAggregates(
  projectId: string,
  cohortId: string,
  init?: RequestInit,
): Promise<CohortAggregates> {
  return apiGet<CohortAggregates>(
    `${base(projectId)}/observed-cohorts/${encodeURIComponent(cohortId)}/aggregates`,
    init,
  );
}

export function fetchTraceObservation(
  projectId: string,
  aiPathId: string,
  init?: RequestInit,
): Promise<TraceObservation> {
  return apiGet<TraceObservation>(
    `${base(projectId)}/trace-observations/${encodeURIComponent(aiPathId)}`,
    init,
  );
}
