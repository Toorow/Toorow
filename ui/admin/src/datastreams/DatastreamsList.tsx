/**
 * DatastreamsList — list view of all datastreams for a project (Story 8.3).
 *
 * Each row:
 *   name | module | autorisation status dot | 35-day mini strip | dernier extrait | actions
 *
 * Actions: activer/désactiver (PATCH enabled), ouvrir (open detail).
 *
 * Premium design (6 rules):
 *  - 52px table rows (theme default).
 *  - Tabular numerals for dates.
 *  - Status dot — no chip.
 *  - One accent color on CTA buttons.
 *  - #EBEBF3 table hairlines (theme default).
 *  - Correct French.
 */
import {
  Box,
  Button,
  CircularProgress,
  Switch,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  Tooltip,
  Typography,
} from "@mui/material";
import { useCallback, useEffect, useState } from "react";
import MiniStrip from "./MiniStrip";
import StatusDot from "./StatusDot";
import { DatastreamSummary, LedgerEntry, LedgerResponse } from "./types";

interface DatastreamsListProps {
  projectId: string;
  apiBase: string;
  apiToken: string;
  onOpenDetail: (ds: DatastreamSummary) => void;
}

/** Fetch 35-day ledger for a single datastream. */
async function _fetchMiniLedger(
  dsId: string,
  projectId: string,
  apiBase: string,
  apiToken: string
): Promise<LedgerEntry[]> {
  const today = new Date();
  const dateTo = new Date(today);
  dateTo.setDate(dateTo.getDate() - 1);
  const dateFrom = new Date(today);
  dateFrom.setDate(dateFrom.getDate() - 35);

  const qs = `project_id=${encodeURIComponent(projectId)}&from=${dateFrom.toISOString().slice(0, 10)}&to=${dateTo.toISOString().slice(0, 10)}`;
  const url = `${apiBase}/api/datastreams/${dsId}/ledger?${qs}`;

  const resp = await fetch(url, {
    headers: { Authorization: `Bearer ${apiToken}` },
  });
  if (!resp.ok) return [];
  const data: LedgerResponse = await resp.json();
  return data.ledger || [];
}

/** French date string (short). */
function _frDate(isoStr: string | null): string {
  if (!isoStr) return "—";
  try {
    return new Date(isoStr).toLocaleDateString("fr-FR", {
      day: "2-digit",
      month: "short",
      year: "numeric",
    });
  } catch {
    return isoStr.slice(0, 10);
  }
}

