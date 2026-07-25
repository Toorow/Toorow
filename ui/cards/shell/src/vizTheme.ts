/**
 * vizTheme — the SINGLE color contract for every viz primitive (Story 23.1, AC5).
 *
 * Rule: primitives NEVER import color constants from tokens/dist/theme — module-level
 * constants bypass the WidgetShell ThemeProvider, which is the unique injection point
 * for org branding (AD-11; meta.branding, Story 23.1). This module is the ONLY file
 * in card-shell allowed to read the token constants, and only as defaults behind the
 * live theme values.
 *
 * Everything here derives from the CURRENT theme:
 *   - accent           = palette.primary.main (= brand_primary when branded)
 *   - categorical[>=6] = org colors first (theme.vizBranding), toorow tints after
 *   - diverging(t)     = success → warning → error interpolation (green→yellow→red);
 *                        light/dark/branding follow with zero extra tokens
 *   - track/hairline   = the Origin rail + hairline derivations (BarChart/
 *                        CalendarHeatmap patterns, unchanged visuals by default)
 *
 * A bare createTheme() (tests) yields standard MUI colors without crash (AC5).
 */

import type { Theme } from "@mui/material/styles";
import {
  alpha,
  decomposeColor,
  recomposeColor,
} from "@mui/material/styles";
import { ORIGIN_CATEGORICAL_PALETTE, TRACK_LIGHT } from "../../../tokens/dist/theme";

declare module "@mui/material/styles" {
  interface Theme {
    /** Extra org brand colors carried by the branded theme (Story 23.1, AC3/AC5). */
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

export interface VizPalette {
  /** The hero series color — follows org branding via palette.primary.main. */
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
}

/** Linear RGB(A) interpolation between two CSS colors (hex or rgb/rgba). */
function mix(from: string, to: string, t: number): string {
  try {
    const a = decomposeColor(from);
    const b = decomposeColor(to);
    // Align alpha channels: pad the shorter values with alpha=1.
    const va = a.values.length < 4 ? ([...a.values, 1] as typeof a.values) : a.values;
    const vb = b.values.length < 4 ? ([...b.values, 1] as typeof b.values) : b.values;
    const values = va.map((v, i) => v + ((vb[i] ?? 1) - v) * t);
    return recomposeColor({
      type: "rgba",
      values: values as [number, number, number, number],
    });
  } catch {
    // Unparseable custom color (CSS var, named color…): honest endpoint fallback.
    return t < 0.5 ? from : to;
  }
}

export function getVizPalette(theme: Theme): VizPalette {
  const isDark = theme.palette.mode === "dark";
  const accent = theme.palette.primary.main;

  // Org colors first when the branded theme carries them (Story 23.1 AC3),
  // then the toorow defaults, deduplicated, ALWAYS >= 6 entries.
  const head = [
    accent,
    theme.vizBranding?.secondary ?? null,
    theme.vizBranding?.accent ?? null,
  ].filter((c): c is string => !!c);
  const categorical = [
    ...head,
    ...ORIGIN_CATEGORICAL_PALETTE.filter((c) => !head.includes(c)),
  ];

  const success = theme.palette.success.main;
  const warning = theme.palette.warning.main;
  const error = theme.palette.error.main;

  const diverging = (t: number): string => {
    const c = Math.max(0, Math.min(1, t));
    return c <= 0.5 ? mix(success, warning, c * 2) : mix(warning, error, (c - 0.5) * 2);
  };

  return {
    accent,
    categorical,
    track: isDark ? alpha(TRACK_LIGHT, 0.1) : TRACK_LIGHT,
    hairline: alpha(theme.palette.text.primary, isDark ? 0.1 : 0.08),
    diverging,
    divergingStops: (n: number): string[] => {
      const count = Math.max(2, Math.floor(n));
      return Array.from({ length: count }, (_, i) => diverging(i / (count - 1)));
    },
  };
}
