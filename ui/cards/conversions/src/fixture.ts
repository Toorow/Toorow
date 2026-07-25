/**
 * Standalone dev/test fixture for the conversions card (Story 9.4).
 * Realistic cross-source GA4+Meta envelope:
 *   kpi_row(conversions,cost) + donut(by source) + gauge(CPA vs target) +
 *   table(source,conversions,cost,cpa) + comment.
 */

import type { CardEnvelope } from "@toorow/card-shell";

export const FIXTURE_ENVELOPE: CardEnvelope = {
  schema_version: "1",
  meta: {
    freshness: { last_pull: "2026-07-13T08:00:00Z", cadence_hours: 24, stale_since: null },
    provenance: {
      source_system: "google-analytics",
      source_field: "fact_daily_kpi",
      pull_id: "pull_CV_FIXTURE0000000000000",
      pull_ids: ["pull_CV_FIXTURE0000000000000", "pull_CV_META0000000000000"],
    },
    alerts: [],
    as_of: null,
    project_id: "default",
    card_selection: {
      chosen: "conversions",
      mode: "suggested",
      answers_question: "D'où viennent mes conversions et à quel coût ?",
      alternatives: [],
    },
    context_events: [],
  },
  data: {
    card_id: "conversions",
    card_type: "conversions",
    title: "Conversions",
    answers_question: "D'où viennent mes conversions et à quel coût ?",
    date_range: { start: "2026-06-14", end: "2026-07-13" },
    connectors: ["google-analytics", "meta-ads"],
    metrics: {},
    series: {},
    rendered_comment: "Contexte manquant pour cette période.",
    metric_definitions: {
      conversions: { definition: "Actions de conversion enregistrées.", unit: "conversions", direction: "up_good" },
      cost: { definition: "Coût total des campagnes publicitaires.", unit: "EUR", direction: "down_good" },
      cpa: { definition: "Coût par acquisition (SUM(coût)/SUM(conversions)).", unit: "EUR", direction: "down_good" },
    },
    composition: [
      {
        type: "kpi_row",
        binding: { metrics: ["conversions", "cost"] },
        data: {
          metrics: [
            { metric: "conversions", value: 320, delta: 48, delta_pct: 17.6, direction: "up_good" },
            { metric: "cost", value: 9600, delta: 320, delta_pct: 3.4, direction: "down_good" },
          ],
        },
      },
      {
        type: "donut",
        binding: { metrics: "conversions", dimensions: ["connector"] },
        title: "Par source",
        data: {
          total: 320,
          dimension: "connector",
          slices: [
            { label: "google-ads", value: 180, pct: 56.25 },
            { label: "meta-ads", value: 80, pct: 25.0 },
            { label: "organic", value: 40, pct: 12.5 },
            // "Autres" slice — server contract addition for the conversions donut.
            // Aggregates minor sources below the top-N threshold into a single residual slice.
            { label: "Autres", value: 20, pct: 6.25 },
          ],
        },
      },
      {
        type: "gauge",
        binding: { numerator: "cost", denominator: "conversions", direction: "down_good", unit: "EUR" },
        title: "CPA vs objectif",
        data: {
          value: 30.0,
          target: 50.0,
          target_source: "default",
          unit: "EUR",
          direction: "down_good",
          label: "CPA vs objectif",
        },
      },
      {
        type: "table",
        binding: { metrics: ["conversions", "cost", "cpa"], dimensions: ["connector"] },
        title: "Détail par source",
        data: {
          columns: [
            { key: "_dim", label: "Source", numeric: false, sortable: true },
            { key: "conversions", label: "Conversions", numeric: true, sortable: true },
            { key: "cost", label: "Coût (EUR)", numeric: true, sortable: true },
            { key: "cpa", label: "CPA (EUR)", numeric: true, sortable: true },
          ],
          rows: [
            { _dim: "google-ads", conversions: 180, cost: 5400, cpa: 30.0 },
            { _dim: "meta-ads", conversions: 80, cost: 2880, cpa: 36.0 },
            { _dim: "organic", conversions: 40, cost: 0, cpa: null },
            { _dim: "Autres", conversions: 20, cost: 320, cpa: 16.0 },
          ],
        },
      },
      {
        type: "comment",
        binding: {},
        data: {
          text:
            "Google Ads est le canal leader avec 200 conversions (62,5 %) à un CPA de 30 € — " +
            "sous l'objectif de 50 € (google-analytics:fact_daily_kpi, pull_CV_FIXTURE0000000000000).\n\n" +
            "Contexte manquant pour cette période.",
        },
      },
    ],
  },
};
