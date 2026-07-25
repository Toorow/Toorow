/**
 * Story 23.1 — org branding theme derivation (AC2 / AC3 / AC4).
 *
 * Proves at the single injection point (resolveWidgetTheme + WidgetShell):
 *   AC2: no branding → connectorTheme BY REFERENCE, toorow accent intact.
 *   AC3: branding → brand_primary becomes palette.primary.main in BOTH schemes,
 *        derivation is memoized, vizBranding carries the extra org colors.
 *   AC4: contrast guard — unreadable colors corrected, readable colors untouched.
 */

import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { useTheme, getContrastRatio } from "@mui/material/styles";
import {
  connectorTheme,
  resolveWidgetTheme,
  ensureContrast,
} from "../theme";
import WidgetShell from "../WidgetShell";
import type { WidgetMeta } from "../types";

const TOOROW_ACCENT = "#FF99C8";
const BRAND = "#0F6FFF";

function baseMeta(branding?: WidgetMeta["branding"]): WidgetMeta {
  return {
    freshness: { last_pull: "2026-07-21T00:00:00Z", cadence_hours: 24 },
    provenance: { source_system: "seeded", pull_id: "pull_test" },
    alerts: [],
    ...(branding ? { branding } : {}),
  };
}

/** Probe child that surfaces the ACTIVE theme primary color from inside the shell. */
function ThemeProbe() {
  const theme = useTheme();
  return <div data-testid="theme-probe">{theme.palette.primary.main}</div>;
}

describe("resolveWidgetTheme (AC2 — défaut bit-identique)", () => {
  it("retourne connectorTheme PAR RÉFÉRENCE sans branding", () => {
    expect(resolveWidgetTheme(undefined)).toBe(connectorTheme);
    expect(resolveWidgetTheme(null)).toBe(connectorTheme);
    // Branding présent mais sans aucune couleur (logo seul) → défaut aussi.
    expect(resolveWidgetTheme({ logo_url: "https://x/logo.png" })).toBe(connectorTheme);
  });

  it("le défaut porte l'accent toorow dans les deux schemes", () => {
    const schemes = (connectorTheme as unknown as {
      colorSchemes: Record<string, { palette: { primary: { main: string } } }>;
    }).colorSchemes;
    expect(schemes.light.palette.primary.main).toBe(TOOROW_ACCENT);
    expect(schemes.dark.palette.primary.main).toBe(TOOROW_ACCENT);
  });
});

describe("resolveWidgetTheme (AC3 — fusion des couleurs d'org)", () => {
  it("brand_primary devient palette.primary.main dans les DEUX schemes", () => {
    const theme = resolveWidgetTheme({ brand_primary: BRAND });
    const schemes = (theme as unknown as {
      colorSchemes: Record<string, { palette: { primary: { main: string; contrastText?: string } } }>;
    }).colorSchemes;
    // #0F6FFF est lisible sur les deux surfaces → utilisé tel quel (AC4).
    expect(schemes.light.palette.primary.main).toBe(BRAND);
    expect(schemes.dark.palette.primary.main).toBe(BRAND);
    // contrastText DÉRIVÉ par MUI, jamais demandé à l'org.
    expect(schemes.light.palette.primary.contrastText).toBeTruthy();
  });

  it("est mémoïsé sur les valeurs de branding (même référence)", () => {
    const a = resolveWidgetTheme({ brand_primary: BRAND, org_id: "org_A" });
    const b = resolveWidgetTheme({ brand_primary: BRAND, org_id: "org_B" });
    expect(a).toBe(b); // seules les 3 couleurs clés participent à la clé
  });

  it("expose brand_secondary/brand_accent en vizBranding pour la palette catégorielle", () => {
    const theme = resolveWidgetTheme({
      brand_primary: BRAND,
      brand_secondary: "#00AA55",
      brand_accent: "#AA00CC",
    });
    expect(theme.vizBranding).toEqual({ secondary: "#00AA55", accent: "#AA00CC" });
  });

  it("WidgetShell injecte le thème brandé via son unique ThemeProvider", () => {
    render(
      <WidgetShell meta={baseMeta({ brand_primary: BRAND })}>
        <ThemeProbe />
      </WidgetShell>,
    );
    expect(screen.getByTestId("theme-probe").textContent).toBe(BRAND);
  });

  it("WidgetShell sans branding rend l'accent toorow inchangé", () => {
    render(
      <WidgetShell meta={baseMeta()}>
        <ThemeProbe />
      </WidgetShell>,
    );
    expect(screen.getByTestId("theme-probe").textContent).toBe(TOOROW_ACCENT);
  });
});

describe("ensureContrast (AC4 — garde-fou lisibilité)", () => {
  it("assombrit une couleur quasi blanche sur surface claire jusqu'à 3:1", () => {
    const corrected = ensureContrast("#FEFEFE", "#FFFFFF");
    expect(corrected).not.toBe("#FEFEFE");
    expect(getContrastRatio(corrected, "#FFFFFF")).toBeGreaterThanOrEqual(3);
  });

  it("éclaircit une couleur quasi noire sur surface sombre jusqu'à 3:1", () => {
    const corrected = ensureContrast("#111111", "#1E1E1E");
    expect(getContrastRatio(corrected, "#1E1E1E")).toBeGreaterThanOrEqual(3);
  });

  it("laisse TELLE QUELLE une couleur déjà lisible (pas de dérive esthétique)", () => {
    expect(ensureContrast(BRAND, "#FFFFFF")).toBe(BRAND);
  });

  it("le thème brandé applique le garde par scheme (couleur pathologique)", () => {
    const theme = resolveWidgetTheme({ brand_primary: "#FEFEFE" });
    const schemes = (theme as unknown as {
      colorSchemes: Record<string, { palette: { primary: { main: string } } }>;
    }).colorSchemes;
    expect(getContrastRatio(schemes.light.palette.primary.main, "#FFFFFF")).toBeGreaterThanOrEqual(3);
    // En dark, quasi-blanc est déjà lisible → conservé tel quel.
    expect(schemes.dark.palette.primary.main).toBe("#FEFEFE");
  });
});
