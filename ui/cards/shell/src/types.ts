/**
 * Card data contract (Epic 9, Story 9.1) — mirrors the server `get_card` envelope
 * (server/core/cards.py get_card) exactly. Source-agnostic (AD-2): a card renders
 * CANONICAL metrics, so any module that maps those fields drives the same card.
 *
 * A card is AUTONOMOUS: it renders the deterministic cited comment (rendered_comment,
 * AD-9) on its own, and exposes metric_definitions + llm_commentary_guidelines so
 * Claude can deepen the analysis in chat (hybrid commentary).
 */

/** Re-export the shared metric definition shape (R6). */
export interface MetricDefinition {
  definition: string;
  unit?: string | null;
  direction?: "up_good" | "down_good" | "neutral";
  caveats?: string | null;
}

/** One per-metric rollup entry (delta-aware). Mirrors rollup.compute_rollup output. */
export interface MetricRollup {
  value: number;
  /** Absolute delta vs the prior equivalent period; null when prior was 0/absent. */
  delta: number | null;
  /** Percentage delta vs the prior period; null when undefined. */
  delta_pct: number | null;
  period?: string;
  source_system?: string | null;
  source_field?: string | null;
  pull_id?: string | null;
}

/** One sparkline point. */
export interface SeriesPoint {
  date: string;
  value: number;
}

/** meta.card_selection — the explainable, reversible selection contract. */
export interface CardSelection {
  chosen: string;
  mode: "explicit" | "suggested" | "fallback_empty";
  answers_question: string;
  alternatives: Array<{ id: string; answers_question: string }>;
}

export interface CardMeta {
  freshness?: {
    last_pull: string | null;
    cadence_hours?: number;
    stale_since?: string | null;
  };
  provenance?: {
    source_system?: string | null;
    source_field?: string | null;
    pull_id?: string | null;
    pull_ids?: string[];
  };
  alerts?: Array<{ code: string; severity: string; message: string }>;
  as_of?: string | null;
  trace_id?: string | null;
  project_id?: string | null;
  card_selection?: CardSelection;
  context_events?: Array<{
    id: string | null;
    event_date: string;
    type: string;
    label: string;
  }>;
}

// ---------------------------------------------------------------------------
// Story 9.2c — Card composition contract
// ---------------------------------------------------------------------------

/**
 * Block type tokens. Maps 1:1 to a viz primitive in CardComposition:
 *   kpi_row -> NumberHero row (KpiCardBody)
 *   line    -> LineChart
 *   bar     -> BarChart
 *   gauge   -> Gauge
 *   donut   -> Donut
 *   funnel  -> Funnel
 *   table   -> DataTable
 *   comment -> rendered cited comment slot (rendered_comment)
 */
export type BlockType =
  | "kpi_row"
  | "line"
  | "bar"
  | "gauge"
  | "donut"
  | "funnel"
  | "table"
  | "comment";

/**
 * Binding names what canonical metrics/dimensions a block reads from the card's
 * resolved rows. AD-2 source-agnostic: only canonical names, never module names.
 *   metrics: "*"  = all resolved metrics (universal for kpi_row / comment).
 *   metrics: string = a single canonical metric name (for a specific chart block).
 */
export interface CompositionBinding {
  metrics?: "*" | string | string[];
  dimensions?: string[];
  /** funnel: ordered canonical metric names, one per stage (9.6). */
  steps?: string[];
  /** gauge: ratio numerator/denominator (e.g. cost/conversions for CPA, 9.4). */
  numerator?: string;
  denominator?: string;
  /** gauge: semantic direction + display unit + optional explicit target. */
  direction?: "up_good" | "down_good" | "neutral";
  unit?: string;
  target?: number | null;
  /**
   * Story 10.4: when set, only rows from this connector are used by the block resolver
   * (e.g. 'google-analytics' for the GA4 landing_page bar so GSC 'page' rows are
   * not included). Transparent to the renderer — the server applies the filter.
   */
  connector_scope?: string;
  /**
   * Story 10.4: bar block top-N override (default _DEFAULT_TOP_N = 8).
   * Used by the entry-pages bar to show top 5 landing pages.
   */
  top_n?: number;
  /**
   * Story 10.4: when true, the table resolver computes the 'Part' column
   * against the TOP-N subtotal (SUM of returned rows), not the connector
   * daily total. Required for top-N-bounded partitions where the long tail
   * is dropped (the page partition does not sum to the connector daily total).
   */
  subtotal_share?: boolean;
  /** keywords movers bar (9.3): signed rank-delta instead of top-N-by-value. */
  movers?: boolean;
  /** keywords movers bar: semantic direction for delta sign interpretation. */
  // direction already present above
  /** keywords opportunities table (9.3): show queries with high impressions + weak rank. */
  opportunities?: boolean;
  /** keywords cannibalisation table (10.5): detect query split across >= 2 pages. */
  cannibalisation?: boolean;
  /** connectors context card (9.8): source token for app-level resolution. */
  source?: string;
}

// ---------------------------------------------------------------------------
// Stories 9.3-9.6 — per-block resolved DATA payloads (server -> UI contract).
//
// The server (core.cards.resolve_block) attaches a resolved `data` payload to EVERY
// composition block. The UI renderer reads `block.data` per block type. All payloads
// degrade to an empty shape (empty arrays / null value) so the UI renders its designed
// empty state — a resolver never raises (card rule e).
// ---------------------------------------------------------------------------

/** kpi_row block payload — one entry per bound metric (delta-aware, R6 direction). */
export interface KpiRowBlockData {
  metrics: Array<{
    metric: string;
    value: number | null;
    delta: number | null;
    /** Signed percentage number (e.g. 12 or -5); null when undefined. */
    delta_pct: number | null;
    /** R6 direction; null when no metric definition is present. */
    direction?: "up_good" | "down_good" | "neutral" | null;
  }>;
}

