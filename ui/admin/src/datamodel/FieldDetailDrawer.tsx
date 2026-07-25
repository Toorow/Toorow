/**
 * FieldDetailDrawer — detail panel for a semantic-model field.
 *
 *   - Definition header (display_name, canonical id, type, measure)
 *   - Unified view: which datastreams feed this field, with per-stream source
 *     field, last extract date and verification verdict
 *   - Conflicts banner (CURRENCY_CONFLICT / MEASURE_NULL warnings)
 *   - Inline remap: change a mapping's target field via a select (PUT) with an
 *     optimistic update
 *   - Approval + delete actions for user-defined fields (Story 13.1)
 *
 * v3 restyle: kept on MUI Drawer/Dialog (the portal/focus-trap behavior is load
 * bearing), English throughout, with all colors routed through the application.css
 * CSS variables so the surface is dark-safe.
 */

import {
  Alert,
  Box,
  Button,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Divider,
  Drawer,
  FormControl,
  MenuItem,
  Select,
  SelectChangeEvent,
  Typography,
} from "@mui/material";
import { useEffect, useState } from "react";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface Conflict {
  code: string;
  message: string;
  affected_streams: string[];
}

export interface UsedByEntry {
  datastream_id: string;
  datastream_name: string;
  module_name: string;
  project_id: string;
  enabled: boolean;
  source_field: string;
  last_loaded_at: string | null;
  last_verdict: string | null;
}

export interface FieldDetail {
  name: string;
  display_name: string;
  data_type: string;
  field_kind: "metric" | "dimension";
  measure: string | null;
  description: string | null;
  created_by: string;
  is_default: boolean;
  created_at: string;
  used_by_count: number;
  used_by: UsedByEntry[];
  conflicts: Conflict[];
  /** Story 13.1 */
  status?: "draft" | "approved";
  approved_at?: string | null;
  approved_by?: string | null;
}

export interface AvailableField {
  name: string;
  display_name: string;
}

interface FieldDetailDrawerProps {
  fieldName: string | null;
  open: boolean;
  onClose: () => void;
  availableFields: AvailableField[];
  /** Called when a mapping remap succeeds so parent can refresh */
  onMappingChanged?: () => void;
  /** Called after approve or delete so parent refreshes the list */
  onFieldChanged?: () => void;
}

// ---------------------------------------------------------------------------
// Verdict badge
// ---------------------------------------------------------------------------

function VerdictBadge({ verdict }: { verdict: string | null }) {
  if (!verdict) {
    return (
      <Typography variant="caption" sx={{ color: "var(--muted)", opacity: 0.6 }}>
        —
      </Typography>
    );
  }
  const color =
    verdict === "ok"
      ? "var(--success)"
      : verdict === "partial"
        ? "var(--warning)"
        : "var(--error)";
  return (
    <Box
      sx={{
        display: "inline-flex",
        alignItems: "center",
        gap: "4px",
        px: "6px",
        py: "1px",
        borderRadius: "3px",
        backgroundColor: `color-mix(in srgb, ${color} 12%, transparent)`,
      }}
    >
      <Box
        sx={{
          width: 5,
          height: 5,
          borderRadius: "50%",
          backgroundColor: color,
          flexShrink: 0,
        }}
      />
      <Typography variant="caption" sx={{ color, fontWeight: 500 }}>
        {verdict}
      </Typography>
    </Box>
  );
}

// ---------------------------------------------------------------------------
// Inline remap row
// ---------------------------------------------------------------------------

