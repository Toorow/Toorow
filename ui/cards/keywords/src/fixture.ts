/**
 * Standalone dev/test fixture for the keywords card (Story 9.3).
 * Realistic GSC envelope: kpi_row(clicks,impressions) + bar(rank movers) +
 * table(query,clicks,impressions,avg_position) + comment.
 *
 * Exports:
 *   FIXTURE_ENVELOPE         — happy-path (30-day period)
 *   FIXTURE_ENVELOPE_STALE   — stale data badge scenario (F-10 ui)
 *   FIXTURE_ENVELOPE_PARTIAL — partial connector period (F-10 ui)
 */

import type { CardEnvelope } from "@toorow/card-shell";

export const FIXTURE_ENVELOPE: CardEnvelope = {
  schema_version: "1",
  meta: {
    freshness: { last_pull: "2026-07-13T08:00:00Z", cadence_hours: 24, stale_since: null },
    provenance: {
      source_system: "google-search-console",
      source_field: "fact_daily_kpi",
      pull_id: "pull_KW_FIXTURE0000000000000",
      pull_ids: ["pull_KW_FIXTURE0000000000000"],
    },
    alerts: [],
    as_of: null,
    project_id: "default",
    card_selection: {
      chosen: "keywords",
      mode: "suggested",
      answers_question: "Comment se comportent mes mots-clés sur la période ?",
      alternatives: [],
    },
    context_events: [],
  },
  data: {
    card_id: "keywords",
    card_type: "keywords",
    title: "Mots-clés",
    answers_question: "Comment se comportent mes mots-clés sur la période ?",
    date_range: { start: "2026-06-14", end: "2026-07-13" },
    connectors: ["google-search-console"],
    metrics: {},
    series: {},
    rendered_comment: "Contexte manquant pour cette période.",
    metric_definitions: {
      clicks: { definition: "Nombre de clics depuis les résultats de recherche.", unit: "clics", direction: "up_good" },
      impressions: { definition: "Nombre d'impressions dans les SERP.", unit: "impressions", direction: "up_good" },
      average_position: { definition: "Position moyenne pondérée par impressions.", unit: "position", direction: "down_good" },
    },
    composition: [
      {
        type: "kpi_row",
        binding: { metrics: ["clicks", "impressions"] },
        data: {
          metrics: [
            { metric: "clicks", value: 3842, delta: 312, delta_pct: 8.8, direction: "up_good" },
            { metric: "impressions", value: 94500, delta: -1200, delta_pct: -1.3, direction: "up_good" },
          ],
        },
      },
      {
        // Rank movers (queries that moved >=2 positions, signed delta).
        // Values are position deltas: positive = gained positions (better = up_good for clicks
        // but "down_good" for average_position). Here we use signed deltas: positive = improved
        // (lower position number = better rank). The bar chart renders movers sorted by magnitude.
        // NOTE: the server (cards.py _resolve_bar for keywords) currently ships top-N by clicks,
        // not movers. Until server aligns, this fixture provides the intended mover shape so the
        // UI is ready. Server-side contract mismatch logged in story notes.
        type: "bar",
        binding: { metrics: "average_position", dimensions: ["query"], direction: "down_good" },
        title: "Requêtes en mouvement (±pos.)",
        data: {
          orientation: "horizontal",
          dimension: "query",
          bars: [
            { label: "bottes de randonnée", value: 3.2, direction: "up" as const },
            { label: "chaussures trail femme", value: -2.5, direction: "down" as const },
            { label: "semelles orthopédiques sport", value: 2.1, direction: "up" as const },
            { label: "chaussettes de randonnée", value: -3.8, direction: "down" as const },
            { label: "imperméabilisation chaussures", value: 1.4, direction: "up" as const },
          ],
        },
      },
      {
        type: "table",
        binding: { metrics: ["clicks", "impressions", "average_position"], dimensions: ["query"] },
        title: "Détail requêtes",
        data: {
          columns: [
            { key: "_dim", label: "Requête", numeric: false, sortable: true },
            { key: "clicks", label: "Clics", numeric: true, sortable: true },
            { key: "impressions", label: "Impressions", numeric: true, sortable: true },
            { key: "average_position", label: "Position moy.", numeric: true, sortable: true },
          ],
          rows: [
            { _dim: "bottes de randonnée", clicks: 1200, impressions: 18000, average_position: 2.4 },
            { _dim: "chaussures trail femme", clicks: 850, impressions: 12000, average_position: 3.1 },
            { _dim: "semelles orthopédiques sport", clicks: 420, impressions: 8500, average_position: 5.8 },
            { _dim: "chaussettes de randonnée", clicks: 310, impressions: 6200, average_position: 4.2 },
            { _dim: "imperméabilisation chaussures", clicks: 240, impressions: 5100, average_position: 7.6 },
          ],
        },
      },
      {
        // Opportunités — queries with impressions but position > 10 (page 2+), server contract addition.
        // Columns: Requête / Impressions / Position moyenne.
        // Renders via the generic table path in CardComposition after the movers bar (order preserved).
        type: "table",
        binding: { metrics: ["impressions", "average_position"], dimensions: ["query"], filter: "position_gt_10" },
        title: "Opportunités",
        data: {
          columns: [
            // Keys aligned on the REAL server payload (cards.py _resolve_table_opportunities
            // emits `average_position`, never `avg_position`) — review-10-6 UI F-1 / AI-54.
            { key: "_dim",             label: "Requête",           numeric: false, sortable: true },
            { key: "impressions",      label: "Impressions",       numeric: true,  sortable: true },
            { key: "average_position", label: "Position moyenne",  numeric: true,  sortable: true },
          ],
          rows: [
            { _dim: "chaussures montagne imperméables", impressions: 4800, average_position: 14.2 },
            { _dim: "bâtons randonnée pliables",        impressions: 3200, average_position: 18.7 },
            { _dim: "sac à dos trekking 40L",           impressions: 2900, average_position: 11.5 },
            { _dim: "guêtres randonnée neige",          impressions: 2100, average_position: 22.3 },
            { _dim: "lampe frontale rechargeable",      impressions: 1850, average_position: 16.9 },
          ],
        },
      },
      {
        // Cannibalisation (Story 10.5) — queries split across >=2 pages read from the
        // JOINT query>page grain. Server contract (cards.py _resolve_table_cannibalisation):
        // columns ["Requête","Page","Part (%)","Position moy."], empty -> designed empty
        // table with empty_label. This fixture ships the DESIGNED EMPTY state (rows []) since
        // the seed has no cannibalising query; the block is ALWAYS present (never absent).
        type: "table",
        binding: { metrics: ["impressions", "average_position"], dimensions: ["query", "page"], cannibalisation: true },
        title: "Cannibalisation",
        data: {
          columns: ["Requête", "Page", "Part (%)", "Position moy."],
          rows: [],
          empty_label: "Aucune cannibalisation détectée",
        },
      },
      {
        type: "comment",
        binding: {},
        data: {
          text:
            "« bottes de randonnée » est la requête leader avec 1 200 clics (+8,8 % vs période précédente) " +
            "à la position 2,4 (google-search-console:fact_daily_kpi, pull_KW_FIXTURE0000000000000).\n\n" +
            "Contexte manquant pour cette période.",
        },
      },
    ],
  },
};

