/**
 * GoogleConnectPanel — Panneau unique de connexion Google (Story 18.4, AD-15).
 *
 * Ce panneau remplace les entrées Google-par-produit par un seul point de
 * gestion pour l'ensemble du stack Google (GA4, GSC, Ads, Sheets).
 *
 * Fonctionnalités :
 *   - Bouton « Connecter Google » : déclenche le flux 18.2 (redirect vers
 *     authorize_url retourné par GET /api/google/oauth/authorize).
 *   - État connecté : liste des scopes accordés (libellés FR lisibles),
 *     expiry/santé (badge ok/stale/not_connected), bouton « Déconnecter ».
 *   - Gestion du retour ?google_oauth=success|error (bandeau FR).
 *   - Confirmation avant déconnexion (dialog MUI).
 *   - Copie française accentuée (UX-DR10).
 *
 * AD-15 : le flux OAuth vit dans la console, jamais dans l'iframe chat.
 * NFR3 : aucun token n'apparaît côté UI.
 * AD-14 : l'identité réelle est transmise via le Bearer token du serveur.
 * BLOCKED Phase B (AI-08) : passe live OAuth Google non disponible en dev ;
 *   le bouton rend l'URL mais le consentement réel est bloqué sans client prod.
 */
import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  Box,
  Button,
  Chip,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogContentText,
  DialogTitle,
  List,
  ListItem,
  ListItemText,
  Snackbar,
  Tooltip,
  Typography,
} from "@mui/material";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface GoogleScopeEntry {
  scope: string;
  label: string;
}

export interface GoogleStatus {
  connection_ref_id: string;
  auth_path: "google_direct" | "nango" | string;
  health: "ok" | "stale" | "not_connected" | "unknown";
  token_expiry: string | null;
  granted_scopes: GoogleScopeEntry[];
  project_id: string;
}

// ---------------------------------------------------------------------------
// Health badge
// ---------------------------------------------------------------------------

function HealthBadge({ health }: { health: GoogleStatus["health"] }) {
  switch (health) {
    case "ok":
      return (
        <Chip
          label="🟢 Connected"
          color="success"
          size="small"
          data-testid="google-health-ok"
        />
      );
    case "stale":
      return (
        <Chip
          label="🟠 Token Expired (Refresh Required)"
          color="warning"
          size="small"
          data-testid="google-health-stale"
        />
      );
    case "not_connected":
      return (
        <Chip
          label="🔴 Not Connected"
          color="default"
          size="small"
          data-testid="google-health-not-connected"
        />
      );
    case "unknown":
    default:
      return (
        <Chip
          label="Unknown Status"
          color="default"
          size="small"
          data-testid="google-health-unknown"
        />
      );
  }
}

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

interface GoogleConnectPanelProps {
  /** ID de la connexion Google directe du projet (conn_ ULID). */
  connectionRefId: string;
  /** ID du projet actif — transmis à l'endpoint authorize. */
  projectId: string;
  /** Rappel déclenché après une connexion ou déconnexion réussie. */
  onStatusChange?: () => void;
}

// ---------------------------------------------------------------------------
// Component principal
// ---------------------------------------------------------------------------

