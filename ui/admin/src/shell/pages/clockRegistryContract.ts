/**
 * The platform clock wire contract — declared, observed, and the verdict between.
 *
 * NAMED `clockRegistryContract`, NOT `platformClocks`. A module whose name
 * differs from `PlatformClocks.tsx` only in casing resolves to the same file on
 * Windows and macOS: TypeScript reports TS1261 and the screen's own default
 * export becomes unreachable from its test. Measured here, on the first
 * typecheck of this pair.
 *
 * WHY THIS FILE IS SEPARATE FROM THE SCREEN. Everything the console assumes
 * about the shape of `platform_clocks_api.py` lives here and nowhere else, so a
 * disagreement with the server is one file to reconcile rather than forty call
 * sites. The screen imports these types and these paths; it never builds a URL
 * or reads a raw field of its own.
 *
 * THE CONTRACT IS THE MIGRATION, not a guess. Every field below is a column of
 * `infra/nango/migrations/195_platform_clock_registry.sql`, with its exact name
 * and its exact vocabulary:
 *
 *   declared_*        what this deployment says the clock must be
 *   observed_*        what Cloud Scheduler held at the last reconciliation
 *   drift_verdict     in_sync | drifted | missing_in_gcp | unmanaged_in_gcp | unknown
 *   drift_detail      {"schedule": {"declared": "...", "observed": "..."}, ...}
 *   observation_error why the verdict is `unknown`
 *
 * TWO SHAPES ARE ACCEPTED, DELIBERATELY. A REST layer over that table may serve
 * the columns flat (`declared_schedule`) or grouped (`declared: {schedule}`).
 * Both are read here, because the alternative is a screen that renders every
 * field as "Unavailable" the day the API groups what it used to flatten — a
 * blank column that reads exactly like "nothing is configured". The one thing
 * this module never does is invent a value: an absent field stays `null` and
 * the screen says so.
 *
 * WHAT THIS IS NOT. Not the cadence of a Datastream. That object is
 * `app.datastream_schedule_state`, it is edited in the Datastream workbench
 * (`datastreams/workbench/SchedulePanel.tsx`), and it is per-Datastream. These
 * five rows are the platform's own heartbeat: one set per deployment.
 */

/** Root of the platform-clock routes served by `server/core/platform_clocks_api.py`.
 *
 * Measured against that file rather than assumed: it registers
 * `/api/platform/clocks`, `/api/platform/clocks/{clock_name}` (GET, PATCH) and
 * the two action paths below. This screen and the server were written in
 * parallel and had drifted -- the screen aimed at `/api/admin/platform-clocks`
 * and would have 404'd on every call. The server is the authority here because
 * its routes are the ones actually mounted and covered by a seam test.
 */
export const PLATFORM_CLOCK_ROOT = "/api/platform/clocks";

export function clockListPath(): string {
  return PLATFORM_CLOCK_ROOT;
}
export function clockPath(clockName: string): string {
  return `${PLATFORM_CLOCK_ROOT}/${encodeURIComponent(clockName)}`;
}
export function clockApplyPath(clockName: string): string {
  return `${clockPath(clockName)}/apply`;
}
export function clockRunNowPath(clockName: string): string {
  // The server serves `/run`, not `/run-now`.
  return `${clockPath(clockName)}/run`;
}

/**
 * The five verdicts. `unmanaged_in_gcp` is the one that is never stored — a job
 * found in Cloud Scheduler with no declaration here is REPORTED, never absorbed
 * into the registry (the migration's `ck_platform_clocks_never_declares_unmanaged`).
 * It reaches this screen through the list response all the same, because a job
 * nobody declared is the most dangerous of the five, not the least worth showing.
 */
export type ClockVerdict =
  | "in_sync"
  | "drifted"
  | "missing_in_gcp"
  | "unmanaged_in_gcp"
  | "unknown";

export const CLOCK_VERDICTS: readonly ClockVerdict[] = [
  "in_sync",
  "drifted",
  "missing_in_gcp",
  "unmanaged_in_gcp",
  "unknown",
];

/** `desired_state` — 'retired' means the clock MUST NOT exist in GCP any more. */
export type DesiredState = "enabled" | "paused" | "retired";
export const DESIRED_STATES: readonly DesiredState[] = ["enabled", "paused", "retired"];

/** Cloud Scheduler's own `Job.State`, kept verbatim: `update_failed` is the one
 *  state that says the last write to that job did not take, and collapsing it
 *  into `disabled` would hide it. */
