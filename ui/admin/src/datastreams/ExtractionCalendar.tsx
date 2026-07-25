/**
 * ExtractionCalendar — month-grid heatmap of day extraction statuses.
 *
 * Features:
 *  - Month grid: weeks in rows, Mon-Sun columns.
 *  - Color per day from STATUS_META (low-alpha tints — Rule 4: no solid chips).
 *  - Click to select/deselect individual days.
 *  - Completeness % shown below each day cell when expected_rows known.
 *  - Legend at bottom (ok/partiel/vide/échoué/en cours/jamais).
 *
 * Rule 3: completeness % is the numeric hero — tabular-nums.
 * Rule 5: generous padding, cell gaps, no cramped layout.
 */
import { Box, Typography, Tooltip } from "@mui/material";
import { useMemo } from "react";
import { LedgerEntry, STATUS_META } from "./types";

interface ExtractionCalendarProps {
  ledger: LedgerEntry[];
  selectedDates: Set<string>;
  onToggleDate: (date: string) => void;
}

/** ISO week day: 0=Mon .. 6=Sun (matching CSS grid layout). */
function _isoWeekday(dateStr: string): number {
  const d = new Date(dateStr + "T00:00:00");
  return (d.getDay() + 6) % 7; // convert Sun=0 to Mon=0
}

/** Group ledger entries by YYYY-MM month. */
function _groupByMonth(ledger: LedgerEntry[]): Map<string, LedgerEntry[]> {
  const months = new Map<string, LedgerEntry[]>();
  for (const entry of ledger) {
    const month = entry.date.slice(0, 7); // YYYY-MM
    if (!months.has(month)) months.set(month, []);
    months.get(month)!.push(entry);
  }
  return months;
}

const WEEK_LABELS = ["L", "M", "M", "J", "V", "S", "D"];
const MONTH_FR: Record<string, string> = {
  "01": "Janvier", "02": "Février", "03": "Mars", "04": "Avril",
  "05": "Mai", "06": "Juin", "07": "Juillet", "08": "Août",
  "09": "Septembre", "10": "Octobre", "11": "Novembre", "12": "Décembre",
};

interface DayCellProps {
  entry: LedgerEntry;
  isSelected: boolean;
  onToggle: (date: string) => void;
}

function DayCell({ entry, isSelected, onToggle }: DayCellProps) {
  const meta = STATUS_META[entry.status];
  const pct =
    entry.completeness_ratio != null
      ? `${Math.round(entry.completeness_ratio * 100)}%`
      : null;

  const tooltipContent =
    `${entry.date} · ${meta.label}` +
    (pct ? ` · ${pct}` : "") +
    (entry.row_count != null ? ` · ${entry.row_count.toLocaleString("fr-FR")} lignes` : "");

  return (
    <Tooltip title={tooltipContent} arrow placement="top">
      <Box
        component="button"
        onClick={() => onToggle(entry.date)}
        aria-label={tooltipContent}
        aria-pressed={isSelected}
        sx={{
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          gap: "2px",
          width: 36,
          padding: "4px 2px",
          border: isSelected ? "1.5px solid #FF99C8" : "1px solid transparent",
          borderRadius: 1,
          background: isSelected ? "rgba(155,225,246,0.15)" : meta.color,
          cursor: "pointer",
          transition: "all 0.1s ease",
          "&:hover": {
            borderColor: "#FF99C8",
            background: isSelected ? "rgba(155,225,246,0.20)" : `${meta.color}`,
            opacity: 0.9,
          },
          "&:focus-visible": {
            outline: "2px solid #FF99C8",
            outlineOffset: 1,
          },
        }}
      >
        {/* Day number */}
        <Typography
          component="span"
          sx={{
            fontSize: "0.6875rem",
            fontWeight: isSelected ? 700 : 500,
            color: "text.primary",
            fontVariantNumeric: "lining-nums tabular-nums",
            lineHeight: 1,
          }}
        >
          {entry.date.slice(8)} {/* DD */}
        </Typography>

        {/* Colored status bar */}
        <Box
          component="span"
          sx={{
            width: 20,
            height: 4,
            borderRadius: 1,
            bgcolor: meta.dotColor,
            opacity: 0.8,
          }}
        />

        {/* Completeness % */}
        {pct && (
          <Typography
            component="span"
            sx={{
              fontSize: "0.5625rem",
              color: "text.secondary",
              fontVariantNumeric: "lining-nums tabular-nums",
              lineHeight: 1,
            }}
          >
            {pct}
          </Typography>
        )}
      </Box>
    </Tooltip>
  );
}

/** Empty placeholder cell (padding before month start or after end). */
function EmptyCell() {
  return <Box sx={{ width: 36, height: "auto" }} />;
}

