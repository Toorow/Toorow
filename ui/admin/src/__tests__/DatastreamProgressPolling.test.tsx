/**
 * Story 63.3 -- the poll stops when the run does.
 *
 * WHAT THESE TESTS ARE FOR. The poll's whole justification is what it does NOT
 * do: the service runs at `--min-instances=0` with no connection pool, so every
 * tick wakes an instance and opens a connection. A tab left open overnight at
 * five seconds is 17_280 wake-ups per surface. Every assertion below that
 * counts calls after advancing the clock is measuring that expense, not a
 * cosmetic detail.
 *
 * THE FETCH DOUBLE IS A PLAIN OBJECT, NOT `new Response(...)`. Under
 * `vi.useFakeTimers()` a real `Response.json()` may settle on a timer of the
 * platform's own, which the fake clock then owns -- a test that hangs for
 * reasons that have nothing to do with the subject. `{ok, status, json}` is
 * everything `apiJson` reads, and it settles on microtasks only.
 */
import { act, render, screen, within } from "@testing-library/react";
import {
  LIVE_INTERVAL_MS,
  MAX_ATTEMPTS,
  QUIET_AFTER_TICKS,
  QUIET_INTERVAL_MS,
  RETRY_BASE_MS,
  isRunMoving,
  useDatastreamProgress,
} from "../datastreams/workbench/datastreamProgress";
import { EXECUTION_STATES, executionState } from "../datastreams/workbench/executionStates";
import hookSource from "../datastreams/workbench/datastreamProgress?raw";
import scheduleSource from "../lib/polledRead?raw";

const PROJECT = "proj_EXAMPLE";
const DATASTREAM = "ds_EXAMPLE";

function running(overrides: Record<string, unknown> = {}) {
  return {
    schema: "datastream_progress.v1",
    project_id: PROJECT,
    datastream_id: DATASTREAM,
    progress: {
      execution_id: "dse_EXAMPLE",
      state: "loading",
      step: "Collect",
      day_in_progress: "2026-07-01",
      days_done: 31,
      days_total: 730,
      windows_done: 1,
      windows_total: 24,
      window_in_progress: { date_from: "2026-08-01", date_to: "2026-08-31", days: 31 },
      rows_written: 4200,
      started_at: "2026-08-06T00:00:00+00:00",
      progress_updated_at: "2026-08-06T00:04:00+00:00",
      plan_version_id: "dpv_EXAMPLE",
      mapping_version_id: "dmv_EXAMPLE",
      estimate: {
        armed: true,
        observations: 4,
        minimum_observations: 3,
        precision: "point",
        seconds_remaining: 2940,
        seconds_remaining_low: 1470,
        seconds_remaining_high: 4410,
        spread_ratio: 2,
        behind_by_seconds: 0,
        measured_at: "2026-08-06T00:04:00+00:00",
        reason: null,
        sentence: "About 49 minutes left.",
      },
      ...overrides,
    },
    idle: null,
  };
}

/** The same run, one tick later: only the estimate moved, because a clock did. */
function runningWithEstimate(secondsRemaining: number, measuredAt: string) {
  return running({
    estimate: {
      armed: true,
      observations: 4,
      minimum_observations: 3,
      precision: "point",
      seconds_remaining: secondsRemaining,
      seconds_remaining_low: secondsRemaining / 2,
      seconds_remaining_high: secondsRemaining * 2,
      spread_ratio: 2,
      behind_by_seconds: 0,
      measured_at: measuredAt,
      reason: null,
      sentence: `About ${Math.round(secondsRemaining / 60)} minutes left.`,
    },
  });
}

const _IDLE_STATE: Record<string, string | null> = {
  never_ran: null,
  last_run_succeeded: "collected",
  last_run_failed: "failed",
  // Story 63.6: a run somebody stopped. Terminal, and NOT a failure.
  last_run_stopped: "cancelled",
};

