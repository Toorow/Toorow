/**
 * CoverageBars — one bar per day: where there is data, where it is partial,
 * where there is none, and the control that asks for it again.
 *
 * Jean, 2026-07-29: *"pas dans un calendrier, dans une vue sur une barre avec
 * une barre par jour"*. He is right, and a calendar was the wrong shape for
 * this: a month grid asks the reader to reassemble a continuous interval out
 * of rows of seven, when the question — *where are the gaps* — is answered in
 * one glance by a continuous run of days. A calendar is for picking a date; a
 * bar strip is for reading a period.
 *
 * This is the library form of `shell/pages/CoverageStrip.tsx`, which already
 * draws exactly this and owns 105 lines of `coverage-strip.css`. That screen
 * keeps the ledger fetch; the geometry and the vocabulary move here, and its
 * stylesheet goes when the screen is migrated.
 *
 * ## The vocabulary is the ledger's, not a new one
 *
 * Verbatim from `CoverageStrip` and `core/extract_ledger.py`:
 *
 *   ok             pulled and verified
 *   partial        pulled, verification says incomplete
 *   empty          pulled, and the provider legitimately returned nothing
 *   failed         the pull failed
 *   running        a pull is in flight
 *   never_fetched  never requested at all
 *
 * `empty` and `never_fetched` look alike and mean opposite things: one is an
 * answer, the other is an absence. Merging them turns "we asked and there was
 * no traffic" into "we never asked". So `empty` is filled and muted — it IS
 * data — and `never_fetched` is a dashed outline with nothing in it: the shape
 * of the day is there, waiting, but no one has asked for it.
 *
 * ## Selecting
 *
 * Click a day to take it; click a second to take everything between. The
 * footer says how many of the selection can ACTUALLY be re-collected, because
 * `ok` and `running` are not worth re-asking. `empty` IS — see `REPAIRABLE`.
 *
 * It fetches nothing. The screen owns the ledger call.
 */
"use client";

import { useMemo, useState } from "react";
import { cn } from "../lib/cn";
import { formatNumber } from "./format";
import { Button } from "../components/ui/button";
import { Status } from "./Data";
import { TONE_BORDER, TONE_SURFACE, TONE_SURFACE_HOVER, type Fill, type Tone } from "./tone";

export type CoverageStatus =
  | "ok"
  | "partial"
  | "empty"
  | "failed"
  | "running"
  | "never_fetched";

export type CoverageDay = {
  /** `YYYY-MM-DD`, the ledger's own key. */
  date: string;
  status: CoverageStatus;
  rowCount?: number | null;
  /**
   * Why there is no `rowCount`, when there is none. The ledger's own word.
   *
   * A NUMBER THAT DISAPPEARS READS AS ZERO. Story 58.1 stopped the ledger
   * copying a collection window's row total onto every day that window covered
   * — a 30-day backfill used to publish its own total thirty times, and this
   * strip displayed it as a daily figure. The repair is right, and on its own it
   * makes the count silently vanish from the tooltip of every backfilled day:
   * the reader sees a bar that says `Collected` with nothing beside it, which is
   * exactly how a zero looks. So the absence carries its reason and the tooltip
   * says it.
   */
  rowCountReason?: string | null;
  /**
   * THE COLLECTING WINDOW'S OWN STATE, when the payload carries one.
   *
   * `/ledger` publishes it beside `status` (`extract_ledger._entry`), and a
   * strip that dropped it said « Never requested » about a day the source
   * REFUSED -- the ledger answers `never_fetched` for `prevented`,
   * `cancelled` and `superseded` alike, so the verdict alone cannot tell an
   * absence from a refusal. Optional because a caller may not hold it; absent,
   * the day is read exactly as it was before.
   */
  jobState?: string | null;
};

/**
 * The ledger's reason for a day with no count of its own.
 *
 * Mirrors `extract_ledger.ROW_COUNT_MEASURED_PER_WINDOW`. Declared once here and
 * read by every strip, because two components spelling the same server word
 * apart is a silence on one of them.
 */
export const MEASURED_PER_WINDOW = "measured_per_window";

/**
 * The OTHER absence, and it is a different fact: nothing verified this pull at
 * all. Mirrors `extract_ledger.ROW_COUNT_NOT_VERIFIED`. A strip could stay
 * silent about it because a bar carries no cell; a grid cannot — a day-by-day
 * row with a blank volume reads as a zero, which is the defect story 58.1 spent
 * itself removing.
 */
