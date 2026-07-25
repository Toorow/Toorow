/**
 * Sparkline -- inline SVG 7-day volume line chart (Story 8.4).
 *
 * Rule 3: thin 1.5px line, accent color, no fill.
 * Rule 4: No MUI chart; pure SVG for minimal footprint.
 * Rule 1: Accent (#FF99C8) for the line; no other colors.
 *
 * Props:
 *   values: number[] -- 7 ints (volume_7d), oldest left, newest right.
 *   width:  number   -- SVG width in px (default 72)
 *   height: number   -- SVG height in px (default 28)
 */

const ACCENT = "#FF99C8";

interface SparklineProps {
  values: number[];
  width?: number;
  height?: number;
  label?: string;
}

export default function Sparkline({
  values,
  width = 72,
  height = 28,
  label = "Volume 7 jours",
}: SparklineProps) {
  // Pad or trim to 7 values.
  const data = values.length >= 7 ? values.slice(-7) : [...Array(7 - values.length).fill(0), ...values];

  const max = Math.max(...data, 1);
  const min = 0;
  const range = max - min || 1;

  const padX = 2;
  const padY = 3;
  const innerW = width - padX * 2;
  const innerH = height - padY * 2;
  const stepX = data.length > 1 ? innerW / (data.length - 1) : innerW;

  // Scale y: max at top (padY), min at bottom (padY + innerH)
  const toY = (v: number) => padY + innerH - ((v - min) / range) * innerH;
  const toX = (i: number) => padX + i * stepX;

  const points = data.map((v, i) => `${toX(i).toFixed(1)},${toY(v).toFixed(1)}`).join(" ");
  const polyline = `M ${points.replace(/ /g, " L ")}`;

  const isAllZero = data.every((v) => v === 0);

  return (
    <svg
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      aria-label={label}
      role="img"
      data-testid="sparkline-svg"
      style={{ display: "inline-block", verticalAlign: "middle", overflow: "visible" }}
    >
      {isAllZero ? (
        /* Flat muted line when no data */
        <line
          x1={padX}
          y1={height / 2}
          x2={width - padX}
          y2={height / 2}
          stroke="rgba(235,235,243,0.4)"
          strokeWidth={1}
          strokeDasharray="2 3"
        />
      ) : (
        <path
          d={polyline}
          fill="none"
          stroke={ACCENT}
          strokeWidth={1.5}
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      )}
    </svg>
  );
}
