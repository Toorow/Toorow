/**
 * Story 50.5 AC8 -- the share mount entry, over a FROZEN Render.
 *
 * IT DOES NOT FETCH. A public share page reads what was frozen, and a runtime
 * that could fetch would be a runtime that could show something other than what
 * was shared. The envelope arrives already embedded in the served document; this
 * entry reads it and mounts.
 *
 * WIRING IT INTO THE SERVED RESPONSE IS STORY 50.7, NOT THIS STORY. The seam is
 * measured and recorded rather than edited here: `server/core/rendus_api.py`
 * serves the public share HTML with an EMPTY `<div id="widget-mount">` inside a
 * `.widget-frame` DIV (not a frame), and injects the payload into
 * `window.__MCP_STRUCTURED_CONTENT__`. Nothing mounts into that div today. This
 * bundle is what will.
 *
 * BUILD MISMATCH IS THE POINT OF A SHARE PAGE. A retained Render pins its
 * renderer and runtime builds; when this bundle is a different build, the runtime
 * refuses with both identities shown rather than redrawing the frozen claim
 * through newer code (AC14).
 */

import { useEffect, useState } from "react";

import VisualizationRuntime, { serializeVisualModel } from "../Runtime";
import AiPathCapability, { AI_PATH_CAPABILITY_SCHEMA } from "../aiPathCapability";
import type { RenderInput } from "../contracts";
import { VizStatePanel } from "../states";
import TargetedFeedback, {
  type ExactFeedbackContext,
  type ExactFeedbackRequest,
  type ExactFeedbackSubmitter,
  type FeedbackSelection,
} from "../targetedFeedback";

declare global {
  interface Window {
    __TOOROW_FROZEN_RENDER__?: unknown;
    __TOOROW_FROZEN_AI_PATH_EVIDENCE__?: unknown;
    __TOOROW_FROZEN_FEEDBACK_CONTEXT__?: unknown;
    __TOOROW_FROZEN_DOSSIER__?: unknown;
  }
}

/** One block of a shared Dossier, exactly as `session/dossier` inlines it (73-2). */
export interface ShareDossierBlock {
  kind: "render" | "narrative";
  text?: string;
  /** Who wrote a narrative block (D3, 2026-09-02). Absent on blocks stored
   *  before that date, when the console was the only writer: read as `human`. */
  authored_by?: "human" | "model";
  render_id?: string;
  render?: RenderInput | null;
  ai_path_evidence?: unknown;
  facts?: { label: string; value: string }[];
  unavailable?: { title?: string; missing_link?: string };
  feedback_context?: unknown;
}

export interface ShareDossierInput {
  label: string;
  version_number: number;
  blocks: ShareDossierBlock[];
}

export interface ShareVisualizationProps {
  input?: RenderInput | null;
  aiPathEvidence?: unknown;
  feedbackContext?: unknown;
  onSubmitFeedback?: ExactFeedbackSubmitter;
}

const ANSWER_SELECTION: FeedbackSelection = { target: { kind: "answer" }, label: "Answer" };

/** Read the frozen envelope embedded in the served document; analytical data is never fetched. */
export function readFrozenRender(): RenderInput | null {
  if (typeof window === "undefined") return null;
  const frozen = window.__TOOROW_FROZEN_RENDER__;
  if (!frozen || typeof frozen !== "object") return null;
  return frozen as RenderInput;
}

/** Read the evidence frozen beside the RenderInput. It never fetches or recomposes a path. */
export function readFrozenAiPathEvidence(): unknown {
  if (typeof window !== "undefined" && window.__TOOROW_FROZEN_AI_PATH_EVIDENCE__ !== undefined) {
    return window.__TOOROW_FROZEN_AI_PATH_EVIDENCE__;
  }
  return {
    schema_version: AI_PATH_CAPABILITY_SCHEMA,
    state: "unavailable",
    reason: "evidence_not_frozen",
  };
}

export function readFrozenFeedbackContext(): unknown {
  return typeof window === "undefined" ? undefined : window.__TOOROW_FROZEN_FEEDBACK_CONTEXT__;
}

/** Read the frozen Dossier sequence, when this grant opens one (73-2). */
export function readFrozenDossier(): ShareDossierInput | null {
  if (typeof window === "undefined") return null;
  const frozen = window.__TOOROW_FROZEN_DOSSIER__;
  if (!frozen || typeof frozen !== "object" || !Array.isArray((frozen as ShareDossierInput).blocks)) {
    return null;
  }
  return frozen as ShareDossierInput;
}

/**
 * The shared Dossier: the SEQUENCE, drawn by the same runtime as a single share.
 *
 * Every figure mounts `VisualizationRuntime` with its own frozen `RenderInput`,
 * so the build-pin refusal, the accessible table fallback and the theming are
 * the single-render page's, once per figure -- never a second renderer. Each
 * figure's provenance facts print beside it: that is what the amendment calls
 * "provenance carried onto paper", and what the PDF (73-3) will reuse.
 * Figure-level feedback is deliberately absent from this slice: the minted
 * context binds one Render, and how a dossier page targets one is the story's
 * remaining half.
 *
 * Two things the amendment of 2026-09-02 asks of this page (74-2): a narrative
 * SAYS who wrote it -- a figure is governed, a narrative is generated or a
 * person's, and the reader must see which -- and each figure carries the
 * address of the reasoning path that produced its Result, through the same
 * `AiPathCapability` the single-render page mounts.
 */
export const NARRATIVE_AUTHOR_LABEL: Record<"human" | "model", string> = {
  model: "Written by the model",
  human: "Written by a person",
};

