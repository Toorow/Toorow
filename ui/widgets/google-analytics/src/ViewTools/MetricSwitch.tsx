/**
 * MetricSwitch — active metric selector (Story 1.7, T4).
 *
 * Renders available metrics derived from data.rows (unique metric field values).
 * Controlled component: no internal state.
 * French-first labels (UX-DR10). Only MD3 tokens — no inline color literals (AD-11).
 * Zero server round-trip (AD-10).
 */

import ToggleButton from "@mui/material/ToggleButton";
import ToggleButtonGroup from "@mui/material/ToggleButtonGroup";

/** French display labels for known metrics. */
export const METRIC_DISPLAY_LABELS: Record<string, string> = {
  sessions: "Sessions",
  active_users: "Utilisateurs actifs",
  conversions: "Conversions",
};

/**
 * T4.2 — Resolve a metric ID to its French display label.
 * Falls back to snake_case → Title Case for unknown future metrics.
 */
export function metricLabel(metric: string): string {
  if (METRIC_DISPLAY_LABELS[metric]) return METRIC_DISPLAY_LABELS[metric];
  // Fallback: snake_case → Title Case
  return metric
    .split("_")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

interface MetricSwitchProps {
  /** All metric IDs available in the loaded rows. */
  metrics: string[];
  /** Currently active metric. */
  value: string;
  onChange: (metric: string) => void;
}

export default function MetricSwitch({ metrics, value, onChange }: MetricSwitchProps) {
  if (metrics.length === 0) return null;

  return (
    <ToggleButtonGroup
      size="small"
      exclusive
      value={value}
      onChange={(_e, v: string | null) => {
        // Never allow empty selection.
        if (v !== null) onChange(v);
      }}
      aria-label="Métrique active"
    >
      {metrics.map((m) => (
        <ToggleButton key={m} value={m} aria-label={metricLabel(m)}>
          {metricLabel(m)}
        </ToggleButton>
      ))}
    </ToggleButtonGroup>
  );
}
