/**
 * Shared TypeScript interfaces for the Quality monitors feature (Stories 8.6 + 13.3).
 *
 * AD-2: source-agnostic; monitor types are data, never hardcoded in logic.
 */

/** One monitor's KPI summary from GET /api/dq/summary */
export interface MonitorSummary {
  // Story 13.3: dq_date_format is the 5th monitor (rows rejected at extraction).
  type: "dq_volume" | "dq_timeliness" | "dq_duplication" | "dq_schema" | "dq_date_format";
  label: string;
  healthy_pct: number;     // 0–100
  unresolved_count: number;
  new_since_yesterday: number;
}

/** Summary response from GET /api/dq/summary */
export interface DQSummaryResponse {
  monitors: MonitorSummary[];
  total_issues_24h: number;
  total_unresolved: number;
}

/** One DQ firing / issue from GET /api/dq/issues */
export interface DQIssue {
  id: string;
  type: string;
  label: string;
  datastream_id: string;
  datastream_name: string;
  module_name: string;
  window_date: string;   // YYYY-MM-DD
  fired_at: string;      // ISO-8601
  severity: "warning" | "error";
  acknowledged: boolean;
  acknowledged_at: string | null;
  message: string;
}

/** Issues response from GET /api/dq/issues */
export interface DQIssuesResponse {
  issues: DQIssue[];
  total: number;
}

/** Monitor display metadata (label, icon concept, description) */
export interface MonitorMeta {
  label: string;
  description: string;
  icon: "volume" | "clock" | "copy" | "schema" | "filter";
}

/** History entry per monitor from GET /api/dq/history (Story 13.3) */
export interface MonitorHistoryEntry {
  type: string;
  label: string;
  days_with_firing: number;
  success_rate: number;  // 0–100 %
}

/** Response from GET /api/dq/history */
export interface DQHistoryResponse {
  window_days: number;
  monitors: MonitorHistoryEntry[];
}

/** Freshness entry per datastream from GET /api/dq/datastream-freshness (Story 13.3) */
export interface DatastreamFreshness {
  datastream_id: string;
  datastream_name: string;
  module_name: string;
  last_pull_at: string | null;       // ISO-8601
  last_success_at: string | null;    // ISO-8601
  last_status: "success" | "error" | "partial" | null;
  row_count: number | null;
}

/** Response from GET /api/dq/datastream-freshness */
export interface DQFreshnessResponse {
  datastreams: DatastreamFreshness[];
}

/** Response from POST /api/dq/evaluate (Story 13.3) */
export interface DQEvaluateResponse {
  status: "triggered";
  project_id: string;
  triggered_at: string;  // ISO-8601
}

export const MONITOR_META: Record<string, MonitorMeta> = {
  dq_volume: {
    label: "Volume",
    description:
      "Flags days where the extracted row count drifts from the historical median " +
      "(> 2.6 sigma, minimum 10 days of history).",
    icon: "volume",
  },
  dq_timeliness: {
    label: "Timeliness",
    description:
      "Checks that the previous day's extract lands before the configured cutoff " +
      "(default 09:00, project time zone).",
    icon: "clock",
  },
  dq_duplication: {
    label: "Duplicates",
    description:
      "Counts identical rows in the raw table for the evaluated day. " +
      "Any duplication is reported.",
    icon: "copy",
  },
  dq_schema: {
    label: "Schema consistency",
    description:
      "Compares the raw table's current column set against the stored reference. " +
      "A drift raises an alert and the reference is updated automatically.",
    icon: "schema",
  },
  // Story 13.3 -- 5th monitor: rows rejected at extraction (dq_date_format).
  dq_date_format: {
    label: "Rejected rows",
    description:
      "Reports rows dropped during extraction — malformed rows or rows outside " +
      "the date range tolerated by the source connector.",
    icon: "filter",
  },
};
