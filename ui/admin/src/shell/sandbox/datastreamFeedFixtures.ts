/**
 * The Datastream a file is PUSHED to — `source_kind = 'managed_feed'`, and the
 * mode of 4 of the 6 live Datastreams.
 *
 * IT HAD NEVER BEEN RENDERED. The sandbox hard-coded `mode: "connector_pull"`
 * with no switch, so every capture this repository holds is of a connector pull.
 * Amendment 7 of the 2026-08-11 review then took the whole connector pull axis
 * away from a file source — no report profile, no connector relation, no
 * collection window — and lot B1 put `DatastreamLandedFile` in its place. Both
 * changed a screen nobody could look at.
 *
 * WHAT MAKES IT A FILE SOURCE, ON THE WIRE:
 *
 *   * `identity.mode` is `managed_feed`, and it is the ONLY field that carries
 *     that word. `identity.connector` is `config.connector_name or module_name`
 *     and both are NULL on all 4 of the live feeds, which is why a guard reading
 *     `connector` never fired — amendment 7's own measurement, quoted in
 *     `DatastreamDailyBreakdown.tsx`;
 *   * `identity.delivery_channels` names the door: `email`, from
 *     `config.channels`, which is the same allowlist `inbound_ingest` refuses an
 *     unlisted channel against;
 *   * `plan.source.kind` is `managed_feed` with its own `managed_feed` block
 *     (`format` / `channel` / `source_ref`), which is what step 1 of the
 *     processing chain reads instead of `module · report`;
 *   * neither conditional capability is open, so the band is back to six tabs.
 *     That IS the reduced state, and it is the thing to look at.
 */
import {
  FEED_DATASTREAM_ID,
  FEED_MAPPING_VERSION,
  FEED_PLAN_VERSION,
  LINKS,
  PROJECT_ID,
} from "./datastreamSandboxScope";

export const FEED_HEADER = {
  schema: "datastream_workbench.header.v1",
  identity: {
    datastream_id: FEED_DATASTREAM_ID,
    project_id: PROJECT_ID,
    name: "Media plan — monthly file",
    mode: "managed_feed",
    data_role: "Plan",
    owner: "owner@example.com",
    module: null,
    connector: null,
    source_account_ref: null,
    declared_writer: "planning@example.com",
    business_domains: [{ id: "bd_EXAMPLE_2", name: "Media investment", slug: "media-investment" }],
    delivery_channels: ["email"],
  },
  axes: {
    lifecycle: "Active",
    configuration: "Ready",
    // The last terminal run of this feed PUBLISHED, and the schedule state is
    // known, so the registry's axis is `Healthy` — the branch of
    // `_operations_axis` the connector-pull fixture cannot show.
    operations: "Healthy",
    publication: "Current",
  },
  versions: {
    active_plan: FEED_PLAN_VERSION,
    active_plan_number: 2,
    active_mapping: FEED_MAPPING_VERSION,
    active_mapping_number: 2,
    proposed_plan: null,
    proposed_plan_number: null,
    proposed_mapping: null,
    proposed_mapping_number: null,
    ready_mapping_proposal: null,
  },
  runs: { latest: "run_EXAMPLE_FEED_7", latest_state: "Published" },
  operations_evidence: {
    // A feed that is DELIVERED to has no cadence of its own: it runs when a file
    // arrives. `mode` is NULL on those rows, and the screen says "No cadence set"
    // — a sentence that is finally true when it appears.
    mode: null,
    next_run_at: null,
    missed_run_count: 0,
    schedule_state_known: true,
    late_reasons: [],
  },
  // EXECUTION IDS, like `compose_header` sends them — see the same repair in
  // `datastreamPullFixtures.ts`. `last_known_good` is cleared as well: it is the
  // PRIOR published execution, this feed has exactly one run, and
  // `_publication_axis` returns the `Current` declared above only when there is
  // no last-known-good — with one set it would have read `Rollback available`.
  publications: {
    candidate: null,
    current: "run_EXAMPLE_FEED_7",
    last_known_good: null,
  },
  links: LINKS,
  primary_action: {
    kind: "prepare_change",
    label: "Prepare change",
    reason: "Active evidence is stable; changes require review.",
    tab: "processing",
  },
  // NEITHER CONDITIONAL TAB IS OPEN, and that is the point of this fixture: the
  // band drops `Cost` and `Placements`, which is the reduced state no capture has
  // ever carried. The four capabilities that open no tab keep their row on
  // `Overview` — a header carrying only the tab-opening ones would show two
  // modules out of six.
  capability_tabs: [
    { capability_key: "country", tab: null, availability: "optional", state: "disabled", open: false },
    { capability_key: "currency_fx", tab: null, availability: "always_present", state: "ready", open: false },
    { capability_key: "reporting_timezone", tab: null, availability: "always_present", state: "draft", open: false },
    { capability_key: "tax_fees", tab: "cost", availability: "optional", state: "disabled", open: false },
    { capability_key: "competitors", tab: null, availability: "optional", state: "disabled", open: false },
    { capability_key: "placement_mapping", tab: "placements", availability: "optional", state: "disabled", open: false },
  ],
};

