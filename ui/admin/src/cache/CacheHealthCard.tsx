/**
 * CacheHealthCard -- Health card for the read-through DuckDB cache (Story 19.3).
 *
 * Shows the current cache state: age, cached tables + window + row counts, hit
 * rate, and the "no-cache" / "stale" / "disabled" states surfaced honestly (AD-9,
 * invariant c/f). The "Rebuild" button -> POST /api/admin/cache/rebuild behind a
 * confirmation dialog (AD-8: never a direct DB call).
 *
 * Possible states:
 *   fresh    -- fresh cache, actively used
 *   stale    -- stale cache (nightly ran without a rebuild) -- warning
 *   no-cache -- ephemeral file missing (Cloud Run restart) -- info
 *   disabled -- TOOROW_CACHE_ENABLED=false -- neutral info
 *
 * AD-8: every operation goes through the REST API.
 */

import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  Chip,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Skeleton,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  Tooltip,
  Typography,
} from "@mui/material";

// Inline refresh icon (no @mui/icons-material -- bundle weight, cf. Sidebar.tsx).
function _RefreshSvg() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
      <path d="M17.65 6.35A7.95 7.95 0 0 0 12 4c-4.42 0-7.99 3.58-7.99 8s3.57 8 7.99 8c3.73 0 6.84-2.55 7.73-6h-2.08A5.99 5.99 0 0 1 12 18c-3.31 0-6-2.69-6-6s2.69-6 6-6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z"/>
    </svg>
  );
}

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface CacheStatus {
  cache_state: "fresh" | "stale" | "no-cache" | "disabled";
  cache_enabled: boolean;
  cache_built_at: string | null;
  age_seconds: number | null;
  min_date: string | null;
  max_date: string | null;
  tables: string[];
  row_counts: Record<string, number>;
  project_ids: string[];
  hit_rate: number | null;
  stats: Record<string, number>;
  last_rebuild_cause: string | null;
}

// ---------------------------------------------------------------------------
// API helpers
// ---------------------------------------------------------------------------

