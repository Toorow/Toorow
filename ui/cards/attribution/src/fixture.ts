/**
 * Fixture de développement / test pour la carte Attribution (Story 16.3).
 *
 * AI-54 : cette fixture est CONSTRUITE selon le contrat d'envelope (16-3-card-attribution.md),
 * PAS encore capturée depuis le vrai get_card (l'orchestrateur n'a pas encore joué le seed
 * 16.1 en live). À RÉGÉNÉRER par l'orchestrateur depuis le vrai get_card après le premier
 * run live :
 *
 *   get_card(template="attribution", project_id="default",
 *            metrics=["conversions","sessions"],
 *            date_from="2026-06-17", date_to="2026-07-16")
 *
 * Composition (ordre serveur contractuel) :
 *   1. kpi_row(conversions — partition session_source_medium uniquement)
 *   2. bar(Canaux — dernier clic, session_source_medium, top-N)
 *   3. bar(Canaux — premier clic, first_user_source_medium, top-N)
 *   4. table(Campagnes — dernier clic, session_campaign, Part top-N)
 *   5. comment (build_attribution_comment cité, AD-9 — jamais de cause inventée)
 *
 * CONTRAINTE DURE double-compte héritée de 16.2 :
 *   session_source_medium, session_campaign, first_user_source_medium sont TROIS
 *   partitions marginales des MÊMES conversions — sommer deux ou trois d'entre elles
 *   donnerait ~2× ou ~3×. Chaque bloc filtre sur UNE SEULE breakdown_dimension.
 *   share_of_conversions = part du top-N (semantique 10.4), jamais du total GA4 réel.
 *
 * Exports :
 *   FIXTURE_ENVELOPE              — happy-path (acquisition datastream ON)
 *   FIXTURE_ENVELOPE_NO_ACQDATA   — datastream OFF : tous les blocs acquisition vides
 */

import type { CardEnvelope } from "@toorow/card-shell";

export const FIXTURE_ENVELOPE: CardEnvelope = {
  schema_version: "1",
  meta: {
    freshness: {
      last_pull: "2026-07-18T00:00:00.000000Z",
      cadence_hours: 24,
      stale_since: null,
    },
    provenance: {
      source_system: "google-analytics",
      source_field: "fact_daily_kpi",
      pull_id: "pull_16_3_fixture",
      pull_ids: ["pull_16_3_fixture"],
    },
    alerts: [],
    trace_id: null,
    as_of: null,
    context_events: [],
    card_selection: {
      chosen: "attribution",
      mode: "explicit",
      answers_question:
        "Quelle part de mes conversions vient de chaque canal, en dernier clic vs premier clic ?",
      alternatives: [],
    },
    project_id: "default",
  },
  data: {
    card_id: "attribution",
    card_type: "attribution",
    title: "Attribution",
    answers_question:
      "Quelle part de mes conversions vient de chaque canal, en dernier clic vs premier clic ?",
    date_range: { start: "2026-06-17", end: "2026-07-16" },
    connectors: ["google-analytics"],
    metrics: {
      conversions: {
        value: 3780.0,
        delta: 420.0,
        delta_pct: "+12%",
        period: "sem. préc.",
        source_system: "google-analytics",
        source_field: "conversions",
        pull_id: "pull_16_3_fixture",
      },
    },
    series: {},
    rendered_comment:
      "Canal leader (dernier clic) : « cpc / google » (1 890 conversions, 50,0 % du top-N dernier clic) " +
      "(connector:fact_daily_kpi, pull_16_3_fixture)\n" +
      "Écart last/first pour « cpc / google » : 50,0 % (dernier clic) vs 35,0 % (premier clic) — plus fort en dernier clic. " +
      "(connector:fact_daily_kpi, pull_16_3_fixture)\n" +
      "Contexte manquant pour cette période.",
    composition: [
      // 1. kpi_row — conversions de la partition session_source_medium uniquement.
      // CONTRAINTE DURE : la valeur 3 780 = SUM(session_source_medium) sur la fenêtre,
      // jamais SUM(toutes partitions) qui serait ~11 340 (3×).
      {
        type: "kpi_row",
        binding: {
          metrics: "conversions",
          breakdown_dim_filter: "session_source_medium",
        },
        data: {
          metrics: [
            {
              metric: "conversions",
              value: 3780.0,
              delta: 420.0,
              delta_pct: 12.0,
              direction: null,
            },
          ],
        },
      },
      // 2. Bar — Canaux dernier clic (session_source_medium uniquement).
      // Part = share du top-N retourné (sémantique 10.4 — pas du total GA4 réel).
      {
        type: "bar",
        title: "Canaux — dernier clic (top-N)",
        binding: {
          metrics: "conversions",
          dimensions: ["session_source_medium"],
          breakdown_dim_filter: "session_source_medium",
        },
        data: {
          orientation: "horizontal",
          dimension: "session_source_medium",
          bars: [
            { label: "cpc / google", value: 1890.0 },
            { label: "organic / google", value: 1134.0 },
            { label: "direct / (none)", value: 504.0 },
            { label: "referral / partenaire", value: 252.0 },
          ],
        },
      },
      // 3. Bar — Canaux premier clic (first_user_source_medium uniquement).
      // Premier clic N'A PAS de dimension campagne (16.1 : firstUserCampaignName non ingérée).
      // sessions = NULL pour le premier clic (16.2 : sessions uniquement sur session_source_medium).
      {
        type: "bar",
        title: "Canaux — premier clic (top-N)",
        binding: {
          metrics: "conversions",
          dimensions: ["first_user_source_medium"],
          breakdown_dim_filter: "first_user_source_medium",
        },
        data: {
          orientation: "horizontal",
          dimension: "first_user_source_medium",
          bars: [
            { label: "organic / google", value: 1512.0 },
            { label: "cpc / google", value: 1323.0 },
            { label: "direct / (none)", value: 630.0 },
            { label: "referral / partenaire", value: 315.0 },
          ],
        },
      },
      // 4. Table — Campagnes dernier clic (session_campaign uniquement).
      // Part (top-N) = part du SUM des lignes retournées, PAS du total GA4 réel.
      // Même sémantique que la colonne « Part » de 10.4 (subtotal_share).
      {
        type: "table",
        title: "Campagnes — dernier clic (top-N)",
        binding: {
          metrics: ["conversions"],
          dimensions: ["session_campaign"],
          breakdown_dim_filter: "session_campaign",
          subtotal_share: true,
          subtotal_share_label: "Part (top-N)",
          dim_label: "Campagne",
        },
        data: {
          columns: [
            { key: "_dim", label: "Campagne", numeric: false },
            { key: "conversions", label: "conversions", numeric: true },
            { key: "part_pct", label: "Part (top-N)", numeric: true },
          ],
          rows: [
            { _dim: "summer_sale", conversions: 1620.0, part_pct: 42.9 },
            { _dim: "brand_awareness", conversions: 1134.0, part_pct: 30.0 },
            { _dim: "retargeting", conversions: 630.0, part_pct: 16.7 },
            { _dim: "prospection", conversions: 396.0, part_pct: 10.5 },
          ],
          subtotal: 3780.0,
        },
      },
      // 5. Comment — build_attribution_comment (AD-9, <= 3 lignes).
      // Ligne 1 : canal leader dernier clic (constat de données, jamais cause).
      // Ligne 2 : écart first vs last si notable (>= 5 pp) — constat de répartition.
      // Ligne 3 : contexte manquant (aucun context_events fourni).
      {
        type: "comment",
        binding: { metrics: "*" },
        data: {
          text:
            "Canal leader (dernier clic) : « cpc / google » (1 890 conversions, 50,0 % du top-N dernier clic) " +
            "(connector:fact_daily_kpi, pull_16_3_fixture)\n" +
            "Écart last/first pour « cpc / google » : 50,0 % (dernier clic) vs 35,0 % (premier clic) — plus fort en dernier clic. " +
            "(connector:fact_daily_kpi, pull_16_3_fixture)\n" +
            "Contexte manquant pour cette période.",
        },
      },
    ],
  },
};

