/**
 * toorow widget theme — the SINGLE theme for the entire widget platform.
 *
 * This file was `createTheme()` over MUI until 2026-08-05. MUI is gone from the
 * repository; what replaces it is this file plus `color.ts`, because the whole
 * of what MUI was doing here is now written down: resolve the token file into a
 * flat palette per colour scheme, derive `light`/`dark`/`contrastText` from a
 * `main`, and hand the result to consumers through a React context.
 *
 * THE SHAPE IS DELIBERATELY THE ONE MUI PRODUCED. `theme.palette.text.primary`,
 * `theme.palette.success.main`, `theme.palette.action.hover` are read at 120
 * sites across `ui/cards/*` and `ui/widgets/*`. Reproducing the shape — down to
 * `text.disabled` being `rgba(0, 0, 0, 0.38)`, a default MUI filled in and the
 * token file never stated — is what let those sites keep rendering the same
 * pixels while the dependency left. The derivations are pinned by
 * `__tests__/PaletteResolution.test.ts`.
 *
 * The theme options are still derived exclusively from the W3C DTCG token file
 * at ui/tokens/tokens.json:
 *
 *   tokens.json -> style-dictionary.config.js -> dist/theme.ts -> here
 *
 * Story 23.1 — org branding injection: when the envelope carries `meta.branding`
 * (org colors from app.organizations, Story 21.2), `resolveWidgetTheme` derives a
 * branded theme at this SAME single injection point: brand_primary replaces
 * palette.primary.main in BOTH color schemes (contrastText and hover DERIVED, a
 * contrast guard keeps the accent readable), brand_secondary/brand_accent ride
 * along as `vizBranding` for the categorical viz palette (card-shell vizTheme).
 * Without branding the exported `connectorTheme` is returned BY REFERENCE —
 * rendering stays bit-identical (AC2).
 */

import themeOptions from "../../tokens/dist/theme";
import {
  TEXT_DARK,
  TEXT_ON_DARK,
  SURFACE_LIGHT,
  SURFACE_DARK_ELEVATED,
} from "../../tokens/dist/theme";
import { darken, getContrastRatio, lighten } from "./color";

export type ColorScheme = "light" | "dark";

export interface PaletteColor {
  main: string;
  light: string;
  dark: string;
  contrastText: string;
}

export interface PaletteText {
  primary: string;
  secondary: string;
  disabled: string;
  icon?: string;
}

export interface PaletteAction {
  hover: string;
  selected: string;
  focus: string;
  active: string;
  disabled: string;
  disabledBackground: string;
  hoverOpacity: number;
  selectedOpacity: number;
  disabledOpacity: number;
  focusOpacity: number;
  activatedOpacity: number;
}

export interface WidgetPalette {
  mode: ColorScheme;
  primary: PaletteColor;
  secondary: PaletteColor;
  error: PaletteColor;
  warning: PaletteColor;
  success: PaletteColor;
  info: PaletteColor;
  background: { default: string; paper: string };
  text: PaletteText;
  divider: string;
  action: PaletteAction;
  /** MUI's contrast picker, same 3:1 threshold and same two return values. */
  getContrastText: (background: string) => string;
}

export interface WidgetTheme {
  /** The ACTIVE scheme, flattened — what `useTheme().palette` reads. */
  palette: WidgetPalette;
  /** Both schemes, so a consumer can pin a colour independently of the host. */
  colorSchemes: Record<ColorScheme, { palette: WidgetPalette }>;
  typography: typeof themeOptions.typography;
  shape: typeof themeOptions.shape;
  /** Extra org brand colors for the viz categorical palette (Story 23.1). */
  vizBranding?: { secondary?: string | null; accent?: string | null };
}

/** meta.branding payload shape (server: core/branding.py — AI-31 additive key). */
export interface OrgBranding {
  org_id?: string | null;
  brand_primary?: string | null;
  brand_secondary?: string | null;
  brand_accent?: string | null;
  /** Carried for provenance; NOT rendered (AD-11/NFR4 bundle rule — epic 23 scope). */
  logo_url?: string | null;
}

// ---------------------------------------------------------------------------
// Palette resolution — MUI's `augmentColor` and its scheme defaults, written out
// ---------------------------------------------------------------------------

/** MUI's tonalOffset. `light` = lighten(main, 0.2), `dark` = darken(main, 0.3). */
const TONAL_OFFSET = 0.2;
/** MUI's contrastThreshold. */
const CONTRAST_THRESHOLD = 3;

const TEXT_ON_LIGHT = "rgba(0, 0, 0, 0.87)";
const TEXT_ON_DARK_SURFACE = "#fff";

