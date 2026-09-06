/**
 * ONE MODULE FORMATS, THE REST CALL IT.
 *
 * `docs/product-architecture/console-presentation.md` §2 says it in words:
 * « `ui/admin/src/ui/format.ts` [...] is the only file in `ui/admin/src`
 * allowed to call `toLocaleString`, `toLocaleDateString`, `toLocaleTimeString`
 * or `Intl.*`, with `Timestamp.tsx` as the one other exception (dates).
 * Nothing else — a grep gate holds it. » And in its `Incomplete if`: « A file
 * under `ui/admin/src` other than `ui/format.ts` and `ui/Timestamp.tsx` calls
 * `toLocaleString`, `toLocaleDateString`, `toLocaleTimeString` or `Intl.` —
 * `ui/admin/src/__tests__/FormattingIsCentral.test.ts` greps it. »
 *
 * WHY A GREP AND NOT A REVIEW. Measured 2026-09-05 before 76-1: 69 such calls
 * in 36 files, answering the same question four different ways — `"en-US"`,
 * `"en-GB"`, `"default"`, and no locale at all, which means the number changed
 * with the machine the browser ran on. Every one of them was added by somebody
 * who did not know the others existed. A rule that depends on a reviewer
 * remembering it is the rule that produced those sixty-nine.
 *
 * THE EXCEPTIONS ARE NAMED WITH THEIR REASON, not merely listed. A path in
 * this table without a sentence explaining why the console's one formatter
 * cannot answer for it is the next sixty-nine starting over.
 *
 * THE FOUR `Intl` SPELLINGS ARE NOT THE WHOLE CLASS, and the first version of
 * this gate learned that the hard way: it held every `toLocaleString` and left
 * 8 `toFixed` and 4 `Math.round(x * 100)` sites formatting numbers beside it,
 * with a DIFFERENT answer — `85%` where `formatPercent` says `85.0 %`, `3d`
 * where `formatRelative` says `3 d ago`. A number reaching a screen through
 * arithmetic is the same defect as one reaching it through `Intl`; the gate
 * refuses both.
 *
 * COMMENTS ARE STRIPPED FIRST, the way `EmptyStatesDoNotInstructTheImpossible`
 * learned to. Three files in the console EXPLAIN this very rule by quoting the
 * forbidden names in a comment — `ui/index.ts` says which two modules may call
 * them, `CacheHealthCard.tsx` says why it stopped — and a matcher that reads
 * prose as code reports the explanation as the defect.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { relative, resolve, sep } from "node:path";
import { describe, expect, it } from "vitest";

const CONSOLE_SRC = resolve(__dirname, "..");

/**
 * The four spellings of "let the platform format this for me". `Intl.` covers
 * `NumberFormat`, `DateTimeFormat`, `RelativeTimeFormat` and `ListFormat`
 * alike; the three `toLocale*` methods are the same decision taken on a `Date`
 * or a `Number` in place.
 */
const FORBIDDEN = /toLocaleString|toLocaleDateString|toLocaleTimeString|Intl\./;

/**
 * The same decision taken with arithmetic instead of a platform call.
 *
 * `toFixed` IS A FORMATTING DECISION: it fixes how many digits a person sees,
 * which is `formatNumber`'s `maximumFractionDigits`, `formatPercent`'s
 * `digits`, `formatBytes`' ladder or `formatDuration`'s `precise`.
 *
 * `Math.round(<expr> * 100)` and `* 1000` are the two scalings the console
 * actually wrote by hand: a ratio turned into a percentage, and seconds turned
 * into milliseconds. Multiplying by 100 to feed a NUMERIC prop is untouched —
 * `Progress value={(done / total) * 100}` carries no rounding and no string.
 */
