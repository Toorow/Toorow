/**
 * The `Data` tab read BY DAY — story 58.2.
 *
 * What these tests hold is the set of things a grid over an extract registry can
 * quietly get wrong, each of which reads as a measurement:
 *
 *   * a volume the route sent as `null` rendered as `0`;
 *   * a header pill with nothing in it where a field is simply unmapped;
 *   * a country or a cost column — or the space held open for one — on a tab
 *     whose evidence carries no capability state at all (amendment 2 of
 *     `datastream-workbench-and-wizard.md`, « Une capacité activée AJOUTE son
 *     onglet » — named rather than numbered, because that document's lines move);
 *   * "the mart has no cells for these days" collapsed into "this broke".
 *
 * NO GEOMETRY IS ASSERTED HERE, and none may be. `vitest.config.ts` sets
 * `environment: "jsdom"` and `css: false`: no stylesheet is applied and no layout is computed,
 * so `offsetWidth` and `getBoundingClientRect` answer zeros. Five test files in
 * this repository stub them by hand, which measures the stub. The 52px row, the
 * pill that must not take two lines and the column that must not leave the frame
 * at 1128px are measured by `scripts/measure_component_sheet.py` against a real
 * browser, or they are not measured at all.
 *
 * `fetch` is stubbed rather than `apiFetch`: the seam guard
 * (`apiSeamGuard.test.ts`) is what proves the bearer is attached, and stubbing
 * one level lower would hide it.
 */
import { useState } from "react";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import WorkbenchDataPage from "../datastreams/workbench/pages/WorkbenchDataPage";
import DateBreakdownGrid from "../datastreams/workbench/DateBreakdownGrid";
import DatastreamWorkbenchRoute from "../datastreams/workbench/DatastreamWorkbenchRoute";
import type { Tab } from "../shell/pages/datastreamTabs";
import type { WorkbenchTabPayload } from "../datastreams/workbench/workbenchTypes";

/** The tab evidence the page already renders below the grid — untouched by 58.2. */
const TAB_PAYLOAD: WorkbenchTabPayload = {
  schema: "datastream_workbench.data.v1",
  tab: "data",
  project_id: "proj_EXAMPLE",
  datastream_id: "ds_EXAMPLE",
  evidence: {
    state: "unavailable",
    sample_state: "unavailable",
    // The server's sentence for a stream that names no source — one sentence,
    // no story, no route (finding D-7 of review #69).
    sample_reason:
      "No sample and no export can be drawn here — this Datastream names no source"
      + " to read from, and the day-by-day reading above is the only reading it has.",
    availability: {
      collected: "unavailable", mapped: "unavailable",
      processed: "unavailable", published: "unavailable",
    },
    // WHY each one is greyed — story 58.3, arbitrage 8. Three of the four are
    // unavailable on every flux of the product, and until now none of them said
    // so. The sentence is the server's; the screen renders it.
    availability_reason: {
      collected: "No collected evidence was persisted for an exact execution of this Datastream, so there is nothing to filter this table to.",
      mapped: "No mapped evidence was persisted for an exact execution of this Datastream, so there is nothing to filter this table to.",
      processed: "No processed evidence was persisted for an exact execution of this Datastream, so there is nothing to filter this table to.",
      published: "No published evidence was persisted for an exact execution of this Datastream, so there is nothing to filter this table to.",
    },
    stages: [],
  },
};

const WINDOW = { start: "2026-07-10", end: "2026-07-13", bounded_at: 92, bound_reached: false };

const COLUMNS = [
  // The grain field, which the reader puts first.
  { source_field: "media_date", target_field: "media_date_98245b", is_key_column: true,
    binding_status: "confirmed" },
  // An identity rename — 44 of the 44 rows of the flat store are one of these.
  { source_field: "clicks", target_field: "clicks", is_key_column: false,
    binding_status: "confirmed" },
  // And a field nothing binds yet: a state of the mapping, not a failure.
  { source_field: "audience_label", target_field: null, is_key_column: false,
    binding_status: "suggested" },
];

/**
 * The reading of one day — story 58.3.
 *
 * Every value here is already `[MASKED]` where the server masked it: masking is a
 * server-side refusal by default and the browser never sees the raw value, so a
 * fixture that carried one would be describing a payload that cannot exist.
 */
function reading(overrides: Record<string, unknown> = {}) {
  return {
    day: "2026-07-10",
    // Lot B3, and the default is the measured estate: `app.project_capabilities`
    // on 2026-08-12 holds 31 projects × 6 keys and ZERO in `ready` or `degraded`,
    // so NOTHING projects onto a reading. Every ON fixture below is fabricated by
    // this file and says so.
    capabilities: [] as Record<string, unknown>[],
    collected: {
      relation: "raw_meta_ads_daily", zone: "main",
      columns: ["date", "campagne_id", "usr_mail", "depense"],
      rows: [
        { date: "2026-07-10", campagne_id: "c1", usr_mail: "[MASKED]", depense: "[MASKED]" },
      ],
      row_count: 1, truncated: false, masked_fields: ["depense", "usr_mail"],
      reason: null, message: null, note: null, note_message: null,
    },
    mapped: {
      relation: "stg_meta_ads_daily", zone: "main_staging",
      columns: ["date", "campaign_id", "cost"],
      rows: [{ date: "2026-07-10", campaign_id: "c1", cost: "[MASKED]" }],
      row_count: 1, truncated: false, masked_fields: ["cost"],
      reason: null, message: null, note: null, note_message: null,
    },
    pairing: PAIRING,
    ...overrides,
  };
}

/**
 * The two readings put on ONE ROW — amendment 12, lot B2.
 *
 * KEYED BY THE MAPPING, and the fixture says so: `campagne_id → campaign_id` is a
 * pair because the mapping binds it, not because the two strings look alike — they
 * do not. `usr_mail` is a column of the collected relation that no field binds, and
 * it stays unpaired with the server's reason rather than being dropped.
 *
 * Every value here is already `[MASKED]` where the server masked it, exactly as in
 * the two sides above: the join ran on the server, before the mask, and only the
 * masked values travel.
 */
const PAIRING = {
  available: true,
  reason: null,
  message: null,
  columns: [
    { source_field: "date", target_field: "date", is_key_column: true },
    { source_field: "campagne_id", target_field: "campaign_id", is_key_column: true },
    { source_field: "depense", target_field: "cost", is_key_column: false },
  ],
  key_columns: [
    { source_field: "date", target_field: "date" },
    { source_field: "campagne_id", target_field: "campaign_id" },
  ],
  unpaired_columns: [
    {
      name: "usr_mail", side: "collected", reason: "unbound",
      message: "usr_mail is bound to no target, so it becomes nothing here.",
    },
  ],
  rows: [
    {
      side: null, reason: null, message: null,
      key: [
        { field: "date", value: "2026-07-10" },
        { field: "campagne_id", value: "c1" },
      ],
      cells: [
        { source_field: "date", target_field: "date", is_key_column: true,
          raw: "2026-07-10", mapped: "2026-07-10", changed: false },
        { source_field: "campagne_id", target_field: "campaign_id", is_key_column: true,
          raw: "c1", mapped: "c1", changed: false },
        // The mapping really moved this one — and it is masked on both sides, which
        // is why the CHANGE has to be carried as a fact rather than left to be seen.
        { source_field: "depense", target_field: "cost", is_key_column: false,
          raw: "[MASKED]", mapped: "[MASKED]", changed: true },
      ],
    },
    {
      // A row of the collected reading the mapped one has no counterpart for. It
      // is SHOWN, with the side it came from: dropping it would make the reading
      // look complete, which is the shape of a fabricated pairing.
      side: "collected", reason: "no_counterpart",
      message: "No row of the other reading carries this key.",
      key: [
        { field: "date", value: "2026-07-10" },
        { field: "campagne_id", value: "c2" },
      ],
      cells: [
        { source_field: "date", target_field: "date", is_key_column: true,
          raw: "2026-07-10", mapped: null, changed: null },
        { source_field: "campagne_id", target_field: "campaign_id", is_key_column: true,
          raw: "c2", mapped: null, changed: null },
        { source_field: "depense", target_field: "cost", is_key_column: false,
          raw: "[MASKED]", mapped: null, changed: null },
      ],
    },
  ],
  row_count: 2,
  paired_row_count: 1,
  unpaired_row_count: 1,
  truncated: false,
  bounded_at: 50,
};

/** The refusal the estate actually carries: 4 of 6 live Datastreams have no
 *  mapping version, and the reviewed one binds 0 of its 22 fields to a target. */
const PAIRING_REFUSED = {
  ...PAIRING,
  available: false,
  reason: "no_bound_field",
  message:
    "No field of the active mapping names a target, so no collected column can be said to become a mapped one. Bind a field to a canonical target on Mapping and its raw value and its mapped value appear on one row.",
  columns: [],
  key_columns: [],
  rows: [],
  row_count: 0,
  paired_row_count: 0,
  unpaired_row_count: 0,
};

/** The four positions as the SERVER decides them — the reason is its sentence. */
const AVAILABLE = [
  { mode: "collected", available: true, reason: null, relation: "raw_meta_ads_daily" },
  { mode: "mapped", available: true, reason: null, relation: "stg_meta_ads_daily" },
  { mode: "processed", available: true, reason: null, relation: "fact_daily_kpi" },
  { mode: "published", available: false, reason: "Requested stage 'published' has no distinct warehouse materialisation.", relation: null },
];

/**
 * The country capability, as the ROUTE decides it — story 58.5.
 *
 * `disabled` is what 1891 projects out of 1891 carry (measured on the disposable
 * cluster 2026-08-07), so the OFF fixture is the estate and the ON fixture is
 * fabricated by this file and by nothing else. No project of this product shows the
 * column today.
 */
const COUNTRY_OFF = {
  capability_state: "disabled",
  active: false,
  degraded: false,
  reason: "capability_not_active",
};

const COUNTRY_ON = {
  capability_state: "ready",
  active: true,
  degraded: false,
  reason: null,
  bounded_at: 12,
  days: {
    "2026-07-10": {
      values: [
        { value: "FR", kind: "country", label: "FR", rows: 5 },
        { value: "DE", kind: "country", label: "DE", rows: 3 },
        // The bucket the mart keeps for a row the source gave no country for. It
        // is NOT counted among the countries — `country_count` says THREE because
        // three countries were measured and only two fit — and it is drawn apart.
        {
          value: "__country_absent__",
          kind: "country_absent",
          label: "No country reported",
          rows: 2,
        },
      ],
      country_count: 3,
    },
  },
};

/**
 * A day whose rows ALL lack a country — the likely day, not the edge.
 *
 * 34 connectors of the 39 report no country at all, so the moment a project turns
 * the capability on this is what most fluxes show: zero NAMED countries and plenty
 * of rows. The count is `0` and the rows are forty; a screen that printed the count
 * would print `0` over them.
 */
