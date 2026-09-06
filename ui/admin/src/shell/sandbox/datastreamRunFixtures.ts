/**
 * `GET /api/projects/{p}/datastreams/{d}/progress` — the band that has NEVER been
 * looked at, in the four states it was built to tell apart.
 *
 * WHY THIS FILE EXISTS. `DatastreamRunLive` is mounted above the tab band since
 * story 63.5, so it is on screen on every one of the eight tabs. The sandbox had
 * no `/progress` branch: `fixtureFor` fell through to its `404`, `404` is in
 * `STOP_STATUSES` (`lib/polledRead.ts`), so the poll stopped on the first tick
 * and the band rendered « The collection state answered 404 » — permanently, on
 * every tab, in every capture this repository has ever taken. Nobody has ever
 * seen this band live, idle, or in its « never ran » state, and every design
 * review of the Workbench was conducted with an error panel at the top of the
 * page.
 *
 * THE SHAPES ARE THE SERVER'S, NOT A GUESS. `compose_payload`
 * (`server/core/datastream_progress_api.py`) always sends the envelope
 * `{schema, project_id, datastream_id, progress, idle}` with exactly one of the
 * last two set, `PROGRESS_FIELDS` naming what a run carries and `IDLE_FIELDS`
 * naming what a silence carries. `estimate` is `estimate_time_left`'s own answer
 * (`datastream_progress_estimate.py`): `armed` with a `precision`, or one of the
 * four silences — and its `sentence` is present on all of them.
 *
 * EVERY COUNTER IS `null` UNTIL IT IS MEASURED. The band prints *Not measured*
 * for a `null` and never a `0`, so a fixture that filled the blanks with zeros
 * would hide the one rule this component was written to hold.
 */
import { PROJECT_ID } from "./datastreamSandboxScope";

/** The states a caller may ask this sandbox for. */
export type RunShape =
  | "running"
  | "not_started"
  | "idle"
  | "never_ran"
  | "failed"
  | "stopped";

export const RUN_SHAPES: readonly RunShape[] = [
  "running",
  "not_started",
  "idle",
  "never_ran",
  "failed",
  "stopped",
];

/** The default shape of each mode: a pull is watched while it runs; a file source
 *  is waited on. Both are the ordinary state of their mode, not an edge. */
export const DEFAULT_RUN_SHAPE: RunShape = "running";

const SCHEMA = "datastream_progress.v1";

/**
 * A run in flight, four windows into a seven-day catch-up.
 *
 * `origin` is a key of `runOrigins.ts` — `scheduler_nightly`, which DOES read
 * provider windows, so the fraction and the window sentence both apply. The
 * estimate is `armed` at `point` precision with its sample disclosed, which is
 * the branch `TimeLeft` renders and which no capture has ever carried.
 */
const RUNNING = {
  execution_id: "run_EXAMPLE_43",
  state: "loading",
  origin: "scheduler_nightly",
  step: "Collecting provider windows",
  day_in_progress: "2026-07-30",
  days_done: 4,
  days_total: 7,
  windows_done: 1,
  windows_total: 3,
  window_in_progress: {
    date_from: "2026-07-29",
    date_to: "2026-07-31",
    days: 3,
    started_at: "2026-08-03T02:01:10Z",
    completed_at: null,
  },
  rows_written: 9840,
  started_at: "2026-08-03T02:00:00Z",
  progress_updated_at: "2026-08-03T02:03:40Z",
  plan_version_id: "dplan_EXAMPLE_v1",
  mapping_version_id: "dmap_EXAMPLE_v1",
  estimate: {
    armed: true,
    observations: 11,
    minimum_observations: 3,
    precision: "point",
    seconds_remaining: 260,
    seconds_remaining_low: 180,
    seconds_remaining_high: 420,
    spread_ratio: 2.33,
    behind_by_seconds: 0,
    measured_at: "2026-08-03T02:03:40Z",
    reason: null,
    sentence: "About 4 minutes left.",
  },
};

/**
 * A run that has been OPENED and has not begun — `hasNotStarted` (exported from
 * `DatastreamRunLive.tsx`) is `!started_at && phaseOf(state) === "todo"`, and
 * `created` is the only stored state whose phase is `todo`.
 *
 * Every counter is `null`, because that is what amendment 8 measured: nothing
 * writes any of them before `open_collection_run` does. The band therefore draws
 * ONE sentence instead of four empty tiles — the repair that amendment made, and
 * the state it could not be seen in.
 */
