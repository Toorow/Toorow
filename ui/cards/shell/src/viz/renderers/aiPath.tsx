/**
 * Story 55.1 -- the `ai_path` visual family, drawn ONCE for every surface.
 *
 * WHY IT LIVES HERE. The path was already drawn twice: by
 * `ui/admin/src/connaissances/AiPathPage.tsx` (Story 49.6) and by the `ai-path`
 * lens of `ui/admin/src/analyze/ResultWorkbench.tsx` (Story 50.2). Two drawings
 * of the same evidence answer differently the first time one of them is fixed.
 * `visualization-and-rendering.md:167` puts the AI Path in the shared registry
 * "under the same registry and evidence rules"; AD-35 names `ui/cards/shell` as
 * the shared runtime the console migrates with. So this file is the drawing, and
 * both screens became consumers of it in the same commit that created it.
 *
 * NO MUI, NO STYLESHEET, NO HEX (AD-35 / CLAUDE.md §5). Plain HTML + Tailwind
 * utility classes; every colour comes from the `VizPalette` the caller obtained
 * through `getVizPalette` / `readCssVizTheme`, which is the single injection
 * point for org branding.
 *
 * IT IS NOT RESOLVED THROUGH `resolveRenderer`, AND THAT IS THE HONEST STATE.
 * A registered renderer receives a `CompiledVisualModel` compiled from a Result
 * and a Visualization Spec. An AI Path is neither: its data is the path record
 * `core.ai_paths` holds, and `server/core/visualization_families.py` declares the
 * family with a rung well that accepts only `classification` -- a role with no
 * server source -- precisely so no Visualization Spec can ask for it. The family
 * therefore stays listed in `registry.ts`'s `KNOWN_UNBUILT_FAMILIES`, with a
 * message that says what is true: the drawing exists, the SPEC ROUTE to it does
 * not. This is the same boundary `evidencePath.tsx` documents.
 *
 * TWO NODE STATES, AND NOT THREE. `core.ai_path_recorder.STEP_STATES` is
 * `("started", "observed")`: there is no `pending` and no `active`, by
 * construction -- a step is emitted because it happened, never to announce that
 * it is about to. So this component draws `completed` and `failed`, and SAYS SO
 * on screen rather than animating a progression it never observed. Reinstating a
 * third state here would be the same class of defect as one screen for a healthy
 * feed and a silent one.
 */

import { useMemo, type ComponentType, type ReactNode } from "react";

import { getVizPalette, readCssVizTheme, type VizPalette } from "../../vizTheme";
import AiPathBranchSubtree, {
  allocateAiPathBranchEvidence,
  aiPathBranchesText,
  type AiPathBranchEvidence,
  type AiPathInspectionSink,
} from "./aiPathBranches";

// ---------------------------------------------------------------------------
// The reading grid. ONE definition site is `core.ai_path_recorder.level_of`;
// this is its TypeScript mirror, and `__tests__/aiPath.test.tsx` reads the Python
// source and fails on drift rather than letting the two vocabularies part ways.
// Nothing is persisted under these names: `app.ai_path_steps.step_kind` keeps its
// six kinds (migration 150).
// ---------------------------------------------------------------------------

export const AI_PATH_LEVELS = ["JOB", "SKILL", "PROCEDURE", "CONTEXT", "TOOL"] as const;
export type AiPathLevel = (typeof AI_PATH_LEVELS)[number];

const PROCEDURE_OBJECT_TYPES = new Set(["procedure"]);
const PROCEDURE_TOOL_NAMES = new Set(["get_procedure"]);
const CONTEXT_TOOL_NAMES = new Set(["search_context"]);
const CONTEXT_STEP_KINDS = new Set(["knowledge_read", "semantic_query"]);
const TOOL_STEP_KINDS = new Set(["tool_call", "data_read"]);

/** Human labels for the rungs. Capitalized once, so no caller invents a synonym. */
export const AI_PATH_LEVEL_LABELS: Record<AiPathLevel, string> = {
  JOB: "Job",
  SKILL: "Skill",
  PROCEDURE: "Procedure",
  CONTEXT: "Context",
  TOOL: "Tool",
};

/**
 * The two states a drawn node can be in. `core.ai_path_recorder` emits no other,
 * so no other is drawn. `AI_PATH_NODE_STATES.length === 2` is asserted by test:
 * a third entry added without a source would be theatre.
 */