const HAND_ROLLED = /\.toFixed\s*\(|Math\.(?:round|floor|ceil)\s*\([^;\n]*\*\s*(?:100|1000)\s*\)/;

/** Either half of the class. */
const offends = (source: string) => FORBIDDEN.test(source) || HAND_ROLLED.test(source);

/**
 * The files allowed to hold one, each with the reason no other file can.
 *
 * Keys are paths relative to `ui/admin/src`, written with forward slashes.
 */
const EXCEPTIONS: Record<string, string> = {
  "ui/format.ts":
    "The module itself. Every number in the console is formatted here, in one locale — including the two `toFixed` calls that ARE the byte ladder and the `precise` duration.",
  "ui/Timestamp.tsx":
    "Dates, the other half of the pair. §2 names it as the one other exception: an instant, a date and a clock have an absent MEANING a number does not, so they keep their own module. It reads CONSOLE_LOCALE from format.ts, so the two cannot drift.",
  "components/ui/calendar.tsx":
    "The shadcn registry layer, which epic 76 leaves as it is. react-day-picker hands its `formatters` and its `data-day` hook a `Date` and takes a string back, so the call cannot move out of this file; both are pinned to CONSOLE_LOCALE, which is what the rule is for. The month name it produces was `\"default\"` — the reader's machine — until 76-1.",
  "shell/pages/reportingDefaults.tsx":
    "NOT A FORMATTING CALL. `Intl.DateTimeFormat().resolvedOptions().timeZone` READS the reader's IANA zone in order to suggest it as a project default; it formats nothing and pinning it to CONSOLE_LOCALE would return the wrong answer — the zone of the console, not of the person.",
};

/** Every `.ts`/`.tsx` file under `ui/admin/src`, tests included. */
function sourceFiles(directory: string, found: string[] = []): string[] {
  for (const entry of readdirSync(directory)) {
    if (entry === "node_modules" || entry === "dist") continue;
    const full = resolve(directory, entry);
    if (statSync(full).isDirectory()) {
      sourceFiles(full, found);
    } else if (/\.tsx?$/.test(entry)) {
      found.push(full);
    }
  }
  return found;
}

/**
 * A test file may hold one: it is allowed to DERIVE its expectation from
 * `Intl` rather than write out a literal that would only be true in one
 * timezone — `TimestampPrimitive.test.tsx` says so in its own header. What a
 * test renders is not a screen.
 */
const isTest = (path: string) => /\.test\.tsx?$/.test(path);

/** Source with comments stripped — see the header. */
function code(path: string): string {
  return readFileSync(path, "utf-8")
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/\/\/.*/g, "");
}

const offenders = sourceFiles(CONSOLE_SRC)
  .filter((path) => !isTest(path))
  .map((path) => relative(CONSOLE_SRC, path).split(sep).join("/"))
  .filter((path) => !(path in EXCEPTIONS))
  .filter((path) => offends(code(resolve(CONSOLE_SRC, path))));

describe("formatting is central", () => {
  it("finds no file outside the module and its named exceptions", () => {
    // The failure prints the paths, because "one file broke the rule" without
    // saying which is a message that costs the reader the grep this test ran.
    expect(offenders).toEqual([]);
  });

  it("keeps every exception justified, and lets none in silently", () => {
    // Four, and each one carries a sentence. A fifth is a decision, not a fix:
    // it belongs in `console-presentation.md` before it belongs here.
    expect(Object.keys(EXCEPTIONS)).toHaveLength(4);
    for (const [path, reason] of Object.entries(EXCEPTIONS)) {
      expect(reason.length).toBeGreaterThan(40);
      // An exception for a file that no longer holds a call is dead weight
      // that the next reader will copy.
      expect(offends(code(resolve(CONSOLE_SRC, path)))).toBe(true);
    }
  });

  it("refuses the hand-rolled half of the class too, not only `Intl`", () => {
    // The shapes that were live in `ui/admin/src` on 2026-09-05 and now are
    // not. Asserted on strings, so the gate's own reach is testable without
    // writing a file that would then have to be remembered and deleted.
    expect(offends('return `${(value * 100).toFixed(1)} %`;')).toBe(true);
    expect(offends('return `${Math.round(row.confidence * 100)}%`;')).toBe(true);
    expect(offends('return `${value.toFixed(1)}s`;')).toBe(true);
    expect(offends("const ms = Math.round(seconds * 1000);")).toBe(true);
    // A multiplication that feeds a NUMBER, with no rounding and no string, is
    // not a formatting decision — `Progress` takes one of these.
    expect(offends("const percent = Math.min(100, (done / total) * 100);")).toBe(false);
    expect(offends("const hours = Math.floor(elapsed / 3_600_000);")).toBe(false);
  });

  it("holds the module and its dash where §2 put them", () => {
    const source = readFileSync(resolve(CONSOLE_SRC, "ui/format.ts"), "utf-8");
    expect(source).toContain('export const CONSOLE_LOCALE = "en-GB"');
    expect(source).toContain('export const NO_VALUE = "—"');
  });
});
