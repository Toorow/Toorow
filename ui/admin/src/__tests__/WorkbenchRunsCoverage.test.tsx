/**
 * The Runs tab answers "which days am I missing", and lets a person re-ask for
 * the ones they choose — AI-144.
 *
 * Both mechanisms were BUILT and unreachable. `CoverageStrip` reads the extract
 * ledger and repairs the repairable days; it was imported by nothing but its own
 * test, so no screen ever showed a gap. And the only recovery surface mounted
 * (`DatastreamRecoveryDialog`) pinned its interval to the SELECTED RUN, so an
 * arbitrary range could not be asked for even though the server has accepted
 * `date_from` + `date_to_exclusive` since Story 12.11.
 *
 * AND THE RANGE PICKER IS GONE, which is what this file now pins instead.
 * `DatastreamReloadPanel` carried two date inputs and a `Prepare reload` button
 * that reached the server and came back REFUSED every time: the bounded verbs
 * mint an execution nothing would advance, so the server refuses them at
 * prepare. The tests that lived here passed only because they mocked a success
 * the real server never returns — a green suite over a gesture that cannot work.
 * They are replaced by the three standings the panel now states, one per verb,
 * and by the assertion that no control on it can be pressed.
 *
 * These tests pin the reachability, not the rendering: that the tab actually
 * calls the ledger, that it still does so when there are NO runs (which is
 * exactly when every day reads `never_fetched`), and that each bounded verb is
 * named separately with its own cost and its own standing.
 *
 * `fetch` is stubbed rather than `apiFetch`, matching the workbench tests: the
 * seam guard is what proves the bearer is attached, and stubbing one level lower
 * would hide it.
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import WorkbenchRunsPage from "../datastreams/workbench/pages/WorkbenchRunsPage";
import { unbuiltRecoveryVerbs, verbCost } from "../datastreams/workbench/boundedRecovery";
import { RUN_ORIGINS } from "../datastreams/workbench/runOrigins";
import type { WorkbenchTabPayload } from "../datastreams/workbench/workbenchTypes";

const LEDGER = {
  ledger: [
    { date: "2026-07-28", status: "ok", row_count: 120 },
    { date: "2026-07-29", status: "failed", row_count: null },
    { date: "2026-07-30", status: "empty", row_count: 0 },
    { date: "2026-07-31", status: "never_fetched", row_count: null },
  ],
};

function payload(evidence: Record<string, unknown>): WorkbenchTabPayload {
  return {
    schema: "datastream_workbench.runs.v1",
    tab: "runs",
    project_id: "proj_1",
    datastream_id: "ds_1",
    evidence,
  };
}

function ok(body: unknown, status = 200) {
  return { ok: status < 400, status, json: async () => body } as Response;
}

type Call = { url: string; init?: RequestInit };

function stubFetch(handler: (url: string, init?: RequestInit) => unknown): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo, init?: RequestInit) => {
      calls.push({ url: String(input), init });
      return handler(String(input), init) as Response;
    }),
  );
  return calls;
}

/** The route this tab reaches for coverage, whatever query it carries. */
function ledgerCalls(calls: Call[]): Call[] {
  return calls.filter((call) => call.url.includes("/ledger"));
}

/**
 * The history, as story 58.10 renders it: ONE BLOCK PER RUN, not a table.
 *
 * These tests were written against the five-column table this tab used to be,
 * and the table answered "which run" while never answering "what did this run
 * do". Every assertion below is the SAME assertion — one rows measurement, the
 * origin in the registry's words, the strip above an empty history — read on
 * the object that replaced the table.
 */
function runBlocks(): HTMLElement[] {
  return Array.from(document.querySelectorAll("[data-testid='run-block']"));
}

async function findRunBlocks(): Promise<HTMLElement[]> {
  await waitFor(() => expect(runBlocks().length).toBeGreaterThan(0));
  return runBlocks();
}

/** The words the history puts on screen, all blocks taken together. */
function historyText(): string {
  return runBlocks()
    .map((block) => block.textContent ?? "")
    .join("\n");
}

