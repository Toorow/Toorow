/**
 * The client half of the Governance read model.
 *
 * The server composes; this file only VALIDATES and carries. It refuses a
 * response whose schema, Project, section, lens or object type is not exactly
 * what was asked for, because a screen that renders whatever arrived is how a
 * neighbouring Project's object ends up under this Project's address.
 *
 * There is no browser-side join here on purpose. Counting, coverage and used-by
 * are answered by the owner, once, server-side — a fan-out of four calls
 * stitched in the browser is authoritative-looking and wrong the moment one of
 * them fails.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, apiGet } from "../lib/apiFetch";

export type GovernanceSection =
  | "master-data"
  | "semantic-model"
  | "controls-quality"
  | "evidence";

export const GOVERNANCE_SECTIONS: readonly GovernanceSection[] = [
  "master-data",
  "semantic-model",
  "controls-quality",
  "evidence",
];

/** available: the owner answered. empty: it answered "none". unavailable: no
 *  owner answered, and nobody may read that as "none". */
export type FacetState = "available" | "empty" | "unavailable";

export interface OwnerReference {
  surface: "project" | "global";
  workspace: string | null;
  section: string | null;
  global_surface: string | null;
  global_section: string | null;
  object_type: string | null;
  object_id: string | null;
  tab: string | null;
  action: string | null;
  version_id: string | null;
  evidence_id?: string | null;
}

export interface VersionRef {
  object_type: string;
  id: string;
  version?: number | null;
  state?: string | null;
  recorded_at?: string | null;
  evidence_hash?: string | null;
  owner_href?: OwnerReference | null;
}

export interface EvidenceRef {
  evidence_id: string;
  kind: string;
  state: string;
  recorded_at?: string | null;
  integrity_ref?: string | null;
  correlation_ref?: string | null;
  owner_href?: OwnerReference | null;
}

export interface Facet<T> {
  state: FacetState;
  count: number;
  refs: T[];
  truncated: boolean;
  /**
   * Carried only when the facet is NOT available (49-2, 2026-09-01). A count
   * the owner could not turn into a list is `unavailable` WITH its count, and
   * this is the sentence that says which gesture resolves it — never a state
   * word rendered raw.
   */
  reason?: UnavailableReason | null;
}

export interface GovernanceObject {
  object_ref: { type: string; id: string; label: string; owner_href: OwnerReference };
  scope: string;
  owner: Record<string, unknown>;
  lifecycle_status: string;
  active_version_ref: VersionRef | null;
  selected_version_ref: VersionRef | null;
  available_tabs: string[];
  default_tab: string;
  allowed_actions: string[];
  used_by: Facet<Record<string, unknown>>;
  versions: Facet<VersionRef>;
  evidence: Facet<EvidenceRef>;
  summary: Record<string, unknown>;
  evidence_as_of: string | null;
}

export interface UnavailableReason {
  code: string;
  message: string;
  owner_reference?: OwnerReference;
}

/** How much of a lens the server could actually prove.
 *
 *  `state` answers "did we find anything?"; `index_state` answers "did we look
 *  at everything?". They are different questions, and a surface that collapses
 *  them reads a backfill in progress as an empty Project. */
export interface Coverage {
  state: FacetState;
  returned: number;
  total: number | null;
  bound: number;
  index_state?: "complete" | "partial" | "unavailable" | "backfilling";
  adapters?: AdapterCoverage[];
  evidence_horizon?: string | null;
}

export interface AdapterCoverage {
  producer: string;
  label: string;
  state: string;
  owner_workspace: string;
  reason_code?: string | null;
  last_indexed_at?: string | null;
  indexed_count?: number;
  failure_class?: string | null;
}

export interface GovernanceCollectionEnvelope {
  schema_version: "governance-collection.v1";
  project_ref: { object_type: string; id: string };
  organization_ref: { object_type: string; id: string };
  section: GovernanceSection;
  generated_at: string;
  evidence_as_of: string | null;
  lens: string;
  default_lens: string;
  available_lenses: string[];
  items: GovernanceObject[];
  coverage: Coverage;
  unavailable_reasons: UnavailableReason[];
  /** Present only on a server-paginated lens. `null` means this is the last page
   *  — NOT that the page was truncated silently. */
  next_cursor?: string | null;
  applied_filters?: Record<string, string>;
  /** What each section declares as narrowable. Master Data declares the scopes
   *  it LOADED — a measurement. Evidence declares the vocabulary its index
   *  VALIDATES against (`_evidence_filter_options`), which is why the three
   *  lists are slugs and carry no labels: naming them is the console's half. */
  filter_options?: {
    scopes?: string[];
    owner_workspaces?: string[];
    correlation_kinds?: string[];
    record_kinds?: string[];
  };
}

/** The Evidence graph, version and audit projections. Every one of them is
 *  composed SERVER-side: there is no browser join here, because a fan-out
 *  stitched in the client is authoritative-looking and wrong the moment one of
 *  its calls fails. */
