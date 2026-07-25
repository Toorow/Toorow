/**
 * Standalone dev/test fixture for the KPI card. Used when no structuredContent is
 * delivered by the host (vite preview / smoke render). Mirrors the server get_card
 * envelope shape (server/core/cards.py).
 */

import type { CardEnvelope } from "@toorow/card-shell";

function series(base: number, days = 30) {
  const out: { date: string; value: number }[] = [];
  const start = new Date("2026-06-14T00:00:00Z");
  for (let i = 0; i < days; i++) {
    const d = new Date(start.getTime() + i * 86400000);
    const iso = d.toISOString().slice(0, 10);
    const wobble = Math.sin(i / 3) * base * 0.12;
    out.push({ date: iso, value: Math.round(base + wobble + i * base * 0.01) });
  }
  return out;
}

export const FIXTURE_ENVELOPE: CardEnvelope = {
  schema_version: "1",
  meta: {
    freshness: { last_pull: "2026-07-13T08:00:00Z", cadence_hours: 24, stale_since: null },
    provenance: {
      source_system: "google-analytics",
      source_field: "fact_daily_kpi",
      pull_id: "pull_FIXTURE0000000000000000",
      pull_ids: ["pull_FIXTURE0000000000000000"],
    },
    alerts: [],
    as_of: null,
    project_id: "default",
    card_selection: {
      chosen: "kpi",
      mode: "suggested",
      answers_question:
        "Comment évoluent mes indicateurs clés sur la période (valeurs, variations, tendance) ?",
      alternatives: [],
    },
    context_events: [],
  },
  data: {
    card_id: "kpi",
    card_type: "kpi",
    title: "Synthèse KPI",
    answers_question:
      "Comment évoluent mes indicateurs clés sur la période (valeurs, variations, tendance) ?",
    date_range: { start: "2026-06-14", end: "2026-07-13" },
    connectors: ["google-analytics"],
    metrics: {
      sessions: {
        value: 42150,
        delta: 3820,
        delta_pct: 9.97,
        period: "période précédente",
        source_system: "google-analytics",
        source_field: "fact_daily_kpi",
        pull_id: "pull_FIXTURE0000000000000000",
      },
      active_users: {
        value: 31820,
        delta: -640,
        delta_pct: -1.97,
        period: "période précédente",
        source_system: "google-analytics",
        source_field: "fact_daily_kpi",
        pull_id: "pull_FIXTURE0000000000000000",
      },
      conversions: {
        value: 1284,
        delta: 210,
        delta_pct: 19.55,
        period: "période précédente",
        source_system: "google-analytics",
        source_field: "fact_daily_kpi",
        pull_id: "pull_FIXTURE0000000000000000",
      },
    },
    series: {
      sessions: series(1400),
      active_users: series(1060),
      conversions: series(43),
    },
    rendered_comment:
      "Sessions: 42 150 (+9,97 % vs période précédente) (google-analytics:fact_daily_kpi, pull_FIXTURE0000000000000000)\n" +
      "Conversions: 1 284 (+19,55 % vs période précédente) (google-analytics:fact_daily_kpi, pull_FIXTURE0000000000000000)\n\n" +
      "Contexte manquant pour cette période.",
    metric_definitions: {
      sessions: {
        definition: "Nombre de séances (visites) sur la période.",
        unit: "séances",
        direction: "up_good",
      },
      conversions: {
        definition: "Actions de conversion enregistrées.",
        unit: "conversions",
        direction: "up_good",
        caveats: "Dédupliquées par priorité de source.",
      },
    },
    llm_commentary_guidelines:
      "Mets en avant la progression des conversions et relie-la à la saisonnalité si pertinent.",
    // Story 9.2c: declared composition (mirrors server CARD_TEMPLATES[kpi].composition).
    composition: [
      { type: "kpi_row", binding: { metrics: "*" } },
      { type: "comment", binding: { metrics: "*" } },
    ],
  },
};
