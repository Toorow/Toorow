/**
 * App — User journey card assembly (Epic 9, Story 9.6).
 *
 * Card answers: "Où les utilisateurs abandonnent-ils dans le parcours ?"
 * Composition: funnel(sessions->active_users->conversions) + kpi_row(sessions,conversions)
 *              + comment.
 *
 * v1 funnel: stage rates derived from sessions -> active_users -> conversions.
 * Richer event-step funnel deferred pending GA4 event-step ingestion (per epic non-goals).
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
    reportRef: `card:${data.card_id}:${data.date_range.end}`,
    module: `card-${data.card_id}`,
  };

  const blocks = data.composition ?? [];
  // Avoid double comment: when the composition renders a comment block, the shell
  // must not also render its own rendered-comment slot (F-8 precedence rule).
  // When the composition includes a comment block, the shell slot is suppressed even
  // if the comment block's text is empty — this is intentional; the CommentBlock
  // empty state renders instead. The shell slot fallback is reserved for backward-compat
  // compositions without a comment block (e.g. the KPI card pre-9.2c).
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
