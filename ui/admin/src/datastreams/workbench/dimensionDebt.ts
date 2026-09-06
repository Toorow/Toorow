/**
 * What a dimension being added owes to the past — amendment 14, ratified
 * 2026-08-11 in `docs/product-architecture/datastream-workbench-and-wizard.md`.
 *
 * « Ajouter une dimension déclare une DETTE D'HISTORIQUE, et l'écran la nomme. »
 *
 * THE MEASUREMENT IS THE SERVER'S; THIS FILE ONLY INTERSECTS TWO SETS.
 * `core/datastream_dimension_history.py` publishes the days of a bounded window,
 * the plan version each one resolves to, the dimensions those versions declared,
 * and when each dimension first entered the plan. A connector may declare 765
 * dimensions, so publishing one day list per field would be 765 × 92 entries for
 * a panel that needs the two or three a person just ticked; the day list is
 * published once and the count is derived here.
 *
 * NOTHING IS INFERRED THAT IS NOT NAMED. Every day this file assigns to a
 * dimension carries the reason it was assigned, and the three reasons are not
 * the same strength:
 *
 *   * `never_declared` — no plan version has EVER declared this dimension, so no
 *     run can have asked for it. Exact, and it is the case of the gesture the
 *     amendment is about: a field being added for the first time.
 *   * `run_plan_version` — the day's own run pinned a plan version, and that
 *     version does not declare the dimension. Exact for what was ASKED.
 *   * `predates_declaration` — the day's run is unknown (`pull_jobs.execution_id`
 *     is nullable and was added late, migration 218), but the day landed BEFORE
 *     the first version declaring the dimension existed. A version that did not
 *     exist cannot have been executed, so the absence still holds.
 *
 * And what does not hold is counted apart as `unknown`: a day whose run is
 * unknown and which landed after the dimension entered the plan. It is never
 * folded into the debt, because a day counted as missing that was not is the
 * defect that made this whole review necessary.
 *
 * WHAT NONE OF IT PROVES, and the payload says so in its own `unmeasurable`
 * field: what a provider actually RETURNED for a past day is recorded nowhere.
 * A day asked for a dimension is not a day that received one. The screen renders
 * the server's sentence rather than composing a stronger one here.
 */

/** One day of the window, as the server resolved it. */
export interface HistoryDay {
  date: string;
  /** `landed` is the only state that can owe a dimension — see the module. */
  state: string;
  plan_version_id: string | null;
  landed_at: string | null;
}

export interface DimensionHistory {
  state: string;
  reason?: string;
  window?: { from: string; to: string; days: number };
  basis?: string;
  unmeasurable?: string;
  refetch_path?: string | null;
  max_provider_backfill_days?: number | null;
  backfill_bound_evidence?: string;
  earliest_recoverable?: string | null;
  days?: HistoryDay[];
  counts?: Record<string, number>;
  plan_dimensions?: Record<string, string[]>;
  first_declared?: Record<string, { created_at: string | null; version_number: number }>;
}

/** Why one day is counted as missing this dimension. Never a bare number. */
export type DebtReason = "never_declared" | "run_plan_version" | "predates_declaration";

export interface DimensionDebt {
  field_id: string;
  /** Days that landed rows and were not asked for this dimension. */
  missing: string[];
  /** How each of those was established, strongest first. */
  reasons: Record<DebtReason, number>;
  /** Days that landed and WERE asked for it. */
  carried: number;
  /** Days that landed, whose run is unknown and which post-date the declaration. */
  unknown: number;
  /** Of `missing`, the ones the provider bound still reaches. */
  recoverable: string[];
  /** Of `missing`, the ones it does not — and that is an answer, not a failure. */
  beyondRecovery: string[];
  /** `true` when no provider bound is declared, so neither list means anything. */
  recoveryUnknown: boolean;
}

const LANDED = "landed";

function parseDay(value: string | null): number | null {
  if (!value) return null;
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? null : parsed;
}

/**
 * The days one dimension is missing from, and why each one counts.
 *
 * Returns `null` when the history carries no measurement to intersect — an
 * unreadable read, a mode that pulls nothing, a stream with no landed day. The
 * caller then renders the server's own `reason` rather than "0 days missing",
 * which is the same string a real measurement of zero would produce and is the
 * lie this function refuses to make possible.
 */
