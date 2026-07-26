/**
 * Story 12.13 — pure unit tests for the source-agnostic wizard logic:
 * intent building per source kind, drift detection (preview obsolescence), and
 * activation blocking reasons. No DOM, no network.
 */
import {
  activationBlockingReasons,
  buildIntent,
  currentIntentSignature,
  deriveClassifiedFields,
  isPreviewObsolete,
} from "../datastreams/wizard/wizardLogic";
import { emptyDraft, type ValidationPlan, type WizardDraft } from "../datastreams/wizard/wizardTypes";

function connectorDraft(): WizardDraft {
  return {
    ...emptyDraft(),
    name: "F",
    sourceKind: "connector_pull",
    connectionRefId: "cref-1",
    reportId: "campaign_daily",
    selectedMetrics: ["spend"],
    selectedDimensions: ["date"],
    grain: ["date"],
  };
}

function validPlan(intentHash: string): ValidationPlan {
  return {
    plan_version_id: "pending:ds-1",
    mapping_version_id: null,
    intent_content_hash: intentHash,
    capability_contract_version: "1",
    capability_fingerprint: "cap-1",
    full_grain: ["date"],
    interval: null,
    timezone: "Europe/Paris",
    safe_kpi_projection: [{ name: "spend" }],
    dq: null,
    rejected_count: null,
    classified_fields: [],
    blocking_issues: [],
  };
}

/** Narrow helper: the intent's `source` object as a record. */
function sourceOf(intent: Record<string, unknown>): Record<string, unknown> {
  return intent.source as Record<string, unknown>;
}

// ---------------------------------------------------------------------------
// C1 — the intent-shape regression test. The server reads the source kind from
// INSIDE `source` (source["kind"] in datastream_intents.py / flows.py) and the
// schema (datastream-intent.schema.json) is additionalProperties:false, so a
// top-level `source_kind` would be REJECTED. This test asserts `source.kind` is
// set for ALL THREE source families and that NO top-level `source_kind` leaks —
// exactly the mismatch the previous fetch mock (returning {id} for any body)
// hid. If buildIntent regresses to top-level source_kind, this test fails.
// ---------------------------------------------------------------------------
it("puts kind INSIDE source for all three families (C1 — server reads source.kind)", () => {
  const conn = buildIntent(connectorDraft());
  expect(sourceOf(conn).kind).toBe("connector_pull");
  expect(sourceOf(conn).writer_kind).toBe("toorow");
  expect(conn.source_kind).toBeUndefined(); // no top-level source_kind

  const bq = buildIntent({ ...emptyDraft(), sourceKind: "external_bq", bigqueryTable: "p.d.t" });
  expect(sourceOf(bq).kind).toBe("external_bq");
  expect(sourceOf(bq).writer_kind).toBe("external");
  expect(bq.source_kind).toBeUndefined();

  const feed = buildIntent({
    ...emptyDraft(),
    sourceKind: "managed_feed",
    managedFeedFormat: "google_sheets",
    spreadsheetId: "1B",
    sheetRange: "A:F",
  });
  expect(sourceOf(feed).kind).toBe("managed_feed");
  expect(sourceOf(feed).writer_kind).toBe("toorow");
  expect(feed.source_kind).toBeUndefined();
});

it("derives the destination policy per source kind (managed_raw vs external_read_only)", () => {
  expect((buildIntent(connectorDraft()).destination as Record<string, unknown>).policy).toBe(
    "managed_raw",
  );
  const bq = buildIntent({ ...emptyDraft(), sourceKind: "external_bq", bigqueryTable: "p.d.t" });
  expect((bq.destination as Record<string, unknown>).policy).toBe("external_read_only");
  const feed = buildIntent({
    ...emptyDraft(),
    sourceKind: "managed_feed",
    managedFeedFormat: "csv",
  });
  expect((feed.destination as Record<string, unknown>).policy).toBe("managed_raw");
});

it("emits ONLY schema-valid top-level keys and a full schedule/selection (schema is additionalProperties:false)", () => {
  const conn = buildIntent(connectorDraft());
  // Exactly the schema's required top-level keys, nothing else (no timezone leak).
  expect(Object.keys(conn).sort()).toEqual(
    ["contract_version", "destination", "historical", "schedule", "source"].sort(),
  );
  expect(conn.contract_version).toBe("1");

  // schedule carries the required sub-objects the schema enforces.
  const schedule = conn.schedule as Record<string, unknown>;
  expect(schedule.mode).toBe("daily");
  expect(schedule.interval_minutes).toBe(1440); // daily => 1440 (schema const)
  expect(schedule.timezone).toBe("Europe/Paris");
  expect(schedule.watermark).toBeDefined();
  expect(schedule.late_arrival).toBeDefined();
  expect(schedule.retry).toBeDefined();
  expect(schedule.missed_run).toBeDefined();

  // historical is present (schema-required).
  expect(conn.historical).toEqual({ start: null, end_exclusive: null });

  // connector source carries connection_ref_id + report_id + a full selection,
  // and NONE of the old non-schema keys (metrics/dimensions/grain on source).
  const src = sourceOf(conn);
  expect(src.connection_ref_id).toBe("cref-1");
  expect(src.report_id).toBe("campaign_daily");
  expect(src.metrics).toBeUndefined();
  expect(src.dimensions).toBeUndefined();
  const selection = src.selection as Record<string, unknown>;
  expect(selection.selection_mode).toBe("subset");
  expect(selection.metrics).toEqual(["spend"]);
  expect(selection.dimensions).toEqual(["date"]);
  expect(selection.grain).toEqual(["date"]);
  expect(selection.filters).toEqual([]);

  // destination carries ONLY policy (no dataset leak under additionalProperties:false).
  expect(Object.keys(conn.destination as Record<string, unknown>)).toEqual(["policy"]);
});

