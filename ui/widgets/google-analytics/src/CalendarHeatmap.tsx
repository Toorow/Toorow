/**
 * CalendarHeatmap — hand-rolled SVG 90-day calendar heatmap (AC1 / T3).
 *
 * Story 8.8 restyling (rules 1-6):
 *   - Accent-scale cells: accent color (#FF99C8) alpha 0.10 → 1.0 over normalized range.
 *   - Hairline grid: 0.5px stroke on #EBEBF3-derived divider (no filled border by default).
 *   - Refined tooltips: native SVG <title> — stays accessible; visual tooltip via
 *     SVG <text> overlay would require state (kept as <title> for bundle size).
 *   - Cell size slightly increased for better touch targets (CELL = 15px).
 *   - Out-of-range cells rendered as a hairline square (visible grid skeleton).
 *
 * Library decision (unchanged): HAND-ROLLED SVG — see original rationale.
 *
 * Color ramp: accent primary color at alpha 0.10..1.0 mapped to normalized value 0→1.
 * This respects light/dark re-theming — no hardcoded hex (AD-11 / UX-DR5).
 */

import { useMemo } from "react";
import { useTheme, alpha } from "@mui/material/styles";
import Box from "@mui/material/Box";
import Typography from "@mui/material/Typography";

const DAY_MS = 24 * 60 * 60 * 1000;
const CELL = 15;        // px, cell edge (slightly larger for clarity)
const GAP = 2;          // px, gap between cells (tight grid feel)
const STEP = CELL + GAP;
const MONTH_LABEL_H = 16;
const DOW_LABEL_W = 22;

const MONTHS_FR = [
  "janv.", "févr.", "mars", "avr.", "mai", "juin",
  "juil.", "août", "sept.", "oct.", "nov.", "déc.",
];
const DOW_FR = ["L", "M", "M", "J", "V", "S", "D"]; // Monday-first

/** Context event marker (Story 4.4, AC7). */
export interface ContextEvent {
  id: string | null;
  event_date: string;
  type: string;
  label: string;
}

interface CalendarHeatmapProps {
  /** Map<"YYYY-MM-DD", value> — the metric total per day. */
  values: Map<string, number>;
  dateRange: { start: string; end: string };
  onDayClick: (date: string) => void;
  metricLabel: string;
  /** Context event markers from meta.context_events (Story 4.4, AC7). */
  contextEvents?: ContextEvent[];
}

function toISO(d: Date): string {
  return d.toISOString().slice(0, 10);
}

/** Monday=0 … Sunday=6 (calendar rows are Monday-first). */
function mondayIndex(d: Date): number {
  return (d.getUTCDay() + 6) % 7;
}

