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
import { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch } from "../lib/apiFetch";
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

interface OrganizationInvitation {
  invitation_id: string;
  role: string;
  state: string;
  expires_at: string;
  issuer: string;
  explicit_grants: Array<{
    scope_type: "project" | "flux";
    scope_id: string;
    capability: string;
  }>;
  explicit_none: boolean;
  accepted_at: string | null;
  revoked_at: string | null;
  superseded_by: string | null;
  available_actions: Array<"resend" | "revoke">;
}

interface InvitationMutationResponse {
  invitation_id: string;
  state: string;
  expires_at?: string;
  delivery_handoff?: {
    url?: string;
    single_return?: boolean;
  };
}

interface DeliveryHandoff {
  url: string | null;
  message: string;
}

interface InvitationGrantPayload {
  scope_id: string;
  capability: "view" | "edit" | "manage";
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

const INVITATION_STATUS_COLORS: Record<
  string,
  "success" | "warning" | "error" | "default"
> = {
  accepted: "success",
  pending: "warning",
  delivered: "warning",
  delivery_failed: "error",
  expired: "default",
  revoked: "default",
};

function deliveryUrl(payload: InvitationMutationResponse): string | null {
  const value = payload.delivery_handoff?.url;
  return typeof value === "string" && value.trim() ? value : null;
}

function parseInvitationGrants(value: string, label: string): InvitationGrantPayload[] {
  const rows = value
    .split(/\r?\n/)
    .map((row) => row.trim())
    .filter(Boolean);
  const seen = new Set<string>();
  return rows.map((row) => {
    const separator = row.lastIndexOf(":");
    const scope_id = row.slice(0, separator).trim();
    const capability = row.slice(separator + 1).trim();
    if (
      separator <= 0 ||
      !scope_id ||
      scope_id.length > 256 ||
      !["view", "edit", "manage"].includes(capability)
    ) {
      throw new Error(`${label} grants must use one line per scope: scope-id:view|edit|manage.`);
    }
    if (seen.has(scope_id)) throw new Error(`${label} grants contain a duplicate scope.`);
    seen.add(scope_id);
    return { scope_id, capability: capability as InvitationGrantPayload["capability"] };
  });
}

function formatInvitationDate(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("en-GB", {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    timeZoneName: "short",
  });
}

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
  const [projectGrantInput, setProjectGrantInput] = useState("");
  const [datastreamGrantInput, setDatastreamGrantInput] = useState("");
  const [expiryHours, setExpiryHours] = useState("48");
  const [invitations, setInvitations] = useState<OrganizationInvitation[]>([]);
  const [invitationsLoading, setInvitationsLoading] = useState(false);
  const [invitationsLoaded, setInvitationsLoaded] = useState(false);
  const [invitationsError, setInvitationsError] = useState<string | null>(null);
  const [invitationMutationError, setInvitationMutationError] = useState<string | null>(null);
  const [busyInvitation, setBusyInvitation] = useState<string | null>(null);
  const [deliveryHandoff, setDeliveryHandoff] = useState<DeliveryHandoff | null>(null);
  const [copyStatus, setCopyStatus] = useState<string | null>(null);
  const requestScope = `${org.id}\u0000${apiToken ?? ""}`;
  const activeRequestScopeRef = useRef(requestScope);
  const issueIdempotencyRef = useRef<{ fingerprint: string; key: string } | null>(null);
  const actionIdempotencyRef = useRef<Record<string, string>>({});
  activeRequestScopeRef.current = requestScope;

  // Mutation error (retrait / changement rôle)
  const [mutationError, setMutationError] = useState<string | null>(null);
  const [mutationWarning, setMutationWarning] = useState<string | null>(null);

