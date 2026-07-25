/**
 * FeedbackBar — 👍/👎 + optional comment + async submit (Story 5.5, AC5, AC6).
 *
 * Rendered by WidgetShell at the bottom of every report widget (shell-level,
 * not per-widget). Calls the core MCP tool `submit_feedback` via the shared
 * `callServerTool` from ./mcpApp (AD-10, AD-2).
 *
 * callServerTool mechanism (Story 9.10 — retires the raw-postMessage-only
 * decision recorded here by Story 5.5):
 *   - SDK path: the official @modelcontextprotocol/ext-apps `App.callServerTool`
 *     — a real awaitable RPC. A rejection (transport failure OR isError tool
 *     result) surfaces the designed error state below (F-11, finally reachable).
 *   - Legacy fallback: raw window.parent.postMessage fire-and-forget with
 *     graceful no-op (Vitest, Storybook, older hosts) — resolves immediately,
 *     so the confirmation stays optimistic on that path, unchanged.
 *
 * UX (review-global-gaps G-09):
 *   - Thumb clicks SET selectedRating (visual selected state via variant
 *     contained/outlined) but do NOT auto-submit.
 *   - "Envoyer" button submits the selectedRating + comment and is disabled
 *     while selectedRating === null.
 *   - One-shot submitted state (same as before).
 *   - Confirmation copy: "Retour envoyé. Merci !"
 *
 * French-first copy (UX-DR10):
 *   - Buttons: "👍 Utile" / "👎 Pas utile"
 *   - Placeholder: "Commentaire optionnel…"
 *   - Submit: "Envoyer"
 *   - Confirmation: "Retour envoyé. Merci !"
 *
 * AC5 / AD-10: widget NEVER writes to Postgres directly — all writes go via
 * callServerTool → submit_feedback MCP tool → core writes to app.feedback.
 *
 * Design (Toorow visual identity):
 *   - Chips (👍/👎): pills with Toorow colors, outlined by default, contained when active
 *   - Submit button: var(--toorow-rose) background, disabled state
 *   - Typography: Plus Jakarta Sans, var() CSS custom properties
 */

import { CSSProperties, useState } from "react";
import { callServerTool } from "./mcpApp";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface FeedbackBarProps {
  /** Project identifier (from envelope data.project_id or context). */
  projectId: string;
  /** OTel trace_id from meta.trace_id. Null when TRACING_ENABLED=false. */
  traceId: string | null;
  /** Report reference string, e.g. "get_daily_report:2026-07-11". */
  reportRef: string;
  /** Module name, e.g. "connector-module-name". */
  module: string;
  /**
   * Story 8.8: compact mode — renders inline in the chrome footer bar.
   * When true: no top border/margin (the footer bar owns the divider).
   * When false/absent: legacy stacked rendering with its own border.
   */
  compact?: boolean;
}

// ---------------------------------------------------------------------------
// Chip component (pill-style with Toorow colors)
// ---------------------------------------------------------------------------

interface ChipProps {
  active: boolean;
  onClick: () => void;
  tone: "up" | "down";
  label: string;
  disabled?: boolean;
  compact?: boolean;
  /** Stable test hook (contract G-09: feedback-thumbs-up / feedback-thumbs-down). */
  testId: string;
}

function Chip({ active, onClick, tone, label, disabled = false, compact = false, testId }: ChipProps) {
  const isSuccess = tone === "up";
  const accent = isSuccess ? "var(--success, #10B981)" : "var(--error, #EF4444)";
  const accentSoft = isSuccess
    ? "var(--success-soft, #CDEBDD)"
    : "var(--error-soft, #FFD6DB)";
  const textOnAccent = isSuccess
    ? "var(--text-on-success-soft, #20603F)"
    : "var(--text-on-error-soft, #8A2B33)";
  const textSecondary = "var(--text-secondary, #6B7280)";
  const borderDefault = "var(--border-default, #D7DAE0)";
  const neutral400 = "var(--neutral-400, #9CA3AF)";
  const padding = compact ? "3px 10px" : "5px 14px";

  const [borderColor, setBorderColor] = useState(
    active ? accent : borderDefault
  );

  const style: CSSProperties = {
    display: "inline-flex",
    alignItems: "center",
    gap: 6,
    cursor: disabled ? "not-allowed" : "pointer",
    fontFamily: "var(--font-primary, 'Plus Jakarta Sans', sans-serif)",
    fontSize: "var(--fs-caption, 13px)",
    fontWeight: 500,
    lineHeight: 1,
    color: active ? textOnAccent : textSecondary,
    background: active ? accentSoft : "var(--surface-card, transparent)",
    border: `1px solid ${borderColor}`,
    borderRadius: "999px",
    padding,
    opacity: disabled ? 0.5 : 1,
    transition:
      "background var(--dur-fast, 150ms) var(--ease-out, cubic-bezier(0.4, 0, 0.2, 1)), " +
      "border-color var(--dur-fast, 150ms) var(--ease-out, cubic-bezier(0.4, 0, 0.2, 1)), " +
      "color var(--dur-fast, 150ms) var(--ease-out, cubic-bezier(0.4, 0, 0.2, 1))",
  };

  const handleMouseEnter = () => {
    if (!active && !disabled) {
      setBorderColor(neutral400);
    }
  };

  const handleMouseLeave = () => {
    setBorderColor(active ? accent : borderDefault);
  };

  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-pressed={active}
      aria-label={label}
      data-testid={testId}
      style={style}
      onMouseEnter={handleMouseEnter}
      onMouseLeave={handleMouseLeave}
    >
      <span>{tone === "up" ? "👍" : "👎"}</span>
      {label}
    </button>
  );
}

