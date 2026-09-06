/**
 * Story 50.5 AC8/AC9 -- the MCP App mount entry, bundled as ONE self-contained
 * resource (AD-11, `ARCHITECTURE-SPINE.md:105`).
 *
 * IT REUSES THE EXISTING SHARED CHANNEL and does not invent a second one:
 * `readInjectedEnvelope` and `connectMcpApp` from `@toorow/shell`
 * (`ui/shell/src/mcpApp.ts`) already handle the SDK path, the legacy path and the
 * sandboxed-frame origin cases. That module is transport, not rendering, so it is
 * preserved and reused rather than superseded.
 *
 * IT FEATURE-DETECTS THE HOST and falls back cleanly:
 *   - display mode -> `mcp-inline` / `mcp-fullscreen` / `mcp-pip`, the three the
 *     ratified Surface parity table names. Picture-in-picture is DRAWN since
 *     2026-08-24, not substituted: `profileFromDisplayMode` hands `mcp-pip` to the
 *     one shared runtime like any other profile, and only the four layout fields
 *     of `profileLayout` differ. A host asking for a profile no build implements
 *     still gets the stated substitution (`responsive.ts`), which is the mechanism
 *     PiP used to need and no longer does;
 *   - colour scheme -> the host's `data-color-scheme`, read as a CSS custom
 *     property scope, exactly like the Console -- there is no MUI ThemeProvider
 *     here and no second theme carrier;
 *   - safe area and max height -> a container the chart measures itself against,
 *     because the viewport belongs to the host and says nothing about the space
 *     this widget was given.
 *
 * THERE IS NO NESTED TOOROW FRAME. This entry mounts React into the host's own
 * frame, and creates no embedded browsing context and no second document of any
 * kind; the guard test asserts that by grep over the whole runtime.
 */

import { useEffect, useRef, useState } from "react";
// The MODULE, not the package barrel. `@toorow/shell`'s index re-exports
// `WidgetShell`, the theme and the DOM primitives, and pulls in the four font
// packages, so a barrel import would drag all of that into a bundle whose whole
// point is to be small (AC12 size ceiling, AC18 module graph). MUI is no longer
// among what it would drag — it left the repository on 2026-08-05 (AI-212) — but
// the fonts and the primitives still are. The transport module itself has no
// such dependency.
import {
  APP_PAYLOAD_META_KEY,
  connectMcpApp,
  readInjectedEnvelope,
  RESULT_META_KEY,
} from "@toorow/shell/src/mcpApp";

import VisualizationRuntime, { serializeVisualModel } from "../Runtime";
import AiPathCapability from "../aiPathCapability";
import type { RenderInput, ResponsiveProfile } from "../contracts";
import PivotMatrix, {
  decodePivotMatrix,
  type PivotContext,
  type PivotMatrixProjection,
} from "../pivotMatrix";
import { resolveProfile } from "../responsive";
import { VizStatePanel } from "../states";
import TargetedFeedback, {
  decodeExactFeedbackContext,
  type ExactFeedbackRequest,
  type FeedbackSelection,
} from "../targetedFeedback";
import {
  decodeInitialResultSlice,
  reduceResultSlice,
  resultSliceRequest,
  RESULT_SLICE_TOOL,
  type ResultSliceWindow,
} from "./resultSliceDelivery";

/** The host's runtime envelope. AI Path evidence stays beside it in Result meta. */
interface McpEnvelope {
  data?: unknown;
  deep_link?: Record<string, unknown>;
  _meta?: { "toorow/display_mode"?: string; "toorow/color_scheme"?: string };
}

const FEEDBACK_META_KEY = "toorow.feedback";
const PIVOT_META_KEY = "toorow.pivot";
const SUBMIT_FEEDBACK_TOOL = "submit_analyze_feedback";
const ANSWER_SELECTION: FeedbackSelection = { target: { kind: "answer" }, label: "Answer" };

function feedbackContextFromMeta(meta?: Record<string, unknown>): unknown {
  return meta?.[FEEDBACK_META_KEY];
}

function resultMeta(result: unknown): Record<string, unknown> | undefined {
  if (!result || typeof result !== "object" || Array.isArray(result)) return undefined;
  const meta = (result as { _meta?: unknown })._meta;
  return meta && typeof meta === "object" && !Array.isArray(meta)
    ? (meta as Record<string, unknown>)
    : undefined;
}

