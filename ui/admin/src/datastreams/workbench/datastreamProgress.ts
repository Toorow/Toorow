/**
 * The poll that watches ONE collection move, and stops when it stops -- story
 * 63.3, epic 63.
 *
 * A HOOK, NOT A COMPONENT, ON PURPOSE. This story owns the state, the cadence
 * and the stop; story 63.5 mounts and draws it on the three surfaces (the list
 * row, the Workbench object header, the `Runs` tab). Drawing here would either
 * pre-empt that story or leave an unmounted `.tsx` behind, which
 * `finished_work_audit.orphan_components()` counts as a defect -- correctly, an
 * unmounted screen file is work nobody can reach.
 *
 * WHY THE POLL STOPS, AND WHY THAT IS THE POINT. The service runs at
 * `--min-instances=0` (`infra/scripts/deploy.sh:104`), with no module-level
 * connection pool (`server/core/db.py:4-5`): every tick wakes an instance and
 * opens a connection. A tab left open overnight at five seconds, on the three
 * surfaces of 63.5, is 3 x 17_280 requests in 24 h and an instance that never
 * scales back to zero -- for a screen nobody is looking at. That expense, not
 * latency, is what stopping buys.
 *
 * SO IT STOPS ON FOUR THINGS, AND EACH ONE IS TESTED:
 *   1. the answer says nothing is running (`progress: null`), or the run's state
 *      is not `active` in the registry -- definitive for this mount;
 *   2. the tab is hidden -- suspended, then resumed by ONE immediate read;
 *   3. the route refuses (`401` / `403` / `404`) -- immediate, a refusal does
 *      not become an acceptance by being asked again;
 *   4. the route or the network keeps failing -- five attempts, backing off
 *      exponentially, then silence.
 *
 * AND THE ARITHMETIC IS NOT HERE EITHER. "How much longer" (story 63.4) is
 * computed by the server and read off the payload: the MCP reads that same
 * payload, and a duration derived in this file would be a number the tools
 * cannot see. What this module owns about it is one thing only -- that it stays
 * OUT of the quiet-cadence signature, because a countdown changes on every tick
 * and would keep the poll at five seconds for the whole length of a run.
 *
 * THE CADENCE MACHINERY IS NOT HERE. Points 2, 3 and 4 are how this console
 * polls, not how this Datastream is watched, so they live in `lib/polledRead.ts`
 * with the one other poll of the repository (`CacheHealthCard`). Two polls with
 * two sets of rules is two answers to "how do we poll here", and the second
 * reader believes the wrong one. What stays here is the only part that is about
 * the DOMAIN: the envelope, its scope check, and point 1.
 *
 * IT KEEPS ITS OWN STATE, BESIDE THE SCREEN'S. Not `useDataSurface`
 * (`data/dataSurface.ts:110-123` sets `status: "loading"` before every reload,
 * so a poll built on it would empty the fleet table on every tick) and not
 * `loadWorkbench` (nine queries and two connections, and the route blanks the
 * screen while it reloads). This never replaces the state of the screen that
 * carries it.
 */
import { apiGet, apiPost } from "../../lib/apiFetch";
import {
  usePolledRead,
  type PollFailure,
  type PollPhase,
} from "../../lib/polledRead";
import { record } from "./evidence";
import { executionState, phaseOf } from "./executionStates";

/** The envelope story 63.2 serves. A body that does not say this is not it. */
export const PROGRESS_SCHEMA = "datastream_progress.v1";

/** Cadence while the run is moving. */
export const LIVE_INTERVAL_MS = 5_000;
/** Cadence once the payload has repeated itself -- same numbers, less noise. */
export const QUIET_INTERVAL_MS = 15_000;
/** How many identical answers in a row before slowing to the quiet cadence. */
export const QUIET_AFTER_TICKS = 3;

// The failure regime is the console's, not this route's -- re-exported so a
// caller reads one module, and so there is no second copy to drift.
export { MAX_ATTEMPTS, RETRY_BASE_MS, STOP_STATUSES } from "../../lib/polledRead";

export interface WindowInProgress {
  date_from: string | null;
  date_to: string | null;
  /** The SIZE of the window in flight -- what explains a day count standing still. */
  days: number | null;
  /** When this window started, by the SERVER's clock. Never this browser's. */
  started_at: string | null;
  /** `null` for as long as it is in flight -- which is what makes that a fact. */
  completed_at: string | null;
}

