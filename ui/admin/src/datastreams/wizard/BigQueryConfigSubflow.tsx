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
        Déclarez la table BigQuery à lire en lecture seule. toorow n’en devient
        jamais propriétaire et ne l’écrit jamais.
      </Typography>

      <TextField
        label="Référence de table (projet.dataset.table)"
        value={bigqueryTable ?? ""}
        onChange={(e) => onTableChange(e.target.value)}
        data-testid="bigquery-table"
        fullWidth
        placeholder="mon-projet.mon_dataset.ma_table"
        helperText="Format BigQuery entièrement qualifié."
        sx={{ maxWidth: 520 }}
      />

      <Alert severity="info" data-testid="bigquery-phaseb">
        La validation en direct de la table (accès, schéma, rétention) sera
        disponible à l’enregistrement de la source externe. Vous pouvez dès à
        présent déclarer la référence et enregistrer un brouillon ; la
        classification et l’aperçu s’appuieront sur le schéma déclaré une fois la
        source externe enregistrée.
      </Alert>
    </Stack>
  );
}