export type ObservedState =
  | "enabled"
  | "paused"
  | "disabled"
  | "update_failed"
  | "absent"
  | "unknown";

export interface ClockDeclared {
  schedule: string;
  timezone: string | null;
  target_path: string | null;
  http_method: string | null;
  attempt_deadline_seconds: number | null;
  desired_state: DesiredState | null;
  purpose: string | null;
}

export interface ClockObserved {
  observed_at: string | null;
  state: ObservedState | null;
  schedule: string | null;
  timezone: string | null;
  target_uri: string | null;
  http_method: string | null;
  attempt_deadline_seconds: number | null;
  last_attempt_at: string | null;
  last_attempt_status: string | null;
}

export interface PlatformClock {
  clock_name: string;
  /** `null` when the registry has a declaration nobody has reconciled yet. The
   *  screen renders that as `unknown` — never as agreement. */
  verdict: ClockVerdict | null;
  /** `null` for `unmanaged_in_gcp`: there is no declaration, by definition. */
  declared: ClockDeclared | null;
  /** `null` when Cloud Scheduler has never been read for this clock, or when the
   *  job is absent. Never an empty object — "we did not read it" and "it holds
   *  nothing" are different statements. */
  observed: ClockObserved | null;
  /** `{field: {declared, observed}}`. Empty for every verdict but `drifted`. */
  drift_detail: Record<string, unknown>;
  /** Why the verdict is `unknown`. Required by the migration when it is. */
  observation_error: string | null;
}

export interface PlatformClockList {
  clocks: PlatformClock[];
  /** When the observed half was last read from Cloud Scheduler, for the whole set. */
  reconciled_at: string | null;
  /** Set when the reconciliation itself failed — an authorization or an API
   *  refusal. Every clock is then `unknown`, and the screen must say why once
   *  rather than repeat "unavailable" five times with no cause. */
  reconciliation_error: string | null;
}

// ---------------------------------------------------------------------------
// Normalization. Nothing below invents a value; it only locates one.
// ---------------------------------------------------------------------------

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function text(value: unknown): string | null {
  return typeof value === "string" && value.trim() !== "" ? value : null;
}

function count(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "" && Number.isFinite(Number(value))) {
    return Number(value);
  }
  return null;
}

/**
 * One field, whichever of the two shapes the server chose:
 *   grouped  `declared: { schedule }`
 *   flat     `declared_schedule`
 *   bare     `purpose`, `desired_state` — columns the migration does not prefix
 */
function field(raw: Record<string, unknown>, group: string, name: string): unknown {
  const nested = record(raw[group]);
  if (name in nested) return nested[name];
  const prefixed = `${group}_${name}`;
  if (prefixed in raw) return raw[prefixed];
  return raw[name];
}

function asVerdict(value: unknown): ClockVerdict | null {
  return typeof value === "string" && (CLOCK_VERDICTS as readonly string[]).includes(value)
    ? (value as ClockVerdict)
    : null;
}

function asDesiredState(value: unknown): DesiredState | null {
  return typeof value === "string" && (DESIRED_STATES as readonly string[]).includes(value)
    ? (value as DesiredState)
    : null;
}

const OBSERVED_STATES: readonly string[] = [
  "enabled",
  "paused",
  "disabled",
  "update_failed",
  "absent",
  "unknown",
];

function asObservedState(value: unknown): ObservedState | null {
  return typeof value === "string" && OBSERVED_STATES.includes(value)
    ? (value as ObservedState)
    : null;
}

