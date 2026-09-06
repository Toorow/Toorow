import { useEffect, useRef, useState } from "react";
import { apiFetch } from "../lib/apiFetch";
import { Button, Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, Status } from "../ui";

/**
 * Governed publication — the human confirmation surface (AI-193).
 *
 * `publication_reviews_api.py#_prepare_publication_review_console` states the
 * invariant this component exists to
 * serve, and states the consequence of not serving it:
 *
 *   > The MCP tool `review_agent_change` DELIBERATELY DROPS that secret before
 *   > returning […] That leaves a gap: **without an out-of-band retrieval path,
 *   > nobody can ever call `confirm_and_publish` and governed publication is
 *   > unreachable.** These REST endpoints ARE that out-of-band path.
 *
 * Governed publication was unreachable. Not because a route was missing — all
 * three are served — but because this modal implemented a flow that did not
 * exist. It took an already-made `candidate`, posted **no body** and **no
 * header**, and nothing mounted it. Three defects, each fatal on its own:
 *
 *  - no `Idempotency-Key` → 422 `missing_idempotency_key` on both actions;
 *  - `/confirm` demands a `confirmation_secret` it had no way to hold;
 *  - `/rollback` demands a `target_mapping_version_id` it never sent.
 *
 * Each was measured against a real server, not deduced —
 * `scripts/org_membership_walk.py` posts the header with an empty body and gets
 * 422 `invalid_request` back.
 *
 * **The flow, as the server defines it.** The review is not something the caller
 * already has; it is something this surface *mints*:
 *
 *   1. `POST /api/governance/publication-reviews` `{proposal_id}` → 201 with the
 *      review AND the opaque `confirmation_secret`, returned **exactly once** to
 *      an authenticated human holding `manage`. This is the only surface in the
 *      product that ever sees that secret.
 *   2. the human reads what will move — the pointer, from which version to which;
 *   3. `POST …/{confirmation_id}/confirm` carries the secret back.
 *
 * **The secret lives in a ref, never in state.** It is not rendered, not logged,
 * not persisted, and it is cleared when the dialog closes. A re-render must not
 * be able to leak it into the DOM, which is the whole point of the split: an
 * agent may inspect the diff through MCP and can never confirm.
 *
 * Rollback is a DISTINCT act, not the opposite button: it advances the pointer
 * back to `prior_mapping_version_id`, needs no secret (the manage floor
 * authorizes it), and is only offered when the review names a prior version —
 * a first publication has nothing to roll back to.
 *
 * Still true, and not this file's to fix: **no screen mounts this component.**
 * `governance.md:707-708` puts *"immutable versions, diffs, status, actor and
 * confirmation"* under Versions & Approvals, and that surface has no home yet.
 * The component is now correct; reaching it is the remaining half.
 */

/** Both mutations REFUSE a request without this header. Held per command so a
 *  retry after a 5xx replays the SAME publication rather than a second one. */
function idempotencyKey(): string {
  return typeof crypto?.randomUUID === "function"
    ? crypto.randomUUID()
    : `idem-${Math.random().toString(36).slice(2)}${Date.now().toString(36)}`;
}

/** The model-safe half of what `prepare_publication_review` returns
 *  (`governed_publication.py:577`). The secret is deliberately NOT typed here:
 *  it never travels with this object. */
export interface PublicationReview {
  confirmation_id: string;
  scope?: { actor?: string; project_id?: string; datastream_id?: string };
  versions?: {
    candidate_mapping_version_id?: string;
    prior_mapping_version_id?: string | null;
    policy_version?: string;
  };
  impact?: { advances_pointer?: boolean; from_version_id?: string | null; to_version_id?: string };
  diff?: unknown;
}

interface PublicationReviewModalProps {
  open: boolean;
  projectId: string;
  /** The `ready` mapping proposal this publication would consume. The review is
   *  minted from it here — the caller does not hold one. */
  proposalId: string | null;
  onClose: () => void;
  /** Called after the pointer actually moved (confirm or rollback). */
  onPublished: () => void;
}

