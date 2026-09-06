/** Thin API client for the proposal-only Datastream setup wizard. */
import { apiFetch } from "../../lib/apiFetch";

export interface WizardApiConfig { apiBase: string; projectId: string; }
export interface ServerValidationIssue {
  code: string;
  path?: string;
  message: string;
  repair?: Record<string, unknown>;
  details?: Record<string, unknown>;
}
export class WizardApiError extends Error {
  readonly status: number; readonly code: string; readonly issues: ServerValidationIssue[];
  constructor(status: number, code: string, message: string, issues: ServerValidationIssue[] = []) {
    super(message);
    this.name = "WizardApiError";
    this.status = status; this.code = code; this.issues = issues;
  }
}
function jsonHeaders(idempotencyKey?: string): HeadersInit {
  return { "Content-Type": "application/json", ...(idempotencyKey ? { "Idempotency-Key": idempotencyKey } : {}) };
}
function fallbackMessage(status: number, fallback: string): string {
  if (status === 401 || status === 403) return "Access denied: your permissions do not allow this action.";
  if (status === 404) return "Resource not found or outside the active project.";
  if (status === 409) return "This request conflicts with a newer draft revision.";
  if (status === 422) return "Validation failed. Review the configuration and try again.";
  return `${fallback} (HTTP ${status}).`;
}
async function responseError(response: Response, fallback: string): Promise<WizardApiError> {
  let code = "unavailable";
  let message = fallbackMessage(response.status, fallback);
  let issues: ServerValidationIssue[] = [];
  try {
    const body = await response.json() as {
      code?: string; message?: string; issues?: ServerValidationIssue[]; errors?: ServerValidationIssue[];
    };
    code = body.code ?? code; message = body.message ?? message; issues = body.issues ?? body.errors ?? [];
  } catch { /* stable HTTP fallback */ }
  return new WizardApiError(response.status, code, message, issues);
}

export type ProposalItemStatus = "complete" | "missing" | "blocked" | "warning" | "not_applicable" | "needs_review";
export interface ProposalEvidenceRef {
  kind: "connector_contract" | "observed_metadata" | "project_setting" | "governance_preset" | "operator_input";
  object_type: string;
  object_id: string;
  version_id: string;
  fingerprint: string;
  observed_at: string;
}
/** A SEMANTIC owner reference — the eleven keys `ContentRouter.openOwner` resolves.
 *  The router builds the address; nothing composes one.
 *
 *  Story 57.4: the capability rows used to carry `route:
 *  "/projects/{id}/settings/capabilities"`, an address `parsePath` refuses on its
 *  first segment, rendered as a plain `<a href>`. The link was dead, and the server
 *  cannot fix it by composing a better string — it does not know the Organization. */
export interface ProposalOwnerReference {
  surface: string;
  workspace: string | null;
  section: string | null;
  global_surface: string | null;
  global_section: string | null;
  object_type: string | null;
  object_id: string | null;
  tab: string | null;
  action: string | null;
  version_id: string | null;
  evidence_id: string | null;
}
/** `owner_reference` is what the server sends; `route` survives only to render a
 *  payload compiled before story 57.4. No section composes an address any more — all
 *  ten it used to compose were project-rooted and `parsePath` refuses every one of
 *  them. `label` names the gesture when "Open X" is the wrong verb — a capability that
 *  is not active is `Propose`, never `Enable`. */
export interface ProposalOwnerLink {
  object: string;
  route?: string;
  label?: string;
  owner_reference?: ProposalOwnerReference;
}
/** The one shape a capability row publishes (story 57.4). `effect` is composed
 *  SERVER-SIDE from the draft's own evidence, one sentence per capability; a capability
 *  whose effect no evidence supports sends the stated absence rather than a sentence
 *  borrowed from another row. Optional because a deployment predating 57.4 sends
 *  neither, which the screen states as unknown — never as "no effect". */
export interface ProposalCapabilityValue {
  key: string;
  name?: string;
  label: string;
  state: string;
  effect?: string;
  effect_coverage?: string;
}
export interface PreconfigurationProposalItem {
  key: string;
  section: string;
  requirement: "required" | "recommended" | "optional" | "automatic";
  status: ProposalItemStatus;
  proposed_value: unknown;
  evidence_refs: ProposalEvidenceRef[];
  confidence: { level: string; rationale: string };
  coverage: { state: string };
  exceptions: Array<Record<string, unknown>>;
  blockers: Array<Record<string, unknown>>;
  warnings: Array<Record<string, unknown>>;
  owner_links: ProposalOwnerLink[];
  downstream_impact: string[];
  dependency_fingerprint: string;
}
export interface PreconfigurationProposalSection {
  key: string;
  status: ProposalItemStatus;
  dependency_fingerprint: string;
  items: PreconfigurationProposalItem[];
}
/** `resume_href` is EMPTY since story 57.4 and stays typed for wire compatibility: no
 *  address can carry a draft id today (`data › datastreams` declares no query
 *  contract), and the path composed before resolved nowhere. What resumes a draft is
 *  `draft_ref` + `first_incomplete_section`, and `resume_reference` names the screen. */
