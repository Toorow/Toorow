/**
 * WHAT A RUN MEASURED, AND THE WORDS FOR WHAT IT DID NOT.
 *
 * Extracted from `WorkbenchRunsPage.tsx` on 2026-08-12, for the amendment the
 * visual review (issue #69, findings D-1, D-7, D-9) forced on this tab. Nothing
 * about the absences themselves is withdrawn: measured on the disposable base
 * 2026-08-07 over 428 executions, a window landed on 0, a duration on 103, a
 * collection counter on 6 and an origin on 100. THE ABSENCE IS WHAT THIS TAB
 * MOSTLY RENDERS, so it keeps a sentence rather than a `0`, a dash or a silent
 * fallback to some other measurement.
 *
 * WHAT CHANGED IS WHERE THE REASON LIVES, and it changed because the reason was
 * being paid for sixty times. Two run blocks at 1600px carried SEVEN explanatory
 * paragraphs each, naming four table columns, two function names, three
 * migration numbers and one story id. On the sixty runs this tab is bounded to,
 * that is four hundred paragraphs of the repository talking to itself, on the
 * screen somebody opened to ask which run failed.
 *
 * So this file holds THREE registers of language, and they are never mixed:
 *
 *   1. THE FACT — `Window not recorded`, `Not measured`, `Not started`. It is on
 *      the run's own row, always, with no click;
 *   2. THE REASON, IN THE PERSON'S WORDS — one sentence per absence, inside the
 *      run that carries it, naming what would have written the measurement and,
 *      where one exists, the gesture that fills it. No column name, no function
 *      name, no migration number, no story id — `CLAUDE.md` requires a message
 *      to name the gesture that repairs, never the technical cause;
 *   3. THE TECHNICAL ACCOUNT — the writers, by name. It is the only honest answer
 *      to "what writes this, exactly", and it belongs to somebody repairing the
 *      platform rather than somebody reading a history. It is therefore folded,
 *      and written ONCE FOR THE TAB rather than once per run.
 */
import { formatNumber } from "../../../../ui/format";
import { dateTime, record, text } from "../../evidence";
import { NOT_MEASURED, hasNotStarted } from "../../DatastreamRunLive";
import { originKey, originLabel } from "../../runOrigins";

// ---------------------------------------------------------------------------
// 1. The facts.
// ---------------------------------------------------------------------------

export const WINDOW_NOT_RECORDED = "Window not recorded";
/** The run has not begun. It did not fail to measure anything — `hasNotStarted`. */
export const NOT_STARTED = "Not started";
/** It began, and nobody wrote down when. A third reading, never one of the two above. */
export const START_NOT_RECORDED = "Start not recorded";

// ---------------------------------------------------------------------------
// 2. The reason, in the person's words. One sentence, inside the run it is about.
// ---------------------------------------------------------------------------

export const WINDOW_ABSENCE =
  "This run did not write down the exact range of days it asked for. Only a re-collection of " +
  "a range you choose records one, and nothing kept that range afterwards — re-collect a " +
  "range above to get a run that carries its own.";
export const ROWS_ABSENCE =
  "This run never counted the rows it landed. Runs opened before the console started counting " +
  "carry no count, and the figure a publication reports counts a different thing, so it is not " +
  "shown here in its place.";
export const DURATION_ABSENCE =
  "A length needs the instant this run opened and the instant it finished. This run recorded " +
  "no opening instant, so no length can be given for it.";
export const ORIGIN_ABSENCE =
  "Nothing recorded what started this run. Runs opened since the console began stamping their " +
  "origin say whether they came from the schedule, from a mapping change or from a repair.";
export const NO_STEP_SPAN =
  "No step of this run was timed. A step is timed from the moment the run enters it, and a run " +
  "that finished before the console timed its steps carries none.";
export const NO_PHASE_EVIDENCE =
  "This run recorded no phase evidence. Phases are written when a Datastream is activated, and " +
  "they are a different reading from the four steps above — not the same one missing.";
export const NOT_STARTED_SENTENCE =
  "This run has not started, so nothing has been measured yet. Its window, its rows and its " +
  "length appear as soon as it opens.";

// ---------------------------------------------------------------------------
// 3. The technical account — folded, and written ONCE for the tab.
// ---------------------------------------------------------------------------

