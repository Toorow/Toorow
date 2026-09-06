/**
 * BarChart (graphique à barres) — horizontal ET vertical, groupé (Story 9.2b).
 *
 * Règles Origin : barres accent, track = viz.track (dérivé de TRACK_LIGHT dans vizTheme), value labels tabulaires, no gridlines,
 * 1.5px stroke si besoin, light+dark via theme. Hand-rolled SVG/HTML — aucune lib
 * graphique externe (AD-11).
 *
 * variant="horizontal" : barres de gauche à droite avec labels à gauche.
 * variant="vertical"   : barres de bas en haut avec labels en bas.
 *
 * Groupé (grouped=true) : chaque entrée peut avoir plusieurs valeurs par catégorie.
 * État vide designé : quand entries est vide.
 */

import { getVizPalette } from "./vizTheme";
import { verdictColor, verdictTone } from "./verdictTone";
import { NBSP, formatMeasure, formatValue } from "./viz/theme/formatters";
import { Box, Typography, alpha, useTheme } from "@toorow/shell";

export interface BarChartEntry {
  /** Libellé de la catégorie (ex: "Organique", "Payant"). */
  label: string;
  /** Valeur unique (simple) ou valeurs groupées (clé → valeur). */
  value?: number;
  values?: Record<string, number>;
  /**
   * direction?: "up" | "down" — from movers bar payload (_resolve_bar_movers).
   * Used in combination with semanticDirection on BarChart to pick the correct color:
   *   "up" = rank gained = GOOD (success), "down" = rank lost = BAD (error).
   * Absent for non-mover bars → flat ACCENT color (unchanged).
   */
  direction?: "up" | "down";
  /**
   * THE SIGNED VALUE, for when `value` was made absolute to size the bar.
   *
   * `CardComposition` passes `Math.abs(b.value)` as `value` so the bar is sized
   * by magnitude (F-1) — and the label, which rebuilt its sign with
   * `v > 0 ? "+" : ""`, could therefore only ever write a plus. Measured on
   * 2026-09-05 on `card-keywords`: « chaussettes de randonnée », whose fixture
   * carries `-3.8`, printed « +3,8 » IN RED above « +3,2 » in green. Two numbers
   * identical but for their sign, two opposite colours, and the sign that
   * explained them lost on the way to the screen.
   */
  signedValue?: number;
}

export interface BarChartProps {
  entries: BarChartEntry[];
  variant?: "horizontal" | "vertical";
  /** Clés pour les barres groupées (dans entry.values). Si absent → barre simple. */
  groupKeys?: string[];
  /** Couleurs par clé (optionnel). */
  groupColors?: Record<string, string>;
  ariaLabel?: string;
  unit?: string;
  /** Nombre max d'entrées affichées (top-N). Défaut 12. */
  topN?: number;
  /**
   * semanticDirection — from block.binding.direction (e.g. "down_good" for rank movers).
   * Controls color assignment for per-entry direction fields:
   *   When present ("up_good" | "down_good"), entry.direction="up" → success, "down" → error,
   *   matching the binding's semantic: for "down_good" (position/rank) a positive delta
   *   (higher position number) is actually bad, but the server emits direction="up" already
   *   meaning "gained positions" (=GOOD). So up=success, down=error in all cases.
   *   When absent/neutral: falls through to flat ACCENT color (default behavior).
   */
  semanticDirection?: "up_good" | "down_good" | "neutral";
}

// Story 23.1 (AC5): colors come from getVizPalette(theme) — the theme-driven
// contract — so org branding (meta.branding → WidgetShell ThemeProvider) reaches
// the bars. No module-level color constants (they bypass the theme).

