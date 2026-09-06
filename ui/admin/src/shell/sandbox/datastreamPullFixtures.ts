/**
 * The Datastream that PULLS from a connector, as the server composes it.
 *
 * Moved out of `DatastreamWorkbenchSandbox.tsx` unchanged except where it lied
 * about the wire — the four repairs of the 2026-08-12 pass are marked below. The
 * move itself is the 1 000-line bound: the sandbox now also answers `/progress`,
 * `/daily-breakdown`, `/workbench/landed-file` and a second source mode.
 *
 * THE CONNECTOR IS `gsc`, AND IT IS NOT A RENAME FOR TASTE. Every fixture here
 * said `search_console`, which is not a connector of this repository: 39 modules
 * are declared under `server/modules/` and the Google Search Console one is
 * `gsc`. Three things read that key for real — `stage_relation_resolver` resolves
 * `(connector, report_profile_id)` to a relation, the mart is grained on
 * `connector`, and `placement_dimension_for` looks it up — so a fixture naming a
 * module that does not exist can only ever render absences, and the absences it
 * renders are not the ones the product produces. The ten fields below are
 * *already* the ten columns of `raw_gsc_daily` (`server/modules/gsc/connector.py`,
 * the `CREATE TABLE`), which is what made the drift measurable.
 */
import { LINKS, MAPPING_VERSION, ORG_ID, PLAN_VERSION, PROJECT_ID, DATASTREAM_ID } from "./datastreamSandboxScope";

/** `app.datastreams.module_name` — the identity every read is scoped by. */
export const CONNECTOR = "gsc";
/** `config.connector_name` — the LABEL, and never an identity. */
export const CONNECTOR_LABEL = "Google Search Console";
/** A report profile the manifest really declares (`gsc/manifest.json`). */
export const REPORT_PROFILE = "page_daily";