/**
 * How much longer this run has -- story 63.4, COMPUTED ON THE SERVER.
 *
 * Nothing here is derived in the console, and that is the point: the MCP reads
 * the same payload as this screen, and a duration computed in a browser would be
 * a number the tools cannot see -- two surfaces answering "how much longer"
 * differently, which story 53.9 refuses. It is also the only way both ends of
 * the subtraction are server instants: comparing `measuredAt` (a browser epoch)
 * with a server `started_at` manufactures time the moment one clock runs ahead.
 *
 * `sentence` is present on EVERY answer, including the ones that decline to
 * estimate: the block says why it is silent, it never goes missing, and it is
 * never a `0` standing in for an absent measurement.
 */
export interface ProgressEstimate {
  /** True when the estimate speaks. False is a measurement, not an absence. */
  armed: boolean;
  /** Finished runs of this Datastream the rate was measured on. */
  observations: number | null;
  /** How many it needs -- disclosed with the answer, as the detector does. */
  minimum_observations: number | null;
  /**
   * What is being claimed: `"point"` one number, `"range"` a band, `null` when
   * the estimate declined. A bare value whose precision the reader has to guess
   * is the thing this field exists to prevent.
   */
  precision: "point" | "range" | null;
  /** The point value -- `null` under `"range"`, and never a `0` countdown. */
  seconds_remaining: number | null;
  /** The band, from the fastest and slowest run ever measured. */
  seconds_remaining_low: number | null;
  seconds_remaining_high: number | null;
  /**
   * Slowest measured rate over fastest. THE DISPERSION OF THE SAMPLE, published
   * beside the number: a median over three runs that agreed and a median over
   * three that disagreed by a factor of 120 are not the same claim, and nothing
   * else in this payload says which one this is.
   */
  spread_ratio: number | null;
  /** How far the window in flight has already outrun its usual cost. */
  behind_by_seconds: number | null;
  /** The SERVER instant the estimate was taken. */
  measured_at: string | null;
  /** Which silence this is, or `null` when it speaks. */
  reason: string | null;
  /** The English sentence to show. Always set. */
  sentence: string;
}

/** The run in flight, with the names story 63.1 writes, as it measured them. */
export interface DatastreamProgress {
  execution_id: string;
  state: string;
  /**
   * WHY this treatment is running -- story 63.7, a key of `runOrigins.ts`.
   *
   * `null` on every execution minted before that story: nothing recorded which
   * path created it, and no reader may work one out afterwards. A key this
   * build does not know arrives here unchanged and is shown as it arrived.
   */
  origin: string | null;
  step: string | null;
  day_in_progress: string | null;
  days_done: number | null;
  days_total: number | null;
  windows_done: number | null;
  windows_total: number | null;
  window_in_progress: WindowInProgress | null;
  rows_written: number | null;
  started_at: string | null;
  progress_updated_at: string | null;
  plan_version_id: string | null;
  mapping_version_id: string | null;
  /** Story 63.4. `null` only from a server that predates it. */
  estimate: ProgressEstimate | null;
  /**
   * WHY A RUN THAT HAS NOT STARTED IS NOT STARTING — Jean, 2026-08-12.
   *
   * A candidate is minted at `created`, and a `candidate_materialization` job is
   * what opens it. When that job dies the execution stays at `created` for ever,
   * and every surface said « this run has not started » — true, and useless: it
   * HAD been attempted, three times, and it failed. Measured that day: 17 of 17
   * such jobs in `dead_letter`, none ever `done`.
   *
   * `null` on a healthy run, on a job still working, and on a run that never had
   * one. `undefined` from a server that predates this — a different fact again,
   * and neither is rendered as the other.
   */
  materialization?: RunMaterialization | null;
}

/** The verdict of the job that was supposed to open a run. */
export interface RunMaterialization {
  state: string;
  /** The queue's own code, VERBATIM. The console frames it and never translates
   *  it: a second vocabulary here would drift from the queue, and an unknown
   *  code would render as nothing — the silence this exists to remove. */
  error_code: string | null;
  attempt_count: number | null;
  last_attempt_at: string | null;
}

export type IdleReason =
  | "never_ran"
  | "last_run_succeeded"
  | "last_run_failed"
  | "last_run_stopped";

/**
 * WHY nothing is running -- four sentences, never one silence.
 *
 * `last_run_stopped` is story 63.6's, and it is not a nicety: a run somebody
 * stopped used to arrive as `last_run_failed`, which fires the failure phrasing
 * on all three surfaces of 63.5 and sends an operator hunting for an outage
 * nobody had.
 *
 * `days_done` / `days_total` / `rows_written` are what the run KEPT. After a
 * stop the run is terminal, so `progress` is `null` and this is the only object
 * on the wire that still carries them -- without them "show what was kept" has
 * no source at all. `null` means not measured; it is never rendered as `0`.
 */
