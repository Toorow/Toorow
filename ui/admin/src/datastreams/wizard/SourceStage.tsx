/**
 * Story 12.13 — Stage 1: Source (source-first, UX-DR11/12, AC2).
 *
 * The operator picks ONE source kind: Connecteur, BigQuery existant, or Flux
 * managé (CSV / Excel / Google Sheets). Ownership and write behaviour are
 * explained plainly per kind BEFORE any configuration, so the data impact is
 * never a surprise. Writer ownership is DERIVED from the kind (12.2 AC1), shown
 * as read-only fact. All paths converge on the same downstream contract.
 */
import {
  Alert,
  Box,
  Card,
  CardActionArea,
  CardContent,
  Chip,
  Stack,
  TextField,
  Typography,
} from "@mui/material";
import {
  type ManagedFeedFormat,
  type SourceKind,
  SOURCE_KIND_META,
  sourceKindMeta,
} from "./wizardTypes";

interface Props {
  name: string;
  onNameChange: (name: string) => void;
  sourceKind: SourceKind | null;
  onSourceKindChange: (kind: SourceKind) => void;
  managedFeedFormat: ManagedFeedFormat | null;
  onManagedFeedFormatChange: (format: ManagedFeedFormat) => void;
}

const MANAGED_FORMAT_LABELS: { value: ManagedFeedFormat; label: string; help: string }[] = [
  { value: "csv", label: "CSV", help: "Governed one-time file import (12.9)." },
  { value: "excel", label: "Excel", help: "Governed one-time file import (12.9)." },
  {
    value: "google_sheets",
    label: "Google Sheets",
    help: "Recurring read-only synchronization (12.10).",
  },
];

export default function SourceStage({
  name,
  onNameChange,
  sourceKind,
  onSourceKindChange,
  managedFeedFormat,
  onManagedFeedFormatChange,
}: Props) {
  return (
    <Stack spacing={3} data-testid="stage-source">
      <TextField
        label="Datastream name"
        value={name}
        onChange={(e) => onNameChange(e.target.value)}
        fullWidth
        required
        helperText="A clear name for finding this Datastream in the list."
        sx={{ maxWidth: 420 }}
      />

      <Box>
        <Typography component="h3" variant="subtitle1" sx={{ mb: 0.5 }}>
          Choose a source
        </Typography>
        <Typography color="text.secondary" variant="body2" sx={{ mb: 2 }}>
          The source determines who owns the destination and how data
          is written. The source is never modified.
        </Typography>

        <Box
          role="radiogroup"
          aria-label="Source type"
          sx={{
            display: "grid",
            gap: 2,
            gridTemplateColumns: { xs: "1fr", md: "repeat(3, 1fr)" },
          }}
        >
          {SOURCE_KIND_META.map((meta) => {
            const selected = sourceKind === meta.kind;
            return (
              <Card
                key={meta.kind}
                variant="outlined"
                sx={{
                  borderRadius: 3,
                  borderColor: selected ? "primary.main" : "divider",
                  borderWidth: selected ? 2 : 1,
                }}
              >
                <CardActionArea
                  role="radio"
                  aria-checked={selected}
                  data-testid={`source-kind-${meta.kind}`}
                  onClick={() => onSourceKindChange(meta.kind)}
                  sx={{ height: "100%", alignItems: "stretch" }}
                >
                  <CardContent>
                    <Stack spacing={1}>
                      <Stack direction="row" spacing={1} sx={{ alignItems: "center" }}>
                        <Typography variant="subtitle1" sx={{ fontWeight: 600 }}>
                          {meta.label}
                        </Typography>
                        {/* Non-color selection cue: an explicit "Selected" chip. */}
                        {selected && (
                          <Chip size="small" color="primary" label="Selected" />
                        )}
                      </Stack>
                      <Typography variant="body2" color="text.secondary">
                        <strong>Ownership:</strong> {meta.ownership}
                      </Typography>
                      <Typography variant="body2" color="text.secondary">
                        <strong>Write behavior:</strong> {meta.writeBehavior}
                      </Typography>
                    </Stack>
                  </CardContent>
                </CardActionArea>
              </Card>
            );
          })}
        </Box>
      </Box>

      {sourceKind === "managed_feed" && (
        <Box>
          <Typography component="h3" variant="subtitle1" sx={{ mb: 1 }}>
            Managed-feed format
          </Typography>
          <TextField
            select
            label="Format"
            value={managedFeedFormat ?? ""}
            onChange={(e) => onManagedFeedFormatChange(e.target.value as ManagedFeedFormat)}
            data-testid="managed-format"
            fullWidth
            slotProps={{ select: { native: true } }}
            sx={{ maxWidth: 420 }}
            helperText={
              managedFeedFormat
                ? MANAGED_FORMAT_LABELS.find((f) => f.value === managedFeedFormat)?.help
                : "CSV and Excel are one-time imports; Google Sheets is a recurring synchronization."
            }
          >
            <option value="" disabled>
              Choose a format...
            </option>
            {MANAGED_FORMAT_LABELS.map((f) => (
              <option key={f.value} value={f.value}>
                {f.label}
              </option>
            ))}
          </TextField>
        </Box>
      )}

      {sourceKind && (
        <Alert severity="info" data-testid="source-ownership-summary">
          Writer:{" "}
          <strong>
            {sourceKindMeta(sourceKind).writer === "toorow" ? "toorow" : "external (you)"}
          </strong>{" "}
          · Destination policy:{" "}
          <strong>
            {sourceKindMeta(sourceKind).destinationPolicy === "managed_raw"
              ? "toorow-managed dataset"
              : "external read-only"}
          </strong>
          . This ownership is derived from the source and cannot be changed.
        </Alert>
      )}
    </Stack>
  );
}
