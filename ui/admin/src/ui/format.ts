/**
 * format — one module, one locale, for every number the console prints.
 *
 * WHY IT EXISTS. Measured on 2026-09-05 before any code: 69 calls to
 * `toLocaleString` / `toLocaleDateString` / `toLocaleTimeString` / `Intl.*`
 * spread over 36 files of `ui/admin/src`, with four different answers to the
 * same question — `"en-US"`, `"en-GB"`, `"default"`, and no locale at all (so
 * the reading changed with the machine the browser ran on). A person comparing
 * a row count on one screen with the same row count on another cannot tell
 * whether the evidence disagrees or the formatting does.
 *
 * THE RULE, from `docs/product-architecture/console-presentation.md` §2:
 * « `ui/admin/src/ui/format.ts` [...] is the only file in `ui/admin/src`
 * allowed to call `toLocaleString`, `toLocaleDateString`, `toLocaleTimeString`
 * or `Intl.*`, with `Timestamp.tsx` as the one other exception (dates).
 * Nothing else — a grep gate holds it. »
 * `ui/admin/src/__tests__/FormattingIsCentral.test.ts` is that gate.
 *
 * DATES ARE NOT HERE. `Timestamp.tsx` owns instants, dates and clocks; it
 * reads `CONSOLE_LOCALE` from this file so the two never drift. Splitting them
 * is deliberate: a date has an absent MEANING ("Never run", "Not scheduled")
 * that a number does not.
 *
 * NOTHING HERE ROUNDS ON ITS OWN INITIATIVE. `formatNumber` passes the
 * caller's `maximumFractionDigits` through and adds none; the two functions
 * that do round — `formatCompact` and `formatBytes` — round because their unit
 * is the rounding (`1.2k` is what `k` means).
 */

/**
 * `console-presentation.md` §2: « **Locale.** `en-GB`, the locale
 * `Timestamp.tsx` already pinned by majority on 2026-08-14. Grouping with a
 * comma, decimal point, 24-hour clock. One constant, `CONSOLE_LOCALE`, read by
 * both files. »
 */
export const CONSOLE_LOCALE = "en-GB";

/**
 * `console-presentation.md` §2, table: « `NO_VALUE` — `—`, the same dash as
 * `NO_TIMESTAMP`; a blank cell is a rendering bug. »
 *
 * `Timestamp.tsx` re-exports it as `NO_TIMESTAMP`, so the console has one dash
 * with one spelling and its existing callers keep working.
 */
export const NO_VALUE = "—";

/** Every shape a screen actually holds where a number was expected. */
export type NumericValue = number | null | undefined;

/**
 * `null`, `undefined`, `NaN` and the infinities are not numbers a screen can
 * print. They render as `NO_VALUE` rather than `NaN`, `Infinity` or a blank
 * cell — the reader learns nothing from any of the three.
 */
function readable(value: NumericValue): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/**
 * `console-presentation.md` §2, table: « `formatNumber(value, {
 * maximumFractionDigits? })` — grouping always, no rounding beyond what the
 * caller asks, `null`/`undefined`/`NaN` → `NO_VALUE`. » Examples: `12,480`,
 * `0.42`.
 *
 * With no options this is exactly `new Intl.NumberFormat(CONSOLE_LOCALE)`,
 * whose own default is three fraction digits at most — so a call site that
 * held `new Intl.NumberFormat().format(n)` reads the same number after
 * migration, on every machine instead of only on a British one.
 */
export function formatNumber(
  value: NumericValue,
  options: { maximumFractionDigits?: number; minimumFractionDigits?: number } = {},
): string {
  const numeric = readable(value);
  if (numeric === null) return NO_VALUE;
  // `-0` is a rounding artefact, never a measurement: it prints as `0`.
  return new Intl.NumberFormat(CONSOLE_LOCALE, options).format(numeric === 0 ? 0 : numeric);
}

