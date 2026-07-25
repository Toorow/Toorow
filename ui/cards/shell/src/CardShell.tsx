/**
 * CardShell — shared chrome for every Connector card (Epic 9, Story 9.1).
 *
 * Origin design system. Provides the card frame every template shares; only the BODY
 * (children) differs per card. Chrome:
 *   1. Header: the card TITLE (the question it answers, muted subtitle) + freshness/
 *      as-of badge (right).
 *   2. Body slot (children) — the hero content (numbers), owned by the template.
 *   3. Rendered-comment slot: the deterministic cited what+why (AD-9), MUTED, with a
 *      source affordance (a small "source" chip that reveals provenance on hover).
 *   4. Definitions popover: fed by data.metric_definitions (R6) — a toggle that opens
 *      the per-metric definition list.
 *   5. Slim footer: source-system line + date range.
 *
 * Delegates theme + light/dark re-theming + stale/alert/auth_expired banners to
 * @toorow/shell's WidgetShell (the widget-level shell). CardShell renders NO
 * ThemeProvider / background of its own (AD-11 / NFR11).
 *
 * The 6 Origin rules + card rules: title muted, the NUMBER (in children) is the hero,
 * the comment is secondary/muted, light+dark via WidgetShell, French-first with accents.
 */

import { useState, useId, useRef, type ReactNode } from "react";
import WidgetShell from "@toorow/shell";
import type { WidgetMeta } from "@toorow/shell";
import Box from "@mui/material/Box";
import Collapse from "@mui/material/Collapse";
import Typography from "@mui/material/Typography";
import Button from "@mui/material/Button";
import { useTheme, alpha } from "@mui/material/styles";
import { SHADOW_CARD_LIGHT, SHADOW_CARD_DARK } from "../../../tokens/dist/theme";
import type { CardMeta, MetricDefinition } from "./types";
import { metricLabel } from "./types";
import CardFeedbackBar from "./CardFeedbackBar";
import type { CardFeedbackBarProps } from "./CardFeedbackBar";

interface CardShellProps {
  title: string;
  /** The FR question this card answers (muted subtitle). */
  answersQuestion?: string;
  meta: CardMeta;
  dateRange: { start: string; end: string };
  /** Deterministic cited comment (AD-9). Rendered muted in the comment slot. */
  renderedComment?: string;
  /** R6: per-metric definitions for the definitions popover. */
  metricDefinitions?: Record<string, MetricDefinition>;
  adminConsoleUrl?: string;
  /**
   * Story 9.2b: feedback props → renders CardFeedbackBar in the footer chrome.
   * Derive from meta.project_id, meta.trace_id, data.card_id in the template.
   * When omitted, the feedback chrome row is not rendered.
   */
  feedbackProps?: CardFeedbackBarProps;
  children: ReactNode;
}

/** Format an ISO timestamp to a compact "il y a ..." freshness badge (French). */
function freshnessLabel(lastPull: string | null | undefined): string | null {
  if (!lastPull) return null;
  const d = new Date(lastPull);
  if (isNaN(d.getTime())) return null;
  const pad = (n: number) => String(n).padStart(2, "0");
  return `MAJ ${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`;
}

