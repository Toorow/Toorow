/**
 * `GET …/daily-breakdown` — the day grid and `Read one day`, which had never once
 * been rendered on the tab route they live on.
 *
 * WHY THIS FILE EXISTS. `DateBreakdownGrid` is mounted by `WorkbenchDataPage`
 * through `DatastreamDailyBreakdown`, and the sandbox had no `/daily-breakdown`
 * branch: the call fell to the harness's `404`, so the `Data` tab drew « The
 * daily breakdown could not be read » and the grid was only ever seen through the
 * `/debug/components` sheet — beside hand-written props, never against a payload.
 *
 * THE ENVELOPE IS `read_daily_breakdown`'s, KEY FOR KEY
 * (`server/core/datastream_daily_breakdown_api.py`, the final `return`). That is
 * not a nicety: the Placements fixture drifted by exactly two keys and produced
 * `Unavailable / 3` and three empty badges in the only preview tool built for
 * that tab, and a design review reported the SCREEN as broken. A fixture that
 * drifts from the server's shape is worse than none.
 *
 * WHAT THE PAYLOAD SAYS ABOUT ITSELF, AND WHY IT IS NOT ALL GREEN:
 *
 *   * `stage_relations` is `resolve_stage_relations(connector="gsc",
 *     report_profile_id="page_daily")`. That profile declares `raw_gsc_daily` and
 *     NO staging model, and four staging models read that relation
 *     (`server/modules/gsc/dbt/staging/`), so the resolver answers
 *     `staging_relation_not_declared`. `Collected` is therefore offered and
 *     `Mapped` is greyed WITH ITS REASON — the exact pair arbitrage 2 of story
 *     58.3 exists to produce, and a state no capture has shown;
 *   * `column_row_join_available` is `false` with its reason, because it is
 *     `False` in the module, unconditionally;
 *   * the money provenance of both zones says `currency_fx` is NOT ACTIVE — lot
 *     B3, amendment 11. It used to say `no_monetary_concept_declared`, which is a
 *     fact about the Semantic Model and is only worth saying once somebody turned
 *     the capability on; `currency_fx` is `draft` on every live project, so what
 *     the server sends is the refusal that names the shut door, and the reading
 *     shows nothing at all about money. « Éteinte, la capacité n'apparaît nulle
 *     part — ni onglet, ni panneau, ni colonne. »
 *   * and `country` IS on, which this fixture already declared for the day grid.
 *     Since lot B3 that same state also colours the field the mapping binds to the
 *     canonical dimension, so `country` is marked in the collected reading. This
 *     is the ONLY place in the product where that branch can be seen: measured
 *     2026-08-12, `app.project_capabilities` holds 31 projects × 6 keys and ZERO
 *     in `ready` or `degraded`.
 */
import { MAPPING_VERSION, PLAN_VERSION, PROJECT_ID } from "./datastreamSandboxScope";
import { CONNECTOR, REPORT_PROFILE } from "./datastreamPullFixtures";

const SCHEMA = "datastream_daily_breakdown.v1";

/** `MAX_WINDOW_DAYS = cache_warehouse.SAMPLE_MAX_DAYS`. */
const BOUNDED_AT = 92;
/** `SERVED_VIEW_MODE = cache_warehouse.SAMPLE_SERVED_STAGE`. */
const SERVED_VIEW_MODE = "processed";

const COLLECTED_RELATION = "raw_gsc_daily";
/** `warehouse_tenancy.raw_schema` with the org-schema flag off, which is the
 *  default: every connector lands in `main` today. */
const ZONE = "main";

const STAGING_NOT_DECLARED =
  "This report profile declares no staging model, and several read its raw relation. "
  + "Naming one of them would show the rows of another profile under this flux.";

const PUBLISHED_HAS_NO_RELATION =
  "Requested stage 'published' has no distinct warehouse materialisation; served the "
  + "closest available stage 'processed' (consolidated mart). Stage version provenance "
  + "cannot be version-bound by the current mart.";

/** `resolve_stage_relations`'s answer for this (connector, profile) pair. */
const STAGE_RELATIONS = {
  report_profile_id: REPORT_PROFILE,
  collected_relation: COLLECTED_RELATION,
  mapped_relation: null,
  reason: "staging_relation_not_declared",
  message: STAGING_NOT_DECLARED,
};

/** `view_mode_availability`, in pipeline order, with each sentence quoted from
 *  the module that measured it. The greyed control and the `422` a forced call
 *  earns read the same words — which is why neither is written here twice. */
