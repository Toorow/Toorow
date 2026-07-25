/**
 * Standalone dev/test fixture for the connectors card (Story 9.8).
 *
 * Composition contract (server side, being built in parallel):
 *   composition = [
 *     {type: "table", title: "Connecteurs", data: {columns: [...], rows: [...]}},
 *     {type: "comment", data: {text: "<cited comment>"}}
 *   ]
 *   data.card_type = "connectors"
 *   status values: "Opérationnel" | "Obsolète" | "Révoqué" | "En erreur" | "Inconnu"
 *
 * Exports:
 *   FIXTURE_ENVELOPE        — happy-path (3 connectors: one per distinct status bucket)
 *   FIXTURE_ENVELOPE_EMPTY  — no connectors configured -> designed empty state
 *   FIXTURE_ENVELOPE_PARTIAL — single connector, minimal fields
 */

import type { CardEnvelope } from "@toorow/card-shell";

export const FIXTURE_ENVELOPE: CardEnvelope = {
  schema_version: "1",
  meta: {
    freshness: { last_pull: "2026-07-14T06:00:00Z", cadence_hours: 24, stale_since: null },
    provenance: {
      source_system: "connector-registry",
      source_field: "connector_health",
      pull_id: "pull_CN_FIXTURE0000000000000",
      pull_ids: ["pull_CN_FIXTURE0000000000000"],
    },
    alerts: [],
    as_of: null,
    project_id: "default",
    card_selection: {
      chosen: "connectors",
      mode: "suggested",
      answers_question: "Quels connecteurs sont disponibles et que alimentent-ils ?",
      alternatives: [],
    },
    context_events: [],
  },
  data: {
    card_id: "connectors",
    card_type: "connectors",
    title: "Connecteurs",
    answers_question: "Quels connecteurs sont disponibles et que alimentent-ils ?",
    date_range: { start: "2026-07-14", end: "2026-07-14" },
    connectors: ["connector-registry"],
    metrics: {},
    series: {},
    rendered_comment:
      "Le connecteur google-ads n'a pas extrait de données depuis 2026-07-01 " +
      "(connector-registry:connector_health, pull_CN_FIXTURE0000000000000).",
    metric_definitions: {},
    composition: [
      {
        type: "table",
        title: "Connecteurs",
        binding: {},
        data: {
          columns: [
            { key: "connector",     label: "Connecteur",      numeric: false, sortable: true  },
            { key: "status",        label: "Statut",          numeric: false, sortable: true  },
            { key: "last_extract",  label: "Dernier extrait", numeric: false, sortable: true  },
            { key: "flows",         label: "Flows alimentés", numeric: false, sortable: false },
          ],
          rows: [
            {
              connector:    "google-analytics",
              status:       "Opérationnel",
              last_extract: "2026-07-14",
              flows:        "rapport-seo, rapport-audience",
            },
            {
              connector:    "google-ads",
              status:       "Obsolète",
              last_extract: "2026-07-01",
              flows:        "rapport-performance",
            },
            {
              connector:    "meta-ads",
              status:       "En erreur",
              last_extract: "2026-07-13",
              flows:        "rapport-social, rapport-performance",
            },
          ],
        },
      },
      {
        type: "comment",
        binding: {},
        data: {
          text:
            "Le connecteur google-ads n'a pas extrait de données depuis 2026-07-01 " +
            "(connector-registry:connector_health, pull_CN_FIXTURE0000000000000).",
        },
      },
    ],
  },
};

/**
 * FIXTURE_ENVELOPE_EMPTY — no connectors configured.
 * Used to verify the designed empty state "Aucun connecteur configuré".
 */
export const FIXTURE_ENVELOPE_EMPTY: CardEnvelope = {
  ...FIXTURE_ENVELOPE,
  data: {
    ...FIXTURE_ENVELOPE.data,
    rendered_comment: "",
    composition: [
      {
        type: "table",
        title: "Connecteurs",
        binding: {},
        data: {
          columns: [
            { key: "connector",     label: "Connecteur",      numeric: false, sortable: true  },
            { key: "status",        label: "Statut",          numeric: false, sortable: true  },
            { key: "last_extract",  label: "Dernier extrait", numeric: false, sortable: true  },
            { key: "flows",         label: "Flows alimentés", numeric: false, sortable: false },
          ],
          rows: [],
        },
      },
    ],
  },
};

/**
 * FIXTURE_ENVELOPE_PARTIAL — single connector, minimal fields.
 * Verifies the table renders gracefully with a subset of rows.
 */
export const FIXTURE_ENVELOPE_PARTIAL: CardEnvelope = {
  ...FIXTURE_ENVELOPE,
  data: {
    ...FIXTURE_ENVELOPE.data,
    composition: [
      {
        type: "table",
        title: "Connecteurs",
        binding: {},
        data: {
          columns: [
            { key: "connector",     label: "Connecteur",      numeric: false, sortable: true  },
            { key: "status",        label: "Statut",          numeric: false, sortable: true  },
            { key: "last_extract",  label: "Dernier extrait", numeric: false, sortable: true  },
            { key: "flows",         label: "Flows alimentés", numeric: false, sortable: false },
          ],
          rows: [
            {
              connector:    "google-analytics",
              status:       "Opérationnel",
              last_extract: "2026-07-14",
              flows:        "rapport-seo",
            },
          ],
        },
      },
      {
        type: "comment",
        binding: {},
        data: { text: "Tous les connecteurs sont opérationnels." },
      },
    ],
  },
};
