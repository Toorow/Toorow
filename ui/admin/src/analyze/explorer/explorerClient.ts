/**
 * The ONE client boundary between the Analytics Explorer and its server owners.
 *
 * Four calls, four owners, and no composition on this side: the browser never
 * decides which sources can be crossed, never counts a matched row, never
 * freezes a plan and never aggregates a cell. It asks, and it renders what came
 * back — including the states it would be tempting to smooth over.
 *
 * Everything goes through `apiJson`, so a failure arrives as a typed `ApiError`
 * carrying the server's `{code, message}` envelope and never as a silent
 * fallback to invented data.
 */
import { apiGet, apiPost } from "../../lib/apiFetch";

/** The three states a match reports, and they are never merged into one badge. */
export type MatchAuthority = "governed" | "needs_governance";
export type ObservedCoverage = "exact" | "estimated" | "unavailable";
export type ExecutionSafety = "ready" | "review_required" | "unsafe";

export interface MatchSide {
  datastream_id: string;
  name: string;
  mapping_version_id: string;
  mapping_version_number?: number | null;
  published_execution_id: string;
  output_version_id: string;
  measures: {
    canonical_field_id: string;
    name: string;
    aggregation: string;
  }[];
}

export interface MatchKeyPath {
  canonical_name: string | null;
  canonical_field_id: string | null;
  left_field: string | null;
  right_field: string | null;
  /**
   * Le type physique declare de chaque cote, gele pour qu'une epingle soit
   * lisible. OPTIONNEL parce qu'il l'est reellement : seule la branche gouvernee
   * les pose, les deux branches candidates construisent le meme tableau sans eux.
   * Un tableau, deux formes selon `kind` -- le declarer obligatoire ferait mentir
   * le type sur ce que le serveur envoie.
   */
  left_physical_type?: string | null;
  right_physical_type?: string | null;
}

/** The EXECUTOR's own refusal for this relationship, whole. `null` means the
 *  executor has no objection — never "nobody checked". Added 2026-08-16, when
 *  discovery was found advertising `ready` for relationships compilation
 *  refuses; the badge alone named no gesture. */
export interface ExecutionBlock {
  code: string;
  message: string;
}

export interface DatastreamMatch {
  kind:
    | "governed"
    | "candidate_key_missing"
    | "candidate_binding_missing"
    // Le serveur l'emet depuis toujours (`_pair_matches`), et ce type ne le
    // connaissait pas : exactement l'argument << le troisieme etat que le
    // contrat existe pour interdire >>, non applique a lui-meme.
    | "ambiguous_relationship";
  authority: MatchAuthority;
  observed_coverage: ObservedCoverage;
  execution_safety: ExecutionSafety;
  execution_blocked: ExecutionBlock | null;
  left: MatchSide;
  right: MatchSide;
  common_key: {
    id: string;
    name: string;
    version_id: string;
    version_number: number;
    components: { canonical_field_id: string; canonical_name: string }[];
  } | null;
  key_paths: MatchKeyPath[];
  relationship: {
    relationship_name: string;
    cardinality: string;
    fan_out_policy: string;
    bridge_dataset: string | null;
    view_id: string;
    view_version_id: string;
    view_version_number: number;
    business_domain_refs?: string[];
  } | null;
  unlocked_measures: number;
  analysis: string;
  next_action?: string;
  explore_together: {
    datastreams: {
      datastream_id: string;
      mapping_version_id: string;
      published_execution_id: string;
      output_version_id: string;
    }[];
    common_key_version_id: string;
    view_version_id: string;
    relationship_name: string;
  } | null;
}

export interface MatchCatalog {
  matches: DatastreamMatch[];
  counts: { datastreams_published: number; governed: number; candidates: number; returned: number };
  bounds: {
    max_datastreams_scanned: number;
    max_matches: number;
    truncated: boolean;
    datastream_scan_truncated?: boolean;
  };
  observed_coverage_state: ObservedCoverage;
  empty_reason: { code: string; message: string } | null;
}