describe("Runs tab — coverage and range re-collection", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("reads the extract ledger, so a missing day is visible on the tab", async () => {
    const calls = stubFetch(() => ok(LEDGER));
    render(
      <WorkbenchRunsPage
        payload={payload({ runs: [{ id: "dse_1", state: "succeeded", row_count: 10 }], timeline: [] })}
        projectId="proj_1"
        datastreamId="ds_1"
        onConfirmed={vi.fn()}
      />,
    );

    await waitFor(() => expect(screen.getByTestId("coverage-strip")).toBeTruthy());
    expect(ledgerCalls(calls).length).toBe(1);
    expect(ledgerCalls(calls)[0].url).toContain("/api/datastreams/ds_1/ledger");
    expect(ledgerCalls(calls)[0].url).toContain("project_id=proj_1");
  });

  it("still shows coverage when there is no run at all", async () => {
    // A Datastream with zero executions is the case where EVERY day reads
    // `never_fetched` — the one moment coverage matters most. The tab used to
    // return an empty state before reaching the strip.
    const calls = stubFetch(() => ok({ ledger: [] }));
    render(
      <WorkbenchRunsPage
        payload={payload({ runs: [], timeline: [] })}
        projectId="proj_1"
        datastreamId="ds_1"
        onConfirmed={vi.fn()}
      />,
    );

    await waitFor(() => expect(screen.getByTestId("coverage-strip")).toBeTruthy());
    expect(ledgerCalls(calls).length).toBe(1);
    expect(screen.getByTestId("recovery-verbs")).toBeTruthy();
    expect(screen.getByText("No runs")).toBeTruthy();
  });

  it("names each bounded verb separately, with its own cost and its own standing", async () => {
    // The defect this replaces: one two-date picker and one `Prepare reload`
    // button standing for three verbs the server refuses alike. Two of them are
    // performed elsewhere in the product and one is genuinely missing — and a
    // person reading the tab could learn none of that.
    stubFetch(() => ok(LEDGER));
    render(
      <WorkbenchRunsPage
        payload={payload({ runs: [], timeline: [] })}
        projectId="proj_1"
        datastreamId="ds_1"
        onConfirmed={vi.fn()}
      />,
    );

    const panel = await screen.findByTestId("recovery-verbs");
    // FOLDED SINCE 2026-08-18, and everything below is still there. The panel
    // says only what cannot be asked for, and it stood above the run list on the
    // tab a person opens during an incident.
    fireEvent.click(within(panel).getByTestId("recovery-verbs-trigger"));
    for (const verb of unbuiltRecoveryVerbs()) {
      const block = within(panel).getByTestId(`recovery-verb-${verb.origin.key}`);
      expect(within(block).getByText(verb.origin.label)).toBeTruthy();
      // Spend is stated on EVERY verb, not only the expensive ones: "would spend
      // nothing" is the fact that separates Reprocess from the other two.
      expect(
        within(panel).getByTestId(`recovery-cost-${verb.origin.key}`).textContent,
      ).toBe(verbCost(verb.origin));
    }
  });

  it("keeps the two retired verbs, and Reprocess has left the panel by itself", async () => {
    // AMENDED 2026-08-17 (67-15b). This asserted that `Reprocess` was present
    // and refused as "Not built yet". It is BUILT, so it is gone from a panel
    // whose whole subject is verbs that cannot be granted -- and it left without
    // anyone editing this component, because the panel reads `has_engine` from
    // the registry mirror instead of typing the verbs out. That was the reason
    // for reading the registry, and this is the day it paid.
    stubFetch(() => ok(LEDGER));
    render(
      <WorkbenchRunsPage
        payload={payload({ runs: [], timeline: [] })}
        projectId="proj_1"
        datastreamId="ds_1"
        onConfirmed={vi.fn()}
      />,
    );

    const panel = await screen.findByTestId("recovery-verbs");
    fireEvent.click(within(panel).getByTestId("recovery-verbs-trigger"));
    const spends = (key: string) =>
      within(panel).getByTestId(`recovery-cost-${key}`).textContent ?? "";
    expect(spends("bounded_synchronize")).toMatch(/spend on the account/);
    expect(spends("bounded_reload")).toMatch(/spend on the account/);

    // The two that spend say where the delivered gesture lives, so a person who
    // wants yesterday's data again is sent somewhere rather than stopped.
    const covered = within(panel).getByTestId("recovery-verb-bounded_reload");
    expect(covered.textContent).toContain("Day-by-day coverage");
    expect(covered.textContent).toContain("Retired");

    // And the built verb is NOT refused here. A panel that still explained why
    // Reprocess cannot run would be the console and the server disagreeing about
    // what this build does -- the exact defect the mirror exists to prevent.
    expect(within(panel).queryByTestId("recovery-verb-bounded_reprocess")).toBeNull();
    expect(panel.textContent).not.toContain("Not built yet");
    expect(unbuiltRecoveryVerbs().map((v) => v.origin.key)).not.toContain(
      "bounded_reprocess",
    );
  });

  it("offers no control for a verb it cannot grant, and asks the server for nothing", async () => {
    // A control a person can press for a verb that always refuses is the defect,
    // not a softer version of it. The panel carries no button and no input, and
    // the tab never reaches the bounded endpoints.
    const calls = stubFetch(() => ok(LEDGER));
    render(
      <WorkbenchRunsPage
        payload={payload({ runs: [], timeline: [] })}
        projectId="proj_1"
        datastreamId="ds_1"
        onConfirmed={vi.fn()}
      />,
    );

    const panel = await screen.findByTestId("recovery-verbs");
    // AMENDED 2026-08-18: the panel now has exactly ONE button, and it is the
    // disclosure — a control that reveals a refusal, never one that asks for it.
    // Once open, the verbs themselves still carry none.
    const trigger = within(panel).getByTestId("recovery-verbs-trigger");
    expect(within(panel).queryAllByRole("button")).toEqual([trigger]);
    fireEvent.click(trigger);
    const detail = within(panel).getByTestId("recovery-verbs-detail");
    expect(within(detail).queryAllByRole("button").length).toBe(0);
    expect(within(panel).queryAllByRole("textbox").length).toBe(0);
    expect(panel.querySelectorAll("input").length).toBe(0);
    expect(calls.some((call) => call.url.includes("/bounded/"))).toBe(false);
  });

  it("reads the verbs from the registry, so an engine that lands removes one", () => {
    // The list is a JOIN on `has_engine`, never a second table of three names.
    // The day the server grants a verb, this panel stops naming it without a
    // second edit anybody has to remember.
    const shown = unbuiltRecoveryVerbs().map((verb) => verb.origin.key);
    const refused = RUN_ORIGINS.filter((origin) => !origin.has_engine).map((o) => o.key);
    expect(shown).toEqual(refused);
    expect(shown.length).toBeGreaterThan(0);
  });
});

