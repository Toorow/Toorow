/**
 * TypeScript interfaces for the VueEnsemble control-tower page (Story 8.4).
 *
 * AD-2: source-agnostic (no module-specific strings).
 * UX-DR10: French-first copy in consuming components.
 */

/** KPI summary from GET /api/overview */
export interface OverviewKpis {
  extracts_today_ok: number;
  extracts_today_failed: number;
  extracts_running: number;
  vs_yesterday_ok_delta: number; // signed; today_ok - yesterday_ok
}

/** Last extract info per datastream */
export interface LastExtract {
  date: string;   // YYYY-MM-DD
  status: "ok" | "partial" | "empty" | "failed" | "running" | "never_fetched";
}

/** One fleet row from GET /api/overview */
export interface FleetRow {
  id: string;
  name: string;
  module_name: string;
  enabled: boolean;
  auth_status: string | null;
  issues_count: number;
  last_extract: LastExtract | null;
  next_run: string;     // "Demain 02h00" | "Manuel"
  volume_7d: number[];  // 7 ints, oldest first
}

/** Circuit breaker state */
export interface BreakerInfo {
  platform: string;
  state: "closed" | "open" | "half_open" | string;
  budget_remaining: number | null;
}

/** Full response from GET /api/overview */
export interface OverviewData {
  kpis: OverviewKpis;
  fleet: FleetRow[];
  deadletters_24h: number;
  mirror_last_sync: string | null; // ISO-8601 or null
  breakers: BreakerInfo[];
}

/** Fetch state for the overview page */
export type OverviewFetchState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ok"; data: OverviewData };
