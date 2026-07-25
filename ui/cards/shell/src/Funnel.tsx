/**
 * Funnel (entonnoir) — étapes ordonnées avec taux de passage et shading drop-off (Story 9.2b).
 *
 * Exemples : sessions → conversions, entrée → checkout → paiement.
 * Chaque étape : libellé, valeur, taux de passage depuis l'étape précédente.
 * Drop-off : shading visuel entre étapes (la perte est visible).
 * Aucune dépendance graphique externe (AD-11). Light+dark via theme.
 * État vide designé quand steps est vide.
 */

import { useTheme, alpha } from "@mui/material/styles";
import Box from "@mui/material/Box";
import Typography from "@mui/material/Typography";

export interface FunnelStep {
  label: string;
  value: number;
}

export interface FunnelProps {
  steps: FunnelStep[];
  unit?: string;
  ariaLabel?: string;
  /**
   * Colour threshold: through-rates >= successThreshold are coloured success.main,
   * rates < successThreshold are coloured error.main (designed partial state, not neutral).
   * Rates > 100% (impossible funnel / data anomaly) are never coloured success — they
   * render in warning.main with the raw value in the label to flag the anomaly (F-7/F-9).
   * Defaults to 50 for backward-compat; pass a lower value for conversion-rate funnels
   * (e.g. 5 for typical e-commerce) to avoid false-red rates that are within benchmark.
   */
  successThreshold?: number;
}

export default function Funnel({
  steps,
  unit,
  ariaLabel = "Entonnoir de conversion",
  successThreshold = 50,
}: FunnelProps) {
  const theme = useTheme();
  const accent = theme.palette.primary.main;

  if (steps.length === 0) {
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
        data-testid="funnel-empty"
        role="img"
        aria-label={`${ariaLabel} — aucune donnée`}
      >
        <Typography variant="body2" color="text.disabled">
          —
        </Typography>
        <Typography variant="caption" color="text.disabled">
          Aucune étape disponible
        </Typography>
      </Box>
    );
  }

  const maxVal = Math.max(...steps.map((s) => s.value), 1);

  return (
    <Box
      data-testid="funnel"
      role="img"
      aria-label={ariaLabel}
      sx={{ display: "flex", flexDirection: "column", gap: 0 }}
    >
      {steps.map((step, i) => {
        const prev = i > 0 ? steps[i - 1] : null;
        const pctOfMax = Math.max(4, (step.value / maxVal) * 100);
        // Taux de passage depuis l'étape précédente
        const rawThroughRate =
          prev && prev.value > 0
            ? Math.round((step.value / prev.value) * 100)
            : null;
        // Anomaly: through-rate > 100% means data inconsistency (double-counted rows etc.).
        // Never colour it success; render in warning.main with the raw value visible (F-7).
        const isAnomaly = rawThroughRate !== null && rawThroughRate > 100;
        // Display rate clamped at 100% for the bar width; label shows raw value (F-7).
        const throughRate = rawThroughRate;
        // Drop-off visuel : espace entre les barres
        const dropOff = prev ? prev.value - step.value : 0;
        // For anomaly (step > prev), dropOff is negative — show 0% drop in that case.
        const dropPct = prev && prev.value > 0 && !isAnomaly
          ? 100 - Math.min(100, (throughRate ?? 100))
          : 0;

        return (
          <Box key={step.label} sx={{ display: "flex", flexDirection: "column", gap: 0 }}>
            {/* Indicateur de passage inter-étapes */}
            {i > 0 && (
              <Box
                sx={{
                  display: "flex",
                  alignItems: "center",
                  gap: 1,
                  py: 0.4,
                  pl: 1,
                  flexWrap: "wrap",
                }}
              >
                <Typography
                  variant="caption"
                  color={
                    isAnomaly
                      ? "warning.main"
                      : throughRate !== null && throughRate >= successThreshold
                        ? "success.main"
                        : "error.main"
                  }
                  sx={{ fontVariantNumeric: "lining-nums tabular-nums", lineHeight: 1 }}
                  data-testid={`funnel-through-rate-${i}`}
                >
                  {isAnomaly ? "⚠" : "▲"}{" "}
                  {throughRate !== null ? `${throughRate} %` : "—"}
                  {isAnomaly && (
                    <Typography component="span" variant="caption" color="text.disabled" sx={{ ml: 0.5 }}>
                      (anomalie données)
                    </Typography>
                  )}
                </Typography>
                {dropOff > 0 && (
                  <Typography
                    variant="caption"
                    color="text.disabled"
                    sx={{ fontVariantNumeric: "lining-nums tabular-nums", lineHeight: 1 }}
                  >
                    (−{dropOff.toLocaleString("fr-FR")} {unit ?? ""}, −{dropPct} %)
                  </Typography>
                )}
              </Box>
            )}

            {/* Barre de l'étape — track pleine largeur, barre interne qui se réduit
                (jamais de débordement quelle que soit la valeur de l'étape). */}
            <Box sx={{ display: "flex", alignItems: "center", gap: 1 }}>
              <Box sx={{ flex: 1, minWidth: 0 }} aria-hidden>
                <Box
                  sx={{
                    width: `${pctOfMax}%`,
                    height: 28,
                    minWidth: 56,
                    bgcolor: alpha(accent, 0.15 + (step.value / maxVal) * 0.25),
                    borderLeft: `3px solid ${accent}`,
                    borderRadius: "0 4px 4px 0",
                    display: "flex",
                    alignItems: "center",
                    pl: 1,
                    transition: "width 0.3s",
                  }}
                >
                  <Typography
                    variant="body2"
                    noWrap
                    sx={{
                      fontWeight: 600,
                      fontVariantNumeric: "lining-nums tabular-nums",
                      color: "text.primary",
                      lineHeight: 1,
                    }}
                  >
                    {step.value.toLocaleString("fr-FR")}
                    {unit ? ` ${unit}` : ""}
                  </Typography>
                </Box>
              </Box>
              {/* Label à droite */}
              <Typography
                variant="caption"
                color="text.secondary"
                sx={{ flexShrink: 0, width: 110, lineHeight: 1.3 }}
              >
                {step.label}
              </Typography>
            </Box>
          </Box>
        );
      })}
    </Box>
  );
}