function nothingRunning(reason: string) {
  return {
    schema: "datastream_progress.v1",
    project_id: PROJECT,
    datastream_id: DATASTREAM,
    progress: null,
    idle: {
      reason,
      execution_id: reason === "never_ran" ? null : "dse_EXAMPLE",
      state: _IDLE_STATE[reason] ?? "collected",
      ended_at: reason === "never_ran" ? null : "2026-08-06T00:09:00+00:00",
      error_code:
        reason === "last_run_failed"
          ? "provider_refused"
          : reason === "last_run_stopped"
            ? "collection_run_stopped"
            : null,
      // What the run KEPT. After a stop this is the only place they exist.
      days_done: reason === "last_run_stopped" ? 31 : null,
      days_total: reason === "last_run_stopped" ? 730 : null,
      rows_written: reason === "last_run_stopped" ? 18_420 : null,
    },
  };
}

function ok(body: unknown): Response {
  return { ok: true, status: 200, json: () => Promise.resolve(body) } as unknown as Response;
}

function refused(status: number, code = "unavailable"): Response {
  return {
    ok: false,
    status,
    json: () => Promise.resolve({ code, message: `HTTP ${status}` }),
  } as unknown as Response;
}

let fetchMock: ReturnType<typeof vi.fn>;
let signals: AbortSignal[] = [];
let answer: (call: number) => Response | Promise<Response>;

function Probe({
  projectId = PROJECT as string | undefined,
  datastreamId = DATASTREAM as string | undefined,
}: {
  projectId?: string | undefined;
  datastreamId?: string | undefined;
}) {
  const poll = useDatastreamProgress(projectId, datastreamId);
  return (
    <div>
      <span data-testid="phase">{poll.phase}</span>
      <span data-testid="state">{poll.progress?.state ?? "none"}</span>
      <span data-testid="rows">{poll.progress?.rows_written ?? "none"}</span>
      <span data-testid="idle">{poll.idle?.reason ?? "none"}</span>
      <span data-testid="estimate">{poll.progress?.estimate?.sentence ?? "none"}</span>
      <span data-testid="remaining">{poll.progress?.estimate?.seconds_remaining ?? "none"}</span>
      <span data-testid="spread">{poll.progress?.estimate?.spread_ratio ?? "none"}</span>
      <span data-testid="measured-at">{poll.measuredAt ?? "never"}</span>
      <span data-testid="age">
        {poll.measuredAt === null ? "never" : Date.now() - poll.measuredAt}
      </span>
      <span data-testid="error">{poll.error ? `${poll.error.status}` : "none"}</span>
      <span data-testid="offline">{poll.error?.offline ? "yes" : "no"}</span>
      <span data-testid="attempts">{poll.attempts}</span>
      {/* The one way out of a poll that has stopped -- and what the band calls
          after a successful stop, so the switch does not wait for a tick. */}
      <button type="button" data-testid="refresh" onClick={poll.refresh}>
        Refresh
      </button>
    </div>
  );
}

async function flush(): Promise<void> {
  await act(async () => {
    for (let i = 0; i < 8; i += 1) await Promise.resolve();
  });
}

async function advance(ms: number): Promise<void> {
  await act(async () => {
    vi.advanceTimersByTime(ms);
    for (let i = 0; i < 8; i += 1) await Promise.resolve();
  });
}

function setVisibility(value: "visible" | "hidden"): void {
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => value });
}

