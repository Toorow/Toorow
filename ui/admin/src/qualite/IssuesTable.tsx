/**
 * IssuesTable — DQ issues list with acknowledge action (Story 8.6).
 *
 * Design rules (Epic 8 Part B):
 *  Rule 2: #EBEBF3-derived hairline dividers only.
 *  Rule 3: Dates/counts as hero with tabular-nums.
 *  Rule 4: No default-MUI table density or chip colors.
 *  Rule 5: 52px row height, 24px horizontal padding.
 */
import {
  Box,
  Checkbox,
  CircularProgress,
  Tooltip,
  Typography,
} from "@mui/material";
import { DQIssue } from "./types";

// ---------------------------------------------------------------------------
// Severity tint palette
// ---------------------------------------------------------------------------

const SEV_TINT: Record<string, string> = {
  warning: "rgba(245,124,0,0.08)",
  error: "rgba(229,57,53,0.10)",
};

const SEV_DOT: Record<string, string> = {
  warning: "#F57C00",
  error: "#E53935",
};

const SEV_LABEL: Record<string, string> = {
  warning: "Warning",
  error: "Error",
};

const MONITOR_LABEL: Record<string, string> = {
  dq_volume: "Volume",
  dq_timeliness: "Timeliness",
  dq_duplication: "Duplicates",
  dq_schema: "Schema",
};

// ---------------------------------------------------------------------------
// Column header
// ---------------------------------------------------------------------------

function ColHead({ children }: { children: React.ReactNode }) {
  return (
    <Typography
      variant="caption"
      component="th"
      sx={{
        fontWeight: 600,
        color: "text.secondary",
        textTransform: "uppercase",
        letterSpacing: "0.06em",
        px: 3,
        py: 1.5,
        textAlign: "left",
        borderBottom: "1px solid rgba(235,235,243,0.12)",
        whiteSpace: "nowrap",
      }}
    >
      {children}
    </Typography>
  );
}

// ---------------------------------------------------------------------------
// IssuesTable
// ---------------------------------------------------------------------------

interface IssuesTableProps {
  issues: DQIssue[];
  loading: boolean;
  onAcknowledge: (firingId: string) => void;
  acknowledging: Set<string>;
}

export default function IssuesTable({
  issues,
  loading,
  onAcknowledge,
  acknowledging,
}: IssuesTableProps) {
  if (loading) {
    return (
      <Box
        sx={{ display: "flex", justifyContent: "center", py: 6 }}
        aria-label="Loading issues"
      >
        <CircularProgress size={32} sx={{ color: "#FF99C8" }} />
      </Box>
    );
  }

  if (issues.length === 0) {
    return (
      <Box
        sx={{
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          py: 8,
          gap: 1.5,
          color: "text.secondary",
        }}
      >
        {/* Shield-check icon */}
        <svg
          width="40"
          height="40"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth={1.5}
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
          style={{ opacity: 0.25 }}
        >
          <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
          <polyline points="9 12 11 14 15 10" />
        </svg>
        <Typography variant="body2" sx={{ fontWeight: 600, color: "text.primary" }}>
          No issues detected
        </Typography>
        <Typography variant="caption">
          All of your monitors are green.
        </Typography>
      </Box>
    );
  }

  return (
    <Box
      component="table"
      sx={{
        width: "100%",
        borderCollapse: "collapse",
        "& tr": {
          transition: "background 0.15s",
        },
      }}
    >
      <thead>
        <tr>
          <ColHead>Monitor</ColHead>
          <ColHead>Stream</ColHead>
          <ColHead>Date</ColHead>
          <ColHead>Severity</ColHead>
          <ColHead>Acknowledged</ColHead>
        </tr>
      </thead>
      <tbody>
        {issues.map((issue) => {
          const isAcking = acknowledging.has(issue.id);
          const isAcked = issue.acknowledged || isAcking;
          const rowBg = SEV_TINT[issue.severity] ?? "transparent";

          return (
            <Box
              component="tr"
              key={issue.id}
              sx={{
                height: 52,
                background: isAcked ? "transparent" : rowBg,
                opacity: isAcked ? 0.55 : 1,
                "&:hover": {
                  background: isAcked
                    ? "rgba(235,235,243,0.04)"
                    : `${rowBg}`,
                },
                borderBottom: "1px solid rgba(235,235,243,0.08)",
              }}
            >
              {/* Monitor type */}
              <td style={{ padding: "0 24px" }}>
                <Box sx={{ display: "flex", alignItems: "center", gap: 1 }}>
                  <span
                    style={{
                      width: 7,
                      height: 7,
                      borderRadius: "50%",
                      background: SEV_DOT[issue.severity] ?? "#6B6A74",
                      flexShrink: 0,
                    }}
                  />
                  <Typography variant="body2" sx={{ fontWeight: 500 }}>
                    {MONITOR_LABEL[issue.type] ?? issue.label}
                  </Typography>
                </Box>
              </td>

              {/* Stream name */}
              <td style={{ padding: "0 24px" }}>
                <Tooltip
                  title={issue.message}
                  placement="top"
                  arrow
                >
                  <Typography
                    variant="body2"
                    sx={{
                      maxWidth: 220,
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                      color: "text.secondary",
                      cursor: "default",
                    }}
                  >
                    {issue.datastream_name || issue.datastream_id || "—"}
                  </Typography>
                </Tooltip>
              </td>

              {/* Date */}
              <td style={{ padding: "0 24px" }}>
                <Typography
                  variant="body2"
                  sx={{
                    fontVariantNumeric: "lining-nums tabular-nums",
                    color: "text.secondary",
                  }}
                >
                  {issue.window_date || "—"}
                </Typography>
              </td>

              {/* Severity badge */}
              <td style={{ padding: "0 24px" }}>
                <Box
                  sx={{
                    display: "inline-flex",
                    alignItems: "center",
                    gap: 0.5,
                    px: 1,
                    py: 0.25,
                    borderRadius: 1,
                    background:
                      issue.severity === "warning"
                        ? "rgba(245,124,0,0.12)"
                        : "rgba(229,57,53,0.12)",
                  }}
                >
                  <Typography
                    variant="caption"
                    sx={{
                      color:
                        issue.severity === "warning" ? "#F57C00" : "#E53935",
                      fontWeight: 600,
                    }}
                  >
                    {SEV_LABEL[issue.severity] ?? issue.severity}
                  </Typography>
                </Box>
              </td>

              {/* Acknowledge checkbox */}
              <td style={{ padding: "0 24px" }}>
                {isAcking ? (
                  <CircularProgress size={16} sx={{ color: "#FF99C8" }} />
                ) : (
                  <Tooltip
                    title={
                      isAcked
                        ? `Acknowledged ${issue.acknowledged_at ? new Date(issue.acknowledged_at).toLocaleString() : ""}`
                        : "Mark as acknowledged"
                    }
                    placement="top"
                    arrow
                  >
                    <Checkbox
                      checked={isAcked}
                      disabled={isAcked}
                      onChange={() => {
                        if (!isAcked) onAcknowledge(issue.id);
                      }}
                      size="small"
                      sx={{
                        color: "rgba(235,235,243,0.30)",
                        "&.Mui-checked": { color: "#FF99C8" },
                        padding: "4px",
                      }}
                      slotProps={{ input: { "aria-label": `Acknowledge ${issue.type}` } }}
                    />
                  </Tooltip>
                )}
              </td>
            </Box>
          );
        })}
      </tbody>
    </Box>
  );
}
