/**
 * FlowSummary — horizontal band showing the full data flow chain for a datastream.
 *
 * Source → Autorisation → Profil → Planification → Destination
 *
 * Refinement R1: "make the whole flow legible per the health of each node."
 * Rule 5: generous spacing — each node is a card.
 * Rule 2: hairlines only for connectors (#EBEBF3).
 */
import { Box, Typography } from "@mui/material";
import { Datastream } from "./types";

interface FlowNode {
  label: string;
  value: string | null;
  statusDot?: string; // CSS color or null
}

interface FlowSummaryProps {
  datastream: Datastream;
  connectionStatus?: string | null;
}

function FlowNodeCard({ node, isLast }: { node: FlowNode; isLast: boolean }) {
  return (
    <Box sx={{ display: "flex", alignItems: "center", gap: 0 }}>
      <Box
        sx={{
          px: 2,
          py: 1.5,
          borderRadius: 2,
          bgcolor: "background.paper",
          boxShadow: "0 1px 3px rgba(1,0,10,0.06)",
          minWidth: 100,
          maxWidth: 160,
        }}
      >
        <Typography
          variant="overline"
          sx={{ color: "text.secondary", display: "block", lineHeight: 1.2, mb: 0.5 }}
        >
          {node.label}
        </Typography>
        <Box sx={{ display: "flex", alignItems: "center", gap: 0.75 }}>
          {node.statusDot && (
            <Box
              component="span"
              sx={{
                width: 6,
                height: 6,
                borderRadius: "50%",
                bgcolor: node.statusDot,
                flexShrink: 0,
              }}
            />
          )}
          <Typography
            variant="body2"
            sx={{
              fontWeight: 500,
              color: "text.primary",
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
          >
            {node.value || "—"}
          </Typography>
        </Box>
      </Box>

      {!isLast && (
        <Box
          component="span"
          sx={{
            mx: 0.75,
            color: "text.secondary",
            fontSize: 14,
            userSelect: "none",
            opacity: 0.5,
          }}
        >
          →
        </Box>
      )}
    </Box>
  );
}

/** Map connection_status to a display color. */
function _connStatusColor(status: string | null | undefined): string {
  if (status === "active") return "#2E7D32";
  if (status === "populate_failed") return "#F57C00";
  if (status === "revoked" || status === "auth_expired") return "#E53935";
  return "#6B6A74";
}

/** French schedule mode label. */
function _scheduleLabel(mode: string, refetchDays: number): string {
  if (mode === "nightly") return `Nuit · J-${refetchDays}`;
  return "Manuel";
}

export default function FlowSummary({
  datastream,
  connectionStatus,
}: FlowSummaryProps) {
  const nodes: FlowNode[] = [
    {
      label: "Source",
      value: datastream.module_name,
      statusDot: undefined,
    },
    {
      label: "Autorisation",
      value: datastream.connection_ref_id
        ? `conn_…${datastream.connection_ref_id.slice(-6)}`
        : "Non liée",
      statusDot: _connStatusColor(connectionStatus),
    },
    {
      label: "Profil",
      value: datastream.report_profile_id || "par défaut",
    },
    {
      label: "Planification",
      value: _scheduleLabel(datastream.schedule_mode, datastream.refetch_days),
    },
    {
      label: "Destination",
      value: "Entrepôt",
    },
  ];

  return (
    <Box
      sx={{
        display: "flex",
        alignItems: "center",
        flexWrap: "wrap",
        gap: 0,
        px: 3,
        py: 2,
        bgcolor: "background.default",
        borderTop: "1px solid",
        borderBottom: "1px solid",
        borderColor: "divider",
        overflowX: "auto",
      }}
      role="region"
      aria-label="Résumé du flux"
    >
      {nodes.map((node, idx) => (
        <FlowNodeCard key={node.label} node={node} isLast={idx === nodes.length - 1} />
      ))}
    </Box>
  );
}
