/**
 * The `Runs` tab is the activity track, and the anomaly unfolds INSIDE its run
 * — story 58.10, amended 2026-08-12 (visual review #69, findings D-1/D-7/D-9).
 *
 * WHAT THE AMENDMENT ADDED TO THIS FILE, and it is the last section below:
 * the run is a ROW whose facts are readable with no click, the REASON an absence
 * exists lives inside the run and not beside the fact, the writers' own names
 * (columns, functions, migrations, stories) appear in exactly ONE folded place
 * for the whole tab, and a run with no start instant reads `Not started` rather
 * than `Unavailable` — the same defect the live band was repaired for the same
 * day, and it is decided by the same `hasNotStarted`.
 *
 * WHAT THIS FILE GUARDS, and why each guard is the shape it is:
 *
 *   * the four absences. Measured on the disposable base 2026-08-07 over 428
 *     executions: a window on 0, a duration on 103, a collection counter on 6,
 *     an origin on 100. Each one renders a sentence naming what would write it
 *     — never a `0`, never a dash. A tab whose ordinary case is an absence has
 *     to say the absence well;
 *   * the state label comes from `EXECUTION_STATES`. `titleCase` stood at four
 *     places in this file and FABRICATED a label for a state this build does
 *     not know; the registry answers `Unknown`, which is the true answer;
 *   * the anomaly region is a `Collapsible` in the run's own subtree. THE GUARD
 *     IS A RENDER ASSERTION, NOT AN IMPORT TEST — the page imports
 *     `DatastreamRecoveryDialog`, which arbitrage 6 keeps, so an import check
 *     would pass trivially while a modal anomaly slipped through. A dialog
 *     renders through a portal, which puts its content OUTSIDE the run block:
 *     asserting containment is what actually refuses it (orchestrator control
 *     D, 2026-08-07);
 *   * the four-step track is never four zeroes. `ended_at` NULL means the step
 *     is still running; a reader that cannot tell that from "took no time"
 *     prints `0 s` under a step that is still working.
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import WorkbenchRunsPage, {
  RUN_EXPORT_COLUMNS,
  runsCsv,
  runsExportName,
  DURATION_ABSENCE,
  NOT_STARTED,
  NOT_STARTED_SENTENCE,
  NO_ANOMALY_ON_RUN,
  NO_MONITOR_EVALUATED,
  NO_STEP_SPAN,
  ORIGIN_ABSENCE,
  ROWS_ABSENCE,
  STEP_NOT_REACHED,
  STEP_NOT_TIMED,
  STEP_STILL_RUNNING,
  WHY_ABSENT_SUMMARY,
  WINDOW_ABSENCE,
  WINDOW_NOT_RECORDED,
  durationText,
  startedText,
} from "../datastreams/workbench/pages/WorkbenchRunsPage";
import { executionStateLabel } from "../datastreams/workbench/executionStates";
import type { WorkbenchTabPayload } from "../datastreams/workbench/workbenchTypes";

const PROJECT_ID = "proj_EXAMPLE";
const DATASTREAM_ID = "ds_EXAMPLE";

const LEDGER = { ledger: [] };

function ok(body: unknown, status = 200) {
  return { ok: status < 400, status, json: async () => body } as Response;
}

function stubFetch() {
  vi.stubGlobal("fetch", vi.fn(async () => ok(LEDGER)));
}

/**
 * The tab's own re-read, doubled — amended 2026-08-18.
 *
 * Since the narrowing and the paging moved to the server, a chip, a date, a
 * search box and `Next page` are all ONE gesture: a `GET …/workbench/runs?…`.
 * The double answers whatever the caller decides that query means, and records
 * every url so a test can assert what was actually ASKED — which is the half
 * that a client-side filter could never be wrong about and a server-side one
 * can.
 */
function stubRuns(answer: (query: URLSearchParams) => Record<string, unknown>) {
  const asked: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (!url.includes("/workbench/runs")) return ok(LEDGER);
      asked.push(url);
      const query = new URLSearchParams(url.split("?")[1] ?? "");
      return ok({ evidence: { step_vocabulary: VOCABULARY, timeline: [], ...answer(query) } });
    }),
  );
  return asked;
}

const VOCABULARY = ["Collect", "Map", "Check", "Publish"];

/**
 * A realistic `datastream_workbench.runs.v1`, in the shape `read_tab` composes:
 * a complete published run, a `collected` run that measured almost nothing, and
 * a `failed` run carrying its error code.
 */
