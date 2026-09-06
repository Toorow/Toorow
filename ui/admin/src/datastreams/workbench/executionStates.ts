/**
 * The console's mirror of `server/core/execution_states.py` -- story 63.1.
 *
 * WHY A MIRROR AND NOT A LOCAL GUESS. `WorkbenchRunsPage.phaseState` used to
 * carry its own three `if` lines over run states. When migration 218 added
 * `collected`, that function fell through to `todo` and a run that had
 * collected every one of its windows was painted amber and "waiting" -- while
 * the server considered it finished and successful. The console cannot be
 * allowed a second opinion about what a state means.
 *
 * The table below is compared, entry by entry, against the Python registry by
 * `server/tests/conformance/test_execution_state_registry.py`. Adding a state on
 * one side without the other is a red test, not a screen that lies.
 */

export type ExecutionPhase = "todo" | "running" | "done" | "failed";
export type OperationsAxis = "Unknown" | "Healthy" | "Degraded" | "Blocked";

export type ExecutionStateEntry = {
  readonly name: string;
  /** May `app.datastream_executions.state` hold this value? */
  readonly stored: boolean;
  /** Does it hold the one-non-terminal-execution-per-Datastream lock? */
  readonly active: boolean;
  readonly terminal: boolean;
  /** Did the work finish successfully? Drives the 30-day success rate. */
  readonly success: boolean;
  /** Is it a candidate a person is expected to review? */
  readonly candidate: boolean;
  readonly axis: OperationsAxis;
  readonly phase: ExecutionPhase;
};

export const EXECUTION_STATES: readonly ExecutionStateEntry[] = [
  { name: "created", stored: true, active: true, terminal: false, success: false, candidate: true, axis: "Unknown", phase: "todo" },
  { name: "loading", stored: true, active: true, terminal: false, success: false, candidate: true, axis: "Unknown", phase: "running" },
  { name: "validating", stored: true, active: true, terminal: false, success: false, candidate: true, axis: "Unknown", phase: "running" },
  { name: "ready", stored: true, active: true, terminal: false, success: false, candidate: true, axis: "Healthy", phase: "running" },
  { name: "publishing", stored: true, active: true, terminal: false, success: false, candidate: true, axis: "Unknown", phase: "running" },
  { name: "published", stored: true, active: false, terminal: true, success: true, candidate: false, axis: "Healthy", phase: "done" },
  { name: "collected", stored: true, active: false, terminal: true, success: true, candidate: false, axis: "Healthy", phase: "done" },
  { name: "failed", stored: true, active: false, terminal: true, success: false, candidate: false, axis: "Blocked", phase: "failed" },
  { name: "cancelled", stored: true, active: false, terminal: true, success: false, candidate: false, axis: "Degraded", phase: "failed" },
  { name: "succeeded", stored: false, active: false, terminal: true, success: true, candidate: false, axis: "Healthy", phase: "done" },
  { name: "outcome_unknown", stored: false, active: false, terminal: true, success: false, candidate: false, axis: "Blocked", phase: "failed" },
  { name: "blocked", stored: false, active: false, terminal: true, success: false, candidate: false, axis: "Blocked", phase: "failed" },
  { name: "partial", stored: false, active: false, terminal: true, success: false, candidate: false, axis: "Degraded", phase: "failed" },
  { name: "drifted", stored: false, active: false, terminal: true, success: false, candidate: false, axis: "Degraded", phase: "failed" },
];

const BY_NAME = new Map(EXECUTION_STATES.map((entry) => [entry.name, entry]));

export function executionState(value: unknown): ExecutionStateEntry | undefined {
  return BY_NAME.get(String(value ?? "").toLowerCase());
}

/**
 * What the timeline paints. An unknown name reads `todo` -- a state this build
 * does not know has not been shown to have finished, and claiming otherwise
 * would be the console inventing an outcome.
 */
export function phaseOf(value: unknown): ExecutionPhase {
  return executionState(value)?.phase ?? "todo";
}

/** A run still moving is neither a success nor a failure. */
export function isTerminal(value: unknown): boolean {
  return executionState(value)?.terminal ?? false;
}

/** Did this run finish its work? `collected` counts, and that is the point. */
export function isSuccess(value: unknown): boolean {
  return executionState(value)?.success ?? false;
}

/** The label a person reads, never the raw wire value. */
export function executionStateLabel(value: unknown): string {
  const name = executionState(value)?.name;
  if (!name) return "Unknown";
  return name
    .split("_")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}