/**
 * `console-presentation.md` §2, table: « `formatCompact(value)` — `k` / `M` /
 * `B` / `T`, one decimal below 100, none above. » Examples: `1.2k`, `34M`.
 *
 * `Intl`'s own `notation: "compact"` is used rather than a ladder of divisions,
 * because it is the one that gets the boundaries right (999,999 is `1M`, not
 * `1000.0k`).
 *
 * TWO CORRECTIONS TO WHAT `Intl` RETURNS, both measured rather than assumed:
 *  - en-GB spells the thousand step `K`; the rule spells it `k` (`1.2k`), so
 *    the suffix is lowered. `M`/`B`/`T` are already the rule's spelling.
 *  - "one decimal below 100" is about the COMPACT figure, not the raw one:
 *    1,200 is `1.2k` and 123,400 is `123k`, both under the same rule.
 * Below the first step there is nothing to compact, and rounding 0.42 to
 * `0.4` would lose a digit the caller still has: `formatNumber` answers.
 */
export function formatCompact(value: NumericValue): string {
  const numeric = readable(value);
  if (numeric === null) return NO_VALUE;
  const magnitude = Math.abs(numeric);
  if (magnitude < 1000) return formatNumber(numeric);
  const step = 10 ** (3 * Math.floor(Math.log10(magnitude) / 3));
  return new Intl.NumberFormat(CONSOLE_LOCALE, {
    notation: "compact",
    compactDisplay: "short",
    maximumFractionDigits: magnitude / step < 100 ? 1 : 0,
  })
    .format(numeric)
    .replace(/K$/, "k");
}

/**
 * The space that sits between a number and its unit sign. U+202F, NARROW
 * NO-BREAK SPACE: no-break so `42.0` and `%` never land on two lines, narrow
 * because a full space between a figure and its sign reads as two words.
 */
export const THIN_SPACE = " ";

/**
 * `console-presentation.md` §2, table: « `formatPercent(ratio, { digits = 1
 * })` — the input is a **ratio** (0.42), never a percentage; a thin space
 * before `%`; `0` renders `0.0 %`, never blank. » Example: `42.0 %`.
 *
 * THE INPUT IS A RATIO, AND THAT IS THE WHOLE RISK OF THIS FUNCTION. A call
 * site holding a figure the server already multiplied by 100 passes
 * `{ scaled: true }` and says so at the call site; there is no guess here,
 * because a wrong guess silently multiplies a screen's number by a hundred.
 */
