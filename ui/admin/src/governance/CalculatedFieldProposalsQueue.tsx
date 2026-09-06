/**
 * Proposed calculated fields — the review queue of the promotion rail (75-2).
 *
 * WHERE IT LIVES, AND WHY NOT A FIFTH SCREEN. Governance has exactly four
 * Level 2 screens, and `governance.md` states that "object types and optional
 * capabilities appear inside them, never as additional permanent navigation".
 * So the queue mounts INSIDE the Semantic Model › Concepts lens, above the
 * Concepts it feeds — the same shape `UnresolvedValuesPanel` takes inside the
 * Value Tables lens, for the same reason written there: the work sits above the
 * objects.
 *
 * ITS VOCABULARY IS THE HUB REVIEW QUEUE'S, VERBATIM. `NodeRemarks` says
 * Accept / Decline and marks a machine's remark as `agent`; a second wording
 * for one gesture would be a second product. What differs is what an acceptance
 * PRODUCES: a semantic change-set, prepared and never confirmed, whose id the
 * server writes in `applied_ref`.
 *
 * WHAT IT DOES NOT PRETEND. There is no console screen today that opens a
 * prepared semantic change-set by id — every change-set the console creates is
 * created, prepared and confirmed inside one dialog. So an acceptance names the
 * change-set it opened and the refusal that still stands
 * (`undeclared_aggregation`, by design: a proposal carries the formula, not the
 * declaration of how it aggregates), and says where that declaration is made.
 * Inventing a link to a screen that does not exist would send a person to the
 * unknown-route page; the gap is written in `governance.md` instead.
 */
import { useCallback, useEffect, useState } from "react";

import { ApiError } from "../lib/apiFetch";
import {
  Button,
  EmptyState,
  ObjectId,
  Panel,
  PanelBody,
  PanelHeader,
  Status,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
  TableScroll,
  Timestamp,
  wireWord,
  Retry,
} from "../ui";
import {
  expressionInWords,
  listOpenProposals,
  REFUSAL_GESTURE,
  resolveProposal,
  type CalculatedFieldProposal,
  type NamedRefusal,
} from "./calculatedFieldProposalsClient";

/**
 * What a person DOES about this failure — never the code, never the server's
 * internal sentence.
 *
 * CLAUDE.md: "un message d'erreur nomme le geste qui répare, pas la cause
 * technique". `err.message` on this rail is "The promotion queue is
 * unavailable." — a state, with no gesture in it; and an `ApiError` that never
 * reached an envelope carries the code `unavailable` and the raw status text.
 * `REFUSAL_GESTURE` is the one map both ends of the rail read, so the dialog
 * that files a proposal and this queue answer one code with one sentence.
 */
function gestureFor(err: unknown, fallback: string): string {
  if (err instanceof ApiError) return REFUSAL_GESTURE[err.code] ?? fallback;
  return fallback;
}

/** A refusal in words. The code is a token of the contract, not a sentence: it
 *  is shown in parentheses so a reader can quote it, never on its own. */
function refusalInWords(refusal: NamedRefusal): string {
  const said = REFUSAL_GESTURE[refusal.code] ?? refusal.message;
  return said ? `${said} (${refusal.code})` : refusal.code;
}

type QueueState =
  | { status: "loading" }
  | { status: "ready"; rows: CalculatedFieldProposal[] }
  | { status: "error"; message: string };

/** What an acceptance left behind, kept beside the row it came from. */
interface Prepared {
  proposalId: string;
  name: string;
  changeSetId: string | null;
  /** The WHOLE refusal, not its code: the server sends `{code, message}` and a
   *  banner that printed `undeclared_aggregation` asked its reader to look the
   *  word up somewhere this console does not have. */
  refusals: NamedRefusal[];
}

/** `human | agent`, in the reader's words. A machine's proposal read as a
 *  person's is worth less than nothing, so the two are never collapsed. */