async function switchVisibility(value: "visible" | "hidden"): Promise<void> {
  setVisibility(value);
  await act(async () => {
    document.dispatchEvent(new Event("visibilitychange"));
    for (let i = 0; i < 8; i += 1) await Promise.resolve();
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  signals = [];
  answer = () => ok(running());
  fetchMock = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
    signals.push(init?.signal as AbortSignal);
    return Promise.resolve(answer(fetchMock.mock.calls.length));
  });
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  setVisibility("visible");
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

test("reads once on mount, and says when that number was measured", async () => {
  render(<Probe />);
  await flush();

  expect(fetchMock).toHaveBeenCalledTimes(1);
  expect(screen.getByTestId("phase")).toHaveTextContent("polling");
  expect(screen.getByTestId("state")).toHaveTextContent("loading");
  expect(screen.getByTestId("rows")).toHaveTextContent("4200");
  expect(screen.getByTestId("measured-at").textContent).not.toBe("never");
  expect(screen.getByTestId("age")).toHaveTextContent("0");
});

test("keeps reading while the run's state is active in the registry", async () => {
  render(<Probe />);
  await flush();
  expect(fetchMock).toHaveBeenCalledTimes(1);

  await advance(LIVE_INTERVAL_MS);
  expect(fetchMock).toHaveBeenCalledTimes(2);

  await advance(LIVE_INTERVAL_MS);
  expect(fetchMock).toHaveBeenCalledTimes(3);
  expect(screen.getByTestId("phase")).toHaveTextContent("polling");
});

test("slows to the quiet cadence after three answers that changed nothing", async () => {
  render(<Probe />);
  await flush();

  // Three identical answers after the first: nothing has moved.
  for (let i = 0; i < QUIET_AFTER_TICKS; i += 1) await advance(LIVE_INTERVAL_MS);
  expect(fetchMock).toHaveBeenCalledTimes(1 + QUIET_AFTER_TICKS);

  // The live cadence is no longer armed...
  await advance(LIVE_INTERVAL_MS);
  expect(fetchMock).toHaveBeenCalledTimes(1 + QUIET_AFTER_TICKS);
  // ...the quiet one is.
  await advance(QUIET_INTERVAL_MS - LIVE_INTERVAL_MS);
  expect(fetchMock).toHaveBeenCalledTimes(2 + QUIET_AFTER_TICKS);

  // A payload that moves again puts the live cadence back.
  answer = () => ok(running({ rows_written: 9001 }));
  await advance(QUIET_INTERVAL_MS);
  expect(fetchMock).toHaveBeenCalledTimes(3 + QUIET_AFTER_TICKS);
  await advance(LIVE_INTERVAL_MS);
  expect(fetchMock).toHaveBeenCalledTimes(4 + QUIET_AFTER_TICKS);
});

test("stops for good when the answer says nothing is running, and says what it stopped on", async () => {
  answer = () => ok(nothingRunning("last_run_failed"));
  render(<Probe />);
  await flush();

  expect(fetchMock).toHaveBeenCalledTimes(1);
  expect(screen.getByTestId("phase")).toHaveTextContent("stopped");
  expect(screen.getByTestId("idle")).toHaveTextContent("last_run_failed");

  // Far beyond several intervals of either cadence: the counter is frozen.
  await advance(QUIET_INTERVAL_MS * 40);
  expect(fetchMock).toHaveBeenCalledTimes(1);
});

test("a stop moves the poll to idle and never starts it again", async () => {
  // Story 63.6. The run is moving, then somebody stops it: the very next answer
  // says nothing is running, WITH its own reason. The poll must end there --
  // restarting after a stop would keep waking an instance at min-instances=0
  // for a run a person deliberately ended.
  let stopped = false;
  answer = () => (stopped ? ok(nothingRunning("last_run_stopped")) : ok(running()));
  render(<Probe />);
  await flush();
  expect(screen.getByTestId("phase")).toHaveTextContent("polling");

  stopped = true;
  await advance(LIVE_INTERVAL_MS);
  expect(fetchMock).toHaveBeenCalledTimes(2);
  expect(screen.getByTestId("phase")).toHaveTextContent("stopped");
  // Its OWN reason: a stop reported as `last_run_failed` would fire the failure
  // sentences on all three surfaces of 63.5.
  expect(screen.getByTestId("idle")).toHaveTextContent("last_run_stopped");

  await advance(QUIET_INTERVAL_MS * 40);
  expect(fetchMock).toHaveBeenCalledTimes(2);
});

test("a refreshed poll after a stop reads ONCE, and stops again", async () => {
  // What `poll.refresh()` buys the band: the switch happens without waiting for
  // the next tick, and it does not restart a cadence.
  answer = () => ok(nothingRunning("last_run_stopped"));
  render(<Probe />);
  await flush();
  expect(fetchMock).toHaveBeenCalledTimes(1);

  await act(async () => {
    screen.getByTestId("refresh").click();
    for (let i = 0; i < 8; i += 1) await Promise.resolve();
  });
  expect(fetchMock).toHaveBeenCalledTimes(2);
  expect(screen.getByTestId("idle")).toHaveTextContent("last_run_stopped");

  await advance(QUIET_INTERVAL_MS * 40);
  expect(fetchMock).toHaveBeenCalledTimes(2);
});

test("stops on a stored state the registry does not call active", async () => {
  const overStates = EXECUTION_STATES.filter((entry) => entry.stored && !entry.active).map(
    (entry) => entry.name,
  );
  expect(overStates).toEqual(expect.arrayContaining(["collected", "failed", "cancelled"]));

  for (const state of overStates) {
    answer = () => ok(running({ state }));
    fetchMock.mockClear();
    const view = render(<Probe />);
    await flush();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("phase")).toHaveTextContent("stopped");
    // The last true measurement is kept -- it is not wiped because it is final.
    expect(screen.getByTestId("state")).toHaveTextContent(state);

    await advance(QUIET_INTERVAL_MS * 10);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    view.unmount();
  }
});

