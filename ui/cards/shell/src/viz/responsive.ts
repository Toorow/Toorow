/**
 * Story 50.5 AC9 -- the responsive profile is Story 50.4's enum, mirrored ONCE.
 *
 * The vocabulary is persisted grammar: its single definition site is
 * `server/core/visualization_specs.py` (`RESPONSIVE_PROFILES`), which the
 * database CHECK in migration 156 mirrors. This module is the TypeScript mirror
 * and nothing else -- it does not add a value, and `__tests__/responsive.test.ts`
 * READS that tuple out of the Python source and asserts this one against it, so a
 * drift is a red test, not a surprise in a browser.
 *
 * That test was claimed by this docstring before it existed. The only assertion
 * on the vocabulary hardcoded the same four strings a second time
 * (`__tests__/parity.test.tsx`), which would have stayed green through any
 * server-side change. It exists now.
 *
 * THERE IS NO `compact` PROFILE. Compaction is a behaviour a profile may exhibit
 * (`visualization-and-rendering.md:221-224`), not a fifth name.
 *
 * `mcp-pip` WAS A NAMED DIVERGENCE. IT IS BUILT, SINCE 2026-08-24.
 * `visualization-and-rendering.md:346` gives the MCP layout as
 * "Inline/fullscreen/PiP profile when supported" -- "when supported" qualifies the
 * HOST's capability, never ours -- so picture-in-picture is a mode of the ratified
 * target, and shipping four values promised a layout the product did not have
 * (criterion [14] of that surface). The decision of 2026-08-24 was to honour the
 * table as it stands and build the mode rather than withdraw it from the target,
 * so `mcp-pip` is now a value of the enum, with its own branch in `profileLayout`
 * below and its own row in the parity proof.
 *
 * IT IS THE SAME RUNTIME, NOT A SECOND ONE. Picture-in-picture is a LAYOUT: the
 * MCP App entry passes the profile to the one `VisualizationRuntime`, exactly as
 * inline and fullscreen do, and `__tests__/parity.test.tsx` asserts that the
 * serialized visual model of `mcp-pip` is deep-equal to every other profile's --
 * same values, same colours, same formats, same datum keys, same evidence map,
 * same disclosures. Only the four layout fields below may differ.
 */

/** Story 50.4's enum, in its order. */
export const RESPONSIVE_PROFILES = [
  "console",
  "mcp-inline",
  "mcp-fullscreen",
  "mcp-pip",
  "share",
] as const;

export type ResponsiveProfile = (typeof RESPONSIVE_PROFILES)[number];

/**
 * Profiles a host may ASK for that this build does not implement, each mapped to
 * the profile actually used. Declared, never silently ignored.
 *
 * IT IS EMPTY, AND THE MECHANISM STAYS. Every profile the ratified target names
 * -- console, inline, fullscreen, picture-in-picture, share -- is implemented as
 * of 2026-08-24, so there is nothing left to substitute; `mcp-pip` was its only
 * entry and it moved into the enum above. The map is kept because the NEXT profile
 * the target grows will be asked for by some host before it is drawn here, and the
 * honest answer then is a stated substitution to the nearest implemented layout,
 * not a fall to `console` through the unknown-value branch. Deleting the map would
 * delete that answer along with it.
 */
export const KNOWN_UNSUPPORTED_PROFILES: Record<string, ResponsiveProfile> = {};

export interface ProfileResolution {
  profile: ResponsiveProfile;
  /** English, shown to the user, when a substitution happened. */
  substitution: string | null;
}

export function isResponsiveProfile(value: unknown): value is ResponsiveProfile {
  return (
    typeof value === "string" && (RESPONSIVE_PROFILES as readonly string[]).includes(value)
  );
}

/**
 * Resolve the profile the caller supplied. Never inferred from `window.innerWidth`
 * (AC9): the MCP App is a frame inside a host, so the viewport is the host's and
 * says nothing about the space the widget was given.
 */