export const NOT_VERIFIED = "not_verified";

/**
 * The sentence a reader gets INSTEAD of a number, for each named absence.
 *
 * One wording, read by the strip's tooltip and by the day grid's cell. Two
 * components spelling the same server word apart is a silence on one of them,
 * and the grid is the one where the silence looks like a measurement.
 */
export function rowCountAbsence(reason: string | null | undefined): string | null {
  if (reason === MEASURED_PER_WINDOW) {
    return "rows counted per collection window, not per day";
  }
  if (reason === NOT_VERIFIED) return "no verification counted this Run";
  return null;
}

/** What a strip says instead of a number it does not have. */
export function rowCountNote(
  rowCount: number | null | undefined,
  reason: string | null | undefined,
): string {
  if (rowCount != null) return ` · ${formatNumber(rowCount)} rows`;
  const absence = reason === MEASURED_PER_WINDOW ? rowCountAbsence(reason) : null;
  return absence ? ` · ${absence}` : "";
}

/**
 * Days worth re-asking — and `empty` is one of them since 2026-08-06.
 *
 * JEAN DECIDED IT, and it reverses what this file used to say. The old comment
 * read "`empty` is an answer, not a gap": the FACT is true — the ledger really
 * did record a provider answering nothing — but the prohibition drawn from it
 * was not the fact's to give. A provider can have filled its own hole since, and
 * a person looking at a day they know had traffic is precisely the case where
 * the operator knows what the system does not. His words: *« la personne veut
 * reessayer d avoir sa journee, normalement c est un call avec un JSON
 * retourne »*.
 *
 * `ok` and `running` stay out: one is already collected and verified, the other
 * is in flight, and re-asking either spends quota to learn nothing. The two
 * doors read THIS set — the strip's span and the day grid's row — so a day that
 * can be re-collected from one is re-collectable from the other.
 */
export const REPAIRABLE: ReadonlySet<CoverageStatus> = new Set([
  "never_fetched",
  "failed",
  "partial",
  "empty",
] as const);

/**
 * THE vocabulary of an extract's verdict, and there is only one.
 *
 * Exported since story 58.2 (arbitrage 11): the day-by-day GRID renders the same
 * six words as a pill on a row, and a private table here would have been copied
 * into that screen — one fact under two spellings, one keystroke from drifting.
 * `BAR` below stays private, because it is a strip's palette and not a
 * vocabulary.
 */
export const EXTRACT_STATUS_LABEL: Record<CoverageStatus, string> = {
  ok: "Collected",
  partial: "Partial",
  empty: "Empty — the provider returned nothing",
  failed: "Failed",
  running: "Running",
  never_fetched: "Never requested",
};

/**
 * The same six states as a `Status` tone, for the surfaces that render a pill
 * rather than a mark. Declared beside the labels so a word and its colour cannot
 * be decided in two files.
 *
 * `empty` and `never_fetched` are both quiet — one is an answer, the other an
 * absence — and neither takes a `success` or a `warning`: giving one of them a
 * SEMANTIC colour the other lacks would re-merge them the moment somebody scans
 * by hue and stops reading the word. That fear is right and it is why the tone
 * map does not separate them. `EXTRACT_STATUS_SHAPE` is what does.
 */
export const EXTRACT_STATUS_TONE: Record<CoverageStatus, Fill> = {
  ok: "success",
  partial: "warning",
  empty: "neutral",
  failed: "error",
  running: "info",
  never_fetched: "neutral",
};

/**
 * THE SHAPE THAT SEPARATES THE TWO STATES SHARING ONE TONE — Jean, 2026-08-06.
 *
 * *« Un jour où le fournisseur a répondu sans rien rendre et un jour que
 * personne n'a demandé sont deux faits différents ; l'écran doit le dire sans
 * qu'on survole. »* Both words exist and both are right, but on a pill they
 * rendered IDENTICALLY — same neutral tone, same dotted mark — so only the
 * hover told them apart, and on a strip of sixty marks nobody hovers sixty
 * times.
 *
 * The repair is a shape, not a colour, so the fear recorded above stays honoured:
 * `empty` is FILLED and muted (it is data), `never_fetched` is an open dashed
 * outline (the shape of the day, waiting). It is the same distinction the strip's
 * marks already carry — `BAR` below — carried up into the vocabulary so a THIRD
 * surface rendering these six words inherits it instead of re-deciding it.
 *
 * The other four are empty strings on purpose: their tone already separates
 * them, and adding a container to each would flatten the whole column into pills
 * that all look alike again.
 */
