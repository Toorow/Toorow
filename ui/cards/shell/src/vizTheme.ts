/**
 * vizTheme — the SINGLE color contract for every viz primitive (Story 23.1, AC5).
 *
 * Rule: primitives NEVER import color constants from tokens/dist/theme — module-level
 * constants bypass the theme carrier, which is the unique injection point for org
 * branding (AD-11; meta.branding, Story 23.1). This module is the ONLY file in
 * card-shell allowed to read the token constants, and only as defaults behind the
 * live theme values.
 *
 * NO MUI (AD-35, story console-visual-viztheme-port). This file used to be typed on
 * MUI's `Theme` and to borrow MUI's color math, which made the one contract the whole
 * render runtime depends on unusable outside a MUI tree — and story 50.5 pins theme,
 * formatter and renderer versions for a runtime shared by Console, MCP App and Share.
 * Two changes remove that coupling without moving a single caller:
 *
 *   - the input is a STRUCTURAL shape (`VizThemeInput`), not a MUI type. A MUI theme
 *     satisfies it as-is, because all this ever read were plain property paths.
 *   - the color math (alpha, mix) is local, and parses rather than throws. MUI's
 *     `alpha` raises on a color it cannot decompose; here an unparseable value
 *     degrades to an honest endpoint, because a chart that loses one tint is a far
 *     better outcome than a card that fails to render.
 *
 * Called with no argument it reads the Tailwind 4 `@theme` custom properties that
 * `scripts/generate_theme_css.py` writes into `styles/theme.css` — same contract,
 * CSS-variable carrier, org-overridable by setting the properties on any ancestor.
 *
 * Everything here derives from the CURRENT theme:
 *   - accent           = primary (= brand_primary when branded)
 *   - categorical[>=6] = org colors first (vizBranding), toorow tints after
 *   - diverging(t)     = success → warning → error interpolation (green→yellow→red);
 *                        light/dark/branding follow with zero extra tokens
 *   - track/hairline   = the Origin rail + hairline derivations (BarChart/
 *                        CalendarHeatmap patterns, unchanged visuals by default)
 */

import {
  ACCENT,
  ERROR,
  ORIGIN_CATEGORICAL_PALETTE,
  SUCCESS,
  TEXT_DARK,
  TRACK_LIGHT,
  WARNING,
} from "../../../tokens/dist/theme";

/**
 * The shape `getVizPalette` reads. A MUI `Theme` satisfies it structurally, so the
 * primitives that still live in a MUI tree pass `useTheme()` unchanged; a runtime
 * built on CSS variables satisfies it too (see `readCssVizTheme`).
 */
export interface VizThemeInput {
  palette: {
    mode?: "light" | "dark";
    primary: { main: string };
    success: { main: string };
    warning: { main: string };
    error: { main: string };
    text: { primary: string };
  };
  /** Extra org brand colors carried by the branded theme (Story 23.1, AC3/AC5). */
  vizBranding?: {
    secondary?: string | null;
    accent?: string | null;
  };
  /** Optional rail override; defaults to the Origin track token. */
  vizTrack?: string | null;
}

export interface VizPalette {
  /** The hero series color — follows org branding via primary. */
  accent: string;
  /** Categorical series colors, ALWAYS >= 6 entries; org colors first when branded. */
  categorical: string[];
  /** Background rail behind bars/gauges (theme-correct in dark). */
  track: string;
  /** Hairline stroke for grids/dividers (alpha on text.primary). */
  hairline: string;
  /**
   * Continuous diverging ramp, t in [0,1]: 0 = good (success), 0.5 = warning,
   * 1 = bad (error). Semantic palette of the CURRENT theme — light/dark/branding
   * apply automatically. Out-of-range t is clamped.
   */
  diverging: (t: number) => string;
  /** n evenly-spaced stops of the diverging ramp (n >= 2) — threshold legends. */
  divergingStops: (n: number) => string[];
  /**
   * Continuous SEQUENTIAL ramp, t in [0,1]: 0 = the theme track (low), 1 = the
   * accent (high). Story 50.5 extension, added HERE rather than in a second
   * module: the Visualization Spec grammar declares `color.role: "sequential"`
   * (`server/core/visualization_specs.py` GRAMMAR), and a runtime that read a
   * second variable set for it would be the second colour vocabulary this file
   * exists to forbid. Out-of-range t is clamped.
   */
  sequential: (t: number) => string;
  /** n evenly-spaced stops of the sequential ramp (n >= 2) — legend stops. */
  sequentialStops: (n: number) => string[];
  /**
   * Readable foreground on `accent` — needed by the KPI family and by any badge
   * painted with the org brand colour. Derived from the live accent, never a
   * constant, so a branded org keeps a legible figure. (Story 50.5.)
   */
  onAccent: string;
}