export const HEADER = {
  schema: "datastream_workbench.header.v1",
  identity: {
    datastream_id: DATASTREAM_ID,
    project_id: PROJECT_ID,
    name: "Search Console — daily",
    mode: "connector_pull",
    data_role: "Performance",
    owner: "owner@example.com",
    module: CONNECTOR,
    connector: CONNECTOR_LABEL,
    source_account_ref: "sacc_EXAMPLE",
    declared_writer: null,
    business_domains: [{ id: "bd_EXAMPLE", name: "Organic acquisition", slug: "organic-acquisition" }],
    // AI-113. `compose_header` sorts the Datastream's declared channels and sends
    // the list on every header; a connector pull declares none, and `[]` is that
    // answer. It was MISSING, and a missing key and an empty list are the two
    // things `WorkbenchProcessingPage` tells apart to decide whether an inbound
    // address can be issued.
    delivery_channels: [],
  },
  axes: {
    lifecycle: "Active",
    // `Blocked`, not `Ready`, and the two are not a matter of taste: the data
    // fixture below carries `mapping_executable: false`, and `_configuration_axis`
    // now returns `Blocked` for exactly that. A fixture saying `Ready` beside a
    // panel saying "not executable" shows a state the product cannot produce --
    // and a reviewer reading the capture would report the contradiction as a
    // product defect.
    configuration: "Blocked",
    // `Blocked`, not `Degraded`, for the same reason one line up.
    // `_operations_axis` reads the last TERMINAL run and hands the state to the
    // registry: the runs fixture below has `run_EXAMPLE_42` in `ready` (which
    // `executionStates.ts` marks non-terminal) and `run_EXAMPLE_41` in `failed`,
    // whose axis is `Blocked`. `Degraded` is the axis of `cancelled`, and no run
    // of this fixture was ever cancelled.
    operations: "Blocked",
    publication: "Candidate",
  },
  versions: {
    active_plan: PLAN_VERSION,
    active_plan_number: 1,
    active_mapping: MAPPING_VERSION,
    active_mapping_number: 1,
    proposed_plan: null,
    proposed_plan_number: null,
    proposed_mapping: null,
    proposed_mapping_number: null,
    ready_mapping_proposal: null,
  },
  runs: { latest: "run_EXAMPLE_42", latest_state: "Ready" },
  // THE EVIDENCE UNDER THE OPERATIONS AXIS, and it was absent from every capture.
  // `axisEvidence` (`DatastreamWorkbenchRoute.tsx`) falls back to "No schedule
  // state — the clock was never armed" when this key is missing, so the header
  // said the clock had never been armed while the `/schedule` fixture beside it
  // said `nightly`, armed, next run at 02:00. One of the two was wrong in every
  // screenshot, and it was this one.
  operations_evidence: {
    mode: "nightly",
    next_run_at: "2026-08-03T02:00:00Z",
    missed_run_count: 0,
    schedule_state_known: true,
    late_reasons: [],
  },
  // THE THREE POINTERS ARE EXECUTIONS, AND THEY WERE THREE INVENTED IDS.
  //
  // `compose_header` fills them from `candidate_execution_id`,
  // `current_published_execution_id` and `last_known_good_execution_id`
  // (`datastream_workbench.py`): the wire carries RUN ids. `pub_EXAMPLE_*` is a
  // shape the server never sends, and `WorkbenchOutputsPage` reads them as runs
  // — so `Candidate readiness` found no Output version recording `pub_EXAMPLE_2`
  // and drew "Candidate evidence unavailable … it must not be promoted from this
  // screen", a warning produced entirely by this fixture, and each of the three
  // links opened the `Runs` tab on a selection that is in no run list.
  //
  // The combination was also one the server cannot compose: `_publication_axis`
  // returns `Candidate` only when there is NO current publication, and the axis
  // declared above is `Candidate` while a `current` and a `last_known_good` sat
  // here. One candidate, nothing served yet — that is the state, and
  // `run_EXAMPLE_42` is the execution the Output version below records.
  publications: { candidate: "run_EXAMPLE_42", current: null, last_known_good: null },
  // Shaped like `compose_header` really composes them
  // (`datastream_workbench.py`, the `links` block): `/org/{org}/project/{project}/…`, the
  // grammar `parsePath` actually reads. They were `/p/{project}/…`, which the
  // router refuses on its first segment — so the three owner links under the
  // state axes were three dead links on the one screen that exists to be looked
  // at, and a capture of it showed the product offering them. Fourth fixture in
  // this file to have lied about the wire; this one lied about the address.
  links: LINKS,
  primary_action: {
    kind: "review_candidate",
    label: "Review candidate",
    // `_primary_action`'s own sentence. It read "A Ready candidate exists and has
    // never been reviewed", which no branch of the server composes — and it is
    // the `title` of the one button in the page header.
    reason: "A non-live candidate is ready for review.",
    tab: "outputs",
  },
  // The conditional tabs, so the sandbox shows the band the amendment contracts
  // (story 58.6). `open: true` because a sandbox exists to be LOOKED AT and a
  // capability that is off has, by contract, nothing to look at; the closed half
  // is asserted in `WorkbenchCostTab.test.tsx`, twice, rather than drawn here.
  //
  // SIX ENTRIES SINCE STORY 61.5, because `Overview` draws one module row per
  // entry. One entry here would have shown one module out of the six the product
  // has, on the very screen that exists to be looked at.
  capability_tabs: [
    // `ready`, and it is the switch the DAY GRID reads. `_country_split` refuses
    // the whole country block when `app.project_capabilities.state` is not
    // `ready`/`degraded`, so `disabled` here — beside a capability projection two
    // panels down that reports `country` as APPLICABLE and COVERED — meant the
    // two halves of the same question answered differently in one capture.
    { capability_key: "country", tab: null, availability: "optional", state: "ready", open: false },
    { capability_key: "currency_fx", tab: null, availability: "always_present", state: "draft", open: false },
    { capability_key: "reporting_timezone", tab: null, availability: "always_present", state: "draft", open: false },
    { capability_key: "tax_fees", tab: "cost", availability: "optional", state: "ready", open: true },
    { capability_key: "competitors", tab: null, availability: "optional", state: "disabled", open: false },
    // Story 61.1. `ready` and `open`, so the second conditional tab can be looked
    // at — which is what this sandbox is for. The capability being ON is not the
    // same claim as the connector having placements: the fixture below shows this
    // Datastream reaching this tab and finding no placement dimension, which is
    // the state 37 connectors of 39 reach.
    { capability_key: "placement_mapping", tab: "placements", availability: "optional", state: "ready", open: true },
  ],
};

/** The ten `raw_gsc_daily` fields, with the binding evidence the compiler emits. */
const FIELDS = [
  ["date", "dimension", "date", "date", "primary_date", null, "bound", "high", true],
  ["page", "dimension", "string", "page", "dimension", null, "bound", "high", true],
  ["country", "dimension", "string", "country", "dimension", null, "bound", "high", true],
  ["device", "dimension", "string", "device", "dimension", null, "bound", "medium", false],
  ["query", "dimension", "string", "query", "dimension", null, "ambiguous", "low", false],
  ["search_type", "dimension", "string", null, null, null, "blocking", "low", false],
  ["search_appearance", "dimension", "string", null, null, null, "blocking", "low", false],
  ["clicks", "measure", "integer", "clicks", "metric", "sum", "bound", "high", true],
  ["impressions", "measure", "integer", "impressions", "metric", "sum", "bound", "high", true],
  ["average_position", "measure", "decimal", "average_position", "metric", "custom", "warning", "medium", false],
].map(([id, role, semantic, canonical, mdm, aggregation, status, confidence, included]) => ({
  field_id: id,
  source_identity: id,
  role,
  semantic_type: semantic,
  canonical_target: canonical,
  mdm_target: mdm,
  aggregation,
  sensitivity: "none",
  included,
  binding: { status, confidence, evidence: "Connector contract" },
}));

