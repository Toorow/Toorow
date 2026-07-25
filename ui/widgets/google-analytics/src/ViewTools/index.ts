/**
 * ViewTools barrel re-export (Story 1.7).
 * Import controls and aggregation utilities from here.
 */

export { default as GranularityToggle, GRANULARITY_LABELS } from "./GranularityToggle";
export { default as ConnectorFilter, TOOROW_LABELS, connectorLabel } from "./ConnectorFilter";
export { default as MetricSwitch, METRIC_DISPLAY_LABELS, metricLabel } from "./MetricSwitch";
export {
  default as DimensionSwitch,
  DIMENSION_DISPLAY_LABELS,
  dimensionLabel,
} from "./DimensionSwitch";
export {
  filterRows,
  aggregateRows,
  isoWeekNumber,
  isoWeekStart,
  monthStart,
  periodLabel,
} from "./aggregation";
export type { Granularity, AggregatedRow } from "./aggregation";