/** The columns of the file this feed receives — the mapping's own, so they are
 *  the header of the day grid too. `market` is bound to nothing: a column the
 *  file carries and the mapping has not placed is a real state, and it is the
 *  line a person opens `Mapping` to repair. */
const FEED_FIELDS = [
  ["date", "dimension", "date", "date", "primary_date", null, "bound", true],
  ["campaign", "dimension", "string", "campaign", "dimension", null, "bound", true],
  ["market", "dimension", "string", null, null, null, "blocking", false],
  ["planned_spend", "measure", "decimal", "planned_spend", "metric", "sum", "bound", true],
  ["planned_impressions", "measure", "integer", "planned_impressions", "metric", "sum", "bound", true],
].map(([id, role, semantic, canonical, mdm, aggregation, status, included]) => ({
  field_id: id,
  source_identity: id,
  role,
  semantic_type: semantic,
  canonical_target: canonical,
  mdm_target: mdm,
  aggregation,
  sensitivity: "none",
  included,
  binding: { status, confidence: "high", evidence: "Import template" },
}));

/** The same pairs, in the shape `read_mapping_columns` publishes them — key
 *  columns first, `target_field` null where nothing binds. */
export const FEED_COLUMNS = [
  { source_field: "date", target_field: "date", is_key_column: true, binding_status: "bound", sensitivity: "none" },
  { source_field: "campaign", target_field: "campaign", is_key_column: true, binding_status: "bound", sensitivity: "none" },
  { source_field: "market", target_field: null, is_key_column: false, binding_status: "blocking", sensitivity: "none" },
  { source_field: "planned_impressions", target_field: "planned_impressions", is_key_column: false, binding_status: "bound", sensitivity: "none" },
  { source_field: "planned_spend", target_field: "planned_spend", is_key_column: false, binding_status: "bound", sensitivity: "none" },
];

const FEED_PLAN_PAYLOAD = {
  source: {
    kind: "managed_feed",
    module: null,
    report_id: null,
    // `datastream_activation.py` composes this block: the format, the channel
    // under its own name, and the content address of the file that was pinned.
    managed_feed: {
      format: "csv",
      channel: "inbound_email",
      source_ref: "sha256:4c1d90",
      template_ref: "tmpl_EXAMPLE_mediaplan",
    },
    selection: {
      selection_mode: "template",
      metrics: ["planned_spend", "planned_impressions"],
      dimensions: ["date", "campaign"],
      filters: [],
      grain: ["date", "campaign"],
    },
  },
  destination: { policy: "governed_full_grain" },
  // A delivered feed has no cadence: it runs when a file arrives.
  schedule: { mode: null, interval_minutes: null, timezone: "Europe/Paris" },
  historical: { start: "2026-01-01", end_exclusive: null },
};

const FEED_CAPABILITIES = {
  schema: "datastream_capability_projection.v1",
  capabilities: [
    {
      capability_key: "currency_fx",
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
      capability_key: "country",
      availability: "general",
      applicability: "applicable",
      coverage_state: "unavailable",
      reason: "The file carries a market column and nothing binds it, so no country can be read from it.",
      impact: { bound_fields: 0, expected_fields: 1 },
      blockers: [],
      exceptions: [],
      repair: null,
      governance_owner_reference: { object_type: "project-settings", id: PROJECT_ID },
    },
  ],
};