function RemapRow({
  entry,
  availableFields,
  onRemap,
}: {
  entry: UsedByEntry;
  availableFields: AvailableField[];
  onRemap: (datastreamId: string, sourceField: string, targetField: string | null) => Promise<void>;
}) {
  const [remapping, setRemapping] = useState(false);
  const [optimisticTarget, setOptimisticTarget] = useState<string>(
    entry.source_field,
  );
  // Reset when entry changes
  useEffect(() => {
    setOptimisticTarget(entry.source_field);
  }, [entry.source_field]);

  const handleChange = async (e: SelectChangeEvent<string>) => {
    const newTarget = e.target.value === "__unmapped__" ? null : e.target.value;
    const prev = optimisticTarget;
    setOptimisticTarget(e.target.value);
    setRemapping(true);
    try {
      await onRemap(entry.datastream_id, entry.source_field, newTarget);
    } catch {
      setOptimisticTarget(prev); // revert on error
    } finally {
      setRemapping(false);
    }
  };

  return (
    <Box
      data-testid={`used-by-row-${entry.datastream_id}`}
      sx={{
        py: "12px",
        px: "16px",
        display: "grid",
        gridTemplateColumns: "1fr 140px auto",
        gap: "12px",
        alignItems: "center",
        borderBottom: "1px solid var(--line)",
        "&:last-of-type": { borderBottom: 0 },
      }}
    >
      {/* Stream info */}
      <Box>
        <Box sx={{ display: "flex", alignItems: "center", gap: "8px", mb: "2px" }}>
          <Box
            sx={{
              width: 6,
              height: 6,
              borderRadius: "50%",
              backgroundColor: entry.enabled
                ? "var(--success)"
                : "color-mix(in srgb, var(--muted) 60%, transparent)",
              flexShrink: 0,
            }}
          />
          <Typography
            variant="body2"
            sx={{ fontWeight: 500, color: "var(--ink)" }}
          >
            {entry.datastream_name}
          </Typography>
        </Box>
        <Typography variant="caption" sx={{ color: "var(--muted)", ml: "14px" }}>
          {entry.module_name} · source field:{" "}
          <Box
            component="code"
            sx={{
              fontFamily: '"JetBrains Mono", monospace',
              fontSize: "0.7rem",
              backgroundColor: "var(--surface-2)",
              px: "4px",
              py: "1px",
              borderRadius: "3px",
            }}
          >
            {entry.source_field}
          </Box>
        </Typography>
        <Box sx={{ display: "flex", alignItems: "center", gap: "6px", mt: "4px", ml: "14px" }}>
          <VerdictBadge verdict={entry.last_verdict} />
          {entry.last_loaded_at && (
            <Typography variant="caption" sx={{ color: "var(--muted)" }}>
              {new Date(entry.last_loaded_at).toLocaleDateString("en-GB", {
                day: "2-digit",
                month: "short",
                year: "numeric",
                hour: "2-digit",
                minute: "2-digit",
              })}
            </Typography>
          )}
        </Box>
      </Box>

      {/* Inline remap */}
      <FormControl size="small" sx={{ minWidth: 120 }}>
        <Select
          value={optimisticTarget || "__unmapped__"}
          onChange={handleChange}
          disabled={remapping}
          data-testid={`remap-select-${entry.datastream_id}`}
          sx={{
            fontSize: "0.8125rem",
            "& .MuiSelect-select": { py: "6px" },
          }}
        >
          <MenuItem value="__unmapped__">
            <Typography variant="caption" sx={{ color: "var(--muted)", fontStyle: "italic" }}>
              Unmapped
            </Typography>
          </MenuItem>
          <Divider />
          {availableFields.map((f) => (
            <MenuItem key={f.name} value={f.name}>
              <Box>
                <Typography variant="caption" sx={{ fontWeight: 500 }}>
                  {f.display_name}
                </Typography>
                <Typography
                  variant="caption"
                  sx={{ display: "block", color: "var(--muted)", fontFamily: "monospace", fontSize: "0.65rem" }}
                >
                  {f.name}
                </Typography>
              </Box>
            </MenuItem>
          ))}
        </Select>
      </FormControl>

      {remapping && <CircularProgress size={16} sx={{ color: "var(--rose)" }} />}
    </Box>
  );
}

// ---------------------------------------------------------------------------
// Main drawer
// ---------------------------------------------------------------------------

