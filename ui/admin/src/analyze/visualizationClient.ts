/**
 * The ONE client boundary between ui/admin and the Story 50.4 Visualization service.
 *
 * It extends the seam Story 50.1 established in `queryClient.ts`; it does not fork
 * it. Everything here is a typed call to a server route.
 *
 * WHAT THIS FILE DELIBERATELY DOES NOT CONTAIN: a family table, a role rule, a
 * cardinality limit, a volume limit or any other compatibility logic. The server
 * is the only compatibility authority, and a copied matrix is a second one that
 * drifts the first time a constraint moves. The registry arrives from
 * `/visualization-families` and is RENDERED, never re-derived.
 *
 * There is also no `table_fallback` projection type, because the server emits
 * none: `visualization-and-rendering.md:189` puts the fallback in the shared
 * runtime's responsibilities, so Story 50.5 owns the projection. What the server
 * sends is the OBLIGATION (`table_fallback: "required"`) and the wells its
 * columns derive from; this story's preview builds its table from the Result
 * payload it already fetches.
 */
import { apiGet, apiPost } from "../lib/apiFetch";
import type { FrozenAnalysisContext } from "./explorer/explorerClient";

/** A binding well, named by semantic role. Never by a renderer-library word. */
export interface VisualizationWell {
  name: string;
  label: string;
  accepts: string[];
  required: boolean;
  max_members: number;
  max_cardinality: number | null;
  /** False when every role this well accepts has no server source. */
  available: boolean;
  unavailable_reason: string | null;
  unavailable_owner: string | null;
}

export interface VisualizationFamily {
  id: string;
  label: string;
  description: string;
  wells: VisualizationWell[];
  requires_time_grain: boolean;
  requires_comparison: boolean;
  max_marks: number;
  capabilities: string[];
  /** The wells whose bindings the accessible fallback's columns derive from. */
  table_fallback_wells: string[];
  /** The literal `"required"`. There is no value that turns it off. */
  table_fallback: string;
}

export interface RoleAvailability {
  role: string;
  available: boolean;
  reason: string | null;
  owner: string | null;
}

export interface VisualizationRegistry {
  families: VisualizationFamily[];
  wells: { name: string; label: string; accepts: string[] }[];
  roles: RoleAvailability[];
  /** Families the target names that this release deliberately does not declare. */
  deferred_families: { id: string; reason: string }[];
  max_inline_rows: number;
  spec_contract_version: string;
  schema_version: number;
  responsive_profiles: string[];
  grammar_keys: string[];
}

/**
 * One of AC7's five rail facets, as the server sends it.
 *
 * `value` is what the server READ; `owner` is the surface that owns writing it.
 * A null value is therefore an explicit, attributable absence — never a blank
 * cell and never a plausible sentence. The rail renders both fields, so a reader
 * who finds a definition missing learns where to go and does not file a bug
 * against the Builder.
 */
export interface MemberFacet {
  value: string | null;
  owner: string;
}

/** A member the PINNED Query Spec version selected. The rail shows exactly these. */
export interface VisualizationMember {
  id: string;
  version_id: string;
  label: string;
  role: string;
  /**
   * Definition, grain, additivity, quality state and provenance hint (AC7),
   * SERVED. Keyed by facet name; the facet ORDER is the server's
   * `member_metadata_facets`, never a list retyped here — a sixth facet added on
   * the server must reach the rail without a client change.
   */
  metadata?: Record<string, MemberFacet>;
}

export interface VisualizationOptions {
  query_spec_id: string;
  query_spec_version_id: string;
  semantic_view_id: string;
  semantic_view_version_id: string;
  measures: VisualizationMember[];
  dimensions: VisualizationMember[];
  /** Always empty today, and the reason travels with it rather than being inferred. */
  time: VisualizationMember[];
  classifications: VisualizationMember[];
  roles: RoleAvailability[];
  /** The five facets AC7 requires per rail entry, in the server's order. */
  member_metadata_facets?: string[];
  unavailable_reason: string;
  unavailable_owner: string;
  grain: string | null;
  comparison: string;
  row_limit: number | null;
  /** Immutable business intent inherited from this exact Query Spec version. */
  analysis_context?: FrozenAnalysisContext | null;
}