/** One row of the list response, read into the shape the screen renders. */
export function normalizeClock(input: unknown): PlatformClock | null {
  const raw = record(input);
  const clockName = text(raw.clock_name) ?? text(raw.name);
  if (!clockName) return null;

  const schedule = text(field(raw, "declared", "schedule"));
  // No declared schedule means no declaration at all — the `unmanaged_in_gcp`
  // case. It is NOT an empty declaration, and rendering it as one would show a
  // row of blanks where the truth is "this platform never declared this job".
  const declared: ClockDeclared | null = schedule
    ? {
        schedule,
        timezone: text(field(raw, "declared", "timezone")),
        target_path: text(field(raw, "declared", "target_path")),
        http_method: text(field(raw, "declared", "http_method")),
        attempt_deadline_seconds: count(field(raw, "declared", "attempt_deadline_seconds")),
        desired_state: asDesiredState(field(raw, "declared", "desired_state")),
        purpose: text(field(raw, "declared", "purpose")),
      }
    : null;

  const observedAt = text(raw.observed_at) ?? text(record(raw.observed).observed_at) ?? text(record(raw.observed).at);
  const observedState = asObservedState(field(raw, "observed", "state"));
  const observedSchedule = text(field(raw, "observed", "schedule"));
  // "Never read" and "read, and it holds nothing" are different statements. The
  // observed half exists only when at least one of them is answered.
  const observed: ClockObserved | null =
    observedAt || observedState || observedSchedule
      ? {
          observed_at: observedAt,
          state: observedState,
          schedule: observedSchedule,
          timezone: text(field(raw, "observed", "timezone")),
          target_uri: text(field(raw, "observed", "target_uri")),
          http_method: text(field(raw, "observed", "http_method")),
          attempt_deadline_seconds: count(field(raw, "observed", "attempt_deadline_seconds")),
          last_attempt_at: text(field(raw, "observed", "last_attempt_at")),
          last_attempt_status: text(field(raw, "observed", "last_attempt_status")),
        }
      : null;

  return {
    clock_name: clockName,
    verdict: asVerdict(raw.drift_verdict ?? raw.verdict),
    declared,
    observed,
    drift_detail: record(raw.drift_detail ?? raw.drift),
    observation_error: text(raw.observation_error),
  };
}

/** The list response. A payload that carries no readable clock is an EMPTY list,
 *  and the screen distinguishes that from a failed read — which is why this
 *  function never throws and never fabricates a row. */
export function normalizeClockList(input: unknown): PlatformClockList {
  const raw = record(input);
  const rows = Array.isArray(raw.clocks)
    ? raw.clocks
    : Array.isArray(raw.items)
      ? raw.items
      : Array.isArray(input)
        ? (input as unknown[])
        : [];
  const clocks: PlatformClock[] = [];
  for (const row of rows) {
    const clock = normalizeClock(row);
    if (clock) clocks.push(clock);
  }
  return {
    clocks,
    reconciled_at: text(raw.reconciled_at) ?? text(raw.observed_at),
    reconciliation_error: text(raw.reconciliation_error) ?? text(raw.observation_error),
  };
}

/**
 * The verdict the screen renders. A clock with no verdict has never been
 * reconciled, and that is `unknown` — the one thing it must not become is
 * `in_sync` by omission. This function is the whole reason the screen never
 * reads `clock.verdict` directly.
 */
export function verdictOf(clock: PlatformClock): ClockVerdict {
  return clock.verdict ?? "unknown";
}

/** True when the observed half was genuinely read. Drives "not read" vs "absent". */
export function wasObserved(clock: PlatformClock): boolean {
  return clock.observed !== null;
}

export interface DriftDifference {
  field: string;
  declared: unknown;
  observed: unknown;
}

/** `drift_detail` read as the list of fields that differ, in a stable order. */
export function driftDifferences(clock: PlatformClock): DriftDifference[] {
  const rows: DriftDifference[] = [];
  for (const [name, value] of Object.entries(clock.drift_detail ?? {})) {
    const pair = record(value);
    rows.push({
      field: name,
      declared: "declared" in pair ? pair.declared : undefined,
      observed: "observed" in pair ? pair.observed : undefined,
    });
  }
  rows.sort((left, right) => left.field.localeCompare(right.field));
  return rows;
}

// ---------------------------------------------------------------------------
// The nightly STEP ledger — `execution-substrate.md` "Incomplete if" 2, the
// third locus.
//
// The clock registry above answers "did the heartbeat fire?". It cannot answer
// "and did the work inside that beat happen?": `scheduler._run_isolated_step`
// never re-raises, so until migration 325 a nightly step that silently never ran
// left no row anywhere. `app.nightly_step_runs` is that record, and the shape is
// what makes it one — the WHOLE declared sequence is written at dispatch, so a
// step that never began is an OPEN ROW rather than an absence.
//
// EVERY FIELD BELOW IS A COLUMN OF THAT MIGRATION or a value the server derives
// from two of them. `duration_ms` is derived and never stored; `error_class` is
// the exception class and never its message — the migration's
// `ck_nightly_step_runs_error_class_is_a_class` is what keeps a payload out.
// ---------------------------------------------------------------------------

/** `server/core/platform_clocks_api.py`, route `platform-nightly-steps`.
 *  Outside the `/clocks/` prefix on purpose: under it, `nightly-steps` is a
 *  valid clock NAME and the route would work only by declaration order. */
