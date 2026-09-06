/**
 * Spinner — indeterminate progress, once.
 *
 * `Progress` answers *how far*. This answers *something is happening and I
 * cannot say how far* — and the console had no way to say it, so fourteen files
 * imported MUI's `CircularProgress` and one wrote
 * `<span role="progressbar" className="signal running" />`
 * (`rapports/ReportChainPanel.tsx:243`, retiré le 2026-08-04 avec le reste du
 * sous-arbre mort — le défaut est gardé ici parce qu'il se refait), which
 * announces a determinate bar with
 * no value to screen readers. The distinction is not cosmetic: a `progressbar`
 * without `aria-valuenow` is a promise of a number that never arrives.
 *
 * So this is `role="status"`, not `role="progressbar"`, and it carries a written
 * label. `motion-iconography.md` § Signal requires state to combine colour, a
 * halo, a shape and a **written English label**; a bare rotating ring satisfies
 * none of that on its own.
 *
 * MOTION. `motion-iconography.md:78` forbids animating width, height and shadow
 * blur — `transform` only, which is what a rotation is. Under
 * `prefers-reduced-motion` the ring stops and the tone plus the label still
 * carry the meaning, exactly as `Status active` does.
 *
 * COLOUR. No literal: the ring is `currentColor` through the tone scale, so a
 * spinner and the `Status` next to it cannot disagree about what `info` looks
 * like. Default is `info` — work in flight — never `accent`: rose means *the
 * recommended direction*, and a wait is not a direction.
 */
"use client"

import * as React from "react"
import { cva, type VariantProps } from "class-variance-authority"

import { cn } from "@/lib/cn"
import { TONE_TEXT, type Fill } from "@/ui/tone"

const spinnerVariants = cva(
  // `border-current` + one transparent edge is the whole ring: no SVG, no
  // second colour, nothing to keep in step with the palette.
  "inline-block shrink-0 animate-spin rounded-pill border-solid border-current border-t-transparent motion-reduce:animate-none",
  {
    variants: {
      size: {
        /** Inside a button or a table cell, on the text baseline. */
        inline: "size-4 border-2",
        /** Standalone next to a sentence — the `Status` block mark size. */
        default: "size-[18px] border-2",
        /** A panel waiting for its first payload. */
        page: "size-8 border-[3px]",
      },
    },
    defaultVariants: { size: "default" },
  }
)

function Spinner({
  className,
  tone = "info",
  size,
  label = "Loading",
  showLabel = false,
  ...props
}: React.ComponentProps<"span"> &
  VariantProps<typeof spinnerVariants> & {
    tone?: Fill
    /**
     * What is being waited for. Always announced; rendered visibly only when
     * `showLabel`. "Loading" is a floor, not a target — say what is loading.
     */
    label?: string
    showLabel?: boolean
  }) {
  return (
    <span
      // `status` rather than `progressbar`: there is no value to report, and a
      // progressbar without one reads as broken to a screen reader.
      role="status"
      data-slot="spinner"
      data-tone={tone}
      className={cn("inline-flex items-center gap-2", TONE_TEXT[tone], className)}
      {...props}
    >
      <span aria-hidden className={cn(spinnerVariants({ size }))} />
      <span className={showLabel ? "text-ui text-text-secondary" : "sr-only"}>
        {label}
      </span>
    </span>
  )
}

export { Spinner, spinnerVariants }