/** Source affordance — a muted chip revealing provenance on hover/focus. */
function SourceAffordance({ meta }: { meta: CardMeta }) {
  const [open, setOpen] = useState(false);
  const theme = useTheme();
  const id = useId();
  const src = meta.provenance?.source_system;
  const pull = meta.provenance?.pull_id;
  if (!src && !pull) return null;

  return (
    <Box sx={{ position: "relative", display: "inline-flex", ml: 1 }}>
      <Box
        component="button"
        aria-describedby={id}
        onMouseEnter={() => setOpen(true)}
        onMouseLeave={() => setOpen(false)}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
        sx={{
          all: "unset",
          cursor: "pointer",
          fontSize: "0.65rem",
          color: "text.secondary",
          border: `1px solid ${alpha(theme.palette.text.secondary, 0.3)}`,
          borderRadius: 1,
          px: 0.5,
          lineHeight: 1.6,
          "&:hover, &:focus-visible": { color: "text.primary", borderColor: "text.primary" },
        }}
        data-testid="card-source-affordance"
      >
        source
      </Box>
      {open && (
        <Box
          role="tooltip"
          id={id}
          sx={{
            position: "absolute",
            bottom: "calc(100% + 6px)",
            left: 0,
            zIndex: 1500,
            width: 240,
            p: 1.25,
            borderRadius: 2,
            bgcolor: "background.paper",
            // Use alpha() on theme token — no raw rgba literal (card rule c, F-12).
            boxShadow: `0 4px 20px ${alpha(theme.palette.text.primary, 0.14)}, 0 1px 4px ${alpha(theme.palette.text.primary, 0.08)}`,
            border: `1px solid ${alpha(theme.palette.text.primary, 0.1)}`,
            pointerEvents: "none",
          }}
          data-testid="card-source-tooltip"
        >
          <Typography variant="overline" color="text.secondary" sx={{ display: "block" }}>
            Provenance
          </Typography>
          {src && (
            <Typography variant="caption" component="p">
              Système : {src}
            </Typography>
          )}
          {pull && (
            <Typography variant="caption" component="p" color="text.secondary">
              Pull : {pull}
            </Typography>
          )}
        </Box>
      )}
    </Box>
  );
}

