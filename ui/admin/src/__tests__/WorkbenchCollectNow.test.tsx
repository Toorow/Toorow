/**
 * Asking a Datastream to collect NOW, and what has to be true before it spends.
 *
 * WHAT THIS FILE HOLDS OPEN — added 2026-08-18, and each claim names what its
 * absence cost:
 *
 *   * the gesture EXISTS at all. `SchedulePanel` offers the cadence `manual`,
 *     "This Datastream only runs when someone asks it to", and nothing in the
 *     console asked. `POST /api/datastreams/{id}/run` shipped in story 8.2 and
 *     was never mounted; measured on the live base, the ONE Datastream that is
 *     active, enabled and mapped runs `manual`, so no clock would ever pick it
 *     up and no screen could;
 *   * the WINDOW is named before the spend, and it is the SERVER'S window. The
 *     confirmation must state connector, account and window before anything is
 *     asked of the provider; composing the window in the console would have made
 *     a second arbitration free to drift from `pull_window.resolve_window`, and
 *     a person would confirm one window and pay for another;
 *   * a REFUSAL is never dressed as a confirmation. The window is read first, so
 *     a gated Datastream shows the gate and the gesture that releases it, rather
 *     than a dialog with two ways out and no act;
 *   * nothing is spent when the scope could not be read.
 *
 * `fetch` is stubbed rather than `apiFetch`, so `apiSeamGuard.test.ts` keeps
 * proving the bearer is attached one level below.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import WorkbenchOverviewPage from "../datastreams/workbench/pages/WorkbenchOverviewPage";

const HEADER = {
  schema: "datastream_workbench.header.v1",
  identity: {
    datastream_id: "ds_EXAMPLE",
    project_id: "proj_EXAMPLE",
    name: "Search Console — daily",
    mode: "connector_pull",
    data_role: "fact",
    owner: "owner@example.com",
    module: "search-console",
    connector: "search-console",
    source_account_ref: "sacc_EXAMPLE",
    declared_writer: null,
    business_domains: [],
  },
  axes: {},
  versions: {},
  runs: { latest: null, latest_state: null },
  publications: { candidate: null, current: null, last_known_good: null },
  links: {},
  primary_action: { kind: "prepare_change", label: "Prepare change", reason: "Settled.", tab: "mapping" },
} as never;

const EVIDENCE = {
  state: "available",
  schedule: null,
  mapping_health: { version_id: null },
  dq_monitors: { count: 0, published: 0 },
  downstream_count: 0,
  stage_coverage: [],
};

/** The window the server would really collect — 30 days ending on J-1. */
const PREVIEW = {
  datastream_id: "ds_EXAMPLE",
  project_id: "proj_EXAMPLE",
  date_from: "2026-07-19",
  date_to: "2026-08-17",
  window_days: 30,
  window_source: "date_window_days",
  window_widened_for: null,
  offset_days: 1,
  cadence: "manual",
  refusal: null,
};

function json(body: unknown, status = 200) {
  return Promise.resolve(
    new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }),
  );
}

function stubApi(options: {
  preview?: unknown;
  previewStatus?: number;
  runStatus?: number;
  run?: unknown;
} = {}) {
  const calls: Array<{ url: string; method: string }> = [];
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      calls.push({ url, method: (init?.method ?? "GET").toUpperCase() });
      if (url.includes("/run/preview")) {
        if (options.previewStatus && options.previewStatus !== 200) {
          return json({ code: "db_error", message: "the schedule could not be read" }, options.previewStatus);
        }
        return json(options.preview ?? PREVIEW);
      }
      if (url.includes("/run")) {
        if (options.runStatus && options.runStatus !== 202) {
          return json({ code: "queue_error", message: "the queue refused this window" }, options.runStatus);
        }
        return json(options.run ?? { job_id: "job_EXAMPLE", pull_id: "pull_EXAMPLE", state: "queued" }, 202);
      }
      if (url.includes("/ledger")) return json({ ledger: [] });
      if (url.includes("/schedule")) {
        return json({
          cadence: "manual", enabled: true, lifecycle_state: "active", run_state: "running",
          archived: false, window_days: 30, window_offset_days: 1, window_source: "explicit",
          arrival_hour_local: null, arrival_hour_source: "unset", timezone: "UTC",
          timezone_source: "fallback", on_failure: "nothing_is_retried", retry_count: 0,
          next_run_at: null, last_run_at: null, never_ran: true,
        });
      }
      return json({ code: "not_stubbed", message: `unexpected call to ${url}` }, 500);
    }),
  );
  return calls;
}

