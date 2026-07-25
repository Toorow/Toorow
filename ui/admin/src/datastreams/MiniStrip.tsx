/**
 * MiniStrip — 35-day colored day-cell strip for the datastream list.
 *
 * Each cell is a tiny colored rect representing one day's extract status.
 * Rule 3: The strip is a data hero — tabular-nums on tooltips.
 * Rule 4: No default MUI components; raw HTML/CSS for cell rendering.
 */
import { Tooltip } from "@mui/material";
import { LedgerEntry, STATUS_META } from "./types";

interface MiniStripProps {
  ledger: LedgerEntry[];
  /** Width of each day cell in px (default 6) */
  cellWidth?: number;
  /** Height of each day cell in px (default 16) */
  cellHeight?: number;
}

export default function MiniStrip({
  ledger,
  cellWidth = 6,
  cellHeight = 16,
}: MiniStripProps) {
  if (!ledger || ledger.length === 0) {
    return (
      <span
        style={{
          display: "inline-block",
          width: cellWidth * 35 + 34, // 35 cells + gaps
          height: cellHeight,
          background: "rgba(235,235,243,0.08)",
          borderRadius: 3,
        }}
        aria-label="Aucune donnée"
      />
    );
  }

  return (
    <span
      role="img"
      aria-label="Bande d'extraction 35 jours"
      style={{ display: "inline-flex", gap: 1 }}
    >
      {ledger.map((entry) => {
        const meta = STATUS_META[entry.status];
        const label =
          `${entry.date} · ${meta.label}` +
          (entry.completeness_ratio != null
            ? ` · ${Math.round(entry.completeness_ratio * 100)}%`
            : "");

        return (
          <Tooltip key={entry.date} title={label} arrow placement="top">
            <span
              style={{
                display: "inline-block",
                width: cellWidth,
                height: cellHeight,
                borderRadius: 2,
                backgroundColor: meta.color,
                border: `1px solid ${meta.dotColor}30`,
                cursor: "default",
              }}
            />
          </Tooltip>
        );
      })}
    </span>
  );
}
