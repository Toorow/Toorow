/**
 * OverlayBarChart — primitive barres verticales superposées : Projection (large, claire,
 * semi-transparente) derrière, Actual (étroite, pleine) devant (Story 23.3).
 *
 * Référence visuelle : card « Total revenue » — barres verticales par mois.
 * Règles Origin : accent unique (palette.primary.main), pas de gridlines lourdes,
 * ticks Y propres, tabular-nums, strings françaises.
 * AD-11 : SVG fait main, aucune lib graphique externe.
 * AD-9  : actual: null → seule la projection ; projection: null → seul le réel ;
 *         les deux null → emplacement vide avec le label X.
 */

import React, { useMemo } from "react";
import { useTheme, alpha } from "@mui/material/styles";
import Box from "@mui/material/Box";
import Typography from "@mui/material/Typography";
import { getVizPalette } from "./vizTheme";

// ---------------------------------------------------------------------------
// Types publics
// ---------------------------------------------------------------------------

export interface OverlayBarEntry {
  /** Libellé de la catégorie (ex. « Jan », « Fév »). */
  label: string;
  /** Valeur projetée — null = absent (AD-9). */
  projection: number | null;
  /** Valeur réelle — null = absent (AD-9). */
  actual: number | null;
}

export interface OverlayBarChartProps {
  entries: OverlayBarEntry[];
  /** Label de la série projection (défaut « Projection »). */
  projectionLabel?: string;
  /** Label de la série réelle (défaut « Réel »). */
  actualLabel?: string;
  /** Suffixe/préfixe d'affichage des ticks et tooltips (ex. « € »). */
  unit?: string;
  /** Label aria du SVG (défaut français). */
  ariaLabel?: string;
  /**
   * Slot en haut à droite — typiquement un toggle Monthly/Yearly contrôlé par le parent.
   * La primitive ne gère aucun état de période.
   */
  headerSlot?: React.ReactNode;
}

// ---------------------------------------------------------------------------
// Constantes de mise en page SVG
// ---------------------------------------------------------------------------

/** Hauteur du corps du graphique (barres + axe Y). */
const CHART_H = 180;
/** Marge haute (pour les value labels au-dessus des barres). */
const MARGIN_TOP = 24;
/** Marge basse (pour les labels X). */
const MARGIN_BOTTOM = 28;
/** Marge gauche (pour les ticks Y). */
const MARGIN_LEFT = 48;
/** Marge droite. */
const MARGIN_RIGHT = 12;
/** Nombre de ticks Y. */
const N_TICKS = 4;
/** Largeur d'une colonne (pas entre catégories). */
const STEP_W = 56;
/** Largeur barre projection : ~65 % du pas. */
const PROJ_W_RATIO = 0.65;
/** Largeur barre réelle : ~42 % du pas. */
const ACTUAL_W_RATIO = 0.42;
/** rx barre projection (coins très arrondis). */
const PROJ_RX = 6;
/** rx barre réelle. */
const ACTUAL_RX = 4;
/** Alpha de la barre projection. */
const PROJ_ALPHA = 0.22;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Formate une valeur en fr-FR compact avec unité facultative. */
function fmtValue(v: number, unit?: string): string {
  const abs = Math.abs(v);
  let s: string;
  if (abs >= 1_000_000) {
    s = (v / 1_000_000).toLocaleString("fr-FR", { maximumFractionDigits: 1 }) + " M";
  } else if (abs >= 1_000) {
    s = (v / 1_000).toLocaleString("fr-FR", { maximumFractionDigits: 1 }) + " k";
  } else {
    s = v.toLocaleString("fr-FR");
  }
  return unit ? `${s}${unit}` : s;
}

/**
 * Calcule des ticks Y propres : on cherche un multiple « rond » (1, 2, 2.5, 5, 10…
 * × puissance de 10) tel que les N_TICKS ticks couvrent [0, maxVal].
 */
function niceMax(raw: number, n: number): number {
  if (raw <= 0) return n;
  const step = raw / (n - 1);
  const mag = Math.pow(10, Math.floor(Math.log10(step)));
  const candidates = [1, 2, 2.5, 5, 10].map((m) => m * mag);
  const niceStep = candidates.find((c) => c >= step) ?? candidates[candidates.length - 1]!;
  return niceStep * (n - 1);
}

// ---------------------------------------------------------------------------
// Composant
// ---------------------------------------------------------------------------