const ORIGIN_LABEL: Record<string, string> = { human: "Human", agent: "Agent" };

export default function CalculatedFieldProposalsQueue({ projectId }: { projectId: string }) {
  const [queue, setQueue] = useState<QueueState>({ status: "loading" });
  const [resolving, setResolving] = useState<string | null>(null);
  const [prepared, setPrepared] = useState<Prepared | null>(null);
  const [failure, setFailure] = useState<string | null>(null);

  // THE READ THIS SCREEN'S ERROR BLOCK OFFERS TO REPEAT (76-4). An error
  // that names no way forward is a dead end; this token is what `Retry`
  // moves, and the effect below is the read it re-runs.
  const [reloadToken, setReloadToken] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setQueue({ status: "loading" });
    listOpenProposals(projectId, { signal: controller.signal })
      .then((page) => setQueue({ status: "ready", rows: page.proposals }))
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        setQueue({
          status: "error",
          message: gestureFor(
            err,
            "Reload the page to read the review queue again.",
          ),
        });
      });
    return () => controller.abort();
  }, [projectId, reloadToken]);

  const resolve = useCallback(
    async (row: CalculatedFieldProposal, status: "accepted" | "declined") => {
      setResolving(row.id);
      setFailure(null);
      try {
        const resolved = await resolveProposal(projectId, row.id, status);
        // The row LEAVES the queue, because the queue is what is left to decide.
        setQueue((current) =>
          current.status === "ready"
            ? { status: "ready", rows: current.rows.filter((item) => item.id !== row.id) }
            : current,
        );
        if (status === "accepted") {
          setPrepared({
            proposalId: row.id,
            name: row.name,
            changeSetId: resolved.applied_ref,
            refusals: resolved.prepare_refusals ?? [],
          });
        }
      } catch (err) {
        if (err instanceof ApiError && err.code === "proposal_already_resolved") {
          // An id that exists and a state that refuses is a CONFLICT, not an
          // absence: somebody decided this one first. Drop it from the list and
          // say so, rather than leaving a row whose buttons do nothing.
          setQueue((current) =>
            current.status === "ready"
              ? { status: "ready", rows: current.rows.filter((item) => item.id !== row.id) }
              : current,
          );
          setFailure(
            `${row.name} was already decided by someone else. Reopen the queue to see where it stands.`,
          );
        } else {
          setFailure(
            gestureFor(
              err,
              `${row.name} is still waiting to be decided. Reload the queue and decide it again.`,
            ),
          );
        }
      } finally {
        setResolving(null);
      }
    },
    [projectId],
  );

  return (
    <Panel flush data-testid="calculated-field-proposals">
      <PanelHeader
        title="Proposed calculated fields"
        description="Calculations found while exploring, offered to this Project's semantic model. Accepting one prepares a semantic change-set; nothing is published here."
      />
      <PanelBody className="space-y-3">
        {queue.status === "loading" && (
          <p className="mb-0 text-caption text-text-secondary" role="status">
            Reading the review queue…
          </p>
        )}

        {queue.status === "error" && (
          <Status
            as="block"
            tone="error"
            title="The review queue could not be read"
            data-testid="calculated-field-proposals-error"
          
          action={<Retry onClick={() => setReloadToken((token) => token + 1)} />}
        >
            {queue.message} An empty list here would read as &quot;nothing to decide&quot;, which is
            a different fact.
          </Status>
        )}

        {failure && (
          <Status as="block" tone="warning" title="The verdict was not recorded" data-testid="calculated-field-proposals-failure">
            {failure}
          </Status>
        )}

        {prepared && (
          <Status
            as="block"
            tone="info"
            title={`${prepared.name} is accepted, and its change-set is prepared`}
            data-testid="calculated-field-proposals-prepared"
          >
            {prepared.changeSetId ? (
              <span>
                Change-set <ObjectId value={prepared.changeSetId} />. It stands prepared, never
                confirmed.
              </span>
            ) : (
              <span>No change-set id came back with this acceptance.</span>
            )}{" "}
            <span data-testid="calculated-field-proposals-prepared-next">
              How this field aggregates is still to be declared — a proposal carries the formula,
              not that declaration — and it has to be declared before the change-set can be
              confirmed.
            </span>
            {prepared.refusals.length > 0 && (
              <ul className="mb-0 mt-2 list-disc pl-4">
                {prepared.refusals.map((refusal, index) => (
                  <li
                    key={`${refusal.code}-${index}`}
                    data-testid={`calculated-field-proposals-prepared-refusal-${refusal.code}`}
                  >
                    {refusalInWords(refusal)}
                  </li>
                ))}
              </ul>
            )}
          </Status>
        )}

        {queue.status === "ready" && queue.rows.length === 0 && (
          <EmptyState
            title="Nothing is waiting to be decided"
            description="A calculated field arrives here when someone proposes one from an exploration: open a Result in Analyze and use Propose as governed field. Agents file into this same queue."
          />
        )}

        {queue.status === "ready" && queue.rows.length > 0 && (
          <TableScroll label="Proposed calculated fields">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead scope="col">Name</TableHead>
                  <TableHead scope="col">Calculation</TableHead>
                  <TableHead scope="col">Proposed by</TableHead>
                  <TableHead scope="col">From</TableHead>
                  <TableHead scope="col">Filed</TableHead>
                  <TableHead scope="col">Decide</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {queue.rows.map((row) => (
                  <TableRow key={row.id} data-testid={`calculated-field-proposal-${row.id}`}>
                    <TableCell>
                      <span className="font-semibold">{row.name}</span>
                      {row.description ? (
                        <p className="mb-0 text-caption text-text-secondary">{row.description}</p>
                      ) : null}
                    </TableCell>
                    <TableCell>
                      <span data-testid={`calculated-field-proposal-words-${row.id}`}>
                        {expressionInWords(row.expression, (conceptId, versionId) => {
                          const pin = (row.dependencies ?? []).find(
                            (dependency) =>
                              dependency.concept_id === conceptId
                              && dependency.version_id === versionId,
                          );
                          const named = pin && typeof pin.name === "string" ? pin.name : null;
                          return named ? `${named} (pinned)` : null;
                        })}
                      </span>
                      {row.value_type ? (
                        <p className="mb-0 text-caption text-text-secondary">
                          {row.value_type}
                          {row.unit ? ` · ${row.unit}` : ""}
                        </p>
                      ) : null}
                    </TableCell>
                    <TableCell>
                      {row.requested_by}
                      <p
                        className="mb-0 text-caption text-text-secondary"
                        data-testid={`calculated-field-proposal-origin-${row.id}`}
                      >
                        {ORIGIN_LABEL[row.origin] ?? wireWord(row.origin)}
                      </p>
                    </TableCell>
                    <TableCell>
                      {row.provenance?.result_id ? (
                        <ObjectId value={String(row.provenance.result_id)} />
                      ) : (
                        <span className="text-caption text-text-secondary">
                          No Result recorded
                        </span>
                      )}
                    </TableCell>
                    <TableCell>
                      <Timestamp value={row.created_at} />
                    </TableCell>
                    <TableCell>
                      <div className="flex flex-wrap gap-2">
                        <Button
                          type="button"
                          variant="secondary"
                          data-testid={`calculated-field-proposal-accept-${row.id}`}
                          disabled={resolving !== null}
                          onClick={() => void resolve(row, "accepted")}
                        >
                          {resolving === row.id ? "Deciding…" : "Accept"}
                        </Button>
                        <Button
                          type="button"
                          variant="secondary"
                          data-testid={`calculated-field-proposal-decline-${row.id}`}
                          disabled={resolving !== null}
                          onClick={() => void resolve(row, "declined")}
                        >
                          Decline
                        </Button>
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableScroll>
        )}
      </PanelBody>
    </Panel>
  );
}