const VIEW_MODE = {
  requested: SERVED_VIEW_MODE,
  served: SERVED_VIEW_MODE,
  note: null,
  available: [
    { mode: "collected", available: true, reason: null, relation: COLLECTED_RELATION },
    { mode: "mapped", available: false, reason: STAGING_NOT_DECLARED, relation: null },
    { mode: "processed", available: true, reason: null, relation: "fact_daily_kpi" },
    { mode: "published", available: false, reason: PUBLISHED_HAS_NO_RELATION, relation: null },
  ],
};

/**
 * The header of the grid, from `read_mapping_columns` — the VERSIONED store, so
 * `columns_source` is `mapping_version`. Key columns first, exactly as the module
 * sorts them, and `target_field` is `null` on the two fields nothing binds:
 * that is a state of the mapping, and it is the line a person opens this tab to
 * repair.
 */
const COLUMNS = [
  { source_field: "country", target_field: "country", is_key_column: true, binding_status: "bound", sensitivity: "none" },
  { source_field: "date", target_field: "date", is_key_column: true, binding_status: "bound", sensitivity: "none" },
  { source_field: "page", target_field: "page", is_key_column: true, binding_status: "bound", sensitivity: "none" },
  { source_field: "average_position", target_field: "average_position", is_key_column: false, binding_status: "warning", sensitivity: "none" },
  { source_field: "clicks", target_field: "clicks", is_key_column: false, binding_status: "bound", sensitivity: "none" },
  { source_field: "device", target_field: "device", is_key_column: false, binding_status: "bound", sensitivity: "none" },
  { source_field: "impressions", target_field: "impressions", is_key_column: false, binding_status: "bound", sensitivity: "none" },
  { source_field: "query", target_field: "query", is_key_column: false, binding_status: "ambiguous", sensitivity: "none" },
  { source_field: "search_appearance", target_field: null, is_key_column: false, binding_status: "blocking", sensitivity: "none" },
  { source_field: "search_type", target_field: null, is_key_column: false, binding_status: "blocking", sensitivity: "none" },
];

/**
 * One row of the strip, in `_day`'s own keys.
 *
 * `row_count` is the WINDOW's total and `rows` is the MART's count for the day —
 * story 58.1, arbitrage 9, and they are deliberately different numbers. A day
 * nothing ever covered carries `null` for both and says which absence it is.
 */
function day(
  date: string,
  extractStatus: string,
  jobState: string | null,
  extractCount: number,
  rowCount: number | null,
  rows: number | null,
  extra: Record<string, unknown> = {},
) {
  return {
    date,
    extract_status: extractStatus,
    job_state: jobState,
    extract_count: extractCount,
    row_count: rowCount,
    row_count_reason:
      rowCount === null && extractStatus === "empty"
        ? "The provider returned no row for this day"
        : null,
    expected_rows: null,
    completeness_ratio: null,
    pull_id: extractCount ? `pull_EXAMPLE_${date.replaceAll("-", "")}` : null,
    loaded_at: extractCount ? `${date}T02:06:00Z` : null,
    execution_id: extractCount ? "run_EXAMPLE_42" : null,
    provenance: extractCount ? "scheduler" : null,
    // Story 58.5, arbitrage 6: the day's DATA-QUALITY verdict is a different
    // question from its EXTRACT verdict, and it is empty with its reason on every
    // day of the estate — 0 monitors, 0 evaluations, measured 2026-08-06.
    dq_verdict: null,
    dq_reason: "no_monitor",
    rows,
    rows_reason: null,
    ...extra,
  };
}

/** Seven days, and the seven states the strip paints. `empty` and `never_fetched`
 *  look alike on a chart and mean opposite things, so both are here. */
const DAYS = [
  day("2026-07-27", "ok", "succeeded", 1, 18420, 18420),
  day("2026-07-28", "ok", "succeeded", 1, 18395, 18395),
  day("2026-07-29", "partial", "succeeded", 1, 4100, 4100),
  day("2026-07-30", "empty", "succeeded", 1, null, 0),
  day("2026-07-31", "failed", "failed", 1, null, null, {
    error_class: "provider_quota",
    user_action: "Wait for the quota window to reopen, then re-collect this day.",
  }),
  day("2026-08-01", "never_fetched", null, 0, null, null),
  day("2026-08-02", "ok", "succeeded", 1, 17980, 17980),
];

