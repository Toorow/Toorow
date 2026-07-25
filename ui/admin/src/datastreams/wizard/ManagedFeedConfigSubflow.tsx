/**
 * Story 12.13 — Configure sub-flow for the MANAGED FEED source path.
 *
 * Two shapes:
 *   - CSV / Excel: a bounded upload PREVIEW (no publish) via
 *     POST /api/datastreams/{id}/imports/preview (12.9). The preview surfaces
 *     format / encoding / delimiter / sheet / columns / row & rejected counts /
 *     a content hash — feeding the Classify & Preview stages. The preview
 *     endpoint is keyed on the draft datastream id, so the operator must Save
 *     draft first; until then the upload is disabled with honest copy.
 *   - Google Sheets: the recurring sync config (spreadsheet + range) via
 *     POST .../managed-feed/configure (12.10). Same "Save draft first" gate.
 *
 * WCAG: the file input is labelled; the async preview result is announced by the
 * parent stage's live region; every state is textual.
 */
import { useRef, useState } from "react";
import {
  Alert,
  Box,
  Button,
  Chip,
  Divider,
  MenuItem,
  Stack,
  TextField,
  Typography,
} from "@mui/material";
import type {
  ConnectionSummary,
  ImportPreview,
  ManagedFeedFormat,
} from "./wizardTypes";

interface Props {
  format: ManagedFeedFormat;
  /** The draft datastream id — null until Save draft has run. */
  datastreamId: string | null;
  importPreview: ImportPreview | null;
  previewBusy: boolean;
  onUpload: (file: File) => void;
  spreadsheetId: string | null;
  sheetRange: string | null;
  onSpreadsheetChange: (id: string) => void;
  onSheetRangeChange: (range: string) => void;
  /** Project connections — the Sheets sub-flow needs a Google one (12.10). */
  connections: ConnectionSummary[];
  sheetsConnectionId: string | null;
  onSheetsConnectionChange: (id: string) => void;
}

/** True when a connection's provider is a Google grant able to read Sheets. */
function isGoogleConnection(c: ConnectionSummary): boolean {
  const p = (c.provider ?? "").toLowerCase();
  return p.includes("google") || p.includes("sheets") || p.includes("gsheets");
}