export interface DatastreamIdle {
  reason: IdleReason;
  execution_id: string | null;
  state: string | null;
  ended_at: string | null;
  error_code: string | null;
  days_done: number | null;
  days_total: number | null;
  rows_written: number | null;
}

export interface ProgressEnvelope {
  progress: DatastreamProgress | null;
  idle: DatastreamIdle | null;
}

// Where the poll itself is, and what a failure looks like: the console's
// vocabulary, one definition, in `lib/polledRead.ts`.
export type { PollFailure, PollPhase };

export interface DatastreamProgressPoll {
  phase: PollPhase;
  /** The run in flight at the last SUCCESSFUL read, or null when none was. */
  progress: DatastreamProgress | null;
  /** Why nothing is running, when the last successful read said nothing is. */
  idle: DatastreamIdle | null;
  /**
   * When the last successful read landed, in epoch milliseconds -- null while
   * none has. A caller that renders `progress` without rendering this is
   * presenting an old number as a current one.
   */
  measuredAt: number | null;
  /** The failure the poll is sitting on, or null while it is not. */
  error: PollFailure | null;
  /** Consecutive failures so far; `MAX_ATTEMPTS` is where the poll gives up. */
  attempts: number;
  /** True once nothing more will be read without a person asking. */
  stopped: boolean;
  /** Read now and resume -- the one way out of a poll that gave up. */
  refresh: () => void;
}

/**
 * Raised when an envelope answers about a different flux than the one asked
 * about. Not a transient failure: retrying a server that answers about the
 * wrong Datastream cannot fix it, and rendering it would put another project's
 * numbers under this Datastream's name.
 */
export class ProgressScopeError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ProgressScopeError";
  }
}

