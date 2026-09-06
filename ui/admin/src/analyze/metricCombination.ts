/** What the server says about a metric it would not combine, in the reader's words.
 *
 * THE DEFECT THIS MODULE CLOSES, AND ITS EXACT SHAPE. `analyze-and-test.md:617`
 * has carried one `Incomplete if` since 2026-08-10: *"a refused metric reaches a
 * surface as an absence, with nothing naming the refusal."* The server states the
 * refusal three ways and the console spoke none of them — `metrics_not_combinable`
 * (`reports.py:351`), `combination_check` / `combination_refused` (`rollup.py:639`),
 * and `visual.state` (`answer_contract.py:39`) appeared in zero `.tsx`. A metric the
 * gate refused therefore reached a screen as a hole, which reads exactly like a
 * metric nobody asked for.
 *
 * ONE VOCABULARY, ONE PLACE. Every literal below is the server's own, copied from
 * the module that emits it, and each maps to a sentence a person can act on plus
 * the GESTURE THAT REPAIRS IT when one exists. A surface that renders any of these
 * facts imports from here rather than restating them — three surfaces inventing
 * three wordings for `UNRULED_OVERLAP` is how "not combinable" became a hole in the
 * first place.
 *
 * WHAT IS DELIBERATELY NOT HERE: a default. `describeCombinationRefusal` answers
 * `null` for a status it does not know, and the caller shows the raw status rather
 * than a reassuring sentence invented for it. A wrong explanation is worse than an
 * unexplained code, because only one of the two makes the reader stop looking.
 */

/** `rollup.py:74-81` — the four statuses the one rollup authority refuses on. */
export type CombinationRefused =
  | "UNRULED_OVERLAP"
  | "KEEP_SEPARATE"
  | "NOT_COMBINABLE"
  | "OVERRIDE_NOT_MATERIALIZED";

/** `rollup.py:518-522` — how the question was answered, on EVERY total. */
export type CombinationCheck =
  | "single_source"
  | "not_requested"
  | "verified"
  | "unavailable"
  | "refused";

/** `answer_contract.py:39-45`. */
export type VisualState = "rendered" | "not_applicable" | "unavailable";

/** `semantic_expressions.py:71` / migration 142:157-159. */
export type AdditivityClass = "additive" | "semi_additive" | "non_additive";

export interface Explained {
  /** A short label naming the state, never the database term. */
  title: string;
  /** What happened, in one sentence, from the reader's side. */
  sentence: string;
  /** The next act that repairs it, or `null` when nothing the reader does would. */
  gesture: string | null;
}

/** The per-metric entry of a report envelope (`reports.py:154-159`). */
export interface NotCombinableEntry {
  status: string;
  check?: string | null;
  per_source?: Record<string, number> | null;
  source_systems?: string[] | null;
}

const REFUSAL: Record<CombinationRefused, Explained> = {
  UNRULED_OVERLAP: {
    title: "Two sources report this, and no rule says which one counts",
    sentence:
      "More than one source publishes this metric for the same period, and nothing " +
      "declares how they combine. Adding them would double-count whatever they share.",
    gesture: "Declare a reconciliation rule for this metric in Governance.",
  },
  KEEP_SEPARATE: {
    title: "This metric is declared to stay per source",
    sentence:
      "The reconciliation rule for this metric says each source is reported on its own. " +
      "There is no combined total by design, and the per-source figures below are the answer.",
    gesture: null,
  },
  NOT_COMBINABLE: {
    title: "This metric cannot be added up",
    sentence:
      "This metric is governed as non-additive, so summing it across sources would state " +
      "a number nobody measured.",
    gesture:
      "Ask for the measures it is computed from, and let the Result recompute it after the merge.",
  },
  OVERRIDE_NOT_MATERIALIZED: {
    title: "The rule changed and the figures have not caught up",
    sentence:
      "A reconciliation rule was changed, but the stored figures were built on the previous " +
      "one. The old total is not shown, because it would answer a question nobody is asking.",
    gesture: "Re-run the sources so the stored figures are rebuilt on the current rule.",
  },
};