export function formatPercent(
  value: NumericValue,
  { digits = 1, scaled = false }: { digits?: number; scaled?: boolean } = {},
): string {
  const numeric = readable(value);
  if (numeric === null) return NO_VALUE;
  const ratio = scaled ? numeric / 100 : numeric;
  // Fixed digits, not "up to": `0` must read `0.0 %` and not `0 %`, so a
  // column of percentages lines up on its decimal point.
  const body = new Intl.NumberFormat(CONSOLE_LOCALE, {
    style: "percent",
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(ratio === 0 ? 0 : ratio);
  // `Intl` glues the sign to the figure in en-GB; the rule asks for a thin
  // space, so the sign is re-attached rather than left to the locale.
  return `${body.replace(/\s*%$/, "")}${THIN_SPACE}%`;
}

/**
 * The SAME decision as `formatPercent`, as a number instead of a string.
 *
 * `Progress` and `aria-valuenow` take a number 0-100, not a rendering, and the
 * console wrote `Math.round(x * 100)` by hand in four places to get one — which
 * is the arithmetic half of the class the grep gate refuses. It lives here so
 * the rounding of a percentage is decided once, whether a person reads it or a
 * screen reader announces it.
 *
 * It CLAMPS nothing: 118 % of a quota is a real reading, and the caller that
 * needs a ceiling says so (`Math.min(100, percentValue(...))`).
 */
export function percentValue(value: NumericValue): number {
  const numeric = readable(value);
  return numeric === null ? 0 : Math.round(numeric * 100);
}

/**
 * `console-presentation.md` §2, table: « `formatCurrency(amount, currency)` —
 * `Intl` currency style, narrow symbol; an unknown or missing currency code
 * renders the number followed by the code, never a bare number. » Examples:
 * `€30`, `£1,240.50`, `1,240 XXX`.
 *
 * AND: « **Amounts stay exact.** `formatCurrency` takes a number the server
 * already converted; it never divides micros. The cards' `canonicalAmount.ts`
 * does that on its side and stays there. »
 *
 * TWO WAYS A CODE IS UNKNOWN, and both were measured before this was written.
 * A malformed code (`NOPE`, `""`) makes `Intl` throw a `RangeError`. A
 * well-formed code with no symbol — `XXX`, the ISO code that means « no
 * currency » — does not throw: it renders the GENERIC currency sign `¤`, which
 * tells a reader nothing at all. Both are answered the same way, number then
 * code. A bare figure whose currency nobody can name is a different statement
 * from a labelled one — `evidence.ts#moneyText` already refuses to print it,
 * and this is the same refusal one floor down.
 */
export function formatCurrency(
  value: NumericValue,
  currency: string | null | undefined,
  options: { maximumFractionDigits?: number; minimumFractionDigits?: number } = {},
): string {
  const numeric = readable(value);
  if (numeric === null) return NO_VALUE;
  const amount = numeric === 0 ? 0 : numeric;
  const code = typeof currency === "string" ? currency.trim().toUpperCase() : "";
  if (code) {
    try {
      const rendered = new Intl.NumberFormat(CONSOLE_LOCALE, {
        style: "currency",
        currency: code,
        currencyDisplay: "narrowSymbol",
        ...options,
      }).format(amount);
      // U+00A4 GENERIC CURRENCY SIGN — `Intl` accepted the code and has no
      // symbol for it. Falls through rather than printing a placeholder glyph.
      if (!rendered.includes("¤")) return rendered;
    } catch {
      // Falls through to the number-plus-code form below.
    }
  }
  const figure = formatNumber(amount, options);
  return code ? `${figure} ${code}` : figure;
}

/**
 * `console-presentation.md` §2, table: « `formatBytes(bytes)` — binary steps
 * `B / KB / MB / GB`, one decimal below 10. » Example: `4.2 MB`.
 *
 * `TB` and `PB` continue the same ladder: a warehouse scan estimate reaches
 * them, and stopping at `GB` would print a five-digit `GB` figure nobody
 * reads. The steps are binary (1024), which is what a storage API reports.
 */
export function formatBytes(value: NumericValue): string {
  const numeric = readable(value);
  if (numeric === null) return NO_VALUE;
  const sign = numeric < 0 ? "-" : "";
  const magnitude = Math.abs(numeric);
  if (magnitude < 1024) return `${sign}${magnitude} B`;
  const units = ["KB", "MB", "GB", "TB", "PB"];
  let scaled = magnitude / 1024;
  let unit = 0;
  while (scaled >= 1024 && unit < units.length - 1) {
    scaled /= 1024;
    unit += 1;
  }
  return `${sign}${scaled.toFixed(scaled < 10 ? 1 : 0)} ${units[unit]}`;
}

/**
 * `console-presentation.md` §2, table: « `formatDuration(ms)` — `< 1 s` ·
 * `12 s` · `4 min 05 s` · `2 h 14 min` · `3 d 4 h`; never a raw millisecond
 * count. » Example: `4 min 05 s`.
 *
 * The seconds are zero-padded inside a minutes form and not outside it: `4 min
 * 05 s` is a clock reading, `12 s` is a count. The larger unit always leads, so
 * the eye compares the same field down a column.
 *
 * `precise` IS FOR A MEASUREMENT, NOT FOR A DURATION A PERSON WAITS. Two
 * surfaces measure themselves to the tenth — the nightly sequence's per-step
 * timing (`shell/pages/PlatformClocks.tsx`) and the delivery panel's read
 * latency — and for them `< 1 s` erases the difference between 4 ms and 900 ms,
 * which is the whole point of the reading. Under `precise` a sub-second value
 * keeps its millisecond count and a sub-minute one keeps one decimal; above a
 * minute the ladder is the same, because nobody times a two-minute step to the
 * tenth. It is a parameter and never a default: the other twenty surfaces read
 * better without it.
 */
export function formatDuration(
  value: NumericValue,
  { precise = false }: { precise?: boolean } = {},
): string {
  const numeric = readable(value);
  if (numeric === null) return NO_VALUE;
  const sign = numeric < 0 ? "-" : "";
  const ms = Math.abs(numeric);
  if (ms < 1000) return precise ? `${sign}${Math.round(ms)} ms` : `${sign}< 1 s`;
  const totalSeconds = Math.floor(ms / 1000);
  if (precise && totalSeconds < 60) return `${sign}${(ms / 1000).toFixed(1)} s`;
  if (totalSeconds < 60) return `${sign}${totalSeconds} s`;
  const totalMinutes = Math.floor(totalSeconds / 60);
  if (totalMinutes < 60) {
    return `${sign}${totalMinutes} min ${String(totalSeconds % 60).padStart(2, "0")} s`;
  }
  const totalHours = Math.floor(totalMinutes / 60);
  if (totalHours < 24) return `${sign}${totalHours} h ${totalMinutes % 60} min`;
  return `${sign}${Math.floor(totalHours / 24)} d ${totalHours % 24} h`;
}

/**
 * `console-presentation.md` §2, table: « `formatCount(n, singular, plural?)` —
 * number + noun agreed. » Examples: `1 run`, `12 runs`.
 *
 * The plural is a parameter and not an appended `s`, because the console
 * counts `queries`, `anomalies` and `entities` as often as it counts `runs`.
 * A count nobody measured is `NO_VALUE` and no noun: « 0 runs » and « no
 * measurement » are two different statements and the caller owns the second.
 */
export function formatCount(
  value: NumericValue,
  singular: string,
  plural: string = `${singular}s`,
): string {
  const numeric = readable(value);
  if (numeric === null) return NO_VALUE;
  return `${formatNumber(numeric)} ${Math.abs(numeric) === 1 ? singular : plural}`;
}

/**
 * THE CATCH-UP NOTATION, WRITTEN ONCE — story 76-6, arbitrage 2.
 *
 * A collection window that ends N days before today is spoken as `D−3` in this
 * console. Three things about that string are decisions and not typography:
 *
 *   - the letter is `D`, not `J`. « J-3 » is French (`jour`), and §1 of
 *     `console-presentation.md` settles the console's language; it survived in
 *     `SchedulePanel`'s extraction-offset hint, which was the last instance of
 *     the epic's « Nuit · J-3 » family still in the tree at HEAD.
 *   - the sign is U+2212 MINUS SIGN, the same character `formatRelative`
 *     refuses in front of a count. Here it is not a direction word standing in
 *     for « when » — it is an OFFSET from a named origin, which is a
 *     subtraction and reads as one.
 *   - `D` alone is today. `D−0` is a subtraction nobody performed.
 *
 * A negative offset is not an answer this notation has: a window cannot end in
 * the future, so the caller gets `NO_VALUE` rather than a `D+1` this product
 * has no meaning for.
 */
export const MINUS_SIGN = "−";

export function formatDayOffset(value: NumericValue): string {
  const numeric = readable(value);
  if (numeric === null || numeric < 0) return NO_VALUE;
  const days = Math.floor(numeric);
  return days === 0 ? "D" : `D${MINUS_SIGN}${formatNumber(days)}`;
}
