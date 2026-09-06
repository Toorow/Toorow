/**
 * One observed AI Path, read (Story 49.6).
 *
 * `navigation.ts` declares `{ type: "ai-path" }` under Knowledge Graph and
 * `ContentRouter` had no branch for it, so the address resolved to nothing --
 * a fact `evidence_index.py` states in its own source: "49.6 has registered the
 * `ai-path` route type but mounted no screen for it".
 *
 * `context-hub.md` says what a path IS: "trace evidence showing which governed
 * nodes, knowledge and Skills were used for an answer. It is not another primary
 * content store." So this screen reads and never writes, and every value on it
 * comes from the owner (`core.ai_paths`) — including the assessment, which is
 * derived server-side from the snapshot pinned before the run. Recomputing it
 * here would be a second opinion on what a path is worth.
 *
 * Two distinctions the screen refuses to blur:
 *
 *   - a `recording` path is readable but is NOT evidence. It can still grow
 *     steps, so `ai_path_reference` refuses to pin one. The header says so
 *     instead of letting an in-progress interaction look finished.
 *   - a step that reached nothing governed keeps empty owner fields and stays
 *     listed. The owner's docstring is explicit: "an unrepresented tool call
 *     must stay visible in the path rather than have a graph object invented
 *     for it."
 *
 * STORY 55.1 -- THIS SCREEN NO LONGER DRAWS THE PATH. Both distinctions above
 * now live in `AiPathFamily` (`ui/cards/shell/src/viz/renderers/aiPath.tsx`),
 * the `ai_path` visual family of the shared rendering runtime, because the same
 * evidence was being drawn here AND by the `ai-path` lens of `ResultWorkbench`.
 * Two drawings answer differently the first time one of them is fixed. What is
 * left here is what only this screen knows: which path to fetch, and how to
 * report that the store could not be read.
 */
import { useCallback, useEffect, useState } from "react";

import {
  AiPathFamily,
  aiPathStepsFromWire,
  type AiPathInspection,
  type AiPathWireStep,
} from "@toorow/card-shell/viz";

import { apiFetch } from "../lib/apiFetch";
import type { OwnerReference } from "../governance/governanceSurface";
import {
  Button,
  EmptyState,
  EvidenceRows,
  formatTimestamp,
  Panel,
  PanelHeader,
  Status,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
  TableScroll,
  Textarea,
} from "../ui";

/**
 * What the SERVER says about the owner one step reached (Story 49.6 AC7, lot 4).
 *
 * `owner_reference` is composed by `ai_paths_api._step_owner_reference`, which
 * goes through `evidence_index.owner_route` -- the one registry of owner routes
 * this product has. Nothing here is assembled from ids: this screen either
 * receives a complete reference and hands it to the shell's resolver, or
 * receives `null` and says so.
 *
 * `owner_reference_state` is the server's word for WHY there is no reference,
 * and the two absences are not the same sentence:
 *
 *   * `not_governed` -- the step reached nothing governed. It is not a broken
 *     link; there is nothing to link to, and the family already prints
 *     "nothing governed" for it.
 *   * `unavailable` -- an owner IS named and no route resolves to it. Either no
 *     screen holds that kind of object, or the owner refused to prove it inside
 *     this reader's access. The identifier stays on screen, unlinked.
 */
type OwnerReferenceState = "governed" | "unavailable" | "not_governed";

interface AiPathWireStepWithOwner extends AiPathWireStep {
  /** In a merged interaction: the path this row belongs to and its ordinal THERE
   *  (`step_order` is the merged rank). An inspection is addressed to these two. */
  path_id?: string | null;
  path_step_order?: number | null;
  /** What the call chose -- the arguments' ids and short values, recorded on
   *  the step since 2026-09-05 (`ai_path_recorder.choices_of`). */
  chose?: Record<string, unknown> | null;
  owner_reference?: OwnerReference | null;
  owner_reference_state?: OwnerReferenceState | null;
  /**
   * The CALLER'S OWN recorded reaction on this step, composed by the detail read
   * from the Feedback owner's table (`ai_paths_api._my_feedback`). Null means
   * this person has not reacted to this step — never "nobody has": another
   * person's reaction is not visible here and is not this control's business.
   */
  my_feedback?: {
    polarity: string;
    recorded_at?: string | null;
    comment?: string | null;
  } | null;
}

/**
 * One Event this walk crossed, as its DATA OWNER resolved it (AC9).
 *
 * The same shape and the same server composer as the Knowledge Graph overlay's
 * list -- `ai_paths_api._compose_event_references`. It carries an identity and a
 * pointer and nothing of the Event's content: no mapping, no payload, no source
 * sample, no run evidence. Context Hub may NAME an Event; it never becomes its
 * second owner.
 */
interface AiPathEventReference {
  event_id: string;
  ordinals: number[];
  binding_state: "linked" | "unavailable";
  event_type?: string | null;
  event_date?: string | null;
  version_number?: number | null;
  owner_route?: OwnerReference | null;
}

interface SkillCoverageLine {
  step: string;
  label: string;
  tool?: string | null;
  action?: string | null;
  state: "crossed" | "skipped" | "unobservable";
}

