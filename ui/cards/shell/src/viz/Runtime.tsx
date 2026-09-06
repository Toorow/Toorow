/**
 * Story 50.5 -- THE single public entry of the shared Visualization runtime.
 *
 * There is exactly one exported component and exactly one exported compile
 * function, and neither has a second overload that accepts a bare options object
 * (AC2, asserted by `__tests__/validate.test.ts`). Everything a caller may supply
 * is `RenderInput`; everything else is refused before a renderer is touched.
 *
 * THE ORDER OF OPERATIONS MATTERS AND IS FIXED:
 *   1. resolve the responsive profile (never inferred from the viewport)
 *   2. resolve the renderer from (family, schema_version, profile)
 *   3. validate the envelope AGAINST THAT RENDERER
 *   4. read the live theme, compile the model
 *   5. hand the renderer validated runtime input, and nothing else
 *
 * A refusal at any step renders a named state, never a blank canvas -- AND, when
 * the refusal message says a table is below, an actual table (AC7). That last
 * clause is not decoration: the two refusal messages in `registry.ts` both end
 * with "The accessible table below shows every returned row", and this component
 * used to leave the renderer null and draw nothing under them.
 *
 * LOCAL DISPLAY STATE LIVES HERE (AC10). The legend entries are real buttons
 * wired through `onToggleSeries` to `display.legendHidden`, and every change is
 * reported to the host through `onDisplayChange` so a Render can pin it. An
 * interaction that would need rows the Result did not return computes nothing:
 * it emits one typed `requestNewExecution`.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type {
  DisplayState,
  RenderInput,
  RequestNewExecution,
  VizRefusal,
} from "./contracts";
import { compileVisualModel, type CompiledVisualModel } from "./compile/dataset";
import { DEFAULT_VOLUME_CONSTRAINTS } from "./compile/limits";
import { resolveDatumEvidence, type DatumEvidenceResolution } from "./evidence/resolve";
import { getVizPalette, readCssVizTheme, type VizPalette } from "../vizTheme";
import { installStandardRenderers } from "./renderers";
import { resolveRenderer, type RendererDeclaration } from "./registry";
import { resolveProfile, CONTAINER_REFLOW_PX } from "./responsive";
import { validateRenderInput } from "./validate";
import { isBanner, VizStatePanel, type VizStateKind } from "./states";
import {
  checkBuildIdentity,
  FORMATTER_VERSION,
  RUNTIME_BUILD,
  THEME_VERSION,
} from "./buildInfo";
import EvidencePath from "./renderers/evidencePath";
import TableFallback, { columnsFromResult } from "./renderers/tableFallback";
import type { FeedbackTarget } from "./targetedFeedback";

installStandardRenderers();

export interface VisualizationRuntimeProps {
  input: RenderInput;
  /** Emitted when an interaction would need a NEW Result. The runtime computes nothing. */
  onRequestNewExecution?: (request: RequestNewExecution) => void;
  /** Local display state is owned by the host so a Render can pin it. */
  onDisplayChange?: (display: DisplayState) => void;
  /** Overridable for tests and for a server-side parity check. */
  containerWidthPx?: number;
  onFeedbackTarget?: (target: FeedbackTarget, label: string) => void;
  /** Global Result offset of the immutable window currently displayed. */
  datumRowOffset?: number;
}

/** What `serializeVisualModel` returns -- the object AC8 deep-compares. */
export interface SerializedVisualModel {
  family: string;
  dataset: CompiledVisualModel["dataset"];
  encode: CompiledVisualModel["encode"];
  series: CompiledVisualModel["series"];
  axes: CompiledVisualModel["axes"];
  legend: CompiledVisualModel["legend"];
  formats: CompiledVisualModel["formats"];
  colors: CompiledVisualModel["colors"];
  datumKeys: string[];
  datumFields: Record<string, string[]>;
  datumRowIndex: Record<string, number>;
  datumTargets: Record<string, { row_index: number; field: string }>;
  datumLabels: Record<string, string>;
  disclosures: CompiledVisualModel["disclosures"];
  tableColumns: CompiledVisualModel["tableColumns"];
  volume: CompiledVisualModel["volume"];
  rendererId: string | null;
  rendererBuild: string | null;
  refusals: VizRefusal[];
  profileSubstitution: string | null;
}

