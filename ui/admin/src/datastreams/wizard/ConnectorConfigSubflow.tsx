/**
 * Story 12.13 — Configure sub-flow for the CONNECTOR source path (AC3).
 *
 * Drives GET /api/source-capabilities (the governed 12.1 catalog, the single
 * scope/catalog source per 12.2 AC re-use). Provides:
 *   - a connection picker + report picker (both data, never hardcoded);
 *   - SEARCHABLE Metrics and Dimensions with descriptions;
 *   - selected summaries;
 *   - incompatibility explanations from the report's compatibility constraints;
 *   - the effective grain; quota / cost; supported cadence.
 *
 * Reuses the capability-catalog logic proven in PremierRapportCard (report /
 * grain / cost surfacing) rather than duplicating it wholesale — here it is
 * generalised to arbitrary metric/dimension selection instead of a single
 * recommended report.
 *
 * WCAG: searchable comboboxes are native TextFields with labelled results;
 * selection state is textual (chips + "Sélectionné" summaries), not colour.
 */
import { useMemo, useState } from "react";
import {
  Alert,
  Box,
  Checkbox,
  Chip,
  Divider,
  FormControlLabel,
  List,
  ListItem,
  MenuItem,
  Stack,
  TextField,
  Typography,
} from "@mui/material";
import type {
  CapabilityField,
  CapabilityReport,
  ConnectionSummary,
  SourceCapabilities,
} from "./wizardTypes";

interface Props {
  connections: ConnectionSummary[];
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
}

function connectionRef(c: ConnectionSummary): string {
  return c.connection_ref_id ?? c.id;
}

function fieldMatches(field: CapabilityField, query: string): boolean {
  if (!query) return true;
  const q = query.toLowerCase();
  return (
    field.field_id.toLowerCase().includes(q) ||
    field.source_field.toLowerCase().includes(q) ||
    field.description.toLowerCase().includes(q)
  );
}

/** Fields the active report exposes for a given kind, filtered by the search. */
function reportFields(
  report: CapabilityReport | undefined,
  allFields: CapabilityField[],
  kind: "metric" | "dimension",
  query: string,
): CapabilityField[] {
  if (!report) return [];
  const ids = kind === "metric" ? report.metrics : report.dimensions;
  const byId = new Map(allFields.map((f) => [f.field_id, f]));
  return ids
    .map((id) => byId.get(id))
    .filter((f): f is CapabilityField => Boolean(f))
    .filter((f) => fieldMatches(f, query));
}

