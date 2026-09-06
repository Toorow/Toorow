/**
 * The ONE client boundary between ui/admin and the Story 50.1 analytical service.
 *
 * Everything here is a typed call to a server route — no member catalogue is kept
 * on this side, no compatibility is inferred, and no Result is assembled locally.
 * That is deliberate: the compiled artifact is the semantic authority, and a
 * browser that cached its own copy would keep offering a member the day the
 * compiler stopped proving it.
 *
 * Every call goes through `apiJson`, so the bearer header cannot be forgotten and
 * a failure arrives as a typed `ApiError` with the server's `{code, message}`
 * envelope — never as a silent fallback to invented data.
 */
import { apiGet, apiPost } from "../lib/apiFetch";
import type { ExactFeedbackRequest } from "@toorow/card-shell/viz";

/** A governed member the compiled artifact proved queryable. */
export interface QueryMember {
  concept_id: string;
  version_id: string;
  label: string;
}

export interface QueryOptions {
  semantic_view_id: string;
  semantic_view_version_id: string;
  /** Stated by the server, never inferred here from an empty member list. */
  executable: boolean;
  status: string;
  measures: QueryMember[];
  dimensions: QueryMember[];
  pairs: { measure_id: string; dimension_id: string; queryable: boolean }[];
}

/** The five terminal outcomes. `empty` and `unavailable` are NOT the same state. */
export type ResultOutcome = "success" | "empty" | "degraded" | "refused" | "unavailable";

export interface ExecutionReceipt {
  attempt_id: string;
  result_id: string;
  outcome: ResultOutcome;
  row_count: number;
  truncated: boolean;
  content_hash: string;
  /** Either an AI Path id or the exact literal `No AI path`. Never null. */
  ai_path: string;
}

export interface ResultEvidence {
  result_id: string;
  content_hash: string;
  outcome: ResultOutcome;
  row_count: number;
  truncated: boolean;
  /** `value_type`/`unit` are what the plan froze about a column: a money column
   *  is canonical micros, and a reader that ignores them prints the stored
   *  integer as if it were the amount. Optional because a Result frozen before
   *  the plan carried them says nothing, and silence is not a currency. */
  schema: { fields?: { name: string; value_type?: string | null; unit?: string | null }[] };
  manifest: Record<string, unknown>;
  rows: Record<string, unknown>[];
  feedback_context?: unknown;
}

export interface QuerySpecVersionRef {
  query_spec_id: string;
  id: string;
  version_number: number;
  content_hash: string;
  semantic_view_version_id: string;
}

const base = (projectId: string) => `/api/projects/${encodeURIComponent(projectId)}/analyze`;

export function fetchQueryOptions(
  projectId: string,
  semanticViewId: string,
  semanticViewVersionId: string,
  init?: RequestInit,
): Promise<QueryOptions> {
  const query = new URLSearchParams({
    semantic_view_id: semanticViewId,
    semantic_view_version_id: semanticViewVersionId,
  });
  return apiGet<QueryOptions>(`${base(projectId)}/query-options?${query}`, init);
}

export function createQuerySpec(
  projectId: string,
  body: {
    semantic_view_id: string;
    semantic_view_version_id: string;
    spec: Record<string, unknown>;
    name?: string;
  },
): Promise<QuerySpecVersionRef> {
  return apiPost<QuerySpecVersionRef>(`${base(projectId)}/query-specs`, body);
}

export function executeQuerySpecVersion(
  projectId: string,
  versionId: string,
): Promise<ExecutionReceipt> {
  return apiPost<ExecutionReceipt>(
    `${base(projectId)}/query-spec-versions/${encodeURIComponent(versionId)}/execute`,
  );
}

export function fetchResultEvidence(
  projectId: string,
  resultId: string,
  init?: RequestInit,
  renderId?: string | null,
): Promise<ResultEvidence> {
  const query = renderId ? `?render_id=${encodeURIComponent(renderId)}` : "";
  return apiGet<ResultEvidence>(
    `${base(projectId)}/results/${encodeURIComponent(resultId)}/evidence${query}`,
    init,
  );
}

export function submitExactFeedback(
  projectId: string,
  request: ExactFeedbackRequest,
): Promise<unknown> {
  return apiPost(`/api/projects/${encodeURIComponent(projectId)}/test/feedback`, request);
}
