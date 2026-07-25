/**
 * Sparkline — a minimal inline SVG trend line (Epic 9, Story 9.1/9.2).
 *
 * Origin rules: one accent (the line uses the theme primary), no chartjs/heavy dep
 * (single-file bundle, AD-11), muted vs the hero number (thin stroke, low emphasis).
 * Renders nothing when there are < 2 points (a single point has no trend).
 */

import Box from "@mui/material/Box";
import { useTheme, alpha } from "@mui/material/styles";
import type { SeriesPoint } from "./types";

interface SparklineProps {
  points: SeriesPoint[];
  width?: number;
  height?: number;
  /** Accessible label (French). */
  ariaLabel?: string;
}

export default function Sparkline({
  points,
  width = 120,
  height = 32,
  ariaLabel = "Tendance sur la période",
}: SparklineProps) {
  const theme = useTheme();

  if (!points || points.length < 2) {
    return (
      <Box
        sx={{ width, height, display: "flex", alignItems: "center" }}
        data-testid="sparkline-empty"
      >
        <Box
          component="span"
          sx={{ color: "text.disabled", fontSize: "0.7rem", lineHeight: 1 }}
        >
          —
        </Box>
      </Box>
    );
  }

  const values = points.map((p) => p.value);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const pad = 2;
  const usableW = width - pad * 2;
  const usableH = height - pad * 2;

  const coords = points.map((p, i) => {
    const x = pad + (i / (points.length - 1)) * usableW;
    // Invert Y: higher value = higher on screen.
    const y = pad + (1 - (p.value - min) / span) * usableH;
    return `${x.toFixed(2)},${y.toFixed(2)}`;
  });

  const line = coords.join(" ");
  const area = `${pad},${height - pad} ${line} ${width - pad},${height - pad}`;
  const stroke = theme.palette.primary.main;

  return (
    <Box sx={{ width, height, lineHeight: 0 }} data-testid="sparkline">
      <svg
        width={width}
        height={height}
        viewBox={`0 0 ${width} ${height}`}
        role="img"
        aria-label={ariaLabel}
      >
        <polygon points={area} fill={alpha(stroke, 0.1)} stroke="none" />
        <polyline
          points={line}
          fill="none"
          stroke={stroke}
          strokeWidth={1.5}
          strokeLinejoin="round"
          strokeLinecap="round"
        />
      </svg>
    </Box>
  );
}
