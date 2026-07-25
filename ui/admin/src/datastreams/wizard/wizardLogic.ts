/**
 * Story 12.13 — pure wizard logic (source-agnostic), kept out of the shell so it
 * can be unit-tested directly:
 *   - derive the source-agnostic classified fields for the Classify stage;
 *   - build the versioned intent for Save draft / create (12.2 shape);
 *   - compute the schema hash observed *now* for drift detection;
 *   - compute the activation blocking reasons (blocking validation + drift +
 *     missing plan).
 *
 * These functions never call the network; the shell wires them to the API.
 */
import type {
  CapabilityField,
  ClassifiedField,
  SourceCapabilities,
  ValidationPlan,
  WizardDraft,
} from "./wizardTypes";

/** Map a capability field's kind to a coarse semantic role. */
function roleFromCapabilityField(f: CapabilityField): string {
  if (f.kind === "metric") return "metric";
  const hints = f.semantic_hints.map((h) => h.toLowerCase());
  if (hints.some((h) => h.includes("date") || h.includes("time"))) return "date";
  if (hints.some((h) => h.includes("id") || h.includes("key"))) return "identifier";
  return "dimension";
}

/**
 * Derive the classified fields shown in the Classify stage from whatever the
 * active source produced. Connector: selected metrics/dimensions resolved
 * against the capability catalog. Managed feed CSV/Excel: the preview columns.
 * External BQ: none until the source is registered (Phase B) — empty list.
 */
export function deriveClassifiedFields(
  draft: WizardDraft,
  capabilities: SourceCapabilities | null,
): ClassifiedField[] {
  if (draft.sourceKind === "connector_pull" && capabilities) {
    const byId = new Map(capabilities.fields.map((f) => [f.field_id, f]));
    const selected = [...draft.selectedMetrics, ...draft.selectedDimensions];
    return selected
      .map((id) => byId.get(id))
      .filter((f): f is CapabilityField => Boolean(f))
      .map((f) => ({
        field_id: f.field_id,
        source_field: f.source_field,
        semantic_role: roleFromCapabilityField(f),
        physical_type: f.physical_type,
        mdm_binding: f.canonical_target,
        // The server profiler (12.3) owns the real MDM confidence. We do NOT
        // fabricate a percentage here: a catalog canonical target is a strong
        // signal, but confidence stays "à confirmer" (null) until the server
        // validation/activation applies it. See ClassifyStage degradation note.
        mdm_confidence: null,
        sensitive_state: f.semantic_hints.some((h) =>
          h.toLowerCase().includes("pii"),
        )
          ? "detected"
          : "none",
        sample_values: [],
      }));
  }

  if (draft.sourceKind === "managed_feed" && draft.importPreview) {
    return draft.importPreview.columns.map((c) => {
      const samples =
        c.sample_values ??
        draft.importPreview!.preview_rows
          .map((row) => row[c.name])
          .filter((v) => v != null)
          .slice(0, 3)
          .map((v) => String(v));
      const isDate = /date|jour|day|time/i.test(c.name);
      return {
        field_id: c.name,
        source_field: c.name,
        semantic_role: isDate ? "date" : "dimension",
        physical_type: c.physical_type,
        mdm_binding: null,
        mdm_confidence: null,
        sensitive_state: "none",
        sample_values: samples,
      };
    });
  }

  return [];
}

/**
 * The schema hash observed for the CURRENT source configuration. Drift is
 * detected by comparing this against the hash captured at validation time. For
 * connector + external BQ we key on the selected shape; for managed feed we use
 * the preview's content hash directly.
 */
export function currentSchemaSignature(draft: WizardDraft): string {
  if (draft.sourceKind === "managed_feed" && draft.importPreview) {
    return draft.importPreview.content_hash;
  }
  if (draft.sourceKind === "connector_pull") {
    return [
      draft.connectionRefId ?? "",
      draft.reportId ?? "",
      [...draft.selectedMetrics].sort().join(","),
      [...draft.selectedDimensions].sort().join(","),
      [...draft.grain].join(","),
    ].join("|");
  }
  if (draft.sourceKind === "external_bq") {
    return `bq:${draft.bigqueryTable ?? ""}`;
  }
  return "";
}

/** True when the source/schema drifted since the plan was validated (AC4). */
export function isPreviewObsolete(draft: WizardDraft): boolean {
  if (!draft.plan || draft.validatedSchemaHash == null) return false;
  return currentSchemaSignature(draft) !== draft.validatedSchemaHash;
}

/**
 * The product-facing cadence maps to the `schedule.mode` + `interval_minutes`
 * the server schema (`datastream-intent.schema.json`) enforces:
 *   manual -> interval_minutes MUST be null
 *   daily  -> interval_minutes MUST be 1440
 *   hourly -> interval_minutes in [60..1440], multiple of 60
 * The versioned write path (`save_datastream_intent`) derives the schedule
 * window from THIS object, so the cadence is honored server-side (H1).
 */
export function scheduleIntervalMinutes(mode: WizardDraft["schedule"]["cadence_mode"]): number | null {
  if (mode === "daily") return 1440;
  if (mode === "hourly") return 60;
  return null; // manual
}

/**
 * The default watermark/late-arrival/retry/missed-run policy. The schema marks
 * all four as REQUIRED sub-objects of `schedule`; the wizard does not yet expose
 * them (Phase B), so we send conservative, schema-valid defaults rather than
 * omitting them (which would 422). These are the same defaults the server would
 * pick and are safe for a first activation.
 */
