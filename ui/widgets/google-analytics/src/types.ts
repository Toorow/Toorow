/**
 * TypeScript types for the get_daily_report canonical envelope (AD-1 / Story 1.5).
 *
 * Shapes mirror server/core/envelope.py and server/core/warehouse.py exactly:
 *   - rows come from fact_daily_kpi (Story 1.4 seed)
 *   - meta.provenance is a list[dict] in envelope.py, but P0 callers historically
 *     used a single dict — we accept BOTH shapes defensively (T2.1 note).
 *
 * Story 8.8 (R6): added MetricDefinition + metric_definitions to the envelope data.
 * metric_definitions is OPTIONAL — absent in legacy envelopes; widget renders
 * gracefully with no definitions.
 */

/** One fact_daily_kpi row (warehouse.query_daily_report row shape). */
export interface Row {
  date: string; // "YYYY-MM-DD"
  connector: string; // e.g. "google-analytics"
  metric: string; // "sessions" | "active_users" | "conversions"
  breakdown_dimension: string; // "device_category" | "country"
  breakdown_value: string; // "desktop" | "France" | ...
  value: number;
  pull_id: string; // seeded pull_id from fact_daily_kpi
  loaded_at: string; // ISO timestamp
}

/**
 * Story 8.8 (R6): per-metric semantic grounding from flow.report.schema.json.
 * Matches the flow.report.schema.json#metric_definitions shape exactly.
 * Used as UI affordances (tooltips, direction-aware delta coloring) and as
 * grounding for LLM commentary guidelines.
 */
export interface MetricDefinition {
  /** Human-readable definition of the metric (French, shown in tooltip). */
  definition: string;
  /** Unit label (e.g. "séances", "utilisateurs", "%"). Null when unitless. */
  unit?: string | null;
  /**
   * Direction: "up_good" = positive delta is good (green), negative is bad (red).
   *            "down_good" = inverted — positive delta is bad (red), negative is good (green).
   *            "neutral" = no directional coloring (use text.secondary for arrow).
   */
  direction?: "up_good" | "down_good" | "neutral";
  /** Caveats or methodology notes (French). Null when absent. */
  caveats?: string | null;
}

export interface DailyReportData {
  report_profile: string;
  date_range: { start: string; end: string };
  connectors: string[];
  rows: Row[];
  /**
   * Story 8.8 (R6): optional per-metric definitions.
   * When present: drives KPI tile info tooltip, direction-aware delta color,
   * and the collapsible "Définitions" section.
   * When absent: widget renders exactly as before (backward-compatible).
   */
  metric_definitions?: Record<string, MetricDefinition>;
}

/** meta.provenance entry (AD-9). */
export interface ProvenanceEntry {
  source_system: string;
  pull_id: string | null;
  /** envelope.py may add extra fields; keep it open. */
  [key: string]: unknown;
}

/**
 * Context event marker from meta.context_events.
 * Story 4.4 (AC7): base fields.
 * Story 31.5: added platform, source, value (MMM regressors) + category,
 * default_marker (from dim_event_type join on the server side).
 * All new fields are optional for backward compatibility with legacy envelopes
 * that predate story 31.5.
 */
export interface ContextEventMeta {
  id: string | null;
  event_date: string;
  type: string;
  label: string;
  /** Source platform (e.g. "youtube", "github", null for manual). Story 31.5. */
  platform?: string | null;
  /** Origin: "manual" | connector name (e.g. "youtube-analytics"). Story 31.5. */
  source?: string | null;
  /** Optional MMM regressor magnitude (intensity). Story 31.5. */
  value?: number | null;
  /**
   * Category from dim_event_type.csv (content/engineering/marketing/commerce/
   * business/operations). Absent on legacy envelopes. Story 31.5.
   */
  category?: string;
  /**
   * Marker shape from dim_event_type.default_marker
   * (triangle/diamond/flag/star/line/pin/cross). Absent on legacy envelopes.
   * Story 31.5.
   */
  default_marker?: string;
}

export interface DailyReportMeta {
  freshness: {
    last_pull: string | null;
    cadence_hours: number;
    stale_since?: string | null;
  };
  /** list[dict] in envelope.py; a single dict is also tolerated (defensive). */
  provenance: ProvenanceEntry[] | ProvenanceEntry;
  alerts: Array<{ code: string; severity: string; message: string }>;
  /** Context event markers (Story 4.4, AC7). Absent when no events. */
  context_events?: ContextEventMeta[];
}

export interface DailyReportEnvelope {
  schema_version: string;
  meta: DailyReportMeta;
  data: DailyReportData;
}

/** Metric keys the seeded standard_daily profile exposes (AC2). */
export const METRICS = ["sessions", "active_users", "conversions"] as const;
export type MetricKey = (typeof METRICS)[number];

/** French display labels for metrics (UX-DR10). */
export const METRIC_LABELS: Record<string, string> = {
  sessions: "Sessions",
  active_users: "Utilisateurs actifs",
  conversions: "Conversions",
};

/** Normalize meta.provenance to a list regardless of the shape received. */
export function provenanceList(
  provenance: ProvenanceEntry[] | ProvenanceEntry | undefined | null,
): ProvenanceEntry[] {
  if (!provenance) return [];
  return Array.isArray(provenance) ? provenance : [provenance];
}
