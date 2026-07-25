/**
 * Shared TypeScript contracts for the Datastreams OPERATIONAL surface v2
 * (Story 12.14). These types describe the shapes returned by the wave-4a
 * governed admin API routes:
 *
 *   GET  /api/datastreams/{id}/read-model         (12.14 read model)
 *   POST /api/datastreams/{id}/bounded/prepare     (12.11)
 *   POST /api/datastreams/{id}/bounded/confirm     (12.11)
 *   GET  /api/datastreams/{id}/rollback/preview    (12.12)
 *   POST /api/datastreams/{id}/rollback            (12.12)
 *   GET  /api/datastreams/{id}/append/availability (12.12)
 *   POST /api/datastreams/{id}/replace/preflight   (12.12)
 *   POST /api/datastreams/{id}/destination-policy  (12.12)
 *   GET  /api/datastreams/{id}/managed-feed/status (12.10)
 *
 * IMPORTANT (honesty rule): fields the read-model marks Phase-B / TODO (a
 * unified per-execution trace_id, a denormalised per-execution rejection_count)
 * are intentionally NOT declared as guaranteed. Where the API leaves them
 * absent, the UI shows "Non disponible", never a fabricated value.
 *
 * These interfaces are additive and live under ops/ so they do not collide with
 * the wizard tree (Story 12.13) nor with the Epic-8 datastreams/types.ts.
 */

// ---------------------------------------------------------------------------
// Read-model (GET /api/datastreams/{id}/read-model)
// ---------------------------------------------------------------------------

/** One entry from list_intent_versions (12.2). Loosely typed on purpose: the
 *  server owns the canonical column set; the UI reads a known subset. */
export interface PlanVersion {
  id: string;
  version_number?: number | null;
  executable?: boolean | null;
  writer_kind?: string | null;
  destination_policy?: string | null;
  created_at?: string | null;
  created_by?: string | null;
  reason?: string | null;
  [k: string]: unknown;
}

/** One entry from list_mapping_versions (12.3). */
export interface MappingVersion {
  id: string;
  version_number?: number | null;
  mapping_contract_version?: string | null;
  field_count?: number | null;
  created_at?: string | null;
  created_by?: string | null;
  [k: string]: unknown;
}

/** get_execution(pointer) — the LIVE published execution (12.5). */
export interface PublishedExecution {
  id: string;
  state?: string | null;
  state_changed_at?: string | null; // freshness signal
  content_hash?: string | null;
  row_count?: number | null;
  plan_version_id?: string | null;
  mapping_version_id?: string | null;
  error_code?: string | null;
  created_at?: string | null;
  // DQ state may be surfaced by the server as `dq_state` / nested `dq`;
  // both are optional — the UI shows "Non disponible" when absent.
  dq_state?: string | null;
  dq?: { state?: string | null; unresolved?: number | null } | null;
  [k: string]: unknown;
}

/** The newest non-terminal execution (candidate) — 12.5. */
export interface CandidateExecution {
  id: string;
  state?: string | null;
  state_changed_at?: string | null;
  content_hash?: string | null;
  row_count?: number | null;
  plan_version_id?: string | null;
  mapping_version_id?: string | null;
  error_code?: string | null;
  created_at?: string | null;
  [k: string]: unknown;
}

/** One publication_log row (12.5): carries actor + prior-execution evidence. */
export interface PublicationLogEntry {
  execution_id?: string | null;
  published_by?: string | null; // actor
  published_at?: string | null;
  prior_execution_id?: string | null; // rollback/replace evidence
  action?: string | null;
  reason?: string | null;
  rollback_deadline?: string | null; // added by 12.12 migration 081
  retained?: boolean | null;
  // A unified per-execution trace_id is PHASE B: absent here by design.
  trace_id?: string | null;
  [k: string]: unknown;
}

/** One managed_feed ledger row (row/rejection counts). */
export interface ImportLedgerRow {
  id?: string | null;
  execution_id?: string | null;
  status?: string | null;
  row_count?: number | null;
  rejection_count?: number | null; // present on ledger rows, not on the execution
  loaded_at?: string | null;
  date?: string | null;
  [k: string]: unknown;
}

/** GET /api/datastreams/{id}/read-model response. */
export interface DatastreamReadModel {
  datastream_id: string;
  project_id: string;
  plan_versions: PlanVersion[];
  mapping_versions: MappingVersion[];
  current_published_execution_id: string | null;
  published_execution: PublishedExecution | null;
  current_candidate: CandidateExecution | null;
  publication_log: PublicationLogEntry[];
  recent_imports: ImportLedgerRow[];
}

// ---------------------------------------------------------------------------
// 12.11 bounded recovery (prepare -> confirm)
// ---------------------------------------------------------------------------

export type BoundedRecoveryKind =
  | "sync" // Synchronize
  | "reload" // Reload
  | "reprocess"; // Reprocess

/** POST /bounded/prepare response — the AD-27 immutable proposal. */
export interface BoundedPreparation {
  preparation_id: string;
  kind: string;
  target?: Record<string, unknown> | null;
  target_versions?: Record<string, unknown> | null;
  interval?: { date_from?: string | null; date_to_exclusive?: string | null } | null;
  impact?: Record<string, unknown> | null;
  quota?: Record<string, unknown> | null;
  [k: string]: unknown;
}

/** POST /bounded/confirm response. */
export interface BoundedConfirmResult {
  preparation_id: string;
  operation_id?: string | null;
  outcome?: string | null;
  replayed?: boolean | null;
  result?: Record<string, unknown> | null;
  [k: string]: unknown;
}

// ---------------------------------------------------------------------------
// 12.12 dataset recovery (rollback / replace / append)
// ---------------------------------------------------------------------------

/** GET /rollback/preview response. */
export interface RollbackPreview {
  available: boolean;
  target_execution_id: string | null;
  current_execution_id?: string | null;
  rollback_deadline: string | null;
  deadline_source?: string | null;
  window_source?: string | null;
  expired: boolean;
  reason?: string | null;
}

/** POST /rollback response. */
export interface RollbackResult {
  already_at_target?: boolean;
  target_execution_id?: string | null;
  current_execution_id?: string | null;
  [k: string]: unknown;
}

/** GET /append/availability response. */
export interface AppendAvailability {
  available: boolean;
  fallback_action?: string | null; // "dataset.replace" when append is unsafe
  reason?: string | null;
  [k: string]: unknown;
}

/** POST /replace/preflight response. */
export interface ReplacePreflight {
  ok: boolean;
  action?: string | null; // "dataset.replace"
  [k: string]: unknown;
}

// ---------------------------------------------------------------------------
// Governed API error envelope (shared by every mutation route)
// ---------------------------------------------------------------------------

export interface ApiError {
  code: string;
  message?: string;
  // Route-specific evidence carried on some errors:
  deadline?: string | null;
  deadline_source?: string | null;
  issues?: unknown[];
  blocking_execution_id?: string | null;
  lock_reason?: string | null;
  allowed?: string[];
}
