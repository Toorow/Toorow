/**
 * ConnectionsList — shows existing Nango connections (Story 2.4, AC2, T3;
 *                   Story 2.5, AC3, AC4, T6).
 *
 * Fetches GET /api/connections on mount and renders a MUI Table.
 * Story 2.5: traffic-light health chip (green/orange/red) + last-pull column
 * + on-demand "Actualiser" (refresh) button per row.
 *
 * UX-DR10: French-first copy.
 * AD-3: no token display.
 * Note: @mui/icons-material is NOT used to avoid bundle weight. Refresh icon
 *       is inline SVG (per Story 2.5 Dev Notes).
 */
import { useEffect, useState } from "react";
import {
  Alert,
  Box,
  Chip,
  CircularProgress,
  IconButton,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  Tooltip,
  Typography,
} from "@mui/material";
import ConnectButton from "./ConnectButton";
import GoogleConnectPanel from "./GoogleConnectPanel";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface ConnectionHealth {
  status: "ok" | "stale" | "revoked";
  last_checked_at: string | null;
  last_fetched_at: string | null;
}

export interface Connection {
  id: string;
  nango_connection_id: string;
  provider: string;
  project_id: string;
  created_at: string;
  /** Story 2.5: nested health object (replaces flat status string from 2.4) */
  health?: ConnectionHealth | null;
  /** Story 8.x: number of enabled datastreams reusing this authorization (1 auth -> N streams). */
  active_datastream_count?: number;
  /** Epic 42 (Sources): ownership + sharing + expiry from the Epic 21/36 model. */
  owner_org_id?: string | null;
  owner_org_name?: string | null;
  /** Credential expiry (populated for google_direct; null for Nango-backed). */
  token_expiry?: string | null;
  /** Derived exposure of this credential to the viewing org. */
  exposure?: "owned" | "shared_with_org" | "provided_by_org";
}

interface ConnectionsResponse {
  connections: Connection[];
}

// ---------------------------------------------------------------------------
// Inline refresh SVG icon (no @mui/icons-material — bundle weight concern)
// ---------------------------------------------------------------------------

function RefreshIcon() {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="currentColor"
      aria-hidden="true"
    >
      <path d="M17.65 6.35A7.96 7.96 0 0 0 12 4C7.58 4 4 7.58 4 12s3.58 8 8 8 8-3.58 8-8h-2c0 3.31-2.69 6-6 6s-6-2.69-6-6 2.69-6 6-6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z" />
    </svg>
  );
}

// ---------------------------------------------------------------------------
// Health chip: traffic-light color mapping (AC3)
// ---------------------------------------------------------------------------

function HealthChip({ health }: { health?: ConnectionHealth | null }) {
  if (!health) {
    return <Chip label="Unknown" color="default" size="small" />;
  }

  switch (health.status) {
    case "ok":
      return <Chip label="Active" color="success" size="small" />;
    case "stale":
      return <Chip label="Late" color="warning" size="small" />;
    case "revoked":
      return <Chip label="Token Expired" color="error" size="small" />;
    default:
      return <Chip label="Unknown" color="default" size="small" />;
  }
}

// ---------------------------------------------------------------------------
// Last-pull cell
// ---------------------------------------------------------------------------

