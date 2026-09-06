/**
 * Composing a Rule Set version — one door, every family.
 *
 * WHAT WAS MISSING, MEASURED. The Rule Sets lens lists every family a Project
 * carries and, until 2026-08-24, exactly two of them could be written from the
 * console: `tax_fee` through the preset adoption, and `source_currency` through
 * the mapping screen's conflict dialog. The other six rendered a read-only panel
 * whose empty state said "there is no governed setting to read" and named no
 * gesture that would fill it — the same shape as the ladder tab before the
 * adoption existed. Fixing that one family at a time is the defect `CLAUDE.md`
 * names; this component is the repair made once.
 *
 * THIS FILE KNOWS NO FAMILY. Every question, every answer and every consequence
 * comes from the server, declared by the profile beside the validator that
 * refuses a wrong answer. A field list written here would be a second statement
 * of what a version contains, free to disagree with the one that judges it.
 *
 * TWO ACTS, AND THE FIRST ONE CHANGES NOTHING. Composing stores a draft; putting
 * it in force is a separate gesture, through a Change Set. Between them the
 * draft sits on the screen next to the version in force, so the difference is
 * read before it is adopted rather than after.
 *
 * ONE QUESTION AT A TIME — when there is nothing to start from. Composing the
 * FIRST version of a policy reveals the next question once the previous is
 * answered; correcting a published one shows what is in force, all of it,
 * because those questions are already answered and hiding them would ask again.
 *
 * Contract: docs/product-architecture/governance.md, "A Rule Set version is
 * drafted, then published" (2026-08-24).
 */
import { useCallback, useEffect, useState } from "react";

import { ApiError, apiGet, apiPost } from "../lib/apiFetch";
import {
  Badge,
  Button,
  ChoiceGroup,
  Checkbox,
  EmptyState,
  Field,
  Input,
  Panel,
  PanelBody,
  PanelHeader,
  Status,
  notify,
} from "../ui";

export interface AuthoringOption {
  value: string;
  label: string;
  consequence?: string | null;
}

export interface AuthoringField {
  key: string;
  question: string;
  kind: "choice" | "text" | "integer" | "decimal" | "boolean" | "text_list";
  options: AuthoringOption[];
  why?: string | null;
  required: boolean;
  unit?: string | null;
  value: unknown;
}

export interface AuthoringPlan {
  rule_set: {
    id: string;
    label: string;
    family: string;
    profile: string;
    profile_label: string;
  };
  in_force: { version_id: string; version_number: number; rule_count: number } | null;
  draft: { version_id: string; version_number: number; label?: string | null } | null;
  composed_by: string | null;
  fields: AuthoringField[];
  carries_forward: {
    ordered_rule_count: number;
    pinned_references: Array<{ kind: string; object_id: string; version_id: string }>;
  };
}

type PlanState =
  | { status: "loading" }
  | { status: "ready"; plan: AuthoringPlan }
  | { status: "refused"; message: string };

/** The refusal in the server's own words: every one names the gesture that clears it. */
function refusalOf(error: unknown, fallback: string): string {
  if (error instanceof ApiError) {
    const body = (error.body ?? {}) as Record<string, unknown>;
    return String(body.message ?? error.message);
  }
  return fallback;
}

/** The stored answer, as the control that edits it reads it. */
function asText(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (Array.isArray(value)) return value.map((item) => String(item)).join(", ");
  if (typeof value === "boolean") return value ? "true" : "false";
  return String(value);
}

function isAnswered(field: AuthoringField, answers: Record<string, string>): boolean {
  const raw = field.key in answers ? answers[field.key] : asText(field.value);
  // A yes/no question is answered by either answer, so an unchecked box that was
  // never touched is not "unanswered" — a boolean the server left absent is.
  if (field.kind === "boolean") return raw === "true" || raw === "false";
  return raw.trim() !== "";
}

/**
 * How far down the form to show.
 *
 * Composing the first version, the next question appears once the previous is
 * answered. Correcting one in force, everything is visible: those answers exist,
 * and hiding them behind a reveal would ask a person for what they already said.
 */
function visibleFields(
  fields: AuthoringField[],
  answers: Record<string, string>,
  composingFirst: boolean,
): AuthoringField[] {
  if (!composingFirst) return fields;
  const blocked = fields.findIndex(
    (field) => field.required && !isAnswered(field, answers),
  );
  return blocked === -1 ? fields : fields.slice(0, blocked + 1);
}