export default function BarChart({
  entries,
  variant = "horizontal",
  groupKeys,
  groupColors,
  ariaLabel = "Graphique à barres",
  unit,
  topN = 12,
  semanticDirection,
}: BarChartProps) {
  const theme = useTheme();
  const viz = getVizPalette(theme);
  // Track rail is theme-correct in dark via the viz contract (same derivation as before).
  const track = viz.track;

  const isEmpty = entries.length === 0;
  if (isEmpty) {
    return (
      <Box
        sx={{
          minHeight: 80,
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          border: `1px dashed ${alpha(theme.palette.text.primary, 0.12)}`,
          borderRadius: 2,
          gap: 0.5,
        }}
        data-testid="bar-chart-empty"
        role="img"
        aria-label={`${ariaLabel} — aucune donnée`}
      >
        <Typography variant="body2" color="text.disabled">
          —
        </Typography>
        <Typography variant="caption" color="text.disabled">
          Aucune donnée disponible
        </Typography>
      </Box>
    );
  }

  const sliced = entries.slice(0, topN);
  const isGrouped = groupKeys && groupKeys.length > 0;

  // Resolve max for scaling — use |value| so negative movers bars scale correctly.
  const maxVal = Math.max(
    ...sliced.map((e) => {
      if (isGrouped && e.values) {
        return Math.max(...(groupKeys ?? []).map((k) => Math.abs(e.values![k] ?? 0)));
      }
      return Math.abs(e.value ?? 0);
    }),
    1,
  );

  /**
   * barColor — the fill of a simple (non-grouped) bar.
   *
   * THE VERDICT IS NOT DECIDED HERE, and that is the repair of 2026-09-05. This
   * function used to read `entry.direction` alone — a field the server sets —
   * while the legend beneath the chart stated the convention of
   * `semanticDirection`. On `card-keywords` (binding `down_good`, average
   * position) they disagreed on screen: `+3.2` green above `-3.8` red, under a
   * legend reading "falling is favourable". The verdict now comes from
   * `verdictTone(signed change, semanticDirection)`, the ONE function every
   * primitive shares, so the paint and the sentence cannot part company.
   *
   * `entry.direction` survives as the caller's stated movement when no signed
   * value reached the entry; it can no longer overrule the convention.
   */
  function barColor(entry: BarChartEntry, key?: string): string {
    if (key && groupColors?.[key]) return groupColors[key];
    if (key && groupKeys) {
      const ki = groupKeys.indexOf(key);
      return viz.categorical[ki % viz.categorical.length] ?? viz.accent;
    }
    return verdictColor(theme, verdictTone(signedChange(entry), semanticDirection), viz.accent);
  }

  /**
   * The movement of one bar, signed in the METRIC'S own units.
   *
   * `CardComposition` passes `Math.abs(value)` so the bar is sized by magnitude
   * (F-1) and the signed figure in `signedValue`; an entry that carries only
   * `direction` states its movement without a number, and `±1` is enough for a
   * verdict that only reads the sign.
   */
  function signedChange(entry: BarChartEntry): number | null {
    if (entry.signedValue !== undefined) return entry.signedValue;
    if (entry.direction) return entry.direction === "up" ? 1 : -1;
    return entry.value ?? null;
  }

  function fmt(v: number) {
    return formatMeasure(v, unit);
  }

  if (variant === "horizontal") {
    return (
      <Box
        data-testid="bar-chart"
        data-variant="horizontal"
        role="img"
        aria-label={ariaLabel}
        sx={{ display: "flex", flexDirection: "column", gap: 0.75 }}
      >
        {sliced.map((entry) => (
          <Box
            key={entry.label}
            // The verdict is published on the row so a test can read WHAT the
            // colour says rather than which hex it landed on — the bars said one
            // thing and the legend the opposite until story 76-8 round 2.
            data-testid="bar-chart-row"
            data-label={entry.label}
            data-verdict={verdictTone(signedChange(entry), semanticDirection)}
            sx={{
              display: "grid",
              gridTemplateColumns: "minmax(88px, 28%) 1fr auto",
              alignItems: "center",
              gap: 1,
            }}
          >
            <Typography variant="body2" noWrap title={entry.label}>
              {entry.label}
            </Typography>
            {/* Track + bar(s) */}
            <Box
              sx={{
                position: "relative",
                height: isGrouped ? (groupKeys?.length ?? 1) * 8 + (groupKeys?.length ?? 1) * 3 : 8,
                borderRadius: 999,
                bgcolor: track,
                overflow: "hidden",
              }}
              aria-hidden
            >
              {isGrouped
                ? (groupKeys ?? []).map((k, ki) => {
                    const v = entry.values?.[k] ?? 0;
                    const pct = Math.max(2, (v / maxVal) * 100);
                    return (
                      <Box
                        key={k}
                        sx={{
                          position: "absolute",
                          top: ki * 11,
                          left: 0,
                          width: `${pct}%`,
                          height: 8,
                          bgcolor: barColor(entry, k),
                          borderRadius: "0 999px 999px 0",
                        }}
                      />
                    );
                  })
                : (() => {
                    const v = entry.value ?? 0;
                    const absV = Math.abs(v);
                    // Cap at 98% so the top bar always shows a 2% visible track gap at
                    // the right end, keeping the pill borderRadius visible (F-11).
                    // Use |v| for width so negative movers render at correct magnitude (F-1).
                    const pct = Math.min(98, Math.max(2, (absV / maxVal) * 100));
                    return (
                      <Box
                        sx={{
                          width: `${pct}%`,
                          height: "100%",
                          bgcolor: barColor(entry),
                          borderRadius: "0 999px 999px 0",
                        }}
                      />
                    );
                  })()}
            </Box>
            <Typography
              variant="body2"
              sx={{ fontVariantNumeric: "lining-nums tabular-nums", fontWeight: 600 }}
            >
              {isGrouped && entry.values
                ? Object.values(entry.values)
                    .slice(0, groupKeys?.length)
                    .map((v) => fmt(v))
                    .join(" / ")
                : (() => {
                    const v = entry.value ?? 0;
                    // For direction-aware movers, show the SIGNED label (+4 / -3),
                    // taken from `signedValue` when the width used |value|.
                    if (entry.direction && semanticDirection && semanticDirection !== "neutral") {
                      const signed = entry.signedValue ?? v;
                      const body = formatValue(signed, { signed: true });
                      return unit ? `${body}${NBSP}${unit}` : body;
                    }
                    return fmt(v);
                  })()}
            </Typography>
          </Box>
        ))}
      </Box>
    );
  }

  // Vertical variant
  const BAR_W = 32;
  const BAR_GAP = 8;
  const CHART_H = 120;
  const PAD = 4;
  const innerH = CHART_H - PAD * 2;
  const totalW = sliced.length * BAR_W + (sliced.length - 1) * BAR_GAP + PAD * 2;

  return (
    <Box data-testid="bar-chart" data-variant="vertical" role="img" aria-label={ariaLabel}>
      <svg
        width={totalW}
        height={CHART_H}
        viewBox={`0 0 ${totalW} ${CHART_H}`}
        style={{ display: "block", maxWidth: "100%", overflow: "visible" }}
      >
        <title>{ariaLabel}</title>
        {sliced.map((entry, ei) => {
          const x = PAD + ei * (BAR_W + BAR_GAP);
          const v = entry.value ?? 0;
          const barH = Math.max(2, (v / maxVal) * innerH);
          const y = PAD + innerH - barH;
          return (
            <g key={entry.label}>
              {/* Track */}
              <rect
                x={x}
                y={PAD}
                width={BAR_W}
                height={innerH}
                rx={4}
                fill={track}
              />
              {/* Bar */}
              <rect
                x={x}
                y={y}
                width={BAR_W}
                height={barH}
                rx={4}
                fill={barColor(entry)}
              />
              {/* Value label above bar */}
              <text
                x={x + BAR_W / 2}
                y={y - 4}
                textAnchor="middle"
                fontSize={9}
                style={{ fontVariantNumeric: "lining-nums tabular-nums" }}
                fill={theme.palette.text.secondary}
              >
                {formatValue(v)}
              </text>
            </g>
          );
        })}
      </svg>
      {/* Labels below */}
      <Box
        sx={{
          display: "flex",
          gap: `${BAR_GAP}px`,
          px: `${PAD}px`,
          mt: 0.25,
        }}
      >
        {sliced.map((entry) => (
          <Box key={entry.label} sx={{ width: BAR_W, flexShrink: 0 }}>
            <Typography
              variant="caption"
              color="text.secondary"
              sx={{ display: "block", textAlign: "center", lineHeight: 1.2 }}
              noWrap
              title={entry.label}
            >
              {entry.label}
            </Typography>
          </Box>
        ))}
      </Box>
    </Box>
  );
}