/**
 * The Query Spec version an immutable Result was produced from.
 *
 * The Builder needs it to reach `POST /visualizations` at all: a Visualization
 * pins a Query Spec VERSION, and an address that carries only `?result_id=` has
 * to resolve one. Reading it from the Result — the object the person is looking
 * at — is the only source that cannot disagree with what they saw.
 */
export interface ResultPin {
  id: string;
  query_spec_version_id: string;
  outcome: string;
  content_hash: string;
}

/** One reason, attached to the exact control it is about. */
export interface VisualizationRefusal {
  code: string;
  message: string;
  subject: string | null;
  remedy: string | null;
}

export interface VisualizationRefusalEnvelope {
  code: string;
  message: string;
  refusals: VisualizationRefusal[];
}

export interface VisualizationSpecVersion {
  id: string;
  visualization_id: string;
  version_number: number;
  query_spec_id: string;
  query_spec_version_id: string;
  spec_contract_version: string;
  schema_version: number;
  family: string;
  spec: Record<string, unknown>;
  content_hash: string;
  predecessor_version_id: string | null;
  proposed_by: string;
  created_by: string;
  created_at: string | null;
}

export interface VisualizationHead {
  id: string;
  query_spec_id: string;
  name: string | null;
  current_version_id: string | null;
  created_by: string;
  created_at: string | null;
  updated_at: string | null;
  versions: {
    id: string;
    version_number: number;
    family: string;
    content_hash: string;
    query_spec_version_id: string;
    predecessor_version_id: string | null;
    proposed_by: string;
    created_by: string;
    created_at: string | null;
  }[];
}

/** The second tier: evaluated against ONE exact Result, never stored. */
export interface ResultDisclosures {
  compatible: boolean;
  outcome: string;
  returned_row_count: number;
  truncated: boolean;
  marks: number;
  cardinalities: Record<string, number>;
  family_max_marks: number;
  refusals: VisualizationRefusal[];
  table_fallback: string;
  table_fallback_columns: string[];
}

export interface ValidationReport {
  visualization_spec_version_id: string;
  query_spec_version_id: string;
  family: string;
  shape: { compatible: boolean; refusals: VisualizationRefusal[] };
  /** `null` means NOT EVALUATED. It never reads as "passed". */
  result_disclosures: ResultDisclosures | null;
  result_id: string | null;
}

const base = (projectId: string) => `/api/projects/${encodeURIComponent(projectId)}/analyze`;

/**
 * A shape guard, because a malformed envelope must produce an honest error state
 * rather than a screen rendered from `undefined`. It checks the fields the Builder
 * actually reads; it does not re-validate the grammar, which is the server's job.
 */
export class EnvelopeMismatch extends Error {
  constructor(what: string) {
    super(`The server sent a ${what} this Builder does not recognize.`);
    this.name = "EnvelopeMismatch";
  }
}

function assertRegistry(value: unknown): VisualizationRegistry {
  const v = value as VisualizationRegistry;
  if (!v || !Array.isArray(v.families) || !Array.isArray(v.roles)) {
    throw new EnvelopeMismatch("visual-family registry");
  }
  return v;
}

function assertOptions(value: unknown): VisualizationOptions {
  const v = value as VisualizationOptions;
  if (!v || !Array.isArray(v.measures) || !Array.isArray(v.dimensions)) {
    throw new EnvelopeMismatch("member list");
  }
  return v;
}

export function fetchResultPin(
  projectId: string,
  resultId: string,
  init?: RequestInit,
): Promise<ResultPin> {
  return apiGet<ResultPin>(`${base(projectId)}/results/${encodeURIComponent(resultId)}`, init);
}