type Rgba = [number, number, number, number];

/** Parse #rgb / #rrggbb / #rrggbbaa / rgb() / rgba(). Returns null when unreadable. */
function parseColor(color: string): Rgba | null {
  const value = (color ?? "").trim();
  if (!value) return null;

  if (value.startsWith("#")) {
    const hex = value.slice(1);
    const expand = (part: string): number => parseInt(part.repeat(2 / part.length), 16);
    if (hex.length === 3 || hex.length === 4) {
      const channels = hex.split("").map(expand);
      if (channels.some((c) => Number.isNaN(c))) return null;
      return [channels[0]!, channels[1]!, channels[2]!, (channels[3] ?? 255) / 255];
    }
    if (hex.length === 6 || hex.length === 8) {
      const pairs = hex.match(/../g) ?? [];
      const channels = pairs.map((p) => parseInt(p, 16));
      if (channels.some((c) => Number.isNaN(c))) return null;
      return [channels[0]!, channels[1]!, channels[2]!, (channels[3] ?? 255) / 255];
    }
    return null;
  }

  const fn = value.match(/^rgba?\(([^)]+)\)$/i);
  if (fn) {
    // Both the legacy `r, g, b, a` and the modern `r g b / a` separators.
    const parts = fn[1]!
      .replace("/", " ")
      .split(/[\s,]+/)
      .filter(Boolean)
      .map((p) => (p.endsWith("%") ? (parseFloat(p) * 255) / 100 : parseFloat(p)));
    if (parts.length < 3 || parts.some((p) => Number.isNaN(p))) return null;
    return [parts[0]!, parts[1]!, parts[2]!, parts[3] ?? 1];
  }

  return null;
}

function format([r, g, b, a]: Rgba): string {
  const channel = (v: number): number => Math.round(Math.max(0, Math.min(255, v)));
  return `rgba(${channel(r)}, ${channel(g)}, ${channel(b)}, ${a})`;
}

/**
 * Replace a color's alpha channel (MUI `alpha` semantics: replace, never multiply).
 * An unparseable color is returned untouched instead of raising.
 */
function withAlpha(color: string, opacity: number): string {
  const parsed = parseColor(color);
  if (!parsed) return color;
  return format([parsed[0], parsed[1], parsed[2], opacity]);
}

/** Linear RGBA interpolation between two CSS colors. */
function mix(from: string, to: string, t: number): string {
  const a = parseColor(from);
  const b = parseColor(to);
  if (!a || !b) {
    // Unparseable custom color (CSS var, named color…): honest endpoint fallback.
    return t < 0.5 ? from : to;
  }
  return format([
    a[0] + (b[0] - a[0]) * t,
    a[1] + (b[1] - a[1]) * t,
    a[2] + (b[2] - a[2]) * t,
    a[3] + (b[3] - a[3]) * t,
  ]);
}

/**
 * Relative luminance of a CSS color, per WCAG 2.x. Local, like the rest of the
 * color math in this file, so nothing here depends on a UI library.
 */
function relativeLuminance(color: string): number | null {
  const parsed = parseColor(color);
  if (!parsed) return null;
  const channel = (v: number): number => {
    const n = v / 255;
    return n <= 0.03928 ? n / 12.92 : Math.pow((n + 0.055) / 1.055, 2.4);
  };
  return 0.2126 * channel(parsed[0]) + 0.7152 * channel(parsed[1]) + 0.0722 * channel(parsed[2]);
}

