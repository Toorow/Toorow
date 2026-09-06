/**
 * The ONE client boundary between ui/admin and the Story 51.1 Golden Question
 * capability.
 *
 * Everything here is a typed call to a route that exists in
 * `server/core/golden_questions_api.py`. Nothing is invented on this side: the
 * governed choices (Business Domain versions, pinnable Semantic View versions,
 * result types, severities, assertion types, tolerance kinds, provenance link
 * kinds, the AI Path vocabulary) all arrive from `/golden-questions/options`,
 * which the server composes from the same rows its validator reads. A catalogue
 * kept in the browser would keep offering a domain version the day it was
 * archived — and would let the console offer something the server refuses.
 *
 * Every call goes through `apiGet` / `apiPost`, so the bearer header cannot be
 * forgotten and a failure arrives as a typed `ApiError` carrying the server's
 * `{code, message, refusals}` envelope, never as a silent fallback to invented
 * data.
 *
 * `Render` DOES NOT EXIST (Stories 50.4 / 50.5 / 50.7). `expected_render_ref` is
 * read here and never written: the server refuses a non-null pin, and a missing
 * pin is reported as `Unverifiable`. This file mints no render identifier.
 */
import { apiGet, apiPost } from "../lib/apiFetch";

// ---------------------------------------------------------------------------
// What the server returns.
// ---------------------------------------------------------------------------

/** The version summary the collection carries for each head. */
export interface GoldenQuestionCurrentVersionSummary {
  version_number: number;
  business_domain_id: string;
  business_domain_version_number: number;
  semantic_view_id: string;
  semantic_view_version_id: string;
  result_type: string;
  severity: string;
  capability_tags: string[];
}

export interface GoldenQuestionSummary {
  id: string;
  title: string;
  owner: string;
  lifecycle: string;
  current_version_id: string | null;
  created_by: string | null;
  created_at: string | null;
  updated_at: string | null;
  current_version: GoldenQuestionCurrentVersionSummary | null;
}

export interface Tolerance {
  kind: string;
  value?: unknown;
}

/** One typed assertion. `tolerance: null` means exact; an ABSENT key is refused. */
export interface Assertion {
  assertion_type: string;
  tolerance: Tolerance | null;
  description?: string;
}

export const V2_ASSERTION_TYPES = [
  "value",
  "row_set",
  "ordering",
  "cardinality",
  "invariant",
  "empty",
  "degraded",
  "refused",
] as const;
export type V2AssertionType = (typeof V2_ASSERTION_TYPES)[number];
export type SelectorOperator = "eq" | "ne" | "in" | "exists";

export type AssertionSelector =
  | { field: string; operator: "exists" }
  | { field: string; operator: Exclude<SelectorOperator, "exists">; value: unknown };

export type NumericTolerance = {
  kind: "numeric";
  mode: "absolute" | "relative";
  amount: number;
};
export type TemporalTolerance = { kind: "temporal"; seconds: number };
export type SetTolerance = { kind: "set"; max_missing: number; max_extra: number };

type V2AssertionCommon = { selectors: AssertionSelector[] };
export type V2Assertion =
  | (V2AssertionCommon & {
      assertion_type: "value";
      field: string;
      expected: unknown;
      operator: "equals";
      tolerance: null | NumericTolerance | TemporalTolerance;
    })
  | (V2AssertionCommon & {
      assertion_type: "row_set";
      fields: string[];
      expected_rows: Record<string, unknown>[];
      operator: "equals";
      tolerance: null | SetTolerance;
    })
  | (V2AssertionCommon & {
      assertion_type: "ordering";
      fields: string[];
      operator: "ascending" | "descending";
      tolerance: null;
    })
  | (V2AssertionCommon & {
      assertion_type: "cardinality";
      expected: number;
      operator: "equals";
      tolerance: null | NumericTolerance;
    })
  | (V2AssertionCommon & {
      assertion_type: "invariant";
      fields: string[];
      operator: "unique" | "non_null";
      tolerance: null;
    })
  | (V2AssertionCommon & {
      assertion_type: "empty" | "degraded" | "refused";
      operator: "is";
      tolerance: null;
    });

export interface SelectorDraft {
  field: string;
  operator: SelectorOperator;
  value_json: string;
}

/** One flat editor record. `v2AssertionPayload` is the only function allowed to
 * project it into a member of the server's closed v2 union. */
export interface V2AssertionDraft {
  assertion_type: V2AssertionType;
  selectors: SelectorDraft[];
  field: string;
  fields: string;
  expected_json: string;
  expected_rows_json: string;
  operator: string;
  tolerance_kind: "exact" | "numeric" | "temporal" | "set";
  tolerance_mode: "absolute" | "relative";
  tolerance_amount: string;
  tolerance_seconds: string;
  tolerance_max_missing: string;
  tolerance_max_extra: string;
}

