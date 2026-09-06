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
 * AD-9 : 409 → tone "warning" ; 403 → tone "error" ; 404 → tone "info" ;
 *         jamais swallowed.
 * AD-5 : pas de filtrage client-side.
 *
 * VOCABULAIRE VISUEL (2026-08-02). Ce panneau importait onze composants MUI ;
 * il n'en importe plus aucun. Rien de son comportement ne change : mêmes
 * appels, mêmes codes HTTP, mêmes `data-testid`, même copie.
 *
 * Les trois `<Alert severity>` deviennent `Status as="block"`, et c'est une
 * lecture du socle plutôt qu'un choix : la docstring de `ui/Data.tsx` dit que
 * séparer `Signal` et `Alert` est précisément ce qui a donné au produit six
 * façons de dire vert/orange/rouge. Poser une primitive `alert.tsx` ici aurait
 * rouvert cette porte au lieu de fermer la migration.
 *
 * THE PRINCIPAL IS COMPOSED, NOT DICTATED. The field used to take
 * `serviceAccount:sa@…` as one free string whose only reader was the server:
 * a missing colon, a capital `A` in `serviceaccount`, a bare `sa` with no
 * domain — each of them a round trip that came back 422 with a sentence
 * restating the grammar. The KIND is now chosen from the exact set
 * `dataset_access_api.py:44` accepts, and the identifier is checked against
 * that same expression before the call. The server refusal stays the
 * authority — the client check only names the repair one round trip earlier,
 * and never widens what is accepted.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch } from "../lib/apiFetch";
import {
  Button,
  ConfirmDialog,
  EmptyState,
  Field,
  Input,
  NativeSelect,
  ObjectId,
  Spinner,
  Status,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
  Timestamp,
} from "../ui";

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
  lifecycle_state: "requested" | "effective" | "failed" | "revoked";
  dataset_id: string | null;
  role: "roles/bigquery.dataViewer";
  effective_at: string | null;
  revoked_at: string | null;
  last_provider_error: string | null;
  provider_error_at: string | null;
  revocation_state: "requested" | "failed" | null;
  grant_operation_id: string | null;
  revoke_operation_id: string | null;
  provider_attempt_started_at: string | null;
  updated_at: string | null;
}

const LIFECYCLE_COPY = {
  requested: { label: "Requested", tone: "info" },
  effective: { label: "Effective", tone: "success" },
  failed: { label: "Failed", tone: "error" },
  revoked: { label: "Revoked", tone: "neutral" },
} as const;

function lifecycleCopy(grant: DataAccessGrant) {
  if (grant.revocation_state === "requested") {
    return { label: "Revocation requested", tone: "warning" as const };
  }
  if (grant.revocation_state === "failed") {
    return { label: "Revocation failed", tone: "error" as const };
  }
  return LIFECYCLE_COPY[grant.lifecycle_state];
}

interface DataAccessGrantsPanelProps {
  /** L'org pour laquelle on gère les accès dataset. */
  orgId: string;
  apiBase?: string;
  apiToken?: string;
}

// ---------------------------------------------------------------------------
// The principal, as the server defines it
//
// `server/core/dataset_access_api.py:44` is the only definition there is:
//
//   ^(user|serviceAccount|group):[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$
//
// Three kinds, and one identifier grammar shared by all three — the server
// declares NO domain that is proper to a service account, so none is invented
// here. Mirrored, never widened: anything this file accepts, that expression
// accepts too.
// ---------------------------------------------------------------------------

const PRINCIPAL_KINDS = ["user", "serviceAccount", "group"] as const;
type PrincipalKind = (typeof PRINCIPAL_KINDS)[number];