/** Story 58.5. The capability is `ready` on this Project (the header says so), so
 *  the block carries the measure — and the absence bucket travels beside the
 *  countries rather than competing with them for a place in the ranking. */
const COUNTRY = {
  capability_state: "ready",
  active: true,
  degraded: false,
  bounded_at: 12,
  days: {
    "2026-07-27": {
      values: [
        { value: "fr", kind: "country", label: "fr", rows: 12010 },
        { value: "be", kind: "country", label: "be", rows: 4200 },
        { value: "ch", kind: "country", label: "ch", rows: 2010 },
        { value: "__country_absent__", kind: "absent", label: "No country reported", rows: 200 },
      ],
      country_count: 3,
    },
    "2026-08-02": {
      values: [
        { value: "fr", kind: "country", label: "fr", rows: 11800 },
        { value: "be", kind: "country", label: "be", rows: 4100 },
        { value: "__country_absent__", kind: "absent", label: "No country reported", rows: 2080 },
      ],
      country_count: 2,
    },
  },
  reason: null,
};

/**
 * `currency_fx` is not active, so neither zone designates anything — lot B3.
 *
 * `money_provenance_off`, key for key. The keys are all present and empty: a
 * screen must not have to tell a refusal from an answer by counting keys, which
 * is the rule story 58.7 already holds for this block.
 */
function moneyProvenance() {
  return {
    zone: null as string | null,
    columns: [] as unknown[],
    reason: "currency_fx_not_active",
    message:
      "Currency & FX is not active on this Project, so no amount of this reading carries a "
      + "rate, a rate date or a currency. Nothing is hidden: nothing was computed.",
    title: null as string | null,
    authority: null as string | null,
    capability_key: "currency_fx",
    capability_state: "draft",
    reporting_currency: null,
    reporting_currency_reason: null as string | null,
    reporting_currency_gate: null as string | null,
    rows: [] as unknown[],
  };
}

/**
 * What the ACTIVE `country` capability does to this reading — lot B3.
 *
 * `read_reading_capabilities` emits the effect and the fields it lands on, never
 * a row naming the capability: an inventory of modules is not a functionality.
 * The collected relation carries `country` and the mapping binds it to the
 * canonical dimension, so the field is marked there; the mapped side of this
 * profile is unreadable, so it names none.
 */
const READING_CAPABILITIES = [
  {
    key: "country",
    state: "ready",
    degraded: false,
    effect: "field_marked",
    title: "Country",
    message:
      "The field marked below is the one the active mapping binds to the canonical country "
      + "dimension, so a row of this day can be read by country.",
    reason: null,
    fields: { collected: ["country"], mapped: [] as string[] },
  },
];

/**
 * The COLLECTED side of one day, read from `raw_gsc_daily`.
 *
 * The masking is the day reading's own inversion: a column is shown only where
 * the mapping classifies it `none`, so the four columns of the relation that NO
 * mapping field names — `hour`, `pull_id`, `loaded_at`, `project_id` — are masked
 * and everything the mapping declares is shown. That is the rule, and a fixture
 * that masked nothing would hide it.
 */
function collectedSide(date: string) {
  const columns = [
    "date", "page", "country", "device", "query", "search_type", "search_appearance",
    "hour", "clicks", "impressions", "average_position", "pull_id", "loaded_at", "project_id",
  ];
  const row = (page: string, country: string, clicks: number, impressions: number, position: string) => ({
    date,
    page,
    country,
    device: "desktop",
    query: "pricing",
    search_type: "web",
    search_appearance: null,
    hour: "[MASKED]",
    clicks,
    impressions,
    average_position: position,
    pull_id: "[MASKED]",
    loaded_at: "[MASKED]",
    project_id: "[MASKED]",
  });
  return {
    relation: COLLECTED_RELATION,
    zone: ZONE,
    columns,
    rows: [
      row("/pricing", "fr", 128, 3140, "4.20"),
      row("/product", "fr", 96, 2870, "6.10"),
      row("/pricing", "be", 74, 1980, "5.40"),
    ],
    row_count: 3,
    truncated: false,
    masked_fields: ["hour", "loaded_at", "project_id", "pull_id"],
    reason: null,
    message: null,
    note: null,
    note_message: null,
    money_provenance: { ...moneyProvenance(), zone: "collected" },
  };
}

/** The MAPPED side, which this profile cannot have: `_undeclared` carries the
 *  RESOLVER's reason unread, and `rows` is `null` — never `[]`, which would be a
 *  relation that was read and held nothing. */