export const AI_PATH_NODE_STATES = ["completed", "failed"] as const;
export type AiPathNodeState = (typeof AI_PATH_NODE_STATES)[number];

/** English, stated on screen, because the reduction is a fact about the data. */
export const AI_PATH_STATE_STATEMENT =
  "Two node states are drawn, completed and failed, because that is all the record " +
  "carries. No step is shown as pending or active: this is a walk that already " +
  "happened, not one being watched.";

/** The literal a human-only result renders (`analyze-and-test.md:113`). */
export const NO_AI_PATH = "No AI path";

export interface AiPathStep {
  id?: string | null;
  /**
   * The step's own order in the walk, exactly as `app.ai_path_steps.ordinal`
   * holds it -- therefore ZERO-BASED (`append_step` allocates
   * `COALESCE(MAX(ordinal) + 1, 0)`).
   *
   * This is an IDENTITY, not a label. `evidence_inspections.step_ordinal` is
   * joined against it by `idx_evidence_inspections_path`, so whatever is
   * recorded must be this number and nothing else. Use `displayOrdinal()` to
   * put a position in front of a human, and `identityOrdinal()` to record one.
   * Conflating the two is what shifted every inspection by one (AI-134).
   */
  ordinal?: number | null;
  step_kind?: string | null;
  tool_name?: string | null;
  outcome?: string | null;
  owner_workspace?: string | null;
  owner_object_type?: string | null;
  owner_object_id?: string | null;
  owner_version_id?: string | null;
  /** The owner's own name (the View's label, the field's canonical name), when the
   *  product knows it. Optional: an absent name is never invented (2026-09-05). */
  owner_label?: string | null;
  /** What the call chose: the arguments' ids and short values, as recorded. */
  chose?: Record<string, unknown> | null;
  /** The path this step belongs to, when several paths are read as one interaction. */
  path_id?: string | null;
  /** The safe, resolved Skill-step facts projected by the path owner. */
  skill?: AiPathSkill | null;
  evidence_record_id?: string | null;
  /** Safe codes only. Explanatory prose never belongs in this projection. */
  missing_context?: string[] | null;
  /**
   * When the crossing happened, as the owner recorded it
   * (`app.ai_path_steps.observed_at`). Optional because not every reader of the
   * record carries it -- the console's per-path route does not project it today
   * -- and an invented timestamp is worse than none.
   */
  observed_at?: string | null;
  /** Closed, bounded public evidence. It exists only on eligible retrieval steps. */
  branch_evidence?: AiPathBranchEvidence | null;
}

export interface AiPathSkill {
  version_id: string;
  step_id: string;
  state: string;
  label?: string;
}

/**
 * A step as the WIRE carries it: the owner's projection
 * (`core.ai_paths.wire_step_projection`), served by the console's per-path
 * route and by the render tool's app channel. Same record, plus the owner's
 * own column name for the walk order and the judged-branches detail.
 */
export interface AiPathWireSkill {
  skill_version_id: string;
  skill_step_id: string;
  state: string;
  label?: string;
}

export interface AiPathWireStep extends Omit<AiPathStep, "skill" | "branch_evidence"> {
  /** The owner's own column name for the walk order (the API projects `ordinal`). */
  step_order?: number | null;
  skill_version_id?: string | null;
  /** Legacy owner wire shape; normalized once by `aiPathStepsFromWire`. */
  skill?: AiPathWireSkill | null;
  evidence_record_id?: string | null;
  /** The server's closed public projection. Producer detail is never a UI input. */
  branch_evidence?: unknown;
}

/**
 * The ONE wire-to-renderer mapping. The console's `AiPathPage` and the MCP App
 * entry both consume the same wire projection; two local mappings would answer
 * differently the first time one of them was fixed. The step's own ordinal is
 * passed through UNSHIFTED -- what a human sees (`displayOrdinal`) and what an
 * inspection records (`identityOrdinal`) are decided by the family; adding one
 * here put the two in conflict and made every recorded inspection designate
 * the previous step (AI-134).
 */
