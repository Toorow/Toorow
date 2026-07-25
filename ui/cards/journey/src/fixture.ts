/**
 * Standalone dev/test fixture for the journey card (Story 9.6 + Story 10.4).
 * Realistic GA4 funnel envelope:
 *   funnel(sessions->active_users->conversions, overall_rate) + kpi_row
 *   + bar(Pages d'entrée, sessions by landing_page)
 *   + table(Pages les plus vues, screen_page_views by page, Part on top-N subtotal)
 *   + comment.
 *
 * v1 funnel = sessions -> active_users -> conversions stage rates.
 * Story 10.4: adds entry-pages bar and top-pages table after the kpi_row.
 * Exit pages are OUT OF SCOPE (absent from GA4 Data API v1 — see epic-10 plan).
 *
 * AI-54: the composition below is a REAL get_card payload (captured 2026-07-18, post review-epic-10 rollup fix: KPI/funnel = canonical single partition)
 * from get_card(template="journey", project_id="default",
 *   metrics=["sessions","conversions","screen_page_views"],
 *   date_from="2026-06-17", date_to="2026-07-16") against the DuckDB seeded by
 * run_local_loop.py (story 10.3 pages seed). The comment leads with the funnel
 * drop-off AND the top entry page (« / », 35 364 sessions). « Part » in the top-pages
 * table is a share of the top-N SUBTOTAL (all returned GA page views = 375 503), so
 * the displayed top rows sum to < 100 % — the tail beyond the display is real.
 *
 * Exports:
 *   FIXTURE_ENVELOPE         — happy-path (real payload)
 *   FIXTURE_ENVELOPE_STALE   — stale data scenario (F-10 ui)
 *   FIXTURE_ENVELOPE_PARTIAL — partial data / null overall_rate (F-10 ui)
 */

import type { CardEnvelope } from "@toorow/card-shell";

