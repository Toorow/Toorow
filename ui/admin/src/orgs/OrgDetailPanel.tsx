/**
 * OrgDetailPanel — Story 21.8 (AC4).
 *
 * Vue détail d'une organisation : membres + gestion (ajout, changement de rôle,
 * retrait) + CredentialGrantsPanel (onglet « Autorisations d'auth »).
 *
 * AD-9 : 409 last-owner guard affiché en Alert visible, jamais swallowed.
 * AD-5 : enforcement côté serveur — les boutons admin sont cachés/désactivés
 *        si l'identité n'est pas owner/admin (reflet UX uniquement).
 * UX-DR10 : copie française accentuée.
 */
import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  Box,
  Button,
  Chip,
  CircularProgress,
  Divider,
  FormControl,
  InputLabel,
  MenuItem,
  Select,
  type SelectChangeEvent,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  TextField,
  Typography,
} from "@mui/material";
import type { Org } from "./types";
import CredentialGrantsPanel from "./CredentialGrantsPanel";
import DataAccessGrantsPanel from "./DataAccessGrantsPanel";

// ---------------------------------------------------------------------------
// Types (AI-54 : calquées sur la réponse réelle de l'API)
// GET /api/organizations/{org_id}/members → {"members": [{...}]}
// ---------------------------------------------------------------------------

export interface OrgMember {
  id: string;
  org_id: string;
  identity: string;
  role: string;
  status: string;
  invited_by: string | null;
  invited_at: string | null;
  joined_at: string | null;
  created_at: string;
}

// Rôles valides côté serveur (owner/admin/member/viewer)
const ORG_ROLES = ["owner", "admin", "member", "viewer"] as const;
type OrgRole = (typeof ORG_ROLES)[number];

const ROLE_LABELS: Record<string, string> = {
  owner: "Owner",
  admin: "Admin",
  member: "Member",
  viewer: "Viewer",
};

const STATUS_COLORS: Record<string, "success" | "warning" | "default"> = {
  active: "success",
  invited: "warning",
  suspended: "default",
};

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

interface OrgDetailPanelProps {
  org: Org;
  apiBase?: string;
  apiToken?: string;
}

// ---------------------------------------------------------------------------
// Composant principal
// ---------------------------------------------------------------------------

