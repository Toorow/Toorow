/**
 * The ONE client boundary between ui/admin and the Story 50.3 artifact service.
 *
 * Everything here is a typed call to a server route. No Report, Notebook Run or
 * Render is assembled locally, no evidence is joined in the browser, and no
 * collection is filtered client-side: a page whose count the server never
 * computed is a page that shows a number nobody can reproduce.
 *
 * Every call goes through `apiGet`/`apiPost`/`apiPut`, so the bearer header
 * cannot be forgotten and a failure arrives as a typed `ApiError` carrying the
 * server's `{code, message}` envelope -- never as a silent fallback to invented
 * data.
 *
 * WHY THIS LIVES HERE AND NOT IN `src/analyze/`. Story 50.2 owns
 * `ui/admin/src/analyze/*` and is being implemented in parallel. Writing into
 * that directory would be two sessions editing one boundary blind. The
 * consolidation Story 50.3 Task 8 asks for is a one-file move once 50.2 lands,
 * and it is recorded as such rather than done unilaterally.
 */
import { apiDelete, apiGet, apiPost, apiPut } from "../lib/apiFetch";

/** One named, actionable reason the server refused a request. */
export interface Refusal {
  code: string;
  message: string;
  subject: string | null;
}

/** A downstream contract this deployment does not have yet. */
export interface MissingContract {
  contract: string;
  owner_story: string;
  missing_link: string;
}

/** A presentation kind this deployment cannot resolve, and what would resolve it. */
export interface UnpinnableKind extends MissingContract {
  /** The wire token migration 154's CHECK accepts. Never printed to a person. */
  kind: string;
}

/**
 * Stated by the server, never inferred here from an empty list.
 *
 * `available` and `unpinnable_kinds` are two different facts and the screen must
 * not collapse them. `available` says a preserved artifact can be replayed at
 * all; `unpinnable_kinds` says which presentation kinds have no registry behind
 * them. On 2026-08-31 the deployment answered `available: true` while the Chart
 * Template had no table at all -- and the console offered it for pinning anyway.
 *
 * Optional because a deployment that predates the field simply sends nothing,
 * and an absent list must read as "the server did not say", not as "nothing is
 * missing" -- every reader below treats it as an empty list, which renders no
 * claim rather than a false reassurance.
 */
export interface ContractState {
  available: boolean;
  missing: MissingContract[];
  unpinnable_kinds?: UnpinnableKind[];
  pinnable_kinds?: string[];
}

/** Either a complete presentation reference, or the exact absence literal. */
export interface PresentationRef {
  kind: string | null;
  version_id: string | null;
  absent_literal: string | null;
  /**
   * Whether the Chart Template this pin names has been ARCHIVED since it was pinned.
   *
   * `null` for every pin that is not a Chart Template version — never `false`,
   * which would assert a liveness the server did not measure. An archived
   * template still resolves: archiving retires the head with a date and deletes
   * nothing, so the pin is intact and only NEW pins stop being offered. The
   * screen says so rather than showing a pin that looks live.
   */
  archived?: boolean | null;
}

export interface ReportSummary {
  id: string;
  label: string;
  description: string | null;
  seed_origin: string;
  seed: { module_name: string; report_id: string } | null;
  current_version_id: string | null;
  archived: boolean;
  /** When and by whom it was retired. Null on a live artifact — the two travel
   *  together, enforced by `ck_analysis_reports_archived_pair`. */
  archived_at: string | null;
  archived_by: string | null;
  created_by: string;
  created_at: string | null;
  updated_at: string | null;
  current_version_number: number | null;
  query_spec_version_id: string | null;
  presentation_absent: string | null;
  run_count: number;
}

/** Connector seed AVAILABILITY. Not a Report, and never displayed as one. */
export interface ReportSeed {
  module_name: string;
  report_id: string;
  enabled: boolean;
  display_order: number;
  kind: "connector_seed";
}

/** What an archive answers. `already_archived` is said rather than hidden: a
 *  double-submitted confirmation asked for a state that had already happened. */
export interface ArchiveOutcome {
  id: string;
  archived: boolean;
  archived_at: string | null;
  archived_by: string | null;
  already_archived: boolean;
}

