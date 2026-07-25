/**
 * App — KPI card assembly (Epic 9, Story 9.2 + 9.2b + 9.2c).
 *
 * Wraps the KPI hero body in CardShell (@toorow/card-shell), which provides the
 * Origin chrome: title, freshness badge, rendered-comment slot with source affordance,
 * definitions popover (R6), feedback/export chrome (Story 9.2b), slim footer,
 * and (via WidgetShell) light/dark + alerts.
 *
 * Story 9.2c: when data.composition is present, render via CardComposition (maps blocks
 * -> primitives in order). Falls back to KpiCardBody when composition is absent so the
 * card remains backward-compatible with envelopes from before 9.2c.
 *
 * feedbackProps : dérivé de meta.project_id + meta.trace_id + data.card_id.
 * Le CardShell affiche automatiquement le FeedbackBar dans le footer chrome.
 */

import CardShell, { CardComposition } from "@toorow/card-shell";
import type { CardEnvelope } from "@toorow/card-shell";
import KpiCardBody from "./KpiCardBody";

interface AppProps {
  envelope: CardEnvelope;
  adminConsoleUrl?: string;
}

export default function App({ envelope, adminConsoleUrl = "/admin" }: AppProps) {
  const { meta, data } = envelope;

  // Dériver feedbackProps depuis l'enveloppe (Story 9.2b).
  const feedbackProps = {
    projectId: meta.project_id ?? "unknown",
    traceId: meta.trace_id ?? null,
    reportRef: `card:${data.card_id}:${data.date_range.end}`,
    module: `card-${data.card_id}`,
  };

  // Avoid double comment: when the composition renders a comment block, the shell
  // must not also render its own rendered-comment slot.
  const hasCommentBlock = (data.composition ?? []).some((b) => b.type === "comment");

  // Story 9.2c: use CardComposition when the server provides a declared composition.
  // Falls back to KpiCardBody for backward compatibility with older envelopes.
  const body =
    data.composition && data.composition.length > 0 ? (
      <CardComposition blocks={data.composition} data={data} />
    ) : (
      <KpiCardBody
        metrics={data.metrics}
        series={data.series}
        metricDefinitions={data.metric_definitions}
      />
    );

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
      {body}
    </CardShell>
  );
}