const FEED_STAGES = ["collected", "mapped", "processed", "published"].map((stage, index) => ({
  id: `stg_EXAMPLE_FEED_${index + 1}`,
  stage,
  phase_state: "complete",
  execution_id: "run_EXAMPLE_FEED_7",
  plan_version_id: FEED_PLAN_VERSION,
  mapping_version_id: FEED_MAPPING_VERSION,
  schema_hash: "sha256:2ab704",
  row_count: [412, 412, 412, 412][index],
  grain_evidence: { grain: ["date", "campaign"] },
  occurred_at: "2026-08-01T07:12:00Z",
  artifact_ref: `gs://example/${stage}/run_EXAMPLE_FEED_7`,
  profile_evidence: { null_rate: "0.00", distinct_campaigns: 18 },
  coverage_evidence: { expected_days: 31, observed_days: 31, missing_days: [] },
  safe_error: "",
}));

export const FEED_EVIDENCE: Record<string, Record<string, unknown>> = {
  overview: {
    header: FEED_HEADER,
    state: "available",
    schedule: { mode: null, date_window_days: null, next_run_at: null, timezone: "Europe/Paris" },
    latest_execution: {
      id: "run_EXAMPLE_FEED_7",
      state: "Published",
      started_at: "2026-08-01T07:10:00Z",
      row_count: 412,
    },
    run_success: { window_days: 30, terminal_count: 4, succeeded_count: 4 },
    published_at: "2026-08-01T07:14:00Z",
    published_row_count: 412,
    mapping_health: { version_id: FEED_MAPPING_VERSION, executable: true, blocking_count: 1 },
    stage_coverage: [{ stage: "published", evidence_count: 4, latest_at: "2026-08-01T07:12:00Z" }],
    downstream_count: 1,
    capabilities: FEED_CAPABILITIES,
  },
  data: {
    stages: FEED_STAGES,
    availability: { collected: "available", mapped: "available", processed: "available", published: "available" },
    // WHAT THE SERVER COMPOSES FOR THIS MODE, and nothing else — finding D-7 of
    // the visual review #69, repaired in `datastream_workbench.py` on 2026-08-12
    // and left drifting here. `read_tab` keys both fields on `module_name`, which
    // `create_datastream` sets to None for a `managed_feed` by construction: so
    // this Datastream earns `unavailable`, and the `managed_feed` branch of
    // `sample_reason` verbatim. The sentence it used to carry named story 47.5 —
    // a reference of this repository on a user's screen, held by a fixture after
    // the product had stopped sending it, which is how a capture goes on proving
    // a screen against a payload that no longer exists.
    sample_state: "unavailable",
    sample_reason:
      "No sample and no export can be drawn here — this Datastream receives a"
      + " file, and the rows it received are read in the last file that arrived,"
      + " above.",
    state: "available",
  },
  mapping: {
    versions: [
      {
        id: FEED_MAPPING_VERSION,
        version_number: 2,
        content_hash: "sha256:6f0a13",
        source_schema_hash: "sha256:2ab704",
        capability_fingerprint: null,
        plan_version_id: FEED_PLAN_VERSION,
        executable: true,
        blocking_count: 1,
        mapping_payload: { fields: FEED_FIELDS, grain: ["date", "campaign"], joint_grain: ["date"] },
        ossie_projection: { semantic_model: { metrics: [] } },
        created_by: "person_EXAMPLE",
        created_at: "2026-07-01T09:20:00Z",
      },
    ],
    active_version: FEED_MAPPING_VERSION,
    capabilities: FEED_CAPABILITIES,
  },
  processing: {
    plans: [
      {
        id: FEED_PLAN_VERSION,
        version_number: 2,
        content_hash: "sha256:9d4402",
        executable: true,
        normalized_payload: FEED_PLAN_PAYLOAD,
        validation_issues: [],
        created_at: "2026-07-01T09:20:00Z",
      },
    ],
    active_version: FEED_PLAN_VERSION,
    active_mapping_version: FEED_MAPPING_VERSION,
    capabilities: FEED_CAPABILITIES,
  },
  runs: {
    runs: [
      {
        id: "run_EXAMPLE_FEED_7",
        state: "Published",
        outcome: "published",
        started_at: "2026-08-01T07:10:00Z",
        finished_at: "2026-08-01T07:14:00Z",
        row_count: 412,
        plan_version_id: FEED_PLAN_VERSION,
        mapping_version_id: FEED_MAPPING_VERSION,
        interval_start: "2026-08-01",
        interval_end_exclusive: "2026-09-01",
      },
    ],
    timeline: [
      { stage: "import", state: "complete", occurred_at: "2026-08-01T07:11:00Z", row_count: 412 },
      { stage: "map", state: "complete", occurred_at: "2026-08-01T07:12:00Z", row_count: 412 },
      { stage: "process", state: "complete", occurred_at: "2026-08-01T07:13:00Z", row_count: 412 },
      { stage: "publish", state: "complete", occurred_at: "2026-08-01T07:14:00Z", row_count: 412 },
    ],
  },
  outputs: {
    outputs: [
      {
        id: "out_EXAMPLE_FEED_1",
        output_kind: "governed_full_grain",
        stable_name: "media_plan_monthly",
        version_id: "outv_EXAMPLE_FEED_2",
        execution_id: "run_EXAMPLE_FEED_7",
        publication_log_id: "publog_EXAMPLE_FEED_1",
        plan_version_id: FEED_PLAN_VERSION,
        mapping_version_id: FEED_MAPPING_VERSION,
        projection_version_ref: null,
        relation_ref: "example_dataset.media_plan_monthly",
        delivery_ref: null,
        schema_hash: "sha256:2ab704",
        grain_evidence: { grain: ["date", "campaign"] },
        evidence: { row_count: 412 },
        created_at: "2026-08-01T07:14:00Z",
      },
    ],
    pointers: FEED_HEADER.publications,
    // Same two keys as the pull fixture, same reason: `read_tab`'s `outputs`
    // branch always sends them, and a tab drawing its own fallback instead is a
    // screen nothing checks.
    raw_zone_policy: "managed_raw",
    retention_days: null,
    used_by: [],
  },
};

