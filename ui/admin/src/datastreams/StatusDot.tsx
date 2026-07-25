/**
 * StatusDot — small tinted dot representing an extract ledger status.
 *
 * Rule 4: No default MUI chips — status = small tinted dot only.
 * Rule 1: Only accent for 'running'; semantics use low-alpha tints.
 */
import { Tooltip } from "@mui/material";
import { LedgerEntry, STATUS_META } from "./types";

interface StatusDotProps {
  status: LedgerEntry["status"];
  size?: number;
  showTooltip?: boolean;
}

export default function StatusDot({
  status,
  size = 8,
  showTooltip = true,
}: StatusDotProps) {
  const meta = STATUS_META[status];
  const dot = (
    <span
      aria-label={meta.label}
      style={{
        display: "inline-block",
        width: size,
        height: size,
        borderRadius: "50%",
        backgroundColor: meta.dotColor,
        flexShrink: 0,
      }}
    />
  );

  if (!showTooltip) return dot;

  return (
    <Tooltip title={meta.label} arrow placement="top">
      {dot}
    </Tooltip>
  );
}
