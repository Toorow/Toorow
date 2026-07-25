/**
 * Story 23.1 — the theme-driven viz color contract (AC5) + branded end-to-end (AC6).
 *
 * AC5: getVizPalette works on a bare createTheme() (standard MUI colors, no crash),
 *      puts org colors first in categorical when the theme is branded, derives the
 *      diverging ramp from the semantic palette, and a STATIC GUARD proves no
 *      primitive imports color constants from tokens/dist/theme (only vizTheme.ts).
 * AC6: a BarChart mounted under the branded theme renders fills derived from the
 *      org color, and the default mount keeps the toorow accent.
 */

import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import { ThemeProvider, createTheme, decomposeColor } from "@mui/material/styles";
import { resolveWidgetTheme } from "@toorow/shell";
import { getVizPalette } from "../vizTheme";
import BarChart from "../BarChart";

const BRAND = "#0F6FFF"; // rgb(15, 111, 255)
const TOOROW_ACCENT = "#FF99C8"; // rgb(255, 153, 200)

function rgbOf(color: string): number[] {
  return decomposeColor(color).values.slice(0, 3) as number[];
}

describe("getVizPalette (AC5)", () => {
  it("fonctionne sur un createTheme() nu — couleurs MUI standard, >= 6 catégorielles", () => {
    const theme = createTheme();
    const viz = getVizPalette(theme);
    expect(viz.accent).toBe(theme.palette.primary.main);
    expect(viz.categorical.length).toBeGreaterThanOrEqual(6);
    expect(viz.categorical[0]).toBe(theme.palette.primary.main);
    expect(typeof viz.track).toBe("string");
    expect(typeof viz.hairline).toBe("string");
  });

  it("place les couleurs d'org en tête de la palette catégorielle (thème brandé)", () => {
    const branded = resolveWidgetTheme({
      brand_primary: BRAND,
      brand_secondary: "#00AA55",
      brand_accent: "#AA00CC",
    });
    const viz = getVizPalette(branded);
    expect(viz.categorical[0]).toBe(BRAND);
    expect(viz.categorical[1]).toBe("#00AA55");
    expect(viz.categorical[2]).toBe("#AA00CC");
    expect(viz.categorical.length).toBeGreaterThanOrEqual(6); // complétée par les défauts
  });

  it("diverging(t) interpole success → warning → error du thème courant", () => {
    const theme = createTheme();
    const viz = getVizPalette(theme);
    expect(rgbOf(viz.diverging(0))).toEqual(rgbOf(theme.palette.success.main));
    expect(rgbOf(viz.diverging(0.5))).toEqual(rgbOf(theme.palette.warning.main));
    expect(rgbOf(viz.diverging(1))).toEqual(rgbOf(theme.palette.error.main));
    // t hors bornes clampé — jamais de crash.
    expect(viz.diverging(-1)).toBe(viz.diverging(0));
    expect(viz.diverging(2)).toBe(viz.diverging(1));
  });

  it("divergingStops(n) rend n paliers du vert au rouge", () => {
    const theme = createTheme();
    const viz = getVizPalette(theme);
    const stops = viz.divergingStops(5);
    expect(stops).toHaveLength(5);
    expect(rgbOf(stops[0]!)).toEqual(rgbOf(theme.palette.success.main));
    expect(rgbOf(stops[4]!)).toEqual(rgbOf(theme.palette.error.main));
  });
});

// Raw sources of every top-level primitive module (excludes __tests__), loaded
// through Vite's ?raw pipeline — no node fs access needed under jsdom.
const rawSources = import.meta.glob("../*.{ts,tsx}", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

describe("garde statique (AC5) — aucune primitive n'importe les constantes couleur des tokens", () => {
  it("seul vizTheme.ts importe ORIGIN_CATEGORICAL_PALETTE / TRACK_LIGHT / ACCENT", () => {
    expect(Object.keys(rawSources).length).toBeGreaterThan(10); // le glob voit bien src/
    const offenders: string[] = [];
    for (const [path, content] of Object.entries(rawSources)) {
      if (path.endsWith("/vizTheme.ts")) continue;
      const tokenImport = content.match(
        /import\s*\{([^}]*)\}\s*from\s*["'][^"']*tokens\/dist\/theme["']/,
      );
      if (!tokenImport) continue;
      // Non-color constants (shadows) stay legitimate for CardShell chrome.
      const names: string[] = tokenImport[1]!.split(",").map((s: string) => s.trim());
      const colorNames = names.filter((n: string) =>
        /^(ACCENT|ACCENT_HOVER|ON_ACCENT|ORIGIN_CATEGORICAL_PALETTE|TRACK_LIGHT|ERROR|WARNING|SUCCESS|INFO)$/.test(n),
      );
      if (colorNames.length > 0) offenders.push(`${path}: ${colorNames.join(", ")}`);
    }
    expect(offenders).toEqual([]);
  });
});

describe("BarChart brandé de bout en bout (AC6)", () => {
  const entries = [
    { label: "Organique", value: 120 },
    { label: "Payant", value: 80 },
  ];

  function barFills(container: HTMLElement): string[] {
    // Horizontal variant renders HTML bars via MUI Box backgroundColor; assert on
    // the vertical SVG variant instead — rect fills are plain attributes.
    return Array.from(container.querySelectorAll("svg rect"))
      .map((r) => r.getAttribute("fill") ?? "")
      .filter(Boolean);
  }

  it("avec meta.branding, au moins un fill dérive de la couleur d'org (pas du rose toorow)", () => {
    const branded = resolveWidgetTheme({ brand_primary: BRAND });
    const { container } = render(
      <ThemeProvider theme={branded}>
        <BarChart entries={entries} variant="vertical" ariaLabel="Test brandé" />
      </ThemeProvider>,
    );
    const fills = barFills(container);
    const brandRgb = rgbOf(BRAND);
    const hasBrandFill = fills.some((f) => {
      try {
        return rgbOf(f).every((c, i) => Math.abs(c - brandRgb[i]!) <= 2);
      } catch {
        return false;
      }
    });
    expect(hasBrandFill).toBe(true);
    const accentRgb = rgbOf(TOOROW_ACCENT);
    const hasToorowFill = fills.some((f) => {
      try {
        return rgbOf(f).every((c, i) => Math.abs(c - accentRgb[i]!) <= 2);
      } catch {
        return false;
      }
    });
    expect(hasToorowFill).toBe(false);
  });

  it("sans branding, le fill accent toorow est inchangé", () => {
    const { container } = render(
      <ThemeProvider theme={resolveWidgetTheme(undefined)}>
        <BarChart entries={entries} variant="vertical" ariaLabel="Test défaut" />
      </ThemeProvider>,
    );
    const accentRgb = rgbOf(TOOROW_ACCENT);
    const hasToorowFill = barFills(container).some((f) => {
      try {
        return rgbOf(f).every((c, i) => Math.abs(c - accentRgb[i]!) <= 2);
      } catch {
        return false;
      }
    });
    expect(hasToorowFill).toBe(true);
  });
});
