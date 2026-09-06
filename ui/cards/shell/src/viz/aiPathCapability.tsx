import type { ReactNode } from "react";

import AiPathFamily, {
  NO_AI_PATH,
  type AiPathSkill,
  type AiPathStep,
} from "./renderers/aiPath";
import {
  allocateAiPathBranchEvidence,
  type AiPathBranchEvidence,
} from "./renderers/aiPathBranches";

export const AI_PATH_CAPABILITY_SCHEMA = "observed-ai-path.v1";
export const MAX_OBSERVED_AI_PATH_STEPS = 200;
export const MAX_OBSERVED_AI_PATH_BYTES = 262_144;

const STEP_KINDS = new Set([
  "tool_call",
  "knowledge_read",
  "skill_step",
  "semantic_query",
  "data_read",
  "handoff",
]);
const OUTCOMES = new Set(["succeeded", "failed", "refused", "unavailable"]);
const SKILL_STATES = new Set([
  "resolved",
  "unavailable",
  "unknown_version",
  "no_sequence",
  "unknown_step",
]);

/** Codes safe to disclose without carrying an explanation or hidden reasoning. */
export const AI_PATH_MISSING_CONTEXT_CODES = [
  "required_but_missing",
  "evidence_unverifiable",
  "not_recorded",
  "unavailable",
] as const;
const MISSING_CONTEXT_CODES = new Set<string>(AI_PATH_MISSING_CONTEXT_CODES);

type JsonRecord = Record<string, unknown>;

export interface ObservedAiPathOwner {
  workspace: string;
  object_type: string;
  object_id: string;
  version_id?: string;
  /** The owner's own name, when the product knows it (2026-09-05). */
  label?: string;
}

export interface ObservedAiPathSkill {
  version_id: string;
  step_id: string;
  state: string;
  label?: string;
}

export interface ObservedAiPathStep {
  ordinal: number;
  step_kind: string;
  outcome: string;
  observed_at?: string;
  tool_name?: string;
  owner?: ObservedAiPathOwner;
  skill?: ObservedAiPathSkill;
  evidence_record_id?: string;
  missing_context?: string[];
  branch_evidence?: AiPathBranchEvidence;
  /** What the call chose: bounded ids and short values, as the owner recorded them. */
  chose?: Record<string, string | number | boolean | null | Array<string | number | boolean | null>>;
}

export type ObservedAiPathProjection =
  | {
      schema_version: typeof AI_PATH_CAPABILITY_SCHEMA;
      state: "human_absent";
      literal: typeof NO_AI_PATH;
    }
  | {
      schema_version: typeof AI_PATH_CAPABILITY_SCHEMA;
      state: "unavailable";
      reason: "unavailable" | "evidence_not_frozen";
    }
  | {
      schema_version: typeof AI_PATH_CAPABILITY_SCHEMA;
      state: "completed" | "failed";
      path_id: string;
      lifecycle: "finalized";
      outcome: string;
      steps: ObservedAiPathStep[];
    };

export type AiPathCapabilitySurface = "inline" | "share" | "fullscreen" | "console";

export interface AiPathCapabilityProps {
  projection: unknown;
  surface: AiPathCapabilitySurface;
  renderOwner?: (step: AiPathStep, index: number) => ReactNode;
  onFeedbackTarget?: (target: { kind: "path_step"; ordinal: number }, label: string) => void;
}

function malformed(message: string): never {
  throw new Error(`Malformed observed AI Path: ${message}`);
}

function serializedBytes(value: unknown): number {
  try {
    return new TextEncoder().encode(JSON.stringify(value)).byteLength;
  } catch {
    return Number.POSITIVE_INFINITY;
  }
}

function record(value: unknown, name: string): JsonRecord {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    malformed(`${name} must be an object`);
  }
  return value as JsonRecord;
}

function exactKeys(value: JsonRecord, allowed: readonly string[], name: string): void {
  const accepted = new Set(allowed);
  const unexpected = Object.keys(value).filter((key) => !accepted.has(key));
  if (unexpected.length > 0) malformed(`${name} has unexpected key ${unexpected[0]}`);
}