const MAPPING_PAYLOAD = {
  fields: FIELDS,
  grain: ["date", "page", "country"],
  joint_grain: ["date", "country"],
};

const PLAN_PAYLOAD = {
  source: {
    kind: "connector_pull",
    module: CONNECTOR,
    // `plan.source.report_id` IS the report profile id — `datastream_activation.py`
    // reads `report_profile_id = source.get("report_id")` and hands it to
    // `resolve_stage_relations`. It was `searchanalytics.query`, the Google API
    // method, which resolves to no profile of any manifest.
    report_id: REPORT_PROFILE,
    selection: {
      selection_mode: "explicit",
      metrics: ["clicks", "impressions"],
      dimensions: ["date", "page", "country"],
      filters: [],
      grain: ["date", "page", "country"],
    },
  },
  destination: { policy: "governed_full_grain" },
  schedule: { mode: "daily", interval_minutes: 1440, timezone: "Europe/Paris" },
  historical: { start: "2026-05-01", end_exclusive: null },
};

const CAPABILITIES = {
  schema: "datastream_capability_projection.v1",
  // Shaped like `read_datastream_capabilities` really emits it
  // (`capability_proposals.py`). The previous fixture invented
  // `capability` / `state` / `coverage` / `reasons`, none of which the server
  // sends and none of which the panel reads -- so every row rendered with a
  // BLANK name and React warned about duplicate keys on every capture. A
  // fixture that does not match the wire makes the sandbox lie about the one
  // panel it was mounted to show.
  capabilities: [
    {
      // `country`, the canonical key of `project_settings.py`. My first
      // rewrite wrote `country_split` -- the MODULE's name, not the
      // capability's -- so the row rendered its raw slug while its sibling
      // rendered "Currency & FX". The label map was right; the fixture was not.
      capability_key: "country",
      availability: "general",
      applicability: "applicable",
      coverage_state: "covered",
      impact: { bound_fields: 1, expected_fields: 1 },
      blockers: [],
      exceptions: [],
      repair: null,
      governance_owner_reference: { object_type: "project-settings", id: PROJECT_ID },
    },
    {
      capability_key: "currency_fx",
      availability: "general",
      applicability: "not_applicable",
      coverage_state: "not_applicable",
      reason: "This Datastream pins no money field, so no conversion can apply.",
      impact: {},
      blockers: [],
      exceptions: [],
      repair: null,
      governance_owner_reference: { object_type: "project-settings", id: PROJECT_ID },
    },
  ],
};

const STAGE_EVIDENCE = ["collected", "mapped", "processed", "published"].map((stage, index) => ({
  id: `stg_EXAMPLE_${index + 1}`,
  stage,
  phase_state: "complete",
  execution_id: "run_EXAMPLE_42",
  plan_version_id: PLAN_VERSION,
  mapping_version_id: MAPPING_VERSION,
  schema_hash: "sha256:9f21c4",
  row_count: [18422, 18422, 17980, 17980][index],
  grain_evidence: { grain: ["date", "page", "country"] },
  occurred_at: "2026-08-02T02:14:00Z",
  artifact_ref: `gs://example/${stage}/run_EXAMPLE_42`,
  profile_evidence: { null_rate: "0.00", distinct_pages: 4120, distinct_countries: 48 },
  coverage_evidence: { expected_days: 7, observed_days: 6, missing_days: ["2026-07-30"] },
  safe_error: "",
}));

/**
 * THE `Placements` TAB, IN THE SHAPE THE SCREEN REALLY READS — the fourth defect
 * of the 2026-08-12 pass, and the one that had already shipped a WRONG READING.
 *
 * Two keys were stale, both from before story 61.2:
 *
 *   * `line_counts.attached` — `WorkbenchPlacementsPage.tsx` reads
 *     `counts.matched` for the `Plan lines matched` metric, and `matched` is not
 *     a rename of `attached`: a line whose every match is orphaned receives
 *     nothing from the mart, so it is NOT matched while it IS attached. The
 *     metric rendered `Unavailable / 3`;
 *   * `attached: false` on each line, with no `matching_state` — the same story
 *     replaced that boolean with the state word and its label, and the badge
 *     column rendered three empty badges.
 *
 * And four keys the server sends on EVERY answer were simply absent: `money`,
 * `ambiguity`, `match_level_unrecorded_reason` and `unmapped.counts` /
 * `.empty_message` / `.reason_required_message`. Each is read by a panel of this
 * tab, so each was a panel judged against a payload the server never sends.
 */
