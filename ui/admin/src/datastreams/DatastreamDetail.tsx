/**
 * DatastreamDetail — full detail panel for one datastream (Story 8.3).
 *
 * Layout:
 *   1. Header (name, module, statut, fenêtre, refetch_days) + back button
 *   2. Flow Summary band (Source → Auth → Profil → Planif → Destination)
 *   3. Tabs:
 *      - "Extraits"  : Extraction Calendar + re-fetch CTA (existing logic)
 *      - "Mapping"   : Source→target field mapping (GET/PUT /api/datamodel/mappings)
 *      - "Réglages"  : Schedule/window settings (PATCH /api/datastreams/{id})
 *
 * Polling: while any day is in optimistic 'running' state, polls ledger
 * every 10 s until stable.
 *
 * Premium design (Part B 6 rules):
 *  - Rule 1: accent (#FF99C8) on CTA + selected day border + tab indicator only.
 *  - Rule 2: #EBEBF3 hairlines; theme text.primary.
 *  - Rule 3: row_count and completeness% are numeric heroes (tabular-nums).
 *  - Rule 4: no default MUI Chip for status — StatusDot only.
 *  - Rule 5: px:3 card padding, section gap 32.
 *  - Rule 6: correct French with accents.
 */
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  CircularProgress,
  FormControl,
  IconButton,
  MenuItem,
  Select,
  Tab,
  Tabs,
  TextField,
  Typography,
} from "@mui/material";
import { useCallback, useEffect, useRef, useState } from "react";
import ExtractionCalendar from "./ExtractionCalendar";
import FlowSummary from "./FlowSummary";
import InboundDeliveryPanel from "./InboundDeliveryPanel";
import StatusDot from "./StatusDot";
import { Datastream, LedgerEntry, LedgerResponse, RefetchResponse } from "./types";

interface DatastreamDetailProps {
  datastream: Datastream;
  projectId: string;
  apiBase: string;
  apiToken: string;
  onBack: () => void;
  /** Optional: connection status from the list summary. */
  connectionStatus?: string | null;
}

const LEDGER_DAYS = 35;
const POLL_INTERVAL_MS = 10_000;

function _dateNDaysAgo(n: number): string {
  const d = new Date();
  d.setDate(d.getDate() - n);
  return d.toISOString().slice(0, 10);
}

// ---------------------------------------------------------------------------
// Mapping tab types
// ---------------------------------------------------------------------------

interface MappingEntry {
  datastream_id: string;
  source_field: string;
  target_field: string | null;
}

// ---------------------------------------------------------------------------
// Mapping tab
// ---------------------------------------------------------------------------

