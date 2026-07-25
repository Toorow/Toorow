/**
 * KpiDeltaFooter — pied de card avec blocs KPI côte à côte (Story 23.5).
 *
 * Règles Origin : le nombre est le héros (h5-like, fontWeight 700, tabular-nums),
 * hairline top divider, label en overline/caption text.secondary, delta coloré
 * success/error selon semanticDirection, caption discrète en dessous.
 *
 * Sémantique AD-9 (NON NÉGOCIABLE) :
 *   delta_pct null → « — » sans flèche, jamais interprété.
 *   value null → « — » héros (honnête).
 *
 * semanticDirection (même convention que RankedList) :
 *   "up_good"   : delta > 0 → ↑ success, delta < 0 → ↓ error
 *   "down_good" : delta > 0 → ↑ error,   delta < 0 → ↓ success
 *   "neutral"   : flèches en text.secondary
 *
 * État vide : items vide → rend null (le footer disparaît proprement).
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

export interface KpiDeltaItem {
  label: string;
  value: string | number | null;
  /** Pourcentage de delta (signé, ex. 19.6 ou -5.2) ; null = indéfini (AD-9). */
  delta_pct?: number | null;
  /** Valeur de comparaison discrète affichée sous le delta. */
  caption?: string;
}

export interface KpiDeltaFooterProps {
  items: KpiDeltaItem[];
  /** Sémantique du delta : "up_good" (défaut) | "down_good" | "neutral". */
  semanticDirection?: "up_good" | "down_good" | "neutral";
  ariaLabel?: string;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Formate une valeur héros : number → toLocaleString fr-FR, string → tel quel, null → « — ». */
function fmtHero(v: string | number | null): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "number") return v.toLocaleString("fr-FR");
  return v;
}

/** Formate un pourcentage de delta en 1 décimale fr-FR. */
function fmtPct(v: number): string {
  return v.toLocaleString("fr-FR", {
    minimumFractionDigits: 1,
    maximumFractionDigits: 1,
  });
}

interface DeltaDisplay {
  arrow: "↑" | "↓" | null;
  text: string;
  colorKey: "success" | "error" | "secondary" | null;
}

/**
 * Résout l'affichage du delta_pct.
 * null → indéfini (AD-9) : arrow=null, text="—", colorKey=null.
 */
function resolveDeltaPct(
  delta_pct: number | null | undefined,
  direction: "up_good" | "down_good" | "neutral",
): DeltaDisplay {
  // AD-9 : delta_pct null = indéfini, aucune flèche
  if (delta_pct === null || delta_pct === undefined) {
    return { arrow: null, text: "—", colorKey: null };
  }

  const isPositive = delta_pct > 0;
  const isNeutralDir = direction === "neutral";

  let colorKey: "success" | "error" | "secondary";
  if (isNeutralDir) {
    colorKey = "secondary";
  } else {
    const isGood = direction === "up_good" ? isPositive : !isPositive;
    colorKey = isGood ? "success" : "error";
  }

  const sign = isPositive ? "+" : "";
  const text = `${sign}${fmtPct(delta_pct)} %`;
  const arrow: "↑" | "↓" = isPositive ? "↑" : "↓";

  return { arrow, text, colorKey };
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

/**
 * KpiDeltaFooter — rend null quand items est vide (le footer disparaît proprement).
 */
export default function KpiDeltaFooter({
  items,
  semanticDirection = "up_good",
  ariaLabel = "Résumé KPI",
}: KpiDeltaFooterProps) {
  const theme = useTheme();
  const { hairline } = getVizPalette(theme);

  // État vide : retourne null pour que le footer disparaisse proprement
  if (!items || items.length === 0) {
    return null;
  }

  function resolveColor(colorKey: "success" | "error" | "secondary" | null): string {
    if (colorKey === "success") return theme.palette.success.main;
    if (colorKey === "error") return theme.palette.error.main;
    return theme.palette.text.secondary;
  }

  return (
    <Box
      aria-label={ariaLabel}
      data-testid="kpi-delta-footer"
      sx={{
        display: "flex",
        flexDirection: "row",
        gap: 3,
        pt: 1.5,
        borderTop: `1px solid ${hairline}`,
        flexWrap: "wrap",
      }}
    >
      {items.map((item, idx) => {
        const { arrow, text, colorKey } = resolveDeltaPct(
          item.delta_pct,
          semanticDirection,
        );
        const deltaColor = resolveColor(colorKey);

        return (
          <Box
            key={`${item.label}-${idx}`}
            data-testid="kpi-delta-footer-item"
            sx={{ display: "flex", flexDirection: "column", gap: 0.25 }}
          >
            {/* Label overline */}
            <Typography
              variant="overline"
              color="text.secondary"
              sx={{ lineHeight: 1.2, fontSize: "0.6rem", letterSpacing: "0.08em" }}
              data-testid="kpi-delta-footer-label"
            >
              {item.label}
            </Typography>

            {/* Valeur héros */}
            <Typography
              component="p"
              sx={{
                fontSize: "1.5rem",
                fontWeight: 700,
                lineHeight: 1.1,
                fontVariantNumeric: "lining-nums tabular-nums",
                color: "text.primary",
              }}
              data-testid="kpi-delta-footer-value"
            >
              {fmtHero(item.value)}
            </Typography>

            {/* Ligne delta */}
            <Box
              sx={{ display: "flex", alignItems: "baseline", gap: 0.5 }}
              data-testid="kpi-delta-footer-delta-row"
            >
              {arrow !== null ? (
                <Typography
                  component="span"
                  variant="body2"
                  sx={{
                    color: deltaColor,
                    fontWeight: 600,
                    fontVariantNumeric: "lining-nums tabular-nums",
                  }}
                  data-testid="kpi-delta-footer-delta"
                >
                  {arrow}
                  {text}
                </Typography>
              ) : (
                <Typography
                  component="span"
                  variant="body2"
                  color="text.secondary"
                  data-testid="kpi-delta-footer-delta-null"
                >
                  —
                </Typography>
              )}
            </Box>

            {/* Caption discrète */}
            {item.caption !== undefined && item.caption !== "" && (
              <Typography
                variant="caption"
                color="text.secondary"
                sx={{ lineHeight: 1.3 }}
                data-testid="kpi-delta-footer-caption"
              >
                {item.caption}
              </Typography>
            )}
          </Box>
        );
      })}
    </Box>
  );
}
