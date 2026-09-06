/**
 * The live state of one collection -- ONE file, mounted by every surface that
 * shows it -- story 63.5.
 *
 * WHY IT TAKES THE POLL AS A PROP AND NEVER CALLS THE HOOK. The Workbench
 * object header and the `Runs` tab render from the same component and are
 * therefore on screen together; the fleet list is a different route entirely.
 * A component that opened its own poll would be fine on one surface and wrong
 * on two, so the poll is handed in: `lib/polledRead.ts` keeps one reading per
 * address, and this file renders whatever that reading says. It also makes
 * every case below testable without a clock.
 *
 * WHY IT COMPUTES NOTHING. The duration is the server's (story 63.4, computed
 * inside the payload), the state's meaning is the registry's
 * (`executionStates.ts`, mirrored from `server/core/execution_states.py`), and
 * the day count is the run's own; WHY it is running is a second registry's
 * (`runOrigins.ts`, mirrored from `server/core/run_origins.py`). What this file
 * owns is the ORDER the seven blocks are read in and the words for the silences
 * -- nothing that could disagree with the MCP reading the same payload.
 *
 * WHAT IT NEVER DOES:
 *   - print a `0` where nothing was measured. Every counter on this payload is
 *     `null` until the run writes it, and `null` reads as *Not measured*;
 *   - show a figure without the instant it was read. A number left on screen
 *     after a failed reload, with no age on it, is a lie a reader cannot
 *     detect -- the defect `CacheHealthCard` shipped until story 63.3;
 *   - fold `Cancelled` into `Failed`, or answer one silence for three. A run
 *     that never happened, one that finished well and one that ended badly are
 *     three different sentences;
 *   - type a run-state name. The registry decides what a state is called and
 *     which phase it paints; a literal here would be a second list to keep in
 *     step, and the six that existed before it broke four surfaces at once.
 */
import { type ReactNode, useState } from "react";
import { Button, formatClock, formatNumber, Panel, percentValue, Progress, stateLabel, Status, Retry } from "../../ui";
import type { Tone } from "../../ui";
import { ConfirmDialog } from "../../ui/ConfirmDialog";
import { dateTime } from "./evidence";
import type {
  DatastreamIdle,
  DatastreamProgress,
  DatastreamProgressPoll,
  ProgressEstimate,
} from "./datastreamProgress";
import { isRunProgressing, stopRun, windowsNotStarted } from "./datastreamProgress";
import { executionStateLabel, phaseOf, type ExecutionPhase } from "./executionStates";
import { isKnownOrigin, originSentence, readsProviderWindows } from "./runOrigins";

/**
 * The one word for a counter the run has not written.
 *
 * Shared with the `Runs` table so the same absence is never called two things:
 * `rows_written` is `NULL` on every execution older than migration 218, and a
 * cell that quietly fell back to `row_count` would put two different numbers
 * under one heading.
 */
export const NOT_MEASURED = "Not measured";

/**
 * HAS THIS RUN BEGUN? — amendment 8 of the 2026-08-11 review, with the correction
 * the first version needed.
 *
 * TWO CONDITIONS, and the second is why this is not `started_at === null`. That
 * was the whole test for half a day, and it was wrong about the runs this tab
 * mostly holds: `started_at` is written by `open_collection_run` (migration 218),
 * so a run that ran, collected and PUBLISHED before that migration carries none —
 * measured on the disposable base, 103 of 428 executions. Hiding their tiles
 * would hide real `row_count` and real windows behind « has not started », which
 * is a worse lie than the empty tiles it set out to remove.
 *
 * So the state decides, and the registry owns the state: `created` is the only
 * stored state whose phase is `todo` (`executionStates.ts:35`). A run in `todo`
 * with no start instant has not begun. Anything else has, and keeps all its
 * tiles and all its absence sentences — « a counter that was never written says
 * so » holds wherever there was a counter to write.
 */
export function hasNotStarted(state: unknown, startedAt: unknown): boolean {
  return !startedAt && phaseOf(state) === "todo";
}

