/**
 * Fixture de développement / test pour la carte Déduplication (Story 17.3).
 *
 * AI-54 : cette fixture est CONSTRUITE selon le contrat d'envelope (17-3-agent-surface-and-dedup-card.md),
 * PAS encore capturée depuis le vrai get_card (l'orchestrateur n'a pas encore joué le seed
 * 17.2 en live). À RÉGÉNÉRER par l'orchestrateur depuis le vrai get_card après le premier
 * run live :
 *
 *   get_card(template="dedup", project_id="default",
 *            date_from="2026-06-17", date_to="2026-07-16")
 *
 * RÈGLE AD-9 NON-NÉGOCIABLE :
 *   Toute valeur de duplication_rate est une ESTIMATION — le mot « Estimation » apparaît
 *   VERBATIM dans chaque bloc qui porte un chiffre de déduplication. Jamais présentée
 *   comme une mesure exacte.
 *
 * Composition (ordre serveur contractuel, AI-54) :
 *   1. kpi_row  — taux de duplication héros (estimation)
 *   2. bar      — revendiqué vs dédupliqué par canal (dedup_grouped)
 *   3. table    — détail par canal : Canal / Revendiqué / Contribution dédupliquée (Estimation) / Part
 *   4. comment  — build_dedup_comment (AD-9 : formule + source + estimation verbatim)
 *
 * Valeurs fixture (identiques à l'AC epic-17) :
 *   Meta-ads : 80→50, Google-ads : 60→37,5, LinkedIn-ads : 20→12,5
 *   verified=100/jour × 3 jours = 300 ; claimed=160/jour × 3 = 480 ; rate=1,6
 *
 * Exports :
 *   FIXTURE_ENVELOPE              — happy-path (GA4 source, 3 canaux)
 *   FIXTURE_ENVELOPE_NULL_RATE    — verified NULL (stripe v1 BLOCKED) : taux NULL
 *   FIXTURE_ENVELOPE_OPT_OUT      — projet sans source de vérification (0 lignes)
 */

import type { CardEnvelope } from "@toorow/card-shell";

