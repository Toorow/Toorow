/**
 * The ONE client boundary between ui/admin and the versioned feedback-review
 * capability (Story 65.8).
 *
 * Every call maps to a route in `server/core/feedback_review_api.py`, under
 * `/api/projects/{id}/test/feedback`. The legacy `GET /api/feedback` (Story 5.5)
 * is a different, unpinned read: it is not called, aliased or fallen back to
 * here, because a screen that quietly reads the old row would show annotations
 * that pin no Result and look identical to ones that do.
 *
 * What this file refuses to describe:
 *
 *   - a positive percentage. `analyze-and-test.md:313` forbids presenting a raw
 *     thumbs-up rate as correctness, and the screen that this replaces computed
 *     exactly one. There is no ratio field below and no function that makes one;
 *     `scope` and each bucket carry counts, a denominator, a coverage state and
 *     the version filters that produced them.
 *   - a merged verdict. User polarity, append-only human review and automated
 *     evaluation verdicts remain three independently named records. No key here
 *     combines them.
 *   - client-side classification or aggregation. Every exact pin, cohort and
 *     count below is a server projection of immutable evidence.
 */
import { apiGet, apiJson } from "../lib/apiFetch";
import { definitionPayload } from "./goldenQuestionClient";
import type {
  DefinitionDraft,
  ExpectedAiPath,
  ProvenanceRequirement,
  V2Assertion,
} from "./goldenQuestionClient";

const base = (projectId: string) => `/api/projects/${encodeURIComponent(projectId)}/test`;

/**
 * The review vocabularies. These are the ratified document vocabularies of
 * `analyze-and-test.md` — the six objective dimensions (`:319-326`) plus
 * `not_applicable`, the four verdicts (`:282-283`), the three severities and the
 * six review states — not a catalogue of governed rows that a project can grow.
 * They are mirrored from `core.feedback_review`, which refuses anything outside
 * them by name, so a value offered here is one the server accepts.
 */
export const AFFECTED_DIMENSIONS = [
  "semantic_correctness",
  "provenance_correctness",
  "context_adherence",
  "path_quality",
  "dq_handling",
  "mcp_app_behavior",
  "not_applicable",
] as const;

export const HUMAN_VERDICTS = ["pass", "fail", "unverifiable", "not_applicable"] as const;
export const SEVERITIES = ["critical", "major", "minor"] as const;
export const REVIEW_STATES = [
  "unreviewed",
  "triaged",
  "accepted",
  "rejected",
  "duplicate",
  "resolved",
] as const;

/** The five ratified tabs of `analyze-and-test.md:294`, in that order. */
export const FEEDBACK_REVIEW_TABS = [
  { key: "feedback", label: "Feedback" },
  { key: "result-render", label: "Result & Render" },
  { key: "ai-path", label: "AI Path" },
  { key: "classification", label: "Classification" },
  { key: "resolution", label: "Resolution" },
] as const;

// ---------------------------------------------------------------------------
// What the server returns.
// ---------------------------------------------------------------------------

export interface EvidenceLens {
  state: string;
  reason: string | null;
  owner_stories: string[];
  detail: string | null;
}

export interface OwnerLink {
  workspace: string;
  section: string;
  object_type: string;
  object_id: string;
  tab: string | null;
  version_id: string | null;
}

export interface UnavailableOwnerLink extends EvidenceLens {
  target: string;
}

