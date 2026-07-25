/**
 * SmallMultiples — one mini line chart (sparkline) per connector, all on a
 * SHARED time axis (AC3 / T5 / UX-DR3).
 *
 * Charting decision (unchanged from prior story): hand-rolled SVG polyline.
 *
 * Story 8.8 restyling (rules 1-6):
 *   - 1.5px strokes, accent color (rule 1 / rule 3).
 *   - NO gridlines — clean minimal aesthetic (rule 5).
 *   - Area fill: accent at 8% opacity, fading to 0 at baseline.
 *   - Baseline removed as a visible line (implicit in the y=0 row).
 *
 * Shared time axis (T5.2): every chart is plotted against the SAME ordered date
 * domain (getDateDomain over all connectors), so x-positions line up across
 * connectors.
 *
 * Story 31.5: added contextEvents prop — event markers are drawn on the x-axis
 * of EVERY sparkline via EventAnnotationOverlay (one shared overlay per chart).
 * Markers are cross-source by design: a YouTube video_upload and a GitHub release
 * appear simultaneously on the same GA4 sessions sparkline. Additif — aucune
 * valeur de fact_daily_kpi n'est modifiée (AD-9).
 */

import { useTheme, alpha } from "@mui/material/styles";
import Box from "@mui/material/Box";
import Typography from "@mui/material/Typography";
import { aggregateByDateConnector } from "./dataUtils";
import type { Row, ContextEventMeta } from "./types";
import EventAnnotationOverlay from "./EventAnnotationOverlay";

interface SmallMultiplesProps {
  rows: Row[];
  connectors: string[];
  /** Shared, ordered date domain across all connectors (T5.2). */
  dateDomain: string[];
  metricLabel: string;
  metric: string;
  /**
   * Story 31.5: context event markers to overlay on each sparkline's time axis.
   * Already filtered by the caller (App.tsx category/platform filter).
   * When absent, no overlay is rendered (backward-compatible).
   */
  contextEvents?: ContextEventMeta[];
}

const CHART_W = 320;
const CHART_H = 68;
const PAD_X = 4;
const PAD_Y = 6; // more vertical breathing room

function Sparkline({
  domain,
  series,
  color,
  contextEvents = [],
}: {
  domain: string[];
  series: Map<string, number>;
  color: string;
  /** Story 31.5: events to draw as markers on the x-axis. */
  contextEvents?: ContextEventMeta[];
}) {
  const n = domain.length;
  let max = 0;
  for (const d of domain) {
    const v = series.get(d) ?? 0;
    if (v > max) max = v;
  }
  const innerW = CHART_W - PAD_X * 2;
  const innerH = CHART_H - PAD_Y * 2;

  const pts = domain.map((d, i) => {
    const x = PAD_X + (n <= 1 ? innerW / 2 : (i / (n - 1)) * innerW);
    const v = series.get(d) ?? 0;
    const y = PAD_Y + (max <= 0 ? innerH : innerH - (v / max) * innerH);
    return { x, y };
  });

  const polyPoints = pts.map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(" ");

  // Area fill path: line → down to baseline → back → close
  const baseline = PAD_Y + innerH;
  let areaPath = "";
  if (pts.length >= 2) {
    areaPath = `M ${pts[0].x.toFixed(1)},${pts[0].y.toFixed(1)}`;
    for (let i = 1; i < pts.length; i++) {
      areaPath += ` L ${pts[i].x.toFixed(1)},${pts[i].y.toFixed(1)}`;
    }
    areaPath += ` L ${pts[pts.length - 1].x.toFixed(1)},${baseline}`;
    areaPath += ` L ${pts[0].x.toFixed(1)},${baseline} Z`;
  }

  const areaFill = alpha(color, 0.08);
  // Story 31.5: event markers anchor at the baseline (x-axis bottom of chart area).
  const axisY = baseline;

  return (
    <svg
      width={CHART_W}
      height={CHART_H}
      role="img"
      aria-label="Mini-graphique"
      style={{ display: "block", maxWidth: "100%" }}
      // Dark mode: use CSS var for the fill — the alpha() call already handles it.
      aria-hidden={false}
    >
      {/* Area fill — accent at 8% opacity, fading at bottom */}
      {areaPath && (
        <path
          d={areaPath}
          fill={areaFill}
          strokeWidth={0}
        />
      )}

      {/* 1.5px line — accent color (rule 1 / rule 3) */}
      <polyline
        points={polyPoints}
        fill="none"
        stroke={color}
        strokeWidth={1.5}
        strokeLinejoin="round"
        strokeLinecap="round"
      />

      {/* Story 31.5: event annotation overlay — additive, zero metric impact (AD-9). */}
      {contextEvents.length > 0 && (
        <EventAnnotationOverlay
          events={contextEvents}
          domain={domain}
          padX={PAD_X}
          innerW={innerW}
          axisY={axisY}
        />
      )}
    </svg>
  );
}

export default function SmallMultiples({
  rows,
  connectors,
  dateDomain,
  metricLabel,
  metric,
  contextEvents = [],
}: SmallMultiplesProps) {
  const theme = useTheme();

  return (
    <Box sx={{ display: "flex", flexDirection: "column", gap: 1.5 }}>
      <Typography variant="subtitle2" sx={{ fontWeight: 600 }}>
        Par connecteur — {metricLabel}
      </Typography>
      {connectors.map((connector) => {
        const series = aggregateByDateConnector(rows, connector, metric);
        return (
          <Box key={connector}>
            <Typography
              variant="overline"
              color="text.secondary"
              sx={{ display: "block", mb: 0.25, lineHeight: 1.3 }}
            >
              {connector}
            </Typography>
            <Sparkline
              domain={dateDomain}
              series={series}
              color={theme.palette.primary.main}
              contextEvents={contextEvents}
            />
          </Box>
        );
      })}
    </Box>
  );
}
