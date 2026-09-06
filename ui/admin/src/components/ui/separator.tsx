/**
 * Separator — the hairline, and the reason it is a component rather than a class.
 *
 * Eight screens still reached for MUI's `Divider` for this, which is one import
 * of a whole design system to draw a one-pixel line. A bare `<hr>` would have
 * done the drawing, and that is precisely what makes a component worth it here:
 * a rule between two blocks is DECORATIVE, and an `<hr>` is announced by a
 * screen reader as a thematic break. Radix's primitive takes `decorative` and
 * emits `role="none"` for it, so the common case stops being narrated.
 *
 * The colour is `--color-divider-base`, the same hairline every table row and
 * panel edge already uses, so a rule inside a card and the card's own border are
 * one decision rather than two that drift.
 *
 * No `variant`, no `light`/`dark`, no inset: the mockups draw exactly one
 * hairline. An option nobody asked for is an invitation to make a second one.
 */
"use client"

import * as React from "react"
import { Separator as SeparatorPrimitive } from "radix-ui"

import { cn } from "@/lib/cn"

function Separator({
  className,
  orientation = "horizontal",
  // Decorative BY DEFAULT, unlike Radix, and that is a deliberate divergence:
  // in this console a rule almost always sits between two blocks that are
  // already labelled. A caller that means a real thematic break says so.
  decorative = true,
  ...props
}: React.ComponentProps<typeof SeparatorPrimitive.Root>) {
  return (
    <SeparatorPrimitive.Root
      data-slot="separator"
      decorative={decorative}
      orientation={orientation}
      className={cn(
        "shrink-0 bg-divider-base",
        orientation === "horizontal" ? "h-px w-full" : "h-full w-px",
        className
      )}
      {...props}
    />
  )
}

export { Separator }
