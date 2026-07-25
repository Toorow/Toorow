/**
 * OrgThemeProvider — Epic 42 story 42.1.
 *
 * Org color tokenization (Jean 2026-07-24): no hardcoded accent. The active
 * organization's branding (Epic 21.2: up to 3 colors + logo) overrides the base
 * rose accent through the theme, with a WCAG contrast guardrail — if a branded
 * color cannot carry legible text/UI, we keep the base token instead of shipping
 * an inaccessible accent. Base theme stays `adminTheme`; this only overrides.
 */
import { createContext, useContext, useEffect, useMemo, type ReactNode } from "react";
import { ThemeProvider, createTheme, type Theme } from "@mui/material/styles";
import { adminTheme } from "../theme";

export interface OrgBranding {
  /** Primary accent hex (#RRGGBB). Overrides the rose when it passes contrast. */
  accent?: string;
  /** Optional logo URL (self-hosted); consumed by the sidebar brand slot. */
  logoUrl?: string;
  /** Display name, used as a fallback org mark. */
  name?: string;
}

// --- WCAG contrast helpers (no dependency) ---------------------------------

function hexToRgb(hex: string): [number, number, number] | null {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex.trim());
  if (!m) return null;
  const n = parseInt(m[1], 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

function relLuminance([r, g, b]: [number, number, number]): number {
  const f = (c: number) => {
    const s = c / 255;
    return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
  };
  return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
}

/** WCAG contrast ratio between two hex colors (1..21). */
export function contrastRatio(a: string, b: string): number {
  const ra = hexToRgb(a);
  const rb = hexToRgb(b);
  if (!ra || !rb) return 1;
  const la = relLuminance(ra);
  const lb = relLuminance(rb);
  const [hi, lo] = la > lb ? [la, lb] : [lb, la];
  return (hi + 0.05) / (lo + 0.05);
}

/** Pick the legible on-color (near-black vs near-white) for a background. */
export function pickOnColor(bg: string): string {
  return contrastRatio(bg, "#111111") >= contrastRatio(bg, "#FAFAFA")
    ? "#111111"
    : "#FAFAFA";
}

/**
 * Build a themed MUI theme from org branding, applying the accent only when it
 * meets a 3:1 non-text contrast floor against both light surface and page. Below
 * that, the base rose token is kept (fail-safe, never ship an unusable accent).
 */
export function themeForOrg(base: Theme, branding: OrgBranding | null): Theme {
  const accent = branding?.accent;
  if (!accent || !hexToRgb(accent)) return base;

  const usableOnSurface = contrastRatio(accent, "#FFFFFF") >= 3;
  const usableOnPage = contrastRatio(accent, "#F8F9FA") >= 3;
  if (!usableOnSurface || !usableOnPage) return base;

  const onAccent = pickOnColor(accent);
  return createTheme(base, {
    palette: {
      primary: { main: accent, contrastText: onAccent },
    },
  });
}

/**
 * CSS custom-property overrides for the mockup token layer (application.css uses
 * --rose / --rose-soft / --focus). Returns null when the org accent fails the
 * contrast floor, so we keep the base tokens rather than ship an unusable color.
 */
function orgCssVars(branding: OrgBranding | null): Record<string, string> | null {
  const accent = branding?.accent;
  if (!accent || !hexToRgb(accent)) return null;
  if (contrastRatio(accent, "#FFFFFF") < 3 || contrastRatio(accent, "#F8F9FA") < 3) return null;
  return { "--rose": accent, "--rose-soft": `${accent}1f`, "--focus": accent };
}

const ORG_VAR_KEYS = ["--rose", "--rose-soft", "--focus"];

// --- Context ----------------------------------------------------------------

const OrgBrandingContext = createContext<OrgBranding | null>(null);

export function useOrgBranding(): OrgBranding | null {
  return useContext(OrgBrandingContext);
}

export function OrgThemeProvider({
  branding,
  children,
}: {
  branding: OrgBranding | null;
  children: ReactNode;
}) {
  const theme = useMemo(() => themeForOrg(adminTheme, branding), [branding]);

  // Tokenized org customization: drive the mockup CSS variables from branding.
  useEffect(() => {
    const el = document.documentElement;
    const vars = orgCssVars(branding);
    if (vars) ORG_VAR_KEYS.forEach((k) => el.style.setProperty(k, vars[k]));
    else ORG_VAR_KEYS.forEach((k) => el.style.removeProperty(k));
    return () => ORG_VAR_KEYS.forEach((k) => el.style.removeProperty(k));
  }, [branding]);

  return (
    <OrgBrandingContext.Provider value={branding}>
      <ThemeProvider theme={theme}>{children}</ThemeProvider>
    </OrgBrandingContext.Provider>
  );
}
