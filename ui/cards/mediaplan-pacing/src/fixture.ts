/**
 * Fixture de développement / test pour la carte Pacing Médiaplan (Story 22.5).
 *
 * AI-54 (review 22.5 F-4) : le happy-path FIXTURE_ENVELOPE est un SNAPSHOT du
 * VRAI resolveur `_resolve_mediaplan_pacing_card` (fichier
 * `__fixtures__/envelope.json`), généré par :
 *
 *   python scripts/gen_mediaplan_pacing_fixture.py
 *
 * NE PAS l'éditer à la main — régénérer via le script (stand-ins fidèles aux
 * DDL dbt de 22.4). Les variantes EMPTY / PACE_NULL sont dérivées en TS.
 * À re-régénérer au premier passage live (marqueur AI-54 conservé).
 *
 * Les valeurs encodent le cas chiffré de la story 22.4 (Given/When/Then) :
 *   - line-digital-a : 3000 €/30j, 10j écoulés, 1250 € réel
 *       → consommé 41,7 %, pace +25 %, reste 1750 €, extrapolé 3750 € (Estimation)
 *   - line-tv : plan-only (TV Brand) → actual/pace NULL, badge « Plan seul »
 *
 * RÈGLES AD-9 NON-NÉGOCIABLES :
 *   * pace NULL → valeur null dans les données (jamais 0)
 *   * extrapolated_spend = Estimation (étiquette visible)
 *   * dépassement (>+10 %) et sous-livraison (<−10 %) signalés icône + texte
 *
 * Exports :
 *   FIXTURE_ENVELOPE           — happy-path (snapshot du vrai resolveur)
 *   FIXTURE_ENVELOPE_EMPTY     — plan sans lignes (état vide honnête)
 *   FIXTURE_ENVELOPE_PACE_NULL — ligne avec pace NULL (alloc to-date = 0)
 */

import type { CardEnvelope } from "@toorow/card-shell";

import envelopeSnapshot from "./__fixtures__/envelope.json";

const VERSION_ID = "00000000-0000-4000-8000-000000000221";

/** Snapshot du VRAI resolveur (AI-54) — voir scripts/gen_mediaplan_pacing_fixture.py. */
export const FIXTURE_ENVELOPE: CardEnvelope =
  envelopeSnapshot as unknown as CardEnvelope;

/**
 * FIXTURE_ENVELOPE_EMPTY — plan sans lignes (état vide honnête).
 * La carte rend un état vide sans exception.
 */
export const FIXTURE_ENVELOPE_EMPTY: CardEnvelope = {
  ...FIXTURE_ENVELOPE,
  data: {
    ...FIXTURE_ENVELOPE.data,
    connectors: [],
    rendered_comment: "Aucune ligne active dans ce plan.",
    composition: [
      {
        type: "table",
        title: "Lignes du plan",
        binding: { source: "plan_lines" },
        data: {
          columns: [
            { key: "label", label: "Ligne", numeric: false },
            { key: "budget", label: "Budget (€)", numeric: true },
            { key: "pace_pct", label: "Pace (%)", numeric: true },
          ],
          rows: [],
          estimate_columns: ["extrapolated_spend"],
          plan_only_badge_column: "is_plan_only",
        },
      },
      {
        type: "table",
        title: "Rollup par support",
        binding: { source: "plan_channels" },
        data: {
          columns: [
            { key: "channel", label: "Support", numeric: false },
            { key: "budget", label: "Budget (€)", numeric: true },
          ],
          rows: [],
          estimate_columns: ["extrapolated_spend"],
        },
      },
      {
        type: "comment",
        binding: { source: "plan_pacing" },
        data: { text: "Aucune ligne active dans ce plan." },
      },
    ],
  },
};

/**
 * FIXTURE_ENVELOPE_PACE_NULL — ligne dont pace est NULL (allocation to-date = 0).
 * AD-9 RÈGLE : pace_pct doit rester null, jamais 0 ou 0 %.
 */
export const FIXTURE_ENVELOPE_PACE_NULL: CardEnvelope = {
  ...FIXTURE_ENVELOPE,
  data: {
    ...FIXTURE_ENVELOPE.data,
    rendered_comment:
      "Pace non disponible (aucune allocation to-date). Contexte manquant pour cette période.",
    composition: [
      {
        type: "table",
        title: "Lignes du plan",
        binding: { source: "plan_lines" },
        data: {
          columns: [
            { key: "label", label: "Ligne", numeric: false },
            { key: "budget", label: "Budget (€)", numeric: true },
            { key: "consumed_pct", label: "Consommé (%)", numeric: true },
            { key: "pace_pct", label: "Pace (%)", numeric: true },
            {
              key: "extrapolated_spend",
              label: "Extrapolé (€) (Estimation)",
              numeric: true,
            },
            { key: "is_plan_only", label: "Plan seul", numeric: false },
          ],
          rows: [
            {
              line_key: "line-future",
              label: "Campagne future",
              channel: "digital",
              budget: 10000.0,
              actual_to_date: null,
              allocated_to_date: 0.0,
              consumed_pct: null,
              pace_pct: null, // AD-9 : NULL car alloc to-date = 0, jamais 0
              remaining_budget: null,
              extrapolated_spend: null,
              is_plan_only: false,
              plan_version_id: VERSION_ID,
              actual_pull_id_min: null,
              actual_pull_id_max: null,
            },
          ],
          estimate_columns: ["extrapolated_spend"],
          plan_only_badge_column: "is_plan_only",
        },
      },
      {
        type: "table",
        title: "Rollup par support",
        binding: { source: "plan_channels" },
        data: {
          columns: [
            { key: "channel", label: "Support", numeric: false },
            { key: "pace_pct", label: "Pace (%)", numeric: true },
          ],
          rows: [],
          estimate_columns: [],
        },
      },
      {
        type: "comment",
        binding: { source: "plan_pacing" },
        data: {
          text: "Pace non disponible (aucune allocation to-date). Contexte manquant pour cette période.",
        },
      },
    ],
  },
};