function identifier(value: unknown, name: string, max = 200): asserts value is string {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value.length > max ||
    !/^[A-Za-z0-9][A-Za-z0-9_.:@/-]*$/.test(value)
  ) {
    malformed(`${name} is not a safe bounded identifier`);
  }
}

function label(value: unknown, name: string): asserts value is string {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value.length > 200 ||
    value.trim() !== value ||
    /[\u0000-\u001f\u007f]/.test(value)
  ) {
    malformed(`${name} is not safe bounded text`);
  }
}

function optionalIdentifier(value: unknown, name: string): void {
  if (value !== undefined) identifier(value, name);
}

function decodeOwner(value: unknown): void {
  const owner = record(value, "step owner");
  exactKeys(owner, ["workspace", "object_type", "object_id", "version_id", "label"], "step owner");
  identifier(owner.workspace, "owner workspace", 64);
  identifier(owner.object_type, "owner object type", 64);
  identifier(owner.object_id, "owner object id");
  optionalIdentifier(owner.version_id, "owner version id");
  if (owner.label !== undefined) {
    label(owner.label, "owner label");
    if (owner.label.length > 120) malformed("owner label is too long");
  }
}

const CHOICE_MAX_KEYS = 12;
const CHOICE_MAX_ITEMS = 12;
const CHOICE_MAX_CHARS = 120;

function choiceScalar(value: unknown): boolean {
  return (
    value === null ||
    typeof value === "boolean" ||
    (typeof value === "number" && Number.isFinite(value)) ||
    (typeof value === "string" && value.length <= CHOICE_MAX_CHARS)
  );
}

/** A choice is a handful of dotted keys with ids, short values or lists of them -- never prose, never nesting. */
function decodeChoice(value: unknown): void {
  const chose = record(value, "step chose");
  const keys = Object.keys(chose);
  if (keys.length > CHOICE_MAX_KEYS) malformed("step chose carries too many keys");
  for (const key of keys) {
    if (key.length === 0 || key.length > 80) malformed("step chose key is malformed");
    const entry = chose[key];
    if (Array.isArray(entry)) {
      if (entry.length > CHOICE_MAX_ITEMS || !entry.every(choiceScalar)) malformed("step chose list is malformed");
    } else if (!choiceScalar(entry)) {
      malformed("step chose value is malformed");
    }
  }
}

function decodeSkill(value: unknown): void {
  const skill = record(value, "step skill");
  exactKeys(skill, ["version_id", "step_id", "state", "label"], "step skill");
  identifier(skill.version_id, "skill version id");
  identifier(skill.step_id, "skill step id");
  if (typeof skill.state !== "string" || !SKILL_STATES.has(skill.state)) {
    malformed("skill state is not supported");
  }
  if (skill.label !== undefined) label(skill.label, "skill label");
}

function decodeStep(value: unknown): ObservedAiPathStep {
  const step = record(value, "step");
  exactKeys(
    step,
    [
      "ordinal",
      "step_kind",
      "outcome",
      "observed_at",
      "tool_name",
      "owner",
      "skill",
      "evidence_record_id",
      "missing_context",
      "branch_evidence",
      "chose",
    ],
    "step",
  );
  if (step.chose !== undefined) decodeChoice(step.chose);
  if (!Number.isSafeInteger(step.ordinal) || (step.ordinal as number) < 0) {
    malformed("step ordinal must be a non-negative integer");
  }
  if (typeof step.step_kind !== "string" || !STEP_KINDS.has(step.step_kind)) {
    malformed("step kind is not supported");
  }
  if (typeof step.outcome !== "string" || !OUTCOMES.has(step.outcome)) {
    malformed("step outcome is not supported");
  }
  if (step.observed_at !== undefined) {
    label(step.observed_at, "step observed_at");
    if (
      step.observed_at.length > 64 ||
      !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(
        step.observed_at,
      )
    ) {
      malformed("step observed_at is not a timezone-qualified RFC3339 timestamp");
    }
  }
  optionalIdentifier(step.tool_name, "step tool name");
  if (step.owner !== undefined) decodeOwner(step.owner);
  if (step.skill !== undefined) decodeSkill(step.skill);
  optionalIdentifier(step.evidence_record_id, "step evidence record id");
  if (step.missing_context !== undefined) {
    if (
      !Array.isArray(step.missing_context) ||
      step.missing_context.length > 20 ||
      new Set(step.missing_context).size !== step.missing_context.length ||
      step.missing_context.some(
        (code) => typeof code !== "string" || !MISSING_CONTEXT_CODES.has(code),
      )
    ) {
      malformed("step missing context must contain only safe unique codes");
    }
  }
  const branchEligible =
    step.step_kind === "knowledge_read" &&
    (step.tool_name === "search_context" || step.tool_name === "briefing_context_event");
  if (branchEligible !== (step.branch_evidence !== undefined)) {
    malformed("branch evidence presence contradicts retrieval eligibility");
  }
  return step as unknown as ObservedAiPathStep;
}

