/**
 * The console half of the calculated-field promotion rail (story 75-2).
 *
 * Three doors, and no fourth. They are the three `calculated_field_proposal_routes`
 * of `server/core/calculated_field_proposals_api.py`, spelled here exactly as
 * that module spells them, so a path typed twice cannot be typed two ways:
 *
 *   POST   /api/projects/{project_id}/calculated-field-proposals
 *   GET    /api/projects/{project_id}/calculated-field-proposals?status=open
 *   POST   /api/projects/{project_id}/calculated-field-proposals/{id}/resolve
 *
 * EVERY FIELD BELOW IS A COLUMN THE SERVER RETURNS. `_COLUMNS`
 * (`calculated_field_proposals.py`) is the list; `prepare_refusals` is the one
 * key that is NOT a column — `resolve` attaches it after the transaction, and it
 * carries what the change-set the acceptance prepared still refuses. Nothing
 * here is invented: a key this file names that the server does not send would be
 * a screen showing a value nobody wrote.
 *
 * THE ORIGIN IS NEVER SENT. The door stamps `human` on anything the console
 * files, for the reason written at its `_create`: a body that could say `agent`
 * would let a person file a machine's proposal, which is the exact confusion
 * `origin` exists to prevent.
 */
import { apiGet, apiPost } from "../lib/apiFetch";

/** `open | accepted | declined` — the Hub review queue's vocabulary, verbatim. */
export type ProposalStatus = "open" | "accepted" | "declined";

/** `human | agent`. A machine's proposal read as a person's is worth less than
 *  nothing, so the two are never collapsed on screen. */
export type ProposalOrigin = "human" | "agent";

/** One exact `(concept_id, version_id)` pin the server's walk collected. */
export interface ProposalDependency {
  concept_id: string;
  version_id: string;
  [key: string]: unknown;
}

/** `{"origin": "exploration", "result_id": ..., "query_spec_version_id": ...}`
 *  — the exploration this calculation came from, verified server-side. */
export interface ProposalProvenance {
  origin?: string;
  result_id?: string | null;
  query_spec_version_id?: string | null;
  [key: string]: unknown;
}

/** A refusal the server NAMED. `code` is the word; `message` is its sentence. */
export interface NamedRefusal {
  code: string;
  message?: string;
  path?: string;
  [key: string]: unknown;
}

export interface CalculatedFieldProposal {
  id: string;
  org_id: string;
  project_id: string;
  status: ProposalStatus;
  origin: ProposalOrigin;
  name: string;
  description: string | null;
  expression: Record<string, unknown>;
  value_type: string | null;
  unit: string | null;
  currency: string | null;
  dependencies: ProposalDependency[];
  provenance: ProposalProvenance;
  requested_by: string;
  created_at: string | null;
  resolved_by: string | null;
  resolved_at: string | null;
  /** The change-set an acceptance PREPARED. Never a published version. */
  applied_ref: string | null;
  /** What that prepared change-set still refuses — `undeclared_aggregation` on
   *  a fresh promotion, by design. Present on the resolve answer only. */
  prepare_refusals?: NamedRefusal[];
}

export interface OpenProposalsPage {
  project_id: string;
  status: "open";
  proposals: CalculatedFieldProposal[];
  proposals_total: number;
}

export interface ProposalDraft {
  name: string;
  expression: Record<string, unknown>;
  provenance: { result_id?: string | null; query_spec_version_id?: string | null };
  description?: string | null;
}

