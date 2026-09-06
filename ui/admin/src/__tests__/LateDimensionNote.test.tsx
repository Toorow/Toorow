/**
 * Why a reading is hollow — amendment 14 of the 2026-08-11 review, on the three
 * surfaces where the hollow is actually drawn.
 *
 * WHAT WAS RED. The amendment was delivered on `Processing`, where a change is
 * COMPOSED: `dimensionDebt.ts` states, before the click, that a dimension being
 * added exists on none of the collected days. The READING surfaces — the day
 * grid and `Read one day`, both on `Data` — carried none of it. A dimension
 * added last month therefore drew empty over every older day with nothing on the
 * screen saying that was why, which is the amendment's own forbidden case:
 * « un trou muet est un bug que l'opérateur impute au produit ».
 *
 * THREE SURFACES, THREE ASSERTIONS, ONE MEASUREMENT. The sentence is derived in
 * `lateDimensionNote.ts` from the server's `dimension_history` payload — the
 * same block `Processing` reads, now served on the `data` tab too. These tests
 * hold what each surface must say, and just as hard what it must NOT: a note
 * over a window nothing predates, or over a day that already carried the field,
 * would send somebody to re-collect days that are complete.
 */
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import WorkbenchDataPage from "../datastreams/workbench/pages/WorkbenchDataPage";
import DateBreakdownGrid from "../datastreams/workbench/DateBreakdownGrid";
import DayReadingPanel from "../datastreams/workbench/DayReadingPanel";
import type { WorkbenchTabPayload } from "../datastreams/workbench/workbenchTypes";

const WINDOW = { start: "2026-07-10", end: "2026-07-13", bounded_at: 92, bound_reached: false };

const COLUMNS = [
  { source_field: "media_date", target_field: "media_date", is_key_column: true,
    binding_status: "confirmed" },
  { source_field: "device", target_field: "device", is_key_column: false,
    binding_status: "confirmed" },
];

const AVAILABLE = [
  { mode: "collected", available: true, relation: "raw_meta_ads_daily", reason: null },
  { mode: "mapped", available: true, relation: "stg_meta_ads_daily", reason: null },
];

/**
 * The day-window payload of `/daily-breakdown` — trimmed to what these walks read.
 *
 * Two days landed on the 10th and 11th, and `device` enters the plan on the 12th.
 * So both landed days predate it, and neither of them carries the column: the
 * exact shape the amendment is about.
 */
function breakdown(overrides: Record<string, unknown> = {}) {
  return {
    schema: "datastream_daily_breakdown.v1",
    project_id: "proj_EXAMPLE",
    datastream_id: "ds_EXAMPLE",
    connector: "meta-ads",
    window: WINDOW,
    view_mode: { requested: "collected", served: "collected", note: null, available: AVAILABLE },
    stage_relations: {
      report_profile_id: "campaign_daily",
      collected_relation: "raw_meta_ads_daily",
      mapped_relation: "stg_meta_ads_daily",
      reason: null, message: null,
    },
    reading: null,
    versions: { plan_version_id: "dsp_1", mapping_version_id: "dmap_1",
                version_binding_available: false },
    rows_note: null,
    columns: COLUMNS,
    columns_reason: null,
    columns_source: "mapping_version",
    column_row_join_available: false,
    column_row_join_reason: "mart_metric_is_a_dbt_literal",
    days: [
      { date: "2026-07-10", extract_status: "ok", job_state: "done", extract_count: 1,
        row_count: 150, row_count_reason: null, rows: 4, rows_reason: null,
        execution_id: "dse_1", provenance: null, dq_verdict: null, dq_reason: "no_monitor" },
      { date: "2026-07-11", extract_status: "ok", job_state: "done", extract_count: 1,
        row_count: 120, row_count_reason: null, rows: 3, rows_reason: null,
        execution_id: "dse_2", provenance: null, dq_verdict: null, dq_reason: "no_monitor" },
    ],
    reason: null,
    country: { active: false, capability_state: "draft", degraded: false },
    ...overrides,
  };
}

/**
 * `core/datastream_dimension_history.py`'s own payload shape.
 *
 * `landed` is the only day state that can owe a dimension, and `landed_at` is
 * the instant that proves it: a plan version created after a day landed cannot
 * have been executed for it. That is the `predates_declaration` fact
 * `dimensionDebt` already relies on, and the only one exact without a run.
 */