function LastPullCell({ lastFetchedAt }: { lastFetchedAt: string | null | undefined }) {
  if (!lastFetchedAt) {
    return (
      <Typography variant="body2" color="text.secondary">
        Never
      </Typography>
    );
  }
  const formatted = new Intl.DateTimeFormat("en-US", {
    dateStyle: "short",
    timeStyle: "short",
  }).format(new Date(lastFetchedAt));
  return <Typography variant="body2">{formatted}</Typography>;
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

interface ConnectionsListProps {
  /** Story 7.1 (AC6): active project scope. When set, only that project's
   *  connections are shown. Defaults to undefined (show all) for compatibility. */
  projectId?: string;
}

export default function ConnectionsList({ projectId }: ConnectionsListProps = {}) {
  const [connections, setConnections] = useState<Connection[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState<Record<string, boolean>>({});

  async function fetchConnections() {
    setLoading(true);
    setError(null);
    try {
      const url = projectId
        ? `/api/connections?project_id=${encodeURIComponent(projectId)}`
        : "/api/connections";
      const resp = await fetch(url);
      if (!resp.ok) {
        throw new Error(`HTTP ${resp.status}: ${resp.statusText}`);
      }
      const data: ConnectionsResponse = await resp.json();
      setConnections(data.connections ?? []);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void fetchConnections();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  // Story 7.1 (AC6): scope the displayed rows to the active project.
  const visibleConnections = projectId
    ? connections.filter((c) => c.project_id === projectId)
    : connections;

  /** AC4: on-demand health refresh — updates only the affected row in state */
  async function handleRefresh(connId: string) {
    setRefreshing((prev) => ({ ...prev, [connId]: true }));
    try {
      const resp = await fetch(`/api/connections/${connId}/refresh-health`, {
        method: "POST",
      });
      if (!resp.ok) {
        throw new Error(`HTTP ${resp.status}: ${resp.statusText}`);
      }
      const data: { id: string; health: ConnectionHealth } = await resp.json();
      setConnections((prev) =>
        prev.map((c) => (c.id === data.id ? { ...c, health: data.health } : c))
      );
    } catch (err) {
      // Silently log -- a toast notification system is outside Story 2.5 scope
      console.error("Refresh health failed:", err);
    } finally {
      setRefreshing((prev) => ({ ...prev, [connId]: false }));
    }
  }

  if (loading) {
    return (
      <Box sx={{ display: "flex", justifyContent: "center", mt: 4 }}>
        <CircularProgress />
      </Box>
    );
  }

  if (error) {
    return (
      <Alert severity="error" sx={{ mt: 2 }}>
        Error loading connections: {error}
      </Alert>
    );
  }

  return (
    <Box>
      <Box sx={{ display: "flex", justifyContent: "space-between", alignItems: "center", mb: 2 }}>
        <Typography variant="h5" component="h1">
          Authorizations
        </Typography>
        <ConnectButton onSuccess={() => { void fetchConnections(); }} projectId={projectId} />
      </Box>

      {/* Story 18.4 (AD-21) : panneau Google unifié — un seul consentement pour toute
          la stack Google. Câblé sur la première connexion Google du projet ; sans
          connexion Google existante, le flux Nango classique ci-dessus reste le point
          d'entrée pour créer la connexion (bascule google_direct via re-consentement). */}
      {(() => {
        const googleConn = visibleConnections.find((c) =>
          (c.provider || "").toLowerCase().startsWith("google")
        );
        return googleConn && projectId ? (
          <Box sx={{ mb: 3 }}>
            <GoogleConnectPanel
              connectionRefId={googleConn.id}
              projectId={projectId}
              onStatusChange={() => { void fetchConnections(); }}
            />
          </Box>
        ) : null;
      })()}

      {visibleConnections.length === 0 ? (
        <Typography color="text.secondary" sx={{ mt: 2 }}>
          No connections configured. Start by connecting a data source.
        </Typography>
      ) : (
        <Table>
          <TableHead>
            <TableRow>
              <TableCell>Provider</TableCell>
              <TableCell>Connection ID</TableCell>
              <TableCell>Project</TableCell>
              <TableCell>Streams</TableCell>
              <TableCell>Status</TableCell>
              <TableCell>Last Extracted</TableCell>
              <TableCell>Created At</TableCell>
              <TableCell aria-label="Actions" />
            </TableRow>
          </TableHead>
          <TableBody>
            {visibleConnections.map((conn) => (
              <TableRow key={conn.id}>
                <TableCell>{conn.provider}</TableCell>
                <TableCell>{conn.nango_connection_id}</TableCell>
                <TableCell>{conn.project_id}</TableCell>
                <TableCell>
                  <Typography variant="body2" sx={{ color: "text.secondary" }}>
                    {conn.active_datastream_count ?? 0}
                    {" stream(s)"}
                  </Typography>
                </TableCell>
                <TableCell>
                  <HealthChip health={conn.health} />
                </TableCell>
                <TableCell>
                  <LastPullCell lastFetchedAt={conn.health?.last_fetched_at} />
                </TableCell>
                <TableCell>{new Date(conn.created_at).toLocaleString("en-US")}</TableCell>
                <TableCell>
                  <Tooltip title="Refresh">
                    <span>
                      <IconButton
                        size="small"
                        aria-label="Refresh"
                        onClick={() => { void handleRefresh(conn.id); }}
                        disabled={refreshing[conn.id] ?? false}
                      >
                        {refreshing[conn.id] ? (
                          <CircularProgress size={18} />
                        ) : (
                          <RefreshIcon />
                        )}
                      </IconButton>
                    </span>
                  </Tooltip>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </Box>
  );
}