export interface DatastreamSetupDraft {
  draft_ref: string;
  project_ref: string;
  state: string;
  current_revision_ref: string;
  current_revision: number;
  current_proposal_ref: string | null;
  first_incomplete_section: string;
  invalidation_causes: Array<Record<string, unknown>>;
  resume_href: string;
  resume_reference?: ProposalOwnerReference;
  idempotent_replay: boolean;
  operator_input?: Record<string, unknown>;
}
export interface DatastreamPreconfigurationProposal {
  schema_version: string;
  draft_ref: string;
  draft_revision_ref: string;
  proposal_ref: string;
  dependency_fingerprint: string;
  proposal_token: string;
  content_hash: string;
  is_stale: boolean;
  invalidation_causes: Array<Record<string, unknown>>;
  sections: PreconfigurationProposalSection[];
  section_fingerprints?: Record<string, string>;
  dependency_snapshot?: Record<string, unknown>;
  confirmed_intent_bundle?: {
    datastream_name?: string; data_role?: string; joint_grain?: string[];
    field_mappings?: Array<Record<string, unknown>>; business_domain_ids?: string[];
    capability_effects?: Array<Record<string, unknown>>; dq_gates?: Array<Record<string, unknown>>;
    processing?: Record<string, unknown>; outputs?: Array<Record<string, unknown>>;
    exceptions?: Array<Record<string, unknown>>; owner_proposals?: Array<Record<string, unknown>>;
  };
  configuration_summary: {
    existing: Array<Record<string, unknown>>; will_be_created: Array<Record<string, unknown>>;
    will_remain_a_proposal: Array<Record<string, unknown>>;
    downstream_impact: Array<Record<string, unknown> | string>;
  };
  resume_href: string;
  resume_reference?: ProposalOwnerReference;
  idempotent_replay: boolean;
}
/** `opens` answers the step's own first question: which Connector this scope can
 *  serve. One Google consent opens ten of them, so `connector_ref` -- the
 *  authorization's provider, the bare string "google" -- names no tool at all. */
export interface DatastreamSetupConnectorOpened {
  connector_name: string;
  display_name: string;
  available: boolean;
  reason?: string;
}
/** `external_object_ref` is the scope's own name when that name IS a
 *  `project.dataset.table` reference — which for BigQuery it always is, because
 *  only a LEAF of the discovery walk carries an id. It exists so the wizard can
 *  read the object instead of asking for it a second time.
 *
 *  `truncated` is the other half, and without it the first would lie: the walk
 *  lists at most `listing_bound` objects per dataset, so a dataset that reached
 *  the bound hides objects. True reopens free entry and says why; false leaves a
 *  read-only prefill. All four are optional: a deployment predating story 57.1
 *  sends none of them, which reads as "unknown", never as "complete". */
/** `connection_ref` is THE AUTHORIZATION this scope belongs to, and it is what
 *  makes step 1's second question askable. The ratified document held that the
 *  question could not be asked "until the source-options payload carries an
 *  authorization reference per account"; measured 2026-08-10 against a real
 *  Postgres, it does — `data_surface.py` selects `cr.id AS connection_ref_id`
 *  and `project_source_account` puts it here, so two Google consents in one
 *  Project arrive as two distinct ids and every account carries one. It is the
 *  same id `POST /api/connections/{id}/backfill` takes.
 *
 *  Optional, because a deployment predating that column sends none — which the
 *  screen states as an authorization it cannot name, never as one authorization
 *  shared by all. `authorization_ref` is what it is CALLED: a scope, a kind and
 *  the owning organization, and no id — which is why the two coexist. */
export interface DatastreamSetupAuthorizationRef {
  object_type?: string;
  owner_scope?: string;
  kind?: string;
  owner_org_name?: string | null;
}
export interface DatastreamSetupSourceAccount {
  object_ref: { id: string };
  connector_ref: { id: string };
  connection_ref?: { object_type?: string; id: string } | null;
  authorization_ref?: DatastreamSetupAuthorizationRef | null;
  discovered_for_connector?: string | null;
  opens?: DatastreamSetupConnectorOpened[];
  label: string;
  states: { availability: string; [key: string]: string };
  external_object_ref?: string | null;
  truncated?: boolean;
  listed_objects?: number;
  listing_bound?: number;
}
/** Does this Source Account serve this Connector? ONE predicate, every caller.
 *
 *  Every place that asked wrote `account.connector_ref.id === connectorId`, which
 *  compares the AUTHORIZATION'S PROVIDER to a Connector id. A Google direct grant
 *  is stored `provider='google'` and opens ten Connectors, so that test is false
 *  for all ten — `"google" === "google-sheets"` — and each channel that copied it
 *  showed an empty list with nothing saying why. On a Nango connection the two
 *  spellings coincide, which is why the comparison looks correct wherever it is
 *  exercised.
 *
 *  `opens` carries the exact answer (`connectors_opened_by_account`, server side).
 *  The provider is still accepted because on a Nango connection it IS the
 *  Connector id. Written once: a fix made in the branch that revealed it is a fix
 *  to make ten times. */