export default function OrgDetailPanel({
  org,
  apiBase = "",
  apiToken = "",
}: OrgDetailPanelProps) {
  const [members, setMembers] = useState<OrgMember[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Ajout membre
  const [addIdentity, setAddIdentity] = useState("");
  const [addRole, setAddRole] = useState<OrgRole>("member");
  const [addError, setAddError] = useState<string | null>(null);
  const [addSaving, setAddSaving] = useState(false);

  // Mutation error (retrait / changement rôle)
  const [mutationError, setMutationError] = useState<string | null>(null);
  const [mutationWarning, setMutationWarning] = useState<string | null>(null);

  // Onglet actif : "membres" | "grants" | "data-access"
  const [activeTab, setActiveTab] = useState<"membres" | "grants" | "data-access">("membres");

  const headers: HeadersInit = {
    "Content-Type": "application/json",
    ...(apiToken ? { Authorization: `Bearer ${apiToken}` } : {}),
  };

  // Identité courante : on la déduit de la liste des membres si possible
  // (la liste est filtrée côté serveur ; si l'utilisateur est dans la liste
  // on peut déduire son rôle pour afficher/cacher les boutons UX).
  // Note : en prod, l'identité vient du token — ici on ne réimplémente pas
  // l'access-control côté client, on se contente d'un reflet UX basique.
  // Faute de /api/me/profile dans le props, les boutons restent visibles ;
  // le serveur rejette les actions non autorisées de toute façon (AD-5).

  const loadMembers = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const resp = await fetch(
        `${apiBase}/api/organizations/${encodeURIComponent(org.id)}/members`,
        { headers }
      );
      if (!resp.ok) {
        throw new Error(
          `GET /api/organizations/${org.id}/members : HTTP ${resp.status}`
        );
      }
      const data = await resp.json() as { members: OrgMember[] };
      setMembers(data.members ?? []);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [org.id, apiBase]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    void loadMembers();
  }, [loadMembers]);

  // ---------------------------------------------------------------------------
  // Ajout membre
  // ---------------------------------------------------------------------------

  async function handleAddMember() {
    if (!addIdentity.trim()) return;
    setAddSaving(true);
    setAddError(null);
    setMutationError(null);
    setMutationWarning(null);
    try {
      const resp = await fetch(
        `${apiBase}/api/organizations/${encodeURIComponent(org.id)}/members`,
        {
          method: "POST",
          headers,
          body: JSON.stringify({ identity: addIdentity.trim(), role: addRole }),
        }
      );
      if (!resp.ok) {
        const data = await resp.json().catch(() => null);
        setAddError(
          data?.message ?? `HTTP error ${resp.status} while adding the member.`
        );
        return;
      }
      setAddIdentity("");
      setAddRole("member");
      await loadMembers();
    } catch (err) {
      setAddError(err instanceof Error ? err.message : "Unexpected error.");
    } finally {
      setAddSaving(false);
    }
  }

  // ---------------------------------------------------------------------------
  // Changement de rôle
  // ---------------------------------------------------------------------------

  async function handleChangeRole(identity: string, newRole: string) {
    setMutationError(null);
    setMutationWarning(null);
    try {
      const resp = await fetch(
        `${apiBase}/api/organizations/${encodeURIComponent(org.id)}/members/${encodeURIComponent(identity)}`,
        {
          method: "PATCH",
          headers,
          body: JSON.stringify({ role: newRole }),
        }
      );
      if (!resp.ok) {
        const data = await resp.json().catch(() => null);
        if (resp.status === 409) {
          setMutationWarning(
            "Cannot change the role of the organization's last active owner."
          );
        } else if (resp.status === 403) {
          setMutationError("Insufficient permissions.");
        } else {
          setMutationError(
            data?.message ?? `HTTP error ${resp.status} while changing the role.`
          );
        }
        return;
      }
      await loadMembers();
    } catch (err) {
      setMutationError(err instanceof Error ? err.message : "Unexpected error.");
    }
  }

  // ---------------------------------------------------------------------------
  // Retrait membre
  // ---------------------------------------------------------------------------

  async function handleRemoveMember(identity: string) {
    setMutationError(null);
    setMutationWarning(null);
    try {
      const resp = await fetch(
        `${apiBase}/api/organizations/${encodeURIComponent(org.id)}/members/${encodeURIComponent(identity)}`,
        { method: "DELETE", headers }
      );
      if (!resp.ok) {
        const data = await resp.json().catch(() => null);
        // AD-9 : 409 last-owner guard → Alert severity="warning"
        if (
          resp.status === 409 ||
          (data?.code === "conflict") ||
          (data?.message ?? "").toLowerCase().includes("last active owner") ||
          (data?.message ?? "").toLowerCase().includes("last owner")
        ) {
          setMutationWarning(
            "Cannot remove the organization's last active owner."
          );
        } else if (resp.status === 403) {
          setMutationError("Insufficient permissions.");
        } else {
          setMutationError(
            data?.message ?? `HTTP error ${resp.status} while removing the member.`
          );
        }
        return;
      }
      await loadMembers();
    } catch (err) {
      setMutationError(err instanceof Error ? err.message : "Unexpected error.");
    }
  }

  // ---------------------------------------------------------------------------
  // Rendu
  // ---------------------------------------------------------------------------

  return (
    <Box>
      {/* En-tête */}
      <Typography variant="h5" sx={{ fontWeight: 600, mb: 0.5 }}>
        {org.name}
      </Typography>
      <Typography
        variant="body2"
        color="text.secondary"
        sx={{ mb: 2, fontFamily: "var(--font-mono, monospace)" }}
      >
        {org.slug}
      </Typography>

      {/* Onglets (membres / autorisations d'auth) */}
      <Box sx={{ display: "flex", gap: 1, mb: 3 }}>
        <Button
          size="small"
          variant={activeTab === "membres" ? "contained" : "outlined"}
          disableElevation
          onClick={() => setActiveTab("membres")}
          data-testid="tab-membres"
          sx={{ textTransform: "none" }}
        >
          Members
        </Button>
        <Button
          size="small"
          variant={activeTab === "grants" ? "contained" : "outlined"}
          disableElevation
          onClick={() => setActiveTab("grants")}
          data-testid="tab-grants"
          sx={{ textTransform: "none" }}
        >
          Auth grants
        </Button>
        <Button
          size="small"
          variant={activeTab === "data-access" ? "contained" : "outlined"}
          disableElevation
          onClick={() => setActiveTab("data-access")}
          data-testid="tab-data-access"
          sx={{ textTransform: "none" }}
        >
          Data access
        </Button>
      </Box>

      {activeTab === "membres" ? (
        <Box>
          {/* Erreur réseau */}
          {error && (
            <Alert severity="error" sx={{ mb: 2 }} data-testid="members-error">
              Unable to load members: {error}
            </Alert>
          )}

          {/* Erreurs / avertissements de mutation */}
          {mutationWarning && (
            <Alert
              severity="warning"
              sx={{ mb: 2 }}
              onClose={() => setMutationWarning(null)}
              data-testid="members-warning"
            >
              {mutationWarning}
            </Alert>
          )}
          {mutationError && (
            <Alert
              severity="error"
              sx={{ mb: 2 }}
              onClose={() => setMutationError(null)}
              data-testid="members-mutation-error"
            >
              {mutationError}
            </Alert>
          )}

          {loading ? (
            <Box sx={{ display: "flex", alignItems: "center", gap: 2 }}>
              <CircularProgress size={18} />
              <Typography variant="body2" color="text.secondary">
                Loading members…
              </Typography>
            </Box>
          ) : (
            <>
              {/* Table des membres */}
              {members.length === 0 ? (
                <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
                  No members in this organization.
                </Typography>
              ) : (
                <Table size="small" sx={{ mb: 3 }} data-testid="members-table">
                  <TableHead>
                    <TableRow>
                      <TableCell>Identity</TableCell>
                      <TableCell>Role</TableCell>
                      <TableCell>Status</TableCell>
                      <TableCell>Joined</TableCell>
                      <TableCell align="right">Actions</TableCell>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {members.map((m) => (
                      <TableRow key={m.id} data-testid={`member-row-${m.identity}`}>
                        <TableCell>
                          <Typography variant="body2">{m.identity}</Typography>
                        </TableCell>
                        <TableCell>
                          <Chip
                            label={ROLE_LABELS[m.role] ?? m.role}
                            size="small"
                            variant="outlined"
                            aria-label={`Role: ${ROLE_LABELS[m.role] ?? m.role}`}
                          />
                        </TableCell>
                        <TableCell>
                          <Chip
                            label={m.status}
                            size="small"
                            color={STATUS_COLORS[m.status] ?? "default"}
                            aria-label={`Status: ${m.status}`}
                          />
                        </TableCell>
                        <TableCell>
                          <Typography variant="body2" color="text.secondary">
                            {m.joined_at
                              ? new Date(m.joined_at).toLocaleDateString("fr-FR")
                              : "—"}
                          </Typography>
                        </TableCell>
                        <TableCell align="right">
                          <Box sx={{ display: "flex", gap: 1, justifyContent: "flex-end" }}>
                            {/* Sélecteur de rôle */}
                            <FormControl size="small" sx={{ minWidth: 130 }}>
                              <InputLabel id={`role-label-${m.identity}`}>
                                Role
                              </InputLabel>
                              <Select
                                labelId={`role-label-${m.identity}`}
                                value={m.role}
                                label="Role"
                                onChange={(e: SelectChangeEvent) =>
                                  handleChangeRole(m.identity, e.target.value)
                                }
                                data-testid={`role-select-${m.identity}`}
                                aria-label={`Change the role of ${m.identity}`}
                              >
                                {ORG_ROLES.map((r) => (
                                  <MenuItem key={r} value={r}>
                                    {ROLE_LABELS[r]}
                                  </MenuItem>
                                ))}
                              </Select>
                            </FormControl>
                            <Button
                              size="small"
                              color="error"
                              onClick={() => handleRemoveMember(m.identity)}
                              data-testid={`remove-member-${m.identity}`}
                              sx={{ textTransform: "none" }}
                            >
                              Remove
                            </Button>
                          </Box>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              )}

              <Divider sx={{ mb: 3 }} />

              {/* Formulaire ajout membre */}
              <Typography
                variant="overline"
                color="text.secondary"
                sx={{ display: "block", mb: 1 }}
              >
                Add a member
              </Typography>
              <Box sx={{ display: "flex", gap: 2, alignItems: "flex-start" }}>
                <TextField
                  label="Identity (email or identifier)"
                  value={addIdentity}
                  onChange={(e) => setAddIdentity(e.target.value)}
                  size="small"
                  sx={{ flex: 1 }}
                  data-testid="add-member-identity"
                  slotProps={{
                    htmlInput: {
                      "aria-label": "New member identity",
                    },
                  }}
                />
                <FormControl size="small" sx={{ minWidth: 140 }}>
                  <InputLabel id="add-role-label">Role</InputLabel>
                  <Select
                    labelId="add-role-label"
                    value={addRole}
                    label="Role"
                    onChange={(e: SelectChangeEvent) =>
                      setAddRole(e.target.value as OrgRole)
                    }
                    data-testid="add-member-role"
                    aria-label="New member role"
                  >
                    {ORG_ROLES.map((r) => (
                      <MenuItem key={r} value={r}>
                        {ROLE_LABELS[r]}
                      </MenuItem>
                    ))}
                  </Select>
                </FormControl>
                <Button
                  variant="contained"
                  disableElevation
                  onClick={handleAddMember}
                  disabled={addSaving || !addIdentity.trim()}
                  data-testid="add-member-submit"
                  sx={{ textTransform: "none" }}
                >
                  {addSaving ? "Adding…" : "Add"}
                </Button>
              </Box>
              {addError && (
                <Alert severity="error" sx={{ mt: 2 }} data-testid="add-member-error">
                  {addError}
                </Alert>
              )}
            </>
          )}
        </Box>
      ) : activeTab === "grants" ? (
        /* Onglet autorisations d'auth */
        <CredentialGrantsPanel
          orgId={org.id}
          apiBase={apiBase}
          apiToken={apiToken}
        />
      ) : (
        /* Onglet data access (BigQuery marts) */
        <DataAccessGrantsPanel
          orgId={org.id}
          apiBase={apiBase}
          apiToken={apiToken}
        />
      )}
    </Box>
  );
}
