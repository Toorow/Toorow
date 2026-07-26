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
 *   - History tab: full version timeline with restore (Story 44.8)
 *
 * v3 restyle: kept on MUI Drawer/Dialog (the portal/focus-trap behavior is load
 * bearing), English throughout, with all colors routed through the application.css
 * CSS variables so the surface is dark-safe.
 *
 * IMPORTANT -- Story 44.8 AC3 (permission-gated Restore) is DEFERRED, not
 * implemented: no org-permission/role model exists anywhere in the
 * dictionary API today (that belongs to epic 43's shared governed-command
 * work). Restore is therefore available to ANY authenticated operator here;
 * the Restore button is never disabled on a "no write rights" basis. Any
 * failure the server returns (validation error, not-found, db error, ...)
 * surfaces verbatim next to the History timeline (role="alert") instead.
 * Do NOT reintroduce a client-invented 403/"forbidden reason" state -- see
 * the epic file's Story 44.8 Acceptance Criteria for the ratified note.
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

/** Story 44.8 -- one row of GET /api/datamodel/fields/{name}/history. */
export type ChangeKind = "created" | "updated" | "approved" | "deleted" | "restored";

export interface FieldVersionSnapshot {
  display_name: string;
  measure: string | null;
  description: string | null;
  status: string;
}

export interface FieldVersionDiffEntry {
  before: unknown;
  after: unknown;
}