export function accountServesConnector(account: DatastreamSetupSourceAccount, ...connectorNames: string[]): boolean {
  if (connectorNames.includes(account.connector_ref?.id)) return true;
  return (account.opens ?? []).some((entry) => entry.available && connectorNames.includes(entry.connector_name));
}
/** The safety verdict of `recommend_first_report` (story 36.8), REPEATED, never
 *  re-decided. `recommended` is the single safe report family of the contract,
 *  `needs_choice` one of several the contract cannot choose between, and
 *  `no_safe_recommendation` one the engine did not retain — which is an answer,
 *  not a fault. */
export interface DatastreamSetupReportSafety {
  outcome: "recommended" | "needs_choice" | "no_safe_recommendation";
  reason: string;
}
/** `smallest_declared_grain` and `safety` are optional because a deployment
 *  predating story 57.6 sends neither. Absent, they render as a stated absence;
 *  a screen that read `undefined` as "safe" would be the one fabrication the
 *  safety engine exists to prevent. */
export interface DatastreamSetupReportOption {
  report_ref: string;
  display_name: string;
  availability?: Record<string, unknown> | null;
  metrics: string[];
  dimensions: string[];
  supported_grains?: string[][] | null;
  smallest_declared_grain?: string[] | null;
  safety?: DatastreamSetupReportSafety | null;
  history?: unknown;
  cadence?: unknown;
  quota_cost?: unknown;
}
export interface DatastreamSetupFieldOption {
  field_id: string;
  kind?: string;
  description?: string;
  physical_type?: string;
}
/** `source_category` is the connector manifest's `public_catalog.category`, read
 *  server-side and shown READ-ONLY: it describes the product, not the Datastream.
 *  Optional because a deployment that predates story 57.10 does not send it, and
 *  because a manifest may carry none — both cases render as a stated absence, not
 *  as a guessed category. `source_category_origin` is its evidence source. */
/** THE CATALOGUE IS THE MODULE REGISTRY (AI-279). Every entry is a Connector
 *  module this deployment holds, and its reports and fields are what that module
 *  DECLARES today. Nothing narrows the list — not an installation row, not what
 *  the Project's authorizations already open — because a person who has
 *  authorized nothing yet still has to see the Connector they came to add.
 *
 *  `contract_state` is then about the PIN, the contract a binding committed to,
 *  recorded at the moment the Connector was bound:
 *
 *    `unverified`  — nothing has bound this Connector here, so nothing is
 *                    pinned. Selectable: the binding is what writes the pin.
 *    `verified`    — a pin exists and the module still declares that contract;
 *    `stale`       — a pin exists and the module has CHANGED under it. Still
 *                    selectable: what is listed is current. What it warns about
 *                    is the Datastreams built on the older contract.
 *
 *  Absent, it reads `verified`. `contract_version_ref` is "" while nothing is
 *  pinned, and `observed_at` is null for the same reason — a date invented for a
 *  manifest would claim an observation that never happened. */
export interface DatastreamSetupConnectorOption {
  connector_ref: string;
  contract_version_ref: string;
  contract_fingerprint: string;
  display_name: string;
  observed_at: string | null;
  contract_state?: "verified" | "unverified" | "stale";
  /** Which door opens this Connector, from the manifest's `auth_type`. The step
   *  that demands an authorization has to be able to OFFER the gesture that
   *  makes one, and Google and Nango are not the same gesture. */
  authorization_kind?: string;
  /** The wizard modes this Connector can be reached through, from its manifest.
   *  The mode question narrows the card grid with it. */
  onboarding_modes?: string[];
  source_category?: string | null;
  source_category_origin?: string | null;
  reports: DatastreamSetupReportOption[];
  fields: DatastreamSetupFieldOption[];
}
/** `domain` is the VERIFIED inbound domain of this deployment, read in the same
 *  query that decides `availability` (57.3). It is the domain, never an address:
 *  an address is a secret minted against a Datastream that does not exist yet. */
export interface DatastreamSetupChannelOption {
  channel: "file_upload" | "google_sheets" | "inbound_email" | "webhook";
  availability: string;
  template_ref: string | null;
  display_name?: string;
  domain?: string | null;
}
/** Step 1's `Recommended` (target `:48`): best account and report family, with
 *  its evidence. Derived from the persisted contract by the story 36.8 engine —
 *  never a hand-written preset catalogue, which is why a connector that changes
 *  its contract cannot leave a card promising a report it no longer serves. */
