/**
 * ValueGrid — grille de cellules arrondies colorées par performance (Story 23.4).
 *
 * Primitive de densité : chaque cellule affiche sa valeur numérique sur un fond
 * dégradé divergent (vert → jaune → rouge) dérivé du thème courant via getVizPalette.
 * Normalisation min-max sur les valeurs non-null ; null → cellule squelette (AD-9).
 * Aucune lib graphique externe (AD-11). Light + dark via MUI theme.
 *
 * Référence visuelle : card « Sales Report » — squircles denses, chiffres centrés,
 * contraste du texte garanti via getContrastText. Strings françaises (UX-DR10).
 */

import { useMemo } from "react";
import Box from "@mui/material/Box";
import Typography from "@mui/material/Typography";
import { useTheme, alpha } from "@mui/material/styles";
import { getVizPalette } from "./vizTheme";

export interface ValueGridCell {
  /** Libellé optionnel — affiché dans le tooltip natif (ex. une date, un nom). */
  label?: string;
  /** Valeur numérique ; null → cellule squelette (ne compte pas dans la normalisation). */
  value: number | null;
}

export interface ValueGridProps {
  cells: ValueGridCell[];
  /** Nombre de colonnes de la grille. Défaut : 7. */
  columns?: number;
  /**
   * Sémantique de direction :
   * - "up_good"   (défaut) : valeur max → diverging(0) vert, valeur min → diverging(1) rouge.
   * - "down_good"           : valeur min → diverging(0) vert, valeur max → diverging(1) rouge.
   */
  direction?: "up_good" | "down_good";
  /** Unité affichée dans le tooltip (ex. « € », « % »). */
  unit?: string;
  /** Aria-label global pour l'accessibilité. */
  ariaLabel?: string;
}

/** Taille d'une cellule (px). */
const CELL_SIZE = 36;

export default function ValueGrid({
  cells,
  columns = 7,
  direction = "up_good",
  unit,
  ariaLabel = "Grille de valeurs",
}: ValueGridProps) {
  const theme = useTheme();
  const viz = getVizPalette(theme);

  const isEmpty = cells.length === 0;

  // Calcul min/max sur les valeurs non-null uniquement (AD-9).
  const { min, max } = useMemo(() => {
    const nonNull = cells
      .map((c) => c.value)
      .filter((v): v is number => v !== null);
    if (nonNull.length === 0) return { min: 0, max: 0 };
    return {
      min: Math.min(...nonNull),
      max: Math.max(...nonNull),
    };
  }, [cells]);

  if (isEmpty) {
    return (
      <Box
        data-testid="value-grid-empty"
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
   * Retourne la couleur de fond d'une cellule en fonction de sa valeur normalisée.
   * - null         → fond squelette (hairline très léger)
   * - up_good      : 0→vert (t=0), 1→rouge (t=1)
   * - down_good    : 0→rouge (t=1), 1→vert (t=0)
   */
  function bgColor(value: number | null): string {
    if (value === null) {
      // Cellule squelette : fond hairline, jamais traitée comme 0 (AD-9).
      return alpha(theme.palette.text.primary, 0.06);
    }
    const range = max - min;
    if (range === 0) {
      // Toutes les valeurs sont identiques : couleur médiane (neutre jaune).
      return viz.diverging(direction === "up_good" ? 0 : 1);
    }
    const norm = (value - min) / range; // 0..1, 0 = min, 1 = max
    const t = direction === "up_good"
      ? 1 - norm   // up_good : max → t=0 (vert), min → t=1 (rouge)
      : norm;      // down_good : min → t=0 (vert), max → t=1 (rouge)
    return viz.diverging(t);
  }

  /**
   * Texte du tooltip natif pour une cellule.
   */
  function tooltipFor(cell: ValueGridCell): string {
    const label = cell.label ?? "";
    if (cell.value === null) {
      return label ? `${label} : —` : "—";
    }
    const formatted = cell.value.toLocaleString("fr-FR");
    const withUnit = unit ? `${formatted} ${unit}` : formatted;
    return label ? `${label} : ${withUnit}` : withUnit;
  }

  return (
    <Box
      role="grid"
      aria-label={ariaLabel}
      data-testid="value-grid"
      sx={{
        display: "grid",
        gridTemplateColumns: `repeat(${columns}, ${CELL_SIZE}px)`,
        gap: "4px",
      }}
    >
      {cells.map((cell, idx) => {
        const bg = bgColor(cell.value);
        const isNull = cell.value === null;
        // getContrastText nécessite une couleur opaque ; pour le squelette on utilise
        // text.disabled car le fond est trop transparent pour un contraste fiable.
        const textColor = isNull
          ? theme.palette.text.disabled
          : theme.palette.getContrastText(bg);

        return (
          <Box
            key={idx}
            role="gridcell"
            title={tooltipFor(cell)}
            aria-label={tooltipFor(cell)}
            data-testid="value-grid-cell"
            sx={{
              width: CELL_SIZE,
              height: CELL_SIZE,
              borderRadius: "10px",
              bgcolor: bg,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              flexShrink: 0,
            }}
          >
            <Typography
              variant="caption"
              sx={{
                fontVariantNumeric: "lining-nums tabular-nums",
                fontWeight: 600,
                color: textColor,
                lineHeight: 1,
                fontSize: "0.65rem",
              }}
            >
              {isNull ? "—" : cell.value!.toLocaleString("fr-FR")}
            </Typography>
          </Box>
        );
      })}
    </Box>
  );
}