const NOT_STARTED = {
  execution_id: "run_EXAMPLE_44",
  state: "created",
  origin: "mapping_change",
  step: null,
  day_in_progress: null,
  days_done: null,
  days_total: null,
  windows_done: null,
  windows_total: null,
  window_in_progress: null,
  rows_written: null,
  started_at: null,
  progress_updated_at: null,
  plan_version_id: "dplan_EXAMPLE_v1",
  mapping_version_id: "dmap_EXAMPLE_v1",
  // `estimate_time_left` returns the first of its four silences here: `days_total`
  // is null, so there is nothing to multiply. The sentence is verbatim.
  estimate: {
    armed: false,
    observations: 11,
    minimum_observations: 3,
    precision: null,
    seconds_remaining: null,
    seconds_remaining_low: null,
    seconds_remaining_high: null,
    spread_ratio: 2.33,
    behind_by_seconds: null,
    measured_at: null,
    reason: "run_not_measured_yet",
    sentence: "This run has not said how much it will collect yet — no estimate.",
  },
};

/** `IDLE_FIELDS`, in payload order. A silence carries what the run KEPT. */
function idle(fields: {
  reason: string;
  execution_id: string | null;
  state: string | null;
  ended_at: string | null;
  error_code: string | null;
  days_done: number | null;
  days_total: number | null;
  rows_written: number | null;
}) {
  return fields;
}

const IDLE_SUCCEEDED = idle({
  reason: "last_run_succeeded",
  execution_id: "run_EXAMPLE_42",
  state: "published",
  ended_at: "2026-08-02T02:14:00Z",
  error_code: null,
  days_done: 7,
  days_total: 7,
  rows_written: 17980,
});

/** A Datastream nobody has ever collected from. Three of the four `idle` reasons
 *  say what the LAST run did; this one says there has never been one, and it is
 *  the only sentence of the band with no run behind it. */
const IDLE_NEVER_RAN = idle({
  reason: "never_ran",
  execution_id: null,
  state: null,
  ended_at: null,
  error_code: null,
  days_done: null,
  days_total: null,
  rows_written: null,
});

const IDLE_FAILED = idle({
  reason: "last_run_failed",
  execution_id: "run_EXAMPLE_41",
  state: "failed",
  ended_at: "2026-08-01T02:03:00Z",
  error_code: "provider_quota_exhausted",
  days_done: 2,
  days_total: 7,
  rows_written: null,
});

/** Story 63.6. A run somebody STOPPED is not a run that failed, and this is the
 *  only payload that still carries what it kept — the run is terminal, so
 *  `PROGRESS_SQL` no longer returns it. `rows_written` is `null` on purpose:
 *  this run finished no window, and « 0 rows landed » is a claim it never made. */
const IDLE_STOPPED = idle({
  reason: "last_run_stopped",
  execution_id: "run_EXAMPLE_40",
  state: "cancelled",
  ended_at: "2026-07-31T02:06:00Z",
  error_code: null,
  days_done: 3,
  days_total: 7,
  rows_written: null,
});

const IDLE_BY_SHAPE: Partial<Record<RunShape, ReturnType<typeof idle>>> = {
  idle: IDLE_SUCCEEDED,
  never_ran: IDLE_NEVER_RAN,
  failed: IDLE_FAILED,
  stopped: IDLE_STOPPED,
};

/** The envelope, always — never a bare `null` on the wire. */
export function progressFixture(shape: RunShape, datastreamId: string) {
  const progress = shape === "running" ? RUNNING : shape === "not_started" ? NOT_STARTED : null;
  return {
    schema: SCHEMA,
    project_id: PROJECT_ID,
    datastream_id: datastreamId,
    progress,
    idle: progress === null ? IDLE_BY_SHAPE[shape] ?? IDLE_NEVER_RAN : null,
  };
}

/**
 * What `POST …/runs/{id}/stop` answers — so the ONE gesture this band offers can
 * be walked to its end. Without it the confirmation opened, wrote, and rendered
 * the sandbox's `404` inside the dialog, which is the failed-stop branch and not
 * the stop.
 */
export function stopRunFixture() {
  // `STOP_FIELDS`, in payload order (`server/core/execution_progress.py`). The
  // answer names no Project and no Datastream: the write was addressed to one.
  return {
    execution_id: RUNNING.execution_id,
    state: "cancelled",
    windows_refused: 1,
    window_in_flight: {
      date_from: RUNNING.window_in_progress.date_from,
      date_to: RUNNING.window_in_progress.date_to,
    },
    days_kept: RUNNING.days_done,
    rows_kept: RUNNING.rows_written,
    stopped_at: "2026-08-03T02:04:00Z",
  };
}