export interface DatastreamSetupRecommendation {
  recommendation_ref: string;
  confidence: { level: "high" | "medium"; rationale: string };
  source_account_ref: string;
  source_account_label: string;
  connector_ref: string;
  connector_display_name: string;
  connector_contract_version_ref: string;
  report_ref: string;
  report_display_name: string;
  derived_grain: string[];
  metric_count: number;
  dimension_count: number;
  currency: string;
  estimated_cost: { read_points?: number; unit?: string; window_days?: number; quota_pre_check?: string } | null;
  evidence_refs: ProposalEvidenceRef[];
}
/** The window and the offset a Datastream gets when nobody states one. They are
 *  COLUMN DEFAULTS of `app.datastreams`, not a Connector declaration — none of
 *  the 133 report profiles carries either — so they arrive with `origin` and the
 *  screen labels them with it. Optional: a deployment predating story 57.6 sends
 *  none, and the card then says nothing rather than printing a number. */
export interface DatastreamSetupPlatformDefaults {
  date_window_days: number;
  window_offset_days: number;
  origin: string;
}
export interface DatastreamSetupSourceOptions {
  draft_ref: string;
  project_ref: string;
  source_accounts: DatastreamSetupSourceAccount[];
  connectors: DatastreamSetupConnectorOption[];
  managed_channels: DatastreamSetupChannelOption[];
  external_access: DatastreamSetupSourceAccount[];
  recommendations?: DatastreamSetupRecommendation[];
  platform_defaults?: DatastreamSetupPlatformDefaults | null;
}
export interface DatastreamSetupAsset {
  asset_ref: string;
  draft_ref: string;
  content_hash: string;
  detected_format: string;
  byte_count: number;
  state: string;
  expires_at: string;
  cleanup_owner: string;
  idempotent_replay: boolean;
}
/** EXACTLY the seven keys `_safe_field` lets through, and no eighth: anything
 *  else an adapter emits is dropped by the normalizer, so typing it here would
 *  promise a field the screen can never receive. No example value appears in
 *  this list, and that is the decision — a row value is refused upstream, and
 *  the masked sample belongs to the preview step. */
export interface DatastreamSetupObservedField {
  name?: string;
  field_id?: string;
  type?: string;
  kind?: string;
  nullable?: boolean;
  mode?: string;
  description?: string;
}
/** An ADDRESSABLE OBJECT the discovery saw — a tab of a workbook (57.2), an
 *  object of a channel (57.3). Exactly the two keys `_safe_object` lets through:
 *  `object_ref` is what the source reference becomes when it is picked, `label`
 *  is what a person reads. The list is bounded server-side and says so through
 *  `coverage.object_list`, so a screen must never present it as complete. */
export interface DatastreamSetupObservedObject { object_ref: string; label: string; }
/** `fields` is optional: a deployment whose adapter pair is still uncovered
 *  sends none, which is an emptiness to state, not an empty table to draw. */
export interface DatastreamSetupObservation {
  observation_ref: string;
  draft_ref: string;
  draft_revision_ref: string;
  draft_revision: number;
  mode: "connector_pull" | "external_bq" | "managed_feed";
  discovery_kind: string;
  adapter_ref: string;
  connector_contract_version_ref: string | null;
  request_fingerprint: string;
  evidence_fingerprint: string;
  schema_hash: string | null;
  safe_metadata: {
    fields?: DatastreamSetupObservedField[]; objects?: DatastreamSetupObservedObject[];
    location?: unknown; watermark?: unknown; freshness?: unknown; quota_cost?: unknown;
    [key: string]: unknown;
  };
  coverage: Record<string, string>;
  exceptions: Array<{ code: string }>;
  observed_at: string;
  expires_at: string | null;
  idempotent_replay: boolean;
}
export interface DatastreamSetupPreview {
  preview_ref: string;
  draft_revision_ref: string;
  proposal_ref: string;
  observation_ref: string;
  mode: "connector_pull" | "external_bq" | "managed_feed";
  dependency_hash: string;
  evidence_hash: string;
  safe_evidence: { sample?: Array<Record<string, unknown>>; status?: string; [key: string]: unknown };
  status: "ready_for_review" | "blocked";
  is_stale: boolean;
}
export interface DatastreamSetupPreviewJob {
  job_ref?: string;
  job_id?: string;
  state: "queued" | "running" | "done" | "failed" | "dead_letter";
  error_code?: string | null;
  preview?: DatastreamSetupPreview;
}
export interface DatastreamFinalReview {
  final_review_ref: string;
  content_hash: string;
  proposal_ref: string;
  preview_ref: string;
  acknowledged_warning_ids: string[];
  confirmed_intent_bundle: Record<string, unknown>;
  schedule: Record<string, unknown>;
  is_stale?: boolean;
  idempotent_replay: boolean;
}
export interface DatastreamConfirmation {
  confirmation_ref: string;
  confirmation_secret: string;
  command: string;
  expires_at: string;
  review_hash?: string;
  content_hash?: string;
}
export interface DatastreamMaterialization {
  operation_ref: string;
  outcome: string;
  materialization_id: string;
  datastream_id: string;
  lifecycle_state: "draft";
  plan_version_id: string;
  mapping_version_id: string;
  candidate_execution_id: string;
  current_published_execution_id: null;
  schedule_active: false;
  candidate_job_id: string;
}
export interface DatastreamMaterializationStatus {
  state: "not_materialized" | "materialized";
  datastream_ref?: string;
  candidate_execution_ref?: string;
  candidate_state?: string;
  job_state?: string;
  lifecycle_state?: string;
  current_published_execution_ref?: string | null;
}