interface MonthGridProps {
  month: string; // YYYY-MM
  entries: LedgerEntry[];
  selectedDates: Set<string>;
  onToggleDate: (date: string) => void;
}

function MonthGrid({ month, entries, selectedDates, onToggleDate }: MonthGridProps) {
  const [year, mm] = month.split("-");
  const monthLabel = `${MONTH_FR[mm] || mm} ${year}`;

  // Build a map date -> entry for quick lookup.
  const entryMap = useMemo(() => {
    const m = new Map<string, LedgerEntry>();
    for (const e of entries) m.set(e.date, e);
    return m;
  }, [entries]);

  if (entries.length === 0) return null;

  const firstDay = entries[0].date;
  const lastDay = entries[entries.length - 1].date;
  const firstWeekday = _isoWeekday(firstDay);

  // We render a grid: padding empty cells + day cells.
  const cells: (LedgerEntry | null)[] = [
    ...Array(firstWeekday).fill(null), // leading empties
  ];

  // Fill day cells from firstDay to lastDay.
  let cur = new Date(firstDay + "T00:00:00");
  const endDate = new Date(lastDay + "T00:00:00");
  while (cur <= endDate) {
    const iso = cur.toISOString().slice(0, 10);
    cells.push(entryMap.get(iso) ?? null);
    cur.setDate(cur.getDate() + 1);
  }

  // Pad to multiple of 7.
  while (cells.length % 7 !== 0) cells.push(null);

  const weeks: (LedgerEntry | null)[][] = [];
  for (let i = 0; i < cells.length; i += 7) {
    weeks.push(cells.slice(i, i + 7));
  }

  return (
    <Box sx={{ mb: 3 }}>
      <Typography
        variant="overline"
        sx={{ color: "text.secondary", display: "block", mb: 1 }}
      >
        {monthLabel}
      </Typography>

      {/* Week day headers */}
      <Box sx={{ display: "flex", gap: "4px", mb: 0.5 }}>
        {WEEK_LABELS.map((d, i) => (
          <Box key={i} sx={{ width: 36, textAlign: "center" }}>
            <Typography
              component="span"
              sx={{ fontSize: "0.625rem", color: "text.secondary", fontWeight: 600 }}
            >
              {d}
            </Typography>
          </Box>
        ))}
      </Box>

      {/* Week rows */}
      {weeks.map((week, wi) => (
        <Box key={wi} sx={{ display: "flex", gap: "4px", mb: "4px" }}>
          {week.map((cell, di) =>
            cell ? (
              <DayCell
                key={cell.date}
                entry={cell}
                isSelected={selectedDates.has(cell.date)}
                onToggle={onToggleDate}
              />
            ) : (
              <EmptyCell key={`empty-${wi}-${di}`} />
            )
          )}
        </Box>
      ))}
    </Box>
  );
}

/** Legend row at the bottom of the calendar. */
function Legend() {
  const statuses: LedgerEntry["status"][] = [
    "ok", "partial", "empty", "failed", "running", "never_fetched",
  ];
  return (
    <Box
      sx={{
        display: "flex",
        flexWrap: "wrap",
        gap: 2,
        mt: 1,
        pt: 2,
        borderTop: "1px solid",
        borderColor: "divider",
      }}
    >
      {statuses.map((s) => {
        const meta = STATUS_META[s];
        return (
          <Box key={s} sx={{ display: "flex", alignItems: "center", gap: 0.75 }}>
            <Box
              component="span"
              sx={{
                width: 10,
                height: 10,
                borderRadius: 0.5,
                bgcolor: meta.dotColor,
                opacity: 0.85,
                flexShrink: 0,
              }}
            />
            <Typography variant="caption" sx={{ color: "text.secondary" }}>
              {meta.label}
            </Typography>
          </Box>
        );
      })}
    </Box>
  );
}

export default function ExtractionCalendar({
  ledger,
  selectedDates,
  onToggleDate,
}: ExtractionCalendarProps) {
  const byMonth = useMemo(() => _groupByMonth(ledger), [ledger]);
  const months = Array.from(byMonth.keys()).sort();

  if (!ledger || ledger.length === 0) {
    return (
      <Box sx={{ py: 4, textAlign: "center" }}>
        <Typography variant="body2" sx={{ color: "text.secondary" }}>
          Aucune donnée d&apos;extraction disponible.
        </Typography>
      </Box>
    );
  }

  return (
    <Box>
      {months.map((month) => (
        <MonthGrid
          key={month}
          month={month}
          entries={byMonth.get(month) || []}
          selectedDates={selectedDates}
          onToggleDate={onToggleDate}
        />
      ))}
      <Legend />
    </Box>
  );
}
