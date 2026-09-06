/**
 * The client boundary for the Story 50.2 reads, sitting beside the Story 50.1
 * one rather than replacing it.
 *
 * `queryClient.ts` owns creating, executing and retrieving — the write path and
 * its receipts. This file owns the composed READS the workbench renders: the
 * governed facet bundle, the six Result lenses, and the immutable Query Spec
 * version. Two files, one seam: both go through `apiFetch`, so the bearer header
 * cannot be forgotten and every failure arrives as a typed `ApiError`.
 *
 * Nothing here joins two responses. If a lens needs a Governance label, the
 * server put it there; a browser assembling it would be a second authority that
 * no test watches, and it would keep showing a definition after Governance
 * changed it.
 *
 * The envelope check below is deliberately narrow: it verifies the schema
 * version, the Project and the identity the caller ASKED FOR before anything is
 * rendered. A response for the previous Project arriving late is the failure it
 * exists to catch — the effect that fired it has already been cleaned up, but a
 * promise resolved after a Project switch would otherwise paint one Project's
 * numbers under another Project's name.
 */
import { apiGet } from "../lib/apiFetch";

export const FACETS_SCHEMA_VERSION = "analyze-query-facets.v1";
export const LENS_SCHEMA_VERSION = "analyze-result-lens.v1";

/** The six lenses, in workbench order. `view` is the declared default. */
export const RESULT_LENSES = [
  "view",
  "data",
  "definitions",
  "quality",
  "provenance",
  "ai-path",
] as const;
export type ResultLens = (typeof RESULT_LENSES)[number];
export const DEFAULT_RESULT_LENS: ResultLens = "view";

export const LENS_LABELS: Record<ResultLens, string> = {
  view: "View",
  data: "Data",
  definitions: "Definitions",
  quality: "Quality",
  provenance: "Provenance",
  "ai-path": "AI Path",
};

export function isResultLens(value: string | null | undefined): value is ResultLens {
  return !!value && (RESULT_LENSES as readonly string[]).includes(value);
}

/**
 * An exact, authorized owner the server resolved. Never built from a label.
 *
 * `section` may be `null`, and that is a contract, not a gap. Which console
 * section holds a given object type is owned by `shell/navigation.ts`; when the
 * server knows only the workspace/object-type/object-id triple — an AI Path step
 * records exactly that triple and nothing more — it says so, and `ownerTarget`
 * resolves the section through the one registry, refusing when the answer is not
 * unambiguous. A section guessed on the server was how every AI Path step that
 * was not a Context Topic became unreachable.
 */
export interface OwnerRef {
  workspace: string;
  section: string | null;
  object_type: string | null;
  object_id: string | null;
  tab: string | null;
  version_id: string | null;
}

export interface FacetMember {
  concept_id: string;
  version_id: string;
  label: string;
  definition?: string | null;
  value_type?: string | null;
  unit?: string | null;
  aggregation?: string | null;
  additivity_class?: string | null;
  currency_behavior?: unknown;
  time_behavior?: unknown;
  semantic_type?: string | null;
  allowed_grains?: string[];
  owner_ref: OwnerRef;
}

export interface ClassificationFacet {
  facet: string;
  dimension_id: string;
  dimension_version_id: string;
  members: {
    classification_object_id: string;
    slug: string;
    label: string;
    hierarchy_id: string | null;
    hierarchy_version_id: string | null;
  }[];
  reserved_members: string[];
}

