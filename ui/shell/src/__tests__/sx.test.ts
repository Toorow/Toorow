/**
 * `sx` translation — the 232 call sites depend on these exact conversions.
 *
 * The cases below are not invented: each one is a shape that appears in
 * `ui/cards/*` or `ui/widgets/*` today. A spacing unit that stopped being 8px,
 * or `color: "text.secondary"` that stopped resolving, would not fail a single
 * component test — the components assert on their data, not their pixels — so
 * this is where that failure has to surface.
 */

import { afterEach, describe, expect, it } from "vitest";
import { resolveSx, resolveColor, __resetSxStylesForTests } from "../sx";
import { connectorTheme } from "../theme";

const theme = connectorTheme;

afterEach(() => {
  __resetSxStylesForTests();
});

describe("spacing shorthands are 8px units", () => {
  it("expands the one-letter margin and padding props", () => {
    expect(resolveSx({ mb: 1 }, theme).style).toEqual({ marginBottom: "8px" });
    expect(resolveSx({ mt: 2 }, theme).style).toEqual({ marginTop: "16px" });
    expect(resolveSx({ pt: 1.5 }, theme).style).toEqual({ paddingTop: "12px" });
    expect(resolveSx({ py: 0.25 }, theme).style).toEqual({
      paddingTop: "2px",
      paddingBottom: "2px",
    });
    expect(resolveSx({ px: 1 }, theme).style).toEqual({
      paddingLeft: "8px",
      paddingRight: "8px",
    });
  });

  it("treats gap as a spacing unit too, but leaves a string alone", () => {
    expect(resolveSx({ gap: 1 }, theme).style).toEqual({ gap: "8px" });
    expect(resolveSx({ gap: "0.4rem" }, theme).style).toEqual({ gap: "0.4rem" });
  });
});

describe("colour props resolve palette paths", () => {
  it("walks a dotted path into the active palette", () => {
    expect(resolveSx({ color: "text.secondary" }, theme).style).toEqual({ color: "#6B6A74" });
    expect(resolveSx({ bgcolor: "primary.main" }, theme).style).toEqual({
      backgroundColor: "#FF99C8",
    });
  });

  it("resolves single-segment keys like divider", () => {
    expect(resolveColor("divider", theme)).toBe("#EBEBF3");
  });

  it("leaves a real CSS colour untouched", () => {
    for (const value of ["#FF99C8", "rgba(0, 0, 0, 0.5)", "currentColor", "transparent"]) {
      expect(resolveColor(value, theme)).toBe(value);
    }
  });

  it("returns an unresolvable path unchanged rather than undefined", () => {
    // An invalid CSS value is visible; `undefined` silently drops the rule.
    expect(resolveColor("text.nonexistent", theme)).toBe("text.nonexistent");
  });
});

describe("border shorthand and borderColor are folded into one declaration", () => {
  it("puts the colour into the shorthand and drops the longhand", () => {
    // Exactly WidgetShell's footer: `{ borderTop: "1px solid", borderColor: "divider" }`.
    expect(resolveSx({ borderTop: "1px solid", borderColor: "divider" }, theme).style).toEqual({
      borderTop: "1px solid #EBEBF3",
    });
  });

  it("leaves borderColor alone when the shorthand already names a colour", () => {
    const style = resolveSx({ border: "1px solid red", borderColor: "divider" }, theme).style;
    expect(style).toEqual({ border: "1px solid red", borderColor: "#EBEBF3" });
  });
});

describe("nested selectors become a real CSS rule", () => {
  it("emits one class and one stylesheet for &:hover", () => {
    const { className, style } = resolveSx(
      { color: "text.secondary", "&:hover": { color: "text.primary" } },
      theme,
    );
    expect(style).toEqual({ color: "#6B6A74" });
    expect(className).toBeTruthy();

    const sheet = document.querySelector("style[data-toorow-sx]");
    expect(sheet?.textContent).toContain(`.${className}:hover`);
    expect(sheet?.textContent).toContain("color: #111111");
  });

  it("splits a comma-separated selector group so each part gets the class", () => {
    const { className } = resolveSx(
      { "&:hover, &:focus-visible": { color: "text.primary" } },
      theme,
    );
    const css = document.querySelector("style[data-toorow-sx]")?.textContent ?? "";
    expect(css).toContain(`.${className}:hover`);
    expect(css).toContain(`.${className}:focus-visible`);
  });

  it("reuses one class for an identical rule instead of growing the sheet", () => {
    const a = resolveSx({ "&:hover": { color: "text.primary" } }, theme).className;
    const b = resolveSx({ "&:hover": { color: "text.primary" } }, theme).className;
    expect(a).toBe(b);
  });
});

describe("everything else passes through", () => {
  it("keeps plain CSS properties as written", () => {
    expect(
      resolveSx({ display: "flex", minWidth: 0, flex: "1 1 120px", fontSize: "0.7rem" }, theme)
        .style,
    ).toEqual({ display: "flex", minWidth: 0, flex: "1 1 120px", fontSize: "0.7rem" });
  });

  it("drops undefined and null so a conditional style is inert, not invalid", () => {
    expect(resolveSx({ color: undefined, mb: null, display: "flex" }, theme).style).toEqual({
      display: "flex",
    });
  });

  it("reads a numeric borderRadius in shape units", () => {
    expect(resolveSx({ borderRadius: 1 }, theme).style).toEqual({ borderRadius: "12px" });
  });
});
