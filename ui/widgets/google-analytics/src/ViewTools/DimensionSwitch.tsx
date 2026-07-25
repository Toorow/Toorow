/**
 * DimensionSwitch — active breakdown-dimension selector (Story 8.11, R5).
 *
 * Lets the user pick which breakdown series to view, INCLUDING composite
 * sub-dimension splits ('country>device' → « Pays > Appareil »). Data-driven:
 * the available dimensions are derived from the loaded rows, so a module that
 * emits new (or new composite) dimensions gets the toggle for free.
 *
 * Controlled component: no internal state. French-first labels (UX-DR10).
 * Only MD3 tokens — no inline color literals (AD-11). Zero server round-trip
 * (AD-10): switching dimensions re-derives from the preloaded rows.
 */

import ToggleButton from "@mui/material/ToggleButton";
import ToggleButtonGroup from "@mui/material/ToggleButtonGroup";
import { COMPOSITE_SEPARATOR } from "../dataUtils";

/** French display labels for known single dimensions. */
export const DIMENSION_DISPLAY_LABELS: Record<string, string> = {
  date: "Date",
  device_category: "Appareil",
  device: "Appareil",
  country: "Pays",
  page: "Page",
};

/**
 * Resolve a breakdown dimension id to its French display label.
 * Composite dimensions ('country>device') are labeled part-by-part joined with
 * ' > ' (« Pays > Appareil »). Unknown single dims fall back to Title Case.
 */
export function dimensionLabel(dimension: string): string {
  if (dimension.includes(COMPOSITE_SEPARATOR)) {
    return dimension
      .split(COMPOSITE_SEPARATOR)
      .map((part) => dimensionLabel(part.trim()))
      .join(" > ");
  }
  if (DIMENSION_DISPLAY_LABELS[dimension]) return DIMENSION_DISPLAY_LABELS[dimension];
  return dimension
    .split("_")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

interface DimensionSwitchProps {
  /** All breakdown dimension ids available in the loaded rows. */
  dimensions: string[];
  /** Currently active dimension. */
  value: string;
  onChange: (dimension: string) => void;
}

export default function DimensionSwitch({
  dimensions,
  value,
  onChange,
}: DimensionSwitchProps) {
  if (dimensions.length === 0) return null;

  return (
    <ToggleButtonGroup
      size="small"
      exclusive
      value={value}
      onChange={(_e, v: string | null) => {
        // Never allow empty selection.
        if (v !== null) onChange(v);
      }}
      aria-label="Ventilation active"
    >
      {dimensions.map((d) => (
        <ToggleButton key={d} value={d} aria-label={dimensionLabel(d)}>
          {dimensionLabel(d)}
        </ToggleButton>
      ))}
    </ToggleButtonGroup>
  );
}