export function dimensionDebt(
  history: DimensionHistory | null | undefined,
  fieldId: string,
): DimensionDebt | null {
  if (!history || history.state !== "measured" || !Array.isArray(history.days)) return null;

  const planDimensions = history.plan_dimensions ?? {};
  const declared = history.first_declared?.[fieldId] ?? null;
  const declaredAt = parseDay(declared?.created_at ?? null);

  const missing: string[] = [];
  const reasons: Record<DebtReason, number> = {
    never_declared: 0,
    run_plan_version: 0,
    predates_declaration: 0,
  };
  let carried = 0;
  let unknown = 0;

  for (const day of history.days) {
    if (day.state !== LANDED) continue;
    if (declared === null) {
      // No version ever declared it. Exact, and no run needs to be resolved.
      missing.push(day.date);
      reasons.never_declared += 1;
      continue;
    }
    const version = day.plan_version_id;
    if (version && Object.prototype.hasOwnProperty.call(planDimensions, version)) {
      if (planDimensions[version].includes(fieldId)) carried += 1;
      else {
        missing.push(day.date);
        reasons.run_plan_version += 1;
      }
      continue;
    }
    const landedAt = parseDay(day.landed_at);
    if (landedAt !== null && declaredAt !== null && landedAt < declaredAt) {
      missing.push(day.date);
      reasons.predates_declaration += 1;
      continue;
    }
    unknown += 1;
  }

  // The provider bound, read and NOT assumed. Measured 2026-08-12: 0 of the 140
  // declared reports fill `max_provider_backfill_days`, so this is the live
  // branch — the screen says the reach of a re-collection is unknown instead of
  // splitting the days on a bound nobody committed to.
  const floor = parseDay(history.earliest_recoverable ?? null);
  const recoveryUnknown = floor === null;
  const recoverable = recoveryUnknown
    ? []
    : missing.filter((day) => (parseDay(day) ?? 0) >= floor);
  const beyondRecovery = recoveryUnknown
    ? []
    : missing.filter((day) => (parseDay(day) ?? 0) < floor);

  return {
    field_id: fieldId,
    missing,
    reasons,
    carried,
    unknown,
    recoverable,
    beyondRecovery,
    recoveryUnknown,
  };
}

/**
 * The debt of a whole addition, as ONE proposal — « la dette d'une dimension est
 * un ENSEMBLE de ces journées, et elle se propose comme telle ».
 *
 * The union, not the sum: one re-collection of a day brings back every dimension
 * the plan then asks for, so a day owed by three added dimensions is one day of
 * spend and must be counted once. Summing them would inflate the confirmation's
 * own figure, which is the class of defect `extract_ledger` already paid for
 * when a window's row count was published on each of its days.
 */
export function debtDays(debts: DimensionDebt[]): string[] {
  const union = new Set<string>();
  for (const debt of debts) for (const day of debt.missing) union.add(day);
  return [...union].sort();
}

/** The same union, restricted to the days the provider bound still reaches. */
export function recoverableDays(debts: DimensionDebt[]): string[] {
  const union = new Set<string>();
  for (const debt of debts) for (const day of debt.recoverable) union.add(day);
  return [...union].sort();
}

/**
 * The debt of one dimension, in the words of the confirmation.
 *
 * It states the count, the window it was counted over, and — when a day was
 * counted from something weaker than the day's own run — WHICH fact carried it.
 * A person who reads "60 of the 92 days" must be able to ask how that was known.
 */
export function debtSentence(debt: DimensionDebt, window?: { days: number }): string {
  const total = debt.missing.length;
  const span = window ? ` of the last ${window.days} days` : "";
  if (total === 0) {
    return debt.unknown > 0
      ? `No day${span} is known to be missing ${debt.field_id}, and ${debt.unknown} could not be attributed to a run.`
      : `Every collected day${span} was already asked for ${debt.field_id}.`;
  }
  const head =
    debt.reasons.never_declared === total
      ? `${debt.field_id} exists on none of the ${total} collected day(s)${span}: no version of this plan has ever asked for it`
      : `${total} collected day(s)${span} were not asked for ${debt.field_id}`;
  const tail = debt.unknown > 0 ? ` ${debt.unknown} further day(s) could not be attributed to a run.` : "";
  return `${head}.${tail}`;
}
