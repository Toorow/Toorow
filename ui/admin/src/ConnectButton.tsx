/**
 * ConnectButton — "Connecter une source" OAuth flow trigger (Story 2.4, AC3, T4).
 *
 * Nango SDK decision (recorded per Dev Notes / T4.1):
 *   @nangohq/frontend is the official Nango browser SDK. It handles the OAuth
 *   popup/redirect lifecycle cleanly in a normal browser tab (not just iframes).
 *   We construct the Nango auth URL manually and open a popup, then listen for
 *   the postMessage callback from Nango. This avoids the @nangohq/frontend npm
 *   package (which may add bundle weight and has evolving APIs) while replicating
 *   its core behaviour. The Nango auth URL pattern:
 *     ${VITE_NANGO_BASE_URL}/oauth/connect/<integration-key>?
 *       connection_id=<uuid>&public_key=<public_key>
 *   On success, we call POST /api/connections with the nango_connection_id.
 *
 * AD-15: OAuth runs in the admin console browser tab only — never inside a
 * Claude chat iframe.
 * AD-3: no tokens are stored; only nango_connection_id is passed to the server.
 * UX-DR10: French-first copy with proper accents.
 *
 * review-global-gaps G-08: projectId prop + MUI Snackbar (no window.alert).
 * Epic 8 polish: the button is PROVIDER-AGNOSTIC — a menu lists the available
 * modules (fed by /api/modules/available, fallback to the known four) instead
 * of hardcoding Google Analytics. AD-2 spirit in the UI.
 */
import { useEffect, useState } from "react";
import {
  Alert,
  Button,
  CircularProgress,
  ListItemText,
  Menu,
  MenuItem,
  Snackbar,
} from "@mui/material";

interface ConnectButtonProps {
  onSuccess: () => void;
  /** Active project ID — used in the POST /api/connections body. Defaults to "default". */
  projectId?: string;
}

const NANGO_PUBLIC_KEY = import.meta.env.VITE_NANGO_PUBLIC_KEY as string | undefined;
const NANGO_BASE_URL =
  (import.meta.env.VITE_NANGO_BASE_URL as string | undefined) ?? "http://localhost:3003";

/** Fallback list when /api/modules/available is unreachable. */
const FALLBACK_PROVIDERS = [
  { name: "google-analytics", display_name: "Google Analytics 4" },
  { name: "meta-ads", display_name: "Meta Ads" },
  { name: "gsc", display_name: "Google Search Console" },
  { name: "github", display_name: "GitHub" },
];

interface ProviderOption {
  name: string;
  display_name: string;
}

function generateConnectionId(provider: string): string {
  const prefix = provider.slice(0, 8);
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return `${prefix}-${crypto.randomUUID()}`;
  }
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export default function ConnectButton({ onSuccess, projectId }: ConnectButtonProps) {
  const [loading, setLoading] = useState(false);
  const [providers, setProviders] = useState<ProviderOption[]>(FALLBACK_PROVIDERS);
  const [menuAnchor, setMenuAnchor] = useState<HTMLElement | null>(null);
  const [snackbar, setSnackbar] = useState<{
    open: boolean;
    severity: "error" | "warning";
    message: string;
  }>({ open: false, severity: "error", message: "" });

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const resp = await fetch("/api/modules/available");
        if (!resp.ok) return;
        const body = await resp.json();
        const list = (Array.isArray(body) ? body : (body.modules ?? []))
          .map((m: { name?: string; display_name?: string }) => ({
            name: m.name ?? "",
            display_name: m.display_name ?? m.name ?? "",
          }))
          .filter((m: ProviderOption) => m.name);
        if (!cancelled && list.length > 0) setProviders(list);
      } catch {
        /* fallback list stays */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  function showSnackbar(severity: "error" | "warning", message: string) {
    setSnackbar({ open: true, severity, message });
  }

  function handleCloseSnackbar() {
    setSnackbar((prev) => ({ ...prev, open: false }));
  }

  async function handleConnect(provider: string) {
    setMenuAnchor(null);
    setLoading(true);
    try {
      const connectionId = generateConnectionId(provider);

      // Build the Nango auth URL
      const params = new URLSearchParams({
        connection_id: connectionId,
        ...(NANGO_PUBLIC_KEY ? { public_key: NANGO_PUBLIC_KEY } : {}),
      });
      const authUrl = `${NANGO_BASE_URL}/oauth/connect/${provider}?${params.toString()}`;

      // Open OAuth popup (AD-15: runs in admin tab, never in iframe)
      const popup = window.open(
        authUrl,
        "nango-oauth",
        "width=600,height=700,scrollbars=yes,resizable=yes"
      );

      if (!popup) {
        showSnackbar(
          "warning",
          "Popup window was blocked by your browser. Please allow popups for this page."
        );
        setLoading(false);
        return;
      }

      // Poll until popup closes (Nango redirects back to its own callback)
      await new Promise<void>((resolve) => {
        const interval = setInterval(() => {
          if (popup.closed) {
            clearInterval(interval);
            resolve();
          }
        }, 500);
      });

      // After popup closes, register the connection on the server (AC5)
      const resp = await fetch("/api/connections", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          nango_connection_id: connectionId,
          provider,
          project_id: projectId ?? "default",
        }),
      });

      if (!resp.ok) {
        const body = await resp.text();
        throw new Error(`POST /api/connections failed: HTTP ${resp.status} — ${body}`);
      }

      // Trigger list refresh
      onSuccess();
    } catch (err) {
      console.error("ConnectButton error:", err);
      showSnackbar(
        "error",
        `Connection failed: ${err instanceof Error ? err.message : String(err)}`
      );
    } finally {
      setLoading(false);
    }
  }

  return (
    <>
      <Button
        variant="contained"
        color="primary"
        onClick={(e) => setMenuAnchor(e.currentTarget)}
        disabled={loading}
        startIcon={loading ? <CircularProgress size={16} color="inherit" /> : null}
        data-testid="connect-source-button"
      >
        {loading ? "Connecting..." : "Connect Source"}
      </Button>
      <Menu
        anchorEl={menuAnchor}
        open={Boolean(menuAnchor)}
        onClose={() => setMenuAnchor(null)}
      >
        {providers.map((p) => (
          <MenuItem
            key={p.name}
            onClick={() => {
              void handleConnect(p.name);
            }}
            data-testid={`connect-provider-${p.name}`}
          >
            <ListItemText primary={p.display_name} secondary={p.name} />
          </MenuItem>
        ))}
      </Menu>

      <Snackbar
        open={snackbar.open}
        autoHideDuration={6000}
        onClose={handleCloseSnackbar}
        anchorOrigin={{ vertical: "bottom", horizontal: "center" }}
      >
        <Alert
          onClose={handleCloseSnackbar}
          severity={snackbar.severity}
          variant="filled"
          sx={{ width: "100%" }}
        >
          {snackbar.message}
        </Alert>
      </Snackbar>
    </>
  );
}
