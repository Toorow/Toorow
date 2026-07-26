/**
 * Story 12.13 — Stage 2: Configure. Branches by source kind to the correct
 * sub-flow, all of which converge on the same downstream contract (AC2):
 *   - connector_pull -> ConnectorConfigSubflow (searchable fields, grain, cost)
 *   - external_bq    -> BigQueryConfigSubflow (Phase B: declare table)
 *   - managed_feed   -> ManagedFeedConfigSubflow (CSV/Excel preview | Sheets)
 */
import { Typography } from "@mui/material";
import BigQueryConfigSubflow from "./BigQueryConfigSubflow";
import ConnectorConfigSubflow from "./ConnectorConfigSubflow";
import ManagedFeedConfigSubflow from "./ManagedFeedConfigSubflow";
import type {
  ConnectionSummary,
  ImportPreview,
  ManagedFeedFormat,
  SourceCapabilities,
  SourceKind,
} from "./wizardTypes";

interface Props {
  sourceKind: SourceKind;
  managedFeedFormat: ManagedFeedFormat | null;
  datastreamId: string | null;

  // connector
  connections: ConnectionSummary[];
  connectionsLoading: boolean;
  connectionRefId: string | null;
  onConnectionChange: (id: string) => void;
  capabilities: SourceCapabilities | null;
  capabilitiesLoading: boolean;
  reportId: string | null;
  onReportChange: (id: string) => void;
  selectedMetrics: string[];
  selectedDimensions: string[];
  onToggleField: (fieldId: string, kind: "metric" | "dimension") => void;
  grain: string[];
  onGrainChange: (grain: string[]) => void;

  // bigquery
  bigqueryTable: string | null;
  onTableChange: (table: string) => void;

  // managed feed
  importPreview: ImportPreview | null;
  previewBusy: boolean;
  onUpload: (file: File) => void;
  spreadsheetId: string | null;
  sheetRange: string | null;
  onSpreadsheetChange: (id: string) => void;
  onSheetRangeChange: (range: string) => void;
  sheetsConnectionId: string | null;
  onSheetsConnectionChange: (id: string) => void;
}

export default function ConfigureStage(props: Props) {
  if (props.sourceKind === "connector_pull") {
    return (
      <ConnectorConfigSubflow
        connections={props.connections}
        connectionsLoading={props.connectionsLoading}
        connectionRefId={props.connectionRefId}
        onConnectionChange={props.onConnectionChange}
        capabilities={props.capabilities}
        capabilitiesLoading={props.capabilitiesLoading}
        reportId={props.reportId}
        onReportChange={props.onReportChange}
        selectedMetrics={props.selectedMetrics}
        selectedDimensions={props.selectedDimensions}
        onToggleField={props.onToggleField}
        grain={props.grain}
        onGrainChange={props.onGrainChange}
      />
    );
  }
  if (props.sourceKind === "external_bq") {
    return (
      <BigQueryConfigSubflow
        bigqueryTable={props.bigqueryTable}
        onTableChange={props.onTableChange}
      />
    );
  }
  if (props.sourceKind === "managed_feed") {
    if (!props.managedFeedFormat) {
      return (
        <Typography color="text.secondary" data-testid="configure-need-format">
          Choose a managed-feed format in Source first.
        </Typography>
      );
    }
    return (
      <ManagedFeedConfigSubflow
        format={props.managedFeedFormat}
        datastreamId={props.datastreamId}
        importPreview={props.importPreview}
        previewBusy={props.previewBusy}
        onUpload={props.onUpload}
        spreadsheetId={props.spreadsheetId}
        sheetRange={props.sheetRange}
        onSpreadsheetChange={props.onSpreadsheetChange}
        onSheetRangeChange={props.onSheetRangeChange}
        connections={props.connections}
        connectionsLoading={props.connectionsLoading}
        sheetsConnectionId={props.sheetsConnectionId}
        onSheetsConnectionChange={props.onSheetsConnectionChange}
      />
    );
  }
  return null;
}