export const EXTRACT_STATUS_SHAPE: Record<CoverageStatus, string> = {
  ok: "",
  partial: "",
  failed: "",
  running: "",
  empty: "rounded-pill bg-surface-muted px-2",
  never_fetched: "rounded-pill border border-dashed border-border-control px-2",
};

/**
 * THE SAME DISTINCTION AS A FACT, for the surfaces that draw a mark rather than
 * a pill — a 6px cell in the fleet list, an 8px dot beside a row.
 *
 * `EXTRACT_STATUS_SHAPE` above is Tailwind, which a component drawing an inline
 * `style` cannot use; without this, those two surfaces had to re-decide the
 * distinction and did — as 60% against 20% of the SAME grey, which is no
 * distinction at all at 6px. One fact, two renderings, and
 * `CoverageBars.test`/`MiniStrip.test` hold them together.
 *
 * `open` means: no fill, a visible edge — the shape of the day is there and
 * nobody has asked for it. Every other state is `filled`, because every other
 * state is an answer.
 */
export type ExtractMark = "filled" | "open";

export const EXTRACT_STATUS_MARK: Record<CoverageStatus, ExtractMark> = {
  ok: "filled",
  partial: "filled",
  empty: "filled",
  failed: "filled",
  running: "filled",
  never_fetched: "open",
};

/** The mark of a status, `filled` for a word this build does not know. */
export function extractStatusMark(status: string | null | undefined): ExtractMark {
  if (!status) return "filled";
  return EXTRACT_STATUS_MARK[status as CoverageStatus] ?? "filled";
}

/** The shape of a status, or nothing for a word this build does not know. */
export function extractStatusShape(status: string | null | undefined): string {
  if (!status) return "";
  return EXTRACT_STATUS_SHAPE[status as CoverageStatus] ?? "";
}

/**
 * A DAY, as much of it as a sentence about that day needs.
 *
 * IT IS ONE ARGUMENT ON PURPOSE, and that is the repair rather than a tidy-up.
 * `extractGapSentence` used to take `(status, connector, date, jobState?)`, and
 * the optional fourth is exactly what the second caller forgot: the row of the
 * grid said « did not allow this collection » over a prevented day while the
 * confirmation that row opens said « was never asked for » about the same day,
 * one click apart. A field that can be dropped silently WILL be. Handing the day
 * over whole makes the wrong sentence unwriteable — every caller already holds
 * one, and the payload's own key names are the ones used here.
 */
export interface GapDay {
  date: string;
  extract_status?: string | null;
  job_state?: string | null;
  /** The connector's own sentence on a prevented window — AI-307. */
  prevented_message?: string | null;
}

/** A window the source refused to run, which is not a window nobody asked for. */
export function isPrevented(day: GapDay): boolean {
  return day.job_state === "prevented";
}

/**
 * WHAT IS MISSING ON THIS DAY, named with the connector and the date.
 *
 * "Meta returned nothing for 2026-06-09" is the sentence story 58.4's `Montre`
 * line asks for, and it is composed HERE rather than in a screen because two
 * doors say it: the day grid's action and the confirmation that action opens.
 * Two spellings of one fact is how a person ends up quoting the wrong one back.
 *
 * `null` for a day nothing is missing from — `ok` and `running` — so a caller
 * cannot draw a gap sentence over a day that has none. The provider's name is
 * the route's `connector`; when the payload carries none the sentence says "The
 * provider", which is the wording `EXTRACT_STATUS_LABEL` already uses, never an
 * invented vendor.
 *
 * `job_state` DECIDES BETWEEN TWO ABSENCES THAT LOOK ALIKE (AI-307). A day whose
 * window is `prevented` reports `never_fetched` to the ledger, which is the
 * right day-grain answer and the wrong sentence: the provider was asked, and it
 * refused. « was never asked for » over such a day sends a person looking for a
 * schedule that never ran, when what is missing is a grant at the provider.
 *
 * AND A REFUSAL CARRIES THE GESTURE, because a message that names none is the
 * defect `CLAUDE.md` forbids on every message. « did not allow this collection »
 * is a state, not something to do; the thing to do is the CONNECTOR'S sentence
 * — « Request the reviews allowlist for this project, then re-ask these dates »
 * — written in the module, carried by the window, published on the day, and
 * rendered verbatim. A prevented day that arrives without one says THAT, and
 * says the only gesture that is true of every refusal, rather than pretending
 * the state was the whole answer.
 */
