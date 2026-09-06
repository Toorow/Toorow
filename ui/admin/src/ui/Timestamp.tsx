/**
 * Timestamp — one instant, one rendering, everywhere in the console.
 *
 * WHY IT EXISTS. Six hand-rolled formatters were producing three different
 * strings for the same instant: `toLocaleString("en-GB")`, `toLocaleString()`
 * with no locale (so the reading changes with the machine), and
 * `toISOString()` sliced and suffixed with a literal ` UTC`. A person
 * comparing a Run in one screen with the same Run in another cannot tell
 * whether the evidence disagrees or the formatting does.
 *
 * THE FORMAT IS `en-GB`, AND THAT IS A MAJORITY DECISION, NOT A PREFERENCE.
 * No document under `docs/product-architecture/` fixes a datetime format, so
 * the tie is broken by what the console already does: `toLocaleString("en-GB")`
 * is what fourteen call sites use (Governance evidence, Data collection, Org
 * settings, the Datastream workbench), against one `en-US` and one ISO. Day
 * before month, 24-hour clock, seconds kept — evidence times are compared to
 * the second.
 *
 * THE ZONE IS ALWAYS NAMED. That is the one property the ISO formatter in
 * `analyze-artifacts/Shared.tsx` had and the majority formatter did not, and
 * it is additive rather than contradictory: `timeZoneName: "short"` keeps the
 * en-GB ordering and appends `CEST`/`UTC`/`GMT+2`. A bare local time is how
 * two people reading two screens conclude the evidence disagrees.
 *
 * THE INSTANT SURVIVES THE FORMATTING. The rendering is local and therefore
 * lossy across readers, so the component emits a `<time dateTime>` carrying
 * the exact ISO instant. Nothing has to be re-derived from the visible text.
 *
 * ABSENT IS A STATEMENT, NOT A BLANK. `null`, `undefined` and `""` render as
 * an em dash whose `title` says what the dash means. `Invalid Date` never
 * reaches a screen: a value that will not parse is shown verbatim, because a
 * server sending `"soon"` should be legible to whoever has to fix it, not
 * hidden behind a placeholder that blames the reader's browser.
 *
 * SINCE 76-1 THIS FILE IS ONE HALF OF A PAIR. `ui/format.ts` owns numbers,
 * this one owns dates, and `console-presentation.md` §2 makes the two the ONLY
 * files in `ui/admin/src` allowed to call `toLocaleString`,
 * `toLocaleDateString`, `toLocaleTimeString` or `Intl.*`. The locale and the
 * dash come from there, so a change to either moves both files at once.
 */
import type { ReactNode } from "react";

import { CONSOLE_LOCALE, NO_VALUE } from "./format";

/**
 * What a missing time reads as. One dash, one meaning, one spelling — and
 * since 76-1 the SAME dash as every other absence in the console
 * (`console-presentation.md` §2: « `NO_VALUE` — `—`, the same dash as
 * `NO_TIMESTAMP` »). Kept under its own name because a screen that says
 * `NO_TIMESTAMP` is saying which absence it means.
 */
export const NO_TIMESTAMP = NO_VALUE;

/** What that dash means, said out loud rather than left to be guessed. */
export const NO_TIMESTAMP_MEANING = "No time recorded";

export type TimestampValue = string | number | Date | null | undefined;

/**
 * The string form, for the places that cannot mount a component — a `title`
 * attribute, an aria-label, a `Metric` value, a sentence being composed.
 *
 * Callers that CAN mount a component should, so the machine-readable instant
 * travels with the human one.
 */
export function formatTimestamp(value: TimestampValue): string {
  const parsed = parseTimestamp(value);
  if (parsed === "absent") return NO_TIMESTAMP;
  // Not a datetime. Show what was actually received — see the header.
  if (parsed === "unreadable") return String(value);
  return parsed.toLocaleString(CONSOLE_LOCALE, { timeZoneName: "short" });
}

/**
 * `absent` when there is nothing to show, `unreadable` when there is something
 * that is not a time, and the `Date` otherwise. Three outcomes because the
 * three mean different things to whoever is reading the screen.
 */
function parseTimestamp(value: TimestampValue): Date | "absent" | "unreadable" {
  if (value === null || value === undefined || value === "") return "absent";
  const parsed = value instanceof Date ? value : new Date(value);
  return Number.isNaN(parsed.getTime()) ? "unreadable" : parsed;
}

/**
 * The DATE grain, no time — `console-presentation.md` §2 applied to the eight
 * `toLocaleDateString` calls 76-1 gathered here (« Last verified 14 Aug 2026 »,
 * an authorization expiry, a Skill's `updated_at`). Day before month and the
 * month as a word, because `14/08` and `08/14` are the same six characters and
 * a reader cannot tell which convention a screen chose.
 *
 * `year: false` is for a dense cell whose surrounding window already fixes the
 * year — the Data workspace's publication column. It is a parameter and never
 * a guess: dropping the year from a row that spans two of them is how a table
 * shows the same date twice.
 */
export function formatDate(
  value: TimestampValue,
  { year = true }: { year?: boolean } = {},
): string {
  const parsed = parseTimestamp(value);
  if (parsed === "absent") return NO_TIMESTAMP;
  if (parsed === "unreadable") return String(value);
  return parsed.toLocaleDateString(CONSOLE_LOCALE, {
    day: "numeric",
    month: "short",
    ...(year ? { year: "numeric" as const } : {}),
  });
}