export default function RuleSetVersionAuthoring({
  projectId,
  ruleSetId,
  onChanged,
}: {
  projectId?: string;
  ruleSetId: string;
  /** Reload the workbench: what is in force may have just changed. */
  onChanged?: () => void;
}) {
  const [state, setState] = useState<PlanState>({ status: "loading" });
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);

  const root = projectId
    ? `/api/projects/${encodeURIComponent(projectId)}/governance/controls-quality/` +
      `rule-sets/${encodeURIComponent(ruleSetId)}/versions`
    : null;

  const load = useCallback(async () => {
    if (!root) return;
    setState({ status: "loading" });
    setAnswers({});
    setFailure(null);
    try {
      setState({ status: "ready", plan: await apiGet<AuthoringPlan>(root) });
    } catch (error) {
      setState({
        status: "refused",
        message: refusalOf(
          error,
          "What can be composed here could not be read. Nothing was changed.",
        ),
      });
    }
  }, [root]);

  useEffect(() => {
    void load();
  }, [load]);

  if (!projectId || state.status === "loading") return null;

  if (state.status === "refused") {
    return (
      <Status as="block" tone="warning" title="This Rule Set cannot be composed here">
        {state.message}
      </Status>
    );
  }

  const { plan } = state;

  // A family whose content is composed by another governed gesture names it. A
  // second editor here would be a second place to write one fact.
  if (plan.composed_by) {
    return (
      <Panel flush>
        <PanelHeader title="Where a version of this Rule Set is composed" />
        <PanelBody>
          <Status as="block" tone="info" title="Not composed on this screen">
            {plan.composed_by}
          </Status>
        </PanelBody>
      </Panel>
    );
  }

  if (plan.fields.length === 0) {
    return (
      <Panel flush>
        <PanelHeader title="Compose a version" />
        <PanelBody>
          <EmptyState
            title="No version of this family can be composed in the console"
            description={`A ${plan.rule_set.family} version is a tree of ordered match rules rather than a policy, and this build offers no gesture that composes one. Nothing here is hidden by a permission: the door does not exist yet.`}
          />
        </PanelBody>
      </Panel>
    );
  }

  const composingFirst = plan.in_force === null;
  const shown = visibleFields(plan.fields, answers, composingFirst);
  const unanswered = plan.fields.filter(
    (field) => field.required && !isAnswered(field, answers),
  );

  const compose = async () => {
    if (!root) return;
    setBusy(true);
    setFailure(null);
    try {
      const body: Record<string, unknown> = {};
      for (const field of plan.fields) {
        body[field.key] = field.key in answers ? answers[field.key] : asText(field.value);
      }
      await apiPost(root, { answers: body });
      notify("Draft composed. Nothing is in force until you publish it.");
      await load();
      onChanged?.();
    } catch (error) {
      setFailure(refusalOf(error, "Nothing was composed."));
    } finally {
      setBusy(false);
    }
  };

  const publish = async () => {
    if (!root || !plan.draft) return;
    setBusy(true);
    setFailure(null);
    try {
      await apiPost(
        `${root}/${encodeURIComponent(plan.draft.version_id)}/publication`,
        {},
        {
          // Stable across retries of THIS gesture, so a double click replays the
          // same change set instead of minting a rival.
          headers: { "Idempotency-Key": `publish-${ruleSetId}-${plan.draft.version_id}` },
        },
      );
      notify(`Version ${plan.draft.version_number} is now in force.`);
      await load();
      onChanged?.();
    } catch (error) {
      setFailure(refusalOf(error, "Nothing was published."));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Panel flush data-testid="rule-set-authoring">
      <PanelHeader
        title={plan.in_force ? "Change this policy" : "Compose the first version"}
        description={
          plan.in_force
            ? `Answering these composes a new ${plan.rule_set.profile_label} version. Nothing changes for this Project until that version is put in force, which is a second gesture.`
            : `Nothing has been published into this Rule Set yet, so this Project has no ${plan.rule_set.profile_label} and every read that needs one is refused rather than guessed. Answering these composes the first version.`
        }
      />
      <PanelBody className="grid gap-4">
        {plan.draft ? (
          <Status
            as="block"
            tone="info"
            title={`Version ${plan.draft.version_number} is composed and NOT in force`}
            data-testid="rule-set-draft-pending"
          >
            <div className="grid gap-3">
              <span>
                {plan.in_force
                  ? `It would replace version ${plan.in_force.version_number}. Until you put it in force, this Project keeps computing with version ${plan.in_force.version_number}.`
                  : "This Project still has no policy of this family until you put this version in force."}
              </span>
              <div>
                <Button type="button" disabled={busy} onClick={() => void publish()}>
                  {busy ? "Putting in force…" : "Put this version in force"}
                </Button>
              </div>
            </div>
          </Status>
        ) : null}

        {/* Stated, not implied: a person about to change a rounding needs to
            know the ladder and the pins survive it. */}
        {plan.carries_forward.ordered_rule_count > 0 ||
        plan.carries_forward.pinned_references.length > 0 ? (
          <div className="flex flex-wrap items-center gap-2 text-caption text-text-secondary">
            <span>Carried into the new version untouched:</span>
            {plan.carries_forward.ordered_rule_count > 0 ? (
              <Badge tone="neutral">{`${plan.carries_forward.ordered_rule_count} published rule${
                plan.carries_forward.ordered_rule_count === 1 ? "" : "s"
              }`}</Badge>
            ) : null}
            {plan.carries_forward.pinned_references.map((pin) => (
              <Badge key={`${pin.kind}:${pin.object_id}`} tone="neutral">
                {`${pin.kind} @ ${pin.version_id}`}
              </Badge>
            ))}
          </div>
        ) : null}

        {shown.map((field) => (
          <AuthoringControl
            key={field.key}
            field={field}
            value={field.key in answers ? answers[field.key] : asText(field.value)}
            onAnswer={(value) => setAnswers((prev) => ({ ...prev, [field.key]: value }))}
          />
        ))}

        {failure ? (
          <Status as="block" tone="error" title="Nothing was changed">
            {failure}
          </Status>
        ) : null}

        <div>
          <Button
            type="button"
            variant="secondary"
            disabled={busy || unanswered.length > 0}
            onClick={() => void compose()}
          >
            {busy ? "Composing…" : "Compose this version"}
          </Button>
        </div>
      </PanelBody>
    </Panel>
  );
}

