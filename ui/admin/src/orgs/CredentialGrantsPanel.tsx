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
 * AD-9 : 409 (already granted) → tone "info" ; 403 → tone "error".
 * AD-5 : la liste des comptes n'est pas re-filtrée côté client.
 *
 * VOCABULAIRE VISUEL (2026-08-02). Onze imports MUI retirés, comportement
 * inchangé : mêmes appels, mêmes codes HTTP, mêmes `data-testid`, même copie.
 * Les `<Alert severity>` deviennent `Status as="block"` — voir la note de
 * `DataAccessGrantsPanel.tsx` pour pourquoi ce n'est pas une primitive neuve.
 *
 * THE TYPED IDENTIFIER IS NOT A CONTROL WHEN THE CALLER ALREADY HOLDS ONE.
 * `shell/pages/OrgSettings.tsx` says so in its own words — the connection is
 * CHOSEN there, never recalled — and this panel nonetheless rendered the field
 * and its Load button underneath that chooser, so the screen asked the same
 * question twice and answered itself with a `cred_…` a person would have to
 * remember. The field now exists only on a mount that supplies no
 * `credentialId`; when the parent supplies one it is not rendered at all, and
 * nothing else about the flow changes.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch } from "../lib/apiFetch";
import { Button, ConfirmDialog, EmptyState, Field, Input, ObjectId, Spinner, Status, Table, TableBody, TableCell, TableHead, TableHeader, TableRow, Timestamp } from "../ui";

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
  /** The caller named the connection, so the person is not asked to. */
  const chosenByCaller = Boolean(initialCredentialId);
  /** What a STANDALONE mount typed and loaded. Ignored when the caller chose. */
  const [typedCredentialId, setTypedCredentialId] = useState("");
  const [credentialIdInput, setCredentialIdInput] = useState("");
  // ONE source of truth for which connection is being read. It used to be a
  // `useState` seeded from the prop, which never re-seeds: choosing a second
  // connection in `OrgSettings` changed the prop and left this panel showing the
  // FIRST connection's accounts, with its Expose button posting to it.
  const credentialId = initialCredentialId || typedCredentialId;

  const [accounts, setAccounts] = useState<CredentialAccount[]>([]);
  const [grants, setGrants] = useState<CredentialGrant[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  // Feedback opérations
  const [opError, setOpError] = useState<string | null>(null);
  const [opInfo, setOpInfo] = useState<string | null>(null);
  const [pendingRevoke, setPendingRevoke] = useState<CredentialAccount | null>(null);
  const [revoking, setRevoking] = useState(false);

  /** One `Idempotency-Key` per (credential, account, grantee) command — see
   *  `handleExpose`. `DELETE .../grants/{org}` needs none: only the POST does. */
  const exposeKeys = useRef<Record<string, string>>({});

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
        apiFetch(`${apiBase}/api/credentials/${encodeURIComponent(cid)}/accounts`, {
          headers,
        }),
        apiFetch(`${apiBase}/api/credentials/${encodeURIComponent(cid)}/grants`, {
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

  // Chargement auto dès qu'une connexion est désignée — par le parent ou par la
  // saisie d'un montage autonome.
  useEffect(() => {
    if (credentialId) {
      void loadData(credentialId);
    } else {
      setAccounts([]);
      setGrants([]);
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
    // `_create_account_grant` REFUSES a request without this header (422
    // `missing_idempotency_key`) — so every click of this button answered 422
    // until 2026-08-04. The stubbed `fetch` in this panel's own tests demands no
    // header, so nothing caught it until the button was walked against a real
    // server. Held per (account, org) so a retry after a 5xx is the same command
    // and cannot create a second grant; dropped on a 4xx, which is a rejection.
    const command = `${credentialId}:${externalAccountId}:${orgId}`;
    exposeKeys.current[command] ??=
      typeof crypto?.randomUUID === "function"
        ? crypto.randomUUID()
        : `idem-${Math.random().toString(36).slice(2)}${Date.now().toString(36)}`;
    try {
      const resp = await apiFetch(
        `${apiBase}/api/credentials/${encodeURIComponent(credentialId)}/accounts/${encodeURIComponent(externalAccountId)}/grants`,
        {
          method: "POST",
          headers: { ...headers, "Idempotency-Key": exposeKeys.current[command] },
          body: JSON.stringify({ grantee_org_id: orgId }),
        }
      );
      if (resp.ok || resp.status < 500) delete exposeKeys.current[command];
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
    if (revoking) return;
    setRevoking(true);
    setOpError(null);
    setOpInfo(null);
    try {
      const resp = await apiFetch(
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
      setPendingRevoke(null);
      await loadData(credentialId);
    } catch (err) {
      setOpError(err instanceof Error ? err.message : "Unexpected error.");
    } finally {
      setRevoking(false);
    }
  }

  // ---------------------------------------------------------------------------
  // Rendu
  // ---------------------------------------------------------------------------

  /** The dismiss control the two MUI `Alert`s carried as `onClose`. */
  const dismiss = (onClick: () => void, what: string) => (
    <Button variant="ghost" size="xs" onClick={onClick} aria-label={`Dismiss the ${what}`}>
      Dismiss
    </Button>
  );

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col gap-1.5">
        <p className="m-0 text-label font-label text-text-secondary uppercase tracking-wide">
          Auth grants
        </p>
        <p className="m-0 text-ui text-text-secondary">
          {chosenByCaller
            ? "Each account of this Authorization is exposed to this organization, or "
              + "revoked, on its own row. Exposure never reveals token material."
            : "Expose accounts from an Authorization to this organization. "
              + "Enter the Authorization identifier to get started."}
        </p>
      </div>

      {/* Saisie du credential ID — seulement quand personne ne l'a désignée.
          Un écran qui offre déjà un sélecteur de connexion ne redemande pas le
          même fait sous la forme d'un identifiant à retenir. */}
      {!chosenByCaller && (
        <div className="flex items-end gap-3">
          <div className="flex-1">
            <Field label="Connection identifier (credential)">
              {(field) => (
                <Input
                  {...field}
                  value={credentialIdInput}
                  onChange={(e) => setCredentialIdInput(e.target.value)}
                  data-testid="credential-id-input"
                  aria-label="Connection identifier"
                />
              )}
            </Field>
          </div>
          <Button
            variant="secondary"
            onClick={() => {
              const trimmed = credentialIdInput.trim();
              if (trimmed) {
                setTypedCredentialId(trimmed);
              }
            }}
            disabled={!credentialIdInput.trim() || loading}
            data-testid="credential-load-button"
          >
            Load
          </Button>
        </div>
      )}

      {/* Erreur chargement */}
      {loadError && (
        <Status
          as="block"
          tone="error"
          title="The exposed accounts could not be read"
          data-testid="grants-load-error"
          action={<Button size="sm" onClick={() => void loadData(credentialId)}>Try again</Button>}
        >
          {loadError} — nothing is listed rather than an empty table, which would read as
          &ldquo;no account is exposed&rdquo;.
        </Status>
      )}

      {/* Feedback opérations */}
      {opInfo && (
        <Status
          as="block"
          tone="info"
          action={dismiss(() => setOpInfo(null), "notice")}
          data-testid="grants-op-info"
        >
          {opInfo}
        </Status>
      )}
      {opError && (
        <Status
          as="block"
          tone="error"
          action={dismiss(() => setOpError(null), "error")}
          data-testid="grants-op-error"
        >
          {opError}
        </Status>
      )}

      {/* Chargement */}
      {loading && <Spinner label="Loading accounts…" showLabel />}

      {/* Liste vide — ce qui manque, et le geste qui la remplit. La table
          `app.credential_accounts` n'est écrite que par la découverte
          (`account_topology.reconcile_discovered_accounts`), déclenchée en se
          connectant ou en vérifiant un compte : c'est ce geste-là qui est nommé,
          aucun autre n'existe. */}
      {!loading && credentialId && accounts.length === 0 && !loadError && (
        <div data-testid="credential-accounts-empty">
          <EmptyState
            title="No account discovered on this Authorization"
            description={
              "Accounts appear here once the provider has been asked what this Authorization "
              + "reaches. Reconnect this source from Data > Sources, or verify an account "
              + "there — nothing can be exposed before the connection names what it holds."
            }
          />
        </div>
      )}

      {!loading && accounts.length > 0 && (
        <Table data-testid="accounts-table">
          <TableHeader>
            <TableRow>
              {/* One column, because a label and the id it names are one object.
                  Two columns printed the raw external id at reading weight and
                  the only human word about the account beside it, in a cell that
                  was empty as often as not. */}
              <TableHead>Account</TableHead>
              <TableHead>Discovered</TableHead>
              <TableHead numeric>Action</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {accounts.map((acct) => {
              const grant = existingGrant(acct.external_account_id);
              return (
                <TableRow
                  key={acct.external_account_id}
                  data-testid={`account-row-${acct.external_account_id}`}
                >
                  <TableCell>
                    {/* The name the provider gave leads; the identifier is kept
                        underneath, demoted rather than hidden. The payload
                        carries no connector for an account
                        (`credential_accounts_api.py:315`), so none is drawn. */}
                    {acct.label ? (
                      <span className="block text-ui text-text">{acct.label}</span>
                    ) : null}
                    <ObjectId value={acct.external_account_id} title="Account identifier" />
                  </TableCell>
                  <TableCell>
                    <Timestamp
                      className="text-text-secondary"
                      value={acct.discovered_at}
                      absentMeaning="No discovery time recorded"
                    />
                  </TableCell>
                  <TableCell numeric>
                    {grant ? (
                      <Button
                        variant="destructive"
                        size="sm"
                        onClick={() => setPendingRevoke(acct)}
                        data-testid={`revoke-grant-${acct.external_account_id}`}
                      >
                        Revoke
                      </Button>
                    ) : (
                      <Button
                        variant="secondary"
                        size="sm"
                        onClick={() => handleExpose(acct.external_account_id)}
                        data-testid={`expose-grant-${acct.external_account_id}`}
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
      <ConfirmDialog
        open={pendingRevoke !== null}
        onOpenChange={(open) => { if (!open) setPendingRevoke(null); }}
        title="Revoke this account exposure?"
        description="This organization will stop being able to use the exposed source account."
        evidence={{
          // THE ACCOUNT'S WORD, THEN ITS ADDRESS -- two rows, two jobs. The
          // `?? pendingRevoke?.external_account_id` this row carried made the
          // first row a copy of the second whenever the provider served no
          // label, so the dialog asked a person to revoke `act_<id>` twice and
          // never once named the account. An unlabelled account now reads as
          // `Unavailable` here and stays identified by the row below it.
          Account: pendingRevoke?.label ?? null,
          "Account identifier": pendingRevoke?.external_account_id ?? null,
          Organization: orgId,
        }}
        evidenceLabel="Exposure that will be revoked"
        confirmLabel="Revoke exposure"
        destructive
        busy={revoking}
        error={opError}
        onConfirm={() => pendingRevoke
          ? void handleRevoke(pendingRevoke.external_account_id)
          : undefined}
      />
    </div>
  );
}
