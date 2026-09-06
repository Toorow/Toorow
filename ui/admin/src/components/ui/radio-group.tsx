/**
 * RadioGroup — the last component the inventory listed as missing.
 *
 * No mockup draws a bare radio, so its geometry comes from the rules that do
 * apply: it matches the checkbox at 18px so a form of mixed controls lines up,
 * and the fill is the rose accent, like every other selected state.
 *
 * Two things Jean called out on 2026-07-29 — *"we cannot see it is a radio"*:
 * the unchecked border was the divider at 1.19:1 on white, invisible, and is
 * now `border-control`; and the checked state is a **ring**, not a filled
 * disc — the accent border with a white gap and an accent centre, which is the
 * silhouette that says "one of these" from across the screen. A radio that
 * fills solid is indistinguishable from a checkbox at a glance.
 *
 * It replaced `ps-radio`, the local re-implementation that lived in the
 * Project Settings stylesheet until Story 46.3 deleted that file.
 */
"use client"

import * as React from "react"
import { RadioGroup as RadioGroupPrimitive } from "radix-ui"

import { cn } from "@/lib/cn"

function RadioGroup({
  className,
  ...props
}: React.ComponentProps<typeof RadioGroupPrimitive.Root>) {
  return (
    <RadioGroupPrimitive.Root
      data-slot="radio-group"
      className={cn("grid gap-3", className)}
      {...props}
    />
  )
}

function RadioGroupItem({
  className,
  ...props
}: React.ComponentProps<typeof RadioGroupPrimitive.Item>) {
  return (
    <RadioGroupPrimitive.Item
      data-slot="radio-group-item"
      className={cn(
        "aspect-square size-[18px] shrink-0 rounded-pill border-2 border-border-control bg-surface-light transition-colors outline-none focus-visible:outline-3 focus-visible:outline-offset-2 focus-visible:outline-focus disabled:cursor-not-allowed disabled:opacity-50 aria-invalid:border-primary data-[state=checked]:border-primary",
        className
      )}
      {...props}
    >
      <RadioGroupPrimitive.Indicator
        data-slot="radio-group-indicator"
        className="relative flex size-full items-center justify-center"
      >
        <span aria-hidden className="size-2.5 rounded-pill bg-primary" />
      </RadioGroupPrimitive.Indicator>
    </RadioGroupPrimitive.Item>
  )
}

export { RadioGroup, RadioGroupItem }