/**
 * A phase, in the tone scale -- one mapping, used wherever a run is shown.
 *
 * A run in flight is `info`, not `warning`: amber is what a screen uses to say
 * something needs attention, and a nightly collection doing exactly what it was
 * asked to do needs none. It was painted amber on the `Runs` tab, so every
 * Datastream looked mildly wrong for the length of its own pull.
 */
const TONE_BY_PHASE: Record<ExecutionPhase, Tone> = {
  todo: "neutral",
  running: "info",
  done: "success",
  failed: "error",
};

export function toneForPhase(phase: ExecutionPhase): Tone {
  // A `Record` keyed by the registry's own type rather than a chain of string
  // comparisons: the compiler refuses a phase that is missing and a phase that
  // does not exist, and no run-state name is ever quoted in this file.
  return TONE_BY_PHASE[phase] ?? TONE_BY_PHASE.todo;
}

/**
 * What a run state is called, in one badge.
 *
 * The fleet list carries this and nothing else: `latest_candidate_state` is
 * already on the wire for every row (`data_surface.py`), so a list of forty
 * Datastreams says which ones are collecting for **zero** new requests. One
 * poll per row would be 40 x 17_280 wake-ups a night on a service that scales
 * to zero, to answer a question the payload already answered.
 */
export function DatastreamRunStateBadge({ state }: { state: unknown }) {
  // THE SPINNER IS A CLAIM THAT SOMETHING IS HAPPENING, so it reads the phase and
  // not the lock. `created` holds the lock and has begun nothing; animating it
  // told a person their collection was working when no worker had opened it.
  const progressing = isRunProgressing(state);
  return (
    <Status tone={toneForPhase(phaseOf(state))} active={progressing}>
      {executionStateLabel(state)}
    </Status>
  );
}

/** How old a reading is, in the words a person reads. */
function measurementAge(measuredAt: number): string {
  const seconds = Math.max(0, Math.round((Date.now() - measuredAt) / 1000));
  const clock = formatClock(measuredAt, { seconds: true });
  return `${seconds} s ago, at ${clock}`;
}

/** One fact of the run, with its name. Absent facts say so; they never read 0. */
function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <div className="text-caption text-text-secondary">{label}</div>
      <div className="mt-1 text-ui text-text">{children}</div>
    </div>
  );
}

/**
 * The words that stand where the bar would be, on a run that has none.
 *
 * Story 63.7, and it is derived rather than guessed: the registry says whether
 * a run of this origin pulls provider windows at all. A mapping change binds ONE
 * activation job and no `app.pull_jobs` row, so the three LATERALs of the
 * progress route return nothing at once -- `windows_total` is `null` because the
 * run declared no window, not because a measurement failed. A bar at 0 % there
 * would be a fraction nobody computed, and "this run has not said how many days
 * it covers YET" would promise a number that is never coming.
 */
const NO_PROVIDER_WINDOW = "This update reads no provider window.";
const NOT_SAID_YET = "This run has not said how many days it covers yet.";