export function emptyV2AssertionDraft(assertionType: V2AssertionType = "value"): V2AssertionDraft {
  return {
    assertion_type: assertionType,
    selectors: [],
    field: "",
    fields: "",
    expected_json: "",
    expected_rows_json: "[]",
    operator:
      assertionType === "ordering"
        ? "ascending"
        : assertionType === "invariant"
          ? "unique"
          : assertionType === "empty" || assertionType === "degraded" || assertionType === "refused"
            ? "is"
            : "equals",
    tolerance_kind: "exact",
    tolerance_mode: "absolute",
    tolerance_amount: "0",
    tolerance_seconds: "0",
    tolerance_max_missing: "0",
    tolerance_max_extra: "0",
  };
}

function jsonValue(value: string, subject: string): unknown {
  try {
    return JSON.parse(value);
  } catch {
    throw new Error(`${subject} must be valid JSON.`);
  }
}

function numberValue(value: string, subject: string): number {
  if (!value.trim()) throw new Error(`${subject} must be declared.`);
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) throw new Error(`${subject} must be a finite number.`);
  return parsed;
}

function nonNegativeNumberValue(value: string, subject: string): number {
  const parsed = numberValue(value, subject);
  if (parsed < 0) throw new Error(`${subject} must be non-negative.`);
  return parsed;
}

function nonNegativeIntegerValue(value: string, subject: string): number {
  const parsed = nonNegativeNumberValue(value, subject);
  if (!Number.isInteger(parsed)) throw new Error(`${subject} must be an integer.`);
  return parsed;
}

function selectorPayload(selector: SelectorDraft, index: number): AssertionSelector {
  const field = selector.field.trim();
  if (!field) throw new Error(`Selector ${index + 1} field must be declared.`);
  if (selector.operator === "exists") return { field, operator: "exists" };
  return {
    field,
    operator: selector.operator,
    value: jsonValue(selector.value_json, `Selector ${index + 1} value`),
  };
}

function fieldsPayload(value: string): string[] {
  const fields = value.split(",").map((field) => field.trim()).filter(Boolean);
  if (fields.length === 0) throw new Error("At least one referenced field must be declared.");
  if (fields.length > 16) throw new Error("An assertion may reference at most 16 fields.");
  return fields;
}

function numericTolerancePayload(draft: V2AssertionDraft): NumericTolerance {
  return {
    kind: "numeric",
    mode: draft.tolerance_mode,
    amount: nonNegativeNumberValue(draft.tolerance_amount, "Numeric tolerance amount"),
  };
}

function assertReferencedFieldBudget(fields: string[], selectors: AssertionSelector[]): void {
  const referenced = new Set([...fields, ...selectors.map((selector) => selector.field)]);
  if (referenced.size > 16) throw new Error("An assertion may reference at most 16 fields.");
}

export function v2AssertionPayload(draft: V2AssertionDraft): V2Assertion {
  if (draft.selectors.length > 8) throw new Error("An assertion may contain at most 8 selectors.");
  const selectors = draft.selectors.map(selectorPayload);
  switch (draft.assertion_type) {
    case "value": {
      const field = draft.field.trim();
      if (!field) throw new Error("Value assertion field must be declared.");
      assertReferencedFieldBudget([field], selectors);
      const tolerance = draft.tolerance_kind === "exact"
        ? null
        : draft.tolerance_kind === "numeric"
          ? numericTolerancePayload(draft)
          : draft.tolerance_kind === "temporal"
            ? {
                kind: "temporal" as const,
                seconds: nonNegativeNumberValue(draft.tolerance_seconds, "Temporal tolerance seconds"),
              }
            : (() => { throw new Error("Value assertions accept only exact, numeric or temporal tolerance."); })();
      return {
        assertion_type: "value",
        selectors,
        field,
        expected: jsonValue(draft.expected_json, "Expected value"),
        operator: "equals",
        tolerance,
      };
    }
    case "row_set": {
      const fields = fieldsPayload(draft.fields);
      assertReferencedFieldBudget(fields, selectors);
      const expectedRows = jsonValue(draft.expected_rows_json, "Expected rows");
      if (!Array.isArray(expectedRows) || expectedRows.length > 100 || expectedRows.some((row) => !row || typeof row !== "object" || Array.isArray(row))) {
        throw new Error("Expected rows must be an array of at most 100 objects.");
      }
      const tolerance = draft.tolerance_kind === "exact"
        ? null
        : draft.tolerance_kind === "set"
          ? {
              kind: "set" as const,
              max_missing: nonNegativeIntegerValue(draft.tolerance_max_missing, "Maximum missing rows"),
              max_extra: nonNegativeIntegerValue(draft.tolerance_max_extra, "Maximum extra rows"),
            }
          : (() => { throw new Error("Row-set assertions accept only exact or set tolerance."); })();
      return {
        assertion_type: "row_set",
        selectors,
        fields,
        expected_rows: expectedRows as Record<string, unknown>[],
        operator: "equals",
        tolerance,
      };
    }
    case "ordering": {
      const fields = fieldsPayload(draft.fields);
      assertReferencedFieldBudget(fields, selectors);
      if (draft.operator !== "ascending" && draft.operator !== "descending") {
        throw new Error("Ordering operator must be ascending or descending.");
      }
      return {
        assertion_type: "ordering",
        selectors,
        fields,
        operator: draft.operator,
        tolerance: null,
      };
    }
    case "cardinality":
      return {
        assertion_type: "cardinality",
        selectors,
        expected: nonNegativeIntegerValue(draft.expected_json, "Expected cardinality"),
        operator: "equals",
        tolerance: draft.tolerance_kind === "numeric" ? numericTolerancePayload(draft) : null,
      };
    case "invariant": {
      const fields = fieldsPayload(draft.fields);
      assertReferencedFieldBudget(fields, selectors);
      if (draft.operator !== "unique" && draft.operator !== "non_null") {
        throw new Error("Invariant operator must be unique or non_null.");
      }
      return {
        assertion_type: "invariant",
        selectors,
        fields,
        operator: draft.operator,
        tolerance: null,
      };
    }
    case "empty":
    case "degraded":
    case "refused":
      return { assertion_type: draft.assertion_type, selectors, operator: "is", tolerance: null };
  }
}