export default function PublicationReviewModal({
  open,
  projectId,
  proposalId,
  onClose,
  onPublished,
}: PublicationReviewModalProps) {
  const [review, setReview] = useState<PublicationReview | null>(null);
  const [preparing, setPreparing] = useState(false);
  const [busy, setBusy] = useState<"confirm" | "rollback" | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Never state: state is rendered, and this must not be renderable.
  const secret = useRef<string | null>(null);
  const keys = useRef<Record<string, string>>({});

  useEffect(() => {
    if (!open || !proposalId) return;
    let alive = true;
    setPreparing(true);
    setError(null);
    setReview(null);
    secret.current = null;
    void (async () => {
      try {
        const resp = await apiFetch("/api/governance/publication-reviews", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ proposal_id: proposalId }),
        });
        if (!resp.ok) {
          const body = (await resp.json().catch(() => ({}))) as { message?: string };
          throw new Error(body.message ?? `HTTP ${resp.status}`);
        }
        const payload = (await resp.json()) as PublicationReview & { confirmation_secret?: string };
        if (!alive) return;
        // Split the moment it arrives: the secret goes to the ref, everything
        // else to state. Nothing downstream can render what it never receives.
        const { confirmation_secret, ...safe } = payload;
        secret.current = confirmation_secret ?? null;
        setReview(safe as PublicationReview);
        if (!confirmation_secret) {
          setError(
            "The review was created but no confirmation secret came back. It is returned once and " +
              "cannot be re-read, so this review cannot be confirmed — close and open a new one.",
          );
        }
      } catch (err) {
        if (alive) setError(err instanceof Error ? err.message : String(err));
      } finally {
        if (alive) setPreparing(false);
      }
    })();
    return () => { alive = false; };
  }, [open, proposalId]);

  function close() {
    // The secret does not outlive the dialog.
    secret.current = null;
    keys.current = {};
    setReview(null);
    setError(null);
    onClose();
  }

  async function run(action: "confirm" | "rollback", body: Record<string, unknown>) {
    if (!review) return;
    setBusy(action);
    setError(null);
    try {
      const command = `${review.confirmation_id}:${action}`;
      keys.current[command] ??= idempotencyKey();
      const resp = await apiFetch(
        `/api/governance/publication-reviews/${encodeURIComponent(review.confirmation_id)}/${action}` +
          `?project_id=${encodeURIComponent(projectId)}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json", "Idempotency-Key": keys.current[command] },
          body: JSON.stringify(body),
        },
      );
      if (resp.ok || resp.status < 500) delete keys.current[command];
      if (!resp.ok) {
        const payload = (await resp.json().catch(() => ({}))) as { message?: string };
        throw new Error(payload.message ?? `HTTP ${resp.status}`);
      }
      onPublished();
      close();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  const prior = review?.versions?.prior_mapping_version_id ?? null;
  const canConfirm = review !== null && secret.current !== null && busy === null;

  return (
    <Dialog open={open} onOpenChange={(value) => { if (!value) close(); }}>
      <DialogContent className="sm:max-w-[550px]">
        <DialogHeader>
          <DialogTitle>Confirm this publication</DialogTitle>
          <DialogDescription>
            Publishing moves the Datastream's live mapping pointer. Read what moves, and to
            which version, before confirming.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 py-2">
          {preparing ? (
            <Status as="block" active title="Preparing the review">
              Assembling the exact versions this publication would make live.
            </Status>
          ) : null}

          {review ? (
            <div className="space-y-2 rounded-md border border-divider-base p-4 text-ui" data-testid="publication-review">
              <div className="flex justify-between gap-4">
                <span className="text-text-secondary">Datastream</span>
                <span className="font-mono text-caption">{review.scope?.datastream_id ?? "—"}</span>
              </div>
              <div className="flex justify-between gap-4">
                <span className="text-text-secondary">Currently live</span>
                <span className="font-mono text-caption">{prior ?? "nothing — this is the first publication"}</span>
              </div>
              <div className="flex justify-between gap-4">
                <span className="text-text-secondary">Becomes live</span>
                <span className="font-mono text-caption font-semibold">
                  {review.versions?.candidate_mapping_version_id ?? "—"}
                </span>
              </div>
              <div className="flex justify-between gap-4 border-t border-divider-base pt-2">
                <span className="text-text-secondary">Policy version</span>
                <span className="font-mono text-caption">{review.versions?.policy_version ?? "—"}</span>
              </div>
            </div>
          ) : null}

          {error ? <Status tone="error" data-testid="publication-review-error">{error}</Status> : null}
        </div>

        <DialogFooter className="flex gap-2">
          <Button variant="secondary" onClick={close} disabled={busy !== null}>
            Cancel
          </Button>
          {/* Only when there IS a prior version: a first publication has nothing
              to roll back to, and a button that always fails is not a control. */}
          {prior ? (
            <Button
              variant="secondary"
              disabled={busy !== null}
              data-testid="publication-rollback"
              onClick={() => void run("rollback", { target_mapping_version_id: prior })}
            >
              {busy === "rollback" ? "Rolling back…" : "Roll back to the live version"}
            </Button>
          ) : null}
          <Button
            variant="default"
            disabled={!canConfirm}
            data-testid="publication-confirm"
            onClick={() => void run("confirm", { confirmation_secret: secret.current })}
          >
            {busy === "confirm" ? "Publishing…" : "Confirm and publish"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