function MappingTab({
  datastreamId,
  projectId,
  apiBase,
  apiToken,
}: {
  datastreamId: string;
  projectId: string;
  apiBase: string;
  apiToken: string;
}) {
  const [mappings, setMappings] = useState<MappingEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState<string | null>(null); // source_field being saved
  const [saveError, setSaveError] = useState<string | null>(null);

  const fetchMappings = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const qs = `datastream_id=${encodeURIComponent(datastreamId)}&project_id=${encodeURIComponent(projectId)}`;
      const resp = await fetch(`${apiBase}/api/datamodel/mappings?${qs}`, {
        headers: { Authorization: `Bearer ${apiToken}` },
      });
      if (!resp.ok) {
        const body = await resp.json().catch(() => ({}));
        throw new Error(body.message || `Erreur ${resp.status}`);
      }
      // GET /api/datamodel/mappings returns {"mappings": [...]}.
      const body = await resp.json();
      const data: MappingEntry[] = Array.isArray(body) ? body : (body.mappings ?? []);
      setMappings(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [datastreamId, projectId, apiBase, apiToken]);

  useEffect(() => {
    fetchMappings();
  }, [fetchMappings]);

  const handleRemap = async (sourceField: string, targetField: string | null) => {
    setSaving(sourceField);
    setSaveError(null);
    const prev = mappings;
    // Optimistic update
    setMappings((m) =>
      m.map((e) =>
        e.source_field === sourceField ? { ...e, target_field: targetField } : e
      )
    );
    try {
      const resp = await fetch(`${apiBase}/api/datamodel/mappings`, {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${apiToken}`,
        },
        body: JSON.stringify({
          datastream_id: datastreamId,
          source_field: sourceField,
          target_field: targetField,
        }),
      });
      if (!resp.ok) {
        const body = await resp.json().catch(() => ({}));
        throw new Error(body.message || `Erreur ${resp.status}`);
      }
    } catch (err) {
      setMappings(prev); // revert
      setSaveError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(null);
    }
  };

  if (loading) {
    return (
      <Box sx={{ display: "flex", justifyContent: "center", py: 6 }}>
        <CircularProgress size={24} sx={{ color: "#FF99C8" }} />
      </Box>
    );
  }

  if (error) {
    return (
      <Box sx={{ py: 4, textAlign: "center" }}>
        <Typography variant="body2" color="error">{error}</Typography>
        <Button size="small" onClick={fetchMappings} sx={{ mt: 1, color: "text.secondary" }}>
          Réessayer
        </Button>
      </Box>
    );
  }

  return (
    <Box sx={{ px: 3, pt: 3 }}>
      {saveError && (
        <Alert severity="error" sx={{ mb: 2 }} onClose={() => setSaveError(null)}>
          {saveError}
        </Alert>
      )}

      <Typography variant="subtitle2" sx={{ mb: 1.5, color: "text.secondary", textTransform: "uppercase", letterSpacing: "0.05em", fontSize: 11, fontWeight: 600 }}>
        Mapping source → cible
      </Typography>

      {mappings.length === 0 ? (
        <Box sx={{ py: 6, textAlign: "center" }}>
          <Typography variant="body2" sx={{ color: "text.secondary" }}>
            Aucun mapping configuré pour ce datastream.
          </Typography>
          <Typography variant="caption" sx={{ color: "text.disabled", display: "block", mt: 1 }}>
            Les mappings sont créés automatiquement après la première extraction.
          </Typography>
        </Box>
      ) : (
        <Box
          sx={{
            border: "1px solid rgba(235,235,243,0.30)",
            borderRadius: 1.5,
            overflow: "hidden",
          }}
          data-testid="mapping-table"
        >
          {/* Header */}
          <Box
            sx={{
              display: "grid",
              gridTemplateColumns: "1fr 1fr auto",
              gap: 2,
              px: 2,
              py: 1,
              borderBottom: "1px solid rgba(235,235,243,0.30)",
              bgcolor: "rgba(235,235,243,0.08)",
            }}
          >
            <Typography variant="caption" sx={{ color: "text.secondary", fontWeight: 600, textTransform: "uppercase", fontSize: 10, letterSpacing: "0.06em" }}>
              Champ source
            </Typography>
            <Typography variant="caption" sx={{ color: "text.secondary", fontWeight: 600, textTransform: "uppercase", fontSize: 10, letterSpacing: "0.06em" }}>
              Champ cible
            </Typography>
            <Box />
          </Box>

          {/* Rows */}
          {mappings.map((m) => (
            <Box
              key={m.source_field}
              data-testid={`mapping-row-${m.source_field}`}
              sx={{
                display: "grid",
                gridTemplateColumns: "1fr 1fr auto",
                gap: 2,
                px: 2,
                py: 1.5,
                alignItems: "center",
                borderBottom: "1px solid rgba(235,235,243,0.20)",
                "&:last-of-type": { borderBottom: 0 },
                bgcolor: m.target_field ? "transparent" : "rgba(235,235,243,0.06)",
              }}
            >
              {/* Source field */}
              <Box
                component="code"
                sx={{
                  fontFamily: '"JetBrains Mono", monospace',
                  fontSize: "0.75rem",
                  color: "text.primary",
                  bgcolor: "rgba(235,235,243,0.5)",
                  px: "6px",
                  py: "2px",
                  borderRadius: "3px",
                  display: "inline-block",
                }}
              >
                {m.source_field}
              </Box>

              {/* Target field select */}
              <Box sx={{ display: "flex", alignItems: "center", gap: 1 }}>
                {!m.target_field && (
                  <Box
                    sx={{
                      width: 6,
                      height: 6,
                      borderRadius: "50%",
                      bgcolor: "rgba(107,106,116,0.4)",
                      flexShrink: 0,
                    }}
                  />
                )}
                <Typography
                  variant="caption"
                  sx={{
                    color: m.target_field ? "text.primary" : "text.disabled",
                    fontStyle: m.target_field ? "normal" : "italic",
                    fontFamily: m.target_field ? '"JetBrains Mono", monospace' : "inherit",
                    fontSize: "0.75rem",
                  }}
                >
                  {m.target_field ?? "Non attribué"}
                </Typography>
              </Box>

              {/* Remap action */}
              {saving === m.source_field ? (
                <CircularProgress size={14} sx={{ color: "#FF99C8" }} />
              ) : (
                <Button
                  size="small"
                  variant="text"
                  sx={{ color: "text.secondary", minWidth: 0, fontSize: "0.75rem", p: "2px 6px" }}
                  onClick={() => handleRemap(m.source_field, m.target_field ? null : m.source_field)}
                >
                  {m.target_field ? "Supprimer" : "Auto-mapper"}
                </Button>
              )}
            </Box>
          ))}
        </Box>
      )}
    </Box>
  );
}

// ---------------------------------------------------------------------------
// Réglages tab
// ---------------------------------------------------------------------------

function ReglagTab({
  datastream,
  projectId,
  apiBase,
  apiToken,
}: {
  datastream: Datastream;
  projectId: string;
  apiBase: string;
  apiToken: string;
}) {
  const [scheduleMode, setScheduleMode] = useState<string>(datastream.schedule_mode);
  const [refetchDays, setRefetchDays] = useState<string>(String(datastream.refetch_days));
  const [dateWindowDays, setDateWindowDays] = useState<string>(String(datastream.date_window_days));
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saveSuccess, setSaveSuccess] = useState(false);

  const handleSave = async () => {
    setSaving(true);
    setSaveError(null);
    setSaveSuccess(false);
    try {
      const resp = await fetch(`${apiBase}/api/datastreams/${datastream.id}`, {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${apiToken}`,
        },
        body: JSON.stringify({
          project_id: projectId,
          schedule_mode: scheduleMode,
          refetch_days: parseInt(refetchDays, 10),
          date_window_days: parseInt(dateWindowDays, 10),
        }),
      });
      if (!resp.ok) {
        const body = await resp.json().catch(() => ({}));
        throw new Error(body.message || `Erreur ${resp.status}`);
      }
      setSaveSuccess(true);
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  const isDirty =
    scheduleMode !== datastream.schedule_mode ||
    refetchDays !== String(datastream.refetch_days) ||
    dateWindowDays !== String(datastream.date_window_days);

  return (
    <Box sx={{ px: 3, pt: 3, maxWidth: 480 }}>
      <Typography variant="subtitle2" sx={{ mb: 2, color: "text.secondary", textTransform: "uppercase", letterSpacing: "0.05em", fontSize: 11, fontWeight: 600 }}>
        Planification &amp; fenêtre de données
      </Typography>

      {saveError && (
        <Alert severity="error" sx={{ mb: 2 }} onClose={() => setSaveError(null)}>
          {saveError}
        </Alert>
      )}
      {saveSuccess && (
        <Alert severity="success" sx={{ mb: 2 }} onClose={() => setSaveSuccess(false)}>
          Réglages enregistrés.
        </Alert>
      )}

      <Box sx={{ display: "flex", flexDirection: "column", gap: 2.5 }}>
        {/* Schedule mode */}
        <Box>
          <Typography variant="caption" sx={{ color: "text.secondary", fontWeight: 500, display: "block", mb: 0.5 }}>
            Mode de planification
          </Typography>
          <FormControl size="small" fullWidth>
            <Select
              value={scheduleMode}
              onChange={(e) => setScheduleMode(e.target.value)}
              sx={{ fontSize: "0.875rem" }}
              inputProps={{ "data-testid": "setting-schedule-mode" }}
            >
              <MenuItem value="nightly">Nuitée (automatique)</MenuItem>
              <MenuItem value="manual">Manuel</MenuItem>
            </Select>
          </FormControl>
        </Box>

        {/* Refetch days */}
        <Box>
          <Typography variant="caption" sx={{ color: "text.secondary", fontWeight: 500, display: "block", mb: 0.5 }}>
            Rattrapage (jours en arrière)
          </Typography>
          <TextField
            type="number"
            size="small"
            value={refetchDays}
            onChange={(e) => setRefetchDays(e.target.value)}
            slotProps={{ htmlInput: { min: 1, max: 90, "data-testid": "setting-refetch-days" } }}
            sx={{ maxWidth: 120 }}
          />
          <Typography variant="caption" sx={{ color: "text.disabled", display: "block", mt: 0.5 }}>
            Nombre de jours à re-vérifier lors de chaque extraction nuitée.
          </Typography>
        </Box>

        {/* Date window days */}
        <Box>
          <Typography variant="caption" sx={{ color: "text.secondary", fontWeight: 500, display: "block", mb: 0.5 }}>
            Fenêtre de données (jours)
          </Typography>
          <TextField
            type="number"
            size="small"
            value={dateWindowDays}
            onChange={(e) => setDateWindowDays(e.target.value)}
            slotProps={{ htmlInput: { min: 1, max: 730, "data-testid": "setting-date-window" } }}
            sx={{ maxWidth: 120 }}
          />
          <Typography variant="caption" sx={{ color: "text.disabled", display: "block", mt: 0.5 }}>
            Profondeur historique maximale lors d'un premier chargement.
          </Typography>
        </Box>

        <Box>
          <Button
            variant="contained"
            disabled={saving || !isDirty}
            onClick={handleSave}
            data-testid="save-settings-btn"
            sx={{
              bgcolor: "#FF99C8",
              color: "text.primary",
              fontWeight: 600,
              "&:hover": { bgcolor: "#F77FB4" },
              "&.Mui-disabled": { opacity: 0.5 },
            }}
          >
            {saving ? <CircularProgress size={14} sx={{ mr: 1, color: "text.primary" }} /> : null}
            Enregistrer
          </Button>
        </Box>
      </Box>
    </Box>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export default function DatastreamDetail({
  datastream,
  projectId,
  apiBase,
  apiToken,
  onBack,
  connectionStatus,
}: DatastreamDetailProps) {
  const [ledger, setLedger] = useState<LedgerEntry[]>([]);
  const [loadingLedger, setLoadingLedger] = useState(true);
  const [ledgerError, setLedgerError] = useState<string | null>(null);
  const [selectedDates, setSelectedDates] = useState<Set<string>>(new Set());
  const [refetching, setRefetching] = useState(false);
  const [refetchError, setRefetchError] = useState<string | null>(null);

  // Tab state (0=Extracts, 1=Mapping, 2=Settings, 3=Delivery — managed_feed only)
  const [activeTab, setActiveTab] = useState<0 | 1 | 2 | 3>(0);
  const isManagedFeed = datastream.source_kind === "managed_feed";

  // Optimistic 'running' override for days we just re-fetched.
  const [optimisticRunning, setOptimisticRunning] = useState<Set<string>>(new Set());
  const pollTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const dateFrom = _dateNDaysAgo(LEDGER_DAYS);
  const dateTo = _dateNDaysAgo(1);

  const fetchLedger = useCallback(
    async (silent = false) => {
      if (!silent) setLoadingLedger(true);
      setLedgerError(null);
      try {
        const qs = `project_id=${encodeURIComponent(projectId)}&from=${dateFrom}&to=${dateTo}`;
        const resp = await fetch(
          `${apiBase}/api/datastreams/${datastream.id}/ledger?${qs}`,
          { headers: { Authorization: `Bearer ${apiToken}` } }
        );
        if (!resp.ok) {
          const body = await resp.json().catch(() => ({}));
          throw new Error(body.message || `Erreur ${resp.status}`);
        }
        const data: LedgerResponse = await resp.json();
        setLedger(data.ledger || []);
        // Remove optimistic overrides for days now back in stable state.
        setOptimisticRunning((prev) => {
          const next = new Set(prev);
          for (const entry of data.ledger || []) {
            if (entry.status !== "running") {
              next.delete(entry.date);
            }
          }
          return next;
        });
      } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : String(err);
        if (!silent) setLedgerError(msg);
      } finally {
        if (!silent) setLoadingLedger(false);
      }
    },
    [apiBase, apiToken, datastream.id, dateFrom, dateTo, projectId]
  );

  // Initial load.
  useEffect(() => {
    fetchLedger();
  }, [fetchLedger]);

  // Polling while optimistic running days exist.
  useEffect(() => {
    if (optimisticRunning.size > 0) {
      if (!pollTimerRef.current) {
        pollTimerRef.current = setInterval(() => {
          fetchLedger(true);
        }, POLL_INTERVAL_MS);
      }
    } else {
      if (pollTimerRef.current) {
        clearInterval(pollTimerRef.current);
        pollTimerRef.current = null;
      }
    }
    return () => {
      if (pollTimerRef.current) {
        clearInterval(pollTimerRef.current);
        pollTimerRef.current = null;
      }
    };
  }, [optimisticRunning.size, fetchLedger]);

  const handleToggleDate = useCallback((d: string) => {
    setSelectedDates((prev) => {
      const next = new Set(prev);
      if (next.has(d)) {
        next.delete(d);
      } else {
        next.add(d);
      }
      return next;
    });
  }, []);

  // Build effective ledger: overlay optimistic 'running' on top of real data.
  const effectiveLedger: LedgerEntry[] = ledger.map((e) =>
    optimisticRunning.has(e.date)
      ? { ...e, status: "running" as const }
      : e
  );

  const handleRefetch = async () => {
    if (selectedDates.size === 0) return;
    setRefetching(true);
    setRefetchError(null);

    try {
      const resp = await fetch(`${apiBase}/api/datastreams/${datastream.id}/refetch`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${apiToken}`,
        },
        body: JSON.stringify({
          project_id: projectId,
          dates: Array.from(selectedDates),
        }),
      });
      if (!resp.ok) {
        const body = await resp.json().catch(() => ({}));
        throw new Error(body.message || `Erreur ${resp.status}`);
      }
      const data: RefetchResponse = await resp.json();

      // Mark all date ranges from jobs as optimistically running.
      const newRunning = new Set(optimisticRunning);
      for (const job of data.jobs || []) {
        let cur = new Date(job.date_from + "T00:00:00");
        const end = new Date(job.date_to + "T00:00:00");
        while (cur <= end) {
          const iso = cur.toISOString().slice(0, 10);
          if (selectedDates.has(iso)) newRunning.add(iso);
          cur.setDate(cur.getDate() + 1);
        }
      }
      setOptimisticRunning(newRunning);
      setSelectedDates(new Set());
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      setRefetchError(msg);
    } finally {
      setRefetching(false);
    }
  };

  // Derived status for the header StatusDot: last day's status.
  const latestEntry = effectiveLedger[effectiveLedger.length - 1];
  const headerStatus = latestEntry?.status ?? "never_fetched";

  return (
    <Box sx={{ display: "flex", flexDirection: "column", gap: 0 }}>
      {/* ------------------------------------------------------------------ */}
      {/* Header */}
      {/* ------------------------------------------------------------------ */}
      <Box
        sx={{
          display: "flex",
          alignItems: "flex-start",
          gap: 2,
          px: 3,
          pt: 3,
          pb: 2,
          borderBottom: "1px solid",
          borderColor: "divider",
        }}
      >
        <IconButton
          onClick={onBack}
          size="small"
          aria-label="Retour à la liste"
          sx={{ mt: 0.25 }}
        >
          {/* Left arrow — inline SVG, no icon package needed */}
          <svg
            width="18"
            height="18"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <polyline points="15 18 9 12 15 6" />
          </svg>
        </IconButton>

        <Box sx={{ flex: 1 }}>
          <Box sx={{ display: "flex", alignItems: "center", gap: 1.5, mb: 0.5 }}>
            <Typography variant="h5" sx={{ fontWeight: 700, color: "text.primary" }}>
              {datastream.name}
            </Typography>
            <StatusDot status={headerStatus} size={10} />
          </Box>

          <Box sx={{ display: "flex", flexWrap: "wrap", gap: 3, mt: 0.5 }}>
            <Box>
              <Typography variant="caption" sx={{ color: "text.secondary" }}>
                MODULE
              </Typography>
              <Typography variant="body2" sx={{ fontWeight: 500 }}>
                {datastream.module_name}
              </Typography>
            </Box>
            <Box>
              <Typography variant="caption" sx={{ color: "text.secondary" }}>
                FENÊTRE
              </Typography>
              <Typography
                variant="body2"
                sx={{ fontWeight: 500, fontVariantNumeric: "lining-nums tabular-nums" }}
              >
                {datastream.date_window_days} j
              </Typography>
            </Box>
            <Box>
              <Typography variant="caption" sx={{ color: "text.secondary" }}>
                RATTRAPAGE
              </Typography>
              <Typography
                variant="body2"
                sx={{ fontWeight: 500, fontVariantNumeric: "lining-nums tabular-nums" }}
              >
                J−{datastream.refetch_days}
              </Typography>
            </Box>
            <Box>
              <Typography variant="caption" sx={{ color: "text.secondary" }}>
                PLANIFICATION
              </Typography>
              <Typography variant="body2" sx={{ fontWeight: 500 }}>
                {datastream.schedule_mode === "nightly" ? "Nuitée" : "Manuel"}
              </Typography>
            </Box>
          </Box>
        </Box>
      </Box>

      {/* ------------------------------------------------------------------ */}
      {/* Flow Summary band */}
      {/* ------------------------------------------------------------------ */}
      <FlowSummary datastream={datastream} connectionStatus={connectionStatus} />

      {/* ------------------------------------------------------------------ */}
      {/* Tabs */}
      {/* ------------------------------------------------------------------ */}
      <Box sx={{ borderBottom: "1px solid", borderColor: "divider", px: 3 }}>
        <Tabs
          value={activeTab}
          onChange={(_e, v) => setActiveTab(v as 0 | 1 | 2 | 3)}
          aria-label="Sections du datastream"
          sx={{
            "& .MuiTab-root": {
              fontSize: "0.875rem",
              fontWeight: 500,
              textTransform: "none",
              color: "text.secondary",
              minHeight: 44,
              px: 1.5,
            },
            "& .Mui-selected": { color: "text.primary", fontWeight: 600 },
            "& .MuiTabs-indicator": { backgroundColor: "#FF99C8", height: 2 },
          }}
        >
          <Tab label="Extraits" data-testid="tab-extraits" />
          <Tab label="Mapping" data-testid="tab-mapping" />
          <Tab label="Réglages" data-testid="tab-reglages" />
          {isManagedFeed && (
            <Tab label="Delivery" data-testid="tab-delivery" />
          )}
        </Tabs>
      </Box>

      {/* ------------------------------------------------------------------ */}
      {/* Tab panels */}
      {/* ------------------------------------------------------------------ */}

      {/* Extraits tab */}
      {activeTab === 0 && (
        <Box sx={{ px: 3, pt: 3 }} data-testid="panel-extraits">
          <Box
            sx={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              mb: 2,
            }}
          >
            <Typography variant="h6" sx={{ fontWeight: 600 }}>
              Calendrier d&apos;extraction
            </Typography>

            {/* CTA: Re-fetch */}
            {selectedDates.size > 0 && (
              <Button
                variant="contained"
                size="small"
                disabled={refetching}
                onClick={handleRefetch}
                aria-busy={refetching}
                sx={{
                  bgcolor: "#FF99C8",
                  color: "text.primary",
                  fontWeight: 600,
                  "&:hover": { bgcolor: "#F77FB4" },
                  "&.Mui-disabled": { opacity: 0.5 },
                }}
              >
                {refetching ? (
                  <CircularProgress size={14} sx={{ color: "text.primary", mr: 1 }} />
                ) : null}
                Re-fetch la sélection ({selectedDates.size} j)
              </Button>
            )}
          </Box>

          {refetchError && (
            <Box
              sx={{
                mb: 2,
                p: 1.5,
                borderRadius: 2,
                bgcolor: "rgba(229,57,53,0.08)",
                border: "1px solid rgba(229,57,53,0.20)",
              }}
            >
              <Typography variant="caption" sx={{ color: "#E53935" }}>
                Erreur lors du re-fetch : {refetchError}
              </Typography>
            </Box>
          )}

          {loadingLedger ? (
            <Box sx={{ display: "flex", justifyContent: "center", py: 6 }}>
              <CircularProgress size={28} />
            </Box>
          ) : ledgerError ? (
            <Box sx={{ py: 4, textAlign: "center" }}>
              <Typography variant="body2" sx={{ color: "error.main" }}>
                Impossible de charger le calendrier : {ledgerError}
              </Typography>
              <Button
                size="small"
                onClick={() => fetchLedger()}
                sx={{ mt: 1, color: "text.secondary" }}
              >
                Réessayer
              </Button>
            </Box>
          ) : (
            <Card
              sx={{
                boxShadow: "0 2px 8px rgba(1,0,10,0.06), 0 1px 3px rgba(1,0,10,0.04)",
              }}
            >
              <CardContent sx={{ p: 3, "&:last-child": { pb: 3 } }}>
                <ExtractionCalendar
                  ledger={effectiveLedger}
                  selectedDates={selectedDates}
                  onToggleDate={handleToggleDate}
                />
              </CardContent>
            </Card>
          )}

          <Box sx={{ pb: 6 }} />
        </Box>
      )}

      {/* Mapping tab */}
      {activeTab === 1 && (
        <Box data-testid="panel-mapping">
          <MappingTab
            datastreamId={datastream.id}
            projectId={projectId}
            apiBase={apiBase}
            apiToken={apiToken}
          />
          <Box sx={{ pb: 6 }} />
        </Box>
      )}

      {/* Réglages tab */}
      {activeTab === 2 && (
        <Box data-testid="panel-reglages">
          <ReglagTab
            datastream={datastream}
            projectId={projectId}
            apiBase={apiBase}
            apiToken={apiToken}
          />
          <Box sx={{ pb: 6 }} />
        </Box>
      )}

      {/* Delivery tab — managed_feed only */}
      {activeTab === 3 && isManagedFeed && (
        <Box data-testid="panel-delivery">
          <InboundDeliveryPanel
            datastream={datastream}
            projectId={projectId}
            apiBase={apiBase}
            apiToken={apiToken}
          />
        </Box>
      )}
    </Box>
  );
}