/**
 * A named refusal turned into the GESTURE that repairs it.
 *
 * IT LIVES HERE, BESIDE THE DOORS, BECAUSE BOTH ENDS OF THE RAIL NEED IT. The
 * dialog that files a proposal and the queue that decides one receive the same
 * `{code, message}` envelope from the same three routes; a map that lived in
 * only one of them left the other printing bare codes — `undeclared_aggregation`
 * on an acceptance banner, `db_error` on a failed read — which is the exact
 * thing CLAUDE.md forbids: "un message d'erreur nomme le geste qui répare, pas
 * la cause technique".
 *
 * The codes are the server's own (`calculated_field_proposals.py`,
 * `semantic_expressions.py`, `apiFetch.ts` for the transport fallback), and the
 * sentences say what to DO. A code this map does not carry falls back to the
 * server's message, which is written for a person too; inventing a sentence for
 * an unknown code would be worse than repeating the one the server wrote.
 */
export const REFUSAL_GESTURE: Record<string, string> = {
  unresolved_reference: "Pin each measure to a version: pick the Concept version this Result executed, not the Concept alone.",
  unknown_reference: "Pick a Concept version this Project can read: the version named here is not one of them.",
  malformed_node: "Pin each measure to a version — an operand is missing the exact version it points at.",
  unknown_operation: "Rebuild the formula from the operations offered above: this one is not in the governed contract.",
  incompatible_types: "Make the two sides comparable — a count and an amount of money cannot be added.",
  incompatible_unit: "Make the two sides share a unit, or wrap one so they do.",
  incompatible_currency: "Make the two sides share a currency, or convert one before combining them.",
  undeclared_zero_behavior: "Say what a zero denominator means: no value, zero, or refuse the row.",
  unsafe_sum: "Aggregate before combining: this sum is not safe at this grain.",
  invalid_name: "Give it a canonical name: lower-case letters, digits and underscores, starting with a letter (for example cost_per_click). The description is where people read plain words.",
  missing_name: "Give the field the name it would be known by.",
  name_too_long: "Shorten the name to 120 characters or fewer.",
  description_too_long: "Shorten the description to 4000 characters or fewer.",
  empty_expression: "Build the formula above: an empty one calculates nothing.",
  missing_provenance: "Open this from a Result: a promotion names the exploration it came from.",
  unknown_provenance: "Open this from a Result of this Project: the exploration named here is not one this Project holds.",
  // The envelope of a whole formula the walk refused. Its `refusals` list says
  // which node, and each of those has its own gesture above.
  invalid_expression: "Rewrite the formula with the operations offered above: as written it is not expressible in the governed contract.",
  // `db_error` is the server's 500 on this rail, `unavailable` is what
  // `apiFetch` names an answer that was not an envelope at all. Both are
  // "nothing was written", and the gesture is the same one.
  db_error: "Try again in a moment: the promotion queue did not answer, and nothing was written.",
  unavailable: "Reload the page and try again: the promotion queue could not be reached, and nothing was written.",
  // What a FRESH promotion always still refuses, by design: a proposal carries
  // the formula, never the declaration of how it aggregates.
  undeclared_aggregation: "Declare how this field aggregates — sum, average, ratio of sums — on the Concept, before the change-set can be confirmed.",
  undeclared_default_branch: "Say what the conditional returns when no branch matches: an implicit blank hides 'no rule matched' behind 'unknown'.",
  incompatible_branch_types: "Make every branch of the conditional return the same kind of quantity.",
  non_boolean_condition: "Make each branch condition a comparison: a branch is taken on a yes or no, not on a number.",
};

const base = (projectId: string) =>
  `/api/projects/${encodeURIComponent(projectId)}/calculated-field-proposals`;

/** File one proposal. 201 with the row, or a named refusal as an `ApiError`. */
export async function createProposal(
  projectId: string,
  draft: ProposalDraft,
  init?: RequestInit,
): Promise<CalculatedFieldProposal> {
  const body = await apiPost<{ proposal: CalculatedFieldProposal }>(
    base(projectId),
    {
      name: draft.name,
      expression: draft.expression,
      provenance: draft.provenance,
      description: draft.description ?? null,
    },
    init ?? {},
  );
  return body.proposal;
}

/** The queue. `status=open` is the only word this door serves, and it is sent
 *  explicitly rather than left to a default a future server might change. */