/** Strictly decode the single safe projection produced for every surface. */
export function decodeAiPathCapability(value: unknown): ObservedAiPathProjection {
  if (serializedBytes(value) > MAX_OBSERVED_AI_PATH_BYTES) {
    malformed(`projection exceeds the ${MAX_OBSERVED_AI_PATH_BYTES} byte bound`);
  }
  const projection = record(value, "projection");
  if (projection.schema_version !== AI_PATH_CAPABILITY_SCHEMA) {
    malformed(`schema_version must be ${AI_PATH_CAPABILITY_SCHEMA}`);
  }
  if (projection.state === "human_absent") {
    exactKeys(projection, ["schema_version", "state", "literal"], "projection");
    if (projection.literal !== NO_AI_PATH) malformed(`literal must be ${NO_AI_PATH}`);
    return projection as unknown as ObservedAiPathProjection;
  }
  if (projection.state === "unavailable") {
    exactKeys(projection, ["schema_version", "state", "reason"], "projection");
    if (projection.reason !== "unavailable" && projection.reason !== "evidence_not_frozen") {
      malformed("unavailable reason is not supported");
    }
    return projection as unknown as ObservedAiPathProjection;
  }
  if (projection.state !== "completed" && projection.state !== "failed") {
    malformed("state is not supported");
  }
  exactKeys(
    projection,
    ["schema_version", "state", "path_id", "lifecycle", "outcome", "steps"],
    "projection",
  );
  identifier(projection.path_id, "path id");
  if (projection.lifecycle !== "finalized") malformed("lifecycle must be finalized");
  if (typeof projection.outcome !== "string" || !OUTCOMES.has(projection.outcome)) {
    malformed("path outcome is not supported");
  }
  if (
    (projection.state === "completed" && projection.outcome !== "succeeded") ||
    (projection.state === "failed" && projection.outcome === "succeeded")
  ) {
    malformed("state contradicts the raw path outcome");
  }
  if (!Array.isArray(projection.steps) || projection.steps.length > MAX_OBSERVED_AI_PATH_STEPS) {
    malformed(`steps exceed the ${MAX_OBSERVED_AI_PATH_STEPS} step bound`);
  }
  const steps = projection.steps.map(decodeStep);
  for (let index = 1; index < steps.length; index += 1) {
    if (steps[index]!.ordinal <= steps[index - 1]!.ordinal) {
      malformed("step order must be strictly increasing");
    }
  }
  const allocated = allocateAiPathBranchEvidence(
    steps.map((step) => ({
      ordinal: step.ordinal,
      eligible:
        step.step_kind === "knowledge_read" &&
        (step.tool_name === "search_context" || step.tool_name === "briefing_context_event"),
      evidence: step.branch_evidence,
      producer:
        step.tool_name === "search_context"
          ? "context_search"
          : step.tool_name === "briefing_context_event"
            ? "briefing_context_event"
            : undefined,
    })),
  );
  projection.steps = steps.map((step, index) => {
    const projectedStep = { ...step };
    if (allocated[index]) projectedStep.branch_evidence = allocated[index];
    else delete projectedStep.branch_evidence;
    return projectedStep;
  });
  return projection as unknown as ObservedAiPathProjection;
}