const PUBLISHED = {
  id: "dse_EXAMPLE_PUBLISHED",
  state: "published",
  created_at: "2026-08-06T00:00:00Z",
  started_at: "2026-08-06T00:00:00Z",
  state_changed_at: "2026-08-06T00:04:00Z",
  duration_seconds: 240,
  rows_written: 412_000,
  origin: "scheduler_nightly",
  plan_version_id: "dsp_EXAMPLE",
  mapping_version_id: "dmap_EXAMPLE",
  recovery: {
    eligible: false,
    kinds: [],
    reviews: {},
    interval: { from: "2026-07-01", to_exclusive: "2026-08-01" },
    reason: "No adverse execution state",
  },
  steps: [
    { step: "Collect", started_at: "2026-08-06T00:00:00Z", ended_at: "2026-08-06T00:02:00Z", duration_seconds: 120 },
    { step: "Map", started_at: "2026-08-06T00:02:00Z", ended_at: "2026-08-06T00:02:02Z", duration_seconds: 2 },
    { step: "Check", started_at: "2026-08-06T00:02:02Z", ended_at: "2026-08-06T00:02:02.4Z", duration_seconds: 0.4 },
    { step: "Publish", started_at: "2026-08-06T00:02:02Z", ended_at: "2026-08-06T00:04:00Z", duration_seconds: 118 },
  ],
  anomalies: { anomalies: 2, evaluations: 3 },
};

/** The ordinary run of this base: it measured almost nothing, and says so. */
const COLLECTED = {
  id: "dse_EXAMPLE_COLLECTED",
  state: "collected",
  created_at: "2026-08-05T00:00:00Z",
  started_at: null,
  state_changed_at: null,
  duration_seconds: null,
  rows_written: null,
  origin: null,
  recovery: { eligible: false, kinds: [], reviews: {}, interval: null, reason: "" },
  steps: [],
  anomalies: { anomalies: 0, evaluations: 0 },
};

const FAILED = {
  id: "dse_EXAMPLE_FAILED",
  state: "failed",
  created_at: "2026-08-04T00:00:00Z",
  started_at: "2026-08-04T00:00:00Z",
  state_changed_at: "2026-08-04T00:00:30Z",
  duration_seconds: 30,
  rows_written: null,
  origin: "scheduler_nightly",
  error_code: "collection_window_failed",
  error_detail: "1 of 24 windows failed",
  recovery: { eligible: true, kinds: ["synchronize"], reviews: {}, interval: null, reason: "" },
  steps: [
    { step: "Collect", started_at: "2026-08-04T00:00:00Z", ended_at: null, duration_seconds: null },
  ],
  anomalies: { anomalies: 0, evaluations: 2 },
};

function payload(evidence: Record<string, unknown>): WorkbenchTabPayload {
  return {
    schema: "datastream_workbench.runs.v1",
    tab: "runs",
    project_id: PROJECT_ID,
    datastream_id: DATASTREAM_ID,
    evidence: {
      step_vocabulary: ["Collect", "Map", "Check", "Publish"],
      runs_limit: 200,
      runs_truncated: false,
      timeline: [],
      ...evidence,
    },
  };
}

function mount(evidence: Record<string, unknown>, stubbed = false) {
  if (!stubbed) stubFetch();
  return render(
    <WorkbenchRunsPage
      payload={payload(evidence)}
      projectId={PROJECT_ID}
      datastreamId={DATASTREAM_ID}
      onConfirmed={vi.fn()}
    />,
  );
}

function blockOf(runId: string) {
  const block = document.querySelector(`[data-run-id="${runId}"]`);
  if (!block) throw new Error(`no run block for ${runId}`);
  return block as HTMLElement;
}

beforeEach(() => {
  vi.unstubAllGlobals();
});

// ---------------------------------------------------------------------------
// One block per run, and it says what the run did.
// ---------------------------------------------------------------------------

