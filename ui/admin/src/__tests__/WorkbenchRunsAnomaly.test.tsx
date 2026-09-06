/**
 * The anomaly, unfolded inside its run — story 59.1.
 *
 * 58.10 laid the region and left a sentence saying the rows, the export and the
 * acknowledgement were this story's. These tests pin the four things that could
 * silently go wrong once they land:
 *
 *   * the four facts of arbitrage 7 are FOUR sentences, pairwise distinct, and
 *     the fourth one is rendered ONCE and outside the run blocks — because it is
 *     a fact about the Datastream, not about any run;
 *   * unfolding an issue costs a read of the warehouse, so it happens on the
 *     click and exactly once — never per run of the list;
 *   * a profile with no replayable rows shows its own sentence, no empty table
 *     and no `Download`: an export that can export nothing is a button that
 *     lies, and the screen holds no list of profile names to decide it with;
 *   * `Mark as reviewed` reaches the NEW route under the Workbench base, which
 *     moves `app.dq_issues` — not the `/api/dq/*` route story 49.4 unmounted.
 *
 * `fetch` is stubbed rather than `apiFetch`, matching the workbench tests
 * (`WorkbenchRunsCoverage.test.tsx:17-19`): the seam guard is what proves the
 * bearer is attached, and stubbing one level lower would hide it.
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import WorkbenchRunsPage from "../datastreams/workbench/pages/WorkbenchRunsPage";
import {
  EVALUATIONS_WITHOUT_RUN_ABSENCE,
  NO_ANOMALY_ON_RUN,
  NO_MONITOR_EVALUATED,
  REPLAYED_NOW,
} from "../datastreams/workbench/RunAnomalies";
import type { WorkbenchTabPayload } from "../datastreams/workbench/workbenchTypes";

function payload(evidence: Record<string, unknown>): WorkbenchTabPayload {
  return {
    schema: "datastream_workbench.runs.v1",
    tab: "runs",
    project_id: "proj_1",
    datastream_id: "ds_1",
    evidence: { timeline: [], ...evidence },
  };
}

function ok(body: unknown, status = 200) {
  return {
    ok: status < 400,
    status,
    json: async () => body,
    blob: async () => ({}) as Blob,
  } as Response;
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

/** The replay route, whatever query it carries. The ledger's calls are not it. */
function rowCalls(calls: Call[]): Call[] {
  return calls.filter((call) => call.url.includes("/anomalies/") && call.url.includes("/rows"));
}

const NULL_RATE_ISSUE = {
  id: "dqi_EXAMPLE_1",
  monitor_id: "dqm_EXAMPLE_1",
  monitor_label: "Null rate: example flux",
  check_profile: "null_rate",
  status: "open",
  severity: "degrading",
  first_seen_at: "2026-08-04T02:10:00Z",
  last_seen_at: "2026-08-05T02:10:00Z",
  replayable_rows: true,
  rows_absence_reason: null,
  rows_absence_message: null,
};

const ZERO_ROWS_ISSUE = {
  id: "dqi_EXAMPLE_2",
  monitor_id: "dqm_EXAMPLE_2",
  monitor_label: "Zero rows: example flux",
  check_profile: "zero_rows",
  status: "open",
  severity: "blocking",
  first_seen_at: "2026-08-04T02:10:00Z",
  last_seen_at: "2026-08-05T02:10:00Z",
  replayable_rows: false,
  rows_absence_reason: "no_faulty_row_by_nature",
  rows_absence_message:
    "This monitor's finding is not about particular rows: there is no faulty row to replay.",
};

const REPLAY = {
  issue_id: "dqi_EXAMPLE_1",
  replayable_rows: true,
  replayed_at: "2026-08-08T09:00:00Z",
  columns: ["date", "campaign_id"],
  rows: [{ date: "2026-08-04", campaign_id: null }],
  row_count: 1,
  truncated: false,
  note: null,
  note_message: null,
  reason: null,
  message: null,
};

function renderRuns(evidence: Record<string, unknown>) {
  return render(
    <WorkbenchRunsPage
      payload={payload(evidence)}
      projectId="proj_1"
      datastreamId="ds_1"
      onConfirmed={vi.fn()}
    />,
  );
}

/** The FIRST run's anomaly region, unfolded. A list can carry several. */
async function anomalyRegion(): Promise<HTMLElement> {
  await waitFor(() => expect(screen.getAllByTestId("run-anomalies").length).toBeGreaterThan(0));
  const block = screen.getAllByTestId("run-anomalies")[0];
  fireEvent.click(within(block).getByTestId("run-anomalies-trigger"));
  return block;
}