/**
 * FIXTURE_ENVELOPE_NO_ACQDATA — datastream acquisition OFF.
 *
 * Quand les données d'acquisition (session_source_medium, session_campaign,
 * first_user_source_medium) ne sont pas encore ingérées (16.1 pas encore joué),
 * tous les blocs acquisition retournent des états vides designés.
 * La carte reste rendable — jamais d'erreur (règle e épique 9).
 */
export const FIXTURE_ENVELOPE_NO_ACQDATA: CardEnvelope = {
  ...FIXTURE_ENVELOPE,
  data: {
    ...FIXTURE_ENVELOPE.data,
    rendered_comment: "Conversions totales : 0. (contexte manquant)\nContexte manquant pour cette période.",
    composition: [
      // kpi_row : aucune ligne session_source_medium -> valeur null (empty-state honnête)
      {
        type: "kpi_row",
        binding: {
          metrics: "conversions",
          breakdown_dim_filter: "session_source_medium",
        },
        data: {
          metrics: [
            { metric: "conversions", value: null, delta: null, delta_pct: null, direction: null },
          ],
        },
      },
      // bar dernier clic : vide (bars: [])
      {
        type: "bar",
        title: "Canaux — dernier clic (top-N)",
        binding: {
          metrics: "conversions",
          dimensions: ["session_source_medium"],
          breakdown_dim_filter: "session_source_medium",
        },
        data: { orientation: "horizontal", dimension: null, bars: [] },
      },
      // bar premier clic : vide (bars: [])
      {
        type: "bar",
        title: "Canaux — premier clic (top-N)",
        binding: {
          metrics: "conversions",
          dimensions: ["first_user_source_medium"],
          breakdown_dim_filter: "first_user_source_medium",
        },
        data: { orientation: "horizontal", dimension: null, bars: [] },
      },
      // table campagnes : vide (rows: [])
      {
        type: "table",
        title: "Campagnes — dernier clic (top-N)",
        binding: {
          metrics: ["conversions"],
          dimensions: ["session_campaign"],
          breakdown_dim_filter: "session_campaign",
          subtotal_share: true,
          subtotal_share_label: "Part (top-N)",
          dim_label: "Campagne",
        },
        data: {
          columns: [
            { key: "_dim", label: "Campagne", numeric: false },
            { key: "conversions", label: "conversions", numeric: true },
            { key: "part_pct", label: "Part (top-N)", numeric: true },
          ],
          rows: [],
        },
      },
      // comment : dégradé
      {
        type: "comment",
        binding: { metrics: "*" },
        data: {
          text: "Conversions totales : 0. (contexte manquant)\nContexte manquant pour cette période.",
        },
      },
    ],
  },
};