/**
 * `GET …/workbench/landed-file` — lot B1, and the panel that replaced the pull
 * axis on the `Data` tab of a file source.
 *
 * `read_landed_file` has EIGHT refusal reasons and they are not
 * interchangeable: « no file has arrived yet » names the door a file comes in
 * by, « the last one could not be read » is something to repair, and the panel
 * splits them by TONE — `EMPTY_REASONS` in `DatastreamLandedFile.tsx` draws the
 * first family as an `EmptyState` and everything else as an error. Each shape
 * below is one of those branches, with the server's own sentence
 * (`_MESSAGES`, `server/core/datastream_landed_file.py`) rather than a paraphrase.
 */
export type FileShape =
  | "read"
  | "no_file_yet"
  | "arrived_not_landed"
  | "arrival_refused"
  | "import_failed"
  | "relation_absent"
  | "warehouse_unavailable";

export const FILE_SHAPES: readonly FileShape[] = [
  "read",
  "no_file_yet",
  "arrived_not_landed",
  "arrival_refused",
  "import_failed",
  "relation_absent",
  "warehouse_unavailable",
];

const FILE_MESSAGES: Record<string, string> = {
  not_a_pushed_source:
    "This Datastream pulls its rows from a connector, so no file is ever pushed to it and "
    + "there is none to read back.",
  no_file_yet: "No file has arrived on this Datastream yet.",
  arrived_not_landed:
    "A file has arrived and is still on its way in: it has been received but no import has "
    + "written its rows yet.",
  arrival_refused:
    "The last file that arrived was refused before any row was written, so there is nothing "
    + "to read back from it.",
  import_not_landed: "The last import is still open: it has not written its rows yet.",
  import_failed: "The last file could not be imported, so no row of it landed.",
  landing_not_recorded:
    "The last import finished without recording where its rows landed, so they cannot be "
    + "read back.",
  relation_absent:
    "The relation the last import named does not exist in the warehouse, so the last file "
    + "could not be read.",
  warehouse_unavailable: "The last file could not be read: the warehouse did not answer.",
};

const DOORS = { channels: ["email"], upload_available: true };

const ARRIVAL = {
  raw_import_id: "rawimp_EXAMPLE_9",
  filename: "media-plan-2026-08.csv",
  state: "IMPORTED",
  size_bytes: 41984,
  arrived_at: "2026-08-01T07:09:40Z",
  error_code: null,
};