const PLACEMENTS = {
  state: "available",
  capability: { key: "placement_mapping", state: "ready", active: true },
  reason: "No plan line names this connector yet",
  empty_code: "no_line_names_this_connector",
  owner: "Governance",
  placement_dimension: {
    declared: false,
    dimension: null,
    source_field: null,
    // `NO_PLACEMENT_DIMENSION_REASON`, verbatim (`plan_line_placements.py`).
    reason:
      "This connector declares no placement dimension in its manifest, so no placement "
      + "can be observed on it and none can be attached. Only 2 of the 39 connectors "
      + "declare one.",
  },
  plans: [
    { id: "mplan_EXAMPLE_brand", name: "Brand — H2", currency: "EUR", created_at: "2026-07-01T08:00:00Z" },
    { id: "mplan_EXAMPLE_alwayson", name: "Always-on", currency: "EUR", created_at: "2026-01-05T08:00:00Z" },
  ],
  selected_plan: { id: "mplan_EXAMPLE_brand", name: "Brand — H2", currency: "EUR", created_at: "2026-07-01T08:00:00Z" },
  grain: {
    connector: CONNECTOR,
    datastream_grain: false,
    reason_code: "datastream_scope_ambiguous",
    datastreams_on_connector: 2,
    reason:
      `This reading is the ${CONNECTOR} slice of the Project, not this Datastream's: the mart `
      + "carries no Datastream discriminator, and 2 Datastream(s) of this Project collect from "
      + "this connector.",
  },
  // `matched`, and the third key of the block: `nothing_awaiting_message` is
  // `null` whenever something IS awaiting, which is the case here.
  line_counts: { total: 3, matched: 0, nothing_awaiting_message: null },
  lines: [
    { line_key: "line_brand_display", label: "Brand display", channel: "display", start_date: "2026-07-01", end_date: "2026-12-31", budget: "120000.00", buy_mode: "cpm", is_plan_only: false, matching_state: "unmatched", matching_state_label: "Nothing observed", campaigns: [] },
    { line_key: "line_brand_video", label: "Brand video", channel: "video", start_date: "2026-07-01", end_date: "2026-12-31", budget: "80000.00", buy_mode: "cpv", is_plan_only: false, matching_state: "unmatched", matching_state_label: "Nothing observed", campaigns: [] },
    { line_key: "line_brand_search", label: "Brand search", channel: "search", start_date: "2026-07-01", end_date: "2026-12-31", budget: "40000.00", buy_mode: "cpc", is_plan_only: false, matching_state: "unmatched", matching_state_label: "Nothing observed", campaigns: [] },
  ],
  // `AMBIGUITY_IS_COMPUTED_ON_DEMAND`, verbatim, with the gesture that computes
  // the candidates. Absent, the `Suggest matches` control had no reason beside it
  // and never appeared — a state of the tab nobody could see.
  ambiguity: {
    available: false,
    reason:
      "No plan line is reported as an ambiguity here: the candidate campaigns that would "
      + "justify one are computed on request (core/plan_mapping_suggest.py sweeps every line "
      + "against every campaign of this connector over the plan's window), and paying that "
      + "sweep on every open would charge it to people who never asked. Ask for the matches to "
      + "see the candidates and the arbitrations. Several placements on one line is the normal "
      + "case and is never an ambiguity.",
    action_label: "Suggest matches",
  },
  match_level_unrecorded_reason:
    "This match was made before the level was recorded, so how it was obtained is not "
    + "known. It is not a manual match: nothing states who or what proposed it.",
  // Story 61.4. `plan_currency_mismatch` is not the interesting case here: the
  // plan is in EUR and so is the Project's canonical currency, and no Money
  // Policy is confirmed on this fixture — which is `money_policy_unconfirmed`,
  // the state of 18 projects out of 18.
  money: {
    state: "money_policy_unconfirmed",
    plan_currency: "EUR",
    spend_currency: "EUR",
    reporting_currency: null,
    money_policy_version_id: null,
    comparable: false,
    message:
      "This connector's spend is in EUR, which an operator set rather than a confirmed "
      + "Money Policy. The amounts are shown under EUR and the Project has no governed "
      + "reporting currency yet.",
    analyze_reference: {
      surface: "project", workspace: "analyze", section: "explore",
      global_surface: null, global_section: null,
      object_type: null, object_id: null, tab: null, action: null,
      version_id: null, evidence_id: null,
    },
    analyze_label: "Open Analyze",
    analyze_reason:
      "Planned versus observed -- the consumed share, the pace against the allocation, the "
      + "remainder and the extrapolation -- is an Analyze reading, with the reporting currency "
      + "and its FX provenance. It is not read on this tab. The MCP App carries the whole "
      + "Result today through the context card `mediaplan_pacing`; the console Report that "
      + "would carry it is owed and not delivered.",
    kpi_variance_reason:
      "No KPI target is stored anywhere, so no KPI variance can be computed. A media plan "
      + "line carries eleven columns -- id, version, key, label, channel, start date, end "
      + "date, budget, buy mode, plan-only flag, sort order -- and none of them is a target "
      + "for impressions, clicks, CPM or conversions. Only the budget can be compared.",
  },
  unmapped: {
    window: { start: "2026-07-01", end: "2026-12-31" },
    rows: [],
    counts: { total: 0, accepted: 0, awaiting_decision: 0 },
    reason:
      "Spend this connector reported inside the plan's window that no active match "
      + "ventilates. It is a decision to take, not an error.",
    empty_message: "No spend of this connector falls outside this plan",
    reason_required_message:
      "Accepting spend as unplanned requires a reason: it is what makes the row a decision "
      + "rather than a click.",
  },
  observed_placements: { state: "not_applicable", values: [], reason: null },
  governance_owner_reference: {
    surface: "project", workspace: "governance", section: "master-data",
    object_type: null, object_id: null, tab: null, action: null,
    version_id: null, evidence_id: null, global_surface: null, global_section: null,
  },
};