interface Prepared {
  refusals: VizRefusal[];
  renderer: RendererDeclaration | null;
  refusal: { code: string; message: string } | null;
  model: CompiledVisualModel | null;
  palette: VizPalette;
  profileSubstitution: string | null;
  profile: RenderInput["profile"];
}

/**
 * The whole pipeline, without React. Exported so the parity proof can compare
 * three surfaces without mounting three DOM trees, and so a test can assert a
 * refusal without asserting on markup.
 */
export function prepareVisualization(
  input: RenderInput,
  themeScope?: Element | null,
): Prepared {
  const profileResolution = resolveProfile(input?.profile);
  const palette = getVizPalette(readCssVizTheme(themeScope ?? null));

  const family = input?.spec?.document?.family;
  const schemaVersion = input?.spec?.schema_version;
  const resolution =
    typeof family === "string" && typeof schemaVersion === "number"
      ? resolveRenderer(family, schemaVersion, profileResolution.profile)
      : null;

  const renderer = resolution?.kind === "renderer" ? resolution.renderer : null;
  const refusal = resolution?.kind === "refused" ? resolution : null;

  const { refusals } = validateRenderInput(input, renderer);
  if (refusals.length > 0 || !renderer || !input?.spec?.document) {
    return {
      refusals,
      renderer,
      refusal,
      model: null,
      palette,
      profileSubstitution: profileResolution.substitution,
      profile: profileResolution.profile,
    };
  }

  const model = compileVisualModel(input.result, input.spec.document, {
    palette,
    limits: renderer.volume ?? DEFAULT_VOLUME_CONSTRAINTS,
  });

  return {
    refusals,
    renderer,
    refusal,
    model,
    palette,
    profileSubstitution: profileResolution.substitution,
    profile: profileResolution.profile,
  };
}

/**
 * AC8 -- the serialized visual model. The three entries produce this from the same
 * envelope, and the parity test deep-compares it with `profile` excluded.
 */
export function serializeVisualModel(
  input: RenderInput,
  themeScope?: Element | null,
): SerializedVisualModel {
  const prepared = prepareVisualization(input, themeScope);
  const model = prepared.model;
  return {
    family: model?.family ?? (input?.spec?.document?.family ?? "unknown"),
    dataset: model?.dataset ?? { dimensions: [], source: [] },
    encode: model?.encode ?? [],
    series: model?.series ?? [],
    axes:
      model?.axes ?? ({} as CompiledVisualModel["axes"]),
    legend: model?.legend ?? { position: "none", visible: false, entries: [] },
    formats: model?.formats ?? { numberStyle: "auto", dateStyle: "auto", unit: null },
    colors: model?.colors ?? { accent: "", semanticDirection: "none", palette: [] },
    datumKeys: model?.datumKeys ?? [],
    datumFields: model?.datumFields ?? {},
    datumRowIndex: model?.datumRowIndex ?? {},
    datumTargets: model?.datumTargets ?? {},
    datumLabels: model?.datumLabels ?? {},
    disclosures:
      model?.disclosures ?? ({} as CompiledVisualModel["disclosures"]),
    tableColumns: model?.tableColumns ?? [],
    volume: model?.volume ?? ({} as CompiledVisualModel["volume"]),
    rendererId: prepared.renderer?.renderer_id ?? null,
    rendererBuild: prepared.renderer?.build ?? null,
    refusals: prepared.refusals,
    profileSubstitution: prepared.profileSubstitution,
  };
}

/** Map a Result outcome to the runtime state that presents it. */
function outcomeState(input: RenderInput): VizStateKind | null {
  const outcome = input?.result?.outcome;
  if (outcome === "unavailable") return "unavailable";
  if (outcome === "refused") return "refused";
  if (outcome === "degraded") return "degraded";
  if (outcome === "empty") return "empty";
  if (outcome === "success") return null;
  return "unknown";
}

