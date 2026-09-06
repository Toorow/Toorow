import { createContext, useContext, useEffect, type ReactNode } from "react";

export interface OrgBranding {
  accent?: string;
  /** Story 50.5 AC17 — the org's second and third chart series colours. They
   *  reach the shared Visualization runtime as `--viz-brand-secondary` and
   *  `--viz-brand-accent`, which is what `readCssVizTheme()` in
   *  `ui/cards/shell/src/vizTheme.ts` reads. Before this, `brandingOf` in
   *  `scope.tsx` dropped both, those two properties were never set, and a
   *  branded org's second series silently fell back to a toorow default in the
   *  Console while the MUI carrier (`ui/shell/src/theme.ts`) passed all three. */
  secondary?: string;
  brandAccent?: string;
  logoUrl?: string;
  name?: string;
}

function hexToRgb(hex: string): [number, number, number] | null {
  const match = /^#?([0-9a-f]{6})$/i.exec(hex.trim());
  if (!match) return null;
  const value = parseInt(match[1], 16);
  return [(value >> 16) & 255, (value >> 8) & 255, value & 255];
}

function relLuminance([red, green, blue]: [number, number, number]): number {
  const channel = (value: number) => {
    const normalized = value / 255;
    return normalized <= 0.03928 ? normalized / 12.92 : Math.pow((normalized + 0.055) / 1.055, 2.4);
  };
  return 0.2126 * channel(red) + 0.7152 * channel(green) + 0.0722 * channel(blue);
}

export function contrastRatio(left: string, right: string): number {
  const leftRgb = hexToRgb(left);
  const rightRgb = hexToRgb(right);
  if (!leftRgb || !rightRgb) return 1;
  const leftLuminance = relLuminance(leftRgb);
  const rightLuminance = relLuminance(rightRgb);
  const [high, low] = leftLuminance > rightLuminance
    ? [leftLuminance, rightLuminance]
    : [rightLuminance, leftLuminance];
  return (high + 0.05) / (low + 0.05);
}

export function pickOnColor(background: string): string {
  return contrastRatio(background, "#111111") >= contrastRatio(background, "#FAFAFA") ? "#111111" : "#FAFAFA";
}

function acceptedAccent(branding: OrgBranding | null): string | null {
  const accent = branding?.accent;
  if (!accent || !hexToRgb(accent)) return null;
  return contrastRatio(accent, "#FFFFFF") >= 3 && contrastRatio(accent, "#F8F9FA") >= 3 ? accent : null;
}

/**
 * THE contrast guard, and there is exactly one (Story 50.5 AC17).
 *
 * Two behaviours existed: `acceptedAccent` here REFUSES an accent below 3:1
 * against both light surfaces, and `ensureContrast` in `ui/shell/src/theme.ts`
 * STEPS a colour until it reaches 3:1. Refusing is kept, and stepping is not
 * reimplemented here, for one reason: stepping returns a colour the organization
 * never chose and then presents it as their brand. Refusing falls back to the
 * toorow accent and leaves the org's own colour visible in Settings, where it can
 * be fixed. `ui/shell/src/theme.ts` keeps its own behaviour for the eight card
 * apps that still build on the MUI carrier; nothing new adopts it.
 *
 * The guard applies to the ACCENT ONLY. `brand_secondary` and `brand_accent`
 * reach the runtime as CHART SERIES colours, not as interactive-surface colours:
 * a series is identified by its legend entry and its accessible name, never by
 * colour alone (AC13), so a 3:1 threshold against a page background is not the
 * right question for them. They are accepted when they parse and dropped when
 * they do not.
 */
function acceptedSeriesColor(value: string | undefined): string | null {
  if (!value || !hexToRgb(value)) return null;
  return value;
}

/**
 * Derive the hover accent instead of assigning the raw accent to both
 * (Story 50.5 AC17, gap 2). `--color-primary-hover` was set to the accent
 * itself, so a branded org had no hover feedback on any primary control. MUI
 * derived it for the card apps (`ui/shell/src/theme.ts`, "MUI derives dark
 * (hover)"); the Tailwind side did not. Darken on light accents, lighten on dark
 * ones, so the step is visible in both directions.
 */
function deriveHover(accent: string): string {
  const rgb = hexToRgb(accent);
  if (!rgb) return accent;
  const towards = relLuminance(rgb) > 0.45 ? 0 : 255;
  const step = 0.14;
  const mixed = rgb.map((c) => Math.round(c + (towards - c) * step));
  return `#${mixed.map((c) => Math.max(0, Math.min(255, c)).toString(16).padStart(2, "0")).join("")}`;
}

const OrgBrandingContext = createContext<OrgBranding | null>(null);

export function useOrgBranding(): OrgBranding | null {
  return useContext(OrgBrandingContext);
}

const TOKEN_KEYS = [
  "--color-primary",
  "--color-primary-hover",
  "--color-on-primary",
  "--focus",
  // Story 50.5 AC17 — read by `readCssVizTheme()` in
  // `ui/cards/shell/src/vizTheme.ts`. Listed here so the cleanup path removes
  // them too: a brand colour left on `documentElement` after the org changed
  // would paint the next org's chart.
  "--viz-brand-secondary",
  "--viz-brand-accent",
];

export function OrgThemeProvider({ branding, children }: { branding: OrgBranding | null; children: ReactNode }) {
  useEffect(() => {
    const root = document.documentElement;
    const accent = acceptedAccent(branding);
    const secondary = acceptedSeriesColor(branding?.secondary);
    const brandAccent = acceptedSeriesColor(branding?.brandAccent);
    if (accent) {
      root.style.setProperty("--color-primary", accent);
      root.style.setProperty("--color-primary-hover", deriveHover(accent));
      root.style.setProperty("--color-on-primary", pickOnColor(accent));
      root.style.setProperty("--focus", accent);
    } else {
      root.style.removeProperty("--color-primary");
      root.style.removeProperty("--color-primary-hover");
      root.style.removeProperty("--color-on-primary");
      root.style.removeProperty("--focus");
    }
    // Set independently of the accent: an org may carry a valid second series
    // colour while its primary fails the 3:1 guard, and dropping it then would
    // lose a colour for a reason that has nothing to do with it.
    if (secondary) root.style.setProperty("--viz-brand-secondary", secondary);
    else root.style.removeProperty("--viz-brand-secondary");
    if (brandAccent) root.style.setProperty("--viz-brand-accent", brandAccent);
    else root.style.removeProperty("--viz-brand-accent");

    return () => TOKEN_KEYS.forEach((key) => root.style.removeProperty(key));
  }, [branding]);

  return <OrgBrandingContext.Provider value={branding}>{children}</OrgBrandingContext.Provider>;
}