export const NIGHTLY_STEPS_ROOT = "/api/platform/nightly-steps";

export function nightlyStepsPath(): string {
  return NIGHTLY_STEPS_ROOT;
}

/**
 * The five states a declared step can be in, and no two of them mean the same
 * thing. The three that are not an outcome are the whole point:
 *
 *   unfinished     started, never closed — a crash, an OOM, a container
 *                  replaced mid-step. NOT a failure: nothing judged it.
 *   never_started  declared at dispatch and never begun. THE SILENCE.
 *   unrecorded     the sequence declares this step and the night holds no row
 *                  for it at all — the dispatch-time write did not land. "The
 *                  ledger did not record it" is not "it did not run".
 */
export type StepState =
  | "succeeded"
  | "failed"
  | "unfinished"
  | "never_started"
  | "unrecorded";

export const STEP_STATES: readonly StepState[] = [
  "succeeded",
  "failed",
  "unfinished",
  "never_started",
  "unrecorded",
];

export interface NightlyStep {
  step_name: string;
  step_ordinal: number;
  state: StepState;
  started_at: string | null;
  ended_at: string | null;
  /** Derived server-side from the two timestamps; `null` while the row is open. */
  duration_ms: number | null;
  /** The exception CLASS. Never a message, by CHECK constraint. */
  error_class: string | null;
  /** False for a step the night recorded but the sequence no longer declares.
   *  It ran; dropping it would rewrite what happened. */
  declared: boolean;
}

export interface NightlyRun {
  run_id: string;
  as_of_date: string | null;
  steps: NightlyStep[];
  /** Every step that is not a closed success, named by the server so the two
   *  surfaces cannot count it differently. */
  unresolved: string[];
}

export interface NightlyStepLedger {
  runs: NightlyRun[];
  declared_steps: string[];
  /** FALSE IS NOT "NOTHING WENT WRONG". It means the nightly has not run at all
   *  since this ledger existed, and the screen owes that its own state. */
  has_run: boolean;
}

function asStepState(value: unknown): StepState {
  // An unreadable state is `unrecorded`, never `succeeded`. The one thing this
  // screen must never do is render an unknown as agreement — the same rule the
  // clock verdicts follow, for the same reason.
  return typeof value === "string" && (STEP_STATES as readonly string[]).includes(value)
    ? (value as StepState)
    : "unrecorded";
}

function normalizeNightlyStep(input: unknown, fallbackOrdinal: number): NightlyStep | null {
  const raw = record(input);
  const stepName = text(raw.step_name);
  if (!stepName) return null;
  return {
    step_name: stepName,
    step_ordinal: count(raw.step_ordinal) ?? fallbackOrdinal,
    state: asStepState(raw.state),
    started_at: text(raw.started_at),
    ended_at: text(raw.ended_at),
    duration_ms: count(raw.duration_ms),
    error_class: text(raw.error_class),
    declared: raw.declared !== false,
  };
}

/** The ledger response. Never throws, never fabricates a step, and never turns
 *  an unreadable payload into an empty-but-healthy night. */
export function normalizeNightlySteps(input: unknown): NightlyStepLedger {
  const raw = record(input);
  const rawRuns = Array.isArray(raw.runs) ? raw.runs : [];
  const runs: NightlyRun[] = [];
  for (const rawRun of rawRuns) {
    const run = record(rawRun);
    const runId = text(run.run_id);
    if (!runId) continue;
    const steps: NightlyStep[] = [];
    const rawSteps = Array.isArray(run.steps) ? run.steps : [];
    rawSteps.forEach((rawStep, index) => {
      const step = normalizeNightlyStep(rawStep, index);
      if (step) steps.push(step);
    });
    runs.push({
      run_id: runId,
      as_of_date: text(run.as_of_date),
      steps,
      unresolved: Array.isArray(run.unresolved)
        ? run.unresolved.filter((name): name is string => typeof name === "string")
        : steps.filter((step) => step.state !== "succeeded").map((step) => step.step_name),
    });
  }
  const declared = Array.isArray(raw.declared_steps)
    ? raw.declared_steps.filter((name): name is string => typeof name === "string")
    : [];
  return {
    runs,
    declared_steps: declared,
    // Read from the payload when the server says it, otherwise derived from what
    // actually arrived. Never assumed true.
    has_run: typeof raw.has_run === "boolean" ? raw.has_run : runs.length > 0,
  };
}