export const EVIDENCE: Record<string, Record<string, unknown>> = {
  overview: {
    header: HEADER,
    state: "available",
    schedule: { mode: "daily", date_window_days: 7, next_run_at: "2026-08-03T02:00:00Z", timezone: "Europe/Paris" },
    latest_execution: { id: "run_EXAMPLE_42", state: "Ready", started_at: "2026-08-02T02:00:00Z", row_count: 17980 },
    // The four named metrics the server now composes.
    run_success: { window_days: 30, terminal_count: 120, succeeded_count: 119 },
    published_at: "2026-08-03T04:00:00Z",
    published_row_count: 18420,
    // Two blocking bindings: the fixture shows the state a person must ACT on,
    // not the settled one. A sandbox that only ever renders "healthy" cannot
    // show whether the unhealthy path leads anywhere.
    mapping_health: { version_id: MAPPING_VERSION, executable: false, blocking_count: 2 },
    stage_coverage: [{ stage: "published", evidence_count: 6, latest_at: "2026-08-02T02:14:00Z" }],
    downstream_count: 2,
    capabilities: CAPABILITIES,
  },
  data: {
    stages: STAGE_EVIDENCE,
    availability: { collected: "available", mapped: "available", processed: "available", published: "available" },
    // WHAT THE SERVER COMPOSES FOR *THIS* SHAPE — finding D-7 of the visual
    // review #69. The comment that stood here said the `data` branch of
    // `read_tab` "is not conditional"; it is, and on one thing: `module_name`.
    // A connector is what the mart is grained on, so a Datastream that names one
    // earns `sample_state: "reachable"` and NO sentence — the console then draws
    // the sample itself rather than a reason it cannot.
    //
    // This fixture names `gsc`, and the route that serves the sample is mounted:
    // `datastream_workbench_api.py` registers `…/runs/{execution_id}/sample`, and
    // the sandbox answers it with the `SAMPLE` fixture below. Holding
    // `unavailable` here kept that fixture unreachable and put an absence on the
    // one tab whose subject is the rows — while the old sentence went on naming
    // story 47.5, a reference of this repository, on a user's screen.
    sample_state: "reachable",
    sample_reason: null,
    state: "available",
  },
  mapping: {
    versions: [
      {
        id: MAPPING_VERSION,
        version_number: 1,
        content_hash: "sha256:1a4f9c",
        source_schema_hash: "sha256:9f21c4",
        capability_fingerprint: "cap:country_split",
        plan_version_id: PLAN_VERSION,
        executable: true,
        blocking_count: 2,
        mapping_payload: MAPPING_PAYLOAD,
        ossie_projection: { semantic_model: { metrics: [] } },
        created_by: "person_EXAMPLE",
        created_at: "2026-07-30T23:09:00Z",
      },
    ],
    active_version: MAPPING_VERSION,
    capabilities: CAPABILITIES,
  },
  processing: {
    plans: [
      {
        id: PLAN_VERSION,
        version_number: 1,
        content_hash: "sha256:77bd21",
        executable: true,
        normalized_payload: PLAN_PAYLOAD,
        validation_issues: [],
        created_at: "2026-07-30T23:09:00Z",
      },
    ],
    active_version: PLAN_VERSION,
    active_mapping_version: MAPPING_VERSION,
    capabilities: CAPABILITIES,
  },
  runs: {
    runs: [
      {
        id: "run_EXAMPLE_42",
        state: "Ready",
        outcome: "ready",
        started_at: "2026-08-02T02:00:00Z",
        finished_at: "2026-08-02T02:14:00Z",
        row_count: 17980,
        plan_version_id: PLAN_VERSION,
        mapping_version_id: MAPPING_VERSION,
        interval_start: "2026-07-26",
        interval_end_exclusive: "2026-08-02",
      },
      {
        id: "run_EXAMPLE_41",
        state: "Failed",
        outcome: "failed",
        started_at: "2026-08-01T02:00:00Z",
        finished_at: "2026-08-01T02:03:00Z",
        row_count: 0,
        plan_version_id: PLAN_VERSION,
        mapping_version_id: MAPPING_VERSION,
        interval_start: "2026-07-25",
        interval_end_exclusive: "2026-08-01",
        safe_error: "The Search Console quota was exhausted for this property.",
      },
    ],
    timeline: [
      { stage: "extract", state: "complete", occurred_at: "2026-08-02T02:02:00Z", row_count: 18422 },
      { stage: "load", state: "complete", occurred_at: "2026-08-02T02:06:00Z", row_count: 18422 },
      { stage: "map", state: "complete", occurred_at: "2026-08-02T02:09:00Z", row_count: 18422 },
      { stage: "process", state: "complete", occurred_at: "2026-08-02T02:12:00Z", row_count: 17980 },
      { stage: "publish", state: "candidate", occurred_at: "2026-08-02T02:14:00Z", row_count: 17980 },
    ],
  },
  outputs: {
    // Shaped like `read_tab("outputs")` really selects it
    // (`datastream_workbench.py`, the `outputs` branch): output_kind / stable_name /
    // version_id / execution_id / delivery_ref / created_at, and used_by with
    // its `owner_href`. The previous fixture invented `kind` / `location` /
    // `candidate` / `row_count` / `published_at` -- none of which the page
    // reads -- so EVERY cell of the Physical Outputs table rendered
    // "Unavailable" and a design review reported it as a product defect.
    // Third fixture in this file to have lied about the wire.
    outputs: [
      {
        id: "out_EXAMPLE_1",
        output_kind: "governed_full_grain",
        stable_name: "gsc_daily",
        version_id: "outv_EXAMPLE_3",
        execution_id: "run_EXAMPLE_42",
        publication_log_id: "publog_EXAMPLE_1",
        plan_version_id: PLAN_VERSION,
        mapping_version_id: MAPPING_VERSION,
        projection_version_ref: null,
        relation_ref: "example_dataset.gsc_daily",
        delivery_ref: null,
        schema_hash: "sha256:9f21c4",
        grain_evidence: { grain: ["date", "page", "country"] },
        evidence: { row_count: 17980 },
        created_at: "2026-08-01T02:15:00Z",
      },
    ],
    pointers: HEADER.publications,
    // THE TWO KEYS THE `outputs` BRANCH ALWAYS SENDS, and this fixture sent
    // neither: `read_tab` composes `raw_zone_policy` from
    // `config.destination.policy` with `"managed_raw"` as its default, and
    // `retention_days` from the same object with no default at all. Absent from
    // the payload, the tab still drew "Managed Raw" and "Indefinite" — from its
    // own fallbacks, so the capture was proving the screen against nothing. This
    // Datastream declares no destination, which is what these two values are.
    raw_zone_policy: "managed_raw",
    retention_days: null,
    used_by: [
      {
        output_id: "out_EXAMPLE_1",
        output_version_id: "outv_EXAMPLE_3",
        consumer_kind: "semantic_view",
        consumer_ref: "sv_EXAMPLE",
        consumer_version_ref: "svv_EXAMPLE_2",
        owner_href: `/org/${ORG_ID}/project/${PROJECT_ID}/governance/semantic-model`,
        created_at: "2026-08-01T02:20:00Z",
      },
      {
        output_id: "out_EXAMPLE_1",
        output_version_id: "outv_EXAMPLE_3",
        consumer_kind: "report",
        consumer_ref: "rep_EXAMPLE",
        consumer_version_ref: null,
        owner_href: `/org/${ORG_ID}/project/${PROJECT_ID}/analyze/reports`,
        created_at: "2026-08-01T02:20:00Z",
      },
    ],
  },
  // THE NUMBERS BELOW ARE MEASURED, NOT CHOSEN (story 58.6, arbitrage 7). Every
  // amount is a row of `main_marts.fee_tax_ladder_daily` for the DuckDB fixture
  // Project `feetax_dev_complete`, read on 2026-08-07, and the two levels of the
  // platform-fee phase are that fixture's own rules -- one laid at the Project,
  // one at the Datastream. A fixture chosen to read well produces a screen that
  // looks like it works, which is the fault this repository pays for most often.
  cost: {
    state: "available",
    capability: { key: "tax_fees", state: "ready", active: true },
    window: {
      start: "2026-07-09",
      end: "2026-08-07",
      days: 30,
      reason: "The cascade is read over the last 30 days. This tab holds no window control, so the bound is stated rather than left to be mistaken for the whole history.",
    },
    grain: {
      connector: CONNECTOR,
      datastream_grain: false,
      ambiguous: false,
      datastreams_on_connector: null,
      reason: "The cascade is computed per connector, not per Datastream: the mart carries no Datastream discriminator.",
    },
    currency: "EUR",
    currencies: ["EUR"],
    measures: [
      { key: "net_media", label: "Net media", unit: "micros", micros: 12345670000, percent: null, currency: "EUR", gap_code: null, reason: null },
      { key: "what_we_add", label: "What we add", unit: "micros", micros: 7593313266, percent: null, currency: "EUR", gap_code: null, reason: null },
      { key: "total", label: "Total", unit: "micros", micros: 19938983266, percent: null, currency: "EUR", gap_code: null, reason: null },
      { key: "uplift", label: "Uplift", unit: "percent", micros: null, percent: "61.5", currency: null, gap_code: null, reason: null },
    ],
    cascade: {
      aggregation: "phase",
      aggregation_reason: "The cascade is aggregated by PHASE, not by rule: the mart carries five phase columns and one list of applied rule identifiers per row, and no relation of rule to contributed amount. Each phase names the levels that laid it; an amount per rule needs a relation that does not exist yet.",
      applied_rule_count: 5,
      steps: [
        {
          key: "platform_fee", label: "Platform fee", category: "PLATFORM_FEE",
          micros: 493826800, gap_code: null, reason: null,
          levels: [
            { kind: "project", label: "Project", covers: "Every Datastream of this Project.", rule_count: 1 },
            { kind: "datastream", label: "Datastream", covers: "This Datastream alone.", rule_count: 1 },
          ],
          unresolved_rule_count: 0, unresolved_reason: null,
        },
        {
          key: "regulatory_tax", label: "Regulatory tax", category: "REGULATORY_TAX",
          micros: 0, gap_code: null, reason: null,
          levels: [], unresolved_rule_count: 0, unresolved_reason: null,
        },
        {
          key: "wht_gross_up", label: "Withholding gross-up", category: "WHT_GROSS_UP",
          micros: 2265793553, gap_code: null, reason: null,
          levels: [{ kind: "project", label: "Project", covers: "Every Datastream of this Project.", rule_count: 1 }],
          unresolved_rule_count: 0, unresolved_reason: null,
        },
        {
          key: "agency_fee", label: "Agency fee", category: "AGENCY_FEE",
          micros: 1510529035, gap_code: null, reason: null,
          levels: [{ kind: "project", label: "Project", covers: "Every Datastream of this Project.", rule_count: 1 }],
          unresolved_rule_count: 0, unresolved_reason: null,
        },
        {
          key: "sales_tax", label: "Sales tax", category: "SALES_TAX",
          micros: 3323163878, gap_code: null, reason: null,
          levels: [{ kind: "project", label: "Project", covers: "Every Datastream of this Project.", rule_count: 1 }],
          unresolved_rule_count: 0, unresolved_reason: null,
        },
      ],
    },
    levels: {
      rendered: [
        { kind: "project", label: "Project", covers: "Every Datastream of this Project." },
        { kind: "plan_version", label: "Plan version", covers: "One published version of a media plan. It is a version of a plan, not a storey of configuration." },
        { kind: "datastream", label: "Datastream", covers: "This Datastream alone." },
      ],
      absent: [
        { name: "Organisation", reason: "There is no organisation level: `scope_kind` admits project, plan version and Datastream, and nothing else. Opening one is a migration." },
        { name: "Source category", reason: "A source category exists as data -- eight closed values, declared by all 39 connectors -- but not as a level a rule can be laid at, because it is a property of the connector and not of the Datastream." },
      ],
    },
    refused_rules: {
      state: "available",
      reason: null,
      rules: [
        {
          rule_key: "platform_fee_marketplace",
          code: "source_type_out_of_scope",
          reason: "scoped to MARKETPLACE; this Datastream is ANALYTICS_PRODUCT",
        },
      ],
    },
    governance_owner_reference: {
      surface: "project", workspace: "governance", section: "controls-quality",
      object_type: "rule-set", object_id: null, tab: "rule-sets", action: null,
      version_id: null, evidence_id: null, global_surface: null, global_section: null,
    },
  },
  // THE SECOND CONDITIONAL TAB (story 61.1), IN THE STATE THIS DATASTREAM CAN
  // REALLY REACH — and that is the whole point of the fixture.
  //
  // This sandbox's Datastream is Google Search Console, one of the 37 connectors
  // of 39 whose manifest declares NO placement dimension (`project-settings.md`,
  // ratified: "GA4 and Search Console expose no placement identifier"). So
  // `placement_dimension.declared` is `false` here, the attach control is absent,
  // and the plan's lines all read `Nothing observed`. Handing this Datastream a
  // cm360 placement to make the three-level table appear would be a capture of a
  // screen the product cannot produce — the rule written at the top of the
  // sandbox itself.
  placements: PLACEMENTS,
};