function mount(header: unknown = HEADER, evidence: Record<string, unknown> = EVIDENCE) {
  return render(
    <WorkbenchOverviewPage
      header={header as never}
      payload={{ evidence } as never}
      projectId="proj_EXAMPLE"
      datastreamId="ds_EXAMPLE"
      connector="search-console"
      sourceAccountRef="sacc_EXAMPLE"
      onNavigateTab={vi.fn() as never}
    />,
  );
}

describe("Collect now", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("offers the gesture on the Collect stage, beside the schedule it obeys", () => {
    stubApi();
    mount();

    // On the stage that owns the cadence -- not on `Runs`, which would separate
    // the act from the setting that governs it.
    expect(screen.getByTestId("stage-collect-now")).toBeInTheDocument();
    expect(screen.getByTestId("stage-collect-action")).toBeInTheDocument();
  });

  it("names the connector, the account and the window BEFORE anything is spent", async () => {
    const calls = stubApi();
    mount();

    fireEvent.click(screen.getByTestId("stage-collect-now"));

    const dialog = await screen.findByTestId("collect-now-confirm");
    // The three the surface requires by name...
    expect(dialog).toHaveTextContent("search-console");
    expect(dialog).toHaveTextContent("sacc_EXAMPLE");
    // ...and the window, as the SERVER composed it -- both bounds and the count.
    expect(dialog).toHaveTextContent("2026-07-19 → 2026-08-17");
    expect(dialog).toHaveTextContent("30 days");

    // And NOTHING has been asked of the provider at this point: the only call is
    // the read. A confirmation that had already spent would be a receipt.
    expect(calls.some((call) => call.method === "POST")).toBe(false);
  });

  it("never invents a spend figure, and says why there is none", async () => {
    stubApi();
    mount();
    fireEvent.click(screen.getByTestId("stage-collect-now"));

    const dialog = await screen.findByTestId("collect-now-confirm");
    expect(dialog).toHaveTextContent("Not measured");
    // The reason travels with the absence -- `NOT_MEASURED` alone is an apology.
    expect(dialog).toHaveTextContent(/quota engine publishes an open\/closed breaker/);
  });

  it("says a fallback window is a fallback, not this Datastream's setting", async () => {
    stubApi({
      preview: { ...PREVIEW, window_days: 3, window_source: "defensive_default",
        date_from: "2026-08-15", date_to: "2026-08-17" },
    });
    mount();
    fireEvent.click(screen.getByTestId("stage-collect-now"));

    const dialog = await screen.findByTestId("collect-now-confirm");
    // Three days because the row declares nothing -- read as "the window is
    // three days", that is a setting nobody chose.
    expect(dialog).toHaveTextContent(/declares no retrieval window/);
  });

  it("starts the run only on the confirmation, and reports what was queued", async () => {
    const calls = stubApi();
    mount();

    fireEvent.click(screen.getByTestId("stage-collect-now"));
    await screen.findByTestId("collect-now-confirm");
    fireEvent.click(screen.getByTestId("collect-now-go"));

    await waitFor(() => expect(screen.getByText("Collection queued")).toBeInTheDocument());
    const run = calls.filter((call) => call.method === "POST" && call.url.includes("/run"));
    expect(run).toHaveLength(1);
    // The window is NOT pinned from the console: the route resolves it again
    // from the same row, so a clock that moved between the read and the click
    // does not leave this screen the author of a stale window.
    expect(run[0].url).not.toContain("date_from");
  });

  it("a window already in flight is not reported as a second spend", async () => {
    stubApi({ run: { job_id: "job_EXAMPLE", state: "running", deduplicated: true } });
    mount();

    fireEvent.click(screen.getByTestId("stage-collect-now"));
    await screen.findByTestId("collect-now-confirm");
    fireEvent.click(screen.getByTestId("collect-now-go"));

    await waitFor(() =>
      expect(screen.getByText(/already in flight, so no second collection/)).toBeInTheDocument(),
    );
  });

  /**
   * A REFUSAL IS NOT A CONFIRMATION.
   *
   * The window is read before the dialog opens, so a gated Datastream never
   * reaches a dialog whose only acts are two ways out. The gate lands where the
   * button was pressed, carrying the gesture that releases it.
   */
  it("shows a named gate instead of a confirmation, and spends nothing", async () => {
    const calls = stubApi({
      preview: {
        ...PREVIEW,
        refusal: {
          code: "not_armed",
          message: "This Datastream is stopped. Start it on the Schedule panel before running it.",
        },
      },
    });
    mount();

    fireEvent.click(screen.getByTestId("stage-collect-now"));

    await waitFor(() =>
      expect(screen.getByText("This Datastream will not collect yet")).toBeInTheDocument(),
    );
    expect(screen.getByText(/Start it on the Schedule panel/)).toBeInTheDocument();
    // No dialog was opened at all...
    expect(screen.queryByTestId("collect-now-confirm")).not.toBeInTheDocument();
    // ...and no run was started.
    expect(calls.some((call) => call.method === "POST")).toBe(false);
  });

  it("an archived Datastream is told to be RESTORED, not started", async () => {
    // The sentence `gate_refusal` now answers for the row a real archive leaves
    // behind. Until 2026-08-18 it read the `lifecycle_state` word nothing
    // writes, and answered "Start it on the Schedule panel" -- a panel which,
    // for an archived Datastream, refuses to start anything.
    stubApi({
      preview: {
        ...PREVIEW,
        refusal: {
          code: "not_armed",
          message: "This Datastream is archived. Restore it before collecting anything for it.",
        },
      },
    });
    mount();

    fireEvent.click(screen.getByTestId("stage-collect-now"));

    await waitFor(() => expect(screen.getByText(/Restore it before collecting/)).toBeInTheDocument());
    expect(screen.queryByText(/Start it on the Schedule panel/)).not.toBeInTheDocument();
  });

  it("a pushed source is not offered a fetch it has nothing to fetch", () => {
    stubApi();
    mount({
      ...(HEADER as object),
      identity: { ...(HEADER as never as { identity: object }).identity, mode: "managed_feed" },
    });

    // ABSENT, not disabled: a greyed control with no word reads as a bug, and
    // the stage's own detail already says how a file reaches this Datastream.
    expect(screen.queryByTestId("stage-collect-now")).not.toBeInTheDocument();
    expect(screen.getByTestId("stage-collect-action")).toBeInTheDocument();
  });

  it("asks the provider for nothing when the scope could not be read", async () => {
    const calls = stubApi({ previewStatus: 500 });
    mount();

    fireEvent.click(screen.getByTestId("stage-collect-now"));

    await waitFor(() =>
      expect(screen.getByText("Nothing was asked of the provider")).toBeInTheDocument(),
    );
    expect(calls.some((call) => call.method === "POST")).toBe(false);
  });

  it("keeps the confirmation open on a refused run, carrying the server's sentence", async () => {
    stubApi({ runStatus: 500 });
    mount();

    fireEvent.click(screen.getByTestId("stage-collect-now"));
    await screen.findByTestId("collect-now-confirm");
    fireEvent.click(screen.getByTestId("collect-now-go"));

    // Closing on a refusal leaves a person believing the run started (63.6).
    await waitFor(() =>
      expect(screen.getByText(/the queue refused this window/)).toBeInTheDocument(),
    );
    expect(screen.getByTestId("collect-now-confirm")).toBeInTheDocument();
    expect(screen.queryByText("Collection queued")).not.toBeInTheDocument();
  });
});