export interface ReportCollection {
  reports: ReportSummary[];
  seeds: ReportSeed[];
  presentation_contract: ContractState;
}

export interface ReportVersion {
  id: string;
  version_number: number;
  label: string;
  query_spec_id: string;
  query_spec_version_id: string;
  presentation: PresentationRef;
  content_hash: string;
  predecessor_version_id: string | null;
  created_by: string;
  created_at: string | null;
}

export interface ReportRun {
  id: string;
  report_version_id: string;
  query_spec_version_id: string;
  result_id: string;
  render_id: string | null;
  outcome: string;
  requested_as_of: string | null;
  resolved_as_of: string | null;
  actor: string;
  started_at: string | null;
  ended_at: string | null;
}

export interface ReportDetail extends ReportSummary {
  versions: ReportVersion[];
  runs: ReportRun[];
  /**
   * What this deployment can actually pin. The Presentation panel is the screen
   * that offers the gesture, so it reads the contract here rather than assuming
   * every kind the picker can spell exists.
   */
  presentation_contract?: ContractState;
  /**
   * Story 72.5, AC21 — the versions a pin could actually RESOLVE, keyed by the
   * wire token the pin stores. `presentation_contract` says which KINDS this
   * deployment can resolve at all; this says which VERSIONS of those kinds this
   * Project holds. The two are different facts and the picker needs both: a kind
   * with no registry is disabled, a kind with an empty list says the Project has
   * none and names the gesture that makes one.
   *
   * Optional because a deployment that predates the field sends nothing, and an
   * absent map must read as "the server did not say" — which renders as the
   * empty state, never as a text box for an identifier nobody verified.
   */
  pinnable_presentation_versions?: Record<string, PinnablePresentationVersion[]>;
}

/** One presentation version a Report may pin, as the server lists it. */
export interface PinnablePresentationVersion {
  version_id: string;
  label: string;
  version_number: number;
  family: string;
  content_hash: string;
  created_at: string | null;
  /** The product noun of the kind. The stored token is never printed. */
  kind_noun: string;
}

export interface NotebookBlock {
  block_key: string;
  position: number;
  block_type: "query" | "report" | "narrative";
  query_spec_version_id: string | null;
  report_version_id: string | null;
  presentation: PresentationRef;
  renders: boolean;
  as_of_rule: string;
  narrative: Record<string, unknown>;
  content_hash: string;
}

export interface NotebookVersion {
  id: string;
  version_number: number;
  label: string;
  content_hash: string;
  predecessor_version_id: string | null;
  created_by: string;
  created_at: string | null;
  blocks: NotebookBlock[];
}

export interface NotebookRunBlock {
  block_key: string;
  position: number;
  block_type: string;
  query_spec_version_id: string | null;
  report_version_id: string | null;
  result_id: string | null;
  render_id: string | null;
  /** Exactly one of `render_id` / `render_absent_literal` is set. Never both. */
  render_absent_literal: string | null;
  status: string;
  limitation: string | null;
  resolved_as_of: string | null;
  started_at: string | null;
  ended_at: string | null;
}

export interface NotebookRun {
  id: string;
  notebook_id: string;
  notebook_version_id: string;
  idempotency_key: string;
  dispatch_source: string;
  state: string;
  outcome: string | null;
  requested_as_of: string | null;
  resolved_as_of: string | null;
  actor: string;
  accepted_at: string | null;
  terminal_at: string | null;
  blocks: NotebookRunBlock[];
}

export interface NotebookSchedule {
  recurrence: string;
  timezone?: string;
  enabled: boolean;
  next_due_at: string | null;
  /** What dispatch actually did. Absent on the collection projection. */
  last_dispatched_at?: string | null;
  last_run_id?: string | null;
  last_dispatch_note?: string | null;
  /**
   * The exact call site that turns this policy into Runs, named by the server.
   * A named dispatcher can be checked; the boolean this panel used to imply
   * ("Enabled: Yes") could not, and was false for the whole of Story 50.3's
   * first delivery — nothing in the repository read the schedule.
   */
  dispatcher?: string;
}