export default function ConnectorConfigSubflow({
  connections,
  connectionRefId,
  onConnectionChange,
  capabilities,
  capabilitiesLoading,
  reportId,
  onReportChange,
  selectedMetrics,
  selectedDimensions,
  onToggleField,
  grain,
  onGrainChange,
}: Props) {
  const [metricQuery, setMetricQuery] = useState("");
  const [dimensionQuery, setDimensionQuery] = useState("");

  const activeReport = useMemo(
    () => capabilities?.reports.find((r) => r.id === reportId),
    [capabilities, reportId],
  );

  const metricFields = useMemo(
    () => reportFields(activeReport, capabilities?.fields ?? [], "metric", metricQuery),
    [activeReport, capabilities, metricQuery],
  );
  const dimensionFields = useMemo(
    () => reportFields(activeReport, capabilities?.fields ?? [], "dimension", dimensionQuery),
    [activeReport, capabilities, dimensionQuery],
  );

  // Incompatibility explanations: any report constraint touching a selected field.
  const selectedSet = useMemo(
    () => new Set([...selectedMetrics, ...selectedDimensions]),
    [selectedMetrics, selectedDimensions],
  );
  const activeIncompatibilities = useMemo(
    () =>
      (activeReport?.compatibility ?? []).filter((c) =>
        c.field_ids.some((id) => selectedSet.has(id)),
      ),
    [activeReport, selectedSet],
  );

  return (
    <Stack spacing={3} data-testid="connector-config">
      <TextField
        select
        label="Connexion"
        value={connectionRefId ?? ""}
        onChange={(e) => onConnectionChange(e.target.value)}
        data-testid="connector-connection"
        fullWidth
        helperText="Une connexion autorisée de ce projet."
        sx={{ maxWidth: 480 }}
      >
        {connections.length === 0 && (
          <MenuItem value="" disabled>
            Aucune connexion autorisée
          </MenuItem>
        )}
        {connections.map((c) => (
          <MenuItem key={connectionRef(c)} value={connectionRef(c)}>
            {c.display_name ?? c.provider ?? connectionRef(c)}
            {c.status ? ` · ${c.status}` : ""}
          </MenuItem>
        ))}
      </TextField>

      {capabilitiesLoading && (
        <Typography color="text.secondary" role="status">
          Chargement du catalogue de capacités…
        </Typography>
      )}

      {capabilities && (
        <>
          <TextField
            select
            label="Rapport"
            value={reportId ?? ""}
            onChange={(e) => onReportChange(e.target.value)}
            data-testid="connector-report"
            fullWidth
            helperText="Le rapport détermine les métriques, dimensions et grains disponibles."
            sx={{ maxWidth: 480 }}
          >
            {capabilities.reports.map((r) => (
              <MenuItem
                key={r.id}
                value={r.id}
                disabled={r.availability.status !== "selectable"}
              >
                {r.id}
                {r.availability.status !== "selectable"
                  ? ` · indisponible (${r.availability.reason_code ?? "?"})`
                  : ""}
              </MenuItem>
            ))}
          </TextField>

          {activeReport && (
            <>
              <Divider />

              {/* Searchable Metrics + Dimensions with descriptions (AC3). */}
              <Box
                sx={{
                  display: "grid",
                  gap: 3,
                  gridTemplateColumns: { xs: "1fr", md: "1fr 1fr" },
                }}
              >
                <Box data-testid="metrics-picker">
                  <Typography component="h3" variant="subtitle1" sx={{ mb: 1 }}>
                    Métriques
                  </Typography>
                  <TextField
                    label="Rechercher une métrique"
                    value={metricQuery}
                    onChange={(e) => setMetricQuery(e.target.value)}
                    size="small"
                    fullWidth
                    type="search"
                    slotProps={{ htmlInput: { "aria-label": "Rechercher une métrique" } }}
                    sx={{ mb: 1 }}
                  />
                  <List dense disablePadding sx={{ maxHeight: 260, overflowY: "auto" }}>
                    {metricFields.map((f) => (
                      <ListItem key={f.field_id} disableGutters sx={{ display: "block" }}>
                        <FormControlLabel
                          control={
                            <Checkbox
                              checked={selectedMetrics.includes(f.field_id)}
                              onChange={() => onToggleField(f.field_id, "metric")}
                              slotProps={{ input: { "aria-label": `Métrique ${f.field_id}` } }}
                            />
                          }
                          label={
                            <Box>
                              <Typography variant="body2" sx={{ fontWeight: 600 }}>
                                {f.field_id}
                              </Typography>
                              <Typography variant="caption" color="text.secondary">
                                {f.description}
                              </Typography>
                            </Box>
                          }
                        />
                      </ListItem>
                    ))}
                    {metricFields.length === 0 && (
                      <Typography variant="body2" color="text.secondary">
                        Aucune métrique ne correspond.
                      </Typography>
                    )}
                  </List>
                </Box>

                <Box data-testid="dimensions-picker">
                  <Typography component="h3" variant="subtitle1" sx={{ mb: 1 }}>
                    Dimensions
                  </Typography>
                  <TextField
                    label="Rechercher une dimension"
                    value={dimensionQuery}
                    onChange={(e) => setDimensionQuery(e.target.value)}
                    size="small"
                    fullWidth
                    type="search"
                    slotProps={{ htmlInput: { "aria-label": "Rechercher une dimension" } }}
                    sx={{ mb: 1 }}
                  />
                  <List dense disablePadding sx={{ maxHeight: 260, overflowY: "auto" }}>
                    {dimensionFields.map((f) => (
                      <ListItem key={f.field_id} disableGutters sx={{ display: "block" }}>
                        <FormControlLabel
                          control={
                            <Checkbox
                              checked={selectedDimensions.includes(f.field_id)}
                              onChange={() => onToggleField(f.field_id, "dimension")}
                              slotProps={{ input: { "aria-label": `Dimension ${f.field_id}` } }}
                            />
                          }
                          label={
                            <Box>
                              <Typography variant="body2" sx={{ fontWeight: 600 }}>
                                {f.field_id}
                              </Typography>
                              <Typography variant="caption" color="text.secondary">
                                {f.description}
                              </Typography>
                            </Box>
                          }
                        />
                      </ListItem>
                    ))}
                    {dimensionFields.length === 0 && (
                      <Typography variant="body2" color="text.secondary">
                        Aucune dimension ne correspond.
                      </Typography>
                    )}
                  </List>
                </Box>
              </Box>

              {/* Selected summaries. */}
              <Box data-testid="selection-summary">
                <Typography variant="subtitle2" sx={{ mb: 0.5 }}>
                  Sélection ({selectedMetrics.length} métrique(s),{" "}
                  {selectedDimensions.length} dimension(s))
                </Typography>
                <Stack direction="row" spacing={1} sx={{ flexWrap: "wrap", gap: 1 }}>
                  {[...selectedMetrics, ...selectedDimensions].map((id) => (
                    <Chip key={id} size="small" label={id} variant="outlined" />
                  ))}
                  {selectedSet.size === 0 && (
                    <Typography variant="body2" color="text.secondary">
                      Rien de sélectionné pour l’instant.
                    </Typography>
                  )}
                </Stack>
              </Box>

              {/* Incompatibility explanations (AC3). */}
              {activeIncompatibilities.length > 0 && (
                <Alert severity="warning" data-testid="incompatibility-explanations">
                  <Typography variant="subtitle2">
                    Combinaisons incompatibles
                  </Typography>
                  <List dense disablePadding>
                    {activeIncompatibilities.map((c) => (
                      <ListItem key={c.reason_code} disableGutters sx={{ display: "block" }}>
                        <Typography variant="body2">
                          <strong>{c.reason_code} :</strong> {c.description}
                        </Typography>
                      </ListItem>
                    ))}
                  </List>
                </Alert>
              )}

              {/* Effective grain + quota/cost + supported cadence (AC3). */}
              <TextField
                select
                label="Grain effectif"
                value={grain.join(",")}
                onChange={(e) =>
                  onGrainChange(e.target.value ? e.target.value.split(",") : [])
                }
                data-testid="connector-grain"
                fullWidth
                helperText="Uniquement des grains supportés par le rapport."
                sx={{ maxWidth: 480 }}
              >
                {activeReport.supported_grains.map((g) => (
                  <MenuItem key={g.join(",")} value={g.join(",")}>
                    {g.join(" · ") || "—"}
                  </MenuItem>
                ))}
              </TextField>

              <Stack spacing={0.5} data-testid="quota-cadence">
                <Typography variant="body2">
                  <strong>Coût / quota :</strong> {activeReport.quota_cost.read_points}{" "}
                  {activeReport.quota_cost.unit}
                </Typography>
                <Typography variant="body2">
                  <strong>Cadence supportée :</strong>{" "}
                  {activeReport.cadence.supported_modes.join(", ")} · intervalle minimum{" "}
                  {activeReport.cadence.minimum_interval_minutes} min
                </Typography>
              </Stack>
            </>
          )}
        </>
      )}
    </Stack>
  );
}
