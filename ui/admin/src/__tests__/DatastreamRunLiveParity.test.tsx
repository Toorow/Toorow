/**
 * The same live state at the three places -- story 63.5, and this is the test
 * that carries the story.
 *
 * WHAT IT MEASURES, AND WHY IT IS NOT A RENDERING DETAIL. Until 2026-08-06 each
 * mount of `usePolledRead` owned its own timer, its own backoff and its own
 * quiet-cadence counter, so two surfaces of one Datastream opened two polls of
 * one address -- and the one that slowed to fifteen seconds sat beside the one
 * still reading every five, showing numbers measured up to fifteen seconds
 * apart. Both exact. Side by side. That is "two surfaces, two answers", which
 * story 53.9 forbids, and it is what the registry in `lib/polledRead.ts` closes.
 *
 * AMENDED 2026-08-11 (Jean), amendment 3 of that review. The two surfaces this
 * file was written to keep in step are now ONE: the route mounts the band above
 * `NavTabs`, and `WorkbenchRunsPage` mounted it a second time a screen below --
 * one subject drawn twice on one Datastream, which is the defect a step up from
 * the one the registry closed. So the assertion inverts: on `…/runs` there is
 * exactly ONE band. That is strictly stronger than the parity it replaces --
 * two readings cannot disagree when there is one of them -- and the shared
 * registry is still measured, because the fleet list and this band remain two
 * readers of one address.
 *
 * The third surface is the fleet list, and it deliberately says less: the state
 * only, read off `evidence.latest_candidate_state`, which is already on the wire
 * for every row. A poll per row would be 40 x 17_280 requests a night at forty
 * Datastreams, and a day count that moves only when the whole fleet is reloaded
 * would be a number that lies between reloads. What the three surfaces must
 * agree on is what they BOTH say -- the state of the run.
 *
 * The clock is frozen, because "read 0 s ago" must be the same sentence in both
 * bands for a comparison of their text to mean anything at all.
 */