const IMPORT_RECORD = {
  ledger_id: "mfil_EXAMPLE_9",
  execution_id: "run_EXAMPLE_FEED_7",
  feed_format: "csv",
  filename: "media-plan-2026-08.csv",
  outcome: "published",
  row_count: 412,
  rejected_row_count: 3,
  error_code: null,
  observed_at: "2026-08-01T07:09:40Z",
  imported_at: "2026-08-01T07:11:00Z",
};

/** The base every shape starts from — every key present, always. A key that
 *  disappears is a key a screen cannot tell from a route that never sent it. */
function base(datastreamId: string) {
  return {
    project_id: PROJECT_ID,
    datastream_id: datastreamId,
    mode: "managed_feed",
    doors: DOORS,
    arrival: null as unknown,
    import: null as unknown,
    relation: null as string | null,
    columns: [] as string[],
    rows: null as unknown,
    row_count: null as number | null,
    truncated: false,
    masked_fields: [] as string[],
    reason: null as string | null,
    message: null as string | null,
  };
}

function refusal(datastreamId: string, reason: string, extra: Record<string, unknown> = {}) {
  return {
    ...base(datastreamId),
    ...extra,
    reason,
    message: FILE_MESSAGES[reason] ?? null,
  };
}

export function landedFileFixture(shape: FileShape, datastreamId: string) {
  switch (shape) {
    case "read":
      return {
        ...base(datastreamId),
        arrival: ARRIVAL,
        import: IMPORT_RECORD,
        relation: "main.managed_feed_ds_example_feed",
        // `market` is masked: nothing in the mapping classifies it, and the
        // policy of this reading is that an unclassified column is not an
        // allowed one. The three the mapping declares `none` are shown.
        columns: ["date", "campaign", "market", "planned_spend", "planned_impressions"],
        rows: [
          { date: "2026-08-01", campaign: "Brand — always on", market: "[MASKED]", planned_spend: "12000.00", planned_impressions: 2400000 },
          { date: "2026-08-01", campaign: "Product launch", market: "[MASKED]", planned_spend: "8000.00", planned_impressions: 1500000 },
          { date: "2026-08-02", campaign: "Brand — always on", market: "[MASKED]", planned_spend: "12000.00", planned_impressions: 2400000 },
        ],
        row_count: 3,
        truncated: true,
        masked_fields: ["market"],
      };
    case "no_file_yet":
      // Nothing has ever arrived, so there is no arrival and no import to name —
      // and the panel says which DOOR one would come in by, from `doors`.
      return refusal(datastreamId, "no_file_yet");
    case "arrived_not_landed":
      return refusal(datastreamId, "arrived_not_landed", {
        arrival: { ...ARRIVAL, state: "RECEIVED", error_code: null },
      });
    case "arrival_refused":
      return refusal(datastreamId, "arrival_refused", {
        arrival: {
          ...ARRIVAL,
          state: "REJECTED",
          error_code: "sender_not_allowed",
        },
      });
    case "import_failed":
      return refusal(datastreamId, "import_failed", {
        arrival: ARRIVAL,
        import: {
          ...IMPORT_RECORD,
          outcome: "failed",
          // NEVER coalesced to 0: `row_count` is NULL until it is measured, and a
          // 0 here would read as a file that contained nothing.
          row_count: null,
          rejected_row_count: null,
          error_code: "column_missing_date",
          imported_at: "2026-08-01T07:11:00Z",
        },
      });
    case "relation_absent":
      return refusal(datastreamId, "relation_absent", {
        arrival: ARRIVAL,
        import: IMPORT_RECORD,
        relation: "main.managed_feed_ds_example_feed",
      });
    case "warehouse_unavailable":
      return refusal(datastreamId, "warehouse_unavailable", {
        arrival: ARRIVAL,
        import: IMPORT_RECORD,
        relation: "main.managed_feed_ds_example_feed",
      });
  }
}

/** What a CONNECTOR PULL gets from the same address. The panel is not mounted for
 *  it — `WorkbenchDataPage` guards on `managed_feed` alone — but the route
 *  answers, and a sandbox that answered something else would teach the wrong
 *  thing to whoever reads it next. */
export function notAPushedSource(datastreamId: string) {
  return {
    ...refusal(datastreamId, "not_a_pushed_source"),
    mode: "connector_pull",
    doors: { channels: [], upload_available: true },
  };
}