export default function ManagedFeedConfigSubflow({
  format,
  datastreamId,
  importPreview,
  previewBusy,
  onUpload,
  spreadsheetId,
  sheetRange,
  onSpreadsheetChange,
  onSheetRangeChange,
  connections,
  sheetsConnectionId,
  onSheetsConnectionChange,
}: Props) {
  const fileRef = useRef<HTMLInputElement | null>(null);
  const [fileName, setFileName] = useState<string>("");

  const needsDraft = !datastreamId;

  if (format === "google_sheets") {
    const googleConnections = connections.filter(isGoogleConnection);
    const noGoogleConnection = googleConnections.length === 0;
    return (
      <Stack spacing={2} data-testid="sheets-config">
        <Typography color="text.secondary" variant="body2">
          Synchronisation récurrente en lecture seule d’une feuille Google Sheets.
          Chaque exécution crée un candidat managé immuable après validation.
        </Typography>
        <TextField
          select
          label="Connexion Google (lecture de la feuille)"
          value={sheetsConnectionId ?? ""}
          onChange={(e) => onSheetsConnectionChange(e.target.value)}
          data-testid="sheets-connection"
          disabled={noGoogleConnection}
          fullWidth
          helperText={
            noGoogleConnection
              ? "Aucune connexion Google disponible dans ce projet : connectez un compte Google avant de configurer la synchronisation."
              : "Le compte Google dont l’autorisation lira la feuille à chaque synchronisation."
          }
          sx={{ maxWidth: 520 }}
        >
          {googleConnections.map((c) => {
            const id = c.connection_ref_id ?? c.id;
            return (
              <MenuItem key={id} value={id}>
                {c.display_name ?? c.provider ?? id}
              </MenuItem>
            );
          })}
        </TextField>
        <TextField
          label="Identifiant du classeur (spreadsheet_id)"
          value={spreadsheetId ?? ""}
          onChange={(e) => onSpreadsheetChange(e.target.value)}
          data-testid="sheets-spreadsheet"
          fullWidth
          sx={{ maxWidth: 520 }}
        />
        <TextField
          label="Plage (ex. Budget2026!A:F)"
          value={sheetRange ?? ""}
          onChange={(e) => onSheetRangeChange(e.target.value)}
          data-testid="sheets-range"
          fullWidth
          helperText="La plage lue à chaque synchronisation."
          sx={{ maxWidth: 520 }}
        />
        {noGoogleConnection && (
          <Alert severity="warning" data-testid="sheets-no-connection">
            La synchronisation récurrente exige une connexion Google. Tant qu’aucun
            compte Google n’est connecté, la configuration de synchronisation reste
            indisponible : vous pouvez enregistrer le brouillon, mais la
            synchronisation ne sera pas programmée.
          </Alert>
        )}
        {needsDraft && (
          <Alert severity="info" data-testid="sheets-needs-draft">
            Enregistrez d’abord un brouillon : la configuration de synchronisation
            est rattachée au flux. Le bouton « Enregistrer le brouillon » est
            disponible à tout moment en bas de l’assistant.
          </Alert>
        )}
      </Stack>
    );
  }

  // CSV / Excel upload preview.
  return (
    <Stack spacing={2} data-testid="file-config">
      <Typography color="text.secondary" variant="body2">
        Importez un fichier {format === "csv" ? "CSV" : "Excel"}. toorow en fait un
        aperçu borné <strong>sans rien publier</strong> ; vous validez ensuite le
        candidat aux étapes suivantes.
      </Typography>

      <Box>
        <input
          ref={fileRef}
          type="file"
          accept={format === "csv" ? ".csv,text/csv" : ".xlsx,.xls"}
          data-testid="file-input"
          aria-label={`Fichier ${format === "csv" ? "CSV" : "Excel"}`}
          style={{ display: "none" }}
          onChange={(e) => {
            const file = e.target.files?.[0];
            if (file) {
              setFileName(file.name);
              onUpload(file);
            }
          }}
        />
        <Button
          variant="outlined"
          onClick={() => fileRef.current?.click()}
          disabled={needsDraft || previewBusy}
          data-testid="file-choose"
        >
          {previewBusy ? "Aperçu en cours…" : "Choisir un fichier et prévisualiser"}
        </Button>
        {fileName && (
          <Typography variant="body2" sx={{ mt: 1 }}>
            Fichier : {fileName}
          </Typography>
        )}
      </Box>

      {needsDraft && (
        <Alert severity="info" data-testid="file-needs-draft">
          Enregistrez d’abord un brouillon : l’aperçu d’import est rattaché au
          flux. Le bouton « Enregistrer le brouillon » est disponible à tout
          moment en bas de l’assistant.
        </Alert>
      )}

      {importPreview && (
        <>
          <Divider />
          <Stack spacing={0.5} data-testid="import-preview-summary">
            <Stack direction="row" spacing={1} sx={{ flexWrap: "wrap", gap: 1 }}>
              <Chip size="small" label={`Format : ${importPreview.format}`} />
              {importPreview.encoding && (
                <Chip size="small" label={`Encodage : ${importPreview.encoding}`} />
              )}
              {importPreview.delimiter && (
                <Chip size="small" label={`Délimiteur : ${importPreview.delimiter}`} />
              )}
              {importPreview.sheet_name && (
                <Chip size="small" label={`Feuille : ${importPreview.sheet_name}`} />
              )}
            </Stack>
            <Typography variant="body2">
              <strong>Lignes :</strong> {importPreview.row_count.toLocaleString("fr-FR")} ·{" "}
              <strong>Rejets :</strong> {importPreview.rejected_count.toLocaleString("fr-FR")}
            </Typography>
            <Typography variant="body2">
              <strong>Colonnes :</strong>{" "}
              {importPreview.columns.map((c) => c.name).join(", ")}
            </Typography>
            <Typography
              component="code"
              variant="caption"
              sx={{ fontFamily: "var(--font-mono, monospace)", overflowWrap: "anywhere" }}
            >
              empreinte contenu …{importPreview.content_hash.slice(-12)}
            </Typography>
          </Stack>
        </>
      )}
    </Stack>
  );
}
