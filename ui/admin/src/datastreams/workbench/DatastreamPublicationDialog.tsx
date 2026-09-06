import { useState } from "react";
import {
  Button,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  EvidenceRows,
  Status,
} from "../../ui";
import { apiFetch } from "../../lib/apiFetch";
import { filledRecord, record, text, type EvidenceRecord } from "./evidence";

/** The outcome of an irreversible action, CARRIED rather than guessed.
 *
 *  Both dialogs decided their banner colour by looking for the substrings
 *  "unknown" or "failed" in the message text, and rendered anything else as a
 *  SUCCESS. A design review reproduced it live: a genuine `HTTP 404` -- which
 *  contains neither word -- appeared inside a green success banner, on the
 *  screen that publishes and rolls back. A failure shown as a success is the one
 *  outcome a person cannot recover from by reading more carefully.
 *
 *  The caller always knows which happened. It says so now. */
interface Outcome { text: string; tone: "success" | "error" }
const failed = (text: string): Outcome => ({ text, tone: "error" });
const succeeded = (text: string): Outcome => ({ text, tone: "success" });

/** The identity and version references AC8 requires on the candidate review. */
const CANDIDATE_FIELDS = [
  "execution_id",
  "plan_version_id",
  "mapping_version_id",
  "artifact_ref",
  "row_count",
  "expected_current_execution_id",
] as const;

async function json(response: Response, fallback: string): Promise<EvidenceRecord> {
  const value = await response.json().catch(() => null);
  const parsed = record(value);
  if (!response.ok || !parsed) throw new Error(typeof parsed?.message === "string" ? parsed.message : fallback);
  return parsed;
}

