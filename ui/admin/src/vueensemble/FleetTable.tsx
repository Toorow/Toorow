/**
 * FleetTable -- 52px-row table of all datastreams (Story 8.4).
 *
 * Columns: flux (name+module), autorisation dot, sparkline 7j, dernier extrait,
 *          prochain run, issues badge.
 *
 * Rule 1: One accent; only StatusDot + Sparkline use non-neutral color.
 * Rule 2: Near-black text, #EBEBF3 dividers.
 * Rule 4: No default MUI density -- custom 52px row height, no default chips.
 * Rule 5: Generous padding (16px cells).
 *
 * reuses: StatusDot from datastreams (import, not duplicate)
 *
 * Props:
 *   rows: FleetRow[]        -- from /api/overview
 *   onOpenDatastream?: fn   -- App wires this later; if absent row not clickable
 *
 * UX-DR10: French-first copy.
 */
import { Box, Skeleton, Tooltip, Typography } from "@mui/material";
import StatusDot from "../datastreams/StatusDot";
import type { FleetRow } from "./types";
import Sparkline from "./Sparkline";

// ---------------------------------------------------------------------------
// Design tokens
// ---------------------------------------------------------------------------
const ROW_HEIGHT = 52;
const DIVIDER_COLOR = "rgba(235,235,243,0.30)";
const ISSUES_BG = "rgba(229,57,53,0.10)";
const ISSUES_TEXT = "#E53935";
const HEADER_COLOR = "rgba(1,0,10,0.45)";

// ---------------------------------------------------------------------------
// Helper: format last extract date
// Note: NEAR_BLACK removed — use theme token color: "text.primary" in sx
// ---------------------------------------------------------------------------
function formatDate(iso: string): string {
  try {
    return new Intl.DateTimeFormat("fr-FR", { day: "2-digit", month: "short" }).format(
      new Date(iso)
    );
  } catch {
    return iso;
  }
}

// ---------------------------------------------------------------------------
// Auth status dot (connection_health.status)
// ---------------------------------------------------------------------------
function AuthDot({ status }: { status: string | null }) {
  const color =
    status === "active"
      ? "#2E7D32"
      : status === null || status === undefined
      ? "rgba(235,235,243,0.40)"
      : "#E53935";
  const label =
    status === "active"
      ? "Autorisation active"
      : status === null
      ? "Aucune autorisation"
      : `Autorisation ${status}`;

  return (
    <Tooltip title={label} arrow placement="top">
      <span
        aria-label={label}
        style={{
          display: "inline-block",
          width: 8,
          height: 8,
          borderRadius: "50%",
          backgroundColor: color,
          flexShrink: 0,
        }}
      />
    </Tooltip>
  );
}

// ---------------------------------------------------------------------------
// Issues badge (hidden when issues_count == 0)
// ---------------------------------------------------------------------------
function IssuesBadge({ count }: { count: number }) {
  if (count === 0) return null;
  return (
    <Box
      aria-label={`${count} problème${count > 1 ? "s" : ""}`}
      sx={{
        display: "inline-flex",
        alignItems: "center",
        px: 0.75,
        py: 0.25,
        borderRadius: 1,
        bgcolor: ISSUES_BG,
        color: ISSUES_TEXT,
        fontVariantNumeric: "lining-nums tabular-nums",
        fontSize: 11,
        fontWeight: 600,
        lineHeight: 1.4,
        userSelect: "none",
        whiteSpace: "nowrap",
      }}
    >
      {count}
    </Box>
  );
}

// ---------------------------------------------------------------------------
// Table header cell
// ---------------------------------------------------------------------------
function TH({ children }: { children: React.ReactNode }) {
  return (
    <Box
      component="th"
      sx={{
        py: 1.5,
        px: 2,
        textAlign: "left",
        borderBottom: `1px solid ${DIVIDER_COLOR}`,
        color: HEADER_COLOR,
        fontWeight: 500,
        fontSize: 11,
        textTransform: "uppercase",
        letterSpacing: "0.06em",
        whiteSpace: "nowrap",
      }}
    >
      {children}
    </Box>
  );
}

// ---------------------------------------------------------------------------
// Table data cell
// ---------------------------------------------------------------------------
function TD({ children, sx }: { children: React.ReactNode; sx?: object }) {
  return (
    <Box
      component="td"
      sx={{
        py: 0,
        px: 2,
        height: ROW_HEIGHT,
        borderBottom: `1px solid ${DIVIDER_COLOR}`,
        verticalAlign: "middle",
        ...sx,
      }}
    >
      {children}
    </Box>
  );
}