/** One legacy `app.notebook_runs` row, with the evidence it actually holds. */
export interface LegacyNotebookRun {
  id: string;
  executed_at: string | null;
  as_of: string | null;
  status: string;
  error_message: string | null;
  evidence: "inline_envelope" | "deferred" | "absent";
  pull_id_count: number;
  summary_snippet: string | null;
  /** Always null. A legacy Run has no per-block pin, and none is invented. */
  result_id: null;
  render_id: null;
  limitation: string;
  classification: "legacy_incomplete_evidence";
}

/** One legacy `app.notebooks` row. Never a canonical Notebook. */
export interface LegacyNotebook {
  id: string;
  title: string;
  report_ref: string;
  window_rule: string;
  narrative_prompt: string | null;
  scheduled: boolean;
  schedule_rule: string | null;
  /** Whether a legacy share exists. The token itself never leaves the server. */
  is_shared: boolean;
  shared_at: string | null;
  created_by: string;
  created_at: string | null;
  updated_at: string | null;
  classification: "legacy_mutable_definition";
  limitation: string;
  runs: LegacyNotebookRun[];
}

export interface NotebookSummary {
  id: string;
  label: string;
  description: string | null;
  current_version_id: string | null;
  archived: boolean;
  archived_at: string | null;
  archived_by: string | null;
  legacy_notebook_id: string | null;
  created_by: string;
  created_at: string | null;
  updated_at: string | null;
  current_version_number: number | null;
  run_count: number;
  last_run_at: string | null;
  last_run_outcome: string | null;
  schedule: NotebookSchedule | null;
}

export interface NotebookDetail extends NotebookSummary {
  versions: NotebookVersion[];
  runs: Omit<NotebookRun, "blocks">[];
}

export interface RenderSummary {
  id: string;
  result_id: string;
  visualization_spec_version_id: string;
  renderer_adapter: string;
  renderer_build_id: string;
  runtime_build_id: string;
  theme_version: string;
  formatter_version: string;
  responsive_profile: string;
  origin_kind: string;
  creation_surface: string;
  created_by: string;
  created_at: string | null;
  replayable: boolean;
}

export interface RenderCollection {
  renders: RenderSummary[];
  next_cursor: string | null;
  contract: ContractState;
}

/** A legacy snapshot. Never a Render, and it says which pins it lacks. */
export interface LegacySnapshot {
  id: string;
  tool_name: string;
  summary_snippet: string | null;
  question: string | null;
  identity: string | null;
  trace_id: string | null;
  created_at: string | null;
  had_widget_uri: boolean;
  classification: "legacy_unverifiable";
  replayable: false;
  missing_pins: string[];
}

export interface RenderDetail extends RenderSummary {
  result_content_hash: string;
  result_payload_retained: boolean;
  display_state: Record<string, unknown>;
  evidence_manifest: Record<string, unknown>;
  datum_evidence_keys: Record<string, unknown>;
  origin_report_run_id: string | null;
  origin_notebook_run_id: string | null;
  predecessor_render_id: string | null;
  content_hash: string;
  retention_actions: {
    action: string;
    reason: string;
    policy_ref: string;
    actor: string;
    created_at: string | null;
  }[];
  sharing: { canonical_share_available: boolean; reason: string };
}

export interface LegacyClassification {
  project_reports: Record<string, unknown>;
  notebooks: Record<string, unknown>;
  notebook_runs: Record<string, unknown>;
  render_snapshots: Record<string, unknown> & { missing_pins: string[] };
  render_snapshot_shares: Record<string, unknown>;
  canonical: { reports: number; notebooks: number; renders: number };
  render_contract: ContractState;
}

const base = (projectId: string) => `/api/projects/${encodeURIComponent(projectId)}/analyze`;

/** Pull the structured refusal list out of an ApiError body, or return none. */
export function refusalsOf(error: unknown): Refusal[] {
  const body = (error as { body?: { refusals?: Refusal[] } } | undefined)?.body;
  return Array.isArray(body?.refusals) ? body.refusals : [];
}

/** Pull the named missing contracts out of a refusal body, when there are any. */
export function missingContractsOf(error: unknown): string[] {
  return refusalsOf(error)
    .filter((r) => r.code === "missing_contract")
    .map((r) => r.message);
}