export function extractGapSentence(
  day: GapDay,
  connector: string | null | undefined,
): string | null {
  const who = connector?.trim() || "The provider";
  if (isPrevented(day)) {
    const gesture = day.prevented_message?.trim();
    return (
      `${who} did not allow this collection for ${day.date} — ` +
      (gesture ||
        "this window recorded no sentence naming what releases it, so the " +
          `access has to be obtained at ${who} before re-asking`)
    );
  }
  switch (day.extract_status) {
    case "empty":
      return `${who} returned nothing for ${day.date}`;
    case "never_fetched":
      return `${who} was never asked for ${day.date}`;
    case "failed":
      return `${who} failed to return ${day.date}`;
    case "partial":
      return `${who} returned only part of ${day.date}`;
    default:
      return null;
  }
}


/**
 * A status this build does not know about, said as such.
 *
 * `CoverageStrip.tsx:62` maps an unrecognised status onto `never_fetched`, which
 * is survivable on a strip of sixty marks and a LIE on a row that carries a date
 * and a word: it would report "never requested" about a day somebody collected.
 * A grid renders the raw word instead — unreadable is honest, wrong is not.
 */
export function extractStatusLabel(status: string | null | undefined): string {
  if (!status) return "No extract state reported";
  return EXTRACT_STATUS_LABEL[status as CoverageStatus] ?? status;
}

export function extractStatusTone(status: string | null | undefined): Fill {
  if (!status) return "neutral";
  return EXTRACT_STATUS_TONE[status as CoverageStatus] ?? "neutral";
}

/** Every value `app.pull_jobs.state` may hold — `core/pull_job_states.py`. */
export type JobState =
  | "queued"
  | "running"
  | "done"
  | "failed"
  | "dead_letter"
  | "superseded"
  | "cancelled"
  | "prevented";

/**
 * THE COLLECTING WINDOW'S OWN STATE, which is a different fact from the
 * extract's verdict and had no vocabulary in this console at all.
 *
 * `cancelled`, `superseded` and `prevented` all report `never_fetched` to the
 * ledger (`pull_job_states.py`), so on the extract verdict alone a window a
 * person STOPPED at 10:02 reads, at 10:03, as a day nobody ever asked for. The
 * eight names are the server's, one for one; the sentences are what a reader
 * needs and the machine name is not.
 *
 * `prevented` (AI-307) is the one that used to be INVISIBLE rather than merely
 * misread: a window the source refused was written `done`, so this console said
 * "Collection finished" over a collection that never happened. The label says
 * what happened; the sentence that says what to do about it travels on the
 * window itself, from the connector that knows which grant is missing.
 */
export const JOB_STATE_LABEL: Record<JobState, string> = {
  queued: "Queued",
  running: "Collecting",
  done: "Collection finished",
  failed: "Collection failed",
  dead_letter: "Collection abandoned after retries",
  superseded: "Replaced by a newer window",
  cancelled: "Stopped before it started",
  // The em dash is this vocabulary's, not a taste: `EXTRACT_STATUS_LABEL.empty`
  // reads « Empty — the provider returned nothing », the two maps are rendered
  // in the same column of the same grid, and one word punctuated differently is
  // the drift these tables exist to prevent.
  prevented: "Prevented — the source did not allow it",
};

/**
 * WHAT A PERSON READS FOR A WINDOW STATE THIS BUILD HAS NO WORD FOR.
 *
 * Criterion 18 of `execution-substrate.md` -- « A payload publishes a raw
 * `app.pull_jobs.state` value as a status a person reads, i.e. any mapping that
 * ends by returning the column » -- and this map ENDED BY RETURNING IT. The
 * server closed the same hole at `dq_api._map_state_to_status` in the same
 * commit; the console kept it, so the next name added to the CHECK constraint
 * would have arrived in the `Collection window` cell as a database word.
 *
 * It is not `extractStatusLabel`'s bargain, and the difference is which column
 * is being spelled. An unknown EXTRACT verdict is shown as it arrived because
 * the grid's filter chips key on that raw word and a day nobody can name must
 * still be findable. Nothing keys on the window state: it is rendered in one
 * cell of one grid, so an unreadable word buys nothing and costs the reader a
 * sentence.
 *
 * The sentence names what to do -- reload, because a state this console has no
 * word for is a state its build predates -- and it does not name a run, because
 * a day may carry none.
 */