import { act, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import DatastreamWorkbenchRoute from "../datastreams/workbench/DatastreamWorkbenchRoute";
import DataWorkspace from "../shell/pages/DataWorkspace";
import { executionStateLabel } from "../datastreams/workbench/executionStates";
import { originLabel, originSentence } from "../datastreams/workbench/runOrigins";

const PROJECT = "proj_EXAMPLE";
const DATASTREAM = "ds_EXAMPLE";
const RUN_STATE = "loading";
const RUN_ORIGIN = "mapping_change";
const NOW = 1_772_000_000_000;

/** ONE payload, read by the three surfaces. */
const PROGRESS = {
  schema: "datastream_progress.v1",
  project_id: PROJECT,
  datastream_id: DATASTREAM,
  progress: {
    execution_id: "dse_EXAMPLE",
    state: RUN_STATE,
    // Story 63.7: WHY it is running travels on the same one reading.
    origin: RUN_ORIGIN,
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
  },
  idle: null,
};

const HEADER = {
  schema: "datastream_workbench.header.v1",
  identity: {
    datastream_id: DATASTREAM,
    project_id: PROJECT,
    name: "Campaign performance",
    mode: "connector_pull",
    data_role: "fact",
    owner: "owner@example.com",
    module: "meta-ads",
    source_account_ref: "acct_EXAMPLE",
    declared_writer: null,
    business_domains: [],
  },
  axes: { lifecycle: "Active", configuration: "Ready", operations: "Healthy", publication: "Current" },
  versions: { active_plan: "dpv_EXAMPLE", active_mapping: "dmv_EXAMPLE", proposed_plan: null, proposed_mapping: null },
  operations_evidence: { next_run_at: null, missed_run_count: 0, schedule_state_known: true, late_reasons: [] },
  runs: { latest: "dse_EXAMPLE", latest_state: RUN_STATE },
  publications: { candidate: null, current: "dse_PUBLISHED", last_known_good: "dse_PUBLISHED" },
  links: {},
  primary_action: { kind: "prepare_change", label: "Prepare change", reason: "Stable.", tab: "processing" },
};

/**
 * The run list of the tab. The first run carries BOTH counters, and they
 * disagree on purpose: `row_count` is what a publication reported, `rows_written`
 * is what this run collected. The second predates migration 218 and has no
 * collection counter at all.
 */
const RUNS_PAYLOAD = {
  schema: "datastream_workbench.runs.v1",
  tab: "runs",
  project_id: PROJECT,
  datastream_id: DATASTREAM,
  evidence: {
    runs: [
      {
        id: "dse_EXAMPLE",
        state: RUN_STATE,
        created_at: "2026-08-06T00:00:00+00:00",
        row_count: 999_999,
        rows_written: 412_000,
        origin: RUN_ORIGIN,
      },
      {
        id: "dse_OLDER",
        state: "published",
        created_at: "2026-07-06T00:00:00+00:00",
        row_count: 777_777,
        rows_written: null,
        // Older than migration 218 (no collection counter) but not older than
        // story 63.7: the two absences are independent, and only one of them is
        // this run's.
        origin: "scheduler_nightly",
      },
    ],
    runs_limit: 200,
    runs_truncated: false,
    timeline: [],
  },
};

const FLEET = {
  schema_version: "data-datastreams.v1",
  project_ref: { object_type: "project", id: PROJECT },
  generated_at: "2026-08-06T00:04:00Z",
  evidence_as_of: "2026-08-06T00:04:00Z",
  items: [
    {
      object_ref: { object_type: "datastream", id: DATASTREAM },
      name: "Campaign performance",
      source_kind: "connector",
      connector_ref: { object_type: "connector", id: "meta-ads" },
      states: { lifecycle: "active", validation: "executable", publication: "published", health: "available" },
      evidence: { latest_candidate_state: RUN_STATE },
      evidence_as_of: "2026-08-06T00:04:00Z",
      links: {},
    },
    {
      object_ref: { object_type: "datastream", id: "ds_AT_REST" },
      name: "Panel extract",
      source_kind: "connector",
      connector_ref: null,
      states: { lifecycle: "active", validation: "executable", publication: "published", health: "available" },
      // A run that ENDED. The lock is free, so nothing is collecting.
      evidence: { latest_candidate_state: "collected" },
      evidence_as_of: "2026-08-06T00:04:00Z",
      links: {},
    },
  ],
  unavailable_reasons: [],
  allowed_actions: [],
};

function ok(body: unknown): Response {
  return { ok: true, status: 200, json: () => Promise.resolve(body) } as unknown as Response;
}

let urls: string[] = [];

function stubFetch(): void {
  urls = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      urls.push(url);
      if (url.includes("/progress")) return Promise.resolve(ok(PROGRESS));
      if (url.includes("/ledger")) return Promise.resolve(ok({ ledger: [] }));
      if (url.includes("/workbench/runs")) return Promise.resolve(ok(RUNS_PAYLOAD));
      if (url.includes("/workbench")) return Promise.resolve(ok(HEADER));
      return Promise.resolve(ok(FLEET));
    }),
  );
}

async function flush(): Promise<void> {
  await act(async () => {
    for (let i = 0; i < 12; i += 1) await Promise.resolve();
  });
}

