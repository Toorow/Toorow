/**
 * App — Keywords card assembly (Epic 9, Story 9.3).
 *
 * Renders via CardComposition (maps blocks -> primitives in order). The composition is
 * server-provided (data.composition). CardShell provides the Origin chrome: title,
 * freshness badge, rendered-comment slot, definitions popover (R6), feedback/export footer.
 *
 * Card answers: "Comment se comportent mes mots-clés sur la période ?"
 * Composition: kpi_row(clicks,impressions) + bar(top requêtes) + table(query detail) + comment.
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