export interface MatchProfileSide {
  datastream_id: string;
  name: string;
  state: ObservedCoverage;
  total_rows: number | null;
  null_key_rows: number | null;
  distinct_keys: number | null;
  duplicate_state: ObservedCoverage;
  duplicated_keys: number | null;
  max_rows_per_key: number | null;
}

/**
 * What one profile cost the warehouse. `analyze-and-test.md`, *the person
 * inspecting sees what the inspection cost*.
 *
 * Every figure carries the state that produced it, and the three states are the
 * server's: `exact`, `not_applicable` (the engine does not produce this figure —
 * DuckDB bills no bytes) and `unavailable` (it does, and this job said nothing —
 * a cache hit, a job that never reached the engine).
 */
export interface MatchProfileCost {
  engine: string | null;
  warehouse_jobs_issued: number;
  billed_bytes: number | null;
  billed_bytes_state: "exact" | "not_applicable" | "unavailable";
  elapsed_ms: number | null;
  elapsed_ms_state: "exact" | "not_applicable" | "unavailable";
  /** Present only on a profile served from a receipt: it issued no job of its own. */
  reused_from_receipt?: boolean;
  warehouse_jobs_issued_now?: number;
}

export interface MatchProfile {
  cost?: MatchProfileCost;
  components: { canonical_field_id: string; canonical_name: string }[];
  left: MatchProfileSide;
  right: MatchProfileSide;
  matched: {
    state: ObservedCoverage;
    matched_keys: number | null;
    left_unmatched_keys: number | null;
    right_unmatched_keys: number | null;
  };
  multiplication: {
    state: ObservedCoverage;
    worst_case_rows_per_key: number | null;
    explanation: string;
  };
  execution_safety: ExecutionSafety;
  execution_blocked?: ExecutionBlock | null;
  profile_receipt?: string;
}

export interface CompiledPlan {
  query_spec_id: string;
  query_spec_version_id: string;
  content_hash: string;
  plan: Record<string, unknown>;
}

export interface AnalysisGoldenQuestion {
  id: string;
  title: string;
  current_version_id: string | null;
  current_version: {
    version_number: number;
    business_domain_id: string;
    business_domain_version_number: number;
    /**
     * SERVED, NEVER COMPOSED HERE. `list_golden_questions` joins the Business
     * Domain and hands its name over. The screen used to look the pin up in the
     * org's domain list and print `bd_<ULID>` when it was not there -- a picker
     * showing an identifier, which `visualization-and-rendering.md` forbids in
     * its `Incomplete if`. It is nullable because a pin whose domain was deleted
     * genuinely has no name, and the screen says so rather than inventing one.
     */
    business_domain_name: string | null;
    semantic_view_version_id: string;
  } | null;
}

export interface AnalysisBusinessDomain {
  id: string;
  name: string;
  version_number: number;
}

export interface AnalysisSkill {
  id: string;
  name?: string;
  version_number?: number;
}

export interface FrozenAnalysisContext {
  contract_version: "analysis-context.v1";
  semantic_view_version_id: string;
  business_domain: {
    id: string;
    version_number: number;
    version_id: string;
    name: string;
  } | null;
  golden_question: {
    id: string;
    version_id: string;
    version_number: number;
    title: string;
    content_hash: string;
  } | null;
  requested_skills: {
    procedure_id: string;
    version_number: number;
    version_id: string;
    name: string;
  }[];
}

export interface ExecutionReceipt {
  result_id: string;
  outcome: "success" | "empty" | "degraded" | "refused" | "unavailable";
  row_count: number;
}

export interface PivotCellValue {
  value: number | null;
  absent_reason?: string;
  recomputed_from?: string[];
}

export type PivotAxisValue = string | number | boolean | null;

export interface PivotComparisonValue {
  value: number | null;
  absent_reason?: string;
}

