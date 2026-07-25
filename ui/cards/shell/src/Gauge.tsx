/**
 * Gauge (jauge) — valeur vs cible/seuil, arc radial SVG (Story 9.2b).
 *
 * Exemples d'usage : CPA vs objectif, taux de complétion, score qualité.
 * Direction-aware : selon `direction`, dépasser la cible est bon ou mauvais.
 *   - "up_good"  : value >= target → tint vert (success).
 *   - "down_good": value <= target → tint vert (sous le seuil = bon, ex. CPA).
 *   - "neutral"  : toujours couleur accent, pas de tint sémantique.
 *
 * Arc radial : angle de 210° (−120° à +120° depuis bas), fond gris, remplissage accent.
 * Aucune dépendance graphique externe (AD-11). Light+dark via theme.
 * État vide designé quand value === undefined/null.
 */

import { useTheme, alpha } from "@mui/material/styles";
import Box from "@mui/material/Box";
import Typography from "@mui/material/Typography";

export interface GaugeProps {
  /** Valeur courante. */
  value: number | null | undefined;
  /** Cible ou seuil (optionnel). */
  target?: number | null;
  /** Direction sémantique. */
  direction?: "up_good" | "down_good" | "neutral";
  /** Unité affichée (ex: "€", "%"). */
  unit?: string;
  /** Label court affiché sous la valeur (ex: "CPA", "Complétion"). */
  label?: string;
  ariaLabel?: string;
  size?: number;
}

const ARC_ANGLE = 210; // degrés totaux
const START_ANGLE = (180 + (360 - ARC_ANGLE) / 2) * (Math.PI / 180);

function polarToXY(cx: number, cy: number, r: number, angleRad: number) {
  return {
    x: cx + r * Math.cos(angleRad),
    y: cy + r * Math.sin(angleRad),
  };
}

function arcPath(cx: number, cy: number, r: number, startA: number, endA: number): string {
  const s = polarToXY(cx, cy, r, startA);
  const e = polarToXY(cx, cy, r, endA);
  const large = endA - startA > Math.PI ? 1 : 0;
  return `M ${s.x.toFixed(2)} ${s.y.toFixed(2)} A ${r} ${r} 0 ${large} 1 ${e.x.toFixed(2)} ${e.y.toFixed(2)}`;
}

export default function Gauge({
  value,
  target,
  direction = "neutral",
  unit = "",
  label,
  ariaLabel = "Jauge",
  size = 120,
}: GaugeProps) {
  const theme = useTheme();

  // Empty state
  if (value === null || value === undefined) {
    return (
      <Box
        sx={{
          width: size,
          height: size * 0.7,
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          border: `1px dashed ${alpha(theme.palette.text.primary, 0.12)}`,
          borderRadius: 2,
          gap: 0.5,
        }}
        data-testid="gauge-empty"
        role="img"
        aria-label={`${ariaLabel} — aucune donnée`}
      >
        <Typography variant="body2" color="text.disabled">
          —
        </Typography>
        <Typography variant="caption" color="text.disabled">
          Donnée manquante
        </Typography>
      </Box>
    );
  }

  // Direction-aware tint
  let fillColor = theme.palette.primary.main;
  if (direction !== "neutral" && target !== null && target !== undefined) {
    const isGood =
      direction === "up_good" ? value >= target : value <= target;
    fillColor = isGood ? theme.palette.success.main : theme.palette.error.main;
  }

  // Arc math
  const cx = size / 2;
  const cy = size * 0.55; // slightly below center for arc aesthetics
  const r = size * 0.38;
  const strokeW = size * 0.07;

  const arcStart = START_ANGLE;
  const totalAngleRad = ARC_ANGLE * (Math.PI / 180);
  const arcEnd = arcStart + totalAngleRad;

  // Fill fraction: value/target or value/value*2 when no target.
  // Use explicit null/undefined check so target=0 (a valid threshold) is handled correctly (F-5).
  const effectiveMax =
    target !== null && target !== undefined
      ? Math.max(target * 1.5, value * 1.1, 1)
      : value * 2 || 1;
  const fraction = Math.min(1, Math.max(0, value / effectiveMax));
  // Cap fill arc at 95% of track so there is always a visible gap between fill
  // and track endpoint, avoiding visual ambiguity at extreme values (F-4 ui).
  const cappedFraction = Math.min(0.95, fraction);
  const fillEnd = arcStart + cappedFraction * totalAngleRad;
  const trackPath = arcPath(cx, cy, r, arcStart, arcEnd);
  const fillPath = cappedFraction > 0 ? arcPath(cx, cy, r, arcStart, fillEnd) : null;

  const trackColor = alpha(theme.palette.text.primary, 0.1);

  // Labels
  const valueFmt = value.toLocaleString("fr-FR", { maximumFractionDigits: 1 });
  // Use explicit null/undefined check so target=0 shows its label correctly (F-5).
  const targetFmt =
    target !== null && target !== undefined
      ? target.toLocaleString("fr-FR", { maximumFractionDigits: 1 })
      : undefined;

  // Dynamic fontSize: scale down when value string + unit is long, clamped to inner arc width.
  // Inner arc diameter = innerR * 2 ≈ size * 0.56. At 7 chars, the default 0.18*size fits;
  // beyond that we reduce proportionally so text never overprints the arc stroke (F-4 ui).
  const valueStr = `${valueFmt}${unit}`;
  const charCount = valueStr.length;
  const baseFontSize = size * 0.18;
  const valueFontSize = charCount > 7 ? Math.max(size * 0.11, baseFontSize * (7 / charCount)) : baseFontSize;

  return (
    <Box data-testid="gauge" role="img" aria-label={ariaLabel} sx={{ display: "inline-flex", flexDirection: "column", alignItems: "center" }}>
      <svg width={size} height={size * 0.7} viewBox={`0 0 ${size} ${size * 0.7}`} style={{ display: "block", overflow: "visible" }}>
        <title>{ariaLabel}</title>
        {/* Track arc */}
        <path
          d={trackPath}
          fill="none"
          stroke={trackColor}
          strokeWidth={strokeW}
          strokeLinecap="round"
        />
        {/* Fill arc */}
        {fillPath && (
          <path
            d={fillPath}
            fill="none"
            stroke={fillColor}
            strokeWidth={strokeW}
            strokeLinecap="round"
          />
        )}
        {/* Center value — fontSize scales down dynamically for long strings (F-4 ui). */}
        <text
          x={cx}
          y={cy + 4}
          textAnchor="middle"
          fontSize={valueFontSize}
          fontWeight={700}
          style={{ fontVariantNumeric: "lining-nums tabular-nums" }}
          fill={theme.palette.text.primary}
        >
          {valueFmt}{unit}
        </text>
        {/* Target label */}
        {targetFmt && (
          <text
            x={cx}
            y={cy + size * 0.15}
            textAnchor="middle"
            fontSize={size * 0.1}
            fill={theme.palette.text.secondary}
          >
            cible : {targetFmt}{unit}
          </text>
        )}
      </svg>
      {label && (
        <Typography
          variant="caption"
          color="text.secondary"
          sx={{ mt: 0.75, fontVariantNumeric: "lining-nums tabular-nums" }}
        >
          {label}
        </Typography>
      )}
    </Box>
  );
}