/**
 * Refuse a 200 that does not carry the collection it is supposed to carry.
 *
 * Every `list*` below fed its result straight into `{ status: "ready", data:
 * body.<key> }`, and the screens then read `.length` off it. A response missing
 * the key — a version skew, a partial rollout, a proxy answering something
 * else — therefore set `ready` with `data: undefined` and the screen threw on
 * its first render. Measured 2026-08-04 in the sandbox: Notebooks, Renders and
 * Widget Feedback each rendered **a blank page**, `Cannot read properties of
 * undefined (reading 'length')`, zero characters of body text.
 *
 * `getDataSurface` has refused malformed envelopes since it was written and its
 * screens say so out loud. This gives the Analyze artifacts the same floor: an
 * unusable answer is an ERROR, which every one of these screens already knows
 * how to show, and never a ready state built on nothing.
 */
function collection<T>(promise: Promise<T>, key: keyof T & string): Promise<T> {
  return promise.then((body) => {
    if (!body || !Array.isArray((body as Record<string, unknown>)[key])) {
      throw new Error(`The response carries no "${key}". Nothing was read.`);
    }
    return body;
  });
}

/**
 * The Reports of a Project, live by default.
 *
 * `includeArchived` is the one way back to a retired artifact, and it exists
 * because the archive confirmation PROMISES it: "an archived Report can be
 * listed again". The route has served `?include_archived=true` since the
 * archive landed and nothing in `ui/admin/src` ever sent it, so the sentence was
 * true of the server and false of the product.
 */
export function listReports(
  projectId: string,
  init?: RequestInit,
  options: { includeArchived?: boolean } = {},
): Promise<ReportCollection> {
  const query = options.includeArchived ? "?include_archived=true" : "";
  return collection(
    apiGet<ReportCollection>(`${base(projectId)}/reports${query}`, init),
    "reports",
  );
}

export function fetchReport(
  projectId: string,
  reportId: string,
  init?: RequestInit,
): Promise<ReportDetail> {
  return apiGet<ReportDetail>(
    `${base(projectId)}/reports/${encodeURIComponent(reportId)}`,
    init,
  );
}

export function createReport(
  projectId: string,
  body: {
    label: string;
    query_spec_version_id: string;
    description?: string;
    seed_origin?: string;
    seed_module_name?: string;
    seed_report_id?: string;
    presentation?: { kind: string; version_id: string };
  },
): Promise<{ report_id: string; id: string; version_number: number }> {
  return apiPost(`${base(projectId)}/reports`, body);
}

export function createReportVersion(
  projectId: string,
  reportId: string,
  body: {
    query_spec_version_id: string;
    label?: string;
    description?: string;
    presentation?: { kind: string; version_id: string };
  },
): Promise<{ id: string; version_number: number }> {
  return apiPost(
    `${base(projectId)}/reports/${encodeURIComponent(reportId)}/versions`,
    body,
  );
}

/** Retire a Report. The archived one leaves the default list; `listReports`
 *  with `includeArchived` is the way back. There is no delete — the schema
 *  refuses to remove a head whose versions and runs are evidence. */
export function archiveReport(
  projectId: string,
  reportId: string,
): Promise<ArchiveOutcome> {
  return apiPost(
    `${base(projectId)}/reports/${encodeURIComponent(reportId)}/archive`,
    {},
  );
}

/** Retire a Notebook. Its schedule stops with it — `dispatch_due_notebook_schedules`
 *  already filtered archived Notebooks out, on a column nothing used to write. */
export function archiveNotebook(
  projectId: string,
  notebookId: string,
): Promise<ArchiveOutcome> {
  return apiPost(
    `${base(projectId)}/notebooks/${encodeURIComponent(notebookId)}/archive`,
    {},
  );
}

export function runReportVersion(
  projectId: string,
  reportVersionId: string,
): Promise<{ run_id: string; result_id: string; outcome: string; render: ContractState }> {
  return apiPost(
    `${base(projectId)}/report-versions/${encodeURIComponent(reportVersionId)}/run`,
    {},
  );
}