/**
 * Story 63.5 — the tab that lists the runs that ENDED must also show the one
 * that is moving, above them, and it must not describe two different
 * measurements with one word.
 */
describe("Runs tab — the live collection", () => {
  const PROGRESS = {
    schema: "datastream_progress.v1",
    project_id: "proj_1",
    datastream_id: "ds_1",
    progress: {
      execution_id: "dse_1",
      state: "loading",
      step: "Collect",
      day_in_progress: "2026-07-01",
      days_done: 31,
      days_total: 90,
      windows_done: 1,
      windows_total: 3,
      window_in_progress: {
        date_from: "2026-08-01", date_to: "2026-08-31", days: 31,
        started_at: "2026-08-06T00:00:00+00:00", completed_at: null,
      },
      rows_written: 412_000,
      started_at: "2026-08-06T00:00:00+00:00",
      progress_updated_at: "2026-08-06T00:04:00+00:00",
      plan_version_id: "dpv_1",
      mapping_version_id: "dmv_1",
      estimate: {
        armed: true, observations: 4, minimum_observations: 3, precision: "point",
        seconds_remaining: 2_940, seconds_remaining_low: 1_470, seconds_remaining_high: 4_410,
        spread_ratio: 2, behind_by_seconds: 0, measured_at: "2026-08-06T00:04:00+00:00",
        reason: null, sentence: "About 49 minutes left.",
      },
    },
    idle: null,
  };

  const IDLE = {
    schema: "datastream_progress.v1",
    project_id: "proj_1",
    datastream_id: "ds_1",
    progress: null,
    idle: {
      reason: "last_run_succeeded",
      execution_id: "dse_1",
      state: "collected",
      ended_at: "2026-08-05T23:09:00+00:00",
      error_code: null,
    },
  };

  // Story 63.7: the two runs carry the two paths a Datastream actually mixes --
  // a nightly collection in flight and the mapping change that preceded it.
  const RUNS = [
    { id: "dse_1", state: "loading", created_at: "2026-08-06T00:00:00+00:00", row_count: 999_999, rows_written: 412_000, origin: "scheduler_nightly" },
    { id: "dse_0", state: "published", created_at: "2026-07-06T00:00:00+00:00", row_count: 777_777, rows_written: null, origin: "mapping_change" },
  ];

  function stubWith(progress: unknown) {
    return stubFetch((url) => (url.includes("/progress") ? ok(progress) : ok(LEDGER)));
  }

  it("does not draw the live band a second time — the route above owns it", async () => {
    // AMENDED 2026-08-11 (Jean), amendment 3. This tab mounted `DatastreamRunLive`
    // and so does `DatastreamWorkbenchRoute`, above `NavTabs`: one subject drawn
    // twice on one Datastream, a screen apart, over one poll. The route's mount
    // is the one that survives -- from there the band is read from all six tabs,
    // which is the reason story 63.5 gave for putting it there. What the band
    // SAYS is measured by `DatastreamRunLive.test.tsx`, and that it is drawn
    // exactly once on `…/runs` by `DatastreamRunLiveParity.test.tsx`.
    stubWith(PROGRESS);
    render(
      <WorkbenchRunsPage
        payload={payload({ runs: RUNS, timeline: [] })}
        projectId="proj_1"
        datastreamId="ds_1"
        onConfirmed={vi.fn()}
      />,
    );

    // The tab's own evidence is intact, and none of it came from the band.
    const [firstRun] = await findRunBlocks();
    expect(firstRun).toBeTruthy();
    expect(screen.getByTestId("coverage-strip")).toBeTruthy();
    expect(screen.queryByTestId("datastream-run-live")).toBeNull();
  });

  it("names ONE rows column, reads ONE column, and says when it was never written", async () => {
    // `row_count` is what a publication reported; `rows_written` is what this
    // run collected. Under one heading they were two numbers of the same run.
    stubWith(PROGRESS);
    render(
      <WorkbenchRunsPage
        payload={payload({ runs: RUNS, timeline: [] })}
        projectId="proj_1"
        datastreamId="ds_1"
        onConfirmed={vi.fn()}
      />,
    );
    await findRunBlocks();
    const runs = historyText();

    expect(runs).toContain("Rows collected");
    expect(screen.queryByText("Rows")).toBeNull();
    expect(runs).toContain("412,000");
    expect(runs).not.toContain("999,999");
    // An execution older than migration 218 wrote no collection counter, and
    // the fact says so instead of falling back to the other measurement.
    expect(runs).toContain("Not measured");
    expect(runs).not.toContain("777,777");
  });

  it("keeps its own evidence when nothing is collecting, and still draws no band", async () => {
    // Why nothing is collecting is the band's sentence, and the band is the
    // route's -- `DatastreamRunLive.test.tsx` measures both. What this tab owes
    // an idle Datastream is its history and its coverage, and it owes them
    // whether or not anything is in flight.
    stubWith(IDLE);
    render(
      <WorkbenchRunsPage
        payload={payload({ runs: RUNS, timeline: [] })}
        projectId="proj_1"
        datastreamId="ds_1"
        onConfirmed={vi.fn()}
      />,
    );
    expect((await findRunBlocks()).length).toBe(2);
    expect(screen.getByTestId("coverage-strip")).toBeTruthy();
    expect(screen.queryByTestId("datastream-run-live")).toBeNull();
  });
});