const COUNTRY_ALL_ABSENT = {
  ...COUNTRY_ON,
  days: {
    "2026-07-10": {
      values: [
        {
          value: "__country_absent__",
          kind: "country_absent",
          label: "No country reported",
          rows: 40,
        },
      ],
      country_count: 0,
    },
  },
};

function breakdown(overrides: Record<string, unknown> = {}) {
  return {
    schema: "datastream_daily_breakdown.v1",
    project_id: "proj_EXAMPLE",
    datastream_id: "ds_EXAMPLE",
    connector: "meta-ads",
    window: WINDOW,
    view_mode: {
      requested: "processed", served: "processed", note: null, available: AVAILABLE,
    },
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
      { date: "2026-07-11", extract_status: "empty", job_state: "done", extract_count: 1,
        row_count: 0, row_count_reason: null, rows: 0, rows_reason: null,
        execution_id: "dse_2", provenance: null, dq_verdict: null, dq_reason: "no_monitor" },
      // A day covered by a MULTI-DAY window: the ledger refuses to copy that
      // window's total onto it, and the absence carries its reason.
      { date: "2026-07-12", extract_status: "ok", job_state: "done", extract_count: 2,
        row_count: null, row_count_reason: "measured_per_window", rows: 12,
        rows_reason: null, execution_id: "dse_3", provenance: null },
      // A window a PERSON stopped. On the extract verdict alone it reads exactly
      // like a day nobody ever asked for.
      { date: "2026-07-13", extract_status: "never_fetched", job_state: "cancelled",
        extract_count: 1, row_count: null, row_count_reason: "not_verified", rows: null,
        rows_reason: "connector_not_in_mart", execution_id: null, provenance: null },
    ],
    reason: null,
    // Story 58.5: the state travels on every payload; the MEASURE only when the
    // project turned the capability on. The default is the measured estate.
    country: COUNTRY_OFF,
    ...overrides,
  };
}

function stubBreakdown(body: unknown, ok = true, status = 200) {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (!url.includes("/daily-breakdown")) throw new Error(`unexpected ${url}`);
    return { ok, status, json: async () => body } as Response;
  }));
}

/**
 * The grid's call and the reading's call, told apart by the `day` parameter.
 *
 * Two addresses would have been easier to stub and wrong: the reading is one day
 * of the same envelope, and the day the screen opens on is DERIVED from the days
 * the first call sent — never invented here.
 */
function stubWithReading(
  body: Record<string, unknown>,
  dayReading: Record<string, unknown> | null,
  /** What the READING call answers when it is not a `200` — the branch that was
   *  written and never mounted, so the panel drew "no day is open" over a `503`. */
  readingFailure?: { status: number; body: Record<string, unknown> },
) {
  const calls: string[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    calls.push(url);
    if (!url.includes("/daily-breakdown")) throw new Error(`unexpected ${url}`);
    if (url.includes("day=")) {
      if (readingFailure) {
        return {
          ok: false, status: readingFailure.status,
          json: async () => readingFailure.body,
        } as Response;
      }
      return { ok: true, status: 200, json: async () => ({ ...body, reading: dayReading }) } as Response;
    }
    return { ok: true, status: 200, json: async () => body } as Response;
  }));
  return calls;
}

function renderTab(props: Record<string, unknown> = {}) {
  return render(
    <WorkbenchDataPage
      payload={TAB_PAYLOAD}
      projectId="proj_EXAMPLE"
      datastreamId="ds_EXAMPLE"
      {...props}
    />,
  );
}

const HEADER = {
  schema: "datastream_workbench.header.v1",
  identity: {
    datastream_id: "ds_EXAMPLE", project_id: "proj_EXAMPLE", name: "Orders",
    mode: "connector_pull", data_role: "fact", owner: "owner@example.com",
    module: "meta-ads", source_account_ref: "acct_1", declared_writer: null,
    business_domains: [],
  },
  axes: { lifecycle: "Active", configuration: "Ready", operations: "Healthy",
          publication: "Current" },
  versions: { active_plan: "dsp_1", active_mapping: "dmap_1", proposed_plan: null,
              proposed_mapping: null },
  operations_evidence: { next_run_at: null, missed_run_count: 0,
                         schedule_state_known: true, late_reasons: [] },
  runs: { latest: "dse_3", latest_state: "published" },
  publications: { candidate: null, current: "dse_3", last_known_good: "dse_1" },
  links: {
    source: "/org/org_1/project/proj_EXAMPLE/data/sources",
    project_settings: "/org/org_1/project/proj_EXAMPLE/settings/general",
    governance: "/org/org_1/project/proj_EXAMPLE/governance/master-data",
  },
  primary_action: { kind: "prepare_change", label: "Prepare change",
                    reason: "Stable.", tab: "processing" },
};

/** The shell's own job — holding which tab is open — so the walk is a real one. */
function Workbench() {
  const [tab, setTab] = useState<Tab>("data");
  return (
    <DatastreamWorkbenchRoute
      projectId="proj_EXAMPLE"
      datastreamId="ds_EXAMPLE"
      tab={tab}
      onNavigateTab={setTab}
    />
  );
}

/** The rows of the day grid alone — the tab carries a second table below it. */
async function gridRows() {
  const grid = within(await screen.findByRole("region", { name: "Day-by-day breakdown" }));
  return grid.getAllByRole("row").slice(1); // drop the header row
}

afterEach(() => vi.unstubAllGlobals());