/** Blocks 3 and 4: how far it has got, and what it has landed. */
function RunMeasurements({ progress }: { progress: DatastreamProgress }) {
  const { days_done: done, days_total: total, window_in_progress: window } = progress;
  const counted = typeof done === "number" && typeof total === "number" && total > 0;
  // A NUMBER for `Progress` and for `aria-valuenow`, so `percentValue` and not
  // `formatPercent` — the same rounding decision, in the shape this one takes.
  const percent = counted ? Math.min(100, percentValue(done / total)) : 0;
  // Known origin that reads no provider window -> the reason. An origin this
  // build does not know says nothing about windows either way: silence, not a
  // reason it invented.
  const noWindowEver = isKnownOrigin(progress.origin) && !readsProviderWindows(progress.origin);
  return (
    <div className="grid gap-3">
      {counted ? (
        <div className="grid gap-2">
          <Progress
            value={percent}
            size="thin"
            tone="info"
            aria-label="Days collected"
            data-testid="run-live-days"
          />
          <p className="m-0 text-ui text-text">
            {formatNumber(done)} of {formatNumber(total)} days collected
          </p>
        </div>
      ) : (
        <p className="m-0 text-ui text-text-secondary" data-testid="run-live-no-fraction">
          {noWindowEver ? NO_PROVIDER_WINDOW : NOT_SAID_YET}
        </p>
      )}
      {/* The window is what makes a plateau legible: `days_done` moves only when
          a window lands, so a counter that has not moved in twenty minutes and a
          run that is stuck are the same picture without it. It is skipped
          entirely on a run that will never have one -- "no window is in flight"
          reads as a passing state, and for these there is nothing to wait for. */}
      {!noWindowEver && (
      <p className="m-0 text-caption text-text-secondary">
        {window === null
          ? "No window is in flight."
          : typeof window.days === "number"
            ? `A ${formatNumber(window.days)}-day window is in flight` +
              (window.date_from && window.date_to ? ` (${window.date_from} to ${window.date_to}).` : ".")
            : "A window is in flight, and its size was not measured."}
      </p>
      )}
      <div className="grid grid-cols-2 gap-4 max-lg:grid-cols-1">
        <Fact label="Rows collected">
          {typeof progress.rows_written === "number"
            ? formatNumber(progress.rows_written)
            : NOT_MEASURED}
        </Fact>
        <Fact label="Step">
          {progress.step ?? "This run has not named a step yet."}
        </Fact>
      </div>
    </div>
  );
}

/**
 * Block 5 -- how much longer, in the server's own words.
 *
 * The sentence is always present, armed or not: the four ways this estimate
 * declines each have their own sentence, and an absent line is not a
 * measurement. `observations` / `minimum_observations` travel with it, as the
 * anomaly detector discloses its own bound.
 */
function TimeLeft({ estimate }: { estimate: ProgressEstimate | null }) {
  if (!estimate) {
    return (
      <Fact label="Time left">
        This server sends no estimate for a run in flight.
      </Fact>
    );
  }
  const sample =
    typeof estimate.observations === "number" && typeof estimate.minimum_observations === "number"
      ? `Measured on ${formatNumber(estimate.observations)} finished run(s); ` +
        `${formatNumber(estimate.minimum_observations)} are needed.`
      : null;
  return (
    <Fact label="Time left">
      <span data-testid="run-live-estimate">{estimate.sentence}</span>
      {sample && <span className="mt-1 block text-caption text-text-secondary">{sample}</span>}
    </Fact>
  );
}

/** A count the run measured, or the one word for a count it did not. */
function counted(value: number | null): string {
  return typeof value === "number" ? formatNumber(value) : NOT_MEASURED;
}

/**
 * What a stopped run KEPT -- story 63.6.
 *
 * The whole promise of stopping is that the days already collected stay: the raw
 * zone is append-only, so nothing is undone and nothing is half published. That
 * promise is worth nothing unless the screen says what was kept, and after the
 * stop the `idle` payload is the only place those numbers still exist -- the run
 * is terminal, so `progress` is `null`.
 *
 * Every number comes from the payload. `Not measured` when it is `null`, never
 * a `0`: a run that finished no window collected no days, and "0 days were
 * collected" is a claim it never made.
 */
function StoppedKept({ idle }: { idle: DatastreamIdle }) {
  const { days_done: done, days_total: total, rows_written: rows } = idle;
  const scope =
    typeof total === "number"
      ? `${counted(done)} of ${counted(total)} days were collected and kept`
      : `${counted(done)} days were collected and kept`;
  return (
    <p className="m-0 text-ui text-text-secondary" data-testid="run-live-kept">
      Stopped. {scope}, and {counted(rows)} rows landed.
    </p>
  );
}

