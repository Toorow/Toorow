/**
 * The palette the widgets read is the one MUI used to hand them (AD-35).
 *
 * WHERE THE EXPECTED VALUES COME FROM. They were not invented here. On
 * 2026-08-05, before `@mui/material` was removed, MUI's `createTheme()` was run
 * over `ui/tokens/dist/theme.ts` and its resolved palette dumped for both colour
 * schemes; the numbers below are that dump, transcribed. That is what makes this
 * a migration test rather than a description of whatever the new code happens to
 * do — a re-derivation that drifted would change a colour on every card and
 * every connector widget at once, silently, because nothing else looks at it.
 *
 * The derived values are the interesting ones. `primary.light`, `secondary.dark`
 * and `text.disabled` are NOT in tokens.json: MUI filled them in from
 * `tonalOffset` and its per-mode defaults, and 120 read sites inherited them
 * without anyone writing them down. They are written down now.
 */

import { describe, expect, it } from "vitest";
import { connectorTheme, createTheme, resolveWidgetTheme, ensureContrast } from "../theme";
import { getContrastRatio } from "../color";

const light = connectorTheme.colorSchemes.light.palette;
const dark = connectorTheme.colorSchemes.dark.palette;

describe("the resolved palette matches what MUI produced from the same tokens", () => {
  it("carries the token values through untouched", () => {
    expect(light.primary.main).toBe("#FF99C8");
    expect(light.primary.dark).toBe("#F77FB4");
    expect(light.primary.contrastText).toBe("#111111");
    expect(light.text.primary).toBe("#111111");
    expect(light.text.secondary).toBe("#6B6A74");
    expect(light.divider).toBe("#EBEBF3");
    expect(light.background.paper).toBe("#FFFFFF");
    expect(light.action.hover).toBe("#EBEBF380");

    expect(dark.text.primary).toBe("#FAFAFA");
    expect(dark.divider).toBe("rgba(235,235,243,0.30)");
    expect(dark.background.paper).toBe("#1E1E1E");
    expect(dark.error.main).toBe("#F28B93");
  });

  it("derives light/dark from main with MUI's tonalOffset (0.2 / 0.3)", () => {
    // tokens.json states neither; every gradient and hover state used them.
    expect(light.primary.light).toBe("rgb(255, 173, 211)");
    expect(light.secondary.light).toBe("rgb(136, 135, 143)");
    expect(light.secondary.dark).toBe("rgb(74, 74, 81)");
    expect(light.error.dark).toBe("rgb(149, 48, 56)");
    expect(light.warning.dark).toBe("rgb(162, 112, 42)");
    expect(light.success.dark).toBe("rgb(43, 108, 77)");
    expect(light.info.dark).toBe("rgb(88, 74, 137)");
  });

  it("fills the per-mode text and action defaults MUI supplied", () => {
    expect(light.text.disabled).toBe("rgba(0, 0, 0, 0.38)");
    expect(light.action.active).toBe("rgba(0, 0, 0, 0.54)");
    expect(light.action.disabled).toBe("rgba(0, 0, 0, 0.26)");
    expect(light.action.hoverOpacity).toBe(0.04);
    expect(light.action.selectedOpacity).toBe(0.08);

    expect(dark.text.disabled).toBe("rgba(255, 255, 255, 0.5)");
    expect(dark.text.icon).toBe("rgba(255, 255, 255, 0.5)");
    expect(dark.action.active).toBe("#fff");
    expect(dark.action.focus).toBe("rgba(255, 255, 255, 0.12)");
    expect(dark.action.hoverOpacity).toBe(0.08);
    expect(dark.action.activatedOpacity).toBe(0.24);
  });

  it("getContrastText picks the same two values at the same 3:1 threshold", () => {
    expect(light.getContrastText("#FFFFFF")).toBe("rgba(0, 0, 0, 0.87)");
    expect(light.getContrastText("#FF99C8")).toBe("rgba(0, 0, 0, 0.87)");
    expect(light.getContrastText("#1E1E1E")).toBe("#fff");
    expect(light.getContrastText("#3E9B6E")).toBe("#fff");
  });

  it("mode is set, and the theme defaults to light before the host speaks", () => {
    expect(light.mode).toBe("light");
    expect(dark.mode).toBe("dark");
    expect(connectorTheme.palette).toBe(light);
    expect(connectorTheme.shape.borderRadius).toBe(12);
  });
});

describe("createTheme overrides reach both schemes", () => {
  it("a bare call returns the canonical theme by reference", () => {
    expect(createTheme()).toBe(connectorTheme);
  });

  it("a palette override is deep-merged, not a replacement", () => {
    const theme = createTheme({ palette: { primary: { main: "rgb(200, 50, 100)" } } });
    for (const scheme of ["light", "dark"] as const) {
      const palette = theme.colorSchemes[scheme].palette;
      expect(palette.primary.main).toBe("rgb(200, 50, 100)");
      // The rest of the scheme survives — an override is not a reset.
      expect(palette.text.primary).toBe(connectorTheme.colorSchemes[scheme].palette.text.primary);
      expect(palette.success.main).toBe(connectorTheme.colorSchemes[scheme].palette.success.main);
    }
    // And it does not leak into the canonical theme.
    expect(connectorTheme.palette.primary.main).toBe("#FF99C8");
  });
});

describe("the branding contrast guard still walks the same steps (Story 23.1 AC4)", () => {
  it("a readable colour is returned unchanged", () => {
    expect(ensureContrast("#0F6FFF", "#FFFFFF")).toBe("#0F6FFF");
  });

  it("an unreadable colour is corrected to at least 3:1", () => {
    const corrected = ensureContrast("#FEFEFE", "#FFFFFF");
    expect(corrected).not.toBe("#FEFEFE");
    expect(getContrastRatio(corrected, "#FFFFFF")).toBeGreaterThanOrEqual(3);
  });

  it("the branded theme derives contrastText rather than asking the org for it", () => {
    const branded = resolveWidgetTheme({ brand_primary: "#0F6FFF" });
    expect(branded.colorSchemes.light.palette.primary.main).toBe("#0F6FFF");
    expect(branded.colorSchemes.light.palette.primary.contrastText).toBe("#fff");
    expect(branded.colorSchemes.light.palette.primary.light).toBe("rgb(63, 139, 255)");
  });
});