export interface ProvenanceRequirement {
  link_kind: string;
  required: boolean;
  expected_ref?: string;
}

/** One expected-path node, in the exact vocabulary of `app.ai_path_steps`. */
export interface PathNode {
  /** `workspace/object_type/object_id` -- the same triple `app.ai_path_steps`
   *  records, and the only address by which an expected node is matched. */
  key: string;
  step_kind?: string;
  owner_version_id?: string;
  skill_pin?: { skill_version_id: string; skill_step_id: string };
  tool?: { tool_name: string; tool_catalog_version?: string };
}

export interface OrderConstraint {
  /** A node KEY, not an index. `expected_ai_path.py` matches observed steps by
   *  `workspace/type/id`, so a constraint that named a position could never be
   *  checked against what was observed. */
  before: string;
  after: string;
  reason?: string;
}

export interface ForbiddenNode {
  key?: string;
  owner_workspace?: string;
  owner_object_type?: string;
  tool_name?: string;
  reason?: string;
}

export interface AlternativeBranch {
  name: string;
  required_nodes: PathNode[];
}

export interface AlternativeGroup {
  name: string;
  branches: AlternativeBranch[];
}

export interface ExtraStepRule {
  kind: "cost" | "retry" | "safety" | "capability";
  limit: number;
  applies_to?: string;
  reason?: string;
}

/** The versioned grammar of `server/core/schemas/expected-ai-path.schema.json` --
 *  the ONE vocabulary the write path stores and the comparator reads. A question
 *  that declares no expected path stores `{}`, never an object of empty lists:
 *  a pattern asserting nothing must read as unverifiable, never as a pass. */
export interface ExpectedAiPath {
  grammar_version?: number;
  required_nodes?: PathNode[];
  forbidden_nodes?: ForbiddenNode[];
  order_constraints?: OrderConstraint[];
  alternatives?: AlternativeGroup[];
  extra_step_rules?: ExtraStepRule[];
}

export interface ReferencePath {
  ordinal: number;
  query_spec_version_id: string;
  role: string;
}

export interface GoldenQuestionVersion {
  id: string;
  golden_question_id: string;
  version_number: number;
  business_domain_id: string;
  business_domain_version_number: number;
  business_classification_id: string | null;
  semantic_view_id: string;
  semantic_view_version_id: string;
  semantic_view_version_role: string;
  question: string;
  time_boundary: Record<string, unknown>;
  contract_version: "golden-question.v1" | "golden-question.v2";
  expected_result: Assertion[] | V2Assertion[];
  required_provenance: ProvenanceRequirement[];
  expected_ai_path: ExpectedAiPath;
  result_type: string;
  capability_tags: string[];
  severity: string;
  /** Declared and always null until Story 50.4 delivers the Render object. */
  expected_render_ref: string | null;
  content_hash: string;
  predecessor_version_id: string | null;
  created_by: string | null;
  created_at: string | null;
  reference_paths: ReferencePath[];
}

export interface GoldenQuestionDetail {
  id: string;
  title: string;
  owner: string;
  lifecycle: string;
  current_version_id: string | null;
  created_by: string | null;
  created_at: string | null;
  updated_at: string | null;
  lifecycle_transitions: string[];
  current_version: GoldenQuestionVersion | null;
  versions: GoldenQuestionVersion[];
}

export interface BusinessDomainOption {
  id: string;
  name: string;
  status: string;
  latest_version_number: number;
}

