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
import { connectionBlockingReason, isConnectionUsable } from "./wizardLogic";
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
  connectionsLoading: boolean;
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
  connectionsLoading,
  sheetsConnectionId,
  onSheetsConnectionChange,
}: Props) {
  const fileRef = useRef<HTMLInputElement | null>(null);
  const [fileName, setFileName] = useState<string>("");

  const needsDraft = !datastreamId;

  if (format === "google_sheets") {
    const googleConnections = connections.filter(isGoogleConnection);
    const usableGoogleConnections = googleConnections.filter(isConnectionUsable);
    const noGoogleConnection = googleConnections.length === 0;
    return (
      <Stack spacing={2} data-testid="sheets-config">
        <Typography color="text.secondary" variant="body2">
          Recurring read-only synchronization of a Google Sheet.
          Each run creates an immutable managed candidate after validation.
        </Typography>
        <TextField
          select
          label="Google connection (read sheet)"
          value={sheetsConnectionId ?? ""}
          onChange={(e) => onSheetsConnectionChange(e.target.value)}
          data-testid="sheets-connection"
          disabled={connectionsLoading || noGoogleConnection}
          fullWidth
          helperText={
            connectionsLoading
              ? "Loading project-authorized Google accounts..."
              : noGoogleConnection
                ? "No Google account is authorized for this project."
                : usableGoogleConnections.length === 0
                  ? "No healthy Google account is usable. Reconnect or repair one first."
                  : "Only an active Google account with verified healthy access can read the sheet."
          }
          sx={{ maxWidth: 520 }}
        >
          {googleConnections.map((connection) => {
            const id = connection.connection_ref_id ?? connection.id;
            const blocked = connectionBlockingReason(connection);
            return (
              <MenuItem key={id} value={id} disabled={blocked != null}>
                {connection.display_name ??
                  connection.account_label ??
                  connection.nango_connection_id ??
                  connection.provider ??
                  id}
                {blocked ? ` - unavailable: ${blocked}` : " - healthy"}
              </MenuItem>
            );
          })}
        </TextField>
        <TextField
          label="Spreadsheet ID (spreadsheet_id)"
          value={spreadsheetId ?? ""}
          onChange={(e) => onSpreadsheetChange(e.target.value)}
          data-testid="sheets-spreadsheet"
          fullWidth
          sx={{ maxWidth: 520 }}
        />
        <TextField
          label="Range (for example, Budget2026!A:F)"
          value={sheetRange ?? ""}
          onChange={(e) => onSheetRangeChange(e.target.value)}
          data-testid="sheets-range"
          fullWidth
          helperText="The range read during each synchronization."
          sx={{ maxWidth: 520 }}
        />
        {noGoogleConnection && (
          <Alert severity="warning" data-testid="sheets-no-connection">
            Recurring synchronization requires a Google connection. Until a
            Google account is connected, synchronization configuration remains
            unavailable: you can save the draft, but synchronization
            will not be scheduled.
          </Alert>
        )}
        {needsDraft && (
          <Alert severity="info" data-testid="sheets-needs-draft">
            Save a draft first: synchronization configuration
            is attached to the Datastream. The Save draft button is
            always available at the bottom of the wizard.
          </Alert>
        )}
      </Stack>
    );
  }

  // CSV / Excel upload preview.
  return (
    <Stack spacing={2} data-testid="file-config">
      <Typography color="text.secondary" variant="body2">
        Import a {format === "csv" ? "CSV" : "Excel"}. toorow creates a
        bounded preview <strong>without publishing anything</strong>; you then validate the
        candidate in the following stages.
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
          {previewBusy ? "Previewing..." : "Choose a file and preview"}
        </Button>
        {fileName && (
          <Typography variant="body2" sx={{ mt: 1 }}>
            File: {fileName}
          </Typography>
        )}
      </Box>

      {needsDraft && (
        <Alert severity="info" data-testid="file-needs-draft">
          Save a draft first: the import preview is attached to the
          Datastream. The Save draft button is available at any
          time at the bottom of the wizard.
        </Alert>
      )}

      {importPreview && (
        <>
          <Divider />
          <Stack spacing={0.5} data-testid="import-preview-summary">
            <Stack direction="row" spacing={1} sx={{ flexWrap: "wrap", gap: 1 }}>
              <Chip size="small" label={`Format: ${importPreview.format}`} />
              {importPreview.encoding && (
                <Chip size="small" label={`Encoding: ${importPreview.encoding}`} />
              )}
              {importPreview.delimiter && (
                <Chip size="small" label={`Delimiter: ${importPreview.delimiter}`} />
              )}
              {importPreview.sheet_name && (
                <Chip size="small" label={`Sheet: ${importPreview.sheet_name}`} />
              )}
            </Stack>
            <Typography variant="body2">
              <strong>Rows:</strong> {importPreview.row_count.toLocaleString("fr-FR")} ·{" "}
              <strong>Rejected:</strong> {importPreview.rejected_count.toLocaleString("fr-FR")}
            </Typography>
            <Typography variant="body2">
              <strong>Columns:</strong>{" "}
              {importPreview.columns.map((c) => c.name).join(", ")}
            </Typography>
            <Typography
              component="code"
              variant="caption"
              sx={{ fontFamily: "var(--font-mono, monospace)", overflowWrap: "anywhere" }}
            >
              content fingerprint ...{importPreview.content_hash.slice(-12)}
            </Typography>
          </Stack>
        </>
      )}
    </Stack>
  );
}
