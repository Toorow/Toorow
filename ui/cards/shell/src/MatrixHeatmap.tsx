/**
 * MatrixHeatmap — matrice de chaleur catégorielle 2D (Story 23.2).
 *
 * Règles Origin : cellules arrondies accent-scale, hairlines, tooltip SVG natif,
 * légende à paliers (thresholds), état vide designé, aria complet.
 * Hand-rolled SVG/HTML — aucune lib graphique externe (AD-11).
 * Couleurs uniquement via getVizPalette(useTheme()) — jamais de hex en dur.
 * Light + dark via MUI theme. Copy français-first (UX-DR10).
 *
 * Ref visuelle : card « Visitor Counter » (matrice pays × tranches d'âge,
 * cellules roses d'intensité variable, légende à paliers, tooltip au survol).
 */

import { useTheme, alpha } from "@mui/material/styles";
import Box from "@mui/material/Box";
import Typography from "@mui/material/Typography";
import { getVizPalette } from "./vizTheme";

// ─── Constantes de layout ────────────────────────────────────────────────────
const CELL_W = 36;      // px — largeur d'une cellule
const CELL_H = 28;      // px — hauteur d'une cellule
const CELL_RX = 4;      // px — arrondi des coins
const GAP = 2;          // px — espacement entre cellules
const ROW_LABEL_W = 100; // px — largeur réservée aux labels de lignes
const COL_LABEL_H = 36;  // px — hauteur réservée aux labels de colonnes
const LEGEND_CHIP_SIZE = 10; // px — côté du carré de légende
const MIN_ALPHA = 0.10;      // alpha minimal de la rampe continue
const MAX_ALPHA = 1.00;      // alpha maximal de la rampe continue

// ─── Types ───────────────────────────────────────────────────────────────────

/** Palier de couleur pour la légende par seuils. */
export interface MatrixHeatmapThreshold {
  /** Valeur minimale inclusive du palier. */
  min: number;
  /** Label affiché dans la légende (ex. « 0-10 », « >25 »). */
  label: string;
}