type RenderDelivery =
  | { status: "waiting" }
  | { status: "ready"; input: RenderInput }
  | { status: "error"; message: string };

type SliceDelivery =
  | { status: "none" }
  | { status: "ready" | "loading"; window: ResultSliceWindow }
  | { status: "error"; window: ResultSliceWindow; message: string; canRetry: boolean };

function pivotContext(value: unknown): PivotContext {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  const envelope = value as Record<string, unknown>;
  return {
    sources: Array.isArray(envelope.sources) ? envelope.sources as PivotContext["sources"] : [],
    common_keys: Array.isArray(envelope.common_keys)
      ? envelope.common_keys as PivotContext["common_keys"]
      : [],
    analysis_context:
      envelope.analysis_context && typeof envelope.analysis_context === "object"
        ? envelope.analysis_context as PivotContext["analysis_context"]
        : null,
  };
}

export function decodeRenderAppPayload(
  envelope: McpEnvelope,
  meta?: Record<string, unknown>,
): RenderDelivery {
  const candidate = meta?.[APP_PAYLOAD_META_KEY];
  if (candidate && typeof candidate === "object") {
    const wrapper = candidate as Record<string, unknown>;
    if (wrapper.kind !== "render") {
      if (envelope.data && typeof envelope.data === "object") {
        return { status: "ready", input: envelope.data as RenderInput };
      }
      return {
        status: "error",
        message: "The host delivered a result without a render payload. Retry the analysis or open the Result workbench.",
      };
    }
    if (
      typeof wrapper.schema_version === "number" &&
      Number.isInteger(wrapper.schema_version) &&
      wrapper.schema_version > 1
    ) {
      return {
        status: "error",
        message: "This visualization payload needs a newer runtime. Upgrade or retry the analysis.",
      };
    }
    const allowed = new Set(["schema_version", "kind", "render_input", "moved"]);
    if (
      wrapper.schema_version !== 1 ||
      Object.keys(wrapper).some((key) => !allowed.has(key)) ||
      !wrapper.render_input ||
      typeof wrapper.render_input !== "object"
    ) {
      return {
        status: "error",
        message: "The host delivered a malformed render payload. Retry the analysis.",
      };
    }
    return { status: "ready", input: wrapper.render_input as RenderInput };
  }
  if (envelope.data && typeof envelope.data === "object") {
    return { status: "ready", input: envelope.data as RenderInput };
  }
  return {
    status: "error",
    message: "The host delivered a result without a render payload. Retry the analysis or open the Result workbench.",
  };
}

function RenderDeliveryError({ message }: { message: string }) {
  return (
    <div
      role="alert"
      aria-live="assertive"
      data-viz-entry-error="true"
      className="my-3 flex flex-col gap-2 rounded-md border-l-4 border-l-red-600 px-4 py-3 text-sm"
    >
      <p className="m-0 font-semibold">This visualization could not be opened</p>
      <p className="m-0 opacity-90">{message}</p>
      <p className="m-0 opacity-75">Retry once. If it still fails, open the Result workbench.</p>
    </div>
  );
}

/** Read the canonical projection without interpreting it; the shared capability validates it. */
function aiPathEvidenceFromMeta(meta?: Record<string, unknown>): unknown {
  const result = meta?.[RESULT_META_KEY];
  if (!result || typeof result !== "object" || Array.isArray(result)) return undefined;
  return (result as Record<string, unknown>).ai_path_walk;
}

export interface McpAppVisualizationProps {
  /** Supplied by a test or a host that already has the envelope. */
  input?: RenderInput | null;
  /** `inline` | `fullscreen` | `pip`, as the host reports it. */
  displayMode?: string | null;
  /** Canonical observed-ai-path.v1 projection supplied directly by a host or test. */
  aiPathEvidence?: unknown;
  /** Server-projected pivot supplied directly by a host or test. */
  pivotEvidence?: unknown;
  /** Host navigation seam: receives the structured reference, never an invented URL. */
  onOpenResult?: (ownerReference: Record<string, unknown>) => void | Promise<void>;
}

function ownerNavigationTargetOrigin(): string {
  const origins = (
    document.location as Location & { ancestorOrigins?: { readonly length: number; 0?: string } }
  ).ancestorOrigins;
  return origins && origins.length > 0 && origins[0] ? origins[0] : "*";
}