/**
 * FIXTURE_ENVELOPE_STALE — stale data scenario (F-10 ui).
 * last_pull > 48 h ago; stale_since set. Card must render freshness badge and
 * stale indicator from WidgetShell; body still renders with available data.
 */
export const FIXTURE_ENVELOPE_STALE: CardEnvelope = {
  ...FIXTURE_ENVELOPE,
  meta: {
    ...FIXTURE_ENVELOPE.meta,
    freshness: {
      last_pull: "2026-07-11T08:00:00Z",
      cadence_hours: 24,
      stale_since: "2026-07-12T08:00:00Z",
    },
    alerts: [{ code: "STALE_DATA", severity: "warning", message: "Données non mises à jour depuis 2 jours." }],
  },
};

/**
 * FIXTURE_ENVELOPE_PARTIAL — partial period scenario (F-10 ui).
 * Connector configured 3 days ago; only 3 rows of data, no prior period for delta.
 */
export const FIXTURE_ENVELOPE_PARTIAL: CardEnvelope = {
  ...FIXTURE_ENVELOPE,
  data: {
    ...FIXTURE_ENVELOPE.data,
    date_range: { start: "2026-07-11", end: "2026-07-13" },
    composition: (FIXTURE_ENVELOPE.data.composition ?? []).map((b) => {
      if (b.type === "kpi_row") {
        return {
          ...b,
          data: {
            metrics: [
              { metric: "clicks", value: 320, delta: null, delta_pct: null, direction: "up_good" as const },
              { metric: "impressions", value: 8400, delta: null, delta_pct: null, direction: "up_good" as const },
            ],
          },
        };
      }
      if (b.type === "bar") {
        return {
          ...b,
          data: {
            orientation: "horizontal" as const,
            dimension: "query",
            bars: [
              { label: "bottes de randonnée", value: 2.1, direction: "up" as const },
            ],
          },
        };
      }
      return b;
    }),
  },
};
