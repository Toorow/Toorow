/**
 * The live state of one collection, block by block -- story 63.5.
 *
 * The component takes the poll as a PROP, so every case below is exercised
 * without a clock and without a network: what is tested here is the reading of
 * a payload, and `DatastreamRunLiveParity.test.tsx` tests that the three
 * surfaces mount this same file over one reading.
 *
 * Six empty states are pinned here, because each one shipped as a silence
 * somewhere: the three `idle` answers of story 63.2 (a Datastream that never
 * ran, a last run that finished well, one that ended badly) and the estimate
 * that declines (story 63.4, four reasons, one of them a window that has
 * outrun everything ever measured).
 */
import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import DatastreamRunLive, {
  DatastreamRunStateBadge,
  NOT_MEASURED,
  toneForPhase,
} from "../datastreams/workbench/DatastreamRunLive";
import type {
  DatastreamProgress,
  DatastreamProgressPoll,
} from "../datastreams/workbench/datastreamProgress";
import { EXECUTION_STATES } from "../datastreams/workbench/executionStates";
import { RUN_ORIGINS, originSentence } from "../datastreams/workbench/runOrigins";
import runLiveSource from "../datastreams/workbench/DatastreamRunLive?raw";

const MEASURED_AT = 1_772_000_000_000;

function progress(overrides: Partial<DatastreamProgress> = {}): DatastreamProgress {
  return {
    execution_id: "dse_EXAMPLE",
    state: "loading",
    // Story 63.7: a nightly collection -- the origin that reads provider
    // windows, so the day count and its bar are exactly what this run has.
    origin: "scheduler_nightly",
    step: "Collect",
    day_in_progress: "2026-07-01",
    days_done: 31,
    days_total: 90,
    windows_done: 1,
    windows_total: 3,
    window_in_progress: {
      date_from: "2026-08-01",
      date_to: "2026-08-31",
      days: 31,
      started_at: "2026-08-06T00:00:00+00:00",
      completed_at: null,
    },
    rows_written: 412_000,
    started_at: "2026-08-06T00:00:00+00:00",
    progress_updated_at: "2026-08-06T00:04:00+00:00",
    plan_version_id: "dpv_EXAMPLE",
    mapping_version_id: "dmv_EXAMPLE",
    estimate: {
      armed: true,
      observations: 4,
      minimum_observations: 3,
      precision: "point",
      seconds_remaining: 2_940,
      seconds_remaining_low: 1_470,
      seconds_remaining_high: 4_410,
      spread_ratio: 2,
      behind_by_seconds: 0,
      measured_at: "2026-08-06T00:04:00+00:00",
      reason: null,
      sentence: "About 49 minutes left.",
    },
    ...overrides,
  };
}

function poll(overrides: Partial<DatastreamProgressPoll> = {}): DatastreamProgressPoll {
  return {
    phase: "polling",
    progress: progress(),
    idle: null,
    measuredAt: MEASURED_AT,
    error: null,
    attempts: 0,
    stopped: false,
    refresh: vi.fn(),
    ...overrides,
  };
}

function idlePoll(
  reason: "never_ran" | "last_run_succeeded" | "last_run_failed",
  state: string | null,
  errorCode: string | null = null,
): DatastreamProgressPoll {
  return poll({
    phase: "stopped",
    progress: null,
    stopped: true,
    idle: {
      reason,
      execution_id: reason === "never_ran" ? null : "dse_EXAMPLE",
      state,
      ended_at: reason === "never_ran" ? null : "2026-08-06T00:09:00+00:00",
      error_code: errorCode,
      // Story 63.6: what the run kept. `null` here, because these cases are
      // about the SENTENCE; the numbers have their own tests in
      // `DatastreamRunStop.test.tsx`.
      days_done: null,
      days_total: null,
      rows_written: null,
    },
  });
}