function progressCalls(): string[] {
  return urls.filter((url) => url.includes("/progress"));
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
  stubFetch();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

it("draws the live band ONCE on the Runs tab, and says what the payload says", async () => {
  render(
    <DatastreamWorkbenchRoute
      projectId={PROJECT}
      datastreamId={DATASTREAM}
      tab="runs"
      onNavigateTab={vi.fn()}
    />,
  );
  await flush();

  // ONE band -- amendment 3 of the 2026-08-11 review. This tab is where the two
  // mounts were on screen together, so it is where the second one is refused.
  const bands = screen.getAllByTestId("datastream-run-live");
  expect(bands).toHaveLength(1);
  // And it says what the payload says, through the registry's label.
  expect(bands[0].textContent).toContain(executionStateLabel(RUN_STATE));
  expect(bands[0].textContent).toContain("31 of 90 days collected");
  expect(bands[0].textContent).toContain("412,000");
  expect(bands[0].textContent).toContain("About 49 minutes left.");
  // The age of the reading is part of the sentence.
  expect(bands[0].textContent).toContain("Read 0 s ago");
  // Story 63.7: and so is WHY it is running -- one sentence, composed once.
  const sentence = originSentence(RUN_ORIGIN, "2026-08-06T00:00:00+00:00")!;
  expect(sentence).toContain(originLabel(RUN_ORIGIN)!);
  expect(bands[0].textContent).toContain(sentence);
});

/**
 * The history of this tab, as story 58.10 renders it: ONE BLOCK PER RUN, read
 * as the words it puts on screen. The five-column table it replaced answered
 * "which run" and never "what did this run do".
 */
function historyText(): string {
  return Array.from(document.querySelectorAll("[data-testid='run-block']"))
    .map((block) => block.textContent ?? "")
    .join("\n");
}

it("says the same origin in the band and in the run blocks it sits above", async () => {
  // Two readers of the SAME field on one screen: the band reads
  // `progress.origin` from the polled payload, each run block reads `origin`
  // off its own run. One registry resolves both, so a person cannot be shown
  // "Mapping change" in one and something else two centimetres below.
  render(
    <DatastreamWorkbenchRoute
      projectId={PROJECT}
      datastreamId={DATASTREAM}
      tab="runs"
      onNavigateTab={vi.fn()}
    />,
  );
  await flush();

  const label = originLabel(RUN_ORIGIN)!;
  const bands = screen.getAllByTestId("datastream-run-live");
  expect(bands[0].textContent).toContain(label);
  expect(historyText()).toContain(label);
});

it("reads the progress address ONCE, and one tick advances it by one read", async () => {
  render(
    <DatastreamWorkbenchRoute
      projectId={PROJECT}
      datastreamId={DATASTREAM}
      tab="runs"
      onNavigateTab={vi.fn()}
    />,
  );
  await flush();

  expect(screen.getAllByTestId("datastream-run-live")).toHaveLength(1);
  expect(progressCalls()).toHaveLength(1);
  expect(progressCalls()[0]).toBe(
    `/api/projects/${PROJECT}/datastreams/${DATASTREAM}/progress`,
  );

  // One timer, and it stays one: the registry in `lib/polledRead.ts` is what
  // guarantees it whatever the number of surfaces watching, and the fleet list
  // still shares this address with the band.
  await act(async () => {
    vi.advanceTimersByTime(5_000);
    for (let i = 0; i < 12; i += 1) await Promise.resolve();
  });
  expect(progressCalls()).toHaveLength(2);
});

it("never puts row_count and rows_written under the same word", async () => {
  render(
    <DatastreamWorkbenchRoute
      projectId={PROJECT}
      datastreamId={DATASTREAM}
      tab="runs"
      onNavigateTab={vi.fn()}
    />,
  );
  await flush();

  const runs = historyText();
  // The fact names the measurement, and there is no bare "Rows" anywhere to
  // collide with the band above.
  expect(runs).toContain("Rows collected");
  expect(screen.queryByText("Rows")).not.toBeInTheDocument();
  // The collection counter, the same one the band shows.
  expect(runs).toContain("412,000");
  // The publication counter of the same run is NOT shown here.
  expect(runs).not.toContain("999,999");
  // And a run older than migration 218 says its counter was never written,
  // rather than silently falling back to the other number.
  expect(runs).toContain("Not measured");
  expect(runs).not.toContain("777,777");
});

it("says on the fleet list which Datastream is collecting, and asks nothing to find out", async () => {
  render(<DataWorkspace projectId={PROJECT} />);
  await flush();

  const rows = within(screen.getByRole("table")).getAllByRole("row");
  const moving = rows.find((row) => row.textContent?.includes("Campaign performance"))!;
  const atRest = rows.find((row) => row.textContent?.includes("Panel extract"))!;

  // The same word as the two bands, for the same run.
  expect(within(moving).getByText(executionStateLabel(RUN_STATE))).toBeInTheDocument();
  // A run that ended is not a collection in flight, and the row says so rather
  // than leaving the cell blank.
  expect(within(atRest).getByText("Not collecting")).toBeInTheDocument();
  expect(within(atRest).queryByText(executionStateLabel("collected"))).not.toBeInTheDocument();

  // THE COST: nothing was polled. One read of the fleet endpoint, and no read
  // of the progress address at all -- forty rows would otherwise be forty
  // timers and 691_200 requests a night. Story 57.12 added ONE more one-shot
  // read beside the fleet: the setup-drafts list behind the Resume door. It is
  // named, counted and not a poll; the pin this test guards is unchanged.
  expect(progressCalls()).toHaveLength(0);
  expect(urls.filter((url) => !url.includes("/datastream-setup-drafts"))).toHaveLength(1);
  expect(urls.filter((url) => url.includes("/datastream-setup-drafts"))).toHaveLength(1);
});
