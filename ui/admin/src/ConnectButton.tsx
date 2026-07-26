/**
 * ConnectButton — "Connect a source" OAuth flow trigger (Story 2.4, AC3, T4).
 *
 * Nango SDK decision (recorded per Dev Notes / T4.1):
 *   @nangohq/frontend is the official Nango browser SDK. We replicate its core
 *   behaviour without the npm dependency: build the Nango auth URL, open a
 *   popup, and LISTEN FOR THE postMessage CALLBACK Nango emits on its own
 *   origin. The Nango auth URL pattern:
 *     ${VITE_NANGO_BASE_URL}/oauth/connect/<integration-key>?
 *       connection_id=<uuid>&public_key=<public_key>
 *   Only on an authorization-succeeded message do we call POST /api/connections
 *   with the nango_connection_id.
 *
 * WHY THAT MATTERS (the defect this file used to carry):
 *   Success used to be inferred from the POPUP CLOSING. Cancelling the consent
 *   screen — or the provider refusing — closed the popup exactly like a success
 *   did, so the console registered a connection and told the user their source
 *   was connected. Popup closure is now what it actually is: an ABSENCE of an
 *   answer, reported as "authorization was not completed". Nothing is POSTed.
 *
 *   Two sibling fictions are gone with it: the four hard-coded FALLBACK_PROVIDERS
 *   (GA4 / Meta / GSC / GitHub offered even with no module installed, and kept
 *   when the API legitimately answered "none"), and the
 *   `?? "http://localhost:3003"` Nango base URL that shipped to production —
 *   ui/admin/.env sets no VITE_NANGO_BASE_URL, so every production click opened
 *   a popup at localhost. With no configured base URL the flow now refuses to
 *   start and says so.
 *
 * AD-15: OAuth runs in the admin console browser tab only — never inside a
 * Claude chat iframe.
 * AD-3: no tokens are stored; only nango_connection_id is passed to the server.
 * AD-2: the provider list is DATA (/api/modules/available), never hardcoded.
 *
 * review-global-gaps G-08: projectId prop + MUI Snackbar (no window.alert).
 */
import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  Button,
  CircularProgress,
  ListItemText,
  Menu,
  MenuItem,
  Snackbar,
} from "@mui/material";
import { apiFetch } from "./lib/apiFetch";

interface ConnectButtonProps {
  onSuccess: () => void;
  /** Active project ID — used in the POST /api/connections body. */
  projectId?: string;
  /** Re-consent one existing Nango connection instead of creating a new connection_ref. */
  fixedProvider?: string;
  nangoConnectionId?: string;
  /** Providers handled by a dedicated server path stay out of this menu. */
  excludeProviders?: string[];
  label?: string;
}

/** The Nango server base URL, or null when the build carries no configuration.
 *  Read at call time (not module scope) so it is honestly re-evaluated and can
 *  be exercised in tests. NO localhost default: an unconfigured build must fail
 *  loudly, not silently point production at a developer machine. */
function nangoBaseUrl(): string | null {
  const raw = (import.meta.env.VITE_NANGO_BASE_URL as string | undefined)?.trim();
  return raw ? raw.replace(/\/+$/, "") : null;
}

function nangoPublicKey(): string | undefined {
  const raw = (import.meta.env.VITE_NANGO_PUBLIC_KEY as string | undefined)?.trim();
  return raw || undefined;
}

interface ProviderOption {
  name: string;
  display_name: string;
}

/** The provider list is loaded, not assumed. `error` and `empty` are distinct:
 *  "we could not ask" is never rendered as "there is nothing". */
type ProvidersState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ok"; list: ProviderOption[] };

function generateConnectionId(provider: string): string {
  const prefix = provider.slice(0, 8);
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return `${prefix}-${crypto.randomUUID()}`;
  }
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

/**
 * What the OAuth popup actually reported.
 *
 * `authorized` is the ONLY outcome that may lead to POST /api/connections. It
 * requires an explicit success message from the Nango origin — never the mere
 * disappearance of the popup.
 */
type OAuthOutcome =
  | { kind: "authorized" }
  | { kind: "failed"; message: string }
  | { kind: "cancelled" };

