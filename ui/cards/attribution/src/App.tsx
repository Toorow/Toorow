/**
 * App — Carte Attribution (Epic 16, Story 16.3).
 *
 * Répond à : « Quelle part de mes conversions vient de chaque canal,
 * en dernier clic vs premier clic ? »
 *
 * Composition (ordre serveur) :
 *   kpi_row(conversions — partition session_source_medium)
 *   + bar(Canaux — dernier clic, session_source_medium)
 *   + bar(Canaux — premier clic, first_user_source_medium)
 *   + table(Campagnes — dernier clic, session_campaign, Part top-N)
 *   + comment (build_attribution_comment cité, AD-9)
 *
 * Dégradé propre : chaque bloc dégrade en état vide designé quand les
 * données d'acquisition sont absentes (datastream OFF ou non encore ingéré).
 * La carte est TOUJOURS rendable — jamais d'erreur (règle e épique 9).
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
