/**
 * Progress — two objects, one component, both measured.
 *
 * The mockups draw a bar twice, and they are not the same statement:
 *
 *     .health-bar       { height: 8px; border-radius: 8px }        application-v4.css l.129
 *     .health-bar i     { background: var(--success) }
 *                       -> a METER: how healthy, how covered, how fresh.
 *
 *     .history-progress { height: 6px; border-radius: 999px }      first-publication.html
 *     .history-progress i { background: var(--info) }
 *                       -> an OPERATION advancing: the historical backfill.
 *
 * `size="meter"` is the 8px one and defaults to `success`; `size="thin"` is
 * the 6px one and is what `PhaseTimeline` puts inside a phase. The distinction
 * is not cosmetic: the mockup deliberately makes a running backfill read as
 * smaller than a health reading, because its progress must never be mistaken
 * for the operation's own state.
 *
 * The gloss and the gradient are Jean's, 2026-07-29 — an amendment to both
 * mockups, which draw a flat fill. `accent` runs violet to rose; every other
 * tone travels inside its own hue; `neutral` is the grey. The fill carries the
 * inner halo so the bar reads as an object lying in a groove.
 *
 * The default is `success` rather than the registry's rose because
 * `design-direction-foundation.md` forbids exactly one combination — rose as a
 * healthy state — so the careless case has to be the safe one.
 */
"use client"

import * as React from "react"
import { cva, type VariantProps } from "class-variance-authority"
import { Progress as ProgressPrimitive } from "radix-ui"

import { cn } from "@/lib/cn"
import { FILL_GRADIENT, type Fill } from "@/ui/tone"

const trackVariants = cva(
  "relative w-full overflow-hidden bg-track-light shadow-track-inset",
  {
    variants: {
      size: {
        meter: "h-2 rounded-lg",
        thin: "h-1.5 rounded-pill",
      },
    },
    defaultVariants: { size: "meter" },
  }
)

function Progress({
  className,
  value,
  tone = "success",
  size,
  ...props
}: React.ComponentProps<typeof ProgressPrimitive.Root> &
  VariantProps<typeof trackVariants> & { tone?: Fill }) {
  return (
    <ProgressPrimitive.Root
      data-slot="progress"
      data-tone={tone}
      // THE VALUE GOES TO THE ROOT, and it used to go nowhere but the transform.
      //
      // `value` was destructured out of the props and only ever used to move
      // the indicator, so the primitive's root never received it: radix sets
      // `aria-valuenow`, `aria-valuetext` and `data-state` from the value it is
      // GIVEN, and with none it declares every bar indeterminate. Measured
      // 2026-08-06: five render sites, one of them `PhaseTimeline`, which is
      // mounted by the Runs tab and every screen that draws a phase — so every
      // progress bar of the product announced "in progress, amount unknown" to
      // a screen reader while showing an exact figure to everybody else. On a
      // bar whose whole content is its value (the days of a collection in
      // flight, the datastreams against a plan limit) that is the information
      // itself, not a decoration.
      value={value}
      className={cn(trackVariants({ size }), className)}
      {...props}
    >
      <ProgressPrimitive.Indicator
        data-slot="progress-indicator"
        className={cn(
          // Only `transform` is animated — motion-iconography.md l.78 forbids
          // animating width, height or shadow blur.
          "h-full w-full flex-1 rounded-[inherit] shadow-inner-halo transition-transform",
          FILL_GRADIENT[tone]
        )}
        style={{ transform: `translateX(-${100 - (value || 0)}%)` }}
      />
    </ProgressPrimitive.Root>
  )
}

export { Progress, trackVariants }
