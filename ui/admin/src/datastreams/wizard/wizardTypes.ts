/**
 * Story 12.13 — shared types for the ONE source-first "Créer un flux" wizard.
 *
 * AD-2 / UX-DR11..17: source-agnostic. Every source path (connector, existing
 * BigQuery, managed feed CSV/Excel/Google Sheets) converges on the SAME
 * versioned mapping / preview / validation / activation contract. These types
 * describe the wizard's local draft state (never persisted here — Save draft
 * hits the governed server API) plus the shapes of the wired 12.1–12.10 routes
 * the stages drive.
 *
 * Nothing in this file is hardcoded per connector: labels are copy, module /
 * report / field identifiers are data returned by the server.
 */

// ---------------------------------------------------------------------------
// Source kinds + the six wizard stages (UX-DR11).
// ---------------------------------------------------------------------------

/** The three typed source/ownership modes of Story 12.2. */
export type SourceKind = "connector_pull" | "external_bq" | "managed_feed";

/** Managed-feed sub-formats (12.8/12.9 file import, 12.10 recurring Sheets). */
export type ManagedFeedFormat = "csv" | "excel" | "google_sheets";

/** The six ordered stages (revisitable). */
export type WizardStage =
  | "source"
  | "configure"
  | "destination"
  | "classify"
  | "preview"
  | "schedule";

export const WIZARD_STAGES: WizardStage[] = [
  "source",
  "configure",
  "destination",
  "classify",
  "preview",
  "schedule",
];

export const STAGE_LABELS: Record<WizardStage, string> = {
  source: "Source",
  configure: "Configure",
  destination: "Destination",
  classify: "Classify & map",
  preview: "Preview & validate",
  schedule: "Schedule & activate",
};

/**
 * Ownership + write behaviour per source kind, stated plainly in French so the
 * operator understands the data impact BEFORE configuring (UX-DR12/AC2).
 * `writer` is DERIVED from source kind (12.2 AC1), never a client override.
 */
export interface SourceKindMeta {
  kind: SourceKind;
  label: string;
  /** Who owns the written destination. */
  writer: "toorow" | "external";
  /** The destination policy this source path implies (12.2). */
  destinationPolicy: "managed_raw" | "external_read_only";
  /** Plain-language ownership + write-behaviour explanation. */
  ownership: string;
  writeBehavior: string;
}

// ---------------------------------------------------------------------------
// Connector capability catalog (GET /api/source-capabilities) — 12.1.
// A source-agnostic subset of the normalized capability response the Configure
// stage searches over.
// ---------------------------------------------------------------------------

export interface CapabilityField {
  field_id: string;
  source_field: string;
  kind: "metric" | "dimension" | string;
  physical_type: string;
  description: string;
  semantic_hints: string[];
  canonical_target: string | null;
  aggregation: string | null;
  non_additive: boolean;
}

export interface CapabilityConstraint {
  reason_code: string;
  description: string;
  constraint: string;
  field_ids: string[];
}

export interface CapabilityReport {
  id: string;
  selection_mode: string;
  availability: { status: string; reason_code?: string; follow_up?: string };
  metrics: string[];
  dimensions: string[];
  supported_grains: string[][];
  compatibility: CapabilityConstraint[];
  quota_cost: { read_points: number; unit: string };
  cadence: { minimum_interval_minutes: number; supported_modes: string[] };
}

export interface SourceCapabilities {
  contract_version: string;
  project_id: string;
  connection_ref_id: string;
  module: { name: string; display_name: string; module_kind: string };
  fields: CapabilityField[];
  reports: CapabilityReport[];
}

/** A project/org-scoped provider account returned by GET /api/connections. */
export interface ConnectionSummary {
  id: string;
  connection_ref_id?: string;
  provider?: string;
  display_name?: string;
  nango_connection_id?: string;
  account_label?: string | null;
  account_state?: string | null;
  status?: string | null;
  enabled?: boolean;
  health?: {
    status: string;
    last_checked_at?: string | null;
    last_fetched_at?: string | null;
  } | null;
}

// ---------------------------------------------------------------------------
// Managed-feed CSV/Excel preview (POST /api/datastreams/{id}/imports/preview) —
// 12.9. Server returns the dataclass fields below.
// ---------------------------------------------------------------------------

export interface ImportPreviewColumn {
  name: string;
  physical_type?: string;
  sample_values?: string[];
}

export interface ImportPreview {
  format: string;
  encoding?: string | null;
  delimiter?: string | null;
  sheet_name?: string | null;
  columns: ImportPreviewColumn[];
  row_count: number;
  rejected_count: number;
  preview_rows: Record<string, unknown>[];
  content_hash: string;
}

// ---------------------------------------------------------------------------
// Classify & preview (12.3 profile + 12.4 projection) — the versioned plan the
// Preview & validate stage renders. Source or schema drift makes this obsolete.
// ---------------------------------------------------------------------------

