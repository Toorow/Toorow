/**
 * App — Conversions card assembly (Epic 9, Story 9.4).
 *
 * Renders via CardComposition (maps blocks -> primitives in order). The composition is
 * server-provided (data.composition). CardShell provides the Origin chrome.
 *
 * Card answers: "D'où viennent mes conversions et à quel coût ?"
 * Composition: kpi_row(conversions,cost) + donut(by source) + gauge(CPA vs target)
 *              + table(source detail) + comment.
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
  // must not also render its own rendered-comment slot.
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