export interface FeedbackAnnotation {
  schema_version: "feedback-review-detail.v1";
  id: string;
  source: "authenticated" | "anonymous_share";
  target_schema_version: string | null;
  polarity: string;
  comment: string | null;
  actor: unknown | null;
  actor_source: unknown | null;
  observed_surface: string;
  interaction_ref: string | Record<string, unknown> | null;
  w3c_trace_id: string | null;
  result: {
    id: string;
    content_hash: string | null;
    outcome: string | null;
    query_spec_version_id: string | null;
  };
  render: {
    render_id: string | null;
    visualization_spec_version_id: string | null;
    renderer_build_id: string | null;
    runtime_build_id: string | null;
    theme_version: string | null;
    formatter_version: string | null;
  } | null;
  target:
    | { kind: "answer" }
    | { kind: "datum"; row_index: number; field: string }
    | { kind: "path_step"; ordinal: number }
    | null;
  semantic_view: { id: string | null; version_id: string | null };
  classification: {
    schema_version: "evaluation-classification.v1";
    semantic_view: Record<string, unknown> | null;
    business_domains: Record<string, unknown> | null;
    skills: Record<string, unknown> | null;
    capability: Record<string, unknown> | null;
    result_type: Record<string, unknown> | null;
    classification_hash?: string;
  };
  /** An exact path id or the literal `No AI path`. Never both, never neither. */
  ai_path: string | null;
  visible_versions: Record<string, unknown>;
  visible_versions_hash: string | null;
  /** What the person SAW versus what the record PINS, when they disagree. */
  version_divergence: Array<{ field: string; visible: unknown; pinned: unknown }>;
  observed_at: string | null;
  created_at: string | null;
  evidence_lenses: Record<string, EvidenceLens>;
  owner_links: OwnerLink[];
  unavailable_owner_links: UnavailableOwnerLink[];
  review: {
    current_state: string;
    current_version_id: string | null;
    versions?: ReviewVersion[];
    versions_truncated?: boolean;
    versions_next_cursor?: string | null;
  };
  automated_verdicts: AutomatedVerdicts;
  blocking_use: string;
}

export interface AutomatedVerdict {
  run_id: string;
  case_id: string;
  dimension: string;
  verdict: string;
  reason_code: string;
  evidence_refs: unknown;
  created_at: string | null;
  owner_link: OwnerLink;
}

export interface AutomatedVerdicts {
  state: "available" | "unavailable";
  reason: string | null;
  items: AutomatedVerdict[];
  truncated: boolean;
}

/** Collection rows are bounded summaries; exact automated matches belong to detail. */
export type FeedbackCollectionItem = Omit<FeedbackAnnotation, "automated_verdicts"> & {
  automated_verdicts?: AutomatedVerdicts;
};

export interface ReviewVersion {
  id: string;
  version_number: number;
  review_state: string;
  affected_dimension: string;
  /** The HUMAN verdict, under a name that says so. */
  human_verdict: string;
  severity: string;
  reason: string;
  reviewer: string;
  predecessor_version_id: string | null;
  created_at: string | null;
}

/** Immutable acknowledgement of one append (or its idempotent replay). */
export interface FeedbackReviewReceipt {
  schema_version: "feedback-review-receipt.v1";
  status: "recorded" | "replayed";
  feedback_id: string;
  review_version_id: string;
  version_number: number;
  predecessor_version_id: string | null;
}

export interface RegressionDomainOption {
  domain_id: string;
  version_number: number;
}

export interface FeedbackRegressionCreateCommand {
  schema_version: "feedback-regression-create.v1";
  expected_review_version_id: string;
  retry_key: string;
  title: string;
  owner: string;
  reproduction_reason: string;
  selected_domain: RegressionDomainOption;
  question: string;
  time_boundary: Record<string, unknown>;
  expected_result: V2Assertion[];
  required_provenance: ProvenanceRequirement[];
  expected_ai_path: ExpectedAiPath;
}

export interface FeedbackRegressionDraftAvailable {
  schema_version: "feedback-regression-draft.v1";
  state: "available";
  reason: null;
  subject: { feedback_id: string; source: "authenticated" | "anonymous_share" };
  review: {
    version_id: string;
    state: "accepted";
    affected_dimension: string;
    human_verdict: "fail";
    severity: string;
    reason: string;
  };
  frozen: {
    result: { id: string; content_hash: string; outcome: string };
    query_spec_version_id: string;
    classification_hash: string;
    semantic_view: { state: "attributed"; id: string; version_id: string };
    capability: { key: string; version_id: string };
    result_type: string;
    eligible_target: { kind: "answer" } | { kind: "datum"; row_index: number; field: string } | { kind: "path_step"; ordinal: number };
    render: FeedbackAnnotation["render"];
    ai_path: string;
  };
  domain_options: RegressionDomainOption[];
  requires_domain_selection: boolean;
  create_contract: {
    schema_version: "feedback-regression-create.v1";
    expected_review_version_id: string;
  };
}

