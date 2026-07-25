/**
 * Standalone dev/test fixture (Data Delivery to Widget — Dev Notes).
 *
 * Used only when NO structuredContent is delivered by the host (i.e. the widget
 * is opened outside Claude, e.g. `vite preview` or a Node smoke render). It lets
 * the widget render deterministically without a live MCP round-trip.
 *
 * Small on purpose: a handful of days × 1 connector × 3 metrics. The real
 * payload (~527 kB, 86–90 days) arrives from get_daily_report at runtime.
 */

import type { DailyReportEnvelope, MetricDefinition, Row } from "./types";

function buildRows(): Row[] {
  const rows: Row[] = [];
  const start = new Date("2026-04-11T00:00:00Z");
  const metrics = ["sessions", "active_users", "conversions"];
  const devices = ["desktop", "mobile", "tablet"];
  // Story 8.11: two countries so the composite country>device split is meaningful.
  const countries = ["FR", "DE"];
  const countryFactor: Record<string, number> = { FR: 0.6, DE: 0.4 };
  const push = (
    iso: string,
    metric: string,
    dim: string,
    val: string,
    value: number,
  ) =>
    rows.push({
      date: iso,
      connector: "google-analytics",
      metric,
      breakdown_dimension: dim,
      breakdown_value: val,
      value: Math.max(0, value),
      pull_id: "pull_FIXTURE0000000000000000",
      loaded_at: "2026-07-10T00:00:00Z",
    });

  for (let d = 0; d < 90; d++) {
    const day = new Date(start.getTime() + d * 86400000);
    const iso = day.toISOString().slice(0, 10);
    const week = Math.sin((d / 7) * Math.PI * 2);
    for (const metric of metrics) {
      const base =
        metric === "sessions" ? 1200 : metric === "active_users" ? 800 : 40;
      const dayTotal = Math.round(base * (1 + 0.3 * week + 0.1 * ((d % 5) - 2)));

      // device_category series (day total split by device) — the pinned total series.
      for (const device of devices) {
        const factor = device === "desktop" ? 0.6 : device === "mobile" ? 0.3 : 0.1;
        push(iso, metric, "device_category", device, Math.round(dayTotal * factor));
      }

      // country series (same day total split by country).
      for (const country of countries) {
        push(iso, metric, "country", country, Math.round(dayTotal * countryFactor[country]));
      }

      // Story 8.11: composite country>device series — SAME day total split across
      // country x device. Sums back to the single-dimension totals (no double count).
      for (const country of countries) {
        for (const device of devices) {
          const factor = device === "desktop" ? 0.6 : device === "mobile" ? 0.3 : 0.1;
          push(
            iso,
            metric,
            "country>device",
            `${country}>${device}`,
            Math.round(dayTotal * countryFactor[country] * factor),
          );
        }
      }
    }
  }
  return rows;
}

/**
 * Story 8.8 (R6): dev fixture metric_definitions for sessions, active_users,
 * and conversions — added so the definition tooltip affordance is visible during
 * local development. Production envelopes carry this from the flow.report schema.
 */
const FIXTURE_METRIC_DEFINITIONS: Record<string, MetricDefinition> = {
  sessions: {
    definition:
      "Nombre de sessions initiées sur la propriété. Une session correspond à un groupe d'interactions d'un utilisateur sur votre site dans un intervalle de temps donné (expiration après 30 minutes d'inactivité).",
    unit: "séances",
    direction: "up_good",
    caveats:
      "Les sessions multi-appareils peuvent être comptabilisées séparément. Le filtrage des bots est appliqué automatiquement.",
  },
  active_users: {
    definition:
      "Nombre d'utilisateurs distincts ayant engagé avec le site au cours de la période sélectionnée (événement engagement ou session d'au moins 10 secondes).",
    unit: "utilisateurs",
    direction: "up_good",
    caveats:
      "Basé sur les cookies (web) ou les ID d'instance (app). Les utilisateurs non-consentants peuvent être sous-comptés selon la configuration du mode consentement.",
  },
  conversions: {
    definition:
      "Nombre total d'événements marqués comme conversions dans la propriété GA4 (achat, formulaire soumis, etc.). Chaque conversion est comptée une fois par session.",
    unit: "conversions",
    direction: "up_good",
    caveats:
      "La définition des événements de conversion est propre à chaque propriété GA4. Vérifiez la configuration dans la console GA4 si les valeurs semblent inattendues.",
  },
};

export const FIXTURE_ENVELOPE: DailyReportEnvelope = {
  schema_version: "1",
  meta: {
    freshness: { last_pull: "2026-07-10T00:00:00Z", cadence_hours: 24, stale_since: null },
    provenance: [
      { source_system: "google-analytics", pull_id: "pull_FIXTURE0000000000000000" },
    ],
    alerts: [],
  },
  data: {
    report_profile: "standard_daily",
    date_range: { start: "2026-04-11", end: "2026-07-09" },
    connectors: ["google-analytics"],
    rows: buildRows(),
    metric_definitions: FIXTURE_METRIC_DEFINITIONS,
  },
};
