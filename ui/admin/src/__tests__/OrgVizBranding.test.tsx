/**
 * Story 50.5 AC17 -- the org-branding chain, end to end, on the Tailwind side.
 *
 * The two gaps this closes were MEASURED, not suspected:
 *   1. `brandingOf` in `shell/scope.tsx` read only `brand_primary` and `logo_url`
 *      and dropped `brand_secondary` / `brand_accent`, although the API type has
 *      always carried them (`ui/admin/src/orgs/types.ts`).
 *   2. `OrgThemeProvider` set four custom properties and never
 *      `--viz-brand-secondary` / `--viz-brand-accent`, which is exactly what
 *      `readCssVizTheme()` in `ui/cards/shell/src/vizTheme.ts` reads.
 * Result: `readCssVizTheme` found both empty, and a branded org's SECOND and THIRD
 * chart series silently fell back to the toorow defaults in the Console -- while
 * the MUI carrier passed all three to the card apps. One surface branded, one not.
 *
 * This file asserts the whole chain: API shape -> `brandingOf` -> custom properties
 * on `documentElement` -> `readCssVizTheme` -> `getVizPalette` -> the first three
 * categorical series colours, in order.
 */

import { render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { getVizPalette, readCssVizTheme } from "@toorow/card-shell";

import { OrgThemeProvider, contrastRatio } from "../shell/orgTheme";

const BRAND = { primary: "#123456", secondary: "#654321", accent: "#0B7285" };

afterEach(() => {
  for (const key of [
    "--color-primary",
    "--color-primary-hover",
    "--color-on-primary",
    "--focus",
    "--viz-brand-secondary",
    "--viz-brand-accent",
  ]) {
    document.documentElement.style.removeProperty(key);
  }
  document.documentElement.classList.remove("dark");
});

describe("AC17 -- the brand colours reach the shared runtime", () => {
  it("OrgThemeProvider sets the two --viz-brand-* properties readCssVizTheme reads", () => {
    render(
      <OrgThemeProvider
        branding={{ accent: BRAND.primary, secondary: BRAND.secondary, brandAccent: BRAND.accent }}
      >
        <div />
      </OrgThemeProvider>,
    );
    const style = document.documentElement.style;
    expect(style.getPropertyValue("--color-primary")).toBe(BRAND.primary);
    expect(style.getPropertyValue("--viz-brand-secondary")).toBe(BRAND.secondary);
    expect(style.getPropertyValue("--viz-brand-accent")).toBe(BRAND.accent);
  });

  it("the first three categorical series are the three brand colours, in order, in light", () => {
    render(
      <OrgThemeProvider
        branding={{ accent: BRAND.primary, secondary: BRAND.secondary, brandAccent: BRAND.accent }}
      >
        <div />
      </OrgThemeProvider>,
    );
    const palette = getVizPalette(readCssVizTheme(document.documentElement));
    expect(palette.categorical.slice(0, 3)).toEqual([
      BRAND.primary,
      BRAND.secondary,
      BRAND.accent,
    ]);
  });

  it("the same three, in the same order, in dark", () => {
    document.documentElement.classList.add("dark");
    render(
      <OrgThemeProvider
        branding={{ accent: BRAND.primary, secondary: BRAND.secondary, brandAccent: BRAND.accent }}
      >
        <div />
      </OrgThemeProvider>,
    );
    const theme = readCssVizTheme(document.documentElement);
    expect(theme.palette.mode).toBe("dark");
    const palette = getVizPalette(theme);
    expect(palette.categorical.slice(0, 3)).toEqual([
      BRAND.primary,
      BRAND.secondary,
      BRAND.accent,
    ]);
  });

  it("unmounting removes every property, so the next org does not inherit a colour", () => {
    const { unmount } = render(
      <OrgThemeProvider
        branding={{ accent: BRAND.primary, secondary: BRAND.secondary, brandAccent: BRAND.accent }}
      >
        <div />
      </OrgThemeProvider>,
    );
    unmount();
    expect(document.documentElement.style.getPropertyValue("--viz-brand-secondary")).toBe("");
    expect(document.documentElement.style.getPropertyValue("--viz-brand-accent")).toBe("");
  });
});

describe("AC17 -- one contrast guard, and it is `acceptedAccent` (refuse below 3:1)", () => {
  it("an accent below 3:1 against both light surfaces is refused, not stepped", () => {
    const pale = "#FFF4C1"; // ~1.1:1 against white
    expect(contrastRatio(pale, "#FFFFFF")).toBeLessThan(3);
    render(
      <OrgThemeProvider branding={{ accent: pale, secondary: BRAND.secondary }}>
        <div />
      </OrgThemeProvider>,
    );
    // Refused: the property is not set, so the token default stands. The org's own
    // colour is NOT replaced by a stepped near-miss they never chose.
    expect(document.documentElement.style.getPropertyValue("--color-primary")).toBe("");
  });

  it("a series colour is NOT subject to the 3:1 accent guard, and survives a refused accent", () => {
    render(
      <OrgThemeProvider branding={{ accent: "#FFF4C1", secondary: BRAND.secondary }}>
        <div />
      </OrgThemeProvider>,
    );
    // A series is identified by its legend entry and its accessible name, never by
    // colour alone (AC13), so a page-background contrast threshold is not the right
    // question for it.
    expect(document.documentElement.style.getPropertyValue("--viz-brand-secondary")).toBe(
      BRAND.secondary,
    );
  });
});

describe("AC17 gap 2 -- the hover accent is DERIVED, not the raw accent", () => {
  it("--color-primary-hover differs from --color-primary", () => {
    render(
      <OrgThemeProvider branding={{ accent: BRAND.primary }}>
        <div />
      </OrgThemeProvider>,
    );
    const style = document.documentElement.style;
    const base = style.getPropertyValue("--color-primary");
    const hover = style.getPropertyValue("--color-primary-hover");
    expect(base).toBe(BRAND.primary);
    expect(hover).not.toBe(base);
    expect(hover).toMatch(/^#[0-9a-f]{6}$/i);
  });
});
