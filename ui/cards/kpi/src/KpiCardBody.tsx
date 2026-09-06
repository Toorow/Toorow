/**
 * KpiCardBody — the KPI card hero body (Epic 9, Story 9.2).
 *
 * Per metric: a hero tabular number + direction-aware delta (R6) + a sparkline.
 * The NUMBER is the hero (h3, tabular-nums); the delta is small/muted; the comment
 * (rendered by CardShell) is secondary. Renders a designed empty/partial state when a
 * metric has no data (never blank). French-first with accents (UX-DR10).
 *
 * Direction semantics (R6):
 *   up_good  : +delta => success tint, -delta => error tint
 *   down_good: inverted
 *   neutral  : text.secondary (no semantic color)
 */

import {
  NO_VALUE,
  Sparkline,
  formatPercent,
  formatValue,
  metricLabel,
  metricUnitSuffix,
} from "@toorow/card-shell";
import type { MetricRollup, MetricDefinition, SeriesPoint } from "@toorow/card-shell";
import { ArrowUpwardIcon, ArrowDownwardIcon } from "./icons";
import { Box, Typography, useTheme } from "@toorow/shell";
import type { WidgetTheme } from "@toorow/shell";

interface KpiCardBodyProps {
  metrics: Record<string, MetricRollup>;
  series: Record<string, SeriesPoint[]>;
  metricDefinitions?: Record<string, MetricDefinition>;
}

function deltaColorFor(
  theme: WidgetTheme,
  deltaPct: number | null,
  direction: MetricDefinition["direction"],
): string {
  const dir = direction ?? "up_good";
  if (deltaPct === null || dir === "neutral") return theme.palette.text.secondary;
  const isPositive = deltaPct >= 0;
  const isGood = dir === "up_good" ? isPositive : !isPositive;
  return isGood ? theme.palette.success.main : theme.palette.error.main;
}

function KpiMetricTile({
  metric,
  rollup,
  points,
  definition,
}: {
  metric: string;
  rollup: MetricRollup;
  points: SeriesPoint[];
  definition?: MetricDefinition;
}) {
  const theme = useTheme();
  const deltaPct = rollup.delta_pct ?? null;
  const color = deltaColorFor(theme, deltaPct, definition?.direction);
  // `toFixed` never sees a locale: it is the second decimal convention a card
  // showed at once (story 76-8). The pinned formatter is the only one now.
  const deltaText = deltaPct === null ? NO_VALUE : formatPercent(deltaPct, { signed: true });

  return (
    <Box
      sx={{
        flex: "1 1 180px",
        minWidth: 160,
        display: "flex",
        flexDirection: "column",
        gap: 0.5,
      }}
      data-testid="kpi-metric-tile"
      data-metric={metric}
    >
      <Typography variant="overline" color="text.secondary" sx={{ lineHeight: 1.2 }}>
        {metricLabel(metric)}
        {metricUnitSuffix(definition?.unit)}
      </Typography>

      {/* HERO number */}
      <Typography
        variant="h3"
        component="p"
        sx={{
          fontWeight: 700,
          fontVariantNumeric: "lining-nums tabular-nums",
          lineHeight: 1.1,
          color: "text.primary",
        }}
        data-testid="kpi-hero-value"
      >
        {formatValue(rollup.value)}
      </Typography>

      {/* Delta + sparkline row */}
      <Box sx={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 1 }}>
        <Box sx={{ display: "flex", alignItems: "center", gap: 0.25 }} data-testid="kpi-delta">
          {deltaPct !== null && (
            <Box component="span" sx={{ color, display: "flex", alignItems: "center" }}>
              {deltaPct >= 0 ? <ArrowUpwardIcon size={14} /> : <ArrowDownwardIcon size={14} />}
            </Box>
          )}
          <Typography
            variant="caption"
            component="span"
            sx={{ color, fontVariantNumeric: "lining-nums tabular-nums" }}
            data-testid="kpi-delta-text"
          >
            {deltaText}
          </Typography>
        </Box>
        <Sparkline points={points} ariaLabel={`Tendance ${metricLabel(metric)}`} />
      </Box>
    </Box>
  );
}

export default function KpiCardBody({ metrics, series, metricDefinitions }: KpiCardBodyProps) {
  const metricKeys = Object.keys(metrics);

  // Designed empty state — never blank (card rule e).
  if (metricKeys.length === 0) {
    return (
      <Box
        sx={{
          py: 4,
          textAlign: "center",
          color: "text.secondary",
        }}
        data-testid="kpi-empty-state"
      >
        <Typography variant="body2" sx={{ mb: 0.5 }}>
          No data is available for the period requested.
        </Typography>
        <Typography variant="caption" color="text.disabled">
          Check that the canonical metrics this card requires are being fed.
        </Typography>
      </Box>
    );
  }

  return (
    <Box
      sx={{ display: "flex", flexWrap: "wrap", gap: 3, rowGap: 2.5 }}
      data-testid="kpi-card-body"
    >
      {metricKeys.map((metric) => (
        <KpiMetricTile
          key={metric}
          metric={metric}
          rollup={metrics[metric]}
          points={series[metric] ?? []}
          definition={metricDefinitions?.[metric]}
        />
      ))}
    </Box>
  );
}
