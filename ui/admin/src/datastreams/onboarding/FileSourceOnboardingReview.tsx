/**
 * Story 22.19 — File-source onboarding: mapping result and validation gate.
 *
 * The admin surface shown when a sample import is previewed. It renders the
 * per-field mapping with confidence (from the 22.17 recognizer + 12.3 profiling),
 * the matrix placement (22.14), and the validation gate status (22.15). The
 * operator can resolve a flagged field (pick a canonical target) and confirm,
 * which locks the template. Low confidence or a missing required field blocks the
 * lock — the Confirm control stays disabled with the offending fields surfaced.
 *
 * WCAG 2.2 AA: a semantic table with header scope; every state is textual (never
 * colour alone); the gate status is an aria-live region so a change after a
 * resolution is announced.
 */
import {
  Alert,
  Box,
  Button,
  Chip,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  Typography,
} from "@mui/material";

export type FieldStatus = "matched" | "ambiguous" | "unmatched" | "flagged";

export interface MappedField {
  source_column: string;
  canonical_target: string | null;
  confidence: number | null;
  status: FieldStatus;
}

export interface Placement {
  class: "planned" | "actual" | "extrapolated";
  metric: string;
  dimension: string[];
  period: string;
}

export interface FlaggedField {
  source_column?: string;
  canonical_target?: string;
  blocking_reason?: string | null;
}

export interface GateStatus {
  passed: boolean;
  missing_required: string[];
  flagged: FlaggedField[];
}

interface Props {
  fields: MappedField[];
  placement: Placement;
  gate: GateStatus;
  /** Canonical field ids the operator may pick to resolve a flagged/unmatched field. */
  canonicalOptions: string[];
  onResolveField?: (sourceColumn: string, canonicalId: string) => void;
  onConfirm?: () => void;
}

function confidenceLabel(confidence: number | null): string {
  // Null = not scored yet; never a fabricated percentage.
  if (confidence == null) return "to confirm";
  return `${Math.round(confidence * 100)}%`;
}

function statusLabel(status: FieldStatus): string {
  switch (status) {
    case "matched":
      return "Matched";
    case "ambiguous":
      return "Ambiguous — pick one";
    case "flagged":
      return "Flagged — needs a target";
    default:
      return "Unmatched (ignored)";
  }
}

function needsResolution(status: FieldStatus): boolean {
  return status === "ambiguous" || status === "flagged" || status === "unmatched";
}

export default function FileSourceOnboardingReview({
  fields,
  placement,
  gate,
  canonicalOptions,
  onResolveField,
  onConfirm,
}: Props) {
  const blockedCount = gate.missing_required.length + gate.flagged.length;

  return (
    <Stack spacing={2} data-testid="file-source-onboarding-review">
      <Typography variant="body2" color="text.secondary">
        Review how each source column maps to a canonical field, the placement, and
        the validation gate. Resolve any flagged field, then confirm to lock the
        template.
      </Typography>

      {/* Placement summary (matrix class + coordinates). */}
      <Box
        data-testid="placement-summary"
        sx={{ display: "flex", gap: 1, alignItems: "center", flexWrap: "wrap" }}
      >
        <Chip
          size="small"
          variant="outlined"
          label={`Class: ${placement.class}`}
          data-testid="placement-class"
        />
        <Typography variant="body2" color="text.secondary">
          Metric <strong>{placement.metric}</strong> · Period{" "}
          <strong>{placement.period}</strong> · Dimensions{" "}
          <strong>{placement.dimension.join(", ") || "—"}</strong>
        </Typography>
      </Box>

      {/* Per-field mapping with confidence. */}
      <Box sx={{ overflowX: "auto" }}>
        <Table size="small" aria-label="Column mapping">
          <TableHead>
            <TableRow>
              <TableCell scope="col">Source column</TableCell>
              <TableCell scope="col">Canonical field</TableCell>
              <TableCell scope="col">Confidence</TableCell>
              <TableCell scope="col">Status</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {fields.map((f) => (
              <TableRow key={f.source_column} data-testid={`map-row-${f.source_column}`}>
                <TableCell>
                  <Typography variant="body2" sx={{ fontWeight: 600 }}>
                    {f.source_column}
                  </Typography>
                </TableCell>
                <TableCell>
                  {needsResolution(f.status) ? (
                    <Box
                      component="select"
                      aria-label={`Canonical field for ${f.source_column}`}
                      data-testid={`resolve-${f.source_column}`}
                      value={f.canonical_target ?? ""}
                      onChange={(e: React.ChangeEvent<HTMLSelectElement>) =>
                        onResolveField?.(f.source_column, e.target.value)
                      }
                      sx={{ minWidth: 200, p: 0.75, borderRadius: 1 }}
                    >
                      <option value="">— select —</option>
                      {canonicalOptions.map((id) => (
                        <option key={id} value={id}>
                          {id}
                        </option>
                      ))}
                    </Box>
                  ) : f.canonical_target ? (
                    <Chip size="small" variant="outlined" label={f.canonical_target} />
                  ) : (
                    <Typography variant="body2" color="text.secondary">
                      Unmapped
                    </Typography>
                  )}
                </TableCell>
                <TableCell data-testid={`confidence-${f.source_column}`}>
                  {confidenceLabel(f.confidence)}
                </TableCell>
                <TableCell data-testid={`status-${f.source_column}`}>
                  <Typography variant="body2">{statusLabel(f.status)}</Typography>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </Box>

      {/* Validation gate status (aria-live so a resolution is announced). */}
      <Box role="status" aria-live="polite" data-testid="gate-status">
        {gate.passed ? (
          <Alert severity="success" data-testid="gate-passed">
            Validation gate passed — the required canonical fields were recovered
            and no mapping is low-confidence or ambiguous.
          </Alert>
        ) : (
          <Alert severity="warning" data-testid="gate-flagged">
            Validation gate: {blockedCount} field(s) need attention before the
            template can lock.
            {gate.missing_required.length > 0 && (
              <Box component="span" data-testid="gate-missing-required">
                {" "}
                Missing required: {gate.missing_required.join(", ")}.
              </Box>
            )}
            {gate.flagged.length > 0 && (
              <Box component="span" data-testid="gate-flagged-fields">
                {" "}
                Flagged:{" "}
                {gate.flagged
                  .map((f) => f.source_column ?? f.canonical_target ?? "field")
                  .join(", ")}
                .
              </Box>
            )}
          </Alert>
        )}
      </Box>

      <Box>
        <Button
          variant="contained"
          disabled={!gate.passed}
          onClick={() => onConfirm?.()}
          data-testid="confirm-lock"
        >
          Confirm and lock template
        </Button>
        {!gate.passed && (
          <Typography
            variant="caption"
            color="text.secondary"
            sx={{ display: "block", mt: 0.5 }}
            data-testid="confirm-blocked-hint"
          >
            Resolve the flagged fields and map every required field to enable lock.
          </Typography>
        )}
      </Box>
    </Stack>
  );
}
