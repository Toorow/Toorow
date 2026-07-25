/**
 * RankedList — classement compact à deltas (Story 23.5).
 *
 * Règles Origin : lignes ~40px, hairlines entre lignes (pas la dernière),
 * valeurs numériques en tabular-nums + fontWeight 600, triangles ▲/▼ colorés
 * via theme.palette.success/error selon semanticDirection.
 *
 * Sémantique AD-9 (NON NÉGOCIABLE) :
 *   delta null → « — » discret, AUCUN triangle, jamais interprété comme 0.
 *   value null → « — » pour la valeur principale.
 *   delta = 0 → « = » en text.secondary (pas de fausse tendance).
 *
 * semanticDirection :
 *   "up_good"   : delta > 0 → ▲ success, delta < 0 → ▼ error
 *   "down_good" : delta > 0 → ▲ error (monter est mauvais), delta < 0 → ▼ success
 *   "neutral"   : triangles en text.secondary
 *
 * Strings françaises (UX-DR10).
 */

import Box from "@mui/material/Box";
import Typography from "@mui/material/Typography";
import { useTheme } from "@mui/material/styles";
import { getVizPalette } from "./vizTheme";

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

export interface RankedListEntry {
  label: string;
  value: number | null;
  secondary?: number | null;
  delta?: number | null;
}

export interface RankedListProps {
  entries: RankedListEntry[];
  /** Sémantique du delta : "up_good" (défaut) | "down_good" | "neutral". */
  semanticDirection?: "up_good" | "down_good" | "neutral";
  unit?: string;
  secondaryUnit?: string;
  ariaLabel?: string;
  /** Nombre maximum de lignes affichées (top-N). */
  maxRows?: number;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Formate un nombre en fr-FR ou retourne « — » si null. */
function fmtNum(v: number | null | undefined, unit?: string): string {
  if (v === null || v === undefined) return "—";
  const formatted = v.toLocaleString("fr-FR");
  return unit ? `${formatted} ${unit}` : formatted;
}

interface DeltaResult {
  symbol: "▲" | "▼" | "=" | null;
  label: string;
  colorKey: "success" | "error" | "secondary" | null;
}

/**
 * Résout le symbole et la couleur sémantique d'un delta.
 * Retourne null pour symbol et colorKey quand delta est null (AD-9).
 */
function resolveDelta(
  delta: number | null | undefined,
  direction: "up_good" | "down_good" | "neutral",
): DeltaResult {
  // AD-9 : delta null = indéfini, aucun triangle
  if (delta === null || delta === undefined) {
    return { symbol: null, label: "indéfini", colorKey: null };
  }

  if (delta === 0) {
    return { symbol: "=", label: "stable", colorKey: "secondary" };
  }

  const isPositive = delta > 0;

  if (direction === "neutral") {
    return {
      symbol: isPositive ? "▲" : "▼",
      label: isPositive ? "en hausse" : "en baisse",
      colorKey: "secondary",
    };
  }

  // up_good : positif = bon (success), négatif = mauvais (error)
  // down_good : positif = mauvais (error), négatif = bon (success)
  const isGood =
    direction === "up_good" ? isPositive : !isPositive;

  return {
    symbol: isPositive ? "▲" : "▼",
    label: isPositive ? "en hausse" : "en baisse",
    colorKey: isGood ? "success" : "error",
  };
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function RankedList({
  entries,
  semanticDirection = "up_good",
  unit,
  secondaryUnit,
  ariaLabel = "Classement",
  maxRows,
}: RankedListProps) {
  const theme = useTheme();
  const { hairline } = getVizPalette(theme);

  // État vide designé (card rule e)
  if (!entries || entries.length === 0) {
    return (
      <Box
        data-testid="ranked-list-empty"
        role="status"
        sx={{
          py: 3,
          textAlign: "center",
          color: "text.secondary",
        }}
      >
        <Typography variant="body2">Aucune donnée à afficher</Typography>
      </Box>
    );
  }

  const sliced = maxRows !== undefined ? entries.slice(0, maxRows) : entries;

  function resolveColor(
    colorKey: "success" | "error" | "secondary" | null,
  ): string {
    if (colorKey === "success") return theme.palette.success.main;
    if (colorKey === "error") return theme.palette.error.main;
    if (colorKey === "secondary") return theme.palette.text.secondary;
    // null (delta indéfini) : non utilisé (on n'affiche pas de symbole)
    return theme.palette.text.secondary;
  }

  return (
    <Box
      role="list"
      aria-label={ariaLabel}
      data-testid="ranked-list"
      sx={{ width: "100%" }}
    >
      {sliced.map((entry, idx) => {
        const { symbol, label: deltaLabel, colorKey } = resolveDelta(
          entry.delta,
          semanticDirection,
        );
        const symbolColor = resolveColor(colorKey);

        // Texte accessible de la ligne
        const valueText =
          entry.value !== null && entry.value !== undefined
            ? fmtNum(entry.value, unit)
            : "—";
        const deltaDesc =
          symbol === null ? "—" : deltaLabel;

        return (
          <Box
            key={`${entry.label}-${idx}`}
            role="listitem"
            aria-label={`${entry.label} : ${valueText}, ${deltaDesc}`}
            data-testid="ranked-list-row"
            sx={{
              display: "flex",
              alignItems: "center",
              minHeight: 40,
              borderBottom:
                idx < sliced.length - 1
                  ? `1px solid ${hairline}`
                  : "none",
              gap: 1,
              px: 0.5,
            }}
          >
            {/* Label */}
            <Typography
              variant="body2"
              noWrap
              title={entry.label}
              sx={{ flex: "1 1 auto", minWidth: 0 }}
            >
              {entry.label}
            </Typography>

            {/* Valeurs numériques + delta — alignés à droite */}
            <Box
              sx={{
                display: "flex",
                alignItems: "center",
                gap: 0.75,
                flexShrink: 0,
              }}
            >
              {/* Valeur secondaire */}
              {entry.secondary !== undefined && (
                <Typography
                  variant="body2"
                  color="text.secondary"
                  sx={{ fontVariantNumeric: "lining-nums tabular-nums" }}
                  data-testid="ranked-list-secondary"
                >
                  {fmtNum(entry.secondary, secondaryUnit)}
                </Typography>
              )}

              {/* Valeur principale */}
              <Typography
                variant="body2"
                sx={{
                  fontVariantNumeric: "lining-nums tabular-nums",
                  fontWeight: 600,
                }}
                data-testid="ranked-list-value"
              >
                {fmtNum(entry.value, unit)}
              </Typography>

              {/* Indicateur delta */}
              <Box
                aria-hidden
                data-testid={symbol !== null ? "ranked-list-delta" : undefined}
                sx={{
                  minWidth: 18,
                  textAlign: "center",
                  fontVariantNumeric: "lining-nums tabular-nums",
                }}
              >
                {symbol !== null ? (
                  <Typography
                    component="span"
                    variant="caption"
                    sx={{
                      color: symbolColor,
                      fontWeight: 700,
                      lineHeight: 1,
                    }}
                    data-testid="ranked-list-delta-symbol"
                  >
                    {symbol}
                  </Typography>
                ) : (
                  <Typography
                    component="span"
                    variant="caption"
                    color="text.secondary"
                    sx={{ lineHeight: 1 }}
                    data-testid="ranked-list-delta-null"
                  >
                    —
                  </Typography>
                )}
              </Box>
            </Box>
          </Box>
        );
      })}
    </Box>
  );
}