describe("a run is the unit", () => {
  it("renders one block per run, each naming its identifier", async () => {
    mount({ runs: [PUBLISHED, COLLECTED, FAILED] });
    await waitFor(() => expect(screen.getAllByTestId("run-block").length).toBe(3));
    for (const run of [PUBLISHED, COLLECTED, FAILED]) {
      expect(within(blockOf(run.id)).getByText(run.id)).toBeTruthy();
    }
  });

  it("reads the state from the registry, and answers Unknown for one it does not know", async () => {
    mount({ runs: [{ ...COLLECTED, id: "dse_EXAMPLE_ALIEN", state: "a_state_no_build_knows" }] });
    const block = await waitFor(() => blockOf("dse_EXAMPLE_ALIEN"));
    // `titleCase` rendered "A State No Build Knows" — a product word for a
    // value no build has ever defined. The badge and the diagnosis both read
    // the registry, which is why the word appears more than once.
    expect(within(block).getAllByText("Unknown").length).toBeGreaterThan(0);
    expect(within(block).queryByText(/A State No Build Knows/)).toBeNull();
  });

  it("shows what a complete run covered, collected, and how long it took", async () => {
    mount({ runs: [PUBLISHED] });
    const block = await waitFor(() => blockOf(PUBLISHED.id));
    expect(within(block).getAllByText(executionStateLabel("published")).length).toBeGreaterThan(0);
    expect(within(block).getByText("Nightly collection")).toBeTruthy();
    expect(within(block).getByText("2026-07-01 → 2026-08-01")).toBeTruthy();
    expect(within(block).getByText("412,000")).toBeTruthy();
    expect(within(block).getByText("4 min")).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// The four absences — a sentence each, never a `0` and never a dash.
// ---------------------------------------------------------------------------

describe("what a run did not measure", () => {
  it("names what would write each of the four", async () => {
    mount({ runs: [COLLECTED] });
    const block = await waitFor(() => blockOf(COLLECTED.id));
    expect(within(block).getByText(WINDOW_NOT_RECORDED)).toBeTruthy();
    expect(within(block).getByText(WINDOW_ABSENCE)).toBeTruthy();
    expect(within(block).getByText(ROWS_ABSENCE)).toBeTruthy();
    expect(within(block).getByText(DURATION_ABSENCE)).toBeTruthy();
    expect(within(block).getByText(ORIGIN_ABSENCE)).toBeTruthy();
  });

  it("never prints a zero or a dash where nothing was measured", async () => {
    mount({ runs: [COLLECTED] });
    const block = await waitFor(() => blockOf(COLLECTED.id));
    for (const forbidden of ["0", "0 s", "—", "-", "0 rows"]) {
      expect(within(block).queryByText(forbidden)).toBeNull();
    }
    expect(within(block).getAllByText("Not measured").length).toBeGreaterThan(0);
  });
});

// ---------------------------------------------------------------------------
// The track — its substrate is migration 223, and it is NOT the seven phases.
// ---------------------------------------------------------------------------

describe("the four-step track", () => {
  it("gives each ratified step the time it took", async () => {
    mount({ runs: [PUBLISHED] });
    const block = await waitFor(() => blockOf(PUBLISHED.id));
    const track = within(block).getByTestId("run-track");
    expect(within(track).getByTestId("run-step-Collect").textContent).toContain("2 min");
    expect(within(track).getByTestId("run-step-Map").textContent).toContain("2 s");
    // A step measured in milliseconds is a REAL step: `under 1 s`, never `0 s`.
    expect(within(track).getByTestId("run-step-Check").textContent).toContain("under 1 s");
    expect(within(track).getByTestId("run-step-Publish").textContent).toContain("1 min 58 s");
  });

  it("says a run recorded no span at all, instead of four zeroes", async () => {
    mount({ runs: [COLLECTED] });
    const block = await waitFor(() => blockOf(COLLECTED.id));
    expect(within(block).getByTestId("run-track-absent").textContent).toBe(NO_STEP_SPAN);
    expect(within(block).queryByTestId("run-track")).toBeNull();
  });

  it("distinguishes a step still running from a step never reached", async () => {
    // The run is `loading`: its `Collect` span has no end because it is still
    // working, and `Map` has no span because the run never got there.
    mount({ runs: [{ ...FAILED, id: "dse_EXAMPLE_MOVING", state: "loading" }] });
    const block = await waitFor(() => blockOf("dse_EXAMPLE_MOVING"));
    const track = within(block).getByTestId("run-track");
    expect(within(track).getByTestId("run-step-Collect").textContent).toContain(STEP_STILL_RUNNING);
    expect(within(track).getByTestId("run-step-Map").textContent).toContain(STEP_NOT_REACHED);
    expect(within(track).queryByText("0 s")).toBeNull();
  });

  it("refuses to time a span whose two ends are the same instant", async () => {
    // A run whose `Collect` span opened and closed inside one transaction. The
    // step WAS entered — that is why the row exists — but nothing was measured,
    // and the block says exactly that instead of `under 1 s`.
    mount({
      runs: [
        {
          ...PUBLISHED,
          id: "dse_EXAMPLE_UNTIMED",
          steps: [
            {
              step: "Collect",
              started_at: "2026-08-06T00:00:00Z",
              ended_at: "2026-08-06T00:00:00Z",
              duration_seconds: 0,
            },
          ],
        },
      ],
    });
    const block = await waitFor(() => blockOf("dse_EXAMPLE_UNTIMED"));
    const collect = within(block).getByTestId("run-step-Collect");
    expect(collect.textContent).toContain(STEP_NOT_TIMED);
    expect(collect.getAttribute("data-measured")).toBe("false");
    expect(collect.textContent).not.toContain("under 1 s");
    expect(collect.textContent).not.toContain("0 s");
  });

  it("does not call a step still running on a run that ended", async () => {
    // THE READ-SIDE GUARANTEE, and it is the one that covers the paths no
    // writer closes. `commit_publication`, `datastream_activation`,
    // `reconcile_execution` and `_reconcile_fail_closed` reach a terminal state
    // with their own UPDATE; a run can therefore be `failed` or `published`
    // with an open span, and the ONLY thing that can tell the track it is not
    // working is the run's own state. Pinned here so it cannot quietly become
    // an assumption again.
    mount({ runs: [FAILED] });
    const block = await waitFor(() => blockOf(FAILED.id));
    const collect = within(block).getByTestId("run-step-Collect");
    expect(collect.textContent).toContain(STEP_NOT_TIMED);
    expect(collect.textContent).not.toContain(STEP_STILL_RUNNING);
    expect(collect.getAttribute("data-measured")).toBe("false");
  });

  it("reads an open span as running ONLY while the run itself is running", async () => {
    // The same span, the same absence of an end — two different sentences,
    // decided by the registry and by nothing else. `published` is terminal,
    // `loading` is not.
    const openSpan = [
      {
        step: "Collect",
        started_at: "2026-08-06T00:00:00Z",
        ended_at: null,
        duration_seconds: null,
      },
    ];
    mount({
      runs: [
        { ...PUBLISHED, id: "dse_EXAMPLE_ENDED", state: "published", steps: openSpan },
        { ...PUBLISHED, id: "dse_EXAMPLE_MOVING2", state: "loading", steps: openSpan },
      ],
    });
    await waitFor(() => expect(screen.getAllByTestId("run-block").length).toBe(2));

    const ended = within(blockOf("dse_EXAMPLE_ENDED")).getByTestId("run-step-Collect");
    const moving = within(blockOf("dse_EXAMPLE_MOVING2")).getByTestId("run-step-Collect");
    expect(ended.textContent).toContain(STEP_NOT_TIMED);
    expect(moving.textContent).toContain(STEP_STILL_RUNNING);
    // And neither invents a number.
    for (const cell of [ended, moving]) {
      expect(cell.getAttribute("data-measured")).toBe("false");
      expect(cell.textContent).not.toContain("0 s");
    }
  });
});

describe("durationText", () => {
  it("calls an elapsed of exactly zero an absence, never a fast step", () => {
    // The signature of a span whose two ends were written in ONE transaction:
    // `NOW()` is `transaction_timestamp()`, so it timed nothing. Reading it as
    // `under 1 s` is the fabricated zero of this story in a friendlier word,
    // and it is what shipped in the first cut.
    expect(durationText(0)).toBe(STEP_NOT_TIMED);
    expect(durationText(-1)).toBe(STEP_NOT_TIMED);
  });

  it("never answers zero for a span that was measured", () => {
    expect(durationText(0.4)).toBe("under 1 s");
    expect(durationText(2)).toBe("2 s");
    expect(durationText(120)).toBe("2 min");
    expect(durationText(118)).toBe("1 min 58 s");
    expect(durationText(3600)).toBe("1 h");
    expect(durationText(5_400)).toBe("1 h 30 min");
  });
});

// ---------------------------------------------------------------------------
// The place the anomaly unfolds in — 58.10 lays it, 59.1 fills it.
// ---------------------------------------------------------------------------

describe("the anomaly region", () => {
  it("unfolds INSIDE the run block, and its content stays in that subtree", async () => {
    mount({ runs: [PUBLISHED] });
    const block = await waitFor(() => blockOf(PUBLISHED.id));
    const trigger = within(block).getByTestId("run-anomalies-trigger");
    // Folded: Radix keeps the region mounted and hidden, so the state is read
    // rather than the presence — `hidden` content is not on screen either way.
    expect(
      within(block).getByTestId("run-anomalies-content").getAttribute("data-state"),
    ).toBe("closed");

    fireEvent.click(trigger);

    const content = await within(block).findByTestId("run-anomalies-content");
    await waitFor(() => expect(content.getAttribute("data-state")).toBe("open"));
    // THE GUARD. A dialog renders through a portal at the document root, so the
    // run block would NOT contain its content and this line falls.
    expect(block.contains(content)).toBe(true);
    expect(content.textContent).toContain("3 evaluation(s) ran on this run");

    fireEvent.click(within(block).getByTestId("run-anomalies-trigger"));
    await waitFor(() =>
      expect(
        within(block).getByTestId("run-anomalies-content").getAttribute("data-state"),
      ).toBe("closed"),
    );
  });

  it("shows no empty panel on a run with no anomaly, and tells the two silences apart", async () => {
    mount({ runs: [COLLECTED, FAILED] });
    await waitFor(() => expect(screen.getAllByTestId("run-block").length).toBe(2));
    const noMonitor = within(blockOf(COLLECTED.id));
    const checkedAndClean = within(blockOf(FAILED.id));

    expect(noMonitor.queryByTestId("run-anomalies")).toBeNull();
    expect(noMonitor.getByTestId("run-anomalies-none").textContent).toBe(NO_MONITOR_EVALUATED);
    expect(checkedAndClean.getByTestId("run-anomalies-none").textContent).toBe(NO_ANOMALY_ON_RUN);
  });
});

// ---------------------------------------------------------------------------
// The chips, the cap, and the strip that leads the tab.
// ---------------------------------------------------------------------------

describe("filtering and the strip", () => {
  /**
   * THE NARROWING IS THE SERVER'S — amended 2026-08-18, and the defect it closes
   * is not cosmetic. A chip applied to the 200 rows already in hand answered
   * "no run in this state" for a Datastream whose last failure was on page two:
   * a filter over a SAMPLE, presented as a history.
   */
  it("asks the server for the state, instead of narrowing the page it holds", async () => {
    const asked = stubRuns((query) =>
      query.get("state") === "failed"
        ? { runs: [FAILED], runs_matching: 1, run_states_present: ["published", "collected", "failed"] }
        : { runs: [PUBLISHED, COLLECTED, FAILED], runs_matching: 3, run_states_present: ["published", "collected", "failed"] },
    );
    mount(
      {
        runs: [PUBLISHED, COLLECTED, FAILED],
        runs_matching: 3,
        run_states_present: ["published", "collected", "failed"],
      },
      true,
    );
    await waitFor(() => expect(screen.getAllByTestId("run-block").length).toBe(3));

    fireEvent.click(screen.getByRole("radio", { name: executionStateLabel("failed") }));
    await waitFor(() => expect(screen.getAllByTestId("run-block").length).toBe(1));
    expect(screen.getByText(FAILED.id)).toBeTruthy();
    // The proof it is not a client-side filter: the state travelled.
    expect(asked.some((url) => url.includes("state=failed"))).toBe(true);

    fireEvent.click(screen.getByRole("radio", { name: "All runs" }));
    await waitFor(() => expect(screen.getAllByTestId("run-block").length).toBe(3));
  });

  it("draws a chip for a state no run ON THE PAGE carries, when the collection has one", async () => {
    // The page holds one published run; the Datastream has failed runs further
    // down. Deriving the chips from the page would take away the only control
    // that could reach them.
    stubRuns(() => ({ runs: [PUBLISHED] }));
    mount({ runs: [PUBLISHED], run_states_present: ["published", "failed"] }, true);
    await waitFor(() => expect(screen.getAllByTestId("run-block").length).toBe(1));
    expect(screen.getByRole("radio", { name: executionStateLabel("failed") })).toBeTruthy();
  });

  it("sends a date range, a search and an origin as ONE query on the collection", async () => {
    const asked = stubRuns(() => ({
      runs: [FAILED],
      runs_matching: 1,
      // The server sends the origins that EXIST on every answer, narrowed or
      // not: a selector that vanished with the first narrowing would take away
      // the control that undoes it.
      run_origins_present: ["scheduler_nightly"],
    }));
    mount(
      { runs: [PUBLISHED, FAILED], runs_matching: 2, run_origins_present: ["scheduler_nightly"] },
      true,
    );
    await waitFor(() => expect(screen.getAllByTestId("run-block").length).toBe(2));

    fireEvent.change(screen.getByTestId("runs-from"), { target: { value: "2026-08-04" } });
    await waitFor(() => expect(asked.some((url) => url.includes("from=2026-08-04"))).toBe(true));

    fireEvent.change(screen.getByTestId("runs-origin"), {
      target: { value: "scheduler_nightly" },
    });
    await waitFor(() => expect(asked.some((url) => url.includes("origin=scheduler_nightly"))).toBe(true));

    // The search commits on Enter, not on every keystroke: a re-read per letter
    // is six readings of a collection for one question.
    const search = screen.getByTestId("runs-search");
    fireEvent.change(search, { target: { value: "collection_window_failed" } });
    expect(asked.some((url) => url.includes("q=collection_window_failed"))).toBe(false);
    fireEvent.keyDown(search, { key: "Enter" });
    await waitFor(() => expect(asked.some((url) => url.includes("q=collection_window_failed"))).toBe(true));
  });

  it("says how many runs match, and offers a way past the cap", async () => {
    const asked = stubRuns(() => ({ runs: [COLLECTED], runs_matching: 428, runs_next_cursor: null }));
    mount(
      {
        runs: [PUBLISHED],
        runs_truncated: true,
        runs_limit: 200,
        runs_matching: 428,
        runs_next_cursor: "dse_EXAMPLE_PUBLISHED",
      },
      true,
    );
    // A DENOMINATOR, not a cap somebody can only distrust.
    await waitFor(() =>
      expect(screen.getByTestId("runs-count").textContent).toContain("1 of 428 run(s)"),
    );

    // And the cap has a door. Before this amendment the sentence « this
    // Datastream has more » was the whole of the answer.
    const pager = within(screen.getByTestId("runs-pager"));
    expect(pager.getByRole("button", { name: "Previous page" })).toBeDisabled();
    fireEvent.click(pager.getByRole("button", { name: "Next page" }));
    await waitFor(() =>
      expect(asked.some((url) => url.includes("cursor=dse_EXAMPLE_PUBLISHED"))).toBe(true),
    );
    // Walking forward makes the way back live — the three states of `PagerStep`.
    await waitFor(() =>
      expect(
        within(screen.getByTestId("runs-pager")).getByRole("button", { name: "Previous page" }),
      ).not.toBeDisabled(),
    );
  });

  it("says a sort covers this page only, because the collection is paged", async () => {
    mount({ runs: [PUBLISHED, FAILED] });
    await waitFor(() => expect(screen.getAllByTestId("run-block").length).toBe(2));
    expect(screen.getByTestId("page-sort-note")).toBeTruthy();
    // The two columns a person compares runs by carry the order, and they
    // announce it — `aria-sort` was absent from this whole table.
    const started = screen.getByRole("columnheader", { name: /Started/ });
    expect(started.getAttribute("aria-sort")).toBe("none");
    fireEvent.click(within(started).getByRole("button"));
    await waitFor(() => expect(started.getAttribute("aria-sort")).toBe("ascending"));
    // And the oldest run is first once it is ascending.
    expect(screen.getAllByTestId("run-block")[0].getAttribute("data-run-id")).toBe(FAILED.id);
  });

  it("keeps the coverage strip and the re-collection panel above an empty history", async () => {
    mount({ runs: [] });
    await waitFor(() => expect(screen.getByTestId("coverage-strip")).toBeTruthy());
    // `recovery-verbs`, not `reload-range` — the panel stopped being a date
    // picker for a verb the server refuses every time, and became the standing
    // of all three bounded verbs. A name that outlives what it names is how a
    // reader learns to distrust the next one.
    expect(screen.getByTestId("recovery-verbs")).toBeTruthy();
    expect(screen.getByText("No runs")).toBeTruthy();
    expect(screen.queryAllByTestId("run-block").length).toBe(0);
  });

  /**
   * THE PANEL THAT CANNOT BE PRESSED IS FOLDED — amended 2026-08-18.
   *
   * Nothing it says is withdrawn: it is the honest standing of three verbs, two
   * retired and one unbuilt. What changed is that it stood at the TOP of the tab
   * a person opens during an incident, pushing the first run row below the fold
   * at 1600px — a screen answering "which run failed" that opens on three
   * refusals.
   */
  it("folds the unpressable verbs, and keeps every word of them behind the disclosure", async () => {
    mount({ runs: [PUBLISHED] });
    const trigger = await screen.findByTestId("recovery-verbs-trigger");
    // Closed: Radix renders nothing of the content, so the refusal does not
    // occupy the tab.
    expect(screen.queryByText(/Nothing here can be asked for/)).toBeNull();
    // But the trigger already says what is behind it, and how much.
    expect(trigger.textContent).toContain("recovery verb(s) this build cannot grant");

    fireEvent.click(trigger);
    expect(await screen.findByText(/Nothing here can be asked for/)).toBeTruthy();
    expect(
      screen.getByText(/would block every later publication and every following night's collection/),
    ).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// The page as a file — one formatter, two readers.
// ---------------------------------------------------------------------------

describe("the export", () => {
  it("writes the rows on the page, in the words the table shows", () => {
    const file = runsCsv([PUBLISHED, COLLECTED]);
    const [header, published, collected] = file.split("\r\n");
    expect(header).toBe(RUN_EXPORT_COLUMNS.join(","));
    expect(published).toContain(PUBLISHED.id);
    expect(published).toContain("Nightly collection");
    expect(published).toContain("4 min");
    // AND THE ABSENCE IS THE ABSENCE. A run that measured nothing writes the
    // tab's own sentence, never a `0` — the file may not be more confident than
    // the screen it came from.
    expect(collected).toContain("Not measured");
    expect(collected).not.toContain(",0,");
  });

  it("names the file after the narrowing that produced it, and the rows in it", () => {
    expect(runsExportName(DATASTREAM_ID, { state: "failed", cursor: "dse_X" }, 12)).toBe(
      "runs_ds_EXAMPLE_state-failed_later-page_12-rows.csv",
    );
  });
});

// ---------------------------------------------------------------------------
// The amendment of 2026-08-12 — the fact stays, the explanation moves.
// ---------------------------------------------------------------------------

/** A run nobody has opened yet: `created` is the only state whose phase is `todo`. */
const NOT_YET = {
  id: "dse_EXAMPLE_WAITING",
  state: "created",
  created_at: "2026-08-07T00:00:00Z",
  started_at: null,
  duration_seconds: null,
  rows_written: null,
  origin: "mapping_change",
  recovery: { eligible: false, kinds: [], reviews: {}, interval: null, reason: "" },
  steps: [],
  anomalies: { anomalies: 0, evaluations: 0 },
};

/**
 * The words a screen may never print, and the ONE place this tab may.
 *
 * `CLAUDE.md` requires a message to name the gesture that repairs, never the
 * technical cause; finding D-9 of the visual review found four column names
 * reaching the screen on a neighbouring block. The technical account is still
 * worth having — it is the only honest answer to "what writes this, exactly" —
 * so it survives in exactly one folded disclosure, and this list is what proves
 * it did not leak back out of it.
 */
const REPOSITORY_WORDS = [
  "app.pull_jobs",
  "rows_written",
  "row_count",
  "started_at",
  "state_changed_at",
  "open_collection_run",
  "record_window_progress",
  "migration 218",
  "migration 223",
  "story 63.7",
];

describe("the run is a row, and the row is the whole first reading", () => {
  it("puts every fact of a run on its own row, with no click", async () => {
    mount({ runs: [PUBLISHED, COLLECTED, FAILED] });
    await waitFor(() => expect(screen.getAllByTestId("run-block").length).toBe(3));
    // FAILED is not the selected run, so its detail is folded — and every one of
    // these still has to be legible. A history you must unfold run by run to
    // find the failure is not a history.
    const row = within(blockOf(FAILED.id)).getByTestId("run-summary");
    expect(within(row).getByText(FAILED.id)).toBeTruthy();
    expect(within(row).getByText(executionStateLabel("failed"))).toBeTruthy();
    expect(within(row).getByTestId("run-origin").textContent).toBe("Nightly collection");
    expect(row.textContent).toContain("30 s");
    expect(row.textContent).toContain(WINDOW_NOT_RECORDED);
    expect(row.textContent).toContain("Not measured");
  });

  it("keeps the reason OFF the row and inside the run that carries it", async () => {
    mount({ runs: [COLLECTED] });
    const block = await waitFor(() => blockOf(COLLECTED.id));
    const row = within(block).getByTestId("run-summary");
    const detail = within(block).getByTestId("run-detail");
    for (const sentence of [WINDOW_ABSENCE, ROWS_ABSENCE, DURATION_ABSENCE, ORIGIN_ABSENCE]) {
      expect(row.textContent).not.toContain(sentence);
      expect(detail.textContent).toContain(sentence);
    }
  });

  it("says nothing about a measurement when there was nothing to measure", async () => {
    mount({ runs: [PUBLISHED] });
    const block = await waitFor(() => blockOf(PUBLISHED.id));
    // A complete run has no absences, so the section does not exist at all —
    // rather than existing and saying "none", which is a paragraph again.
    expect(within(block).queryByTestId("run-absences")).toBeNull();
  });

  it("unfolds and folds one run without touching the others", async () => {
    mount({ runs: [PUBLISHED, FAILED] });
    await waitFor(() => expect(screen.getAllByTestId("run-block").length).toBe(2));
    const first = within(blockOf(PUBLISHED.id));
    const second = within(blockOf(FAILED.id));
    // The run somebody arrives for opens with the tab; the rest stay folded.
    expect(first.getByTestId("run-detail").hasAttribute("hidden")).toBe(false);
    expect(second.getByTestId("run-detail").hasAttribute("hidden")).toBe(true);

    fireEvent.click(second.getByTestId("run-details-trigger"));
    await waitFor(() =>
      expect(second.getByTestId("run-detail").hasAttribute("hidden")).toBe(false),
    );
    expect(first.getByTestId("run-detail").hasAttribute("hidden")).toBe(false);

    fireEvent.click(second.getByTestId("run-details-trigger"));
    await waitFor(() =>
      expect(second.getByTestId("run-detail").hasAttribute("hidden")).toBe(true),
    );
  });
});

describe("the words that reach a person", () => {
  it("names no column, function, migration or story outside the one folded account", async () => {
    mount({ runs: [PUBLISHED, COLLECTED, FAILED, NOT_YET] });
    await waitFor(() => expect(screen.getAllByTestId("run-block").length).toBe(4));
    // OPENED ON PURPOSE. Radix renders the children of a closed `Collapsible`
    // not at all, so scanning the folded page would subtract nothing from
    // nothing and pass for the wrong reason — which is exactly what the first
    // cut of this test did.
    fireEvent.click(screen.getByTestId("runs-absence-trigger"));
    const account = await waitFor(() => {
      const said = screen.getByTestId("runs-absence-detail").textContent ?? "";
      expect(said).toContain("rows_written");
      return said;
    });
    expect(account).toContain("open_collection_run");
    // Everything the page renders, minus the one disclosure that is allowed to
    // carry the writers' names.
    const rendered = (document.body.textContent ?? "").split(account).join("");
    for (const word of REPOSITORY_WORDS) {
      expect(rendered).not.toContain(word);
    }
  });

  it("writes the technical account ONCE for the tab, never once per run", async () => {
    mount({ runs: [PUBLISHED, COLLECTED, FAILED, NOT_YET] });
    await waitFor(() => expect(screen.getAllByTestId("run-block").length).toBe(4));
    expect(screen.getAllByTestId("runs-absence-detail").length).toBe(1);
    expect(screen.getAllByTestId("runs-absence-trigger").length).toBe(1);
    // And the summary standing above the fold is true on its own: somebody who
    // never opens it still learns these are readings nobody took.
    expect(screen.getByTestId("runs-absence-summary").textContent).toBe(WHY_ABSENT_SUMMARY);
    for (const word of REPOSITORY_WORDS) {
      expect(WHY_ABSENT_SUMMARY).not.toContain(word);
    }
  });

  it("offers no explanation when no run on screen is missing anything", async () => {
    mount({ runs: [PUBLISHED] });
    await waitFor(() => expect(screen.getAllByTestId("run-block").length).toBe(1));
    expect(screen.queryByTestId("runs-absence-trigger")).toBeNull();
  });
});

describe("a run that has not started", () => {
  it("says so, instead of rendering a start that could not be read", async () => {
    mount({ runs: [NOT_YET] });
    const block = await waitFor(() => blockOf(NOT_YET.id));
    const row = within(block).getByTestId("run-summary");
    // `Started Unavailable` was what this said: `dateTime` answers `Unavailable`
    // for anything it cannot parse, so a run that has not begun was rendered as
    // a measurement that FAILED.
    expect(row.textContent).toContain(NOT_STARTED);
    expect(row.textContent).not.toContain("Unavailable");
    expect(within(block).getByTestId("run-not-started").textContent).toBe(NOT_STARTED_SENTENCE);
  });

  it("draws no measurement at all for it — not a dash, not a zero, not three absences", async () => {
    mount({ runs: [NOT_YET] });
    const block = await waitFor(() => blockOf(NOT_YET.id));
    const row = within(block).getByTestId("run-summary");
    expect(row.textContent).not.toContain(WINDOW_NOT_RECORDED);
    expect(row.textContent).not.toContain("Not measured");
    for (const forbidden of ["0", "0 s", "—", "-"]) {
      expect(within(row).queryByText(forbidden)).toBeNull();
    }
    // Its window, its rows and its duration are not absences of THIS run: it
    // has not reached them, so the detail explains none of the three.
    const detail = within(block).getByTestId("run-detail");
    for (const sentence of [WINDOW_ABSENCE, ROWS_ABSENCE, DURATION_ABSENCE]) {
      expect(detail.textContent).not.toContain(sentence);
    }
  });

  it("reads the same rule as the live band, and answers three different things", () => {
    // `hasNotStarted(state, startedAt)` is the authority, exported from
    // `DatastreamRunLive.tsx`: a third rule here is how two regions of one
    // screen come to disagree about one run.
    expect(startedText({ state: "created", started_at: null, created_at: "2026-08-07T00:00:00Z" }))
      .toBe(NOT_STARTED);
    // A run that HAS started keeps its instant — and a run older than the writer
    // of `started_at` falls back to the instant it entered the history, which is
    // what the list is ordered by, never to `Unavailable`.
    expect(startedText({ state: "published", started_at: null, created_at: "2026-08-07T00:00:00Z" }))
      .not.toBe(NOT_STARTED);
    expect(startedText({ state: "published", started_at: null, created_at: "2026-08-07T00:00:00Z" }))
      .not.toContain("Unavailable");
    // Started, and nobody wrote down when: a third answer, never one of the two.
    expect(startedText({ state: "published", started_at: null, created_at: null }))
      .toBe("Start not recorded");
  });
});