export function DatastreamPublicationDialog({
  projectId,
  datastreamId,
  candidateId,
  onConfirmed,
}: {
  projectId: string;
  datastreamId: string;
  candidateId: string | null;
  onConfirmed: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [review, setReview] = useState<EvidenceRecord | null>(null);
  const [confirmation, setConfirmation] = useState<EvidenceRecord | null>(null);
  const [idempotencyKey, setIdempotencyKey] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<Outcome | null>(null);
  const root = `/api/projects/${encodeURIComponent(projectId)}/datastreams/${encodeURIComponent(datastreamId)}/executions`;

  const load = async () => {
    if (!candidateId) return;
    setOpen(true);
    setBusy(true);
    try {
      setReview(await json(await apiFetch(`${root}/${encodeURIComponent(candidateId)}/candidate-review`, { cache: "no-store" }), "Candidate review is unavailable."));
    } catch (reason) {
      setMessage(failed(reason instanceof Error ? reason.message : "Candidate review is unavailable."));
    } finally {
      setBusy(false);
    }
  };
  const prepare = async () => {
    if (!candidateId || !review) return;
    setBusy(true);
    try {
      const key = crypto.randomUUID();
      const value = await json(await apiFetch(`${root}/${encodeURIComponent(candidateId)}/publish-confirmations`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "Idempotency-Key": key },
        body: "{}",
      }), "Publication confirmation could not be prepared.");
      if (value.review_hash !== review.review_hash) throw new Error("Confirmation was not bound to this unchanged review.");
      setConfirmation(value);
      setIdempotencyKey(key);
    } catch (reason) {
      setMessage(failed(reason instanceof Error ? reason.message : "Publication confirmation could not be prepared."));
    } finally {
      setBusy(false);
    }
  };
  const confirm = async () => {
    if (!candidateId || !confirmation || !idempotencyKey) return;
    setBusy(true);
    try {
      const response = await apiFetch(`${root}/${encodeURIComponent(candidateId)}/publish-activate`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "Idempotency-Key": idempotencyKey },
        body: JSON.stringify({
          confirmation_ref: confirmation.confirmation_ref,
          confirmation_secret: confirmation.confirmation_secret,
        }),
      });
      if (response.status >= 500) {
        setMessage(failed("Publication outcome is unknown. Reconcile durable evidence before retrying."));
        return;
      }
      await json(response, "Publication failed.");
      setMessage(succeeded(`Execution ${candidateId} is now Current.`));
      setReview(null);
      setConfirmation(null);
      setIdempotencyKey(null);
      onConfirmed();
    } catch {
      setMessage(failed("Publication outcome is unknown. Reconcile durable evidence before retrying."));
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <Button disabled={!candidateId} onClick={() => void load()}>Review candidate</Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="sm:max-w-3xl">
          <DialogHeader>
            <DialogTitle>Review candidate</DialogTitle>
            <DialogDescription>
              Only an unchanged Ready candidate can atomically advance execution, plan,
              mapping and publication references.
            </DialogDescription>
          </DialogHeader>
          {message && <Status as="block" tone={message.tone}>{message.text}</Status>}
          {busy && !review ? <Status as="block" active>Loading exact candidate evidence…</Status> : review && (
            <>
              <dl className="grid grid-cols-2 gap-3 text-ui">
                {CANDIDATE_FIELDS.map((field) => (
                  <div key={field}>
                    <dt className="text-text-secondary">{field.replaceAll("_", " ")}</dt>
                    <dd className="m-0 font-mono">{text(review[field], "None")}</dd>
                  </div>
                ))}
              </dl>
              {/* AC8: the candidate review shows readiness, DQ, plan, mapping,
                  processing, Output and expected-current evidence. Each of the
                  three governed records is read field by field. */}
              {[
                ["dq", "Data quality outcome"],
                ["output_plan", "Output plan"],
                ["schedule", "Cadence after publication"],
              ].map(([field, heading]) => {
                const source = record(review[field]);
                return (
                  <section key={field}>
                    <h3 className="mb-2 text-label text-text-secondary">{heading}</h3>
                    {source ? (
                      <EvidenceRows source={source} label={heading} className="max-h-48" />
                    ) : (
                      <Status as="block" tone="warning">
                        {heading} evidence is unavailable for this candidate.
                      </Status>
                    )}
                  </section>
                );
              })}
            </>
          )}
          <DialogFooter>
            {review && (confirmation ? (
              <Button disabled={busy} onClick={() => void confirm()}>Confirm publish atomically</Button>
            ) : (
              <Button disabled={busy} onClick={() => void prepare()}>Prepare publication confirmation</Button>
            ))}
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

export function DatastreamRollbackDialog({
  projectId,
  datastreamId,
  targetExecutionId,
  onConfirmed,
}: {
  projectId: string;
  datastreamId: string;
  targetExecutionId: string | null;
  onConfirmed: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [preparation, setPreparation] = useState<EvidenceRecord | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<Outcome | null>(null);
  const root = `/api/projects/${encodeURIComponent(projectId)}/datastreams/${encodeURIComponent(datastreamId)}/workbench/outputs/rollback-preparations`;
  const prepare = async () => {
    setOpen(true);
    setBusy(true);
    try {
      setPreparation(await json(await apiFetch(root, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target_execution_id: targetExecutionId }),
      }), "Rollback review could not be prepared."));
    } catch (reason) {
      setMessage(failed(reason instanceof Error ? reason.message : "Rollback review could not be prepared."));
    } finally {
      setBusy(false);
    }
  };
  const confirm = async () => {
    if (!preparation) return;
    setBusy(true);
    try {
      await json(await apiFetch(`${root}/${encodeURIComponent(text(preparation.preparation_id))}/confirm`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ confirmation_secret: preparation.confirmation_secret }),
      }), "Rollback confirmation failed.");
      setMessage(succeeded(`Publication ${targetExecutionId} and its compatible operating set are Current.`));
      setPreparation(null);
      onConfirmed();
    } catch (reason) {
      setMessage(failed(reason instanceof Error ? reason.message : "Rollback outcome is unknown; reconcile before retrying."));
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <Button variant="secondary" disabled={!targetExecutionId} onClick={() => void prepare()}>Prepare rollback</Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>Review rollback</DialogTitle>
            <DialogDescription>
              This restores one exact compatible publication, plan, mapping, lifecycle
              and schedule set.
            </DialogDescription>
          </DialogHeader>
          {message && <Status as="block" tone={message.tone}>{message.text}</Status>}
          {busy && !preparation ? (
            <Status as="block" active>Preparing exact rollback evidence…</Status>
          ) : preparation && (
            /* AC9: bound to exact scope, versions, consequence and expected
               pointers. A rollback is irreversible; it is read, not dumped. */
            /* `filledRecord`, not `record`: `record({})` is truthy, so an empty
               consequence took this branch, `EvidenceRows` rendered nothing, and
               the confirm button below stayed ENABLED. An irreversible action
               was confirmable with no evidence on screen -- the exact opposite
               of the line above it. */
            filledRecord(preparation.review)
              ? <EvidenceRows source={filledRecord(preparation.review)!} label="Exact rollback consequence" className="max-h-72" />
              : <Status as="block" tone="error" title="Rollback evidence unavailable"
          action={<Retry onClick={() => void load()} />}
        >
                  No exact consequence was returned, so nothing can be confirmed here.
                </Status>
          )}
          <DialogFooter>
            {preparation && filledRecord(preparation.review) && (
              <Button disabled={busy} onClick={() => void confirm()}>Confirm exact rollback</Button>
            )}
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
