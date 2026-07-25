/**
 * App — Carte Déduplication (Epic 17, Story 17.3).
 *
 * Répond à : « Mes conversions sont-elles comptées en double par les régies ? »
 *
 * RÈGLE AD-9 NON-NÉGOCIABLE :
 *   Chaque chiffre de déduplication est une ESTIMATION — le mot « Estimation »
 *   est visible dans chaque bloc qui porte une valeur dédupliquée.
 *   Jamais présentée comme une mesure exacte.
 *
 * Composition (ordre serveur) :
 *   kpi_row(duplication_rate — héros Estimation)
 *   + bar(dedup_grouped : revendiqué vs dédupliqué par canal)
 *   + table(Canal / Revendiqué / Contribution dédupliquée / Part)
 *   + comment (build_dedup_comment cité : formule + source + estimation verbatim)
 *
 * Dégradés propres :
 *   - kpi_row value=null → état vide honnête « source indisponible » (jamais 0)
 *   - bar series vides → bar-chart-empty
 *   - table rows:[] → data-table-empty
 *   - opt-out (0 lignes) → message guidance « configurez une source »
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
    reportRef: `card:${data.card_id}:${data.date_range?.end ?? ""}`,
    module: `card-${data.card_id}`,
  };

  const blocks = data.composition ?? [];
  // Évite le double commentaire : quand la composition contient un bloc comment,
  // le slot rendered_comment du shell est supprimé (règle F-8).
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
