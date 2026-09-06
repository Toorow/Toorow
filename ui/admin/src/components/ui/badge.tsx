/**
 * Badge — the mockup's chip, adapted from the shadcn component.
 *
 * `application-v3.css` lines 346-359:
 *
 *     .chip { display: inline-flex; align-items: center; gap: 7px;
 *             min-height: 28px; padding: 0 10px; border-radius: 999px;
 *             background: the neutral tint; font-size: 12px; font-weight: 600 }
 *
 * shadcn's variant axis is intent (`default`/`secondary`/`destructive`); ours
 * is `tone`, read from the single scale in `ui/tone.ts`, so a badge and a
 * `Status` saying the same thing cannot disagree about the colour. The
 * `outline` variant is kept because it is a distinct object — a badge with no
 * fill, for a label that must not read as a state.
 *
 * This replaces `dso-chip`, `imports-chip`, `state-chip` and `kind-chip`.
 */
import * as React from "react"
import { cva, type VariantProps } from "class-variance-authority"
import { Slot } from "radix-ui"

import { cn } from "@/lib/cn"
import { TONE_FILL, type Fill } from "@/ui/tone"

const badgeVariants = cva(
  "inline-flex min-h-7 w-fit shrink-0 items-center justify-center gap-1.5 overflow-hidden rounded-pill border border-transparent px-2.5 text-caption font-semibold whitespace-nowrap transition-colors focus-visible:outline-3 focus-visible:outline-offset-2 focus-visible:outline-focus [&>svg]:pointer-events-none [&>svg]:size-3",
  {
    variants: {
      tone: TONE_FILL as Record<Fill, string>,
      /** No fill: a label that names a thing rather than a state. */
      outline: {
        true: "border-divider-base bg-transparent text-text-secondary",
        false: "",
      },
    },
    defaultVariants: { tone: "neutral", outline: false },
  }
)

function Badge({
  className,
  tone = "neutral",
  outline = false,
  asChild = false,
  ...props
}: React.ComponentProps<"span"> &
  VariantProps<typeof badgeVariants> & { asChild?: boolean }) {
  const Comp = asChild ? Slot.Root : "span"

  return (
    <Comp
      data-slot="badge"
      data-tone={tone}
      className={cn(badgeVariants({ tone, outline }), className)}
      {...props}
    />
  )
}

export { Badge, badgeVariants }
