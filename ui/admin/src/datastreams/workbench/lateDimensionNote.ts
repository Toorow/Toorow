/**
 * Why a reading is hollow over history — amendment 14 of the 2026-08-11 review,
 * `docs/product-architecture/datastream-workbench-and-wizard.md`.
 *
 * « Ce que cela interdit : […] surtout rendre un graphique creux sur
 * l'historique sans une phrase qui dit pourquoi il est creux. Un trou expliqué
 * est une donnée ; un trou muet est un bug que l'opérateur impute au produit. »
 *
 * WHERE THE DEFECT WAS. The amendment was delivered on `Processing` — where the
 * change is COMPOSED — and `dimensionDebt.ts` states the debt of a dimension a
 * person is about to tick. That is the right sentence in the wrong place for
 * this question: nobody composing a change is looking at the hollow. The hollow
 * is on `Data`, on the day grid and in the reading of one day, and those three
 * surfaces said nothing at all.
 *
 * SO THIS DERIVES, IT DOES NOT MEASURE. Same payload
 * (`core/datastream_dimension_history.py`, now served on the `data` tab too),
 * same `first_declared` map, same `window`. A second measurement would be a
 * second answer to one question, and the day the two disagreed the screen would
 * be the last to know.
 *
 * AND IT NAMES ONE DIMENSION, NOT A COUNT. « une phrase » — the sentence has to
 * be actionable, so it names the dimension that arrived latest and the day it
 * entered the plan. A list of seven names is a table, and a table is what the
 * person already has on `Processing`.
 */

import type { DimensionHistory } from "./dimensionDebt";

/** The dimension that entered the plan latest, and when. */
export interface LateDimension {
  /** The field as the plan spells it. */
  field: string;
  /** The ISO instant its first declaring version was created. */
  declaredAt: string;
  /** That version's ordinal, so a person can find it in the plan ledger. */
  versionNumber: number;
  /**
   * Days of the published window that landed BEFORE the declaration.
   *
   * Counted on `landed_at` alone and never on a run: a version that did not
   * exist cannot have been executed, which is the `predates_declaration` fact
   * `dimensionDebt` already relies on and the only one exact without a run.
   */
  hollowDays: number;
  /** How many days the window holds, so the count is read against something. */
  windowDays: number;
}

const LANDED = "landed";

function instant(value: string | null | undefined): number | null {
  if (!value) return null;
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? null : parsed;
}

/**
 * The dimension whose arrival explains the hollow, or `null`.
 *
 * `null` — and therefore NO sentence — whenever there is nothing measured to
 * explain: an unreadable history, a mode that pulls nothing, a window with no
 * landed day, a plan whose every dimension predates every day of the window.
 * The last case matters most: a screen that always says something says nothing,
 * and a note printed over a reading that is hollow for another reason would send
 * a person to re-collect days that already carry the field.
 */
export function lateDimension(
  history: DimensionHistory | null | undefined,
): LateDimension | null {
  if (!history || history.state !== "measured" || !Array.isArray(history.days)) return null;
  const declared = history.first_declared;
  if (!declared) return null;

  const landed = history.days
    .filter((day) => day.state === LANDED)
    .map((day) => instant(day.landed_at))
    .filter((value): value is number => value !== null);
  if (landed.length === 0) return null;

  let latest: LateDimension | null = null;
  for (const [field, entry] of Object.entries(declared)) {
    const declaredAt = instant(entry?.created_at ?? null);
    if (declaredAt === null) continue;
    const hollowDays = landed.filter((day) => day < declaredAt).length;
    if (hollowDays === 0) continue;
    if (latest && instant(latest.declaredAt)! >= declaredAt) continue;
    latest = {
      field,
      declaredAt: entry.created_at as string,
      versionNumber: entry.version_number,
      hollowDays,
      windowDays: history.window?.days ?? history.days.length,
    };
  }
  return latest;
}

/**
 * The one sentence a hollow reading owes its reader.
 *
 * It names the dimension, the day it arrived, and how many collected days
 * predate it — never « some days are missing », which is the mute hole the
 * amendment refuses. It states no repair: the bounded, costed re-collection
 * lives on `Processing` beside the change that creates the debt, and offering a
 * second door to it here would be the same gesture asked twice.
 */
export function lateDimensionSentence(late: LateDimension): string {
  const day = late.declaredAt.slice(0, 10);
  return (
    `${late.field} entered this plan on ${day} (version ${late.versionNumber}), ` +
    `so the ${late.hollowDays} day(s) of this window collected before then carry ` +
    `no value for it. The gap is its arrival, not a failed collection.`
  );
}

/**
 * The same sentence for ONE day, or `null` when that day is not affected.
 *
 * Held apart from `lateDimensionSentence` deliberately: the grid explains a
 * hollow STRIP and the day panel explains ONE hollow reading, and printing the
 * window-wide sentence beside a single day would answer a question nobody asked
 * there. A day that post-dates the declaration gets nothing at all.
 */
export function lateDimensionSentenceForDay(
  history: DimensionHistory | null | undefined,
  day: string | null | undefined,
): string | null {
  const late = lateDimension(history);
  if (!late || !day) return null;
  const entry = history?.days?.find((candidate) => candidate.date === day);
  if (!entry || entry.state !== LANDED) return null;
  const landedAt = instant(entry.landed_at);
  const declaredAt = instant(late.declaredAt);
  if (landedAt === null || declaredAt === null || landedAt >= declaredAt) return null;
  return (
    `This day was collected before ${late.field} entered the plan on ` +
    `${late.declaredAt.slice(0, 10)}, so that column is empty here because it was ` +
    `never asked for — not because the source returned nothing.`
  );
}