export interface TraceNode {
  record_id: string;
  producer: string;
  owner_workspace: string;
  owner_object_type: string;
  owner_object_id: string;
  owner_version_id: string | null;
  is_anchor: boolean;
  occurred_at: string | null;
  observed_at: string | null;
  integrity_hash: string | null;
  availability: string;
  correlations: Array<{ kind: string; id: string }>;
  owner_href: OwnerReference | null;
}

export interface TraceEdge {
  id: string;
  relation: string;
  from: string;
  to: string;
  ordinal: number | null;
  integrity_hash: string | null;
}

export interface TraceOwnerReference {
  id: string;
  relation: string;
  from: string;
  owner_workspace: string | null;
  owner_object_type: string | null;
  owner_object_id: string | null;
  owner_version_id: string | null;
  ordinal: number | null;
  owner_href: OwnerReference | null;
}

export interface TraceGraph {
  anchor?: { record_id: string; producer: string; owner_object_type: string; owner_object_id: string };
  nodes: TraceNode[];
  edges: TraceEdge[];
  owner_references: TraceOwnerReference[];
  roots: string[];
  terminals: string[];
  coverage: string;
  unavailable_reasons: UnavailableReason[];
}

export interface VersionDiff {
  state: "available" | "unavailable";
  reason_code?: string;
  message?: string;
  selected_version_id?: string | null;
  predecessor_version_id?: string | null;
  predecessor_record_id?: string;
  selected_integrity_hash?: string | null;
  predecessor_integrity_hash?: string | null;
  changed?: boolean;
  owner_href?: OwnerReference | null;
}

export interface VersionDetail {
  diff: VersionDiff;
  approvals: { state: FacetState; items: Array<Record<string, unknown>> };
  used_by: { state: FacetState; items: Array<Record<string, unknown>> };
}

export interface AuditDetail {
  state: "available" | "unavailable";
  reason_code?: string;
  message?: string;
  audit_id?: string;
  actor?: string | null;
  action?: string | null;
  outcome?: string | null;
  occurred_at?: string | null;
  resource_path?: string[];
  correlation?: { trace_id: string | null; operation_id: string | null };
  versions?: Record<string, string | null>;
  hashes?: Record<string, string | null>;
}