function defaultSchedulePolicies(): Record<string, unknown> {
  return {
    watermark: { kind: "none", delay_minutes: 0 },
    late_arrival: { lookback_minutes: 0 },
    retry: { max_attempts: 3, initial_backoff_seconds: 60, max_backoff_seconds: 3600 },
    missed_run: { mode: "skip", max_catchup_windows: 1 },
  };
}

/** The full `historical` block (schema-required). Nulls => server default window. */
function buildHistorical(): Record<string, unknown> {
  return { start: null, end_exclusive: null };
}

/** The schema-valid `schedule` block for the chosen cadence + timezone. */
function buildSchedule(draft: WizardDraft): Record<string, unknown> {
  return {
    mode: draft.schedule.cadence_mode,
    interval_minutes: scheduleIntervalMinutes(draft.schedule.cadence_mode),
    timezone: draft.schedule.timezone,
    ...defaultSchedulePolicies(),
  };
}

/**
 * The schema-required `selection` block. `filters` is always present (empty
 * array is valid). For non-connector sources the wizard has no metric/dimension
 * split, so selection is empty until the server profiler resolves it.
 */
function buildSelection(draft: WizardDraft): Record<string, unknown> {
  if (draft.sourceKind === "connector_pull") {
    return {
      selection_mode: "subset",
      metrics: draft.selectedMetrics,
      dimensions: draft.selectedDimensions,
      grain: draft.grain,
      filters: [],
    };
  }
  return { selection_mode: "subset", metrics: [], dimensions: [], grain: [], filters: [] };
}

/**
 * Parse a fully-qualified BigQuery reference `project.dataset.object` into the
 * schema's `external_object`. `writer_identity` is required by the schema and is
 * NOT collected pre-registration (Phase B) — we derive it from the table's own
 * project so the draft is schema-valid; the real writer identity is bound when
 * the external source is registered (Story 12.7).
 */
function buildExternalObject(bigqueryTable: string | null): Record<string, unknown> {
  const parts = (bigqueryTable ?? "").split(".");
  const [project, dataset, object] = [parts[0] ?? "", parts[1] ?? "", parts[2] ?? ""];
  return {
    project: project || "unknown_project",
    dataset: dataset || "unknown_dataset",
    object: object || "unknown_object",
    // Placeholder until external-source registration binds the real identity.
    writer_identity: project || "unknown_project",
  };
}

/**
 * Build the versioned intent for POST /api/datastreams (12.2), shaped to VALIDATE
 * against `server/core/schemas/datastream-intent.schema.json` and consumed by
 * `save_datastream_intent` (which reads `source.kind` / `source.writer_kind`).
 *
 * CRITICAL (C1): `kind` lives INSIDE `source` — the server reads
 * `source["kind"]` (datastream_intents.py, flows.py). A top-level `source_kind`
 * is NOT part of the schema and, under `additionalProperties: false`, would be
 * rejected as `invalid_intent_schema`; likewise every non-schema key (metrics on
 * source, destination.dataset, top-level timezone) is dropped. The schema
 * requires exactly: contract_version, source, destination, historical, schedule.
 */
export function buildIntent(draft: WizardDraft): Record<string, unknown> {
  const base: Record<string, unknown> = {
    contract_version: "1",
    historical: buildHistorical(),
    schedule: buildSchedule(draft),
  };

  if (draft.sourceKind === "connector_pull") {
    return {
      ...base,
      source: {
        kind: "connector_pull",
        writer_kind: "toorow",
        connection_ref_id: draft.connectionRefId ?? "",
        report_id: draft.reportId ?? "",
        selection: buildSelection(draft),
      },
      destination: { policy: "managed_raw" },
    };
  }
  if (draft.sourceKind === "external_bq") {
    return {
      ...base,
      source: {
        kind: "external_bq",
        writer_kind: "external",
        external_object: buildExternalObject(draft.bigqueryTable),
        selection: buildSelection(draft),
      },
      destination: { policy: "external_read_only" },
    };
  }
  // managed_feed — the schema's managed_feed accepts a format + optional source_ref.
  const format = draft.managedFeedFormat ?? "csv";
  const sourceRef =
    format === "google_sheets"
      ? draft.spreadsheetId ?? undefined
      : draft.importPreview?.content_hash ?? undefined;
  return {
    ...base,
    source: {
      kind: "managed_feed",
      writer_kind: "toorow",
      managed_feed: {
        format,
        ...(sourceRef ? { source_ref: sourceRef } : {}),
      },
      selection: buildSelection(draft),
    },
    destination: { policy: "managed_raw" },
  };
}

/**
 * The reasons that BLOCK activation (empty => activation allowed). Covers the
 * AC1 rule (blocking validation disables activation) plus drift-obsolescence and
 * the "must validate first" precondition.
 */
export function activationBlockingReasons(
  draft: WizardDraft,
  plan: ValidationPlan | null,
): string[] {
  const reasons: string[] = [];
  if (!plan) {
    reasons.push("Le plan n’a pas encore été validé à l’étape Aperçu & valider.");
    return reasons;
  }
  if (plan.blocking_issues.length > 0) {
    reasons.push(
      `${plan.blocking_issues.length} problème(s) bloquant(s) de validation à corriger.`,
    );
  }
  if (isPreviewObsolete(draft)) {
    reasons.push(
      "La source ou le schéma a changé : revalidez le plan (l’aperçu est obsolète).",
    );
  }
  return reasons;
}