export default function OverlayBarChart({
  entries,
  projectionLabel = "Projection",
  actualLabel = "Réel",
  unit,
  ariaLabel = "Graphique de projection vs réel",
  headerSlot,
}: OverlayBarChartProps) {
  const theme = useTheme();
  const viz = getVizPalette(theme);
  const accent = viz.accent;
  const textSecondary = theme.palette.text.secondary;

  // ---- État vide ----
  if (entries.length === 0) {
    return (
      <Box
        data-testid="overlay-bar-empty"
        role="status"
        sx={{
          minHeight: 120,
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          border: `1px dashed ${alpha(theme.palette.text.primary, 0.12)}`,
          borderRadius: 2,
          gap: 0.5,
        }}
      >
        <Typography variant="body2" color="text.disabled">—</Typography>
        <Typography variant="caption" color="text.disabled">
          Aucune donnée à afficher
        </Typography>
      </Box>
    );
  }

  // ---- Calcul des dimensions SVG ----
  const innerH = CHART_H - MARGIN_TOP - MARGIN_BOTTOM;
  const innerW = entries.length * STEP_W;
  const svgW = MARGIN_LEFT + innerW + MARGIN_RIGHT;
  const svgH = CHART_H;

  // ---- Domaine Y ----
  const rawMax = useMemo(() => {
    let m = 0;
    for (const e of entries) {
      if (e.projection !== null) m = Math.max(m, e.projection);
      if (e.actual !== null) m = Math.max(m, e.actual);
    }
    return m;
  }, [entries]);

  const yMax = niceMax(rawMax || 1, N_TICKS);

  const yScale = (v: number) => innerH - (v / yMax) * innerH;

  // Ticks Y : N_TICKS valeurs de 0 à yMax inclus
  const yTicks = Array.from({ length: N_TICKS }, (_, i) =>
    (yMax / (N_TICKS - 1)) * i,
  );

  // Couleurs dérivées de l'accent
  const projFill = alpha(accent, PROJ_ALPHA);
  const actualFill = accent;

  // ---- Rendu ----
  return (
    <Box sx={{ display: "flex", flexDirection: "column", gap: 0 }}>
      {/* Header : titre implicite + slot parent */}
      {headerSlot && (
        <Box
          sx={{
            display: "flex",
            justifyContent: "flex-end",
            mb: 1,
          }}
        >
          {headerSlot}
        </Box>
      )}

      {/* SVG principal */}
      <Box sx={{ overflowX: "auto" }}>
        <svg
          role="img"
          aria-label={ariaLabel}
          width={svgW}
          height={svgH}
          viewBox={`0 0 ${svgW} ${svgH}`}
          style={{ display: "block", minWidth: svgW, fontFamily: "inherit" }}
        >
          <title>{ariaLabel}</title>

          {/* ---- Ticks Y + labels (sans gridlines lourdes — règle Origin) ---- */}
          {yTicks.map((tick) => {
            const y = MARGIN_TOP + yScale(tick);
            return (
              <g key={tick}>
                {/* Hairline très discrète */}
                <line
                  x1={MARGIN_LEFT}
                  x2={MARGIN_LEFT + innerW}
                  y1={y}
                  y2={y}
                  stroke={viz.hairline}
                  strokeWidth={0.5}
                />
                {/* Label Y */}
                <text
                  x={MARGIN_LEFT - 6}
                  y={y + 4}
                  textAnchor="end"
                  fontSize={10}
                  fill={textSecondary}
                  style={{ fontVariantNumeric: "lining-nums tabular-nums" }}
                >
                  {fmtValue(tick, unit)}
                </text>
              </g>
            );
          })}

          {/* ---- Barres par catégorie ---- */}
          {entries.map((entry, i) => {
            const cx = MARGIN_LEFT + i * STEP_W + STEP_W / 2; // centre de la colonne
            const projW = STEP_W * PROJ_W_RATIO;
            const actualW = STEP_W * ACTUAL_W_RATIO;

            // Calcul des hauteurs (AD-9 : null = absent, pas de barre à h=0)
            const projH =
              entry.projection !== null
                ? Math.max(2, (entry.projection / yMax) * innerH)
                : null;
            const actualH =
              entry.actual !== null
                ? Math.max(2, (entry.actual / yMax) * innerH)
                : null;

            const baseY = MARGIN_TOP + innerH; // baseline Y (bas des barres)

            // Tooltip natif
            const projTip =
              entry.projection !== null
                ? fmtValue(entry.projection, unit)
                : "—";
            const actualTip =
              entry.actual !== null ? fmtValue(entry.actual, unit) : "—";
            const tooltipText = `${entry.label} — ${projectionLabel} : ${projTip} · ${actualLabel} : ${actualTip}`;

            return (
              <g key={entry.label} data-category={entry.label}>
                <title>{tooltipText}</title>

                {/* Barre Projection (large, claire, DERRIÈRE) */}
                {projH !== null && (
                  <rect
                    data-testid="overlay-bar-projection"
                    x={cx - projW / 2}
                    y={baseY - projH}
                    width={projW}
                    height={projH}
                    rx={PROJ_RX}
                    ry={PROJ_RX}
                    fill={projFill}
                  />
                )}

                {/* Barre Actual (étroite, pleine, DEVANT) */}
                {actualH !== null && (
                  <rect
                    data-testid="overlay-bar-actual"
                    x={cx - actualW / 2}
                    y={baseY - actualH}
                    width={actualW}
                    height={actualH}
                    rx={ACTUAL_RX}
                    ry={ACTUAL_RX}
                    fill={actualFill}
                  />
                )}

                {/* Label X */}
                <text
                  x={cx}
                  y={baseY + 16}
                  textAnchor="middle"
                  fontSize={10}
                  fill={textSecondary}
                >
                  {entry.label}
                </text>
              </g>
            );
          })}
        </svg>
      </Box>

      {/* ---- Légende en bas ---- */}
      <Box
        data-testid="overlay-bar-legend"
        sx={{
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          gap: 2.5,
          mt: 1.5,
        }}
      >
        {/* Projection : pastille claire */}
        <Box sx={{ display: "flex", alignItems: "center", gap: 0.75 }}>
          <Box
            component="span"
            aria-hidden
            sx={{
              width: 10,
              height: 10,
              borderRadius: "50%",
              backgroundColor: projFill,
              border: `1.5px solid ${alpha(accent, 0.5)}`,
              flexShrink: 0,
            }}
          />
          <Typography variant="caption" color="text.secondary">
            {projectionLabel}
          </Typography>
        </Box>

        {/* Actual : pastille pleine */}
        <Box sx={{ display: "flex", alignItems: "center", gap: 0.75 }}>
          <Box
            component="span"
            aria-hidden
            sx={{
              width: 10,
              height: 10,
              borderRadius: "50%",
              backgroundColor: actualFill,
              flexShrink: 0,
            }}
          />
          <Typography variant="caption" color="text.secondary">
            {actualLabel}
          </Typography>
        </Box>
      </Box>
    </Box>
  );
}
