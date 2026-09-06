/**
 * ONE function decides whether a variation is favourable, for every primitive
 * that colours one (story 76-8, review round 2).
 *
 * WHY IT EXISTS, AND IT IS A CONTRADICTION ON SCREEN RATHER THAN A TIDY-UP.
 * `BarChart` coloured from `entry.direction` alone -- a field the server sets --
 * while the legend beneath it stated the convention of `semanticDirection`. On
 * `card-keywords` (binding `down_good`, average position) the two disagreed in
 * the most visible way possible: `+3.2` was painted GREEN and `-3.8` RED under a
 * legend reading "lower is favourable". A reader who trusts the legend reads
 * every one of those five bars backwards.
 *
 * So the verdict is derived HERE, from the signed change and the governed
 * convention, and `VariationLegend` states exactly the rule this function
 * applies. One cannot drift from the other because there is only one of each.
 *
 * IT NEVER INVENTS A VERDICT. No convention (`neutral`, or none declared) means
 * no verdict: the caller keeps its flat accent. A change of zero is not an
 * improvement, so it is `none` too -- except where the caller compares against a
 * TARGET, where meeting it exactly is meeting it (`zeroIsFavourable`).
 */

import type { WidgetTheme } from "@toorow/shell";

/** How the governed member reads: which way is the desirable one. */
export type VerdictDirection = "up_good" | "down_good" | "neutral";

/** What the colour is allowed to say. */
export type Verdict = "favourable" | "unfavourable" | "none";

export interface VerdictOptions {
  /**
   * A change of exactly zero. `false` (the default) for a period-over-period
   * delta: nothing moved, so nothing is judged. `true` when the caller compares
   * a value against a TARGET, where landing on it is landing on it.
   */
  zeroIsFavourable?: boolean;
}

/**
 * The signed change, plus the convention, gives the verdict.
 *
 * `change` is the movement in the METRIC'S own units and sign — `-3.8` positions,
 * `+8.8` percent, `value - target`. Callers that hold an absolute magnitude for
 * layout must pass the signed one here; that split is why `BarChartEntry` grew
 * `signedValue`.
 */
export function verdictTone(
  change: number | null | undefined,
  direction: VerdictDirection | undefined,
  options: VerdictOptions = {},
): Verdict {
  if (!direction || direction === "neutral") return "none";
  if (change === null || change === undefined || !Number.isFinite(change)) return "none";
  if (change === 0) return options.zeroIsFavourable ? "favourable" : "none";
  const rising = change > 0;
  const good = direction === "up_good" ? rising : !rising;
  return good ? "favourable" : "unfavourable";
}

/**
 * The one place a verdict becomes a colour. `fallback` is what the primitive
 * paints when nothing is judged — its own accent, never a third semantic tone.
 */
export function verdictColor(theme: WidgetTheme, verdict: Verdict, fallback: string): string {
  if (verdict === "favourable") return theme.palette.success.main;
  if (verdict === "unfavourable") return theme.palette.error.main;
  return fallback;
}

/**
 * The mark a surface draws beside the figure. Kept here so the arrow and the
 * colour cannot tell two different stories: both read the same change.
 */
export function changeMark(change: number | null | undefined): "▲" | "▼" | "=" | null {
  if (change === null || change === undefined || !Number.isFinite(change)) return null;
  if (change === 0) return "=";
  return change > 0 ? "▲" : "▼";
}
