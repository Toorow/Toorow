/**
 * VersionHistory — per-document version list (Story 11.4).
 *
 * Reads version_number from the current document returned by GET /api/context/topics/{id}
 * or GET /api/context/procedures/{id}. The 11.1 API returns the current
 * version_number in the document body; it does NOT expose a dedicated versions
 * list endpoint, so this component shows a numbered row for each version from 1
 * to version_number and marks the current one.
 *
 * NOTE (API gap): The 11.1 REST API does not expose a `GET .../versions` list
 * with per-version diffs. The versions table exists in the DB
 * (context_topics_versions, procedures_versions) but has no read route in 11.1.
 * This component shows the version count correctly; full diff/restore functionality
 * would require a new GET /api/context/topics/{id}/versions endpoint (11.5 or later).
 *
 * Copy: French.
 */

import { Box, Chip, List, ListItem, ListItemText, Typography } from "@mui/material";

interface VersionHistoryProps {
  currentVersion: number;
  createdBy: string;
  createdAt: string;
  updatedAt: string;
}

function formatDate(iso: string): string {
  try {
    return new Date(iso).toLocaleString("fr-FR", {
      day: "2-digit",
      month: "short",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

export default function VersionHistory({
  currentVersion,
  createdBy,
  createdAt,
  updatedAt,
}: VersionHistoryProps) {
  // Build a synthetic list: one entry per version.
  // Only version metadata we know: v1 = created_at, latest = updated_at.
  const versions = Array.from({ length: currentVersion }, (_, i) => {
    const vNum = i + 1;
    const isCurrent = vNum === currentVersion;
    const isFirst = vNum === 1;
    return {
      vNum,
      isCurrent,
      date: isFirst ? createdAt : isCurrent ? updatedAt : null,
      by: isFirst ? createdBy : isCurrent ? createdBy : null,
    };
  }).reverse(); // newest first

  return (
    <Box>
      <Typography
        variant="caption"
        sx={{
          fontWeight: 600,
          textTransform: "uppercase",
          letterSpacing: ".04em",
          fontSize: "10px",
          color: "text.secondary",
          display: "block",
          mb: 1,
        }}
      >
        Historique des versions
      </Typography>

      <List dense disablePadding>
        {versions.map((v) => (
          <ListItem
            key={v.vNum}
            disableGutters
            sx={{
              py: 0.5,
              borderBottom: "1px solid",
              borderColor: "divider",
              gap: 1,
            }}
          >
            <ListItemText
              primary={
                <Box sx={{ display: "flex", alignItems: "center", gap: 1 }}>
                  <Typography variant="body2" sx={{ fontWeight: v.isCurrent ? 700 : 400 }}>
                    v{v.vNum}
                  </Typography>
                  {v.isCurrent && (
                    <Chip
                      label="actuelle"
                      size="small"
                      variant="outlined"
                      sx={{ height: 18, fontSize: "10px" }}
                      data-testid="version-current-badge"
                    />
                  )}
                </Box>
              }
              secondary={
                v.date
                  ? `${formatDate(v.date)}${v.by ? ` · ${v.by}` : ""}`
                  : `Version ${v.vNum}`
              }
            />
          </ListItem>
        ))}
      </List>

      {currentVersion <= 1 && (
        <Typography variant="caption" sx={{ color: "text.disabled", display: "block", mt: 1 }}>
          Aucune modification enregistrée — version initiale uniquement.
        </Typography>
      )}
    </Box>
  );
}
