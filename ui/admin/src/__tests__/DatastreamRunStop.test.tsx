/**
 * Stopping a run, from the band that watches it -- story 63.6, epic 63.
 *
 * WHAT THESE TESTS ARE FOR. This is the FIRST write this band offers: the five
 * stories before it read. A write reached from a screen that watches something
 * move has three ways to lie, and each of them has a test here:
 *
 *   1. offering the gesture when there is nothing to stop -- a button with no
 *      object, which a person clicks and then wonders what happened;
 *   2. promising an interruption the product cannot perform. `_execute_job` is
 *      synchronous and the stale sweep is at 5400 s, so the window in flight
 *      finishes whatever the screen says. Somebody watching a counter that does
 *      not stop after they stopped the run has been told something false;
 *   3. reporting a stop that the server refused. The band keeps its figures, the
 *      poll keeps running, and the message stays in the dialog -- a refused stop
 *      is not a stopped run.
 *
 * The component takes the poll as a PROP, so nothing here needs a clock. What
 * needs a double is `fetch`, because the write is the subject.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import DatastreamRunLive from "../datastreams/workbench/DatastreamRunLive";
import {
  stopRunPath,
  windowsNotStarted,
} from "../datastreams/workbench/datastreamProgress";
import type {
  DatastreamProgress,
  DatastreamProgressPoll,
} from "../datastreams/workbench/datastreamProgress";
import bandSource from "../datastreams/workbench/DatastreamRunLive?raw";

const PROJECT = "proj_EXAMPLE";
const DATASTREAM = "ds_EXAMPLE";
const MEASURED_AT = 1_772_000_000_000;

let fetchMock: ReturnType<typeof vi.fn>;

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
    days_total: 730,
    windows_done: 1,
    windows_total: 24,
    window_in_progress: {
      date_from: "2026-08-01",
      date_to: "2026-08-31",
      days: 31,
      started_at: "2026-08-06T00:00:00+00:00",
      completed_at: null,
    },
    rows_written: 18_420,
    started_at: "2026-08-06T00:00:00+00:00",
    progress_updated_at: "2026-08-06T00:04:00+00:00",
    plan_version_id: "dpv_EXAMPLE",
    mapping_version_id: "dmv_EXAMPLE",
    estimate: null,
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

function band(overrides: Partial<DatastreamProgressPoll> = {}) {
  const value = poll(overrides);
  render(
    <DatastreamRunLive poll={value} projectId={PROJECT} datastreamId={DATASTREAM} />,
  );
  return value;
}

function ok(body: unknown): Response {
  return { ok: true, status: 200, json: () => Promise.resolve(body) } as unknown as Response;
}

function refused(status: number, code: string, message: string): Response {
  return {
    ok: false,
    status,
    json: () => Promise.resolve({ code, message }),
  } as unknown as Response;
}

const STOPPED = {
  execution_id: "dse_EXAMPLE",
  state: "cancelled",
  windows_refused: 22,
  window_in_flight: { date_from: "2026-08-01", date_to: "2026-08-31" },
  days_kept: 31,
  rows_kept: 18_420,
  stopped_at: "2026-08-06T10:04:00+00:00",
};

beforeEach(() => {
  fetchMock = vi.fn(() => Promise.resolve(ok(STOPPED)));
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

// ---------------------------------------------------------------------------
// When the gesture is offered at all.
// ---------------------------------------------------------------------------

test("offers no Stop button when nothing is collecting", () => {
  band({
    phase: "stopped",
    progress: null,
    stopped: true,
    idle: {
      reason: "last_run_succeeded",
      execution_id: "dse_EXAMPLE",
      state: "collected",
      ended_at: "2026-08-06T00:09:00+00:00",
      error_code: null,
      days_done: 31,
      days_total: 31,
      rows_written: 900,
    },
  });

  expect(screen.queryByTestId("run-live-stop")).toBeNull();
});

test("offers no Stop button on a surface that has not resolved its scope", () => {
  render(<DatastreamRunLive poll={poll()} />);
  expect(screen.queryByTestId("run-live-stop")).toBeNull();
});

test("offers the Stop button while a run is moving", () => {
  band();
  expect(screen.getByTestId("run-live-stop")).toBeInTheDocument();
});

// ---------------------------------------------------------------------------
// The confirmation names its scope, BEFORE the write.
// ---------------------------------------------------------------------------

test("nothing is written until the confirmation is confirmed", () => {
  band();
  fireEvent.click(screen.getByTestId("run-live-stop"));

  expect(screen.getByTestId("run-live-stop-confirm")).toBeInTheDocument();
  expect(fetchMock).not.toHaveBeenCalled();
});

test("the confirmation names the flux, what it refuses, and what finishes anyway", () => {
  band();
  fireEvent.click(screen.getByTestId("run-live-stop"));
  const dialog = screen.getByTestId("run-live-stop-confirm");

  expect(dialog).toHaveTextContent(DATASTREAM);
  // 24 windows, 1 finished, 1 in flight -> 22 never started. The COUNT BEFORE.
  expect(dialog).toHaveTextContent("22");
  expect(dialog).toHaveTextContent("2026-08-01");
  expect(dialog).toHaveTextContent("2026-08-31");
  // The days already kept, so nobody confirms without knowing what stays.
  expect(dialog).toHaveTextContent("31");
  expect(dialog).toHaveTextContent("18,420");
});

test("the confirmation refuses to promise an interruption it cannot perform", () => {
  band();
  fireEvent.click(screen.getByTestId("run-live-stop"));
  const dialog = screen.getByTestId("run-live-stop-confirm");

  expect(dialog).toHaveTextContent(/cannot be interrupted/i);
  expect(dialog).toHaveTextContent(/it will finish/i);
  expect(dialog).toHaveTextContent(/Nothing already collected is undone/i);
});

test("a run with no window count says the scope is not measured, and invents none", () => {
  band({ progress: progress({ windows_total: null, windows_done: null }) });
  fireEvent.click(screen.getByTestId("run-live-stop"));

  expect(screen.getByTestId("run-live-stop-confirm")).toHaveTextContent("Not measured");
});

test("cancelling writes nothing at all", () => {
  band();
  fireEvent.click(screen.getByTestId("run-live-stop"));
  fireEvent.click(screen.getByTestId("run-live-stop-cancel"));

  expect(fetchMock).not.toHaveBeenCalled();
  expect(screen.queryByTestId("run-live-stop-confirm")).toBeNull();
  // And the band still shows the run, because nothing happened to it.
  expect(screen.getByTestId("datastream-run-live")).toHaveTextContent("31");
});

// ---------------------------------------------------------------------------
// The write.
// ---------------------------------------------------------------------------

test("confirming posts to the run's own address and refreshes the poll", async () => {
  const value = band();
  fireEvent.click(screen.getByTestId("run-live-stop"));
  fireEvent.click(screen.getByTestId("run-live-stop-go"));

  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
  const [url, init] = fetchMock.mock.calls[0];
  expect(String(url)).toContain(stopRunPath(PROJECT, DATASTREAM, "dse_EXAMPLE"));
  expect((init as RequestInit).method).toBe("POST");
  // The band switches without waiting for the next tick: a person who just
  // stopped a run may not be shown it still running.
  await waitFor(() => expect(value.refresh).toHaveBeenCalledTimes(1));
  await waitFor(() => expect(screen.queryByTestId("run-live-stop-confirm")).toBeNull());
});

test("a second click during the call does not send a second write", async () => {
  let settle: (value: Response) => void = () => {};
  fetchMock.mockImplementation(
    () => new Promise<Response>((resolve) => { settle = resolve; }),
  );
  band();
  fireEvent.click(screen.getByTestId("run-live-stop"));
  fireEvent.click(screen.getByTestId("run-live-stop-go"));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

  fireEvent.click(screen.getByTestId("run-live-stop-go"));
  fireEvent.click(screen.getByTestId("run-live-stop-go"));
  expect(fetchMock).toHaveBeenCalledTimes(1);

  settle(ok(STOPPED));
  await waitFor(() => expect(screen.queryByTestId("run-live-stop-confirm")).toBeNull());
});

// ---------------------------------------------------------------------------
// A refused stop is NOT a stopped run.
// ---------------------------------------------------------------------------

test("a refusal stays in the dialog, and the band keeps its figures", async () => {
  fetchMock.mockResolvedValue(
    refused(409, "run_not_running", "This run has already ended, so there is nothing to stop."),
  );
  const value = band();
  fireEvent.click(screen.getByTestId("run-live-stop"));
  fireEvent.click(screen.getByTestId("run-live-stop-go"));

  await waitFor(() =>
    expect(screen.getByTestId("run-live-stop-confirm")).toHaveTextContent(
      /already ended/i,
    ),
  );
  // The dialog is still open: closing it would leave a person believing the run
  // was stopped.
  expect(screen.getByTestId("run-live-stop-confirm")).toBeInTheDocument();
  // The band still shows the run, and the poll was never told to refresh.
  expect(screen.getByTestId("datastream-run-live")).toHaveTextContent("31");
  expect(value.refresh).not.toHaveBeenCalled();
});

test("the server's own sentence is shown, never a message this screen invented", async () => {
  fetchMock.mockResolvedValue(
    refused(409, "not_a_collection_run", "This run is not a collection, so it cannot be stopped here."),
  );
  band();
  fireEvent.click(screen.getByTestId("run-live-stop"));
  fireEvent.click(screen.getByTestId("run-live-stop-go"));

  await waitFor(() =>
    expect(screen.getByTestId("run-live-stop-confirm")).toHaveTextContent(
      /not a collection/i,
    ),
  );
});

// ---------------------------------------------------------------------------
// After the stop: what was kept.
// ---------------------------------------------------------------------------

function stoppedPoll(overrides: Record<string, unknown> = {}) {
  return band({
    phase: "stopped",
    progress: null,
    stopped: true,
    idle: {
      reason: "last_run_stopped",
      execution_id: "dse_EXAMPLE",
      state: "cancelled",
      ended_at: "2026-08-06T10:04:00+00:00",
      error_code: "collection_run_stopped",
      days_done: 31,
      days_total: 730,
      rows_written: 18_420,
      ...overrides,
    },
  });
}

test("after the stop the band says what was collected and kept", () => {
  stoppedPoll();
  const kept = screen.getByTestId("run-live-kept");

  expect(kept).toHaveTextContent("Stopped.");
  expect(kept).toHaveTextContent("31 of 730 days were collected and kept");
  expect(kept).toHaveTextContent("18,420 rows landed");
});

test("a stopped run is not painted as a failure", () => {
  stoppedPoll();
  const idle = screen.getByTestId("run-live-idle");

  expect(idle).toHaveTextContent("Nothing is collecting.");
  // The failure branch prints the error code; a stop must not reach it.
  expect(idle).not.toHaveTextContent("It reported collection_run_stopped.");
});

test("a stopped run that measured nothing says so, and never reads 0", () => {
  stoppedPoll({ days_done: null, days_total: null, rows_written: null });
  const kept = screen.getByTestId("run-live-kept");

  expect(kept).toHaveTextContent("Not measured");
  expect(kept).not.toHaveTextContent(" 0 ");
});

// ---------------------------------------------------------------------------
// The arithmetic of the scope, on its own.
// ---------------------------------------------------------------------------

test("the refused count is derived from measured numbers, and is null when they are absent", () => {
  expect(windowsNotStarted(progress())).toBe(22);
  // No window in flight: nothing is subtracted for it.
  expect(windowsNotStarted(progress({ window_in_progress: null }))).toBe(23);
  // A run that declared no windows declared none -- it did not declare zero.
  expect(windowsNotStarted(progress({ windows_total: null }))).toBeNull();
  expect(windowsNotStarted(null)).toBeNull();
  // Never negative, whatever a server sends.
  expect(windowsNotStarted(progress({ windows_total: 1, windows_done: 5 }))).toBe(0);
});

// ---------------------------------------------------------------------------
// The band still types no state name.
// ---------------------------------------------------------------------------

test("the band names no run state of its own, stop included", () => {
  const code = bandSource
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/(^|[^:])\/\/.*$/gm, "$1");
  for (const name of ["cancelled", "collected", "loading", "publishing"]) {
    expect(code).not.toContain(`"${name}"`);
  }
});
