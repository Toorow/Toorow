/**
 * The ONE client boundary between the console and the Chart Template service.
 *
 * Every call is a typed call to a route of `server/core/chart_templates_api.py`.
 * Nothing is assembled here: not a compatibility verdict, not a requirement
 * sentence, not a provenance label, not an empty-state reason. The ratified
 * criterion is explicit — "a refusal, an empty state or a compatibility sentence
 * renders a product identifier where a name is expected, or is composed in the
 * browser" — so every sentence below arrives already written.
 *
 * Everything goes through `apiGet`/`apiPost`, so the bearer header cannot be
 * forgotten and a refusal arrives as a typed `ApiError` carrying the server's
 * `{code, message, refusals}` envelope.
 */
import { apiGet, apiPost } from "../../lib/apiFetch";

const base = (projectId: string) => `/api/projects/${encodeURIComponent(projectId)}/analyze`;

/** One named, actionable reason the server refused, or one unsatisfied predicate. */
export interface TemplateRefusal {
  code: string;
  message: string;
  subject: string | null;
  remedy?: string | null;
}

/** The three states, never two. `unavailable` is not `incompatible`. */
export type VerdictState = "compatible" | "incompatible" | "unavailable";

export interface CompatibilityVerdict {
  state: VerdictState;
  template_version_id: string;
  result_id: string;
  family: string | null;
  answers_question: string | null;
  /** Every unsatisfied predicate at once, each with the gesture that repairs it. */
  unmet?: TemplateRefusal[];
  /** Present only on `unavailable`: what could not be read, and why. */
  unreadable?: TemplateRefusal | null;
}

export interface TemplateRequirement {
  well: string;
  well_label: string;
  accepts: string[];
  min: number | null;
  max_cardinality: number | null;
}

export interface ChartTemplateRow {
  id: string;
  label: string;
  description: string | null;
  answers_question: string | null;
  family: string | null;
  family_label: string | null;
  requires: TemplateRequirement[];
  /** Composed by the server. The row prints it; it never phrases one itself. */
  requires_sentence: string | null;
  readable: boolean;
  unreadable_reason: TemplateRefusal | null;
  content_hash: string | null;
  seed_origin: string;
  seed: { module_name: string | null; template_id: string | null } | null;
  /** "This Project", "Shipped with toorow", "Seeded by google-ads". */
  origin_label: string;
  current_version_id: string | null;
  version_count: number;
  archived: boolean;
  archived_at: string | null;
  created_by: string;
  created_at: string | null;
  updated_at: string | null;
  /** Present only when a Result was chosen. `null` means not evaluated. */
  verdict?: CompatibilityVerdict | null;
}

export interface AvailableSeed {
  module_name: string;
  seed_template_id: string;
  label: string;
  answers_question: string;
  family: string;
}

export interface ResultChoice {
  result_id: string;
  outcome: string;
  row_count: number;
  created_at: string | null;
  question: string | null;
}

export interface ChartTemplateCollection {
  templates: ChartTemplateRow[];
  available_seeds: AvailableSeed[];
  unusable_seeds: { module_name: string; seed_template_id: string; reason: string }[];
  connected_modules: string[];
  results: ResultChoice[];
  result_id: string | null;
  /** `null` means no Result was chosen — never zero, which would mean none fit. */
  compatible_count: number | null;
}

export interface ChartTemplateVersion {
  id: string;
  version_number: number;
  family: string;
  spec_contract_version: string;
  schema_version: number;
  document: Record<string, unknown>;
  content_hash: string;
  predecessor_version_id: string | null;
  proposed_by: string;
  created_by: string;
  created_at: string | null;
}

export interface ChartTemplateDetail extends ChartTemplateRow {
  versions: ChartTemplateVersion[];
  used_by: {
    reports: { report_id: string; label: string; report_version_id: string }[];
    visualizations: { visualization_id: string; visualization_spec_version_id: string }[];
  };
  results: ResultChoice[];
  verdict: CompatibilityVerdict | null;
}

/**
 * One well of one visual family, exactly as the shipped registry declares it.
 *
 * The editing screen reads its bounds from here and offers nothing outside them,
 * so a requirement the server would refuse cannot be composed by a control.
 */
export interface TemplateFamilyWell {
  name: string;
  label: string;
  accepts: string[];
  required: boolean;
  max_members: number;
  max_cardinality: number | null;
  available: boolean;
  /** Present only when the well cannot be required yet, and it says why. */
  unavailable_reason: string | null;
  /** The surface that owns making it available. Never a story number. */
  unavailable_owner: string | null;
}

/** One visual family a person can choose, with the wells it declares. */
export interface TemplateFamilyChoice {
  id: string;
  label: string;
  description: string;
  wells: TemplateFamilyWell[];
}

/** The wells, roles, families and profiles a template document may speak. */
export interface TemplateVocabulary {
  contract_version: string;
  schema_version: number;
  max_bytes: number;
  wells: { name: string; label: string }[];
  roles: string[];
  available_roles: string[];
  families: string[];
  /** The same families, each carrying its label and its own wells. */
  family_catalogue: TemplateFamilyChoice[];
  deferred_families: Record<string, unknown>[];
  responsive_profiles: string[];
  document_keys: string[];
  subtracted_from_visualization_spec: Record<string, string>;
  derived_from: string;
}

export interface MaterializedSpec {
  visualization_id: string;
  id: string;
  version_number: number;
  content_hash: string;
  created_at: string | null;
  family: string;
  query_spec_id: string;
  query_spec_version_id: string;
  proposed_by: string;
  spec: Record<string, unknown>;
  materialized_from_template_version_id: string | null;
  verdict: CompatibilityVerdict;
  result_id: string;
}

