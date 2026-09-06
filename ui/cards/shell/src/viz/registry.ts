/**
 * Story 50.5 AC6 -- the renderer registry: a CLOSED, DECLARATIVE CODE contract.
 *
 * Downstream decision 3: this is a build-time module, NOT a database table and
 * NOT connector metadata. A registry row a connector could write would be
 * executable presentation metadata resolved at runtime, which
 * `ARCHITECTURE-SPINE.md:51` (AD-2) forbids in the same sentence that forbids a
 * connector-owned standard renderer. A specialized renderer enters through a
 * versioned trusted-renderer contract as a PINNED BUILD DEPENDENCY -- that is,
 * as an entry in this file, reviewed and shipped -- never as runtime data.
 *
 * WHAT A RENDERER DECLARES, all of it as data a test can read:
 *   renderer_id + build          -- AC14, and the build is generated, not typed
 *   schemaVersions               -- an INTEGER range over Story 50.4's integer
 *                                   `schema_version`. Never "latest", and never
 *                                   a range comparison against the
 *                                   `spec_contract_version` STRING literal,
 *                                   which is a type error dressed as a check.
 *   families                     -- which visual families it serves
 *   requiredWells                -- required BINDING ROLES, taken from the well
 *                                   vocabulary the pinned Query Spec's members
 *                                   filled. Never inferred from the Result
 *                                   schema, which carries `name` alone.
 *   volume                       -- max_rows / max_series / max_cells (AC7)
 *   profiles                     -- which values of Story 50.4's profile enum
 *   evidenceHit                  -- whether a pointer/keyboard hit maps back to a
 *                                   datum key, and at which granularity
 *
 * FAMILY -> RENDERER IS DATA, NOT A `switch` IN A SCREEN. `resolveRenderer()`
 * below is the only lookup, and a family with no renderer is REFUSED BY NAME with
 * an English message -- never silently substituted by a nearer family, and never
 * drawn as an empty canvas.
 */

import type { ComponentType } from "react";

import type {
  DisplayState,
  RenderInput,
  ResponsiveProfile,
  VisualFamilyId,
} from "./contracts";
import { RESPONSIVE_PROFILES } from "./responsive";
import { rendererBuild } from "./buildInfo";
import { DEFAULT_VOLUME_CONSTRAINTS, type VolumeConstraints } from "./compile/limits";
import type { CompiledVisualModel } from "./compile/dataset";
import type { FeedbackTarget } from "./targetedFeedback";
import type { VizPalette } from "../vizTheme";
import AiPathCapability, {
  AI_PATH_CAPABILITY_SCHEMA,
  type AiPathCapabilityProps,
} from "./aiPathCapability";

/** What every renderer receives, and the ONLY thing it receives. */
export interface RendererProps {
  model: CompiledVisualModel;
  input: RenderInput;
  palette: VizPalette;
  profile: ResponsiveProfile;
  containerWidthPx: number;
  /**
   * The LIVE local display state (AC10). It is a separate prop from
   * `input.display`, which is only the state the caller supplied at mount: a
   * renderer that read `input.display` could never see the effect of a control
   * the reader just operated, which is how AC10's mechanism ends up wired to
   * nothing.
   */
  display: DisplayState;
  onDatumFocus: (datumKey: string | null) => void;
  onDatumActivate: (datumKey: string) => void;
  onFeedbackTarget?: (target: FeedbackTarget, label: string) => void;
  datumRowOffset: number;
  /**
   * Hide or show one series LOCALLY. It changes what is drawn from the rows the
   * Result already returned; it never removes a row, a total or a disclosure.
   */
  onToggleSeries: (seriesId: string) => void;
}

export type EvidenceHitGranularity = "datum" | "series" | "category" | "none";

export interface RendererDeclaration {
  renderer_id: string;
  /** `<family>/<renderer_id>@<semver>` (AC14). */
  build: string;
  /** Inclusive integer range over Story 50.4's `schema_version`. */
  schemaVersions: { min: number; max: number };
  families: VisualFamilyId[];
  /** Wells that must carry at least one member for this renderer to draw. */
  requiredWells: string[];
  volume: VolumeConstraints;
  profiles: ResponsiveProfile[];
  evidenceHit: { pointer: boolean; keyboard: boolean; granularity: EvidenceHitGranularity };
  component: ComponentType<RendererProps>;
}

/** A family this build KNOWS and does not draw. Registered, never omitted. */
export interface KnownUnbuiltFamily {
  family: string;
  message: string;
}

/** A shared renderer whose input is a bounded capability, never a RenderInput. */
export interface CapabilityRendererDeclaration {
  family: "ai_path";
  schemaVersions: readonly [typeof AI_PATH_CAPABILITY_SCHEMA];
  visualizationSpecSelectable: false;
  component: ComponentType<AiPathCapabilityProps>;
}

const capabilityRegistry: readonly CapabilityRendererDeclaration[] = [
  {
    family: "ai_path",
    schemaVersions: [AI_PATH_CAPABILITY_SCHEMA],
    visualizationSpecSelectable: false,
    component: AiPathCapability,
  },
];

export function registeredCapabilities(): readonly CapabilityRendererDeclaration[] {
  return capabilityRegistry;
}