export interface FeedbackRegressionDraftUnavailable {
  schema_version: "feedback-regression-draft.v1";
  state: "unavailable";
  reason: string;
  subject: null;
  review: null;
  frozen: null;
  domain_options: [];
  requires_domain_selection: false;
  create_contract: null;
}

export type FeedbackRegressionDraft =
  | FeedbackRegressionDraftAvailable
  | FeedbackRegressionDraftUnavailable;

export interface FeedbackRegressionAuthoringDraft {
  title: string;
  owner: string;
  reproduction_reason: string;
  selected_domain: RegressionDomainOption;
  definition: DefinitionDraft;
}

/** Project the Resolution authoring state into the one closed create command.
 * Frozen Result and classification evidence are intentionally not parameters:
 * the server already owns those pins, and the UI cannot turn observed output
 * into expected truth by copying it into this command. */
export function feedbackRegressionCreatePayload(
  contract: FeedbackRegressionDraftAvailable["create_contract"],
  retryKey: string,
  authored: FeedbackRegressionAuthoringDraft,
): FeedbackRegressionCreateCommand {
  const definition = definitionPayload(authored.definition);
  return {
    schema_version: contract.schema_version,
    expected_review_version_id: contract.expected_review_version_id,
    retry_key: retryKey,
    title: authored.title.trim(),
    owner: authored.owner.trim(),
    reproduction_reason: authored.reproduction_reason.trim(),
    selected_domain: authored.selected_domain,
    question: String(definition.question ?? ""),
    time_boundary: definition.time_boundary as Record<string, unknown>,
    expected_result: definition.expected_result as V2Assertion[],
    required_provenance: definition.required_provenance as ProvenanceRequirement[],
    expected_ai_path: definition.expected_ai_path as ExpectedAiPath,
  };
}

export interface FeedbackRegressionReceipt {
  schema_version: "feedback-regression-receipt.v1";
  status: "created" | "replayed";
  regression_case_id: string;
  golden_question_id: string;
  golden_question_version_id: string;
  review_version_id: string;
  result_id: string;
  result_content_hash: string;
  classification_hash: string;
  owner_links: OwnerLink[];
}

export interface EvaluationResultReceipt {
  schema_version: "evaluation-result-receipt.v1";
  status: "evaluated" | "replayed";
  evaluation_case_id: string;
  run_id: string;
  producer: "result-case-evaluator.v1";
  producer_contract_hash: string;
  assertion_results: Array<Record<string, unknown>>;
  verdicts: Record<string, {
    verdict_id: string;
    verdict: string;
    reason_code: string;
    evidence_refs: Record<string, unknown>;
    missing?: unknown[];
  }>;
  owner_links: OwnerLink[];
}

export interface FeedbackRegressionResolutionReceipt {
  schema_version: "feedback-regression-resolution.v1";
  status: "resolved" | "unresolved";
  resolution_id: string | null;
  reason: string | null;
  regression_case_id: string;
  evaluation_case_id: string;
  evaluation_run_id: string | null;
  verdict_id: string | null;
  owner_links: OwnerLink[];
}

export interface AggregateBucket {
  key: Record<string, unknown> | null;
  attributed: boolean;
  unattributed_reason: string | null;
  positive: number;
  negative: number;
  annotations: number;
  annotated_interactions: number;
  eligible_interactions: number | null;
  coverage: number | null;
  coverage_state: string;
  coverage_reason: string | null;
  unresolved_critical_negatives: number;
  normalized_filters: Record<string, unknown>;
  compatibility_key: string;
}

export interface AggregateAxis {
  buckets: AggregateBucket[];
  buckets_are_exclusive: boolean;
  buckets_overlap_note: string | null;
}