export default function DatastreamsList({
  projectId,
  apiBase,
  apiToken,
  onOpenDetail,
}: DatastreamsListProps) {
  const [datastreams, setDatastreams] = useState<DatastreamSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [ledgers, setLedgers] = useState<Record<string, LedgerEntry[]>>({});
  const [toggling, setToggling] = useState<Set<string>>(new Set());

  const fetchDatastreams = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const qs = `project_id=${encodeURIComponent(projectId)}`;
      const resp = await fetch(`${apiBase}/api/datastreams?${qs}`, {
        headers: { Authorization: `Bearer ${apiToken}` },
      });
      if (!resp.ok) {
        const body = await resp.json().catch(() => ({}));
        throw new Error(body.message || `Erreur ${resp.status}`);
      }
      const data: DatastreamSummary[] = await resp.json();
      setDatastreams(data);

      // Fetch mini ledgers in parallel (best-effort; failures show empty strip).
      const ledgerEntries = await Promise.allSettled(
        data.map((ds) =>
          _fetchMiniLedger(ds.id, projectId, apiBase, apiToken).then(
            (ledger) => ({ id: ds.id, ledger })
          )
        )
      );
      const ledgerMap: Record<string, LedgerEntry[]> = {};
      for (const result of ledgerEntries) {
        if (result.status === "fulfilled") {
          ledgerMap[result.value.id] = result.value.ledger;
        }
      }
      setLedgers(ledgerMap);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      setError(msg);
    } finally {
      setLoading(false);
    }
  }, [apiBase, apiToken, projectId]);

  useEffect(() => {
    fetchDatastreams();
  }, [fetchDatastreams]);

  const handleToggleEnabled = async (ds: DatastreamSummary) => {
    setToggling((prev) => new Set(prev).add(ds.id));
    try {
      const resp = await fetch(`${apiBase}/api/datastreams/${ds.id}`, {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${apiToken}`,
        },
        body: JSON.stringify({ project_id: projectId, enabled: !ds.enabled }),
      });
      if (!resp.ok) return;
      // Optimistic update.
      setDatastreams((prev) =>
        prev.map((d) => (d.id === ds.id ? { ...d, enabled: !ds.enabled } : d))
      );
    } finally {
      setToggling((prev) => {
        const next = new Set(prev);
        next.delete(ds.id);
        return next;
      });
    }
  };

  if (loading) {
    return (
      <Box sx={{ display: "flex", justifyContent: "center", py: 8 }}>
        <CircularProgress size={28} />
      </Box>
    );
  }

  if (error) {
    return (
      <Box sx={{ py: 4, textAlign: "center" }}>
        <Typography variant="body2" sx={{ color: "error.main", mb: 1 }}>
          Impossible de charger les datastreams : {error}
        </Typography>
        <Button size="small" onClick={fetchDatastreams} sx={{ color: "text.secondary" }}>
          Réessayer
        </Button>
      </Box>
    );
  }

  if (datastreams.length === 0) {
    return (
      <Box
        sx={{
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          minHeight: 320,
          gap: 2,
          color: "text.secondary",
        }}
      >
        <Typography variant="h6" sx={{ fontWeight: 600, color: "text.primary" }}>
          Aucun datastream
        </Typography>
        <Typography variant="body2" sx={{ maxWidth: 380, textAlign: "center" }}>
          Créez un premier flux de données pour commencer à extraire vos données.
        </Typography>
      </Box>
    );
  }

  return (
    <Box>
      <Table>
        <TableHead>
          <TableRow>
            <TableCell>Nom</TableCell>
            <TableCell>Module</TableCell>
            <TableCell>Auth</TableCell>
            <TableCell>Extractions (35 j)</TableCell>
            <TableCell>Dernier extrait</TableCell>
            <TableCell>Actif</TableCell>
            <TableCell align="right">Actions</TableCell>
          </TableRow>
        </TableHead>
        <TableBody>
          {datastreams.map((ds) => {
            const dsLedger = ledgers[ds.id] || [];
            const lastPullAt = ds.last_pull_completed_at;

            return (
              <TableRow key={ds.id} hover>
                <TableCell>
                  <Typography variant="body2" sx={{ fontWeight: 500 }}>
                    {ds.name}
                  </Typography>
                </TableCell>

                <TableCell>
                  <Typography variant="body2" sx={{ color: "text.secondary" }}>
                    {ds.module_name}
                  </Typography>
                </TableCell>

                <TableCell>
                  <Tooltip
                    title={ds.connection_status || "Non liée"}
                    arrow
                    placement="top"
                  >
                    <Box component="span">
                      <StatusDot
                        status={
                          ds.connection_status === "active"
                            ? "ok"
                            : ds.connection_status === "populate_failed"
                            ? "partial"
                            : ds.connection_ref_id
                            ? "failed"
                            : "never_fetched"
                        }
                        size={8}
                        showTooltip={false}
                      />
                    </Box>
                  </Tooltip>
                </TableCell>

                <TableCell>
                  <MiniStrip ledger={dsLedger} />
                </TableCell>

                <TableCell>
                  <Typography
                    variant="body2"
                    sx={{
                      color: "text.secondary",
                      fontVariantNumeric: "lining-nums tabular-nums",
                    }}
                  >
                    {_frDate(lastPullAt)}
                  </Typography>
                </TableCell>

                <TableCell>
                  <Switch
                    checked={ds.enabled}
                    size="small"
                    disabled={toggling.has(ds.id)}
                    onChange={() => handleToggleEnabled(ds)}
                    aria-label={`Activer ${ds.name}`}
                  />
                </TableCell>

                <TableCell align="right">
                  <Button
                    size="small"
                    variant="outlined"
                    onClick={() => onOpenDetail(ds)}
                    sx={{
                      borderColor: "#EBEBF3",
                      color: "text.primary",
                      fontWeight: 500,
                      "&:hover": {
                        borderColor: "#FF99C8",
                        bgcolor: "rgba(155,225,246,0.08)",
                      },
                    }}
                  >
                    Ouvrir
                  </Button>
                </TableCell>
              </TableRow>
            );
          })}
        </TableBody>
      </Table>
    </Box>
  );
}