interface SkillCoverage {
  skill_version: string;
  skill_name?: string | null;
  prescribed: number;
  crossed: string[];
  skipped: string[];
  unobservable: string[];
  steps: SkillCoverageLine[];
  sequence_readable: boolean;
}

interface SiblingPath {
  path_id: string;
  state?: string | null;
  outcome?: string | null;
  owner_route?: Record<string, unknown> | null;
}

interface AiPath {
  schema_version?: "ai-path.v2";
  id: string;
  lifecycle?: string | null;
  outcome?: string | null;
  actor?: string | null;
  started_at?: string | null;
  ended_at?: string | null;
  w3c_trace_id?: string | null;
  model_ref?: string | null;
  policy_snapshot_hash?: string | null;
  content_hash?: string | null;
  steps?: AiPathWireStepWithOwner[];
  /** 2026-09-05: what the Skill prescribed vs what the walk crossed, and the
   *  other paths of the same interaction, routed. */
  skill_coverage?: SkillCoverage[];
  same_interaction?: SiblingPath[];
  /** id -> name for the governed ids the steps chose (2026-09-05). */
  names?: Record<string, string>;
  /** Every path of the trace merged in the order it happened, each step tagged with its path. */
  interaction?: {
    paths: { path_id: string; state?: string | null }[];
    steps: AiPathWireStepWithOwner[];
    /** The drawing is bounded (200 rows); the judgement is not. Said on the wire (round 6). */
    truncated?: boolean | null;
    total_steps?: number | null;
  } | null;
  event_references?: AiPathEventReference[];
  assessment?: Record<string, unknown> | null;
  referenceable_as_evidence?: boolean;
}

/**
 * THIS RECORD'S OWN WORDS, key by key.
 *
 * The header rows were reaching a person as the wire spelled them —
 * `w3c_trace_id`, `policy_snapshot_hash`, `model_ref`,
 * `referenceable_as_evidence`. `label()` de-snakes a column, which is a
 * typographic repair and not a vocabulary one: `W3c trace id` is still the
 * transport protocol talking.
 *
 * LOCAL, and deliberately so — the same shape and the same reason as
 * `FieldCatalogRail`'s `FACET_LABELS`. `lifecycle` here means "this walk can
 * still grow steps"; the same column on another object would not mean that, so
 * the word belongs to this screen and not to the shared `FIELD_VOCABULARY`. The
 * stored key is never destroyed: `EvidenceRows` keeps it on the row's `title`.
 */
const PATH_FIELD_LABELS: Record<string, string> = {
  lifecycle: "State of the record",
  outcome: "How it ended",
  assessment_verdict: "Verdict",
  actor: "Who asked",
  started_at: "Started",
  ended_at: "Ended",
  w3c_trace_id: "Trace across services",
  model_ref: "Model that answered",
  policy_snapshot_hash: "Rules in force at the time",
  referenceable_as_evidence: "Can be cited as evidence",
};

/** The assessment is the owner's own record, and its keys are its own too. */
const ASSESSMENT_FIELD_LABELS: Record<string, string> = {
  verdict: "Verdict",
  reason: "Why",
  assessed_at: "Assessed",
  content_hash: "Exact content",
  policy_snapshot_hash: "Rules in force at the time",
};

/**
 * ONE step's +/- annotation (Story 49.6 AC8).
 *
 * The boundary this component obeys is `context-hub.md:83-85` -- "Paths can be
 * inspected per run, aggregated into Knowledge Graph overlays, annotated with
 * positive or negative feedback" -- read with the amendment that says WHO owns
 * that annotation (`context-hub.md:701-764`, 2026-08-30): "an annotation
 * initiated from an AI Path uses the Test-owned Feedback command and its
 * reference; the Context Hub owns neither Feedback nor Events and adds no
 * editor for them" (idempotent per key; a change of mind is a new row and the
 * detail read composes the latest one, comment included). The
 * ownership itself is `analyze-and-test.md:193`: Test "receives stable Result,
 * Render, trace and feedback references for evaluation", the reading surface
 * only "displays feedback controls". So this renders a control and posts to the
 * TEST namespace (`/api/projects/{id}/test/feedback/ai-path-steps`); it holds no
 * store, writes no database and composes no address.
 *
 * WHAT IT SENDS, and nothing else: the path this screen was opened on, the
 * ordinal the server projected for this step, a polarity and an optional
 * comment. Every owner behind them -- the walk, the step, the Result it
 * delivered, the Semantic View versions visible at the time -- is re-resolved
 * server-side by `feedback_review.submit_ai_path_step_feedback`. Nothing is
 * joined here.
 *
 * ONE QUESTION AT A TIME. The thumbs ask the only question a reader can answer
 * without thinking twice -- was this step right or wrong. The comment appears
 * afterwards, optional, and the second click is the one that records. A single
 * `Idempotency-Key` is minted per (path, step) and reused: a retry after a slow
 * network is the SAME command carried through, not a second row the aggregate
 * would count twice.
 *
 * THE VERDICT SHOWN IS THE STORED ONE. After a successful post the control is
 * replaced by what the server says it recorded (`polarity` read back from the
 * receipt), never by the polarity this component sent. A replay of an existing
 * key answers with the row that already exists, so a second click cannot make
 * the screen show a reaction the table does not hold.
 */