describe("Runs tab — the anomaly panel, story 59.1", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows the issue's monitor, severity and dates inside the run block", async () => {
    stubFetch(() => ok({ ledger: [] }));
    renderRuns({
      runs: [
        {
          id: "dse_EXAMPLE_1",
          state: "published",
          anomalies: { anomalies: 1, evaluations: 1, issues: [NULL_RATE_ISSUE] },
        },
      ],
      evaluations_without_run: 0,
    });

    const region = await anomalyRegion();
    const issue = within(region).getByTestId("run-anomaly");
    expect(within(issue).getByTestId("run-anomaly-monitor").textContent).toBe(
      "Null rate: example flux",
    );
    // The severity reads as a sentence now: its private map is deleted and the
    // three words are declared in the shared vocabulary (76-2 review).
    expect(issue.textContent).toContain("Degrading");
    // The anomaly is INSIDE its run's block, never in a portal.
    const runBlock = document.querySelector("[data-testid='run-block']");
    expect(runBlock?.contains(issue)).toBe(true);
  });

  it("unfolding an issue costs ONE read, and the list pays for none", async () => {
    const calls = stubFetch((url) =>
      url.includes("/anomalies/") ? ok(REPLAY) : ok({ ledger: [] }),
    );
    renderRuns({
      runs: [
        {
          id: "dse_EXAMPLE_1",
          state: "published",
          anomalies: { anomalies: 1, evaluations: 1, issues: [NULL_RATE_ISSUE] },
        },
        {
          id: "dse_EXAMPLE_2",
          state: "published",
          anomalies: { anomalies: 1, evaluations: 1, issues: [NULL_RATE_ISSUE] },
        },
      ],
      evaluations_without_run: 0,
    });

    const region = await anomalyRegion();
    // NOTHING was read for the two runs the list displays.
    expect(rowCalls(calls).length).toBe(0);

    fireEvent.click(within(region).getAllByTestId("run-anomaly-rows-trigger")[0]);
    await waitFor(() => expect(rowCalls(calls).length).toBe(1));
    expect(rowCalls(calls)[0].url).toContain(
      "/api/projects/proj_1/datastreams/ds_1/workbench/runs/dse_EXAMPLE_1/anomalies/dqi_EXAMPLE_1/rows",
    );

    // Folding and unfolding again does not pay for a second read.
    fireEvent.click(within(region).getAllByTestId("run-anomaly-rows-trigger")[0]);
    fireEvent.click(within(region).getAllByTestId("run-anomaly-rows-trigger")[0]);
    await waitFor(() => expect(rowCalls(calls).length).toBe(1));
  });

  it("says the rows are today's, not the detection's", async () => {
    stubFetch((url) => (url.includes("/anomalies/") ? ok(REPLAY) : ok({ ledger: [] })));
    renderRuns({
      runs: [
        {
          id: "dse_EXAMPLE_1",
          state: "published",
          anomalies: { anomalies: 1, evaluations: 1, issues: [NULL_RATE_ISSUE] },
        },
      ],
      evaluations_without_run: 0,
    });

    const region = await anomalyRegion();
    fireEvent.click(within(region).getByTestId("run-anomaly-rows-trigger"));
    const rows = await within(region).findByTestId("run-anomaly-rows");
    expect(rows.textContent).toContain(REPLAYED_NOW);
    await waitFor(() => expect(rows.textContent).toContain("campaign_id"));
  });

  it("a replay that matches nothing says so, and never 'no rows were returned'", async () => {
    stubFetch((url) =>
      url.includes("/anomalies/")
        ? ok({
            ...REPLAY,
            rows: [],
            row_count: 0,
            note: "nothing_matches_now",
            note_message: "Nothing matches this condition now.",
          })
        : ok({ ledger: [] }),
    );
    renderRuns({
      runs: [
        {
          id: "dse_EXAMPLE_1",
          state: "published",
          anomalies: { anomalies: 1, evaluations: 1, issues: [NULL_RATE_ISSUE] },
        },
      ],
      evaluations_without_run: 0,
    });

    const region = await anomalyRegion();
    fireEvent.click(within(region).getByTestId("run-anomaly-rows-trigger"));
    const empty = await within(region).findByTestId("run-anomaly-empty");
    expect(empty.textContent).toBe("Nothing matches this condition now.");
  });

  it("a profile with no replayable rows shows its sentence, no table and no Download", async () => {
    stubFetch(() => ok({ ledger: [] }));
    renderRuns({
      runs: [
        {
          id: "dse_EXAMPLE_1",
          state: "published",
          anomalies: { anomalies: 1, evaluations: 1, issues: [ZERO_ROWS_ISSUE] },
        },
      ],
      evaluations_without_run: 0,
    });

    const region = await anomalyRegion();
    expect(within(region).getByTestId("run-anomaly-no-rows").textContent).toBe(
      ZERO_ROWS_ISSUE.rows_absence_message,
    );
    expect(within(region).queryByTestId("run-anomaly-rows-trigger")).toBeNull();
    expect(within(region).queryByText("Download")).toBeNull();
    expect(region.querySelector("table")).toBeNull();
  });

  it("Mark as reviewed reaches the new route and shows the status the server read back", async () => {
    const calls = stubFetch((url) =>
      url.includes("/transitions")
        ? ok({ issue_id: "dqi_EXAMPLE_1", status: "acknowledged" })
        : ok({ ledger: [] }),
    );
    renderRuns({
      runs: [
        {
          id: "dse_EXAMPLE_1",
          state: "published",
          anomalies: { anomalies: 1, evaluations: 1, issues: [NULL_RATE_ISSUE] },
        },
      ],
      evaluations_without_run: 0,
    });

    const region = await anomalyRegion();
    fireEvent.click(within(region).getByTestId("run-anomaly-acknowledge"));

    await waitFor(() =>
      expect(
        within(region).getByTestId("run-anomaly-status").textContent,
      ).toBe("acknowledged"),
    );
    const transition = calls.filter((call) => call.url.includes("/transitions"));
    expect(transition.length).toBe(1);
    expect(transition[0].url).toBe(
      "/api/projects/proj_1/datastreams/ds_1/workbench/anomalies/dqi_EXAMPLE_1/transitions",
    );
    expect(transition[0].init?.method).toBe("POST");
    // NOT the route story 49.4 unmounted, and not `app.alert_firings`.
    expect(calls.every((call) => !call.url.includes("/api/dq/"))).toBe(true);
  });

  it("the four facts of arbitrage 7 are four distinct sentences", async () => {
    stubFetch(() => ok({ ledger: [] }));
    renderRuns({
      runs: [
        {
          id: "dse_EXAMPLE_1",
          state: "published",
          anomalies: { anomalies: 1, evaluations: 1, issues: [NULL_RATE_ISSUE] },
        },
        {
          id: "dse_EXAMPLE_2",
          state: "published",
          anomalies: { anomalies: 0, evaluations: 2, issues: [] },
        },
        {
          id: "dse_EXAMPLE_3",
          state: "published",
          anomalies: { anomalies: 0, evaluations: 0, issues: [] },
        },
      ],
      evaluations_without_run: 5,
    });

    await screen.findByTestId("run-anomalies");
    const blocks = Array.from(
      document.querySelectorAll("[data-testid='run-block']"),
    ) as HTMLElement[];

    // 1. detail under its run; 2. checked and clean; 3. no evaluation named it.
    const detail = within(blocks[0]).getByTestId("run-anomalies-trigger").textContent ?? "";
    const clean = within(blocks[1]).getByTestId("run-anomalies-none").textContent ?? "";
    const unevaluated = within(blocks[2]).getByTestId("run-anomalies-none").textContent ?? "";
    expect(clean).toBe(NO_ANOMALY_ON_RUN);
    expect(unevaluated).toBe(NO_MONITOR_EVALUATED);

    // 4. the flux-grain fact, ONCE and OUTSIDE every run block.
    const withoutRun = screen.getAllByTestId("evaluations-without-run");
    expect(withoutRun.length).toBe(1);
    expect(withoutRun[0].textContent).toContain("5 evaluation(s)");
    expect(withoutRun[0].textContent).toContain(EVALUATIONS_WITHOUT_RUN_ABSENCE);
    expect(blocks.some((block) => block.contains(withoutRun[0]))).toBe(false);

    // FOUR SENTENCES ARE SIX PAIRS, and the arbitrage is that a person can tell
    // all four facts apart. One inequality proves two of them are distinct and
    // says nothing about the other four pairs.
    const said = [detail, clean, unevaluated, withoutRun[0].textContent ?? ""];
    expect(said.every((sentence) => sentence.trim().length > 0)).toBe(true);
    const pairs: Array<[number, number]> = [];
    for (let i = 0; i < said.length; i += 1) {
      for (let j = i + 1; j < said.length; j += 1) pairs.push([i, j]);
    }
    expect(pairs.length).toBe(6);
    for (const [i, j] of pairs) {
      expect(said[i]).not.toBe(said[j]);
      // Not merely different strings: neither may be a prefix or a fragment of
      // the other, which is how two facts come to read as one on a screen.
      expect(said[i].includes(said[j])).toBe(false);
      expect(said[j].includes(said[i])).toBe(false);
    }
  });

  it("says nothing about evaluations without a run when the count was not read", async () => {
    // ABSENT IS NOT ZERO. A payload that carries no count has measured nothing,
    // and a `0 evaluations could not name a run` would be a measurement nobody
    // took.
    stubFetch(() => ok({ ledger: [] }));
    renderRuns({
      runs: [{ id: "dse_EXAMPLE_1", state: "published" }],
    });

    await waitFor(() => expect(screen.getByTestId("runs-count")).toBeTruthy());
    expect(screen.queryByTestId("evaluations-without-run")).toBeNull();
  });

  it("a run with no anomaly gets no region at all", async () => {
    stubFetch(() => ok({ ledger: [] }));
    renderRuns({
      runs: [
        {
          id: "dse_EXAMPLE_1",
          state: "published",
          anomalies: { anomalies: 0, evaluations: 0, issues: [] },
        },
      ],
      evaluations_without_run: 0,
    });

    await waitFor(() => expect(screen.getByTestId("runs-count")).toBeTruthy());
    expect(screen.queryByTestId("run-anomalies")).toBeNull();
    expect(screen.getByTestId("run-anomalies-none").textContent).toBe(NO_MONITOR_EVALUATED);
  });
});