export interface FeedbackAggregates {
  schema_version: "feedback-aggregate.v1";
  project_id: string;
  compatibility_schema_version: "feedback-compatibility.v1";
  normalized_filters: Record<string, unknown>;
  scope: {
    eligible_interactions: number | null;
    annotated_interactions: number;
    annotations: number;
    coverage: number | null;
    coverage_state: string;
    coverage_reason: string | null;
  };
  axes: Record<string, AggregateAxis>;
  blocking_use: string;
  truncated: boolean;
  next_cursor: string | null;
}

/** The document's five axes, in `analyze-and-test.md:308-309` order. */
export const AGGREGATE_AXES = [
  { key: "business_domain", label: "Business Domain" },
  { key: "skill", label: "Skill" },
  { key: "semantic_view", label: "Semantic View" },
  { key: "capability", label: "Capability" },
  { key: "result_type", label: "Result type" },
] as const;

// ---------------------------------------------------------------------------
// The routes.
// ---------------------------------------------------------------------------

export interface FeedbackFilters {
  observed_from: string;
  observed_to: string;
  semantic_view_version_id?: string;
  business_domain_id?: string;
  business_domain_version_number?: string;
  skill_version_id?: string;
  capability?: string;
  result_type?: string;
  target_kind?: string;
  source?: string;
  surface?: string;
  visualization_spec_version_id?: string;
  renderer_build_id?: string;
  runtime_build_id?: string;
  theme_version?: string;
  formatter_version?: string;
}

function appendQuery(search: URLSearchParams, values: object) {
  for (const [name, value] of Object.entries(values)) {
    if (value !== undefined && value !== null && value !== "") search.set(name, String(value));
  }
}

export function listFeedback(
  projectId: string,
  options: FeedbackFilters & { polarity?: string; limit?: number; cursor?: string },
  init?: RequestInit,
): Promise<{
  schema_version: "feedback-review-collection.v1";
  project_id: string;
  normalized_filters: Record<string, unknown>;
  limit: number;
  items: FeedbackCollectionItem[];
  next_cursor: string | null;
  truncated: boolean;
}> {
  const search = new URLSearchParams();
  appendQuery(search, options);
  const query = search.toString();
  return apiGet(`${base(projectId)}/feedback${query ? `?${query}` : ""}`, init);
}

export function fetchFeedbackAggregates(
  projectId: string,
  filters: FeedbackFilters,
  paging: { cursor?: string; limit?: number } = {},
  init?: RequestInit,
): Promise<FeedbackAggregates> {
  const search = new URLSearchParams();
  appendQuery(search, { ...filters, ...paging });
  const query = search.toString();
  return apiGet<FeedbackAggregates>(
    `${base(projectId)}/feedback/aggregates${query ? `?${query}` : ""}`,
    init,
  );
}

export interface FeedbackReviewHistory {
  feedback_id: string;
  versions: ReviewVersion[];
  truncated: boolean;
  next_cursor: string | null;
}

export interface CriticalNegativeCollection {
  schema_version: "feedback-critical-negatives.v1";
  project_id: string;
  items: FeedbackCollectionItem[];
  truncated: boolean;
  next_cursor: string | null;
  seed_target_owner_story: string;
}

export function listFeedbackReviews(
  projectId: string,
  feedbackId: string,
  options: { cursor?: string; limit?: number } = {},
  init?: RequestInit,
): Promise<FeedbackReviewHistory> {
  const search = new URLSearchParams();
  appendQuery(search, options);
  const query = search.toString();
  return apiGet(
    `${base(projectId)}/feedback/${encodeURIComponent(feedbackId)}/reviews${query ? `?${query}` : ""}`,
    init,
  );
}

export function fetchFeedbackCriticalNegatives(
  projectId: string,
  options: { limit?: number; cursor?: string } = {},
  init?: RequestInit,
): Promise<CriticalNegativeCollection> {
  const search = new URLSearchParams();
  appendQuery(search, options);
  const query = search.toString();
  return apiGet(`${base(projectId)}/feedback/critical-negatives${query ? `?${query}` : ""}`, init);
}