/**
 * Block 0 -- WHY this treatment is running, and since when. Story 63.7.
 *
 * IT LEADS THE OTHER SIX BECAUSE IT IS THE FIRST QUESTION. Somebody opens a
 * Datastream, finds a run in flight they did not start, and needs to know
 * whether it is tonight's collection, the mapping change they confirmed four
 * minutes ago, or something nobody can account for. "Loading, 0 of - days" is
 * the same picture for all three, and the third one is an incident.
 *
 * `Not measured` for a run minted before this story -- nothing recorded which
 * path created it, and there is no way to work it out afterwards. An origin this
 * build does not know is shown EXACTLY as it arrived, with no label of its own:
 * a console older than its server may not fold an unknown reason onto a
 * neighbouring one, and a raw key a person can search for is worth more than a
 * plausible wrong word.
 */
function RunOrigin({ progress }: { progress: DatastreamProgress }) {
  const sentence = originSentence(progress.origin, progress.started_at);
  return (
    <p className="m-0 text-ui text-text" data-testid="run-live-origin">
      {sentence ?? NOT_MEASURED}
    </p>
  );
}

/** Why nothing is running -- four sentences, never one silence. */
function RunIdle({ poll }: { poll: DatastreamProgressPoll }) {
  const idle = poll.idle;
  if (!idle) return null;
  if (idle.reason === "never_ran") {
    return (
      <p className="m-0 text-ui text-text" data-testid="run-live-idle">
        This Datastream has never run.
      </p>
    );
  }
  // The exact state travels with the reason, so a run that was CANCELLED reads
  // as cancelled rather than as one more failure.
  const ended = `The last run ended as ${executionStateLabel(idle.state)} on ${dateTime(idle.ended_at)}.`;
  return (
    <div className="grid gap-1" data-testid="run-live-idle">
      <p className="m-0 text-ui text-text">Nothing is collecting. {ended}</p>
      {idle.reason === "last_run_failed" && (
        <p className="m-0 text-ui text-text-secondary">
          {idle.error_code
            ? `It reported ${idle.error_code}.`
            : "It recorded no error code."}
        </p>
      )}
      {idle.reason === "last_run_stopped" && <StoppedKept idle={idle} />}
    </div>
  );
}

/**
 * Stop this run -- story 63.6, and the ONE gesture this band writes.
 *
 * WHAT IT PROMISES AND WHAT IT REFUSES TO PROMISE. A provider call already in
 * flight cannot be interrupted: `_execute_job` is synchronous and the stale
 * sweep is at 5400 s. So the dialog says, in those words, that the window being
 * collected will finish and that nothing already collected is undone. Promising
 * an interruption would manufacture a false expectation in somebody watching a
 * counter refuse to stop.
 *
 * THE EVIDENCE NAMES THE SCOPE BEFORE THE WRITE, and it names the count BEFORE,
 * never after: the flux, the windows that will be refused, the window that will
 * finish anyway, and the days already kept. `Not measured` wherever the run has
 * measured nothing -- the dialog may not invent a figure to look precise.
 *
 * A FAILED STOP IS NOT A STOPPED RUN. The server's message stays IN the dialog,
 * the band keeps its figures and the poll keeps running: closing the dialog on a
 * refusal would leave a person believing a run was stopped that is still going.
 */
