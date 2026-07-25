/**
 * BreakdownBars — per-value totals for the active breakdown dimension (Story 8.11).
 *
 * Renders a compact horizontal bar list of the metric total per breakdown_value
 * for ONE selected breakdown_dimension. When that dimension is a composite split
 * ('country>device') the labels render as « FR > mobile » via breakdownLabel().
 *
 * Pins a single breakdown_dimension (aggregateByBreakdown) so composite and
 * single-dimension series are never mixed — no double counting (DESIGN §2).
 *
 * Style follows the Origin design system (Story 8.8): tabular numerals as the
 * hero, accent bars, hairline dividers, no gridlines.
 */

import { useTheme, alpha } from "@mui/material/styles";
import Box from "@mui/material/Box";
import Typography from "@mui/material/Typography";
import { aggregateByBreakdown, breakdownLabel } from "./dataUtils";
import type { Row } from "./types";

interface BreakdownBarsProps {
  rows: Row[];
  metric: string;
  dimension: string;
  metricLabel: string;
  dimensionLabel: string;
  dateRange?: { start: string; end: string };
  /** Cap the number of rows shown (top-N by value). Default 12. */
  topN?: number;
}

export default function BreakdownBars({
  rows,
  metric,
  dimension,
  metricLabel,
  dimensionLabel,
  dateRange,
  topN = 12,
}: BreakdownBarsProps) {
  const theme = useTheme();
  const totals = aggregateByBreakdown(rows, metric, dimension, dateRange);

  const entries = [...totals.entries()]
    .sort((a, b) => b[1] - a[1])
    .slice(0, topN);

  if (entries.length === 0) {
    return (
      <Box>
        <Typography variant="subtitle2" sx={{ fontWeight: 600, mb: 0.5 }}>
          {dimensionLabel} — {metricLabel}
        </Typography>
        <Typography variant="body2" color="text.secondary">
          Aucune ventilation disponible.
        </Typography>
      </Box>
    );
  }

  const max = entries[0][1] || 1;

  return (
    <Box sx={{ display: "flex", flexDirection: "column", gap: 0.75 }}>
      <Typography variant="subtitle2" sx={{ fontWeight: 600 }}>
        {dimensionLabel} — {metricLabel}
      </Typography>
      {entries.map(([value, total]) => (
        <Box
          key={value}
          sx={{
            display: "grid",
            gridTemplateColumns: "minmax(96px, 30%) 1fr auto",
            alignItems: "center",
            gap: 1,
          }}
        >
          <Typography variant="body2" noWrap title={breakdownLabel(value)}>
            {breakdownLabel(value)}
          </Typography>
          <Box
            aria-hidden
            sx={{
              height: 8,
              borderRadius: 999,
              backgroundColor: alpha(theme.palette.primary.main, 0.15),
              overflow: "hidden",
            }}
          >
            <Box
              sx={{
                width: `${Math.max(2, (total / max) * 100)}%`,
                height: "100%",
                backgroundColor: theme.palette.primary.main,
              }}
            />
          </Box>
          <Typography
            variant="body2"
            sx={{ fontVariantNumeric: "lining-nums tabular-nums", fontWeight: 600 }}
          >
            {Number(total).toLocaleString("fr-FR")}
          </Typography>
        </Box>
      ))}
    </Box>
  );
}