export function fetchFeedbackAnnotation(
  projectId: string,
  feedbackId: string,
  init?: RequestInit,
): Promise<FeedbackAnnotation> {
  return apiGet<FeedbackAnnotation>(
    `${base(projectId)}/feedback/${encodeURIComponent(feedbackId)}`,
    init,
  );
}

export function fetchFeedbackRegressionDraft(
  projectId: string,
  feedbackId: string,
  init?: RequestInit,
): Promise<FeedbackRegressionDraft> {
  return apiGet<FeedbackRegressionDraft>(
    `${base(projectId)}/feedback/${encodeURIComponent(feedbackId)}/regression-draft`,
    init,
  );
}

function postJson<T>(url: string, body: unknown, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (!headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  return apiJson<T>(url, {
    ...init,
    method: "POST",
    headers,
    body: JSON.stringify(body),
  });
}

export function createFeedbackRegressionCase(
  projectId: string,
  feedbackId: string,
  command: FeedbackRegressionCreateCommand,
  init?: RequestInit,
): Promise<FeedbackRegressionReceipt> {
  return postJson(
    `${base(projectId)}/feedback/${encodeURIComponent(feedbackId)}/regression-cases`,
    command,
    init,
  );
}

export function evaluateFeedbackRegressionCase(
  projectId: string,
  caseId: string,
  retryKey: string,
  init?: RequestInit,
): Promise<EvaluationResultReceipt> {
  return postJson(
    `${base(projectId)}/evaluation-cases/${encodeURIComponent(caseId)}/evaluate`,
    { schema_version: "evaluation-request.v1", retry_key: retryKey },
    init,
  );
}

export function resolveFeedbackRegression(
  projectId: string,
  feedbackId: string,
  regressionCaseId: string,
  evaluationCaseId: string,
  init?: RequestInit,
): Promise<FeedbackRegressionResolutionReceipt> {
  return postJson(
    `${base(projectId)}/feedback/${encodeURIComponent(feedbackId)}/regression-resolution`,
    {
      schema_version: "feedback-regression-resolve.v1",
      regression_case_id: regressionCaseId,
      evaluation_case_id: evaluationCaseId,
    },
    init,
  );
}

/** The review draft, flat and all-strings, mirroring what the server accepts. */
export interface ReviewDraft {
  state: string;
  affected_dimension: string;
  human_verdict: string;
  severity: string;
  reason: string;
}

export function emptyReviewDraft(): ReviewDraft {
  return {
    state: "",
    affected_dimension: "",
    human_verdict: "",
    severity: "",
    reason: "",
  };
}

/**
 * The draft as the exact document `POST /feedback/{id}/reviews` accepts.
 *
 * All seven fields are sent because `feedback-review-command.v1` is a closed
 * command contract. The console never adds classification or seed fields.
 */
export function reviewPayload(
  draft: ReviewDraft,
  expectedHead: string | null,
  retryKey: string,
): Record<string, unknown> {
  return {
    schema_version: "feedback-review-command.v1",
    expected_head: expectedHead,
    retry_key: retryKey,
    state: draft.state,
    affected_dimension: draft.affected_dimension,
    human_verdict: draft.human_verdict,
    severity: draft.severity,
    reason: draft.reason.trim(),
  };
}

export function appendFeedbackReview(
  projectId: string,
  feedbackId: string,
  draft: ReviewDraft,
  expectedHead: string | null,
  retryKey: string,
  init: RequestInit = {},
): Promise<FeedbackReviewReceipt> {
  const headers = new Headers(init.headers);
  if (!headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  return apiJson<FeedbackReviewReceipt>(
    `${base(projectId)}/feedback/${encodeURIComponent(feedbackId)}/reviews`,
    {
      ...init,
      method: "POST",
      headers,
      body: JSON.stringify(reviewPayload(draft, expectedHead, retryKey)),
    },
  );
}
