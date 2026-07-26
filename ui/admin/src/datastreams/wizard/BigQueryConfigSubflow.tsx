/**
 * Story 12.13 — Configure sub-flow for the EXISTING BIGQUERY source path.
 *
 * PHASE B (honest degradation): Story 12.7 registers external BigQuery read-only
 * sources, but the create/registration REST route is not wired in admin_api.py
 * yet (no /external-bq/register route exists). Rather than fabricate a call, the
 * operator can still declare the table reference and Save draft (the versioned
 * intent carries source.kind="external_bq" with an external_read_only
 * destination), but live validation of the table is disabled with explanatory
 * copy. The writer_identity is a placeholder until registration binds the real
 * one (see buildExternalObject in wizardLogic).
 *
 * Ownership is stated plainly: you own the table; toorow reads it read-only.
 */
import { Alert, Stack, TextField, Typography } from "@mui/material";

interface Props {
  bigqueryTable: string | null;
  onTableChange: (table: string) => void;
}

export default function BigQueryConfigSubflow({ bigqueryTable, onTableChange }: Props) {
  return (
    <Stack spacing={2} data-testid="bigquery-config">
      <Typography color="text.secondary" variant="body2">
        Declare the BigQuery table to read. toorow never becomes its
        owner and never writes to it.
      </Typography>

      <TextField
        label="Table reference (project.dataset.table)"
        value={bigqueryTable ?? ""}
        onChange={(e) => onTableChange(e.target.value)}
        data-testid="bigquery-table"
        fullWidth
        placeholder="my-project.my_dataset.my_table"
        helperText="Fully qualified BigQuery format."
        sx={{ maxWidth: 520 }}
      />

      <Alert severity="info" data-testid="bigquery-phaseb">
        Live table validation (access, schema, retention) will be
        available when the external source is registered. You can already
        declare the reference and save a draft; classification and
        preview will use the declared schema once the
        external source is registered.
      </Alert>
    </Stack>
  );
}