export const JOB_STATE_UNKNOWN =
  "Collection state this console has no word for — reload this page, and open " +
  "this day's run if it stays";

/** The window state said in words, never the database's own. */
export function jobStateLabel(state: string | null | undefined): string | null {
  if (!state) return null;
  return JOB_STATE_LABEL[state as JobState] ?? JOB_STATE_UNKNOWN;
}

/**
 * WHAT A DAY SAYS, AS ONE KEY -- read from the DAY, never from one field of it.
 *
 * `extractGapLabel` already refused to count a refusal as an absence; two other
 * readers of the same grid still counted and spelled `extract_status` alone, and
 * the screen said both things at once. Measured 2026-08-21 on
 * `WorkbenchDataPage`, 2 prevented days + 1 never-requested day: the filter band
 * read « Never requested · 3 » while the confirmation of the same page read
 * « 2 Prevented -- the source did not allow it; 1 Never requested », and the
 * prevented ROW carried a pill reading « Never requested » beside a button named
 * « ... did not allow this collection for 2026-07-12 ». One key, composed here,
 * is what makes those three readings the same reading: a surface that groups,
 * filters, colours or spells a day asks for the key and gets one answer.
 *
 * The key is NOT a status: `job_state:` prefixes it so it can never collide with
 * a value of `app.datastream_extracts.status`, present or future.
 */
export const PREVENTED_GAP_KEY = "job_state:prevented";

export function extractGapKey(day: GapDay): string {
  if (isPrevented(day)) return PREVENTED_GAP_KEY;
  return String(day.extract_status ?? "");
}

/** The word for a gap key -- the extract vocabulary, plus the refusal. */
export function gapKeyLabel(key: string): string {
  if (key === PREVENTED_GAP_KEY) return JOB_STATE_LABEL.prevented;
  return extractStatusLabel(key);
}

/**
 * The order these keys are read in, so two surfaces cannot sort them apart.
 *
 * The extract vocabulary's own order, then the refusal: `prevented` is the state
 * a reader acts on last, after every day whose verdict is its own.
 */
export const GAP_KEY_ORDER: readonly string[] = [
  ...Object.keys(EXTRACT_STATUS_LABEL),
  PREVENTED_GAP_KEY,
];

/**
 * The tone and the shape of a DAY, for the surfaces that draw a pill.
 *
 * A refusal takes `warning` where `never_fetched` is neutral -- and that does
 * not reopen the fear recorded on `EXTRACT_STATUS_TONE`. That fear is about two
 * states a reader must not merge by scanning hue; this is the opposite gesture,
 * separating two words that arrive as ONE ledger status. The shape stays the
 * dashed open outline: nothing landed on this day either.
 */
export function extractGapTone(day: GapDay): Fill {
  if (isPrevented(day)) return "warning";
  return extractStatusTone(day.extract_status);
}

export function extractGapShape(day: GapDay): string {
  if (isPrevented(day)) return EXTRACT_STATUS_SHAPE.never_fetched;
  return extractStatusShape(day.extract_status);
}

/**
 * THE WORD FOR WHAT IS MISSING, when days are COUNTED rather than named.
 *
 * The same distinction `extractGapSentence` draws, read from the same day, so a
 * bulk confirmation cannot say « 4 Never requested » about four windows the
 * source refused. Counting on `extract_status` alone is precisely how it did:
 * the ledger answers `never_fetched` for all three of `cancelled`, `superseded`
 * and `prevented`, and only the window's own state tells them apart.
 */
export function extractGapLabel(day: GapDay): string {
  return gapKeyLabel(extractGapKey(day));
}