describe("Data tab, read by day", () => {
  it("draws one row per day the route sent, in date order", async () => {
    stubBreakdown(breakdown());
    renderTab();

    const rows = await gridRows();
    expect(rows).toHaveLength(4);
    expect(rows.map((row) => row.querySelector("td")?.textContent)).toEqual([
      "2026-07-10", "2026-07-11", "2026-07-12", "2026-07-13",
    ]);
    // Two verdicts, told apart: the extract's and the collecting WINDOW's. A day
    // somebody stopped reports `never_fetched` to the ledger, exactly like a day
    // nobody asked for, and only the window's own state separates them.
    expect(within(rows[3]).getByText("Never requested")).toBeInTheDocument();
    expect(within(rows[3]).getByText("Stopped before it started")).toBeInTheDocument();
  });

  it("carries both pills on a mapped field, and says Unmapped instead of an empty one", async () => {
    stubBreakdown(breakdown());
    renderTab();

    const band = within(await screen.findByTestId("daily-breakdown-fields"));
    // Source above canonical, both drawn — including the identity rename, whose
    // single pill would read exactly like an unmapped field.
    expect(band.getByText("media_date")).toBeInTheDocument();
    expect(band.getByText("media_date_98245b")).toBeInTheDocument();
    expect(band.getAllByText("clicks")).toHaveLength(2);
    // A field nothing binds keeps its source pill and gets a WORD, never a pill
    // with nothing in it.
    expect(band.getByText("audience_label")).toBeInTheDocument();
    expect(band.getByText("Unmapped")).toBeInTheDocument();
    for (const pill of screen.getByTestId("daily-breakdown-fields")
      .querySelectorAll("[data-slot=badge]")) {
      expect(pill.textContent?.trim()).not.toBe("");
    }
    // And the header names the store that answered: an empty header is otherwise
    // indistinguishable from a reader looking in one place out of two.
    expect(band.getByText(/active mapping version/i)).toBeInTheDocument();
  });

  it("says why a volume is missing instead of printing a zero", async () => {
    stubBreakdown(breakdown());
    renderTab();

    const rows = await gridRows();
    expect(within(rows[2]).getByText(/counted per collection window, not per day/i))
      .toBeInTheDocument();
    expect(within(rows[3]).getByText(/no verification counted this Run/i))
      .toBeInTheDocument();
    // The measured zeros are still zeros — 2026-07-11 collected an empty day and
    // says so. What must never appear is a zero standing in for the two absences
    // above, so the count of `0` cells is exactly the number of measured ones.
    const zeros = (await gridRows()).flatMap((row) =>
      within(row).queryAllByText("0", { selector: "td" }),
    );
    expect(zeros).toHaveLength(2);
  });

  it("says no day was collected without claiming the read failed", async () => {
    stubBreakdown(breakdown({ days: [], reason: "no_run_in_window" }));
    renderTab();

    expect(await screen.findByText("No day was collected in this window")).toBeInTheDocument();
    // The window it asked for, said back — "nothing" without a period is not an
    // answer a person can act on.
    expect(screen.getByText(/Nothing covered 2026-07-10 to 2026-07-13/)).toBeInTheDocument();
    expect(screen.queryByText("The daily breakdown could not be read")).not.toBeInTheDocument();
  });

  it("draws no row at all when the breakdown cannot be read", async () => {
    stubBreakdown({ code: "unavailable", message: "Daily breakdown is unavailable" }, false, 503);
    renderTab();

    await waitFor(() =>
      expect(screen.getByText("The daily breakdown could not be read")).toBeInTheDocument());
    // The route's own sentence, not `HTTP 503`.
    expect(screen.getByText(/Daily breakdown is unavailable/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Day-by-day breakdown" })).not.toBeInTheDocument();
    expect(screen.queryByText("2026-07-10")).not.toBeInTheDocument();
  });

  it("keeps the days when the mart refuses the cells, and does not read as broken", async () => {
    // A THIRD READING, and it is a served answer: the extract registry lives in
    // Postgres, the volumes live in the mart, and a warehouse that cannot answer
    // must not take the strip of days down with it.
    stubBreakdown(breakdown({
      days: breakdown().days.map((day) => ({
        ...day, rows: null, rows_reason: "warehouse_unavailable",
      })),
    }));
    renderTab();

    const rows = await gridRows();
    expect(rows).toHaveLength(4);
    expect(within(rows[0]).getByText("150")).toBeInTheDocument();
    expect(within(rows[0]).getByText(/the warehouse could not be read/i)).toBeInTheDocument();
    expect(screen.queryByText("The daily breakdown could not be read")).not.toBeInTheDocument();
  });

  it("holds no country and no cost column open when the capability is off", async () => {
    // Amendment 2, « Une capacité activée AJOUTE son onglet »: a capability that
    // is off appears "ni onglet, ni panneau, ni colonne". The payload now CARRIES
    // the state (story 58.5) — `disabled` on 1891 projects out of 1891 — and the
    // screen must show nothing for it: not a column, not a header, not an empty
    // list. The DOM is what is interrogated, never the payload.
    stubBreakdown(breakdown({ country: COUNTRY_OFF }));
    renderTab();

    await gridRows();
    for (const forbidden of [/country/i, /market/i, /cost/i, /currency/i, /\bfx\b/i, /tax/i]) {
      expect(screen.queryByText(forbidden)).not.toBeInTheDocument();
    }
    const headers = (await screen.findByRole("region", { name: "Day-by-day breakdown" }))
      .querySelectorAll("th");
    expect([...headers].map((cell) => cell.textContent)).toEqual([
      "Date", "Extracts", "Rows collected", "Rows at the mart", "Extract", "Collection window",
      // Story 58.4 — and it is the LAST column of the list on purpose: an
      // action belongs after the facts it acts on, and the three forbidden
      // columns above are still forbidden with it in place. The word is short
      // because the column is one twelfth of 1128px: measured, not guessed.
      "Re-ask",
    ]);
  });

  // -------------------------------------------------------------------------
  // THE COUNTRY SPLIT — story 58.5.
  //
  // Two mounts of the same screen, one payload apart: the block appears and
  // disappears, and what the story forbids is everything in between — a header
  // with no data, an empty list, a `0` for an absence, and the mart's own
  // identity for the no-country bucket rendered where a person reads a place.
  // -------------------------------------------------------------------------

  it("grows the country column when the project turned the capability on", async () => {
    stubBreakdown(breakdown({ country: COUNTRY_ON }));
    renderTab();

    const headers = (await screen.findByRole("region", { name: "Day-by-day breakdown" }))
      .querySelectorAll("th");
    expect([...headers].map((cell) => cell.textContent)).toEqual([
      "Date", "Extracts", "Rows collected", "Rows at the mart", "Countries", "Extract",
      "Collection window", "Re-ask",
    ]);
    // And the panel says what the split is NOT: the country series is one of
    // several parallel series, so it does not decompose the day's total rows.
    expect(screen.getByTestId("country-block")).toHaveTextContent(
      /not the parts of the day's total rows/,
    );
  });

  it("disappears entirely when the same screen is mounted with the capability off", async () => {
    // THE SAME MOUNT, TWICE. A column that appears is only half the contract; the
    // other half is that nothing of it survives when the capability goes off.
    stubBreakdown(breakdown({ country: COUNTRY_ON }));
    const first = renderTab();
    await screen.findByTestId("country-block");
    first.unmount();

    stubBreakdown(breakdown({ country: COUNTRY_OFF }));
    renderTab();
    await gridRows();
    expect(screen.queryByTestId("country-block")).not.toBeInTheDocument();
    expect(screen.queryByText("Countries")).not.toBeInTheDocument();
    expect(screen.queryByTestId("country-unfold")).not.toBeInTheDocument();
  });

  it("keeps the day one row and unfolds it into its countries", async () => {
    // Arbitrage 2: a row per (day × country) would multiply the date axis by an
    // unbounded cardinality. The day unfolds instead, and the unfolding is opened
    // by a control that names what it opens.
    stubBreakdown(breakdown({ country: COUNTRY_ON }));
    renderTab();

    const rows = await gridRows();
    expect(rows).toHaveLength(4);
    const open = within(rows[0]).getByRole("button", { name: /country values of 2026-07-10/ });
    expect(open).toHaveTextContent("3");
    await userEvent.click(open);

    const unfold = within(screen.getByTestId("country-unfold"));
    expect(unfold.getByText(/FR · 5/)).toBeInTheDocument();
    expect(unfold.getByText(/DE · 3/)).toBeInTheDocument();
    // The bound is SAID, with the count that was measured — a list cut at twelve
    // that stated only its own length would read as the whole answer.
    expect(screen.getByText(/2 of 3 countries are shown · at most 12 per day/))
      .toBeInTheDocument();
  });

  it("never renders the no-country bucket as if it were a country", async () => {
    // Arbitrage 1. The mart keeps the row under a declared identity; the SCREEN
    // shows the server's label, apart from the countries and after them, and the
    // identity itself never reaches the DOM.
    stubBreakdown(breakdown({ country: COUNTRY_ON }));
    renderTab();

    const rows = await gridRows();
    await userEvent.click(
      within(rows[0]).getByRole("button", { name: /country values of 2026-07-10/ }),
    );

    const absent = screen.getByTestId("country-absent");
    expect(absent).toHaveTextContent("No country reported · 2");
    expect(screen.queryByText(/__country_absent__/)).not.toBeInTheDocument();
    // LAST, and outside the run of country chips.
    const chips = [...screen.getByTestId("country-unfold").querySelectorAll("li")];
    expect(chips[chips.length - 1]).toBe(absent);
  });

  it("prints no zero on a day whose rows all lack a country", async () => {
    // THE LIKELY DAY, and the one that printed `0` over forty rows. Zero NAMED
    // countries is not zero rows: the count is not shown at all, the server's word
    // takes its place, and the accessible name says what opening it will show.
    stubBreakdown(breakdown({ country: COUNTRY_ALL_ABSENT }));
    renderTab();

    const rows = await gridRows();
    const cell = within(rows[0]).getAllByRole("cell")[4];
    expect(cell.textContent).not.toMatch(/\b0\b/);
    const open = within(rows[0]).getByRole("button", { name: /rows of 2026-07-10 that carry no country/ });
    expect(open).toHaveTextContent("No country reported");
    await userEvent.click(open);
    expect(within(screen.getByTestId("country-unfold")).getByText(/No country reported · 40/))
      .toBeInTheDocument();
  });

  it("says why a day carries no country instead of printing a zero", async () => {
    // The rule of 58.1, one column further: a `0` where the route sent nothing is
    // a measurement nobody made. The capability is ON and this connector reports
    // no country — which is NOT the capability being off, and says so.
    stubBreakdown(breakdown({
      country: { ...COUNTRY_ON, days: undefined, reason: "connector_reports_no_country" },
    }));
    renderTab();

    const rows = await gridRows();
    for (const row of rows) {
      const cell = within(row).getAllByRole("cell")[4];
      expect(cell).toHaveTextContent(/this connector reports no country/);
      expect(cell.textContent?.trim()).not.toBe("0");
    }
    expect(screen.getByTestId("country-block")).toHaveTextContent(
      /this connector reports no country/,
    );
  });

  it("shows the split and says so when the capability is degraded", async () => {
    // Arbitrage 5: hiding rows already collected is worse than showing them
    // diminished.
    stubBreakdown(breakdown({
      country: { ...COUNTRY_ON, capability_state: "degraded", degraded: true },
    }));
    renderTab();

    expect(await screen.findByTestId("country-block")).toHaveTextContent(/degraded/);
    expect(screen.getByText("Countries")).toBeInTheDocument();
  });

  it("draws no re-collection column at all without a handler for it", () => {
    // ASKED OF THE GRID, not of the tab: the tab always supplies the handler, so
    // mounting it proves nothing about the other mount — the component sheet,
    // which renders this grid on its own. A column of buttons that do nothing is
    // a control lying about what it does, and the sheet is where it would ship.
    render(<DateBreakdownGrid breakdown={breakdown() as never} />);

    const headers = screen.getByTestId("daily-breakdown-grid").querySelectorAll("th");
    expect([...headers].map((cell) => cell.textContent)).not.toContain("Re-ask");
    // ANCHORED ON THE CONNECTOR — 2026-08-18. The row action's accessible name is
    // `extractGapSentence`, which opens with the provider: « meta-ads returned
    // nothing for 2026-07-11 ». The state filter added the same day spells the
    // vocabulary's own label, « Empty — the provider returned nothing », so an
    // unanchored `/returned nothing/` now matches a chip that re-collects nothing
    // and never did. What this test guards is the ACTION, so it names it.
    expect(screen.queryByRole("button", { name: /^meta-ads returned nothing/ })).toBeNull();
    // …and the column appears the moment there is something for it to do.
    render(<DateBreakdownGrid breakdown={breakdown() as never} onRepullDay={() => undefined} />);
    expect(screen.getAllByRole("button", { name: /^meta-ads returned nothing/ })).toHaveLength(1);
  });

  it("draws an empty day and a never-requested day as two different shapes", async () => {
    // JEAN, 2026-08-06, and it is a CLASS fix: the two states share the neutral
    // tone deliberately — a `success` or a `warning` on one would make the
    // column scannable by hue and stop the word being read — so a SHAPE tells
    // them apart. Before this they rendered identically and only the hover
    // separated them, on a grid of up to 92 rows.
    stubBreakdown(breakdown());
    renderTab();

    const rows = await gridRows();
    const pill = (row: HTMLElement) =>
      within(row).getByTestId("day-extract-status").className;
    // Row 1 is `empty`, row 3 is `never_fetched`.
    expect(pill(rows[1])).not.toBe(pill(rows[3]));
    expect(pill(rows[1])).toMatch(/bg-surface-muted/);
    expect(pill(rows[3])).toMatch(/dashed/);
    // And neither took a semantic tone, which is what the shape exists to avoid.
    for (const row of [rows[1], rows[3]]) {
      expect(pill(row)).not.toMatch(/success|warning|error/);
    }
    // The two words are still there and still carry the meaning.
    expect(within(rows[1]).getByText(/Empty — the provider returned nothing/))
      .toBeInTheDocument();
    expect(within(rows[3]).getByText("Never requested")).toBeInTheDocument();
  });

  it("keeps one line per cell with the action in place", async () => {
    // The geometry itself is measured against a real browser by
    // `scripts/measure_component_sheet.py`; jsdom computes no layout. What is
    // checkable here is the shape the measurement depends on: the action is a
    // compact control inside the cell, not a second row and not a wrapper that
    // makes the row grow.
    stubBreakdown(breakdown());
    renderTab();

    const rows = await gridRows();
    for (const row of rows) {
      expect(within(row).getAllByRole("cell")).toHaveLength(7);
    }
    const action = within(rows[1]).getByRole("button", { name: /returned nothing/ });
    expect(action.closest("td")).toBe(within(rows[1]).getAllByRole("cell")[6]);
    // Compact, and its cell gives up the grid's own padding: at one twelfth of
    // 1128px there are 83px, and `measure_component_sheet.py` reported the grid
    // scrolling until both were true.
    expect(action.className).toMatch(/px-2/);
    expect(action.closest("td")!.className).toMatch(/px-2/);
  });

  it("hands over the execution of the day, and refuses to pretend for a day with none", async () => {
    const opened = vi.fn();
    stubBreakdown(breakdown());
    renderTab({ onOpenRun: opened });

    const rows = await gridRows();
    await userEvent.click(within(rows[2]).getByRole("button", { name: "2026-07-12" }));
    // THE ID, not the tab. `onNavigateTab("runs")` was the first shape of this
    // and it dropped the only thing the gesture carries.
    expect(opened).toHaveBeenCalledWith("dse_3");
    // No execution behind the day: THE DATE is text, not a control that leads
    // nowhere. Narrowed in story 58.4 from "this row has no button at all": the
    // row now carries its own re-collection action, and the assertion has to
    // name the control it is about or it starts refusing an unrelated one.
    expect(within(rows[3]).queryByRole("button", { name: "2026-07-13" }))
      .not.toBeInTheDocument();
    expect(within(rows[3]).getByRole("button", { name: /was never asked for/ }))
      .toBeInTheDocument();
  });

  it("states that the header and the rows do not join", async () => {
    stubBreakdown(breakdown());
    renderTab();

    expect(await screen.findByText("These fields are not the columns below")).toBeInTheDocument();
    expect(screen.getByText(/mart_metric_is_a_dbt_literal/)).toBeInTheDocument();
  });

  it("lands on the RUN of the day it was opened from, not on a list of sixty", async () => {
    // THE WHOLE WALK, because the halves each looked delivered on their own: the
    // grid carried the `execution_id` to its handler, the Runs page had a
    // `selectedId`, and the route threw the id away between them. On a 60-day
    // window that is an unfiltered list and the work of finding the run again.
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/daily-breakdown")) {
        return { ok: true, status: 200, json: async () => breakdown() } as Response;
      }
      if (url.endsWith("/data")) {
        return { ok: true, status: 200, json: async () => TAB_PAYLOAD } as Response;
      }
      if (url.endsWith("/runs")) {
        return { ok: true, status: 200, json: async () => ({
          ...TAB_PAYLOAD, tab: "runs", schema: "datastream_workbench.runs.v1",
          evidence: {
            runs: [
              { id: "dse_1", state: "published", created_at: "2026-07-10T06:00:00Z" },
              { id: "dse_3", state: "published", created_at: "2026-07-12T06:00:00Z" },
            ],
            timeline: [],
          },
        }) } as Response;
      }
      if (url.includes("/progress")) {
        return { ok: true, status: 200, json: async () => ({ state: "idle" }) } as Response;
      }
      return { ok: true, status: 200, json: async () => HEADER } as Response;
    }));

    render(<Workbench />);
    const grid = within(await screen.findByRole("region", { name: "Day-by-day breakdown" }));
    await userEvent.click(grid.getByRole("button", { name: "2026-07-12" }));

    // The run of THAT day is the one open, and it is not merely the first of the
    // list — `dse_1` is first and is not the one that was asked for. Story
    // 58.10 turned the Runs tab into one block per run, so the run that was
    // asked for is the SELECTED block rather than a detail panel beside a table.
    await waitFor(() =>
      expect(
        document
          .querySelector("[data-testid='run-block'][data-state='selected']")
          ?.getAttribute("data-run-id"),
      ).toBe("dse_3"),
    );
  });

  it("offers the three readings and names the relation each one reads", async () => {
    stubWithReading(breakdown(), reading());
    renderTab();

    const panel = within(await screen.findByTestId("day-reading"));
    for (const label of ["Collected", "Mapped", "Side by side"]) {
      expect(panel.getByRole("radio", { name: label })).toBeEnabled();
    }
    // The ADDRESS, on the control: a position that does not say what it reads
    // cannot be checked against anything.
    expect(panel.getAllByText("raw_meta_ads_daily").length).toBeGreaterThan(0);
    expect(panel.getAllByText("stg_meta_ads_daily").length).toBeGreaterThan(0);
  });

  it("shows a disabled reading with THE SERVER'S sentence, never one of its own", async () => {
    // The doubled reason is CHANGED here on purpose: if the screen held a
    // sentence of its own, this test would still find the old one.
    const refusal = "This Datastream names no report profile, so no relation can be resolved for it.";
    stubWithReading(
      breakdown({
        view_mode: {
          requested: "processed", served: "processed", note: null,
          available: [
            { mode: "collected", available: false, reason: refusal, relation: null },
            { mode: "mapped", available: false, reason: refusal, relation: null },
            { mode: "processed", available: true, reason: null, relation: "fact_daily_kpi" },
            { mode: "published", available: false, reason: "no materialisation", relation: null },
          ],
        },
        stage_relations: {
          report_profile_id: null, collected_relation: null, mapped_relation: null,
          reason: "report_profile_not_set", message: refusal,
        },
      }),
      null,
    );
    renderTab();

    const panel = within(await screen.findByTestId("day-reading"));
    expect(panel.getByRole("radio", { name: "Collected" })).toBeDisabled();
    expect(panel.getByRole("radio", { name: "Mapped" })).toBeDisabled();
    expect(panel.getByRole("radio", { name: "Side by side" })).toBeDisabled();
    expect(panel.getAllByText(refusal).length).toBeGreaterThan(0);
  });

  it("renders whatever reason the route sends, word for word", async () => {
    // The SAME case with a different sentence. A screen with a hardcoded phrase
    // passes the test above and fails this one.
    const refusal = "No staging model reads this raw relation, so the collected rows have no mapped counterpart.";
    stubWithReading(
      breakdown({
        view_mode: {
          requested: "processed", served: "processed", note: null,
          available: [
            { mode: "collected", available: true, reason: null, relation: "raw_meta_ads_daily" },
            { mode: "mapped", available: false, reason: refusal, relation: null },
            { mode: "processed", available: true, reason: null, relation: "fact_daily_kpi" },
            { mode: "published", available: false, reason: "no materialisation", relation: null },
          ],
        },
      }),
      reading(),
    );
    renderTab();

    const panel = within(await screen.findByTestId("day-reading"));
    expect(panel.getByRole("radio", { name: "Mapped" })).toBeDisabled();
    // Twice: `Mapped` cannot be served, and neither can `Side by side`, which
    // needs it — one refusal, said on both controls it applies to.
    expect(panel.getAllByText(refusal)).toHaveLength(2);
    expect(panel.getByRole("radio", { name: "Collected" })).toBeEnabled();
  });

  it("switches reading without asking the route again, and without moving the day", async () => {
    const calls = stubWithReading(breakdown(), reading());
    renderTab();

    const panel = within(await screen.findByTestId("day-reading"));
    await waitFor(() => expect(panel.getByTestId("reading-collected")).toBeInTheDocument());
    const before = calls.length;

    await userEvent.click(panel.getByRole("radio", { name: "Mapped" }));
    expect(within(screen.getByTestId("day-reading")).getByTestId("reading-mapped"))
      .toBeInTheDocument();
    // Nothing was fetched: that is what makes the window, the grid and the open
    // day unable to move.
    expect(calls.length).toBe(before);
    expect((await gridRows())).toHaveLength(4);
    // The opened day is the most recent one the route said carried an extract,
    // and it is still the one open after the switch.
    expect(screen.getByDisplayValue("2026-07-13")).toBeInTheDocument();
  });

  /**
   * `Side by side` — amendment 12, lot B2.
   *
   * What these cases hold is the promise epic 58 was built on and shipped without:
   * « la valeur brute ET sa valeur mappée » on ONE row, « avec la transformation
   * surlignée ». And, just as hard, the three refusals that were paid for — nothing
   * paired that the mapping does not pair, every value masked server-side, and a
   * reading that says which side an unpairable row came from.
   */
  async function openSideBySide(dayReading = reading()) {
    stubWithReading(breakdown(), dayReading);
    renderTab();
    const panel = within(await screen.findByTestId("day-reading"));
    await userEvent.click(panel.getByRole("radio", { name: "Side by side" }));
    return within(screen.getByTestId("day-reading"));
  }

  it("puts the raw value and the value it becomes on ONE row", async () => {
    const opened = await openSideBySide();

    const paired = within(opened.getByTestId("paired-reading"));
    // THE DOUBLE PILL, over the two values it names — which is what 58.2 built it
    // for and what it had never been attached to. The two names are different
    // strings: this pair exists because the MAPPING binds it, not because they look
    // alike.
    expect(paired.getByText("campagne_id")).toBeInTheDocument();
    expect(paired.getByText("campaign_id")).toBeInTheDocument();
    // One row of the reading, carrying both sides of every paired column.
    const row = within(paired.getAllByTestId("paired-row")[0]);
    expect(row.getAllByTestId("paired-cell")).toHaveLength(3);
    // AND THE TRANSFORMATION IS VISIBLE: the cell the mapping moved is marked
    // apart from the two it passed through. The marking is the server's fact —
    // nothing here compares two strings to decide it.
    const transforms = row
      .getAllByTestId("paired-cell")
      .map((cell) => cell.getAttribute("data-transform"));
    expect(transforms).toEqual(["unchanged", "unchanged", "changed"]);
    // And the two blocks that did not join are no longer the answer.
    expect(opened.queryByTestId("pairing-refused")).not.toBeInTheDocument();
  });

  it("says which side a row that could not be paired came from", async () => {
    const opened = await openSideBySide();

    const lonely = within(opened.getByTestId("unpaired-row"));
    expect(lonely.getByText("Collected only")).toBeInTheDocument();
    // The server's sentence, whole. A wording composed here would be a second
    // answer to a question that already has one.
    expect(lonely.getByText("No row of the other reading carries this key."))
      .toBeInTheDocument();
    // « rien à comparer » is not « rien n'a changé »: the cells of that row carry
    // neither mark.
    const row = within(opened.getAllByTestId("paired-row")[1]);
    expect(
      row.getAllByTestId("paired-cell").map((cell) => cell.getAttribute("data-transform")),
    ).toEqual(["incomparable", "incomparable", "incomparable"]);
  });

  it("names the columns the mapping does not pair instead of dropping them", async () => {
    const opened = await openSideBySide();

    const unpaired = within(opened.getByTestId("unpaired-columns"));
    expect(unpaired.getByText("usr_mail is bound to no target, so it becomes nothing here."))
      .toBeInTheDocument();
  });

  it("shows only the values the server masked, on the paired row too", async () => {
    const opened = await openSideBySide();

    // The masked amount appears on both sides of its cell and nowhere raw: the
    // join ran on the server, before the mask, and only the mask travelled.
    expect(opened.getAllByText("[MASKED]").length).toBe(3);
    expect(opened.queryByText("0", { selector: "td" })).not.toBeInTheDocument();
  });

  it("renders the server's refusal verbatim when nothing can be paired", async () => {
    // The estate: 4 of 6 live Datastreams carry no mapping version, and the
    // reviewed one binds 0 of its 22 fields to a target.
    const opened = await openSideBySide(reading({ pairing: PAIRING_REFUSED }));

    expect(opened.getByText("These two readings are not paired")).toBeInTheDocument();
    expect(opened.getByText(PAIRING_REFUSED.message)).toBeInTheDocument();
    // And the fallback is the two readings, one after the other — an answer now,
    // rather than an excuse.
    expect(opened.getByTestId("reading-collected")).toBeInTheDocument();
    expect(opened.getByTestId("reading-mapped")).toBeInTheDocument();
    expect(opened.queryByTestId("paired-reading")).not.toBeInTheDocument();
  });

  it("tells a pairing that refused from a route that sent none", async () => {
    const opened = await openSideBySide(reading({ pairing: undefined }));

    expect(opened.getByText(/arrived without the block that pairs the two sides/))
      .toBeInTheDocument();
    expect(opened.getByTestId("reading-collected")).toBeInTheDocument();
  });

  it("shows the masked values exactly as the server sent them", async () => {
    stubWithReading(breakdown(), reading());
    renderTab();

    const panel = within(await screen.findByTestId("day-reading"));
    await waitFor(() => expect(panel.getByTestId("reading-collected")).toBeInTheDocument());
    // The source's own column names, which is what `Collected` means.
    expect(panel.getByText("campagne_id")).toBeInTheDocument();
    expect(panel.getByText("usr_mail")).toBeInTheDocument();
    expect(panel.getAllByText("[MASKED]").length).toBe(2);
    // And no `0` anywhere near a masked or absent value.
    expect(panel.queryByText("0", { selector: "td" })).not.toBeInTheDocument();
  });

  it("tells an empty day from a broken reading", async () => {
    stubWithReading(
      breakdown(),
      reading({
        collected: {
          ...reading().collected, rows: [], row_count: 0,
          note: "candidate_not_published",
          note_message: "No row landed for this day in the shared relation. A run that is still a candidate lands in a relation of its own, which this reading does not open.",
        },
      }),
    );
    renderTab();

    const panel = within(await screen.findByTestId("day-reading"));
    expect(await panel.findByText("No row landed for this day in raw_meta_ads_daily"))
      .toBeInTheDocument();
    expect(panel.getByText(/still a candidate/)).toBeInTheDocument();
    expect(screen.queryByText("The daily breakdown could not be read")).not.toBeInTheDocument();
  });

  it("says the reading BROKE, and never that no day is open", async () => {
    // The defect this case exists for: a `503` on the reading call left
    // `reading = null`, and the panel then drew « No day is open » — while the
    // selector showed the chosen date. A failed read rendered as a read nobody
    // asked for is an absence this repository fabricated.
    stubWithReading(breakdown(), null, {
      status: 503,
      body: { code: "unavailable", message: "Daily breakdown is unavailable" },
    });
    renderTab();

    const panel = within(await screen.findByTestId("day-reading"));
    expect(await panel.findByText("The Collected reading could not be loaded"))
      .toBeInTheDocument();
    // The route's own sentence, and a way out.
    expect(panel.getByText(/Daily breakdown is unavailable/)).toBeInTheDocument();
    expect(panel.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    // And NOT the empty state: the two must never render the same.
    expect(panel.queryByText("No day is open")).not.toBeInTheDocument();
    expect(panel.queryByTestId("reading-collected")).not.toBeInTheDocument();
    // The day grid above is untouched — a failure of the reading is local to it.
    expect(await gridRows()).toHaveLength(4);
  });

  it("cites the status when the failed reading carried no sentence", async () => {
    stubWithReading(breakdown(), null, { status: 502, body: {} });
    renderTab();

    const panel = within(await screen.findByTestId("day-reading"));
    expect(await panel.findByText(/HTTP 502/)).toBeInTheDocument();
    expect(panel.queryByText("No day is open")).not.toBeInTheDocument();
  });

  // -------------------------------------------------------------------------
  // The money provenance under the amount — story 58.7.
  //
  // FOUR MOUNTS, AND THREE OF THEM ARE ABSENCES, because that is the shape of the
  // estate: 0 project of 18 declares a `money` Concept, so what almost every
  // reading shows is the empty designation with its reason. The fourth is the day
  // one is declared, and it exists so the empty ones cannot be mistaken for the
  // whole behaviour.
  // -------------------------------------------------------------------------

  /**
   * `currency_fx` turned on, as the SERVER decides it — lot B3, amendment 11.
   *
   * The capability is `draft` on every live project, so this state is fabricated
   * here. What travels is the EFFECT and the FIELD it lands on — never a row
   * reading « Currency & FX · Ready », which is exactly what the amendment
   * removed.
   */
  const CURRENCY_FX_ON = {
    key: "currency_fx",
    state: "ready",
    degraded: false,
    effect: "money_line",
    title: "Currency & FX",
    message:
      "Each amount below carries the currency the source reported, the rate that was applied and the day that rate was quoted. The figures are the relation's own and nothing is converted here.",
    reason: null,
    fields: { collected: [], mapped: ["cost"] },
  };

  /** A `mapped` side that really converts — the columns of `stg_meta_ads_daily`. */
  function moneyReading(overrides: Record<string, unknown> = {}) {
    return {
      ...reading(),
      capabilities: [CURRENCY_FX_ON],
      mapped: {
        relation: "stg_meta_ads_daily",
        zone: "main_staging",
        columns: ["date", "campaign_id", "cost", "impressions"],
        rows: [{ date: "2026-07-10", campaign_id: "c1", cost: "100.000000000",
                 impressions: "4200" }],
        row_count: 1,
        truncated: false,
        masked_fields: [],
        reason: null, message: null, note: null, note_message: null,
        money_provenance: {
          columns: [{
            column: "cost",
            native_currency: "cost_source_currency",
            native_value: "cost_source_value",
            fx_rate: "fx_rate",
            fx_as_of_date: "fx_as_of_date",
          }],
          reason: null,
          message: null,
          reporting_currency: null,
          reporting_currency_reason:
            "No reporting currency is named by this reading. These amounts are in the currency the source reported.",
          reporting_currency_gate: "the project's governed Money Policy",
          rows: [{
            cost: {
              native_currency: "USD", native_value: "100.000000000",
              fx_rate: "0.920000000", fx_as_of_date: "2026-07-01",
              money_gap_code: null, money_gap_message: null,
            },
          }],
        },
        ...overrides,
      },
    };
  }

  async function openMapped() {
    const panel = within(await screen.findByTestId("day-reading"));
    await userEvent.click(panel.getByRole("radio", { name: "Mapped" }));
    return within(screen.getByTestId("day-reading"));
  }

  it("writes the currency, the rate and the rate's date under the amount", async () => {
    stubWithReading(breakdown(), moneyReading());
    renderTab();

    const opened = await openMapped();
    // ONE line, from the payload, in the cell of the amount it explains.
    expect(opened.getByTestId("money-line")).toHaveTextContent(
      "USD · 0.920000000 · 2026-07-01",
    );
    // The date is the QUOTATION date the seed carries, never the day the validity
    // window opened — `2020-01-01` was printed under 43 of 43 converted rows.
    expect(opened.queryByText(/2020-01-01/)).not.toBeInTheDocument();
    // And a column the server did not designate carries nothing new.
    expect(opened.getAllByTestId("money-line")).toHaveLength(1);
    // No reporting currency is named, and the door that would confirm one is.
    expect(opened.getByText(/These amounts are in the source's currency/))
      .toBeInTheDocument();
    expect(opened.getByText(/governed Money Policy/)).toBeInTheDocument();
    expect(opened.queryByText(/EUR/)).not.toBeInTheDocument();
  });

  it("says WHY an amount was not converted instead of drawing a dash", async () => {
    const money = moneyReading();
    stubWithReading(breakdown(), {
      ...money,
      mapped: {
        ...money.mapped,
        money_provenance: {
          ...money.mapped.money_provenance,
          rows: [{
            cost: {
              native_currency: "USD", native_value: "100.000000000",
              fx_rate: null, fx_as_of_date: null,
              money_gap_code: "fx_rate_unavailable",
              money_gap_message:
                "No rate covered this row, so the amount stayed in the currency the source reported and was not converted.",
            },
          }],
        },
      },
    });
    renderTab();

    const opened = await openMapped();
    expect(opened.getByTestId("money-gap")).toHaveTextContent(
      "No rate covered this row",
    );
    // Never a rate of `1.0` and never a dash: both make an unconverted amount
    // look like a converted one.
    expect(opened.queryByTestId("money-line")).not.toBeInTheDocument();
    expect(opened.queryByText("1.0")).not.toBeInTheDocument();
  });

  it("says no column of this reading is an amount, with the reason it came with", async () => {
    const money = moneyReading();
    const refusal =
      "No column of this reading is an amount: nothing declares one. A column becomes an amount through a published Semantic Concept version whose value_type is 'money' (app.semantic_concept_versions), and this project has published none.";
    stubWithReading(breakdown(), {
      ...money,
      mapped: {
        ...money.mapped,
        money_provenance: {
          ...money.mapped.money_provenance,
          columns: [],
          reason: "no_monetary_concept_declared",
          message: refusal,
          title: "No monetary column in this reading",
          rows: [],
        },
      },
    });
    renderTab();

    const opened = await openMapped();
    // The heading comes from the SERVER, like the sentence under it. It used to be
    // written here, and it read "No monetary column" under the two reasons that mean
    // the amount IS present and its rate is not.
    expect(opened.getByText("No monetary column in this reading")).toBeInTheDocument();
    // The server's sentence, word for word — it names what would fill the key.
    expect(opened.getByText(refusal)).toBeInTheDocument();
    expect(opened.queryByTestId("money-line")).not.toBeInTheDocument();
    // EMPTY is not BROKEN, and the two never render together.
    expect(opened.queryByText("The money provenance could not be read"))
      .not.toBeInTheDocument();
  });

  it("says the provenance could NOT be read when the route sent none", async () => {
    const money = moneyReading();
    const { money_provenance: _dropped, ...withoutProvenance } = money.mapped;
    stubWithReading(breakdown(), { ...money, mapped: withoutProvenance });
    renderTab();

    const opened = await openMapped();
    expect(opened.getByText("The money provenance could not be read"))
      .toBeInTheDocument();
    // And NOT the empty-state sentence: « vide » and « cassé » are two causes.
    expect(opened.queryByText("No monetary column in this reading"))
      .not.toBeInTheDocument();
    // The rows are still drawn — a missing annotation is not a missing reading.
    expect(opened.getByText("100.000000000")).toBeInTheDocument();
  });


  // An active capability is seen ON THE DATA — lot B3, amendment 11.
  //
  // « Une capacité activée AJOUTE UN ONGLET, OU COLORE LE CHAMP DANS L'APERÇU —
  // jamais une liste de modules. Un inventaire de modules n'est pas une
  // fonctionnalité : l'effet l'est. » So what is held here is: OFF draws nothing
  // at all, ON draws on the value or on the field, and NOWHERE does the reading
  // enumerate a capability with its state.
  //
  // AND NONE OF THESE ON STATES EXISTS ON A LIVE PROJECT. Measured 2026-08-12:
  // 31 projects, 6 capability keys each, ZERO in `ready` or `degraded` — four are
  // `disabled` and `currency_fx` / `reporting_timezone` are `draft`. Every ON
  // fixture below is fabricated here, exactly as the country split's is.
  // -------------------------------------------------------------------------

  /** `country`, on, landing on the field the mapping binds to the canonical
   *  dimension. `campagne_id` is deliberately NOT it: the mark follows the
   *  mapping, never the spelling. */
  const COUNTRY_CAPABILITY_ON = {
    key: "country",
    state: "ready",
    degraded: false,
    effect: "field_marked",
    title: "Country",
    message:
      "The field marked below is the one the active mapping binds to the canonical country dimension, so a row of this day can be read by country.",
    reason: null,
    fields: { collected: ["depense"], mapped: ["cost"] },
  };

  /** The same capability, on, with nothing on this reading to land on. */
  const COUNTRY_CAPABILITY_NOTHING_TO_MARK = {
    ...COUNTRY_CAPABILITY_ON,
    reason: "no_country_field_in_mapping",
    message:
      "This capability is on and no field of the active mapping is bound to the canonical country dimension, so no column of this reading is a country. Bind one on Mapping and it is marked here.",
    fields: { collected: [], mapped: [] },
  };

  const TIMEZONE_CAPABILITY_ON = {
    key: "reporting_timezone",
    state: "ready",
    degraded: false,
    effect: "day_boundary_signal",
    title: "Reporting timezone",
    message:
      "The rows below are the day the SOURCE drew. This Project reports on Europe/Paris, and where the source's boundary differs some of these rows belong to the neighbouring day at the Project's. The offset is signalled and never corrected: at DATE grain there is no hour to re-slice, so not one value below is moved.",
    reason: null,
    fields: { collected: [], mapped: [] },
    project_reporting_timezone: "Europe/Paris",
  };

  async function openCollected() {
    const opened = within(await screen.findByTestId("day-reading"));
    await opened.findByRole("table");
    return opened;
  }

  it("draws no capability anywhere when the project turned none on", async () => {
    // The estate, and the whole point of the amendment: no row, no panel, no
    // column, and not even the money note that used to sit on every reading of
    // every project for a capability nobody had switched on.
    stubWithReading(breakdown(), reading());
    renderTab();

    const opened = await openCollected();
    expect(opened.queryByTestId("capability-marked-field")).not.toBeInTheDocument();
    expect(opened.queryByTestId("day-boundary-signal")).not.toBeInTheDocument();
    expect(opened.queryByTestId("capability-nothing-to-mark")).not.toBeInTheDocument();
    // And no sentence about money at all: neither « empty » nor « broken », both
    // of which are answers about a projection this Project never requested.
    expect(opened.queryByText("No monetary column in this reading")).not.toBeInTheDocument();
    expect(opened.queryByText("The money provenance could not be read"))
      .not.toBeInTheDocument();
    expect(opened.queryByTestId("money-line")).not.toBeInTheDocument();
    // NEVER a module inventory: not one state word reaches the reading.
    for (const forbidden of [/Currency & FX/, /\bReady\b/, /Reporting timezone/]) {
      expect(opened.queryByText(forbidden)).not.toBeInTheDocument();
    }
  });

  it("colours the field the mapping binds to the country dimension", async () => {
    stubWithReading(breakdown(), {
      ...reading(),
      capabilities: [COUNTRY_CAPABILITY_ON],
    });
    renderTab();

    const opened = await openCollected();
    const marked = opened.getAllByTestId("capability-marked-field");
    expect(marked.length).toBeGreaterThan(0);
    expect(marked[0]).toHaveTextContent("depense");
    // The capability's own name rides beside the colour: a mark carried by hue
    // alone is not carried at all for part of the estate.
    expect(marked[0]).toHaveTextContent("Country");
    // And a column the mapping does NOT bind to it carries no mark, even in the
    // same header.
    expect(opened.getByText("campagne_id").closest("[data-testid='capability-marked-field']"))
      .toBeNull();
  });

  it("says so on the reading when the capability is on and lands on nothing", async () => {
    stubWithReading(breakdown(), {
      ...reading(),
      capabilities: [COUNTRY_CAPABILITY_NOTHING_TO_MARK],
    });
    renderTab();

    const opened = await openCollected();
    const said = opened.getByTestId("capability-nothing-to-mark");
    // The server's sentence, whole, and it names the gesture rather than the
    // mechanism that produced the absence.
    expect(said).toHaveTextContent("Bind one on Mapping");
    expect(opened.queryByTestId("capability-marked-field")).not.toBeInTheDocument();
  });

  it("signals the day boundary and moves not one value", async () => {
    const plain = reading();
    stubWithReading(breakdown(), {
      ...plain,
      capabilities: [TIMEZONE_CAPABILITY_ON],
    });
    renderTab();

    const opened = await openCollected();
    const signal = opened.getByTestId("day-boundary-signal");
    expect(signal).toHaveTextContent("never corrected");
    expect(signal).toHaveTextContent("Europe/Paris");
    // The rows are the source's day, untouched: the same values, the same day.
    const rows = within(opened.getByRole("table"));
    expect(rows.getByText("2026-07-10")).toBeInTheDocument();
    expect(rows.getByText("c1")).toBeInTheDocument();
    // It colours no field, and holds no column open for one.
    expect(opened.queryByTestId("capability-marked-field")).not.toBeInTheDocument();
  });

  it("writes the rate under the amount ON THE PAIRED ROW, not only beside it", async () => {
    // Amendment 11 met with amendment 12: the paired table is the one place a
    // person compares a raw value with what it becomes, so the effect of an
    // active capability cannot stop at its edge.
    const paired = {
      ...reading(),
      capabilities: [CURRENCY_FX_ON],
      pairing: {
        ...PAIRING,
        rows: [
          {
            ...PAIRING.rows[0],
            collected_index: 0,
            mapped_index: 0,
            cells: [
              ...PAIRING.rows[0].cells.slice(0, 2),
              {
                ...PAIRING.rows[0].cells[2],
                raw: "100.000000000",
                mapped: "100.000000000",
                changed: false,
                // The raw zone carries no rate column at all, so the server sends
                // nothing for that half — and the screen draws nothing there.
                raw_money: null,
                mapped_money: {
                  native_currency: "USD", native_value: "100.000000000",
                  fx_rate: "0.920000000", fx_as_of_date: "2026-07-01",
                  money_gap_code: null, money_gap_message: null,
                },
              },
            ],
          },
        ],
        row_count: 1,
        paired_row_count: 1,
        unpaired_row_count: 0,
      },
    };
    stubWithReading(breakdown(), paired);
    renderTab();

    const panel = within(await screen.findByTestId("day-reading"));
    await userEvent.click(panel.getByRole("radio", { name: "Side by side" }));
    const opened = within(screen.getByTestId("day-reading"));

    const row = within(opened.getAllByTestId("paired-row")[0]);
    expect(row.getByTestId("money-line")).toHaveTextContent(
      "USD · 0.920000000 · 2026-07-01",
    );
    // ONE line, under the half that has an amount. The other half carries none.
    expect(row.getAllByTestId("money-line")).toHaveLength(1);
  });

  it("annotates no paired cell at all when the capability is off", async () => {
    const paired = {
      ...reading(),
      pairing: {
        ...PAIRING,
        rows: [
          {
            ...PAIRING.rows[0],
            cells: [
              ...PAIRING.rows[0].cells.slice(0, 2),
              {
                ...PAIRING.rows[0].cells[2],
                mapped_money: {
                  native_currency: "USD", native_value: "100.000000000",
                  fx_rate: "0.920000000", fx_as_of_date: "2026-07-01",
                  money_gap_code: null, money_gap_message: null,
                },
              },
            ],
          },
        ],
      },
    };
    // Even if a payload carried the values, the capability is what decides. The
    // browser must not draw an effect the server did not declare active.
    stubWithReading(breakdown(), paired);
    renderTab();

    const panel = within(await screen.findByTestId("day-reading"));
    await userEvent.click(panel.getByRole("radio", { name: "Side by side" }));
    const opened = within(screen.getByTestId("day-reading"));

    expect(opened.queryByTestId("money-line")).not.toBeInTheDocument();
    expect(opened.queryByText("0.920000000")).not.toBeInTheDocument();
  });

  it("colours the pill of the paired header, and adds no fourth chip to it", async () => {
    stubWithReading(breakdown(), {
      ...reading(),
      capabilities: [COUNTRY_CAPABILITY_ON],
    });
    renderTab();

    const panel = within(await screen.findByTestId("day-reading"));
    await userEvent.click(panel.getByRole("radio", { name: "Side by side" }));
    const opened = within(screen.getByTestId("day-reading"));

    const marked = opened.getAllByTestId("capability-marked-field");
    // Two pills, one per side of the pair, and each carries ONLY the field name:
    // the double pill of 58.2 stays two chips, not three.
    expect(marked.map((node) => node.textContent)).toEqual(["depense", "cost"]);
    expect(marked[0]).toHaveAttribute("data-tone", "info");
    expect(marked[0].getAttribute("title")).toContain("canonical country dimension");
  });

  it("says the stage selector's silences out loud", async () => {
    // Arbitrage 8, and it is a CLASS fix: `Collected`, `Mapped` and `Processed`
    // are greyed on every flux of this product because one evidence table holds
    // `published` alone. « Unavailable » with nothing beside it was the silence.
    stubBreakdown(breakdown());
    renderTab();

    const selector = within(await screen.findByRole("radiogroup", { name: "Data stage" }));
    expect(selector.getAllByText(/No .* evidence was persisted for an exact execution/))
      .toHaveLength(4);
  });

  it("leaves the stage evidence of the tab exactly where it was", async () => {
    // Arbitrage 12: the date becomes the axis, and nothing is taken away for it.
    // The ratified `Data` cell still promises the stage selector and the bounded
    // masked samples, and that promise was erased once already.
    stubBreakdown(breakdown());
    renderTab();

    await gridRows();
    expect(screen.getByRole("radiogroup", { name: "Data stage" })).toBeInTheDocument();
    expect(screen.getByText("Stage evidence unavailable")).toBeInTheDocument();
    expect(screen.getByText(TAB_PAYLOAD.evidence.sample_reason as string)).toBeInTheDocument();
  });

  // -------------------------------------------------------------------------
  // ONE READING OF THE STAGES, NOT TWO — finding D-6 of the visual review #69.
  //
  // Four cards read `Execution-bound · 412 rows`, and four hundred pixels below
  // a table restated the same four stages, the same execution and the same 412,
  // with the versions, the schema hash, the grain and the observed instant
  // beside them. One is the summary of the other and neither said so. The
  // ratified `Data` cell now fixes the relation: the cards are the entry, the
  // table their unfolding — and NOTHING measured may be lost for it.
  // -------------------------------------------------------------------------

  /** Two stage records of one execution, which is what the review captured. */
  const STAGES = [
    {
      id: "stg_pub", stage: "published", phase_state: "succeeded",
      execution_id: "exec_1", plan_version_id: "plan_1", mapping_version_id: "map_1",
      schema_hash: "sha256:abc123", row_count: 412, grain_evidence: ["date", "campaign_id"],
      occurred_at: "2026-08-01T07:12:00Z", artifact_ref: "art_1",
      profile_evidence: { null_rate: 0.01 }, coverage_evidence: { days_present: 29 },
    },
    {
      id: "stg_proc", stage: "processed", phase_state: "succeeded",
      execution_id: "exec_1", plan_version_id: "plan_1", mapping_version_id: "map_1",
      schema_hash: "sha256:abc123", row_count: 412, grain_evidence: ["date", "campaign_id"],
      occurred_at: "2026-08-01T07:11:00Z", artifact_ref: "art_2",
      profile_evidence: {}, coverage_evidence: {},
    },
  ];

  function stagedPayload(): WorkbenchTabPayload {
    return {
      ...TAB_PAYLOAD,
      evidence: {
        ...TAB_PAYLOAD.evidence,
        stages: STAGES,
        availability: {
          collected: "unavailable", mapped: "unavailable",
          processed: "available", published: "available",
        },
      },
    };
  }

  it("states the row count once, on the card, and folds the table under it", async () => {
    stubBreakdown(breakdown());
    renderTab({ payload: stagedPayload() });

    const selector = within(await screen.findByRole("radiogroup", { name: "Data stage" }));
    // The count is the entry, and it is stated exactly twice — once per available
    // stage — never a third and fourth time in a table drawn flat beside it.
    expect(selector.getAllByText("412 rows")).toHaveLength(2);
    expect(screen.queryByRole("region", { name: "Datastream stage evidence" }))
      .not.toBeInTheDocument();
    // And a fold is not a loss: the schema hash, the versions, the grain and the
    // observed instant are gone from the screen and NOT from the product.
    expect(screen.queryByText("sha256:abc123")).not.toBeInTheDocument();
  });

  it("names every folded fact before the click, and how many records hold it", async () => {
    // A control reading « show details » makes somebody open it to find out
    // whether the evidence they came for still exists. This one answers first.
    stubBreakdown(breakdown());
    renderTab({ payload: stagedPayload() });

    await screen.findByRole("radiogroup", { name: "Data stage" });
    expect(screen.getByText(/2 execution records/)).toBeInTheDocument();
    const announced = screen.getByText(/2 execution records/).textContent ?? "";
    for (const fact of ["mapping version", "schema hash", "grain", "row count", "observed"]) {
      expect(announced).toContain(fact);
    }
  });

  it("unfolds every measured fact, and folds the stage detail back with it", async () => {
    stubBreakdown(breakdown());
    renderTab({ payload: stagedPayload() });

    const open = await screen.findByRole("button", { name: /Read the executions behind these counts/ });
    expect(open).toHaveAttribute("aria-expanded", "false");
    await userEvent.click(open);

    // Every fact the cards cannot carry, in the table the cards summarise.
    expect(screen.getAllByText("sha256:abc123")).toHaveLength(2);
    expect(screen.getAllByText("date, campaign_id")).toHaveLength(2);
    expect(screen.getAllByText("exec_1")).toHaveLength(2);
    expect(screen.getAllByText(/plan_1/)).toHaveLength(2);
    expect(screen.getAllByText(/map_1/)).toHaveLength(2);

    // A row opens its profile and coverage, and closing the unfolding closes it
    // too — a detail panel left standing over a table nobody can see is an
    // orphan of the fold.
    const evidence = within(screen.getByRole("region", { name: "Datastream stage evidence" }));
    await userEvent.click(evidence.getAllByRole("row")[1]);
    expect(await screen.findByText("Published profile and coverage")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /Hide the executions behind these counts/ }));
    expect(screen.queryByText("Published profile and coverage")).not.toBeInTheDocument();
    expect(screen.queryByText("sha256:abc123")).not.toBeInTheDocument();
  });

  // -------------------------------------------------------------------------
  // THE TAB STOPS SPEAKING CONNECTOR TO A FILE SOURCE — amendment 7 of the
  // 2026-08-11 review, applied to the blocks BELOW the one lot B1 replaced.
  //
  // Amendment 7 took the pull axis away from a `managed_feed`; everything under
  // it kept the vocabulary of the mode that was removed. The `Incomplete if` of
  // this surface is explicit on both halves: « a `managed_feed` is shown a
  // report profile, a connector relation or a day-by-day pull axis, or a mode
  // guard tests anything other than `identity.mode` ».
  // -------------------------------------------------------------------------

  /** What the server sends a pushed source once it stops borrowing the other
   *  mode's word. Composed there and rendered here, never written in the
   *  screen — a sentence held by the console would pass this test with the old
   *  server still deployed. */
  const PUSHED_REASON =
    "No sample and no export can be drawn here — this Datastream receives a file,"
    + " and the rows it received are read in the last file that arrived, above.";

  function pushedPayload(reason = PUSHED_REASON): WorkbenchTabPayload {
    return {
      ...TAB_PAYLOAD,
      evidence: { ...TAB_PAYLOAD.evidence, sample_reason: reason },
    };
  }

  it("never tells a file source it declares no connector", async () => {
    stubBreakdown(breakdown());
    renderTab({ mode: "managed_feed", payload: pushedPayload() });

    await screen.findByRole("radiogroup", { name: "Data stage" });
    expect(screen.getByText(PUSHED_REASON)).toBeInTheDocument();
    // The word this mode can never own, anywhere on the tab.
    expect(screen.queryByText(/declares no connector/)).not.toBeInTheDocument();
    expect(screen.queryByText("Bounded samples unavailable")).not.toBeInTheDocument();
  });

  it("renders whatever sentence the server sends a pushed source, word for word", async () => {
    // The SAME case with a different sentence — an `external_bq` source, whose
    // refusal names a relation instead of a file. A screen holding a phrase of
    // its own passes the case above and fails this one.
    const external =
      "No sample and no export can be drawn here — this Datastream reads a relation"
      + " it does not own, and its columns are read on Mapping.";
    stubBreakdown(breakdown());
    renderTab({ mode: "external_bq", payload: pushedPayload(external) });

    expect(await screen.findByText(external)).toBeInTheDocument();
  });

  it("offers a pushed source no export, because the export can only refuse it", async () => {
    // MEASURED, not preferred. The export resolves its mart through
    // `_resolve_datastream_mart`, which keys it on `module_name`;
    // `export_columns_for` returns no column for an empty connector and
    // `read_datastream_export` raises `no_materialization`. `module_name` is
    // NULL on both pushed modes by construction, so the control opened a dialog
    // with nothing to choose and a button that always refused.
    stubBreakdown(breakdown());
    renderTab({ mode: "managed_feed", payload: pushedPayload() });

    await screen.findByRole("radiogroup", { name: "Data stage" });
    expect(screen.queryByRole("button", { name: "Export…" })).not.toBeInTheDocument();
    expect(screen.queryByText(/take them away as a bounded CSV/)).not.toBeInTheDocument();
    // AND THE TAB SAYS SO. A block removed in silence is a tab that got quieter
    // for no stated reason; ONE sentence covers both absences, and it is the one
    // the server composed — no title of the console's own above it.
    expect(screen.getByText(PUSHED_REASON)).toBeInTheDocument();
    expect(
      screen.queryByText("Neither a bounded sample nor an export is keyed to this Datastream"),
    ).not.toBeInTheDocument();
  });

  // -------------------------------------------------------------------------
  // AN ABSENCE COSTS ONE SENTENCE — finding D-7 of the visual review #69.
  //
  // A whole panel read « No governed sample endpoint is mounted. Story 47.5
  // retired the consolidated one because it aliased stages and bound a sample to
  // no exact version; its replacement is not delivered. » — a story number, a
  // route and a deployment state, on the screen of somebody who can act on none
  // of the three. The ratified `Data` cell now fixes the form: one sentence,
  // composed by the server, saying what cannot be done here and where the
  // reading that DOES exist is found.
  // -------------------------------------------------------------------------

  it("never puts a story, a route or a mart on the screen", async () => {
    stubBreakdown(breakdown());
    renderTab({ mode: "managed_feed", payload: pushedPayload() });

    await screen.findByRole("radiogroup", { name: "Data stage" });
    for (const leak of [/Story \d/i, /endpoint/i, /\bmart\b/i, /\bmounted\b/i, /\bdelivered\b/i]) {
      expect(screen.queryAllByText(leak)).toHaveLength(0);
    }
  });

  it("gives the refusal no title of the console's own, in either register", async () => {
    // The title used to restate the body, which is how one sentence became a
    // panel. It is gone for the pushed source AND for the pull that names no
    // source — a defect of form is never repaired on one branch only.
    stubBreakdown(breakdown());
    const { unmount } = renderTab({ mode: "managed_feed", payload: pushedPayload() });
    await screen.findByRole("radiogroup", { name: "Data stage" });
    expect(screen.queryByText("Bounded samples unavailable")).not.toBeInTheDocument();
    unmount();

    const pull =
      "No sample and no export can be drawn here — this Datastream names no source"
      + " to read from, and the day-by-day reading above is the only reading it has.";
    stubBreakdown(breakdown());
    renderTab({ mode: "connector_pull", payload: pushedPayload(pull) });
    expect(await screen.findByText(pull)).toBeInTheDocument();
    expect(screen.queryByText("Bounded samples unavailable")).not.toBeInTheDocument();
  });

  it("keeps the export where it can serve", async () => {
    stubBreakdown(breakdown());
    renderTab({ mode: "connector_pull" });

    expect(await screen.findByRole("button", { name: "Export…" })).toBeInTheDocument();
  });

  it("keeps the stage selector and the stage evidence on a file source", async () => {
    // A JUDGEMENT, and it is the other half of amendment 7. What that amendment
    // removed is CONNECTOR vocabulary; `Collected → Mapped → Processed →
    // Published` are the phases of an EXECUTION, read from an evidence table
    // keyed on an execution, and a pushed source has executions — the flux the
    // review was written against had one. Removing them would leave a file
    // source with no reading of its own executions at all.
    stubBreakdown(breakdown());
    renderTab({ mode: "managed_feed", payload: pushedPayload() });

    const selector = within(await screen.findByRole("radiogroup", { name: "Data stage" }));
    expect(selector.getAllByText(/No .* evidence was persisted for an exact execution/))
      .toHaveLength(4);
    expect(screen.getByText("Stage evidence unavailable")).toBeInTheDocument();
    // And nothing in either block names a provider, a profile or a pull.
    for (const forbidden of [/report profile/i, /relation of the connector/i]) {
      expect(screen.queryByText(forbidden)).not.toBeInTheDocument();
    }
  });
});

