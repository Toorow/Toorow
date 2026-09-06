/**
 * Story 42.9 — the authorization surfaces, at the two levels that own a credential.
 *
 * A credential has two attachments and they answer different questions (migration
 * 101): the PERSON who consented at the provider (`owner_identity`), and the
 * ORGANIZATION it is usable in (`owner_org_id`). Everything on this panel speaks
 * about the person; nothing on it decides whether a sync may run — that stays
 * resolved on the organization, or a sync would break the moment its owner is away.
 *
 * One component, two scopes, because they are the same object read from two
 * distances:
 *
 *   - `mine`  → User > My authorizations. What I connected, under my name,
 *               across every organization I belong to.
 *   - `org`   → Org admin > Authorizations. Everything the organization owns,
 *               each row naming who plugged it in, plus what another owner
 *               exposes to us.
 *
 * Deliberately NOT here: the project view. A project only ever *uses* accounts —
 * it never owns an authorization, so Data > Sources shows no action at all.
 */
import { Fragment, useCallback, useEffect, useState } from "react";
import { Button, ConfirmDialog, ConnectorMark,
  connectorName, EmptyState, formatDate, Status, Table, TableBody, TableCell, TableHead, TableHeader, TableRow, TableScroll } from "../ui";
import { apiFetch } from "../lib/apiFetch";
import type { Tone } from "../ui";
import ConnectButton from "../ConnectButton";
import ConnectGoogleButton from "./ConnectGoogleButton";
import { lastProjectScope } from "../shell/lastProjectScope";

export type AuthorizationScope = { kind: "mine" } | { kind: "org"; orgId: string };

/**
 * THE CREDENTIAL, as the three endpoints that answer about it serialize it.
 *
 * This shape used to live in `ConnectionsList.tsx` — a screen mounted by
 * nothing, which rendered a THIRD list titled "Authorizations" and was the only
 * mount point of `GoogleConnectPanel`. Both were deleted on 2026-08-17, and the
 * type came here because this panel is the only thing that ever read it.
 *
 * Filled by `me_api._serialize_authorization` (both scopes go through it,
 * `org_members_api.py:46`) and by `/api/connections` (`connections_api.py`),
 * which is the only one of the three carrying `active_datastream_count`.
 */
export interface ConnectionHealth {
  status: "ok" | "stale" | "revoked" | "populate_failed" | "provider_denied";
  last_checked_at?: string | null;
  last_fetched_at?: string | null;
}

export interface Connection {
  id: string;
  nango_connection_id: string;
  provider: string;
  project_id: string;
  created_at: string;
  health?: ConnectionHealth | null;
  /** Enabled datastreams reusing this authorization (1 auth -> N streams).
   *  Served by `/api/connections` ONLY; both authorization reads omit it. */
  active_datastream_count?: number;
  owner_org_id?: string | null;
  owner_org_name?: string | null;
  /** Credential expiry (populated for google_direct; null for Nango-backed). */
  token_expiry?: string | null;
  /** Derived exposure of this credential to the viewing org. */
  exposure?: "owned" | "shared_with_org" | "provided_by_org";
  account_label?: string | null;
  account_state?: "pending_account_selection" | "ready" | null;
  /** HOW MANY accounts this one authorization has selected (migration 211). */
  selected_account_count?: number | null;
  auth_path?: "nango" | "google_direct";
  status?: "active" | "revoked";
  can_manage?: boolean;
  /**
   * Story 42.9 — authorship, distinct from the owning organization.
   *
   * A credential is plugged in by ONE person (`owner_identity`, mig 101) but is
   * usable by their whole organization. These fields answer "whose access is
   * this?"; they must never be used to decide whether a sync may run — that
   * stays resolved on the owning org, or the sync breaks the moment its owner
   * is away.
   */
  owner_identity?: string | null;
  owner_display_name?: string | null;
  /** The viewer is the person who connected it. */
  is_mine?: boolean;
  /** Reconnect requires re-consenting at the provider — author only. */
  can_update?: boolean;
  /** Author, or an owner/admin of the owning org as a backstop. */
  can_revoke?: boolean;
}

type LoadState =
  | { status: "loading" }
  | { status: "denied" }
  | { status: "error"; message: string }
  | { status: "ok"; rows: Connection[] };