export const FIXTURE_ENVELOPE: CardEnvelope = {
  schema_version: "1",
  meta: {
    freshness: {
      last_pull: null,
      cadence_hours: 24,
      stale_since: null,
    },
    provenance: {
      source_system: "dedup_estimate",
      source_field: "marts.dedup_estimate",
      pull_id: "pull_17_3_fixture",
      pull_ids: ["pull_17_3_fixture"],
    },
    alerts: [],
    trace_id: null,
    as_of: null,
    context_events: [],
    card_selection: {
      chosen: "dedup",
      mode: "explicit",
      answers_question:
        "Mes conversions sont-elles comptées en double par les régies ?",
      alternatives: [],
    },
    project_id: "default",
  },
  data: {
    card_id: "dedup",
    card_type: "dedup",
    title: "Déduplication",
    answers_question:
      "Mes conversions sont-elles comptées en double par les régies ?",
    date_range: { start: "2026-06-17", end: "2026-07-16" },
    connectors: ["google-ads", "linkedin-ads", "meta-ads"],
    rendered_comment:
      // review-17-5 fix-6: comment format updated — rate line now reads
      // "Estimation : Taux de duplication estimé : 1,6x (sur N jours) (formule source) [cit]"
      // Fixture uses rate_coverage_days=3, rate_claimed_days=3 (full coverage).
      "Estimation : Taux de duplication estimé : 1,6x (sur 3 jours) (taux = Σ revendiqué ÷ réel, source : ga4 (évènement : generate_lead)) " +
      "(dedup_estimate:marts.dedup_estimate, pull_17_3_fixture)\n" +
      "Canal leader (estimation dédupliquée) : « meta-ads » (150 conversions, 50,0 % du réel) " +
      "(dedup_estimate:marts.dedup_estimate, pull_17_3_fixture)\n" +
      "Contexte manquant pour cette période.",
    composition: [
      // 1. kpi_row — taux de duplication en héros.
      // RÈGLE AD-9 : estimate_label='Estimation' obligatoire sur chaque metric entry.
      // value = 1.6 (= 480 revendiqués / 300 vérifiés sur la fenêtre).
      {
        type: "kpi_row",
        title: "Taux de duplication (Estimation)",
        binding: { metrics: "duplication_rate" },
        data: {
          metrics: [
            {
              metric: "duplication_rate",
              value: 1.6,
              delta: null,
              delta_pct: null,
              direction: "down_good",
              // AD-9 : l'étiquette est TOUJOURS présente, même pour une valeur non-NULL.
              estimate_label: "Estimation",
              unit: "x (réclamé / réel)",
            },
          ],
          estimate_label: "Estimation",
          formula: "taux = Σ revendiqué ÷ réel",
        },
      },
      // 2. bar — revendiqué vs dédupliqué par canal (dedup_grouped).
      // Deux séries : claimed_conversions (revendiqué) et deduplicated_contribution.
      // AD-9 : le titre et la légende portent « Estimation » pour la série dédupliquée.
      {
        type: "bar",
        title: "Revendiqué vs dédupliqué par canal (Estimation)",
        binding: {
          metrics: ["claimed_conversions", "deduplicated_contribution"],
          dimensions: ["channel_connector"],
          dedup_grouped: true,
        },
        data: {
          orientation: "horizontal",
          dimension: "channel_connector",
          series: [
            {
              metric: "claimed_conversions",
              label: "Revendiqué",
              bars: [
                { label: "meta-ads", value: 240.0 },
                { label: "google-ads", value: 180.0 },
                { label: "linkedin-ads", value: 60.0 },
              ],
            },
            {
              metric: "deduplicated_contribution",
              label: "Contribution dédupliquée (Estimation)",
              bars: [
                { label: "meta-ads", value: 150.0 },
                { label: "google-ads", value: 112.5 },
                { label: "linkedin-ads", value: 37.5 },
              ],
            },
          ],
          estimate_label: "Estimation",
        },
      },
      // 3. table — détail par canal.
      // Colonnes : Canal / Revendiqué / Contribution dédupliquée (Estimation) / Part (%).
      // Part = deduplicated_contribution / verified_total (300).
      // Meta 150/300=50%, Google 112,5/300=37,5%, LinkedIn 37,5/300=12,5%.
      {
        type: "table",
        title: "Détail par canal",
        binding: {
          metrics: ["claimed_conversions", "deduplicated_contribution"],
          dimensions: ["channel_connector"],
          dedup_share: true,
        },
        data: {
          columns: [
            { key: "_dim", label: "Canal", numeric: false },
            { key: "claimed_conversions", label: "Revendiqué", numeric: true },
            {
              key: "deduplicated_contribution",
              label: "Contribution dédupliquée (Estimation)",
              numeric: true,
            },
            { key: "part_pct", label: "Part (%)", numeric: true },
          ],
          rows: [
            {
              _dim: "meta-ads",
              claimed_conversions: 240.0,
              deduplicated_contribution: 150.0,
              part_pct: 50.0,
            },
            {
              _dim: "google-ads",
              claimed_conversions: 180.0,
              deduplicated_contribution: 112.5,
              part_pct: 37.5,
            },
            {
              _dim: "linkedin-ads",
              claimed_conversions: 60.0,
              deduplicated_contribution: 37.5,
              part_pct: 12.5,
            },
          ],
          estimate_label: "Estimation",
        },
      },
      // 4. comment — build_dedup_comment (AD-9).
      // Ligne 1 : taux + formule + source de vérité (ga4, generate_lead) — cité.
      // Ligne 2 : canal leader + part du réel — cité.
      // Ligne 3 : contexte manquant (aucun context_events fourni) — verbatim AD-9.
      {
        type: "comment",
        binding: { metrics: "*" },
        data: {
          text:
            "Estimation : Taux de duplication estimé : 1,6x (sur 3 jours) (taux = Σ revendiqué ÷ réel, source : ga4 (évènement : generate_lead)) " +
            "(dedup_estimate:marts.dedup_estimate, pull_17_3_fixture)\n" +
            "Canal leader (estimation dédupliquée) : « meta-ads » (150 conversions, 50,0 % du réel) " +
            "(dedup_estimate:marts.dedup_estimate, pull_17_3_fixture)\n" +
            "Contexte manquant pour cette période.",
        },
      },
    ],
  },
};

/**
 * FIXTURE_ENVELOPE_NULL_RATE — verified NULL (stripe v1 BLOCKED, 15.7).
 *
 * Quand la source de vérification est 'stripe' et que la story 15.7 n'est pas encore
 * livrée, verified_total=NULL → duplication_rate=NULL.
 * AD-9 RÈGLE : value=null dans kpi_row (jamais 0), mention explicite dans le commentaire.
 */