it("maps cadence to schedule.mode + interval_minutes the server persists (H1)", () => {
  const daily = buildIntent(connectorDraft()).schedule as Record<string, unknown>;
  expect(daily.mode).toBe("daily");
  expect(daily.interval_minutes).toBe(1440);

  const manualDraft = connectorDraft();
  manualDraft.schedule = { ...manualDraft.schedule, cadence_mode: "manual" };
  const manual = buildIntent(manualDraft).schedule as Record<string, unknown>;
  expect(manual.mode).toBe("manual");
  expect(manual.interval_minutes).toBeNull(); // schema: manual => interval_minutes null

  const hourlyDraft = connectorDraft();
  hourlyDraft.schedule = { ...hourlyDraft.schedule, cadence_mode: "hourly", allow_hourly: true };
  const hourly = buildIntent(hourlyDraft).schedule as Record<string, unknown>;
  expect(hourly.mode).toBe("hourly");
  expect(hourly.interval_minutes).toBe(60); // schema: hourly => 60..1440 multiple of 60
});

it("builds external_object from a fully-qualified BigQuery reference", () => {
  const bq = buildIntent({
    ...emptyDraft(),
    sourceKind: "external_bq",
    bigqueryTable: "proj.dset.tbl",
  });
  const ext = sourceOf(bq).external_object as Record<string, unknown>;
  expect(ext).toMatchObject({
    project: "proj",
    dataset: "dset",
    object: "tbl",
    writer_identity: "proj",
  });
  // external_bq must NOT carry connector/managed keys (schema `not` clauses).
  expect(sourceOf(bq).connection_ref_id).toBeUndefined();
  expect(sourceOf(bq).report_id).toBeUndefined();
  expect(sourceOf(bq).managed_feed).toBeUndefined();
});

it("builds managed_feed with format + optional source_ref, no external/report keys", () => {
  const sheets = buildIntent({
    ...emptyDraft(),
    sourceKind: "managed_feed",
    managedFeedFormat: "google_sheets",
    spreadsheetId: "sheet-123",
    sheetRange: "A:F",
  });
  const mf = sourceOf(sheets).managed_feed as Record<string, unknown>;
  expect(mf.format).toBe("google_sheets");
  expect(mf.source_ref).toBe("sheet-123");
  expect(sourceOf(sheets).external_object).toBeUndefined();
  expect(sourceOf(sheets).report_id).toBeUndefined();

  // CSV without a preview => no source_ref key at all (rather than null).
  const csv = buildIntent({
    ...emptyDraft(),
    sourceKind: "managed_feed",
    managedFeedFormat: "csv",
  });
  const csvMf = sourceOf(csv).managed_feed as Record<string, unknown>;
  expect(csvMf.format).toBe("csv");
  expect("source_ref" in csvMf).toBe(false);
});

it("flags the preview obsolete when the schema signature drifts after validation", () => {
  const draft = connectorDraft();
  const signature = currentIntentSignature(draft);
  const validated: WizardDraft = {
    ...draft,
    plan: validPlan(signature),
    validatedIntentSignature: signature,
  };
  expect(isPreviewObsolete(validated)).toBe(false);

  // Underlying selection drifts while the OLD validated hash is retained.
  const drifted: WizardDraft = { ...validated, selectedMetrics: ["spend", "clicks"] };
  expect(isPreviewObsolete(drifted)).toBe(true);
  expect(activationBlockingReasons(drifted, drifted.plan)).toEqual(
    expect.arrayContaining([expect.stringMatching(/changed after validation/)]),
  );
});

it("blocks activation until a plan exists and has no blocking issues", () => {
  const draft = connectorDraft();
  expect(activationBlockingReasons(draft, null)).toEqual([
    expect.stringMatching(/Validate the plan/),
  ]);

  const sig = currentIntentSignature(draft);
  const withIssues: ValidationPlan = {
    ...validPlan(sig),
    blocking_issues: [{ code: "no_metrics", message: "x" }],
  };
  const d2: WizardDraft = { ...draft, plan: withIssues, validatedIntentSignature: sig };
  expect(activationBlockingReasons(d2, withIssues)).toEqual([
    expect.stringMatching(/blocking validation issue/),
  ]);

  const clean = validPlan(sig);
  const d3: WizardDraft = { ...draft, plan: clean, validatedIntentSignature: sig };
  expect(activationBlockingReasons(d3, clean)).toEqual([]);
});

it("derives source-agnostic classified fields from the connector catalog", () => {
  const draft = connectorDraft();
  const caps = {
    contract_version: "1",
    project_id: "p",
    connection_ref_id: "cref-1",
    module: { name: "m", display_name: "M", module_kind: "kpi" },
    fields: [
      {
        field_id: "spend",
        source_field: "spend",
        kind: "metric",
        physical_type: "number",
        description: "",
        semantic_hints: ["cost"],
        canonical_target: "media_spend",
        aggregation: "sum",
        non_additive: false,
      },
      {
        field_id: "date",
        source_field: "date",
        kind: "dimension",
        physical_type: "date",
        description: "",
        semantic_hints: ["date"],
        canonical_target: "date",
        aggregation: null,
        non_additive: false,
      },
    ],
    reports: [],
  };
  const classified = deriveClassifiedFields(draft, caps);
  expect(classified.map((f) => f.field_id).sort()).toEqual(["date", "spend"]);
  const spend = classified.find((f) => f.field_id === "spend")!;
  expect(spend.semantic_role).toBe("metric");
  expect(spend.mdm_binding).toBe("media_spend");
  // M1 — no fabricated precision: local confidence stays null even when a
  // canonical target exists; the server profiler owns the real score.
  expect(spend.mdm_confidence).toBeNull();
});