export interface GovernanceObjectEnvelope {
  schema_version: "governance-object.v1" | "governance-version.v1";
  project_ref: { object_type: string; id: string };
  organization_ref: { object_type: string; id: string };
  section: GovernanceSection;
  object_type: string;
  generated_at: string;
  evidence_as_of: string | null;
  state: "available" | "unavailable";
  object: GovernanceObject | null;
  unavailable_reasons: UnavailableReason[];
  requested_version_id?: string;
  /** Present only on a version read. `stale` means the exact requested version
   *  is authorized and readable but no longer current — it is NOT replaced. */
  version_state?: "current" | "stale";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

const MISMATCH = "This Governance response does not match the address that was opened.";

function assertHead(value: unknown, projectId: string, section: string): Record<string, unknown> {
  if (!isRecord(value)) throw new Error("The Governance response is malformed.");
  if (!isRecord(value.project_ref) || value.project_ref.id !== projectId) throw new Error(MISMATCH);
  if (value.section !== section) throw new Error(MISMATCH);
  return value;
}

export function parseCollection(
  value: unknown,
  projectId: string,
  section: string,
  lens: string | null,
): GovernanceCollectionEnvelope {
  const head = assertHead(value, projectId, section);
  if (head.schema_version !== "governance-collection.v1") throw new Error(MISMATCH);
  if (!Array.isArray(head.items) || !Array.isArray(head.unavailable_reasons)) {
    throw new Error("The Governance response is malformed.");
  }
  // A lens that came back different from the one asked for would silently
  // relabel the screen. Refuse rather than render another lens' rows.
  if (lens !== null && head.lens !== lens) throw new Error(MISMATCH);
  return head as unknown as GovernanceCollectionEnvelope;
}

export function parseObject(
  value: unknown,
  projectId: string,
  section: string,
  objectType: string,
): GovernanceObjectEnvelope {
  const head = assertHead(value, projectId, section);
  if (head.schema_version !== "governance-object.v1" && head.schema_version !== "governance-version.v1") {
    throw new Error(MISMATCH);
  }
  if (head.object_type !== objectType) throw new Error(MISMATCH);
  const detail = head.object;
  if (detail !== null) {
    if (!isRecord(detail) || !isRecord(detail.object_ref) || detail.object_ref.type !== objectType) {
      throw new Error(MISMATCH);
    }
  }
  return head as unknown as GovernanceObjectEnvelope;
}

const ROOT = (projectId: string, section: string) =>
  `/api/projects/${encodeURIComponent(projectId)}/governance/${encodeURIComponent(section)}`;

export async function getGovernanceCollection(
  projectId: string,
  section: string,
  lens: string | null,
  filters: Record<string, string> = {},
  signal?: AbortSignal,
): Promise<GovernanceCollectionEnvelope> {
  // Built here rather than concatenated: `URLSearchParams` encodes every value,
  // and an unencoded cursor or correlation id is how a `&` in an opaque token
  // silently becomes a second parameter.
  const search = new URLSearchParams();
  if (lens) search.set("lens", lens);
  for (const [key, value] of Object.entries(filters)) {
    if (value) search.set(key, value);
  }
  const query = search.toString();
  const value = await apiGet<unknown>(`${ROOT(projectId, section)}${query ? `?${query}` : ""}`, {
    cache: "no-store",
    signal,
  });
  return parseCollection(value, projectId, section, lens);
}

export async function getGovernanceObject(
  projectId: string,
  section: string,
  objectType: string,
  objectId: string,
  versionId: string | null,
  signal?: AbortSignal,
): Promise<GovernanceObjectEnvelope> {
  const base = `${ROOT(projectId, section)}/objects/${encodeURIComponent(objectType)}/${encodeURIComponent(objectId)}`;
  const path = versionId ? `${base}/versions/${encodeURIComponent(versionId)}` : base;
  const value = await apiGet<unknown>(path, { cache: "no-store", signal });
  return parseObject(value, projectId, section, objectType);
}

/** The five states a Governance surface can honestly be in. `unknown` and
 *  `denied` are distinct from `error`: one is an address, one is a permission. */
export type GovernanceState<T> =
  | { status: "loading" }
  | { status: "ready"; envelope: T }
  | { status: "unknown" }
  | { status: "denied" }
  | { status: "error"; message: string };

function toState<T>(reason: unknown): GovernanceState<T> {
  if (reason instanceof ApiError) {
    if (reason.status === 403) return { status: "denied" };
    if (reason.status === 404) return { status: "unknown" };
  }
  return {
    status: "error",
    message: reason instanceof Error ? reason.message : "Request failed",
  };
}

/** True for a request this component deliberately cancelled.
 *
 *  A cancelled request is not a failure and must never paint an error: the
 *  person switched Project or changed a filter, and the answer they abandoned
 *  arriving late is exactly what the abort exists to discard. */
function wasAborted(reason: unknown): boolean {
  return reason instanceof DOMException && reason.name === "AbortError";
}

export function useGovernanceCollection(
  projectId: string | undefined,
  section: string,
  lens: string | null,
  filters: Record<string, string> = {},
) {
  const scope = projectId?.trim() ?? "";
  const [state, setState] = useState<GovernanceState<GovernanceCollectionEnvelope>>({ status: "loading" });
  const [attempt, setAttempt] = useState(0);
  const reload = useCallback(() => setAttempt((value) => value + 1), []);
  const generation = useRef(0);
  // Serialized so the effect re-runs on a CHANGED filter set rather than on
  // every render: a new object literal is a new dependency every time.
  const fingerprint = JSON.stringify(
    Object.fromEntries(Object.entries(filters).filter(([, value]) => value).sort()),
  );

  useEffect(() => {
    const current = ++generation.current;
    const controller = new AbortController();
    setState({ status: "loading" });
    if (!scope) {
      setState({ status: "error", message: "Select a Project before opening Governance." });
      return;
    }
    const stale = () => controller.signal.aborted || current !== generation.current;
    void getGovernanceCollection(scope, section, lens, JSON.parse(fingerprint), controller.signal)
      .then((envelope) => { if (!stale()) setState({ status: "ready", envelope }); })
      .catch((reason: unknown) => {
        if (stale() || wasAborted(reason)) return;
        setState(toState(reason));
      });
    return () => controller.abort();
  }, [scope, section, lens, fingerprint, attempt]);

  return { state, reload };
}

export function useGovernanceObject(
  projectId: string | undefined,
  section: string,
  objectType: string,
  objectId: string,
  versionId: string | null,
) {
  const scope = projectId?.trim() ?? "";
  const [state, setState] = useState<GovernanceState<GovernanceObjectEnvelope>>({ status: "loading" });
  const [attempt, setAttempt] = useState(0);
  const reload = useCallback(() => setAttempt((value) => value + 1), []);
  const generation = useRef(0);

  useEffect(() => {
    const current = ++generation.current;
    const controller = new AbortController();
    setState({ status: "loading" });
    if (!scope) {
      setState({ status: "error", message: "Select a Project before opening Governance." });
      return;
    }
    const stale = () => controller.signal.aborted || current !== generation.current;
    void getGovernanceObject(scope, section, objectType, objectId, versionId, controller.signal)
      .then((envelope) => { if (!stale()) setState({ status: "ready", envelope }); })
      .catch((reason: unknown) => {
        if (stale() || wasAborted(reason)) return;
        setState(toState(reason));
      });
    return () => controller.abort();
  }, [scope, section, objectType, objectId, versionId, attempt]);

  return { state, reload };
}
