/**
 * Adopting a governed preset into this Project's Tax & Fee ladder.
 *
 * WHAT WAS MISSING, MEASURED. Until 2026-08-17 `grep -rn "ordered_rules"
 * ui/admin/src` returned five hits and every one was a read. The server had been
 * able to publish a ladder version the whole time — a change set of
 * `object_type: "rule-set"` reaches `controls_owner_commands._apply_rule_set` —
 * and nothing had ever created one. So the panel next door said "adopting one is
 * a Change Set prepared by this ladder's owner", and no screen in the product
 * let that owner prepare it.
 *
 * STILL NOT A ONE-CLICK ADOPT. "Never a click on this screen" was about the
 * SHAPE of the act: what is forbidden is turning a shared statutory reference
 * into Project policy by clicking it. Create, prepare, confirm — the same three
 * recorded operations as every other governed change, composed server-side under
 * one idempotency stem, and reached only after every question is answered.
 *
 * ONE QUESTION AT A TIME, and the next appears only once the previous is
 * answered. The questions come from the server, in its order: the postures first
 * because they are answerable from the rule alone, the qualifications after
 * because one of them may send someone to read a contract.
 *
 * THE PLAN IS READ BEFORE ANYTHING IS WRITTEN. `GET .../adoption` returns the
 * ladder that would exist, in order, with the Money Policy version its
 * arithmetic would be composed under. A gesture whose consequence cannot be read
 * first is a gesture people click to find out what it does.
 *
 * Contract: docs/product-architecture/governance.md, "A ladder is adopted where
 * it is read" (2026-08-17).
 */
import { useCallback, useEffect, useState } from "react";

import { ApiError, apiGet, apiPost } from "../lib/apiFetch";
import {
  Badge,
  Button,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Status,
  displayValue,
  notify,
} from "../ui";

interface DecisionOption {
  value: string;
  label: string;
  consequence?: string;
}

interface Decision {
  kind: "posture" | "qualification";
  key: string;
  question: string;
  why?: string;
  options: DecisionOption[];
}

interface AdoptionPlan {
  rule_set: { id: string; label: string; published_rule_count: number };
  proposal: Record<string, unknown>;
  money_policy: { version_id: string; reporting_currency?: string };
  decisions_required: Decision[];
  resulting_rules: Array<Record<string, unknown>>;
}

type PlanState =
  | { status: "loading" }
  | { status: "ready"; plan: AdoptionPlan }
  | { status: "refused"; code: string; message: string };

function refusalOf(error: unknown): { code: string; message: string } {
  if (error instanceof ApiError) {
    const body = (error.body ?? {}) as Record<string, unknown>;
    return {
      code: String(body.code ?? "unavailable"),
      message: String(body.message ?? error.message),
    };
  }
  return {
    code: "unavailable",
    message: "The adoption plan could not be read. Nothing was changed.",
  };
}

