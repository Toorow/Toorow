import type { RenderInput } from "../contracts";
import { FORMATTER_VERSION, RUNTIME_BUILD, THEME_VERSION } from "../buildInfo";
import { specDocument } from "./fixtures";

export const WATERFALL_FIELD_BINDINGS = {
  datum_key: "wf_datum_key",
  sequence: "wf_sequence",
  waterfall_role: "wf_waterfall_role",
  component: "wf_component",
  label: "wf_label",
  value_micros: "wf_value_micros",
  start_total_micros: "wf_start_total_micros",
  running_total_micros: "wf_running_total_micros",
  currency: "wf_currency",
  tax_basis: "wf_tax_basis",
  is_complete: "wf_is_complete",
  covered_row_count: "wf_covered_row_count",
  total_row_count: "wf_total_row_count",
  gap_codes: "wf_gap_codes",
  evidence_key: "wf_evidence_key",
} as const;

export const WATERFALL_ROWS = [
  ["net_media", "Net media", "base", 1_000_000, 0, 1_000_000, "HT"],
  ["platform_fee", "Platform fees", "delta", 100_000, 1_000_000, 1_100_000, "HT"],
  ["regulatory_tax", "Regulatory tax", "delta", 50_000, 1_100_000, 1_150_000, "HT"],
  [
    "withholding_gross_up",
    "Withholding gross-up",
    "delta",
    25_000,
    1_150_000,
    1_175_000,
    "HT",
  ],
  ["agency_fee", "Agency fee", "delta", 75_000, 1_175_000, 1_250_000, "HT"],
  ["total_cost_ht", "Total cost HT", "subtotal", 1_250_000, 1_250_000, 1_250_000, "HT"],
  ["vat_sales_tax", "VAT / sales tax", "delta", 250_000, 1_250_000, 1_500_000, "TTC"],
  ["invoice_ttc", "Invoice TTC", "total", 1_500_000, 1_500_000, 1_500_000, "TTC"],
].map(([component, label, role, value, start, running, basis], index) => ({
  datum_key: `qr_waterfall:${component}`,
  sequence: index + 1,
  waterfall_role: role as string,
  component: component as string,
  label: label as string,
  value_micros: value as number,
  start_total_micros: start as number,
  running_total_micros: running as number,
  currency: "EUR",
  tax_basis: basis as string,
  is_complete: true,
  covered_row_count: 9,
  total_row_count: 10,
  gap_codes: [] as string[],
  evidence_key: `qr_waterfall:evidence:m${index + 1}`,
}));

export function waterfallInput(profile: RenderInput["profile"] = "console"): RenderInput {
  const fields = Object.entries(WATERFALL_FIELD_BINDINGS).map(([name, id]) => ({ id, name }));
  return {
    result: {
      result_id: "qr_waterfall",
      content_hash: "sha256:waterfall-example",
      outcome: "success",
      schema: { contract: "waterfall_v1", fields },
      rows: WATERFALL_ROWS,
      manifest: {
        result_shape: "waterfall_v1",
        field_bindings: WATERFALL_FIELD_BINDINGS,
        coverage: { covered_row_count: 9, total_row_count: 10 },
        pins: {
          tax_fee_rule_set_version_id: "grsv_example",
          project_configuration_version_id: "pcv_example",
        },
        datum_evidence: Object.fromEntries(
          WATERFALL_ROWS.map((row, index) => [
            row.evidence_key,
            {
              member_id: `m${index + 1}`,
              source_system: "dbt",
              source_field: row.component,
              pull_id: "pull_example",
            },
          ]),
        ),
      } as never,
      truncated: false,
      row_count: 8,
    },
    spec: {
      visualization_spec_version_id: "vsv_waterfall",
      spec_contract_version: "visualization-spec.v1",
      schema_version: 1,
      document: specDocument({
        family: "waterfall",
        bindings: {
          dimension: [
            "wf_sequence",
            "wf_waterfall_role",
            "wf_component",
            "wf_label",
            "wf_currency",
            "wf_tax_basis",
            "wf_is_complete",
          ],
          measure: [
            "wf_value_micros",
            "wf_start_total_micros",
            "wf_running_total_micros",
            "wf_covered_row_count",
            "wf_total_row_count",
          ],
          detail: ["wf_datum_key", "wf_gap_codes", "wf_evidence_key"],
        },
        formatting: {
          number_style: "currency",
          date_style: "auto",
          unit_source: "semantic_view",
        },
        evidence: { datum_fields: ["wf_evidence_key"], mark_binding: "datum" },
      }),
    },
    pins: {
      theme_version: THEME_VERSION,
      formatter_version: FORMATTER_VERSION,
      renderer_build: "waterfall/toorow-echarts-waterfall@1.0.0",
      runtime_build: RUNTIME_BUILD,
    },
    profile,
  };
}