export interface QueryFacets {
  schema_version: string;
  project_id: string;
  semantic_view_id: string;
  semantic_view_version_id: string;
  semantic_view_label: string | null;
  semantic_view_version_number: number | null;
  status: string;
  executable: boolean;
  measures: FacetMember[];
  dimensions: FacetMember[];
  pairs: {
    measure_id: string;
    dimension_id: string;
    queryable: boolean;
    reason?: string | null;
  }[];
  time: {
    members: { concept_id: string; version_id: string; label: string; allowed_grains: string[] }[];
    grains: string[];
    grains_unavailable_reason: string | null;
    comparisons: string[];
    /**
     * The conditions a period comparison cannot be compiled without, in the
     * order the validator tests them, each with the validator's own sentence.
     * Optional because a bundle can outlive the server that answers it; the
     * screen's decision to disable the control never depends on it (see
     * `QueryDoor`), only the words it shows do.
     */
    comparison_preconditions?: { condition: string; message: string }[];
    as_of_supported: boolean;
    reporting_boundary_supported: boolean;
    reporting_boundary_unsupported_reason?: string | null;
    timezone_supported: boolean;
    timezone_unsupported_reason?: string | null;
  };
  sort: { directions: string[] };
  filter_operators: string[];
  limits: {
    default_row_limit: number;
    max_row_limit: number;
    max_measures: number;
    max_dimensions: number;
    max_filters: number;
  };
  classification_facets: ClassificationFacet[];
  classification_facets_unavailable: { facet: string; dimension_id: string; reason: string }[];
  unavailable_reasons: { code: string; message: string }[];
  owner_ref: OwnerRef;
}

export interface ResultLensEnvelope {
  schema_version: string;
  result_id: string;
  project_id: string;
  lens: ResultLens;
  outcome: "success" | "empty" | "degraded" | "refused" | "unavailable";
  content_hash: string;
  query_spec_id: string;
  query_spec_version_id: string;
  /**
   * Served, never inferred: an id in a sentence is not a name.
   *
   * Not nullable, and that is a schema fact rather than an optimism:
   * `fk_query_spec_versions_semantic_scope` guarantees the view version row
   * exists, and `label` / `version_number` are NOT NULL. Typing these `| null`
   * bought a fallback string and a test for a state the database refuses.
   */
  query_spec_version_number: number;
  semantic_view_id: string;
  semantic_view_version_id: string;
  semantic_view_label: string;
  semantic_view_version_number: number;
  started_at: string | null;
  ended_at: string | null;
  lenses: string[];
  /** Exactly one of these is present, named by `lens` with `-` folded to `_`. */
  view?: ViewLens;
  data?: DataLens;
  definitions?: DefinitionsLens;
  quality?: QualityLens;
  provenance?: ProvenanceLens;
  ai_path?: AiPathLens;
  feedback_context?: unknown;
}

/**
 * A count the server withheld because the outcome measured nothing.
 *
 * `unavailable` and `refused` Results carry a stored `row_count` of 0 — a fact
 * about an empty payload, not about an answer. The server returns `null` for
 * them, so a tile has to say "Unavailable" instead of "0" (AC9).
 */
export type WithheldNumber = number | null;
export type WithheldBoolean = boolean | null;

export interface QualitySummary {
  outcome: string;
  freshness: { result_ended_at: string | null; requested_as_of: string | null; state: string };
  completeness: {
    row_count: WithheldNumber;
    cell_count: WithheldNumber;
    truncated: WithheldBoolean;
    row_limit: number | null;
    counts_unavailable_reason: string | null;
  };
  limitations: { code: string; message: string }[];
}

