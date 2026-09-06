/**
 * LineChart (courbe) — 1..N séries sur un axe temps/index (Story 9.2b).
 *
 * Règles Origin : trait 1.5px, accent primaire, no gridlines, numerals tabulaires,
 * area fill fading à 8 % d'opacité, légende quand >1 série, light+dark via theme.
 * Hand-rolled inline SVG — aucune dépendance graphique externe (AD-11).
 *
 * État vide designé : quand toutes les séries sont vides/absentes, affiche un
 * message français plutôt qu'un canevas blanc.
 */

import { getVizPalette } from "./vizTheme";
import { Box, Typography, alpha, useTheme } from "@toorow/shell";

export interface LineChartSeries {
  /** Identifiant/nom de la série (affiché en légende). */
  label: string;
  points: { index: number | string; value: number }[];
  /** Optionnel : couleur CSS forcée (sinon palette primaire / séquence). */
  color?: string;
}

/**
 * Un fait DATÉ posé sur l'axe, jamais une série.
 *
 * Une sortie de vidéo, une mise en ligne, un changement de prix : ce n'est pas
 * une mesure, c'est la cause possible du mouvement qu'on lit à côté. Le tracer
 * comme une seconde courbe mentirait sur sa nature (il n'a pas de valeur) et
 * l'écrire dans une phrase sous le graphique obligerait l'oeil à faire lui-même
 * l'alignement qui est tout l'intérêt.
 */
export interface LineChartMarker {
  /** L'index de l'axe où il tombe — même vocabulaire que `points[].index`. */
  index: number | string;
  /** Ce que la personne lit : un titre, jamais un identifiant. */
  label: string;
}

export interface LineChartProps {
  series: LineChartSeries[];
  width?: number;
  height?: number;
  /** Accessible label (French). */
  ariaLabel?: string;
  /** Unité affichée en tooltip ou légende (ex: "séances"). */
  unit?: string;
  /**
   * Faits datés à poser sur l'axe (sorties, mises en ligne).
   *
   * UN REPÈRE HORS FENÊTRE N'EST PAS DESSINÉ : sa date n'est sur aucun point de
   * l'axe, donc le poser demanderait d'inventer une position. Il n'est pas non
   * plus silencieux — le bloc qui fournit les repères dit combien il en a et le
   * comptage se fait chez lui, pas ici.
   */
  markers?: LineChartMarker[];
}

// Story 23.1 (AC5): series colors come from the theme-driven viz contract
// (org branding reaches the lines) — no module-level color constants.
// categorical[0] IS the theme accent (palette.primary.main), so idx 0 keeps
// the exact prior behavior.
function seriesColor(categorical: string[], idx: number, override?: string): string {
  if (override) return override;
  return categorical[idx % categorical.length] ?? categorical[0]!;
}

