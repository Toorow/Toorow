/**
 * THE legend of coloured variations — one component, one placement.
 *
 * WHY IT EXISTS, measured on 2026-09-05. The « queries on the move (±pos.) »
 * bars of `card-keywords` printed one value in RED directly above another in
 * GREEN, and nothing on the screen said why: the metric is average position,
 * where rising is a loss. A colour that judges without naming the rule it judges
 * by is not information, it is a guess — and the reader who guesses is wrong
 * half the time.
 *
 * ONE, NEVER TWO. The funnel had earned a legend of its own under arbitrage 5 of
 * story 76-8; promoting it here rather than writing a second one is the rule the
 * formatter already lives under (« a second module answering the same question
 * is the defect, whatever it answers », `console-presentation.md` §2).
 * `VariationLegendIsTheOnlyOne.test.tsx` refuses the second.
 *
 * ONE PLACEMENT, AND IT IS THE CARD FOOTER. Arbitrage 5 says the legend belongs
 * to the card footer, not under each block: five blocks would otherwise repeat
 * five near-identical grey sentences down one card. `CardShell` renders it once,
 * beside the source line; a widget, which has no card shell, renders it once in
 * its own footer.
 *
 * IT STATES WHAT `verdictTone` APPLIES, and nothing else. The wording is derived
 * from the same `VerdictDirection` the colour is derived from, so the sentence
 * cannot drift from the paint. It guesses nothing about the data.
 *
 * The copy is ENGLISH: everything a card shell or a widget writes itself is
 * served copy (`console-presentation.md`, amendment of 2026-09-05 evening).
 */

import { Typography } from "@toorow/shell";

import type { VerdictDirection } from "./verdictTone";

/** One convention in force on the card, and what it governs. */
export interface VariationConvention {
  direction: VerdictDirection;
  /** What this convention applies to, in the reader's words. */
  subject?: string;
}

export interface VariationLegendProps {
  /**
   * The conventions in force. Several are listed side by side rather than
   * collapsed: a card mixing clicks (`up_good`) with average position
   * (`down_good`) has two, and asserting either one alone is false for the other
   * half of the figures.
   */
  conventions?: VariationConvention[];
  /** A single convention, when the surface has only one. */
  direction?: VerdictDirection;
  /** What the variation is compared against, in the reader's words. */
  comparison?: string;
  /** Does the surface draw ▲ / ▼ marks? */
  arrows?: boolean;
  /** A precision the surface adds, appended to the sentence. */
  note?: string;
  /** Test hook; `variation-legend` by default. */
  testId?: string;
}

/** Which way this convention calls desirable. */
function favourableWay(direction: VerdictDirection): string | null {
  if (direction === "neutral") return null;
  return direction === "up_good" ? "rising" : "falling";
}

/** The sentence the colours are painted from. */
function toneSentence(conventions: VariationConvention[]): string | null {
  const judged = conventions.filter((c) => c.direction !== "neutral");
  if (judged.length === 0) {
    return conventions.length > 0 ? "Colour states no verdict here." : null;
  }
  const head = "Green: favourable · red: unfavourable";
  const ways = new Set(judged.map((c) => favourableWay(c.direction)));
  if (ways.size === 1) {
    return `${head} — for this measure, ${[...ways][0]} is favourable.`;
  }
  // Two conventions in force: name each, because one sentence for both would be
  // false for half the figures on the card.
  const named = judged
    .filter((c) => c.subject)
    .map((c) => `${favourableWay(c.direction)} is favourable for ${c.subject}`);
  return named.length === judged.filter((c) => c.subject).length && named.length > 0
    ? `${head} — ${named.join("; ")}.`
    : `${head} — which way is favourable depends on the measure.`;
}

export default function VariationLegend({
  conventions,
  direction,
  comparison,
  arrows = true,
  note,
  testId = "variation-legend",
}: VariationLegendProps) {
  const inForce: VariationConvention[] =
    conventions ?? (direction ? [{ direction }] : []);
  const parts: string[] = [];

  if (arrows) {
    parts.push(
      comparison
        ? `▲ up · ▼ down — change vs ${comparison}.`
        : "▲ up · ▼ down.",
    );
  } else if (comparison) {
    parts.push(`Change vs ${comparison}.`);
  }

  const tone = toneSentence(inForce);
  if (tone) parts.push(tone);
  if (note) parts.push(note);

  // An empty legend would be a line that teaches nothing; it is not rendered.
  if (parts.length === 0) return null;

  const stated =
    inForce.length === 0
      ? "unstated"
      : inForce.length === 1
        ? inForce[0].direction
        : "mixed";

  return (
    <Typography
      variant="caption"
      color="text.disabled"
      sx={{ display: "block", lineHeight: 1.4 }}
      data-testid={testId}
      data-direction={stated}
    >
      {parts.join(" ")}
    </Typography>
  );
}
