/**
 * App — Connectors card assembly (Epic 9, Story 9.8).
 *
 * Card answers: "Quels connecteurs sont disponibles et que alimentent-ils ?"
 * Composition: table(connecteur / statut / dernier extract / flows) + comment.
 *
 * Status column: renders a subtle status dot + French label via token colors.
 * Statuts: "Opérationnel" | "Obsolète" | "Révoqué" | "En erreur" | "Inconnu"
 *
 * Context-kind card (no fact rows): hero = the connector inventory table.
 * The 6 card rules apply: light+dark via tokens, French-first, designed empty state.
 *
 * The table block is rendered by ConnectorsTable (custom, for the status dot).
 * All other blocks (comment, unknown) are delegated to CardComposition.
 */

import CardShell, { CardComposition } from "@toorow/card-shell";
import type { CardEnvelope, CompositionBlock, DataTableColumn } from "@toorow/card-shell";
import Box from "@mui/material/Box";
import Typography from "@mui/material/Typography";
import { useTheme, alpha } from "@mui/material/styles";

/** Local mirror of TableBlockData for the connectors card (not re-exported from card-shell). */
interface ConnectorTableData {
  columns: DataTableColumn[];
  rows: Record<string, unknown>[];
}

// ---------------------------------------------------------------------------
// Status dot metadata — French labels -> MUI theme palette (token-backed, card rule c).
// ---------------------------------------------------------------------------

/** Connector status kinds — drives the palette slot chosen from the theme. */
type StatusKind = "success" | "warning" | "secondary" | "error" | "divider";

/** Maps French connector status label to a theme palette semantic. */
function statusKind(status: string): StatusKind {
  switch (status) {
    case "Opérationnel": return "success";
    case "Obsolète":     return "warning";
    case "Révoqué":      return "secondary";
    case "En erreur":    return "error";
    default:             return "divider";
  }
}

// ---------------------------------------------------------------------------
// StatusCell — inline status dot + label (tokens only, card rule c)
// ---------------------------------------------------------------------------

function StatusCell({ status }: { status: string }) {
  const theme = useTheme();
  const kind = statusKind(status);

  // Resolve dot color from theme palette (token-backed, no raw hex literals).
  let dotColor: string;
  switch (kind) {
    case "success":   dotColor = theme.palette.success.main; break;
    case "warning":   dotColor = theme.palette.warning.main; break;
    case "error":     dotColor = theme.palette.error.main;   break;
    case "secondary": dotColor = theme.palette.text.secondary; break;
    default:          dotColor = theme.palette.divider;        break;
  }

  return (
    <Box
      sx={{ display: "flex", alignItems: "center", gap: 0.75 }}
      data-testid="connector-status-cell"
      data-status={status}
    >
      <Box
        component="span"
        aria-hidden="true"
        sx={{
          display: "inline-block",
          width: 7,
          height: 7,
          borderRadius: "50%",
          flexShrink: 0,
          backgroundColor: dotColor,
        }}
        data-testid="connector-status-dot"
      />
      <Typography
        variant="body2"
        component="span"
        sx={{ color: "text.primary", lineHeight: 1.4 }}
        data-testid="connector-status-label"
      >
        {status || "Inconnu"}
      </Typography>
    </Box>
  );
}

// ---------------------------------------------------------------------------
// ConnectorsTable — custom table renderer with status-dot column
// ---------------------------------------------------------------------------

const ROW_HEIGHT = 52;

function ConnectorsTable({
  tableData,
  title,
}: {
  tableData: ConnectorTableData;
  title?: string;
}) {
  const theme = useTheme();
  const isDark = theme.palette.mode === "dark";
  const hairline = alpha(theme.palette.text.primary, isDark ? 0.1 : 0.08);
  const headerBg = alpha(theme.palette.text.primary, isDark ? 0.06 : 0.03);

  const rows = tableData.rows ?? [];
  const columns = tableData.columns ?? [];

  // Designed empty state — never blank (card rule e).
  if (rows.length === 0) {
    return (
      <Box
        sx={{ py: 4, textAlign: "center", color: "text.secondary" }}
        data-testid="connectors-table-empty"
        role="status"
        aria-label={title ?? "Connecteurs"}
      >
        <Typography variant="body2" sx={{ mb: 0.5 }}>
          Aucun connecteur configuré.
        </Typography>
        <Typography variant="caption" color="text.disabled">
          Configurez un datastream pour voir les connecteurs disponibles.
        </Typography>
      </Box>
    );
  }

  return (
    <Box
      role="table"
      aria-label={title ?? "Connecteurs"}
      data-testid="connectors-table"
      sx={{ width: "100%", overflow: "hidden", borderRadius: 1 }}
    >
      {/* Header row */}
      <Box
        role="row"
        data-testid="connectors-table-header"
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
            sx={{
              flex: "1 1 auto",
              minWidth: 80,
              color: "text.secondary",
            }}
          >
            <Typography variant="overline" sx={{ lineHeight: 1.2, fontSize: "0.65rem" }}>
              {col.label}
            </Typography>
          </Box>
        ))}
      </Box>

      {/* Data rows */}
      {rows.map((row, idx) => (
        <Box
          key={idx}
          role="row"
          data-testid="connectors-table-row"
          sx={{
            display: "flex",
            alignItems: "center",
            height: ROW_HEIGHT,
            px: 1.5,
            borderBottom: idx < rows.length - 1 ? `1px solid ${hairline}` : "none",
            "&:hover": {
              bgcolor: alpha(theme.palette.text.primary, isDark ? 0.04 : 0.02),
            },
          }}
        >
          {columns.map((col) => {
            const value = row[col.key];

            // Status column gets the special dot treatment
            if (col.key === "status") {
              return (
                <Box key={col.key} role="cell" sx={{ flex: "1 1 auto", minWidth: 80 }}>
                  <StatusCell status={String(value ?? "")} />
                </Box>
              );
            }

            return (
              <Box
                key={col.key}
                role="cell"
                sx={{
                  flex: "1 1 auto",
                  minWidth: 80,
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                }}
              >
                <Typography
                  variant="body2"
                  sx={{ color: "text.primary", lineHeight: 1.4 }}
                  data-testid={`connector-cell-${col.key}`}
                >
                  {value !== null && value !== undefined ? String(value) : "—"}
                </Typography>
              </Box>
            );
          })}
        </Box>
      ))}
    </Box>
  );
}

