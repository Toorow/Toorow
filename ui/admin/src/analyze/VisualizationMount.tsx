/**
 * Story 50.5 Task 9 -- the ONE Console-side adapter for the shared Visualization
 * runtime.
 *
 * It does three things and nothing else:
 *   1. fetches the Result evidence through the Story 50.1 retrieval route
 *      (`fetchResultEvidence`, `ui/admin/src/analyze/queryClient.ts`) -- there is
 *      no second path to a Result, and this component never reads a warehouse;
 *   2. hands the runtime the five-field envelope with `profile: "console"`;
 *   3. renders the runtime's own states while it waits or fails.
 *
 * IT COMPOSES THE RUNTIME; IT DOES NOT RE-IMPLEMENT ANY OF IT. No chart code, no
 * palette, no formatter and no state copy lives here: those are the runtime's, so
 * the Console, the MCP App and a shared link cannot drift (AC8).
 *
 * IT DOES NOT OPEN A WINDOW AND IT DOES NOT `postMessage`. The path it replaces --
 * `window.open(widget_uri)` plus twelve `postMessage` retries at 250 ms -- is
 * deleted in `RenderGalleryPage.tsx` by the same story (AC16a).
 *
 * MOUNTED SINCE 2026-08-04 by `analyze-artifacts/Renders.tsx` (the Render
 * Workbench, Result tab). Until then this file was built, tested and reachable by
 * nobody -- the reserve that sent Story 50.5 back from review. A replay needs
 * the closed Render input: all four build/policy pins, plus the frozen display
 * state, responsive profile, Result and immutable Spec version. Dropping any of
 * them would draw something that is not what was preserved and call it a replay.
 */

import { useEffect, useState } from "react";
import {
  VisualizationRuntime,
  TargetedFeedback,
  VizStatePanel,
  FORMATTER_VERSION,
  RUNTIME_BUILD,
  THEME_VERSION,
  resolveRenderer,
  type DisplayState,
  type RenderInput,
  type RenderPins,
  type RequestNewExecution,
  type ResponsiveProfile,
  type VizResult,
  type VizSpec,
  type ExactFeedbackRequest,
  type FeedbackSelection,
} from "@toorow/card-shell/viz";

import { fetchResultEvidence, submitExactFeedback } from "./queryClient";

const ANSWER_SELECTION: FeedbackSelection = { target: { kind: "answer" }, label: "Answer" };

export interface VisualizationMountProps {
  projectId: string;
  resultId: string;
  /** Present for a replay so the server signs the exact immutable Render pins. */
  renderId?: string | null;
  /** The immutable Visualization Spec version this visual is pinned to. */
  spec: VizSpec;
  /** For replay, the immutable Result hash frozen by the Render. */
  expectedResultContentHash?: string | null;
  /** Only for a REPLAY: all four identities the Render froze. Absent for a live view. */
  pins?: RenderPins | null;
  /**
   * Only for a REPLAY: the display state the Render froze -- the legend hides,
   * the selection and the local zoom that were on screen when it was preserved.
   * It is one of the ten replay pins, so a replay that drops it shows a series
   * the reader had hidden and is therefore NOT the preserved visual. Absent on a
   * live view, where the reader starts from the spec's own defaults.
   */
  display?: DisplayState;
  /**
   * The responsive profile to lay out at. `console` is the default because this
   * adapter is the Console's; a REPLAY passes the profile the Render pinned
   * instead, since the same rows at a different profile are a different visual.
   * The caller resolves an unknown stored value (`resolveProfile`) and says so on
   * screen -- this component never silently substitutes one.
   */
  profile?: ResponsiveProfile;
  /**
   * The local display state the reader produced (legend hides, selection, local
   * zoom). Forwarded, never interpreted: this component does not decide what a
   * legend toggle means, and it does not persist anything -- a screen that wants
   * to pin the state into a Render receives it here and does that itself.
   */
  onDisplayChange?: (display: DisplayState) => void;
  /**
   * The typed intent the runtime emits when an interaction would need a row the
   * Result did not return. It is NOT handled here: the runtime computes nothing,
   * and routing this to a Story 50.1 execution is the calling screen's job,
   * because only the screen knows which Query Spec version to run and where to
   * put the new Result. Absent means the affordance is not offered at all --
   * which is the honest state on a surface that cannot execute.
   */
  onRequestNewExecution?: (request: RequestNewExecution) => void;
}

