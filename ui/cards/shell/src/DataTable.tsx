/**
 * DataTable — a clean tabular primitive for card compositions (Story 9.2c).
 *
 * Origin rules: ~52px rows, hairline dividers, right-aligned numeric columns
 * (tabular-nums), header from column defs, optional client-side sort, designed
 * empty state, aria role="table". Hand-rolled — no heavy table lib (AD-11).
 * Light + dark via MUI theme. French-first with accents.
 */

import { useState } from "react";
import Box from "@mui/material/Box";
import Typography from "@mui/material/Typography";
import { useTheme, alpha } from "@mui/material/styles";
import type { DataTableColumn } from "./types";

export interface DataTableProps {
  columns: DataTableColumn[];
  rows: Record<string, unknown>[];
  /** Accessible label (French). */
  ariaLabel?: string;
  /** Number of rows to display; undefined = all. */
  maxRows?: number;
  /** Whether rows are sortable on column header click. Default true. */
  sortable?: boolean;
}

const ROW_HEIGHT = 52; // px — Origin card spec (~52px rows)

type SortDir = "asc" | "desc";

function fmt(value: unknown, numeric: boolean): string {
  if (value === null || value === undefined) return "—";
  if (numeric && typeof value === "number") {
    return value.toLocaleString("fr-FR");
  }
  return String(value);
}

export default function DataTable({
  columns,
  rows,
  ariaLabel = "Tableau de données",
  maxRows,
  sortable = true,
}: DataTableProps) {
  const theme = useTheme();
  const isDark = theme.palette.mode === "dark";
  const hairline = alpha(theme.palette.text.primary, isDark ? 0.1 : 0.08);
  const headerBg = alpha(theme.palette.text.primary, isDark ? 0.06 : 0.03);

  const [sortKey, setSortKey] = useState<string | null>(null);
  const [sortDir, setSortDir] = useState<SortDir>("asc");

  // Designed empty state — never blank (card rule e).
  if (!rows || rows.length === 0 || !columns || columns.length === 0) {
    return (
      <Box
        sx={{ py: 4, textAlign: "center", color: "text.secondary" }}
        data-testid="data-table-empty"
        role="status"
        aria-label={ariaLabel}
      >
        <Typography variant="body2" sx={{ mb: 0.5 }}>
          Aucune donnée disponible.
        </Typography>
        <Typography variant="caption" color="text.disabled">
          Le tableau sera alimenté dès que les données seront disponibles.
        </Typography>
      </Box>
    );
  }

  function handleSort(col: DataTableColumn) {
    if (!sortable || !col.sortable) return;
    if (sortKey === col.key) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(col.key);
      setSortDir("asc");
    }
  }

  let displayRows = [...rows];
  if (sortKey !== null) {
    displayRows.sort((a, b) => {
      const va = a[sortKey];
      const vb = b[sortKey];
      const na = typeof va === "number" ? va : String(va ?? "");
      const nb = typeof vb === "number" ? vb : String(vb ?? "");
      if (na < nb) return sortDir === "asc" ? -1 : 1;
      if (na > nb) return sortDir === "asc" ? 1 : -1;
      return 0;
    });
  }
  if (maxRows !== undefined) {
    displayRows = displayRows.slice(0, maxRows);
  }

  return (
    <Box
      role="table"
      aria-label={ariaLabel}
      data-testid="data-table"
      sx={{ width: "100%", overflow: "hidden", borderRadius: 1 }}
    >
      {/* Header row */}
      <Box
        role="row"
        data-testid="data-table-header"
        sx={{
          display: "flex",
          alignItems: "center",
          bgcolor: headerBg,
          borderBottom: `1px solid ${hairline}`,
          height: 40,
          px: 1.5,
        }}
      >
        {columns.map((col) => (
          <Box
            key={col.key}
            role="columnheader"
            aria-sort={
              sortKey === col.key
                ? sortDir === "asc"
                  ? "ascending"
                  : "descending"
                : undefined
            }
            sx={{
              flex: col.numeric ? "0 0 auto" : "1 1 auto",
              minWidth: col.numeric ? 80 : 100,
              textAlign: col.numeric ? "right" : "left",
              cursor: sortable && col.sortable ? "pointer" : "default",
              userSelect: "none",
              "&:hover": sortable && col.sortable ? { color: "text.primary" } : {},
              color: "text.secondary",
              display: "flex",
              alignItems: "center",
              justifyContent: col.numeric ? "flex-end" : "flex-start",
              gap: 0.5,
            }}
            onClick={() => handleSort(col)}
          >
            <Typography
              variant="overline"
              sx={{ lineHeight: 1.2, fontSize: "0.65rem" }}
            >
              {col.label}
            </Typography>
            {sortable && col.sortable && sortKey === col.key && (
              <Typography variant="caption" sx={{ fontSize: "0.6rem", lineHeight: 1 }}>
                {sortDir === "asc" ? "▲" : "▼"}
              </Typography>
            )}
          </Box>
        ))}
      </Box>

      {/* Data rows */}
      {displayRows.map((row, idx) => (
        <Box
          key={idx}
          role="row"
          data-testid="data-table-row"
          sx={{
            display: "flex",
            alignItems: "center",
            height: ROW_HEIGHT,
            px: 1.5,
            borderBottom:
              idx < displayRows.length - 1 ? `1px solid ${hairline}` : "none",
            "&:hover": {
              bgcolor: alpha(theme.palette.text.primary, isDark ? 0.04 : 0.02),
            },
          }}
        >
          {columns.map((col) => (
            <Box
              key={col.key}
              role="cell"
              sx={{
                flex: col.numeric ? "0 0 auto" : "1 1 auto",
                minWidth: col.numeric ? 80 : 100,
                textAlign: col.numeric ? "right" : "left",
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
              }}
            >
              <Typography
                variant="body2"
                sx={{
                  fontVariantNumeric: col.numeric ? "lining-nums tabular-nums" : undefined,
                  color: "text.primary",
                  lineHeight: 1.4,
                }}
                data-testid={`cell-${col.key}`}
              >
                {fmt(row[col.key], col.numeric ?? false)}
              </Typography>
            </Box>
          ))}
        </Box>
      ))}
    </Box>
  );
}