export default function VisualizationRuntime(props: VisualizationRuntimeProps) {
  const {
    input,
    onRequestNewExecution,
    onDisplayChange,
    onFeedbackTarget,
    datumRowOffset = 0,
    containerWidthPx,
  } = props;
  const host = useRef<HTMLDivElement | null>(null);
  const evidenceInvoker = useRef<HTMLElement | null>(null);
  const [width, setWidth] = useState(containerWidthPx ?? CONTAINER_REFLOW_PX);
  const [openEvidenceKey, setOpenEvidenceKey] = useState<string | null>(null);
  const [hoveredKey, setHoveredKey] = useState<string | null>(null);

  // AC10 -- the LIVE local display state. The caller seeds it and the host can
  // pin it (that is what `onDisplayChange` is for); between those two moments it
  // lives here, because a control that only reported an intent and never changed
  // what is drawn is not a wired control.
  const [display, setDisplay] = useState<DisplayState>(input?.display ?? {});
  useEffect(() => {
    setDisplay(input?.display ?? {});
  }, [input?.display]);

  const onToggleSeries = useCallback(
    (seriesId: string) => {
      setDisplay((current) => {
        const hidden = new Set(current.legendHidden ?? []);
        if (hidden.has(seriesId)) hidden.delete(seriesId);
        else hidden.add(seriesId);
        const next: DisplayState = { ...current, legendHidden: [...hidden] };
        onDisplayChange?.(next);
        return next;
      });
    },
    [onDisplayChange],
  );

  useEffect(() => {
    if (containerWidthPx !== undefined) {
      setWidth(containerWidthPx);
      return undefined;
    }
    const node = host.current;
    if (!node || typeof ResizeObserver !== "function") return undefined;
    const observer = new ResizeObserver((entries) => {
      const box = entries[0]?.contentRect;
      if (box) setWidth(box.width);
    });
    observer.observe(node);
    return () => observer.disconnect();
  }, [containerWidthPx]);

  const prepared = useMemo(
    () => prepareVisualization(input, host.current),
    // Recompiling on every render would rebuild the dataset for no reason; the
    // envelope is immutable by contract, so its identity is the right key.
    [input],
  );

  const onDatumActivate = useCallback((datumKey: string) => {
    evidenceInvoker.current =
      typeof document === "undefined" ? null : (document.activeElement as HTMLElement | null);
    setOpenEvidenceKey(datumKey);
  }, []);

  const targetOpenEvidence = useCallback(() => {
    if (!openEvidenceKey) return;
    const locator = prepared.model?.datumTargets[openEvidenceKey];
    if (!locator || !onFeedbackTarget) return;
    const rowIndex = datumRowOffset + locator.row_index;
    onFeedbackTarget(
      { kind: "datum", row_index: rowIndex, field: locator.field },
      `Row ${rowIndex + 1} · ${locator.field}`,
    );
  }, [datumRowOffset, onFeedbackTarget, openEvidenceKey, prepared.model]);

  const targetTableCell = useCallback(
    (rowIndex: number, field: string) => {
      const absolute = datumRowOffset + rowIndex;
      onFeedbackTarget?.(
        { kind: "datum", row_index: absolute, field },
        `Row ${absolute + 1} · ${field}`,
      );
    },
    [datumRowOffset, onFeedbackTarget],
  );

  const closeEvidence = useCallback(() => {
    setOpenEvidenceKey(null);
    // Focus returns to whatever opened the drawer (AC13).
    evidenceInvoker.current?.focus?.();
  }, []);

  const evidence: DatumEvidenceResolution | null = useMemo(() => {
    if (!openEvidenceKey || !prepared.model || !input?.spec?.document) return null;
    return resolveDatumEvidence(openEvidenceKey, prepared.model, input.result, input.spec.document);
  }, [openEvidenceKey, prepared.model, input]);

  // --- refusals, in the order they can occur --------------------------------
  const buildMismatches =
    input?.pins && prepared.renderer
      ? checkBuildIdentity(
          input.pins,
          {
            runtime_build: RUNTIME_BUILD,
            renderer_build: prepared.renderer.build,
            theme_version: THEME_VERSION,
            formatter_version: FORMATTER_VERSION,
          },
        )
      : [];

  const state = outcomeState(input);
  const RendererComponent = prepared.renderer?.component ?? null;
  const hasReturnedRows = (input?.result?.rows?.length ?? 0) > 0;
  const mayRenderVisual =
    prepared.model !== null &&
    RendererComponent !== null &&
    (state === null || (state !== null && isBanner(state) && hasReturnedRows));
  const showBlockingStateTable = state !== null && !isBanner(state) && hasReturnedRows;
  const rendererRefusalDetail = prepared.refusal
    ? hasReturnedRows
      ? prepared.refusal.message
      : prepared.refusal.message.replace(
          "The accessible table below shows every returned row.",
          "The Result returned no rows.",
        )
    : null;
  const targetableFields = prepared.model
    ? [...new Set(Object.values(prepared.model.datumTargets).map((target) => target.field))]
    : [];

  return (
    <div ref={host} data-viz-runtime={RUNTIME_BUILD} className="flex w-full flex-col gap-2">
      {prepared.profileSubstitution ? (
        <VizStatePanel kind="partial" detail={prepared.profileSubstitution} />
      ) : null}

      {prepared.refusals.length > 0 ? (
        <>
          <VizStatePanel
            kind="refused"
            detail={prepared.refusals.map((r) => `${r.field}: ${r.message}`).join(" ")}
          />
          {hasReturnedRows ? (
            <TableFallback
              columns={columnsFromResult(input.result)}
              rows={input.result.rows}
              caption="Every row this result returned"
              numberStyle="auto"
              dateStyle="auto"
              unit={null}
              onFeedbackTarget={onFeedbackTarget ? targetTableCell : undefined}
              targetableFields={targetableFields}
            />
          ) : null}
        </>
      ) : prepared.refusal ? (
        /* AC7 -- a refusal that PROMISES a table draws one.
           `registry.ts` ends both the unbuilt-family and the unsupported-profile
           message with "The accessible table below shows every returned row".
           This branch is reached before any renderer resolved, so there is no
           compiled model: the table is built from the Result alone
           (`columnsFromResult`), which is exactly what makes the sentence true.
           A panel stating a table is below, with nothing below it, is worse than
           a bare refusal -- it tells the reader the data is reachable when it is
           not. */
        <>
          <VizStatePanel kind="refused" detail={rendererRefusalDetail} />
          {hasReturnedRows ? (
            <TableFallback
              columns={columnsFromResult(input.result)}
              rows={input.result.rows}
              caption="Every row this result returned"
              numberStyle="auto"
              dateStyle="auto"
              unit={null}
              onFeedbackTarget={onFeedbackTarget ? targetTableCell : undefined}
              targetableFields={targetableFields}
            />
          ) : null}
        </>
      ) : buildMismatches.length > 0 ? (
        <>
          <VizStatePanel
            kind="refused"
            detail={buildMismatches.map((m) => m.message).join(" ")}
          />
          {hasReturnedRows ? (
            <TableFallback
              columns={columnsFromResult(input.result)}
              rows={input.result.rows}
              caption="Every row this result returned"
              numberStyle="auto"
              dateStyle="auto"
              unit={null}
              onFeedbackTarget={onFeedbackTarget ? targetTableCell : undefined}
              targetableFields={targetableFields}
            />
          ) : null}
        </>
      ) : (
        <>
          {state !== null ? (
            <VizStatePanel
              kind={state}
              detail={
                typeof input.result.manifest?.unavailable_reason === "string"
                  ? input.result.manifest.unavailable_reason
                  : null
              }
              missingLink={
                typeof input.result.manifest?.missing_link === "string"
                  ? input.result.manifest.missing_link
                  : null
              }
            />
          ) : null}
          {mayRenderVisual ? (
            <>
              {prepared.model!.disclosures.truncation ? (
                <VizStatePanel kind="truncated" detail={prepared.model!.disclosures.truncation} />
              ) : null}
              <RendererComponent
                model={prepared.model!}
                input={input}
                palette={prepared.palette}
                profile={prepared.profile}
                containerWidthPx={width}
                display={display}
                onDatumFocus={setHoveredKey}
                onDatumActivate={onDatumActivate}
                onFeedbackTarget={onFeedbackTarget}
                datumRowOffset={datumRowOffset}
                onToggleSeries={onToggleSeries}
              />
              <Disclosures model={prepared.model!} />
              {/* A non-success Result may still carry authorized rows. The state
                  stays visible while the shared renderer keeps those rows and
                  their direct table/evidence path inspectable. */}
              {onRequestNewExecution &&
              (prepared.model!.disclosures.truncation || prepared.model!.disclosures.topN) ? (
                <button
                  type="button"
                  data-viz-request-new-execution="rows_not_returned"
                  className="self-start rounded-md border border-[color:var(--color-divider-base,currentColor)] px-3 py-1.5 text-sm focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[color:var(--focus,currentColor)]"
                  onClick={() =>
                    onRequestNewExecution(
                      buildNewExecutionRequest(
                        input,
                        "rows_not_returned",
                        prepared.model!.disclosures.truncation ??
                          prepared.model!.disclosures.topN ??
                          "",
                      ),
                    )
                  }
                >
                  Run this question again for every row
                </button>
              ) : null}
            </>
          ) : showBlockingStateTable ? (
            <TableFallback
              columns={prepared.model?.tableColumns ?? columnsFromResult(input.result)}
              rows={input.result.rows}
              caption="Every row this result returned"
              numberStyle={prepared.model?.formats.numberStyle ?? "auto"}
              dateStyle={prepared.model?.formats.dateStyle ?? "auto"}
              unit={prepared.model?.formats.unit ?? null}
              onFeedbackTarget={onFeedbackTarget ? targetTableCell : undefined}
              targetableFields={targetableFields}
            />
          ) : null}
        </>
      )}

      {/* The evidence layer. One contextual layer, focus restored on close. */}
      {evidence ? (
        <div
          role="dialog"
          aria-modal="false"
          aria-label="Evidence for this mark"
          data-viz-evidence-drawer="true"
          className="rounded-md border border-[color:var(--color-divider-base,currentColor)] p-3 text-sm"
        >
          {evidence.bound ? (
            <>
              <p className="m-0 mb-2 font-semibold">Evidence</p>
              <EvidencePath evidence={evidence} />
            </>
          ) : (
            <p className="m-0">{evidence.message}</p>
          )}
          {openEvidenceKey && prepared.model?.datumTargets[openEvidenceKey] && onFeedbackTarget ? (
            <button
              type="button"
              data-feedback-current-datum
              onClick={targetOpenEvidence}
              className="mt-2 mr-3 underline"
            >
              Target this value for feedback
            </button>
          ) : null}
          <button type="button" onClick={closeEvidence} className="mt-2 underline">
            Close evidence
          </button>
        </div>
      ) : null}

      {/* Hovered datum, announced without stealing focus. The label comes from
          the compiler (`datumLabels`); the raw datum key is an identity, not a
          sentence, and reading a content hash aloud helps nobody. */}
      <span className="sr-only" role="status">
        {hoveredKey
          ? `Mark focused: ${prepared.model?.datumLabels[hoveredKey] ?? hoveredKey}`
          : ""}
      </span>
    </div>
  );
}