export interface ViewLens {
  outcome: string;
  query_context: Record<string, unknown>;
  quality_summary: QualitySummary;
  fields: string[];
  /** AI-289 — what each column MEANS, keyed by the column name the cells carry.
   *
   *  Read through the same `_concept_semantics` the Definitions lens uses, keyed
   *  by concept VERSION: what shows here is the meaning the measure had when the
   *  Result was executed, not today's. Absent for a column the Query Spec does
   *  not name — the lens does not manufacture meaning. */
  field_semantics?: Record<
    string,
    {
      label: string | null;
      definition: string | null;
      /** What the number IS, frozen by the plan (`money`, `integer`, `ratio`…).
       *  The read divides micros exactly once through `formatGovernedValue`;
       *  absent means the Result never said, and nothing is inferred. */
      value_type?: string | null;
      unit: string | null;
      aggregation: string | null;
      version_number: number | null;
      /** False when the pinned version could not be read. The current version is
       *  NOT shown in its place — that would be the historical rewrite AC8
       *  forbids. */
      resolved: boolean;
      owner_ref: OwnerRef | null;
    }
  >;
  values: {
    row_index: number;
    cells: {
      field: string;
      value: unknown;
      datum_key: string;
      /** `measure` | `dimension` | `unknown` — resolved through the manifest's
       *  member/column snapshot, never guessed from the field name. */
      kind: string;
    }[];
  }[];
  /** Present when no cell could be labelled, with the owner of the missing map. */
  kind_unavailable_reason: string | null;
  server_row_count: WithheldNumber;
  returned_row_count: WithheldNumber;
  truncated: WithheldBoolean;
  counts_unavailable_reason: string | null;
  read_sliced: boolean;
  read_slice_size: number;
  local_interaction_scope: string;
}

export interface DataLens {
  /** `type` is the STORAGE type of the column; `value_type`/`unit` are the
   *  governed ones the plan froze and the Result schema carries — the same two
   *  keys `pivot_projection` echoes into the MCP App's matrix. */
  schema: { name: string; type: string; value_type?: string | null; unit?: string | null }[];
  rows: Record<string, unknown>[];
  row_count: WithheldNumber;
  cell_count: WithheldNumber;
  byte_count: WithheldNumber;
  truncated: WithheldBoolean;
  counts_unavailable_reason: string | null;
  read_sliced: boolean;
  read_slice_size: number;
  integrity: {
    result_content_hash: string;
    query_spec_content_hash: string;
    attempt_id: string;
  };
  query_context: Record<string, unknown>;
  manifest: Record<string, unknown>;
  no_rows_explanation: string | null;
}

export interface DefinitionsLens {
  semantic_view: {
    id: string;
    version_id: string;
    label: string | null;
    version_number: number | null;
    status: string | null;
    resolved: boolean;
    owner_ref: OwnerRef;
  };
  members: {
    kind: string;
    concept_id: string;
    version_id: string;
    label: string | null;
    definition: string | null;
    expression: unknown;
    aggregation: string | null;
    additivity_class: string | null;
    unit: string | null;
    value_type: string | null;
    allowed_grains: string[];
    resolved: boolean;
    unresolved_reason: string | null;
    owner_ref: OwnerRef;
  }[];
  classification_pins: {
    member_id: string;
    classification_object_id: string;
    hierarchy_id: string | null;
    hierarchy_version_id: string | null;
    owner_ref: OwnerRef;
  }[];
  source_comparison: {
    requested: string | null;
    executed: boolean;
    meaning: string | null;
    period_field?: string | null;
    current_window?: { start: string | null; end: string | null };
    baseline_window?: { start: string | null; end: string | null };
  };
}

/** Whether the reconciliation gate was asked about one measure, and its answer.
 *
 * CHANTIER 67-15c. `analyze_workbench._metric_combination` asks
 * `rollup.combination_refusal` — the SAME authority the mart path asks — once per
 * measure of the Result, from the source systems the execution snapshotted. The
 * words for every value below live in `./metricCombination`, never here.
 *
 * `check` and `refused` are BOTH null when the Result recorded no source for the
 * measure: `unavailable_reason` then says why, and the screen must not read that
 * silence as `single_source`.
 */
export interface MetricCombination {
  member_id: string;
  member_name: string | null;
  label: string | null;
  /** A `CombinationCheck`, or null when the question could not be asked. */
  check: string | null;
  /** A `CombinationRefused` status, or null when nothing was refused. */
  refused: string | null;
  source_systems: string[];
  unavailable_reason: string | null;
}

