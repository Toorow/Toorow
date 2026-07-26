/**
 * Story 12.13 — Stage 4: Classify & map (AC4, first half).
 *
 * Renders the per-field classification the 12.3 profiler produced: semantic
 * roles, MDM binding + confidence, and sensitive-data state. Samples are shown
 * per field so the operator sees the actual data being classified. This stage
 * is source-agnostic: the same ClassifiedField[] is produced from the connector
 * capability catalog, the CSV/Excel preview columns, or the external BigQuery
 * schema — all paths converge here (AC2).
 *
 * WCAG: the classification table is a semantic table with header scope; state
 * (sensitive / bound / unbound) is textual, never colour alone.
 */
import {
  Alert,
  Box,
  Chip,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  Typography,
} from "@mui/material";
import type { ClassifiedField } from "./wizardTypes";

interface Props {
  fields: ClassifiedField[];
  loading: boolean;
}

function sensitiveLabel(state?: string): string {
  if (state === "detected") return "Sensitive: detected";
  if (state === "masked") return "Sensitive: masked";
  return "Not sensitive";
}

function confidenceLabel(confidence?: number | null): string {
  // Null = the server has not scored this binding yet (no fabricated percentage).
  if (confidence == null) return "not yet confirmed";
  return `${Math.round(confidence * 100)} %`;
}

export default function ClassifyStage({ fields, loading }: Props) {
  if (loading) {
    return (
      <Typography color="text.secondary" role="status" data-testid="classify-loading">
        Classifying fields...
      </Typography>
    );
  }

  if (fields.length === 0) {
    return (
      <Typography color="text.secondary" data-testid="classify-empty">
        No fields are available to classify yet. Complete Configure (source,
        report, or file), then return here.
      </Typography>
    );
  }

  return (
    <Stack spacing={2} data-testid="stage-classify">
      <Typography color="text.secondary" variant="body2">
        Suggested semantic roles, MDM binding and confidence, and sensitive-data
        state. Review every field before preview.
      </Typography>

      {/* M1 — honest disclosure: this classification is a LOCAL estimate. */}
      <Alert severity="info" data-testid="classify-local-estimate">
        Local estimate: roles, MDM binding, and sensitive-data detection
        are browser-derived estimates from the catalog.
        Server classification and quality (Stories 12.3 / 12.4) will be
        applied during validation and activation; MDM confidence remains
        not yet confirmed until the server calculates it.
      </Alert>

      <Box sx={{ overflowX: "auto" }}>
        <Table size="small" aria-label="Field classification">
          <TableHead>
            <TableRow>
              <TableCell scope="col">Field</TableCell>
              <TableCell scope="col">Semantic role</TableCell>
              <TableCell scope="col">MDM binding</TableCell>
              <TableCell scope="col">Confidence</TableCell>
              <TableCell scope="col">Sensitive data</TableCell>
              <TableCell scope="col">Sample</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {fields.map((f) => (
              <TableRow key={f.field_id} data-testid={`classify-row-${f.field_id}`}>
                <TableCell>
                  <Typography variant="body2" sx={{ fontWeight: 600 }}>
                    {f.field_id}
                  </Typography>
                  {f.physical_type && (
                    <Typography variant="caption" color="text.secondary">
                      {f.physical_type}
                    </Typography>
                  )}
                </TableCell>
                <TableCell>{f.semantic_role}</TableCell>
                <TableCell>
                  {f.mdm_binding ? (
                    <Chip size="small" variant="outlined" label={f.mdm_binding} />
                  ) : (
                    <Typography variant="body2" color="text.secondary">
                      Not bound
                    </Typography>
                  )}
                </TableCell>
                <TableCell>{confidenceLabel(f.mdm_confidence)}</TableCell>
                <TableCell>
                  <Chip
                    size="small"
                    variant={f.sensitive_state === "none" || !f.sensitive_state ? "outlined" : "filled"}
                    color={f.sensitive_state === "detected" ? "warning" : "default"}
                    label={sensitiveLabel(f.sensitive_state)}
                  />
                </TableCell>
                <TableCell>
                  <Typography variant="caption" color="text.secondary">
                    {(f.sample_values ?? []).slice(0, 3).join(", ") || "—"}
                  </Typography>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </Box>
    </Stack>
  );
}