/** Ask the embedding host to resolve the canonical owner reference it already owns. */
export function requestHostOwnerNavigation(ownerReference: Record<string, unknown>): void {
  window.parent.postMessage(
    { type: "toorow:openOwnerReference", owner_reference: ownerReference },
    ownerNavigationTargetOrigin(),
  );
}

function profileFromDisplayMode(mode: string | null | undefined): {
  profile: ResponsiveProfile;
  substitution: string | null;
} {
  const requested =
    mode === "fullscreen"
      ? "mcp-fullscreen"
      : mode === "pip"
        ? "mcp-pip"
        : "mcp-inline";
  return resolveProfile(requested);
}

export default function McpAppVisualization(props: McpAppVisualizationProps) {
  const [delivery, setDelivery] = useState<RenderDelivery>(
    props.input ? { status: "ready", input: props.input } : { status: "waiting" },
  );
  const [aiPath, setAiPath] = useState<{ delivered: boolean; projection: unknown }>({
    delivered: props.aiPathEvidence !== undefined,
    projection: props.aiPathEvidence,
  });
  const [pivot, setPivot] = useState<
    | { status: "absent" }
    | { status: "ready"; matrix: PivotMatrixProjection; context: PivotContext }
    | { status: "error"; message: string }
  >(() => {
    if (!props.input || props.pivotEvidence === undefined) return { status: "absent" };
    try {
      const matrix = decodePivotMatrix(
        props.pivotEvidence,
        props.input.result.result_id,
        props.input.result.content_hash,
      );
      return matrix
        ? { status: "ready", matrix, context: pivotContext(props.pivotEvidence) }
        : { status: "absent" };
    } catch (error) {
      return {
        status: "error",
        message: error instanceof Error ? error.message : "Malformed pivot delivery.",
      };
    }
  });
  const [resultPresentation, setResultPresentation] = useState<"pivot" | "visualization">(
    props.pivotEvidence === undefined ? "visualization" : "pivot",
  );
  const [slice, setSlice] = useState<SliceDelivery>({ status: "none" });
  const [requestActive, setRequestActive] = useState(false);
  const [openResultStatus, setOpenResultStatus] = useState<string | null>(null);
  const [feedbackContext, setFeedbackContext] = useState<unknown>(undefined);
  const [feedbackSelection, setFeedbackSelection] = useState<FeedbackSelection>(ANSWER_SELECTION);
  const connectionRef = useRef<ReturnType<typeof connectMcpApp> | null>(null);
  const activeRequest = useRef<symbol | null>(null);
  const deliveryGeneration = useRef(0);

  useEffect(() => {
    if (props.input) return;
    // The legacy global first (older hosts, standalone dev), then the SHARED
    // channel subscription: a real MCP Apps host delivers the tool result as a
    // `ui/notifications/tool-result` notification, which `connectMcpApp` turns
    // into (envelope, meta) -- and the walk travels in the meta half. Reading
    // only the injected global, as this entry used to, is why nothing the
    // server sent ever reached the drawing.
    const envelope = readInjectedEnvelope<McpEnvelope>();
    if (envelope) setDelivery(decodeRenderAppPayload(envelope));
    const handle = connectMcpApp();
    connectionRef.current = handle;
    const unsubscribeResult = handle.onToolResult<McpEnvelope>((next, meta) => {
      deliveryGeneration.current += 1;
      activeRequest.current = null;
      setRequestActive(false);
      setOpenResultStatus(null);
      setFeedbackSelection(ANSWER_SELECTION);
      setFeedbackContext(feedbackContextFromMeta(meta));
      const decoded = decodeRenderAppPayload(next, meta);
      if (decoded.status === "ready") {
        try {
          const matrix = decodePivotMatrix(
            meta?.[PIVOT_META_KEY],
            decoded.input.result.result_id,
            decoded.input.result.content_hash,
          );
          setPivot(matrix
            ? { status: "ready", matrix, context: pivotContext(meta?.[PIVOT_META_KEY]) }
            : { status: "absent" });
          setResultPresentation(matrix ? "pivot" : "visualization");
        } catch (error) {
          setPivot({
            status: "error",
            message: error instanceof Error ? error.message : "Malformed pivot delivery.",
          });
        }
        try {
          const large = decodeInitialResultSlice(
            decoded.input,
            next as Record<string, unknown>,
            meta,
          );
          if (large) {
            setDelivery({ status: "ready", input: large.input });
            setSlice({ status: "ready", window: large });
          } else {
            setDelivery(decoded);
            setSlice({ status: "none" });
          }
        } catch (error) {
          setDelivery({
            status: "error",
            message: error instanceof Error ? error.message : "Malformed large Result delivery.",
          });
          setSlice({ status: "none" });
        }
      } else {
        setDelivery(decoded);
        setSlice({ status: "none" });
        setPivot({ status: "absent" });
      }
      setAiPath({ delivered: true, projection: aiPathEvidenceFromMeta(meta) });
    });
    const unsubscribeError = handle.onToolResultError(({ message }) => {
      deliveryGeneration.current += 1;
      activeRequest.current = null;
      setRequestActive(false);
      setDelivery({ status: "error", message });
      setSlice({ status: "none" });
      setAiPath({ delivered: true, projection: undefined });
      setPivot({ status: "absent" });
      setFeedbackSelection(ANSWER_SELECTION);
      setFeedbackContext(undefined);
    });
    return () => {
      deliveryGeneration.current += 1;
      activeRequest.current = null;
      connectionRef.current = null;
      unsubscribeResult();
      unsubscribeError();
    };
  }, [props.input]);

  const loadNextSlice = async () => {
    if (activeRequest.current || slice.status === "none") return;
    const window = slice.window;
    if (!window.nextCursor || !connectionRef.current) return;
    const requestToken = Symbol("result-slice-request");
    activeRequest.current = requestToken;
    setRequestActive(true);
    const generation = deliveryGeneration.current;
    setSlice({ status: "loading", window });
    try {
      const sentFeedbackContext = decodeExactFeedbackContext(feedbackContext);
      const result = await connectionRef.current.callServerTool(
        RESULT_SLICE_TOOL,
        resultSliceRequest(
          window,
          sentFeedbackContext as unknown as Record<string, unknown> | null,
        ),
      );
      if (generation !== deliveryGeneration.current) return;
      if (!result) {
        setSlice({
          status: "error",
          window,
          canRetry: false,
          message: "This host cannot load another page here.",
        });
        return;
      }
      const nextMeta = resultMeta(result);
      const nextContext = feedbackContextFromMeta(nextMeta);
      if (sentFeedbackContext && !decodeExactFeedbackContext(nextContext)) {
        throw new Error("The next page did not carry an exact feedback context.");
      }
      const next = reduceResultSlice(window, result);
      setDelivery({ status: "ready", input: next.input });
      setSlice({ status: "ready", window: next });
      setFeedbackContext(sentFeedbackContext ? nextContext : undefined);
      setFeedbackSelection(ANSWER_SELECTION);
    } catch (error) {
      if (generation !== deliveryGeneration.current) return;
      setSlice({
        status: "error",
        window,
        canRetry: true,
        message: error instanceof Error ? error.message : "The next page could not be loaded.",
      });
    } finally {
      if (activeRequest.current === requestToken) {
        activeRequest.current = null;
        setRequestActive(false);
      }
    }
  };

  const openResult = async () => {
    if (slice.status === "none") return;
    try {
      if (props.onOpenResult) {
        await props.onOpenResult(slice.window.ownerReference);
        setOpenResultStatus("Result open request sent to the host.");
      } else {
        requestHostOwnerNavigation(slice.window.ownerReference);
        setOpenResultStatus("Result open request sent to the host.");
      }
    } catch {
      setOpenResultStatus("The Result could not be opened in this host.");
    }
  };

  const aiPathSurface = props.displayMode === "fullscreen" ? "fullscreen" : "inline";
  const feedbackInteractionRef =
    decodeExactFeedbackContext(feedbackContext)?.interaction_ref ?? "unavailable";

  const submitFeedback = async (request: ExactFeedbackRequest): Promise<unknown> => {
    const result = await connectionRef.current?.callServerTool(SUBMIT_FEEDBACK_TOOL, { ...request });
    return result?.structuredContent ?? null;
  };

  if (delivery.status !== "ready") {
    // Visualization and path evidence fail independently: one never hides the other.
    return (
      <div data-viz-entry="mcp-app" className="w-full">
        {delivery.status === "waiting" ? (
          <VizStatePanel kind="loading" detail="Waiting for the host to deliver the result." />
        ) : (
          <RenderDeliveryError message={delivery.message} />
        )}
        {aiPath.delivered ? (
          <AiPathCapability projection={aiPath.projection} surface={aiPathSurface} />
        ) : null}
      </div>
    );
  }

  const { profile, substitution } = profileFromDisplayMode(props.displayMode);

  return (
    <div data-viz-entry="mcp-app" className="w-full">
      {substitution ? <VizStatePanel kind="partial" detail={substitution} /> : null}
      {slice.status !== "none" ? (
        <div
          data-viz-slice-status
          role="status"
          aria-live="polite"
          className="my-3 flex flex-wrap items-center gap-3 text-sm"
        >
          <span>
            {slice.window.input.result.rows.length === 0
              ? `No rows in this window (${slice.window.totalRows} total).`
              : `Showing rows ${slice.window.offset + 1}–${
                  slice.window.offset + slice.window.input.result.rows.length
                } of ${slice.window.totalRows}.`}
          </span>
          <span>
            Bounded slice of a frozen Result.
            {slice.window.input.result.truncated ? " The source Result is truncated." : ""}
          </span>
          {slice.status === "ready" && !slice.window.hasMore ? (
            <span>No more rows in this Result.</span>
          ) : null}
          {slice.status === "error" ? (
            <span role="alert">
              {slice.message} Open the Result workbench.
            </span>
          ) : null}
          {slice.status === "error" && slice.canRetry ? (
            <button type="button" data-viz-slice-retry onClick={loadNextSlice}>
              Retry
            </button>
          ) : null}
          {slice.status !== "error" && slice.window.hasMore ? (
            <button
              type="button"
              data-viz-slice-next
              disabled={slice.status === "loading" || requestActive}
              onClick={loadNextSlice}
            >
              {slice.status === "loading" || requestActive ? "Loading…" : "Next"}
            </button>
          ) : null}
          <button type="button" data-viz-open-result onClick={openResult}>
            Open Result
          </button>
          {openResultStatus ? <span>{openResultStatus}</span> : null}
        </div>
      ) : null}
      {pivot.status === "error" ? <RenderDeliveryError message={pivot.message} /> : null}
      {pivot.status === "ready" ? (
        <div className="my-3 flex flex-wrap gap-2" role="group" aria-label="Result presentation">
          <button
            type="button"
            aria-pressed={resultPresentation === "pivot"}
            onClick={() => setResultPresentation("pivot")}
          >
            Pivot
          </button>
          <button
            type="button"
            aria-pressed={resultPresentation === "visualization"}
            onClick={() => setResultPresentation("visualization")}
          >
            {delivery.input.spec.document.family === "table" ? "Table" : "Chart"}
          </button>
        </div>
      ) : null}
      {pivot.status === "ready" && resultPresentation === "pivot" ? (
        <PivotMatrix matrix={pivot.matrix} context={pivot.context} />
      ) : null}
      {pivot.status !== "ready" || resultPresentation === "visualization" ? (
        <VisualizationRuntime
          input={{ ...delivery.input, profile }}
          datumRowOffset={slice.status === "none" ? 0 : slice.window.offset}
          onFeedbackTarget={(target, label) => setFeedbackSelection({ target, label })}
        />
      ) : null}
      {aiPath.delivered ? (
        <AiPathCapability
          projection={aiPath.projection}
          surface={aiPathSurface}
          onFeedbackTarget={(target, label) => setFeedbackSelection({ target, label })}
        />
      ) : null}
      <TargetedFeedback
        context={feedbackContext}
        selection={feedbackSelection}
        onClear={() => setFeedbackSelection(ANSWER_SELECTION)}
        onContextChange={setFeedbackContext}
        onSubmit={submitFeedback}
        resetKey={`${delivery.input.result.result_id}:${delivery.input.result.content_hash}:${slice.status === "none" ? 0 : slice.window.offset}:${feedbackInteractionRef}`}
      />
    </div>
  );
}

/** The serialized model this entry produces. Used by the parity proof. */
export function serializeForMcpApp(
  input: RenderInput,
  displayMode?: string | null,
  scope?: Element | null,
) {
  // The REQUESTED profile is passed through, not the resolved one, so the
  // serialized model carries the substitution statement a reader must see.
  const requested =
    displayMode === "fullscreen"
      ? "mcp-fullscreen"
      : displayMode === "pip"
        ? "mcp-pip"
        : "mcp-inline";
  return serializeVisualModel({ ...input, profile: requested as RenderInput["profile"] }, scope);
}