function history(overrides: Record<string, unknown> = {}) {
  return {
    state: "measured",
    window: { from: "2026-07-10", to: "2026-07-13", days: 4 },
    basis: "what each day was ASKED to carry",
    unmeasurable: "what a provider actually returned for a past day",
    refetch_path: null,
    max_provider_backfill_days: null,
    backfill_bound_evidence: "unavailable",
    earliest_recoverable: null,
    days: [
      { date: "2026-07-10", state: "landed", execution_id: "dse_1",
        landed_at: "2026-07-10T04:00:00+00:00", plan_version_id: "dsp_1" },
      { date: "2026-07-11", state: "landed", execution_id: "dse_2",
        landed_at: "2026-07-11T04:00:00+00:00", plan_version_id: "dsp_1" },
    ],
    counts: { landed: 2, landed_empty: 0, not_landed: 0, in_flight: 0, never_collected: 2 },
    plan_dimensions: { dsp_1: ["media_date"] },
    first_declared: {
      media_date: { plan_version_id: "dsp_1", version_number: 1,
                    created_at: "2026-07-01T09:00:00+00:00" },
      device: { plan_version_id: "dsp_2", version_number: 2,
                created_at: "2026-07-12T09:00:00+00:00" },
    },
    ...overrides,
  };
}

function tabPayload(dimensionHistory: unknown): WorkbenchTabPayload {
  return {
    schema: "datastream_workbench.data.v1",
    tab: "data",
    project_id: "proj_EXAMPLE",
    datastream_id: "ds_EXAMPLE",
    evidence: {
      state: "unavailable",
      sample_state: "unavailable",
      sample_reason:
        "No sample and no export can be drawn here — this Datastream names no source"
        + " to read from, and the day-by-day reading above is the only reading it has.",
      availability: {
        collected: "unavailable", mapped: "unavailable",
        processed: "unavailable", published: "unavailable",
      },
      availability_reason: {
        collected: null, mapped: null, processed: null, published: null,
      },
      stages: [],
      dimension_history: dimensionHistory,
    },
  };
}

function stubBreakdown(body: unknown) {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/daily-breakdown")) {
      return { ok: true, status: 200, json: async () => body } as Response;
    }
    // The tab mounts a matching-opportunities panel of its own; it is not the
    // subject here and must not decide the outcome, so it is answered rather
    // than left to throw.
    return { ok: true, status: 200, json: async () => ({ matches: [] }) } as Response;
  }));
}

/** The reading of one day, at the position the panel opens on. */
function reading(day: string) {
  return {
    status: "ok" as const,
    reading: {
      day,
      capabilities: [] as Record<string, unknown>[],
      collected: {
        relation: "raw_meta_ads_daily", zone: "main",
        columns: ["date", "device"],
        rows: [{ date: day, device: null }],
        row_count: 1, truncated: false, masked_fields: [],
        reason: null, message: null, note: null, note_message: null,
      },
      mapped: {
        relation: "stg_meta_ads_daily", zone: "main_staging",
        columns: ["date", "device"],
        rows: [{ date: day, device: null }],
        row_count: 1, truncated: false, masked_fields: [],
        reason: null, message: null, note: null, note_message: null,
      },
      pairing: { available: false, reason: "no_bound_field", message: "Nothing pairs.",
                 columns: [], key_columns: [], unpaired_columns: [], rows: [],
                 row_count: 0, paired_row_count: 0, unpaired_row_count: 0,
                 truncated: false, bounded_at: 50 },
    },
  };
}

afterEach(() => vi.unstubAllGlobals());