function healthLabel(row: Connection): { label: string; tone: Tone } {
  if (row.status === "revoked") return { label: "Revoked", tone: "error" };
  switch (row.health?.status) {
    case "ok":
      return { label: "Healthy", tone: "success" };
    case "stale":
      return { label: "Reconnect", tone: "warning" };
    case "revoked":
      return { label: "Disconnected", tone: "error" };
    // AI-341: the authorization is alive but the provider refuses the data --
    // the one red whose gesture is NOT "reconnect": restore the connected
    // account's access at the provider (or connect an account that has it),
    // and the next successful collection clears it on its own.
    case "provider_denied":
      return { label: "Access refused by provider", tone: "error" };
    // AI-302's sticky data red -- rendered "Unknown" until AI-341's sweep of
    // this mapping, which hid the exact red the Fleet raises most.
    case "populate_failed":
      return { label: "No data landed", tone: "error" };
    default:
      return { label: "Unknown", tone: "warning" };
  }
}

/** Absolute date, no time — the grain the product uses everywhere, and since
 *  76-1 the one `ui/Timestamp` spells. The empty string stays this panel's
 *  answer for an absence: its callers compose a sentence around it. */
function fmtDate(iso?: string | null): string {
  return iso ? formatDate(iso) : "";
}

function expiryCopy(row: Connection): string {
  const date = fmtDate(row.token_expiry);
  if (!date) return "—";
  return `${new Date(row.token_expiry as string).getTime() < Date.now() ? "Expired" : "Expires"} ${date}`;
}

/** Un Connecteur qu'un re-consentement ajouterait, tel que le serveur le nomme. */
type UnopenedConnector = { connector_name: string; display_name: string };

/** One scope of a Google consent, labelled by the server's own catalogue
 *  (`google_oauth_api._GOOGLE_SCOPE_LABELS`). The raw URI travels with it: the
 *  label is a courtesy, the URI is the fact. */
type GrantedScope = { scope: string; label: string };

/**
 * WHAT A CONSENT ACTUALLY OPENS, read only when asked.
 *
 * `GET /api/google/oauth/status/{connection_ref_id}`
 * (`server/core/google_oauth_api.py:398`, routed at :834) is the only read in
 * the product that returns the scopes a Google consent carries. It was reachable
 * from exactly one surface — `GoogleConnectPanel`, mounted only by the orphan
 * `ConnectionsList` — so the answer to "what did I actually authorize?" existed
 * and reached nobody.
 *
 * Read PER ROW AND ON EXPANSION, never on mount: the endpoint costs a query and
 * a project-access check each time, and a list of twenty would spend twenty of
 * them to answer a question nobody has asked. A 403/404 is a legitimate
 * boundary here for the same reason it is on the connector read — the endpoint
 * gates on project READ access, which an org admin need not have.
 */
type ScopeRead =
  | { status: "reading" }
  | { status: "unreadable" }
  | { status: "ok"; scopes: GrantedScope[] };

/** What revoking one authorization would stop, as far as it can be read. */
type RevokeImpact =
  | { status: "reading" }
  | { status: "unreadable" }
  | { status: "known"; count: number };

/**
 * The consequence, in the reader's words.
 *
 * "Could not be read here" is a real answer and never a zero: an organization
 * admin may legitimately have no view on the Project the authorization lives
 * in, and printing "0 Datastreams" there would tell them revoking is free when
 * it may stop every pull the Project has.
 */
function impactCopy(impact: RevokeImpact): string {
  if (impact.status === "reading") return "Reading…";
  if (impact.status === "unreadable") return "Could not be read from here";
  if (impact.count === 0) return "None — no Datastream pulls with it";
  return impact.count === 1
    ? "1 Datastream stops pulling"
    : `${impact.count} Datastreams stop pulling`;
}