  // Onglet actif : "membres" | "grants" | "data-access"
  const [activeTab, setActiveTab] = useState<
    "membres" | "invitations" | "grants" | "data-access"
  >("membres");

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
      const resp = await apiFetch(
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

  useEffect(() => {
    setInvitations([]);
    setInvitationsLoaded(false);
    setInvitationsError(null);
    setInvitationMutationError(null);
    setDeliveryHandoff(null);
    setCopyStatus(null);
    setAddError(null);
    setAddSaving(false);
    setBusyInvitation(null);
    setAddIdentity("");
    setAddRole("member");
    setProjectGrantInput("");
    setDatastreamGrantInput("");
    setExpiryHours("48");
  }, [org.id, apiToken]);

  const loadInvitations = useCallback(async () => {
    const requestedScope = requestScope;
    setInvitationsLoading(true);
    setInvitationsError(null);
    try {
      const resp = await apiFetch(
        `${apiBase}/api/organizations/${encodeURIComponent(org.id)}/invitations`,
        { headers, cache: "no-store" }
      );
      if (!resp.ok) {
        const data = await resp.json().catch(() => null);
        throw new Error(
          data?.message ??
            `GET /api/organizations/${org.id}/invitations: HTTP ${resp.status}`
        );
      }
      const data = await resp.json() as { items?: OrganizationInvitation[] };
      if (activeRequestScopeRef.current !== requestedScope) return;
      setInvitations(Array.isArray(data.items) ? data.items : []);
      setInvitationsLoaded(true);
    } catch (err) {
      if (activeRequestScopeRef.current !== requestedScope) return;
      setInvitationsError(
        err instanceof Error ? err.message : "Invitations could not be loaded."
      );
    } finally {
      if (activeRequestScopeRef.current === requestedScope) {
        setInvitationsLoading(false);
      }
    }
  }, [org.id, apiBase, apiToken]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (activeTab === "invitations" && !invitationsLoaded) {
      void loadInvitations();
    }
  }, [activeTab, invitationsLoaded, loadInvitations]);

  // ---------------------------------------------------------------------------
  // Ajout membre
  // ---------------------------------------------------------------------------

  async function handleAddMember() {
    if (!addIdentity.trim()) return;
    let projectGrants: InvitationGrantPayload[];
    let datastreamGrants: InvitationGrantPayload[];
    const parsedExpiry = Number(expiryHours);
    try {
      projectGrants = parseInvitationGrants(projectGrantInput, "Project");
      datastreamGrants = parseInvitationGrants(datastreamGrantInput, "Datastream");
      if (projectGrants.length + datastreamGrants.length > 100) {
        throw new Error("An invitation can contain at most 100 grants.");
      }
      if (!Number.isInteger(parsedExpiry) || parsedExpiry < 1 || parsedExpiry > 168) {
        throw new Error("Expiry must be a whole number of hours between 1 and 168.");
      }
    } catch (err) {
      setAddError(err instanceof Error ? err.message : "Invitation scope is invalid.");
      return;
    }
    const requestedOrgId = org.id;
    const requestedScope = requestScope;
    const fingerprint = JSON.stringify([
      requestedOrgId,
      addIdentity.trim(),
      addRole,
      projectGrants,
      datastreamGrants,
      parsedExpiry,
    ]);
    if (issueIdempotencyRef.current?.fingerprint !== fingerprint) {
      issueIdempotencyRef.current = { fingerprint, key: crypto.randomUUID() };
    }
    const idempotencyKey = issueIdempotencyRef.current.key;
    setAddSaving(true);
    setAddError(null);
    setDeliveryHandoff(null);
    setCopyStatus(null);
    setInvitationMutationError(null);
    setMutationError(null);
    setMutationWarning(null);
    try {
      const resp = await apiFetch(
        `${apiBase}/api/organizations/${encodeURIComponent(org.id)}/invitations`,
        {
          method: "POST",
          headers: {
            ...headers,
            "Idempotency-Key": idempotencyKey,
          },
          body: JSON.stringify({
            invited_identity: addIdentity.trim(),
            role: addRole,
            project_grants: projectGrants,
            datastream_grants: datastreamGrants,
            expires_in_hours: parsedExpiry,
          }),
        }
      );
      if (!resp.ok) {
        const data = await resp.json().catch(() => null);
        if (resp.status < 500) issueIdempotencyRef.current = null;
        if (activeRequestScopeRef.current !== requestedScope) return;
        setAddError(
          data?.message ?? `HTTP error ${resp.status} while issuing the invitation.`
        );
        return;
      }
      const result = await resp.json() as InvitationMutationResponse;
      issueIdempotencyRef.current = null;
      if (activeRequestScopeRef.current !== requestedScope) return;
      const url = deliveryUrl(result);
      setAddIdentity("");
      setAddRole("member");
      setProjectGrantInput("");
      setDatastreamGrantInput("");
      setExpiryHours("48");
      setDeliveryHandoff({
        url,
        message: url
          ? "Invitation created. No delivery is claimed: copy this single-use link and share it through an approved channel."
          : "Invitation created, but no delivery link was returned. Nothing was sent. Use Resend from the invitation list to create a replacement link.",
      });
      await loadInvitations();
    } catch (err) {
      if (activeRequestScopeRef.current === requestedScope) {
        setAddError(err instanceof Error ? err.message : "Unexpected error.");
      }
    } finally {
      if (activeRequestScopeRef.current === requestedScope) setAddSaving(false);
    }
  }