export interface BusinessClassificationOption {
  id: string;
  business_domain_id: string;
  name: string;
}

export interface SemanticViewVersionOption {
  semantic_view_version_id: string;
  semantic_view_id: string;
  name: string;
  version_number: number;
  status: string;
}

export interface GoldenQuestionOptions {
  business_domains: BusinessDomainOption[];
  business_classifications: BusinessClassificationOption[];
  semantic_view_versions: SemanticViewVersionOption[];
  semantic_view_version_roles: string[];
  result_types: string[];
  severities: string[];
  lifecycles: string[];
  lifecycle_transitions: Record<string, string[]>;
  assertion_types: string[];
  tolerance_kinds: string[];
  provenance_link_kinds: string[];
  path_step_kinds: string[];
  path_owner_workspaces: string[];
  reference_path_roles: string[];
  capability_rule: string;
}

/** A dimension that cannot be judged, with the reason and the owner story. */
export interface CoverageDimension {
  dimension: string;
  verdict: string;
  reason_code: string;
  owner_story: string;
  message: string;
}

export interface CoveragePins {
  current_version_id: string | null;
  business_domain: { id: string; version_number: number; classification_id: string | null } | null;
  semantic_view: { id: string; version_id: string; role: string } | null;
  result_type: string | null;
  severity: string | null;
  capability_tags: string[];
  reference_path_count: number;
  expected_assertion_count: number;
  required_provenance_count: number;
  expected_required_node_count: number;
}

export interface GoldenQuestionCoverage {
  golden_question_id: string;
  pinned: CoveragePins;
  semantic_view_version: { status: string | null } | null;
  dimensions: CoverageDimension[];
}

export interface CreatedVersionReceipt {
  golden_question_id: string;
  version_id: string;
  version_number: number;
  predecessor_version_id?: string | null;
  content_hash: string;
  created_at: string | null;
}

export interface LifecycleReceipt {
  golden_question_id: string;
  lifecycle: string;
  owner: string;
}

// ---------------------------------------------------------------------------
// The routes.
// ---------------------------------------------------------------------------

const base = (projectId: string) => `/api/projects/${encodeURIComponent(projectId)}/test`;

export function listGoldenQuestions(
  projectId: string,
  options: { q?: string; lifecycle?: string; limit?: number; cursor?: string } = {},
  init?: RequestInit,
): Promise<{ golden_questions: GoldenQuestionSummary[]; total: number; bound: number; next_cursor: string | null }> {
  const query = new URLSearchParams();
  if (options.q) query.set("q", options.q);
  if (options.lifecycle) query.set("lifecycle", options.lifecycle);
  if (options.limit) query.set("limit", String(options.limit));
  if (options.cursor) query.set("cursor", options.cursor);
  return apiGet<{ golden_questions: GoldenQuestionSummary[]; total: number; bound: number; next_cursor: string | null }>(
    `${base(projectId)}/golden-questions${query.size ? `?${query}` : ""}`,
    init,
  );
}

export function fetchGoldenQuestionOptions(
  projectId: string,
  init?: RequestInit,
): Promise<GoldenQuestionOptions> {
  return apiGet<GoldenQuestionOptions>(`${base(projectId)}/golden-questions/options`, init);
}

export function fetchGoldenQuestion(
  projectId: string,
  goldenQuestionId: string,
  init?: RequestInit,
): Promise<GoldenQuestionDetail> {
  return apiGet<GoldenQuestionDetail>(
    `${base(projectId)}/golden-questions/${encodeURIComponent(goldenQuestionId)}`,
    init,
  );
}

export function fetchGoldenQuestionCoverage(
  projectId: string,
  goldenQuestionId: string,
  init?: RequestInit,
): Promise<GoldenQuestionCoverage> {
  return apiGet<GoldenQuestionCoverage>(
    `${base(projectId)}/golden-questions/${encodeURIComponent(goldenQuestionId)}/coverage`,
    init,
  );
}

export function createGoldenQuestion(
  projectId: string,
  body: { title: string; owner: string; definition: Record<string, unknown> },
): Promise<CreatedVersionReceipt> {
  return apiPost<CreatedVersionReceipt>(`${base(projectId)}/golden-questions`, body);
}

export function createGoldenQuestionVersion(
  projectId: string,
  goldenQuestionId: string,
  definition: Record<string, unknown>,
): Promise<CreatedVersionReceipt> {
  return apiPost<CreatedVersionReceipt>(
    `${base(projectId)}/golden-questions/${encodeURIComponent(goldenQuestionId)}/versions`,
    { definition },
  );
}

export function setGoldenQuestionLifecycle(
  projectId: string,
  goldenQuestionId: string,
  lifecycle: string,
): Promise<LifecycleReceipt> {
  return apiPost<LifecycleReceipt>(
    `${base(projectId)}/golden-questions/${encodeURIComponent(goldenQuestionId)}/lifecycle`,
    { lifecycle },
  );
}