test("never reads a state it does not know as still moving", async () => {
  // A server newer than the console is the real case: it ships a state this
  // build has never heard of. Reading the unknown as "active" would keep the
  // poll alive forever on a run that ended; reading it as "moving" anywhere
  // would let the console claim an outcome it was never told. `phaseOf` already
  // paints an unknown state `todo`; the stop condition must agree with it.
  const invented = "collecting_v2";
  expect(EXECUTION_STATES.some((entry) => entry.name === invented)).toBe(false);
  expect(executionState(invented)).toBeUndefined();
  expect(isRunMoving(invented)).toBe(false);
  expect(isRunMoving(undefined)).toBe(false);
  expect(isRunMoving(null)).toBe(false);
  expect(isRunMoving("")).toBe(false);

  answer = () => ok(running({ state: invented }));
  render(<Probe />);
  await flush();

  expect(fetchMock).toHaveBeenCalledTimes(1);
  expect(screen.getByTestId("phase")).toHaveTextContent("stopped");
  // The payload is kept -- refusing to guess is not a reason to lose it.
  expect(screen.getByTestId("state")).toHaveTextContent(invented);

  await advance(QUIET_INTERVAL_MS * 40);
  expect(fetchMock).toHaveBeenCalledTimes(1);
});

test("aborts the read in flight on unmount and never calls again", async () => {
  answer = () => new Promise<Response>(() => undefined);
  const view = render(<Probe />);
  await flush();

  expect(fetchMock).toHaveBeenCalledTimes(1);
  expect(signals[0].aborted).toBe(false);

  view.unmount();
  expect(signals[0].aborted).toBe(true);

  await advance(QUIET_INTERVAL_MS * 10);
  expect(fetchMock).toHaveBeenCalledTimes(1);
});

test("does not poll a hidden tab, and reads exactly once on coming back", async () => {
  setVisibility("hidden");
  render(<Probe />);
  await flush();
  expect(fetchMock).toHaveBeenCalledTimes(0);

  await switchVisibility("visible");
  expect(fetchMock).toHaveBeenCalledTimes(1);

  // Hidden again while a run is moving: the armed tick is disarmed, and a
  // machine that sleeps through ten intervals owes nothing on waking.
  await switchVisibility("hidden");
  await advance(QUIET_INTERVAL_MS * 10);
  expect(fetchMock).toHaveBeenCalledTimes(1);

  // Back: ONE read, immediately -- never the ticks that were missed.
  await switchVisibility("visible");
  expect(fetchMock).toHaveBeenCalledTimes(2);
});