/** The bounded masked sample, shaped like `read_datastream_sample` returns it.
 *  Registered so the Data tab's whole reason to exist can be LOOKED AT: before
 *  it, the server answered "unavailable" unconditionally and no capture could
 *  ever show a row. */
export const SAMPLE = {
  project_id: PROJECT_ID,
  datastream_id: DATASTREAM_ID,
  stage: "published",
  served_stage: "published",
  stage_note: null,
  sample_watermark: "2026-08-02",
  masked_fields: ["query"],
  masked_value_count: 12,
  days: [
    {
      date: "2026-08-02",
      sampled_row_count: 5,
      rejection_count: 0,
      field_count: 5,
      rows: [
        { date: "2026-08-02", page: "/pricing", country: "fr", query: "***", clicks: 128 },
        { date: "2026-08-02", page: "/product", country: "fr", query: "***", clicks: 96 },
      ],
    },
    {
      date: "2026-08-01",
      sampled_row_count: 4,
      rejection_count: 2,
      field_count: 5,
      rows: [
        { date: "2026-08-01", page: "/pricing", country: "be", query: "***", clicks: 74 },
      ],
    },
  ],
};

/** The bounded CSV header the export dialog reads before it offers a download. */
export const EXPORT_COLUMNS = {
  project_id: PROJECT_ID,
  datastream_id: DATASTREAM_ID,
  columns: ["date", "page", "country", "query", "clicks", "impressions"],
  masked_columns: ["query"],
};

