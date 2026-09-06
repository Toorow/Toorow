/**
 * Recovering ONE selected run — and saying the truth when nothing can recover it.
 *
 * WHAT `Recovery ineligible` WAS SAYING, AND WHY IT HAD TO GO. The server offers
 * only verbs its own preflight proved, and it proves none of the three bounded
 * ones: `synchronize`, `reload` and `reprocess` each refuse for want of an
 * engine, so the list reaching this dialog is empty on every failed run. The
 * button then read `Recovery ineligible`, which blames the RUN for a gap in the
 * BUILD, names no verb, and leaves a person with nowhere to go. Two of those
 * verbs are performed elsewhere in the product and one is genuinely missing —
 * three facts, and the screen was showing none of them.
 *
 * SO THE EMPTY LIST BECOMES A DOOR, not a dead control. It opens onto the same
 * per-verb standing the `Runs` tab panel states, rendered from the same
 * component so the two cannot drift. And a run that simply ended well says
 * `Nothing to repair`, which is a different fact and reads as one.
 *
 * `reconcile` is untouched: it resolves an execution that already exists, mints
 * nothing, and is the one verb the server still proves.
 */
import { useState } from "react";
import {
  Button, Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader,
  DialogTitle, EvidenceRows, Status,
} from "../../ui";
import { apiFetch } from "../../lib/apiFetch";
import { filledRecord, record, text, titleCase, type EvidenceRecord } from "./evidence";
import { boundedJson, unbuiltRecoveryVerbs, useBoundedRecovery } from "./boundedRecovery";
import { VerbStanding } from "./DatastreamReloadPanel";

export default function DatastreamRecoveryDialog({
  projectId,
  datastreamId,
  run,
  onConfirmed,
}: {
  projectId: string;
  datastreamId: string;
  run: EvidenceRecord;
  onConfirmed: () => void;
}) {
  const recovery = record(run.recovery);
  const kinds = Array.isArray(recovery?.kinds) ? recovery.kinds.filter((kind): kind is string => typeof kind === "string") : [];
  const interval = record(recovery?.interval);
  // DID THE SERVER ACTUALLY TRY? It carries per-verb refusals only when the run
  // ended badly enough to be worth repairing; a run that ended well is answered
  // before any preflight runs. Reading that rather than the state literal keeps
  // the run vocabulary in the one file that owns it.
  const attempted = record(recovery?.refusals) !== null;
  const unbuilt = kinds.length === 0 && attempted ? unbuiltRecoveryVerbs() : [];
  const explainOnly = unbuilt.length > 0;
  const [open, setOpen] = useState(false);
  // AI-144: the prepare/confirm ceremony is shared with DatastreamReloadPanel.
  // This dialog owns only what is specific to it — the scope of ONE run, and
  // the reconcile verb, which is not a bounded recovery at all.
  const bounded = useBoundedRecovery(projectId, datastreamId);
  const { preparation, busy, message } = bounded;

  // THE KIND THE PERSON ASKED FOR, kept so the error block can ask again
  // rather than making them re-choose (76-4).
  const [lastKind, setLastKind] = useState<string | null>(null);

  const prepare = async (kind: string) => {
    setLastKind(kind);
    if (kind === "reconcile") {
      bounded.setMessage(null);
      try {
        const response = await apiFetch(`/api/datastreams/${encodeURIComponent(datastreamId)}/executions/${encodeURIComponent(text(run.id))}/reconcile`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ project_id: projectId }),
        });
        await boundedJson(response, "Durable reconciliation failed.");
        bounded.setMessage({ text: "Durable execution evidence was reconciled without a blind retry.", tone: "success" });
        onConfirmed();
      } catch (reason) {
        bounded.setMessage({
          text: reason instanceof Error ? reason.message : "Durable reconciliation failed.",
          tone: "error",
        });
      }
      return;
    }
    await bounded.prepare({
      kind,
      reason: `${titleCase(kind)} selected run ${text(run.id)}`,
      date_from: interval?.from,
      date_to_exclusive: interval?.to_exclusive,
      chosen_mapping_version_id: run.mapping_version_id,
    });
  };

  const confirm = async () => {
    if ((await bounded.confirm()) === "completed") onConfirmed();
  };

  return (
    <>
      {/* Three labels for three different facts. A repair is offered, a repair
          is missing and can be explained, or there was nothing to repair — and
          a disabled button that names none of them is what this replaces. */}
      <Button
        variant={explainOnly ? "secondary" : "default"}
        disabled={kinds.length === 0 && !explainOnly}
        onClick={() => setOpen(true)}
        data-testid="recovery-open"
      >
        {kinds.length ? "Prepare recovery" : explainOnly ? "Why this cannot be repaired" : "Nothing to repair"}
      </Button>
      <Dialog open={open} onOpenChange={(next) => { setOpen(next); if (!next) bounded.reset(); }}>
        <DialogContent className="sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>
              {explainOnly ? `Repairing run ${text(run.id)}` : `Recover run ${text(run.id)}`}
            </DialogTitle>
            <DialogDescription>
              {explainOnly
                ? "The repairs named in this product's vocabulary are not all the same promise, and not all of them are missing. Each one below says what it would do, what it would cost, and where it lives if it exists."
                : "Only server-proven operations are offered. Each prepare freezes scope, versions, impact and rollback reference."}
            </DialogDescription>
          </DialogHeader>
          {message && <Status as="block" tone={message.tone}>{message.text}</Status>}
          {explainOnly ? (
            <div className="grid gap-4">
              {unbuilt.map((verb) => (
                <VerbStanding key={verb.origin.key} verb={verb} />
              ))}
            </div>
          ) : preparation && filledRecord(preparation) ? (
            <>
              <Status as="block" tone="warning" title="Exact review required">Confirming creates one durable operation. Current remains authoritative until a later candidate is published.</Status>
              {/* AC9: bound to exact scope, interval, versions, consequence,
                  expected pointers and idempotency key. The operator reads each
                  of them before confirming one durable operation. */}
              <EvidenceRows
                source={preparation}
                label="Frozen recovery scope and consequence"
                labels={{ scope: "Scope" }}
                className="max-h-72"
              />
            </>
          ) : preparation ? (
            <Status
              as="block"
              tone="error"
              title="Recovery evidence unavailable"
              action={lastKind ? <Retry onClick={() => void prepare(lastKind)} /> : undefined}
            >
              No exact consequence was returned, so nothing can be confirmed here.
            </Status>
          ) : (
            <div className="flex flex-wrap gap-2">
              {kinds.map((kind) => (
                <Button key={kind} variant="secondary" disabled={busy} onClick={() => void prepare(kind)}>
                  {titleCase(kind)}
                </Button>
              ))}
            </div>
          )}
          <DialogFooter>{preparation && filledRecord(preparation) && <Button disabled={busy} onClick={() => void confirm()}>{busy ? "Confirming…" : "Confirm exact recovery"}</Button>}</DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