export default function GoogleConnectPanel({
  connectionRefId,
  projectId,
  onStatusChange,
}: GoogleConnectPanelProps) {
  const [status, setStatus] = useState<GoogleStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  // État du dialog de confirmation de déconnexion
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [revoking, setRevoking] = useState(false);

  // Bandeau de retour OAuth (?google_oauth=success|error)
  const [oauthBanner, setOauthBanner] = useState<{
    open: boolean;
    severity: "success" | "error";
    message: string;
  }>({ open: false, severity: "success", message: "" });

  // Snackbar pour les erreurs d'action
  const [snackbar, setSnackbar] = useState<{
    open: boolean;
    severity: "success" | "error" | "info";
    message: string;
  }>({ open: false, severity: "error", message: "" });

  // ---------------------------------------------------------------------------
  // Lecture du résultat OAuth depuis l'URL (?google_oauth=success|error)
  // ---------------------------------------------------------------------------
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const oauthResult = params.get("google_oauth");
    if (oauthResult === "success") {
      setOauthBanner({
        open: true,
        severity: "success",
        message: "Google account connected successfully. Scopes are now active.",
      });
      // Nettoyer le paramètre de l'URL sans rechargement (AD-15)
      const cleanUrl = new URL(window.location.href);
      cleanUrl.searchParams.delete("google_oauth");
      cleanUrl.searchParams.delete("connection");
      window.history.replaceState({}, "", cleanUrl.toString());
    } else if (oauthResult === "error") {
      setOauthBanner({
        open: true,
        severity: "error",
        message:
          "Google connection failed or was cancelled. Please try again or check configuration.",
      });
      const cleanUrl = new URL(window.location.href);
      cleanUrl.searchParams.delete("google_oauth");
      cleanUrl.searchParams.delete("connection");
      window.history.replaceState({}, "", cleanUrl.toString());
    }
  }, []);

  // ---------------------------------------------------------------------------
  // Chargement de l'état Google
  // ---------------------------------------------------------------------------
  const fetchStatus = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const resp = await fetch(
        `/api/google/oauth/status/${encodeURIComponent(connectionRefId)}`
      );
      if (!resp.ok) {
        const body = await resp.json().catch(() => ({}));
        throw new Error(
          (body as { message?: string }).message ??
            `HTTP Error ${resp.status}`
        );
      }
      const data: GoogleStatus = await resp.json();
      setStatus(data);
    } catch (err) {
      setLoadError(
        err instanceof Error ? err.message : "Failed to load Google status."
      );
    } finally {
      setLoading(false);
    }
  }, [connectionRefId]);

  useEffect(() => {
    void fetchStatus();
  }, [fetchStatus]);

  // ---------------------------------------------------------------------------
  // Connexion Google : récupère l'authorize_url et redirige (redirect, pas popup)
  // ---------------------------------------------------------------------------
  async function handleConnect() {
    try {
      const params = new URLSearchParams({
        project_id: projectId,
        connection_ref_id: connectionRefId,
      });
      const resp = await fetch(`/api/google/oauth/authorize?${params.toString()}`);
      if (!resp.ok) {
        const body = await resp.json().catch(() => ({}));
        const msg =
          (body as { message?: string }).message ?? `HTTP Error ${resp.status}`;
        setSnackbar({
          open: true,
          severity: "error",
          message: `Failed to start Google connection: ${msg}`,
        });
        return;
      }
      const { authorize_url } = (await resp.json()) as { authorize_url: string };
      // AD-15: redirect dans la console admin (jamais l'iframe chat).
      window.location.href = authorize_url;
    } catch (err) {
      setSnackbar({
        open: true,
        severity: "error",
        message: `Error during Google connection: ${
          err instanceof Error ? err.message : String(err)
        }`,
      });
    }
  }

  // ---------------------------------------------------------------------------
  // Révocation
  // ---------------------------------------------------------------------------
  async function handleRevoke() {
    setConfirmOpen(false);
    setRevoking(true);
    try {
      const resp = await fetch(
        `/api/google/oauth/revoke/${encodeURIComponent(connectionRefId)}`,
        { method: "POST" }
      );
      if (!resp.ok) {
        const body = await resp.json().catch(() => ({}));
        throw new Error(
          (body as { message?: string }).message ?? `HTTP Error ${resp.status}`
        );
      }
      setSnackbar({
        open: true,
        severity: "success",
        message: "Google account disconnected successfully.",
      });
      onStatusChange?.();
      await fetchStatus();
    } catch (err) {
      setSnackbar({
        open: true,
        severity: "error",
        message: `Disconnection failed: ${
          err instanceof Error ? err.message : String(err)
        }`,
      });
    } finally {
      setRevoking(false);
    }
  }

  // ---------------------------------------------------------------------------
  // Rendu
  // ---------------------------------------------------------------------------

  const isConnected = status?.auth_path === "google_direct";

  return (
    <Box
      data-testid="google-connect-panel"
      sx={{
        border: 1,
        borderColor: "divider",
        borderRadius: 2,
        p: 3,
        mb: 3,
        bgcolor: "background.paper",
      }}
    >
      {/* Bandeau retour OAuth */}
      <Snackbar
        open={oauthBanner.open}
        autoHideDuration={8000}
        onClose={() => setOauthBanner((prev) => ({ ...prev, open: false }))}
        anchorOrigin={{ vertical: "top", horizontal: "center" }}
        data-testid={`oauth-banner-${oauthBanner.severity}`}
      >
        <Alert
          severity={oauthBanner.severity}
          variant="filled"
          onClose={() => setOauthBanner((prev) => ({ ...prev, open: false }))}
          sx={{ width: "100%" }}
        >
          {oauthBanner.message}
        </Alert>
      </Snackbar>

      {/* Snackbar actions */}
      <Snackbar
        open={snackbar.open}
        autoHideDuration={6000}
        onClose={() => setSnackbar((prev) => ({ ...prev, open: false }))}
        anchorOrigin={{ vertical: "bottom", horizontal: "center" }}
      >
        <Alert
          severity={snackbar.severity}
          variant="filled"
          onClose={() => setSnackbar((prev) => ({ ...prev, open: false }))}
          sx={{ width: "100%" }}
          data-testid="google-action-snackbar"
        >
          {snackbar.message}
        </Alert>
      </Snackbar>

      {/* En-tête */}
      <Box
        sx={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          mb: 2,
        }}
      >
        <Typography variant="h6" component="h2">
          Google Connection (Unified Stack)
        </Typography>
        {status && <HealthBadge health={status.health} />}
      </Box>

      <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
        A single consent screen authorizes the entire Google stack:
        Google Analytics 4, Google Search Console, Google Ads, and Google Sheets.
      </Typography>

      {/* État de chargement */}
      {loading && (
        <Box sx={{ display: "flex", justifyContent: "center", my: 2 }}>
          <CircularProgress size={24} />
        </Box>
      )}

      {/* Erreur de chargement */}
      {!loading && loadError && (
        <Alert severity="error" sx={{ mb: 2 }}>
          {loadError}
        </Alert>
      )}

      {/* État non connecté */}
      {!loading && !loadError && !isConnected && (
        <Box>
          <Typography
            color="text.secondary"
            sx={{ mb: 2 }}
            data-testid="google-not-connected-message"
          >
            Google not connected. Click "Connect Google" to authorize access to
            Google Analytics, Search Console, Ads, and Sheets data.
          </Typography>
          <Button
            variant="contained"
            color="primary"
            onClick={() => { void handleConnect(); }}
            data-testid="google-connect-button"
          >
            Connect Google
          </Button>
        </Box>
      )}

      {/* État connecté */}
      {!loading && !loadError && isConnected && status && (
        <Box>
          {/* Expiry */}
          {status.token_expiry && (
            <Typography variant="body2" sx={{ mb: 1 }}>
              <strong>Token Expiration:</strong>{" "}
              {new Intl.DateTimeFormat("en-US", {
                dateStyle: "medium",
                timeStyle: "short",
              }).format(new Date(status.token_expiry))}
            </Typography>
          )}

          {/* Scopes accordés */}
          {status.granted_scopes.length > 0 && (
            <Box sx={{ mb: 2 }}>
              <Typography variant="body2" sx={{ mb: 0.5 }}>
                <strong>Granted Permissions:</strong>
              </Typography>
              <List dense disablePadding data-testid="google-scopes-list">
                {status.granted_scopes.map((entry) => (
                  <ListItem key={entry.scope} disableGutters sx={{ py: 0 }}>
                    <ListItemText
                      primary={
                        <Typography variant="body2">{entry.label}</Typography>
                      }
                      secondary={
                        <Typography variant="caption">{entry.scope}</Typography>
                      }
                      data-testid={`scope-entry-${entry.scope.split("/").pop() ?? entry.scope}`}
                    />
                  </ListItem>
                ))}
              </List>
            </Box>
          )}

          {/* Bouton reconnecter + déconnecter */}
          <Box sx={{ display: "flex", gap: 2, flexWrap: "wrap" }}>
            <Tooltip title="Relaunch consent to obtain new scopes or renew access">
              <Button
                variant="outlined"
                color="primary"
                onClick={() => { void handleConnect(); }}
                data-testid="google-reconnect-button"
              >
                Reconnect Google
              </Button>
            </Tooltip>
            <Button
              variant="outlined"
              color="error"
              onClick={() => setConfirmOpen(true)}
              disabled={revoking}
              startIcon={revoking ? <CircularProgress size={16} color="inherit" /> : null}
              data-testid="google-disconnect-button"
            >
              {revoking ? "Disconnecting…" : "Disconnect Google"}
            </Button>
          </Box>
        </Box>
      )}

      {/* Dialog de confirmation de déconnexion */}
      <Dialog
        open={confirmOpen}
        onClose={() => setConfirmOpen(false)}
        data-testid="google-disconnect-dialog"
      >
        <DialogTitle>Disconnect Google?</DialogTitle>
        <DialogContent>
          <DialogContentText>
            Disconnecting will revoke Google access for this project: Google Analytics 4,
            Search Console, Ads, and Sheets will no longer be able to extract data
            until reconnected.
          </DialogContentText>
        </DialogContent>
        <DialogActions>
          <Button
            onClick={() => setConfirmOpen(false)}
            data-testid="google-disconnect-cancel"
          >
            Cancel
          </Button>
          <Button
            onClick={() => { void handleRevoke(); }}
            color="error"
            variant="contained"
            data-testid="google-disconnect-confirm"
          >
            Disconnect
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}