// ---------------------------------------------------------------------------
// FeedbackBar component
// ---------------------------------------------------------------------------

export default function FeedbackBar({
  projectId,
  traceId,
  reportRef,
  module,
  compact = false,
}: FeedbackBarProps) {
  const [submitting, setSubmitting] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [submitError, setSubmitError] = useState(false);
  const [comment, setComment] = useState("");
  // G-09: selectedRating drives both visual state and Envoyer disabled state.
  const [selectedRating, setSelectedRating] = useState<1 | -1 | null>(null);

  async function handleSubmit() {
    if (submitting || submitted || selectedRating === null) return;
    setSubmitting(true);
    setSubmitError(false);

    try {
      // Story 9.10: callServerTool is the shared helper (./mcpApp). SDK path:
      // real awaitable RPC — a rejection means the host/server refused the
      // submission and shows the designed error state (F-11, now reachable).
      // Legacy path: fire-and-forget postMessage that resolves immediately —
      // the confirmation stays optimistic there, unchanged.
      await callServerTool("submit_feedback", {
        project_id: projectId,
        rating: selectedRating,
        trace_id: traceId ?? null,
        comment: comment.trim() || "",
        report_ref: reportRef,
        module,
      });
      setSubmitted(true);
    } catch {
      setSubmitError(true);
    } finally {
      setSubmitting(false);
    }
  }

  const containerStyle: CSSProperties = {
    fontFamily: "var(--font-primary, 'Plus Jakarta Sans', sans-serif)",
    ...(compact
      ? {}
      : {
          borderTop: "1px solid",
          borderColor: "var(--border-subtle, #E5E7EB)",
          paddingTop: 12,
          marginTop: 12,
        }),
    display: "flex",
    flexDirection: "column",
    gap: 8,
  };

  const rowStyle: CSSProperties = {
    display: "flex",
    alignItems: "center",
    gap: 12,
    flexWrap: "wrap",
  };

  const questionStyle: CSSProperties = {
    fontSize: "var(--fs-caption, 13px)",
    color: "var(--text-tertiary, #9CA3AF)",
  };

  const chipsContainerStyle: CSSProperties = {
    display: "flex",
    gap: 6,
  };

  const commentRowStyle: CSSProperties = {
    display: "flex",
    gap: 8,
    alignItems: "flex-start",
    flexWrap: "wrap",
  };

  const commentInputStyle: CSSProperties = {
    flex: 1,
    minWidth: 180,
    fontFamily: "var(--font-primary, 'Plus Jakarta Sans', sans-serif)",
    fontSize: "var(--fs-caption, 13px)",
    color: "var(--text-primary, #1F2937)",
    background: "var(--surface-card, #FFFFFF)",
    border: "1px solid var(--border-default, #D7DAE0)",
    borderRadius: "var(--radius-md, 8px)",
    padding: "7px 12px",
    outline: "none",
    transition:
      "box-shadow var(--dur-fast, 150ms) var(--ease-out, cubic-bezier(0.4, 0, 0.2, 1))",
  };

  const submitButtonStyle: CSSProperties = {
    fontFamily: "var(--font-primary, 'Plus Jakarta Sans', sans-serif)",
    fontSize: "var(--fs-caption, 13px)",
    fontWeight: 600,
    lineHeight: 1,
    color: "var(--text-on-accent, #FFFFFF)",
    background:
      selectedRating === null || submitting
        ? "var(--toorow-rose, #E11D48)"
        : "var(--toorow-rose, #E11D48)",
    border: "none",
    borderRadius: "999px",
    padding: "7px 15px",
    cursor: selectedRating === null || submitting ? "not-allowed" : "pointer",
    opacity: selectedRating === null || submitting ? 0.5 : 1,
    transition:
      "background var(--dur-fast, 150ms) var(--ease-out, cubic-bezier(0.4, 0, 0.2, 1))",
  };

  const errorTextStyle: CSSProperties = {
    display: "inline-flex",
    alignItems: "center",
    gap: 7,
    fontFamily: "var(--font-primary, 'Plus Jakarta Sans', sans-serif)",
    fontSize: "var(--fs-caption, 13px)",
    fontWeight: 500,
    color: "var(--error, #EF4444)",
    padding: compact ? "4px 0" : "10px 0",
  };

  const confirmationStyle: CSSProperties = {
    display: "inline-flex",
    alignItems: "center",
    gap: 7,
    fontFamily: "var(--font-primary, 'Plus Jakarta Sans', sans-serif)",
    fontSize: "var(--fs-caption, 13px)",
    fontWeight: 500,
    color: "var(--success, #10B981)",
    padding: compact ? "4px 0" : "10px 0",
  };

  // F-11 (Story 9.10): designed error state — reachable on the SDK path when
  // callServerTool rejects (transport failure or isError tool result).
  if (submitError) {
    return (
      <div
        style={{
          ...containerStyle,
          ...(compact ? {} : { borderTop: "1px solid var(--border-subtle, #E5E7EB)", paddingTop: 12 }),
        }}
        data-testid="feedback-error"
      >
        <div style={errorTextStyle}>
          <span>Erreur lors de l'envoi. Veuillez réessayer.</span>
          <button
            type="button"
            onClick={() => setSubmitError(false)}
            data-testid="feedback-retry"
            style={{
              fontFamily: "var(--font-primary, 'Plus Jakarta Sans', sans-serif)",
              fontSize: "var(--fs-caption, 13px)",
              background: "none",
              border: "none",
              color: "var(--error, #EF4444)",
              cursor: "pointer",
              textDecoration: "underline",
              padding: 0,
            }}
          >
            Réessayer
          </button>
        </div>
      </div>
    );
  }

  // Show confirmation after submission
  if (submitted) {
    return (
      <div
        style={{
          ...containerStyle,
          ...(compact ? {} : { borderTop: "1px solid var(--border-subtle, #E5E7EB)", paddingTop: 12 }),
        }}
        data-testid="feedback-confirmation"
      >
        <div style={confirmationStyle}>
          <svg
            width="14"
            height="14"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden="true"
            style={{ flexShrink: 0 }}
          >
            <path d="M20 6 9 17l-5-5" />
          </svg>
          Retour envoyé. Merci !
        </div>
      </div>
    );
  }

  return (
    <div style={containerStyle} data-testid="feedback-bar">
      {/* Question + 👍/👎 chips */}
      <div style={rowStyle}>
        <span style={questionStyle}>Ce rapport vous a-t-il été utile ?</span>
        <div style={chipsContainerStyle}>
          <Chip
            tone="up"
            active={selectedRating === 1}
            onClick={() => setSelectedRating(1)}
            label="Utile"
            disabled={submitting}
            compact={compact}
            testId="feedback-thumbs-up"
          />
          <Chip
            tone="down"
            active={selectedRating === -1}
            onClick={() => setSelectedRating(-1)}
            label="Pas utile"
            disabled={submitting}
            compact={compact}
            testId="feedback-thumbs-down"
          />
        </div>
      </div>

      {/* Optional comment field + submit button.
          Contract G-09: ALWAYS rendered — Envoyer is DISABLED (not hidden)
          while no rating is selected, so the affordance is discoverable. */}
      <div style={commentRowStyle}>
          <input
            type="text"
            placeholder="Commentaire optionnel…"
            value={comment}
            onChange={(e) => setComment(e.target.value)}
            disabled={submitting}
            data-testid="feedback-comment"
            style={{
              ...commentInputStyle,
              ...(submitError ? { borderColor: "var(--error, #EF4444)" } : {}),
            }}
            onFocus={(e) => {
              (e.currentTarget as HTMLInputElement).style.boxShadow =
                "var(--shadow-focus, 0 0 0 3px rgba(225, 29, 72, 0.1))";
            }}
            onBlur={(e) => {
              (e.currentTarget as HTMLInputElement).style.boxShadow = "none";
            }}
          />
          <button
            type="button"
            onClick={() => {
              void handleSubmit();
            }}
            disabled={submitting || selectedRating === null}
            data-testid="feedback-submit"
            style={submitButtonStyle}
            onMouseEnter={(e) => {
              if (selectedRating !== null && !submitting) {
                (e.currentTarget as HTMLButtonElement).style.background =
                  "var(--toorow-rose-hover, #BE123C)";
              }
            }}
            onMouseLeave={(e) => {
              (e.currentTarget as HTMLButtonElement).style.background =
                "var(--toorow-rose, #E11D48)";
            }}
          >
            Envoyer
          </button>
        </div>
    </div>
  );
}