type State =
  | { status: "loading" }
  | { status: "ready"; result: VizResult; feedbackContext: unknown }
  | { status: "refused"; message: string; nextAction: string }
  | { status: "error"; message: string }
  | { status: "denied" };

export default function VisualizationMount(props: VisualizationMountProps) {
  const {
    projectId,
    resultId,
    renderId,
    spec,
    expectedResultContentHash,
    pins,
    display,
    profile = "console",
    onDisplayChange,
    onRequestNewExecution,
  } = props;
  const [state, setState] = useState<State>({ status: "loading" });
  const [feedbackSelection, setFeedbackSelection] = useState<FeedbackSelection>(ANSWER_SELECTION);

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();
    setState({ status: "loading" });
    setFeedbackSelection(ANSWER_SELECTION);
    fetchResultEvidence(projectId, resultId, { signal: controller.signal }, renderId)
      .then((evidence) => {
        if (cancelled) return;
        if (
          expectedResultContentHash &&
          evidence.content_hash !== expectedResultContentHash
        ) {
          setState({
            status: "refused",
            message: "The Result no longer matches this frozen Render.",
            nextAction: "Open the Result and create a new Render.",
          });
          return;
        }
        const { outcome, row_count: rowCount, truncated } = evidence;
        if (
          (outcome !== "success" &&
            outcome !== "empty" &&
            outcome !== "degraded" &&
            outcome !== "unavailable" &&
            outcome !== "refused") ||
          typeof rowCount !== "number" ||
          !Number.isInteger(rowCount) ||
          rowCount < 0 ||
          typeof truncated !== "boolean"
        ) {
          throw new Error("Result evidence is missing outcome, row_count or truncated");
        }
        // Outcome and completeness are Result facts. Inferring them from rows
        // turns a degraded/refused Result into success and drops truncation.
        setState({
          status: "ready",
          result: {
            result_id: evidence.result_id,
            content_hash: evidence.content_hash,
            outcome,
            schema: evidence.schema,
            rows: evidence.rows as VizResult["rows"],
            manifest: evidence.manifest as VizResult["manifest"],
            row_count: rowCount,
            truncated,
          },
          feedbackContext: evidence.feedback_context,
        });
      })
      .catch((error: unknown) => {
        if (cancelled || controller.signal.aborted) return;
        const message = error instanceof Error ? error.message : "The result could not be loaded.";
        setState(
          /403|401|denied|forbidden/i.test(message)
            ? { status: "denied" }
            : { status: "error", message },
        );
      });
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [expectedResultContentHash, projectId, renderId, resultId]);

  if (state.status === "loading") {
    return <VizStatePanel kind="loading" detail="Retrieving the immutable result." />;
  }
  if (state.status === "denied") {
    return <VizStatePanel kind="denied" />;
  }
  if (state.status === "error") {
    return <VizStatePanel kind="errored" detail={state.message} />;
  }
  if (state.status === "refused") {
    return (
      <VizStatePanel
        kind="refused"
        detail={state.message}
        nextAction={state.nextAction}
      />
    );
  }

  // The renderer this build would use for this family; its build identity is one
  // of the four pins. Resolving it here rather than hardcoding a string keeps the
  // pin and the registry from drifting apart.
  const resolution = resolveRenderer(spec.document.family, spec.schema_version, "console");
  const rendererBuild = resolution.kind === "renderer" ? resolution.renderer.build : "";

  const input: RenderInput = {
    result: state.result,
    spec,
    pins:
      pins ??
      {
        theme_version: THEME_VERSION,
        formatter_version: FORMATTER_VERSION,
        renderer_build: rendererBuild,
        runtime_build: RUNTIME_BUILD,
      },
    profile,
    display,
  };

  const submitFeedback = (request: ExactFeedbackRequest) => submitExactFeedback(projectId, request);

  return (
    <div className="flex flex-col gap-3">
      <VisualizationRuntime
        input={input}
        onDisplayChange={onDisplayChange}
        onRequestNewExecution={onRequestNewExecution}
        onFeedbackTarget={(target, label) => setFeedbackSelection({ target, label })}
      />
      <TargetedFeedback
        context={state.feedbackContext}
        selection={feedbackSelection}
        onClear={() => setFeedbackSelection(ANSWER_SELECTION)}
        onSubmit={submitFeedback}
        resetKey={`${state.result.result_id}:${state.result.content_hash}:${renderId ?? "live"}:${spec.visualization_spec_version_id}`}
      />
    </div>
  );
}
