/**
 * Label — the control label, 13px/700, as every validated mockup draws it.
 *
 * Used standalone: next to a checkbox, a switch, a radio. Inside a form,
 * prefer `Field` from `src/ui/Form.tsx` — it renders the same label *and*
 * wires `id` / `aria-describedby` / `aria-invalid`, which a bare label does
 * not.
 */
import * as React from "react"
import { Label as LabelPrimitive } from "radix-ui"

import { cn } from "@/lib/cn"

function Label({
  className,
  ...props
}: React.ComponentProps<typeof LabelPrimitive.Root>) {
  return (
    <LabelPrimitive.Root
      data-slot="label"
      className={cn(
        "flex items-center gap-2 text-label font-label leading-none text-text select-none group-data-[disabled=true]:pointer-events-none group-data-[disabled=true]:opacity-50 peer-disabled:cursor-not-allowed peer-disabled:opacity-50",
        className
      )}
      {...props}
    />
  )
}

export { Label }