export interface QualityLens extends QualitySummary {
  dq_evaluations: {
    evaluation_id: string;
    monitor_id: string;
    monitor_version_id: string;
    outcome: string;
    evaluated_at: string | null;
    counts: Record<string, number | null>;
    owner_ref: OwnerRef;
  }[];
  dq_unavailable_reason: string | null;
  /** `analyze-and-test.md:424-426`: the criterion is incomplete while the two
   *  Analyze surfaces can answer the same question differently and NEITHER
   *  discloses the other. The mart side has named this path since CAV-17; this
   *  is the mirror. It is a disclosure, not a convergence -- `reconciled` is
   *  false and stays false until the two paths actually meet. */
  analytical_paths?: {
    this_path: { path: string; governed_result: boolean; note: string };
    other_path: { path: string; relation: string; governed_result: boolean; note: string };
    reconciled: boolean;
  } | null;
  degraded_reason?: string | null;
  refused_reason?: string | null;
  unavailable_reason?: string | null;
  missing_link?: string | null;
  /** CHANTIER B: how far this breakdown is from the total its measure declares.
   *  A LIST means the comparison ran; an EMPTY list means there was nothing to
   *  compare; `null` means the comparison itself failed. The three read
   *  differently on the screen, because they are three different facts. */
  grain_reconciliation?: GrainReconciliation[] | null;
  /** `declared_total_authority` when a declaration arbitrated between several
   *  Datastreams that could all have answered; absent when only one could. */
  chosen_by?: string | null;
  /** CHANTIER C: the governed dimensions of this Result for which an inventory of
   *  entities-without-detail can be taken. Empty when none of them names an
   *  entity kind any event of this Project has ever carried. */
  entity_gap_candidates?: { member_id: string; member_name: string }[];
  /** CHANTIER 67-15c: one entry per measure of this Result. An EMPTY list means
   *  the Result declares no measure; the key absent means a server that predates
   *  the transport — neither is "every total was checked". */
  metric_combination?: MetricCombination[];
  semantic_view_version_id?: string | null;
}

export interface EntityDetailGaps {
  member_id: string;
  member_name: string;
  entity_kind: string;
  state: "exact" | "capped" | "unavailable";
  unavailable_reason: string | null;
  observed_count: number | null;
  detailed_count: number | null;
  missing_count: number | null;
  missing: string[];
  missing_truncated: boolean;
  observed_truncated: boolean;
  next_gesture: string | null;
}

export interface GrainReconciliation {
  measure_id: string;
  measure_name: string | null;
  verdict: "expected" | "reconciled" | "unexplained" | "undeclared" | "unavailable";
  total: number | null;
  breakdown_sum: number | null;
  gap: number | null;
  gap_ratio: number | null;
  sums_to: "equals" | "partial_by_design" | null;
  reason: string | null;
  tolerance_ratio: number | null;
  total_datastream_id: string | null;
  breakdown_datastream_id: string;
  statement: string;
}

export interface ProvenanceLens {
  chain: {
    link: string;
    status: "recorded" | "not_recorded";
    identity: string | null;
    owner_ref: OwnerRef | null;
    reason: string | null;
  }[];
  missing_link: string | null;
  predecessor: { result_id: string; owner_ref: OwnerRef } | null;
  successors: { result_id: string; outcome: string; ended_at: string | null; owner_ref: OwnerRef }[];
  source_system: string | null;
  value_source_tuples: {
    member_id: string | null;
    source_system: string | null;
    source_field: string | null;
    pull_id: string | null;
  }[];
  /** `null` once the execution snapshotted its tuples; the named gap otherwise. */
  value_source_unavailable: { code: string; message: string; owner: string } | null;
  /** What the execution manifest RECORDS about freshness, through the one
   *  projection the shared Render also uses (`project_freshness`). Replaces a
   *  bare `stale_since` that the server read from a manifest key nothing writes,
   *  so it was always null and the line never rendered (AI-296).
   *
   *  `stale_since_evaluated` is the field that matters and the reason this is an
   *  object: WITHOUT it, an absent staleness and an unchecked staleness are the
   *  same `null`, and the screen draws the reassuring one. */
  freshness: {
    state: string | null;
    as_of: string | null;
    complete_through: string | null;
    stale_since_evaluated: boolean;
  };
}