/**
 * Filled for an answer, empty for an absence.
 *
 * ONE map, and the legend reads from it too. It briefly had a second map so
 * the legend could use the solid tones while the strip stayed pale — which
 * meant the key was a saturated green beside bars of pale green. Jean,
 * 2026-07-29: "not the same color que la légende". A legend exists to say
 * *this colour means that*; if its colour is not the one on the strip, it has
 * failed at the only thing it does. The swatch is a miniature of a mark now —
 * same fill, same rounding — so the two cannot drift.
 *
 * The marks carry the `*-surface` step, and finding it took three passes —
 * worth recording, because the obvious lever was the wrong one:
 *
 *   `*-container` pastels        washed out
 *   a surface step at 1.55:1     still washed out ("blanchâtre")
 *   the solid tones              too vivid
 *   a surface step at 1.9:1      this one
 *
 * The constraint is not lightness, it is CHROMA. Mixing a pastel toward white
 * does not only lighten it, it desaturates it — the 1.55 step had fallen to
 * .11 chroma on the lavender — and a coverage strip is read by HUE, not by
 * weight. The step here holds .26 to .60 while sitting well below full
 * strength, and every tone lands at the same 1.9:1 so none shouts louder than
 * another.
 *
 * `never_fetched` keeps no fill at all — a dashed outline with nothing in it.
 * The shape of the day is there, waiting, but no one has asked for it.
 */
const surface = (tone: Tone) => `${TONE_SURFACE[tone]} ${TONE_SURFACE_HOVER[tone]}`;

const BAR: Record<CoverageStatus, string> = {
  ok: surface("success"),
  partial: surface("warning"),
  // `empty` IS the neutral surface: a day that was collected and had nothing in
  // it. The muted step this map used to name by hand is what `TONE_SURFACE`
  // carries for `neutral`, so the two cannot drift apart any more.
  empty: surface("neutral"),
  failed: surface("error"),
  running: surface("info"),
  never_fetched:
    "border border-dashed border-border-control/60 bg-transparent hover:bg-surface-subtle",
};

/**
 * THE MARK OF A REFUSAL, which is not the mark of a day nobody asked for.
 *
 * `/ledger` answers `never_fetched` for a prevented window by design, so on the
 * verdict alone this strip drew an empty dashed mark and its legend counted the
 * refusal under « Never requested ». The mark keeps the dashed outline -- nothing
 * landed on that day either -- and takes the warning ink the refusal owns
 * everywhere else in this file, so the two are told apart without a hover.
 */
const PREVENTED_BAR = `border border-dashed ${TONE_BORDER.warning} ${surface("warning")}`;

function gapBar(key: string): string {
  if (key === PREVENTED_GAP_KEY) return PREVENTED_BAR;
  return BAR[key as CoverageStatus] ?? BAR.never_fetched;
}

/** The day as the gap vocabulary reads it -- the strip's own field names differ. */
function asGapDay(day: CoverageDay): GapDay {
  return { date: day.date, extract_status: day.status, job_state: day.jobState ?? null };
}

const ORDER: readonly string[] = GAP_KEY_ORDER;

