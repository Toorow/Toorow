/**
 * KpiTile — premium tabular-numeral KPI hero tile (Story 8.8 / Rule 3).
 *
 * Layout:
 *   - Label (overline, muted) — top
 *   - HERO: the absolute value — big, bold, tabular-nums (rule 3)
 *   - Small muted delta line below the hero (arrow + %, direction-aware tint)
 *   - Optional info affordance: hover/focus tooltip with definition + unit +
 *     caveats (R6 — only when metric_definitions is provided)
 *
 * Direction semantics (R6):
 *   - "up_good": positive delta → accent/success tint, negative → error tint.
 *   - "down_good": inverted — positive delta → error tint, negative → success tint.
 *   - "neutral": delta arrow rendered in text.secondary (no semantic color).
 *
 * Rules compliance (Story 8.8):
 *   1. One accent — the tile chrome is neutral; delta arrow only gets a tint.
 *   2. Near-black/near-white — no hardcoded colors beyond theme tokens.
 *   3. The number is the hero — absolute value is h3, delta is caption.
 *   4. No default-MUI look — borderless card, diffuse shadow.
 *   5. Generous spacing — py: 2.5, px: 2.
 *   6. Tokens from theme — no inline hex.
 */

import { useState, useId } from "react";
import Box from "@mui/material/Box";
import Typography from "@mui/material/Typography";
import { useTheme, alpha } from "@mui/material/styles";
import { ArrowUpwardIcon, ArrowDownwardIcon } from "./icons";
import type { MetricDefinition } from "./types";

interface KpiTileProps {
  label: string;
  currentValue: number;
  /** deltaPct is null when the previous period was 0 (undefined change). */
  deltaPct: number | null;
  /** R6: optional definition for the info tooltip. Absent = no affordance. */
  definition?: MetricDefinition;
}

/**
 * InfoTooltip — hover/focus button that reveals metric definition in a tooltip.
 * Accessible: role="tooltip" + aria-describedby.
 */
function InfoTooltip({ def, id }: { def: MetricDefinition; id: string }) {
  const [visible, setVisible] = useState(false);
  const theme = useTheme();

  return (
    <Box
      sx={{ position: "relative", display: "inline-flex", ml: 0.5, verticalAlign: "middle" }}
    >
      <Box
        component="button"
        aria-label="Définition de la métrique"
        aria-describedby={id}
        onMouseEnter={() => setVisible(true)}
        onMouseLeave={() => setVisible(false)}
        onFocus={() => setVisible(true)}
        onBlur={() => setVisible(false)}
        sx={{
          all: "unset",
          cursor: "pointer",
          width: 16,
          height: 16,
          borderRadius: "50%",
          border: `1px solid ${alpha(theme.palette.text.secondary, 0.4)}`,
          display: "inline-flex",
          alignItems: "center",
          justifyContent: "center",
          fontSize: "0.6rem",
          fontWeight: 700,
          color: theme.palette.text.secondary,
          lineHeight: 1,
          userSelect: "none",
          "&:hover, &:focus-visible": {
            borderColor: theme.palette.primary.main,
            color: theme.palette.primary.main,
          },
        }}
        data-testid="kpi-info-button"
      >
        i
      </Box>

      {/* Tooltip panel */}
      {visible && (
        <Box
          role="tooltip"
          id={id}
          sx={{
            position: "absolute",
            bottom: "calc(100% + 6px)",
            left: "50%",
            transform: "translateX(-50%)",
            zIndex: 1500,
            width: 260,
            p: 1.5,
            borderRadius: 2,
            bgcolor: "background.paper",
            boxShadow: "0 4px 20px rgba(1,0,10,0.14), 0 1px 4px rgba(1,0,10,0.08)",
            border: `1px solid ${alpha(theme.palette.text.primary, 0.1)}`,
            pointerEvents: "none",
          }}
        >
          {def.unit && (
            <Typography
              variant="overline"
              sx={{ display: "block", mb: 0.5, lineHeight: 1.2 }}
              color="text.secondary"
            >
              {def.unit}
            </Typography>
          )}
          <Typography variant="caption" component="p" sx={{ mb: 0.75, lineHeight: 1.5 }}>
            {def.definition}
          </Typography>
          {def.caveats && (
            <Typography
              variant="caption"
              component="p"
              color="text.secondary"
              sx={{
                pt: 0.75,
                borderTop: `1px solid ${alpha(theme.palette.text.primary, 0.08)}`,
                lineHeight: 1.4,
                fontStyle: "italic",
              }}
            >
              {def.caveats}
            </Typography>
          )}
        </Box>
      )}
    </Box>
  );
}

