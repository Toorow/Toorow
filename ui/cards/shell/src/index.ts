/**
 * @toorow/card-shell — public API (Epic 9, Stories 9.1 + 9.2b + 9.2c).
 *
 * Exports:
 *   - CardShell (default) — shared card chrome (title, freshness badge, rendered-
 *     comment slot, definitions popover, feedback/export chrome, slim footer).
 *   - Sparkline           — minimal inline SVG trend line.
 *   - LineChart           — courbe 1..N séries (Story 9.2b).
 *   - BarChart            — barres horizontales ET verticales, groupées (Story 9.2b).
 *   - Gauge               — jauge valeur vs cible, arc radial (Story 9.2b).
 *   - Donut               — répartition en parts, centre total, légende (Story 9.2b).
 *   - Funnel              — entonnoir, taux de passage, drop-off (Story 9.2b).
 *   - CardFeedbackBar     — thumbs + commentaire → submit_feedback (Story 9.2b).
 *   - DataTable           — tableau primitif, 52px rows, tri client, aria (Story 9.2c).
 *   - CardComposition     — renderer blocks -> primitives en ordre (Story 9.2c).
 *   - The card data contract types (mirrors server/core/cards.py get_card envelope).
 */

export { default } from "./CardShell";
export { default as CardShell } from "./CardShell";
export { default as Sparkline } from "./Sparkline";
export { default as LineChart } from "./LineChart";
export { default as BarChart } from "./BarChart";
export { default as Gauge } from "./Gauge";
export { default as Donut } from "./Donut";
export { default as Funnel } from "./Funnel";
export { default as CardFeedbackBar } from "./CardFeedbackBar";
export { default as DataTable } from "./DataTable";
export { default as CardComposition } from "./CardComposition";
// AI-271 — the honest state a card shows when the host injected no envelope.
// It replaces the fixture fallback that every entrypoint used to render.
export { default as NoEnvelope } from "./NoEnvelope";
export type { NoEnvelopeProps } from "./NoEnvelope";
// Epic 23 — extended viz primitives (Stories 23.2-23.5) + the theme-driven
// color contract every primitive reads (Story 23.1; org branding via
// meta.branding → WidgetShell ThemeProvider → getVizPalette).
// `readCssVizTheme` is exported for Story 50.5: the shared Visualization runtime
// reads the LIVE CSS custom properties instead of a MUI ThemeProvider, and it must
// read them through THIS function. A second palette reader over a second variable
// set is the failure `vizTheme.ts:1-8` exists to forbid.
export { getVizPalette, readCssVizTheme } from "./vizTheme";
export type { VizThemeInput } from "./vizTheme";
// Story 50.5 — the shared Visualization runtime is exported under @toorow/card-shell/viz
// to keep ECharts out of card primitive bundles.
export { default as MatrixHeatmap } from "./MatrixHeatmap";
export { default as OverlayBarChart } from "./OverlayBarChart";
export { default as ValueGrid } from "./ValueGrid";
export { default as DotMatrix } from "./DotMatrix";
export { default as RankedList } from "./RankedList";
export { default as KpiDeltaFooter } from "./KpiDeltaFooter";
// Story 76-8 — THE variation legend, one for every primitive that colours a
// verdict (epic 76: the colour semantics of a variation must be legended), plus
// ONE function that decides that verdict and the derivation that tells a card
// which conventions it has in force. Exported so a card App can mount the legend
// in its footer, never so a second one can be written.
export { default as VariationLegend } from "./VariationLegend";
export type { VariationLegendProps, VariationConvention } from "./VariationLegend";
export { verdictTone, verdictColor, changeMark } from "./verdictTone";
export type { Verdict, VerdictDirection, VerdictOptions } from "./verdictTone";
export { variationConventions } from "./variationConventions";
// Story 76-8 — the ONE formatter set, exported so a CARD (not only the shared
// Visualization runtime) reaches it. Before this, every card template grouped
// its own numbers on a locale of its own while its deltas went through
// `toFixed`, so a single card could show two decimal conventions at once.
export {
  FORMATTER_LOCALE,
  NBSP,
  NO_VALUE,
  formatCompact,
  formatCurrency,
  formatMeasure,
  formatNumber,
  formatPercent,
  formatValue,
  isCurrencyCode,
  unitWorthShowing,
} from "./viz/theme/formatters";
export type { ValueFormatOptions } from "./viz/theme/formatters";
export { metricUnitSuffix } from "./types";
export { CARD_METRIC_LABELS, metricLabel } from "./types";
export type {
  CardEnvelope,
  CardData,
  CardMeta,
  CardSelection,
  MetricRollup,
  MetricDefinition,
  SeriesPoint,
  CompositionBlock,
  CompositionBinding,
  BlockType,
  DataTableColumn,
} from "./types";
// Story 9.10 — shared MCP Apps channel (SDK-first, legacy fallback). Owned by
// @toorow/shell (workspace dependency direction: card-shell -> shell);
// re-exported here so card entrypoints import everything from one place.
export {
  connectMcpApp,
  useMcpApp,
  callServerTool,
  readInjectedEnvelope,
  __resetMcpAppForTests,
} from "@toorow/shell";
export type {
  McpAppHandle,
  McpChannelMode,
  McpSdkApp,
  McpToolResultParams,
  ConnectMcpAppOptions,
  EnvelopeCallback,
} from "@toorow/shell";
export type { LineChartSeries, LineChartProps } from "./LineChart";
export type { BarChartEntry, BarChartProps } from "./BarChart";
export type { GaugeProps } from "./Gauge";
export type { DonutSlice, DonutProps } from "./Donut";
export type { FunnelStep, FunnelProps } from "./Funnel";
export type { CardFeedbackBarProps } from "./CardFeedbackBar";
export type { DataTableProps } from "./DataTable";
export type { CardCompositionProps } from "./CardComposition";
export type { VizPalette } from "./vizTheme";
export type { MatrixHeatmapProps, MatrixHeatmapThreshold } from "./MatrixHeatmap";
export type { OverlayBarChartProps, OverlayBarEntry } from "./OverlayBarChart";
export type { ValueGridProps, ValueGridCell } from "./ValueGrid";
export type { DotMatrixProps, DotMatrixGroup } from "./DotMatrix";
export type { RankedListProps, RankedListEntry } from "./RankedList";
export type { KpiDeltaFooterProps, KpiDeltaItem } from "./KpiDeltaFooter";
