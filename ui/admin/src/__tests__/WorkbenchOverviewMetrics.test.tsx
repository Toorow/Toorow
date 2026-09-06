/**
 * The four named metrics of `Overview`.
 *
 * `datastream-workbench-and-wizard.md:74` asks this tab for "cadence, freshness
 * and history coverage; latest run", and the validated mockup leads with four
 * numbers: freshness, run success over 30 days, latest volume, next run. The tab
 * showed none of them — and could not have, because `read_tab("overview")`
 * composed no field for any of them. The four slots were filled with publication
 * IDENTIFIERS instead, at headline weight.
 */
import { render, screen, within } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import WorkbenchOverviewPage from "../datastreams/workbench/pages/WorkbenchOverviewPage";

const HEADER = {
  identity: { name: "Search Console — daily", owner: "owner@example.com" },
  axes: {}, versions: {}, runs: { latest: "run_EXAMPLE", latest_state: "Ready" },
  publications: { candidate: null, current: "pub_EXAMPLE", last_known_good: "pub_EXAMPLE" },
  links: {}, primary_action: { kind: "prepare_change", label: "Prepare change", reason: "Settled.", tab: "mapping" },
} as never;

// Story 58.9: the tab now mounts `CoverageStrip`, which reads the extract
// ledger. `fetch` is stubbed rather than `apiFetch` — the seam guard
// (`apiSeamGuard.test.ts`) is what proves the bearer is attached, and stubbing
// one level lower would hide it.
beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(
    new Response(JSON.stringify({ ledger: [] }), {
      status: 200, headers: { "Content-Type": "application/json" },
    }),
  ));
});

function mount(evidence: Record<string, unknown>) {
  return render(
    <WorkbenchOverviewPage
      header={HEADER}
      payload={{ evidence: { state: "available", ...evidence } } as never}
      projectId="proj_EXAMPLE"
      datastreamId="ds_EXAMPLE"
    />,
  );
}

it("leads with freshness, run success, volume and next run", () => {
  const twoHoursAgo = new Date(Date.now() - 2 * 3_600_000).toISOString();
  mount({
    published_at: twoHoursAgo,
    published_row_count: 18420,
    run_success: { window_days: 30, terminal_count: 120, succeeded_count: 119 },
    schedule: { next_run_at: "2026-08-03T04:00:00Z", mode: "daily" },
  });

  expect(screen.getByText("2h")).toBeInTheDocument();
  expect(screen.getByText("119/120")).toBeInTheDocument();
  // The thin no-break space `formatPercent` puts before the sign, matched as
  // `\s` rather than pinned as a codepoint.
  expect(screen.getByText(/99\.1\s*%|99\.2\s*%/)).toBeInTheDocument();
  expect(screen.getByText("18,420")).toBeInTheDocument();
});

it("counts success over TERMINAL runs only, so an in-flight run is not a failure", () => {
  // `created/loading/validating/ready/publishing` are not outcomes — migration
  // 042's state machine makes only published/failed/cancelled terminal. Counting
  // an in-flight run as a failure would make a healthy Datastream look broken
  // every time someone opened this tab mid-run.
  mount({ run_success: { window_days: 30, terminal_count: 4, succeeded_count: 4 } });
  expect(screen.getByText("4/4")).toBeInTheDocument();
  expect(screen.getByText(/100\.0\s*% · last 30 days/)).toBeInTheDocument();
});

it("says so rather than inventing a number when nothing has published", () => {
  mount({ run_success: { window_days: 30, terminal_count: 0, succeeded_count: 0 } });

  expect(screen.getByText("No run yet")).toBeInTheDocument();
  expect(screen.getByText("Nothing published yet")).toBeInTheDocument();
  // A zero here would read as "0 rows published", which is a different claim
  // from "we have never published".
  expect(screen.getAllByText("Unavailable").length).toBeGreaterThan(0);
});

it("does not print Next run twice on one screen", () => {
  mount({ schedule: { next_run_at: "2026-08-03T04:00:00Z", mode: "daily" } });
  expect(screen.getAllByText("Next run")).toHaveLength(1);
});

/**
 * Mapping health — the FOURTH thing this tab's function names, and the one no
 * repair carried until now.
 *
 * `_configuration_axis` tested `configuration_blocked`, a key `grep -rn` finds
 * READ at one line of this repository and WRITTEN at none. Its `Blocked` branch
 * could not fire, so a Datastream whose active mapping refuses to execute
 * reported `Ready` — the axis said the configuration was fine precisely when it
 * was not.
 */
it("says when the mapping blocks, and how far it is from executable", () => {
  mount({ mapping_health: { version_id: "dmap_EXAMPLE", executable: false, blocking_count: 2 } });

  expect(screen.getByText("Mapping is not executable")).toBeInTheDocument();
  // The COUNT is the actionable half: `executable: false` alone says yes or no,
  // the count says how far.
  expect(screen.getByText(/2 bindings still block this mapping/)).toBeInTheDocument();
});

it("separates 'no mapping at all' from 'blocked', because the work differs", () => {
  mount({ mapping_health: { version_id: null } });

  expect(screen.getByText("No mapping version")).toBeInTheDocument();
  // Sending someone to review bindings that do not exist is worse than silence.
  expect(screen.queryByText(/still block this mapping/)).not.toBeInTheDocument();
});

