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
 *   - the theme hooks, the colour helpers and the DOM primitives that replaced
 *     `@mui/material` on 2026-08-05 — cards and widgets import them FROM HERE,
 *     which is what makes "one theme for the platform" a fact rather than a rule.
 */

export { default } from "./WidgetShell";
export { default as WidgetShell } from "./WidgetShell";
export { default as FeedbackBar } from "./FeedbackBar";
export { default as ExportButton } from "./ExportButton";
// Story 23.1 — org branding theme derivation (single injection point, AD-11).
// Exported for card/widget tests proving the branded chain end-to-end; widgets
// themselves still NEVER build a theme (they mount WidgetShell).
export { connectorTheme, resolveWidgetTheme, ensureContrast, createTheme } from "./theme";
export type {
  CreateThemeOptions,
  OrgBranding,
  ColorScheme,
  PaletteColor,
  WidgetPalette,
  WidgetTheme,
} from "./theme";

// The theme context — `useTheme()` returns the theme with `palette` already
// resolved to the host's active colour scheme.
export { ThemeProvider, useTheme, useColorScheme } from "./themeContext";

// Colour math (was `@mui/material/styles`): pure functions over a colour string.
export {
  alpha,
  darken,
  lighten,
  getContrastRatio,
  getLuminance,
  decomposeColor,
  hexToRgb,
} from "./color";

// DOM primitives (was `@mui/material/*`).
export {
  Box,
  Typography,
  Stack,
  Button,
  IconButton,
  Chip,
  Alert,
  ToggleButton,
  ToggleButtonGroup,
  Collapse,
  Table,
  TableHead,
  TableBody,
  TableRow,
  TableCell,
  Dialog,
  DialogTitle,
  DialogContent,
  TextField,
} from "./primitives";
export type {
  BoxProps,
  TypographyProps,
  TypographyVariant,
  StackProps,
  ButtonProps,
  ChipProps,
  AlertProps,
  ToggleButtonProps,
  ToggleButtonGroupProps,
  CollapseProps,
  TableProps,
  TableCellProps,
  DialogContentProps,
  DialogProps,
  TextFieldProps,
} from "./primitives";
export { resolveSx, resolveColor, __resetSxStylesForTests } from "./sx";
export type { Sx } from "./sx";
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
