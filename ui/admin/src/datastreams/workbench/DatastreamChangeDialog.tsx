import { useEffect, useState } from "react";
import {
  ObjectId,
  Button,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Status,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
  TableScroll,
  Textarea,
  Retry,
} from "../../ui";
import { apiFetch } from "../../lib/apiFetch";
// WHAT IS BEING CONFIRMED, IN VALUES — 2026-08-18. The path-by-path table below
// carries two hashes per contract path, which is what `MutationResult` compares
// and NOT something a person can approve. The server composes the same
// difference in values (`core/mapping_value_diff.py`); this renders it, and the
// version ledger renders the very same entries when two versions are compared.
import MappingValueDiff, { type ValueDiff } from "./mapping/MappingValueDiff";

interface Preparation {
  preparation_id: string;
  confirmation_secret: string;
  review_hash: string;
  expires_in_seconds: number;
  review: {
    diff: Array<{ path: string; before_hash: string; after_hash: string }>;
    /** Absent on a server that predates the amendment; the hashes stay either way. */
    value_diff?: ValueDiff;
    consequence: string;
    expected_plan_version_id: string;
    expected_mapping_version_id: string;
    /**
     * WHAT THE BASE OF THIS CHANGE IS, AND WHETHER ANYTHING RUNS ON IT.
     *
     * Amendment 4 of the 2026-08-11 review, seam half. `in_force` is a version a
     * pointer names; `head_of_ledger` is the most recent recorded version on a
     * Datastream that has never published — 6 of the 8 live ones — and it carries
     * its own sentence, because confirming a change against it still makes
     * nothing live. Absent on a server that predates the amendment, which is why
     * every reader below tolerates `undefined` and falls back to the id alone.
     */
    base_versions?: {
      plan?: BaseVersion;
      mapping?: BaseVersion;
    };
  };
}

interface BaseVersion {
  state: "in_force" | "head_of_ledger" | string;
  version_id: string;
  /** Present only on `head_of_ledger`, and rendered verbatim. */
  reason?: string;
}

/** The label of the version being changed, which is NOT always the active one.
 *
 *  It read « Expected active plan » on every Datastream, and on the 6 that have
 *  no pointer nothing is active — the label lied about the very thing the person
 *  was being asked to approve. */
function baseLabel(axis: string, base: BaseVersion | undefined): string {
  if (base?.state === "head_of_ledger") return `Most recent ${axis} (nothing in force)`;
  return `Expected active ${axis}`;
}

async function body<T>(response: Response, fallback: string): Promise<T> {
  const value = await response.json().catch(() => null) as { message?: string } | null;
  if (!response.ok) throw new Error(value?.message || fallback);
  return value as T;
}

