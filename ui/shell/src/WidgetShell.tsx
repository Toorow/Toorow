/**
 * WidgetShell — shared wrapper for all Connector widgets (Story 8.8 redesign).
 *
 * Provides:
 *   1. MUI ThemeProvider with connectorTheme (Plus Jakarta Sans Variable via tokens)
 *   2. Automatic light/dark re-theming via hostContext CSS vars (AC2 / UX-DR5)
 *   3. Stale badge + alert rendering from meta envelope (AC5 / AD-9)
 *   4. auth_expired reconnect affordance (AC5 / AD-15)
 *   5. French-first copy for all shell-managed strings (AC3 / UX-DR10)
 *
 * Story 8.8 chrome redesign (6 rules):
 *   - FeedbackBar and ExportButton are now integrated into a SLIM FOOTER BAR:
 *     feedback left, export right, hairline top divider.
 *     They are no longer rendered as stacked boxes below the widget content.
 *   - as-of banner rendered as a compact info bar (not a heavy Alert).
 *
 * hostContext binding (T5.3): MutationObserver on document.documentElement
 * watching for `data-color-scheme` attribute changes + MCP Apps SDK 1.7
 * `mcp:hostContextChange` event. Both drive MUI's useColorScheme() hook.
 */

import { useEffect, useMemo } from "react";
import { ThemeProvider, CssBaseline } from "@mui/material";
import { useColorScheme } from "@mui/material/styles";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import { resolveWidgetTheme } from "./theme";
import FeedbackBar from "./FeedbackBar";
import ExportButton from "./ExportButton";
import type { WidgetShellProps } from "./types";

// Import fonts so Vite + vite-plugin-singlefile inlines them into the bundle.
// Story 8.8: switched to Plus Jakarta Sans Variable (toorow design system face).
// AD-11 / NFR4 / UX-DR9.
import "./fonts.css";

/**
 * Inner component that reads/writes the MUI color scheme.
 * Must be rendered inside ThemeProvider to access useColorScheme().
 */