/** Props du composant MatrixHeatmap. */
export interface MatrixHeatmapProps {
  /** Labels des lignes (catégories en ordonnée, ex. pays). */
  rows: string[];
  /** Labels des colonnes (catégories en abscisse, ex. tranches d'âge). */
  cols: string[];
  /**
   * Valeurs indexées [rowIdx][colIdx].
   * `null` = donnée absente (AD-9) — rendu squelette hairline, jamais traité comme 0.
   */
  values: Array<Array<number | null>>;
  /**
   * Paliers optionnels. Quand présents :
   *   - couleur par palier (rampe alpha d'accent échelonnée du plus clair au plus foncé)
   *   - légende de chips en haut à droite (carré coloré + label)
   * Quand absents : rampe alpha continue accent 0.10→1.0 normalisée sur le max.
   */
  thresholds?: MatrixHeatmapThreshold[];
  /** Suffixe des valeurs dans les tooltips (ex. « visiteurs »). */
  unit?: string;
  /** Label accessible du SVG. Défaut : description française générique. */
  ariaLabel?: string;
  /**
   * Callback au clic sur une cellule. Quand fourni, les cellules sont focusables
   * (tabIndex=0, role="button", Enter/Espace déclenchent aussi le callback).
   */
  onCellClick?: (row: string, col: string, value: number | null) => void;
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

/** Formate un nombre en français (séparateur de milliers, décimales). */
function fmt(v: number): string {
  return v.toLocaleString("fr-FR");
}

/** Tooltip textuel d'une cellule : « {row} × {col} — {valeur}{unit} ». */
function cellTitle(row: string, col: string, value: number | null, unit?: string): string {
  const valStr = value === null ? "—" : fmt(value) + (unit ? ` ${unit}` : "");
  return `${row} × ${col} — ${valStr}`;
}

// ─── Composant ───────────────────────────────────────────────────────────────

export default function MatrixHeatmap({
  rows,
  cols,
  values,
  thresholds,
  unit,
  ariaLabel = "Matrice de chaleur",
  onCellClick,
}: MatrixHeatmapProps) {
  const theme = useTheme();
  const viz = getVizPalette(theme);
  const accent = viz.accent;
  const hairline = viz.hairline;

  // ── État vide designé ──────────────────────────────────────────────────────
  const isEmpty =
    rows.length === 0 || cols.length === 0 || values.length === 0;

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
          py: 3,
        }}
        data-testid="matrix-heatmap-empty"
        role="status"
        aria-label={`${ariaLabel} — aucune donnée`}
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

  // ── Calcul des couleurs ────────────────────────────────────────────────────

  // Valeur maximale (hors null) pour la rampe continue.
  let maxVal = 0;
  for (const row of values) {
    for (const v of row) {
      if (v !== null && v > maxVal) maxVal = v;
    }
  }

  /**
   * Couleur d'une cellule.
   * - null  → hairline (squelette)
   * - thresholds présents → rampe alpha par palier
   * - sinon → rampe alpha continue normalisée sur maxVal
   */
  function fillFor(value: number | null): string {
    if (value === null) {
      return alpha(theme.palette.text.primary, 0.06);
    }

    if (thresholds && thresholds.length > 0) {
      // Trier les paliers par min croissant.
      const sorted = [...thresholds].sort((a, b) => a.min - b.min);
      // Trouver le palier correspondant (le plus grand min ≤ value).
      let palierIdx = 0;
      for (let i = 0; i < sorted.length; i++) {
        if (value >= sorted[i]!.min) palierIdx = i;
      }
      // Rampe alpha : du plus clair (premier palier) au plus foncé (dernier).
      const n = sorted.length;
      const alphaVal =
        n === 1
          ? MAX_ALPHA
          : MIN_ALPHA + (palierIdx / (n - 1)) * (MAX_ALPHA - MIN_ALPHA);
      return alpha(accent, alphaVal);
    }

    // Rampe continue.
    if (maxVal <= 0) return alpha(accent, MIN_ALPHA);
    const norm = value / maxVal;
    if (norm <= 0) return alpha(accent, MIN_ALPHA);
    return alpha(accent, MIN_ALPHA + norm * (MAX_ALPHA - MIN_ALPHA));
  }

  /**
   * Couleurs de la légende (dans l'ordre des paliers triés par min croissant).
   * Expose la même logique que fillFor pour les paliers, mais pour un usage en dehors du SVG.
   */
  function legendColors(): Array<{ label: string; color: string }> {
    if (!thresholds || thresholds.length === 0) return [];
    const sorted = [...thresholds].sort((a, b) => a.min - b.min);
    return sorted.map((t, i) => {
      const n = sorted.length;
      const alphaVal =
        n === 1
          ? MAX_ALPHA
          : MIN_ALPHA + (i / (n - 1)) * (MAX_ALPHA - MIN_ALPHA);
      return { label: t.label, color: alpha(accent, alphaVal) };
    });
  }

  const legend = legendColors();
  const interactive = !!onCellClick;

  // ── Dimensions SVG ─────────────────────────────────────────────────────────
  const colStep = CELL_W + GAP;
  const rowStep = CELL_H + GAP;

  const svgW = ROW_LABEL_W + cols.length * colStep - GAP;
  const svgH = rows.length * rowStep - GAP + COL_LABEL_H;

  // ── Rendu ──────────────────────────────────────────────────────────────────
  return (
    <Box>
      {/* Légende paliers (haut à droite) */}
      {legend.length > 0 && (
        <Box
          sx={{
            display: "flex",
            flexWrap: "wrap",
            justifyContent: "flex-end",
            gap: 0.75,
            mb: 1,
          }}
          aria-label="Légende des paliers"
          data-testid="matrix-heatmap-legend"
        >
          {legend.map((item) => (
            <Box
              key={item.label}
              sx={{ display: "flex", alignItems: "center", gap: 0.5 }}
              data-testid="matrix-heatmap-legend-item"
            >
              {/* Carré coloré */}
              <Box
                sx={{
                  width: LEGEND_CHIP_SIZE,
                  height: LEGEND_CHIP_SIZE,
                  borderRadius: 0.5,
                  bgcolor: item.color,
                  border: `1px solid ${hairline}`,
                  flexShrink: 0,
                }}
                aria-hidden
              />
              <Typography
                variant="caption"
                color="text.secondary"
                sx={{ lineHeight: 1.2 }}
              >
                {item.label}
              </Typography>
            </Box>
          ))}
        </Box>
      )}

      {/* Matrice SVG */}
      <Box sx={{ overflowX: "auto" }}>
        <svg
          width={svgW}
          height={svgH}
          viewBox={`0 0 ${svgW} ${svgH}`}
          role="img"
          aria-label={ariaLabel}
          style={{ display: "block" }}
          data-testid="matrix-heatmap"
        >
          <title>{ariaLabel}</title>

          {/* Labels de colonnes (en bas) */}
          {cols.map((col, ci) => {
            const cx = ROW_LABEL_W + ci * colStep + CELL_W / 2;
            const cy = rows.length * rowStep + 6; // 6px gap avant le label
            return (
              <text
                key={`col-${ci}`}
                x={cx}
                y={cy}
                textAnchor="middle"
                dominantBaseline="hanging"
                fontSize={10}
                fontFamily="inherit"
                fill={theme.palette.text.secondary}
                data-testid="matrix-heatmap-col-label"
              >
                {col}
              </text>
            );
          })}

          {/* Lignes + labels de lignes */}
          {rows.map((row, ri) => {
            const rowValues = values[ri] ?? [];
            const y = ri * rowStep;
            return (
              <g key={`row-${ri}`}>
                {/* Label de ligne (à gauche) */}
                <text
                  x={ROW_LABEL_W - 8}
                  y={y + CELL_H / 2}
                  textAnchor="end"
                  dominantBaseline="middle"
                  fontSize={10}
                  fontFamily="inherit"
                  fill={theme.palette.text.secondary}
                  data-testid="matrix-heatmap-row-label"
                >
                  {row}
                </text>

                {/* Cellules */}
                {cols.map((col, ci) => {
                  const cellVal = rowValues[ci] ?? null;
                  const fill = fillFor(cellVal);
                  const x = ROW_LABEL_W + ci * colStep;
                  const title = cellTitle(row, col, cellVal, unit);
                  const ariaLabelCell =
                    cellVal === null
                      ? `${row} × ${col} — donnée absente`
                      : `${row} × ${col} — ${fmt(cellVal)}${unit ? ` ${unit}` : ""}`;

                  const cellProps: React.SVGProps<SVGRectElement> & {
                    "data-testid": string;
                    "data-row": string;
                    "data-col": string;
                  } = {
                    x,
                    y,
                    width: CELL_W,
                    height: CELL_H,
                    rx: CELL_RX,
                    fill,
                    stroke: hairline,
                    strokeWidth: 0.5,
                    "aria-label": ariaLabelCell,
                    "data-testid": "matrix-heatmap-cell",
                    "data-row": row,
                    "data-col": col,
                    style: interactive ? { cursor: "pointer" } : undefined,
                  };

                  if (interactive) {
                    return (
                      <rect
                        key={`cell-${ri}-${ci}`}
                        {...cellProps}
                        tabIndex={0}
                        role="button"
                        onClick={() => onCellClick!(row, col, cellVal)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter" || e.key === " ") {
                            e.preventDefault();
                            onCellClick!(row, col, cellVal);
                          }
                        }}
                        onMouseEnter={(e) => {
                          (e.currentTarget as SVGRectElement).setAttribute(
                            "stroke",
                            accent,
                          );
                          (e.currentTarget as SVGRectElement).setAttribute(
                            "stroke-width",
                            "1.5",
                          );
                        }}
                        onMouseLeave={(e) => {
                          (e.currentTarget as SVGRectElement).setAttribute(
                            "stroke",
                            hairline,
                          );
                          (e.currentTarget as SVGRectElement).setAttribute(
                            "stroke-width",
                            "0.5",
                          );
                        }}
                      >
                        <title>{title}</title>
                      </rect>
                    );
                  }

                  return (
                    <rect
                      key={`cell-${ri}-${ci}`}
                      {...cellProps}
                      onMouseEnter={(e) => {
                        (e.currentTarget as SVGRectElement).setAttribute(
                          "stroke",
                          accent,
                        );
                        (e.currentTarget as SVGRectElement).setAttribute(
                          "stroke-width",
                          "1.5",
                        );
                      }}
                      onMouseLeave={(e) => {
                        (e.currentTarget as SVGRectElement).setAttribute(
                          "stroke",
                          hairline,
                        );
                        (e.currentTarget as SVGRectElement).setAttribute(
                          "stroke-width",
                          "0.5",
                        );
                      }}
                    >
                      <title>{title}</title>
                    </rect>
                  );
                })}
              </g>
            );
          })}
        </svg>
      </Box>
    </Box>
  );
}