/** The Notebooks of a Project, live by default. See `listReports` for why the
 *  flag exists at all. */
export function listNotebooks(
  projectId: string,
  init?: RequestInit,
  options: { includeArchived?: boolean } = {},
): Promise<{ notebooks: NotebookSummary[] }> {
  const query = options.includeArchived ? "?include_archived=true" : "";
  return collection(apiGet(`${base(projectId)}/notebooks${query}`, init), "notebooks");
}

export function fetchNotebook(
  projectId: string,
  notebookId: string,
  init?: RequestInit,
): Promise<NotebookDetail> {
  return apiGet<NotebookDetail>(
    `${base(projectId)}/notebooks/${encodeURIComponent(notebookId)}`,
    init,
  );
}

/**
 * The legacy Notebook definitions and their Runs, read-only.
 *
 * AC12 requires them to remain readable with their actual evidence. They were,
 * through `NotebooksPanel.tsx`, until the canonical screens took over the Analyze
 * sections and that panel stopped being mounted anywhere — which made the
 * requirement false without a line of it being repealed. Separate call, separate
 * panel: the two families are never merged into one list.
 */
export function listLegacyNotebooks(
  projectId: string,
  init?: RequestInit,
): Promise<{ notebooks: LegacyNotebook[] }> {
  return collection(apiGet(`${base(projectId)}/notebooks/legacy`, init), "notebooks");
}

export function createNotebook(
  projectId: string,
  body: { label: string; description?: string; blocks: unknown[] },
): Promise<{ notebook_id: string; id: string; version_number: number }> {
  return apiPost(`${base(projectId)}/notebooks`, body);
}

export function createNotebookVersion(
  projectId: string,
  notebookId: string,
  body: { blocks: unknown[]; label?: string },
): Promise<{ id: string; version_number: number }> {
  return apiPost(
    `${base(projectId)}/notebooks/${encodeURIComponent(notebookId)}/versions`,
    body,
  );
}

/**
 * Run a Notebook. The idempotency key is REQUIRED by the server and generated
 * here per user action, so a double click completes the run already accepted
 * instead of starting a second one over the same evidence.
 */
export function runNotebook(
  projectId: string,
  notebookId: string,
  idempotencyKey: string,
): Promise<NotebookRun & { idempotent_replay: boolean }> {
  return apiPost(`${base(projectId)}/notebooks/${encodeURIComponent(notebookId)}/runs`, {
    idempotency_key: idempotencyKey,
    dispatch_source: "manual",
  });
}

export function setNotebookSchedule(
  projectId: string,
  notebookId: string,
  body: { recurrence: string; enabled: boolean; timezone?: string },
): Promise<{ notebook_id: string; recurrence: string; enabled: boolean }> {
  return apiPut(
    `${base(projectId)}/notebooks/${encodeURIComponent(notebookId)}/schedule`,
    body,
  );
}

export function fetchNotebookRun(
  projectId: string,
  runId: string,
  init?: RequestInit,
): Promise<NotebookRun> {
  return apiGet<NotebookRun>(
    `${base(projectId)}/notebook-runs/${encodeURIComponent(runId)}`,
    init,
  );
}

export function listRenders(
  projectId: string,
  options: { limit?: number; cursor?: string } = {},
  init?: RequestInit,
): Promise<RenderCollection> {
  const query = new URLSearchParams();
  if (options.limit) query.set("limit", String(options.limit));
  if (options.cursor) query.set("cursor", options.cursor);
  const suffix = query.toString() ? `?${query}` : "";
  return collection(apiGet<RenderCollection>(`${base(projectId)}/renders${suffix}`, init), "renders");
}

export function listLegacySnapshots(
  projectId: string,
  init?: RequestInit,
): Promise<{ snapshots: LegacySnapshot[] }> {
  return collection(apiGet(`${base(projectId)}/renders/legacy`, init), "snapshots");
}

export function fetchRender(
  projectId: string,
  renderId: string,
  init?: RequestInit,
): Promise<RenderDetail> {
  return apiGet<RenderDetail>(
    `${base(projectId)}/renders/${encodeURIComponent(renderId)}`,
    init,
  );
}

