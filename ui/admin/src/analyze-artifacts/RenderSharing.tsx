/**
 * Analyze > Renders > Sharing — the Story 50.7 Console surface.
 *
 * WHY THIS FILE EXISTS. Three console routes were mounted and nothing in
 * `ui/admin/src` called them: a person could not create, list or revoke a Render
 * Share anywhere in the product. Per CLAUDE.md §4 the frontier of done is what the
 * person lives, not what the server can answer.
 *
 * THREE RULES THIS SCREEN OBEYS, each one a defect of the surface it replaces:
 *
 *   1. THE LINK IS SHOWN ONCE. The server stored only the bearer's peppered HMAC,
 *      so it cannot produce the URL again — not for this screen, not for support,
 *      not for anyone. The screen says so where it shows it, because a link a
 *      reader assumes is retrievable is a link they will close.
 *   2. THE LIST CARRIES NO LINK AND NO TOKEN. `snapshot_shares.list_shares`
 *      returned `share_token` on every row and argued in its docstring that this
 *      was acceptable because the caller is authenticated. It is not: it turns
 *      every screenshot, browser cache entry and support-ticket paste into a live
 *      public grant.
 *   3. REVOCATION IS CONFIRMED BEFORE IT IS SENT. It is irreversible FOR THE
 *      RECIPIENT — their link stops opening and cannot be reissued as the same
 *      link. That class was recorded in the Epic 46 review and repair.
 *
 * AND A FOURTH, since migration 323: NOTHING LEAVES ON ONE PERSON'S DECISION.
 * `docs/product-architecture/proactive-assertions.md` decision 2 requires a
 * project-scoped capability plus a confirmation by a SECOND role holder, so this
 * screen shows three things it never showed before — the project's posture and
 * who can change it, a requested share WAITING for someone else, and the confirm
 * gesture, offered only where the server says this reader may take it. The
 * delivery link has MOVED: it is created by the confirmation, not by the
 * request, because a link that exists while nobody has authorized the exit is a
 * link somebody can send early.
 *
 * EXPIRY IS REQUIRED, and this screen never chooses one silently. AD-20 calls a
 * Share "revocable, expiring and audited"; the retired table had no expiry column
 * at all, which is why every legacy link is unexpiring today.
 *
 * Composed only from `ui/admin/src/ui/index.ts`. No `@mui/material`, no local
 * stylesheet, no new class prefix, no hardcoded colour, no literal spacing.
 */
import { useCallback, useEffect, useState } from "react";

import { ApiError } from "../lib/apiFetch";
import { Badge, Button, Cluster, ConfirmDialog, CopyButton, EmptyState, Failure, Field, Input, Loading, NoScope, ObjectId, Panel, PanelHeader, Retry, Stack, stateLabel, stateTone, Status, Table, TableBody, TableCell, TableHead, TableHeader, TableRow, TableScroll, Timestamp } from "../ui";
import { confirmRenderShare, createRenderShare, listRenderShares, revokeRenderShare, type RenderShare, type RenderShareConfirmed, type RenderShareRequested } from "./client";

/** A default the operator can change, never one applied silently. */
function defaultExpiry(maxDays: number): string {
  const days = Math.max(1, Math.min(7, maxDays));
  const when = new Date(Date.now() + days * 24 * 60 * 60 * 1000);
  // `datetime-local` wants `YYYY-MM-DDTHH:mm` with no zone and no seconds.
  return new Date(when.getTime() - when.getTimezoneOffset() * 60000)
    .toISOString()
    .slice(0, 16);
}

/**
 * Who authorized this exit, or who is still waiting for someone to.
 *
 * Every branch is a SENTENCE and none is an empty cell: a person looking at a
 * share that is going nowhere needs to know whether it is waiting for them,
 * waiting for somebody else, or lapsed. `absent` names a row that predates the
 * two-person rule instead of pretending a second person signed it.
 */