export default function CardShell({
  title,
  answersQuestion,
  meta,
  dateRange,
  renderedComment,
  metricDefinitions,
  adminConsoleUrl = "/admin",
  feedbackProps,
  children,
}: CardShellProps) {
  const theme = useTheme();
  // Use theme.palette.mode to select card shadow — @media (prefers-color-scheme: dark)
  // never fires in MCP Apps host (theme controlled via ThemeProvider, not OS preference).
  // Token constants from ui/tokens/dist/theme.ts; no raw rgba literals (card rule c, F-2).
  const cardBoxShadow = theme.palette.mode === "dark" ? SHADOW_CARD_DARK : SHADOW_CARD_LIGHT;
  const [defsOpen, setDefsOpen] = useState(false);
  const cardRef = useRef<HTMLDivElement>(null);
  const widgetMeta = meta as unknown as WidgetMeta;

  function handleExport() {
    // Export PNG : window.print() scoped fallback (AD-11 — no heavy dep added).
    // Une story future peut remplacer par html-to-image si la lib est déjà dans le bundle.
    window.print();
  }
  const fresh = freshnessLabel(meta.freshness?.last_pull);
  const hasDefs = metricDefinitions && Object.keys(metricDefinitions).length > 0;
  const sourceSystem = meta.provenance?.source_system;

  return (
    <WidgetShell meta={widgetMeta} adminConsoleUrl={adminConsoleUrl}>
      <Box
        ref={cardRef}
        sx={{
          display: "flex",
          flexDirection: "column",
          gap: 2,
          p: 2,
          borderRadius: 3,
          bgcolor: "background.paper",
          // cardBoxShadow selected by theme.palette.mode above (F-2 — no media query).
          boxShadow: cardBoxShadow,
        }}
        data-testid="card-shell"
      >
        {/* 1. Header — title + question subtitle (muted), freshness badge right */}
        <Box sx={{ display: "flex", alignItems: "flex-start", gap: 1 }}>
          <Box sx={{ flex: 1, minWidth: 0 }}>
            <Typography variant="h6" sx={{ lineHeight: 1.2 }} data-testid="card-title">
              {title}
            </Typography>
            {answersQuestion && (
              <Typography
                variant="caption"
                color="text.secondary"
                sx={{ display: "block", mt: 0.25 }}
              >
                {answersQuestion}
              </Typography>
            )}
          </Box>
          {fresh && (
            <Box
              component="span"
              sx={{
                flexShrink: 0,
                fontSize: "0.65rem",
                color: "text.secondary",
                border: `1px solid ${alpha(theme.palette.text.secondary, 0.25)}`,
                borderRadius: 1,
                px: 0.75,
                py: 0.25,
                lineHeight: 1.4,
              }}
              data-testid="card-freshness-badge"
            >
              {fresh}
            </Box>
          )}
        </Box>

        {/* 2. Body — the hero content (owned by the template) */}
        {children}

        {/* 3. Rendered-comment slot — muted cited what+why (AD-9) + source affordance */}
        {renderedComment && (
          <Box
            sx={{
              pt: 1.5,
              borderTop: `1px solid ${alpha(theme.palette.text.primary, 0.08)}`,
            }}
            data-testid="card-comment-slot"
          >
            <Box sx={{ display: "flex", alignItems: "center", mb: 0.5 }}>
              <Typography variant="overline" color="text.secondary" sx={{ lineHeight: 1 }}>
                Commentaire
              </Typography>
              <SourceAffordance meta={meta} />
            </Box>
            <Typography
              variant="body2"
              color="text.secondary"
              component="div"
              sx={{ whiteSpace: "pre-line", lineHeight: 1.6 }}
              data-testid="card-rendered-comment"
            >
              {renderedComment}
            </Typography>
          </Box>
        )}

        {/* 4. Definitions popover (R6) — toggle + per-metric list */}
        {hasDefs && (
          <Box>
            <Box
              component="button"
              onClick={() => setDefsOpen((v) => !v)}
              aria-expanded={defsOpen}
              aria-controls="card-definitions-panel"
              sx={{
                all: "unset",
                cursor: "pointer",
                display: "inline-flex",
                alignItems: "center",
                gap: 0.5,
                color: "text.secondary",
                "&:hover": { color: "text.primary" },
              }}
              data-testid="card-definitions-toggle"
            >
              <Typography variant="caption" sx={{ fontWeight: 500 }}>
                {defsOpen ? "▾" : "▸"} Définitions des métriques
              </Typography>
            </Box>
            <Collapse in={defsOpen} id="card-definitions-panel">
              <Box
                sx={{
                  mt: 1,
                  p: 1.5,
                  borderRadius: 2,
                  border: `1px solid ${alpha(theme.palette.text.primary, 0.08)}`,
                  display: "flex",
                  flexDirection: "column",
                  gap: 1.25,
                }}
                data-testid="card-definitions-panel"
              >
                {Object.entries(metricDefinitions!).map(([metric, def]) => (
                  <Box key={metric}>
                    <Typography
                      variant="overline"
                      color="text.secondary"
                      sx={{ display: "block", lineHeight: 1.3 }}
                    >
                      {metricLabel(metric)}
                      {def.unit ? ` (${def.unit})` : ""}
                    </Typography>
                    <Typography variant="body2" sx={{ lineHeight: 1.5 }}>
                      {def.definition}
                    </Typography>
                    {def.caveats && (
                      <Typography
                        variant="caption"
                        color="text.secondary"
                        sx={{ fontStyle: "italic" }}
                      >
                        {def.caveats}
                      </Typography>
                    )}
                  </Box>
                ))}
              </Box>
            </Collapse>
          </Box>
        )}

        {/* 5. Chrome feedback + export — hairline divider, au-dessus du footer */}
        {feedbackProps && (
          <Box
            sx={{
              pt: 1,
              borderTop: `1px solid ${alpha(theme.palette.text.primary, 0.06)}`,
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              gap: 1,
              flexWrap: "wrap",
            }}
            data-testid="card-chrome-row"
          >
            <CardFeedbackBar {...feedbackProps} />
            <Button
              size="small"
              variant="text"
              onClick={handleExport}
              data-testid="card-export-button"
              aria-label="Exporter la carte"
              sx={{
                minWidth: 0,
                px: 1,
                py: 0.25,
                fontSize: "0.7rem",
                color: "text.secondary",
                flexShrink: 0,
              }}
            >
              ↓ Exporter
            </Button>
          </Box>
        )}

        {/* 6. Slim footer — source system + date range */}
        <Box
          sx={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            pt: 1,
            borderTop: `1px solid ${alpha(theme.palette.text.primary, 0.06)}`,
          }}
          data-testid="card-footer"
        >
          <Typography variant="caption" color="text.secondary">
            {sourceSystem ? `Source : ${sourceSystem}` : "Source : —"}
          </Typography>
          <Typography variant="caption" color="text.secondary">
            {dateRange.start} → {dateRange.end}
          </Typography>
        </Box>
      </Box>
    </WidgetShell>
  );
}