export function aiPathStepsFromWire(wire: AiPathWireStep[] | null | undefined): AiPathStep[] {
  const values = wire ?? [];
  const normalized = values.map((step, index): AiPathStep => ({
      id: step.id,
      ordinal: step.step_order ?? index,
      step_kind: step.step_kind,
      tool_name: step.tool_name,
      outcome: step.outcome,
      owner_workspace: step.owner_workspace,
      owner_object_type: step.owner_object_type,
      owner_object_id: step.owner_object_id,
      owner_version_id: step.owner_version_id,
      owner_label: step.owner_label,
      chose: step.chose,
      path_id: step.path_id,
      evidence_record_id: step.evidence_record_id,
      missing_context: step.missing_context,
      observed_at: step.observed_at,
      skill: step.skill
        ? {
            version_id: step.skill.skill_version_id,
            step_id: step.skill.skill_step_id,
            state: step.skill.state,
            label: step.skill.label,
          }
        : undefined,
    }));
  const allocated = allocateAiPathBranchEvidence(
    values.map((step, index) => {
      const producer =
        step.step_kind === "knowledge_read" && step.tool_name === "search_context"
          ? "context_search"
          : step.step_kind === "knowledge_read" && step.tool_name === "briefing_context_event"
            ? "briefing_context_event"
            : undefined;
      return {
        ordinal: normalized[index]!.ordinal as number,
        eligible: producer !== undefined,
        evidence: step.branch_evidence,
        producer,
      };
    }),
  );
  return normalized.map((step, index) => ({ ...step, branch_evidence: allocated[index] }));
}

/**
 * The rung one step sits on, or `null` when the kind names no rung.
 *
 * Mirrors `core.ai_path_recorder.level_of` clause for clause, INCLUDING the
 * order: `PROCEDURE` and `CONTEXT` share a `step_kind` (a `get_procedure` is a
 * `tool_call`), so the tool name and the reached object type are what separate
 * them. `handoff` is the standing `null`: a recorded kind that names no rung.
 */
export function levelOfStep(step: AiPathStep): AiPathLevel | null {
  const kind = step.step_kind ?? "";
  if (!kind) return null;
  if (kind === "skill_step") return "SKILL";
  if (
    PROCEDURE_OBJECT_TYPES.has(step.owner_object_type ?? "") ||
    PROCEDURE_TOOL_NAMES.has(step.tool_name ?? "")
  ) {
    return "PROCEDURE";
  }
  if (CONTEXT_STEP_KINDS.has(kind) || CONTEXT_TOOL_NAMES.has(step.tool_name ?? "")) {
    return "CONTEXT";
  }
  if (TOOL_STEP_KINDS.has(kind)) return "TOOL";
  return null;
}

/**
 * `succeeded` is the only outcome that completes. `failed`, `refused` and
 * `unavailable` (`core.ai_paths.OUTCOMES`) are all drawn as `failed`: they are
 * distinguished in the table fallback by their own word, never merged there.
 */
export function nodeStateOfStep(step: AiPathStep): AiPathNodeState {
  return step.outcome === "succeeded" ? "completed" : "failed";
}

/** A step that reached nothing governed. It stays drawn -- see AC8. */
export function reachedNothingGoverned(step: AiPathStep): boolean {
  return !step.owner_object_id;
}

// ---------------------------------------------------------------------------
// The timeline markers. One named glyph per recorded `step_kind`, so a kind is
// told apart by its icon AND its printed name, never by colour alone. Inline
// SVG on purpose: this package takes no icon dependency (AD-35 -- plain HTML +
// Tailwind, colours only from the palette), and `currentColor` keeps it that
// way. An unknown kind gets the dot, never a crash -- the same refusal
// `SkillStepList` documents for a seventh action.
// ---------------------------------------------------------------------------

function marker(children: ReactNode): ComponentType<{ className?: string }> {
  return function Marker({ className }: { className?: string }) {
    return (
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth={2}
        strokeLinecap="round"
        strokeLinejoin="round"
        className={className}
        aria-hidden
      >
        {children}
      </svg>
    );
  };
}

