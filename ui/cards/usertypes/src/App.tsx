/**
 * App — User types card assembly (Epic 9, Story 9.5).
 *
 * Card answers: "Qui sont mes utilisateurs ?"
 * Composition: kpi_row(active_users,sessions) + donut(by device) + bar(by country)
 *              + table(device breakdown) + comment.
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
  // Shell slot suppressed even when comment block text is empty — CommentBlock empty
  // state renders instead. Shell fallback reserved for backward-compat compositions.
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