export default function CalendarHeatmap({
  values,
  dateRange,
  onDayClick,
  metricLabel,
  contextEvents = [],
}: CalendarHeatmapProps) {
  const theme = useTheme();

  // Build a map of event_date -> list of context events for fast lookup.
  const eventsByDate = useMemo(() => {
    const map = new Map<string, ContextEvent[]>();
    for (const evt of contextEvents) {
      const existing = map.get(evt.event_date) ?? [];
      map.set(evt.event_date, [...existing, evt]);
    }
    return map;
  }, [contextEvents]);

  const { cells, weeks, monthLabels, maxValue } = useMemo(() => {
    const start = new Date(`${dateRange.start}T00:00:00Z`);
    const end = new Date(`${dateRange.end}T00:00:00Z`);

    // Grid starts on the Monday on/before the range start.
    const gridStart = new Date(start);
    gridStart.setUTCDate(gridStart.getUTCDate() - mondayIndex(start));

    let max = 0;
    for (const v of values.values()) if (v > max) max = v;

    const cellList: Array<{
      x: number;
      y: number;
      date: string;
      value: number | null;
      inRange: boolean;
    }> = [];
    const monthList: Array<{ x: number; label: string }> = [];
    let lastMonth = -1;
    let col = 0;

    for (let t = gridStart.getTime(); t <= end.getTime(); t += DAY_MS) {
      const d = new Date(t);
      const iso = toISO(d);
      col = Math.floor((t - gridStart.getTime()) / DAY_MS / 7);
      const rowIdx = mondayIndex(d);
      const inRange = iso >= dateRange.start && iso <= dateRange.end;
      const value = inRange ? (values.get(iso) ?? 0) : null;

      cellList.push({ x: col * STEP, y: rowIdx * STEP, date: iso, value, inRange });

      // Month label at the first column where a new month appears.
      const m = d.getUTCMonth();
      if (m !== lastMonth) {
        monthList.push({ x: col * STEP, label: MONTHS_FR[m] });
        lastMonth = m;
      }
    }

    return { cells: cellList, weeks: col + 1, monthLabels: monthList, maxValue: max };
  }, [values, dateRange]);

  const gridW = weeks * STEP;
  const svgW = DOW_LABEL_W + gridW;
  const svgH = MONTH_LABEL_H + 7 * STEP;

  // Colors from theme tokens (no hex literals).
  // Hairline: #EBEBF3-derived divider at 0.15 opacity (rule 2).
  const hairlineStroke = alpha(theme.palette.text.primary, 0.08);
  const labelColor = theme.palette.text.secondary;
  // Out-of-range cell: visible skeleton using #EBEBF3-derived tone.
  const skeletonFill = alpha(theme.palette.text.primary, 0.04);

  function fillFor(value: number | null): string {
    if (value === null) return skeletonFill;
    if (maxValue <= 0) return skeletonFill;
    const norm = value / maxValue;
    if (norm <= 0) return skeletonFill;
    // Accent-scale: from 10% opacity (nearly empty) to 100% (max value).
    return alpha(theme.palette.primary.main, 0.10 + norm * 0.90);
  }

  // Warning color for context event ring (rule 1: only event uses non-accent semantic).
  const eventStroke = theme.palette.warning.main;

  return (
    <Box>
      <Typography variant="subtitle2" sx={{ mb: 0.75, fontWeight: 600 }}>
        {metricLabel} — 90 jours
      </Typography>
      <Box sx={{ overflowX: "auto" }}>
        <svg
          width={svgW}
          height={svgH}
          role="img"
          aria-label={`Heatmap calendaire ${metricLabel} sur 90 jours`}
          style={{ display: "block" }}
        >
          {/* Month labels */}
          {monthLabels.map((m, i) => (
            <text
              key={`m-${i}`}
              x={DOW_LABEL_W + m.x}
              y={MONTH_LABEL_H - 4}
              fontSize={10}
              fontFamily="inherit"
              fill={labelColor}
              fontWeight={500}
            >
              {m.label}
            </text>
          ))}

          {/* Day-of-week labels (Mon-first; show M/W/F to reduce clutter) */}
          {DOW_FR.map((d, i) =>
            i % 2 === 0 ? (
              <text
                key={`dow-${i}`}
                x={0}
                y={MONTH_LABEL_H + i * STEP + CELL - 3}
                fontSize={9}
                fontFamily="inherit"
                fill={labelColor}
              >
                {d}
              </text>
            ) : null,
          )}

          {/* Grid cells */}
          <g transform={`translate(${DOW_LABEL_W}, ${MONTH_LABEL_H})`}>
            {cells.map((c) => {
              const hasEvent = eventsByDate.has(c.date);
              return (
                <g key={c.date}>
                  <rect
                    x={c.x}
                    y={c.y}
                    width={CELL}
                    height={CELL}
                    rx={2.5}
                    fill={fillFor(c.value)}
                    stroke={hasEvent ? eventStroke : hairlineStroke}
                    strokeWidth={hasEvent ? 1.5 : 0.5}
                    style={c.inRange ? { cursor: "pointer" } : undefined}
                    onClick={c.inRange ? () => onDayClick(c.date) : undefined}
                    tabIndex={c.inRange ? 0 : undefined}
                    role={c.inRange ? "button" : undefined}
                    aria-label={
                      c.inRange
                        ? `${c.date} : ${(c.value ?? 0).toLocaleString("fr-FR")}`
                        : undefined
                    }
                    onKeyDown={
                      c.inRange
                        ? (e) => {
                            if (e.key === "Enter" || e.key === " ") onDayClick(c.date);
                          }
                        : undefined
                    }
                  >
                    {c.inRange && (
                      <title>
                        {`${c.date} — ${metricLabel} : ${(c.value ?? 0).toLocaleString("fr-FR")}`}
                      </title>
                    )}
                  </rect>

                  {/* Context event dot marker (Story 4.4 AC7) */}
                  {hasEvent && c.inRange && (
                    <circle
                      cx={c.x + CELL - 3.5}
                      cy={c.y + 3.5}
                      r={2.5}
                      fill={eventStroke}
                      aria-label={
                        `Événement : ${(eventsByDate.get(c.date) ?? []).map((e) => `${e.label} (${e.type})`).join(", ")}`
                      }
                      role="img"
                      style={{ pointerEvents: "none" }}
                    >
                      <title>
                        {(eventsByDate.get(c.date) ?? [])
                          .map((e) => `${e.label} [${e.type}]`)
                          .join(" · ")}
                      </title>
                    </circle>
                  )}
                </g>
              );
            })}
          </g>
        </svg>
      </Box>
    </Box>
  );
}
