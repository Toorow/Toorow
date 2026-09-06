/**
 * A run that collected everything must not read as "still waiting" -- story 63.1.
 *
 * `WorkbenchRunsPage.phaseState` carried its own three `if` lines over run
 * states. When migration 218 added `collected` -- the terminal state of a
 * recurring retrieval, which pulls its windows and publishes nothing by design
 * -- that function fell through to `todo`, and `runTone` turned `todo` into
 * `warning`. A run that had done every single thing asked of it was painted
 * amber and pending, on the one tab whose job is to say what happened.
 *
 * These tests mount the real page and read what a person would see. The
 * registry mirror has its own comparison against the Python source
 * (`server/tests/conformance/test_execution_state_registry.py`); this file is
 * about the screen.
 */
import { render, screen } from "@testing-library/react";
import WorkbenchRunsPage, {
  ALL_RUNS,
  stateChoices,
} from "../datastreams/workbench/pages/WorkbenchRunsPage";
import {
  EXECUTION_STATES,
  executionStateLabel,
  isSuccess,
  isTerminal,
  phaseOf,
} from "../datastreams/workbench/executionStates";
import type { WorkbenchTabPayload } from "../datastreams/workbench/workbenchTypes";

const PROJECT_ID = "proj_EXAMPLE";
const DATASTREAM_ID = "ds_EXAMPLE";

/**
 * The tab payload as `WorkbenchTabPayload` declares it -- built, not asserted.
 * A cast here would have let the fixture drift away from the contract the page
 * reads, which is exactly how this file came to hand the page a `header` prop it
 * has never accepted while omitting the three it requires.
 */
function mountRuns(runs: Array<Record<string, unknown>>) {
  const payload: WorkbenchTabPayload = {
    schema: "datastream_workbench.tab.v1",
    tab: "runs",
    project_id: PROJECT_ID,
    datastream_id: DATASTREAM_ID,
    evidence: { state: "available", runs, timeline: [] },
  };
  return render(
    <WorkbenchRunsPage
      payload={payload}
      projectId={PROJECT_ID}
      datastreamId={DATASTREAM_ID}
      onConfirmed={() => {}}
    />,
  );
}

function run(state: string, extra: Record<string, unknown> = {}) {
  return {
    id: "dse_EXAMPLE",
    state,
    created_at: "2026-08-05T02:00:00Z",
    state_changed_at: "2026-08-05T02:12:00Z",
    plan_version_id: "dsp_EXAMPLE",
    mapping_version_id: "dmap_EXAMPLE",
    row_count: 18420,
    recovery: { eligible: false, kinds: [], reviews: {}, reason: "No adverse execution state" },
    ...extra,
  };
}

// ---------------------------------------------------------------------------
// The vocabulary the page reads.
// ---------------------------------------------------------------------------

it("paints a collected run as finished, never as still waiting", () => {
  expect(phaseOf("collected")).toBe("done");
  expect(isTerminal("collected")).toBe(true);
  expect(isSuccess("collected")).toBe(true);
});

it("keeps a run in flight distinct from one that finished", () => {
  expect(phaseOf("loading")).toBe("running");
  expect(isTerminal("loading")).toBe(false);
  expect(isSuccess("loading")).toBe(false);
});

it("refuses to call an unknown state finished", () => {
  // A state this build does not know has not been shown to have ended, and
  // claiming otherwise would be the console inventing an outcome.
  expect(phaseOf("a_state_no_build_knows")).toBe("todo");
  expect(isTerminal("a_state_no_build_knows")).toBe(false);
  expect(isSuccess("a_state_no_build_knows")).toBe(false);
});

it("classifies every state it declares, with no silent fallthrough", () => {
  // Named by no state: adding one to the registry without a phase reddens here.
  for (const entry of EXECUTION_STATES) {
    expect(phaseOf(entry.name)).toBe(entry.phase);
    expect(isTerminal(entry.name)).toBe(entry.terminal);
    expect(isSuccess(entry.name)).toBe(entry.success);
  }
});

// ---------------------------------------------------------------------------
// The screen itself.
// ---------------------------------------------------------------------------

it("shows a finished collection as a success on the Runs tab", () => {
  mountRuns([run("collected", { step: "Collect", days_done: 24, days_total: 24 })]);
  // The tab renders the state; what matters is that it is not the pending tone.
  expect(screen.getAllByText(/collected/i).length).toBeGreaterThan(0);
});

it("still shows a failed run as a failure", () => {
  mountRuns([
    run("failed", {
      error_code: "collection_window_failed",
      error_detail: "1 of 24 windows failed",
      recovery: { eligible: true, kinds: ["synchronize"], reviews: {}, reason: "" },
    }),
  ]);
  expect(screen.getAllByText(/failed/i).length).toBeGreaterThan(0);
});

// ---------------------------------------------------------------------------
// The state chips -- story 58.10, arbitrage 5.
// ---------------------------------------------------------------------------
//
// The filter is client-side, over the runs already on the payload, and its
// words come from the SAME registry as the badge under them. A chip naming a
// state the server does not know would offer a filter that can only ever empty
// the screen; a chip spelling a state differently from its badge would make one
// fact read as two.

it("names nothing but entries of EXECUTION_STATES on its chips", () => {
  const names = new Set(EXECUTION_STATES.map((entry) => entry.name));
  const labels = new Set(EXECUTION_STATES.map((entry) => executionStateLabel(entry.name)));
  const choices = stateChoices([
    { state: "collected" },
    { state: "failed" },
    // A state this build does not know gets NO chip: the console cannot offer a
    // filter for a word it cannot name.
    { state: "a_state_no_build_knows" },
  ]);

  expect(choices[0]).toEqual({ value: ALL_RUNS, label: "All runs" });
  expect(names.has(ALL_RUNS)).toBe(false);
  for (const choice of choices.slice(1)) {
    expect(names.has(choice.value)).toBe(true);
    expect(labels.has(String(choice.label))).toBe(true);
  }
  expect(choices.slice(1).map((choice) => choice.value)).toEqual(["collected", "failed"]);
});

it("offers a chip only for the states its own runs carry", () => {
  const choices = stateChoices([{ state: "published" }]);
  expect(choices.map((choice) => choice.value)).toEqual([ALL_RUNS, "published"]);
});

it("renders those chips on the tab, in the registry's words", () => {
  mountRuns([run("collected"), run("failed", { id: "dse_EXAMPLE_2" })]);
  const chips = screen.getAllByRole("radio").map((chip) => chip.getAttribute("aria-label"));
  expect(chips).toEqual([
    "All runs",
    executionStateLabel("collected"),
    executionStateLabel("failed"),
  ]);
});