// ---------------------------------------------------------------------------
// Epic 73 / 74 -- the Dossier: several frozen Renders and a narrative, one
// addressable, immutable-per-version document. The shapes are exactly what
// `server/core/dossiers.py` returns (`list_dossiers`, `get_dossier`).
// ---------------------------------------------------------------------------

/** Who wrote a narrative block (D3, 2026-09-02). The server reads a block
 *  stored before that date -- console-written by construction -- as `human`. */
export type DossierNarrativeAuthor = "human" | "model";

export interface DossierSummary {
  id: string;
  label: string;
  description: string | null;
  current_version_id: string | null;
  created_by: string;
  created_at: string | null;
  updated_at: string | null;
  current_version_number: number | null;
  render_count: number;
  narrative_count: number;
}

export interface DossierFigureProvenance {
  result_id: string;
  visualization_spec_version_id: string;
  runtime_build_id: string;
  renderer_build_id: string;
  rendered_at: string | null;
  /** The AI Path that produced the figure's Result, or null when the Result
   *  recorded none. Resolved by the server at read time, never stored. */
  ai_path: string | null;
}

export type DossierResolvedBlock =
  | {
      kind: "render";
      render_id: string;
      provenance: DossierFigureProvenance | null;
      provenance_missing: boolean;
    }
  | { kind: "narrative"; text: string; authored_by: DossierNarrativeAuthor };

export interface DossierVersion {
  id: string;
  version_number: number;
  label: string;
  description: string | null;
  blocks: Array<Record<string, unknown>>;
  content_hash: string;
  predecessor_version_id: string | null;
  created_by: string;
  created_at: string | null;
}

export interface DossierDetail {
  id: string;
  label: string;
  description: string | null;
  current_version_id: string | null;
  archived_at: string | null;
  created_by: string;
  created_at: string | null;
  updated_at: string | null;
  versions: DossierVersion[];
  current_resolved_blocks: DossierResolvedBlock[];
}

export function listDossiers(
  projectId: string,
  init?: RequestInit,
): Promise<{ dossiers: DossierSummary[] }> {
  return collection(apiGet(`${base(projectId)}/dossiers`, init), "dossiers");
}

export function fetchDossier(
  projectId: string,
  dossierId: string,
  init?: RequestInit,
): Promise<DossierDetail> {
  return apiGet<DossierDetail>(
    `${base(projectId)}/dossiers/${encodeURIComponent(dossierId)}`,
    init,
  );
}

// ---------------------------------------------------------------------------
// Story 50.7 -- Render Sharing. One revocable, expiring, audited grant to ONE
// immutable Render.
//
// NOTE WHAT IS ABSENT FROM `RenderShare`: a token, a URL, and any value from
// which either could be rebuilt. The delivery URL exists in exactly one place in
// this contract -- the 201 body of `createRenderShare` -- because the server
// stored only its HMAC and cannot produce it again. The retired listing returned
// `share_token` on every row, which turned every console screenshot and every
// support-ticket paste into a live public grant.
// ---------------------------------------------------------------------------

export interface RenderShare {
  share_id: string;
  render_id: string;
  state: "pending_confirmation" | "active" | "revoked" | "expired";
  expires_at: string;
  created_by: string;
  created_at: string;
  revoked_at: string | null;
  revoked_by: string | null;
  revoke_reason_code: string | null;
  bearer_exchanged: boolean;
  exchange_attempt_count: number;
  exchange_attempt_ceiling: number;
  granted_access_count: number;
  refused_access_count: number;
  /** The ceremony's own state, SEPARATE from the Share's: a link can expire
   *  because its lifetime ran out, a request because nobody looked at it. One
   *  word cannot carry both, so the server names them apart. `absent` is a row
   *  that predates the two-person rule. */
  confirmation_state: "absent" | "pending" | "expired" | "confirmed";
  confirmation_requested_by: string | null;
  confirmation_requested_at: string | null;
  confirmation_expires_at: string | null;
  confirmed_by: string | null;
  confirmed_at: string | null;
  /** Answered by the SERVER: whether this reader is a different person who holds
   *  the role. A screen that computed it would be computing an authority. */
  can_confirm: boolean;
  awaiting_your_own_request: boolean;
}

