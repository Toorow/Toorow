/**
 * CredentialGrantsPanel — Story 21.8 (AC5).
 *
 * Exposition de comptes d'auth (credential_account_grants) à une organisation.
 *
 * Flux :
 *   1. L'utilisateur saisit un credential_id (ID d'une connexion).
 *   2. Charge GET /api/credentials/{credential_id}/accounts (liste des comptes).
 *   3. Charge GET /api/credentials/{credential_id}/grants (grants existants).
 *   4. Pour chaque compte, bouton « Exposer » (POST grants) ou « Révoquer »
 *      (DELETE grants/{org}).
 *
 * AD-9 : 409 (already granted) → Alert severity="info" ; 403 → Alert severity="error".
 * AD-5 : la liste des comptes n'est pas re-filtrée côté client.
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
// GET /api/credentials/{credential_id}/accounts → {"accounts": [{...}]}
// GET /api/credentials/{credential_id}/grants   → {"grants": [{...}]}
// ---------------------------------------------------------------------------

interface CredentialAccount {
  credential_id: string;
  external_account_id: string;
  label: string | null;
  discovered_at: string | null;
}

interface CredentialGrant {
  id: string;
  credential_id: string;
  external_account_id: string;
  grantee_org_id: string;
  granted_by: string | null;
  created_at: string | null;
}

interface CredentialGrantsPanelProps {
  /** L'org pour laquelle on gère les grants. */
  orgId: string;
  /** credential_id pré-sélectionné (optionnel — sinon l'user le saisit). */
  credentialId?: string;
  apiBase?: string;
  apiToken?: string;
}

// ---------------------------------------------------------------------------
// Composant
// ---------------------------------------------------------------------------