describe("Runs tab — why each run exists (story 63.7)", () => {
  const RUNS_WITH_ORIGIN = [
    { id: "dse_1", state: "loading", created_at: "2026-08-06T00:00:00+00:00", rows_written: 1, origin: "scheduler_nightly" },
    { id: "dse_0", state: "published", created_at: "2026-07-06T00:00:00+00:00", rows_written: null, origin: "mapping_change" },
    { id: "dse_OLD", state: "published", created_at: "2026-06-06T00:00:00+00:00", rows_written: null, origin: null },
    { id: "dse_NEW", state: "published", created_at: "2026-05-06T00:00:00+00:00", rows_written: null, origin: "an_origin_no_build_knows" },
  ];

  function renderRuns(runs: unknown[]) {
    stubFetch(() => ok(LEDGER));
    return render(
      <WorkbenchRunsPage
        payload={payload({ runs, timeline: [] })}
        projectId="proj_1"
        datastreamId="ds_1"
        onConfirmed={vi.fn()}
      />,
    );
  }

  it("names the path each run came from, on the run's own block", async () => {
    renderRuns(RUNS_WITH_ORIGIN);
    await findRunBlocks();
    const runs = historyText();

    // The registry's words, for two different paths on one list -- the whole
    // point of the field: before it, a nightly collection and a mapping change
    // were two identical rows.
    expect(runs).toContain("Nightly collection");
    expect(runs).toContain("Mapping change");
  });

  it("says Not measured for a run minted before the origin existed", async () => {
    renderRuns([RUNS_WITH_ORIGIN[2]]);
    const [block] = await findRunBlocks();
    // Two facts read `Not measured` on this run -- the collection counter and
    // the origin -- and neither is a `0` or an invented word.
    expect(within(block).getAllByText("Not measured").length).toBeGreaterThanOrEqual(2);
    expect(within(block).getByTestId("run-origin").textContent).toBe("Not measured");
  });

  it("shows an origin it does not know as it arrived, never folded", async () => {
    renderRuns([RUNS_WITH_ORIGIN[3]]);
    const [block] = await findRunBlocks();
    expect(within(block).getByText("an_origin_no_build_knows")).toBeTruthy();
    for (const entry of RUN_ORIGINS) {
      expect(within(block).queryByText(entry.label)).toBeNull();
    }
  });

  it("leaves the history intact when nothing is running", async () => {
    // The origin belongs to the HISTORY, not to the live band: a Datastream at
    // rest still has to say what its past runs were.
    stubFetch((url) =>
      url.includes("/progress")
        ? ok({ schema: "datastream_progress.v1", project_id: "proj_1", datastream_id: "ds_1",
               progress: null, idle: { reason: "never_ran", execution_id: null, state: null,
                                       ended_at: null, error_code: null } })
        : ok(LEDGER),
    );
    render(
      <WorkbenchRunsPage
        payload={payload({ runs: RUNS_WITH_ORIGIN, timeline: [] })}
        projectId="proj_1"
        datastreamId="ds_1"
        onConfirmed={vi.fn()}
      />,
    );
    await findRunBlocks();
    expect(historyText()).toContain("Nightly collection");
    // And the band above says nothing about an origin: a run that has ended is
    // no longer a treatment to watch.
    expect(screen.queryByTestId("run-live-origin")).toBeNull();
  });
});