export default function KpiTile({ label, currentValue, deltaPct, definition }: KpiTileProps) {
  const theme = useTheme();
  const tooltipId = useId();

  const direction = definition?.direction ?? "up_good";

  // Map delta sign + direction to semantic color.
  function deltaColor(): string {
    if (deltaPct === null) return theme.palette.text.secondary;
    if (direction === "neutral") return theme.palette.text.secondary;

    const isPositive = deltaPct >= 0;
    const isGood = direction === "up_good" ? isPositive : !isPositive;
    return isGood ? theme.palette.success.main : theme.palette.error.main;
  }

  const arrowColor = deltaColor();

  const deltaText =
    deltaPct === null
      ? "—"
      : `${deltaPct >= 0 ? "+" : ""}${deltaPct.toFixed(1)} %`;

  return (
    <Box
      sx={{
        flex: "1 1 0",
        minWidth: 140,
        px: 2,
        py: 2.5,
        borderRadius: 2,
        // Light mode: white card + diffuse shadow (rule 2, 4)
        background: "background.paper",
        bgcolor: "background.paper",
        boxShadow: "0 2px 8px rgba(1,0,10,0.06), 0 1px 3px rgba(1,0,10,0.04)",
        // Dark mode: semi-transparent elevated surface
        "@media (prefers-color-scheme: dark)": {
          boxShadow: "0 2px 16px rgba(0,0,0,0.4)",
        },
        position: "relative",
      }}
      data-testid="kpi-tile"
    >
      {/* Label + optional info button */}
      <Box sx={{ display: "flex", alignItems: "center", mb: 0.5 }}>
        <Typography
          variant="overline"
          color="text.secondary"
          sx={{ lineHeight: 1.2, flexShrink: 0 }}
        >
          {label}
        </Typography>
        {definition && <InfoTooltip def={definition} id={tooltipId} />}
      </Box>

      {/* HERO: absolute value — big, bold, tabular-nums (rule 3) */}
      <Typography
        variant="h3"
        component="p"
        sx={{
          fontWeight: 700,
          fontVariantNumeric: "lining-nums tabular-nums",
          lineHeight: 1.1,
          mb: 0.75,
          color: "text.primary",
        }}
        data-testid="kpi-hero-value"
      >
        {currentValue.toLocaleString("fr-FR")}
      </Typography>

      {/* Delta line — small, muted, direction-aware tint only on arrow (rule 3) */}
      <Box
        sx={{ display: "flex", alignItems: "center", gap: 0.25 }}
        data-testid="kpi-delta-line"
      >
        {deltaPct !== null && (
          <Box component="span" sx={{ color: arrowColor, display: "flex", alignItems: "center" }}>
            {deltaPct >= 0 ? (
              <ArrowUpwardIcon size={14} />
            ) : (
              <ArrowDownwardIcon size={14} />
            )}
          </Box>
        )}
        <Typography
          variant="caption"
          component="span"
          sx={{
            color: arrowColor,
            fontVariantNumeric: "lining-nums tabular-nums",
          }}
          data-testid="kpi-delta-text"
        >
          {deltaText}
        </Typography>
        <Typography variant="caption" color="text.secondary" component="span" sx={{ ml: 0.25 }}>
          vs période préc.
        </Typography>
      </Box>
    </Box>
  );
}