/**
 * NARROWING THE WINDOW, AND ASKING FOR ONE — amendment of 2026-08-18.
 *
 * This is the tab an operator opens more than any other, and it offered sixty
 * rows with no way to ask a question of them: which days failed, and show me the
 * last thirty. The ways the repair can lie:
 *
 *   * a filter that reads as a measurement — a narrowed table saying nothing
 *     about the days it hid is indistinguishable from an empty window;
 *   * a chip for a state no day carries, which empties the table on click;
 *   * a preset counted from the reader's calendar rather than from the data, so
 *     a flux that stopped collecting in June answers « last 7 days » with seven
 *     empty rows and says nothing about why.
 */
describe("Choosing which days to read", () => {
  /** The calls, so a preset can be proved by the window it asked the route for. */
  function stubCalls(body: unknown) {
    const calls: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      calls.push(url);
      if (!url.includes("/daily-breakdown")) throw new Error(`unexpected ${url}`);
      return { ok: true, status: 200, json: async () => body } as Response;
    }));
    return calls;
  }

  it("narrows the grid to one day state, and says what it hid", async () => {
    stubBreakdown(breakdown());
    renderTab();

    expect(await gridRows()).toHaveLength(4);
    // The chips are the states this window CONTAINS, counted — and the words are
    // the vocabulary's own, not a second spelling of them.
    const filter = within(screen.getByTestId("day-state-filter"));
    expect(filter.getByRole("button", { name: /Every day · 4/ })).toBeInTheDocument();
    expect(filter.getByRole("button", { name: /Collected · 2/ })).toBeInTheDocument();
    // A state no day carries has no chip at all: a filter that empties the table
    // reads as a window with nothing in it.
    expect(filter.queryByRole("button", { name: /Failed/ })).toBeNull();

    await userEvent.click(filter.getByRole("button", { name: /Never requested · 1/ }));

    const narrowed = await gridRows();
    expect(narrowed).toHaveLength(1);
    expect(within(narrowed[0]).getByText("2026-07-13")).toBeInTheDocument();
    // IT HID, IT DID NOT MEASURE — and the way back is on screen.
    expect(screen.getByText(/Showing 1 of 4 days/)).toBeInTheDocument();
    expect(screen.getByText(/hidden, not absent/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Show every day" }));
    expect(await gridRows()).toHaveLength(4);
  });

  it("counts the presets back from the data's own anchor, and says which", async () => {
    const calls = stubCalls(breakdown());
    renderTab();

    // The anchor is the most recent day this Datastream collected anything —
    // 2026-07-13 in the fixture — never the reader's today.
    expect(await screen.findByText(/Counted back from 2026-07-13, the most recent day/))
      .toBeInTheDocument();

    await userEvent.click(within(screen.getByTestId("range-presets"))
      .getByRole("button", { name: "7 days" }));

    await waitFor(() =>
      expect(calls.some((url) => url.includes("start=2026-07-07&end=2026-07-13"))).toBe(true));
  });

  it("asks for a whole calendar month when that is the preset", async () => {
    const calls = stubCalls(breakdown());
    renderTab();

    const presets = within(await screen.findByTestId("range-presets"));
    await userEvent.click(presets.getByRole("button", { name: "This month" }));
    await waitFor(() =>
      expect(calls.some((url) => url.includes("start=2026-07-01&end=2026-07-13"))).toBe(true));

    await userEvent.click(presets.getByRole("button", { name: "Last month" }));
    // The month BEFORE the anchor's, whole — its own first day to its own last.
    await waitFor(() =>
      expect(calls.some((url) => url.includes("start=2026-06-01&end=2026-06-30"))).toBe(true));
  });

  it("says the anchor is the calendar's when no day of the window collected", async () => {
    // A window where nothing was collected has no anchor of its own to derive,
    // and the sentence says so rather than letting the reader assume the ranges
    // are about their today.
    stubBreakdown(breakdown({ days: [] }));
    renderTab();

    expect(await screen.findByText(/yesterday — because no day of this window collected/))
      .toBeInTheDocument();
  });
});