test("a network failure keeps the last payload AND its age, and never presents it as current", async () => {
  render(<Probe />);
  await flush();
  const measuredAt = screen.getByTestId("measured-at").textContent;
  expect(measuredAt).not.toBe("never");

  answer = () => Promise.reject(new TypeError("Failed to fetch"));
  await advance(LIVE_INTERVAL_MS);

  expect(screen.getByTestId("phase")).toHaveTextContent("error");
  expect(screen.getByTestId("error")).toHaveTextContent("0");
  expect(screen.getByTestId("offline")).toHaveTextContent("yes");
  // The figures survive -- with the instant they were measured, unchanged, so
  // the screen can say how old they are instead of implying they are fresh.
  expect(screen.getByTestId("rows")).toHaveTextContent("4200");
  expect(screen.getByTestId("measured-at").textContent).toBe(measuredAt);
  expect(screen.getByTestId("age")).toHaveTextContent(String(LIVE_INTERVAL_MS));
});

test("backs off exponentially on 5xx, five attempts, then stops", async () => {
  answer = () => refused(503);
  render(<Probe />);
  await flush();

  expect(fetchMock).toHaveBeenCalledTimes(1);
  expect(screen.getByTestId("error")).toHaveTextContent("503");
  expect(screen.getByTestId("offline")).toHaveTextContent("no");

  for (let attempt = 1; attempt < MAX_ATTEMPTS; attempt += 1) {
    // Nothing before the backoff step has elapsed...
    await advance(RETRY_BASE_MS * 2 ** (attempt - 1) - 1);
    expect(fetchMock).toHaveBeenCalledTimes(attempt);
    // ...and exactly one read when it has.
    await advance(1);
    expect(fetchMock).toHaveBeenCalledTimes(attempt + 1);
  }

  expect(screen.getByTestId("attempts")).toHaveTextContent(String(MAX_ATTEMPTS));
  await advance(RETRY_BASE_MS * 2 ** MAX_ATTEMPTS);
  expect(fetchMock).toHaveBeenCalledTimes(MAX_ATTEMPTS);
});

// ---------------------------------------------------------------------------
// Story 63.4 -- the estimate rides along, and pays for nothing.
// ---------------------------------------------------------------------------

test("the estimate neither restarts the poll nor holds it at the live cadence", async () => {
  // The estimate carries the server instant it was taken and a countdown off
  // it, so it is DIFFERENT on every tick by construction. If it were part of
  // "did this answer change", the poll would never slow down: 17_280 wake-ups a
  // day per surface instead of 5_760, for a number nobody can act on.
  let ticks = 0;
  answer = () => {
    ticks += 1;
    return ok(runningWithEstimate(2940 - ticks * 5, `2026-08-06T00:0${ticks}:00+00:00`));
  };

  render(<Probe />);
  await flush();
  expect(screen.getByTestId("estimate")).toHaveTextContent("minutes left");

  for (let i = 0; i < QUIET_AFTER_TICKS; i += 1) await advance(LIVE_INTERVAL_MS);
  expect(fetchMock).toHaveBeenCalledTimes(1 + QUIET_AFTER_TICKS);

  // The measured numbers repeated themselves, so the quiet cadence is armed --
  // even though every single answer carried a different estimate.
  await advance(LIVE_INTERVAL_MS);
  expect(fetchMock).toHaveBeenCalledTimes(1 + QUIET_AFTER_TICKS);
  await advance(QUIET_INTERVAL_MS - LIVE_INTERVAL_MS);
  expect(fetchMock).toHaveBeenCalledTimes(2 + QUIET_AFTER_TICKS);
});

test("a failed tick keeps the last estimate AND its age, and refreshes neither", async () => {
  render(<Probe />);
  await flush();
  const measuredAt = screen.getByTestId("measured-at").textContent;
  expect(screen.getByTestId("estimate")).toHaveTextContent("About 49 minutes left.");

  answer = () => refused(503);
  await advance(LIVE_INTERVAL_MS);

  expect(screen.getByTestId("phase")).toHaveTextContent("error");
  // The sentence survives, unchanged, with the instant it was measured -- a
  // countdown that kept ticking down on a dead poll would be a lie no reader
  // could detect.
  expect(screen.getByTestId("estimate")).toHaveTextContent("About 49 minutes left.");
  expect(screen.getByTestId("measured-at").textContent).toBe(measuredAt);
  expect(screen.getByTestId("age")).toHaveTextContent(String(LIVE_INTERVAL_MS));
});

