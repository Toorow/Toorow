/**
 * Donut (répartition) — parts en SVG arc, total centré, légende valeurs + % (Story 9.2b).
 *
 * Exemples : conversions par source, new vs returning, répartition device.
 * Accent-scale (séquence de couleurs Origin). Centre : total + unité.
 * Légende : libellé, valeur, pourcentage.
 * Aucune dépendance graphique externe (AD-11). Light+dark via theme.
 * État vide designé quand slices est vide ou total = 0.
 */

import { useTheme, alpha } from "@mui/material/styles";
import Box from "@mui/material/Box";
import Typography from "@mui/material/Typography";
import { getVizPalette } from "./vizTheme";

export interface DonutSlice {
  label: string;
  value: number;
  /** Couleur forcée (sinon séquence accent-scale). */
  color?: string;
}

export interface DonutProps {
  slices: DonutSlice[];
  /** Unité affichée au centre (ex: "conversions"). */
  unit?: string;
  ariaLabel?: string;
  size?: number;
}

// Story 23.1 (AC5): slice colors come from the theme-driven viz contract
// (org branding reaches the donut) — no module-level color constants.
function sliceColor(categorical: string[], idx: number, override?: string): string {
  return override ?? categorical[idx % categorical.length] ?? categorical[0]!;
}

interface ArcDesc {
  d: string;
  color: string;
  label: string;
  value: number;
  pct: number;
}

function buildArcs(slices: DonutSlice[], total: number, cx: number, cy: number, r: number, innerR: number, categorical: string[]): ArcDesc[] {
  let angle = -Math.PI / 2; // start at top
  return slices.map((s, i) => {
    // Clamp negative values to 0 so degenerate backwards arcs never render (F-6).
    const frac = total > 0 ? Math.max(0, s.value / total) : 0;
    const sweep = frac * 2 * Math.PI;
    const endAngle = angle + sweep;

    const x1 = cx + r * Math.cos(angle);
    const y1 = cy + r * Math.sin(angle);
    const x2 = cx + r * Math.cos(endAngle);
    const y2 = cy + r * Math.sin(endAngle);
    const ix1 = cx + innerR * Math.cos(endAngle);
    const iy1 = cy + innerR * Math.sin(endAngle);
    const ix2 = cx + innerR * Math.cos(angle);
    const iy2 = cy + innerR * Math.sin(angle);
    const large = sweep > Math.PI ? 1 : 0;

    const d =
      `M ${x1.toFixed(2)} ${y1.toFixed(2)}` +
      ` A ${r} ${r} 0 ${large} 1 ${x2.toFixed(2)} ${y2.toFixed(2)}` +
      ` L ${ix1.toFixed(2)} ${iy1.toFixed(2)}` +
      ` A ${innerR} ${innerR} 0 ${large} 0 ${ix2.toFixed(2)} ${iy2.toFixed(2)}` +
      ` Z`;

    angle = endAngle;
    return {
      d,
      color: sliceColor(categorical, i, s.color),
      label: s.label,
      value: s.value,
      // Use clamped frac so negatives show 0 % rather than negative percentages.
      pct: Math.round(frac * 100),
    };
  });
}

export default function Donut({
  slices,
  unit,
  ariaLabel = "Graphique en anneau",
  size = 120,
}: DonutProps) {
  const theme = useTheme();
  const viz = getVizPalette(theme);

  // Empty when no slices, all zero, or total is non-positive (negative-only inputs).
  const isEmpty = slices.length === 0 || slices.every((s) => s.value === 0) || slices.reduce((acc, s) => acc + Math.max(0, s.value), 0) <= 0;
  if (isEmpty) {
    return (
      <Box
        sx={{
          width: size,
          height: size,
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          border: `1px dashed ${alpha(theme.palette.text.primary, 0.12)}`,
          borderRadius: "50%",
          gap: 0.5,
        }}
        data-testid="donut-empty"
        role="img"
        aria-label={`${ariaLabel} — aucune donnée`}
      >
        <Typography variant="body2" color="text.disabled">
          —
        </Typography>
        <Typography variant="caption" color="text.disabled" sx={{ textAlign: "center", px: 1 }}>
          Aucune donnée
        </Typography>
      </Box>
    );
  }

  const total = slices.reduce((acc, s) => acc + s.value, 0);
  const cx = size / 2;
  const cy = size / 2;
  const r = size * 0.45;
  const innerR = size * 0.28;

  const arcs = buildArcs(slices, total, cx, cy, r, innerR, viz.categorical);
  const totalFmt = total.toLocaleString("fr-FR");

  return (
    <Box data-testid="donut" sx={{ display: "flex", alignItems: "center", gap: 2, flexWrap: "wrap" }}>
      <Box sx={{ position: "relative", flexShrink: 0 }}>
        <svg
          width={size}
          height={size}
          viewBox={`0 0 ${size} ${size}`}
          role="img"
          aria-label={ariaLabel}
          style={{ display: "block" }}
        >
          <title>{ariaLabel}</title>
          {arcs.map((arc) => (
            <path
              key={arc.label}
              d={arc.d}
              fill={arc.color}
              stroke={theme.palette.background.paper}
              strokeWidth={1.5}
            />
          ))}
          {/* Centre : total */}
          <text
            x={cx}
            y={cy - 4}
            textAnchor="middle"
            fontSize={size * 0.16}
            fontWeight={700}
            style={{ fontVariantNumeric: "lining-nums tabular-nums" }}
            fill={theme.palette.text.primary}
          >
            {totalFmt}
          </text>
          {unit && (
            <text
              x={cx}
              y={cy + size * 0.11}
              textAnchor="middle"
              fontSize={size * 0.09}
              fill={theme.palette.text.secondary}
            >
              {unit}
            </text>
          )}
        </svg>
      </Box>

      {/* Légende */}
      <Box
        sx={{ display: "flex", flexDirection: "column", gap: 0.5 }}
        data-testid="donut-legend"
      >
        {arcs.map((arc) => (
          <Box key={arc.label} sx={{ display: "flex", alignItems: "center", gap: 0.75 }}>
            <Box
              sx={{
                width: 10,
                height: 10,
                borderRadius: "50%",
                bgcolor: arc.color,
                flexShrink: 0,
              }}
            />
            <Typography variant="caption" color="text.secondary" sx={{ flexShrink: 0 }}>
              {arc.label}
            </Typography>
            <Typography
              variant="caption"
              sx={{ fontVariantNumeric: "lining-nums tabular-nums", ml: 0.5 }}
            >
              {arc.value.toLocaleString("fr-FR")}
            </Typography>
            <Typography variant="caption" color="text.secondary">
              ({arc.pct} %)
            </Typography>
          </Box>
        ))}
      </Box>
    </Box>
  );
}
