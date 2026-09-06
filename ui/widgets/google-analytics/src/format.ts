/**
 * Story 76-8 — the widget reads THE SAME formatter as the cards, and this file
 * is the only thing it adds.
 *
 * THIS FILE DECIDES NOTHING. Every line below is `export … from`; there is no
 * locale, no rule and no branch here. `WidgetFormatterIsNotASecondModule.test.ts`
 * asserts exactly that, so the file cannot quietly grow into the second module
 * `console-presentation.md` forbids ("a second module answering the same
 * question is the defect, whatever it answers").
 *
 * WHY A RELATIVE PATH AND NOT A PACKAGE IMPORT — a constraint, not a preference.
 * The pinned formatter lives in `@toorow/card-shell` (`src/viz/theme/
 * formatters.ts`, story 50.5). Declaring it as a dependency of this widget
 * requires a `pnpm-lock.yaml` write, and MEASURED on 2026-09-05:
 *
 *     $ pnpm -C ui install --frozen-lockfile
 *     ERR_PNPM_OUTDATED_LOCKFILE  Cannot install with "frozen-lockfile" because
 *     pnpm-lock.yaml is not up to date with widgets/google-analytics/package.json
 *
 * and `--frozen-lockfile` is what `infra/scripts/deploy.sh:513`,
 * `infra/docker/mcp-server/Dockerfile:79` and `.github/workflows/ci.yml:223`
 * all run — so a manifest committed ahead of its lockfile fails the DEPLOYMENT,
 * not a test. The lockfile is one file shared by every session; rewriting it (and
 * relinking `node_modules`) from inside a story that owns `ui/cards/**` and
 * `ui/widgets/**` is the parallel-session hazard this repository has been bitten
 * by. The declaration therefore belongs to one central gesture:
 *
 *     pnpm -C ui add --filter @toorow/widget-google-analytics @toorow/card-shell@workspace:*
 *
 * after which the single `from` path below becomes `"@toorow/card-shell"` and
 * nothing else in the widget changes.
 *
 * WHAT THIS BUYS. Before it, `KpiTile`, `BreakdownBars`, `DayDetail` and
 * `CalendarHeatmap` each grouped their numbers on a French locale of their own
 * while the tile's delta went through `toFixed` — so the daily report showed
 * « 40 467 » beside « -8.6 % », two decimal conventions on one screen, the same
 * defect measured on `card-keywords`. There is now one, and it is the Render's.
 */

// The vocabulary that displays an identifier travels the same way: a source
// system slug reads « Google Analytics », with the token in monospace beside it
// (`console-presentation.md` §4, story 76-8).
export { sourceSystemLabel } from "../../../cards/shell/src/types";

// The variation legend and the ONE function that decides a verdict colour reach
// the widget through the same door: a widget that judged with its own rule would
// be the second authority `console-presentation.md` §2 forbids, and its tiles
// painted three red deltas with no legend at all until story 76-8 round 2.
export { default as VariationLegend } from "../../../cards/shell/src/VariationLegend";
export type {
  VariationConvention,
  VariationLegendProps,
} from "../../../cards/shell/src/VariationLegend";
export { verdictColor, verdictTone } from "../../../cards/shell/src/verdictTone";
export type { VerdictDirection } from "../../../cards/shell/src/verdictTone";

export {
  FORMATTER_LOCALE,
  NBSP,
  NO_VALUE,
  formatCompact,
  formatCurrency,
  formatMeasure,
  formatPercent,
  formatValue,
} from "../../../cards/shell/src/viz/theme/formatters";