test("an answer with numbers but no sentence is not an estimate", async () => {
  // The ratified contract is that the block always says something. A body that
  // carries a duration and no words would be rendered as a bare number, which
  // is the one thing the silence rules exist to prevent.
  answer = () =>
    ok(running({ estimate: { armed: true, seconds_remaining: 600, sentence: null } }));
  render(<Probe />);
  await flush();

  expect(screen.getByTestId("estimate")).toHaveTextContent("none");
  expect(screen.getByTestId("phase")).toHaveTextContent("polling");
});

test("a run that outran its sample reads as a sentence, never as a countdown", async () => {
  // The defect a `max(0, ...)` clamp shipped once: a window stalled for 48 hours
  // answering "Less than a minute left." The console must carry the refusal AND
  // its dispersion through unchanged -- it may not fill either one in.
  answer = () =>
    ok(
      running({
        estimate: {
          armed: false,
          observations: 4,
          minimum_observations: 3,
          precision: null,
          seconds_remaining: null,
          seconds_remaining_low: null,
          seconds_remaining_high: null,
          spread_ratio: 1.2,
          behind_by_seconds: 172_800,
          measured_at: "2026-08-06T00:04:00+00:00",
          reason: "running_longer_than_measured",
          sentence:
            "This window has been running 2 days longer than anything measured " +
            "for this Datastream — no estimate.",
        },
      }),
    );
  const view = render(<Probe />);
  await flush();

  expect(screen.getByTestId("estimate")).toHaveTextContent("longer than anything measured");
  expect(screen.getByTestId("remaining")).toHaveTextContent("none");
  expect(screen.getByTestId("spread")).toHaveTextContent("1.2");
  view.unmount();
});

test("a scattered sample reaches the screen as a band, not as one number", async () => {
  answer = () =>
    ok(
      running({
        estimate: {
          armed: true,
          observations: 3,
          minimum_observations: 3,
          precision: "range",
          seconds_remaining: null,
          seconds_remaining_low: 100,
          seconds_remaining_high: 12_000,
          spread_ratio: 120,
          behind_by_seconds: 0,
          measured_at: "2026-08-06T00:04:00+00:00",
          reason: null,
          sentence: "Between 2 minutes and 3 hours 20 minutes left.",
        },
      }),
    );
  render(<Probe />);
  await flush();

  expect(screen.getByTestId("estimate")).toHaveTextContent("Between 2 minutes and");
  // No point value to be mistaken for a measurement.
  expect(screen.getByTestId("remaining")).toHaveTextContent("none");
  expect(screen.getByTestId("spread")).toHaveTextContent("120");
});

test("the console computes no duration of its own -- the server owns the number", () => {
  // Two surfaces read this payload: this console and the MCP. An arithmetic
  // here would be an answer the tools cannot see.
  const code = codeOf(hookSource);
  expect(code).not.toContain("days_total -");
  expect(code).not.toContain("Date.now()");
  expect(code).toContain("seconds_remaining");
});

/** A screen whose scope has not resolved yet. `undefined` as a prop would hit
 *  `Probe`'s default parameter and silently poll the example ids instead. */
function UnscopedProbe() {
  const poll = useDatastreamProgress(undefined, DATASTREAM);
  return <span data-testid="phase">{poll.phase}</span>;
}

/** A source with its comments removed -- the guard judges CODE, not prose. */
function codeOf(source: string): string {
  return source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:])\/\/.*$/gm, "$1");
}

test("reads its stop condition from the registry, never from a state name typed here", () => {
  // `WorkbenchRunsPage` once carried its own opinion about run states, and when
  // migration 218 added `collected` it painted a finished run as "waiting". A
  // poll with the same habit would keep asking about a run that is over.
  const code = codeOf(hookSource);
  const typed = EXECUTION_STATES.map((entry) => entry.name).filter((name) =>
    new RegExp(`["'\`]${name}["'\`]`).test(code),
  );
  expect(typed, `state name(s) typed into the poll: ${typed.join(", ")}`).toEqual([]);
  expect(code).toContain('from "./executionStates"');
  // The shared scheduler must not know a domain state either: it decides WHEN
  // to read, never what an answer means.
  expect(codeOf(scheduleSource)).not.toContain("executionState");
});