// ---------------------------------------------------------------------------
// The editable draft.
//
// The draft is a FLAT, all-strings mirror of the document the server accepts, so
// a form field maps to one draft field and the translation happens once, here.
// `definitionPayload` is the only place a document is assembled, which is what
// keeps the console from growing a second, divergent idea of the contract.
// ---------------------------------------------------------------------------

export interface AssertionDraft {
  assertion_type: string;
  description: string;
  /** "" means the author has not decided — the key is then OMITTED and the
   *  server refuses with `missing_tolerance` naming the exact index. "exact"
   *  sends a declared `null`. The difference between "we accept no drift" and
   *  "we never thought about drift" is the reason this field exists, so the
   *  console must not quietly choose one. */
  tolerance_kind: string;
  tolerance_value: string;
}

export type ProvenanceState = "absent" | "required" | "optional";

export interface ProvenanceDraft {
  link_kind: string;
  state: ProvenanceState;
  expected_ref: string;
}

export interface PathNodeDraft {
  step_kind: string;
  owner_workspace: string;
  owner_object_type: string;
  owner_object_id: string;
  owner_version_id: string;
  skill_version_id: string;
  skill_step_id: string;
  tool_name: string;
}

export interface OrderConstraintDraft {
  before: string;
  after: string;
}

export interface ReferencePathDraft {
  query_spec_version_id: string;
  role: string;
}

export type TimeBoundaryKind = "none" | "as_of" | "range";

export interface DefinitionDraft {
  contract_version: "golden-question.v1" | "golden-question.v2";
  business_domain_id: string;
  business_domain_version_number: string;
  business_classification_id: string;
  /** Written together, always: half a pin pins nothing. */
  semantic_view_id: string;
  semantic_view_version_id: string;
  semantic_view_version_role: string;
  question: string;
  time_boundary_kind: TimeBoundaryKind;
  as_of: string;
  from: string;
  to: string;
  timezone: string;
  result_type: string;
  severity: string;
  capability_tags: string;
  assertions: AssertionDraft[];
  v2_assertions: V2AssertionDraft[];
  provenance: ProvenanceDraft[];
  required_nodes: PathNodeDraft[];
  forbidden_tools: string[];
  order_constraints: OrderConstraintDraft[];
  /** Carried through unchanged. This screen displays the constructs it does not
   *  offer to edit, so a stored one is never dropped by an edit that never
   *  intended to touch it. */
  alternatives: AlternativeGroup[];
  /** Forbidden nodes named by anything other than a bare tool -- the panel above
   *  edits tool names only, and these travel back untouched. */
  carried_forbidden_nodes: ForbiddenNode[];
  extra_step_rules: ExtraStepRule[];
  reference_paths: ReferencePathDraft[];
}

export function emptyPathNodeDraft(): PathNodeDraft {
  return {
    step_kind: "",
    owner_workspace: "",
    owner_object_type: "",
    owner_object_id: "",
    owner_version_id: "",
    skill_version_id: "",
    skill_step_id: "",
    tool_name: "",
  };
}

export function emptyDefinitionDraft(linkKinds: string[]): DefinitionDraft {
  return {
    contract_version: "golden-question.v1",
    business_domain_id: "",
    business_domain_version_number: "",
    business_classification_id: "",
    semantic_view_id: "",
    semantic_view_version_id: "",
    semantic_view_version_role: "",
    question: "",
    time_boundary_kind: "none",
    as_of: "",
    from: "",
    to: "",
    timezone: "",
    result_type: "",
    severity: "",
    capability_tags: "",
    assertions: [{ assertion_type: "", description: "", tolerance_kind: "", tolerance_value: "" }],
    v2_assertions: [],
    provenance: linkKinds.map((link_kind) => ({
      link_kind,
      state: "absent" as ProvenanceState,
      expected_ref: "",
    })),
    required_nodes: [],
    forbidden_tools: [],
    order_constraints: [],
    alternatives: [],
    carried_forbidden_nodes: [],
    extra_step_rules: [],
    reference_paths: [],
  };
}

function toleranceDraft(tolerance: Tolerance | null | undefined): {
  tolerance_kind: string;
  tolerance_value: string;
} {
  if (tolerance === null) return { tolerance_kind: "exact", tolerance_value: "" };
  if (!tolerance || typeof tolerance !== "object") return { tolerance_kind: "", tolerance_value: "" };
  const value = tolerance.value;
  return {
    tolerance_kind: String(tolerance.kind ?? ""),
    tolerance_value: value === undefined || value === null ? "" : String(value),
  };
}

function v2ToleranceDraft(tolerance: V2Assertion["tolerance"]): Pick<
  V2AssertionDraft,
  | "tolerance_kind"
  | "tolerance_mode"
  | "tolerance_amount"
  | "tolerance_seconds"
  | "tolerance_max_missing"
  | "tolerance_max_extra"