export interface RenderShareCollection {
  shares: RenderShare[];
  max_lifetime_days: number;
  confirmation_window_hours: number;
  external_sharing: "allowed" | "forbidden";
  external_sharing_decided_by: string | null;
  external_sharing_gesture: string;
}

/** The REQUEST. No link: none exists until a second role holder confirms. */
export interface RenderShareRequested {
  share_id: string;
  render_id: string;
  state: string;
  expires_at: string;
  confirmation_requested_by: string;
  confirmation_expires_at: string;
}

/** Returned ONCE, at confirmation. `delivery_url` is never re-derivable. */
export interface RenderShareConfirmed {
  share_id: string;
  render_id: string;
  state: string;
  expires_at: string;
  delivery_url: string;
  delivery_url_shown_once: boolean;
}

const shareBase = (projectId: string) =>
  `/api/projects/${encodeURIComponent(projectId)}/renders`;

export function listRenderShares(
  projectId: string,
  renderId: string,
  init?: RequestInit,
): Promise<RenderShareCollection> {
  return apiGet<RenderShareCollection>(
    `${shareBase(projectId)}/${encodeURIComponent(renderId)}/shares`,
    init,
  );
}

export function createRenderShare(
  projectId: string,
  renderId: string,
  expiresAt: string,
): Promise<RenderShareRequested> {
  return apiPost<RenderShareRequested>(
    `${shareBase(projectId)}/${encodeURIComponent(renderId)}/shares`,
    { expires_at: expiresAt },
  );
}

/** The second role holder authorizes the exit, and receives the only link. */
export function confirmRenderShare(
  projectId: string,
  shareId: string,
): Promise<RenderShareConfirmed> {
  return apiPost<RenderShareConfirmed>(
    `${shareBase(projectId)}/shares/${encodeURIComponent(shareId)}/confirmation`,
    {},
  );
}

export function revokeRenderShare(
  projectId: string,
  shareId: string,
): Promise<{ share_id: string; state: string }> {
  return apiDelete(`${shareBase(projectId)}/shares/${encodeURIComponent(shareId)}`);
}

export function fetchLegacyClassification(
  projectId: string,
  init?: RequestInit,
): Promise<LegacyClassification> {
  return apiGet<LegacyClassification>(`${base(projectId)}/artifact-migration`, init);
}

// ---------------------------------------------------------------------------
// LEGACY snapshot shares — revocation and history, and nothing else.
//
// These two calls do NOT live under `base()`: they are the retired `/api/rendus`
// routes, which take their scope as a QUERY PARAMETER rather than a path
// segment. They are typed here rather than left as bare `apiFetch` in a screen
// because a live public grant is exactly the thing that must not be revoked
// through hand-rolled request code.
//
// THERE IS NO CREATE. `POST /api/rendus/snapshots/{id}/share` answers 410 Gone
// (`render_shares_api.create_snapshot_share_gone`); a new share is made over one
// immutable Render, and its link is shown once. What remains reachable here is
// the half that must stay reachable: seeing which unexpiring legacy links exist,
// and switching one off.
// ---------------------------------------------------------------------------

/** One legacy share. NO token and NO url — the server stopped selecting both. */
export interface LegacySnapshotShare {
  id: string;
  snapshot_id: string;
  shared_at: string;
  shared_by: string | null;
  revoked_at: string | null;
}

const legacyScope = (projectId: string) =>
  `project_id=${encodeURIComponent(projectId)}`;

export function listLegacySnapshotShares(
  projectId: string,
  snapshotId: string,
  init?: RequestInit,
): Promise<{ shares: LegacySnapshotShare[]; legacy: boolean }> {
  return apiGet(
    `/api/rendus/snapshots/${encodeURIComponent(snapshotId)}/shares?${legacyScope(projectId)}`,
    init,
  );
}

export function revokeLegacySnapshotShare(
  projectId: string,
  shareId: string,
): Promise<unknown> {
  return apiDelete(
    `/api/rendus/shares/${encodeURIComponent(shareId)}?${legacyScope(projectId)}`,
  );
}