/** Nango's success event. The historical (misspelled) name is the one the
 *  service actually emits; the corrected spelling is accepted defensively. */
const SUCCESS_EVENTS = new Set(["AUTHORIZATION_SUCEEDED", "AUTHORIZATION_SUCCEEDED"]);
const FAILURE_EVENTS = new Set(["AUTHORIZATION_FAILED"]);

/** Give a success message posted immediately before the popup closed time to land. */
const CLOSE_GRACE_MS = 400;
/** Hard ceiling so an abandoned popup never leaves a listener running forever. */
const MAX_WAIT_MS = 10 * 60 * 1000;

/**
 * Resolve what the authorization popup reported.
 *
 * Listens for Nango's postMessage on its own origin. A closed popup with no
 * message is `cancelled` — the user backed out, or the provider never
 * confirmed. Either way there is nothing to register.
 */
function awaitOAuthOutcome(
  popup: Window,
  nangoOrigin: string,
  connectionId: string
): Promise<OAuthOutcome> {
  return new Promise<OAuthOutcome>((resolve) => {
    let settled = false;
    let closeTimer: ReturnType<typeof setTimeout> | null = null;

    const cleanup = () => {
      window.removeEventListener("message", onMessage);
      clearInterval(poll);
      clearTimeout(ceiling);
      if (closeTimer !== null) clearTimeout(closeTimer);
    };
    const finish = (outcome: OAuthOutcome) => {
      if (settled) return;
      settled = true;
      cleanup();
      resolve(outcome);
    };

    function onMessage(event: MessageEvent) {
      if (event.origin !== nangoOrigin || event.source !== popup) return;
      const payload = event.data as
        | {
            eventType?: string;
            data?: {
              error?: string;
              message?: string;
              connection_id?: string;
              connectionId?: string;
            };
          }
        | null
        | undefined;
      const eventType = payload?.eventType;
      if (!eventType) return;
      const reportedConnectionId = payload?.data?.connection_id ?? payload?.data?.connectionId;
      if (reportedConnectionId && reportedConnectionId !== connectionId) return;

      if (SUCCESS_EVENTS.has(eventType)) {
        finish({ kind: "authorized" });
        return;
      }
      if (FAILURE_EVENTS.has(eventType)) {
        finish({
          kind: "failed",
          message:
            payload?.data?.message ??
            payload?.data?.error ??
            "the provider refused the authorization",
        });
      }
    }

    window.addEventListener("message", onMessage);

    const poll = setInterval(() => {
      if (!popup.closed) return;
      clearInterval(poll);
      // A success message can be posted a beat before the window closes; wait
      // that beat out before calling it cancelled.
      closeTimer = setTimeout(() => finish({ kind: "cancelled" }), CLOSE_GRACE_MS);
    }, 300);

    const ceiling = setTimeout(() => finish({ kind: "cancelled" }), MAX_WAIT_MS);
  });
}

