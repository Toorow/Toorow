/**
 * Checkbox — 18px, filled with the accent when checked, bold white check.
 *
 * Jean, 2026-07-29, pointing at a filled mark with a heavy white tick: the
 * checked state should read as a solid object, not a tinted outline. So the
 * mark is bigger than the shadcn 16px, the tick is 3px stroke with round caps,
 * and the unchecked border is `border-control` — at the divider's 1.19:1 it
 * was not visibly a control at all, which was the same complaint he made about
 * the buttons.
 *
 * **It stays a rounded square, and the radio stays a circle.** He also asked
 * for the radio to be recognisable as a radio; giving both the same silhouette
 * would answer one request by breaking the other. Shape is the only thing that
 * separates "choose any" from "choose one" once colour is gone.
 */
import * as React from "react"
import { CheckIcon, MinusIcon } from "lucide-react"
import { Checkbox as CheckboxPrimitive } from "radix-ui"

import { cn } from "@/lib/cn"

function Checkbox({
  className,
  ...props
}: React.ComponentProps<typeof CheckboxPrimitive.Root>) {
  return (
    <CheckboxPrimitive.Root
      data-slot="checkbox"
      className={cn(
        "peer grid size-[18px] shrink-0 place-items-center rounded-sm border-2 border-border-control bg-surface-light transition-colors outline-none",
        "focus-visible:outline-3 focus-visible:outline-offset-2 focus-visible:outline-focus",
        "disabled:cursor-not-allowed disabled:opacity-50",
        "aria-invalid:border-primary",
        "data-[state=checked]:border-primary data-[state=checked]:bg-primary data-[state=checked]:text-on-primary",
        "data-[state=indeterminate]:border-primary data-[state=indeterminate]:bg-primary data-[state=indeterminate]:text-on-primary",
        className
      )}
      {...props}
    >
      <CheckboxPrimitive.Indicator
        data-slot="checkbox-indicator"
        className="grid place-content-center text-current"
      >
        {props.checked === "indeterminate" ? (
          <MinusIcon className="size-3 [stroke-width:3.5]" />
        ) : (
          <CheckIcon className="size-3.5 [stroke-width:3.5]" />
        )}
      </CheckboxPrimitive.Indicator>
    </CheckboxPrimitive.Root>
  )
}

export { Checkbox }