/**
 * Story 58.10 — the run blocks replaced the table, and the two mechanisms this
 * file exists for must survive that: the strip that says which days are missing,
 * and the panel that says which repairs this build cannot grant.
 */
describe("Runs tab — the strip and the verb standings survive the run blocks", () => {
  const RUNS = [
    { id: "dse_EXAMPLE_1", state: "collected", created_at: "2026-08-06T00:00:00+00:00", rows_written: 4, origin: "scheduler_nightly" },
  ];

  it("keeps the coverage strip and the re-collection panel ABOVE the first run block", async () => {
    stubFetch(() => ok(LEDGER));
    render(
      <WorkbenchRunsPage
        payload={payload({ runs: RUNS, timeline: [], step_vocabulary: ["Collect", "Map", "Check", "Publish"] })}
        projectId="proj_1"
        datastreamId="ds_1"
        onConfirmed={vi.fn()}
      />,
    );

    const strip = await screen.findByTestId("coverage-strip");
    const [block] = await findRunBlocks();
    expect(screen.getByTestId("recovery-verbs")).toBeTruthy();
    expect(strip.compareDocumentPosition(block) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("puts the delivered repair ABOVE the verbs that are not delivered", async () => {
    // Order is the sentence. The gesture a person can actually perform comes
    // first; what the build cannot grant comes after it, so the tab never opens
    // on a list of things that refuse.
    stubFetch(() => ok(LEDGER));
    render(
      <WorkbenchRunsPage
        payload={payload({ runs: RUNS, timeline: [] })}
        projectId="proj_1"
        datastreamId="ds_1"
        onConfirmed={vi.fn()}
      />,
    );

    const strip = await screen.findByTestId("coverage-strip");
    const standings = screen.getByTestId("recovery-verbs");
    expect(strip.compareDocumentPosition(standings) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect((await findRunBlocks()).length).toBe(1);
  });
});
