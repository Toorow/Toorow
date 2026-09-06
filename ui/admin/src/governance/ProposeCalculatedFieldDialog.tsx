/**
 * "Propose as governed field" — the console door of the promotion rail (75-2).
 *
 * WHAT IT IS FOR. A person reading a Result works out a calculation the shared
 * model does not carry — spend over clicks, margin over revenue. Before this
 * dialog that calculation stayed in the exploration: the only place a formula
 * could be authored was `NewConceptDialog`, four screens away, with nothing
 * pre-filled and no record of where the idea came from. The rail
 * (`docs/product-architecture/context-hub.md`, amendment of 2026-09-05) says
 * the way back into the model is a PROPOSAL, reviewed by a person, and this is
 * where one is written.
 *
 * IT DOES NOT BUILD A SECOND FORMULA EDITOR. `FormulaTreeEditor` is the one
 * editor of the console, extracted from `NewConceptDialog` for this story; the
 * serializer is `buildExpression` (`formulaContract.ts`), the mirror of the
 * server's `ALLOWED_OPERATIONS`. Two builders would be two contracts, and the
 * server validates one.
 *
 * WHAT IT PRE-FILLS, AND FROM WHAT. The Definitions lens of the Result
 * (`analyze-result-lens.v1`) carries, per member, the EXACT `(concept_id,
 * version_id)` the execution pinned. Those are the references offered first,
 * labelled with the version they were executed at, and a two-measure Result
 * opens as a ratio of its first two measures — the shape the promotion is
 * almost always about. The Project's published Concepts are offered beside
 * them, so a formula can reach a Concept the Result did not name.
 *
 * WHEN NO PIN IS AVAILABLE, IT SAYS SO. An exploration whose measures are not
 * Semantic Concepts — the pivot explorer reads Datastream matches by canonical
 * field — exposes no `(concept_id, version_id)` pair. The dialog states that in
 * one sentence and falls back to this Project's published Concepts. It never
 * sends a reference without a version: "a reference to a Concept without a
 * version follows `latest` and is not a reference".
 *
 * WHAT IT NEVER SENDS. `origin`. The door stamps `human` on anything the
 * console files; a body that could say `agent` would let a person file a
 * machine's proposal.
 */
import { useCallback, useEffect, useMemo, useState } from "react";

import { ApiError } from "../lib/apiFetch";
import {
  Button,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Input,
  Label,
  Status,
  Textarea,
} from "../ui";
import { fetchResultLens, type DefinitionsLens } from "../analyze/workbenchClient";
import FormulaTreeEditor, { type ReferenceState } from "./FormulaTreeEditor";
import {
  activeOperands,
  buildExpression,
  emptyFormula,
  operandIsIncomplete,
  type FormulaDraft,
} from "./formulaContract";
import { getGovernanceCollection } from "./governanceSurface";
import {
  createProposal,
  REFUSAL_GESTURE,
  type CalculatedFieldProposal,
  type NamedRefusal,
} from "./calculatedFieldProposalsClient";

/**
 * THE GESTURE MAP MOVED, AND IS RE-EXPORTED HERE ON PURPOSE.
 *
 * It used to be declared in this file, which is why the review queue on the
 * other end of the same rail printed bare codes: it could not reach it without
 * importing a dialog. It now lives beside the three doors
 * (`calculatedFieldProposalsClient.ts`), so both ends say one sentence for one
 * code. Re-exported so a reader who looks for it where it was still finds it.
 */
export { REFUSAL_GESTURE };

/** The measures a Result pinned, in the order the Definitions lens returned. */
export interface PinnedMeasure {
  conceptId: string;
  versionId: string;
  label: string;
}

type LoadState =
  | { status: "loading" }
  | { status: "ready"; measures: PinnedMeasure[]; pinsUnavailableReason: string | null }
  | { status: "error"; message: string };

type SubmitState =
  | { status: "idle" }
  | { status: "sending" }
  | { status: "refused"; code: string; message: string; refusals: NamedRefusal[] }
  | { status: "filed"; proposal: CalculatedFieldProposal };

function measuresOf(lens: DefinitionsLens | undefined): PinnedMeasure[] {
  if (!lens || !Array.isArray(lens.members)) return [];
  return lens.members
    .filter((member) => member.kind === "measure" && member.resolved && member.concept_id && member.version_id)
    .map((member) => ({
      conceptId: member.concept_id,
      versionId: member.version_id,
      label: member.label || member.concept_id,
    }));
}

/** The pre-filled draft: a ratio of the first two measures the Result pinned.
 *  With fewer than two, the shape is offered empty rather than half-guessed. */
