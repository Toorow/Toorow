/**
 * MonitorCard — KPI widget for one DQ monitor (Story 8.6).
 *
 * Design rules (Epic 8 Part B):
 *  Rule 1: One accent (#FF99C8) for the selected-state ring only.
 *  Rule 3: The number (healthy_pct %) is the hero — big, bold, tabular-nums.
 *  Rule 4: No default-MUI raised buttons or chips.
 *  Rule 5: Generous padding (24px).
 *
 */
import { Box, Card, Tooltip, Typography } from "@mui/material";
import { MonitorSummary, MONITOR_META } from "./types";

// ---------------------------------------------------------------------------
// Monitor icon (inline SVG, no external dep)
// ---------------------------------------------------------------------------

function MonitorIcon({ kind, size = 20 }: { kind: string; size?: number }) {
  const props = {
    width: size,
    height: size,
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.5,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    "aria-hidden": true,
  };

  switch (kind) {
    case "volume":
      return (
        <svg {...props}>
          <rect x="3" y="12" width="4" height="9" rx="1" />
          <rect x="10" y="7" width="4" height="14" rx="1" />
          <rect x="17" y="3" width="4" height="18" rx="1" />
        </svg>
      );
    case "clock":
      return (
        <svg {...props}>
          <circle cx="12" cy="12" r="9" />
          <polyline points="12 7 12 12 15 15" />
        </svg>
      );
    case "copy":
      return (
        <svg {...props}>
          <rect x="9" y="9" width="11" height="11" rx="2" />
          <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
        </svg>
      );
    case "schema":
      return (
        <svg {...props}>
          <polyline points="16 18 22 12 16 6" />
          <polyline points="8 6 2 12 8 18" />
        </svg>
      );
    default:
      return null;
  }
}

// ---------------------------------------------------------------------------
// HealthRing — circular progress indicator
// ---------------------------------------------------------------------------

function HealthRing({
  pct,
  size = 64,
  accent = "#FF99C8",
  neutral = "rgba(235,235,243,0.12)",
}: {
  pct: number;
  size?: number;
  accent?: string;
  neutral?: string;
}) {
  const r = (size - 6) / 2;
  const circ = 2 * Math.PI * r;
  const dashOffset = circ * (1 - pct / 100);
  const cx = size / 2;
  const cy = size / 2;

  return (
    <svg width={size} height={size} style={{ flexShrink: 0 }}>
      {/* Track */}
      <circle
        cx={cx}
        cy={cy}
        r={r}
        fill="none"
        stroke={neutral}
        strokeWidth={5}
      />
      {/* Progress */}
      <circle
        cx={cx}
        cy={cy}
        r={r}
        fill="none"
        stroke={pct >= 80 ? accent : pct >= 50 ? "#F57C00" : "#E53935"}
        strokeWidth={5}
        strokeDasharray={circ}
        strokeDashoffset={dashOffset}
        strokeLinecap="round"
        transform={`rotate(-90 ${cx} ${cy})`}
        style={{ transition: "stroke-dashoffset 0.5s ease" }}
      />
    </svg>
  );
}

// ---------------------------------------------------------------------------
// MonitorCard
// ---------------------------------------------------------------------------

interface MonitorCardProps {
  monitor: MonitorSummary;
  selected: boolean;
  onClick: () => void;
}

export default function MonitorCard({ monitor, selected, onClick }: MonitorCardProps) {
  const meta = MONITOR_META[monitor.type] ?? {
    label: monitor.label,
    description: "",
    icon: "schema",
  };

  return (
    <Tooltip title={meta.description} placement="top" arrow>
      <Card
        onClick={onClick}
        sx={{
          p: 3,
          cursor: "pointer",
          border: selected
            ? "1.5px solid #FF99C8"
            : "1.5px solid rgba(235,235,243,0.12)",
          borderRadius: 3,
          bgcolor: selected
            ? "rgba(155,225,246,0.06)"
            : "background.paper",
          boxShadow: "none",
          transition: "border-color 0.2s, background 0.2s",
          "&:hover": {
            borderColor: selected ? "#FF99C8" : "rgba(235,235,243,0.30)",
            bgcolor: "rgba(155,225,246,0.04)",
          },
          display: "flex",
          flexDirection: "column",
          gap: 1.5,
          minWidth: 160,
          flex: "1 1 160px",
        }}
      >
        {/* Icon + label */}
        <Box sx={{ display: "flex", alignItems: "center", gap: 1, color: "text.secondary" }}>
          <MonitorIcon kind={meta.icon} size={16} />
          <Typography
            variant="caption"
            sx={{ fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.06em" }}
          >
            {meta.label}
          </Typography>
        </Box>

        {/* Hero row: ring + pct */}
        <Box sx={{ display: "flex", alignItems: "center", gap: 2 }}>
          <HealthRing pct={monitor.healthy_pct} size={56} />
          <Box>
            <Typography
              sx={{
                fontVariantNumeric: "lining-nums tabular-nums",
                fontSize: "2rem",
                fontWeight: 700,
                lineHeight: 1,
                color: "text.primary",
              }}
            >
              {monitor.healthy_pct.toFixed(0)}
              <span style={{ fontSize: "1rem", fontWeight: 400, opacity: 0.5 }}>%</span>
            </Typography>
            <Typography variant="caption" sx={{ color: "text.secondary" }}>
              healthy streams
            </Typography>
          </Box>
        </Box>

        {/* Counts row */}
        <Box sx={{ display: "flex", gap: 2 }}>
          <Box>
            <Typography
              sx={{
                fontVariantNumeric: "lining-nums tabular-nums",
                fontSize: "1.1rem",
                fontWeight: 600,
                color: monitor.unresolved_count > 0 ? "#F57C00" : "text.primary",
              }}
            >
              {monitor.unresolved_count}
            </Typography>
            <Typography variant="caption" sx={{ color: "text.secondary" }}>
              unresolved
            </Typography>
          </Box>
          {monitor.new_since_yesterday > 0 && (
            <Box>
              <Typography
                sx={{
                  fontVariantNumeric: "lining-nums tabular-nums",
                  fontSize: "1.1rem",
                  fontWeight: 600,
                  color: "#E53935",
                }}
              >
                +{monitor.new_since_yesterday}
              </Typography>
              <Typography variant="caption" sx={{ color: "text.secondary" }}>
                new
              </Typography>
            </Box>
          )}
        </Box>
      </Card>
    </Tooltip>
  );
}