/**
 * One declared question, rendered as the control its answer shape asks for.
 *
 * A `choice` is a card group because its options ARE the information: what each
 * answer costs is stated where it is chosen, never in a note under the form that
 * nobody reads after deciding.
 */
function AuthoringControl({
  field,
  value,
  onAnswer,
}: {
  field: AuthoringField;
  value: string;
  onAnswer: (value: string) => void;
}) {
  const hint = [field.why, field.unit ? `In ${field.unit}.` : null]
    .filter(Boolean)
    .join(" ");

  if (field.kind === "choice") {
    return (
      <Field label={field.question} hint={hint || undefined} required={field.required}>
        {({ id }) => (
          <ChoiceGroup
            id={id}
            variant="card"
            aria-label={field.question}
            value={value || undefined}
            onValueChange={onAnswer}
            choices={field.options.map((option) => ({
              value: option.value,
              label: option.label,
              hint: option.consequence ?? undefined,
            }))}
          />
        )}
      </Field>
    );
  }

  if (field.kind === "boolean") {
    return (
      <Field label={field.question} hint={hint || undefined} required={field.required}>
        {({ id, ...aria }) => (
          <Checkbox
            id={id}
            {...aria}
            checked={value === "true"}
            onCheckedChange={(next) => onAnswer(next === true ? "true" : "false")}
          />
        )}
      </Field>
    );
  }

  return (
    <Field
      label={field.question}
      hint={
        field.kind === "text_list"
          ? [hint, "One per line is not needed — separate them with commas, in order."]
              .filter(Boolean)
              .join(" ")
          : hint || undefined
      }
      required={field.required}
    >
      {({ id, ...aria }) => (
        <Input
          id={id}
          {...aria}
          inputMode={
            field.kind === "integer" ? "numeric" : field.kind === "decimal" ? "decimal" : undefined
          }
          value={value}
          onChange={(event) => onAnswer(event.target.value)}
        />
      )}
    </Field>
  );
}