export interface PivotComparison {
  contract_version: "period-comparison.v1";
  kind: "previous_period" | "previous_year";
  canonical_field_id: string;
  period_field: "k_comparison_period";
  current: { start: string; end: string };
  baseline: { start: string; end: string };
  unavailable_reason?: string;
  deltas: {
    row_key: PivotAxisValue[];
    column_key: PivotAxisValue[];
    value_field: string;
    current: PivotComparisonValue;
    baseline: PivotComparisonValue;
    absolute_delta: PivotComparisonValue;
    relative_delta: PivotComparisonValue;
  }[];
}

export interface PivotMatrix {
  result_id: string;
  content_hash: string;
  row_fields: string[];
  column_fields: string[];
  value_fields: {
    name: string;
    canonical_field_id: string;
    role: string;
    aggregation?: string | null;
    datastream_id?: string | null;
    /** Frozen by the plan and echoed by the projection: money is micros, and a
     *  matrix that prints the integer states the amount 1 000 000× too big. */
    value_type?: string | null;
    unit?: string | null;
  }[];
  row_keys: PivotAxisValue[][];
  column_keys: PivotAxisValue[][];
  cells: {
    row_key: PivotAxisValue[];
    column_key: PivotAxisValue[];
    values: Record<string, PivotCellValue>;
    contributing_rows: number;
    contributing_row_indexes: number[];
  }[];
  row_subtotals?: { row_key: PivotAxisValue[]; values: Record<string, PivotCellValue> }[];
  column_subtotals?: { column_key: PivotAxisValue[]; values: Record<string, PivotCellValue> }[];
  // The policy is READ by the projection, not echoed: `rows` collapses the rows
  // and leaves one total per COLUMN key, `columns` does the mirror, and only
  // `both` produces the corner cell — a corner needs the two axes collapsed.
  grand_total?: {
    policy: string;
    by_column_key?: { column_key: PivotAxisValue[]; values: Record<string, PivotCellValue> }[];
    by_row_key?: { row_key: PivotAxisValue[]; values: Record<string, PivotCellValue> }[];
    overall?: Record<string, PivotCellValue>;
  };
  comparison?: PivotComparison;
  bounds: {
    max_cells: number;
    max_row_keys: number;
    max_column_keys: number;
    rows_truncated: boolean;
    columns_truncated: boolean;
    rows_dropped: number;
    columns_dropped: number;
    cells_truncated: boolean;
    /**
     * Story 66.6. WHICH ceiling cut this page: `axis_cap` (too many keys on one
     * axis), `cell_cap` (the matrix itself is too large) or `byte_budget` (the
     * page is full). `null` when nothing was cut.
     *
     * They do not repair the same way, and the banner used to offer ONE gesture
     * for all three: a byte-budget cut is paged (the cursor is already in the
     * response), a cell-cap cut needs a dimension removed, and only an axis cap
     * is repaired by a filter. Two callers out of three were sent to the wrong
     * gesture.
     */
    truncation_reason?: "axis_cap" | "cell_cap" | "byte_budget" | null;
    row_offset?: number;
    column_offset?: number;
    total_row_keys?: number;
    total_column_keys?: number;
    next_row_offset?: number | null;
    next_column_offset?: number | null;
    /**
     * Story 66.10 AC 9. A SIGNED continuation, bound to the Project, the
     * Result's id and content hash, and the shape of the request. The bare
     * offsets above are a position and assert nothing about which matrix they
     * are a position IN; replaying one under another filter used to serve a
     * page of a different matrix with no refusal. One token per axis, because
     * the two pagers move independently.
     */
    cursor?: string | null;
    next_row_cursor?: string | null;
    next_column_cursor?: string | null;
    /**
     * What the totals cover. Always the filtered set, never the page — a column
     * total on a page serving 2 row keys of 5 is the sum over all five. The two
     * booleans say when the page shows less than that, so the label can.
     */
    totals_span?: string;
    totals_cover_unserved_rows?: boolean;
    totals_cover_unserved_columns?: boolean;
    response_bytes?: number;
  };
}