function HostContextBridge({ children, meta, adminConsoleUrl, feedbackContext, exportContext }: WidgetShellProps) {
  const { setColorScheme } = useColorScheme();

  useEffect(() => {
    /**
     * Sync MUI color scheme with the host's data-color-scheme attribute.
     * Re-entry guard: MUI itself writes data-color-scheme back to <html>
     * after setColorScheme(), which re-fires the observer. Track the last
     * applied value and skip no-op applications.
     */
    let lastApplied: string | null = null;
    function applyColorScheme(scheme: string) {
      if ((scheme === "dark" || scheme === "light") && scheme !== lastApplied) {
        lastApplied = scheme;
        setColorScheme(scheme);
      }
    }

    // Read initial value from data-color-scheme attribute.
    const initial = document.documentElement.getAttribute("data-color-scheme");
    if (initial) applyColorScheme(initial);

    // Watch for attribute changes (host theme switch).
    const observer = new MutationObserver((mutations) => {
      for (const mutation of mutations) {
        if (
          mutation.type === "attributes" &&
          mutation.attributeName === "data-color-scheme"
        ) {
          const val = document.documentElement.getAttribute("data-color-scheme");
          if (val) applyColorScheme(val);
        }
      }
    });
    observer.observe(document.documentElement, { attributes: true });

    // MCP Apps ext-apps SDK 1.7 event interface.
    function onMcpHostContext(e: Event) {
      const detail = (e as CustomEvent).detail;
      if (detail?.colorScheme) applyColorScheme(detail.colorScheme);
    }
    window.addEventListener("mcp:hostContextChange", onMcpHostContext);

    return () => {
      observer.disconnect();
      window.removeEventListener("mcp:hostContextChange", onMcpHostContext);
    };
  }, [setColorScheme]);

  // Check for auth_expired alert (AD-15).
  const authExpiredAlert = meta?.alerts?.find((a) => a.code === "auth_expired");

  // Other error-severity alerts (not auth_expired).
  const errorAlerts = meta?.alerts?.filter(
    (a) => a.severity === "error" && a.code !== "auth_expired"
  ) ?? [];

  // Story 4.6 (AC5): as-of archive banner — French copy (UX-DR10).
  const asOfDisplay = (() => {
    if (!meta?.as_of) return null;
    try {
      const d = new Date(meta.as_of);
      const pad = (n: number) => String(n).padStart(2, "0");
      return (
        `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}` +
        ` ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())} UTC`
      );
    } catch {
      return meta.as_of;
    }
  })();

  // Whether the slim footer bar should render.
  const hasFooter = !!(feedbackContext || exportContext);

  return (
    <Box
      sx={{
        // AD-11 / NFR11: transparent background so Claude's host UI shows through.
        background: "transparent",
        width: "100%",
        display: "flex",
        flexDirection: "column",
      }}
    >
      {/* Story 4.6 (AC5) — as-of archive banner (UX-DR10 French-first) */}
      {asOfDisplay && (
        <Box sx={{ mb: 1 }}>
          <Alert severity="info">
            Vue archivée — données au {asOfDisplay}
          </Alert>
        </Box>
      )}

      {/* Stale badge (AC5 / AD-9) — French copy (AC3 / UX-DR10) */}
      {meta?.freshness?.stale_since && (
        <Box sx={{ mb: 1 }}>
          <Chip
            label={`Données périmées depuis ${meta.freshness.stale_since}`}
            color="warning"
            size="small"
          />
        </Box>
      )}

      {/* auth_expired reconnect affordance (AC5 / AD-15) */}
      {authExpiredAlert && (
        <Box sx={{ mb: 1 }}>
          <Alert
            severity="warning"
            action={
              <Button
                size="small"
                color="inherit"
                onClick={() => {
                  if (adminConsoleUrl) {
                    window.open(adminConsoleUrl, "_blank");
                  }
                }}
              >
                Reconnecter
              </Button>
            }
          >
            Connexion expirée — cliquez pour reconnecter
          </Alert>
        </Box>
      )}

      {/* Other error-severity alerts (AC5) */}
      {errorAlerts.map((alert, i) => (
        <Box key={`${alert.code}-${i}`} sx={{ mb: 1 }}>
          <Alert severity="error">
            Une erreur est survenue : {alert.message}
          </Alert>
        </Box>
      ))}

      {/* Widget-specific content */}
      {children}

      {/*
       * Story 8.8: Integrated slim chrome footer bar.
       * Layout: feedback (left) | spacer | export (right)
       * Hairline top divider separating footer from content.
       * Rendered only when feedbackContext or exportContext is provided.
       */}
      {hasFooter && (
        <Box
          sx={{
            mt: 2,
            pt: 1.5,
            borderTop: "1px solid",
            borderColor: "divider",
            display: "flex",
            alignItems: "flex-start",
            gap: 1,
          }}
          data-testid="widget-chrome-footer"
        >
          {/* Left: FeedbackBar (inline, no stacked box styling of its own) */}
          {feedbackContext && (
            <Box sx={{ flex: 1, minWidth: 0 }}>
              <FeedbackBar
                projectId={meta?.project_id ?? feedbackContext.projectId ?? "default"}
                traceId={(meta as unknown as { trace_id?: string | null })?.trace_id ?? null}
                reportRef={feedbackContext.reportRef}
                module={feedbackContext.module}
                compact
              />
            </Box>
          )}

          {/* Right: ExportButton (inline, no stacked box styling of its own) */}
          {exportContext && (
            <Box sx={{ flexShrink: 0, display: "flex", alignItems: "center" }}>
              <ExportButton
                targetRef={exportContext.targetRef}
                filename={exportContext.filename}
                compact
              />
            </Box>
          )}
        </Box>
      )}

      {/* Fallback: old stacked rendering when footer is absent but components given alone.
          This maintains backward compat for callers that don't use the footer pattern.
          In practice after 8.8 all callers use the footer, but no crash if not. */}
    </Box>
  );
}

/**
 * WidgetShell — mount this as the root of every Connector widget.
 *
 * @example
 * ```tsx
 * <WidgetShell meta={structuredContent.meta} adminConsoleUrl="https://...">
 *   <MyWidget data={structuredContent.data} />
 * </WidgetShell>
 * ```
 */
export default function WidgetShell(props: WidgetShellProps) {
  /**
   * Story 23.1 (AC2/AC3): the SINGLE theme injection point (AD-11).
   * Without meta.branding this is `connectorTheme` by reference (bit-identical);
   * with branding, a memoized branded theme (resolveWidgetTheme caches on the
   * 3 color values — no createTheme per re-render).
   */
  const branding = props.meta?.branding;
  const theme = useMemo(
    () => resolveWidgetTheme(branding),
    [branding?.brand_primary, branding?.brand_secondary, branding?.brand_accent],
  );
  return (
    <ThemeProvider theme={theme}>
      {/* CssBaseline normalises browser defaults (sets body bg transparent per theme) */}
      <CssBaseline />
      <HostContextBridge {...props} />
    </ThemeProvider>
  );
}
