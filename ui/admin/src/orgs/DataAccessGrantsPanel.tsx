/**
 * DataAccessGrantsPanel — Story 24.5 (AC8).
 *
 * Gestion des accès IAM BigQuery au dataset marts d'une organisation.
 * Permet d'accorder (POST) et de révoquer (DELETE) l'accès d'un principal
 * externe (serviceAccount/user/group) au dataset org_<wslug>_marts.
 *
 * Format du principal : type:identifier avec type ∈ {user, serviceAccount, group}
 * et identifier contenant '@'.  Exemple : serviceAccount:sa@project.iam.gserviceaccount.com
 *
 * AD-9 : 409 → Alert severity="warning" ; 403 → Alert severity="error" ;
 *         404 → Alert severity="info" ; jamais swallowed.
 * AD-5 : pas de filtrage client-side.
 * UX-DR10 : copie française accentuée.
 */
import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  Box,
  Button,
  CircularProgress,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  TextField,
  Typography,
} from "@mui/material";

// ---------------------------------------------------------------------------
// Types (AI-54 : calquées sur la réponse réelle de l'API)
// GET /api/organizations/{org_id}/dataset-access → {"grants": [{...}]}
// ---------------------------------------------------------------------------

interface DataAccessGrant {
  id: string;
  org_id: string;
  principal: string;
  granted_by: string | null;
  created_at: string | null;
}

interface DataAccessGrantsPanelProps {
  /** L'org pour laquelle on gère les accès dataset. */
  orgId: string;
  apiBase?: string;
  apiToken?: string;
}

// ---------------------------------------------------------------------------
// Composant
// ---------------------------------------------------------------------------