const KIND_MARKER: Record<string, ComponentType<{ className?: string }>> = {
  tool_call: marker(
    <path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z" />,
  ),
  knowledge_read: marker(
    <>
      <path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z" />
      <path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z" />
    </>,
  ),
  skill_step: marker(
    <>
      <path d="m12.83 2.18a2 2 0 0 0-1.66 0L2.6 6.08a1 1 0 0 0 0 1.83l8.58 3.91a2 2 0 0 0 1.66 0l8.58-3.9a1 1 0 0 0 0-1.83Z" />
      <path d="m22 17.65-9.17 4.16a2 2 0 0 1-1.66 0L2 17.65" />
      <path d="m22 12.65-9.17 4.16a2 2 0 0 1-1.66 0L2 12.65" />
    </>,
  ),
  semantic_query: marker(
    <>
      <circle cx={11} cy={11} r={8} />
      <path d="m21 21-4.3-4.3" />
    </>,
  ),
  data_read: marker(
    <>
      <ellipse cx={12} cy={5} rx={9} ry={3} />
      <path d="M3 5v14a9 3 0 0 0 18 0V5" />
      <path d="M3 12a9 3 0 0 0 18 0" />
    </>,
  ),
  handoff: marker(
    <>
      <path d="M5 12h14" />
      <path d="m12 5 7 7-7 7" />
    </>,
  ),
};

const JOB_MARKER = marker(
  <>
    <path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z" />
    <path d="M4 22v-7" />
  </>,
);

const UNKNOWN_MARKER = marker(<circle cx={12} cy={12} r={4} />);

// ---------------------------------------------------------------------------
// The accessible table fallback -- derived from the family's declared
// `table_fallback_wells`, not from what looked convenient here.
// ---------------------------------------------------------------------------

/**
 * `server/core/visualization_families.py` declares
 * `table_fallback_wells=("dimension", "label", "detail", "color")` for `ai_path`.
 * The order and the well names below ARE that tuple; the test reads the Python
 * source and compares, so a change on either side is red rather than silent.
 */
export const AI_PATH_FALLBACK_COLUMNS = [
  { well: "dimension", header: "Level" },
  { well: "label", header: "Tool" },
  { well: "detail", header: "Reached" },
  { well: "color", header: "Outcome" },
] as const;

/**
 * The number to RECORD. Zero-based, because it must equal
 * `app.ai_path_steps.ordinal` for `evidence_inspections.step_ordinal` to join.
 * The array index is the honest fallback: the API returns steps in walk order,
 * so index and ordinal coincide when the ordinal is missing.
 */
function identityOrdinal(step: AiPathStep, index: number): number {
  return step.ordinal ?? index;
}

/**
 * The number to SHOW. One-based, because a list that starts at 0 reads as a
 * defect to everyone who is not holding the schema. Never record this value --
 * that is the whole of AI-134.
 */
function displayOrdinal(step: AiPathStep, index: number): number {
  return identityOrdinal(step, index) + 1;
}

const DASH = "—";
const NOTHING_GOVERNED = "nothing governed";
const NO_RUNG = "No rung";
const CHOICE_KEYS_SHOWN = 4;

/** « chose family=table, request.pivot.rows=mdm_…, +3 more » -- values only, bounded, no markup. */
export function describeChoice(chose: Record<string, unknown> | null | undefined): string | null {
  if (!chose || typeof chose !== "object") return null;
  const entries = Object.entries(chose).filter(([key]) => key !== "…");
  if (entries.length === 0) {
    // Only the count survived the byte budget (round 4, F4): say so, never « nothing chosen ».
    const leftOut = leftOutCount(chose["…"]);
    return leftOut > 0 ? `chose ${leftOut} value${leftOut > 1 ? "s" : ""} not shown` : null;
  }
  const shown = entries.slice(0, CHOICE_KEYS_SHOWN).map(([key, value]) => {
    const text = Array.isArray(value) ? value.map(String).join(", ") : String(value);
    return `${key}=${text.length > 60 ? `${text.slice(0, 57)}…` : text}`;
  });
  // THE COUNT IS THE WHOLE COUNT (Opus round 3, N5): the server's `…` entry says how
  // many keys it left out; « +1 more » where there were six understated the choice.
  const rest = entries.length - shown.length + leftOutCount(chose["…"]);
  return `chose ${shown.join(", ")}${rest > 0 ? `, +${rest} more` : ""}`;
}