/** The project's REPORTING TIMEZONE, read for the wizard's Schedule step
 *  (57.12, T5). The field there used to render the literal string "Project
 *  Settings timezone" — the NAME of the place instead of the value.
 *  `GET /api/projects/{id}/settings` is the project-scoped read that exists
 *  before any Datastream does, and its `defaults.reporting_timezone.active`
 *  comes from the active Configuration Version — the SAME authority the final
 *  review reads (`datastream_preconfiguration_api.py`, story 48.3), so the
 *  step and the schedule can never disagree. `active` null means no timezone
 *  is confirmed: the schedule then runs on UTC, labelled as a default, and the
 *  field says so rather than printing UTC as if it were chosen.
 *
 *  The read is narrower than the envelope: the wizard needs one default, and
 *  typing the whole settings payload here would promise keys nobody checks. */
export interface ProjectReportingTimezone {
  active: string | null;
  origin: string | null;
  confirmation_status: string | null;
}
export async function readProjectReportingTimezone(cfg: WizardApiConfig): Promise<ProjectReportingTimezone | null> {
  const body = await jsonResponse<{ defaults?: { reporting_timezone?: ProjectReportingTimezone } }>(
    await apiFetch(`${cfg.apiBase}/api/projects/${encodeURIComponent(cfg.projectId)}/settings`, {
      method: "GET",
      cache: "no-store",
    }),
    "Project settings are unavailable",
  );
  return body.defaults?.reporting_timezone ?? null;
}

/** An organization's saved configuration (57.7). `operator_input` is a
 *  `normalized_operator_input` with three keys already removed server-side —
 *  `observation_ref`, `source.staged_asset_ref`, `wizard_state` — so applying
 *  one is a PATCH on the draft, never a translation between two shapes.
 *
 *  `open_variables` is the SERVER'S declaration of what the payload does not
 *  carry, derived from the measured scope of each reference. The screen names
 *  those before the click instead of letting the operator discover an empty
 *  field afterwards. `origin_project_ref` is what makes the second difference
 *  legible: a template saved in another Project of the same organization
 *  applies here, and the card says so rather than staying silent.
 *
 *  `connector_ref` and `report_ref` are nullable because two of the three modes
 *  name no connector at all. `origin_contract_version_ref` is PROVENANCE and
 *  never a pin: zero contract versions exist in this deployment, so a template
 *  pinning one would be born stale — the contract is re-resolved from
 *  `source-options` when the template is applied. */
export interface DatastreamSetupTemplate {
  template_ref: string;
  label: string;
  org_ref?: string;
  origin_project_ref: string;
  origin_datastream_ref?: string;
  mode: "connector_pull" | "external_bq" | "managed_feed";
  connector_ref?: string | null;
  report_ref?: string | null;
  origin_contract_version_ref?: string | null;
  origin_source_account_ref?: string | null;
  operator_input: Record<string, unknown>;
  open_variables?: string[];
  content_hash?: string;
  created_by?: string;
  created_at?: string;
}
/** `count` and `limit` are READ, never counted on screen: the refusal at the
 *  eleventh is the server's, and a screen that computed its own count would
 *  disagree with it the day another operator saves one in a second window. */
export interface DatastreamSetupTemplateList { templates: DatastreamSetupTemplate[]; count: number; limit: number }

function setupTemplatePath(cfg: WizardApiConfig, templateRef?: string): string {
  const root = `${cfg.apiBase}/api/projects/${encodeURIComponent(cfg.projectId)}/datastream-setup-templates`;
  return templateRef ? `${root}/${encodeURIComponent(templateRef)}` : root;
}
export async function listDatastreamSetupTemplates(cfg: WizardApiConfig): Promise<DatastreamSetupTemplateList> {
  return jsonResponse(
    await apiFetch(setupTemplatePath(cfg), { method: "GET", cache: "no-store" }),
    "Saved configurations are unavailable",
  );
}
export async function saveDatastreamSetupTemplate(
  cfg: WizardApiConfig,
  datastreamId: string,
  label: string,
  key: string,
): Promise<DatastreamSetupTemplate> {
  return jsonResponse(
    await apiFetch(setupTemplatePath(cfg), {
      method: "POST",
      headers: jsonHeaders(key),
      body: JSON.stringify({ datastream_id: datastreamId, label }),
      cache: "no-store",
    }),
    "This configuration could not be saved",
  );
}
/** NO CALLER, AND THAT IS SAID PLAINLY (57.12, T9): the endpoint is real
 *  (`DELETE …/datastream-setup-templates/{ref}`, retirement is reversible, a
 *  template is never destroyed) but no screen offers the gesture yet — the
 *  wizard saves templates, nothing retires them. The function stays because
 *  the door is a product decision, not because the code earns its keep: the
 *  day a retire affordance lands, the client is already the server's words.
 *  Deleting it would only re-add it unchanged later. */