export const FIXTURE_ENVELOPE: CardEnvelope = {
  schema_version: "1",
  meta: {
    freshness: { last_pull: "2026-07-18T00:44:31.805655Z", cadence_hours: 24, stale_since: null },
    provenance: {
      source_system: "google-analytics",
      source_field: "fact_daily_kpi",
      pull_id: "pull_01KXSAX55XMYAG20K5F0K92XFN",
      pull_ids: ["pull_01KXSAWDF50RBD1H3JX1YD1A7T", "pull_01KXSAWSX2P2RQGPPR366WT03E", "pull_01KXSAX55XMYAG20K5F0K92XFN"],
    },
    alerts: [],
    as_of: null,
    project_id: "default",
    card_selection: {
      chosen: "journey",
      mode: "explicit",
      answers_question: "Où les utilisateurs décrochent-ils (étapes du parcours, taux de passage) ?",
      alternatives: [],
    },
    context_events: [],
  },
  data: {
    card_id: "journey",
    card_type: "journey",
    title: "Parcours utilisateur",
    answers_question: "Où les utilisateurs décrochent-ils (étapes du parcours, taux de passage) ?",
    date_range: { start: "2026-06-17", end: "2026-07-16" },
    connectors: ["google-analytics"],
    metrics: {},
    series: {},
    rendered_comment:
      "Décroché principal : étape « conversions » (taux de passage 3.3 %) (connector:fact_daily_kpi, pull_01KXSAX55XMYAG20K5F0K92XFN)\n" +
      "Première page d'entrée : « / » (35 364 sessions) (connector:fact_daily_kpi, pull_01KXSAX55XMYAG20K5F0K92XFN)\n" +
      "Contexte manquant pour cette période.",
    metric_definitions: {
      sessions: { definition: "Sessions initiées.", unit: "sessions", direction: "up_good" },
      active_users: { definition: "Utilisateurs actifs ayant engagé.", unit: "utilisateurs", direction: "up_good" },
      conversions: { definition: "Conversions finales.", unit: "conversions", direction: "up_good" },
      screen_page_views: { definition: "Pages vues (Google Analytics).", unit: "vues", direction: "up_good" },
    },
    composition: [
      {
        type: "funnel",
        binding: { steps: ["sessions", "active_users", "conversions"] },
        title: "Parcours sessions -> conversions",
        data: {
          steps: [
            { label: "sessions", value: 268981, rate: 1.0 },
            { label: "conversions", value: 8758, rate: 0.0326 },
          ],
          overall_rate: 0.0326,
        },
      },
      {
        type: "kpi_row",
        binding: { metrics: "*" },
        data: {
          metrics: [
            { metric: "sessions", value: 268981, delta: 47536, delta_pct: 21.0, direction: null },
            { metric: "conversions", value: 8758, delta: 1562, delta_pct: 22.0, direction: null },
            { metric: "screen_page_views", value: 375503, delta: 44230, delta_pct: 13.0, direction: null },
          ],
        },
      },
      // Story 10.4 — Pages d'entrée bar (sessions by landing_page, top 5, GA4 only). Real payload.
      {
        type: "bar",
        title: "Pages d'entrée",
        binding: {
          metrics: "sessions",
          dimensions: ["landing_page"],
          connector_scope: "google-analytics",
          top_n: 5,
        },
        data: {
          orientation: "horizontal",
          dimension: "landing_page",
          bars: [
            { label: "/", value: 35364 },
            { label: "/pricing", value: 16741 },
            { label: "/features", value: 11207 },
            { label: "/blog", value: 8417 },
            { label: "/about", value: 6695 },
          ],
        },
      },
      // Story 10.4 — Pages les plus vues table (screen_page_views by page, Part on top-N subtotal).
      // « Part » = share of the top-N subtotal (all returned GA page views = 375 503);
      // displayed top rows sum to < 100 % (the tail beyond the display is real). Real payload.
      {
        type: "table",
        title: "Pages les plus vues",
        binding: {
          metrics: "screen_page_views",
          dimensions: ["page"],
          connector_scope: "google-analytics",
          subtotal_share: true,
        },
        data: {
          columns: [
            { key: "_dim", label: "Page", numeric: false },
            { key: "screen_page_views", label: "Vues", numeric: true },
            { key: "part_pct", label: "Part", numeric: true },
          ],
          rows: [
            { _dim: "/", screen_page_views: 97339, part_pct: 25.9 },
            { _dim: "/pricing", screen_page_views: 46287, part_pct: 12.3 },
            { _dim: "/features", screen_page_views: 30395, part_pct: 8.1 },
            { _dim: "/blog", screen_page_views: 23144, part_pct: 6.2 },
            { _dim: "/about", screen_page_views: 18561, part_pct: 4.9 },
            { _dim: "/contact", screen_page_views: 15610, part_pct: 4.2 },
            { _dim: "/login", screen_page_views: 12776, part_pct: 3.4 },
            { _dim: "/signup", screen_page_views: 11552, part_pct: 3.1 },
          ],
          subtotal: 375503,
        },
      },
      {
        type: "comment",
        binding: { metrics: "*" },
        data: {
          text:
            "Décroché principal : étape « conversions » (taux de passage 3.3 %) (connector:fact_daily_kpi, pull_01KXSAX55XMYAG20K5F0K92XFN)\n" +
            "Première page d'entrée : « / » (35 364 sessions) (connector:fact_daily_kpi, pull_01KXSAX55XMYAG20K5F0K92XFN)\n" +
            "Contexte manquant pour cette période.",
        },
      },
    ],
  },
};

/**
 * FIXTURE_ENVELOPE_STALE — stale scenario (F-10 ui).
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
 * FIXTURE_ENVELOPE_PARTIAL — partial data with null overall_rate (F-10 ui).
 * Used to verify NumberHero is absent when overall_rate is null.
 */
export const FIXTURE_ENVELOPE_PARTIAL: CardEnvelope = {
  ...FIXTURE_ENVELOPE,
  data: {
    ...FIXTURE_ENVELOPE.data,
    composition: (FIXTURE_ENVELOPE.data.composition ?? []).map((b) => {
      if (b.type === "funnel") {
        return {
          ...b,
          data: {
            steps: [
              { label: "Sessions", value: 1200, rate: 1.0 },
            ],
            overall_rate: null,
          },
        };
      }
      return b;
    }),
  },
};