// ---------------------------------------------------------------------------
// ConnectorsBody — renders blocks in composition order (F-5 fix).
//
// The connectors card uses ConnectorsTable for "table" blocks (custom status-dot
// renderer) and CardComposition for everything else (comment, unknown, etc.).
// We iterate ALL blocks in declared order so that:
//   - A composition with 2 table blocks renders both (no silent discard via find()).
//   - A table block before a comment renders table → comment, not comment → table.
//   - A composition with no table block at all renders the designed empty state
//     once (no orphaned second testid from a split find/filter pattern).
// ---------------------------------------------------------------------------

function ConnectorsBody({
  blocks,
  data,
}: {
  blocks: CompositionBlock[];
  data: CardEnvelope["data"];
}) {
  // If there are no blocks at all, or no table block anywhere, show the no-table fallback.
  const hasAnyTableBlock = blocks.some((b) => b.type === "table");

  if (blocks.length === 0 || !hasAnyTableBlock) {
    return (
      <Box sx={{ display: "flex", flexDirection: "column", gap: 2 }} data-testid="connectors-body">
        {/* No table block in composition — designed empty state (card rule e) */}
        <Box
          sx={{ py: 4, textAlign: "center", color: "text.secondary" }}
          data-testid="connectors-no-table-block"
          role="status"
        >
          <Typography variant="body2" sx={{ mb: 0.5 }}>
            Aucun connecteur configuré.
          </Typography>
          <Typography variant="caption" color="text.disabled">
            Configurez un datastream pour voir les connecteurs disponibles.
          </Typography>
        </Box>
        {/* Non-table blocks still render (e.g. a comment block alone) */}
        {blocks.length > 0 && (
          <CardComposition blocks={blocks} data={data} />
        )}
      </Box>
    );
  }

  // Render all blocks in composition order. Table blocks use ConnectorsTable
  // (status-dot renderer); all other block types fall through to CardComposition
  // (one call per non-table block, preserving order and supporting 2+ table blocks).
  return (
    <Box sx={{ display: "flex", flexDirection: "column", gap: 2 }} data-testid="connectors-body">
      {blocks.map((block, idx) => {
        if (block.type === "table") {
          return (
            <ConnectorsTable
              key={`table-${idx}`}
              tableData={(block.data as ConnectorTableData) ?? { columns: [], rows: [] }}
              title={block.title}
            />
          );
        }
        // Non-table block: delegate to CardComposition (one block at a time, order preserved).
        return (
          <CardComposition key={`other-${idx}`} blocks={[block]} data={data} />
        );
      })}
    </Box>
  );
}

// ---------------------------------------------------------------------------
// App props
// ---------------------------------------------------------------------------

interface AppProps {
  envelope: CardEnvelope;
  adminConsoleUrl?: string;
}

// ---------------------------------------------------------------------------
// App root
// ---------------------------------------------------------------------------

export default function App({ envelope, adminConsoleUrl = "/admin" }: AppProps) {
  const { meta, data } = envelope;

  const feedbackProps = {
    projectId: meta.project_id ?? "unknown",
    traceId: meta.trace_id ?? null,
    reportRef: `card:${data.card_id}:${data.date_range.end}`,
    module: `card-${data.card_id}`,
  };

  const blocks = data.composition ?? [];
  // Suppress shell's comment slot when the composition contains a comment block
  // (same F-8 rule as journey/keywords/etc.)
  const hasCommentBlock = blocks.some((b) => b.type === "comment");

  return (
    <CardShell
      title={data.title}
      answersQuestion={data.answers_question}
      meta={meta}
      dateRange={data.date_range}
      renderedComment={hasCommentBlock ? undefined : data.rendered_comment}
      metricDefinitions={data.metric_definitions}
      adminConsoleUrl={adminConsoleUrl}
      feedbackProps={feedbackProps}
    >
      <ConnectorsBody blocks={blocks} data={data} />
    </CardShell>
  );
}
