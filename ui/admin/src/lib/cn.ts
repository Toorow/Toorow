import { clsx, type ClassValue } from "clsx";
import { extendTailwindMerge } from "tailwind-merge";

/**
 * Merge class names, last conflicting utility wins.
 *
 * The shadcn convention, and the reason every component takes a `className`: a
 * caller can override a single utility (`px-6`, `text-error`) without the
 * component growing a prop for it, and without a stylesheet. `twMerge` is what
 * makes that safe — plain concatenation would leave both `px-4` and `px-6` in
 * the list and let source order decide.
 *
 * It has to be told our scale. `tailwind-merge` ships knowing Tailwind's own
 * names (`rounded-md`, `text-sm`) and nothing else, so a utility built from a
 * token — `rounded-pill`, `text-ui`, `h-control-height` — was invisible to it
 * and simply never merged. That is not theoretical: the icon-only button
 * carried `rounded-pill` from the base and `rounded-control` from its size,
 * both survived, and the 10px square the mockup specifies rendered as a
 * circle. Every group below is a token namespace from `ui/tokens/tokens.json`;
 * adding a token means adding its name here, or the override silently stops
 * working.
 */
const SPACING = [
  "card-padding",
  "section-gap",
  "row-height",
  "row-height-rich",
  "head-height",
  "control-height",
  "control-height-small",
  "field-height",
  "tabs-height",
];

const TYPE_SCALE = ["h1", "h2", "h3", "body", "ui", "label", "caption"];

const twMerge = extendTailwindMerge({
  extend: {
    classGroups: {
      rounded: [{ rounded: ["small", "control", "menu", "large", "pill"] }],
      "font-size": [{ text: TYPE_SCALE }],
      "font-weight": [{ font: TYPE_SCALE }],
      "font-family": [{ font: ["primary", "display", "numeric", "mono"] }],
      shadow: [{ shadow: ["card-light", "card-dark", "overlay"] }],
      h: [{ h: SPACING }],
      w: [{ w: SPACING }],
      size: [{ size: SPACING }],
      "min-h": [{ "min-h": SPACING }],
      p: [{ p: SPACING }],
      px: [{ px: SPACING }],
      py: [{ py: SPACING }],
      gap: [{ gap: SPACING }],
    },
  },
});

export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