export const FIXTURE_ENVELOPE_NULL_RATE: CardEnvelope = {
  ...FIXTURE_ENVELOPE,
  data: {
    ...FIXTURE_ENVELOPE.data,
    rendered_comment:
      "Estimation indisponible : source de vérification absente ou non configurée " +
      "(taux = Σ revendiqué ÷ réel) (dedup_estimate:marts.dedup_estimate, pull_17_3_fixture)\n" +
      "Contexte manquant pour cette période.",
    composition: [
      // kpi_row : rate NULL → valeur absente (jamais 0)
      {
        type: "kpi_row",
        title: "Taux de duplication (Estimation)",
        binding: { metrics: "duplication_rate" },
        data: {
          metrics: [
            {
              metric: "duplication_rate",
              value: null,
              delta: null,
              delta_pct: null,
              direction: "down_good",
              estimate_label: "Estimation",
              unit: "x (réclamé / réel)",
              empty_reason:
                "Source de vérification indisponible (données régies non disponibles ou source 'stripe' non encore livrée).",
            },
          ],
          estimate_label: "Estimation",
          formula: "taux = Σ revendiqué ÷ réel",
        },
      },
      // bar : deduplicated_contribution=null pour chaque canal (dedup impossible sans verified)
      {
        type: "bar",
        title: "Revendiqué vs dédupliqué par canal (Estimation)",
        binding: {
          metrics: ["claimed_conversions", "deduplicated_contribution"],
          dimensions: ["channel_connector"],
          dedup_grouped: true,
        },
        data: {
          orientation: "horizontal",
          dimension: "channel_connector",
          series: [
            {
              metric: "claimed_conversions",
              label: "Revendiqué",
              bars: [{ label: "meta-ads", value: 80.0 }],
            },
            {
              metric: "deduplicated_contribution",
              label: "Contribution dédupliquée (Estimation)",
              bars: [
                {
                  label: "meta-ads",
                  value: null,
                  null_reason: "source indisponible",
                },
              ],
            },
          ],
          estimate_label: "Estimation",
        },
      },
      // table : deduplicated_contribution=null, part_pct=null
      {
        type: "table",
        title: "Détail par canal",
        binding: {
          metrics: ["claimed_conversions", "deduplicated_contribution"],
          dimensions: ["channel_connector"],
          dedup_share: true,
        },
        data: {
          columns: [
            { key: "_dim", label: "Canal", numeric: false },
            { key: "claimed_conversions", label: "Revendiqué", numeric: true },
            {
              key: "deduplicated_contribution",
              label: "Contribution dédupliquée (Estimation)",
              numeric: true,
            },
            { key: "part_pct", label: "Part (%)", numeric: true },
          ],
          rows: [
            {
              _dim: "meta-ads",
              claimed_conversions: 80.0,
              deduplicated_contribution: null,
              part_pct: null,
            },
          ],
          estimate_label: "Estimation",
        },
      },
      // comment : mention explicite de l'absence (jamais 0% déguisé)
      {
        type: "comment",
        binding: { metrics: "*" },
        data: {
          text:
            "Estimation indisponible : source de vérification absente ou non configurée " +
            "(taux = Σ revendiqué ÷ réel) (dedup_estimate:marts.dedup_estimate, pull_17_3_fixture)\n" +
            "Contexte manquant pour cette période.",
        },
      },
    ],
  },
};

/**
 * FIXTURE_ENVELOPE_OPT_OUT — projet sans source de vérification désignée (0 lignes).
 *
 * Un projet qui n'a pas configuré verification_source_type → 0 lignes dans dedup_estimate.
 * La carte retourne un état vide designé avec un message de guidance (lien console).
 * Jamais un chiffre inventé, jamais une erreur.
 */
export const FIXTURE_ENVELOPE_OPT_OUT: CardEnvelope = {
  ...FIXTURE_ENVELOPE,
  data: {
    ...FIXTURE_ENVELOPE.data,
    connectors: [],
    rendered_comment:
      "Aucune source de vérification désignée pour ce projet. " +
      "Configurez une source dans la console d'administration " +
      "pour activer la déduplication estimée.",
    composition: [
      {
        type: "kpi_row",
        title: "Taux de duplication (Estimation)",
        binding: { metrics: "duplication_rate" },
        data: {
          metrics: [
            {
              metric: "duplication_rate",
              value: null,
              delta: null,
              delta_pct: null,
              direction: "down_good",
              estimate_label: "Estimation",
              unit: "x (réclamé / réel)",
              empty_reason: "Aucune source de vérification désignée.",
            },
          ],
          estimate_label: "Estimation",
          formula: "taux = Σ revendiqué ÷ réel",
        },
      },
      {
        type: "bar",
        title: "Revendiqué vs dédupliqué par canal (Estimation)",
        binding: {
          metrics: ["claimed_conversions", "deduplicated_contribution"],
          dimensions: ["channel_connector"],
          dedup_grouped: true,
        },
        data: {
          orientation: "horizontal",
          dimension: "channel_connector",
          series: [
            { metric: "claimed_conversions", label: "Revendiqué", bars: [] },
            {
              metric: "deduplicated_contribution",
              label: "Contribution dédupliquée (Estimation)",
              bars: [],
            },
          ],
          estimate_label: "Estimation",
        },
      },
      {
        type: "table",
        title: "Détail par canal",
        binding: {
          metrics: ["claimed_conversions", "deduplicated_contribution"],
          dimensions: ["channel_connector"],
          dedup_share: true,
        },
        data: {
          columns: [
            { key: "_dim", label: "Canal", numeric: false },
            { key: "claimed_conversions", label: "Revendiqué", numeric: true },
            {
              key: "deduplicated_contribution",
              label: "Contribution dédupliquée (Estimation)",
              numeric: true,
            },
            { key: "part_pct", label: "Part (%)", numeric: true },
          ],
          rows: [],
          estimate_label: "Estimation",
        },
      },
      {
        type: "comment",
        binding: { metrics: "*" },
        data: {
          text:
            "Aucune source de vérification désignée pour ce projet. " +
            "Configurez une source dans la console d'administration " +
            "pour activer la déduplication estimée.",
        },
      },
    ],
  },
};