describe("The day grid says why its window is hollow", () => {
  it("names the dimension, the day it arrived and how many days predate it", () => {
    render(
      <DateBreakdownGrid
        breakdown={breakdown() as never}
        dimensionHistory={history() as never}
      />,
    );

    const note = screen.getByTestId("late-dimension-note");
    // The dimension, not a count of them: a person has to know WHICH column.
    expect(note).toHaveTextContent(/device entered this plan on 2026-07-12/);
    // And how much of the window it explains, so the note is read against
    // something rather than as a general warning.
    expect(note).toHaveTextContent(/2 day\(s\) of this window collected before then/);
    // The amendment's own last clause: the hole is a fact, not a failure.
    expect(note).toHaveTextContent(/not a failed collection/);
  });

  it("says nothing at all when no dimension arrived after a collected day", () => {
    // A note that always appears explains nothing, and this one would send a
    // person to re-collect days that already carry every column.
    render(
      <DateBreakdownGrid
        breakdown={breakdown() as never}
        dimensionHistory={history({
          first_declared: {
            media_date: { plan_version_id: "dsp_1", version_number: 1,
                          created_at: "2026-07-01T09:00:00+00:00" },
            device: { plan_version_id: "dsp_1", version_number: 1,
                      created_at: "2026-07-01T09:00:00+00:00" },
          },
        }) as never}
      />,
    );

    expect(screen.queryByTestId("late-dimension-note")).toBeNull();
  });

  it("says nothing when the history could not be measured", () => {
    // `unreadable` is NEVER "no day is missing" on the server, and it may not
    // become a sentence here either. Same for a grid mounted with no history at
    // all — the component sheet mounts it that way.
    render(
      <DateBreakdownGrid
        breakdown={breakdown() as never}
        dimensionHistory={history({ state: "unreadable", days: undefined }) as never}
      />,
    );
    expect(screen.queryByTestId("late-dimension-note")).toBeNull();

    render(<DateBreakdownGrid breakdown={breakdown() as never} />);
    expect(screen.queryByTestId("late-dimension-note")).toBeNull();
  });
});

describe("Read one day says why THIS day is hollow", () => {
  it("explains a day collected before the dimension entered the plan", () => {
    render(
      <DayReadingPanel
        breakdown={breakdown() as never}
        reading={reading("2026-07-10") as never}
        day="2026-07-10"
        dimensionHistory={history() as never}
      />,
    );

    const note = screen.getByTestId("day-late-dimension-note");
    expect(note).toHaveTextContent(/collected before device entered the plan on 2026-07-12/);
    // The distinction the whole amendment turns on.
    expect(note).toHaveTextContent(/never asked for — not because the source returned nothing/);
  });

  it("says nothing for a day the dimension was already asked for", () => {
    render(
      <DayReadingPanel
        breakdown={breakdown() as never}
        reading={reading("2026-07-11") as never}
        day="2026-07-11"
        dimensionHistory={history({
          days: [
            { date: "2026-07-10", state: "landed", execution_id: "dse_1",
              landed_at: "2026-07-10T04:00:00+00:00", plan_version_id: "dsp_1" },
            // Landed AFTER the declaration: this day carries the column.
            { date: "2026-07-11", state: "landed", execution_id: "dse_2",
              landed_at: "2026-07-13T04:00:00+00:00", plan_version_id: "dsp_2" },
          ],
        }) as never}
      />,
    );

    expect(screen.queryByTestId("day-late-dimension-note")).toBeNull();
  });
});

describe("The Data tab hands the measurement down", () => {
  it("renders the note from the tab payload, without a second request for it", async () => {
    stubBreakdown(breakdown());

    render(
      <WorkbenchDataPage
        payload={tabPayload(history())}
        projectId="proj_EXAMPLE"
        datastreamId="ds_EXAMPLE"
      />,
    );

    await waitFor(() =>
      expect(screen.getByTestId("late-dimension-note")).toHaveTextContent(
        /device entered this plan on 2026-07-12/,
      ),
    );

    // ONE READING OF ONE FACT. The tab payload already carries the history, so a
    // request of this page's own for it would be a second answer to one
    // question — and the day the two disagreed the screen would be the last to
    // know. Nothing this page called names the history route.
    const calls = (globalThis.fetch as unknown as { mock: { calls: unknown[][] } }).mock.calls;
    const urls = calls.map((call) => String(call[0]));
    expect(urls.some((url) => url.includes("dimension_history"))).toBe(false);
  });

  it("draws no note when the tab payload carries no history", async () => {
    stubBreakdown(breakdown());

    render(
      <WorkbenchDataPage
        payload={tabPayload(null)}
        projectId="proj_EXAMPLE"
        datastreamId="ds_EXAMPLE"
      />,
    );

    // The grid IS drawn — so the absence below is the note's absence and not the
    // page having failed to render at all.
    await waitFor(() => expect(screen.getByTestId("daily-breakdown-grid")).toBeInTheDocument());
    expect(screen.queryByTestId("late-dimension-note")).toBeNull();
  });
});
