/**
 * `sx` — the subset of MUI's system prop the widget platform actually uses.
 *
 * MEASURED BEFORE WRITTEN, on 2026-08-05, over the 232 `sx=` sites in
 * `ui/shell`, `ui/cards/*` and `ui/widgets/*`: nine spacing shorthands, `gap`,
 * `color`, `bgcolor`, `borderColor`, `borderRadius`, `boxShadow`, plain CSS
 * pass-through, and SEVEN nested selectors (all `&:hover` / `&:focus-visible`).
 * No responsive array, no breakpoint object, no `theme => ...` callback.
 *
 * So this is not a reimplementation of `sx`; it is the translation of what the
 * repository writes. Anything outside that set is passed straight through to the
 * inline style object, where an unknown property is inert rather than wrong.
 *
 * Nested selectors cannot live in an inline style, so they are emitted as a real
 * CSS rule under a content-hashed class name, inserted once per distinct rule.
 * That keeps `:hover` working on the seven sites without turning each of them
 * into a hand-written mouse-enter handler.
 */

import type { CSSProperties } from "react";
import type { WidgetTheme } from "./theme";

export type SxValue = string | number | undefined | null | SxObject;
export interface SxObject {
  [key: string]: SxValue;
}
export type Sx = SxObject | undefined | null;

/** MUI's default spacing unit. `mb: 1` is 8px, `py: 1.5` is 12px. */
const SPACING_UNIT = 8;

const SPACING_PROPS: Record<string, string[]> = {
  m: ["margin"],
  mt: ["marginTop"],
  mb: ["marginBottom"],
  ml: ["marginLeft"],
  mr: ["marginRight"],
  mx: ["marginLeft", "marginRight"],
  my: ["marginTop", "marginBottom"],
  p: ["padding"],
  pt: ["paddingTop"],
  pb: ["paddingBottom"],
  pl: ["paddingLeft"],
  pr: ["paddingRight"],
  px: ["paddingLeft", "paddingRight"],
  py: ["paddingTop", "paddingBottom"],
};

/** Props whose value may be a theme palette path rather than a CSS colour. */
const COLOR_PROPS = new Set([
  "color",
  "bgcolor",
  "backgroundColor",
  "background",
  "borderColor",
  "borderTopColor",
  "borderBottomColor",
  "borderLeftColor",
  "borderRightColor",
  "outlineColor",
  "fill",
  "stroke",
  "caretColor",
  "columnRuleColor",
  "textDecorationColor",
]);

/** `gap`, `rowGap`, `columnGap` take spacing units in `sx`, like `m` and `p`. */
const GAP_PROPS = new Set(["gap", "rowGap", "columnGap"]);

const CSS_PROP_ALIAS: Record<string, string> = {
  bgcolor: "backgroundColor",
};

/**
 * `"text.secondary"` -> the resolved colour; `"#fff"` / `"rgba(...)"` /
 * `"currentColor"` -> itself. A dotted path that does not resolve is returned
 * unchanged, so a typo shows up as an invalid CSS value rather than `undefined`.
 */
export function resolveColor(value: string, theme: WidgetTheme): string {
  if (!value || value.startsWith("#") || value.includes("(") || !value.includes(".")) {
    // Single-segment palette keys still resolve: `divider`, `background`.
    const direct = (theme.palette as unknown as Record<string, unknown>)[value];
    if (typeof direct === "string") return direct;
    return value;
  }
  let node: unknown = theme.palette;
  for (const segment of value.split(".")) {
    if (node == null || typeof node !== "object") return value;
    node = (node as Record<string, unknown>)[segment];
  }
  return typeof node === "string" ? node : value;
}

function toSpacing(value: SxValue): string | undefined {
  if (typeof value === "number") return `${value * SPACING_UNIT}px`;
  if (typeof value === "string") return value;
  return undefined;
}

// ---------------------------------------------------------------------------
// Nested-selector rules -> one injected stylesheet
// ---------------------------------------------------------------------------

const ruleClassNames = new Map<string, string>();
let styleElement: HTMLStyleElement | null = null;
let ruleCounter = 0;