export default function FieldDetailDrawer({
  fieldName,
  open,
  onClose,
  availableFields,
  onMappingChanged,
  onFieldChanged,
}: FieldDetailDrawerProps) {
  const [detail, setDetail] = useState<FieldDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [approving, setApproving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  // Delete confirmation
  const [deleteConfirmOpen, setDeleteConfirmOpen] = useState(false);

  useEffect(() => {
    if (!fieldName || !open) return;
    setLoading(true);
    setError(null);
    fetch(`/api/datamodel/fields/${encodeURIComponent(fieldName)}`, {
      headers: { Authorization: `Bearer ${localStorage.getItem("api_token") || ""}` },
    })
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((data) => {
        setDetail(data as FieldDetail);
      })
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, [fieldName, open]);

  const handleApprove = async () => {
    if (!fieldName) return;
    setApproving(true);
    setActionError(null);
    try {
      const resp = await fetch(
        `/api/datamodel/fields/${encodeURIComponent(fieldName)}/approve`,
        {
          method: "POST",
          headers: { Authorization: `Bearer ${localStorage.getItem("api_token") || ""}` },
        },
      );
      if (!resp.ok) {
        const body = await resp.json().catch(() => ({}));
        throw new Error(body.message || `HTTP ${resp.status}`);
      }
      const updated = await resp.json();
      setDetail(updated as FieldDetail);
      onFieldChanged?.();
    } catch (e) {
      setActionError(String(e));
    } finally {
      setApproving(false);
    }
  };

  // Opens the confirmation dialog; does not delete directly.
  const handleDelete = () => {
    if (!fieldName) return;
    setDeleteConfirmOpen(true);
  };

  // Effective delete after confirmation.
  const handleDeleteConfirmed = async () => {
    if (!fieldName) return;
    setDeleteConfirmOpen(false);
    setDeleting(true);
    setActionError(null);
    try {
      const resp = await fetch(
        `/api/datamodel/fields/${encodeURIComponent(fieldName)}`,
        {
          method: "DELETE",
          headers: { Authorization: `Bearer ${localStorage.getItem("api_token") || ""}` },
        },
      );
      if (!resp.ok) {
        const body = await resp.json().catch(() => ({}));
        throw new Error(body.message || `HTTP ${resp.status}`);
      }
      onFieldChanged?.();
      onClose();
    } catch (e) {
      setActionError(String(e));
    } finally {
      setDeleting(false);
    }
  };

  const handleRemap = async (
    datastreamId: string,
    sourceField: string,
    targetField: string | null,
  ) => {
    const resp = await fetch("/api/datamodel/mappings", {
      method: "PUT",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${localStorage.getItem("api_token") || ""}`,
      },
      body: JSON.stringify({
        datastream_id: datastreamId,
        source_field: sourceField,
        target_field: targetField,
      }),
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      throw new Error(body.message || `HTTP ${resp.status}`);
    }
    // Refresh detail
    if (fieldName) {
      const refreshResp = await fetch(
        `/api/datamodel/fields/${encodeURIComponent(fieldName)}`,
        {
          headers: { Authorization: `Bearer ${localStorage.getItem("api_token") || ""}` },
        },
      );
      if (refreshResp.ok) {
        setDetail(await refreshResp.json());
      }
    }
    onMappingChanged?.();
  };

  return (
    <Drawer
      anchor="right"
      open={open}
      onClose={onClose}
      slotProps={{
        paper: {
          sx: {
            width: { xs: "100%", sm: 480 },
            p: 0,
            overflow: "hidden",
            display: "flex",
            flexDirection: "column",
            backgroundColor: "var(--surface)",
            color: "var(--ink)",
          },
        },
      }}
      data-testid="field-detail-drawer"
    >
      {/* Header */}
      <Box
        sx={{
          px: 3,
          py: "20px",
          borderBottom: "1px solid var(--line)",
          display: "flex",
          alignItems: "flex-start",
          justifyContent: "space-between",
          gap: 2,
        }}
      >
        <Box sx={{ flex: 1, minWidth: 0 }}>
          {detail ? (
            <>
              <Typography variant="h6" sx={{ fontWeight: 600, color: "var(--ink)", lineHeight: 1.3 }}>
                {detail.display_name}
              </Typography>
              <Box sx={{ display: "flex", alignItems: "center", gap: "8px", mt: "6px" }}>
                <Typography
                  component="code"
                  sx={{
                    fontFamily: '"JetBrains Mono", monospace',
                    fontSize: "0.75rem",
                    color: "var(--muted)",
                    backgroundColor: "var(--surface-2)",
                    px: "6px",
                    py: "2px",
                    borderRadius: "4px",
                  }}
                >
                  {detail.name}
                </Typography>
                <Typography variant="caption" sx={{ color: "var(--muted)" }}>
                  {detail.data_type}
                </Typography>
                {detail.measure && (
                  <Typography variant="caption" sx={{ color: "var(--muted)" }}>
                    · {detail.measure}
                  </Typography>
                )}
              </Box>
              {detail.description && (
                <Typography variant="caption" sx={{ color: "var(--muted)", mt: "8px", display: "block" }}>
                  {detail.description}
                </Typography>
              )}
            </>
          ) : (
            <Typography variant="h6" sx={{ color: "var(--ink)" }}>
              {fieldName || "Field"}
            </Typography>
          )}
        </Box>
        <Button
          size="small"
          onClick={onClose}
          sx={{ color: "var(--muted)", minWidth: 0, p: "4px 8px", flexShrink: 0 }}
        >
          Close
        </Button>
      </Box>

      {/* Delete confirmation dialog */}
      <Dialog
        open={deleteConfirmOpen}
        onClose={() => setDeleteConfirmOpen(false)}
        maxWidth="xs"
        fullWidth
        slotProps={{
          paper: { sx: { backgroundColor: "var(--surface)", color: "var(--ink)" } },
        }}
      >
        <DialogTitle sx={{ fontWeight: 600, fontSize: "15px" }}>
          Delete this field?
        </DialogTitle>
        <DialogContent>
          <Typography variant="body2" sx={{ color: "var(--muted)" }}>
            The field <strong>{detail?.display_name ?? fieldName}</strong> will be
            permanently removed from the dictionary. Existing mappings that
            reference it will be orphaned. This action cannot be undone.
          </Typography>
        </DialogContent>
        <DialogActions>
          <Button
            size="small"
            onClick={() => setDeleteConfirmOpen(false)}
            sx={{ textTransform: "none", color: "var(--muted)" }}
          >
            Cancel
          </Button>
          <Button
            size="small"
            variant="contained"
            color="error"
            onClick={() => void handleDeleteConfirmed()}
            disabled={deleting}
            data-testid="delete-confirm-button"
            sx={{ textTransform: "none", fontWeight: 600 }}
          >
            {deleting ? "Deleting…" : "Delete permanently"}
          </Button>
        </DialogActions>
      </Dialog>

      {/* Body */}
      <Box sx={{ flex: 1, overflow: "auto", px: 3, py: "24px" }}>
        {loading && (
          <Box sx={{ display: "flex", justifyContent: "center", py: 4 }}>
            <CircularProgress size={28} sx={{ color: "var(--rose)" }} />
          </Box>
        )}
        {error && (
          <Alert severity="error" sx={{ mb: 2 }}>
            Could not load the field detail: {error}
          </Alert>
        )}
        {detail && !loading && (
          <>
            {/* Action error banner */}
            {actionError && (
              <Alert severity="error" sx={{ mb: 2 }} onClose={() => setActionError(null)}>
                {actionError}
              </Alert>
            )}

            {/* Approval (Story 13.1) */}
            {!detail.is_default && (
              <Box
                data-testid="approval-section"
                sx={{
                  mb: "20px",
                  p: "12px 16px",
                  borderRadius: "8px",
                  backgroundColor:
                    detail.status === "draft"
                      ? "color-mix(in srgb, var(--warning) 8%, transparent)"
                      : "color-mix(in srgb, var(--success) 8%, transparent)",
                  border: `1px solid ${
                    detail.status === "draft"
                      ? "color-mix(in srgb, var(--warning) 26%, transparent)"
                      : "color-mix(in srgb, var(--success) 26%, transparent)"
                  }`,
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  gap: 2,
                }}
              >
                <Box>
                  <Typography
                    variant="caption"
                    sx={{
                      fontWeight: 600,
                      color: detail.status === "draft" ? "var(--warning)" : "var(--success)",
                      display: "block",
                    }}
                  >
                    {detail.status === "draft" ? "Pending approval" : "Approved"}
                  </Typography>
                  {detail.status === "draft" && (
                    <Typography variant="caption" sx={{ color: "var(--muted)" }}>
                      This field is a draft and does not feed reports.
                    </Typography>
                  )}
                  {detail.status === "approved" && detail.approved_by && (
                    <Typography variant="caption" sx={{ color: "var(--muted)" }}>
                      By {detail.approved_by}
                      {detail.approved_at &&
                        ` · ${new Date(detail.approved_at).toLocaleDateString("en-GB")}`}
                    </Typography>
                  )}
                </Box>
                <Box sx={{ display: "flex", gap: 1, flexShrink: 0 }}>
                  {detail.status === "draft" && (
                    <Button
                      size="small"
                      variant="contained"
                      onClick={handleApprove}
                      disabled={approving}
                      data-testid="approve-button"
                      sx={{
                        backgroundColor: "var(--success)",
                        color: "#fff",
                        fontWeight: 600,
                        "&:hover": { backgroundColor: "var(--success)", filter: "brightness(0.92)" },
                        "&:disabled": { backgroundColor: "color-mix(in srgb, var(--success) 40%, transparent)" },
                      }}
                    >
                      {approving ? "..." : "Approve"}
                    </Button>
                  )}
                  <Button
                    size="small"
                    variant="outlined"
                    color="error"
                    onClick={handleDelete}
                    disabled={deleting}
                    data-testid="delete-button"
                    sx={{ fontWeight: 500 }}
                  >
                    {deleting ? "..." : "Delete"}
                  </Button>
                </Box>
              </Box>
            )}

            {/* Conflicts banner */}
            {detail.conflicts.length > 0 && (
              <Box
                data-testid="conflict-banner"
                sx={{ mb: "24px" }}
              >
                {detail.conflicts.map((c, i) => (
                  <Alert
                    key={i}
                    severity="warning"
                    sx={{
                      mb: 1,
                      backgroundColor: "color-mix(in srgb, var(--warning) 8%, transparent)",
                      borderLeft: "3px solid var(--warning)",
                      borderRadius: "6px",
                      "& .MuiAlert-icon": { color: "var(--warning)" },
                    }}
                  >
                    <Typography variant="caption" sx={{ fontWeight: 600, display: "block", mb: "2px" }}>
                      {c.code === "CURRENCY_CONFLICT"
                        ? "Currency conflict"
                        : c.code === "MEASURE_NULL"
                          ? "Undefined aggregation"
                          : c.code}
                    </Typography>
                    <Typography variant="caption">{c.message}</Typography>
                  </Alert>
                ))}
              </Box>
            )}

            {/* Number hero: used_by_count */}
            <Box sx={{ mb: "24px" }}>
              <Typography
                variant="overline"
                sx={{ color: "var(--muted)", letterSpacing: "0.08em" }}
              >
                Unified view — flows feeding this field
              </Typography>
              <Box sx={{ display: "flex", alignItems: "baseline", gap: "8px", mt: "8px" }}>
                <Typography
                  variant="h2"
                  sx={{
                    fontWeight: 800,
                    color: "var(--ink)",
                    fontVariantNumeric: "lining-nums tabular-nums",
                    lineHeight: 1,
                  }}
                >
                  {detail.used_by_count}
                </Typography>
                <Typography variant="body2" sx={{ color: "var(--muted)" }}>
                  {detail.used_by_count <= 1 ? "source flow" : "source flows"}
                </Typography>
              </Box>
            </Box>

            <Divider sx={{ mb: "16px", borderColor: "var(--line)" }} />

            {/* Used-by list */}
            {detail.used_by.length === 0 ? (
              <Box sx={{ py: 3, textAlign: "center" }}>
                <Typography variant="body2" sx={{ color: "var(--muted)" }}>
                  No datastream feeds this field yet.
                </Typography>
                <Typography variant="caption" sx={{ color: "var(--muted)", opacity: 0.7, mt: "4px", display: "block" }}>
                  Configure a mapping in the datastream's Mapping tab.
                </Typography>
              </Box>
            ) : (
              <Box>
                <Typography
                  variant="overline"
                  sx={{ color: "var(--muted)", letterSpacing: "0.08em", display: "block", mb: "8px" }}
                >
                  Remap the source
                </Typography>
                <Box
                  sx={{
                    border: "1px solid var(--line)",
                    borderRadius: "8px",
                    overflow: "hidden",
                  }}
                >
                  {detail.used_by.map((entry) => (
                    <RemapRow
                      key={`${entry.datastream_id}-${entry.source_field}`}
                      entry={entry}
                      availableFields={availableFields}
                      onRemap={handleRemap}
                    />
                  ))}
                </Box>
              </Box>
            )}
          </>
        )}
      </Box>
    </Drawer>
  );
}