/**
 * The readable foreground on a background: the darker or lighter of the two
 * token endpoints, whichever contrasts more. Unparseable input degrades to the
 * dark text endpoint rather than throwing — a chart with one dull label beats a
 * card that fails to render, the same trade this file already makes for `mix`.
 */
function readableOn(background: string): string {
  const luminance = relativeLuminance(background);
  if (luminance === null) return TEXT_DARK;
  const onLight = (luminance + 0.05) / 0.05;
  const onDark = 1.05 / (luminance + 0.05);
  return onLight >= onDark ? TEXT_DARK : "#FAFAFA";
}

/**
 * Build the theme input from the `@theme` custom properties in styles/theme.css.
 *
 * The carrier is already built and generated from ui/tokens/tokens.json — nothing
 * is decided here. Org branding overrides by setting `--color-primary` (and the two
 * `--viz-brand-*` properties) on any ancestor of *scope*, which is the CSS-variable
 * equivalent of the branded theme the MUI carrier hands down today.
 *
 * Every property falls back to its token default, so this is safe under jsdom and
 * before the stylesheet has loaded.
 */
export function readCssVizTheme(scope?: Element | null): VizThemeInput {
  const element =
    scope ?? (typeof document === "undefined" ? null : document.documentElement);

  const read = (name: string, fallback: string): string => {
    if (!element || typeof getComputedStyle !== "function") return fallback;
    const value = getComputedStyle(element).getPropertyValue(name).trim();
    return value || fallback;
  };

  const isDark = element ? !!element.closest(".dark") : false;

  return {
    palette: {
      mode: isDark ? "dark" : "light",
      primary: { main: read("--color-primary", ACCENT) },
      success: { main: read("--color-success", SUCCESS) },
      warning: { main: read("--color-warning", WARNING) },
      error: { main: read("--color-error", ERROR) },
      text: {
        primary: isDark
          ? read("--color-text-on-dark", "#FAFAFA")
          : read("--color-text", TEXT_DARK),
      },
    },
    vizBranding: {
      secondary: read("--viz-brand-secondary", "") || null,
      accent: read("--viz-brand-accent", "") || null,
    },
    vizTrack: read("--color-divider-base", TRACK_LIGHT),
  };
}

export function getVizPalette(theme?: VizThemeInput | null): VizPalette {
  const source = theme ?? readCssVizTheme();
  const isDark = source.palette.mode === "dark";
  const accent = source.palette.primary.main;

  // Org colors first when the branded theme carries them (Story 23.1 AC3),
  // then the toorow defaults, deduplicated, ALWAYS >= 6 entries.
  const head = [
    accent,
    source.vizBranding?.secondary ?? null,
    source.vizBranding?.accent ?? null,
  ].filter((c): c is string => !!c);
  const categorical = [
    ...head,
    ...ORIGIN_CATEGORICAL_PALETTE.filter((c) => !head.includes(c)),
  ];

  const success = source.palette.success.main;
  const warning = source.palette.warning.main;
  const error = source.palette.error.main;

  const diverging = (t: number): string => {
    const c = Math.max(0, Math.min(1, t));
    return c <= 0.5 ? mix(success, warning, c * 2) : mix(warning, error, (c - 0.5) * 2);
  };

  const track = source.vizTrack || TRACK_LIGHT;
  const resolvedTrack = isDark ? withAlpha(track, 0.1) : track;

  // Story 50.5: low end = the theme's own rail, high end = the live accent, so a
  // branded org's sequential ramp ends on its brand colour with no extra token.
  const sequential = (t: number): string => {
    const c = Math.max(0, Math.min(1, t));
    return mix(resolvedTrack, accent, c);
  };

  return {
    accent,
    categorical,
    track: resolvedTrack,
    hairline: withAlpha(source.palette.text.primary, isDark ? 0.1 : 0.08),
    diverging,
    divergingStops: (n: number): string[] => {
      const count = Math.max(2, Math.floor(n));
      return Array.from({ length: count }, (_, i) => diverging(i / (count - 1)));
    },
    sequential,
    sequentialStops: (n: number): string[] => {
      const count = Math.max(2, Math.floor(n));
      return Array.from({ length: count }, (_, i) => sequential(i / (count - 1)));
    },
    onAccent: readableOn(accent),
  };
}
