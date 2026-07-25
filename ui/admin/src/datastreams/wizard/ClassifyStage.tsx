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
  if (state === "detected") return "Sensible : détecté";
  if (state === "masked") return "Sensible : masqué";
  return "Non sensible";
}

function confidenceLabel(confidence?: number | null): string {
  // Null = the server has not scored this binding yet (no fabricated percentage).
  if (confidence == null) return "à confirmer";
  return `${Math.round(confidence * 100)} %`;
}

export default function ClassifyStage({ fields, loading }: Props) {
  if (loading) {
    return (
      <Typography color="text.secondary" role="status" data-testid="classify-loading">
        Classification des champs en cours…
      </Typography>
    );
  }

  if (fields.length === 0) {
    return (
      <Typography color="text.secondary" data-testid="classify-empty">
        Aucun champ à classer pour l’instant. Complétez l’étape Configurer (source,
        rapport ou fichier) puis revenez ici.
      </Typography>
    );
  }

  return (
    <Stack spacing={2} data-testid="stage-classify">
      <Typography color="text.secondary" variant="body2">
        Rôles sémantiques suggérés, liaison MDM et confiance, état des données
        sensibles. Vérifiez chaque champ avant l’aperçu.
      </Typography>

      {/* M1 — honest disclosure: this classification is a LOCAL estimate. */}
      <Alert severity="info" data-testid="classify-local-estimate">
        Estimation locale : rôles, liaison MDM et détection des données sensibles
        sont une pré-estimation calculée dans le navigateur à partir du catalogue.
        La classification et la qualité serveur (Stories 12.3 / 12.4) seront
        appliquées à la validation puis à l’activation ; la confiance MDM reste
        « à confirmer » tant que le serveur ne l’a pas calculée.
      </Alert>

      <Box sx={{ overflowX: "auto" }}>
        <Table size="small" aria-label="Classification des champs">
          <TableHead>
            <TableRow>
              <TableCell scope="col">Champ</TableCell>
              <TableCell scope="col">Rôle sémantique</TableCell>
              <TableCell scope="col">Liaison MDM</TableCell>
              <TableCell scope="col">Confiance</TableCell>
              <TableCell scope="col">Données sensibles</TableCell>
              <TableCell scope="col">Échantillon</TableCell>
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
                      Non liée
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
