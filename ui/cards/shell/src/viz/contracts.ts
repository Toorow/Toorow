/**
 * Story 50.5 -- the ONE validated input the shared Visualization runtime accepts.
 *
 * WHY THIS FILE IS TYPES AND NOTHING ELSE. The runtime has a single public entry
 * (`renderVisualization`, `Runtime.tsx`). Everything it is allowed to know arrives
 * through `RenderInput` below. There is no second overload, no options bag and no
 * escape hatch: a renderer that could accept a library configuration object would
 * be a second presentation authority, which `ARCHITECTURE-SPINE.md:51` (AD-2)
 * forbids in the same sentence that forbids a connector-owned standard renderer.
 *
 * WHERE EACH SHAPE COMES FROM -- none of it is invented here:
 *   - `VizResult` mirrors `ResultEvidence` in `ui/admin/src/analyze/queryClient.ts`,
 *     which mirrors the server payload built at `server/core/query_execution.py:600`.
 *     `schema.fields` carries `name` AND NOTHING ELSE. A `type` or a `role` on a
 *     field would be a contract the server never sends, so a validator asserting
 *     one would assert a fiction.
 *   - `VizSpec` mirrors Story 50.4's persisted grammar
 *     (`server/core/visualization_specs.py:343` GRAMMAR). Two version keys with
 *     two distinct jobs: `spec_contract_version` is the string literal
 *     "visualization-spec.v1" that anchors the database CHECK, `schema_version`
 *     is an INTEGER and is the only key a range comparison may touch.
 *   - `ResponsiveProfile` is Story 50.4's enum
 *     (`visualization_specs.py` RESPONSIVE_PROFILES), re-declared nowhere:
 *     see `responsive.ts`, which is the single TypeScript mirror and is asserted
 *     against the server list by `__tests__/responsive.test.ts` -- which reads
 *     the tuple out of the Python source rather than restating it.
 *   - `RenderPins` mirrors the four build pins of
 *     `core.analyze_artifacts.RENDER_REPLAY_PINS` that this runtime owns.
 *
 * NO ARBITRARY BAG MAY APPEAR AS A SPEC DOCUMENT FIELD (AC3) -- the string-keyed
 * `unknown` map is absent from this file, and `__tests__/guards.test.ts` asserts
 * its literal absence. The
 * manifest and the row payload are opaque server data and are typed as bounded
 * maps of primitives, never as arbitrary bags that could carry a function, a URL
 * or a string of HTML.
 */

import type { ResponsiveProfile } from "./responsive";

export type { ResponsiveProfile };

/** The five outcomes the execution service can terminalize with. */
export type ResultOutcome = "success" | "empty" | "degraded" | "refused" | "unavailable";

/** Every value a Result cell may carry. A function, an element or a nested bag cannot. */
export type CellValue = string | number | boolean | null | string[];

export interface VizResultSchema {
  contract?: string;
  /** Specialized Result shapes carry a stable id beside the row key. */
  fields?: { id?: string; name: string }[];
}

export interface VizResultManifest {
  grain?: string | null;
  time_window?: { start?: string | null; end?: string | null } | null;
  filters?: string[] | null;
  comparison?: string | null;
  limits?: { row_limit?: number | null } | null;
  truncation?: string | null;
  freshness?:
    | string
    | { state?: string | null; output_created_at?: string | null }
    | null;
  unavailable_reason?: string | null;
  missing_link?: string | null;
  /** Anything else the server discloses. Read for display, never for meaning. */
  [key: string]: CellValue | string[] | Record<string, CellValue> | null | undefined;
}

/** One evidence reference, as the Result's evidence manifest carries it. */
export interface EvidenceRef {
  evidence_id: string;
  label?: string | null;
  source?: string | null;
}

export interface VizResult {
  result_id: string;
  content_hash: string;
  outcome: ResultOutcome;
  schema: VizResultSchema;
  rows: Record<string, CellValue>[];
  manifest: VizResultManifest;
  /** datum key -> evidence reference. Absent means "this Result declared none". */
  evidence?: Record<string, EvidenceRef>;
  /** Server-declared truncation of the returned slice. */
  truncated?: boolean;
  row_count?: number;
}

// ---------------------------------------------------------------------------
// The Visualization Spec, exactly as Story 50.4 persists it.
// ---------------------------------------------------------------------------

export type VisualFamilyId =
  | "table"
  | "kpi"
  | "line"
  | "area"
  | "waterfall"
  | "bar"
  | "stacked_bar"
  | "scatter";

/** The ten wells, named by semantic role (`visualization_families.py:107`). */
export type WellName =
  | "measure"
  | "dimension"
  | "time"
  | "series"
  | "breakdown"
  | "facet"
  | "color"
  | "size"
  | "label"
  | "detail";

