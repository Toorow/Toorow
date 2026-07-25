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

import { useTheme, alpha } from "@mui/material/styles";
import Box from "@mui/material/Box";
import Typography from "@mui/material/Typography";
import { getVizPalette } from "./vizTheme";

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
   * barColor — resolves the fill color for a simple (non-grouped) bar entry.
   *
   * Movers direction semantics (semanticDirection present AND entry.direction set):
   *   The server emits direction="up" to mean "gained positions" (GOOD) and direction="down"
   *   to mean "lost positions" (BAD). This convention holds for both "up_good" and "down_good"
   *   binding semantics — the per-entry direction already encodes good/bad as up/down, so:
   *     up   → success token (green)
   *     down → error token (red)
   *   "down_good" on the binding means the underlying metric (position) is better when lower,
   *   but the server converts that so a positive position delta = rank improvement = up = GOOD.
   *
   * Default (no direction, or semanticDirection absent/neutral): ACCENT (flat, unchanged).
   */
  function barColor(_idx: number, key?: string, entryDirection?: "up" | "down"): string {
    if (key && groupColors?.[key]) return groupColors[key];
    if (key && groupKeys) {
      const ki = groupKeys.indexOf(key);
      return viz.categorical[ki % viz.categorical.length] ?? viz.accent;
    }
    // Direction-aware coloring for movers bar (F-1 + F-3 fix).
    if (entryDirection && semanticDirection && semanticDirection !== "neutral") {
      return entryDirection === "up"
        ? theme.palette.success.main
        : theme.palette.error.main;
    }
    return viz.accent;
  }

  function fmt(v: number) {
    return v.toLocaleString("fr-FR") + (unit ? ` ${unit}` : "");
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
        {sliced.map((entry, ei) => (
          <Box
            key={entry.label}
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
                          bgcolor: barColor(ei, k),
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
                          bgcolor: barColor(ei, undefined, entry.direction),
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
                    // For direction-aware movers, show signed label (+4 / -3).
                    if (entry.direction && semanticDirection && semanticDirection !== "neutral") {
                      return `${v > 0 ? "+" : ""}${v.toLocaleString("fr-FR")}${unit ? ` ${unit}` : ""}`;
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
                fill={barColor(ei)}
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
                {v.toLocaleString("fr-FR")}
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