/**
 * The summary standing above the fold, and it has to be TRUE ON ITS OWN.
 *
 * Somebody who never opens the disclosure must still leave with the right idea:
 * these are not failed readings, they are readings nobody took, and nothing is
 * back-filled later from a neighbouring number.
 */
export const WHY_ABSENT_SUMMARY =
  "A run carries only what was being recorded on the day it opened, and nothing here is filled " +
  "in afterwards from a different measurement. That is why an older run says a counter is " +
  "missing instead of showing a number that would not be its own.";
export const WHY_ABSENT_TRIGGER = "What writes each of these measurements";

/** One entry per measurement: what a person calls it, and what actually writes it. */
export const WHY_ABSENT_DETAIL: ReadonlyArray<{ term: string; account: string }> = [
  {
    term: "What it covered",
    account:
      "An exact interval is written onto the run's plan by a bounded recovery. A recurring " +
      "collection carries none, and app.pull_jobs.execution_id is null on every job measured, " +
      "so the queue cannot answer it either.",
  },
  {
    term: "Rows collected",
    account:
      "rows_written is written at each window boundary by record_window_progress; an execution " +
      "older than migration 218 never had one, and row_count is a different measurement of a " +
      "different thing.",
  },
  {
    term: "Duration",
    account:
      "A duration needs started_at and state_changed_at. started_at is written by " +
      "open_collection_run (migration 218); a run minted before it carries none.",
  },
  {
    term: "Origin",
    account:
      "The origin is stamped onto the projection plan when the run is opened. Runs from " +
      "before that was recorded do not carry one, and are shown without it rather than guessed.",
  },
  {
    term: "Step timings",
    account:
      "A span is written when the run ENTERS a step — open_collection_run for Collect, " +
      "record_window_progress for a move (migration 223); a run that ended before that writer " +
      "landed carries none.",
  },
];

// ---------------------------------------------------------------------------
// The readings themselves.
// ---------------------------------------------------------------------------

export const STEP_NOT_REACHED = "Not reached";
export const STEP_STILL_RUNNING = "Still running";
/**
 * The step was entered, and nothing about its length was measured.
 *
 * TWO different facts land here, and neither is a duration:
 *
 *   1. a span with NO END on a run that has ENDED. Only two writers close a
 *      span — `advance_state` and `commit_publication` — and three others reach
 *      a terminal state with their own `UPDATE` (`datastream_activation`,
 *      `reconcile_execution`, `_reconcile_fail_closed`). There is no seam they
 *      all cross, and wiring a close into each is a call the next writer
 *      forgets, so the guarantee lives at the READ, where nothing can bypass it:
 *      the authority for "is this step still working" is the RUN'S STATE, read
 *      through `isRunMoving` — the registry mirrored from
 *      `server/core/execution_states.py` and compared to it entry by entry;
 *   2. an elapsed of exactly `0`, which is what two ends written inside one
 *      `transaction_timestamp()` produce.
 *
 * THE ZERO CHECK IS NOT THE GUARD FOR (1), and a reader must not mistake it for
 * one. Since the writer moved to `clock_timestamp()`, a span opened and closed
 * inside one transaction lands ~3 ms apart, not at `0` — so the screen would
 * print `under 1 s` for that regression and this constant would never appear.
 * What catches it is the integration assertion that a real collection measures
 * at least 50 ms (`test_a_finished_collection_measures_a_time_that_actually_
 * elapsed`), never the screen.
 */
export const STEP_NOT_TIMED = "Entered, not timed";

/**
 * A measured span, in the words a person reads.
 *
 * ZERO IS NOT A DURATION, and this is the line that decides it. An elapsed of
 * exactly `0` means the two ends carry the same instant, which is what a span
 * written twice inside ONE transaction produces — it timed nothing, and
 * printing `under 1 s` for it would be the fabricated zero of this story
 * wearing a friendlier word. A genuinely sub-second step has a real elapsed
 * time (migration 223 keeps `ended_at >= started_at` precisely so `Check`,
 * measured in milliseconds, is a valid row) and reads `under 1 s`.
 */
export function durationText(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return STEP_NOT_TIMED;
  if (seconds < 1) return "under 1 s";
  if (seconds < 60) return `${Math.round(seconds)} s`;
  if (seconds < 3600) {
    const minutes = Math.floor(seconds / 60);
    const rest = Math.round(seconds - minutes * 60);
    return rest ? `${minutes} min ${rest} s` : `${minutes} min`;
  }
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.round((seconds - hours * 3600) / 60);
  return minutes ? `${hours} h ${minutes} min` : `${hours} h`;
}