export default function LineChart({
  series,
  width = 320,
  height = 96,
  ariaLabel = "Graphique courbe",
  unit,
  markers,
}: LineChartProps) {
  const theme = useTheme();
  const viz = getVizPalette(theme);

  // Designed empty state — also fires when NaN values are present after filtering.
  // Filter NaN/non-finite values before checking emptiness so that null-coerced
  // server values don't produce invisible degenerate SVG paths (F-4).
  const filteredSeries = series.map((s) => ({
    ...s,
    points: s.points.filter((p) => Number.isFinite(p.value)),
  }));
  const isEmpty = filteredSeries.length === 0 || filteredSeries.every((s) => s.points.length < 2);
  if (isEmpty) {
    return (
      <Box
        sx={{
          width,
          height,
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          border: `1px dashed ${alpha(theme.palette.text.primary, 0.12)}`,
          borderRadius: 2,
          gap: 0.5,
        }}
        data-testid="line-chart-empty"
        role="img"
        aria-label={`${ariaLabel} — aucune donnée`}
      >
        <Typography variant="body2" color="text.disabled" sx={{ lineHeight: 1 }}>
          —
        </Typography>
        <Typography variant="caption" color="text.disabled">
          Aucune donnée disponible
        </Typography>
      </Box>
    );
  }

  // Build unified domain (sorted unique indices) — use filteredSeries throughout.
  const allIndices = Array.from(
    new Set(filteredSeries.flatMap((s) => s.points.map((p) => String(p.index)))),
  ).sort();
  const n = allIndices.length;

  const PAD_X = 4;
  const PAD_Y = 6;
  const usableW = width - PAD_X * 2;
  const usableH = height - PAD_Y * 2;

  // Global min/max across all series for a shared Y axis (NaN already filtered above).
  const allValues = filteredSeries.flatMap((s) => s.points.map((p) => p.value));
  const minVal = Math.min(...allValues);
  const maxVal = Math.max(...allValues);
  const span = maxVal - minVal || 1;

  function toX(idx: number) {
    return PAD_X + (n <= 1 ? usableW / 2 : (idx / (n - 1)) * usableW);
  }
  function toY(value: number) {
    return PAD_Y + (1 - (value - minVal) / span) * usableH;
  }

  const baseline = PAD_Y + usableH;

  return (
    <Box data-testid="line-chart">
      <svg
        width={width}
        height={height}
        viewBox={`0 0 ${width} ${height}`}
        role="img"
        aria-label={ariaLabel}
        style={{ display: "block", maxWidth: "100%", overflow: "visible" }}
      >
        <title>{ariaLabel}</title>
        {filteredSeries.map((s, si) => {
          const color = seriesColor(viz.categorical, si, s.color);
          // Map points to domain indices (NaN already filtered in filteredSeries).
          const indexMap = new Map(s.points.map((p) => [String(p.index), p.value]));
          const pts = allIndices
            .map((idx, i) => {
              const v = indexMap.get(idx);
              if (v === undefined) return null;
              return { x: toX(i), y: toY(v) };
            })
            .filter(Boolean) as { x: number; y: number }[];

          if (pts.length < 2) return null;

          const polyPoints = pts.map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(" ");

          // Area path
          let areaPath = `M ${pts[0].x.toFixed(1)},${pts[0].y.toFixed(1)}`;
          for (let i = 1; i < pts.length; i++) {
            areaPath += ` L ${pts[i].x.toFixed(1)},${pts[i].y.toFixed(1)}`;
          }
          areaPath += ` L ${pts[pts.length - 1].x.toFixed(1)},${baseline}`;
          areaPath += ` L ${pts[0].x.toFixed(1)},${baseline} Z`;

          return (
            <g key={s.label} data-series={s.label}>
              <path d={areaPath} fill={alpha(color, 0.08)} strokeWidth={0} />
              <polyline
                points={polyPoints}
                fill="none"
                stroke={color}
                strokeWidth={1.5}
                strokeLinejoin="round"
                strokeLinecap="round"
              />
            </g>
          );
        })}

        {/* LES REPÈRES SE LISENT AVEC LA COURBE, PAS SOUS ELLE. Un trait vertical
            discret + son titre à la verticale : la personne voit l'écart entre la
            sortie et le mouvement sans avoir à aligner deux listes elle-même.
            Dessinés APRÈS les séries pour rester au-dessus du remplissage. */}
        {(markers ?? []).map((marker) => {
          const position = allIndices.indexOf(String(marker.index));
          if (position < 0) return null;
          const x = toX(position);
          return (
            <g key={`${marker.index}-${marker.label}`} data-marker={String(marker.index)}>
              <line
                x1={x}
                x2={x}
                y1={PAD_Y}
                y2={baseline}
                stroke={alpha(theme.palette.text.primary, 0.35)}
                strokeWidth={1}
                strokeDasharray="3 3"
              />
              <circle cx={x} cy={PAD_Y} r={2.5} fill={alpha(theme.palette.text.primary, 0.55)} />
              <title>{marker.label}</title>
            </g>
          );
        })}
      </svg>

      {/* Légende quand >1 série */}
      {filteredSeries.length > 1 && (
        <Box
          sx={{ display: "flex", flexWrap: "wrap", gap: 1.5, mt: 0.75 }}
          data-testid="line-chart-legend"
        >
          {filteredSeries.map((s, si) => {
            const color = seriesColor(viz.categorical, si, s.color);
            return (
              <Box key={s.label} sx={{ display: "flex", alignItems: "center", gap: 0.5 }}>
                <Box
                  sx={{
                    width: 16,
                    height: 2,
                    borderRadius: 1,
                    bgcolor: color,
                    flexShrink: 0,
                  }}
                />
                <Typography
                  variant="caption"
                  color="text.secondary"
                  sx={{ fontVariantNumeric: "lining-nums tabular-nums" }}
                >
                  {s.label}
                  {unit ? ` (${unit})` : ""}
                </Typography>
              </Box>
            );
          })}
        </Box>
      )}
    </Box>
  );
}