/**
 * FINDING ONE EXECUTION RECORD — amendment of 2026-08-18.
 *
 * This table is where a person arrives holding an id, and it had no order but the
 * server's, no search, and bare ULIDs printed at full width with no way to take
 * one anywhere. `aria-sort` appeared zero times in this console before
 * `SortableHead`, which is the whole reason it exists.
 */
describe("The execution-scoped evidence table", () => {
  function stageRow(overrides: Record<string, unknown> = {}) {
    return {
      id: "stg_1", stage: "published", phase_state: "succeeded",
      execution_id: "dse_01KYJ0NPAAA", plan_version_id: "dsp_1",
      mapping_version_id: "dmap_1", schema_hash: "sha256:aaa",
      row_count: 10, grain_evidence: ["date", "campaign_id"],
      occurred_at: "2026-07-10T08:00:00+00:00", artifact_ref: "art_1",
      profile_evidence: {}, coverage_evidence: {}, safe_error: null,
      ...overrides,
    };
  }

  const STAGES = [
    stageRow(),
    stageRow({ id: "stg_2", execution_id: "dse_01KYJ0NPBBB", schema_hash: "sha256:bbb",
      row_count: 2, occurred_at: "2026-07-11T08:00:00+00:00" }),
    stageRow({ id: "stg_3", execution_id: "dse_01KYJ0NPCCC", schema_hash: "sha256:ccc",
      row_count: 400, occurred_at: "2026-07-09T08:00:00+00:00" }),
  ];

  function renderWithStages() {
    stubBreakdown(breakdown());
    return renderTab({
      payload: {
        ...TAB_PAYLOAD,
        evidence: {
          ...TAB_PAYLOAD.evidence,
          availability: { ...(TAB_PAYLOAD.evidence.availability as object), published: "available" },
          stages: STAGES,
        },
      },
    });
  }

  async function openTable() {
    await userEvent.click(
      await screen.findByRole("button", { name: /Read the executions behind these counts/ }),
    );
    return within(screen.getByRole("region", { name: "Datastream stage evidence" }));
  }

  it("lets an id be taken away instead of read off the screen", async () => {
    renderWithStages();
    const table = await openTable();

    // The id is an OBJECT, with its full value on the element rather than a bare
    // string of letters — and a control that puts it on the clipboard.
    expect(table.getAllByTitle("Execution: dse_01KYJ0NPAAA").length).toBeGreaterThan(0);
    expect(table.getAllByRole("button", { name: /Copy execution id/ })).toHaveLength(3);
    expect(table.getAllByTitle("Schema hash: sha256:aaa").length).toBeGreaterThan(0);
  });

  it("finds the one record a person arrived holding, and says how many matched", async () => {
    renderWithStages();
    await openTable();

    await userEvent.type(
      screen.getByRole("textbox", { name: "Search the execution records" }),
      "sha256:bbb",
    );

    const table = within(screen.getByRole("region", { name: "Datastream stage evidence" }));
    expect(table.getAllByRole("row").slice(1)).toHaveLength(1);
    expect(screen.getByText(/1 of 3 records match/)).toBeInTheDocument();
    // NOTHING IS HIDDEN BEYOND THIS TABLE, and the empty state says the gesture
    // that brings the rest back.
    await userEvent.clear(screen.getByRole("textbox", { name: "Search the execution records" }));
    await userEvent.type(
      screen.getByRole("textbox", { name: "Search the execution records" }), "zzz",
    );
    expect(screen.getByText("No execution record matches that")).toBeInTheDocument();
    expect(screen.getByText(/clear the search/)).toBeInTheDocument();
  });

  it("carries the order on the column, where a screen reader can hear it", async () => {
    renderWithStages();
    const table = await openTable();

    const rows = () => table.getAllByRole("row").slice(1);
    const header = table.getByRole("columnheader", { name: /Rows/ });
    // A column that CAN be sorted and is not says so — a different statement
    // from a column that never sorts.
    expect(header).toHaveAttribute("aria-sort", "none");

    await userEvent.click(within(header).getByRole("button"));
    expect(header).toHaveAttribute("aria-sort", "ascending");
    // 2, 10, 400 — numbers compared as numbers, never as strings.
    expect(within(rows()[0]).getByText("2")).toBeInTheDocument();
    expect(within(rows()[2]).getByText("400")).toBeInTheDocument();

    await userEvent.click(within(header).getByRole("button"));
    expect(header).toHaveAttribute("aria-sort", "descending");
    expect(within(rows()[0]).getByText("400")).toBeInTheDocument();
  });

  it("promises the order over everything it holds, and no page it does not", async () => {
    renderWithStages();
    await openTable();

    expect(screen.getByText(/Every execution record this tab was given is in this table/))
      .toBeInTheDocument();
    // The cursor-collection sentence is NOT borrowed: this table is not paged,
    // and a warning that is untrue here would teach a reader to distrust it
    // where it IS true.
    expect(screen.queryByTestId("page-sort-note")).toBeNull();
  });
});