  // ---------------------------------------------------------------------------
  // Changement de rôle
  // ---------------------------------------------------------------------------

  async function handleInvitationAction(
    invitation: OrganizationInvitation,
    action: "resend" | "revoke"
  ) {
    const busyKey = `${invitation.invitation_id}:${action}`;
    const requestedOrgId = org.id;
    const requestedScope = requestScope;
    const commandKey = `${requestedOrgId}:${busyKey}`;
    const idempotencyKey = actionIdempotencyRef.current[commandKey] ?? crypto.randomUUID();
    actionIdempotencyRef.current[commandKey] = idempotencyKey;
    setBusyInvitation(busyKey);
    setInvitationMutationError(null);
    setDeliveryHandoff(null);
    setCopyStatus(null);
    try {
      const resp = await apiFetch(
        `${apiBase}/api/organizations/${encodeURIComponent(org.id)}/invitations/${encodeURIComponent(invitation.invitation_id)}/${action}`,
        {
          method: "POST",
          headers: {
            ...headers,
            "Idempotency-Key": idempotencyKey,
          },
          ...(action === "resend"
            ? { body: JSON.stringify({ expires_in_hours: 48 }) }
            : {}),
        }
      );
      if (!resp.ok) {
        const data = await resp.json().catch(() => null);
        if (resp.status < 500) delete actionIdempotencyRef.current[commandKey];
        throw new Error(
          data?.message ??
            `Invitation ${action} failed (HTTP ${resp.status}).`
        );
      }
      const result = await resp.json() as InvitationMutationResponse;
      delete actionIdempotencyRef.current[commandKey];
      if (activeRequestScopeRef.current !== requestedScope) return;
      if (action === "resend") {
        const url = deliveryUrl(result);
        setDeliveryHandoff({
          url,
          message: url
            ? "Replacement invitation created. The previous link is revoked. No delivery is claimed: copy the new single-use link now."
            : "The resend operation completed, but no replacement link was returned. Nothing was sent. Refresh and retry only if the server still offers Resend.",
        });
      }
      await loadInvitations();
    } catch (err) {
      if (activeRequestScopeRef.current !== requestedScope) return;
      setInvitationMutationError(
        err instanceof Error ? err.message : `Invitation ${action} failed.`
      );
    } finally {
      if (activeRequestScopeRef.current === requestedScope) setBusyInvitation(null);
    }
  }