export async function listOpenProposals(
  projectId: string,
  init?: RequestInit,
): Promise<OpenProposalsPage> {
  return apiGet<OpenProposalsPage>(`${base(projectId)}?status=open`, init ?? {});
}

/** Accept (prepare a change-set) or decline. Never a silence. */
export async function resolveProposal(
  projectId: string,
  proposalId: string,
  status: "accepted" | "declined",
  init?: RequestInit,
): Promise<CalculatedFieldProposal> {
  const body = await apiPost<{ proposal: CalculatedFieldProposal }>(
    `${base(projectId)}/${encodeURIComponent(proposalId)}/resolve`,
    { status },
    init ?? {},
  );
  return body.proposal;
}

/**
 * The formula, in words.
 *
 * A queue that shows `{"op":"ratio","numerator":…}` asks its reader to parse
 * JSON to decide something. This renders the SAME tree the server validated,
 * with each reference named by the pin it carries — never a label invented from
 * an id, and never `latest`.
 */
export function expressionInWords(
  expression: unknown,
  nameFor: (conceptId: string, versionId: string) => string | null,
): string {
  const node = expression as Record<string, unknown> | null;
  if (!node || typeof node !== "object") return "No formula";
  const op = String(node.op ?? "");
  const operand = (value: unknown): string => expressionInWords(value, nameFor);
  switch (op) {
    case "concept_ref": {
      const conceptId = String(node.concept_id ?? "");
      const versionId = String(node.version_id ?? "");
      return nameFor(conceptId, versionId) ?? `${conceptId} at version ${versionId}`;
    }
    case "literal":
      return String(node.value ?? "");
    case "source_measure":
      return String(node.concept ?? "its mapped source measure");
    case "add":
    case "subtract":
    case "multiply": {
      const joiner = op === "add" ? " + " : op === "subtract" ? " − " : " × ";
      const parts = Array.isArray(node.operands) ? node.operands.map(operand) : [];
      return parts.join(joiner);
    }
    case "ratio":
      return `${operand(node.numerator)} ÷ ${operand(node.denominator)}`;
    case "aggregate":
      return `${String(node.function ?? "")} of ${operand(node.operand)}`;
    case "comparison": {
      // The operator in WORDS, never `gte`. A reader deciding whether to accept
      // a formula should not have to know the contract's token for "at least".
      const wording = COMPARISON_IN_WORDS[String(node.operator ?? "")];
      if (!wording) return "an unreadable comparison";
      return `${operand(node.left)} ${wording} ${operand(node.right)}`;
    }
    case "conditional": {
      // Branch by branch, in the order the server walked them, and the
      // `otherwise` branch NAMED — a conditional that hides its default reads as
      // a rule with a silent hole, which is the exact thing
      // `undeclared_default_branch` refuses server-side.
      const branches = Array.isArray(node.when) ? node.when : [];
      const said = branches.map((branch) => {
        const leaf = branch as Record<string, unknown> | null;
        if (!leaf || typeof leaf !== "object") return "an unreadable branch";
        return `${operand(leaf.then)} when ${operand(leaf.condition)}`;
      });
      if (said.length === 0) return "an unreadable conditional";
      return `${said.join("; ")}; otherwise ${operand(node.otherwise)}`;
    }
    case "concept_name":
      // Parsed, and never promotable: the server refuses it as
      // `unresolved_reference`. Saying so here is why a workbench can show
      // pending work without the queue pretending it is a pin.
      return `${String(node.name ?? "an unnamed measure")} (not pinned to a version)`;
    default:
      return op || "No formula";
  }
}

/** The comparison operators of the governed contract, in a reader's words.
 *  `server/core/semantic_expressions.py::_op_comparison` is the allowlist. */
const COMPARISON_IN_WORDS: Record<string, string> = {
  eq: "is",
  ne: "is not",
  lt: "is below",
  lte: "is at most",
  gt: "is above",
  gte: "is at least",
};
