import { wireWord } from "../ui/glossary";

/**
 * The vocabulary the three Test workspace screens share.
 *
 * It exists because two of them would otherwise each grow their own idea of
 * "what a verdict looks like", and the console has already paid for that once:
 * six ways of saying green/amber/red, one per screen
 * (`spec-console-visual-system.md`, `Status` is one object).
 *
 * Two things live here and nothing else:
 *
 *   1. `refusalsOf` — the server's structured refusal, read WITHOUT being
 *      rewritten. The Test capability answers with two envelope shapes:
 *      `{refusals: [{code, message, subject}]}` (`core/evaluation_runs.py`,
 *      `core/feedback_review.py`) and `{reasons: [{code, field, message}]}`
 *      (`core/trace_observation.py`). A reader that understood only one of them
 *      would show "the request was refused" with no field named, and a caller
 *      who cannot see which field was refused guesses.
 *
 *   2. `verdictTone` — the ONE mapping from a server verdict to a tone.
 *      `unverifiable` is neutral: it is not an amber warning and not a red
 *      failure, because nothing was judged. The neutral mark is an open dotted
 *      ring, the one shape a reader cannot mistake for a result
 *      (`analyze-and-test.md:282-284`).
 *
 * There is deliberately no helper here that sums, averages or ranks verdicts.
 * `analyze-and-test.md:336-337` forbids the compensating figure, and the way to
 * keep forbidding it is for the function that would compute it not to exist.
 */

/** One named, actionable reason a write was refused. */
export interface Refusal {
  code: string;
  /** The field the server named, when it named one. */
  subject: string | null;
  message: string;
}

function readEntry(entry: Record<string, unknown>): Refusal {
  return {
    code: String(entry.code ?? "refused"),
    // `subject` is the evaluation/feedback wording, `field` the observed-evidence
    // one. Neither is renamed: whichever the server used is what is shown.
    subject:
      typeof entry.subject === "string"
        ? entry.subject
        : typeof entry.field === "string"
          ? entry.field
          : null,
    message: String(entry.message ?? ""),
  };
}

/** Both refusal envelopes, read as written. An unknown body yields no reason. */
export function refusalsOf(body: unknown): Refusal[] {
  if (!body || typeof body !== "object") return [];
  const envelope = body as { refusals?: unknown; reasons?: unknown };
  const list = Array.isArray(envelope.refusals)
    ? envelope.refusals
    : Array.isArray(envelope.reasons)
      ? envelope.reasons
      : [];
  return list
    .filter((entry): entry is Record<string, unknown> => Boolean(entry) && typeof entry === "object")
    .map(readEntry);
}

/** The four verdicts of `analyze-and-test.md:282-283`, and nothing else. */
export type Verdict = "pass" | "fail" | "unverifiable" | "not_applicable";

/**
 * The tone a verdict carries. Only `pass` is green and only `fail` is red;
 * everything else — including a verdict this console has never heard of — is
 * neutral. Guessing a tone for an unknown verdict is how an unmeasured
 * dimension acquires a colour that reads like evidence.
 */
export function verdictTone(verdict: string): "success" | "error" | "neutral" {
  if (verdict === "pass") return "success";
  if (verdict === "fail") return "error";
  return "neutral";
}

/** The verdict in the words the document uses. */
export function verdictLabel(verdict: string): string {
  switch (verdict) {
    case "pass":
      return "Pass";
    case "fail":
      return "Fail";
    case "unverifiable":
      return "Unverifiable";
    case "not_applicable":
      return "Not applicable";
    default:
      return verdict;
  }
}

/** A dimension key rendered as a sentence, without inventing a new name for it. */
export function dimensionLabel(dimension: string): string {
  return dimension.replace(/_/g, " ").replace(/^./, (first) => first.toUpperCase());
}

/** WHERE AN ANNOTATION'S REVIEW STOOD, said rather than printed.
 *
 *  `FeedbackReviewWorkbench` put `version.review_state` straight into a table
 *  cell, beside two columns that already went through `dimensionLabel` and
 *  `verdictLabel` — so one cell of that row spoke the database and the others
 *  did not. The vocabulary is closed and has one writer:
 *  `feedback_review.REVIEW_STATES`.
 *
 *  `duplicate` and `resolved` both end a review and are NOT merged: one says the
 *  annotation was already filed, the other that the thing it reported was fixed,
 *  and a reader deciding whether to reopen needs the difference.
 *
 *  An unregistered word is returned as it arrived, like `verdictLabel` does:
 *  inventing a plausible label for a state this build does not know would hide a
 *  console that is behind its server. */
const REVIEW_STATE_LABELS: Record<string, string> = {
  unreviewed: "Not reviewed",
  triaged: "Triaged",
  accepted: "Accepted",
  rejected: "Rejected",
  duplicate: "Already filed",
  resolved: "Resolved",
};

export function reviewStateLabel(state: string): string {
  return REVIEW_STATE_LABELS[state] ?? state;
}

/**
 * How a Run gathered its evidence, in the reader's words.
 *
 * `EVIDENCE_MODES` (`server/core/evaluation_runs.py:77`) is a closed pair —
 * `offline | observed_cohort` — and `evaluation_runs.py:444` refuses anything
 * else at write time, so this map is total and TypeScript says so. Two screens
 * printed the token: the Evaluation Run header and the Regression profile row.
 * Declared once here because `testEvidence.ts` is already where this workspace's
 * words live (`dimensionLabel`, `verdictLabel`), and two maps would be the
 * two-stores defect one workspace down.
 *
 * `observed_cohort` is the only one whose word is not its token: a cohort of
 * real interactions was watched, as opposed to a corpus replayed with nobody
 * using the product.
 */
export type EvidenceMode = "offline" | "observed_cohort";

const EVIDENCE_MODE_LABEL: Record<EvidenceMode, string> = {
  offline: "Offline",
  observed_cohort: "Observed cohort",
};

export function evidenceModeLabel(mode: string | null | undefined): string {
  if (!mode) return "";
  return EVIDENCE_MODE_LABEL[mode as EvidenceMode] ?? wireWord(mode);
}
