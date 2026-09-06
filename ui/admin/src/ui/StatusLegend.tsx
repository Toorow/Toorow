/**
 * StatusLegend — what the colours on THIS screen mean, said once.
 *
 * `console-presentation.md` §3: « `StatusLegend` lists the tones a screen uses,
 * mark + label + one-line meaning, on the model of the six-status extraction
 * calendar of the datastream detail. Any screen that shows three tones or more
 * mounts it once, in its `PageHeader`. »
 *
 * THE MODEL IS `CoverageBars`' legend and the copy is deliberate: a swatch that
 * is a MINIATURE OF THE MARK, never a second drawing of it. That lesson was paid
 * for once already (`CoverageBars.tsx` l.510-518, Jean 2026-07-29: "not the same
 * color que la légende") — a legend whose colour is not the colour on the screen
 * has failed at the only thing it does. So the mark here is built from the same
 * two shape rules `Status` uses, out of the same `HALO` map:
 *
 *   warning   a diamond, not a circle — the one shape that differs, and the
 *             reason a warning survives greyscale and colour blindness
 *   neutral   an open dotted ring, never a filled dot
 *
 * THE SCREEN OWNS THE MEANINGS, NOT THIS FILE. A `warning` on the Governance
 * collections means "this object's lifecycle is held up"; on Test › Runs it
 * means "this run has an unresolved pin". One component with a built-in
 * dictionary would have to be wrong on one of them, so the caller passes the
 * sentence and this file passes it through. What this file owns is the mark, the
 * order and the shape — the half that must not differ between two screens.
 *
 * IT RENDERS NOTHING FOR FEWER THAN TWO TONES. A legend of one is a caption
 * pretending to be a key, and §3 asks for the component at three.
 */
import type { ReactNode } from "react";

import { cn } from "../lib/cn";
import { HALO, TONES, type Tone } from "./tone";

export type StatusLegendEntry = {
  /** The tone as it is drawn on this screen. */
  tone: Tone;
  /** The word the screen prints beside that mark — `Stale`, `Blocked`. */
  label: ReactNode;
  /** One line: what a reader learns from seeing that mark HERE. */
  meaning: ReactNode;
};

export type StatusLegendProps = {
  entries: readonly StatusLegendEntry[];
  /** Names the list for a screen reader. Defaults to the generic reading. */
  label?: string;
  className?: string;
};

/**
 * The mark, in miniature. Same rules as `Status`' mark (`ui/Data.tsx` l.86-105)
 * at the legend's size — 12px rather than 16, because it sits on a caption line
 * and a full-size dot beside 12px text reads as a bullet.
 */
function LegendMark({ tone }: { tone: Tone }) {
  return (
    <span
      aria-hidden
      className={cn(
        "relative size-3 shrink-0 rounded-pill border-[3px] bg-current",
        HALO[tone],
        tone === "warning" && "rotate-45 rounded-[3px]",
        tone === "neutral" && "border-2 border-dotted bg-transparent",
      )}
    />
  );
}

/** The order is the tone scale's, never the caller's: two screens listing the
 *  same three tones must list them the same way, or the legend teaches the
 *  screen instead of the product. */
function inScaleOrder(entries: readonly StatusLegendEntry[]): StatusLegendEntry[] {
  return [...entries].sort((a, b) => TONES.indexOf(a.tone) - TONES.indexOf(b.tone));
}

export function StatusLegend({ entries, label = "What these marks mean", className }: StatusLegendProps) {
  if (entries.length < 2) return null;
  return (
    <ul
      aria-label={label}
      data-testid="status-legend"
      className={cn("m-0 flex list-none flex-wrap gap-x-5 gap-y-1.5 p-0", className)}
    >
      {inScaleOrder(entries).map((entry) => (
        <li key={entry.tone} data-tone={entry.tone} className="flex items-center gap-2">
          <LegendMark tone={entry.tone} />
          <span className="text-caption text-text-secondary">
            <span className="font-semibold text-text">{entry.label}</span>
            {" — "}
            {entry.meaning}
          </span>
        </li>
      ))}
    </ul>
  );
}