> {
  const base = emptyV2AssertionDraft();
  if (tolerance === null) {
    return {
      tolerance_kind: "exact",
      tolerance_mode: base.tolerance_mode,
      tolerance_amount: base.tolerance_amount,
      tolerance_seconds: base.tolerance_seconds,
      tolerance_max_missing: base.tolerance_max_missing,
      tolerance_max_extra: base.tolerance_max_extra,
    };
  }
  return {
    tolerance_kind: tolerance.kind,
    tolerance_mode: tolerance.kind === "numeric" ? tolerance.mode : base.tolerance_mode,
    tolerance_amount: tolerance.kind === "numeric" ? String(tolerance.amount) : base.tolerance_amount,
    tolerance_seconds: tolerance.kind === "temporal" ? String(tolerance.seconds) : base.tolerance_seconds,
    tolerance_max_missing: tolerance.kind === "set" ? String(tolerance.max_missing) : base.tolerance_max_missing,
    tolerance_max_extra: tolerance.kind === "set" ? String(tolerance.max_extra) : base.tolerance_max_extra,
  };
}

export function v2DraftFromAssertion(assertion: V2Assertion): V2AssertionDraft {
  const draft = emptyV2AssertionDraft(assertion.assertion_type);
  return {
    ...draft,
    selectors: assertion.selectors.map((selector) => ({
      field: selector.field,
      operator: selector.operator,
      value_json: "value" in selector ? JSON.stringify(selector.value) : "",
    })),
    field: assertion.assertion_type === "value" ? assertion.field : "",
    fields:
      assertion.assertion_type === "row_set"
      || assertion.assertion_type === "ordering"
      || assertion.assertion_type === "invariant"
        ? assertion.fields.join(", ")
        : "",
    expected_json:
      assertion.assertion_type === "value" || assertion.assertion_type === "cardinality"
        ? JSON.stringify(assertion.expected)
        : "",
    expected_rows_json:
      assertion.assertion_type === "row_set" ? JSON.stringify(assertion.expected_rows, null, 2) : "[]",
    operator: assertion.operator,
    ...v2ToleranceDraft(assertion.tolerance),
  };
}

/** An immutable version read back as an editable draft. Saving mints a NEW
 *  version; nothing here rewrites the one it was read from. */
export function draftFromVersion(
  version: GoldenQuestionVersion,
  linkKinds: string[],
): DefinitionDraft {
  const boundary = version.time_boundary ?? {};
  const asOf = typeof boundary.as_of === "string" ? boundary.as_of : "";
  const from = typeof boundary.from === "string" ? boundary.from : "";
  const to = typeof boundary.to === "string" ? boundary.to : "";
  const declared = new Map(
    (version.required_provenance ?? []).map((entry) => [entry.link_kind, entry.required]),
  );
  const kinds = linkKinds.length > 0 ? linkKinds : [...declared.keys()];
  const pathNodes = version.expected_ai_path?.required_nodes ?? [];
  const forbiddenNodes = version.expected_ai_path?.forbidden_nodes ?? [];
  return {
    contract_version: version.contract_version ?? "golden-question.v1",
    business_domain_id: version.business_domain_id,
    business_domain_version_number: String(version.business_domain_version_number),
    business_classification_id: version.business_classification_id ?? "",
    semantic_view_id: version.semantic_view_id,
    semantic_view_version_id: version.semantic_view_version_id,
    semantic_view_version_role: version.semantic_view_version_role,
    question: version.question,
    time_boundary_kind: asOf ? "as_of" : from || to ? "range" : "none",
    as_of: asOf,
    from,
    to,
    timezone: typeof boundary.timezone === "string" ? boundary.timezone : "",
    result_type: version.result_type,
    severity: version.severity,
    capability_tags: (version.capability_tags ?? []).join(", "),
    assertions: version.contract_version === "golden-question.v2"
      ? []
      : (version.expected_result as Assertion[] ?? []).map((assertion) => ({
          assertion_type: assertion.assertion_type,
          description: typeof assertion.description === "string" ? assertion.description : "",
          ...toleranceDraft(assertion.tolerance),
        })),
    v2_assertions: version.contract_version === "golden-question.v2"
      ? (version.expected_result as V2Assertion[] ?? []).map(v2DraftFromAssertion)
      : [],
    provenance: kinds.map((link_kind) => ({
      link_kind,
      state: declared.has(link_kind)
        ? declared.get(link_kind)
          ? ("required" as ProvenanceState)
          : ("optional" as ProvenanceState)
        : ("absent" as ProvenanceState),
      expected_ref: (version.required_provenance ?? []).find((entry) => entry.link_kind === link_kind)?.expected_ref ?? "",
    })),
    required_nodes: pathNodes.map(pathNodeDraft),
    // The panel edits forbidden TOOLS. A forbidden node named any other way is
    // carried, not shown as a tool it is not and not dropped either.
    forbidden_tools: forbiddenNodes
      .filter(isBareToolRule)
      .map((rule) => rule.tool_name as string),
    carried_forbidden_nodes: forbiddenNodes.filter((rule) => !isBareToolRule(rule)),
    // The select above offers positions; the stored constraint names keys. The
    // position is derived from the required nodes it was stored beside, and a
    // constraint whose key is no longer declared resolves to "not chosen" rather
    // than to a silently wrong node.
    order_constraints: (version.expected_ai_path?.order_constraints ?? []).map((constraint) => ({
      before: nodeIndexOf(pathNodes, constraint.before),
      after: nodeIndexOf(pathNodes, constraint.after),
    })),
    alternatives: [...(version.expected_ai_path?.alternatives ?? [])],
    extra_step_rules: [...(version.expected_ai_path?.extra_step_rules ?? [])],
    reference_paths: (version.reference_paths ?? []).map((path) => ({
      query_spec_version_id: path.query_spec_version_id,
      role: path.role,
    })),
  };
}