  async function copyDeliveryLink() {
    if (!deliveryHandoff?.url) return;
    try {
      if (!navigator.clipboard?.writeText) {
        throw new Error("Clipboard unavailable");
      }
      await navigator.clipboard.writeText(deliveryHandoff.url);
      setCopyStatus("Link copied. Share it only with the intended recipient.");
    } catch {
      setCopyStatus("Automatic copy failed. Select the link and copy it manually.");
    }
  }
  async function handleChangeRole(identity: string, newRole: string) {
    setMutationError(null);
    setMutationWarning(null);
    try {
      const resp = await apiFetch(
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
      const resp = await apiFetch(
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
          variant={activeTab === "invitations" ? "contained" : "outlined"}
          disableElevation
          onClick={() => setActiveTab("invitations")}
          data-testid="tab-invitations"
          sx={{ textTransform: "none" }}
        >
          Invitations
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

            </>
          )}
        </Box>
      ) : activeTab === "invitations" ? (
        <Box>
          <Typography variant="overline" color="text.secondary" sx={{ display: "block", mb: 1 }}>
            Invite a person
          </Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
            Issuing an invitation does not create a member and does not prove delivery. The person
            becomes a member only after accepting the exact scope.
          </Typography>
          <Box sx={{ display: "flex", gap: 2, alignItems: "flex-start" }}>
            <TextField
              label="Verified email"
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
              {addSaving ? "Creating..." : "Create invitation"}
            </Button>
          </Box>
          <Box
            sx={{
              display: "grid",
              gridTemplateColumns: { xs: "1fr", md: "1fr 1fr 160px" },
              gap: 2,
              mt: 2,
            }}
          >
            <TextField
              label="Project grants"
              value={projectGrantInput}
              onChange={(event) => setProjectGrantInput(event.target.value)}
              multiline
              minRows={2}
              size="small"
              helperText="Optional. One per line: project-id:view|edit|manage"
              slotProps={{ htmlInput: { "aria-label": "Project invitation grants" } }}
            />
            <TextField
              label="Datastream grants"
              value={datastreamGrantInput}
              onChange={(event) => setDatastreamGrantInput(event.target.value)}
              multiline
              minRows={2}
              size="small"
              helperText="Optional. One per line: datastream-id:view|edit|manage"
              slotProps={{ htmlInput: { "aria-label": "Datastream invitation grants" } }}
            />
            <TextField
              label="Expiry (hours)"
              value={expiryHours}
              onChange={(event) => setExpiryHours(event.target.value)}
              type="number"
              size="small"
              helperText="1 to 168 hours"
              slotProps={{
                htmlInput: {
                  min: 1,
                  max: 168,
                  step: 1,
                  "aria-label": "Invitation expiry in hours",
                },
              }}
            />
          </Box>
          {addError && (
            <Alert severity="error" sx={{ mt: 2 }} data-testid="add-member-error">
              {addError}
            </Alert>
          )}

          {deliveryHandoff && (
            <Alert
              severity={deliveryHandoff.url ? "warning" : "info"}
              sx={{ mt: 2 }}
              data-testid="invitation-handoff"
            >
              <Typography variant="body2" sx={{ mb: deliveryHandoff.url ? 1.5 : 0 }}>
                {deliveryHandoff.message}
              </Typography>
              {deliveryHandoff.url && (
                <>
                  <Box sx={{ display: "flex", gap: 1, alignItems: "flex-start" }}>
                    <TextField
                      value={deliveryHandoff.url}
                      fullWidth
                      size="small"
                      data-testid="invitation-delivery-url"
                      slotProps={{
                        htmlInput: {
                          readOnly: true,
                          autoComplete: "off",
                          spellCheck: false,
                          "aria-label": "Single-use invitation link",
                        },
                      }}
                    />
                    <Button
                      variant="outlined"
                      type="button"
                      onClick={() => void copyDeliveryLink()}
                      data-testid="copy-invitation-link"
                      sx={{ textTransform: "none", whiteSpace: "nowrap" }}
                    >
                      Copy link
                    </Button>
                  </Box>
                  {copyStatus && (
                    <Typography variant="caption" role="status" sx={{ display: "block", mt: 1 }}>
                      {copyStatus}
                    </Typography>
                  )}
                </>
              )}
              <Button
                size="small"
                type="button"
                onClick={() => {
                  setDeliveryHandoff(null);
                  setCopyStatus(null);
                }}
                sx={{ mt: 1, textTransform: "none" }}
              >
                Dismiss
              </Button>
            </Alert>
          )}

          <Divider sx={{ my: 3 }} />

          {invitationsError && (
            <Alert severity="error" sx={{ mb: 2 }} data-testid="invitations-error">
              Invitations could not be loaded: {invitationsError}
              <Button
                size="small"
                type="button"
                onClick={() => void loadInvitations()}
                sx={{ ml: 1, textTransform: "none" }}
              >
                Retry
              </Button>
            </Alert>
          )}
          {invitationMutationError && (
            <Alert
              severity="error"
              sx={{ mb: 2 }}
              data-testid="invitation-mutation-error"
              onClose={() => setInvitationMutationError(null)}
            >
              {invitationMutationError}
            </Alert>
          )}

          {invitationsLoading ? (
            <Box sx={{ display: "flex", alignItems: "center", gap: 2 }}>
              <CircularProgress size={18} />
              <Typography variant="body2" color="text.secondary">
                Loading invitations...
              </Typography>
            </Box>
          ) : invitationsError ? null : invitations.length === 0 ? (
            <Typography variant="body2" color="text.secondary">
              No invitations for this organization.
            </Typography>
          ) : (
            <Table size="small" data-testid="invitations-table">
              <TableHead>
                <TableRow>
                  <TableCell>Role</TableCell>
                  <TableCell>State</TableCell>
                  <TableCell>Expires</TableCell>
                  <TableCell>Issuer</TableCell>
                  <TableCell align="right">Actions</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {invitations.map((invitation) => (
                  <TableRow
                    key={invitation.invitation_id}
                    data-testid={`invitation-row-${invitation.invitation_id}`}
                  >
                    <TableCell>{ROLE_LABELS[invitation.role] ?? invitation.role}</TableCell>
                    <TableCell>
                      <Chip
                        label={invitation.state}
                        size="small"
                        color={INVITATION_STATUS_COLORS[invitation.state] ?? "default"}
                        aria-label={`Invitation state: ${invitation.state}`}
                      />
                    </TableCell>
                    <TableCell>{formatInvitationDate(invitation.expires_at)}</TableCell>
                    <TableCell>
                      <Typography variant="body2" sx={{ fontFamily: "var(--font-mono, monospace)" }}>
                        {invitation.issuer}
                      </Typography>
                    </TableCell>
                    <TableCell align="right">
                      <Box sx={{ display: "flex", gap: 1, justifyContent: "flex-end" }}>
                        {invitation.available_actions.includes("resend") && (
                          <Button
                            size="small"
                            type="button"
                            disabled={busyInvitation !== null}
                            onClick={() => void handleInvitationAction(invitation, "resend")}
                            data-testid={`resend-invitation-${invitation.invitation_id}`}
                            sx={{ textTransform: "none" }}
                          >
                            {busyInvitation === `${invitation.invitation_id}:resend`
                              ? "Resending..."
                              : "Resend"}
                          </Button>
                        )}
                        {invitation.available_actions.includes("revoke") && (
                          <Button
                            size="small"
                            type="button"
                            color="error"
                            disabled={busyInvitation !== null}
                            onClick={() => void handleInvitationAction(invitation, "revoke")}
                            data-testid={`revoke-invitation-${invitation.invitation_id}`}
                            sx={{ textTransform: "none" }}
                          >
                            {busyInvitation === `${invitation.invitation_id}:revoke`
                              ? "Revoking..."
                              : "Revoke"}
                          </Button>
                        )}
                      </Box>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
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