export type AxisScale = "linear" | "log" | "categorical" | "ordinal";
export type TickDensity = "sparse" | "normal" | "dense";
export type LegendPosition = "top" | "right" | "bottom" | "left" | "none";
export type NumberStyle = "auto" | "integer" | "decimal" | "percent" | "currency" | "compact";
export type DateStyle = "auto" | "iso" | "short" | "long" | "month" | "year";
export type ColorRole = "none" | "single" | "categorical" | "sequential" | "diverging";
export type SemanticDirection = "none" | "higher_is_better" | "lower_is_better";
export type MarkBinding = "none" | "datum" | "series" | "category";

export interface VizAxis {
  scale: AxisScale;
  zero_baseline: boolean;
  tick_density: TickDensity;
}

export interface VizSpecDocument {
  spec_contract_version: "visualization-spec.v1";
  /** INTEGER. The only key a version range may compare against. */
  schema_version: number;
  family: VisualFamilyId;
  bindings: Partial<Record<WellName, string[]>>;
  order: { source: "result" };
  top_n: { n: number | null; display_only: true } | null;
  axes: { x: VizAxis; y: VizAxis };
  legend: { position: LegendPosition; visible: boolean };
  formatting: {
    number_style: NumberStyle;
    date_style: DateStyle;
    unit_source: "semantic_view" | "none";
  };
  color: { role: ColorRole; semantic_direction: SemanticDirection };
  thresholds: {
    member_id: string;
    comparator: "gt" | "gte" | "lt" | "lte" | "eq";
    value: number;
    severity: "info" | "success" | "warning" | "critical";
  }[];
  reference_lines: {
    member_id: string;
    kind: "average" | "median" | "target" | "zero";
    value: number | null;
  }[];
  annotations: { evidence_id: string; anchor: "datum" | "mark" | "axis" | "plot" }[];
  interactions: {
    hover: boolean;
    select: boolean;
    zoom: boolean;
    legend_toggle: boolean;
    local_filter: boolean;
  };
  evidence: { datum_fields: string[]; mark_binding: MarkBinding };
  responsive: { profiles: ResponsiveProfile[] };
  accessibility: {
    summary_source: "result_manifest" | "family_default";
    table_fallback: "required";
  };
  labels: { override: Record<string, string> };
}

export interface VizSpec {
  visualization_spec_version_id: string;
  spec_contract_version: "visualization-spec.v1";
  schema_version: number;
  document: VizSpecDocument;
}

// ---------------------------------------------------------------------------
// Pins, display state and the envelope itself.
// ---------------------------------------------------------------------------

export interface RenderPins {
  theme_version: string;
  formatter_version: string;
  /** `<family>/<renderer_id>@<semver>` -- see `buildInfo.ts`. */
  renderer_build: string;
  /** `@toorow/card-shell/viz@<semver>+<git-short-sha>` -- see `buildInfo.ts`. */
  runtime_build: string;
}

/**
 * Bounded LOCAL display state (AC10). Every key here changes what is shown of the
 * rows the Result already returned. None of them recomputes a value, changes a
 * grain, adds a comparison or asks for a row the Result did not carry -- those
 * emit `requestNewExecution` instead.
 */
export interface DisplayState {
  /** Datum keys currently selected. */
  selection?: string[];
  /** Datum key currently hovered. */
  hover?: string | null;
  /** Series ids hidden via the legend. */
  legendHidden?: string[];
  /** Local zoom window over already-returned rows, as row indices. */
  zoom?: { start: number; end: number } | null;
  /** Datum keys hidden locally. Never a filter: the totals stay visible. */
  localHiddenRows?: string[];
}

/** The closed set of display keys. `validate.ts` refuses any other key. */
export const DISPLAY_KEYS = [
  "selection",
  "hover",
  "legendHidden",
  "zoom",
  "localHiddenRows",
] as const;

export type DisplayKey = (typeof DISPLAY_KEYS)[number];

/** THE runtime input. Five fields, no sixth, no overload. */
export interface RenderInput {
  result: VizResult;
  spec: VizSpec;
  pins: RenderPins;
  profile: ResponsiveProfile;
  display?: DisplayState;
}

/**
 * The intent a local interaction emits when it would need a NEW answer.
 * The host routes it to a Story 50.1 execution; the runtime computes nothing.
 */
export interface RequestNewExecution {
  type: "requestNewExecution";
  reason:
    | "grain_change"
    | "regrouping"
    | "new_comparison"
    | "semantic_filter"
    | "rows_not_returned";
  detail: string;
  /** The Result the request was made from, so the host can pin the lineage. */
  from_result_id: string;
  from_visualization_spec_version_id: string;
}

/** One structured refusal. `field` names the offending input, always. */
export interface VizRefusal {
  code: string;
  /** English, actionable, and it names the field. */
  message: string;
  field: string;
}

export class VizInputRefused extends Error {
  readonly refusals: VizRefusal[];
  constructor(refusals: VizRefusal[]) {
    super(
      refusals.length === 1
        ? refusals[0]!.message
        : `The visualization input was refused on ${refusals.length} fields: ` +
            refusals.map((r) => r.field).join(", "),
    );
    this.name = "VizInputRefused";
    this.refusals = refusals;
  }
}
