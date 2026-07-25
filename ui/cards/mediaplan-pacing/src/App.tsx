/**
 * App — Carte Pacing Médiaplan (Epic 22, Story 22.5).
 *
 * Répond à : « Comment mon plan évolue-t-il face aux dépenses réelles
 * (consommé, pace, reste, extrapolé par ligne et par support) ? »
 *
 * RÈGLES AD-9 NON-NÉGOCIABLES :
 *   * pace NULL → affiché « — », JAMAIS 0 %
 *   * lignes plan-only → badge « Plan seul », pas de %
 *   * extrapolated_spend = Estimation (étiquette visible, jamais mesure exacte)
 *   * dépassement (>+10 %) et sous-livraison (<−10 %) signalés
 *     par ICÔNE + TEXTE (jamais par couleur seule — WCAG 2.2 AA)
 *
 * Composition (ordre serveur) :
 *   1. table  — Lignes du plan (label / support / budget / consommé % / pace % / reste / extrapolé)
 *   2. table  — Rollup par support
 *   3. comment — build_mediaplan_pacing_comment cité
 *
 * Dégradés propres :
 *   - table rows:[] → data-table-empty (CardComposition géré par le shell)
 *   - composition vide → carte utilisable sans exception
 */

import CardShell, { CardComposition } from "@toorow/card-shell";
import type { CardEnvelope } from "@toorow/card-shell";

interface AppProps {
  envelope: CardEnvelope;
  adminConsoleUrl?: string;
}

export default function App({ envelope, adminConsoleUrl = "/admin" }: AppProps) {
  const { meta, data } = envelope;

  const feedbackProps = {
    projectId: meta.project_id ?? "unknown",
    traceId: meta.trace_id ?? null,
    reportRef: `card:${data.card_id}:${(data as Record<string, unknown>).plan_id ?? ""}`,
    module: `card-${data.card_id}`,
  };

  const blocks = data.composition ?? [];
  const hasCommentBlock = blocks.some((b) => b.type === "comment");

  return (
    <CardShell
      title={data.title}
      answersQuestion={data.answers_question}
      meta={meta}
      dateRange={data.date_range}
      renderedComment={hasCommentBlock ? undefined : data.rendered_comment}
      metricDefinitions={data.metric_definitions}
      adminConsoleUrl={adminConsoleUrl}
      feedbackProps={feedbackProps}
    >
      <CardComposition blocks={blocks} data={data} />
    </CardShell>
  );
}