test("opens no permanent connection, and no repeating interval -- the service scales to zero", () => {
  // Both files: the cadence moved to the shared module, and a guard that keeps
  // watching the file the code LEFT proves nothing.
  for (const source of [hookSource, scheduleSource]) {
    const code = codeOf(source);
    for (const forbidden of ["EventSource", "WebSocket", "text/event-stream", "setInterval("]) {
      expect(code).not.toContain(forbidden);
    }
  }
  expect(codeOf(scheduleSource)).toContain("setTimeout(");
});

// ---------------------------------------------------------------------------
// Story 63.5 -- one address, one reading, however many surfaces watch it.
// ---------------------------------------------------------------------------

test("two surfaces watching one Datastream share ONE read and ONE timer", async () => {
  // The Workbench object header and the `Runs` tab render from one component,
  // so on `…/runs` they are mounted together. Two polls of one address is
  // double the wake-ups of a service at `--min-instances=0` -- and, worse, two
  // answers.
  render(
    <>
      <Probe />
      <Probe />
    </>,
  );
  await flush();
  expect(fetchMock).toHaveBeenCalledTimes(1);

  await advance(LIVE_INTERVAL_MS);
  expect(fetchMock).toHaveBeenCalledTimes(2);

  const [first, second] = screen.getAllByTestId("measured-at").map((node) => node.textContent);
  expect(first).toBe(second);
  const rows = screen.getAllByTestId("rows").map((node) => node.textContent);
  expect(rows[0]).toBe(rows[1]);
});

test("a surface mounted late joins the reading instead of opening a second one", async () => {
  // THE DEFECT THIS CLOSES, exactly as it happened. Each mount counted its own
  // repeats, so the one mounted first dropped to the quiet cadence while the
  // one mounted later was still reading every five seconds: two numbers
  // measured up to fifteen seconds apart, each of them exact, side by side on
  // one screen.
  let reads = 0;
  answer = () => {
    reads += 1;
    return ok(running({ rows_written: reads * 100 }));
  };

  const first = render(<Probe />);
  await flush();
  await advance(LIVE_INTERVAL_MS);
  expect(fetchMock).toHaveBeenCalledTimes(2);
  const settled = screen.getByTestId("rows").textContent;
  const measuredAt = screen.getByTestId("measured-at").textContent;

  // The second surface appears while the first is mid-run.
  const late = render(<Probe />);
  await flush();

  // No read of its own...
  expect(fetchMock).toHaveBeenCalledTimes(2);
  // ...and the same number, measured at the same instant, as the first.
  const lateView = within(late.container);
  expect(lateView.getByTestId("rows").textContent).toBe(settled);
  expect(lateView.getByTestId("measured-at").textContent).toBe(measuredAt);
  first.unmount();
  late.unmount();
});

test("the reading ends with the last surface watching it", async () => {
  // A timer that outlives every screen watching it is the overnight wake-up
  // this module exists to prevent -- and a snapshot kept past its screen would
  // answer the next mount with figures measured before it existed.
  const first = render(<Probe />);
  const second = render(<Probe />);
  await flush();
  expect(fetchMock).toHaveBeenCalledTimes(1);

  first.unmount();
  await advance(LIVE_INTERVAL_MS);
  // Still one surface watching: the reading goes on.
  expect(fetchMock).toHaveBeenCalledTimes(2);

  second.unmount();
  await advance(QUIET_INTERVAL_MS * 10);
  expect(fetchMock).toHaveBeenCalledTimes(2);

  // A later mount starts a reading of its own rather than inheriting an old one.
  render(<Probe />);
  await flush();
  expect(fetchMock).toHaveBeenCalledTimes(3);
});

test("polls nothing at all until both ids are known", async () => {
  render(<UnscopedProbe />);
  await flush();
  await advance(QUIET_INTERVAL_MS * 4);
  expect(fetchMock).toHaveBeenCalledTimes(0);
  expect(screen.getByTestId("phase")).toHaveTextContent("idle");
});