function Disclosures({ model }: { model: CompiledVisualModel }) {
  const d = model.disclosures;
  const items: [string, string][] = [];
  if (d.grain) items.push(["Grain", d.grain]);
  if (d.timeWindow) items.push(["Window", d.timeWindow]);
  if (d.comparison) items.push(["Comparison", d.comparison]);
  if (d.freshness) items.push(["Freshness", d.freshness]);
  if (d.filters.length > 0) items.push(["Filters", d.filters.join("; ")]);
  if (d.topN) items.push(["Display limit", d.topN]);
  if (items.length === 0) return null;
  return (
    <dl className="m-0 flex flex-wrap gap-x-4 gap-y-1 text-xs opacity-80">
      {items.map(([label, value]) => (
        <div key={label} className="flex gap-1">
          <dt className="font-semibold">{label}:</dt>
          <dd className="m-0">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

/**
 * AC10 -- the ONE way an interaction that would change the answer leaves the
 * runtime. It computes nothing: it builds the intent and hands it to the host,
 * which routes it to a Story 50.1 execution that produces a NEW Result.
 */
export function buildNewExecutionRequest(
  input: RenderInput,
  reason: RequestNewExecution["reason"],
  detail: string,
): RequestNewExecution {
  return {
    type: "requestNewExecution",
    reason,
    detail,
    from_result_id: input.result.result_id,
    from_visualization_spec_version_id: input.spec.visualization_spec_version_id,
  };
}