/** A forbidden node this screen's "forbidden tools" panel can round-trip. */
function isBareToolRule(rule: ForbiddenNode): boolean {
  return Boolean(rule.tool_name) && !rule.key && !rule.owner_workspace && !rule.owner_object_type;
}

/** The stored node key, back into the three form fields it was built from. */
function pathNodeDraft(node: PathNode): PathNodeDraft {
  const [owner_workspace = "", owner_object_type = "", owner_object_id = ""] = (
    node.key ?? ""
  ).split("/");
  return {
    ...emptyPathNodeDraft(),
    step_kind: node.step_kind ?? "",
    owner_workspace,
    owner_object_type,
    owner_object_id,
    owner_version_id: node.owner_version_id ?? "",
    skill_version_id: node.skill_pin?.skill_version_id ?? "",
    skill_step_id: node.skill_pin?.skill_step_id ?? "",
    tool_name: node.tool?.tool_name ?? "",
  };
}

/** The position of a stored node key among the required nodes, as the select
 *  above expresses it -- or "" when no declared node carries that key. */
function nodeIndexOf(nodes: PathNode[], key: string): string {
  const index = nodes.findIndex((node) => node.key === key);
  return index < 0 ? "" : String(index);
}

function pathNodeKey(node: PathNodeDraft): string {
  return [node.owner_workspace, node.owner_object_type, node.owner_object_id]
    .map((part) => part.trim())
    .join("/");
}

function pathNodePayload(node: PathNodeDraft): PathNode {
  // Only what the author actually filled travels. An empty string is not a
  // value: `owner_version_id: ""` would be read as a pin that names nothing.
  // The key is always sent, even when the triple is incomplete, so the server
  // refuses the node BY NAME instead of the screen quietly dropping it.
  const payload: PathNode = { key: pathNodeKey(node) };
  const stepKind = node.step_kind.trim();
  if (stepKind) payload.step_kind = stepKind;
  const ownerVersion = node.owner_version_id.trim();
  if (ownerVersion) payload.owner_version_id = ownerVersion;
  const skillVersion = node.skill_version_id.trim();
  const skillStep = node.skill_step_id.trim();
  // Both or neither, exactly as `ck_ai_path_steps_skill_pin` requires: half a
  // Skill pin resolves to the wrong step the first time a Skill is revised.
  if (skillVersion || skillStep) {
    payload.skill_pin = { skill_version_id: skillVersion, skill_step_id: skillStep };
  }
  const toolName = node.tool_name.trim();
  if (toolName) payload.tool = { tool_name: toolName };
  return payload;
}

/** The draft as the exact document `POST /golden-questions` accepts.
 *
 *  It never repairs and never guesses: a field the author left undecided is
 *  omitted so the server refuses it by name, rather than silently acquiring a
 *  default nobody chose. `expected_render_ref` is never sent at all. */