function authorizationCell(share: RenderShare) {
  if (share.confirmation_state === "confirmed") {
    return <span>{share.confirmed_by}</span>;
  }
  if (share.confirmation_state === "expired") {
    return <span>Nobody confirmed it in time</span>;
  }
  if (share.confirmation_state === "pending") {
    return (
      <span>
        {share.awaiting_your_own_request
          ? "You requested it — someone else must confirm"
          : `Waiting on a second person (${share.confirmation_requested_by} requested it)`}
      </span>
    );
  }
  return <span>Created before shares needed a second confirmation</span>;
}

export function RenderSharing({
  projectId,
  renderId,
  canonicalShareAvailable,
  contractReason,
}: {
  projectId?: string;
  renderId: string;
  /** Stated by the server. This screen never infers it from an empty list. */
  canonicalShareAvailable: boolean;
  contractReason: string;
}) {
  const [shares, setShares] = useState<RenderShare[] | null>(null);
  const [maxDays, setMaxDays] = useState(30);
  const [error, setError] = useState<string | null>(null);
  const [expiresAt, setExpiresAt] = useState("");
  const [requested, setRequested] = useState<RenderShareRequested | null>(null);
  const [confirmed, setConfirmed] = useState<RenderShareConfirmed | null>(null);
  const [busy, setBusy] = useState(false);
  const [pendingRevoke, setPendingRevoke] = useState<RenderShare | null>(null);
  /** Stated by the server, never inferred from a refused create. */
  const [policy, setPolicy] = useState<{
    state: "allowed" | "forbidden";
    decidedBy: string | null;
    gesture: string;
    windowHours: number;
  } | null>(null);

  const load = useCallback(
    (signal?: AbortSignal) => {
      // No request at all when the server has said the replay contract is absent:
      // no Share can exist for such a Render, because creation refuses first. A
      // screen that asked anyway would report an empty list as if it were an
      // answer about sharing rather than about the contract.
      if (!projectId || !canonicalShareAvailable) return;
      listRenderShares(projectId, renderId, { signal })
        .then((collection) => {
          setShares(collection.shares);
          setMaxDays(collection.max_lifetime_days);
          setPolicy({
            state: collection.external_sharing,
            decidedBy: collection.external_sharing_decided_by,
            gesture: collection.external_sharing_gesture,
            windowHours: collection.confirmation_window_hours,
          });
          setExpiresAt((current) => current || defaultExpiry(collection.max_lifetime_days));
        })
        .catch((reason: unknown) => {
          if (signal?.aborted) return;
          setError(reason instanceof Error ? reason.message : "Shares are unavailable.");
        });
    },
    [projectId, renderId, canonicalShareAvailable],
  );

  useEffect(() => {
    const controller = new AbortController();
    load(controller.signal);
    return () => controller.abort();
  }, [load]);

  if (!projectId) return <NoScope what="sharing for this Render" />;

  const create = async () => {
    setBusy(true);
    setError(null);
    try {
      // A `datetime-local` value carries no zone. It is the operator's wall clock,
      // so it is converted here rather than sent as if it were UTC — an expiry an
      // hour out is an expiry nobody chose.
      const iso = new Date(expiresAt).toISOString();
      const outcome = await createRenderShare(projectId, renderId, iso);
      setRequested(outcome);
      load();
    } catch (reason: unknown) {
      const body = (reason as ApiError | undefined)?.body as { message?: string } | undefined;
      setError(body?.message || (reason as Error).message || "The share was refused.");
    } finally {
      setBusy(false);
    }
  };

  const confirm = async (share: RenderShare) => {
    setBusy(true);
    setError(null);
    try {
      const outcome = await confirmRenderShare(projectId, share.share_id);
      setConfirmed(outcome);
      setRequested(null);
      load();
    } catch (reason: unknown) {
      const body = (reason as ApiError | undefined)?.body as { message?: string } | undefined;
      setError(
        body?.message || (reason as Error).message || "The share was not confirmed.",
      );
    } finally {
      setBusy(false);
    }
  };

  const revoke = async (share: RenderShare) => {
    setBusy(true);
    setError(null);
    try {
      await revokeRenderShare(projectId, share.share_id);
      setPendingRevoke(null);
      load();
    } catch (reason: unknown) {
      setError((reason as Error).message || "The share could not be revoked.");
    } finally {
      setBusy(false);
    }
  };

  if (!canonicalShareAvailable) {
    return (
      <Panel>
        <PanelHeader title="Sharing" />
        <Status as="block" tone="info" title="External sharing is not available for this Render">
          <Stack>
            <p>{contractReason}</p>
            <p>
              No path token is created here, and no bearer is returned by any route on
              this screen.
            </p>
          </Stack>
        </Status>
      </Panel>
    );
  }

  const forbidden = policy?.state === "forbidden";

  return (
    <Stack>
      <Panel>
        <PanelHeader
          title="Share this Render"
          description="One revocable, expiring, audited link to exactly this immutable Render. It never follows the latest run, and it can reach nothing else in this project."
        />
        <Stack>
          {/* The posture, stated before the control rather than discovered by
              pressing it. The refusal names the gesture and who can make it,
              because what blocks this person is a decision somebody else takes. */}
          {forbidden ? (
            <Status
              as="block"
              tone="info"
              title="This project does not allow sharing outside the platform"
              data-testid="render-share-forbidden"
            >
              <Stack>
                <p>{policy?.gesture}</p>
                <p>
                  {policy?.decidedBy
                    ? `${policy.decidedBy} set this project to forbid external sharing.`
                    : "Nobody has allowed it yet: new projects forbid it until someone does."}
                </p>
              </Stack>
            </Status>
          ) : null}
          <Field
            label="Link expires"
            hint={`Required, and at most ${maxDays} days from now. A share that never expires is a grant nobody remembers giving.`}
          >
            {(field) => (
              <Input
                {...field}
                type="datetime-local"
                value={expiresAt}
                disabled={forbidden}
                onChange={(event) => setExpiresAt(event.target.value)}
              />
            )}
          </Field>
          <Cluster>
            <Button onClick={() => void create()} disabled={busy || !expiresAt || forbidden}>
              Request a share link
            </Button>
          </Cluster>
          {error ? (
            <Status as="block" tone="error" title="The share was not created">
              {error}
            </Status>
          ) : null}
          {requested ? (
            <Status
              as="block"
              tone="warning"
              title="Requested — a second person must confirm it"
              data-testid="render-share-requested"
            >
              <Stack>
                <p>
                  No link exists yet, and nothing has left this platform. Someone
                  else with the Edit role on this project has to confirm this
                  request; the link is created then, and shown to them once.
                </p>
                <p>
                  If nobody confirms it before{" "}
                  <Timestamp value={requested.confirmation_expires_at} />, the request
                  lapses and you can make a new one over the same Render.
                </p>
              </Stack>
            </Status>
          ) : null}
          {confirmed ? (
            <Status
              as="block"
              tone="warning"
              title="Copy this link now — it is shown once"
            >
              <Stack>
                <p>
                  Only a hash of this link was stored, so this server cannot show it
                  again. If it is lost, request a new share over the same Render; the
                  result the recipient sees is identical.
                </p>
                <code data-testid="render-share-delivery-url">{confirmed.delivery_url}</code>
                <Cluster>
                  {/* The link is above, in full, and it stays there: this is the
                      one screen where a clipboard write that quietly failed
                      would cost the person the only copy of the link. */}
                  <CopyButton
                    value={confirmed.delivery_url}
                    label="Copy link"
                    copiedLabel="Link copied"
                    data-testid="render-share-copy"
                    messageTestId="render-share-copy-refused"
                  />
                  <Button variant="ghost" onClick={() => setConfirmed(null)}>
                    I have copied it
                  </Button>
                </Cluster>
              </Stack>
            </Status>
          ) : null}
        </Stack>
      </Panel>

      <Panel>
        <PanelHeader
          title="Existing shares"
          description="State, expiry, creator and access counts. No link and no token: this server cannot rebuild either, and a list that could would put a live grant in every screenshot."
        />
        {shares === null && !error ? <Loading label="shares for this Render" /> : null}
        {error && shares === null ? (
          <Failure what="The shares of this Render" message={error} action={<Retry onClick={() => load()} />} />
        ) : null}
        {shares !== null && shares.length === 0 ? (
          <EmptyState
            title="This Render has never been shared"
            description="Creating a share here produces one link, shown once, that opens exactly this frozen Render."
          />
        ) : null}
        {shares !== null && shares.length > 0 ? (
          <TableScroll label="Shares of this Render">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Share</TableHead>
                  <TableHead>State</TableHead>
                  <TableHead>Authorized by</TableHead>
                  <TableHead>Expires</TableHead>
                  <TableHead>Created by</TableHead>
                  <TableHead>Created</TableHead>
                  <TableHead>Opened</TableHead>
                  <TableHead>Refused</TableHead>
                  <TableHead>Action</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {shares.map((share) => (
                  <TableRow key={share.share_id}>
                    <TableCell>
                      <ObjectId value={share.share_id} title="Share" />
                    </TableCell>
                    <TableCell>
                      {/* `active | revoked | expired` had a three-line map here
                          and the raw stored word beside it, so a revoked share
                          read `revoked` while every other screen of the console
                          said `Revoked`. Both come from the one vocabulary now,
                          and this screen disagrees with it about nothing. */}
                      <Badge tone={stateTone(share.state)}>{stateLabel(share.state)}</Badge>
                    </TableCell>
                    {/* The two-person rule, read off the row. A pending request
                        says WHO is waiting on WHOM; a live one names the person
                        who authorized the exit, which is the whole point of
                        having asked for a second one. */}
                    <TableCell>{authorizationCell(share)}</TableCell>
                    <TableCell><Timestamp value={share.expires_at} /></TableCell>
                    <TableCell>{share.created_by}</TableCell>
                    <TableCell><Timestamp value={share.created_at} /></TableCell>
                    <TableCell>{share.granted_access_count}</TableCell>
                    <TableCell>{share.refused_access_count}</TableCell>
                    <TableCell>
                      {/* `!forbidden` as well as `can_confirm`: the server reads
                          the Project's posture again at CONFIRMATION time, since
                          that is when the link is minted. Offering the control
                          while the Project forbids the exit would offer a button
                          whose only outcome is the refusal already printed above
                          it. What is left to do on a frozen request is withdraw
                          it, which is the branch below. */}
                      {share.can_confirm && !forbidden ? (
                        <Button
                          variant="ghost"
                          disabled={busy}
                          data-testid={`render-share-confirm-${share.share_id}`}
                          onClick={() => void confirm(share)}
                        >
                          Confirm this share
                        </Button>
                      ) : share.state === "active" ||
                        share.state === "pending_confirmation" ? (
                        <Button
                          variant="ghost"
                          disabled={busy}
                          onClick={() => setPendingRevoke(share)}
                        >
                          {share.state === "active" ? "Revoke" : "Withdraw"}
                        </Button>
                      ) : (
                        <span>{share.revoke_reason_code ?? "—"}</span>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableScroll>
        ) : null}
      </Panel>

      {/* Irreversible for the recipient, so it is confirmed before it is sent.
          The shared primitive, not a hand-rolled dialog: the copy and the
          consequence are this screen's, the shape is the console's. */}
      <ConfirmDialog
        open={pendingRevoke !== null}
        onOpenChange={(open) => {
          if (!open) setPendingRevoke(null);
        }}
        title={
          pendingRevoke?.state === "pending_confirmation"
            ? "Withdraw this share request?"
            : "Revoke this share?"
        }
        description={
          pendingRevoke?.state === "pending_confirmation"
            ? "No link exists yet, so nothing stops working. The request can no longer be confirmed by anyone, and a new one has to be made over the same Render."
            : "The recipient's link stops opening immediately, including a page they already have open. This cannot be undone: the same link can never be reissued, only a new one over the same Render."
        }
        confirmLabel={
          pendingRevoke?.state === "pending_confirmation" ? "Withdraw it" : "Revoke it"
        }
        cancelLabel="Keep the share"
        destructive
        busy={busy}
        onConfirm={() => pendingRevoke && void revoke(pendingRevoke)}
      />
    </Stack>
  );
}

export default RenderSharing;