export default function DataAccessGrantsPanel({
  orgId,
  apiBase = "",
  apiToken = "",
}: DataAccessGrantsPanelProps) {
  const [grants, setGrants] = useState<DataAccessGrant[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  // Formulaire ajout
  const [principalInput, setPrincipalInput] = useState("");
  const [adding, setAdding] = useState(false);

  // Feedback opérations
  const [opWarning, setOpWarning] = useState<string | null>(null);   // 409
  const [opError, setOpError] = useState<string | null>(null);       // 403 / erreur
  const [opInfo, setOpInfo] = useState<string | null>(null);         // 404

  const headers: HeadersInit = {
    "Content-Type": "application/json",
    ...(apiToken ? { Authorization: `Bearer ${apiToken}` } : {}),
  };

  // ---------------------------------------------------------------------------
  // Chargement de la liste des grants actifs
  // ---------------------------------------------------------------------------

  const loadGrants = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const resp = await fetch(
        `${apiBase}/api/organizations/${encodeURIComponent(orgId)}/dataset-access`,
        { headers }
      );
      if (!resp.ok) {
        if (resp.status === 403) {
          setLoadError(
            "Insufficient permissions — access is restricted to owner/admin of the organization."
          );
          return;
        }
        throw new Error(
          `GET /api/organizations/${orgId}/dataset-access : HTTP ${resp.status}`
        );
      }
      const data = (await resp.json()) as { grants: DataAccessGrant[] };
      setGrants(data.grants ?? []);
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [orgId, apiBase]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    void loadGrants();
  }, [loadGrants]);

  // ---------------------------------------------------------------------------
  // Accorder un accès (POST)
  // ---------------------------------------------------------------------------

  async function handleGrant() {
    const principal = principalInput.trim();
    if (!principal) return;
    setAdding(true);
    setOpWarning(null);
    setOpError(null);
    setOpInfo(null);
    try {
      const resp = await fetch(
        `${apiBase}/api/organizations/${encodeURIComponent(orgId)}/dataset-access`,
        {
          method: "POST",
          headers,
          body: JSON.stringify({ principal }),
        }
      );
      if (!resp.ok) {
        const data = await resp.json().catch(() => null);
        if (resp.status === 409) {
          // AD-9 : conflit → Alert severity="warning"
          setOpWarning(
            data?.message ??
              "This principal already has active access to this organization."
          );
        } else if (resp.status === 403) {
          setOpError("Insufficient permissions — owner/admin required.");
        } else if (resp.status === 422) {
          setOpError(
            data?.message ??
              "Invalid principal. Expected format: type:identifier (e.g. serviceAccount:sa@project.iam.gserviceaccount.com)"
          );
        } else {
          setOpError(
            data?.message ?? `HTTP error ${resp.status} while granting access.`
          );
        }
        return;
      }
      setPrincipalInput("");
      await loadGrants();
    } catch (err) {
      setOpError(err instanceof Error ? err.message : "Unexpected error.");
    } finally {
      setAdding(false);
    }
  }

  // ---------------------------------------------------------------------------
  // Révoquer un accès (DELETE)
  // ---------------------------------------------------------------------------

  async function handleRevoke(grantId: string) {
    setOpWarning(null);
    setOpError(null);
    setOpInfo(null);
    try {
      const resp = await fetch(
        `${apiBase}/api/organizations/${encodeURIComponent(orgId)}/dataset-access/${encodeURIComponent(grantId)}`,
        { method: "DELETE", headers }
      );
      if (!resp.ok) {
        const data = await resp.json().catch(() => null);
        if (resp.status === 403) {
          setOpError("Insufficient permissions — owner/admin required.");
        } else if (resp.status === 404) {
          // AD-9 : grant déjà révoqué ou absent → severity="info"
          setOpInfo("This access no longer exists or has already been revoked.");
        } else {
          setOpError(
            data?.message ?? `HTTP error ${resp.status} while revoking.`
          );
        }
        return;
      }
      await loadGrants();
    } catch (err) {
      setOpError(err instanceof Error ? err.message : "Unexpected error.");
    }
  }

  // ---------------------------------------------------------------------------
  // Rendu
  // ---------------------------------------------------------------------------

  return (
    <Box>
      <Typography
        variant="overline"
        color="text.secondary"
        sx={{ display: "block", mb: 1 }}
      >
        Data access (BigQuery marts)
      </Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
        Grant an IAM principal (user, service account, or group)
        read access to this organization's marts dataset.
        Format: <code>type:identifier@domain</code> (e.g.{" "}
        <code>serviceAccount:sa@project.iam.gserviceaccount.com</code>).
      </Typography>

      {/* Erreur chargement */}
      {loadError && (
        <Alert severity="error" sx={{ mb: 2 }} data-testid="data-access-load-error">
          {loadError}
        </Alert>
      )}

      {/* Feedback opérations */}
      {opWarning && (
        <Alert
          severity="warning"
          sx={{ mb: 2 }}
          onClose={() => setOpWarning(null)}
          data-testid="data-access-op-warning"
        >
          {opWarning}
        </Alert>
      )}
      {opError && (
        <Alert
          severity="error"
          sx={{ mb: 2 }}
          onClose={() => setOpError(null)}
          data-testid="data-access-op-error"
        >
          {opError}
        </Alert>
      )}
      {opInfo && (
        <Alert
          severity="info"
          sx={{ mb: 2 }}
          onClose={() => setOpInfo(null)}
          data-testid="data-access-op-info"
        >
          {opInfo}
        </Alert>
      )}

      {/* Formulaire ajout principal */}
      <Box sx={{ display: "flex", gap: 2, mb: 3 }}>
        <TextField
          label="Add a principal"
          placeholder="serviceAccount:sa@project.iam.gserviceaccount.com"
          value={principalInput}
          onChange={(e) => setPrincipalInput(e.target.value)}
          size="small"
          sx={{ flex: 1 }}
          data-testid="data-access-principal-input"
          slotProps={{
            htmlInput: {
              "aria-label": "Principal IAM",
            },
          }}
        />
        <Button
          variant="contained"
          disableElevation
          onClick={handleGrant}
          disabled={adding || !principalInput.trim()}
          data-testid="data-access-grant-button"
          sx={{ textTransform: "none" }}
        >
          {adding ? "Granting…" : "Grant"}
        </Button>
      </Box>

      {/* Chargement */}
      {loading && (
        <Box sx={{ display: "flex", alignItems: "center", gap: 2, mb: 2 }}>
          <CircularProgress size={18} />
          <Typography variant="body2" color="text.secondary">
            Loading access…
          </Typography>
        </Box>
      )}

      {/* Liste vide */}
      {!loading && !loadError && grants.length === 0 && (
        <Typography
          variant="body2"
          color="text.secondary"
          data-testid="data-access-empty"
        >
          No access granted for this organization.
        </Typography>
      )}

      {/* Table des grants actifs */}
      {!loading && grants.length > 0 && (
        <Table size="small" data-testid="data-access-grants-table">
          <TableHead>
            <TableRow>
              <TableCell>Principal</TableCell>
              <TableCell>Granted by</TableCell>
              <TableCell>Date</TableCell>
              <TableCell align="right">Action</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {grants.map((grant) => (
              <TableRow
                key={grant.id}
                data-testid={`data-access-row-${grant.id}`}
              >
                <TableCell>
                  <Typography
                    variant="body2"
                    sx={{ fontFamily: "var(--font-mono, monospace)", fontSize: "12px" }}
                  >
                    {grant.principal}
                  </Typography>
                </TableCell>
                <TableCell>
                  <Typography variant="body2" color="text.secondary">
                    {grant.granted_by ?? "—"}
                  </Typography>
                </TableCell>
                <TableCell>
                  <Typography variant="body2" color="text.secondary">
                    {grant.created_at
                      ? new Date(grant.created_at).toLocaleDateString("fr-FR")
                      : "—"}
                  </Typography>
                </TableCell>
                <TableCell align="right">
                  <Button
                    size="small"
                    color="error"
                    onClick={() => handleRevoke(grant.id)}
                    data-testid={`data-access-revoke-${grant.id}`}
                    sx={{ textTransform: "none" }}
                  >
                    Revoke
                  </Button>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </Box>
  );
}
