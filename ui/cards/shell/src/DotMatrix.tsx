/**
 * DotMatrix — matrice de points ronds par groupe (Story 23.4).
 *
 * Rendu « punch-card LED » : pour chaque groupe, un label à gauche, le total à droite,
 * puis un bloc dense de petits cercles SVG. L'intensité (alpha de l'accent) est normalisée
 * sur le MAX GLOBAL de tous les groupes pour garantir la comparabilité inter-groupes.
 *
 * null  → point « éteint » (alpha plancher très faible, distinct).
 * 0     → alpha plancher 0.10 (allumé mais faible, distinct du null).
 * max   → alpha 1.0.
 *
 * Aucune lib graphique externe (AD-11). Light + dark via MUI theme. Strings françaises (UX-DR10).
 */

import { useMemo } from "react";
import Box from "@mui/material/Box";
import Typography from "@mui/material/Typography";
import { useTheme, alpha } from "@mui/material/styles";
import { getVizPalette } from "./vizTheme";

export interface DotMatrixGroup {
  /** Libellé du groupe (ex. « September », « Octobre »). */
  label: string;
  /** Valeurs : un point par entrée. null = point éteint (donnée absente). */
  values: Array<number | null>;
  /** Total affiché à droite du label (string directe ou number formaté fr-FR). */
  total?: string | number;
}

export interface DotMatrixProps {
  groups: DotMatrixGroup[];
  /** Nombre de points par ligne (wrap). Défaut : 31. */
  columns?: number;
  /** Unité affichée dans les tooltips (ex. « € », « sessions »). */
  unit?: string;
  /** Aria-label global pour l'accessibilité. */
  ariaLabel?: string;
}

/** Rayon d'un point (px). */
const DOT_R = 3.5;
/** Espacement centre à centre (px). */
const DOT_STEP = DOT_R * 2 + 4; // ≈ 11px

/** Alpha du fond du point null (« éteint »). */
const ALPHA_NULL = 0.04;
/** Alpha plancher du point 0 (« allumé faible »). */
const ALPHA_MIN = 0.10;
/** Alpha maximum du point max. */
const ALPHA_MAX = 1.0;

function formatTotal(total: string | number | undefined, unit?: string): string | undefined {
  if (total === undefined) return undefined;
  if (typeof total === "number") {
    const formatted = total.toLocaleString("fr-FR");
    return unit ? `${formatted} ${unit}` : formatted;
  }
  return total;
}

export default function DotMatrix({
  groups,
  columns = 31,
  unit,
  ariaLabel = "Matrice de points",
}: DotMatrixProps) {
  const theme = useTheme();
  const viz = getVizPalette(theme);

  const isEmpty = groups.length === 0 || groups.every((g) => g.values.length === 0);

  // MAX GLOBAL : normalisé sur toutes les valeurs non-null de tous les groupes.
  const globalMax = useMemo(() => {
    let max = 0;
    for (const group of groups) {
      for (const v of group.values) {
        if (v !== null && v > max) max = v;
      }
    }
    return max;
  }, [groups]);

  if (isEmpty) {
    return (
      <Box
        data-testid="dot-matrix-empty"
        role="status"
        aria-label={`${ariaLabel} — aucune donnée`}
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
      >
        <Typography variant="body2" color="text.disabled">
          —
        </Typography>
        <Typography variant="caption" color="text.disabled">
          Aucune donnée à afficher
        </Typography>
      </Box>
    );
  }

  /**
   * Alpha d'un point selon sa valeur et le max global.
   * - null             → ALPHA_NULL (éteint, visuellement distinct)
   * - 0                → ALPHA_MIN (allumé au plancher)
   * - 0 < v <= globalMax → interpolé ALPHA_MIN..ALPHA_MAX
   */
  function dotAlpha(value: number | null): number {
    if (value === null) return ALPHA_NULL;
    if (globalMax <= 0) return ALPHA_MIN;
    const norm = Math.min(1, value / globalMax);
    return ALPHA_MIN + norm * (ALPHA_MAX - ALPHA_MIN);
  }

  /**
   * Tooltip SVG <title> pour un point.
   */
  function dotTitle(group: DotMatrixGroup, i: number, value: number | null): string {
    const label = group.label;
    const valStr = value === null
      ? "—"
      : unit
        ? `${value.toLocaleString("fr-FR")} ${unit}`
        : value.toLocaleString("fr-FR");
    return `${label} · point ${i + 1} : ${valStr}`;
  }

  return (
    <Box
      role="img"
      data-testid="dot-matrix"
      aria-label={ariaLabel}
      sx={{ display: "flex", flexDirection: "column", gap: 1.5 }}
    >
      {groups.map((group, gi) => {
        const rows = Math.ceil(group.values.length / columns);
        const svgW = Math.min(group.values.length, columns) * DOT_STEP - (DOT_STEP - DOT_R * 2);
        const svgH = rows * DOT_STEP - (DOT_STEP - DOT_R * 2);
        const totalStr = formatTotal(group.total, unit);
        const svgLabel = `${group.label}${totalStr ? ` — total : ${totalStr}` : ""}`;

        return (
          <Box key={`${gi}-${group.label}`} data-testid="dot-matrix-group">
            {/* En-tête du groupe : label à gauche, total à droite */}
            <Box
              sx={{
                display: "flex",
                alignItems: "baseline",
                justifyContent: "space-between",
                mb: 0.75,
              }}
            >
              <Typography
                variant="subtitle2"
                sx={{ fontWeight: 600, lineHeight: 1.2 }}
              >
                {group.label}
              </Typography>
              {totalStr !== undefined && (
                <Typography
                  variant="body2"
                  sx={{
                    fontVariantNumeric: "lining-nums tabular-nums",
                    color: "text.secondary",
                    ml: 1,
                  }}
                  data-testid="dot-matrix-total"
                >
                  {totalStr}
                </Typography>
              )}
            </Box>

            {/* Bloc SVG des points */}
            <svg
              width={svgW + DOT_R * 2}
              height={svgH + DOT_R * 2}
              role="img"
              aria-label={svgLabel}
              style={{ display: "block", overflow: "visible" }}
            >
              <title>{svgLabel}</title>
              {group.values.map((value, i) => {
                const col = i % columns;
                const row = Math.floor(i / columns);
                const cx = DOT_R + col * DOT_STEP;
                const cy = DOT_R + row * DOT_STEP;
                const a = dotAlpha(value);
                const fill = alpha(viz.accent, a);
                return (
                  <circle
                    key={i}
                    cx={cx}
                    cy={cy}
                    r={DOT_R}
                    fill={fill}
                    data-testid="dot-matrix-dot"
                    data-alpha={a.toFixed(3)}
                    aria-label={dotTitle(group, i, value)}
                  >
                    <title>{dotTitle(group, i, value)}</title>
                  </circle>
                );
              })}
            </svg>
          </Box>
        );
      })}
    </Box>
  );
}