/**
 * The CLOCK grain, no date — for the places where the day is already said by
 * the row around it (« measured 4 s ago, at 11:05 ») and for the second half
 * of `yesterday 14:05`. 24-hour, per §2's « Grouping with a comma, decimal
 * point, 24-hour clock ».
 *
 * `seconds` is off by default and on where the reading is a measurement rather
 * than an appointment: a poll that refreshes every ten seconds needs them, a
 * scheduled start does not.
 */
export function formatClock(
  value: TimestampValue,
  { seconds = false }: { seconds?: boolean } = {},
): string {
  const parsed = parseTimestamp(value);
  if (parsed === "absent") return NO_TIMESTAMP;
  if (parsed === "unreadable") return String(value);
  return parsed.toLocaleTimeString(CONSOLE_LOCALE, {
    hour: "2-digit",
    minute: "2-digit",
    ...(seconds ? { second: "2-digit" as const } : {}),
  });
}

const MINUTE_MS = 60_000;
const HOUR_MS = 60 * MINUTE_MS;
const DAY_MS = 24 * HOUR_MS;

/** Calendar days between two instants, in the reader's own zone. */
function calendarDaysApart(from: Date, to: Date): number {
  const midnight = (date: Date) =>
    new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
  return Math.round((midnight(from) - midnight(to)) / DAY_MS);
}

/**
 * `console-presentation.md` §2, "Relative time": « `Timestamp.tsx` gains
 * `formatRelative(value, now)` and `<Timestamp relative>`: `just now` ·
 * `4 min ago` · `2 h ago` · `yesterday 14:05` · `tomorrow 02:00` · beyond seven
 * days the absolute form. The absent case keeps its meaning (`Never run`,
 * `Not scheduled`) — one wording per case, declared by the caller through
 * `absentMeaning`, never improvised in a template string. »
 *
 * ONE WORDING PER CASE, AND THE DIRECTION IS ALWAYS SAID. « 4 min » alone was
 * three different screens' way of saying both « four minutes ago » and « in
 * four minutes »; a minus sign in front of a count is not an answer either.
 *
 * `now` IS A PARAMETER because a list renders many rows against ONE reading of
 * the clock — two rows an hour apart must not disagree about what « now » was —
 * and because a formatter that reads the wall clock cannot be tested.
 *
 * YESTERDAY AND TOMORROW ARE CALENDAR DAYS, not 24-hour windows: at 00:30, an
 * event at 23:00 is « yesterday », and calling it « 1 h ago » is true but not
 * what a person asking « which day » wants to read.
 */
export function formatRelative(value: TimestampValue, now: Date = new Date()): string {
  const parsed = parseTimestamp(value);
  if (parsed === "absent") return NO_TIMESTAMP;
  if (parsed === "unreadable") return String(value);

  const delta = parsed.getTime() - now.getTime();
  const distance = Math.abs(delta);
  const ahead = delta > 0;
  const said = (body: string) => (ahead ? `in ${body}` : `${body} ago`);

  if (distance < MINUTE_MS) return "just now";
  if (distance < HOUR_MS) return said(`${Math.floor(distance / MINUTE_MS)} min`);

  const days = calendarDaysApart(parsed, now);
  if (days === 0) return said(`${Math.floor(distance / HOUR_MS)} h`);
  if (days === -1) return `yesterday ${formatClock(parsed)}`;
  if (days === 1) return `tomorrow ${formatClock(parsed)}`;
  // Two days out to the seventh: the relative form still helps, and the
  // wording stays the one this function uses everywhere else.
  if (Math.abs(days) <= 7) return said(`${Math.abs(days)} d`);
  // Beyond seven days a relative count stops being a reading and becomes
  // arithmetic the person has to do. The instant itself is the answer.
  return formatTimestamp(parsed);
}

export interface TimestampProps {
  value: TimestampValue;
  /**
   * What is absent, in the reader's words — "Never run", "Not scheduled". It
   * replaces the generic meaning on the dash's `title`, and it is the only way
   * a column of dashes tells a reader which question it is answering.
   */
  absentMeaning?: string;
  /**
   * Render the RELATIVE wording (`4 min ago`) instead of the instant. The
   * instant stays reachable — on the `title` for a mouse, in `dateTime` for a
   * machine — because a relative reading is a convenience and never the record.
   */
  relative?: boolean;
  /**
   * The reading of the clock this row is measured against. A list passes one
   * for all its rows; omitted, each row reads the wall clock for itself.
   */
  now?: Date;
  className?: string;
  /**
   * A test hook, and the only prop here that is not a design decision — the
   * same exemption `Status` documents beside its own.
   */
  "data-testid"?: string;
}

export function Timestamp({
  value,
  absentMeaning = NO_TIMESTAMP_MEANING,
  relative = false,
  now,
  className,
  "data-testid": testId,
}: TimestampProps): ReactNode {
  const parsed = parseTimestamp(value);

  if (parsed === "absent") {
    return (
      <span className={className} title={absentMeaning} data-testid={testId}>
        <span aria-hidden>{NO_TIMESTAMP}</span>
        {/* The dash is decoration to a screen reader; the meaning is the text. */}
        <span className="sr-only">{absentMeaning}</span>
      </span>
    );
  }

  if (parsed === "unreadable") {
    return (
      <span className={className} title="This value is not a readable time" data-testid={testId}>
        {String(value)}
      </span>
    );
  }

  const absolute = parsed.toLocaleString(CONSOLE_LOCALE, { timeZoneName: "short" });

  return (
    <time
      className={className}
      dateTime={parsed.toISOString()}
      // The absolute reading is what two people compare across two screens, so
      // the relative form never hides it: it moves to the title.
      title={relative ? absolute : undefined}
      data-testid={testId}
    >
      {relative ? formatRelative(parsed, now) : absolute}
    </time>
  );
}
