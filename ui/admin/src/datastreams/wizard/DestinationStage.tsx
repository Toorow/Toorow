/**
 * Story 12.13 — Stage 3: Destination (AC2 — ownership + write behaviour).
 *
 * The destination policy is DERIVED from the source kind (managed_raw for
 * toorow-owned connector/managed-feed; external_read_only for existing
 * BigQuery). The stage states plainly who owns the destination and how it is
 * written, and lets the operator name the managed dataset (label only) where
 * toorow owns it. For external BigQuery the destination is the operator's own
 * table and is shown as read-only.
 */
import { Alert, Box, Stack, TextField, Typography } from "@mui/material";
import { type SourceKind, sourceKindMeta } from "./wizardTypes";

interface Props {
  sourceKind: SourceKind;
  destinationDataset: string;
  onDatasetChange: (dataset: string) => void;
  bigqueryTable: string | null;
}

export default function DestinationStage({
  sourceKind,
  destinationDataset,
  onDatasetChange,
  bigqueryTable,
}: Props) {
  const meta = sourceKindMeta(sourceKind);
  const toorowOwned = meta.destinationPolicy === "managed_raw";

  return (
    <Stack spacing={2} data-testid="stage-destination">
      <Alert severity="info" data-testid="destination-policy">
        <Typography variant="subtitle2">
          Destination policy:{" "}
          {toorowOwned ? "toorow-managed dataset (managed_raw)" : "external read-only (external_read_only)"}
        </Typography>
        <Typography variant="body2" sx={{ mt: 0.5 }}>
          <strong>Ownership:</strong> {meta.ownership}
        </Typography>
        <Typography variant="body2">
          <strong>Write behavior:</strong> {meta.writeBehavior}
        </Typography>
      </Alert>

      {toorowOwned ? (
        <TextField
          label="Managed dataset name"
          value={destinationDataset}
          onChange={(e) => onDatasetChange(e.target.value)}
          data-testid="destination-dataset"
          fullWidth
          helperText="A label for the toorow-managed destination where versioned candidates land."
          sx={{ maxWidth: 520 }}
        />
      ) : (
        <Box data-testid="destination-external">
          <Typography variant="body2">
            <strong>Destination table:</strong>{" "}
            {bigqueryTable || "declare it in Configure"}
          </Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
            The destination is your own read-only BigQuery table.
            toorow writes nothing.
          </Typography>
        </Box>
      )}
    </Stack>
  );
}