/**
 * WHEN THIS RUN STARTED — three readings, and never two for one.
 *
 * `Started Unavailable` is what this row said before 2026-08-12: the header read
 * `dateTime(run.created_at)`, and `dateTime` answers `Unavailable` for anything
 * it cannot parse. A run that has not begun was therefore rendered as a
 * measurement that FAILED, which is the exact defect the live band was repaired
 * for on the same day.
 *
 * The authority is `hasNotStarted`, exported from `DatastreamRunLive.tsx` and
 * read here rather than re-derived: a third rule for "has this run begun" is how
 * two regions of one screen come to disagree about one run.
 */
export function startedText(run: Record<string, unknown>): string {
  if (hasNotStarted(run.state, run.started_at)) return NOT_STARTED;
  // `started_at` is written by `open_collection_run`; a run older than that
  // writer has none and its creation instant is the honest second best — it is
  // the instant the row entered the history, and the list is ordered by it.
  const at = typeof run.started_at === "string" ? run.started_at : run.created_at;
  const said = dateTime(at);
  return said === "Unavailable" ? START_NOT_RECORDED : said;
}

/** The window this run covered, or the word that says nothing wrote one. */
export function windowText(run: Record<string, unknown>): string {
  const interval = record(record(run.recovery)?.interval);
  const from = text(interval?.from, "");
  const toExclusive = text(interval?.to_exclusive, "");
  if (from && toExclusive) return `${from.slice(0, 10)} → ${toExclusive.slice(0, 10)}`;
  return WINDOW_NOT_RECORDED;
}

/** The collection counter, or the word for the counter nobody wrote. */
export function rowsText(run: Record<string, unknown>): string {
  return typeof run.rows_written === "number"
    ? formatNumber(run.rows_written)
    : NOT_MEASURED;
}

/**
 * How long the whole run took.
 *
 * Same rule as a step span: an elapsed of exactly `0` is two ends carrying one
 * instant, which measured nothing. It reads as the absence it is.
 */
export function runDurationText(run: Record<string, unknown>): string {
  const raw = typeof run.duration_seconds === "number" ? run.duration_seconds : null;
  return raw !== null && raw > 0 ? durationText(raw) : NOT_MEASURED;
}

/**
 * WHY THIS RUN EXISTS, in the registry's own word — story 63.7.
 *
 * An origin this build does not know is shown EXACTLY as it arrived rather than
 * folded into a plausible label: a nightly collection and a mapping change are
 * two different things, and inventing a third word for a key nobody registered
 * would make them look like one.
 */
export function originText(run: Record<string, unknown>): string | null {
  return originLabel(run.origin) ?? originKey(run.origin);
}

export type Absence = { label: string; said: string; why: string };

/**
 * WHAT THIS RUN DID NOT MEASURE — the list the unfolded run carries.
 *
 * It holds only the absences THIS run has, so a complete run shows no such
 * section at all and a person never reads a paragraph about a counter that is
 * sitting right there. A run that has not started contributes none of the three
 * measurement absences: it has not failed to measure them, it has not reached
 * them, and three « Not measured » lines would read as three failed readings.
 */
export function absencesOf(run: Record<string, unknown>): Absence[] {
  const absences: Absence[] = [];
  if (!hasNotStarted(run.state, run.started_at)) {
    if (windowText(run) === WINDOW_NOT_RECORDED) {
      absences.push({ label: "What it covered", said: WINDOW_NOT_RECORDED, why: WINDOW_ABSENCE });
    }
    if (typeof run.rows_written !== "number") {
      absences.push({ label: "Rows collected", said: NOT_MEASURED, why: ROWS_ABSENCE });
    }
    if (!(typeof run.duration_seconds === "number" && run.duration_seconds > 0)) {
      absences.push({ label: "Duration", said: NOT_MEASURED, why: DURATION_ABSENCE });
    }
  }
  // The origin is NOT gated by the start: it is stamped when the run is opened,
  // so a run still waiting in `created` can perfectly well carry one, and a run
  // that ran for an hour before the stamp existed carries none.
  if (!originText(run)) {
    absences.push({ label: "Origin", said: NOT_MEASURED, why: ORIGIN_ABSENCE });
  }
  return absences;
}