export default function DatastreamChangeDialog({
  projectId,
  datastreamId,
  kind,
  initialPayload,
  onConfirmed,
  proposedPayload,
  triggerLabel,
  scope,
  disabled,
  testId,
  rawImportId,
}: {
  projectId: string;
  datastreamId: string;
  kind: "mapping" | "processing";
  initialPayload: Record<string, unknown> | null;
  onConfirmed: () => void;
  /**
   * Story 60.6. A payload the SCREEN built, so the act does not go through the
   * raw JSON contract below.
   *
   * Excluding a column was reachable only by hand-editing a mapping contract in
   * a `Textarea` and re-typing it without a typo — which is not a control, it is
   * a way of saying the control was never built. When this is supplied the
   * textarea is not rendered at all: there is nothing to edit, the change is
   * already exactly what the person clicked.
   */
  proposedPayload?: Record<string, unknown> | null;
  triggerLabel?: string;
  /**
   * What the confirmation is ABOUT, in words, counted BEFORE the act — the rule
   * every destructive confirmation in this console follows. An excluded column
   * stops landing, so the count that matters is the one being excluded now.
   */
  scope?: string | null;
  disabled?: boolean;
  testId?: string;
  /** Retained file that opened this repair. The server freezes it beside the
   *  exact plan, mapping and Template versions; generic changes omit it. */
  rawImportId?: string | null;
}) {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState("");
  const [preparation, setPreparation] = useState<Preparation | null>(null);
  const [state, setState] = useState<"idle" | "busy" | "confirmed">("idle");
  const [error, setError] = useState<string | null>(null);
  const authored = proposedPayload !== undefined;
  useEffect(() => {
    if (open) setDraft(JSON.stringify(initialPayload ?? {}, null, 2));
  }, [initialPayload, open]);

  const prepare = async () => {
    setState("busy");
    setError(null);
    try {
      const contract: unknown = authored ? proposedPayload : (JSON.parse(draft) as unknown);
      if (typeof contract !== "object" || contract === null || Array.isArray(contract)) {
        throw new Error("The proposed contract must be a JSON object.");
      }
      const base = `/api/projects/${encodeURIComponent(projectId)}/datastreams/${encodeURIComponent(datastreamId)}/workbench`;
      const response = await apiFetch(`${base}/${kind}/changes`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify({
          proposed_payload: contract,
          ...(rawImportId ? { raw_import_id: rawImportId } : {}),
        }),
      });
      setPreparation(await body<Preparation>(response, "Change review could not be prepared."));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Change review could not be prepared.");
    } finally {
      setState("idle");
    }
  };

  const confirm = async () => {
    if (!preparation) return;
    setState("busy");
    setError(null);
    try {
      const base = `/api/projects/${encodeURIComponent(projectId)}/datastreams/${encodeURIComponent(datastreamId)}/workbench`;
      const response = await apiFetch(`${base}/changes/${encodeURIComponent(preparation.preparation_id)}/confirm`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ confirmation_secret: preparation.confirmation_secret }),
      });
      await body(response, "Change confirmation failed.");
      setState("confirmed");
      onConfirmed();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The durable outcome is unknown; reconcile before retrying.");
      setState("idle");
    }
  };

  const label = kind === "mapping" ? "mapping" : "processing";
  return (
    <>
      <Button
        disabled={disabled ?? !initialPayload}
        data-testid={testId}
        onClick={() => setOpen(true)}
      >
        {triggerLabel ?? `Prepare ${label} change`}
      </Button>
      <Dialog open={open} onOpenChange={(next) => { setOpen(next); if (!next) { setPreparation(null); setError(null); setState("idle"); } }}>
        <DialogContent className="sm:max-w-3xl">
          <DialogHeader>
            <DialogTitle>Prepare {label} change</DialogTitle>
            <DialogDescription>{authored ? "Review a full immutable contract. Confirmation creates non-live versions and one candidate; Current stays unchanged." : "Advanced: edit the raw JSON contract directly. For field-level changes (exclude, join, split), use the controls in the binding table instead."}</DialogDescription>
          </DialogHeader>
          {error && (
            <Status
              as="block"
              tone="error"
              title="Change unavailable"
              action={<Retry onClick={() => void prepare()} />}
            >
              {error}
            </Status>
          )}
          {/* Story 60.6. The scope, counted BEFORE the act — an excluded column
              stops landing, so the number that has to be on screen is the one
              about to be excluded, never the remainder afterwards. */}
          {scope && state !== "confirmed" && (
            <Status as="block" tone="warning" title="What this change covers" data-testid="change-scope">
              {scope}
            </Status>
          )}
          {state === "confirmed" ? (
            <Status as="block" tone="success" title="Candidate dispatched">The exact change was confirmed. Its immutable versions remain non-live until Outputs publication.</Status>
          ) : preparation ? (
            <>
              <Status as="block" tone="warning" title="Exact confirmation required">{preparation.review.consequence}</Status>
              <dl className="grid grid-cols-2 gap-3 text-ui">
                <div>
                  <dt className="text-text-secondary">
                    {baseLabel("plan", preparation.review.base_versions?.plan)}
                  </dt>
                  <dd className="m-0"><ObjectId value={preparation.review.expected_plan_version_id} title="Plan version" /></dd>
                </div>
                <div>
                  <dt className="text-text-secondary">
                    {baseLabel("mapping", preparation.review.base_versions?.mapping)}
                  </dt>
                  <dd className="m-0"><ObjectId value={preparation.review.expected_mapping_version_id} title="Mapping version" /></dd>
                </div>
              </dl>
              {/* The server's own sentence, whole, and only when it sent one. It
                  says the consequence the label cannot carry: confirming against
                  a head makes nothing live. */}
              {[preparation.review.base_versions?.plan, preparation.review.base_versions?.mapping]
                .filter((base): base is BaseVersion => Boolean(base?.reason))
                .slice(0, 1)
                .map((base) => (
                  <Status as="block" tone="neutral" key={base.version_id} data-testid="base-not-in-force">
                    {base.reason}
                  </Status>
                ))}
              {/* WHAT CHANGES, IN VALUES, FIRST — 2026-08-18. It sits above the
                  hashes because it is the thing being confirmed; the hashes are
                  the identity of what was frozen, and an identity is checked
                  after a decision, not instead of one. */}
              <MappingValueDiff
                diff={preparation.review.value_diff}
                label={{ before: "Base", after: "Proposed" }}
                emptyTitle="No reading of this contract changes"
                emptyDescription={
                  "Every reading this Datastream is read by is identical on both sides. The "
                  + "contract paths below still differ — key order or a value nothing reads."
                }
                testId="change-value-diff"
              />
              {/* The diff is what the operator is confirming. It is a typed
                  list of paths with a before and an after hash, so it reads as
                  a table -- AC5 asks for "scope and consequence before
                  confirmation", which braces do not give. */}
              {preparation.review.diff.length === 0 ? (
                <Status as="block" tone="neutral" title="No contract path changes">
                  The proposed contract is identical to the active one on every governed path.
                </Status>
              ) : (
                <TableScroll label={`Proposed ${label} change, path by path`} className="max-h-64">
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Contract path</TableHead>
                        {/* THE BASE, NOT THE ACTIVE VERSION. On a Datastream
                            with no pointer the two are different things, and
                            this column shows the first. */}
                        <TableHead>Base</TableHead>
                        <TableHead>Proposed</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {preparation.review.diff.map((change) => (
                        <TableRow key={change.path}>
                          <TableCell className="font-mono">{change.path}</TableCell>
                          <TableCell className="font-mono text-caption">{change.before_hash || "Absent"}</TableCell>
                          <TableCell className="font-mono text-caption">{change.after_hash || "Removed"}</TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </TableScroll>
              )}
            </>
          ) : authored ? null : (
            <>
              {/* WHY THIS DOOR IS STILL HERE, AND WHAT IT COSTS — 2026-08-18.
                  It is not decoration and it is not retired: the row controls
                  write three paths of this contract (`binding.status`,
                  `binding.mdm_target`, `column_treatments`), and a Datastream
                  with no version yet has NO row to press at all. Everything else
                  the contract carries — the grain, a semantic role, an
                  aggregation — is reachable from here and from nowhere else.
                  What it does not carry is the refusals: a claimed concept, a
                  join of one column, a split pattern in the wrong dialect are
                  all stated by the controls BEFORE the click and discovered here
                  only when the append refuses. The risk is named rather than
                  implied by the word "Advanced". */}
              <Status as="block" tone="warning" title="This edits the whole contract by hand" data-testid="raw-contract-risk">
                The binding table refuses a concept already claimed, a join of fewer than two
                columns and a split pattern the engine cannot read — before you click. Typing the
                contract here reaches paths no control does, and those refusals arrive only at the
                append. The review on the next step shows exactly what you changed.
              </Status>
              <Textarea aria-label={`Proposed ${label} contract`} className="min-h-80 font-mono text-caption" value={draft} onChange={(event) => setDraft(event.target.value)} />
            </>
          )}
          <DialogFooter>
            {state === "confirmed" ? (
              <Button onClick={() => setOpen(false)}>Close</Button>
            ) : preparation ? (
              <Button disabled={state === "busy"} onClick={() => void confirm()}>
                {state === "busy" ? "Confirming…" : "Confirm and dispatch candidate"}
              </Button>
            ) : (
              <Button
                data-testid="prepare-exact-review"
                disabled={state === "busy" || (authored ? !proposedPayload : !initialPayload)}
                onClick={() => void prepare()}
              >
                {state === "busy" ? "Preparing…" : "Prepare exact review"}
              </Button>
            )}
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