export interface FieldVersion {
  version_number: number;
  change_kind: ChangeKind;
  changed_by: string;
  changed_at: string;
  /**
   * Per-field {before, after}; may also carry a non-field `_restored_from`
   * key holding the plain int version_number the snapshot was restored from
   * (Story 44.8 finding #4: validated to an int server-side, never a raw
   * client-supplied shape).
   */
  diff: Record<string, FieldVersionDiffEntry | number> | null;
  snapshot: FieldVersionSnapshot;
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
// History: change_kind badge (Story 44.8) -- token-derived colors only.
// ---------------------------------------------------------------------------

const CHANGE_KIND_COLOR: Record<ChangeKind, string> = {
  created: "var(--success)",
  updated: "var(--muted)",
  approved: "var(--rose)",
  deleted: "var(--error)",
  restored: "var(--info)",
};

const CHANGE_KIND_LABEL: Record<ChangeKind, string> = {
  created: "Created",
  updated: "Updated",
  approved: "Approved",
  deleted: "Deleted",
  restored: "Restored",
};

function ChangeKindBadge({ kind }: { kind: ChangeKind }) {
  const color = CHANGE_KIND_COLOR[kind] ?? "var(--muted)";
  return (
    <Box
      data-testid="change-kind-badge"
      sx={{
        display: "inline-flex",
        alignItems: "center",
        gap: "6px",
        px: "8px",
        py: "2px",
        borderRadius: "999px",
        backgroundColor: `color-mix(in srgb, ${color} 14%, transparent)`,
      }}
    >
      <Box
        sx={{ width: 6, height: 6, borderRadius: "50%", backgroundColor: color, flexShrink: 0 }}
      />
      <Typography variant="caption" sx={{ color, fontWeight: 600 }}>
        {CHANGE_KIND_LABEL[kind] ?? kind}
      </Typography>
    </Box>
  );
}

function formatHistoryDate(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("en-GB", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

// ---------------------------------------------------------------------------
// History: one timeline row, with expandable diff and restore action.
// ---------------------------------------------------------------------------

function FieldVersionRow({
  version,
  isCurrent,
  fieldDeleted,
  restoring,
  onRestoreRequested,
}: {
  version: FieldVersion;
  isCurrent: boolean;
  fieldDeleted: boolean;
  restoring: boolean;
  onRestoreRequested: (version: FieldVersion) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const diffEntries = Object.entries(version.diff || {}).filter(
    ([key]) => key !== "_restored_from",
  ) as [string, FieldVersionDiffEntry][];

  return (
    <Box
      data-testid={`history-row-${version.version_number}`}
      sx={{
        py: "12px",
        px: "16px",
        borderBottom: "1px solid var(--line)",
        "&:last-of-type": { borderBottom: 0 },
      }}
    >
      <Box sx={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 1 }}>
        <Box sx={{ display: "flex", alignItems: "center", gap: "10px" }}>
          <ChangeKindBadge kind={version.change_kind} />
          <Typography variant="body2" sx={{ color: "var(--ink)", fontWeight: 500 }}>
            {version.changed_by}
          </Typography>
          <Typography
            variant="caption"
            className="number"
            sx={{ color: "var(--muted)" }}
          >
            {formatHistoryDate(version.changed_at)}
          </Typography>
        </Box>

        <Box sx={{ display: "flex", alignItems: "center", gap: "8px" }}>
          {diffEntries.length > 0 && (
            <Button
              size="small"
              onClick={() => setExpanded((v) => !v)}
              data-testid={`history-diff-toggle-${version.version_number}`}
              sx={{ textTransform: "none", color: "var(--info)", minWidth: 0, p: "2px 6px" }}
            >
              {expanded ? "Hide diff" : "Show diff"}
            </Button>
          )}
          {/*
            Story 44.8 finding #1 / AC3: Restore is available to ANY
            authenticated operator -- there is no permission model in the
            dictionary API today to gate this on write rights. AC3
            (permission-gated Restore, disabled with a reason) is DEFERRED
            to the org-role/governed-command work (epic 43); see the note
            under Story 44.8's Acceptance Criteria in the epic file. The
            button therefore stays enabled here (only `restoring` and the
            structural isCurrent/fieldDeleted guards affect it); any
            failure the server returns surfaces verbatim next to the
            timeline (role="alert"), not as a fabricated disabled state.
          */}
          {!isCurrent && !fieldDeleted && (
            <Button
              size="small"
              variant="outlined"
              disabled={restoring}
              onClick={() => onRestoreRequested(version)}
              data-testid={`restore-button-${version.version_number}`}
              sx={{ textTransform: "none", fontWeight: 500 }}
            >
              Restore this version
            </Button>
          )}
        </Box>
      </Box>

      {expanded && diffEntries.length > 0 && (
        <Box
          data-testid={`history-diff-${version.version_number}`}
          sx={{
            mt: "10px",
            p: "10px 12px",
            borderRadius: "6px",
            backgroundColor: "var(--surface-2)",
          }}
        >
          {diffEntries.map(([field, change]) => (
            <Box key={field} sx={{ display: "flex", gap: "8px", fontSize: "0.8125rem", mb: "4px" }}>
              <Typography variant="caption" sx={{ color: "var(--muted)", minWidth: 100 }}>
                {field}
              </Typography>
              <Typography variant="caption" sx={{ color: "var(--ink)" }}>
                {String(change.before ?? "—")} {"→"} {String(change.after ?? "—")}
              </Typography>
            </Box>
          ))}
        </Box>
      )}
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

  // Story 44.8: History tab
  const [activeTab, setActiveTab] = useState<"detail" | "history">("detail");
  const [history, setHistory] = useState<FieldVersion[] | null>(null);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [restoreCandidate, setRestoreCandidate] = useState<FieldVersion | null>(null);
  const [restoring, setRestoring] = useState(false);
  // Story 44.8 finding #1: AC3 (permission-gated Restore) is DEFERRED --
  // no org-permission model exists in this API today. Restore stays enabled
  // for any authenticated operator; any non-2xx response is surfaced here
  // verbatim, honestly, instead of a fabricated "forbidden" UI state.
  const [restoreError, setRestoreError] = useState<string | null>(null);

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

  // Reset History tab state whenever a different field is opened / the drawer closes.
  useEffect(() => {
    setActiveTab("detail");
    setHistory(null);
    setHistoryError(null);
    setRestoreCandidate(null);
    setRestoreError(null);
  }, [fieldName, open]);

  const fetchHistory = () => {
    if (!fieldName) return;
    setHistoryLoading(true);
    setHistoryError(null);
    fetch(`/api/datamodel/fields/${encodeURIComponent(fieldName)}/history`, {
      headers: { Authorization: `Bearer ${localStorage.getItem("api_token") || ""}` },
    })
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((data: { versions: FieldVersion[] }) => {
        setHistory(data.versions || []);
      })
      .catch((e) => setHistoryError(String(e)))
      .finally(() => setHistoryLoading(false));
  };

  useEffect(() => {
    if (activeTab === "history" && fieldName && open && history === null && !historyLoading) {
      fetchHistory();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTab, fieldName, open]);

  const handleRestoreRequested = (version: FieldVersion) => {
    setRestoreCandidate(version);
  };

  // Story 44.8 finding #2: GET /fields/{name} is the only response shape
  // that carries used_by/used_by_count/conflicts -- PATCH/approve return the
  // bare target_fields row. Refetching here (rather than trusting the
  // mutation's response body) is what keeps the Detail tab from crashing on
  // revisit after a restore or an approve.
  const refetchDetail = async () => {
    if (!fieldName) return;
    const resp = await fetch(`/api/datamodel/fields/${encodeURIComponent(fieldName)}`, {
      headers: { Authorization: `Bearer ${localStorage.getItem("api_token") || ""}` },
    });
    if (!resp.ok) {
      // 44.8 re-review: a failed refetch after a SUCCESSFUL mutation must
      // not silently leave stale pre-mutation content on screen.
      const body = await resp.json().catch(() => ({}));
      setActionError(
        (body as { message?: string }).message ||
          `Refresh failed (HTTP ${resp.status}) — the change was saved but the view is stale`,
      );
      return;
    }
    setDetail((await resp.json()) as FieldDetail);
  };

  const handleRestoreConfirmed = async () => {
    if (!fieldName || !restoreCandidate) return;
    const version = restoreCandidate;
    setRestoreCandidate(null);
    setRestoring(true);
    setRestoreError(null);
    try {
      const resp = await fetch(`/api/datamodel/fields/${encodeURIComponent(fieldName)}`, {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${localStorage.getItem("api_token") || ""}`,
        },
        body: JSON.stringify({
          display_name: version.snapshot.display_name,
          measure: version.snapshot.measure,
          description: version.snapshot.description,
          // Story 44.8 finding #4: the server accepts (and validates) only a
          // plain int -- send the version_number directly, not wrapped.
          restored_from: version.version_number,
        }),
      });
      if (!resp.ok) {
        // Story 44.8 finding #1: no permission model exists -- ANY non-2xx
        // (validation error, not-found, db error, whatever the server sends)
        // surfaces here verbatim. The Restore button is never disabled as a
        // result; the operator can simply retry.
        const body = await resp.json().catch(() => ({}));
        const message = body.message || `HTTP ${resp.status}`;
        setRestoreError(message);
        return;
      }
      await refetchDetail();
      await fetchHistory();
      onFieldChanged?.();
    } catch (e) {
      setRestoreError(String(e));
    } finally {
      setRestoring(false);
    }
  };

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
      // Story 44.8 finding #2 (same latent defect as restore): the approve
      // response is also the bare row -- refetch for used_by/conflicts.
      await refetchDetail();
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

      {/* Tab bar (Story 44.8) */}
      <Box
        sx={{
          display: "flex",
          gap: "4px",
          px: 3,
          pt: "10px",
          borderBottom: "1px solid var(--line)",
        }}
        data-testid="field-drawer-tabs"
      >
        {(["detail", "history"] as const).map((tab) => (
          <Button
            key={tab}
            size="small"
            onClick={() => setActiveTab(tab)}
            data-testid={`field-drawer-tab-${tab}`}
            sx={{
              textTransform: "none",
              borderRadius: 0,
              px: "10px",
              py: "8px",
              fontWeight: activeTab === tab ? 600 : 500,
              color: activeTab === tab ? "var(--ink)" : "var(--muted)",
              borderBottom: activeTab === tab ? "2px solid var(--rose)" : "2px solid transparent",
            }}
          >
            {tab === "detail" ? "Detail" : "History"}
          </Button>
        ))}
      </Box>

      {/* Restore confirmation dialog (Story 44.8) */}
      <Dialog
        open={!!restoreCandidate}
        onClose={() => setRestoreCandidate(null)}
        maxWidth="xs"
        fullWidth
        slotProps={{
          paper: { sx: { backgroundColor: "var(--surface)", color: "var(--ink)" } },
        }}
      >
        <DialogTitle sx={{ fontWeight: 600, fontSize: "15px" }}>
          Restore this version?
        </DialogTitle>
        <DialogContent>
          <Typography variant="body2" sx={{ color: "var(--muted)" }}>
            Version {restoreCandidate?.version_number} will be applied as a new
            update on top of the current field (display name
            &quot;{restoreCandidate?.snapshot.display_name}&quot;). Prior
            history rows are never rewritten.
          </Typography>
        </DialogContent>
        <DialogActions>
          <Button
            size="small"
            onClick={() => setRestoreCandidate(null)}
            sx={{ textTransform: "none", color: "var(--muted)" }}
          >
            Cancel
          </Button>
          <Button
            size="small"
            variant="contained"
            onClick={() => void handleRestoreConfirmed()}
            disabled={restoring}
            data-testid="restore-confirm-button"
            sx={{
              textTransform: "none",
              fontWeight: 600,
              backgroundColor: "var(--info)",
              "&:hover": { backgroundColor: "var(--info)", filter: "brightness(0.92)" },
            }}
          >
            {restoring ? "Restoring…" : "Restore"}
          </Button>
        </DialogActions>
      </Dialog>

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
        {activeTab === "detail" && loading && (
          <Box sx={{ display: "flex", justifyContent: "center", py: 4 }}>
            <CircularProgress size={28} sx={{ color: "var(--rose)" }} />
          </Box>
        )}
        {activeTab === "detail" && error && (
          <Alert severity="error" sx={{ mb: 2 }}>
            Could not load the field detail: {error}
          </Alert>
        )}
        {activeTab === "detail" && detail && !loading && (
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

        {/* History tab (Story 44.8) */}
        {activeTab === "history" && (
          <>
            {historyLoading && (
              <Box sx={{ display: "flex", justifyContent: "center", py: 4 }}>
                <CircularProgress size={28} sx={{ color: "var(--rose)" }} />
              </Box>
            )}
            {historyError && (
              <Alert severity="error" sx={{ mb: 2 }} onClose={() => setHistoryError(null)}>
                {historyError}
              </Alert>
            )}
            {/*
              Story 44.8 finding #1: honest reactive error surfacing -- any
              non-2xx PATCH response during a restore lands here verbatim,
              next to the timeline, role="alert" for assistive tech. Restore
              is never disabled as a consequence (see the comment on the
              Restore button in FieldVersionRow).
            */}
            {restoreError && (
              <Alert
                severity="error"
                role="alert"
                data-testid="restore-error"
                sx={{ mb: 2 }}
                onClose={() => setRestoreError(null)}
              >
                {restoreError}
              </Alert>
            )}
            {!historyLoading && history && (
              history.length === 0 ? (
                <Box sx={{ py: 3, textAlign: "center" }}>
                  <Typography variant="body2" sx={{ color: "var(--muted)" }}>
                    No history recorded for this field yet.
                  </Typography>
                </Box>
              ) : (
                <>
                  {history[0]?.snapshot.status === "deleted" && (
                    <Alert severity="warning" sx={{ mb: 2 }} data-testid="history-deleted-banner">
                      Deleted — recreate the field to revive its history.
                    </Alert>
                  )}
                  <Box
                    data-testid="history-timeline"
                    sx={{ border: "1px solid var(--line)", borderRadius: "8px", overflow: "hidden" }}
                  >
                    {history.map((version, idx) => (
                      <FieldVersionRow
                        key={version.version_number}
                        version={version}
                        isCurrent={idx === 0}
                        fieldDeleted={history[0]?.snapshot.status === "deleted"}
                        restoring={restoring}
                        onRestoreRequested={handleRestoreRequested}
                      />
                    ))}
                  </Box>
                </>
              )
            )}
          </>
        )}
      </Box>
    </Drawer>
  );
}
