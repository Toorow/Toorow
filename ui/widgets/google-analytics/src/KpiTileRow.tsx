/**
 * KpiTileRow — a row of tabular-numeral KPI hero tiles (Story 8.8 / AC2 / T4.2).
 *
 * Covers the seeded metrics: sessions, active_users, conversions.
 * Each tile shows the current-period total as a HERO number and the delta vs
 * the previous equivalent period below (small, muted, direction-aware).
 *
 * Story 8.8 (R6):
 *   - Accepts optional metric_definitions from the envelope.
 *   - Passes the relevant definition to each KpiTile.
 *   - Renders a collapsible "Définitions" section below the tiles when definitions
 *     are present — this is the LLM-comment grounding surfaced to humans.
 */

import { useState } from "react";
import Box from "@mui/material/Box";
import Collapse from "@mui/material/Collapse";
import Typography from "@mui/material/Typography";
import { useTheme, alpha } from "@mui/material/styles";
import KpiTile from "./KpiTile";
import { computeDeltas } from "./dataUtils";
import { METRICS, METRIC_LABELS } from "./types";
import type { Row, MetricDefinition } from "./types";

interface KpiTileRowProps {
  rows: Row[];
  dateRange: { start: string; end: string };
  /** R6: optional per-metric definitions from envelope data. */
  metricDefinitions?: Record<string, MetricDefinition>;
}

export default function KpiTileRow({ rows, dateRange, metricDefinitions }: KpiTileRowProps) {
  const [definitionsOpen, setDefinitionsOpen] = useState(false);
  const theme = useTheme();
  const hasDefinitions = metricDefinitions && Object.keys(metricDefinitions).length > 0;

  return (
    <Box>
      {/* KPI tiles row */}
      <Box sx={{ display: "flex", gap: 1.5, flexWrap: "wrap" }}>
        {METRICS.map((metric) => {
          const { current, deltaPct } = computeDeltas(rows, metric, dateRange);
          return (
            <KpiTile
              key={metric}
              label={METRIC_LABELS[metric] ?? metric}
              currentValue={current}
              deltaPct={deltaPct}
              definition={metricDefinitions?.[metric]}
            />
          );
        })}
      </Box>

      {/* R6: collapsible Définitions section (only when definitions are present) */}
      {hasDefinitions && (
        <Box sx={{ mt: 1.5 }}>
          <Box
            component="button"
            onClick={() => setDefinitionsOpen((v) => !v)}
            aria-expanded={definitionsOpen}
            aria-controls="metric-definitions-panel"
            sx={{
              all: "unset",
              cursor: "pointer",
              display: "inline-flex",
              alignItems: "center",
              gap: 0.5,
              color: "text.secondary",
              "&:hover": { color: "text.primary" },
            }}
            data-testid="definitions-toggle"
          >
            <Typography variant="caption" sx={{ fontWeight: 500 }}>
              {definitionsOpen ? "▾" : "▸"} Définitions des métriques
            </Typography>
          </Box>

          <Collapse in={definitionsOpen} id="metric-definitions-panel">
            <Box
              sx={{
                mt: 1,
                p: 2,
                borderRadius: 2,
                border: `1px solid ${alpha(theme.palette.text.primary, 0.08)}`,
                display: "flex",
                flexDirection: "column",
                gap: 1.5,
              }}
              data-testid="definitions-panel"
            >
              {METRICS.filter((m) => metricDefinitions![m]).map((metric) => {
                const def = metricDefinitions![metric];
                return (
                  <Box key={metric}>
                    <Typography
                      variant="overline"
                      color="text.secondary"
                      sx={{ display: "block", lineHeight: 1.3, mb: 0.25 }}
                    >
                      {METRIC_LABELS[metric] ?? metric}
                      {def.unit ? ` (${def.unit})` : ""}
                    </Typography>
                    <Typography variant="body2" sx={{ mb: 0.25, lineHeight: 1.55 }}>
                      {def.definition}
                    </Typography>
                    {def.caveats && (
                      <Typography
                        variant="caption"
                        color="text.secondary"
                        sx={{ fontStyle: "italic" }}
                      >
                        {def.caveats}
                      </Typography>
                    )}
                  </Box>
                );
              })}
            </Box>
          </Collapse>
        </Box>
      )}
    </Box>
  );
}
