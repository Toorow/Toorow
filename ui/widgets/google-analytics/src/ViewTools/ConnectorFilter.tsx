/**
 * ConnectorFilter — multi-select connector filter (Story 1.7, T3).
 *
 * Controlled component: all logic lives in the parent. Data-driven from
 * data.connectors — not hard-coded. At least one connector must remain selected.
 * Uses only MD3 tokens — no inline color literals (AD-11).
 * French-first labels where applicable (UX-DR10).
 * Zero server round-trip (AD-10).
 */

import ToggleButton from "@mui/material/ToggleButton";
import ToggleButtonGroup from "@mui/material/ToggleButtonGroup";

/** Human-readable French display names for known connector IDs. */
export const TOOROW_LABELS: Record<string, string> = {
  "google-analytics": "Google Analytics",
  "meta-ads": "Meta Ads",
  "google-search-console": "Search Console",
};

/** Resolve a connector ID to its display label (falls back to raw ID). */
export function connectorLabel(id: string): string {
  return TOOROW_LABELS[id] ?? id;
}

interface ConnectorFilterProps {
  /** All connector IDs available in the data (from data.connectors). */
  connectors: string[];
  /** Currently selected connector IDs. */
  selected: string[];
  onChange: (selected: string[]) => void;
}

export default function ConnectorFilter({
  connectors,
  selected,
  onChange,
}: ConnectorFilterProps) {
  if (connectors.length <= 1) {
    // Single connector — no filter needed (still render for AC2 data-driven requirement).
    // But we suppress the control when there is exactly 0 available connectors.
    if (connectors.length === 0) return null;
  }

  function handleChange(_e: unknown, newValues: string[]) {
    // T3.3: prevent empty selection — keep current if the toggle would deselect the last.
    if (newValues.length === 0) return;
    onChange(newValues);
  }

  return (
    <ToggleButtonGroup
      size="small"
      value={selected}
      onChange={handleChange}
      aria-label="Filtrer par connecteur"
    >
      {connectors.map((id) => (
        <ToggleButton key={id} value={id} aria-label={connectorLabel(id)}>
          {connectorLabel(id)}
        </ToggleButton>
      ))}
    </ToggleButtonGroup>
  );
}
