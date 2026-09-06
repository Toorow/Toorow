/**
 * The question that now comes BEFORE "build a visualization" (story 72.5, AC22).
 *
 * WHAT IT REPLACES. Two screens turned a Result into a Visualization —
 * `ResultWorkbench` and `explorer/AnalyticsExplorer` — and both went straight to
 * `createVisualizationFromResult`, which seeded the `table` family because a
 * constant said so. The Chart Template, the object whose entire purpose is to be
 * a validated starting point, was offered at neither. This component is the
 * missing question, asked once and rendered the same way on both screens, because
 * an affordance that differs between two screens is two affordances.
 *
 * ONE QUESTION, AND IT REDUCES THE NEXT. It only ever appears when there is a
 * Result to judge against, and it only ever lists templates the SERVER judged
 * compatible with that exact Result. It never ranks, never explains a refusal and
 * never shows an incompatible template as a greyed choice: the catalogue with its
 * unmet predicates is the Chart Templates lens, and this is the moment of
 * choosing, not of browsing.
 *
 * THE FALLBACK STAYS AND IS NAMED. When the Project has no template that fits,
 * this says so in one sentence — `FALLBACK_REASON` — and the screen's own
 * "start from a table" control remains exactly where it was. A Project with no
 * template must still be able to open a Builder; what changed is that doing so is
 * now an answer rather than the only path.
 *
 * IT DRAWS NOTHING. No preview, no thumbnail: the only pixels a template ever
 * produces come from the shared runtime, in the workbench.
 */
import { useEffect, useState } from "react";

import { Button, Field, NativeSelect, Stack, Status } from "../../ui";
import {
  compatibleTemplatesForResult,
  FALLBACK_REASON,
} from "./seedVisualization";
import type { ChartTemplateRow } from "../templates/chartTemplateClient";

type State =
  | { status: "asking" }
  | { status: "offers"; templates: ChartTemplateRow[] }
  //: The catalogue could not be read. That is NOT "no template fits": one says
  //: nobody knows, the other is an answer, and the screen must not print the
  //: second when it means the first.
  | { status: "unreadable" };

export default function StartingPointChoice({
  projectId,
  resultId,
  busy,
  onStartFromTemplate,
  testId = "starting-point-choice",
}: {
  projectId: string;
  /** Null when no exact Result is on screen; the component then renders nothing. */
  resultId: string | null;
  busy: boolean;
  onStartFromTemplate: (templateVersionId: string) => void;
  testId?: string;
}) {
  const [state, setState] = useState<State>({ status: "asking" });
  const [chosen, setChosen] = useState("");

  useEffect(() => {
    if (!projectId || !resultId) return;
    const controller = new AbortController();
    setState({ status: "asking" });
    setChosen("");
    compatibleTemplatesForResult(projectId, resultId, { signal: controller.signal })
      .then((templates) => setState({ status: "offers", templates }))
      .catch(() => {
        if (!controller.signal.aborted) setState({ status: "unreadable" });
      });
    return () => controller.abort();
  }, [projectId, resultId]);

  if (!resultId) return null;
  if (state.status === "asking") return null;
  if (state.status === "unreadable") {
    return (
      <Status as="inline" tone="neutral" title="The Chart Templates could not be read" data-testid={testId}>
        Which starting points fit this Result is unknown right now, so none is offered. The
        control below still opens a Builder.
      </Status>
    );
  }
  if (state.templates.length === 0) {
    return (
      <Status as="inline" tone="info" title="No Chart Template fits this Result" data-testid={testId}>
        {FALLBACK_REASON}
      </Status>
    );
  }

  return (
    <Stack data-testid={testId}>
      <div className="flex flex-wrap items-end gap-3">
        <Field label="Start from a Chart Template">
          {(field) => (
            <NativeSelect
              {...field}
              value={chosen}
              onChange={(event) => setChosen(event.target.value)}
              data-testid={`${testId}-select`}
            >
              <option value="">Choose a starting point</option>
              {state.templates.map((template) => (
                <option key={template.id} value={template.current_version_id ?? ""}>
                  {template.answers_question || template.label} · {template.family_label}
                </option>
              ))}
            </NativeSelect>
          )}
        </Field>
        <Button
          onClick={() => chosen && onStartFromTemplate(chosen)}
          disabled={busy || !chosen}
          data-testid={`${testId}-start`}
        >
          Start from this template
        </Button>
      </div>
      <span className="text-caption text-text-secondary">
        Applying a template produces an ordinary Visualization of this Project, pinned to the
        question this Result already answered. It re-asks nothing.
      </span>
    </Stack>
  );
}