function camelToKebab(prop: string): string {
  return prop.replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`);
}

function declarationsOf(style: CSSProperties): string {
  return Object.entries(style)
    .filter(([, v]) => v !== undefined && v !== null && v !== "")
    .map(([k, v]) => `${camelToKebab(k)}: ${typeof v === "number" && !UNITLESS.has(k) ? `${v}px` : v}`)
    .join("; ");
}

/** React adds `px` to a bare number except for these; the rule writer must match. */
const UNITLESS = new Set([
  "opacity", "zIndex", "fontWeight", "lineHeight", "flex", "flexGrow", "flexShrink",
  "order", "gridRow", "gridColumn", "columnCount", "fillOpacity", "strokeOpacity", "zoom",
]);

function classNameForRules(rules: Array<[string, CSSProperties]>): string | undefined {
  const body = rules
    .map(([selector, style]) => `${selector}{${declarationsOf(style)}}`)
    .filter((chunk) => !chunk.endsWith("{}"))
    .join("");
  if (!body) return undefined;

  const existing = ruleClassNames.get(body);
  if (existing) return existing;
  if (typeof document === "undefined") return undefined;

  ruleCounter += 1;
  const className = `tw-sx-${ruleCounter}`;
  ruleClassNames.set(body, className);

  if (!styleElement) {
    styleElement = document.createElement("style");
    styleElement.setAttribute("data-toorow-sx", "");
    document.head.appendChild(styleElement);
  }
  styleElement.appendChild(
    document.createTextNode(body.replace(/&/g, `.${className}`)),
  );
  return className;
}

/** Test seam: drop the injected sheet so a suite starts from nothing. */
export function __resetSxStylesForTests(): void {
  ruleClassNames.clear();
  ruleCounter = 0;
  styleElement?.remove();
  styleElement = null;
}

// ---------------------------------------------------------------------------

export interface ResolvedSx {
  style: CSSProperties;
  className?: string;
}

/** Translate one `sx` object into an inline style plus, if needed, a class. */
export function resolveSx(sx: Sx, theme: WidgetTheme): ResolvedSx {
  if (!sx) return { style: {} };
  const style: Record<string, unknown> = {};
  const nested: Array<[string, CSSProperties]> = [];

  for (const [key, value] of Object.entries(sx)) {
    if (value === undefined || value === null) continue;

    if (key.startsWith("&") || key.startsWith(":") || key.startsWith("@")) {
      // A selector group. Each comma-separated part becomes its own rule so
      // `"&:hover, &:focus-visible"` survives the `&` -> class substitution.
      const inner = resolveSx(value as SxObject, theme).style;
      for (const selector of key.split(",")) {
        nested.push([selector.trim(), inner]);
      }
      continue;
    }

    if (key in SPACING_PROPS) {
      const resolved = toSpacing(value);
      if (resolved !== undefined) for (const prop of SPACING_PROPS[key]) style[prop] = resolved;
      continue;
    }

    if (GAP_PROPS.has(key)) {
      const resolved = toSpacing(value);
      if (resolved !== undefined) style[key] = resolved;
      continue;
    }

    const cssProp = CSS_PROP_ALIAS[key] ?? key;
    if (COLOR_PROPS.has(key) && typeof value === "string") {
      style[cssProp] = resolveColor(value, theme);
      continue;
    }

    if (key === "borderRadius" && typeof value === "number") {
      // `sx` reads borderRadius in shape units; the token shape is 12px.
      style[cssProp] = `${value * theme.shape.borderRadius}px`;
      continue;
    }

    style[cssProp] = value;
  }

  mergeBorderColorIntoShorthand(style);
  return { style: style as CSSProperties, className: classNameForRules(nested) };
}

const BORDER_SHORTHANDS = ["border", "borderTop", "borderRight", "borderBottom", "borderLeft"];

/**
 * `{ borderTop: "1px solid", borderColor: "divider" }` is idiomatic `sx` and
 * emotion resolved it into one declaration. React does not: it sets both on the
 * CSSOM, warns about mixing shorthand with longhand, and the shorthand's implicit
 * `currentColor` can win. Fold the colour into the shorthand instead — same
 * output, no warning, and the divider colour actually lands.
 */
function mergeBorderColorIntoShorthand(style: Record<string, unknown>): void {
  const color = style.borderColor;
  if (typeof color !== "string") return;
  const targets = BORDER_SHORTHANDS.filter((prop) => typeof style[prop] === "string");
  if (!targets.length) return;
  let folded = false;
  for (const prop of targets) {
    const value = style[prop] as string;
    // Only a colourless shorthand ("1px solid") is missing what borderColor has.
    if (/^\s*[\d.]+\S*\s+\w+\s*$/.test(value)) {
      style[prop] = `${value.trim()} ${color}`;
      folded = true;
    }
  }
  if (folded) delete style.borderColor;
}
