/**
 * Gauge (jauge) — valeur vs cible/seuil, arc radial SVG (Story 9.2b).
 *
 * Exemples d'usage : CPA vs objectif, taux de complétion, score qualité.
 * Direction-aware : selon `direction`, dépasser la cible est bon ou mauvais.
 *   - "up_good"  : value >= target → tint vert (success).
 *   - "down_good": value <= target → tint vert (sous le seuil = bon, ex. CPA).
 *   - "neutral"  : toujours couleur accent, pas de tint sémantique.
 *
 * NO TARGET, NO VERDICT (CAV-08). When `target` is null/undefined there is no second
 * operand, so the arc keeps the neutral accent whatever `direction` says, `data-verdict`
 * reads "none", and the gauge prints "No target set" instead of leaving the slot empty.
 * A gauge that compares against nothing must not look like one that compares favourably.
 *
 * THE BOX CONTAINS THE ARC (story 76-8). The `<svg>` height was fixed at
 * `size * 0.7` while the arc's lowest point falls at `cy + r + stroke/2`, i.e.
 * `size * 0.965`: with `overflow: visible` the arc left its box and printed over
 * the label beneath the gauge, and « 30EUR » with no space landed on
 * "No target set". The height is now DERIVED from the geometry, and the amount
 * goes through `formatMeasure` ("€30", never "30EUR").
 *
 * Arc radial : angle de 210° (−120° à +120° depuis bas), fond gris, remplissage accent.
 * Aucune dépendance graphique externe (AD-11). Light+dark via theme.
 * État vide designé quand value === undefined/null.
 */

import { verdictColor, verdictTone } from "./verdictTone";
import { formatMeasure } from "./viz/theme/formatters";
import { Box, Typography, alpha, useTheme } from "@toorow/shell";
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
/**
 * cy (0.55) + r (0.38) + half the stroke (0.035) + 2 % of margin: the box
 * contains the arc instead of letting it spill over what follows.
 */
const SVG_HEIGHT_RATIO = 0.55 + 0.38 + 0.035 + 0.02;

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
  // The empty box follows the full one: two different heights would make the
  // composition jump the moment a value arrives.
  const boxHeight = size * SVG_HEIGHT_RATIO;

  // Empty state
  if (value === null || value === undefined) {
    return (
      <Box
        sx={{
          width: size,
          height: boxHeight,
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

  // A verdict needs two operands. Without a target there is nothing to compare the
  // value to, so the arc keeps the neutral accent and the gauge states the absence
  // rather than painting a colour that reads as a judgement (CAV-08).
  // `target = 0` is a real threshold, hence the explicit null/undefined check (F-5).
  const hasTarget = target !== null && target !== undefined;
  // ONE function decides every verdict colour on a card (story 76-8, round 2).
  // Landing exactly on the target is meeting it, hence `zeroIsFavourable`.
  const tone = verdictTone(hasTarget ? value - target : null, direction, {
    zeroIsFavourable: true,
  });
  const verdict: "good" | "bad" | "none" =
    tone === "favourable" ? "good" : tone === "unfavourable" ? "bad" : "none";

  const fillColor = verdictColor(theme, tone, theme.palette.primary.main);

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
  const effectiveMax = hasTarget
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

  // Labels — the value carries its unit (currency, %, word) and never welded to
  // the digits: `formatMeasure` picks the shape from the governed unit.
  const valueFmt = formatMeasure(value, unit);
  // Use explicit null/undefined check so target=0 shows its label correctly (F-5).
  const targetFmt = hasTarget ? formatMeasure(target, unit) : undefined;

  // Dynamic fontSize: scale down when value string + unit is long, clamped to inner arc width.
  // Inner arc diameter = innerR * 2 ≈ size * 0.56. At 7 chars, the default 0.18*size fits;
  // beyond that we reduce proportionally so text never overprints the arc stroke (F-4 ui).
  const charCount = valueFmt.length;
  const baseFontSize = size * 0.18;
  const valueFontSize = charCount > 7 ? Math.max(size * 0.11, baseFontSize * (7 / charCount)) : baseFontSize;

  return (
    <Box
      data-testid="gauge"
      data-verdict={verdict}
      role="img"
      aria-label={ariaLabel}
      sx={{ display: "inline-flex", flexDirection: "column", alignItems: "center" }}
    >
      <svg
        width={size}
        height={boxHeight}
        viewBox={`0 0 ${size} ${boxHeight}`}
        style={{ display: "block" }}
      >
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
          {valueFmt}
        </text>
      </svg>
      {/* The target — or its stated absence — sits UNDER the arc, never inside
          it. Inside, the width available at that height is `2·√(r² − dy²)`
          ≈ 84 px on a 120 px gauge, and the sentence overran the stroke. A
          verdict that overlaps the measure it comments on does not read
          (story 76-8). */}
      {targetFmt !== undefined ? (
        <Typography
          variant="caption"
          color="text.secondary"
          sx={{ mt: 0.25, fontVariantNumeric: "lining-nums tabular-nums" }}
          data-testid="gauge-target"
        >
          Target: {targetFmt}
        </Typography>
      ) : (
        <Typography
          variant="caption"
          color="text.disabled"
          sx={{ mt: 0.25 }}
          data-testid="gauge-no-target"
        >
          No target set
        </Typography>
      )}
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
