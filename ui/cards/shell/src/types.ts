/**
 * Card data contract (Epic 9, Story 9.1) — mirrors the server `get_card` envelope
 * (server/core/cards.py get_card) exactly. Source-agnostic (AD-2): a card renders
 * CANONICAL metrics, so any module that maps those fields drives the same card.
 *
 * A card is AUTONOMOUS: it renders the deterministic cited comment (rendered_comment,
 * AD-9) on its own, and exposes metric_definitions + llm_commentary_guidelines so
 * Claude can deepen the analysis in chat (hybrid commentary).
 */

import { unitWorthShowing } from "./viz/theme/formatters";

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

/**
 * meta.analytical_path — WHICH engine produced the figures on this card
 * (story 53.9, CAV-17). Toorow answers the same business question through two
 * paths reading two different relations: the card path reads an ungoverned
 * relation (`fact_daily_kpi` for the generic card), while the Console's
 * Explore/Result path reads the Datastream's published output relation and pins
 * a Semantic View version and a Query Spec version on the Result. They can
 * disagree, and neither reconciles against the other.
 *
 * Declared server-side by `core.envelope.declare_analytical_path`, and stamped
 * unconditionally by `core.envelope.build_envelope` on every envelope built from
 * story 53.9 onward. Optional HERE for one honest reason: a Render frozen before
 * that change carries no such key, and a required field would describe those
 * persisted envelopes falsely. Absence means "built before the disclosure", never
 * "governed".
 */
export interface AnalyticalPath {
  path: string;
  relation: string;
  governed_result: boolean;
  note: string;
}

export interface CardMeta {
  freshness?: {
    last_pull: string | null;
    cadence_hours?: number;
    stale_since?: string | null;
    /**
     * `stale_since: null` alone reads as "evaluated, and fresh". Only
     * `core.health_enrichment` evaluates staleness, and no card path reaches it,
     * so the server always declares WHICH of the two situations a reader is in.
     */
    stale_since_evaluated?: boolean;
  };
  analytical_path?: AnalyticalPath;
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
  /**
   * Faits datés posés sur l'axe (sorties de vidéo, mises en ligne).
   *
   * Ce ne sont PAS des mesures : un repère n'a pas de valeur, il a une date et
   * un titre. Le serveur en fournit autant qu'il en a lus ; ceux dont la date ne
   * tombe sur aucun point de l'axe ne sont pas dessinés, et `markers_reason` dit
   * pourquoi la liste est vide plutôt que de laisser croire qu'il n'y a rien eu.
   */
  markers?: Array<{ index: string; label: string }>;
  /** Pourquoi il n'y a aucun repère — jamais une liste vide muette. */
  markers_reason?: { code: string; message: string } | null;
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
  /** The STABLE identifier the product joins on. It never reaches a person. */
  dimension: string | null;
  /**
   * The word a person reads at the centre of the donut — the client's own name
   * for `dimension` when somebody named it, otherwise the word derived from the
   * identifier. Resolved server-side by the SAME seam the block title above it
   * uses, because a heading and the unit under it must not call one dimension
   * two things. Until 2026-08-31 the shell printed `dimension` here, so a donut
   * of `device_category` announced its unit in the words of the database.
   */
  dimension_label?: string | null;
  slices: Array<{ label: string; value: number; pct: number }>;
}

/** gauge block payload — a metric/ratio vs a target. value is null on zero-division. */
export interface GaugeBlockData {
  value: number | null;
  target: number | null;
  /** Where the objective came from. null target <=> "unset" — see below (CAV-08). */
  target_source: "unset" | "project" | "binding";
  unit: string;
  direction: "up_good" | "down_good" | "neutral";
  label: string;
}

/*
 * `GaugeBlockData.target_source` mirrors `cards.py:_resolve_gauge`, most specific first:
 *   - "binding" — the card template bound the objective;
 *   - "project" — the project preference `cpa_target` supplied it;
 *   - "unset"   — nobody defined one, so `target` is null and NO verdict is drawn.
 *
 * "default" was the fourth member, and it named the platform constant
 * `_DEFAULT_CPA_TARGET = 50.0`. The server stopped emitting it in 34cd021 and can no
 * longer produce that value, so the union above no longer admits it; conversely "unset"
 * was undeclarable here while the server had already been emitting it.
 *
 * This rationale sits BELOW the interface on purpose: `target_source` must stay on line
 * 196, which `server/core/cards.py:100`, `server/tests/core/test_card_blocks.py:233` and
 * the 53.5 record all cite by line.
 */

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
  // A raw token on a screen is a defect: « SCREEN_PAGE_VIEWS » printed as it
  // stood on `card-journey` for want of a label (story 76-8).
  screen_page_views: "Pages vues",
  page_views: "Pages vues",
  bounce_rate: "Taux de rebond",
};

export function metricLabel(metric: string): string {
  return CARD_METRIC_LABELS[metric] ?? metric;
}

/**
 * WHAT IS ASKED ONCE IS NOT ASKED TWICE (story 76-8).
 *
 * Measured on 2026-09-05 across the cards at HEAD: « CLICS (CLICS) »,
 * « CONVERSIONS (CONVERSIONS) », « SESSIONS (SÉANCES) », « IMPRESSIONS
 * (IMPRESSIONS) ». The parenthesis repeated the label and added nothing.
 *
 * The rule is DERIVED rather than a list of forbidden words: a unit earns its
 * parenthesis when it changes HOW THE NUMBER READS (a currency, a percentage, a
 * ratio); a unit that is a plain word only renames what the label already said.
 * `unitWorthShowing` carries the rule, here and everywhere else.
 */
export function metricUnitSuffix(unit: string | null | undefined): string {
  return unitWorthShowing(unit) ? ` (${unit!.trim()})` : "";
}

/**
 * AN IDENTIFIER IS SHOWN IN DOUBLE, OR NOT AT ALL (`console-presentation.md` §4,
 * applied to the cards by story 76-8).
 *
 * The card footer read « Source : google-search-console » — the technical slug,
 * alone, in a sentence addressed to a human. This function returns the LABEL;
 * the caller prints the slug beside it, in discreet monospace.
 *
 * The transformation is DERIVED, not a catalogue: separators become spaces, each
 * word takes its capital, and a word with no vowel is not a word — it is an
 * acronym, and it is written in capitals (`gsc` gives `GSC`).
 */
export function sourceSystemLabel(slug: string | null | undefined): string | null {
  const trimmed = slug?.trim();
  if (!trimmed) return null;
  return trimmed
    .split(/[-_\s]+/)
    .filter(Boolean)
    .map((word) =>
      /[aeiouyàâäéèêëîïôöùûü]/i.test(word)
        ? word.charAt(0).toUpperCase() + word.slice(1)
        : word.toUpperCase(),
    )
    .join(" ");
}