export function fetchMatches(projectId: string): Promise<MatchCatalog> {
  return apiGet<MatchCatalog>(`/api/projects/${projectId}/analyze/matches`);
}

export function fetchAnalysisGoldenQuestions(
  projectId: string,
): Promise<{ golden_questions: AnalysisGoldenQuestion[]; truncated: boolean }> {
  return (async () => {
    const questions: AnalysisGoldenQuestion[] = [];
    let cursor: string | null = null;
    for (let page = 0; page < 5; page += 1) {
      const query = new URLSearchParams({ lifecycle: "active", limit: "100" });
      if (cursor) query.set("cursor", cursor);
      const answer = await apiGet<{
        golden_questions: AnalysisGoldenQuestion[];
        next_cursor?: string | null;
      }>(`/api/projects/${projectId}/test/golden-questions?${query.toString()}`);
      questions.push(...answer.golden_questions);
      cursor = answer.next_cursor ?? null;
      if (!cursor) return { golden_questions: questions, truncated: false };
    }
    return { golden_questions: questions, truncated: cursor !== null };
  })();
}

export function fetchAnalysisBusinessDomains(
  projectId: string,
): Promise<{ domains: AnalysisBusinessDomain[]; truncated: boolean }> {
  return apiGet<{ domains: AnalysisBusinessDomain[] }>(
    `/api/context/business-taxonomy?project_id=${encodeURIComponent(projectId)}&projection=analysis-picker`,
  ).then((answer) => ({
    domains: answer.domains,
    truncated: (answer as { truncated?: boolean }).truncated === true,
  }));
}

export function fetchAnalysisSkills(
  projectId: string,
): Promise<{ procedures: AnalysisSkill[]; truncated: boolean }> {
  return apiGet<{ procedures: AnalysisSkill[]; truncated?: boolean }>(
    `/api/context/procedures?project_id=${encodeURIComponent(projectId)}&projection=analysis-picker`,
  ).then((answer) => ({
    procedures: answer.procedures,
    truncated: answer.truncated === true,
  }));
}

export function fetchMatchProfile(
  projectId: string,
  left: string,
  right: string,
  commonKeyVersionId: string,
  relationshipName: string,
  viewVersionId: string,
): Promise<{ profile: MatchProfile }> {
  const query = new URLSearchParams({
    left,
    right,
    common_key_version_id: commonKeyVersionId,
    relationship_name: relationshipName,
    view_version_id: viewVersionId,
  });
  return apiGet<{ profile: MatchProfile }>(
    `/api/projects/${projectId}/analyze/matches/profile?${query.toString()}`,
  );
}

/** Compiling is an ANALYTICAL edit: it freezes what will be asked. */
export function compilePlan(
  projectId: string,
  request: Record<string, unknown>,
  signal?: AbortSignal,
): Promise<CompiledPlan> {
  return apiPost<CompiledPlan>(
    `/api/projects/${projectId}/analyze/multi-source/plans`,
    request,
    { signal },
  );
}

export function executePlan(
  projectId: string,
  querySpecVersionId: string,
  signal?: AbortSignal,
): Promise<{ result: ExecutionReceipt }> {
  return apiPost<{ result: ExecutionReceipt }>(
    `/api/projects/${projectId}/analyze/multi-source/plans/${querySpecVersionId}/execute`,
    {},
    { signal },
  );
}

/** Pivoting is a PRESENTATION edit: it rearranges an answer already given. */
export function pivotResult(
  projectId: string,
  resultId: string,
  request: Record<string, unknown>,
  signal?: AbortSignal,
): Promise<{ pivot: PivotMatrix }> {
  return apiPost<{ pivot: PivotMatrix }>(
    `/api/projects/${projectId}/analyze/results/${resultId}/pivot`,
    request,
    { signal },
  );
}
