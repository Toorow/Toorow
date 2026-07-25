/**
 * Canonical envelope `meta` shape (AD-1 / AD-9).
 *
 * Widgets receive this via their `structuredContent` payload from the MCP server.
 * The shell consumes `meta` to render stale badges, alert states, and the
 * auth_expired reconnect affordance (AC5).
 */
export interface WidgetMeta {
  freshness: {
    /** ISO 8601 timestamp of the last successful data pull */
    last_pull: string;
    /** Expected cadence in hours (used to compute staleness) */
    cadence_hours: number;
    /** ISO 8601 timestamp since when data has been stale (absent if fresh) */
    stale_since?: string;
  };
  provenance: {
    /** Upstream system identifier (e.g. "google_workspace", "seeded") */
    source_system: string;
    /** Unique pull job identifier */
    pull_id: string;
  };
  alerts: Array<{
    /**
     * Machine-readable alert code.
     * Known codes:
     *   - "auth_expired" — OAuth token is invalid; show reconnect affordance (AD-15)
     */
    code: string;
    severity: "info" | "warn" | "error";
    message: string;
  }>;
  /**
   * Story 4.6 (AC5): ISO-8601 datetime of the as-of replay boundary.
   * Non-null when get_daily_report was called with as_of parameter.
   * Null / absent for current-view (live) reports.
   */
  as_of?: string | null;
  /**
   * Story 5.5 (AC2): OTel trace_id hex (32 chars) for the current tool call.
   * Null when TRACING_ENABLED=false or no active span.
   * Echoed back to submit_feedback so the Langfuse score links to the same
   * trace (AD-13). Additive under 1.x (AI-31 policy).
   */
  trace_id?: string | null;
  /**
   * Story 7.1 (AC7): the project id this envelope belongs to.
   * Additive meta key (AI-31 policy). Populated by the MCP server envelope
   * builder when a project is resolved. The widget shell reads this to scope
   * callServerTool invocations (e.g. submit_feedback) to the correct project,
   * falling back to feedbackContext.projectId and finally 'default' when absent.
   */
  project_id?: string | null;
  /**
   * Story 23.1 (AC1/AC3): org branding resolved server-side from the project's
   * organization (app.organizations brand_* columns, Story 21.2 / migration 036).
   * Additive meta key (AI-31): ABSENT when the org defined no branding, the
   * project has no org, or resolution failed — the shell then renders the
   * default toorow theme. When present, WidgetShell merges these colors into
   * its single ThemeProvider (AD-11) with a contrast guard.
   */
  branding?: {
    org_id?: string | null;
    brand_primary?: string | null;
    brand_secondary?: string | null;
    brand_accent?: string | null;
    /** Provenance only — never rendered (AD-11/NFR4 bundle rule). */
    logo_url?: string | null;
  };
}

/**
 * Props accepted by the WidgetShell component.
 */
export interface WidgetShellProps {
  /** Canonical envelope meta (optional — widgets without a full envelope render normally) */
  meta?: WidgetMeta;
  /**
   * Deep-link URL to the Connector admin console.
   * Used by the auth_expired reconnect affordance (AD-15).
   * No OAuth flow runs inside the widget iframe.
   */
  adminConsoleUrl?: string;
  children: React.ReactNode;
  /**
   * Story 5.5 (AC6): FeedbackBar context.
   * When provided, the shell renders a FeedbackBar below the widget content.
   * If absent, no FeedbackBar is shown (backward-compatible).
   */
  feedbackContext?: {
    /** Project identifier for feedback attribution. */
    projectId: string;
    /** Report reference, e.g. "get_daily_report:2026-07-11". */
    reportRef: string;
    /** Module name for the feedback row. */
    module: string;
  };
  /**
   * Story 6.6 (AC4): ExportButton context.
   * When provided, the shell renders an "Exporter PNG" button below widget content.
   * The widget passes a ref to its main container + a filename base.
   * If absent, no export button is shown (backward-compatible).
   */
  exportContext?: {
    /** Ref to the DOM element to capture. */
    targetRef: React.RefObject<HTMLElement | null>;
    /** Base filename for the download (without extension). */
    filename: string;
  };
}