export function seedFromMeasures(measures: PinnedMeasure[]): FormulaDraft {
  const draft = emptyFormula();
  if (measures.length < 2) return draft;
  return {
    ...draft,
    op: "ratio",
    numerator: {
      op: "concept_ref",
      reference: `${measures[0].conceptId}|${measures[0].versionId}`,
      literalValue: "",
      literalType: "decimal",
    },
    denominator: {
      op: "concept_ref",
      reference: `${measures[1].conceptId}|${measures[1].versionId}`,
      literalValue: "",
      literalType: "decimal",
    },
  };
}

export default function ProposeCalculatedFieldDialog({
  open,
  onClose,
  projectId,
  resultId,
  querySpecVersionId = null,
  queueHref = null,
  onFiled,
}: {
  open: boolean;
  onClose: () => void;
  projectId: string;
  resultId: string;
  querySpecVersionId?: string | null;
  /** The address of the Governance queue, built by the router by the caller.
   *  `null` when the caller has no organization in hand — the confirmation then
   *  NAMES the queue in words rather than offering a link nobody validated. */
  queueHref?: string | null;
  onFiled?: (proposal: CalculatedFieldProposal) => void;
}) {
  const [load, setLoad] = useState<LoadState>({ status: "loading" });
  const [references, setReferences] = useState<ReferenceState>({ status: "loading" });
  // THE CONCEPT READ THE FORMULA EDITOR OFFERS TO REPEAT (76-4), for the
  // reason `NewConceptDialog` gives beside its own.
  const [referencesAttempt, setReferencesAttempt] = useState(0);
  const [formula, setFormula] = useState<FormulaDraft>(emptyFormula());
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [submit, setSubmit] = useState<SubmitState>({ status: "idle" });

  // WHAT THE EXPLORATION PINNED. Read here rather than passed in, because the
  // two callers hold different things: the Result Workbench renders one lens at
  // a time and the pivot explorer holds no Concept at all. One read, one shape.
  useEffect(() => {
    if (!open) return undefined;
    const controller = new AbortController();
    setLoad({ status: "loading" });
    setSubmit({ status: "idle" });
    fetchResultLens(projectId, resultId, "definitions", { signal: controller.signal })
      .then((envelope) => {
        const measures = measuresOf(envelope.definitions);
        setLoad({
          status: "ready",
          measures,
          pinsUnavailableReason:
            measures.length === 0
              ? "This exploration exposes no Concept pinned to a version, so nothing could be pre-filled. Pick published Concepts below — a reference without a version is not a reference."
              : null,
        });
        setFormula(seedFromMeasures(measures));
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        setLoad({
          status: "ready",
          measures: [],
          pinsUnavailableReason:
            "This exploration's measures could not be read, so nothing was pre-filled: "
            + (err instanceof Error ? err.message : "the read did not answer")
            + ". Pick published Concepts below rather than proposing an unpinned reference.",
        });
        setFormula(emptyFormula());
      });
    return () => controller.abort();
  }, [open, projectId, resultId]);

  // The Project's published Concepts, the same read `NewConceptDialog` makes.
  useEffect(() => {
    if (!open) return undefined;
    const controller = new AbortController();
    setReferences({ status: "loading" });
    getGovernanceCollection(projectId, "semantic-model", "concepts", {}, controller.signal)
      .then((envelope) => {
        setReferences({
          status: "ready",
          options: envelope.items
            .filter((item) => item.active_version_ref?.id)
            .map((item) => ({
              value: `${item.object_ref.id}|${item.active_version_ref!.id}`,
              label: `${item.object_ref.label} · version ${item.active_version_ref!.version ?? "?"}`,
            })),
        });
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        setReferences({
          status: "error",
          message: err instanceof Error ? err.message : "The Concept list did not answer.",
          retry: () => setReferencesAttempt((value) => value + 1),
        });
      });
    return () => controller.abort();
  }, [open, projectId, referencesAttempt]);

  /** The exploration's pins FIRST, then the Project's published Concepts. Both,
   *  never one: the version a Result executed is often not the current one, and
   *  offering only the current one would quietly re-point the formula. */
  const offered = useMemo<ReferenceState>(() => {
    const pinned = load.status === "ready"
      ? load.measures.map((measure) => ({
        value: `${measure.conceptId}|${measure.versionId}`,
        label: `${measure.label} · the version this Result executed`,
      }))
      : [];
    if (references.status !== "ready") {
      return pinned.length > 0 ? { status: "ready", options: pinned } : references;
    }
    const seen = new Set(pinned.map((option) => option.value));
    return {
      status: "ready",
      options: [...pinned, ...references.options.filter((option) => !seen.has(option.value))],
    };
  }, [load, references]);

  const incomplete = activeOperands(formula).some(operandIsIncomplete);
  const sending = submit.status === "sending";

  const file = useCallback(async () => {
    setSubmit({ status: "sending" });
    try {
      const proposal = await createProposal(projectId, {
        name: name.trim(),
        expression: buildExpression(formula, name.trim()),
        provenance: { result_id: resultId, query_spec_version_id: querySpecVersionId },
        description: description.trim() || null,
      });
      setSubmit({ status: "filed", proposal });
      if (onFiled) onFiled(proposal);
    } catch (err) {
      if (err instanceof ApiError) {
        const body = err.body as { message?: string; refusals?: NamedRefusal[] } | undefined;
        setSubmit({
          status: "refused",
          code: err.code,
          message: body?.message ?? err.message,
          refusals: Array.isArray(body?.refusals) ? body!.refusals! : [],
        });
        return;
      }
      setSubmit({
        status: "refused",
        code: "unavailable",
        message: err instanceof Error ? err.message : "The promotion queue did not answer.",
        refusals: [],
      });
    }
  }, [description, formula, name, onFiled, projectId, querySpecVersionId, resultId]);

  const close = useCallback(() => {
    setSubmit({ status: "idle" });
    onClose();
  }, [onClose]);

  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next) close(); }}>
      <DialogContent className="max-w-2xl" data-testid="propose-calculated-field">
        <DialogHeader>
          <DialogTitle>Propose as governed field</DialogTitle>
          <DialogDescription>
            The calculation goes to the Governance review queue with the exploration it came
            from. Nothing is published here: a person accepts it, and the change-set that
            follows is confirmed in Governance.
          </DialogDescription>
        </DialogHeader>

        {submit.status === "filed" ? (
          <div className="space-y-3" data-testid="propose-filed">
            <Status as="block" tone="success" title="Proposed. It is waiting in the review queue.">
              <span data-testid="propose-filed-where">
                {submit.proposal.name} is now open in Governance › Semantic Model › Concepts,
                in <strong>Proposed calculated fields</strong>. Accepting it there prepares the
                semantic change-set; declaring how it aggregates and confirming happen on that
                change-set.
              </span>
            </Status>
            {queueHref ? (
              <p className="mb-0">
                <a href={queueHref} data-testid="propose-queue-link">Open the review queue</a>
              </p>
            ) : null}
          </div>
        ) : (
          <div className="space-y-3">
            {load.status === "loading" && (
              <p className="mb-0 text-caption text-text-secondary" role="status">
                Reading what this exploration pinned…
              </p>
            )}
            {load.status === "ready" && load.pinsUnavailableReason && (
              <Status
                as="block"
                tone="warning"
                title="Nothing could be pre-filled from this exploration"
                data-testid="propose-no-pins"
              >
                {load.pinsUnavailableReason}
              </Status>
            )}

            <div className="space-y-1.5">
              <Label htmlFor="propose-name">Canonical Name (ID/Code)</Label>
              <Input
                id="propose-name"
                placeholder="e.g. cost_per_click"
                value={name}
                disabled={sending}
                onChange={(e) => setName(e.target.value)}
              />
              <p className="mb-0 text-caption text-text-secondary">
                Lower-case letters, digits and underscores. It is the name every mapping and
                view joins on.
              </p>
            </div>

            <FormulaTreeEditor
              idPrefix="propose"
              formula={formula}
              onChange={setFormula}
              references={offered}
              disabled={sending}
            />

            <div className="space-y-1.5">
              <Label htmlFor="propose-description">Business Definition</Label>
              <Textarea
                id="propose-description"
                placeholder="What this calculation means, and when it is the right one."
                value={description}
                disabled={sending}
                onChange={(e) => setDescription(e.target.value)}
              />
            </div>

            {submit.status === "refused" && (
              <Status
                as="block"
                tone="error"
                title="This proposal was refused"
                data-testid="propose-refusal"
              >
                <span data-testid="propose-refusal-gesture">
                  {REFUSAL_GESTURE[submit.code] ?? submit.message}
                </span>
                {submit.refusals.length > 0 && (
                  <ul className="mb-0 mt-2 list-disc pl-4">
                    {submit.refusals.map((refusal, index) => (
                      <li key={`${refusal.code}-${index}`} data-testid={`propose-refusal-${refusal.code}`}>
                        {REFUSAL_GESTURE[refusal.code] ?? refusal.message ?? refusal.code}
                        {refusal.path ? <span className="text-caption text-text-secondary"> ({refusal.path})</span> : null}
                      </li>
                    ))}
                  </ul>
                )}
              </Status>
            )}
          </div>
        )}

        <DialogFooter>
          {submit.status === "filed" ? (
            <Button type="button" onClick={close}>Done</Button>
          ) : (
            <>
              <Button
                type="button"
                onClick={() => void file()}
                disabled={sending || !name.trim() || incomplete}
              >
                {sending ? "Proposing…" : "Propose"}
              </Button>
              <Button type="button" variant="secondary" onClick={close} disabled={sending}>
                Cancel
              </Button>
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
