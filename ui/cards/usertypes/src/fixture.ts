/**
 * Standalone dev/test fixture for the usertypes card (Story 9.5 / 10.2).
 *
 * AI-54: this is a REAL get_card payload, NOT hand-authored. Captured on
 * 2026-07-18 (post review-epic-10 rollup fix: KPI = canonical single partition) from:
 *   get_card(template="usertypes", project_id="default",
 *            metrics=["sessions","active_users"],
 *            date_from="2026-06-17", date_to="2026-07-16")
 * against the DuckDB seeded by run_local_loop.py (story 10.1 user_type seed).
 *
 * Composition (real server order):
 *   kpi_row(sessions,active_users)
 *   + donut(user_type — Nouveaux/Fidèles/Indéterminés)  ← Story 10.2 LEAD block
 *   + donut(device_category)
 *   + bar(country)
 *   + table(device_category)
 *   + comment (cites the dominant user_type segment).
 */

import type { CardEnvelope } from "@toorow/card-shell";

export const FIXTURE_ENVELOPE: CardEnvelope = {
  schema_version: "1",
  meta: {
    freshness: { last_pull: "2026-07-18T00:44:07.525608Z", cadence_hours: 24, stale_since: null },
    provenance: {
      source_system: "google-analytics",
      source_field: "fact_daily_kpi",
      pull_id: "pull_01KXSAWDF50RBD1H3JX1YD1A7T",
      pull_ids: ["pull_01KXSAW8VQQQK21J01JNXJGEH8", "pull_01KXSAWCBAG1SEF5SAEXN83Z2A", "pull_01KXSAWDF50RBD1H3JX1YD1A7T"],
    },
    alerts: [],
    trace_id: null,
    as_of: null,
    context_events: [],
    card_selection: {
      chosen: "usertypes",
      mode: "explicit",
      answers_question: "Qui sont mes utilisateurs (Nouveaux vs fidèles, appareil, pays) ?",
      alternatives: [
        {
          id: "kpi",
          answers_question:
            "Comment évoluent mes indicateurs clés sur la période (valeurs, variations, tendance) ?",
        },
      ],
    },
    project_id: "default",
  },
  data: {
    card_id: "usertypes",
    card_type: "usertypes",
    title: "Types d'utilisateurs",
    answers_question: "Qui sont mes utilisateurs (Nouveaux vs fidèles, appareil, pays) ?",
    date_range: { start: "2026-06-17", end: "2026-07-16" },
    connectors: ["google-analytics"],
    metrics: {
      sessions: {
        value: 268981.0,
        delta: 47536.0,
        delta_pct: "+21%",
        period: "sem. préc.",
        source_system: "google-analytics",
        source_field: "sessions",
        pull_id: "pull_01KXSAWDF50RBD1H3JX1YD1A7T",
      },
      active_users: {
        value: 221102.0,
        delta: 38729.0,
        delta_pct: "+21%",
        period: "sem. préc.",
        source_system: "google-analytics",
        source_field: "active_users",
        pull_id: "pull_01KXSAWCBAG1SEF5SAEXN83Z2A",
      },
    },
    series: {},
    rendered_comment:
      "Type dominant : « Fidèles » (61.1 % des utilisateurs actifs) (connector:fact_daily_kpi, pull_01KXSAWDF50RBD1H3JX1YD1A7T)\n" +
      "Premier pays : « FR » (72 063 utilisateurs actifs) (connector:fact_daily_kpi, pull_01KXSAWDF50RBD1H3JX1YD1A7T)\n" +
      "Contexte manquant pour cette période.",
    metric_definitions: {
      active_users: { definition: "Utilisateurs actifs sur la période.", unit: "utilisateurs", direction: "up_good" },
      sessions: { definition: "Nombre de séances.", unit: "séances", direction: "up_good" },
    },
    composition: [
      {
        type: "kpi_row",
        binding: { metrics: "*" },
        data: {
          metrics: [
            { metric: "sessions", value: 268981.0, delta: 47536.0, delta_pct: 21.0, direction: null },
            { metric: "active_users", value: 221102.0, delta: 38729.0, delta_pct: 21.0, direction: null },
          ],
        },
      },
      {
        // Story 10.2 LEAD block: new/returning donut — user_type dimension.
        // FR labels applied server-side: new→Nouveaux, returning→Fidèles, unknown→Indéterminés.
        type: "donut",
        title: "Nouveaux vs fidèles",
        binding: { metrics: "active_users", dimensions: ["user_type"] },
        data: {
          total: 220065.0,
          dimension: "user_type",
          slices: [
            { label: "Fidèles", value: 134511.0, pct: 61.1 },
            { label: "Nouveaux", value: 81136.0, pct: 36.9 },
            { label: "Indéterminés", value: 4418.0, pct: 2.0 },
          ],
        },
      },
      {
        type: "donut",
        title: "Utilisateurs par appareil",
        binding: { metrics: "active_users", dimensions: ["device_category"] },
        data: {
          total: 221102.0,
          dimension: "device_category",
          slices: [
            { label: "desktop", value: 119951.0, pct: 54.3 },
            { label: "mobile", value: 81060.0, pct: 36.7 },
            { label: "tablet", value: 20091.0, pct: 9.1 },
          ],
        },
      },
      {
        type: "bar",
        title: "Utilisateurs par pays",
        binding: { metrics: "active_users", dimensions: ["country"] },
        data: {
          orientation: "horizontal",
          dimension: "country",
          bars: [
            { label: "FR", value: 72063.0 },
            { label: "DE", value: 47818.0 },
            { label: "GB", value: 40368.0 },
            { label: "US", value: 31765.0 },
            { label: "ES", value: 29088.0 },
          ],
        },
      },
      {
        type: "table",
        title: "Par appareil",
        binding: { metrics: ["active_users", "sessions"], dimensions: ["device_category"] },
        data: {
          columns: [
            { key: "_dim", label: "device_category", numeric: false },
            { key: "active_users", label: "active_users", numeric: true },
            { key: "sessions", label: "sessions", numeric: true },
          ],
          rows: [
            { _dim: "desktop", active_users: 119951.0, sessions: 146722.0 },
            { _dim: "mobile", active_users: 81060.0, sessions: 98023.0 },
            { _dim: "tablet", active_users: 20091.0, sessions: 24236.0 },
          ],
        },
      },
      {
        type: "comment",
        binding: { metrics: "*" },
        data: {
          text:
            "Type dominant : « Fidèles » (61.1 % des utilisateurs actifs) (connector:fact_daily_kpi, pull_01KXSAWDF50RBD1H3JX1YD1A7T)\n" +
            "Premier pays : « FR » (72 063 utilisateurs actifs) (connector:fact_daily_kpi, pull_01KXSAWDF50RBD1H3JX1YD1A7T)\n" +
            "Contexte manquant pour cette période.",
        },
      },
    ],
  },
};