/** A source with its comments removed -- the guards judge CODE, not prose. */
function codeOf(source: string): string {
  return source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:])\/\/.*$/gm, "$1");
}

describe("DatastreamRunLive -- the six blocks", () => {
  it("shows the run's state, step, days, rows, time left and the age of the reading", () => {
    vi.useFakeTimers();
    vi.setSystemTime(MEASURED_AT);
    render(<DatastreamRunLive poll={poll()} />);
    const band = within(screen.getByTestId("datastream-run-live"));

    // 0. WHY it is running, at the head -- story 63.7. It is the first question
    // somebody asks in front of a run they did not start.
    expect(band.getByTestId("run-live-origin")).toHaveTextContent(
      /^Nightly collection, started /,
    );
    // 1. the state, through the registry's own label -- never the wire value.
    expect(band.getByText("Loading")).toBeInTheDocument();
    // 2. the step this run named for itself.
    expect(band.getByText("Collect")).toBeInTheDocument();
    // 3. the day count, and the window that explains why it moves in steps.
    expect(band.getByText("31 of 90 days collected")).toBeInTheDocument();
    expect(band.getByText(/A 31-day window is in flight \(2026-08-01 to 2026-08-31\)\./)).toBeInTheDocument();
    // The bar carries the fraction, and it ANNOUNCES it: 31 of 90 is 34%. The
    // primitive dropped `value` before it reached its root until 2026-08-06, so
    // this bar -- whose whole content is its value -- read as indeterminate to
    // a screen reader while showing an exact fill to everybody else.
    const bar = band.getByTestId("run-live-days");
    expect(bar).toHaveAttribute("role", "progressbar");
    expect(bar).toHaveAttribute("aria-valuenow", "34");
    // 4. rows, named for what they are: rows COLLECTED, not rows published.
    expect(band.getByText("Rows collected")).toBeInTheDocument();
    expect(band.getByText("412,000")).toBeInTheDocument();
    // 5. the server's sentence, with the sample it stands on.
    expect(band.getByTestId("run-live-estimate")).toHaveTextContent("About 49 minutes left.");
    expect(band.getByText(/Measured on 4 finished run\(s\); 3 are needed\./)).toBeInTheDocument();
    // 6. the age of the reading, beside the numbers it dates.
    expect(band.getByTestId("run-live-measured")).toHaveTextContent(/^Read 0 s ago, at /);
    vi.useRealTimers();
  });

  it("renders a run that is moving as active, and an unknown state as neither", () => {
    const invented = "collecting_v2";
    expect(EXECUTION_STATES.some((entry) => entry.name === invented)).toBe(false);

    // `Status active` is the breathing halo AND `role="status"` -- "this is
    // changing under you", rather than "this is the state".
    const view = render(<DatastreamRunStateBadge state="loading" />);
    expect(screen.getByText("Loading").closest("[role='status']")).not.toBeNull();
    view.unmount();

    // A state this build has never heard of is not painted as a run in flight:
    // a console older than its server must not claim an outcome, in either
    // direction.
    render(<DatastreamRunStateBadge state={invented} />);
    expect(screen.getByText("Unknown")).toBeInTheDocument();
    expect(screen.getByText("Unknown").closest("[role='status']")).toBeNull();
  });

  it("says a field was not measured instead of printing a zero", () => {
    render(
      <DatastreamRunLive
        poll={poll({
          progress: progress({
            rows_written: null,
            days_done: null,
            days_total: null,
            window_in_progress: null,
            step: null,
          }),
        })}
      />,
    );
    const band = within(screen.getByTestId("datastream-run-live"));
    expect(band.getByText(NOT_MEASURED)).toBeInTheDocument();
    expect(band.getByText("This run has not said how many days it covers yet.")).toBeInTheDocument();
    expect(band.getByText("No window is in flight.")).toBeInTheDocument();
    expect(band.getByText("This run has not named a step yet.")).toBeInTheDocument();
    // The defect this rule exists for: a `0` is a measurement, and none of
    // these was made.
    expect(band.queryByText("0")).not.toBeInTheDocument();
    expect(band.queryByText("0 of 0 days collected")).not.toBeInTheDocument();
  });

  it("carries the estimate's silence through, and never fills it in", () => {
    render(
      <DatastreamRunLive
        poll={poll({
          progress: progress({
            estimate: {
              armed: false,
              observations: 1,
              minimum_observations: 3,
              precision: null,
              seconds_remaining: null,
              seconds_remaining_low: null,
              seconds_remaining_high: null,
              spread_ratio: null,
              behind_by_seconds: null,
              measured_at: "2026-08-06T00:04:00+00:00",
              reason: "not_enough_history",
              sentence: "1 finished run measured; 3 are needed before an estimate.",
            },
          }),
        })}
      />,
    );
    expect(screen.getByTestId("run-live-estimate")).toHaveTextContent(
      "1 finished run measured; 3 are needed before an estimate.",
    );
    // The count AND the minimum are disclosed, exactly as the anomaly detector
    // discloses its own bound.
    expect(screen.getByText(/Measured on 1 finished run\(s\); 3 are needed\./)).toBeInTheDocument();
  });

  it("carries the fourth silence -- a window that outran everything measured", () => {
    render(
      <DatastreamRunLive
        poll={poll({
          progress: progress({
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
        })}
      />,
    );
    expect(screen.getByTestId("run-live-estimate")).toHaveTextContent("longer than anything measured");
    // The defect a `max(0, …)` clamp shipped once: "Less than a minute left."
    // forever, on the screen a person opens when something is wrong.
    expect(screen.queryByText(/Less than a minute/)).not.toBeInTheDocument();
  });

  it("says nothing is estimated at all when the server sends no estimate", () => {
    render(<DatastreamRunLive poll={poll({ progress: progress({ estimate: null }) })} />);
    expect(screen.getByText("This server sends no estimate for a run in flight.")).toBeInTheDocument();
  });
});

describe("DatastreamRunLive -- the three silences of an idle Datastream", () => {
  it("says a Datastream has never run", () => {
    render(<DatastreamRunLive poll={idlePoll("never_ran", null)} />);
    expect(screen.getByTestId("run-live-idle")).toHaveTextContent("This Datastream has never run.");
  });

  it("names the last run that finished well, with its state and its instant", () => {
    render(<DatastreamRunLive poll={idlePoll("last_run_succeeded", "collected")} />);
    const idle = screen.getByTestId("run-live-idle");
    expect(idle).toHaveTextContent("Nothing is collecting.");
    expect(idle).toHaveTextContent("The last run ended as Collected on");
    expect(idle).not.toHaveTextContent("never run");
  });

  it("names the failure AND its code, and never folds Cancelled into Failed", () => {
    const view = render(<DatastreamRunLive poll={idlePoll("last_run_failed", "failed", "provider_refused")} />);
    expect(screen.getByTestId("run-live-idle")).toHaveTextContent("The last run ended as Failed on");
    expect(screen.getByTestId("run-live-idle")).toHaveTextContent("It reported provider_refused.");
    view.unmount();

    // `cancelled` arrives under the same REASON and must not read as a failure:
    // the exact state travels with it precisely so the screen can tell them
    // apart.
    render(<DatastreamRunLive poll={idlePoll("last_run_failed", "cancelled", null)} />);
    expect(screen.getByTestId("run-live-idle")).toHaveTextContent("The last run ended as Cancelled on");
    expect(screen.getByTestId("run-live-idle")).not.toHaveTextContent("Failed");
    expect(screen.getByTestId("run-live-idle")).toHaveTextContent("It recorded no error code.");
  });
});

describe("DatastreamRunLive -- a broken read", () => {
  it("keeps the last payload with its age and never calls it current", () => {
    vi.useFakeTimers();
    vi.setSystemTime(MEASURED_AT + 12_000);
    render(
      <DatastreamRunLive
        poll={poll({ phase: "error", attempts: 2, error: { status: 503, message: "HTTP 503", offline: false } })}
      />,
    );
    const band = within(screen.getByTestId("datastream-run-live"));
    expect(band.getByText("412,000")).toBeInTheDocument();
    expect(band.getByTestId("run-live-measured")).toHaveTextContent(/^Not current — last read 12 s ago, at /);
    expect(band.getByTestId("run-live-error")).toHaveTextContent("The collection state answered 503");
    // Still retrying on its own: asking by hand would race the backoff.
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
    vi.useRealTimers();
  });

  it("distinguishes a network that never answered from a route that refused", () => {
    render(
      <DatastreamRunLive
        poll={poll({ phase: "error", attempts: 1, error: { status: 0, message: "Failed to fetch", offline: true } })}
      />,
    );
    expect(screen.getByTestId("run-live-error")).toHaveTextContent("The network did not answer");
  });

  it("says the poll gave up, and offers the one gesture that resumes it", () => {
    const refresh = vi.fn();
    render(
      <DatastreamRunLive
        poll={poll({
          phase: "error",
          attempts: 5,
          stopped: true,
          refresh,
          error: { status: 503, message: "HTTP 503", offline: false },
        })}
      />,
    );
    expect(screen.getByTestId("run-live-error")).toHaveTextContent("Nothing more will be read until you ask.");
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(refresh).toHaveBeenCalledOnce();
  });
});

describe("DatastreamRunLive -- why this treatment is running (story 63.7)", () => {
  it("leads with the origin, and reads it off the registry", () => {
    const view = render(
      <DatastreamRunLive poll={poll({ progress: progress({ origin: "mapping_change" }) })} />,
    );
    const band = screen.getByTestId("datastream-run-live");
    const origin = screen.getByTestId("run-live-origin");
    // The registry's words, and the run's own instant -- both off the payload.
    expect(origin).toHaveTextContent(/^Mapping change, started /);
    // At the HEAD of the six blocks of 63.5: the band's first text is the
    // reason, not the state. A treatment nobody can account for is an incident,
    // and "Loading" alone is the same picture for all five paths.
    expect(band.textContent?.indexOf("Mapping change")).toBe(0);
    view.unmount();

    // Every origin the registry declares renders through the same function --
    // no case here names one, so the origin added tomorrow is covered.
    for (const entry of RUN_ORIGINS) {
      const one = render(
        <DatastreamRunLive poll={poll({ progress: progress({ origin: entry.key }) })} />,
      );
      expect(screen.getByTestId("run-live-origin")).toHaveTextContent(entry.label);
      one.unmount();
    }
  });

  it("says an origin was not measured rather than inventing one", () => {
    // Every execution minted before story 63.7 is this case: nothing recorded
    // which path created it, and nothing can work it out afterwards.
    render(<DatastreamRunLive poll={poll({ progress: progress({ origin: null }) })} />);
    expect(screen.getByTestId("run-live-origin")).toHaveTextContent(NOT_MEASURED);
  });

  it("shows an origin it does not know exactly as it arrived, unpainted", () => {
    const invented = "an_origin_no_build_knows";
    expect(RUN_ORIGINS.some((entry) => entry.key === invented)).toBe(false);

    render(<DatastreamRunLive poll={poll({ progress: progress({ origin: invented }) })} />);
    const origin = screen.getByTestId("run-live-origin");
    // The raw key, which a person can search for -- never a neighbouring label
    // that would put a reason on screen no path ever wrote.
    expect(origin).toHaveTextContent(invented);
    for (const entry of RUN_ORIGINS) {
      expect(origin).not.toHaveTextContent(entry.label);
    }
    // And it is not painted: a status tone would be this console claiming to
    // know what an unknown reason means.
    expect(origin.closest("[role='status']")).toBeNull();
  });

  it("puts the REASON where the bar would be, never a bar at zero", () => {
    // A mapping change binds ONE activation job and no pull job, so the three
    // LATERALs of the progress route come back empty at once. `windows_total`
    // is null because the run declared no window -- not because a measurement
    // failed, and 0 % would be a fraction nobody computed.
    render(
      <DatastreamRunLive
        poll={poll({
          progress: progress({
            origin: "mapping_change",
            days_done: null,
            days_total: null,
            windows_done: null,
            windows_total: null,
            window_in_progress: null,
            step: null,
          }),
        })}
      />,
    );
    const band = within(screen.getByTestId("datastream-run-live"));
    expect(band.getByTestId("run-live-no-fraction")).toHaveTextContent(
      "This update reads no provider window.",
    );
    expect(band.queryByTestId("run-live-days")).not.toBeInTheDocument();
    // Neither of the two sentences that would promise a number that is never
    // coming, nor a window this run will never have.
    expect(band.queryByText(/has not said how many days/)).not.toBeInTheDocument();
    expect(band.queryByText("No window is in flight.")).not.toBeInTheDocument();
    expect(band.queryByText("0")).not.toBeInTheDocument();
  });

  it("keeps the collection sentence for a run that DOES read windows", () => {
    // The reason is derived from the registry, not from the emptiness: a
    // nightly collection that has not declared its total yet is still waiting
    // for a number, and saying "reads no provider window" there would be false.
    render(
      <DatastreamRunLive
        poll={poll({
          progress: progress({
            origin: "scheduler_nightly",
            days_done: null,
            days_total: null,
            window_in_progress: null,
          }),
        })}
      />,
    );
    expect(screen.getByTestId("run-live-no-fraction")).toHaveTextContent(
      "This run has not said how many days it covers yet.",
    );
  });

  it("keeps the origin, with its age, on a reading that failed", () => {
    vi.useFakeTimers();
    vi.setSystemTime(MEASURED_AT + 12_000);
    render(
      <DatastreamRunLive
        poll={poll({
          phase: "error",
          attempts: 2,
          progress: progress({ origin: "plan_change" }),
          error: { status: 503, message: "HTTP 503", offline: false },
        })}
      />,
    );
    const band = within(screen.getByTestId("datastream-run-live"));
    expect(band.getByTestId("run-live-origin")).toHaveTextContent("Plan change");
    // Never said to be current: the age travels with it, as with every other
    // figure of the band.
    expect(band.getByTestId("run-live-measured")).toHaveTextContent(/^Not current — last read/);
    vi.useRealTimers();
  });

  it("says nothing about an origin when nothing is running", () => {
    // A finished run is not a treatment to watch, so the idle band carries the
    // three sentences of 63.5 unchanged and no origin at all.
    render(<DatastreamRunLive poll={idlePoll("last_run_succeeded", "collected")} />);
    expect(screen.queryByTestId("run-live-origin")).not.toBeInTheDocument();
    expect(screen.getByTestId("run-live-idle")).toHaveTextContent("Nothing is collecting.");
  });

  it("composes the sentence in ONE place, for the surfaces that compare it", () => {
    // `DatastreamRunLiveParity.test.tsx` compares two bands character for
    // character; a second composition would be the divergence that test exists
    // to catch, one correction later.
    expect(originSentence("refetch", "2026-08-06T00:00:00+00:00")).toContain(
      "Day re-collection, started ",
    );
    expect(originSentence(null, "2026-08-06T00:00:00+00:00")).toBeNull();
  });
});

describe("DatastreamRunLive -- structural guards", () => {
  it("types no run-state name: the registry decides what a state is called", () => {
    const code = codeOf(runLiveSource);
    const typed = EXECUTION_STATES.map((entry) => entry.name).filter((name) =>
      new RegExp(`["'\`]${name}["'\`]`).test(code),
    );
    expect(typed, `state name(s) typed into the band: ${typed.join(", ")}`).toEqual([]);
    expect(code).toContain('from "./executionStates"');
  });

  it("types no origin key either: the registry decides what a treatment is", () => {
    const code = codeOf(runLiveSource);
    const typed = RUN_ORIGINS.map((entry) => entry.key).filter((key) =>
      new RegExp(`["'\`]${key}["'\`]`).test(code),
    );
    expect(typed, `origin key(s) typed into the band: ${typed.join(", ")}`).toEqual([]);
    // Nor a label, which would be the same second vocabulary with nicer words.
    const labels = RUN_ORIGINS.map((entry) => entry.label).filter((label) =>
      code.includes(label),
    );
    expect(labels).toEqual([]);
    expect(code).toContain('from "./runOrigins"');
  });

  it("adds no stylesheet and no literal colour", () => {
    const code = codeOf(runLiveSource);
    expect(code).not.toMatch(/import\s+["'][^"']*\.css["']/);
    expect(code).not.toMatch(/#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(/);
  });

  it("computes no duration of its own -- the server owns the number", () => {
    // Two surfaces read this payload: this console and the MCP. Arithmetic here
    // would be an answer the tools cannot see.
    const code = codeOf(runLiveSource);
    expect(code).not.toContain("seconds_remaining");
    expect(code).toContain("estimate.sentence");
  });

  it("paints a run in flight as info, not as something to worry about", () => {
    expect(toneForPhase("running")).toBe("info");
    expect(toneForPhase("done")).toBe("success");
    expect(toneForPhase("failed")).toBe("error");
    expect(toneForPhase("todo")).toBe("neutral");
  });
});

it("says WHY a run that has not started is not starting, when the job that opens it died", async () => {
  // Jean, 2026-08-12: « soit ça connecte pas correctement, soit y a encore des
  // problèmes à traiter ». It was the second, and this band could not say it.
  // A candidate is minted at `created` and opened by a
  // `candidate_materialization` job; when that job dies the execution stays at
  // `created` for ever while the screen reads « has not started » — true, and
  // useless. Measured that day: 17 of 17 such jobs in `dead_letter`, none ever
  // `done`, the oldest sitting for thirteen hours behind a spinner.
  render(
    <DatastreamRunLive
      poll={poll({
        progress: {
          ...progress(),
          state: "created",
          started_at: null,
          materialization: {
            state: "Dead letter",
            error_code: "activation_work_failed",
            attempt_count: 3,
            last_attempt_at: "2026-08-12T09:12:38+00:00",
          },
        },
      })}
      projectId="proj_1"
      datastreamId="ds_1"
    />,
  );

  const band = await screen.findByTestId("run-live-materialization-failed");
  // The queue's own code, verbatim: a sentence per code written in the console
  // would be a second vocabulary, and an unknown code would say nothing at all.
  expect(band.textContent).toContain("activation_work_failed");
  expect(band.textContent).toContain("3 attempts");
  // The state travels as DATA, not as a word this file chose.
  expect(band.textContent).toContain("Dead letter");
  expect(band.textContent).toContain("stopped");
  // And the honest silence it replaces is gone — the two must never both show.
  expect(screen.queryByTestId("run-live-not-started")).toBeNull();
});

it("keeps the plain sentence when the job that opens the run has not failed", async () => {
  // `null` covers three different facts — the job is queued, it is running, or
  // there never was one — and not one of them is a reason to alarm anybody.
  render(
    <DatastreamRunLive
      poll={poll({
        progress: { ...progress(), state: "created", started_at: null, materialization: null },
      })}
      projectId="proj_1"
      datastreamId="ds_1"
    />,
  );

  expect(await screen.findByTestId("run-live-not-started")).toBeTruthy();
  expect(screen.queryByTestId("run-live-materialization-failed")).toBeNull();
});