/**
 * The test is made against WHITE, and white is what wins it: a background that
 * reaches 3:1 against white takes white text; anything paler takes the near-black.
 * Stated because the intuition runs the other way and the inverted version is
 * self-consistent — it just turns every dark chip's label black.
 */
function getContrastText(background: string): string {
  return getContrastRatio(background, TEXT_ON_DARK_SURFACE) >= CONTRAST_THRESHOLD
    ? TEXT_ON_DARK_SURFACE
    : TEXT_ON_LIGHT;
}

/** A token colour states `main` and usually `contrastText`; the rest is derived. */
function augmentColor(color: {
  main: string;
  light?: string;
  dark?: string;
  contrastText?: string;
}): PaletteColor {
  return {
    main: color.main,
    light: color.light ?? lighten(color.main, TONAL_OFFSET),
    dark: color.dark ?? darken(color.main, TONAL_OFFSET * 1.5),
    contrastText: color.contrastText ?? getContrastText(color.main),
  };
}

/** The `text` and `action` defaults MUI fills per mode; the token file states neither. */
const SCHEME_DEFAULTS: Record<
  ColorScheme,
  { text: Omit<PaletteText, "primary" | "secondary">; action: Omit<PaletteAction, "hover" | "selected" | "focus"> }
> = {
  light: {
    text: { disabled: "rgba(0, 0, 0, 0.38)" },
    action: {
      active: "rgba(0, 0, 0, 0.54)",
      disabled: "rgba(0, 0, 0, 0.26)",
      disabledBackground: "rgba(0, 0, 0, 0.12)",
      hoverOpacity: 0.04,
      selectedOpacity: 0.08,
      disabledOpacity: 0.38,
      focusOpacity: 0.12,
      activatedOpacity: 0.12,
    },
  },
  dark: {
    text: { disabled: "rgba(255, 255, 255, 0.5)", icon: "rgba(255, 255, 255, 0.5)" },
    action: {
      active: "#fff",
      disabled: "rgba(255, 255, 255, 0.3)",
      disabledBackground: "rgba(255, 255, 255, 0.12)",
      hoverOpacity: 0.08,
      selectedOpacity: 0.16,
      disabledOpacity: 0.38,
      focusOpacity: 0.12,
      activatedOpacity: 0.24,
    },
  },
};

/** Light has no `action.focus` default in MUI; dark does. Kept as measured. */
const DARK_ACTION_FOCUS = "rgba(255, 255, 255, 0.12)";

type RawScheme = {
  palette: {
    primary: { main: string; light?: string; dark?: string; contrastText?: string };
    secondary: { main: string; light?: string; dark?: string; contrastText?: string };
    error: { main: string; light?: string; dark?: string; contrastText?: string };
    warning: { main: string; light?: string; dark?: string; contrastText?: string };
    success: { main: string; light?: string; dark?: string; contrastText?: string };
    info: { main: string; light?: string; dark?: string; contrastText?: string };
    background: { default: string; paper: string };
    text: { primary: string; secondary: string };
    divider: string;
    action: { hover: string; selected: string; focus?: string };
  };
};

function resolvePalette(mode: ColorScheme, raw: RawScheme): WidgetPalette {
  const p = raw.palette;
  const defaults = SCHEME_DEFAULTS[mode];
  return {
    mode,
    primary: augmentColor(p.primary),
    secondary: augmentColor(p.secondary),
    error: augmentColor(p.error),
    warning: augmentColor(p.warning),
    success: augmentColor(p.success),
    info: augmentColor(p.info),
    background: { ...p.background },
    text: { primary: p.text.primary, secondary: p.text.secondary, ...defaults.text },
    divider: p.divider,
    action: {
      hover: p.action.hover,
      selected: p.action.selected,
      focus: p.action.focus ?? (mode === "dark" ? DARK_ACTION_FOCUS : "rgba(0, 0, 0, 0.12)"),
      ...defaults.action,
    },
    getContrastText,
  };
}

function buildTheme(
  schemes: Record<ColorScheme, RawScheme>,
  vizBranding?: WidgetTheme["vizBranding"],
): WidgetTheme {
  const light = resolvePalette("light", schemes.light);
  const dark = resolvePalette("dark", schemes.dark);
  return {
    // Default to light, exactly as MUI's CSS-variables mode does before the host
    // announces a scheme; ThemeProvider swaps this for the active one.
    palette: light,
    colorSchemes: { light: { palette: light }, dark: { palette: dark } },
    typography: themeOptions.typography,
    shape: themeOptions.shape,
    ...(vizBranding ? { vizBranding } : {}),
  };
}

const RAW_SCHEMES = themeOptions.colorSchemes as unknown as Record<ColorScheme, RawScheme>;

/** connectorTheme — the canonical theme for all toorow widgets. */
export const connectorTheme: WidgetTheme = buildTheme(RAW_SCHEMES);