const CHECK: Record<CombinationCheck, Explained> = {
  single_source: {
    title: "One source",
    sentence: "Exactly one source publishes this metric, so nothing had to be reconciled.",
    gesture: null,
  },
  not_requested: {
    title: "Not checked",
    sentence:
      "This total was produced without asking the reconciliation gate. It may be right; " +
      "nobody verified it.",
    gesture: null,
  },
  verified: {
    title: "Checked",
    sentence: "The reconciliation gate was asked and it permits this combined total.",
    gesture: null,
  },
  unavailable: {
    title: "Could not be checked",
    sentence:
      "The reconciliation gate was asked and could not answer, so this total carries no " +
      "verdict. It is not a verified number.",
    gesture: null,
  },
  refused: {
    title: "Refused",
    sentence: "The reconciliation gate refuses a combined total for this metric.",
    gesture: null,
  },
};

const VISUAL: Record<VisualState, Explained> = {
  rendered: {
    title: "Chart shown",
    sentence: "The visual was built from this answer's own blocks.",
    gesture: null,
  },
  not_applicable: {
    title: "No chart for this answer",
    sentence: "This question is answered without a chart. Nothing is missing.",
    gesture: null,
  },
  unavailable: {
    title: "The chart could not be built",
    sentence:
      "The blocks this answer declares resolved no data, so no visual was produced. " +
      "An empty chart has NOT been drawn in its place.",
    gesture: "Check the sources behind this answer, then ask again.",
  },
};

const ADDITIVITY: Record<AdditivityClass, Explained> = {
  additive: {
    title: "Can be added up",
    sentence: "This measure may be summed across every dimension of this Result.",
    gesture: null,
  },
  semi_additive: {
    title: "Cannot be added up everywhere",
    sentence:
      "This measure may be summed over some dimensions and not others, so a total that " +
      "crosses the wrong one would be wrong without looking wrong.",
    gesture: "Check which dimensions it may be summed over before combining it.",
  },
  non_additive: {
    title: "Cannot be added up",
    sentence:
      "This measure is governed as non-additive. Summing it — across sources, days or " +
      "campaigns — states a number nobody measured.",
    gesture:
      "Ask for the measures it is computed from, and let the Result recompute it after the merge.",
  },
};

/** `null` when the status is one this console does not know. Never a guess. */
export function describeCombinationRefusal(status: string | null | undefined): Explained | null {
  if (!status) return null;
  return REFUSAL[status as CombinationRefused] ?? null;
}

export function describeCombinationCheck(check: string | null | undefined): Explained | null {
  if (!check) return null;
  return CHECK[check as CombinationCheck] ?? null;
}

export function describeVisualState(state: string | null | undefined): Explained | null {
  if (!state) return null;
  return VISUAL[state as VisualState] ?? null;
}

export function describeAdditivity(klass: string | null | undefined): Explained | null {
  if (!klass) return null;
  return ADDITIVITY[klass as AdditivityClass] ?? null;
}

/** Is this measure one a merge may fold to a shared key?
 *
 * `semi_additive` answers NO, for the reason `multi_source_plan._metric_additivity`
 * gives on the server side: a measure that may be summed over some dimensions and
 * not others cannot be summed over a merge key nobody constrained. The two sides
 * must agree, or the console promises what the compiler refuses.
 */
export function isCombinable(klass: string | null | undefined): boolean {
  return klass === "additive";
}

/** The measures of a Result that cannot be combined, named rather than counted. */
export function notCombinableMembers<
  T extends { label?: string | null; concept_id?: string; additivity_class?: string | null },
>(members: readonly T[]): { member: T; explained: Explained }[] {
  const named: { member: T; explained: Explained }[] = [];
  for (const member of members) {
    // An undeclared additivity is NOT an accusation: the registry refuses to store
    // a metric declaring neither an aggregation nor its non-additivity, so a blank
    // here is a vocabulary this Result could not read, not a verdict against it.
    if (!member.additivity_class) continue;
    if (isCombinable(member.additivity_class)) continue;
    const explained = describeAdditivity(member.additivity_class);
    if (explained) named.push({ member, explained });
  }
  return named;
}
