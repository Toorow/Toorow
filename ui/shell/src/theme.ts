/**
 * Connector MUI Theme.
 *
 * This file is the SINGLE MUI theme for the entire Connector widget platform.
 * All widgets import WidgetShell from @toorow/shell to receive this theme
 * automatically — widgets MUST NOT call createTheme() themselves (AD-11).
 *
 * The theme options are derived exclusively from the W3C DTCG token file at
 * ui/tokens/tokens.json via the Style Dictionary pipeline:
 *
 *   tokens.json → style-dictionary.config.js → dist/theme.ts (ThemeOptions) → here
 *
 * Run `pnpm build:tokens` before this file is consumed (CI does this automatically).
 *
 * No inline color literals are permitted in this file beyond what Style Dictionary
 * derives from tokens.json (T3.2 constraint).
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

import {
  createTheme,
  darken,
  lighten,
  getContrastRatio,
  type Theme,
  type ThemeOptions,
} from "@mui/material/styles";
import themeOptions from "../../tokens/dist/theme";
import {
  TEXT_DARK,
  TEXT_ON_DARK,
  SURFACE_LIGHT,
  SURFACE_DARK_ELEVATED,
} from "../../tokens/dist/theme";

declare module "@mui/material/styles" {
  interface Theme {
    /** Extra org brand colors for the viz categorical palette (Story 23.1). */
    vizBranding?: {
      secondary?: string | null;
      accent?: string | null;
    };
  }
  interface ThemeOptions {
    vizBranding?: {
      secondary?: string | null;
      accent?: string | null;
    };
  }
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

/**
 * connectorTheme — the canonical MUI theme for all Connector widgets.
 *
 * CSS Variables mode is enabled (cssVariables: true via themeOptions) so that
 * useColorScheme() can switch light/dark at runtime without a full re-render (AC2).
 *
 * The colorSchemeSelector "data" means MUI writes the active scheme to the
 * data-color-scheme attribute on :root — WidgetShell.tsx syncs this with the
 * host's data-color-scheme signal (AC2 / UX-DR5).
 */
// The generated artifact is dependency-free pure data; the ThemeOptions
// contract is applied here, at the single consumption point.
export const connectorTheme = createTheme(themeOptions as ThemeOptions);

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
  const surfaceIsLight = getContrastRatio(surface, TEXT_DARK) > getContrastRatio(surface, TEXT_ON_DARK);
  for (let i = 0; i < GUARD_MAX_ITERATIONS; i += 1) {
    if (getContrastRatio(effective, surface) >= MIN_CONTRAST) return effective;
    effective = surfaceIsLight ? darken(effective, GUARD_STEP) : lighten(effective, GUARD_STEP);
  }
  return effective;
}

/** Branded themes are memoized on the 3 color values — no createTheme per render (AC3). */
const brandedThemeCache = new Map<string, Theme>();

/**
 * resolveWidgetTheme — the single entry WidgetShell uses to pick its theme.
 *
 * No branding (or no color set) → `connectorTheme` BY REFERENCE (AC2).
 * Branding → derived theme where brand_primary becomes palette.primary.main in
 * both schemes (contrast-guarded per scheme surface; contrastText + hover
 * derived by MUI — the org only ever provides 3 colors), and
 * brand_secondary/brand_accent are exposed as theme.vizBranding for the
 * categorical viz palette (AC3/AC5).
 */
export function resolveWidgetTheme(branding?: OrgBranding | null): Theme {
  const primary = branding?.brand_primary ?? null;
  const secondary = branding?.brand_secondary ?? null;
  const accent = branding?.brand_accent ?? null;
  if (!primary && !secondary && !accent) return connectorTheme;

  const cacheKey = `${primary}|${secondary}|${accent}`;
  const cached = brandedThemeCache.get(cacheKey);
  if (cached) return cached;

  // Generated artifact is pure data — safe to spread scheme-by-scheme.
  const base = themeOptions as ThemeOptions & {
    colorSchemes: Record<"light" | "dark", { palette: Record<string, unknown> }>;
    components: Record<string, unknown>;
  };

  const primaryLight = primary ? ensureContrast(primary, SURFACE_LIGHT) : null;
  const primaryDark = primary ? ensureContrast(primary, SURFACE_DARK_ELEVATED) : null;

  const brandScheme = (
    scheme: { palette: Record<string, unknown> },
    primaryMain: string | null,
  ) =>
    primaryMain
      ? {
          ...scheme,
          palette: {
            ...scheme.palette,
            // main only: MUI derives dark (hover) and contrastText (AC3 — no 4th color).
            primary: { main: primaryMain },
            action: {
              ...(scheme.palette.action as Record<string, unknown>),
              selected: `${primaryMain}14`,
              focus: `${primaryMain}20`,
            },
          },
        }
      : scheme;

  // Assembled as pure data (same as the generated artifact) and typed at the
  // single createTheme consumption point below — matches the connectorTheme
  // pattern; strict object-literal checking rejects the variant-level button
  // override keys otherwise.
  const options = {
    ...base,
    vizBranding: { secondary, accent },
    colorSchemes: {
      light: brandScheme(base.colorSchemes.light, primaryLight),
      dark: brandScheme(base.colorSchemes.dark, primaryDark),
    },
    components: primaryLight
      ? {
          ...base.components,
          // The shared button override hardcodes the toorow accent from tokens —
          // re-derive it so shell chrome (e.g. reconnect) follows the brand too.
          MuiButton: {
            ...(base.components.MuiButton as Record<string, unknown>),
            styleOverrides: {
              ...((base.components.MuiButton as { styleOverrides?: Record<string, unknown> })
                .styleOverrides ?? {}),
              containedPrimary: {
                backgroundColor: primaryLight,
                color:
                  getContrastRatio(primaryLight, TEXT_DARK) >=
                  getContrastRatio(primaryLight, TEXT_ON_DARK)
                    ? TEXT_DARK
                    : TEXT_ON_DARK,
                "&:hover": { backgroundColor: darken(primaryLight, 0.12) },
              },
              outlinedPrimary: {
                borderColor: primaryLight,
                "&:hover": { backgroundColor: `${primaryLight}14` },
              },
            },
          },
        }
      : base.components,
  };

  const branded = createTheme(options as ThemeOptions);
  brandedThemeCache.set(cacheKey, branded);
  return branded;
}