export function resolveProfile(requested: unknown): ProfileResolution {
  if (isResponsiveProfile(requested)) {
    return { profile: requested, substitution: null };
  }
  if (typeof requested === "string" && requested in KNOWN_UNSUPPORTED_PROFILES) {
    const fallback = KNOWN_UNSUPPORTED_PROFILES[requested]!;
    return {
      profile: fallback,
      substitution:
        `This host asked for the "${requested}" layout, which this build does not ` +
        `implement yet. The "${fallback}" layout is shown instead; every value, ` +
        `colour, format and evidence link is identical.`,
    };
  }
  return {
    profile: "console",
    substitution:
      `"${String(requested)}" is not a responsive profile. The "console" layout is ` +
      `shown instead. Valid profiles are ${RESPONSIVE_PROFILES.join(", ")}.`,
  };
}

/**
 * Downstream decision 5 -- ONE shared container-width threshold, in CSS pixels,
 * for every renderer. Per-renderer breakpoints would let two renderers disagree
 * about when a legend disappears and would leave AC8's parity assertion with no
 * fixed point. Container width, not viewport: the MCP App does not own the
 * viewport.
 */
export const CONTAINER_REFLOW_PX = 640;

export function isNarrow(containerWidthPx: number): boolean {
  return containerWidthPx > 0 && containerWidthPx < CONTAINER_REFLOW_PX;
}

/**
 * Layout-only decisions a profile makes. NOTHING here may change a value, a
 * format, a colour's semantic direction, a disclosed filter or an evidence key --
 * `__tests__/parity.test.tsx` asserts exactly that.
 */
export interface ProfileLayout {
  /** Legend placement may move or fold away; the series never disappear. */
  legendCollapsed: boolean;
  /** Chart height in CSS pixels. */
  height: number;
  /** Whether the disclosure strip renders inline or stacked. */
  disclosureStacked: boolean;
  /** Whether the table fallback starts expanded (share pages print). */
  tableFallbackOpen: boolean;
}

export function profileLayout(
  profile: ResponsiveProfile,
  containerWidthPx: number,
): ProfileLayout {
  const narrow = isNarrow(containerWidthPx);
  switch (profile) {
    case "mcp-inline":
      return {
        legendCollapsed: true,
        height: narrow ? 200 : 240,
        disclosureStacked: true,
        tableFallbackOpen: false,
      };
    // Picture-in-picture: the smallest surface the product draws on. A host keeps
    // it floating beside a conversation that continues, so the chart gets less
    // room than inline and the legend never competes with it for that room.
    //
    // WHAT IT MAY NOT DO, and this is the clause that bounds the branch
    // (`visualization-and-rendering.md`, "Responsive profiles may rearrange
    // controls, labels and legends"): it drops no disclosed filter, no truncation
    // notice and no evidence link. It cannot -- `Runtime.tsx` renders the
    // disclosure strip and the evidence layer outside every renderer, so no
    // profile can reach them. What a compact profile summarizes stays reachable in
    // the SAME surface: the series remain in the renderer's text legend, and the
    // full table stays one keyboard-reachable toggle away, exactly as inline.
    case "mcp-pip":
      return {
        legendCollapsed: true,
        height: narrow ? 140 : 160,
        disclosureStacked: true,
        tableFallbackOpen: false,
      };
    case "mcp-fullscreen":
      return {
        legendCollapsed: narrow,
        height: narrow ? 320 : 480,
        disclosureStacked: narrow,
        tableFallbackOpen: false,
      };
    case "share":
      return {
        legendCollapsed: narrow,
        height: narrow ? 280 : 380,
        disclosureStacked: narrow,
        tableFallbackOpen: true,
      };
    case "console":
    default:
      return {
        legendCollapsed: narrow,
        height: narrow ? 260 : 360,
        disclosureStacked: narrow,
        tableFallbackOpen: false,
      };
  }
}