export async function retireDatastreamSetupTemplate(
  cfg: WizardApiConfig,
  templateRef: string,
): Promise<{ template_ref: string; is_active: boolean }> {
  return jsonResponse(
    await apiFetch(setupTemplatePath(cfg, templateRef), { method: "DELETE", cache: "no-store" }),
    "This configuration could not be retired",
  );
}

function setupDraftPath(cfg: WizardApiConfig, draftId?: string): string {
  const root = `${cfg.apiBase}/api/projects/${encodeURIComponent(cfg.projectId)}/datastream-setup-drafts`;
  return draftId ? `${root}/${encodeURIComponent(draftId)}` : root;
}
async function jsonResponse<T>(response: Response, fallback: string): Promise<T> {
  if (!response.ok) throw await responseError(response, fallback);
  return await response.json() as T;
}
/** One row of `GET …/datastream-setup-drafts` — the list answer (57.12, T7).
 *  EXACTLY what `_draft_payload` puts on the wire, no more: `updated_at` is
 *  ORDERED BY in the query but never selected, so no "last touched" line can
 *  be drawn from this payload and none is drawn. Resumable means `state` is
 *  `draft`; the CHECK constraint's other two (`materialized`, `archived`) are
 *  terminal, and the partial unique index makes ONE resumable draft per Project
 *  the most there can be. `exited` was a fourth declared state that nothing ever
 *  wrote — retired in migration 284. */
export interface DatastreamSetupDraftListItem {
  draft_ref: string;
  project_ref: string;
  state: string;
  current_revision_ref: string;
  current_revision: number;
  current_proposal_ref: string | null;
  first_incomplete_section: string;
  invalidation_causes: Array<Record<string, unknown>>;
  resume_href: string;
  resume_reference?: ProposalOwnerReference;
  idempotent_replay: boolean;
}
export async function listDatastreamSetupDrafts(cfg: WizardApiConfig): Promise<DatastreamSetupDraftListItem[]> {
  const body = await jsonResponse<{ drafts?: DatastreamSetupDraftListItem[] }>(
    await apiFetch(setupDraftPath(cfg), { method: "GET", cache: "no-store" }),
    "Setup drafts are unavailable",
  );
  return body.drafts ?? [];
}
export async function readDatastreamSetupDraft(cfg: WizardApiConfig, draftId: string): Promise<DatastreamSetupDraft> {
  return jsonResponse(
    await apiFetch(setupDraftPath(cfg, draftId), { method: "GET", cache: "no-store" }),
    "Setup draft is unavailable",
  );
}
export async function createDatastreamSetupDraft(cfg: WizardApiConfig, key: string): Promise<DatastreamSetupDraft> {
  return jsonResponse(
    await apiFetch(setupDraftPath(cfg), { method: "POST", headers: jsonHeaders(key), body: "{}", cache: "no-store" }),
    "Setup draft could not be created",
  );
}
/** `Discard this draft` — the end of a draft, ratified by Jean 2026-08-31 (AI-336).
 *
 *  `DELETE` on the draft resource, and a SOFT ARCHIVE behind it: the server writes
 *  `state = 'archived'`, the row and its revisions stay, and what ends is the
 *  resume. The same verb, the same shape and the same soft outcome as
 *  `DELETE /api/datastreams/{id}` — one verb must not mean two things on one
 *  surface. No `Idempotency-Key`: discarding twice is the same discard, answered
 *  as a replay. */