const POLARITY_WORD: Record<string, string> = {
  positive: "Helpful",
  negative: "Not helpful",
};

/**
 * The refusals this control must be able to say, in the reader's words.
 *
 * Each one names the gesture rather than the cause. The server sends the same
 * sentences; they are restated here because a console that printed only what the
 * wire happened to carry would show an empty box the day a code arrived without
 * a message.
 */
const FEEDBACK_REFUSALS: Record<string, string> = {
  ai_path_still_recording:
    "This walk is still being recorded. It can be annotated once it has finished.",
  ai_path_delivered_no_result:
    "This walk delivered no result, so a step of it cannot be annotated yet.",
  // The key is deterministic on (path, step), so the SAME control sending a
  // different polarity under the same key is this refusal, in the person's own
  // words. (A change of mind is a new row through a new key; the door holds no
  // uniqueness per person and step -- context-hub.md, amendment of 2026-08-30.)
  idempotency_conflict:
    "You already recorded a reaction on this step.",
  missing_idempotency_key:
    "This reaction could not be recorded safely. Reopen the path and try again.",
  invalid_field: "This step could not be named to the recorder. Reopen the path and try again.",
};

type FeedbackState =
  | { kind: "idle" }
  | { kind: "choosing"; polarity: "positive" | "negative" }
  | { kind: "sending"; polarity: "positive" | "negative" }
  | { kind: "recorded"; polarity: string; comment: string | null; recordedAt?: string | null }
  | { kind: "refused"; message: string };

function StepFeedback({
  projectId,
  pathId,
  ordinal,
  recorded,
  onRecorded,
}: {
  projectId: string;
  pathId: string;
  ordinal: number;
  /**
   * The reaction this person ALREADY recorded on this step, as the server
   * composed it on the detail read (`my_feedback`). Present => the control
   * starts in its recorded state and asks nothing: a screen that re-asked after
   * a reload is a screen that files a second reaction for one judgement.
   */
  recorded?: { polarity: string; comment: string | null; recordedAt?: string | null } | null;
  /** Lifts what was just recorded, so the table fallback states the same fact. */
  onRecorded?: (ordinal: number, verdict: { polarity: string; comment: string | null }) => void;
}) {
  const [state, setState] = useState<FeedbackState>(
    recorded
      ? {
          kind: "recorded",
          polarity: recorded.polarity,
          comment: recorded.comment,
          recordedAt: recorded.recordedAt ?? null,
        }
      : { kind: "idle" },
  );
  const [comment, setComment] = useState("");
  // DETERMINISTIC on (path, step), with NO timestamp in it. A key minted per
  // mount made every reload a NEW command: the same person, the same step, a
  // second row, and an aggregate that counted one judgement twice. With this
  // key a second submission is a REPLAY — same polarity answers with the stored
  // receipt, a different polarity is the server's `idempotency_conflict`, read
  // back above as "you already recorded a reaction on this step".
  const idempotencyKey = `ai-path-step-${pathId}-${ordinal}`;

  async function record(polarity: "positive" | "negative") {
    setState({ kind: "sending", polarity });
    try {
      const res = await apiFetch(
        `/api/projects/${encodeURIComponent(projectId)}/test/feedback/ai-path-steps`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json", "Idempotency-Key": idempotencyKey },
          body: JSON.stringify({
            ai_path_id: pathId,
            step_ordinal: ordinal,
            polarity,
            comment: comment.trim() ? comment.trim() : null,
          }),
        },
      );
      if (res.status === 404) {
        // The server hides existence on purpose: a step of another Project and a
        // step that no longer exists answer identically.
        setState({
          kind: "refused",
          message: "This step is no longer available in this project.",
        });
        return;
      }
      const body = (await res.json().catch(() => null)) as
        | { code?: string; message?: string; polarity?: string; comment?: string | null }
        | null;
      if (!res.ok) {
        const code = typeof body?.code === "string" ? body.code : "";
        setState({
          kind: "refused",
          message:
            FEEDBACK_REFUSALS[code] ??
            (typeof body?.message === "string" && body.message
              ? body.message
              : "This reaction was not recorded."),
        });
        return;
      }
      const verdict = {
        // READ BACK. The receipt is the record; what this component sent is not.
        polarity: typeof body?.polarity === "string" ? body.polarity : polarity,
        comment: typeof body?.comment === "string" ? body.comment : null,
      };
      setState({ kind: "recorded", ...verdict });
      onRecorded?.(ordinal, verdict);
    } catch {
      setState({ kind: "refused", message: "This reaction could not be sent." });
    }
  }

  if (state.kind === "recorded") {
    return (
      <p className="text-caption" data-testid={`ai-path-step-feedback-recorded-${ordinal}`}>
        Recorded: {POLARITY_WORD[state.polarity] ?? state.polarity}
        {state.comment ? ` — ${state.comment}` : ""}
        {state.recordedAt ? ` (on ${formatTimestamp(state.recordedAt)})` : ""}
      </p>
    );
  }

  if (state.kind === "refused") {
    return (
      <p className="text-caption" data-testid={`ai-path-step-feedback-refused-${ordinal}`}>
        {state.message}
      </p>
    );
  }

  if (state.kind === "choosing" || state.kind === "sending") {
    const { polarity } = state;
    return (
      <div data-testid={`ai-path-step-feedback-comment-${ordinal}`}>
        <label className="text-caption" htmlFor={`ai-path-step-comment-${ordinal}`}>
          {POLARITY_WORD[polarity]} — add a comment (optional)
        </label>
        <Textarea
          id={`ai-path-step-comment-${ordinal}`}
          value={comment}
          rows={2}
          onChange={(event) => setComment(event.target.value)}
        />
        <Button
          type="button"
          size="xs"
          disabled={state.kind === "sending"}
          data-testid={`ai-path-step-feedback-record-${ordinal}`}
          onClick={() => void record(polarity)}
        >
          {state.kind === "sending" ? "Recording…" : "Record this reaction"}
        </Button>
        <Button
          type="button"
          size="xs"
          variant="ghost"
          disabled={state.kind === "sending"}
          data-testid={`ai-path-step-feedback-cancel-${ordinal}`}
          onClick={() => {
            setComment("");
            setState({ kind: "idle" });
          }}
        >
          Cancel
        </Button>
      </div>
    );
  }

  return (
    <div data-testid={`ai-path-step-feedback-${ordinal}`}>
      <Button
        type="button"
        size="icon-xs"
        variant="ghost"
        aria-label={`This step was helpful — step ${ordinal + 1}`}
        data-testid={`ai-path-step-feedback-positive-${ordinal}`}
        onClick={() => setState({ kind: "choosing", polarity: "positive" })}
      >
        👍
      </Button>
      <Button
        type="button"
        size="icon-xs"
        variant="ghost"
        aria-label={`This step was not helpful — step ${ordinal + 1}`}
        data-testid={`ai-path-step-feedback-negative-${ordinal}`}
        onClick={() => setState({ kind: "choosing", polarity: "negative" })}
      >
        👎
      </Button>
    </div>
  );
}

