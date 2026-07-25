/**
 * GranularityToggle — day / week / month granularity control (Story 1.7, T2).
 *
 * Controlled component: passes value + onChange up; no internal state.
 * Uses only MD3 tokens from the WidgetShell theme — no inline color literals (AD-11).
 * French-first labels (UX-DR10).
 * Zero server round-trip (AD-10).
 */

import ToggleButton from "@mui/material/ToggleButton";
import ToggleButtonGroup from "@mui/material/ToggleButtonGroup";
import type { Granularity } from "./aggregation";

export const GRANULARITY_LABELS: Record<Granularity, string> = {
  day: "Jour",
  week: "Semaine",
  month: "Mois",
};

const GRANULARITIES: Granularity[] = ["day", "week", "month"];

interface GranularityToggleProps {
  value: Granularity;
  onChange: (g: Granularity) => void;
}

export default function GranularityToggle({ value, onChange }: GranularityToggleProps) {
  return (
    <ToggleButtonGroup
      size="small"
      exclusive
      value={value}
      onChange={(_e, v: Granularity | null) => {
        // MUI passes null when the user clicks the already-selected button;
        // keep the current value in that case (never allow empty selection).
        if (v !== null) onChange(v);
      }}
      aria-label="Granularité"
    >
      {GRANULARITIES.map((g) => (
        <ToggleButton key={g} value={g} aria-label={GRANULARITY_LABELS[g]}>
          {GRANULARITY_LABELS[g]}
        </ToggleButton>
      ))}
    </ToggleButtonGroup>
  );
}