it("stays silent when the mapping is healthy — a settled state is not a warning", () => {
  mount({ mapping_health: { version_id: "dmap_EXAMPLE", executable: true, blocking_count: 0 } });

  expect(screen.queryByText(/Mapping is not executable/)).not.toBeInTheDocument();
  expect(screen.queryByText("No mapping version")).not.toBeInTheDocument();
});

/**
 * Story 58.9 — the four metrics SURVIVE the four stages.
 *
 * The stages name the same facts the headline numbers do: the next run, the
 * mapping, the downstream count. Printing any of them twice at headline weight
 * would make a reader check whether the two agree, which is the exact reason
 * `Next run` was taken out of the posture list below in the first place.
 */
it("keeps the four headline metrics when the four stages are drawn", () => {
  const twoHoursAgo = new Date(Date.now() - 2 * 3_600_000).toISOString();
  mount({
    published_at: twoHoursAgo,
    published_row_count: 18420,
    run_success: { window_days: 30, terminal_count: 120, succeeded_count: 119 },
    schedule: { next_run_at: "2026-08-03T04:00:00Z", mode: "daily" },
    mapping_health: { version_id: "dmap_EXAMPLE", executable: true, blocking_count: 0 },
    downstream_count: 3,
    dq_monitors: { count: 0, published: 0 },
  });

  expect(screen.getByText("2h")).toBeInTheDocument();
  expect(screen.getByText("119/120")).toBeInTheDocument();
  expect(screen.getByText("18,420")).toBeInTheDocument();
  // The four stages are there and the metric label is still printed ONCE.
  expect(screen.getByTestId("pipeline")).toBeInTheDocument();
  expect(screen.getAllByText("Next run")).toHaveLength(1);
});

/**
 * THE ORDER, AND WHAT SURVIVED THE THREE PANELS THAT CLOSED — finding D-4.
 *
 * The tab was 3742px tall with the flow eighth. The ratified target says « cette
 * page EST le flux », so the flow leads and the rest is read against it. These
 * three tests are the ones that fail if a later change quietly reorders the page
 * or drops a fact while tidying it.
 */
it("leads with the flow, then the one next step, then the numbers", () => {
  mount({ schedule: { next_run_at: "2026-08-03T04:00:00Z", mode: "daily" } });

  const flow = screen.getByTestId("pipeline");
  const nextStep = screen.getByText("Next step · Prepare change");
  const metrics = screen.getByText("Run success");
  const following = Node.DOCUMENT_POSITION_FOLLOWING;

  // `compareDocumentPosition` reads the DOM order, which is the reading order —
  // jsdom lays nothing out, so this is the only order there is to pin.
  expect(flow.compareDocumentPosition(nextStep) & following).toBeTruthy();
  expect(nextStep.compareDocumentPosition(metrics) & following).toBeTruthy();
});

it("answers freshness once, and says which fact each half is", () => {
  const twoHoursAgo = new Date(Date.now() - 2 * 3_600_000).toISOString();
  mount({
    published_at: twoHoursAgo,
    schedule: { next_run_at: "2026-08-03T04:00:00Z", mode: "daily", last_committed_watermark: "2026-07-28T00:00:00Z" },
  });

  // ONE `Freshness`. The page carried a `Freshness` metric AND a `Freshness
  // watermark` row 3000px apart, and they answered different questions under
  // one word: the age of the publication, and the data boundary collection has
  // committed up to.
  expect(screen.getAllByText("Freshness")).toHaveLength(1);
  expect(screen.queryByText("Freshness watermark")).toBeNull();
  expect(screen.getByText("2h")).toBeInTheDocument();
  // Both facts are still on the page, each saying what it measures.
  expect(screen.getByText(/^Published .* · data collected through .*$/)).toBeInTheDocument();
});

it("keeps every pointer of the publication panel it stopped repeating", () => {
  // The four tiles said `Waiting / Served / Retained / None` — the words of the
  // `Publication` axis one screen higher, and of the `Outputs` tab, which also
  // opens the run behind each role. What only they carried was the exact
  // pointer, and that is what stayed.
  mount({ schedule: { next_run_at: "2026-08-03T04:00:00Z", mode: "daily" } });

  expect(screen.getByText("Exact references")).toBeInTheDocument();
  expect(screen.getByText("run_EXAMPLE")).toBeInTheDocument();
  expect(screen.getAllByText("pub_EXAMPLE")).toHaveLength(2);
  // A null candidate is a real answer, never an unreadable one — `ObjectId`
  // renders "Unavailable" for an absent value, which would be a different fact.
  const candidate = screen.getByText("Candidate publication").parentElement!;
  expect(within(candidate).getByText("None")).toBeInTheDocument();
  expect(within(candidate).queryByText("Unavailable")).toBeNull();
});

/** `0 / 4 stages` is gone: with no stage evidence — which is every Datastream of
 *  preprod — it printed a fraction of a measurement nobody took. */
it("never prints a stage fraction where no stage evidence exists", () => {
  mount({ stage_coverage: [], run_success: { window_days: 30, terminal_count: 0, succeeded_count: 0 } });

  expect(screen.getByText("No stage has recorded any evidence")).toBeInTheDocument();
  expect(screen.queryByText(/0 \/ 4 stages/)).not.toBeInTheDocument();
  // The stage that has none is still NAMED, which was always the useful half.
  expect(
    screen.getByText("No evidence: Collected, Mapped, Processed, Published"),
  ).toBeInTheDocument();
});