/** Opaque server projection. The shared AiPathCapability is its only UI decoder. */
export type AiPathLens = unknown;

export interface QuerySpecVersionDetail {
  schema_version: string;
  query_spec_id: string;
  query_spec_version_id: string;
  version_number: number;
  semantic_view_id: string;
  semantic_view_version_id: string;
  spec: Record<string, unknown>;
  content_hash: string;
  predecessor_version_id: string | null;
  created_at: string | null;
  created_by: string | null;
  name: string | null;
  is_current_version: boolean;
  results: {
    result_id: string;
    outcome: string;
    row_count: number | null;
    truncated: boolean;
    started_at: string | null;
    ended_at: string | null;
    content_hash: string;
    owner_ref: OwnerRef;
  }[];
  semantic_view_owner_ref: OwnerRef;
}

/** Thrown when a response does not describe what the caller asked for. */
export class EnvelopeMismatch extends Error {}

function assertEnvelope(
  body: { schema_version?: string; project_id?: string },
  expected: { schema_version: string; project_id: string },
): void {
  if (body?.schema_version !== expected.schema_version) {
    throw new EnvelopeMismatch(
      `expected ${expected.schema_version}, received ${String(body?.schema_version)}`,
    );
  }
  if (body?.project_id !== expected.project_id) {
    // Refuse rather than render. A Project's figures shown under another
    // Project's name is the one mistake this surface must never make.
    throw new EnvelopeMismatch("this response belongs to another Project");
  }
}

const base = (projectId: string) => `/api/projects/${encodeURIComponent(projectId)}/analyze`;

export async function fetchQueryFacets(
  projectId: string,
  semanticViewId: string,
  semanticViewVersionId: string,
  init?: RequestInit,
): Promise<QueryFacets> {
  const query = new URLSearchParams({
    semantic_view_id: semanticViewId,
    semantic_view_version_id: semanticViewVersionId,
  });
  const body = await apiGet<QueryFacets>(`${base(projectId)}/query-facets?${query}`, init);
  assertEnvelope(body, { schema_version: FACETS_SCHEMA_VERSION, project_id: projectId });
  if (
    body.semantic_view_id !== semanticViewId
    || body.semantic_view_version_id !== semanticViewVersionId
  ) {
    throw new EnvelopeMismatch("this response pins another Semantic View version");
  }
  return body;
}

export async function fetchResultLens(
  projectId: string,
  resultId: string,
  lens: ResultLens,
  init?: RequestInit,
): Promise<ResultLensEnvelope> {
  const body = await apiGet<ResultLensEnvelope>(
    `${base(projectId)}/results/${encodeURIComponent(resultId)}/lens/${encodeURIComponent(lens)}`,
    init,
  );
  assertEnvelope(body, { schema_version: LENS_SCHEMA_VERSION, project_id: projectId });
  if (body.result_id !== resultId || body.lens !== lens) {
    throw new EnvelopeMismatch("this response describes another Result or lens");
  }
  return body;
}

export async function fetchQuerySpecVersion(
  projectId: string,
  querySpecVersionId: string,
  init?: RequestInit,
): Promise<QuerySpecVersionDetail> {
  const body = await apiGet<QuerySpecVersionDetail>(
    `${base(projectId)}/query-spec-versions/${encodeURIComponent(querySpecVersionId)}`,
    init,
  );
  if (body?.schema_version !== LENS_SCHEMA_VERSION) {
    throw new EnvelopeMismatch(
      `expected ${LENS_SCHEMA_VERSION}, received ${String(body?.schema_version)}`,
    );
  }
  if (body.query_spec_version_id !== querySpecVersionId) {
    throw new EnvelopeMismatch("this response describes another Query Spec version");
  }
  return body;
}