type DeepPartial<T> = { [K in keyof T]?: T[K] extends object ? DeepPartial<T[K]> : T[K] };

export interface CreateThemeOptions {
  /** Overrides applied to BOTH colour schemes, deep-merged over the tokens. */
  palette?: DeepPartial<RawScheme["palette"]>;
}

function deepMerge<T>(base: T, override: unknown): T {
  if (override === undefined || override === null) return base;
  if (typeof base !== "object" || base === null || Array.isArray(base)) return override as T;
  const out: Record<string, unknown> = { ...(base as Record<string, unknown>) };
  for (const [key, value] of Object.entries(override as Record<string, unknown>)) {
    out[key] = deepMerge((base as Record<string, unknown>)[key], value);
  }
  return out as T;
}

/**
 * createTheme — a theme with overrides deep-merged over the token defaults.
 *
 * Only tests use it, and only to pin one palette entry ("give this chart a
 * primary of rgb(200, 50, 100) and prove the fill follows"). Production never
 * builds a theme: widgets mount WidgetShell, which is the single injection
 * point (AD-11). Overrides apply to BOTH schemes, so a test does not
 * accidentally assert against light while the component reads dark.
 */
export function createTheme(options?: CreateThemeOptions): WidgetTheme {
  if (!options?.palette) return connectorTheme;
  return buildTheme({
    light: deepMerge(RAW_SCHEMES.light, { palette: options.palette }),
    dark: deepMerge(RAW_SCHEMES.dark, { palette: options.palette }),
  });
}

// ---------------------------------------------------------------------------
// Story 23.1 — branded theme derivation
// ---------------------------------------------------------------------------

/** Minimum accent-vs-surface contrast ratio (large graphic elements, AC4). */
const MIN_CONTRAST = 3;
const GUARD_STEP = 0.08;
const GUARD_MAX_ITERATIONS = 20;

/**
 * Contrast guard (AC4): an org color unreadable against *surface* is darkened
 * (light surface) / lightened (dark surface) stepwise until it reaches 3:1.
 * A color already readable is returned UNCHANGED — no aesthetic drift.
 */
export function ensureContrast(color: string, surface: string): string {
  let effective = color;
  const surfaceIsLight =
    getContrastRatio(surface, TEXT_DARK) > getContrastRatio(surface, TEXT_ON_DARK);
  for (let i = 0; i < GUARD_MAX_ITERATIONS; i += 1) {
    if (getContrastRatio(effective, surface) >= MIN_CONTRAST) return effective;
    effective = surfaceIsLight ? darken(effective, GUARD_STEP) : lighten(effective, GUARD_STEP);
  }
  return effective;
}

/** Branded themes are memoized on the 3 color values — no rebuild per render (AC3). */
const brandedThemeCache = new Map<string, WidgetTheme>();

/**
 * resolveWidgetTheme — the single entry WidgetShell uses to pick its theme.
 *
 * No branding (or no color set) -> `connectorTheme` BY REFERENCE (AC2).
 * Branding -> derived theme where brand_primary becomes palette.primary.main in
 * both schemes (contrast-guarded per scheme surface; contrastText + hover
 * derived here — the org only ever provides 3 colors), and
 * brand_secondary/brand_accent are exposed as theme.vizBranding for the
 * categorical viz palette (AC3/AC5).
 */
export function resolveWidgetTheme(branding?: OrgBranding | null): WidgetTheme {
  const primary = branding?.brand_primary ?? null;
  const secondary = branding?.brand_secondary ?? null;
  const accent = branding?.brand_accent ?? null;
  if (!primary && !secondary && !accent) return connectorTheme;

  const cacheKey = `${primary}|${secondary}|${accent}`;
  const cached = brandedThemeCache.get(cacheKey);
  if (cached) return cached;

  const primaryLight = primary ? ensureContrast(primary, SURFACE_LIGHT) : null;
  const primaryDark = primary ? ensureContrast(primary, SURFACE_DARK_ELEVATED) : null;

  const brandScheme = (scheme: RawScheme, primaryMain: string | null): RawScheme =>
    primaryMain
      ? {
          ...scheme,
          palette: {
            ...scheme.palette,
            // main only: light/dark/contrastText are derived (AC3 — no 4th color).
            primary: { main: primaryMain },
            action: {
              ...scheme.palette.action,
              selected: `${primaryMain}14`,
              focus: `${primaryMain}20`,
            },
          },
        }
      : scheme;

  const branded = buildTheme(
    {
      light: brandScheme(RAW_SCHEMES.light, primaryLight),
      dark: brandScheme(RAW_SCHEMES.dark, primaryDark),
    },
    { secondary, accent },
  );
  brandedThemeCache.set(cacheKey, branded);
  return branded;
}