export function listChartTemplates(
  projectId: string,
  init?: RequestInit,
  options: { includeArchived?: boolean; resultId?: string | null } = {},
): Promise<ChartTemplateCollection> {
  const params = new URLSearchParams();
  if (options.includeArchived) params.set("include_archived", "true");
  if (options.resultId) params.set("result_id", options.resultId);
  const query = params.toString() ? `?${params.toString()}` : "";
  return apiGet<ChartTemplateCollection>(`${base(projectId)}/chart-templates${query}`, init);
}

export function fetchChartTemplate(
  projectId: string,
  templateId: string,
  init?: RequestInit,
  resultId?: string | null,
): Promise<ChartTemplateDetail> {
  const query = resultId ? `?result_id=${encodeURIComponent(resultId)}` : "";
  return apiGet<ChartTemplateDetail>(
    `${base(projectId)}/chart-templates/${encodeURIComponent(templateId)}${query}`,
    init,
  );
}

export function fetchTemplateVocabulary(
  projectId: string,
  init?: RequestInit,
): Promise<TemplateVocabulary> {
  return apiGet<TemplateVocabulary>(`${base(projectId)}/chart-template-vocabulary`, init);
}

export function fetchTemplateCompatibility(
  projectId: string,
  templateVersionId: string,
  resultId: string,
  init?: RequestInit,
): Promise<CompatibilityVerdict> {
  return apiGet<CompatibilityVerdict>(
    `${base(projectId)}/chart-template-versions/${encodeURIComponent(templateVersionId)}` +
      `/compatibility?result_id=${encodeURIComponent(resultId)}`,
    init,
  );
}

/**
 * Apply one template version to one Result. THE act of this surface.
 *
 * It goes to `template_materialization.materialize_template` and to nothing
 * else: the verdict is asked first and is binding, the Query Spec version the
 * Result already carries is the pin, and the Visualization Spec version that
 * comes back is an ordinary one.
 */
export function applyChartTemplate(
  projectId: string,
  templateVersionId: string,
  body: { result_id: string; visualization_id?: string | null; name?: string | null },
  init?: RequestInit,
): Promise<MaterializedSpec> {
  return apiPost<MaterializedSpec>(
    `${base(projectId)}/chart-template-versions/${encodeURIComponent(templateVersionId)}/apply`,
    body,
    init ?? {},
  );
}

/** What `POST /chart-templates/{id}/versions` answers. */
export interface AppendedTemplateVersion {
  template_id: string;
  version_id: string;
  version_number: number;
  content_hash: string;
  family: string;
  predecessor_version_id: string | null;
  seed_origin: string;
  /** True when this edit is the one that took the head away from its seed. */
  became_project_owned: boolean;
}

/**
 * Propose a SUCCESSOR version of one Chart Template. The act of the Presentation tab.
 *
 * No `Idempotency-Key`, and that is the domain's pattern rather than an
 * omission: `chart_templates_api.py` states it, and neither
 * `POST /visualizations` nor `POST /visualizations/{id}/versions` carries one.
 * Both are append-only writes of an immutable version whose identity is its
 * content, so a replay appends a version carrying the SAME `content_hash`
 * instead of corrupting anything. Only `POST /reports/{id}/runs` takes a key,
 * because a run EXECUTES a question.
 *
 * The version this succeeds is left exactly as it was — the server appends and
 * never rewrites — and a head that came from a seed becomes project-owned in the
 * same statement, which is why the screen says so BEFORE the gesture.
 */
export function appendChartTemplateVersion(
  projectId: string,
  templateId: string,
  document: Record<string, unknown>,
  init?: RequestInit,
): Promise<AppendedTemplateVersion> {
  return apiPost<AppendedTemplateVersion>(
    `${base(projectId)}/chart-templates/${encodeURIComponent(templateId)}/versions`,
    { document },
    init ?? {},
  );
}

/** What the archive and restore routes answer. Never a creation, always a state. */
export interface ChartTemplateArchiveState {
  template_id: string;
  archived: boolean;
  archived_at: string | null;
  /** True when the template was ALREADY in the state that was asked for. */
  unchanged: boolean;
}

/**
 * Retire one Chart Template of this Project. Nothing is deleted.
 *
 * The head leaves the live list and accepts no new version; every version stays
 * readable and every pin that already names one goes on resolving. That is the
 * product's archive pattern — a version and a date — and it is why this is a
 * state and not a destruction.
 *
 * Idempotent: archiving an archived template answers 200 and says `unchanged`,
 * so a double-submit is not an error about a state that is already what was
 * asked for.
 */
export function archiveChartTemplate(
  projectId: string,
  templateId: string,
  init?: RequestInit,
): Promise<ChartTemplateArchiveState> {
  return apiPost<ChartTemplateArchiveState>(
    `${base(projectId)}/chart-templates/${encodeURIComponent(templateId)}/archive`,
    {},
    init ?? {},
  );
}

/** The way back the archive confirmation promises. Same idempotence, other direction. */
export function restoreChartTemplate(
  projectId: string,
  templateId: string,
  init?: RequestInit,
): Promise<ChartTemplateArchiveState> {
  return apiPost<ChartTemplateArchiveState>(
    `${base(projectId)}/chart-templates/${encodeURIComponent(templateId)}/restore`,
    {},
    init ?? {},
  );
}

/** AC15 — draw one connector's declared templates into this Project. */
export function seedConnectorChartTemplates(
  projectId: string,
  moduleName: string,
  init?: RequestInit,
): Promise<{
  module_name: string;
  declared: number;
  created: { template_id: string; version_id: string; label: string }[];
  already_present: string[];
  unusable: { seed_template_id: string; reason: string }[];
}> {
  return apiPost(`${base(projectId)}/chart-templates/seeds`, { module_name: moduleName }, init ?? {});
}