export function resolveCapability(
  family: string,
  schemaVersion: string,
): CapabilityRendererDeclaration | null {
  return (
    capabilityRegistry.find(
      (entry) => entry.family === family && entry.schemaVersions.includes(schemaVersion as never),
    ) ?? null
  );
}

const RENDERER_SEMVER = "1.0.0";

export function declare(
  family: VisualFamilyId,
  rendererId: string,
  component: ComponentType<RendererProps>,
  overrides: Partial<Omit<RendererDeclaration, "renderer_id" | "build" | "component">> = {},
): RendererDeclaration {
  return {
    renderer_id: rendererId,
    build: rendererBuild(family, rendererId, RENDERER_SEMVER),
    schemaVersions: { min: 1, max: 1 },
    families: [family],
    requiredWells: ["measure"],
    volume: DEFAULT_VOLUME_CONSTRAINTS,
    profiles: [...RESPONSIVE_PROFILES],
    evidenceHit: { pointer: true, keyboard: true, granularity: "datum" },
    component,
    ...overrides,
  };
}

/**
 * The six ordinary families `visualization-and-rendering.md:162-164` names that
 * Story 50.4's grammar does not yet declare, plus the six specialized ones. They
 * are listed so a reader can tell "not yet" from "not wanted": an absence leaves
 * almost no trace, and destroying the little it leaves erases the inventory of
 * the work that remains (CLAUDE.md anti-drift rule 3).
 *
 * A family in this list that reaches the runtime is refused BY NAME.
 */
export const KNOWN_UNBUILT_FAMILIES: KnownUnbuiltFamily[] = [
  "distribution",
  "heatmap",
  "calendar_heatmap",
  "gauge",
  "small_multiples",
  "timeline",
  "sankey",
  "geographic_map",
  "knowledge_graph",
  "mindmap",
].map((family) => ({
  family,
  message:
    `The "${family}" renderer is not built in this release. It is a known family, ` +
    `not an unknown one: no nearer family is substituted for it, because a chart ` +
    `drawn by the wrong family would make a claim this Result does not support. ` +
    `The accessible table below shows every returned row.`,
}));

const UNBUILT_BY_NAME = new Map(KNOWN_UNBUILT_FAMILIES.map((f) => [f.family, f]));

export type RendererResolution =
  | { kind: "renderer"; renderer: RendererDeclaration }
  | { kind: "refused"; code: string; message: string };

const registry: RendererDeclaration[] = [];

export function register(declaration: RendererDeclaration): void {
  const clash = registry.find(
    (r) =>
      r.renderer_id === declaration.renderer_id ||
      r.families.some((f) => declaration.families.includes(f)),
  );
  if (clash) {
    throw new Error(
      `Renderer "${declaration.renderer_id}" collides with "${clash.renderer_id}": one family ` +
        `has exactly one renderer in a build, or two renderers could draw the same Render ` +
        `differently.`,
    );
  }
  registry.push(declaration);
}

export function registered(): readonly RendererDeclaration[] {
  return registry;
}

/** Test-only: drop every registration so a suite can assert on a fresh registry. */
export function __resetRegistryForTests(): void {
  registry.length = 0;
}

/**
 * The ONE lookup. `(family, schema_version, profile)` -> a renderer or a NAMED
 * refusal. There is no fallback family and no "closest match".
 */
export function resolveRenderer(
  family: string,
  schemaVersion: number,
  profile: ResponsiveProfile,
): RendererResolution {
  if (capabilityRegistry.some((entry) => entry.family === family)) {
    return {
      kind: "refused",
      code: "capability_not_visualization_spec",
      message:
        `The "${family}" family consumes its own bounded capability and cannot be selected ` +
        `by a Visualization Spec or compiled from Result rows.`,
    };
  }
  const unbuilt = UNBUILT_BY_NAME.get(family);
  if (unbuilt) {
    return { kind: "refused", code: "renderer_not_built", message: unbuilt.message };
  }
  const byFamily = registry.filter((r) => r.families.includes(family as VisualFamilyId));
  if (byFamily.length === 0) {
    return {
      kind: "refused",
      code: "unknown_family",
      message:
        `"${family}" is not a visual family this runtime knows. Known families are ` +
        `${registry.flatMap((r) => r.families).join(", ")}.`,
    };
  }
  const byVersion = byFamily.filter(
    (r) => schemaVersion >= r.schemaVersions.min && schemaVersion <= r.schemaVersions.max,
  );
  if (byVersion.length === 0) {
    const ranges = byFamily
      .map((r) => `${r.renderer_id} supports ${r.schemaVersions.min}..${r.schemaVersions.max}`)
      .join("; ");
    return {
      kind: "refused",
      code: "schema_version_unsupported",
      message:
        `This Visualization Spec declares schema_version ${schemaVersion}, which no ` +
        `renderer for "${family}" supports (${ranges}).`,
    };
  }
  const byProfile = byVersion.filter((r) => r.profiles.includes(profile));
  if (byProfile.length === 0) {
    return {
      kind: "refused",
      code: "profile_unsupported",
      message:
        `No "${family}" renderer supports the "${profile}" responsive profile. ` +
        `The accessible table below shows every returned row.`,
    };
  }
  return { kind: "renderer", renderer: byProfile[0]! };
}