function mappedSide() {
  return {
    relation: null,
    zone: null,
    columns: [] as string[],
    rows: null,
    row_count: null,
    truncated: false,
    masked_fields: [] as string[],
    readable: false,
    reason: "staging_relation_not_declared",
    message: STAGING_NOT_DECLARED,
    note: null,
    note_message: null,
    money_provenance: { ...moneyProvenance(), zone: "mapped" },
  };
}

/** One side of the pair could not be read, so nothing can be paired with it —
 *  `_refused(SIDE_UNREADABLE)`, the whole shape, never a missing key. */
const PAIRING_REFUSED = {
  available: false,
  reason: "side_unreadable",
  message:
    "One of the two readings could not be read, so there is nothing to pair it with. The "
    + "reading that refused says why, in its own words.",
  columns: [] as unknown[],
  key_columns: [] as unknown[],
  unpaired_columns: [] as unknown[],
  rows: [] as unknown[],
  row_count: 0,
  paired_row_count: 0,
  unpaired_row_count: 0,
  truncated: false,
  bounded_at: null,
};

/** The shapes this sandbox can be addressed in. `empty` is a Datastream that has
 *  never collected anything: `days` is `[]` — 92 identical « never fetched » rows
 *  are noise, not evidence — with `reason: no_run_in_window`. */
export type DayShape = "served" | "empty";

export const DAY_SHAPES: readonly DayShape[] = ["served", "empty"];

/**
 * The whole envelope. `day` is the query parameter: the route reads one day's
 * rows only when it was asked for one, so `reading` is `null` on the window call
 * and present on the day call — which is exactly how `DatastreamDailyBreakdown`
 * makes its two requests.
 */
export function dailyBreakdownFixture({
  datastreamId,
  start,
  end,
  day: openedDay,
  shape,
  mode,
  columns = COLUMNS,
}: {
  datastreamId: string;
  start: string;
  end: string;
  day: string | null;
  shape: DayShape;
  mode: string;
  /** The header of the grid. It is read from the MAPPING, so it belongs to the
   *  Datastream and not to the mode: a file source has one too, and its columns
   *  are the file's. */
  columns?: unknown[];
}) {
  // A `managed_feed` enqueues no pull window at all, so it HAS no extract
  // registry to read — `days: []`, `connector: null`, `rows: null`. Stated by
  // the module in those words, and pinned by its test.
  const pushed = mode === "managed_feed";
  const served = shape === "served" && !pushed;
  return {
    schema: SCHEMA,
    project_id: PROJECT_ID,
    datastream_id: datastreamId,
    connector: pushed ? null : CONNECTOR,
    window: { start, end, bounded_at: BOUNDED_AT, bound_reached: false },
    view_mode: VIEW_MODE,
    stage_relations: pushed
      ? {
          report_profile_id: null,
          collected_relation: null,
          mapped_relation: null,
          reason: "report_profile_not_set",
          message:
            "This Datastream names no report profile, so no relation can be resolved for it. "
            + "The profile is what says which relation of the connector its rows land in.",
        }
      : STAGE_RELATIONS,
    reading:
      openedDay && served
        ? {
            window: { start: openedDay, end: openedDay },
            day: openedDay,
            collected: collectedSide(openedDay),
            mapped: mappedSide(),
            pairing: PAIRING_REFUSED,
            capabilities: READING_CAPABILITIES,
          }
        : null,
    versions: {
      plan_version_id: PLAN_VERSION,
      mapping_version_id: MAPPING_VERSION,
      // No mart column binds a row to a version, so the honest answer is the one
      // `_get_datastream_sample` already gives.
      version_binding_available: false,
    },
    rows_note: null,
    // The country block can never be safer than the total it decomposes: a flux
    // whose rows the mart does not hold refuses the split for the SAME reason and
    // in the same word. A `managed_feed` names no connector, so the mart models
    // nothing for it.
    country: served
      ? COUNTRY
      : {
          capability_state: "ready",
          active: true,
          degraded: false,
          reason: pushed ? "connector_not_in_mart" : "no_country_row_in_window",
        },
    columns,
    columns_reason: columns.length ? null : "no_mapping",
    columns_source: columns.length ? "mapping_version" : null,
    column_row_join_available: false,
    column_row_join_reason: "mart_metric_is_a_dbt_literal",
    days: served ? DAYS : [],
    reason: served ? null : "no_run_in_window",
  };
}