function StopThisRun({
  projectId,
  datastreamId,
  progress,
  onStopped,
}: {
  projectId: string;
  datastreamId: string;
  progress: DatastreamProgress;
  onStopped: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refused = windowsNotStarted(progress);
  const flight = progress.window_in_progress;

  const confirm = async () => {
    // A second click during the call would send a second write; `busy` is what
    // the dialog disables its confirm on, and this is the same guard on the
    // handler so a keyboard repeat cannot slip past it.
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await stopRun(projectId, datastreamId, progress.execution_id);
      setOpen(false);
      onStopped();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The stop was refused.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <Button
        variant="secondary"
        size="sm"
        onClick={() => {
          setError(null);
          setOpen(true);
        }}
        data-testid="run-live-stop"
      >
        Stop this run
      </Button>
      <ConfirmDialog
        open={open}
        onOpenChange={(next) => {
          if (busy) return;
          setOpen(next);
          if (!next) setError(null);
        }}
        title="Stop this run"
        description={
          "The windows that have not started will be refused. The window being " +
          "collected right now cannot be interrupted — it will finish and its rows " +
          "will land. Nothing already collected is undone."
        }
        evidenceLabel="What this stops"
        evidence={{
          Datastream: datastreamId,
          "Windows refused": counted(refused),
          "Window that will finish anyway": flight
            ? `${flight.date_from ?? NOT_MEASURED} to ${flight.date_to ?? NOT_MEASURED}`
            : "None is in flight",
          "Days already kept": counted(progress.days_done),
          "Rows already kept": counted(progress.rows_written),
        }}
        confirmLabel="Stop this run"
        destructive
        busy={busy}
        error={error}
        onConfirm={confirm}
        data-testid="run-live-stop-confirm"
        cancelTestId="run-live-stop-cancel"
        confirmTestId="run-live-stop-go"
      />
    </>
  );
}

/**
 * The band: the live state of this collection, in seven blocks and in this
 * order -- story 63.7 put WHY it is running at the head of the six of 63.5.
 *
 * It is a band of its own and never a fifth tile beside the four state axes:
 * `datastream-workbench-and-wizard.md` makes "lifecycle, configuration,
 * operations, run and publication states mixed into one status" an
 * `Incomplete if`, and where a run has got to is not an axis of the Datastream.
 */
export default function DatastreamRunLive({
  poll,
  projectId,
  datastreamId,
}: {
  poll: DatastreamProgressPoll;
  /**
   * The scope of the ONE write this band offers (story 63.6). Optional, because
   * a surface that has not resolved its scope yet must render the reading rather
   * than a button it cannot address -- and because the parity test mounts this
   * file over a payload with no route behind it.
   */
  projectId?: string;
  datastreamId?: string;
}) {
  const { progress, measuredAt, error } = poll;
  const stale = poll.phase === "error";
  const gaveUp = poll.stopped && error !== null;
  // Offered only while something is actually running: a Stop button on a band
  // that shows no run is a gesture with no object.
  const stoppable = progress !== null && projectId && datastreamId;

  return (
    <Panel
      role="group"
      aria-label="Collection state"
      data-testid="datastream-run-live"
      className="grid gap-4"
    >
      {progress ? (
        <>
          {/* Story 63.7: why this is running, before what it has got to. */}
          <RunOrigin progress={progress} />
          <div className="flex flex-wrap items-center gap-3">
            <DatastreamRunStateBadge state={progress.state} />
            {progress.day_in_progress && (
              <span className="text-caption text-text-secondary">
                Collecting {progress.day_in_progress}
              </span>
            )}
            {stoppable && (
              <span className="ml-auto">
                <StopThisRun
                  projectId={projectId as string}
                  datastreamId={datastreamId as string}
                  progress={progress}
                  // The band switches without waiting for the next tick: a person
                  // who just stopped a run may not be shown it still running.
                  onStopped={poll.refresh}
                />
              </span>
            )}
          </div>
          {/* A RUN THAT HAS NOT STARTED SHOWS NO MEASUREMENT — amendment 8 of
              the 2026-08-11 review.

              `started_at` is written by `open_collection_run` (migration 218).
              Before it, every writer this band reads is silent BY CONSTRUCTION:
              `rows_written` lands at a window boundary, `days_total` when the
              run declares its window, the estimate needs finished runs. So the
              band rendered four tiles and four paragraphs, all of them saying a
              different way that nothing had been measured — « ça sert à quoi de
              mettre des metrics de mesure, c'est pour rien retourner ».

              This does NOT weaken the rule it looks like it contradicts. « A
              counter that was never written says so rather than falling back in
              silence to another measurement » protects against a WRONG number,
              and it still holds for every run that has started: those tiles are
              unchanged. A run that has not started has no counter to be honest
              about — it has a state, and one gesture. */}
          {hasNotStarted(progress.state, progress.started_at) ? (
            /* AND WHY IT IS NOT STARTING, WHEN THE SERVER KNOWS — Jean,
               2026-08-12: « soit ça connecte pas correctement, soit y a encore
               des problèmes à traiter ». It was the second, and this band could
               not say it: a candidate is opened by a `candidate_materialization`
               job, and when that job dies the run stays at `created` for ever
               while the screen says « has not started ». True, and useless.
               Measured that day: 17 of 17 such jobs dead, none ever done, the
               oldest sitting thirteen hours.
               The code is the queue's, rendered verbatim — a sentence per code
               written here would be a second vocabulary, and an unknown code
               would then say nothing at all. */
            progress.materialization ? (
              <Status
                as="block"
                tone="error"
                title="This run could not be opened"
                data-testid="run-live-materialization-failed"
                /* THE ONE GESTURE THIS BAND HAS (76-4). Nothing here can restart
                   a dead materialization job -- that is the queue's, and this
                   file may not hold a second opinion about it -- but the band
                   CAN ask the server again, which is what a person does when a
                   job has been repaired elsewhere. An error that offers nothing
                   leaves them reloading the browser. */
                action={<Retry onClick={poll.refresh} />}
              >
                {/* NOT ONE STATE NAME IS TYPED HERE, and the band's own
                    conformance test is what enforces it: this file may not hold
                    a second opinion about what a state is called. The server
                    only sends this block for a job that stopped, so the sentence
                    says what happened without naming which of the two stopped
                    states it was, and the state itself travels as DATA. */}
                The job that opens it stopped
                {typeof progress.materialization.attempt_count === "number"
                  ? ` after ${progress.materialization.attempt_count} attempt${
                      progress.materialization.attempt_count === 1 ? "" : "s"
                    }`
                  : ""}
                {progress.materialization.error_code
                  ? `, reporting ${progress.materialization.error_code}`
                  : ", reporting no code"}
                {progress.materialization.last_attempt_at
                  ? ` on ${dateTime(progress.materialization.last_attempt_at)}`
                  : ""}
                . Nothing has been collected, and nothing will be until it is repaired.
                <span className="mt-1 block font-mono text-caption">
                  {stateLabel(progress.materialization.state)}
                </span>
              </Status>
            ) : (
            <p className="m-0 text-ui text-text-secondary" data-testid="run-live-not-started">
              This run has not started, so nothing has been measured yet. Its rows, its window
              and its duration appear as soon as it opens.
            </p>
            )
          ) : (
            <>
              <RunMeasurements progress={progress} />
              <TimeLeft estimate={progress.estimate} />
            </>
          )}
        </>
      ) : (
        <RunIdle poll={poll} />
      )}

      {/* Nothing has landed yet -- said, rather than shown as an empty band. */}
      {progress === null && poll.idle === null && (
        <p className="m-0 text-ui text-text-secondary" role="status">
          {stale ? "No state of this collection has been read yet." : "Reading the state of this collection…"}
        </p>
      )}

      {/* THE AGE OF THE READING, always beside the numbers it dates. */}
      <p className="m-0 text-caption text-text-secondary" data-testid="run-live-measured">
        {measuredAt === null
          ? "Nothing has been read yet."
          : stale
            ? `Not current — last read ${measurementAge(measuredAt)}.`
            : `Read ${measurementAge(measuredAt)}.`}
      </p>

      {error && (
        <Status
          as="block"
          tone="error"
          title={
            error.offline
              ? "The network did not answer"
              : `The collection state answered ${error.status}`
          }
          data-testid="run-live-error"
          action={
            // A person asking is not a poll: offered only once the poll has
            // given up, because retrying under it would race its own backoff.
            gaveUp ? (
              <Button variant="secondary" size="sm" onClick={poll.refresh}>
                Retry
              </Button>
            ) : undefined
          }
        >
          {gaveUp
            ? `${error.message} Nothing more will be read until you ask.`
            : `${error.message} The figures above are the last successful reading.`}
        </Status>
      )}
    </Panel>
  );
}