type State =
  | { kind: "loading" }
  | { kind: "ready"; path: AiPath }
  | { kind: "error"; message: string };

export default function AiPathPage({
  projectId,
  pathId,
  onOpenOwner,
}: {
  projectId: string;
  pathId: string;
  /**
   * Hands ONE server-composed owner reference to the shell's resolver.
   *
   * The same seam every other workbench uses (`objectSurfaces.tsx` passes
   * `openOwner`), and the same refusal contract: an owner reference is
   * untrusted input, so the shell validates it against the navigation contracts
   * and answers out loud when it cannot open it. This screen never navigates by
   * itself and never composes an address.
   *
   * Absent prop => the owner is named and NOT linked, which is the honest state
   * on a host that cannot navigate. A button that goes nowhere would be worse
   * than plain text, and hiding the owner would hide evidence.
   */
  onOpenOwner?: (owner: OwnerReference) => void;
}) {
  const [state, setState] = useState<State>({ kind: "loading" });
  const [wholeInteraction, setWholeInteraction] = useState(false);

  /**
   * AC8 -- what THIS person has recorded on the steps of this walk, since the
   * screen opened.
   *
   * It is not a second store: the server's `my_feedback` is the record, and this
   * only carries forward what the receipt of a write just said, so the
   * accessible table below states the same fact as the timeline without the
   * control being mounted twice. A reload reads it back from the server.
   */
  const [ownReactions, setOwnReactions] = useState<
    Record<number, { polarity: string; comment: string | null }>
  >({});
  const rememberReaction = useCallback(
    (ordinal: number, verdict: { polarity: string; comment: string | null }) =>
      setOwnReactions((previous) => ({ ...previous, [ordinal]: verdict })),
    [],
  );

  /**
   * Story 55.2 AC4 -- opening a step is itself observable, and this is the one
   * surface that can write it today.
   *
   * AC8: it issues NO data read. It posts what the person was just shown, after
   * they were shown it, and the request is fire-and-forget on purpose -- AC6
   * says a failed write must leave the subtree open, so nothing here awaits,
   * re-renders or surfaces an error. A screen that reported "your click was not
   * recorded" would have made observation able to take down the observed.
   */
  const recordInspection = useCallback(
    (inspection: AiPathInspection, target?: { pathId: string; stepOrdinal: number | null }) => {
      const owningPath = target?.pathId ?? pathId;
      const stepOrdinal = target ? target.stepOrdinal : inspection.stepOrdinal;
      void apiFetch(
        `/api/projects/${encodeURIComponent(projectId)}/context/ai-paths/${encodeURIComponent(owningPath)}/inspections`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            kind: inspection.kind,
            surface: "console",
            displayed_state: inspection.displayedState,
            branches_listed: inspection.branchesListed,
            step_ordinal: stepOrdinal,
          }),
        },
      ).catch(() => {
        // Swallowed, and not logged as an error: see above.
      });
    },
    [projectId, pathId],
  );

  useEffect(() => {
    let live = true;
    setState({ kind: "loading" });
    apiFetch(
      `/api/projects/${encodeURIComponent(projectId)}/context/ai-paths/${encodeURIComponent(pathId)}`,
      { cache: "no-store" },
    )
      .then(async (res) => {
        if (!live) return;
        if (res.status === 404) {
          // The server hides existence on purpose: a foreign path and a missing
          // one answer identically, so this copy must not guess which it was.
          setState({ kind: "error", message: "This AI Path is not available in this project." });
          return;
        }
        if (!res.ok) {
          setState({
            kind: "error",
            message:
              res.status === 503
                ? "AI Paths could not be read. This is not an empty history."
                : `This AI Path could not be opened (${res.status}).`,
          });
          return;
        }
        setState({ kind: "ready", path: (await res.json()) as AiPath });
      })
      .catch(() => {
        if (live) setState({ kind: "error", message: "This AI Path could not be reached." });
      });
    return () => {
      live = false;
    };
  }, [projectId, pathId]);

  if (state.kind === "loading") {
    return (
      <Panel>
        <PanelHeader title="AI Path" description="Reading the recorded trace." />
      </Panel>
    );
  }

  if (state.kind === "error") {
    return (
      <Panel>
        <PanelHeader title="AI Path" description="Observed evidence for one interaction." />
        <Status as="block" tone="warning" title="Nothing is shown in its place">
          {state.message}
        </Status>
      </Panel>
    );
  }

  const { path } = state;
  // The ONE wire-to-renderer mapping lives in the family (`aiPathStepsFromWire`,
  // AI-134 included): the MCP App entry maps the same wire shape, and two local
  // mappings would answer differently the first time one of them was fixed.
  const wireSteps = path.steps ?? [];
  const ownSteps = aiPathStepsFromWire(wireSteps);
  // ONE INTERACTION, ONE TIMELINE (2026-09-05): the recorder splits an answer
  // into the Result's execution, the context search and the calls around them.
  // « Whole interaction » reads them as one sequence; an inspection made there is
  // addressed to the row's OWN path and its ordinal there (`interactionInspectionTarget`).
  const interactionSteps = path.interaction?.steps ? aiPathStepsFromWire(path.interaction.steps) : null;
  const steps = wholeInteraction && interactionSteps ? interactionSteps : ownSteps;
  const names = path.names ?? {};
  const named = (value: unknown): string => {
    const text = String(value);
    return names[text] ? `${names[text]} (${text})` : text;
  };
  const eventReferences = path.event_references ?? [];

  /**
   * AC8 -- the reaction the control starts from, per step ordinal.
   *
   * The SERVER's answer first (`my_feedback`, composed on the detail read from
   * the Feedback owner's own table), then anything recorded since this screen
   * opened. Without the server half, a reload showed an unanswered control and
   * asked a question this person had already answered.
   */
  const reactions: Record<
    number,
    { polarity: string; comment: string | null; recordedAt?: string | null }
  > = {};
  wireSteps.forEach((wire, index) => {
    const ordinal = wire.step_order ?? index;
    if (wire.my_feedback?.polarity) {
      reactions[ordinal] = {
        polarity: wire.my_feedback.polarity,
        comment: wire.my_feedback.comment ?? null,
        recordedAt: wire.my_feedback.recorded_at ?? null,
      };
    }
  });
  Object.entries(ownReactions).forEach(([ordinal, verdict]) => {
    reactions[Number(ordinal)] = verdict;
  });

  /**
   * AC7 -- the owner of a step, as a LINK to that exact object and version.
   *
   * The family calls this with the step AND its original index, and documents
   * why: "the original index travels with each step so `renderOwner` ... address
   * the SAME step as before the sort". So the reference is read from the wire
   * record at that index -- the identical record `aiPathStepsFromWire` mapped,
   * one-to-one and in order.
   *
   * Three answers, and none of them is a guess:
   *
   *   - a complete reference => a control that hands it to the shell resolver;
   *   - `unavailable` => the sentence, not a link. The identifier is still
   *     printed by the family beside it, so the evidence survives the refusal;
   *   - no handler => the identifier, plain. See `onOpenOwner`.
   *
   * This function composes NOTHING. It never joins a workspace, a section and an
   * id into an address: doing that here would make the console a second registry
   * of owner routes, and the day a screen moves the two would disagree with
   * neither side failing.
   */
  const renderOwner = (_step: unknown, index: number) => {
    // The same list the family draws (round 4, F5b): in « Whole interaction » the
    // index addresses the merged timeline, and reading the path's own wire steps
    // with it opened a DIFFERENT step's owner.
    const wire = (wholeInteraction && path.interaction?.steps ? path.interaction.steps : wireSteps)[index];
    const reference = wire?.owner_reference ?? null;
    if (!reference || !onOpenOwner) {
      return (
        <span data-testid={`ai-path-owner-unavailable-${index}`}>
          {wire?.owner_object_id ?? ""}
          {reference ? "" : " — no screen opens this object from here"}
        </span>
      );
    }
    return (
      <Button
        type="button"
        variant="secondary"
        size="sm"
        data-testid={`ai-path-owner-open-${index}`}
        onClick={() => onOpenOwner(reference)}
      >
        {wire?.owner_object_id ?? "Open the owner"}
      </Button>
    );
  };
  // The verdict is read, never recomputed (see the header of this file): it is
  // surfaced next to the lifecycle so the state of the path -- recording or
  // finalized, and what the owner judged it to be worth -- is read at the top,
  // with the full assessment left in its own panel below.
  const assessmentVerdict =
    path.assessment && typeof path.assessment.verdict === "string"
      ? path.assessment.verdict
      : null;

  return (
    <>
      <Panel>
        <PanelHeader
          title={`AI Path ${path.id}`}
          description="Which governed objects an answer actually went through."
        />
        {/* EvidenceRows is the shared "read me this record" vocabulary, extracted
            in 47.5 so there is ONE of them and not one per screen. The WORDS
            are this record's (see `PATH_FIELD_LABELS`); the rendering is
            everyone's. */}
        <EvidenceRows
          label="Path"
          labels={PATH_FIELD_LABELS}
          source={{
            lifecycle: path.lifecycle,
            outcome: path.outcome,
            assessment_verdict: assessmentVerdict,
            actor: path.actor,
            // One instant, one rendering. `formatTimestamp` is the console's
            // single decision for the places that can only produce a string —
            // these rows were printing the raw ISO the wire carries.
            started_at: path.started_at ? formatTimestamp(path.started_at) : null,
            ended_at: path.ended_at ? formatTimestamp(path.ended_at) : null,
            // THE WIRE KEY, not a hand-shortened one. The row is READ under
            // its human label; what the `title` discloses has to be the key the
            // payload actually carries, or the disclosure names nothing.
            w3c_trace_id: path.w3c_trace_id,
            model_ref: path.model_ref,
            policy_snapshot_hash: path.policy_snapshot_hash,
            referenceable_as_evidence: path.referenceable_as_evidence
              ? "yes"
              : "no - still recording",
          }}
        />
      </Panel>

      <Panel>
        <PanelHeader
          title="Steps"
          description="In the order they happened, including the ones that reached nothing governed."
        />
        {/* The server projects a closed `branch_evidence` object on eligible
            retrieval steps. The shared family validates and draws that same
            object for Console and shared Result surfaces; raw producer detail
            is neither fetched separately nor reconstructed here. */}
        {interactionSteps && (
          <div className="mb-3 flex items-center gap-2" data-testid="ai-path-scope">
            <Button
              type="button"
              size="sm"
              variant={wholeInteraction ? "ghost" : "secondary"}
              onClick={() => setWholeInteraction(false)}
              data-testid="ai-path-scope-path"
            >
              This path
            </Button>
            <Button
              type="button"
              size="sm"
              variant={wholeInteraction ? "secondary" : "ghost"}
              onClick={() => setWholeInteraction(true)}
              data-testid="ai-path-scope-interaction"
            >
              Whole interaction ({path.interaction?.paths.length ?? 0} paths, {interactionSteps.length} steps
              {path.interaction?.truncated && typeof path.interaction.total_steps === "number"
                ? `, the last of ${path.interaction.total_steps}; the judgement reads them all`
                : ""}
              )
            </Button>
          </div>
        )}
        <AiPathFamily
          steps={steps}
          lifecycle={path.lifecycle}
          /* From « Whole interaction » an inspection is addressed to the OWNING
             path and its ordinal THERE (round 5): the merged rank names the row,
             the row names its path -- a reader who opened a branch on a sibling's
             step recorded nothing before, and could not see that it recorded
             nothing. */
          onInspect={
            wholeInteraction
              ? (inspection) => {
                  const target = interactionInspectionTarget(path.interaction?.steps, inspection.stepOrdinal);
                  if (target) recordInspection(inspection, target);
                }
              : recordInspection
          }
          /* The family draws the walk; the console attaches the navigation. That
             seam has existed since 55.1 (`renderOwner`) and nothing was passed
             into it, so every owner on this screen was plain text -- a walk that
             named the objects an answer went through and opened none of them. */
          renderOwner={renderOwner}
          /* AC8 -- the +/- annotation, on the exact step. The family already
             held the slot (`renderStepAction`) and nothing was passed into it,
             so an AI Path named the objects an answer went through and offered
             no way to say one of them was wrong. The command is Test's; only the
             control is here. The ordinal is the family's own identity rule
             (`identityOrdinal`): the step's stored `ordinal`, or its index when
             the walk carries none. Deciding it differently here is the
             off-by-one AI-134 already cost.

             ONE CONTROL PER STEP. The family draws each step twice -- the
             timeline and the accessible table fallback -- so this slot is called
             twice and the SITE says which one. The control is mounted on the
             timeline only; the table states the recorded reaction in text.
             Mounting it on both gave one step two React states, two
             `data-testid`s and two DOM ids, and let one person file the same
             reaction from either copy without the other knowing
             (`context-hub.md:755-764`). */
          renderStepAction={wholeInteraction ? undefined : (step, index, site) => {
            const ordinal = step.ordinal ?? index;
            if (site === "table") {
              const verdict = reactions[ordinal];
              return (
                <span data-testid={`ai-path-step-feedback-table-${ordinal}`}>
                  {verdict ? POLARITY_WORD[verdict.polarity] ?? verdict.polarity : "No reaction"}
                </span>
              );
            }
            return (
              <StepFeedback
                projectId={projectId}
                pathId={path.id}
                ordinal={ordinal}
                recorded={reactions[ordinal] ?? null}
                onRecorded={rememberReaction}
              />
            );
          }}
        />
      </Panel>

      {/* WAS THE SKILL FOLLOWED (2026-09-05). The walk marks the steps it crossed;
          this names the whole sequence the served version prescribed and what
          became of each step: crossed, skipped (a tool never called), or
          unobservable (a read the recorder cannot see). Not a verdict -- the
          assessment below owns those -- a reading. */}
      {(path.skill_coverage ?? []).length > 0 && (
        <Panel>
          <PanelHeader
            title="Skill followed"
            description="What the served Skill version prescribed, and what became of each step on this walk."
          />
          {(path.skill_coverage ?? []).map((skill) => (
            <div key={skill.skill_version} className="grid gap-2" data-testid={`ai-path-skill-${skill.skill_version}`}>
              <p className="m-0 text-body text-text">
                <span className="font-semibold">{skill.skill_name ?? skill.skill_version}</span>
                <span className="text-text-secondary">
                  {" "}· {skill.crossed.length} of {skill.prescribed} step{skill.prescribed > 1 ? "s" : ""} crossed
                  {skill.skipped.length > 0 ? `, ${skill.skipped.length} skipped` : ""}
                  {skill.unobservable.length > 0 ? `, ${skill.unobservable.length} not observable` : ""}
                </span>
              </p>
              {!skill.sequence_readable && (
                <p className="m-0 text-caption text-text-secondary">
                  The served version's sequence could not be read; only the crossed steps are known.
                </p>
              )}
              {skill.steps.length > 0 && (
                <TableScroll label={`Skill ${skill.skill_name ?? skill.skill_version}`}>
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Step</TableHead>
                        <TableHead>Prescribed</TableHead>
                        <TableHead>Tool</TableHead>
                        <TableHead>On this walk</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {skill.steps.map((line) => (
                        <TableRow key={`${skill.skill_version}-${line.step}`} data-testid={`ai-path-skill-step-${line.step}`}>
                          <TableCell className="text-text-secondary">{line.step}</TableCell>
                          <TableCell className="text-text">{line.label}</TableCell>
                          <TableCell className="font-mono text-caption text-text-secondary">{line.tool ?? "—"}</TableCell>
                          <TableCell>
                            <Status tone={line.state === "crossed" ? "success" : line.state === "skipped" ? "warning" : "neutral"}>
                              {line.state === "crossed" ? "Crossed" : line.state === "skipped" ? "Skipped" : "Not observable"}
                            </Status>
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </TableScroll>
              )}
            </div>
          ))}
        </Panel>
      )}

      {/* THE REST OF THE INTERACTION (2026-09-05). A Result's execution is its own
          finalized path; the calls around it live on the interaction's path under
          the same trace. Each names the others, routed, so the reader walks from
          the figure to the reasoning and back. */}
      {(path.same_interaction ?? []).length > 0 && (
        <Panel>
          <PanelHeader
            title="Same interaction"
            description="Other paths recorded under this interaction's trace."
          />
          <ul className="m-0 grid list-none gap-1 p-0" data-testid="ai-path-same-interaction">
            {(path.same_interaction ?? []).map((sibling) => (
              <li key={sibling.path_id} className="flex items-center gap-2 text-body">
                {sibling.owner_route && onOpenOwner ? (
                  <Button
                    type="button"
                    variant="secondary"
                    size="sm"
                    data-testid={`ai-path-sibling-${sibling.path_id}`}
                    onClick={() => onOpenOwner(sibling.owner_route as unknown as OwnerReference)}
                  >
                    {sibling.path_id}
                  </Button>
                ) : (
                  <span className="font-mono text-caption">{sibling.path_id}</span>
                )}
                <span className="text-caption text-text-secondary">
                  {sibling.state ?? "unknown"}
                  {sibling.outcome ? ` · ${sibling.outcome}` : ""}
                </span>
              </li>
            ))}
          </ul>
        </Panel>
      )}

      {/* WHAT EACH STEP CHOSE (2026-09-05). Jean: « tu devrais voir quel skill a
          été utilisé et quelle solution tu as choisie ». The walk above names the
          tools; this names what they were asked -- the Datastreams, the measures
          in their pivot wells, the family, the columns pinned -- as the recorder
          kept them. Page-owned: the family renderer's contract is shared with the
          Result tab and stays exact. Rendered only when a step recorded a choice. */}
      {wireSteps.some((step) => step.chose && Object.keys(step.chose).length > 0) && (
        <Panel>
          <PanelHeader
            title="What each step chose"
            description="The arguments each call carried, as recorded. Ids and short values only; never the answer."
          />
          <TableScroll label="What each step chose">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>#</TableHead>
                  <TableHead>Tool</TableHead>
                  <TableHead>Chose</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {wireSteps
                  .filter((step) => step.chose && Object.keys(step.chose).length > 0)
                  .map((step, index) => (
                    <TableRow key={`chose-${step.step_order ?? index}`} data-testid={`ai-path-chose-${step.step_order ?? index}`}>
                      <TableCell className="text-text-secondary">{(step.step_order ?? index) + 1}</TableCell>
                      <TableCell className="font-mono text-caption text-text">{step.tool_name ?? "—"}</TableCell>
                      <TableCell>
                        <dl className="m-0 grid gap-0.5">
                          {Object.entries(step.chose ?? {}).map(([key, value]) => (
                            <div key={key} className="flex gap-2 text-caption">
                              <dt className="m-0 shrink-0 font-mono text-text-secondary">{key}</dt>
                              <dd className="m-0 break-all text-text">
                                {Array.isArray(value) ? value.map(named).join(", ") : named(value)}
                              </dd>
                            </div>
                          ))}
                        </dl>
                      </TableCell>
                    </TableRow>
                  ))}
              </TableBody>
            </Table>
          </TableScroll>
        </Panel>
      )}

      {/* Story 49.6 AC9 on the WORKBENCH.

          A path opened directly used to lose the Data deep-link the Knowledge
          Graph overlay gives for the same walk: the same evidence, read two
          ways, answered differently. The list is composed by the server through
          the SAME function the overlay route calls, and it carries an identity
          and a pointer only -- the Event's mapping, payload, samples and run
          evidence stay where they are owned, one link away.

          The panel is rendered only when the walk crossed an Event: an empty
          "Events" panel on a path that touched none would invent an absence. */}
      {eventReferences.length > 0 && (
        <Panel>
          <PanelHeader
            title="Events on this path"
            description="Owned by their Datastream in Data. Named here, never copied."
          />
          <ul data-testid="ai-path-event-refs">
            {eventReferences.map((reference) => (
              <li key={reference.event_id} data-testid={`ai-path-event-ref-${reference.event_id}`}>
                {reference.binding_state === "linked" && reference.owner_route && onOpenOwner ? (
                  <Button
                    type="button"
                    variant="secondary"
                    size="sm"
                    data-testid={`ai-path-event-open-${reference.event_id}`}
                    onClick={() => onOpenOwner(reference.owner_route as OwnerReference)}
                  >
                    {reference.event_type ?? "Event"}
                    {reference.event_date ? ` · ${reference.event_date}` : ""}
                  </Button>
                ) : reference.binding_state === "linked" && reference.owner_route ? (
                  <span>
                    {reference.event_type ?? "Event"}
                    {reference.event_date ? ` · ${reference.event_date}` : ""}
                  </span>
                ) : (
                  // Non-disclosing, and the SAME sentence the overlay uses: an
                  // Event of another Project, one that was deleted, and one
                  // whose Datastream binding was never proven must read
                  // identically. Saying which would turn this list into a way to
                  // ask whether an Event exists.
                  <span data-testid={`ai-path-event-unavailable-${reference.event_id}`}>
                    An Event on this path could not be resolved to its owner — shown as
                    unavailable rather than linked to a Datastream nobody proved owns it.
                  </span>
                )}{" "}
                {reference.ordinals.length > 0 ? (
                  <span className="ml-1 rounded-sm bg-background-light px-1.5 py-0.5 text-xs text-text-secondary">
                    step {reference.ordinals.map((ordinal) => ordinal + 1).join(", ")}
                  </span>
                ) : null}
              </li>
            ))}
          </ul>
        </Panel>
      )}

      <Panel>
        <PanelHeader
          title="Assessment"
          description="Derived by the owner from the snapshot pinned before the run."
        />
        {path.assessment ? (
          <EvidenceRows
            label="Assessment"
            labels={ASSESSMENT_FIELD_LABELS}
            source={path.assessment}
          />
        ) : (
          <EmptyState
            title="No assessment"
            description="The owner produced none for this path."
          />
        )}
      </Panel>
    </>
  );
}

/**
 * The path and ordinal an inspection made from the merged timeline is addressed
 * to. The merged rank is the row's index (`merge_interaction_steps` makes
 * `step_order` the rank), and the row carries the path it came from and its
 * ordinal there. Nothing to address when the row is unknown or unrouted.
 */
export function interactionInspectionTarget(
  rows: AiPathWireStepWithOwner[] | null | undefined,
  mergedOrdinal: number | null,
): { pathId: string; stepOrdinal: number | null } | null {
  if (!rows || mergedOrdinal === null || mergedOrdinal < 0) return null;
  const row = rows[mergedOrdinal];
  if (!row || typeof row.path_id !== "string" || row.path_id.length === 0) return null;
  return { pathId: row.path_id, stepOrdinal: typeof row.path_step_order === "number" ? row.path_step_order : null };
}