function _authHeader(): Record<string, string> {
  const token =
    typeof window !== "undefined"
      ? (window as Window & { __TOOROW_API_KEY__?: string }).__TOOROW_API_KEY__ ?? ""
      : "";
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function fetchCacheStatus(): Promise<CacheStatus> {
  const res = await fetch("/api/admin/cache/status", { headers: _authHeader() });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const data = await res.json();
  // Invariant f: degrade gracefully when the payload is unexpected (tests, proxies).
  if (!data || typeof data !== "object" || !("cache_state" in data)) {
    throw new Error("Unexpected response from the cache server");
  }
  return data as CacheStatus;
}

async function triggerRebuild(): Promise<{ status: string; performed_by: string }> {
  const res = await fetch("/api/admin/cache/rebuild", {
    method: "POST",
    headers: _authHeader(),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({})) as { message?: string };
    throw new Error(body.message ?? `HTTP ${res.status}`);
  }
  return res.json();
}

// ---------------------------------------------------------------------------
// UI helpers
// ---------------------------------------------------------------------------

function _formatAge(seconds: number | null): string {
  if (seconds === null) return "—";
  if (seconds < 60) return `${Math.round(seconds)} s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min`;
  const h = Math.floor(seconds / 3600);
  const m = Math.round((seconds % 3600) / 60);
  return m > 0 ? `${h} h ${m} min` : `${h} h`;
}

function _stateChip(state: CacheStatus["cache_state"]) {
  const configs: Record<
    CacheStatus["cache_state"],
    { label: string; color: "success" | "warning" | "default" | "info" }
  > = {
    fresh: { label: "Fresh", color: "success" },
    stale: { label: "Stale", color: "warning" },
    "no-cache": { label: "Absent", color: "default" },
    disabled: { label: "Disabled", color: "default" },
  };
  const cfg = configs[state] ?? configs["no-cache"];
  return (
    <Chip
      label={cfg.label}
      color={cfg.color}
      size="small"
      sx={{ fontWeight: 600, fontSize: "0.75rem" }}
    />
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

interface CacheHealthCardProps {
  /** Auto-refresh interval in milliseconds (default: 60,000 = 1 min). */
  refreshIntervalMs?: number;
}

export default function CacheHealthCard({
  refreshIntervalMs = 60_000,
}: CacheHealthCardProps) {
  const [status, setStatus] = useState<CacheStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [rebuilding, setRebuilding] = useState(false);
  const [rebuildResult, setRebuildResult] = useState<string | null>(null);
  const [confirmOpen, setConfirmOpen] = useState(false);

  const load = useCallback(() => {
    fetchCacheStatus()
      .then((s) => {
        setStatus(s);
        setError(null);
      })
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    load();
    const interval = setInterval(load, refreshIntervalMs);
    return () => clearInterval(interval);
  }, [load, refreshIntervalMs]);

  const handleRebuildClick = () => setConfirmOpen(true);

  const handleRebuildConfirm = async () => {
    setConfirmOpen(false);
    setRebuilding(true);
    setRebuildResult(null);
    try {
      const result = await triggerRebuild();
      setRebuildResult(
        result.status === "ok"
          ? "Rebuild succeeded."
          : `Result: ${result.status}.`
      );
      // Reload the status after the rebuild.
      load();
    } catch (e) {
      setRebuildResult(`Error: ${String(e)}`);
    } finally {
      setRebuilding(false);
    }
  };

  // ---------------------------------------------------------------------------
  // Render
  // ---------------------------------------------------------------------------

  return (
    <Card
      sx={{
        border: "1.5px solid rgba(235,235,243,0.12)",
        borderRadius: 3,
        boxShadow: "none",
        bgcolor: "background.paper",
      }}
    >
      <CardContent sx={{ p: 3 }}>
        {/* Header */}
        <Box sx={{ display: "flex", alignItems: "center", gap: 2, mb: 2.5 }}>
          <Typography
            variant="subtitle2"
            sx={{ fontWeight: 700, letterSpacing: "0.05em", textTransform: "uppercase" }}
          >
            DuckDB cache
          </Typography>
          {status && _stateChip(status.cache_state)}
          <Box sx={{ flex: 1 }} />
          <Tooltip title="Trigger a manual cache rebuild">
            <span>
              <Button
                variant="outlined"
                size="small"
                startIcon={
                  rebuilding ? <CircularProgress size={14} /> : <_RefreshSvg />
                }
                disabled={rebuilding || loading || !status?.cache_enabled}
                onClick={handleRebuildClick}
                data-testid="cache-rebuild-button"
                sx={{ textTransform: "none", fontWeight: 600 }}
              >
                Rebuild
              </Button>
            </span>
          </Tooltip>
        </Box>

        {/* Initial loading */}
        {loading && (
          <Box sx={{ display: "flex", flexDirection: "column", gap: 1 }}>
            <Skeleton variant="text" width={200} height={16} />
            <Skeleton variant="text" width={160} height={16} />
            <Skeleton variant="rectangular" height={80} sx={{ borderRadius: 1 }} />
          </Box>
        )}

        {/* Error */}
        {!loading && error && (
          <Alert severity="error" sx={{ mb: 2 }}>
            Could not load the cache status: {error}
          </Alert>
        )}

        {/* Rebuild result */}
        {rebuildResult && (
          <Alert
            severity={rebuildResult.startsWith("Error") ? "error" : "success"}
            sx={{ mb: 2 }}
            onClose={() => setRebuildResult(null)}
          >
            {rebuildResult}
          </Alert>
        )}

        {/* Content */}
        {!loading && status && (
          <Box sx={{ display: "flex", flexDirection: "column", gap: 2 }}>
            {/* Honest state warnings (invariant c/f) */}
            {status.cache_state === "disabled" && (
              <Alert severity="info" data-testid="cache-state-disabled">
                The cache is disabled ({" "}
                <code>TOOROW_CACHE_ENABLED=false</code>). Every query goes
                straight to the origin.
              </Alert>
            )}
            {status.cache_state === "no-cache" && (
              <Alert severity="info" data-testid="cache-state-no-cache">
                No cache present (Cloud Run restart?). The service reads from the
                origin — nominal latency until the next rebuild.
              </Alert>
            )}
            {status.cache_state === "stale" && (
              <Alert severity="warning" data-testid="cache-state-stale">
                Stale cache: the nightly ran without a rebuild. Queries bypass the
                cache (no stale data is ever served).
              </Alert>
            )}

            {/* Key metrics */}
            <Box sx={{ display: "flex", flexWrap: "wrap", gap: 3 }}>
              <Box>
                <Typography variant="caption" color="text.secondary">
                  Age
                </Typography>
                <Typography variant="h6" sx={{ fontWeight: 700 }} data-testid="cache-age">
                  {_formatAge(status.age_seconds)}
                </Typography>
              </Box>
              <Box>
                <Typography variant="caption" color="text.secondary">
                  Window
                </Typography>
                <Typography variant="h6" sx={{ fontWeight: 700 }} data-testid="cache-window">
                  {status.min_date && status.max_date
                    ? `${status.min_date} → ${status.max_date}`
                    : "—"}
                </Typography>
              </Box>
              <Box>
                <Typography variant="caption" color="text.secondary">
                  Session hit rate
                </Typography>
                <Typography variant="h6" sx={{ fontWeight: 700 }} data-testid="cache-hit-rate">
                  {status.hit_rate !== null
                    ? `${(status.hit_rate * 100).toFixed(1)} %`
                    : "—"}
                </Typography>
              </Box>
              <Box>
                <Typography variant="caption" color="text.secondary">
                  Projects covered
                </Typography>
                <Typography variant="h6" sx={{ fontWeight: 700 }} data-testid="cache-projects">
                  {(status.project_ids ?? []).length > 0
                    ? (status.project_ids ?? []).join(", ")
                    : "—"}
                </Typography>
              </Box>
            </Box>

            {/* Table by table */}
            {(status.tables ?? []).length > 0 && (
              <Box>
                <Typography variant="caption" color="text.secondary" sx={{ mb: 1, display: "block" }}>
                  Cached tables
                </Typography>
                <Table size="small" sx={{ "& td, & th": { py: 0.5, px: 1 } }}>
                  <TableHead>
                    <TableRow>
                      <TableCell sx={{ fontWeight: 600 }}>Table</TableCell>
                      <TableCell align="right" sx={{ fontWeight: 600 }}>
                        Rows
                      </TableCell>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {(status.tables ?? []).map((t) => (
                      <TableRow key={t}>
                        <TableCell>
                          <Typography variant="body2" sx={{ fontFamily: "monospace" }}>
                            {t}
                          </Typography>
                        </TableCell>
                        <TableCell align="right">
                          <Typography variant="body2">
                            {(status.row_counts[t] ?? 0).toLocaleString("en-US")}
                          </Typography>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </Box>
            )}

            {/* Last rebuild */}
            {status.cache_built_at && (
              <Typography variant="caption" color="text.secondary">
                Last build: {new Date(status.cache_built_at).toLocaleString()}
              </Typography>
            )}
          </Box>
        )}
      </CardContent>

      {/* Rebuild confirmation dialog */}
      <Dialog open={confirmOpen} onClose={() => setConfirmOpen(false)} maxWidth="xs">
        <DialogTitle sx={{ fontWeight: 700 }}>Rebuild the cache?</DialogTitle>
        <DialogContent>
          <Typography variant="body2">
            This reloads the marts from the origin warehouse and writes a fresh
            DuckDB snapshot. It can take a few seconds.
          </Typography>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setConfirmOpen(false)} sx={{ textTransform: "none" }}>
            Cancel
          </Button>
          <Button
            variant="contained"
            onClick={handleRebuildConfirm}
            data-testid="cache-rebuild-confirm"
            sx={{ textTransform: "none", fontWeight: 600 }}
          >
            Rebuild
          </Button>
        </DialogActions>
      </Dialog>
    </Card>
  );
}
