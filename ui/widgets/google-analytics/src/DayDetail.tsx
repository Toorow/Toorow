/**
 * DayDetail — day-click detail panel (AC4 / AC5 / T6).
 *
 * Story 8.8 restyling:
 *   - Clean dialog panel using Origin card style (radius 12, soft shadow).
 *   - Table uses hairline dividers + 52px rows from theme tokens.
 *   - Provenance drill-down collapses into a clean inset block.
 *   - DialogTitle uses h5 weight with muted freshness subtitle (caption).
 *
 * AC4: opens on heatmap day click; shows ALL metrics for that date, broken down
 * by connector (and breakdown_dimension/value).
 *
 * AC5 (provenance drill-down / UX-DR7 / AD-9): each metric row has an info
 * affordance that expands to the full provenance chain.
 * The dialog header shows the KPI confidence / freshness state from meta.freshness.
 */

import { useState } from "react";
import Dialog from "@mui/material/Dialog";
import DialogTitle from "@mui/material/DialogTitle";
import DialogContent from "@mui/material/DialogContent";
import IconButton from "@mui/material/IconButton";
import Box from "@mui/material/Box";
import Typography from "@mui/material/Typography";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Collapse from "@mui/material/Collapse";
import { InfoIcon } from "./icons";
import { getRowsForDate, breakdownLabel } from "./dataUtils";
import { METRIC_LABELS, provenanceList } from "./types";
import type { Row, DailyReportMeta, ProvenanceEntry } from "./types";

interface DayDetailProps {
  date: string | null;
  rows: Row[];
  meta: DailyReportMeta;
  onClose: () => void;
}

function ProvenanceRow({
  row,
  provenance,
}: {
  row: Row;
  provenance: ProvenanceEntry[];
}) {
  const [open, setOpen] = useState(false);
  // Match provenance entry by connector/source_system; fall back to row's own.
  const provEntry = provenance.find((p) => p.source_system === row.connector);
  const sourceSystem = provEntry?.source_system ?? row.connector;

  return (
    <>
      <TableRow>
        <TableCell>{row.connector}</TableCell>
        <TableCell>{METRIC_LABELS[row.metric] ?? row.metric}</TableCell>
        <TableCell align="right">{Number(row.value).toLocaleString("fr-FR")}</TableCell>
        <TableCell>
          {row.breakdown_dimension}
          {row.breakdown_value ? `: ${breakdownLabel(row.breakdown_value)}` : ""}
        </TableCell>
        <TableCell padding="none" align="center">
          <IconButton
            size="small"
            aria-label="Provenance"
            aria-expanded={open}
            onClick={() => setOpen((v) => !v)}
          >
            <InfoIcon size={16} title="Provenance" />
          </IconButton>
        </TableCell>
      </TableRow>
      <TableRow>
        <TableCell colSpan={5} sx={{ py: 0, border: 0 }}>
          <Collapse in={open} unmountOnExit>
            <Box sx={{ py: 1, pl: 1 }}>
              <Typography variant="caption" component="div" color="text.secondary">
                source_system : <strong>{sourceSystem}</strong>
              </Typography>
              <Typography variant="caption" component="div" color="text.secondary">
                pull_id : <code>{provEntry?.pull_id ?? row.pull_id}</code>
              </Typography>
              <Typography variant="caption" component="div" color="text.secondary">
                loaded_at : <code>{row.loaded_at}</code>
              </Typography>
            </Box>
          </Collapse>
        </TableCell>
      </TableRow>
    </>
  );
}

export default function DayDetail({ date, rows, meta, onClose }: DayDetailProps) {
  const open = date !== null;
  const dayRows = date ? getRowsForDate(rows, date) : [];
  const provenance = provenanceList(meta?.provenance);

  const lastPull = meta?.freshness?.last_pull;
  const staleSince = meta?.freshness?.stale_since;

  return (
    <Dialog open={open} onClose={onClose} maxWidth="md" fullWidth>
      <DialogTitle>
        Détail du {date}
        <Typography variant="caption" component="div" color="text.secondary">
          {lastPull
            ? `Dernière mise à jour : ${lastPull}`
            : "Dernière mise à jour : —"}
          {typeof meta?.freshness?.cadence_hours === "number"
            ? ` · cadence ${meta.freshness.cadence_hours} h`
            : ""}
          {staleSince ? ` · périmé depuis ${staleSince}` : ""}
        </Typography>
      </DialogTitle>
      <DialogContent dividers>
        {dayRows.length === 0 ? (
          <Typography variant="body2" color="text.secondary">
            Aucune donnée pour ce jour.
          </Typography>
        ) : (
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell>Connecteur</TableCell>
                <TableCell>Métrique</TableCell>
                <TableCell align="right">Valeur</TableCell>
                <TableCell>Ventilation</TableCell>
                <TableCell padding="none" align="center">
                  Prov.
                </TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {dayRows.map((row, i) => (
                <ProvenanceRow
                  key={`${row.connector}-${row.metric}-${row.breakdown_value}-${i}`}
                  row={row}
                  provenance={provenance}
                />
              ))}
            </TableBody>
          </Table>
        )}
      </DialogContent>
    </Dialog>
  );
}