export default function LadderAdoptionDialog({
  open,
  projectId,
  ruleSetId,
  presetVersionId,
  presetLabel,
  onClose,
  onAdopted,
}: {
  open: boolean;
  projectId: string;
  ruleSetId: string;
  presetVersionId: string;
  presetLabel: string;
  onClose: () => void;
  onAdopted?: () => void;
}) {
  const [state, setState] = useState<PlanState>({ status: "loading" });
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [adopting, setAdopting] = useState(false);
  const [failure, setFailure] = useState<{ code: string; message: string } | null>(null);

  const load = useCallback(async () => {
    setState({ status: "loading" });
    setAnswers({});
    setFailure(null);
    try {
      const plan = await apiGet<AdoptionPlan>(
        `/api/projects/${encodeURIComponent(projectId)}/governance/controls-quality/` +
          `rule-sets/${encodeURIComponent(ruleSetId)}/adoption` +
          `?preset_version_id=${encodeURIComponent(presetVersionId)}`,
      );
      setState({ status: "ready", plan });
    } catch (error) {
      const refusal = refusalOf(error);
      setState({ status: "refused", ...refusal });
    }
  }, [projectId, ruleSetId, presetVersionId]);

  useEffect(() => {
    if (open) void load();
  }, [open, load]);

  const adopt = async () => {
    if (state.status !== "ready") return;
    setAdopting(true);
    setFailure(null);
    try {
      await apiPost(
        `/api/projects/${encodeURIComponent(projectId)}/governance/controls-quality/` +
          `rule-sets/${encodeURIComponent(ruleSetId)}/adoption`,
        { preset_version_id: presetVersionId, answers },
        {
          // Stable across retries of THIS gesture, so a double click or a lost
          // response replays the same change set instead of minting a rival.
          headers: { "Idempotency-Key": `adopt-${ruleSetId}-${presetVersionId}` },
        },
      );
      notify(`Ladder published: ${presetLabel} is now a rule of this ladder.`);
      onAdopted?.();
      onClose();
    } catch (error) {
      setFailure(refusalOf(error));
    } finally {
      setAdopting(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={(next) => (next ? undefined : onClose())}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>Adopt “{presetLabel}” into this ladder</DialogTitle>
          <DialogDescription>
            This publishes a new version of the ladder through a Change Set: created,
            prepared and confirmed, each with its own audit record.
          </DialogDescription>
        </DialogHeader>

        {state.status === "loading" ? (
          <Status as="block" tone="info" title="Reading what this would publish…" />
        ) : null}

        {state.status === "refused" ? (
          /* Every refusal the server raises names the gesture that clears it, so
             it is rendered as written rather than replaced by a generic one. */
          <Status
            as="block"
            tone="warning"
            title="This preset cannot be adopted"
            data-testid="adoption-refused"
          >
            {state.message}
          </Status>
        ) : null}

        {state.status === "ready" ? (
          <AdoptionForm
            plan={state.plan}
            answers={answers}
            onAnswer={(key, value) => setAnswers((prev) => ({ ...prev, [key]: value }))}
          />
        ) : null}

        {failure ? (
          <Status as="block" tone="error" title="Nothing was published">
            {failure.message}
          </Status>
        ) : null}

        <DialogFooter>
          <Button type="button" variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button
            type="button"
            onClick={() => void adopt()}
            disabled={
              adopting ||
              state.status !== "ready" ||
              state.plan.decisions_required.some((decision) => !answers[decision.key])
            }
          >
            {adopting ? "Publishing…" : "Publish this version"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function AdoptionForm({
  plan,
  answers,
  onAnswer,
}: {
  plan: AdoptionPlan;
  answers: Record<string, string>;
  onAnswer: (key: string, value: string) => void;
}) {
  // One question at a time: the next appears once the previous is answered. A
  // form that shows six at once invites someone to pick the shape of the page
  // rather than the answer to the question.
  const answeredCount = plan.decisions_required.filter(
    (decision) => Boolean(answers[decision.key]),
  ).length;
  const visible = plan.decisions_required.slice(0, answeredCount + 1);

  return (
    <div className="grid gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <Badge outline>{`${plan.resulting_rules.length} rule${plan.resulting_rules.length === 1 ? "" : "s"} after this`}</Badge>
        <Badge outline>{`Money Policy ${displayValue(plan.money_policy.reporting_currency)}`}</Badge>
        <span className="text-caption text-text-secondary">
          pinned at version {plan.money_policy.version_id}
        </span>
      </div>

      {visible.map((decision) => (
        <fieldset
          key={decision.key}
          className="grid gap-2 rounded-medium border border-divider-base p-4"
          data-testid={`decision-${decision.key}`}
        >
          <legend className="px-1 text-ui font-medium">{decision.question}</legend>
          {decision.why ? (
            <p className="m-0 text-caption text-text-secondary">{decision.why}</p>
          ) : null}
          <div className="grid gap-2">
            {decision.options.map((option) => (
              <label key={option.value} className="grid gap-1">
                <span className="flex items-center gap-2 text-ui">
                  <input
                    type="radio"
                    name={decision.key}
                    value={option.value}
                    checked={answers[decision.key] === option.value}
                    onChange={() => onAnswer(decision.key, option.value)}
                  />
                  {option.label}
                </span>
                {/* What it costs, stated where it is chosen — not in a note under
                    the form that nobody reads after deciding. */}
                {option.consequence ? (
                  <span className="pl-6 text-caption text-text-secondary">
                    {option.consequence}
                  </span>
                ) : null}
              </label>
            ))}
          </div>
        </fieldset>
      ))}

      {answeredCount === plan.decisions_required.length ? (
        <Status as="block" tone="info" title="What gets published">
          {`${plan.resulting_rules.length} ordered rule${
            plan.resulting_rules.length === 1 ? "" : "s"
          }, in the ladder “${plan.rule_set.label}”, composed under the Money Policy version above.`}
        </Status>
      ) : null}
    </div>
  );
}