/** line block payload — one named series of {x:date, y:value} points per bound metric. */
export interface LineBlockData {
  series: Array<{ name: string; points: Array<{ x: string; y: number }> }>;
}

/** bar block payload — top-N bars grouped by the resolved dimension. */
export interface BarBlockData {
  orientation: "horizontal" | "vertical";
  /** The canonical dimension actually grouped on ("connector" = by source). */
  dimension: string | null;
  /**
   * direction?: "up" | "down" — emitted by the movers resolver (_resolve_bar_movers).
   * "up" = gained positions = good (success color); "down" = lost positions = bad (error color).
   * Absent for non-mover bars (flat accent color, unchanged).
   */
  bars: Array<{ label: string; value: number; direction?: "up" | "down" }>;
}

/** donut block payload — share of an additive metric by the resolved dimension. */
export interface DonutBlockData {
  total: number;
  dimension: string | null;
  slices: Array<{ label: string; value: number; pct: number }>;
}

/** gauge block payload — a metric/ratio vs a target. value is null on zero-division. */
export interface GaugeBlockData {
  value: number | null;
  target: number | null;
  /** "binding" when the target came from the template; "default" for the fallback. */
  target_source: "binding" | "default";
  unit: string;
  direction: "up_good" | "down_good" | "neutral";
  label: string;
}

/** funnel block payload — ordered stages with pass-through rate (first rate = 1.0). */
export interface FunnelBlockData {
  steps: Array<{ label: string; value: number; rate: number | null }>;
  /** Overall conversion rate (last step / first step); null on zero first step. */
  overall_rate?: number | null;
}

/** table block payload — columns + rows keyed by column.key (first col = "_dim"). */
export interface TableBlockData {
  columns: DataTableColumn[];
  rows: Record<string, unknown>[];
}

/** comment block payload — the deterministic cited comment text (AD-9). */
export interface CommentBlockData {
  text: string;
}

/**
 * Union of all resolved block data payloads (server-attached under block.data). This is
 * the documentation of what each block type's `data` carries (Stories 9.3-9.6). The
 * `CompositionBlock.data` field stays intentionally permissive (below) so existing
 * renderer code (table blocks reading `data.rows`/`data.columns`) keeps compiling; a
 * per-block widget narrows to the relevant payload via a cast, e.g.
 * `const d = block.data as BarBlockData`.
 */
export type BlockData =
  | KpiRowBlockData
  | LineBlockData
  | BarBlockData
  | DonutBlockData
  | GaugeBlockData
  | FunnelBlockData
  | TableBlockData
  | CommentBlockData;

/** One block in a card's ordered composition (Story 9.2c; data payload 9.3-9.6). */
export interface CompositionBlock {
  type: BlockType | string; // string fallback for future block types (renders empty state)
  binding: CompositionBinding;
  /** Optional display title for the block section. */
  title?: string;
  /**
   * Server-resolved data payload (Stories 9.3-9.6). The concrete shape depends on
   * block.type — see the per-type `*BlockData` interfaces above and cast accordingly.
   * Kept permissive (indexable + rows/columns for table blocks) so existing renderer code
   * compiles unchanged. Always present after get_card; may be an empty shape.
   */
  data?: {
    rows?: Record<string, unknown>[];
    columns?: DataTableColumn[];
    [key: string]: unknown;
  };
}

export interface CardData {
  card_id: string;
  /** Story 9.2c: card_type = template_id for end-to-end traceability. */
  card_type?: string;
  title: string;
  answers_question: string;
  date_range: { start: string; end: string };
  connectors: string[];
  /** {metric: rollup} — delta-aware (R6). */
  metrics: Record<string, MetricRollup>;
  /** {metric: [{date, value}]} — per-metric sparkline series. */
  series: Record<string, SeriesPoint[]>;
  /** Deterministic cited what+why (AD-9). Rendered in the comment slot. */
  rendered_comment: string;
  /** R6: per-metric definitions (definitions popover + direction-aware delta). */
  metric_definitions?: Record<string, MetricDefinition>;
  /** R6: operator-set commentary directives (chat springboard; not shown as hero). */
  llm_commentary_guidelines?: string;
  /**
   * Story 9.2c: ordered composition blocks from the server (mirrors CardTemplate.composition).
   * When present, CardComposition renders the card body from these blocks in order.
   * When absent, the card falls back to its inline layout (backward-compatible).
   */
  composition?: CompositionBlock[];
}

/** Column definition for DataTable. */
export interface DataTableColumn {
  /** Canonical key in each row object. */
  key: string;
  /** Display header label (French). */
  label: string;
  /** Right-align numeric columns (tabular-nums). Default false. */
  numeric?: boolean;
  /** Optional sort weight for default ordering. */
  sortable?: boolean;
}

export interface CardEnvelope {
  schema_version: string;
  meta: CardMeta;
  data: CardData;
}

/** French metric labels (UX-DR10 French-first). Canonical vocab, source-agnostic. */
export const CARD_METRIC_LABELS: Record<string, string> = {
  clicks: "Clics",
  impressions: "Impressions",
  sessions: "Sessions",
  active_users: "Utilisateurs actifs",
  conversions: "Conversions",
  cost: "Coût",
  revenue: "Revenu",
  average_position: "Position moyenne",
  roas: "ROAS",
  ctr: "CTR",
  cpa: "CPA",
};

export function metricLabel(metric: string): string {
  return CARD_METRIC_LABELS[metric] ?? metric;
}