export function CoverageBars({
  days,
  onRepair,
  className,
}: {
  days: CoverageDay[];
  /** Only the repairable days are passed — never `ok`, never `running`. */
  onRepair?: (dates: string[]) => void;
  className?: string;
}) {
  const [anchor, setAnchor] = useState<number | null>(null);
  const [end, setEnd] = useState<number | null>(null);

  const [from, to] =
    anchor === null
      ? [null, null]
      : end === null
        ? [anchor, anchor]
        : [Math.min(anchor, end), Math.max(anchor, end)];

  const selection = useMemo(() => {
    if (from === null || to === null) return { dates: [] as string[], repairable: [] as string[] };
    const picked = days.slice(from, to + 1);
    return {
      dates: picked.map((d) => d.date),
      repairable: picked.filter((d) => REPAIRABLE.has(d.status)).map((d) => d.date),
    };
  }, [days, from, to]);

  const counts = useMemo(() => {
    // COUNTED ON THE DAY, not on its verdict: two prevented days and one nobody
    // asked for are « 2 Prevented; 1 Never requested », never « 3 Never
    // requested ». Same key as the grid's chips and the confirmation's count.
    const out = new Map<string, number>();
    for (const day of days) {
      const key = extractGapKey(asGapDay(day));
      out.set(key, (out.get(key) ?? 0) + 1);
    }
    return out;
  }, [days]);

  function pick(index: number) {
    if (anchor === null || end !== null) {
      setAnchor(index);
      setEnd(null);
    } else {
      setEnd(index);
    }
  }

  return (
    <div className={cn("flex flex-col gap-3", className)}>
      {/* Separate marks, one per day (Jean's reference, 2026-07-29) — a day is
          a unit of collection and you have to be able to land on one.

          It is a GRID, and the selection bracket is a grid item spanning
          `from`..`to` in that same grid. Not a preference: `flex-1` marks are
          not all the same width once gaps and sub-pixel rounding are in play,
          so every attempt to place the bracket by arithmetic drifted — 31px
          off at day 48 of 61, twice, with two different formulas. Letting the
          grid place it is exact by construction and deletes the arithmetic.

          NO TRAY. It had one, the page tint with 8px of padding, and on a
          white panel the two are so close that it never read as a container —
          only as a pale band above and below the marks. Jean: "still look
          white on top of it". Scanning the pixels settled where it came from:
          a mark is one flat colour from its top edge to its bottom, so the
          white was never on the mark, it was the gutter. The marks carry their
          own shape and the dashed ones already show the full extent of the
          window, so the container was doing no work. */}
      <div>
        <div
          role="group"
          aria-label="Daily coverage"
          className="grid h-10 items-stretch gap-[3px]"
          style={{ gridTemplateColumns: `repeat(${days.length}, minmax(0, 1fr))` }}
        >
          {days.map((day, i) => {
            const inSelection = from !== null && to !== null && i >= from && i <= to;
            const key = extractGapKey(asGapDay(day));
            const word = gapKeyLabel(key);
            return (
              <button
                key={day.date}
                type="button"
                onClick={() => pick(i)}
                aria-pressed={inSelection}
                style={{ gridRow: 1, gridColumn: i + 1 }}
                title={`${day.date} — ${word}${rowCountNote(
                  day.rowCount,
                  day.rowCountReason,
                )}`}
                className={cn(
                  "min-w-0 rounded-control outline-none transition-colors",
                  "focus-visible:relative focus-visible:z-20 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus",
                  gapBar(key),
                )}
              >
                <span className="sr-only">
                  {day.date} — {word}
                </span>
              </button>
            );
          })}
          {/* ONE bracket around the span, not a ring per day: a selection is a
              single statement about an interval, in solid ink so it holds its
              own beside solid marks (Jean, 2026-07-29 — "use solid tone, and
              make the border of the selection also"). It was charcoal for one
              round, back when the marks were pastel and a black frame read as
              a modal; against full-strength tones charcoal disappears.

              And nothing dims outside it. Fading sixty marks so that nine
              stand out is what made the strip look washed the moment anything
              was selected; the frame is enough on its own. */}
          {from !== null && to !== null && (
            <span
              aria-hidden
              className="pointer-events-none z-10 -my-1.5 -mx-1 rounded-large border-2 border-text"
              style={{ gridRow: 1, gridColumn: `${from + 1} / span ${to - from + 1}` }}
            />
          )}
        </div>
      </div>

      <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-2">
        <ul className="m-0 flex list-none flex-wrap gap-x-5 gap-y-2 p-0">
          {ORDER.filter((key) => counts.get(key)).map((key) => (
            <li key={key} className="flex items-center gap-2">
              <span aria-hidden className={cn("h-3.5 w-2.5 shrink-0 rounded-[3px]", gapBar(key))} />
              <span className="text-caption text-text-secondary">
                {gapKeyLabel(key)}
                <span className="ml-1.5 font-numeric [font-variant-numeric:lining-nums_tabular-nums]">
                  {counts.get(key)}
                </span>
              </span>
            </li>
          ))}
        </ul>
        {days.length > 0 && (
          <span className="font-numeric text-caption text-text-secondary [font-variant-numeric:lining-nums_tabular-nums]">
            {days[0].date} → {days[days.length - 1].date}
          </span>
        )}
      </div>

      {selection.dates.length > 0 && (
        <div className="flex flex-wrap items-center justify-between gap-3 border-t border-divider-base pt-3.5">
          <Status tone={selection.repairable.length ? "accent" : "neutral"}>
            {selection.dates.length === 1
              ? selection.dates[0]
              : `${selection.dates[0]} → ${selection.dates[selection.dates.length - 1]}`}
            {" · "}
            {/* The honest count: 30 days with 4 gaps re-collects 4. */}
            {selection.repairable.length} of {selection.dates.length} can be re-collected
          </Status>
          <div className="flex items-center gap-2.5">
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                setAnchor(null);
                setEnd(null);
              }}
            >
              Clear
            </Button>
            <Button
              size="sm"
              disabled={!selection.repairable.length}
              onClick={() => onRepair?.(selection.repairable)}
            >
              Re-collect {selection.repairable.length || ""}
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