function ShareDossierFigure({
  block,
  index,
  onSubmitFeedback,
}: {
  block: ShareDossierBlock;
  index: number;
  onSubmitFeedback?: ExactFeedbackSubmitter;
}) {
  //  73-2, last half: each figure takes feedback through its OWN minted
  //  context. The server aims the write at the figure the context binds -- a
  //  verified handle, never a request parameter -- so one page can praise one
  //  figure and fault another.
  const [feedbackContext, setFeedbackContext] = useState<unknown>(block.feedback_context);
  const [selection, setSelection] = useState<FeedbackSelection>(ANSWER_SELECTION);
  return (
    <section aria-label={"Figure " + (index + 1)}>
      {block.facts && block.facts.length > 0 ? (
        <dl>
          {block.facts.map((fact) => (
            <div key={fact.label} className="share-fact">
              <dt>{fact.label}</dt>
              <dd>{fact.value}</dd>
            </div>
          ))}
        </dl>
      ) : null}
      {block.render ? (
        <>
          <VisualizationRuntime
            input={{ ...block.render, profile: "share" }}
            onFeedbackTarget={(target, label) => setSelection({ target, label })}
          />
          <AiPathCapability
            projection={block.ai_path_evidence}
            surface="share"
            onFeedbackTarget={(target, label) => setSelection({ target, label })}
          />
          {feedbackContext != null ? (
            <TargetedFeedback
              context={feedbackContext}
              selection={selection}
              onClear={() => setSelection(ANSWER_SELECTION)}
              onContextChange={(next: ExactFeedbackContext) => setFeedbackContext(next)}
              onSubmit={onSubmitFeedback ?? submitFrozenShareFeedback}
              resetKey={
                (block.render.result?.result_id ?? "invalid") +
                ":" +
                (block.render.result?.content_hash ?? "invalid") +
                ":" +
                (block.render.spec?.visualization_spec_version_id ?? "invalid")
              }
            />
          ) : null}
        </>
      ) : (
        <VizStatePanel
          kind="unavailable"
          detail={block.unavailable?.title ?? "This shared figure carries no frozen values."}
          missingLink={block.unavailable?.missing_link ?? "app.render_frozen_payloads"}
        />
      )}
    </section>
  );
}

export function ShareDossier({
  dossier,
  onSubmitFeedback,
}: {
  dossier: ShareDossierInput;
  onSubmitFeedback?: ExactFeedbackSubmitter;
}) {
  return (
    <div data-viz-entry="share-dossier" className="w-full">
      <h1>{dossier.label}</h1>
      {dossier.blocks.map((block, index) => {
        if (block.kind === "narrative") {
          const author = block.authored_by === "model" ? "model" : "human";
          return (
            <div key={index} className="share-narrative" data-share-narrative-author={author}>
              <p>{block.text}</p>
              <p className="share-narrative-author">{NARRATIVE_AUTHOR_LABEL[author]}</p>
            </div>
          );
        }
        return (
          <ShareDossierFigure
            key={index}
            block={block}
            index={index}
            onSubmitFeedback={onSubmitFeedback}
          />
        );
      })}
    </div>
  );
}

export async function submitFrozenShareFeedback(request: ExactFeedbackRequest): Promise<unknown> {
  const response = await fetch("/api/render-shares/session/feedback", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
  });
  const body = await response.json().catch(() => null);
  if (!response.ok) throw new Error("Feedback was refused.");
  return body;
}

export default function ShareVisualization(props: ShareVisualizationProps) {
  const [dossier, setDossier] = useState<ShareDossierInput | null>(null);
  const [input, setInput] = useState<RenderInput | null>(props.input ?? null);
  const [aiPathEvidence, setAiPathEvidence] = useState<unknown>(props.aiPathEvidence);
  const [feedbackContext, setFeedbackContext] = useState<unknown>(props.feedbackContext);
  const [feedbackSelection, setFeedbackSelection] = useState<FeedbackSelection>(ANSWER_SELECTION);

  useEffect(() => {
    if (props.input) return;
    setDossier(readFrozenDossier());
    setInput(readFrozenRender());
    setAiPathEvidence(readFrozenAiPathEvidence());
    setFeedbackContext(readFrozenFeedbackContext());
    setFeedbackSelection(ANSWER_SELECTION);
  }, [props.input]);

  if (dossier) {
    return <ShareDossier dossier={dossier} onSubmitFeedback={props.onSubmitFeedback} />;
  }

  if (!input) {
    return (
      <VizStatePanel
        kind="unavailable"
        detail="This shared page carries no frozen result."
        missingLink="window.__TOOROW_FROZEN_RENDER__"
      />
    );
  }

  return (
    <div data-viz-entry="share" className="w-full">
      <VisualizationRuntime
        input={{ ...input, profile: "share" }}
        onFeedbackTarget={(target, label) => setFeedbackSelection({ target, label })}
      />
      <AiPathCapability
        projection={aiPathEvidence}
        surface="share"
        onFeedbackTarget={(target, label) => setFeedbackSelection({ target, label })}
      />
      <TargetedFeedback
        context={feedbackContext}
        selection={feedbackSelection}
        onClear={() => setFeedbackSelection(ANSWER_SELECTION)}
        onContextChange={(next: ExactFeedbackContext) => {
          setFeedbackContext(next);
          window.__TOOROW_FROZEN_FEEDBACK_CONTEXT__ = next;
        }}
        onSubmit={props.onSubmitFeedback ?? submitFrozenShareFeedback}
        resetKey={`${input.result?.result_id ?? "invalid"}:${input.result?.content_hash ?? "invalid"}:${input.spec?.visualization_spec_version_id ?? "invalid"}`}
      />
    </div>
  );
}

/** The serialized model this entry produces. Used by the parity proof. */
export function serializeForShare(input: RenderInput, scope?: Element | null) {
  return serializeVisualModel({ ...input, profile: "share" }, scope);
}