export interface ClassifiedField {
  field_id: string;
  source_field?: string;
  /** Suggested semantic role (date / dimension / metric / identifier / …). */
  semantic_role: string;
  physical_type?: string;
  /** MDM binding + confidence (12.3). */
  mdm_binding?: string | null;
  mdm_confidence?: number | null;
  /** Sensitive-data classification state. */
  sensitive_state?: "none" | "detected" | "masked" | string;
  sample_values?: string[];
}

export interface ValidationPlan {
  /** Immutable version identifiers frozen for this validated plan. */
  plan_version_id: string | null;
  mapping_version_id: string | null;
  /** Hash of the normalized intent validated for this immutable plan. */
  intent_content_hash: string | null;
  /** Governed provider-catalog evidence used during validation. */
  capability_contract_version: string | null;
  capability_fingerprint: string | null;
  /** Effective full grain (ordered key columns). */
  full_grain: string[];
  interval: { start: string | null; end_exclusive: string | null } | null;
  timezone: string | null;
  /** Safe-KPI projection summary (12.4). */
  safe_kpi_projection: { name: string; expression?: string }[];
  /** Data-quality state + rejections. */
  dq: { total_unresolved: number; degraded: boolean } | null;
  rejected_count: number | null;
  classified_fields: ClassifiedField[];
  /** Blocking issues disable activation (AC1). */
  blocking_issues: { code: string; message: string }[];
}

// ---------------------------------------------------------------------------
// Cadence (12.2 / 12.10). Product-facing modes only.
// ---------------------------------------------------------------------------

export type CadenceMode = "manual" | "daily" | "hourly";

export interface ScheduleConfig {
  cadence_mode: CadenceMode;
  timezone: string;
  allow_hourly: boolean;
}

// ---------------------------------------------------------------------------
// The wizard's whole local draft.
// ---------------------------------------------------------------------------

export interface WizardDraft {
  /** Once created via Save draft, the server datastream/flow id. */
  datastreamId: string | null;
  name: string;
  sourceKind: SourceKind | null;
  managedFeedFormat: ManagedFeedFormat | null;
  /** Connector path: chosen connection + report + selected fields. */
  connectionRefId: string | null;
  reportId: string | null;
  selectedMetrics: string[];
  selectedDimensions: string[];
  grain: string[];
  /** external_bq path: the read-only table reference. */
  bigqueryTable: string | null;
  /** managed_feed CSV/Excel: last preview + its content hash. */
  importPreview: ImportPreview | null;
  /** managed_feed Google Sheets. */
  spreadsheetId: string | null;
  sheetRange: string | null;
  /**
   * managed_feed Google Sheets: the Google connection whose OAuth grant reads the
   * sheet. REQUIRED by the recurring-sync configure route (12.10); collected on
   * the Sheets sub-flow. Null until the operator picks a Google connection.
   */
  sheetsConnectionId: string | null;
  /** Destination policy (derived from source, editable label only). */
  destinationDataset: string;
  /** The validated plan (Preview & validate). */
  plan: ValidationPlan | null;
  /** The canonical intent signature observed at validation time (drift detection). */
  validatedIntentSignature: string | null;
  schedule: ScheduleConfig;
  timezone: string;
}

export function emptyDraft(): WizardDraft {
  return {
    datastreamId: null,
    name: "",
    sourceKind: null,
    managedFeedFormat: null,
    connectionRefId: null,
    reportId: null,
    selectedMetrics: [],
    selectedDimensions: [],
    grain: [],
    bigqueryTable: null,
    importPreview: null,
    spreadsheetId: null,
    sheetRange: null,
    sheetsConnectionId: null,
    destinationDataset: "",
    plan: null,
    validatedIntentSignature: null,
    schedule: { cadence_mode: "daily", timezone: "Europe/Paris", allow_hourly: false },
    timezone: "Europe/Paris",
  };
}

export const SOURCE_KIND_META: SourceKindMeta[] = [
  {
    kind: "connector_pull",
    label: "Connector",
    writer: "toorow",
    destinationPolicy: "managed_raw",
    ownership:
      "toorow owns the destination: extracted data lands in a toorow-managed dataset.",
    writeBehavior:
      "toorow reads the report on each run and writes versioned candidates. The source is never modified.",
  },
  {
    kind: "external_bq",
    label: "Existing BigQuery",
    writer: "external",
    destinationPolicy: "external_read_only",
    ownership:
      "You own the BigQuery table: toorow never owns or modifies it.",
    writeBehavior:
      "toorow reads the table only. No data is written or copied outside your BigQuery project.",
  },
  {
    kind: "managed_feed",
    label: "Managed feed (CSV, Excel, Google Sheets)",
    writer: "toorow",
    destinationPolicy: "managed_raw",
    ownership:
      "toorow owns the managed dataset: every import or synchronization creates an immutable candidate in a dedicated destination.",
    writeBehavior:
      "You provide a file or sheet; toorow writes a versioned candidate after validation. The original source is never modified.",
  },
];

export function sourceKindMeta(kind: SourceKind): SourceKindMeta {
  const found = SOURCE_KIND_META.find((m) => m.kind === kind);
  if (!found) throw new Error(`unknown source kind: ${kind}`);
  return found;
}

export const COMMON_TIMEZONES = ["UTC", "Europe/Paris", "Europe/London", "America/New_York"];