/** The server's `…` entry reads « 5 more »; anything else counts for nothing. */
function leftOutCount(marker: unknown): number {
  if (typeof marker !== "string") return 0;
  const parsed = Number.parseInt(marker, 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 0;
}

/** One fallback row per step, in walk order. Values only, no markup. */
export function aiPathFallbackRows(
  steps: AiPathStep[],
): { ordinal: string; dimension: string; label: string; detail: string; color: string }[] {
  return steps.map((step, index) => {
    const level = levelOfStep(step);
    const skill = step.skill
      ? [
          `Skill ${step.skill.version_id} step ${step.skill.step_id}`,
          step.skill.state,
          step.skill.label,
        ]
          .filter(Boolean)
          .join(" · ")
      : null;
    const owner = reachedNothingGoverned(step)
      ? NOTHING_GOVERNED
      : [
          // THE NAME FIRST, THE ID AFTER IT: a reader recognises « Kardinal crossing »,
          // not `sv_01M1QP0Z…`; the id stays because it is what an evidence link
          // and a search need (2026-09-05).
          step.owner_label
            ? `${step.owner_object_type ?? ""} ${step.owner_label} (${step.owner_object_id ?? ""})`.trim()
            : [step.owner_workspace, step.owner_object_type, step.owner_object_id]
                .filter(Boolean)
                .join(" "),
          step.owner_version_id ? `version ${step.owner_version_id}` : null,
        ]
          .filter(Boolean)
          .join(" · ");
    const detail = [
      owner,
      step.observed_at,
      step.evidence_record_id ? `evidence ${step.evidence_record_id}` : null,
      step.missing_context?.length
        ? `missing context ${step.missing_context.join(", ")}`
        : null,
      describeChoice(step.chose),
      step.path_id ? `path ${step.path_id}` : null,
    ]
      .filter(Boolean)
      .join(" · ");
    return {
      ordinal: String(displayOrdinal(step, index)),
      dimension: `${level ? AI_PATH_LEVEL_LABELS[level] : NO_RUNG} · ${step.step_kind ?? DASH}`,
      label: [step.tool_name, skill].filter(Boolean).join(" · ") || DASH,
      detail,
      color: step.outcome ?? DASH,
    };
  });
}

// ---------------------------------------------------------------------------
// The component.
// ---------------------------------------------------------------------------

export interface AiPathJob {
  /** What was asked. The host supplies it; this channel never carries a question. */
  label: string;
  outcome?: string | null;
}

/**
 * The two places this family draws the same step: the timeline drawing and the
 * accessible table fallback. Named so a per-step slot can tell them apart.
 */
export type AiPathActionSite = "timeline" | "table";

export interface AiPathFamilyProps {
  steps: AiPathStep[];
  /** The JOB rung: the request itself, when the caller has an identity for it. */
  job?: AiPathJob | null;
  /** `recording` is readable and is NOT evidence -- the header says so (AC6). */
  lifecycle?: string | null;
  /** A human-only result. Renders the `No AI path` literal and nothing else. */
  humanOnly?: boolean;
  palette?: VizPalette;
  /**
   * The console attaches its own owner navigation here. Absent means the owner is
   * rendered as plain text -- the affordance is simply not offered, which is the
   * honest state on a surface that cannot navigate.
   */
  renderOwner?: (step: AiPathStep, index: number) => ReactNode;
  /**
   * Optional per-step slot, invoked ONCE for each of the two render sites.
   *
   * The family draws every step twice on purpose -- the timeline is the drawing,
   * the table is its direct fallback -- so a slot that returned the SAME
   * interactive control for both mounted it twice: two React states, two DOM
   * ids, and one reaction a person could file from either copy without the
   * other knowing (`context-hub.md:755-764`, "one step is offered for
   * annotation twice on the same screen"). The site therefore travels with the
   * call, and the caller decides where the control lives and what the other
   * site states in text.
   */
  renderStepAction?: (
    step: AiPathStep,
    index: number,
    site: AiPathActionSite,
  ) => ReactNode;
  ariaLabel?: string;

  /**
   * Where an expansion is recorded. Absent means nothing is recorded, which is a
   * surface that cannot write -- not a surface that chose not to. The callback is
   * always invoked through `safeInspect`, so a sink that throws cannot collapse
   * the drawing (AC6).
   */
  onInspect?: AiPathInspectionSink;
  /**
   * AC7 -- a host that cannot mount the widget gets the SAME information in
   * text, never an empty panel. An empty panel on this surface reads as "no
   * route was explored", which is the one claim the record does not support.
   */
  textOnly?: boolean;
}

interface Rung {
  level: AiPathLevel | null;
  label: string;
  steps: { step: AiPathStep; index: number }[];
}

/** Group the walk by rung, keeping walk order inside each rung. */
export function aiPathRungs(steps: AiPathStep[], job?: AiPathJob | null): Rung[] {
  const byLevel = new Map<AiPathLevel | null, { step: AiPathStep; index: number }[]>();
  steps.forEach((step, index) => {
    const level = levelOfStep(step);
    const bucket = byLevel.get(level) ?? [];
    bucket.push({ step, index });
    byLevel.set(level, bucket);
  });

  const rungs: Rung[] = AI_PATH_LEVELS.map((level) => ({
    level,
    label: AI_PATH_LEVEL_LABELS[level],
    steps: byLevel.get(level) ?? [],
  }));
  // A kind that names no rung is NOT dropped: the absence would read as a walk
  // that skipped it. `core.ai_path_recorder.level_of` returns null on purpose.
  const unrunged = byLevel.get(null) ?? [];
  if (unrunged.length > 0) {
    rungs.push({ level: null, label: NO_RUNG, steps: unrunged });
  }
  return rungs.filter((rung) => rung.steps.length > 0 || (rung.level === "JOB" && !!job));
}

export default function AiPathFamily({
  steps,
  job = null,
  lifecycle = null,
  humanOnly = false,
  palette,
  renderOwner,
  renderStepAction,
  ariaLabel = "AI Path: the walk this answer took through the knowledge tree",
  onInspect,
  textOnly = false,
}: AiPathFamilyProps) {
  const colours = useMemo(() => palette ?? getVizPalette(readCssVizTheme()), [palette]);
  // The drawing is the WALK, not the reading grid: one timeline row per step,
  // ordered by the step's own ordinal (`app.ai_path_steps.ordinal`), never
  // grouped -- grouping by rung reorders what happened, and this drawing is
  // the record of what happened. The rung survives as a per-row label. The
  // wire already arrives in walk order (`ORDER BY ordinal`); sorting here
  // keeps that true for every caller, not only the well-behaved ones. The
  // original index travels with each step so `renderOwner`
  // and the inspection ordinal below address the SAME step as before the sort.
  const ordered = useMemo(
    () =>
      steps
        .map((step, index) => ({ step, index }))
        .sort((a, b) => identityOrdinal(a.step, a.index) - identityOrdinal(b.step, b.index)),
    [steps],
  );
  const orderedSteps = useMemo(() => ordered.map(({ step }) => step), [ordered]);
  const rows = useMemo(() => aiPathFallbackRows(orderedSteps), [orderedSteps]);

  if (humanOnly) {
    // The exact literal, as a statement of fact. Not null, not an empty drawing:
    // an empty path would read as a path that was lost.
    return (
      <div className="flex flex-col gap-1" data-testid="ai-path" data-ai-path-state="human-only">
        <p className="text-sm font-medium">{NO_AI_PATH}</p>
        <p className="text-xs opacity-70">This result was produced without AI.</p>
      </div>
    );
  }

  const recording = lifecycle === "recording";
  const stateStyle = (state: AiPathNodeState) => ({
    borderColor: state === "completed" ? colours.accent : colours.diverging(1),
  });
  if (textOnly) {
    // AC7. Plain text, the SAME facts, in the same order the drawing puts them.
    // Composed here rather than by the host: a host that had to re-derive the
    // walk statement from the steps would be a second reading of the record.
    const lines = [
      recording ? "Still recording — not evidence" : "Observed walk",
      AI_PATH_STATE_STATEMENT,
      ...(ordered.length === 0
        ? ["No step recorded."]
        : ordered.map(({ step, index }) =>
            step.branch_evidence
              ? aiPathBranchesText({
                  stepLabel: `${displayOrdinal(step, index)}. ${step.tool_name ?? step.step_kind ?? DASH}`,
                  evidence: step.branch_evidence,
                })
              : `${displayOrdinal(step, index)}. ${step.tool_name ?? step.step_kind ?? DASH}`,
          )),
    ];
    return (
      <div className="flex flex-col gap-2" data-testid="ai-path" data-ai-path-state="text-only">
        <pre className="whitespace-pre-wrap text-xs" data-testid="ai-path-text-fallback">
          {lines.join("\n")}
        </pre>
        {ordered.map(({ step, index }) => {
          if (!step.branch_evidence) return null;
          // Rendered (hidden from the accessibility tree, since the text above
          // already carries it) purely so the inspection of a fallback is
          // RECORDED like any other -- a host fallback nobody can observe being
          // taken is exactly the state `mcp_app_behavior` cannot grade.
          return (
            <div key={step.id ?? `fallback-${index}`} hidden>
              <AiPathBranchSubtree
                stepOrdinal={identityOrdinal(step, index)}
                evidence={step.branch_evidence}
                onInspect={onInspect}
                palette={colours}
                textOnly
              />
            </div>
          );
        })}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4" data-testid="ai-path" aria-label={ariaLabel}>
      <header className="flex flex-col gap-1" data-testid="ai-path-header">
        <p className="text-sm font-medium" data-ai-path-lifecycle={recording ? "recording" : "final"}>
          {recording ? "Still recording — not evidence" : "Observed walk"}
        </p>
        <p className="text-xs opacity-70">
          {recording
            ? "This interaction may still gain steps, so it cannot be pinned as evidence. " +
              "That is a state, not a failure: nothing in the protocol says an interaction " +
              "has ended, so it is never stamped finished on our own initiative."
            : "Every rung below was crossed. The walk is finished and pinned."}
        </p>
        <p className="text-xs opacity-70" data-testid="ai-path-state-statement">
          {AI_PATH_STATE_STATEMENT}
        </p>
      </header>

      {ordered.length === 0 && !job ? (
        <p className="text-sm opacity-70" data-testid="ai-path-empty">
          No step recorded. The path exists and its owner recorded no step against it.
        </p>
      ) : (
        <ol className="flex flex-col" data-testid="ai-path-timeline">
          {job ? (
            <li
              className="flex gap-3"
              data-ai-path-rung="JOB"
              data-ai-path-node="job"
              data-node-state={job.outcome === "succeeded" ? "completed" : "failed"}
            >
              <span
                className="flex size-8 shrink-0 items-center justify-center rounded-full border"
                style={stateStyle(job.outcome === "succeeded" ? "completed" : "failed")}
              >
                <JOB_MARKER className="size-4" />
              </span>
              <div className="min-w-0 flex-1 pb-4">
                <div className="flex flex-wrap items-baseline gap-2">
                  <span className="text-xs uppercase tracking-wide opacity-60">job</span>
                  <span className="text-sm font-medium">{job.label}</span>
                </div>
              </div>
            </li>
          ) : null}
          {ordered.map(({ step, index }, position) => {
            const state = nodeStateOfStep(step);
            const level = levelOfStep(step);
            const kind = step.step_kind ?? "";
            const Marker = KIND_MARKER[kind] ?? UNKNOWN_MARKER;
            return (
              <li
                key={step.id ?? `${step.ordinal ?? index}`}
                className="flex gap-3"
                data-ai-path-step=""
                data-ai-path-kind={kind || "unknown"}
                data-ai-path-ordinal={displayOrdinal(step, index)}
                data-ai-path-rung={level ?? "none"}
                data-ai-path-node="step"
                data-node-state={state}
                data-node-reached={reachedNothingGoverned(step) ? "none" : "governed"}
              >
                <div className="flex flex-col items-center">
                  <span
                    className="flex size-8 shrink-0 items-center justify-center rounded-full border"
                    style={stateStyle(state)}
                  >
                    <Marker className="size-4" />
                  </span>
                  {position < ordered.length - 1 ? (
                    <span
                      className="w-px flex-1"
                      style={{ backgroundColor: colours.hairline }}
                      aria-hidden
                    />
                  ) : null}
                </div>
                <div className="min-w-0 flex-1 pb-4">
                  <div className="flex flex-wrap items-baseline gap-2">
                    <span className="text-xs tabular-nums opacity-60">
                      {displayOrdinal(step, index)}
                    </span>
                    <span className="text-xs uppercase tracking-wide opacity-60">
                      {kind || "step"}
                    </span>
                    <span className="text-sm font-medium">
                      {step.tool_name ?? step.step_kind ?? DASH}
                    </span>
                    <span className="text-xs opacity-60">
                      {level ? AI_PATH_LEVEL_LABELS[level] : NO_RUNG}
                    </span>
                    <span className="text-xs opacity-60">{step.outcome ?? DASH}</span>
                    {step.observed_at ? (
                      <span className="text-xs opacity-60">{step.observed_at}</span>
                    ) : null}
                  </div>
                  <p className="mt-0.5 text-xs opacity-70">
                    {reachedNothingGoverned(step) ? (
                      // The owner's docstring is explicit: an unrepresented
                      // call stays visible rather than have a graph object
                      // invented for it.
                      NOTHING_GOVERNED
                    ) : renderOwner ? (
                      <>
                        {[step.owner_workspace, step.owner_object_type]
                          .filter(Boolean)
                          .join(" ")}
                        {step.owner_workspace || step.owner_object_type ? " " : ""}
                        {renderOwner(step, index)}
                        {step.owner_version_id ? ` · version ${step.owner_version_id}` : ""}
                      </>
                    ) : (
                      [
                        [step.owner_workspace, step.owner_object_type, step.owner_object_id]
                          .filter(Boolean)
                          .join(" "),
                        step.owner_version_id ? `version ${step.owner_version_id}` : null,
                      ]
                        .filter(Boolean)
                        .join(" · ")
                    )}
                  </p>
                  {step.skill ? (
                    <p className="mt-0.5 text-xs opacity-70">
                      Skill {step.skill.version_id} step {step.skill.step_id} · {step.skill.state}
                      {step.skill.label ? ` · ${step.skill.label}` : ""}
                    </p>
                  ) : null}
                  {step.evidence_record_id ? (
                    <p className="mt-0.5 text-xs opacity-70">
                      evidence {step.evidence_record_id}
                    </p>
                  ) : null}
                  {step.missing_context?.length ? (
                    <p className="mt-0.5 text-xs opacity-70">
                      missing context {step.missing_context.join(", ")}
                    </p>
                  ) : null}
                  {describeChoice(step.chose) ? (
                    // What the call chose, on the timeline itself (round 5): it
                    // reached the reader only through the accessibility table.
                    <p className="mt-0.5 text-xs opacity-70" data-ai-path-chose="">
                      {describeChoice(step.chose)}
                    </p>
                  ) : null}
                  {renderStepAction ? (
                    <div className="mt-1">{renderStepAction(step, index, "timeline")}</div>
                  ) : null}
                  {step.branch_evidence ? (
                    <AiPathBranchSubtree
                      stepOrdinal={identityOrdinal(step, index)}
                      stepLabel={step.tool_name ?? step.step_kind ?? null}
                      evidence={step.branch_evidence}
                      palette={colours}
                      onInspect={onInspect}
                    />
                  ) : null}
                </div>
              </li>
            );
          })}
        </ol>
      )}

      {/* The direct table fallback, always -- "a direct table fallback for every
          visual" (visualization-and-rendering.md). Its columns are the family's
          declared `table_fallback_wells`, in that order. */}
      <div
        className="overflow-x-auto rounded-sm focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2"
        role="region"
        aria-label="AI Path step table"
        tabIndex={0}
      >
        <table className="w-full border-collapse text-sm" data-testid="ai-path-table">
          <caption className="text-left text-xs opacity-70">
            Every recorded step of this walk, in order, including the ones that reached
            nothing governed.
          </caption>
          <thead>
            <tr>
              <th scope="col" className="py-1 pr-3 text-left font-medium">
                #
              </th>
              {AI_PATH_FALLBACK_COLUMNS.map((column) => (
                <th
                  key={column.well}
                  scope="col"
                  className="py-1 pr-3 text-left font-medium"
                  data-fallback-well={column.well}
                >
                  {column.header}
                </th>
              ))}
              {renderStepAction ? <th scope="col">Feedback</th> : null}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, rowIndex) => (
              <tr key={`ai-path-row-${row.ordinal}`} data-ai-path-row={row.ordinal}>
                <th scope="row" className="py-1 pr-3 text-left font-normal tabular-nums">
                  {row.ordinal}
                </th>
                <td className="py-1 pr-3">{row.dimension}</td>
                <td className="py-1 pr-3">{row.label}</td>
                <td className="py-1 pr-3">{row.detail || DASH}</td>
                <td className="py-1 pr-3">{row.color}</td>
                {renderStepAction && ordered[rowIndex] ? (
                  <td className="py-1 pr-3">
                    {renderStepAction(
                      ordered[rowIndex]!.step,
                      ordered[rowIndex]!.index,
                      "table",
                    )}
                  </td>
                ) : null}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
