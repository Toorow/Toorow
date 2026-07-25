/**
 * EventAnnotationOverlay — SVG overlay that draws event markers on a time axis.
 *
 * Story 31.5: superimposes context_events as typed SVG markers over any time-series
 * chart that shares the same date domain + coordinate system. The overlay is a
 * transparent <g> layer positioned identically to the chart's plot area.
 *
 * Marker shapes (from dim_event_type.default_marker):
 *   triangle  — pointing up (content events: video_upload, blog_post, …)
 *   diamond   — rotated square (engineering: release, deployment)
 *   flag      — vertical line + flag head (marketing: campaign_launch, promotion)
 *   star      — 5-point star (commerce: product_launch)
 *   line      — vertical rule (commerce: price_change)
 *   pin       — teardrop (business: milestone)
 *   cross     — × mark (operations: incident)
 *
 * Design decisions:
 *  - All paths use `currentColor` fill/stroke — callers pass `color` (from MUI
 *    theme via categoryPaletteKey) so the overlay respects light/dark mode (AD-11).
 *  - Markers are rendered at bottom of the chart area (y = chartH) pointing upward,
 *    so they sit on the x-axis and never occlude the data lines.
 *  - Tooltip: native SVG <title> for accessibility; no additional state needed
 *    (consistent with CalendarHeatmap approach — keep bundle size minimal).
 *  - AD-9: purely additive — no metric value is modified.
 *  - AD-10: zero network calls — data arrives in the envelope.
 */

import { useMemo } from "react";
import { useTheme } from "@mui/material/styles";
import type { ContextEventMeta } from "./types";
import {
  resolveMarkerShape,
  resolveCategory,
  categoryPaletteKey,
  dateToX,
  type MarkerShape,
} from "./eventMarkers";

// Marker geometry constants (px, relative to the marker anchor at the x-axis).
const MARKER_H = 10; // height of the marker above the axis
const HALF_W = 4;    // half-width of the marker footprint

// ---------------------------------------------------------------------------
// Per-shape SVG path builders (anchor at bottom center: 0,0)
// ---------------------------------------------------------------------------

/** Triangle pointing upward. */
function trianglePath(): string {
  return `M 0,0 L -${HALF_W},${MARKER_H} L ${HALF_W},${MARKER_H} Z`;
}

/** Diamond (rotated square). */
function diamondPath(): string {
  return `M 0,0 L -${HALF_W},${MARKER_H / 2} L 0,${MARKER_H} L ${HALF_W},${MARKER_H / 2} Z`;
}

/** Flag: vertical pole + small rectangular flag head. */
function flagPath(): string {
  const poleX = -2;
  const headW = HALF_W + 1;
  const headH = MARKER_H / 2;
  return (
    `M ${poleX},0 L ${poleX},${MARKER_H} ` +
    `M ${poleX},${headH} L ${poleX + headW},${headH * 0.6} L ${poleX},${headH * 0.2} Z`
  );
}

/** 5-point star (simplified). */
function starPath(): string {
  const cx = 0;
  const cy = MARKER_H / 2;
  const r1 = HALF_W;
  const r2 = HALF_W * 0.45;
  const pts: string[] = [];
  for (let i = 0; i < 5; i++) {
    const outerAngle = (i * 4 * Math.PI) / 5 - Math.PI / 2;
    const innerAngle = outerAngle + (2 * Math.PI) / 10;
    pts.push(`${(cx + r1 * Math.cos(outerAngle)).toFixed(2)},${(cy + r1 * Math.sin(outerAngle)).toFixed(2)}`);
    pts.push(`${(cx + r2 * Math.cos(innerAngle)).toFixed(2)},${(cy + r2 * Math.sin(innerAngle)).toFixed(2)}`);
  }
  return `M ${pts.join(" L ")} Z`;
}

/** Vertical line rule (for price_change). */
function linePath(): string {
  return `M 0,0 L 0,${MARKER_H}`;
}

/** Teardrop pin (for milestone). */
function pinPath(): string {
  const r = HALF_W * 0.7;
  return (
    `M 0,0 ` +
    `Q -${HALF_W},${MARKER_H * 0.5} 0,${MARKER_H} ` +
    `Q ${HALF_W},${MARKER_H * 0.5} 0,0 Z`
  );
}