export default function ConnectButton({
  onSuccess,
  projectId,
  fixedProvider,
  nangoConnectionId,
  excludeProviders = [],
  label = "Connect Source",
}: ConnectButtonProps) {
  const [loading, setLoading] = useState(false);
  const [providers, setProviders] = useState<ProvidersState>({ status: "loading" });
  const [menuAnchor, setMenuAnchor] = useState<HTMLElement | null>(null);
  const [snackbar, setSnackbar] = useState<{
    open: boolean;
    severity: "error" | "warning" | "success";
    message: string;
  }>({ open: false, severity: "error", message: "" });

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const resp = await apiFetch("/api/modules/available");
        if (!resp.ok) {
          if (!cancelled) {
            setProviders({ status: "error", message: `HTTP ${resp.status}` });
          }
          return;
        }
        const body = await resp.json();
        const list: ProviderOption[] = (Array.isArray(body) ? body : (body.modules ?? []))
          .map((m: { name?: string; display_name?: string }) => ({
            name: m.name ?? "",
            display_name: m.display_name ?? m.name ?? "",
          }))
          .filter((m: ProviderOption) => m.name);
        // An empty list is the API's answer, not a reason to invent providers.
        if (!cancelled) setProviders({ status: "ok", list });
      } catch (err) {
        if (!cancelled) {
          setProviders({
            status: "error",
            message: err instanceof Error ? err.message : String(err),
          });
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const showSnackbar = useCallback(
    (severity: "error" | "warning" | "success", message: string) => {
      setSnackbar({ open: true, severity, message });
    },
    []
  );

  function handleCloseSnackbar() {
    setSnackbar((prev) => ({ ...prev, open: false }));
  }

  async function handleConnect(provider: string) {
    setMenuAnchor(null);
    if (!projectId) {
      showSnackbar(
        "error",
        "Select a real project before connecting a source. No source was connected."
      );
      return;
    }


    const baseUrl = nangoBaseUrl();
    if (!baseUrl) {
      showSnackbar(
        "error",
        "Cannot start the authorization: this console build has no OAuth service " +
          "address configured (VITE_NANGO_BASE_URL). No source was connected."
      );
      return;
    }

    let nangoOrigin: string;
    try {
      nangoOrigin = new URL(baseUrl).origin;
    } catch {
      showSnackbar(
        "error",
        `Cannot start the authorization: the configured OAuth service address ` +
          `(${baseUrl}) is not a valid URL. No source was connected.`
      );
      return;
    }

    setLoading(true);
    try {
      const connectionId = nangoConnectionId ?? generateConnectionId(provider);

      const publicKey = nangoPublicKey();
      const params = new URLSearchParams({
        connection_id: connectionId,
        ...(publicKey ? { public_key: publicKey } : {}),
      });
      const authUrl = `${baseUrl}/oauth/connect/${encodeURIComponent(provider)}?${params.toString()}`;

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

      // Success is REPORTED by Nango, never inferred from the popup closing.
      const outcome = await awaitOAuthOutcome(popup, nangoOrigin, connectionId);

      if (outcome.kind === "cancelled") {
        showSnackbar(
          "warning",
          "Authorization was not completed — the window closed before the provider " +
            "confirmed it. No source was connected."
        );
        return;
      }
      if (outcome.kind === "failed") {
        showSnackbar(
          "error",
          `Authorization failed: ${outcome.message}. No source was connected.`
        );
        return;
      }

      if (nangoConnectionId) {
        onSuccess();
        return;
      }

      // Authorized: register the connection on the server (AC5).
      const resp = await apiFetch("/api/connections", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          nango_connection_id: connectionId,
          provider,
          project_id: projectId,
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

  const visibleProviders =
    providers.status === "ok"
      ? providers.list.filter(
          (provider) => !excludeProviders.some((prefix) => provider.name.startsWith(prefix))
        )
      : [];

  return (
    <>
      <Button
        variant="contained"
        color="primary"
        onClick={(e) => {
          if (fixedProvider) void handleConnect(fixedProvider);
          else setMenuAnchor(e.currentTarget);
        }}
        disabled={loading}
        startIcon={loading ? <CircularProgress size={16} color="inherit" /> : null}
        data-testid="connect-source-button"
      >
        {loading ? "Connecting..." : label}
      </Button>
      <Menu anchorEl={menuAnchor} open={!fixedProvider && Boolean(menuAnchor)} onClose={() => setMenuAnchor(null)}>
        {providers.status === "loading" && (
          <MenuItem disabled data-testid="connect-providers-loading">
            <ListItemText primary="Loading available sources…" />
          </MenuItem>
        )}
        {providers.status === "error" && (
          <MenuItem disabled data-testid="connect-providers-error">
            <ListItemText
              primary="Could not load the available sources"
              secondary={`${providers.message} — the list below is unknown, not empty.`}
            />
          </MenuItem>
        )}
        {providers.status === "ok" &&
          visibleProviders.length === 0 && (
            <MenuItem disabled data-testid="connect-providers-empty">
              <ListItemText
                primary="No connector module is installed"
                secondary="Install a module before connecting a source."
              />
            </MenuItem>
          )}
        {providers.status === "ok" &&
          visibleProviders.map((p) => (
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
        autoHideDuration={8000}
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