export default function CredentialGrantsPanel({
  orgId,
  credentialId: initialCredentialId,
  apiBase = "",
  apiToken = "",
}: CredentialGrantsPanelProps) {
  const [credentialId, setCredentialId] = useState(initialCredentialId ?? "");
  const [credentialIdInput, setCredentialIdInput] = useState(
    initialCredentialId ?? ""
  );

  const [accounts, setAccounts] = useState<CredentialAccount[]>([]);
  const [grants, setGrants] = useState<CredentialGrant[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  // Feedback opérations
  const [opError, setOpError] = useState<string | null>(null);
  const [opInfo, setOpInfo] = useState<string | null>(null);

  const headers: HeadersInit = {
    "Content-Type": "application/json",
    ...(apiToken ? { Authorization: `Bearer ${apiToken}` } : {}),
  };

  // ---------------------------------------------------------------------------
  // Chargement comptes + grants
  // ---------------------------------------------------------------------------

  const loadData = useCallback(async (cid: string) => {
    if (!cid) return;
    setLoading(true);
    setLoadError(null);
    setOpError(null);
    setOpInfo(null);
    try {
      const [respAccounts, respGrants] = await Promise.all([
        fetch(`${apiBase}/api/credentials/${encodeURIComponent(cid)}/accounts`, {
          headers,
        }),
        fetch(`${apiBase}/api/credentials/${encodeURIComponent(cid)}/grants`, {
          headers,
        }),
      ]);

      if (!respAccounts.ok) {
        if (respAccounts.status === 403) {
          setLoadError(
            "Insufficient permissions — access is restricted to owner/admin of the owning organization."
          );
          setLoading(false);
          return;
        }
        throw new Error(`GET /api/credentials/${cid}/accounts : HTTP ${respAccounts.status}`);
      }
      if (!respGrants.ok) {
        if (respGrants.status === 403) {
          setLoadError(
            "Insufficient permissions — access is restricted to owner/admin of the owning organization."
          );
          setLoading(false);
          return;
        }
        throw new Error(`GET /api/credentials/${cid}/grants : HTTP ${respGrants.status}`);
      }

      const acctData = await respAccounts.json() as { accounts: CredentialAccount[] };
      const grantsData = await respGrants.json() as { grants: CredentialGrant[] };
      setAccounts(acctData.accounts ?? []);
      setGrants(grantsData.grants ?? []);
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [apiBase]); // eslint-disable-line react-hooks/exhaustive-deps

  // Chargement auto si credentialId fourni en prop
  useEffect(() => {
    if (credentialId) {
      void loadData(credentialId);
    }
  }, [credentialId, loadData]);

  // ---------------------------------------------------------------------------
  // Helpers pour détecter les grants existants sur un compte pour cet org
  // ---------------------------------------------------------------------------

  function existingGrant(externalAccountId: string): CredentialGrant | undefined {
    return grants.find(
      (g) =>
        g.external_account_id === externalAccountId &&
        g.grantee_org_id === orgId
    );
  }

  // ---------------------------------------------------------------------------
  // Exposer un compte
  // ---------------------------------------------------------------------------

  async function handleExpose(externalAccountId: string) {
    setOpError(null);
    setOpInfo(null);
    try {
      const resp = await fetch(
        `${apiBase}/api/credentials/${encodeURIComponent(credentialId)}/accounts/${encodeURIComponent(externalAccountId)}/grants`,
        {
          method: "POST",
          headers,
          body: JSON.stringify({ grantee_org_id: orgId }),
        }
      );
      if (!resp.ok) {
        const data = await resp.json().catch(() => null);
        if (resp.status === 409) {
          // AD-9 : already granted → Alert severity="info"
          setOpInfo("This account is already exposed to this organization.");
        } else if (resp.status === 403) {
          setOpError("Insufficient permissions.");
        } else {
          setOpError(
            data?.message ?? `HTTP error ${resp.status} while exposing the account.`
          );
        }
        return;
      }
      await loadData(credentialId);
    } catch (err) {
      setOpError(err instanceof Error ? err.message : "Unexpected error.");
    }
  }

  // ---------------------------------------------------------------------------
  // Révoquer un grant
  // ---------------------------------------------------------------------------

  async function handleRevoke(externalAccountId: string) {
    setOpError(null);
    setOpInfo(null);
    try {
      const resp = await fetch(
        `${apiBase}/api/credentials/${encodeURIComponent(credentialId)}/accounts/${encodeURIComponent(externalAccountId)}/grants/${encodeURIComponent(orgId)}`,
        { method: "DELETE", headers }
      );
      if (!resp.ok) {
        const data = await resp.json().catch(() => null);
        if (resp.status === 403) {
          setOpError("Insufficient permissions.");
        } else {
          setOpError(
            data?.message ?? `HTTP error ${resp.status} while revoking.`
          );
        }
        return;
      }
      await loadData(credentialId);
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
        Auth grants
      </Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
        Expose accounts from a connection (credential) to this organization.
        Enter the connection identifier to get started.
      </Typography>

      {/* Saisie du credential ID */}
      <Box sx={{ display: "flex", gap: 2, mb: 3 }}>
        <TextField
          label="Connection identifier (credential)"
          value={credentialIdInput}
          onChange={(e) => setCredentialIdInput(e.target.value)}
          size="small"
          sx={{ flex: 1 }}
          data-testid="credential-id-input"
          slotProps={{
            htmlInput: {
              "aria-label": "Connection identifier",
            },
          }}
        />
        <Button
          variant="outlined"
          onClick={() => {
            const trimmed = credentialIdInput.trim();
            if (trimmed) {
              setCredentialId(trimmed);
            }
          }}
          disabled={!credentialIdInput.trim() || loading}
          data-testid="credential-load-button"
          sx={{ textTransform: "none" }}
        >
          Load
        </Button>
      </Box>

      {/* Erreur chargement */}
      {loadError && (
        <Alert severity="error" sx={{ mb: 2 }} data-testid="grants-load-error">
          {loadError}
        </Alert>
      )}

      {/* Feedback opérations */}
      {opInfo && (
        <Alert
          severity="info"
          sx={{ mb: 2 }}
          onClose={() => setOpInfo(null)}
          data-testid="grants-op-info"
        >
          {opInfo}
        </Alert>
      )}
      {opError && (
        <Alert
          severity="error"
          sx={{ mb: 2 }}
          onClose={() => setOpError(null)}
          data-testid="grants-op-error"
        >
          {opError}
        </Alert>
      )}

      {/* Chargement */}
      {loading && (
        <Box sx={{ display: "flex", alignItems: "center", gap: 2, mb: 2 }}>
          <CircularProgress size={18} />
          <Typography variant="body2" color="text.secondary">
            Loading accounts…
          </Typography>
        </Box>
      )}

      {/* Table des comptes */}
      {!loading && credentialId && accounts.length === 0 && !loadError && (
        <Typography variant="body2" color="text.secondary">
          No accounts found for this connection.
        </Typography>
      )}

      {!loading && accounts.length > 0 && (
        <Table size="small" data-testid="accounts-table">
          <TableHead>
            <TableRow>
              <TableCell>Account</TableCell>
              <TableCell>Label</TableCell>
              <TableCell>Discovered</TableCell>
              <TableCell align="right">Action</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {accounts.map((acct) => {
              const grant = existingGrant(acct.external_account_id);
              return (
                <TableRow
                  key={acct.external_account_id}
                  data-testid={`account-row-${acct.external_account_id}`}
                >
                  <TableCell>
                    <Typography
                      variant="body2"
                      sx={{ fontFamily: "var(--font-mono, monospace)", fontSize: "12px" }}
                    >
                      {acct.external_account_id}
                    </Typography>
                  </TableCell>
                  <TableCell>
                    <Typography variant="body2">
                      {acct.label ?? "—"}
                    </Typography>
                  </TableCell>
                  <TableCell>
                    <Typography variant="body2" color="text.secondary">
                      {acct.discovered_at
                        ? new Date(acct.discovered_at).toLocaleDateString("fr-FR")
                        : "—"}
                    </Typography>
                  </TableCell>
                  <TableCell align="right">
                    {grant ? (
                      <Button
                        size="small"
                        color="error"
                        onClick={() => handleRevoke(acct.external_account_id)}
                        data-testid={`revoke-grant-${acct.external_account_id}`}
                        sx={{ textTransform: "none" }}
                      >
                        Revoke
                      </Button>
                    ) : (
                      <Button
                        size="small"
                        variant="outlined"
                        onClick={() => handleExpose(acct.external_account_id)}
                        data-testid={`expose-grant-${acct.external_account_id}`}
                        sx={{ textTransform: "none" }}
                      >
                        Expose
                      </Button>
                    )}
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      )}
    </Box>
  );
}