/** THE DAYS, in the ledger's own six words. Added with story 58.9, which mounts
 *  `CoverageStrip` on `Overview` as well: without a fixture the one element Jean
 *  named as correct rendered its unavailable state on the screen that exists to
 *  be looked at. `empty` and `never_fetched` are both present on purpose — they
 *  look alike on a chart and mean opposite things. */
export const LEDGER = {
  ledger: ["ok", "ok", "partial", "empty", "failed", "never_fetched", "running"].map(
    (status, index) => ({
      date: `2026-07-${String(26 + index).padStart(2, "0")}`,
      status,
      row_count: status === "ok" ? 18420 : status === "partial" ? 4100 : null,
      row_count_reason: status === "empty" ? "The provider returned no row for this day" : null,
    }),
  ),
};

/** THE SHAPE THE ROUTE REALLY ANSWERS (`admin_api.py`, the schedule branch), not
 *  the one this harness invented: `schedule_mode` / `date_window_days` are the
 *  COLUMN names, and `SchedulePanel` reads `cadence` / `window_days`, so the
 *  panel rendered every field empty on a fixture that looked plausible. */
export const SCHEDULE = {
  cadence: "nightly",
  enabled: true,
  lifecycle_state: "active",
  // Derived by `schedule_mcp._run_state` from the pair above plus `archived_at`;
  // the panel refuses to guess it from `enabled` alone.
  run_state: "running",
  archived: false,
  window_days: 7,
  window_offset_days: 1,
  window_source: "explicit",
  arrival_hour_local: 6,
  arrival_hour_source: "datastream",
  timezone: "Europe/Paris",
  timezone_source: "project_preference",
  on_failure: "retry_at_next_hour",
  retry_count: 0,
  next_run_at: "2026-08-03T02:00:00Z",
  last_run_at: "2026-08-02T02:04:00Z",
  never_ran: false,
};
