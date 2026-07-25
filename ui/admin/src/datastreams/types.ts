/**
 * Shared TypeScript interfaces for the Datastreams feature (Story 8.3).
 *
 * AD-2: source-agnostic; module_name is data, never hardcoded here.
 * UX-DR10: French-first copy in components consuming these types.
 */

/** Day-grain extract ledger entry from GET /api/datastreams/{id}/ledger */
export interface LedgerEntry {
  date: string; // YYYY-MM-DD
  status: "ok" | "partial" | "empty" | "failed" | "running" | "never_fetched";
  row_count: number | null;
  expected_rows: number | null;
  completeness_ratio: number | null;
  pull_id: string | null;
  loaded_at: string | null; // ISO-8601
}

/** Datastream list item from GET /api/datastreams?project_id= */
export interface DatastreamSummary {
  id: string;
  name: string;
  module_name: string;
  connection_ref_id: string | null;
  enabled: boolean;
  schedule_mode: "nightly" | "manual";
  refetch_days: number;
  connection_status: string | null;
  last_pull_id: string | null;
  last_pull_state: string | null;
  last_pull_completed_at: string | null;
}

/** Full datastream from GET /api/datastreams/{id} */
export interface Datastream {
  id: string;
  project_id: string;
  name: string;
  module_name: string;
  connection_ref_id: string | null;
  report_profile_id: string | null;
  enabled: boolean;
  schedule_mode: "nightly" | "manual";
  refetch_days: number;
  date_window_days: number;
  config: Record<string, unknown> | null;
  created_by: string;
  created_at: string;
  updated_at: string;
  /** Source kind — "connector_pull" | "managed_feed" | "external_bq".
   *  Normalised to "connector_pull" server-side when absent. */
  source_kind: "connector_pull" | "managed_feed" | "external_bq";
}

/** A refetch job result from POST /api/datastreams/{id}/refetch */
export interface RefetchJob {
  job_id: string;
  pull_id: string;
  state: string;
  date_from: string;
  date_to: string;
  deduplicated?: boolean;
}

/** Response from POST /api/datastreams/{id}/refetch */
export interface RefetchResponse {
  jobs: RefetchJob[];
}

/** Response from GET /api/datastreams/{id}/ledger */
export interface LedgerResponse {
  ledger: LedgerEntry[];
}

/** Status display metadata */
export interface StatusMeta {
  label: string;     // French label
  color: string;     // CSS color (low-alpha tint or solid for 'ok')
  dotColor: string;  // Solid dot color
}

export const STATUS_META: Record<LedgerEntry["status"], StatusMeta> = {
  ok: {
    label: "OK",
    color: "rgba(46,125,50,0.12)",   // green tint
    dotColor: "#2E7D32",
  },
  partial: {
    label: "Partiel",
    color: "rgba(245,124,0,0.12)",   // orange tint
    dotColor: "#F57C00",
  },
  empty: {
    label: "Vide",
    color: "rgba(235,235,243,0.30)", // neutral tint
    dotColor: "#6B6A74",
  },
  failed: {
    label: "Échoué",
    color: "rgba(229,57,53,0.12)",   // red tint
    dotColor: "#E53935",
  },
  running: {
    label: "En cours",
    color: "rgba(155,225,246,0.20)", // accent tint
    dotColor: "#FF99C8",
  },
  never_fetched: {
    label: "Jamais extrait",
    color: "rgba(235,235,243,0.08)", // barely visible
    dotColor: "#EBEBF3",
  },
};