/** The identifier half of the server's expression, character for character. */
const PRINCIPAL_IDENTIFIER = /^[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$/;

const KIND_LABEL: Record<PrincipalKind, string> = {
  user: "A person (user)",
  serviceAccount: "A service account",
  group: "A group",
};

const KIND_PLACEHOLDER: Record<PrincipalKind, string> = {
  user: "person@example.com",
  serviceAccount: "sa@project.iam.gserviceaccount.com",
  group: "team@example.com",
};

/** What repairs the identifier, said in the words of the kind that was chosen. */
function identifierRepair(kind: PrincipalKind): string {
  return kind === "serviceAccount"
    ? "Write the service account's full address, like sa@project.iam.gserviceaccount.com — "
      + "the address Google Cloud shows on the account, not its display name."
    : `Write the full email address, like ${KIND_PLACEHOLDER[kind]} — a name on its own `
      + "names nobody outside this console.";
}

/** An identity the server recorded. Shown as itself when it is an address. */
function isAddress(value: string): boolean {
  return PRINCIPAL_IDENTIFIER.test(value);
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

  // Formulaire ajout — la nature, puis l'identifiant qu'elle réduit.
  const [principalKind, setPrincipalKind] = useState<PrincipalKind>("user");
  const [principalInput, setPrincipalInput] = useState("");
  /** The client-side refusal, raised only once the person has asked to grant. */
  const [shapeError, setShapeError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);

  // Feedback opérations
  const [opWarning, setOpWarning] = useState<string | null>(null);   // 409
  const [opError, setOpError] = useState<string | null>(null);       // 403 / erreur
  const [opInfo, setOpInfo] = useState<string | null>(null);         // 404
  const [pendingRevoke, setPendingRevoke] = useState<DataAccessGrant | null>(null);
  const [revoking, setRevoking] = useState(false);
  const grantKeys = useRef<Record<string, string>>({});
  const revokeKeys = useRef<Record<string, string>>({});

  const headers: HeadersInit = {
    "Content-Type": "application/json",
    ...(apiToken ? { Authorization: `Bearer ${apiToken}` } : {}),
  };

  // ---------------------------------------------------------------------------
  // Chargement de l'historique complet des demandes
  // ---------------------------------------------------------------------------

  const loadGrants = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const resp = await apiFetch(
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

  async function requestGrant(principal: string, clearInput: boolean) {
    setAdding(true);
    setOpWarning(null);
    setOpError(null);
    setOpInfo(null);
    const command = `${orgId}:${principal}`;
    grantKeys.current[command] ??= crypto.randomUUID();
    try {
      const resp = await apiFetch(
        `${apiBase}/api/organizations/${encodeURIComponent(orgId)}/dataset-access`,
        {
          method: "POST",
          headers: { ...headers, "Idempotency-Key": grantKeys.current[command] },
          body: JSON.stringify({ principal }),
        }
      );
      const data = await resp.json().catch(() => null);
      if (!resp.ok) {
        delete grantKeys.current[command];
        if (resp.status === 409) {
          // AD-9 : conflit → Alert severity="warning"
          setOpWarning(
            data?.message ??
              "This principal already has active access to this organization."
          );
        } else if (resp.status === 403) {
          setOpError("Insufficient permissions — owner/admin required.");
        } else if (resp.status === 422) {
          // The server remains the authority on the shape. When it refuses one
          // the client accepted, its own sentence is shown — never overwritten
          // by the guess above it.
          setShapeError(data?.message ?? identifierRepair(principalKind));
        } else {
          setOpError(
            data?.message ?? `HTTP error ${resp.status} while requesting access.`
          );
        }
        if (resp.status >= 500) await loadGrants();
        return;
      }
      if (resp.status === 202) {
        setOpInfo(data?.message ?? "The provider outcome is pending. Retry this request.");
        await loadGrants();
        return;
      }
      delete grantKeys.current[command];
      if (clearInput) setPrincipalInput("");
      await loadGrants();
    } catch (err) {
      setOpError(err instanceof Error ? err.message : "Unexpected error.");
    } finally {
      setAdding(false);
    }
  }

  async function handleGrant() {
    const identifier = principalInput.trim();
    if (!identifier) return;
    if (!PRINCIPAL_IDENTIFIER.test(identifier)) {
      setShapeError(identifierRepair(principalKind));
      return;
    }
    setShapeError(null);
    await requestGrant(`${principalKind}:${identifier}`, true);
  }

  // ---------------------------------------------------------------------------
  // Révoquer un accès (DELETE)
  // ---------------------------------------------------------------------------

  async function handleRevoke(grantId: string) {
    if (revoking) return;
    setRevoking(true);
    setOpWarning(null);
    setOpError(null);
    setOpInfo(null);
    revokeKeys.current[grantId] ??= crypto.randomUUID();
    try {
      const resp = await apiFetch(
        `${apiBase}/api/organizations/${encodeURIComponent(orgId)}/dataset-access/${encodeURIComponent(grantId)}`,
        {
          method: "DELETE",
          headers: { ...headers, "Idempotency-Key": revokeKeys.current[grantId] },
        }
      );
      const data = await resp.json().catch(() => null);
      if (!resp.ok) {
        delete revokeKeys.current[grantId];
        if (resp.status === 409) {
          setOpWarning(
            data?.message ?? "Retry the pending provider operation before cancelling."
          );
          setPendingRevoke(null);
        } else if (resp.status === 403) {
          setOpError("Insufficient permissions — owner/admin required.");
        } else if (resp.status === 404) {
          // AD-9 : grant déjà révoqué ou absent → severity="info"
          setOpInfo("This access no longer exists or has already been revoked.");
          setPendingRevoke(null);
        } else {
          setOpError(
            data?.message ?? `HTTP error ${resp.status} while revoking.`
          );
        }
        if (resp.status >= 500) await loadGrants();
        return;
      }
      if (resp.status === 202) {
        setOpInfo(data?.message ?? "The revocation outcome is pending. Retry it.");
        setPendingRevoke(null);
        await loadGrants();
        return;
      }
      delete revokeKeys.current[grantId];
      setPendingRevoke(null);
      await loadGrants();
    } catch (err) {
      setOpError(err instanceof Error ? err.message : "Unexpected error.");
    } finally {
      setRevoking(false);
    }
  }

  // ---------------------------------------------------------------------------
  // Rendu
  // ---------------------------------------------------------------------------

  /** The dismiss control the three MUI `Alert`s carried as `onClose`.
   *  `Status` takes it as `action`, so the affordance survives the migration
   *  instead of quietly disappearing with the component that provided it. */
  const dismiss = (onClick: () => void, what: string) => (
    <Button variant="ghost" size="xs" onClick={onClick} aria-label={`Dismiss the ${what}`}>
      Dismiss
    </Button>
  );

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col gap-1.5">
        <p className="m-0 text-label font-label text-text-secondary uppercase tracking-wide">
          Data access (BigQuery marts)
        </p>
        <p className="m-0 text-ui text-text-secondary">
          Request read access to this organization's marts dataset for a person,
          service account or group. Google Cloud confirmation is required before
          the request becomes effective.
        </p>
      </div>

      {/* Erreur chargement */}
      {loadError && (
        <Status
          as="block"
          tone="error"
          title="The dataset access requests could not be read"
          data-testid="data-access-load-error"
          action={<Button size="sm" onClick={() => void loadGrants()}>Try again</Button>}
        >
          {loadError} — nothing is listed rather than an empty table, which would read as
          &ldquo;nobody has requested access&rdquo;.
        </Status>
      )}

      {/* Feedback opérations */}
      {opWarning && (
        <Status
          as="block"
          tone="warning"
          action={dismiss(() => setOpWarning(null), "warning")}
          data-testid="data-access-op-warning"
        >
          {opWarning}
        </Status>
      )}
      {opError && (
        <Status
          as="block"
          tone="error"
          action={dismiss(() => setOpError(null), "error")}
          data-testid="data-access-op-error"
        >
          {opError}
        </Status>
      )}
      {opInfo && (
        <Status
          as="block"
          tone="info"
          action={dismiss(() => setOpInfo(null), "notice")}
          data-testid="data-access-op-info"
        >
          {opInfo}
        </Status>
      )}

      {/* Formulaire ajout principal — deux questions, la première réduit la
          seconde : la nature choisie change l'exemple, le contrôle et la phrase
          qui répare. Le fil (`type:identifier`) est composé ici, jamais tapé. */}
      <div className="flex items-start gap-3">
        <div className="w-56 shrink-0">
          <Field label="Who is being given access">
            {(field) => (
              <NativeSelect
                {...field}
                value={principalKind}
                onChange={(e) => {
                  setPrincipalKind(e.target.value as PrincipalKind);
                  setShapeError(null);
                }}
                data-testid="data-access-principal-kind"
              >
                {PRINCIPAL_KINDS.map((kind) => (
                  <option key={kind} value={kind}>
                    {KIND_LABEL[kind]}
                  </option>
                ))}
              </NativeSelect>
            )}
          </Field>
        </div>
        <div className="flex-1">
          <Field
            label="Identifier"
            hint={`Its address, as the provider writes it — ${KIND_PLACEHOLDER[principalKind]}.`}
            error={shapeError}
          >
            {(field) => (
              <Input
                {...field}
                placeholder={KIND_PLACEHOLDER[principalKind]}
                value={principalInput}
                onChange={(e) => {
                  setPrincipalInput(e.target.value);
                  if (shapeError) setShapeError(null);
                }}
                data-testid="data-access-principal-input"
              />
            )}
          </Field>
        </div>
        <Button
          className="mt-6"
          onClick={handleGrant}
          disabled={adding || !principalInput.trim()}
          data-testid="data-access-grant-button"
        >
          {adding ? "Requesting…" : "Request access"}
        </Button>
      </div>

      {/* Chargement */}
      {loading && <Spinner label="Loading access…" showLabel />}

      {/* Liste vide */}
      {!loading && !loadError && grants.length === 0 && (
        <div data-testid="data-access-empty">
          <EmptyState
            title="No dataset access requests for this organization"
            description={
              "No external principal has requested access to this organization's marts "
              + "dataset. A request is only effective after Google Cloud confirms it."
            }
          />
        </div>
      )}

      {/* Historique des demandes et grants */}
      {!loading && grants.length > 0 && (
        <Table data-testid="data-access-grants-table">
          <TableHeader>
            <TableRow>
              <TableHead>Principal</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Dataset</TableHead>
              {/* `granted_by` is whatever identity the server put on the row
                  (`dataset_access_api.py:185`) — an address when the session
                  carried one, an opaque subject otherwise. The column says
                  which, rather than printing both as if they were the same. */}
              <TableHead title="The identity the server recorded when the grant was made">
                Granted by
              </TableHead>
              <TableHead>Requested at</TableHead>
              <TableHead numeric>Action</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {grants.map((grant) => (
              <TableRow
                key={grant.id}
                data-testid={`data-access-row-${grant.id}`}
              >
                <TableCell>
                  <span className="font-mono text-caption">{grant.principal}</span>
                  {grant.last_provider_error && (
                    <span className="mt-1 block max-w-xl text-caption text-status-error-text">
                      {grant.last_provider_error}
                    </span>
                  )}
                </TableCell>
                <TableCell>
                  <Status
                    tone={lifecycleCopy(grant).tone}
                    active={grant.lifecycle_state === "requested"
                      || grant.revocation_state === "requested"}
                  >
                    {lifecycleCopy(grant).label}
                  </Status>
                </TableCell>
                <TableCell>
                  {grant.dataset_id ? (
                    <ObjectId value={grant.dataset_id} title="BigQuery marts dataset" />
                  ) : (
                    <span className="text-text-secondary">Not resolved</span>
                  )}
                </TableCell>
                <TableCell>
                  {grant.granted_by == null ? (
                    <span className="text-text-secondary" title="No identity recorded on this grant">
                      —
                    </span>
                  ) : isAddress(grant.granted_by) ? (
                    <span className="text-text-secondary">{grant.granted_by}</span>
                  ) : (
                    <ObjectId value={grant.granted_by} title="Identity recorded by the server" />
                  )}
                </TableCell>
                <TableCell>
                  <Timestamp
                    className="text-text-secondary"
                    value={grant.created_at}
                    absentMeaning="No grant time recorded"
                  />
                </TableCell>
                <TableCell numeric>
                  {grant.lifecycle_state !== "revoked" ? (
                    <div className="flex justify-end gap-2">
                      {(grant.lifecycle_state === "requested"
                        || grant.lifecycle_state === "failed") && (
                        <Button
                          variant="secondary"
                          size="sm"
                          onClick={() => void requestGrant(grant.principal, false)}
                          data-testid={`data-access-retry-${grant.id}`}
                        >
                          Retry
                        </Button>
                      )}
                      {grant.revocation_state === "failed" && (
                        <Button
                          variant="secondary"
                          size="sm"
                          onClick={() => void handleRevoke(grant.id)}
                          data-testid={`data-access-retry-revoke-${grant.id}`}
                        >
                          Retry revocation
                        </Button>
                      )}
                      <Button
                        variant="destructive"
                        size="sm"
                        onClick={() => setPendingRevoke(grant)}
                        data-testid={`data-access-revoke-${grant.id}`}
                      >
                        {grant.lifecycle_state === "effective" ? "Revoke" : "Cancel request"}
                      </Button>
                    </div>
                  ) : (
                    <span className="text-text-secondary">—</span>
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
      <ConfirmDialog
        open={pendingRevoke !== null}
        onOpenChange={(open) => { if (!open) setPendingRevoke(null); }}
        title={pendingRevoke?.lifecycle_state === "effective"
          ? "Revoke this marts access?"
          : "Cancel this access request?"}
        description={pendingRevoke?.lifecycle_state === "effective"
          ? "Google Cloud will be asked to remove this principal from the marts dataset."
          : "This request never became effective, so cancelling it does not revoke live access."}
        evidence={{ Principal: pendingRevoke?.principal ?? null, Organization: orgId }}
        evidenceLabel={pendingRevoke?.lifecycle_state === "effective"
          ? "Access that will be revoked"
          : "Request that will be cancelled"}
        confirmLabel={pendingRevoke?.lifecycle_state === "effective"
          ? "Revoke access"
          : "Cancel request"}
        destructive
        busy={revoking}
        error={opError}
        onConfirm={() => pendingRevoke ? void handleRevoke(pendingRevoke.id) : undefined}
      />
    </div>
  );
}