/** Cross (×) for incident. */
function crossPath(): string {
  const d = HALF_W * 0.7;
  const cy = MARKER_H / 2;
  return (
    `M -${d},${cy - d} L ${d},${cy + d} ` +
    `M ${d},${cy - d} L -${d},${cy + d}`
  );
}

function buildPath(shape: MarkerShape): string {
  switch (shape) {
    case "triangle": return trianglePath();
    case "diamond":  return diamondPath();
    case "flag":     return flagPath();
    case "star":     return starPath();
    case "line":     return linePath();
    case "pin":      return pinPath();
    case "cross":    return crossPath();
    default:         return pinPath();
  }
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export interface EventAnnotationOverlayProps {
  /**
   * Context events to render (already filtered by the caller if needed).
   * Only events whose date falls within `domain` are rendered.
   */
  events: ContextEventMeta[];
  /** Ordered date strings ["YYYY-MM-DD", …] matching the chart x-axis. */
  domain: string[];
  /** X offset of the plot area inside the parent SVG (px). */
  padX: number;
  /** Usable inner width (px) = chartW - 2*padX. */
  innerW: number;
  /** Y position of the x-axis inside the parent SVG (px), where markers anchor. */
  axisY: number;
}

/**
 * Transparent SVG <g> layer that draws typed event markers on the time axis.
 * Drop this inside an SVG alongside the sparkline/area chart elements.
 * The caller controls z-order (render after the data lines so markers sit on top).
 */
export default function EventAnnotationOverlay({
  events,
  domain,
  padX,
  innerW,
  axisY,
}: EventAnnotationOverlayProps) {
  const theme = useTheme();

  // Group events by date so multiple events on the same day share one anchor x.
  const byDate = useMemo(() => {
    const m = new Map<string, ContextEventMeta[]>();
    for (const e of events) {
      const existing = m.get(e.event_date) ?? [];
      m.set(e.event_date, [...existing, e]);
    }
    return m;
  }, [events]);

  if (byDate.size === 0) return null;

  return (
    <g aria-label="Annotations événements" role="group">
      {[...byDate.entries()].map(([date, evts]) => {
        const x = dateToX(date, domain, padX, innerW);
        if (x === null) return null;

        // When multiple events share a date, pick the one whose marker has the
        // highest visual priority (cross > star > diamond > flag > triangle > pin > line).
        const PRIORITY: Record<MarkerShape, number> = {
          cross: 7, star: 6, diamond: 5, flag: 4, triangle: 3, pin: 2, line: 1,
        };
        const sorted = [...evts].sort((a, b) => {
          const pa = PRIORITY[resolveMarkerShape(a)] ?? 0;
          const pb = PRIORITY[resolveMarkerShape(b)] ?? 0;
          return pb - pa;
        });
        const primary = sorted[0];
        const shape = resolveMarkerShape(primary);
        const cat = resolveCategory(primary);
        const palKey = categoryPaletteKey(cat);
        const color = theme.palette[palKey]?.main ?? theme.palette.primary.main;

        // Tooltip: all labels for the date.
        const tooltipText = evts
          .map((e) => `${e.label} [${e.type}]${e.source && e.source !== "manual" ? ` · ${e.source}` : ""}`)
          .join(" · ");

        const ariaLabel = `Événement ${date} : ${evts.map((e) => `${e.label} (${e.type})`).join(", ")}`;

        const isStroke = shape === "line" || shape === "cross" || shape === "flag";
        const isFill = !isStroke || shape === "flag";

        return (
          <g
            key={date}
            transform={`translate(${x.toFixed(1)},${axisY})`}
            role="img"
            aria-label={ariaLabel}
          >
            <title>{tooltipText}</title>
            <path
              d={buildPath(shape)}
              fill={isFill ? color : "none"}
              stroke={color}
              strokeWidth={isStroke ? 1.5 : 0.5}
              strokeLinejoin="round"
              strokeLinecap="round"
              opacity={0.85}
              style={{ pointerEvents: "none" }}
            />
            {/* Invisible hit target for tooltip (wider than marker for easier hover) */}
            <rect
              x={-(HALF_W + 2)}
              y={-(MARKER_H + 2)}
              width={(HALF_W + 2) * 2}
              height={MARKER_H + 4}
              fill="transparent"
              style={{ cursor: "default" }}
            />
          </g>
        );
      })}
    </g>
  );
}