export default function AuthorizationsPanel({ scope }: { scope: AuthorizationScope }) {
  const [state, setState] = useState<LoadState>({ status: "loading" });
  const [pendingRevoke, setPendingRevoke] = useState<Connection | null>(null);
  const [revoking, setRevoking] = useState(false);
  const [impact, setImpact] = useState<RevokeImpact>({ status: "reading" });
  const [actionError, setActionError] = useState<string | null>(null);
  // AI-112 : par autorisation, ce qu'elle N'OUVRE PAS. Cle = connection_ref id.
  // Une entree absente veut dire << pas encore lu, ou illisible >>, et se rend
  // comme rien du tout -- jamais comme << aucun >>.
  const [unopened, setUnopened] = useState<Record<string, UnopenedConnector[]>>({});
  // The one row whose permissions are open. One at a time, because the question
  // "what does THIS consent open" is asked about one credential.
  const [openScopes, setOpenScopes] = useState<string | null>(null);
  const [scopeReads, setScopeReads] = useState<Record<string, ScopeRead>>({});
  // What a revocation did NOT manage to undo, said after the fact rather than
  // swallowed. See `revoke()`.
  const [consentRemains, setConsentRemains] = useState<string | null>(null);

  const url =
    scope.kind === "mine"
      ? "/api/me/authorizations"
      : `/api/organizations/${encodeURIComponent(scope.orgId)}/authorizations`;

  const load = useCallback(async () => {
    setState({ status: "loading" });
    try {
      const resp = await apiFetch(url);
      if ([401, 403, 404].includes(resp.status)) {
        setState({ status: "denied" });
        return;
      }
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data: { authorizations?: Connection[] } = await resp.json();
      // An empty list is the answer. Nothing is substituted for a failed or
      // empty read (project doctrine).
      setState({ status: "ok", rows: data.authorizations ?? [] });
    } catch (err) {
      setState({ status: "error", message: err instanceof Error ? err.message : String(err) });
    }
  }, [url]);

  useEffect(() => {
    void load();
  }, [load]);

  // AI-112 -- ce qu'ajouter un scope au produit laisse derriere lui.
  //
  // Une autorisation deja emise ne porte pas les scopes ajoutes depuis. Les
  // quatre scopes d'AI-94 ne sont dans aucune, donc trois Connecteurs installes
  // depuis des semaines etaient invisibles sur toute connexion anterieure -- sans
  // que rien ne le dise. La personne n'avait rien decoche : le produit avait
  // change sous elle.
  //
  // Lecture SEPAREE de la liste, et volontairement : elle peut echouer sans
  // emporter le tableau. Un 404 ici est LEGITIME -- l'endpoint verifie que
  // l'appelant peut lire le projet, et un admin d'organisation n'a pas
  // forcement ce droit sur le projet ou vit l'autorisation. Dans ce cas on
  // n'affiche rien plutot qu'une invite qu'on ne peut pas justifier.
  useEffect(() => {
    if (state.status !== "ok") return;
    let cancelled = false;
    const rows = state.rows.filter(
      // Une connexion Nango n'ouvre qu'un Connecteur : rien a comparer, et
      // filtrer ici evite autant d'appels inutiles que de lignes.
      (row) => row.auth_path === "google_direct" && row.status !== "revoked" && row.project_id,
    );
    void Promise.all(
      rows.map(async (row) => {
        try {
          const resp = await apiFetch(
            `/api/connections/${encodeURIComponent(row.id)}/connectors` +
              `?project_id=${encodeURIComponent(row.project_id)}`,
          );
          if (!resp.ok) return;
          const data = (await resp.json()) as { unlocked_by_reconsent?: UnopenedConnector[] };
          if (cancelled) return;
          setUnopened((prev) => ({ ...prev, [row.id]: data.unlocked_by_reconsent ?? [] }));
        } catch {
          // Silencieux par dessein : cette invite est un supplement, pas la page.
        }
      }),
    );
    return () => {
      cancelled = true;
    };
  }, [state]);

  // WHAT THE REVOCATION COSTS, read on the gesture and only then.
  //
  // The inline two-button confirm this replaced asked "Revoke your access?" and
  // showed nothing else -- so the one fact that decides the answer, how many
  // Datastreams stop pulling, was not on screen. Neither authorization endpoint
  // carries the count (`me_api._serialize_authorization`,
  // `org_members_api._list_org_authorizations`); `/api/connections` does, per
  // Project (`connections_api.py:134`). It is therefore read HERE, once, for the
  // single row being revoked -- never on mount, where a list of twenty would
  // cost twenty reads to answer a question nobody has asked yet.
  useEffect(() => {
    const row = pendingRevoke;
    if (!row) return;
    if (typeof row.active_datastream_count === "number") {
      setImpact({ status: "known", count: row.active_datastream_count });
      return;
    }
    if (!row.project_id) {
      setImpact({ status: "unreadable" });
      return;
    }
    let cancelled = false;
    setImpact({ status: "reading" });
    void (async () => {
      try {
        const resp = await apiFetch(
          `/api/connections?project_id=${encodeURIComponent(row.project_id)}`,
        );
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = (await resp.json()) as { connections?: Connection[] };
        const found = data.connections?.find((candidate) => candidate.id === row.id);
        if (cancelled) return;
        setImpact(
          typeof found?.active_datastream_count === "number"
            ? { status: "known", count: found.active_datastream_count }
            : { status: "unreadable" },
        );
      } catch {
        if (!cancelled) setImpact({ status: "unreadable" });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [pendingRevoke]);

  /** Opens one row's permissions, reading them the first time and never again. */
  function toggleScopes(row: Connection) {
    if (openScopes === row.id) {
      setOpenScopes(null);
      return;
    }
    setOpenScopes(row.id);
    if (scopeReads[row.id]) return;
    setScopeReads((prev) => ({ ...prev, [row.id]: { status: "reading" } }));
    void (async () => {
      try {
        const resp = await apiFetch(
          `/api/google/oauth/status/${encodeURIComponent(row.id)}`,
        );
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = (await resp.json()) as { granted_scopes?: GrantedScope[] };
        setScopeReads((prev) => ({
          ...prev,
          [row.id]: { status: "ok", scopes: data.granted_scopes ?? [] },
        }));
      } catch {
        // Not a failure of the page: the read is project-gated, and an empty
        // list here would read as "this consent opens nothing".
        setScopeReads((prev) => ({ ...prev, [row.id]: { status: "unreadable" } }));
      }
    })();
  }

  async function revoke(row: Connection) {
    setActionError(null);
    setConsentRemains(null);
    setRevoking(true);
    try {
      // WITHDRAWING THE CONSENT, not only marking the row.
      //
      // `/api/authorizations/{id}/revoke` runs
      // `connection_revocation._apply_credential_revocation`: it deletes at
      // Nango, purges the health cache and sets `status='revoked'`. It does NOT
      // call Google's revoke endpoint and does NOT purge the encrypted blob --
      // so on a `google_direct` credential the grant stayed live in the
      // consenting person's Google Account and the refresh token stayed in
      // `app.connection_ref`. The only route that undoes both is
      // `/api/google/oauth/revoke/{id}` (`google_oauth_api.py:496`), and its
      // only caller was the deleted `GoogleConnectPanel`.
      //
      // It is called FIRST and best-effort: it gates on `identity_can_manage_org`
      // where the generic route also accepts the author, so a legitimate 403 must
      // not abandon a revocation the person asked for. What it answers is
      // REPORTED (below), never assumed.
      let remains: string | null = null;
      if (row.auth_path === "google_direct") {
        try {
          const resp = await apiFetch(
            `/api/google/oauth/revoke/${encodeURIComponent(row.id)}`,
            { method: "POST" },
          );
          if (!resp.ok) remains = "refused";
        } catch {
          remains = "unreachable";
        }
      }

      const resp = await apiFetch(
        `/api/authorizations/${encodeURIComponent(row.id)}/revoke`,
        { method: "POST" },
      );
      if (!resp.ok) {
        const body = (await resp.json().catch(() => ({}))) as { message?: string };
        throw new Error(body.message ?? `HTTP ${resp.status}`);
      }
      setPendingRevoke(null);
      setConsentRemains(
        remains
          ? "The Authorization is revoked here and no Run can use it. The consent itself was not "
            + "withdrawn at Google: only an owner or admin of the owning organization can do that "
            + "from here. Until one does, it stays listed in the Google Account of the person who "
            + "connected it and can be removed there."
          : null,
      );
      await load();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setRevoking(false);
    }
  }

  if (state.status === "loading") {
    return <Status as="block" active title="Reading authorizations">The list is not shown until it is read.</Status>;
  }
  if (state.status === "denied") {
    return (
      <Status as="block" tone="warning" title="Not readable here">
        {scope.kind === "mine"
          ? "Your authorizations are not readable with this session."
          : "Only an owner or admin of this organization can see its authorizations."}
      </Status>
    );
  }
  if (state.status === "error") {
    return (
      <Status
        as="block"
        tone="error"
        title="Authorizations could not be read"
        action={<Button type="button" variant="secondary" size="sm" onClick={() => void load()}>Try again</Button>}
      >
        <span className="block">No list is shown in their place.</span>
        <span className="mt-1 block font-mono text-caption">{state.message}</span>
      </Status>
    );
  }
  if (state.rows.length === 0) {
    // THE GESTURE, NOT ONLY ITS DESCRIPTION. Both empty states named the act that
    // fills the list and offered no way to perform it -- `EmptyState` takes an
    // `action` (`ui/Data.tsx:213`) and neither passed one, on the one screen that
    // already imports the door.
    //
    // A consent is granted FOR A PROJECT, and this panel is scoped to a person or
    // to an organization: there is no Project in its address. The Project the
    // person was last in is not a guess (`shell/lastProjectScope.ts`) -- but on
    // the organization view it is only offered when it belongs to THIS
    // organization, or the button would start a consent in someone else's scope.
    // When there is none, the sentence stands alone, as it did.
    const remembered = lastProjectScope();
    const connectHere =
      scope.kind === "mine" || remembered?.organizationId === scope.orgId
        ? remembered?.projectId
        : undefined;
    return (
      <EmptyState
        title={scope.kind === "mine" ? "No provider account connected" : "No provider authorization"}
        description={
          scope.kind === "mine"
            ? "Connect one from Data > Sources, or while adding a Datastream: a consent is granted "
              + "for a Project, so it is asked where a Project is in hand."
            : "This organization owns no provider authorization yet. One is granted for a PROJECT -- "
              + "open the project and connect from Data > Sources, or while adding a Datastream."
        }
        action={connectHere ? <ConnectGoogleButton projectId={connectHere} /> : undefined}
      />
    );
  }

  const showOwner = scope.kind === "org";

  return (
    <>
      {/* A revocation that fails keeps its dialog open, so the sentence saying so
          belongs INSIDE it, beside the button that was pressed -- not in a banner
          above a table the dialog is covering. */}
      {actionError && !pendingRevoke && (
        <Status as="block" tone="error" title="The action did not go through">{actionError}</Status>
      )}
      {/* A revocation that only half landed says so. It is a WARNING and not an
          error: the credential is revoked, the pulls have stopped, and what
          remains is named with the person who can finish it. */}
      {consentRemains && (
        <Status
          as="block"
          tone="warning"
          title="Revoked here, still granted at Google"
          data-testid="consent-not-withdrawn"
        >
          {consentRemains}
        </Status>
      )}
      {/* The library's Table, not raw `<table>` with `authorizations-*` classes.
          Those classes are declared in NO stylesheet in this repository — grep
          returns this file and nothing else — so every cell rendered with zero
          padding and the header read "Usable inConnected by". Jean's capture,
          2026-08-05. Markup that depends on CSS nobody wrote is the same defect
          as the legacy `.provider-logo` chip, one screen further on. */}
      <TableScroll label="Authorizations">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead scope="col">Provider</TableHead>
              <TableHead scope="col">Account</TableHead>
              <TableHead scope="col">Usable in</TableHead>
              {showOwner && <TableHead scope="col">Connected by</TableHead>}
              <TableHead scope="col">Health</TableHead>
              <TableHead scope="col">Authorization</TableHead>
              <TableHead scope="col"><span className="sr-only">Actions</span></TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {state.rows.map((row) => {
              const health = healthLabel(row);
              const provided = row.exposure === "provided_by_org";
              // Which rows carry scopes is not guessed from the provider string:
              // `auth_path` is the field that decides it, and it is the same
              // discriminator the connector read above already uses. A Nango
              // credential has no scope catalogue to show at all.
              const carriesScopes = row.auth_path === "google_direct";
              const open = openScopes === row.id;
              const detailId = `authorization-scopes-${row.id}`;
              const read = scopeReads[row.id];
              return (
                <Fragment key={row.id}>
                <TableRow>
                  <TableCell>
                    <span className="flex items-center gap-2">
                      <ConnectorMark provider={row.provider} alt={row.provider} size={24} />
                      <span className="truncate">{connectorName(row.provider)}</span>
                    </span>
                  </TableCell>
                  <TableCell>
                    {row.account_label ?? (
                      <span className="text-text-secondary">
                        {row.account_state === "pending_account_selection"
                          ? "No account selected yet"
                          : "—"}
                      </span>
                    )}
                    {/* ONE CONSENT HOLDS SEVERAL ACCOUNTS, and this cell showed
                        one. Since migration 211 there is a scope row PER
                        account -- a Google consent commonly carries a YouTube
                        channel and several Search Console properties -- and the
                        server has sent `selected_account_count` all along
                        precisely "so a surface can stop implying there is only
                        ever one" (its own comment). Nothing read it, so an
                        authorization serving five accounts read as serving the
                        most recently verified one. */}
                    {(row.selected_account_count ?? 0) > 1 && (
                      <span className="block text-caption text-text-secondary">
                        {`and ${(row.selected_account_count ?? 0) - 1} other `}
                        {(row.selected_account_count ?? 0) - 1 === 1 ? "account" : "accounts"}
                      </span>
                    )}
                    {/* Une OFFRE, jamais un reproche : `connection_ref` ne garde
                        pas les scopes demandes au consentement, donc << decoche >>
                        et << n'existait pas encore >> sont indistinguables. La
                        copie ne doit affirmer ni l'un ni l'autre. */}
                    {(unopened[row.id]?.length ?? 0) > 0 && (
                      <span className="mt-1 block text-caption text-text-secondary">
                        {unopened[row.id].length === 1
                          ? "1 more connector is available with this account"
                          : `${unopened[row.id].length} more connectors are available with this account`}
                        {": "}
                        {unopened[row.id].map((c) => c.display_name).join(", ")}
                        {". Reconnect to authorize."}
                      </span>
                    )}
                  </TableCell>
                  <TableCell>
                    {row.owner_org_name ?? "—"}
                    {row.exposure === "shared_with_org" && (
                      <span className="text-text-secondary"> · shared out</span>
                    )}
                  </TableCell>
                  {showOwner && (
                    <TableCell>
                      {provided ? (
                        // A beneficiary org is entitled to the owning organization,
                        // never to the person behind the credential.
                        <span className="text-text-secondary">
                          Provided by {row.owner_org_name ?? "another organization"}
                        </span>
                      ) : (
                        <>
                          {row.owner_display_name ?? "—"}
                          {row.is_mine && <span className="text-text-secondary"> You</span>}
                        </>
                      )}
                    </TableCell>
                  )}
                  <TableCell>
                    <Status tone={health.tone}>{health.label}</Status>
                  </TableCell>
                  <TableCell className="text-text-secondary">
                    {expiryCopy(row)}
                    {carriesScopes && (
                      <button
                        type="button"
                        className="mt-1 block text-caption text-text-secondary underline underline-offset-4"
                        aria-expanded={open}
                        aria-controls={detailId}
                        data-testid={`authorization-scopes-trigger-${row.id}`}
                        onClick={() => toggleScopes(row)}
                      >
                        {open ? "Hide what it grants" : "What it grants"}
                      </button>
                    )}
                  </TableCell>
                  <TableCell>
                    <span className="flex flex-wrap items-center justify-end gap-2">
                      {scope.kind === "mine" && row.can_update && row.status !== "revoked" && (
                        row.auth_path === "google_direct" ? (
                          <ConnectGoogleButton
                            projectId={row.project_id}
                            connectionRefId={row.id}
                            label="Reconnect"
                            variant="secondary"
                          />
                        ) : (
                          <ConnectButton
                            projectId={row.project_id}
                            fixedProvider={row.provider}
                            nangoConnectionId={row.nango_connection_id}
                            label="Reconnect"
                            onSuccess={() => void load()}
                          />
                        )
                      )}
                      {row.can_revoke && row.status !== "revoked" ? (
                        <Button
                          type="button"
                          variant="ghost"
                          size="sm"
                          data-testid={`revoke-authorization-${row.id}`}
                          onClick={() => {
                            setActionError(null);
                            setPendingRevoke(row);
                          }}
                        >
                          Revoke
                        </Button>
                      ) : null}
                    </span>
                  </TableCell>
                </TableRow>
                {/* THE DISCLOSURE. Rendered only when opened, unlike the run
                    detail rows of the workbench: nothing on the page asks a
                    question ACROSS the folded ones, and mounting a read-gated
                    panel per row would put its `aria-controls` target in the
                    accessibility tree for lines nobody expanded. */}
                {carriesScopes && open && (
                  <TableRow id={detailId} data-testid={`authorization-scopes-${row.id}`}>
                    <TableCell colSpan={showOwner ? 7 : 6} className="p-0">
                      <div className="grid gap-3 px-4.5 py-4">
                        <p className="m-0 text-caption text-text-secondary">
                          {/* WHEN it was last confirmed, beside WHAT it opens:
                              a scope list read from a consent nobody has
                              checked in months is a claim about the past. */}
                          {row.health?.last_checked_at
                            ? `Last verified ${fmtDate(row.health.last_checked_at)}.`
                            : "Never verified — this Authorization's health has not been checked."}
                        </p>
                        {!read || read.status === "reading" ? (
                          <Status active>Reading the granted permissions…</Status>
                        ) : read.status === "unreadable" ? (
                          <p className="m-0 text-ui text-text-secondary">
                            The granted permissions could not be read from here. Nothing is shown
                            in their place — reading them needs access to the Project this
                            authorization lives in.
                          </p>
                        ) : read.scopes.length === 0 ? (
                          <p className="m-0 text-ui text-text-secondary">
                            The stored consent records no scope. Reconnect to grant them again.
                          </p>
                        ) : (
                          <ul className="m-0 grid list-none gap-1 p-0" data-testid={`granted-scopes-${row.id}`}>
                            {read.scopes.map((entry) => (
                              <li key={entry.scope} className="grid gap-0.5">
                                <span className="text-ui text-text">{entry.label}</span>
                                <span className="font-mono text-caption text-text-secondary">
                                  {entry.scope}
                                </span>
                              </li>
                            ))}
                          </ul>
                        )}
                      </div>
                    </TableCell>
                  </TableRow>
                )}
                </Fragment>
              );
            })}
          </TableBody>
        </Table>
      </TableScroll>
      <p className="mt-4 text-caption text-text-secondary">
        {showOwner
          ? "Reconnecting an Authorization means consenting again at the provider, so only the person who connected it can do that. An owner or admin can revoke, which is recorded as an administrative action."
          : "These Authorizations are usable by your whole organization: a colleague can refresh a report that feeds on them without holding the access themselves. Revoking stops all future use immediately; data already published stays governed and auditable."}
      </p>
      {/* The shared confirmation, with its evidence -- the pattern every other
          destructive gesture of this console uses (`orgs/CredentialGrantsPanel`).
          What it adds over the two inline buttons it replaces is the row the
          decision actually turns on: what stops pulling. */}
      <ConfirmDialog
        open={pendingRevoke !== null}
        onOpenChange={(next) => { if (!next) { setPendingRevoke(null); setActionError(null); } }}
        title={pendingRevoke?.is_mine ? "Revoke your access?" : "Revoke on behalf of the organization?"}
        description={
          pendingRevoke?.auth_path === "google_direct"
            ? "Future pulls stop immediately and the consent is withdrawn at Google, so the stored "
              + "token stops working there too. Data already published stays governed and auditable, "
              + "and reconnecting means consenting again at the provider."
            : "Future pulls stop immediately. Data already published stays governed and auditable, "
              + "and reconnecting means consenting again at the provider."
        }
        evidence={{
          Provider: pendingRevoke?.provider ?? null,
          Account: pendingRevoke?.account_label
            ?? (pendingRevoke?.account_state === "pending_account_selection"
              ? "No account selected yet"
              : null),
          "Usable in": pendingRevoke?.owner_org_name ?? null,
          "Connected by": pendingRevoke?.is_mine
            ? "You"
            : pendingRevoke?.owner_display_name ?? null,
          "What stops": impactCopy(impact),
          // ABSENT, not null: `evidenceRows` keeps every key it is given, so a
          // null here would print an empty row on every Nango credential —
          // a line that says nothing about a thing that does not apply.
          ...(pendingRevoke?.auth_path === "google_direct"
            ? { "Consent at Google": "Withdrawn by this action" }
            : {}),
        }}
        evidenceLabel="Authorization that will be revoked"
        confirmLabel="Revoke"
        destructive
        busy={revoking}
        error={actionError}
        data-testid="revoke-authorization-dialog"
        confirmTestId="revoke-authorization-confirm"
        cancelTestId="revoke-authorization-cancel"
        onConfirm={() => { if (pendingRevoke) void revoke(pendingRevoke); }}
      />
    </>
  );
}