function rendererSteps(steps: ObservedAiPathStep[]): AiPathStep[] {
  return steps.map((step) => ({
    ordinal: step.ordinal,
    step_kind: step.step_kind,
    outcome: step.outcome,
    observed_at: step.observed_at,
    tool_name: step.tool_name,
    owner_workspace: step.owner?.workspace,
    owner_object_type: step.owner?.object_type,
    owner_object_id: step.owner?.object_id,
    owner_version_id: step.owner?.version_id,
    owner_label: step.owner?.label,
    chose: step.chose,
    skill: step.skill as AiPathSkill | undefined,
    evidence_record_id: step.evidence_record_id,
    missing_context: step.missing_context,
    branch_evidence: step.branch_evidence,
  }));
}

function summary(projection: ObservedAiPathProjection): string {
  if (projection.state === "human_absent") return `AI Path · ${NO_AI_PATH}`;
  if (projection.state === "unavailable") {
    return projection.reason === "evidence_not_frozen"
      ? "AI Path · evidence not frozen"
      : "AI Path · unavailable";
  }
  const count = projection.steps.length;
  return `AI Path · ${projection.outcome} · ${count} observed ${count === 1 ? "step" : "steps"}`;
}

const UNAVAILABLE_PROJECTION: ObservedAiPathProjection = Object.freeze({
  schema_version: AI_PATH_CAPABILITY_SCHEMA,
  state: "unavailable",
  reason: "unavailable",
});

function safeProjection(value: unknown): ObservedAiPathProjection {
  try {
    return decodeAiPathCapability(value);
  } catch {
    return UNAVAILABLE_PROJECTION;
  }
}

function disclosureKey(projection: ObservedAiPathProjection): string {
  if (projection.state === "human_absent") return "human_absent";
  if (projection.state === "unavailable") return `unavailable:${projection.reason}`;
  return `${projection.state}:${projection.path_id}`;
}

export default function AiPathCapability({
  projection: input,
  surface,
  renderOwner,
  onFeedbackTarget,
}: AiPathCapabilityProps) {
  const projection = safeProjection(input);
  const expanded = surface === "console";
  return (
    <details
      key={disclosureKey(projection)}
      open={expanded}
      data-ai-path-capability={projection.state}
    >
      <summary className="cursor-pointer text-sm font-medium">{summary(projection)}</summary>
      <div className="mt-3">
        {projection.state === "human_absent" ? (
          <AiPathFamily steps={[]} humanOnly />
        ) : projection.state === "unavailable" ? (
          <div role="status" className="flex flex-col gap-1 text-sm">
            {projection.reason === "evidence_not_frozen" ? (
              <p className="m-0 font-medium">
                AI Path evidence was not frozen with this shared Result.
              </p>
            ) : (
              <>
                <p className="m-0 font-medium">This AI Path is unavailable here.</p>
                <p className="m-0 opacity-70">
                  Open the Result in an authorized Toorow workspace.
                </p>
              </>
            )}
          </div>
        ) : (
          <div className="flex flex-col gap-3">
            <p className="m-0 text-sm" data-ai-path-capability-identity>
              Path {projection.path_id} · {projection.outcome}
            </p>
            <AiPathFamily
              steps={rendererSteps(projection.steps)}
              lifecycle={projection.lifecycle}
              renderOwner={renderOwner}
              /* ONE control per step. The family draws each step on the
                 timeline AND in the accessible table fallback, so a slot that
                 returned the button for both sites mounted two buttons with the
                 same `data-feedback-path-step` for one step. The table site
                 states the step in text instead -- the fallback stays
                 informative, and the affordance is offered once. */
              renderStepAction={onFeedbackTarget ? (step, _index, site) => (
                site === "timeline" ? (
                  <button
                    type="button"
                    data-feedback-path-step={step.ordinal}
                    className="underline underline-offset-2"
                    onClick={() => onFeedbackTarget(
                      { kind: "path_step", ordinal: step.ordinal as number },
                      `AI Path step ${(step.ordinal as number) + 1}`,
                    )}
                  >
                    Target this step
                  </button>
                ) : (
                  <span className="opacity-70">
                    Targetable on the timeline above
                  </span>
                )
              ) : undefined}
            />
          </div>
        )}
      </div>
    </details>
  );
}