// ---------------------------------------------------------------------------
// Skeleton row
// ---------------------------------------------------------------------------
function SkeletonRow() {
  return (
    <Box component="tr">
      {[140, 40, 80, 80, 80, 40].map((w, i) => (
        <TD key={i}>
          <Skeleton variant="rectangular" width={w} height={14} sx={{ bgcolor: "#EBEBF3", borderRadius: 1 }} />
        </TD>
      ))}
    </Box>
  );
}

// ---------------------------------------------------------------------------
// Public component
// ---------------------------------------------------------------------------

interface FleetTableProps {
  rows?: FleetRow[];
  loading?: boolean;
  onOpenDatastream?: (id: string) => void;
}

export default function FleetTable({ rows, loading, onOpenDatastream }: FleetTableProps) {
  const clickable = typeof onOpenDatastream === "function";

  if (loading) {
    return (
      <Box
        component="table"
        data-testid="fleet-table-loading"
        sx={{ width: "100%", borderCollapse: "collapse" }}
      >
        <Box component="thead">
          <Box component="tr">
            <TH>Stream</TH>
            <TH>Auth</TH>
            <TH>7-Day Volume</TH>
            <TH>Last Extracted</TH>
            <TH>Next Run</TH>
            <TH>Issues</TH>
          </Box>
        </Box>
        <Box component="tbody">
          {[0, 1, 2].map((i) => <SkeletonRow key={i} />)}
        </Box>
      </Box>
    );
  }

  if (!rows || rows.length === 0) {
    return (
      <Box
        data-testid="fleet-table-empty"
        sx={{
          py: 6,
          textAlign: "center",
          color: "text.secondary",
          borderTop: `1px solid ${DIVIDER_COLOR}`,
        }}
      >
        <Typography variant="body2">
          No data streams configured for this project.
        </Typography>
      </Box>
    );
  }

  return (
    <Box
      component="table"
      data-testid="fleet-table"
      sx={{ width: "100%", borderCollapse: "collapse" }}
    >
      <Box component="thead">
        <Box component="tr">
          <TH>Stream</TH>
          <TH>Auth</TH>
          <TH>7-Day Volume</TH>
          <TH>Last Extracted</TH>
          <TH>Next Run</TH>
          <TH>Issues</TH>
        </Box>
      </Box>
      <Box component="tbody">
        {rows.map((row) => {
          const rowClickProps = clickable
            ? {
                onClick: () => onOpenDatastream!(row.id),
                onKeyDown: (e: React.KeyboardEvent) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    onOpenDatastream!(row.id);
                  }
                },
                tabIndex: 0,
                role: "button" as const,
                "aria-label": `Ouvrir le datastream ${row.name}`,
                sx: {
                  cursor: "pointer",
                  "&:hover": { bgcolor: "rgba(155,225,246,0.06)" },
                  "&:focus-visible": { outline: "2px solid #FF99C8", outlineOffset: -2 },
                },
              }
            : {};

          return (
            <Box
              component="tr"
              key={row.id}
              data-testid={`fleet-row-${row.id}`}
              {...rowClickProps}
            >
              {/* Flux: name + module */}
              <TD>
                <Box sx={{ display: "flex", flexDirection: "column", gap: 0.25 }}>
                  <Typography
                    variant="body2"
                    sx={{ fontWeight: 600, color: "text.primary", lineHeight: 1.2 }}
                  >
                    {row.name}
                  </Typography>
                  <Typography
                    variant="caption"
                    sx={{ color: "text.secondary", lineHeight: 1.2 }}
                  >
                    {row.module_name}
                  </Typography>
                </Box>
              </TD>

              {/* Autorisation dot */}
              <TD>
                <AuthDot status={row.auth_status} />
              </TD>

              {/* Volume sparkline */}
              <TD>
                <Sparkline values={row.volume_7d} />
              </TD>

              {/* Dernier extrait */}
              <TD>
                {row.last_extract ? (
                  <Box sx={{ display: "flex", alignItems: "center", gap: 1 }}>
                    <StatusDot status={row.last_extract.status} size={8} showTooltip />
                    <Typography
                      variant="caption"
                      sx={{
                        color: "text.secondary",
                        fontVariantNumeric: "lining-nums tabular-nums",
                      }}
                    >
                      {formatDate(row.last_extract.date)}
                    </Typography>
                  </Box>
                ) : (
                  <Typography variant="caption" sx={{ color: "text.disabled" }}>
                    Never Extracted
                  </Typography>
                )}
              </TD>

              {/* Prochain run */}
              <TD>
                <Typography
                  variant="caption"
                  sx={{ color: "text.secondary", whiteSpace: "nowrap" }}
                >
                  {row.next_run}
                </Typography>
              </TD>

              {/* Issues badge */}
              <TD sx={{ textAlign: "center" }}>
                <IssuesBadge count={row.issues_count} />
              </TD>
            </Box>
          );
        })}
      </Box>
    </Box>
  );
}
