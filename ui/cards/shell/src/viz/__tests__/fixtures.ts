/**
 * Test fixtures shaped EXACTLY like what the server returns.
 *
 * `result` mirrors `GET /api/projects/{id}/analyze/results/{result_id}/evidence`
 * (`server/core/query_specs_api.py`), whose payload is built at
 * `server/core/query_execution.py:600` -- so `schema.fields` carries `name` alone
 * and the manifest carries the provenance shape from `:340-355`.
 *
 * `spec.document` mirrors Story 50.4's GRAMMAR
 * (`server/core/visualization_specs.py:343`), every key present with its default,
 * because the runtime's validator is written against the persisted document, not
 * against a convenient subset.
 *
 * No real domain, no personal address, no production identifier (CLAUDE.md).
 */

import type { RenderInput, VizSpecDocument } from "../contracts";
import { FORMATTER_VERSION, RUNTIME_BUILD, THEME_VERSION } from "../buildInfo";

export function specDocument(overrides: Partial<VizSpecDocument> = {}): VizSpecDocument {
  return {
    spec_contract_version: "visualization-spec.v1",
    schema_version: 1,
    family: "bar",
    bindings: { dimension: ["channel"], measure: ["sessions"] },
    order: { source: "result" },
    top_n: null,
    axes: {
      x: { scale: "categorical", zero_baseline: true, tick_density: "normal" },
      y: { scale: "linear", zero_baseline: true, tick_density: "normal" },
    },
    legend: { position: "right", visible: true },
    formatting: { number_style: "auto", date_style: "auto", unit_source: "semantic_view" },
    color: { role: "categorical", semantic_direction: "higher_is_better" },
    thresholds: [],
    reference_lines: [],
    annotations: [],
    interactions: {
      hover: true,
      select: true,
      zoom: false,
      legend_toggle: true,
      local_filter: false,
    },
    evidence: { datum_fields: ["channel"], mark_binding: "datum" },
    responsive: { profiles: ["console"] },
    accessibility: { summary_source: "result_manifest", table_fallback: "required" },
    labels: { override: {} },
    ...overrides,
  };
}

export const RESULT_ROWS = [
  { channel: "organic", sessions: 1240, conversions: 31 },
  { channel: "paid", sessions: 880, conversions: 44 },
  { channel: "referral", sessions: 305, conversions: 9 },
];

export function renderInput(overrides: Partial<RenderInput> = {}): RenderInput {
  const document = overrides.spec?.document ?? specDocument();
  return {
    result: {
      result_id: "res_EXAMPLE_0001",
      content_hash: "sha256:examplehash0001",
      outcome: "success",
      schema: { fields: [{ name: "channel" }, { name: "sessions" }, { name: "conversions" }] },
      rows: RESULT_ROWS,
      manifest: {
        grain: "day",
        time_window: { start: "2026-07-01", end: "2026-07-31" },
        filters: ["country = FR"],
        comparison: "none",
        truncation: null,
        provenance: {
          source_system: "example_source",
          datastream_id: "ds_EXAMPLE",
          mapping_version_id: "mv_EXAMPLE",
          relation: "marts.example_relation",
          pull_id: "pull_EXAMPLE",
          publication_log_id: "pub_EXAMPLE",
          values: [
            {
              member_id: "channel",
              source_system: "example_source",
              source_field: "utm_medium",
              pull_id: "pull_EXAMPLE",
            },
            {
              member_id: "sessions",
              source_system: "example_source",
              source_field: "session_count",
              pull_id: "pull_EXAMPLE",
            },
          ],
        },
        freshness: { output_created_at: "2026-07-31T00:00:00+00:00" },
        dq_evaluation_ids: [],
      } as never,
      truncated: false,
      row_count: 3,
    },
    spec: {
      visualization_spec_version_id: "vsv_EXAMPLE_0001",
      spec_contract_version: "visualization-spec.v1",
      schema_version: 1,
      document,
      ...overrides.spec,
    },
    pins: {
      theme_version: THEME_VERSION,
      formatter_version: FORMATTER_VERSION,
      renderer_build: "bar/toorow-echarts-bar@1.0.0",
      runtime_build: RUNTIME_BUILD,
      ...overrides.pins,
    },
    profile: overrides.profile ?? "console",
    display: overrides.display,
    ...(overrides.result ? { result: overrides.result } : {}),
  };
}
