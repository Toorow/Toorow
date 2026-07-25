/**
 * @toorow/shell — public API.
 *
 * Widgets import from this module at build time (Vite resolves the workspace
 * package via the pnpm workspace; @toorow/shell → ui/shell/src/index.ts).
 *
 * Exports:
 *   - WidgetShell  (default) — the shared wrapper component (ThemeProvider + fonts + meta)
 *   - WidgetMeta             — TypeScript type for the canonical envelope meta field
 *   - WidgetShellProps       — TypeScript type for WidgetShell's props contract
 */

export { default } from "./WidgetShell";
export { default as WidgetShell } from "./WidgetShell";
export { default as FeedbackBar } from "./FeedbackBar";
export { default as ExportButton } from "./ExportButton";
// Story 23.1 — org branding theme derivation (single injection point, AD-11).
// Exported for card/widget tests proving the branded chain end-to-end; widgets
// themselves still NEVER call createTheme (they mount WidgetShell).
export { connectorTheme, resolveWidgetTheme, ensureContrast } from "./theme";
export type { OrgBranding } from "./theme";
export type { WidgetMeta, WidgetShellProps } from "./types";
export type { FeedbackBarProps } from "./FeedbackBar";
export type { ExportButtonProps } from "./ExportButton";

// Story 9.10 — shared MCP Apps channel (SDK-first, legacy fallback). Owned
// here; @toorow/card-shell re-exports it so cards import from one place.
export {
  connectMcpApp,
  useMcpApp,
  callServerTool,
  readInjectedEnvelope,
  __resetMcpAppForTests,
} from "./mcpApp";
export type {
  McpAppHandle,
  McpChannelMode,
  McpSdkApp,
  McpToolResultParams,
  ConnectMcpAppOptions,
  EnvelopeCallback,
} from "./mcpApp";

// Import fonts.css here so that any direct import of @toorow/shell (not via
// WidgetShell) still triggers font inlining (belt-and-suspenders).
import "./fonts.css";