export async function discardDatastreamSetupDraft(
  cfg: WizardApiConfig,
  draftId: string,
): Promise<DatastreamSetupDraftListItem> {
  return jsonResponse(
    await apiFetch(setupDraftPath(cfg, draftId), { method: "DELETE", cache: "no-store" }),
    "Setup draft could not be discarded",
  );
}
export async function updateDatastreamSetupDraft(
  cfg: WizardApiConfig,
  draftId: string,
  expectedRevision: number,
  operatorInput: Record<string, unknown>,
  key: string,
  changeReason = "autosave",
): Promise<DatastreamSetupDraft> {
  return jsonResponse(
    await apiFetch(setupDraftPath(cfg, draftId), {
      method: "PATCH",
      headers: jsonHeaders(key),
      body: JSON.stringify({
        expected_revision: expectedRevision, operator_input: operatorInput, change_reason: changeReason,
      }),
      cache: "no-store",
    }),
    "Setup draft could not be saved",
  );
}
export async function compileDatastreamSetupDraft(
  cfg: WizardApiConfig,
  draftId: string,
  expectedRevision: number,
  key: string,
): Promise<DatastreamPreconfigurationProposal> {
  return jsonResponse(
    await apiFetch(`${setupDraftPath(cfg, draftId)}/compile`, {
      method: "POST",
      headers: jsonHeaders(key),
      body: JSON.stringify({ expected_revision: expectedRevision }),
      cache: "no-store",
    }),
    "Proposal could not be compiled",
  );
}
export async function readDatastreamPreconfigurationProposal(
  cfg: WizardApiConfig,
  draftId: string,
  proposalId: string,
): Promise<DatastreamPreconfigurationProposal> {
  return jsonResponse(
    await apiFetch(`${setupDraftPath(cfg, draftId)}/proposals/${encodeURIComponent(proposalId)}`, {
      method: "GET",
      cache: "no-store",
    }),
    "Proposal is unavailable",
  );
}
export async function readDatastreamSetupSourceOptions(
  cfg: WizardApiConfig,
  draftId: string,
): Promise<DatastreamSetupSourceOptions> {
  return jsonResponse(
    await apiFetch(`${setupDraftPath(cfg, draftId)}/source-options`, { method: "GET", cache: "no-store" }),
    "Source options are unavailable",
  );
}
export async function stageDatastreamSetupAsset(
  cfg: WizardApiConfig,
  draftId: string,
  file: File,
  key: string,
): Promise<DatastreamSetupAsset> {
  return jsonResponse(
    await apiFetch(`${setupDraftPath(cfg, draftId)}/assets`, {
      method: "POST",
      headers: {
        "Content-Type": file.type || "application/octet-stream", "X-File-Name": file.name, "Idempotency-Key": key,
      },
      body: file,
      cache: "no-store",
    }),
    "File staging is unavailable",
  );
}
export async function createDatastreamSetupObservation(
  cfg: WizardApiConfig,
  draftId: string,
  request: Record<string, unknown>,
  key: string,
): Promise<DatastreamSetupObservation> {
  return jsonResponse(
    await apiFetch(`${setupDraftPath(cfg, draftId)}/observations`, {
      method: "POST",
      headers: jsonHeaders(key),
      body: JSON.stringify(request),
      cache: "no-store",
    }),
    "Source discovery is unavailable",
  );
}
export async function createDatastreamSetupPreview(
  cfg: WizardApiConfig,
  draftId: string,
  expectedRevision: number,
  proposalRef: string,
  observationRef: string,
  key: string,
): Promise<DatastreamSetupPreviewJob> {
  return jsonResponse(
    await apiFetch(`${setupDraftPath(cfg, draftId)}/previews`, {
      method: "POST",
      headers: jsonHeaders(key),
      body: JSON.stringify({
        expected_revision: expectedRevision, proposal_ref: proposalRef, observation_ref: observationRef,
      }),
      cache: "no-store",
    }),
    "Preview could not be queued",
  );
}
export async function readDatastreamSetupPreviewJob(
  cfg: WizardApiConfig,
  draftId: string,
  jobId: string,
): Promise<DatastreamSetupPreviewJob> {
  return jsonResponse(
    await apiFetch(`${setupDraftPath(cfg, draftId)}/preview-jobs/${encodeURIComponent(jobId)}`, {
      method: "GET",
      cache: "no-store",
    }),
    "Preview status is unavailable",
  );
}
export async function prepareDatastreamFinalReview(
  cfg: WizardApiConfig,
  draftId: string,
  previewRef: string,
  acknowledgedWarningIds: string[],
  key: string,
): Promise<DatastreamFinalReview> {
  return jsonResponse(
    await apiFetch(`${setupDraftPath(cfg, draftId)}/final-reviews`, {
      method: "POST",
      headers: jsonHeaders(key),
      body: JSON.stringify({ preview_ref: previewRef, acknowledged_warning_ids: acknowledgedWarningIds }),
      cache: "no-store",
    }),
    "Final review could not be prepared",
  );
}
export async function prepareDatastreamDraftConfirmation(
  cfg: WizardApiConfig,
  draftId: string,
  finalReviewRef: string,
  key: string,
): Promise<DatastreamConfirmation> {
  return jsonResponse(
    await apiFetch(`${setupDraftPath(cfg, draftId)}/draft-confirmations`, {
      method: "POST",
      headers: jsonHeaders(key),
      body: JSON.stringify({ final_review_ref: finalReviewRef }),
      cache: "no-store",
    }),
    "Draft confirmation could not be issued",
  );
}
export async function confirmDatastreamDraft(
  cfg: WizardApiConfig,
  draftId: string,
  finalReviewRef: string,
  confirmation: DatastreamConfirmation,
  key: string,
): Promise<DatastreamMaterialization> {
  return jsonResponse(
    await apiFetch(`${setupDraftPath(cfg, draftId)}/materialize`, {
      method: "POST",
      headers: jsonHeaders(key),
      body: JSON.stringify({
        final_review_ref: finalReviewRef,
        confirmation_ref: confirmation.confirmation_ref,
        confirmation_secret: confirmation.confirmation_secret,
      }),
      cache: "no-store",
    }),
    "Datastream Draft could not be created",
  );
}
export async function readDatastreamMaterialization(
  cfg: WizardApiConfig,
  draftId: string,
): Promise<DatastreamMaterializationStatus> {
  return jsonResponse(
    await apiFetch(`${setupDraftPath(cfg, draftId)}/materialization`, { method: "GET", cache: "no-store" }),
    "Materialization status is unavailable",
  );
}
export async function readDatastreamSetupObservation(
  cfg: WizardApiConfig,
  draftId: string,
  observationId: string,
): Promise<DatastreamSetupObservation> {
  return jsonResponse(
    await apiFetch(`${setupDraftPath(cfg, draftId)}/observations/${encodeURIComponent(observationId)}`, {
      method: "GET",
      cache: "no-store",
    }),
    "Source observation is unavailable",
  );
}

