/**
 * Switch — adapted to the validated mockups.
 *
 *     .switch       { width: 34px; height: 19px; border-radius: 999px; padding: 3px }
 *     .switch-large { width: 38px; height: 22px; border-radius: 999px; padding: 3px }
 *
 * `default` is the 22px mock, `sm` the 19px one. The thumb is the track height
 * minus the 3px padding on each side.
 *
 * **The on colour is a choice** (Jean, 2026-07-29). `accent` — the rose, and
 * the default — is a control you operate: turning a module on, opting into a
 * split. The tones are for a switch whose on position also *reports* something,
 * which is how the mocks draw theirs: `success` on an enabled, healthy module.
 * Both are real, so neither is baked in.
 *
 * The off track is the divider in every case: an off switch is not a state, it
 * is an absence.
 */
"use client"

import * as React from "react"
import { Switch as SwitchPrimitive } from "radix-ui"

import { cn } from "@/lib/cn"
import { FILL_BG_CHECKED, type Fill } from "@/ui/tone"

function Switch({
  className,
  size = "default",
  tone = "accent",
  ...props
}: React.ComponentProps<typeof SwitchPrimitive.Root> & {
  size?: "sm" | "default"
  tone?: Fill
}) {
  return (
    <SwitchPrimitive.Root
      data-slot="switch"
      data-size={size}
      data-tone={tone}
      className={cn(
        "peer group/switch inline-flex shrink-0 items-center rounded-pill border border-transparent p-[3px] transition-colors outline-none focus-visible:outline-3 focus-visible:outline-offset-2 focus-visible:outline-focus disabled:cursor-not-allowed disabled:opacity-50 data-[size=default]:h-[22px] data-[size=default]:w-[38px] data-[size=sm]:h-[19px] data-[size=sm]:w-[34px] data-[state=unchecked]:bg-divider-base",
        FILL_BG_CHECKED[tone],
        className
      )}
      {...props}
    >
      <SwitchPrimitive.Thumb
        data-slot="switch-thumb"
        className="pointer-events-none block rounded-pill bg-surface-light shadow-card-light ring-0 transition-transform group-data-[size=default]/switch:size-4 group-data-[size=sm]/switch:size-[13px] data-[state=checked]:translate-x-full data-[state=unchecked]:translate-x-0"
      />
    </SwitchPrimitive.Root>
  )
}

export { Switch }