function nullableString(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function nullableNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function readWindow(value: unknown): WindowInProgress | null {
  const source = record(value);
  if (!source) return null;
  return {
    date_from: nullableString(source.date_from),
    date_to: nullableString(source.date_to),
    days: nullableNumber(source.days),
    started_at: nullableString(source.started_at),
    completed_at: nullableString(source.completed_at),
  };
}

/**
 * The verdict of the job that was supposed to open this run.
 *
 * `undefined` is a server that predates the field; `null` is a server that has
 * it and has nothing to report. Both mean "say nothing", and they are kept apart
 * because only the first is a reason to suspect the reading itself.
 */
function readMaterialization(value: unknown): RunMaterialization | null {
  const source = record(value);
  if (!source) return null;
  const state = nullableString(source.state);
  if (!state) return null;
  return {
    state,
    error_code: nullableString(source.error_code),
    attempt_count: nullableNumber(source.attempt_count),
    last_attempt_at: nullableString(source.last_attempt_at),
  };
}

/**
 * The estimate, read as the server sent it -- and never recomputed here.
 *
 * An answer with no sentence is not an estimate: the ratified contract is that
 * the block always says something, so a body that carries numbers and no words
 * is refused rather than rendered as a bare duration.
 */
function readEstimate(value: unknown): ProgressEstimate | null {
  const source = record(value);
  if (!source) return null;
  const sentence = nullableString(source.sentence);
  if (!sentence) return null;
  const precision = nullableString(source.precision);
  return {
    armed: source.armed === true,
    observations: nullableNumber(source.observations),
    minimum_observations: nullableNumber(source.minimum_observations),
    precision: precision === "point" || precision === "range" ? precision : null,
    seconds_remaining: nullableNumber(source.seconds_remaining),
    seconds_remaining_low: nullableNumber(source.seconds_remaining_low),
    seconds_remaining_high: nullableNumber(source.seconds_remaining_high),
    spread_ratio: nullableNumber(source.spread_ratio),
    behind_by_seconds: nullableNumber(source.behind_by_seconds),
    measured_at: nullableString(source.measured_at),
    reason: nullableString(source.reason),
    sentence,
  };
}

/**
 * What "this answer said the same thing as the last one" means.
 *
 * EVERYTHING THE RUN MEASURED, AND NEVER THE ESTIMATE. The estimate carries the
 * server instant it was taken and a countdown derived from it, so it differs on
 * every single tick by construction. Signing it would hold the poll at the live
 * cadence for the entire length of every run -- 17_280 wake-ups a day per
 * surface instead of 5_760 -- while nothing an operator can act on had changed.
 * A clock that always moves is not new information.
 */
export function progressSignature(progress: DatastreamProgress | null): string {
  if (!progress) return "no-run";
  const { estimate: _estimate, ...measured } = progress;
  return JSON.stringify(measured);
}

/** The reasons this build understands. A body naming another one is not read. */
const IDLE_REASONS: readonly IdleReason[] = [
  "never_ran",
  "last_run_succeeded",
  "last_run_failed",
  "last_run_stopped",
];

function readIdle(value: unknown): DatastreamIdle | null {
  const source = record(value);
  if (!source) return null;
  const reason = nullableString(source.reason);
  if (!reason || !IDLE_REASONS.includes(reason as IdleReason)) return null;
  return {
    reason: reason as IdleReason,
    execution_id: nullableString(source.execution_id),
    state: nullableString(source.state),
    ended_at: nullableString(source.ended_at),
    error_code: nullableString(source.error_code),
    days_done: nullableNumber(source.days_done),
    days_total: nullableNumber(source.days_total),
    rows_written: nullableNumber(source.rows_written),
  };
}

/**
 * The envelope, checked against the flux that was asked about.
 *
 * The scope check is the one `workbenchApi.ts:17-19` already applies to the
 * Workbench header: three polls are in flight at once (story 63.5's three
 * surfaces), so an answer that does not name its flux cannot be routed, and one
 * that names another flux must be refused rather than displayed.
 *
 * A field story 63.1 has not written yet reads `null`. It is never turned into
 * a `0`: zero rows written is a measurement, and this one was not made.
 */
export function parseProgressEnvelope(
  value: unknown,
  projectId: string,
  datastreamId: string,
): ProgressEnvelope {
  const envelope = record(value);
  if (!envelope || envelope.schema !== PROGRESS_SCHEMA) {
    throw new ProgressScopeError("The progress answer did not carry the expected envelope.");
  }
  if (envelope.project_id !== projectId || envelope.datastream_id !== datastreamId) {
    throw new ProgressScopeError(
      "The progress answer described a different Project or Datastream than the one requested.",
    );
  }
  const source = record(envelope.progress);
  if (!source) return { progress: null, idle: readIdle(envelope.idle) };
  const executionId = nullableString(source.execution_id);
  const state = nullableString(source.state);
  if (!executionId || !state) {
    throw new ProgressScopeError("The progress answer did not say which run it describes.");
  }
  return {
    progress: {
      execution_id: executionId,
      state,
      // Story 63.7. Read as it arrived and resolved nowhere here: an origin the
      // registry does not know is shown as received, and a payload from a
      // server that predates this story carries none at all.
      origin: nullableString(source.origin),
      step: nullableString(source.step),
      day_in_progress: nullableString(source.day_in_progress),
      days_done: nullableNumber(source.days_done),
      days_total: nullableNumber(source.days_total),
      windows_done: nullableNumber(source.windows_done),
      windows_total: nullableNumber(source.windows_total),
      window_in_progress: readWindow(source.window_in_progress),
      rows_written: nullableNumber(source.rows_written),
      started_at: nullableString(source.started_at),
      progress_updated_at: nullableString(source.progress_updated_at),
      plan_version_id: nullableString(source.plan_version_id),
      mapping_version_id: nullableString(source.mapping_version_id),
      estimate: readEstimate(source.estimate),
      materialization: readMaterialization(source.materialization),
    },
    idle: null,
  };
}

/**
 * Is this run still moving? The registry decides, here as everywhere.
 *
 * `executionStates.ts` is compared entry by entry against
 * `server/core/execution_states.py`; a state literal typed here would be a
 * second list to keep in step, and the six that existed before this registry
 * broke four surfaces at once. An unknown state is NOT treated as active: a
 * build that does not know a state has not been shown a run that is moving.
 */
export function isRunMoving(state: unknown): boolean {
  return executionState(state)?.active === true;
}

/**
 * IS THIS RUN ACTUALLY PROGRESSING? — Jean, 2026-08-12: « l'animation sur
 * `Created`, on dirait qu'il charge un truc alors que pas du tout ».
 *
 * `active` and `progressing` are two different facts and the registry only had a
 * word for the first. `active` means the run OCCUPIES the Datastream — it is not
 * terminal, it holds the lock, it can be stopped. `created` carries
 * `active: true` for exactly that reason (`executionStates.ts`), and it also
 * carries `phase: "todo"`: nothing has begun.
 *
 * One word did both jobs, so a run that had never started was painted with the
 * spinner that means « working ». Measured the same day: 10 executions sat at
 * `created`, the oldest for thirteen hours, every one of them animated as if it
 * were collecting.
 *
 * The phase is the authority, and it is mirrored from
 * `server/core/execution_states.py`. `isRunMoving` keeps its meaning for the
 * things that genuinely ask it — whether to keep polling, whether a stop is
 * offered — and every ANIMATION reads this instead.
 */
export function isRunProgressing(state: unknown): boolean {
  return phaseOf(state) === "running";
}

/** The address of the poll -- the project AND the datastream, both encoded. */
export function progressPath(projectId: string, datastreamId: string): string {
  return `/api/projects/${encodeURIComponent(projectId)}/datastreams/${encodeURIComponent(
    datastreamId,
  )}/progress`;
}

/** The address of the stop -- story 63.6. The run is named, never inferred. */
export function stopRunPath(
  projectId: string,
  datastreamId: string,
  executionId: string,
): string {
  return `${progressPath(projectId, datastreamId).replace(
    /\/progress$/,
    "",
  )}/runs/${encodeURIComponent(executionId)}/stop`;
}

/** What the stop answered: what it refused, what finishes anyway, what it kept. */
export interface StopRunAnswer {
  execution_id: string | null;
  state: string | null;
  windows_refused: number | null;
  window_in_flight: { date_from: string | null; date_to: string | null } | null;
  days_kept: number | null;
  rows_kept: number | null;
  stopped_at: string | null;
}

/**
 * How many windows this stop would refuse -- COMPUTED FROM THE PAYLOAD.
 *
 * The confirmation has to name the scope BEFORE the write, and the only thing
 * the screen holds at that moment is the progress payload. `windows_total` and
 * `windows_done` are the run's own counts and the window in flight is a fact on
 * the row, so the subtraction is a reading of measured numbers -- never an
 * invented figure. `null` when the run declared no window count: an unknown
 * scope is said, not guessed at.
 */
export function windowsNotStarted(progress: DatastreamProgress | null): number | null {
  if (!progress) return null;
  const { windows_total: total, windows_done: done, window_in_progress: flight } = progress;
  if (typeof total !== "number") return null;
  const finished = typeof done === "number" ? done : 0;
  const remaining = total - finished - (flight ? 1 : 0);
  return remaining > 0 ? remaining : 0;
}

/** Ask the server to stop this run. One write, three doors -- this is one. */
export async function stopRun(
  projectId: string,
  datastreamId: string,
  executionId: string,
): Promise<StopRunAnswer> {
  return apiPost<StopRunAnswer>(stopRunPath(projectId, datastreamId, executionId));
}

/**
 * Watch one Datastream's collection until it is over.
 *
 * Pass `undefined` for either id (a screen that has not resolved its scope yet)
 * and nothing is polled at all -- an unscoped read would be a request that
 * cannot be routed to an answer.
 */
export function useDatastreamProgress(
  projectId: string | undefined,
  datastreamId: string | undefined,
): DatastreamProgressPoll {
  const scoped = Boolean(projectId && datastreamId);
  const path = scoped ? progressPath(projectId as string, datastreamId as string) : null;

  const poll = usePolledRead<ProgressEnvelope>({
    key: path,
    intervalMs: LIVE_INTERVAL_MS,
    read: async (signal) => {
      const body = await apiGet<unknown>(path as string, { signal });
      return parseProgressEnvelope(body, projectId as string, datastreamId as string);
    },
    // The ONLY stop that is about the domain: nothing is running, or the run's
    // state no longer holds the active lock. The registry decides which states
    // those are -- an unknown state is not read as "still moving", so a console
    // older than its server stops rather than inventing an outcome.
    continues: (envelope) => envelope.progress !== null && isRunMoving(envelope.progress.state),
    // Three polls are in flight at once (story 63.5's three surfaces). An
    // envelope that names another flux is a contract not being honoured, not a
    // passing incident, and asking again cannot mend it.
    fatal: (reason) => reason instanceof ProgressScopeError,
    quiet: {
      afterTicks: QUIET_AFTER_TICKS,
      intervalMs: QUIET_INTERVAL_MS,
      // The estimate is deliberately outside the signature -- see
      // `progressSignature`. It moves on every tick and means nothing new.
      signature: (envelope) => progressSignature(envelope.progress),
    },
  });

  return {
    phase: poll.phase,
    progress: poll.value?.progress ?? null,
    idle: poll.value?.idle ?? null,
    measuredAt: poll.measuredAt,
    error: poll.error,
    attempts: poll.attempts,
    stopped: poll.stopped,
    refresh: poll.refresh,
  };
}