/** The LIVE WAREHOUSE BROWSE behind the external-BigQuery object question
 *  (57.12, T2b). This is the door 57.1 D2 ratified — the same
 *  `GET /api/connections/{id}/accounts` the Sources page walks — not a second
 *  route opened for the wizard. The answer is the module's own tree; for
 *  BigQuery it is project → dataset → table, only a LEAF carries an `id` (the
 *  `project.dataset.table` reference the draft persists), and a dataset that
 *  reached the listing bound carries `truncated: true` (bounds 50 projects /
 *  200 datasets / 500 tables, `server/modules/bigquery/connector.py`).
 *
 *  THE `?connector=` PARAM IS REQUIRED, and it is required for the reason the
 *  opposite used to be written here. This comment said: « NO `?connector=`
 *  PARAM, and that is measured — `resolve_connection_connector` REFUSES
 *  `bigquery` on a `google_direct` authorization by design, the BigQuery module
 *  declares `auth_type: "none"` and reads with the deployment's own
 *  credentials, so the connection's provider IS the resolution. » Every clause
 *  of that is now false. BigQuery reads through the PERSON's Google consent
 *  (`docs/product-architecture/datastream-workbench-and-wizard.md`, amended
 *  2026-08-11), its manifest declares `auth_path: "google_direct"`, and AI-285
 *  put `bigquery.readonly` in `connection_tools.GOOGLE_SCOPE_CONNECTORS`.
 *
 *  What the omission cost, measured 2026-08-16 against the schema:
 *
 *    resolve_connection_connector, consent carrying bigquery.readonly ALONE
 *      no ?connector=          -> bigquery       (one connector, so it guesses right)
 *      ?connector=bigquery     -> bigquery
 *    resolve_connection_connector, consent carrying bigquery + analytics
 *      no ?connector=          -> REFUSES        <- the production shape
 *      ?connector=bigquery     -> bigquery
 *
 *  The console asks for every Google scope on ONE consent screen, so the second
 *  row IS the real one. Unnamed, the endpoint keeps `module=None` and discovers
 *  against the row's own provider — `google`, which declares no topology
 *  (`account_topology.discover_accounts` says so in its own docstring) — and
 *  answers 409 `no_account_topology`. The walk then fell back to the free-text
 *  three-part reference without saying why, which is the empty list that names
 *  no gesture. Naming the tool is what makes the walk exist.
 *
 *  The route is connection-scoped, not project-scoped (project isolation is
 *  enforced server-side, AD-5), which is why it takes no draft and no project
 *  path. One measured side effect, accepted by 57.11 and said here rather
 *  than discovered: each call reconciles the exposed account set
 *  (`account_topology.reconcile_discovered_accounts`) — a GET that writes. */
export interface ConnectionAccountNode {
  id?: string;
  label: string;
  kind?: string;
  truncated?: boolean;
  table_type?: string;
  children?: ConnectionAccountNode[];
}
export interface ConnectionAccountsListing {
  connection_ref_id: string;
  topology: unknown;
  accounts: ConnectionAccountNode[];
}
export async function listConnectionAccounts(
  cfg: WizardApiConfig,
  connectionId: string,
  connector: string,
): Promise<ConnectionAccountsListing> {
  const query = `?connector=${encodeURIComponent(connector)}`;
  return jsonResponse(
    await apiFetch(
      `${cfg.apiBase}/api/connections/${encodeURIComponent(connectionId)}/accounts${query}`,
      { method: "GET", cache: "no-store" },
    ),
    "The live warehouse listing is unavailable",
  );
}

/** Name an entity the provider will not ENUMERATE, and let the product prove
 *  the access. `channels.list(mine=true)` returns one YouTube channel -- the
 *  consenting identity's -- while `reports.query` answers for every channel that
 *  identity manages, which is an agency's whole business. The server runs the
 *  Connector's own declared verification (`account_topology.verification`) and
 *  refuses when the provider refuses; there is no client-side trust here. */
export async function verifyConnectionAccount(
  connectionRefId: string,
  accountId: string,
  connector: string,
): Promise<{ ok: true } | { ok: false; message: string }> {
  const resp = await apiFetch(
    `/api/connections/${encodeURIComponent(connectionRefId)}/account`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ account_id: accountId, connector }),
      cache: "no-store",
    },
  );
  if (resp.ok) return { ok: true };
  const body = (await resp.json().catch(() => ({}))) as { message?: string };
  return { ok: false, message: body.message ?? `The provider refused (HTTP ${resp.status}).` };
}