export function fetchVisualizationFamilies(
  projectId: string,
  init?: RequestInit,
): Promise<VisualizationRegistry> {
  return apiGet<unknown>(`${base(projectId)}/visualization-families`, init).then(assertRegistry);
}

export function fetchVisualizationOptions(
  projectId: string,
  querySpecVersionId: string,
  init?: RequestInit,
): Promise<VisualizationOptions> {
  const query = new URLSearchParams({ query_spec_version_id: querySpecVersionId });
  return apiGet<unknown>(`${base(projectId)}/visualization-options?${query}`, init).then(
    assertOptions,
  );
}

export function createVisualization(
  projectId: string,
  body: {
    query_spec_version_id: string;
    spec: Record<string, unknown>;
    name?: string;
    /** Evidence, never a permission. Both values travel this same route. */
    proposed_by?: "person" | "model";
  },
  init?: RequestInit,
): Promise<VisualizationSpecVersion & { visualization_id: string }> {
  return apiPost(`${base(projectId)}/visualizations`, body, init);
}

export function addVisualizationVersion(
  projectId: string,
  visualizationId: string,
  body: {
    spec: Record<string, unknown>;
    query_spec_version_id?: string;
    proposed_by?: "person" | "model";
  },
): Promise<VisualizationSpecVersion & { visualization_id: string }> {
  return apiPost(
    `${base(projectId)}/visualizations/${encodeURIComponent(visualizationId)}/versions`,
    body,
  );
}

export function createCanonicalRender(
  projectId: string,
  body: Record<string, unknown>,
): Promise<{ id: string; result_id: string; content_hash: string; created_at: string | null }> {
  return apiPost(`${base(projectId)}/renders`, body);
}

export function fetchVisualization(
  projectId: string,
  visualizationId: string,
  init?: RequestInit,
): Promise<VisualizationHead> {
  return apiGet<VisualizationHead>(
    `${base(projectId)}/visualizations/${encodeURIComponent(visualizationId)}`,
    init,
  );
}

export function fetchVisualizationSpecVersion(
  projectId: string,
  versionId: string,
  init?: RequestInit,
): Promise<VisualizationSpecVersion> {
  return apiGet<VisualizationSpecVersion>(
    `${base(projectId)}/visualization-spec-versions/${encodeURIComponent(versionId)}`,
    init,
  );
}

export function validateVisualizationSpecVersion(
  projectId: string,
  versionId: string,
  resultId?: string | null,
): Promise<ValidationReport> {
  const query = resultId ? `?${new URLSearchParams({ result_id: resultId })}` : "";
  return apiPost<ValidationReport>(
    `${base(projectId)}/visualization-spec-versions/${encodeURIComponent(versionId)}/validate${query}`,
  );
}

/**
 * The ten states the preview may be in (AC9). `empty` and `unavailable` are
 * different states and always render differently: an empty Result means the query
 * ran and nothing matched; an unavailable one means we could not ask.
 */
export type PreviewState =
  | "loading"
  | "empty"
  | "partial"
  | "truncated"
  | "stale"
  | "incompatible"
  | "refused"
  | "unavailable"
  | "denied"
  | "unknown"
  | "ready";

/** Pull the refusal list out of an `ApiError` body, or return null if it is not one. */
export function refusalsOf(body: unknown): VisualizationRefusal[] | null {
  const envelope = body as VisualizationRefusalEnvelope | null;
  if (!envelope || !Array.isArray(envelope.refusals)) return null;
  return envelope.refusals;
}

/** Group refusals by the well they belong to, so each one lands on its own control. */
export function refusalsByWell(
  refusals: VisualizationRefusal[],
): Record<string, VisualizationRefusal[]> {
  const grouped: Record<string, VisualizationRefusal[]> = {};
  for (const refusal of refusals) {
    const subject = refusal.subject ?? "";
    const match = /^\/bindings\/([a-z_]+)/.exec(subject);
    const key = match ? match[1] : "__page__";
    (grouped[key] ??= []).push(refusal);
  }
  return grouped;
}
