/**
 * ExportButton — one-click PNG export of any widget DOM node (Story 6.6, AC4).
 *
 * Shell-level component (same pattern as FeedbackBar from Story 5.5): rendered
 * below widget content, not per-widget code. Widgets pass a `targetRef` and
 * `filename` to the shell; the shell renders this button.
 *
 * Implementation approach (Dev Agent Record):
 *   Uses `html-to-image` npm package (toPng function) because it works on both
 *   DOM nodes and SVG elements without external URL fetches (AD-11 compliant:
 *   fonts are inlined in fonts.css). The `cacheBust: true` option avoids CORS
 *   issues with cached data URIs.
 *
 * Bundle gate: html-to-image adds ~100KB uncompressed to the bundle. The GA
 * widget baseline is ~533KB, well under the 1.5MB gate (AI-04). See Dev Agent
 * Record for measured delta.
 *
 * Fallback (if html-to-image is not available or toPng fails): graceful no-op
 * — error is caught and swallowed, button remains clickable.
 *
 * French-first copy (UX-DR10): "Exporter PNG"
 * AD-11: no external http(s) references in output.
 */

import { useState } from "react";
import { toPng } from "html-to-image";
import Box from "@mui/material/Box";

export interface ExportButtonProps {
  /** Ref to the DOM element to capture (the widget's main container). */
  targetRef: React.RefObject<HTMLElement | null>;
  /** Base filename for the download (without extension). */
  filename: string;
  /**
   * Story 8.8: compact mode — renders inline in the chrome footer bar.
   * When true: no top border/margin (the footer bar owns the divider).
   * When false/absent: legacy stacked rendering with its own border.
   */
  compact?: boolean;
}

export default function ExportButton({ targetRef, filename, compact = false }: ExportButtonProps) {
  const [state, setState] = useState<"idle" | "working" | "done">("idle");

  const handleExport = async () => {
    if (!targetRef.current || state === "working") return;
    setState("working");
    try {
      const dataUrl = await toPng(targetRef.current, { cacheBust: true });
      const link = document.createElement("a");
      link.download = `${filename}.png`;
      link.href = dataUrl;
      link.click();
      setState("done");
      setTimeout(() => setState("idle"), 1600);
    } catch {
      // Graceful no-op — do not surface errors to the user.
      // Common causes: cross-origin iframe, browser security policy.
      setState("idle");
    }
  };

  const label = state === "working" ? "Export…" : state === "done" ? "Exporté ✓" : compact ? "↓ PNG" : "↓ Exporter PNG";
  const isSuccess = state === "done";

  return (
    <Box
      sx={
        compact
          ? { display: "flex", justifyContent: "flex-end" }
          : {
              display: "flex",
              justifyContent: "flex-end",
              borderTop: "1px solid var(--border-subtle)",
              paddingTop: "10px",
              marginTop: "10px",
            }
      }
      data-testid="export-button-container"
    >
      <button
        type="button"
        disabled={state === "working"}
        onClick={handleExport}
        data-testid="export-png-button"
        aria-label="Exporter en PNG"
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: "6px",
          fontFamily: "var(--font-primary)",
          fontSize: "var(--fs-caption)",
          fontWeight: 600,
          color: "var(--text-on-accent)",
          background: isSuccess ? "var(--success)" : "var(--toorow-rose)",
          border: "none",
          borderRadius: "999px",
          padding: "7px 16px",
          cursor: state === "working" ? "not-allowed" : "pointer",
          transition: "background var(--dur-fast) var(--ease-out), transform var(--dur-fast) var(--ease-out)",
          opacity: state === "working" ? 0.7 : 1,
        }}
        onMouseDown={(e) => {
          if (state === "idle") {
            (e.currentTarget as HTMLButtonElement).style.transform = "scale(0.97)";
          }
        }}
        onMouseUp={(e) => {
          (e.currentTarget as HTMLButtonElement).style.transform = "scale(1)";
        }}
        onMouseLeave={(e) => {
          (e.currentTarget as HTMLButtonElement).style.transform = "scale(1)";
        }}
        onMouseEnter={(e) => {
          if (state === "idle" && !isSuccess) {
            (e.currentTarget as HTMLButtonElement).style.background = "var(--toorow-rose-hover)";
          }
        }}
      >
        <svg
          width="14"
          height="14"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2.2"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
          <polyline points="7 10 12 15 17 10" />
          <line x1="12" y1="15" x2="12" y2="3" />
        </svg>
        {label}
      </button>
    </Box>
  );
}