describe("governed Datastream matching handoff", () => {
  it("names the common key and hands the exact governed match to Analytics", async () => {
    const match = {
      kind: "governed",
      authority: "governed",
      observed_coverage: "exact",
      execution_safety: "ready",
      left: {
        datastream_id: "ds_EXAMPLE",
        name: "Campaign spend",
        mapping_version_id: "dmv_a",
        published_execution_id: "dse_a",
        output_version_id: "dov_a",
        measures: [],
      },
      right: {
        datastream_id: "ds_CONVERSIONS",
        name: "Conversions",
        mapping_version_id: "dmv_b",
        published_execution_id: "dse_b",
        output_version_id: "dov_b",
        measures: [],
      },
      common_key: {
        id: "mck_1",
        name: "Campaign + day",
        version_id: "mckv_1",
        version_number: 3,
        components: [{ canonical_field_id: "mdm_day", canonical_name: "day" }],
      },
      key_paths: [],
      relationship: {
        relationship_name: "spend_to_conversions",
        cardinality: "many_to_one",
        fan_out_policy: "forbid",
        bridge_dataset: null,
        view_id: "sv_1",
        view_version_id: "svv_1",
        view_version_number: 1,
      },
      unlocked_measures: 2,
      analysis: "Campaign spend and Conversions can be compared by campaign and day.",
      explore_together: {
        datastreams: [],
        common_key_version_id: "mckv_1",
        view_version_id: "svv_1",
        relationship_name: "spend_to_conversions",
      },
    } as const;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/analyze/matches")) {
        return {
          ok: true,
          status: 200,
          json: async () => ({ matches: [match], counts: {}, bounds: {}, empty_reason: null }),
        } as Response;
      }
      if (url.includes("/daily-breakdown")) {
        return { ok: true, status: 200, json: async () => breakdown() } as Response;
      }
      throw new Error(`unexpected ${url}`);
    }));
    const openAnalytics = vi.fn();
    renderTab({ onOpenAnalytics: openAnalytics });

    const panel = within(await screen.findByTestId("datastream-match-opportunities"));
    expect(panel.getByText("Conversions")).toBeInTheDocument();
    expect(panel.getByText(/Campaign \+ day · v3 · many_to_one/)).toBeInTheDocument();
    await userEvent.click(panel.getByRole("button", { name: "Open in Analytics" }));
    expect(openAnalytics).toHaveBeenCalledWith(match);
  });
});