export function definitionPayload(draft: DefinitionDraft): Record<string, unknown> {
  const payload: Record<string, unknown> = {};

  if (draft.contract_version === "golden-question.v2") {
    payload.contract_version = "golden-question.v2";
  }

  const domainId = draft.business_domain_id.trim();
  if (domainId) payload.business_domain_id = domainId;
  const domainVersion = Number.parseInt(draft.business_domain_version_number, 10);
  if (Number.isInteger(domainVersion)) payload.business_domain_version_number = domainVersion;
  const classification = draft.business_classification_id.trim();
  if (classification) payload.business_classification_id = classification;

  const viewId = draft.semantic_view_id.trim();
  const viewVersionId = draft.semantic_view_version_id.trim();
  if (viewId) payload.semantic_view_id = viewId;
  if (viewVersionId) payload.semantic_view_version_id = viewVersionId;
  if (draft.semantic_view_version_role) {
    payload.semantic_view_version_role = draft.semantic_view_version_role;
  }

  payload.question = draft.question.trim();

  const boundary: Record<string, string> = {};
  if (draft.time_boundary_kind === "as_of" && draft.as_of.trim()) {
    boundary.as_of = draft.as_of.trim();
  }
  if (draft.time_boundary_kind === "range") {
    if (draft.from.trim()) boundary.from = draft.from.trim();
    if (draft.to.trim()) boundary.to = draft.to.trim();
  }
  if (draft.time_boundary_kind !== "none" && draft.timezone.trim()) {
    boundary.timezone = draft.timezone.trim();
  }
  payload.time_boundary = boundary;

  if (draft.contract_version === "golden-question.v2" && (draft.v2_assertions.length === 0 || draft.v2_assertions.length > 32)) {
    throw new Error("Golden Question v2 requires between 1 and 32 assertions.");
  }
  payload.expected_result = draft.contract_version === "golden-question.v2"
    ? draft.v2_assertions.map(v2AssertionPayload)
    : draft.assertions.map((assertion) => {
    const entry: Record<string, unknown> = { assertion_type: assertion.assertion_type.trim() };
    if (assertion.description.trim()) entry.description = assertion.description.trim();
    if (assertion.tolerance_kind === "exact") {
      entry.tolerance = null;
    } else if (assertion.tolerance_kind) {
      const tolerance: Record<string, unknown> = { kind: assertion.tolerance_kind };
      const raw = assertion.tolerance_value.trim();
      if (raw) {
        const numeric = Number(raw);
        tolerance.value =
          assertion.tolerance_kind === "numeric" && Number.isFinite(numeric) ? numeric : raw;
      }
      entry.tolerance = tolerance;
    }
    // No `else`: an undeclared tolerance leaves the key ABSENT on purpose.
    return entry;
      });

  payload.required_provenance = draft.provenance
    .filter((entry) => entry.state !== "absent")
    .map((entry) => ({
      link_kind: entry.link_kind,
      required: entry.state === "required",
      ...(entry.expected_ref.trim() ? { expected_ref: entry.expected_ref.trim() } : {}),
    }));

  const expectedNodes = draft.required_nodes.map(pathNodePayload);
  const forbiddenNodes: ForbiddenNode[] = [
    ...draft.forbidden_tools
      .map((tool) => tool.trim())
      .filter(Boolean)
      .map((tool_name) => ({ tool_name })),
    ...draft.carried_forbidden_nodes,
  ];
  // The positions the select works in become the node KEYS the comparator
  // matches on. A constraint whose position no longer names a declared node is
  // dropped here rather than sent as a rule nothing can ever check.
  const orderConstraints: OrderConstraint[] = draft.order_constraints
    .map((constraint) => ({
      before: expectedNodes[Number.parseInt(constraint.before, 10)]?.key,
      after: expectedNodes[Number.parseInt(constraint.after, 10)]?.key,
    }))
    .filter((constraint): constraint is OrderConstraint =>
      Boolean(constraint.before && constraint.after),
    );

  const expectedAiPath: ExpectedAiPath = {
    grammar_version: 1,
    required_nodes: expectedNodes,
  };
  if (forbiddenNodes.length > 0) expectedAiPath.forbidden_nodes = forbiddenNodes;
  if (orderConstraints.length > 0) expectedAiPath.order_constraints = orderConstraints;
  if (draft.alternatives.length > 0) expectedAiPath.alternatives = draft.alternatives;
  if (draft.extra_step_rules.length > 0) expectedAiPath.extra_step_rules = draft.extra_step_rules;
  payload.expected_ai_path = expectedAiPath;

  if (draft.result_type) payload.result_type = draft.result_type;
  payload.capability_tags = draft.capability_tags
    .split(",")
    .map((tag) => tag.trim())
    .filter(Boolean);
  if (draft.severity) payload.severity = draft.severity;

  payload.reference_paths = draft.reference_paths
    .filter((path) => path.query_spec_version_id.trim() !== "")
    .map((path) => ({
      query_spec_version_id: path.query_spec_version_id.trim(),
      role: path.role,
    }));

  return payload;
}

/** The server's structured refusal list, read without being rewritten. */
export interface Refusal {
  code: string;
  message: string;
  subject: string | null;
}

export function refusalsOf(body: unknown): Refusal[] {
  if (!body || typeof body !== "object") return [];
  const list = (body as { refusals?: unknown }).refusals;
  if (!Array.isArray(list)) return [];
  return list
    .filter((entry): entry is Record<string, unknown> => Boolean(entry) && typeof entry === "object")
    .map((entry) => ({
      code: String(entry.code ?? "refused"),
      message: String(entry.message ?? ""),
      subject: typeof entry.subject === "string" ? entry.subject : null,
    }));
}
