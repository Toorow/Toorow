/**
 * Input — adapted to the validated mockup.
 *
 * `key-datastreams.html` measures the search/filter field:
 *
 *     .search { height: 44px; padding: 0 14px; border: 1px solid var(--line);
 *               border-radius: 12px; background: #fff }
 *
 * 12px is the "fields and selectors" radius from
 * `design-direction-foundation.md`, and it is a full 44px tall — the shadcn
 * default of 36px belongs to a denser product than this one.
 *
 * The focus ring is the shared one: a 3px lavender outline offset by 2px, the
 * same rule the buttons and the select use, defined once per component and
 * never again in a screen stylesheet.
 */
import * as React from "react"

import { cn } from "@/lib/cn"

function Input({ className, type, ...props }: React.ComponentProps<"input">) {
  return (
    <input
      type={type}
      data-slot="input"
      className={cn(
        "h-field-height w-full min-w-0 rounded-lg border border-divider-base bg-surface-light px-3.5 text-ui text-text transition-colors outline-none",
        "selection:bg-primary selection:text-on-primary placeholder:text-text-secondary",
        "file:inline-flex file:h-8 file:border-0 file:bg-transparent file:text-ui file:font-label file:text-text",
        "focus-visible:outline-3 focus-visible:outline-offset-2 focus-visible:outline-focus",
        "disabled:cursor-not-allowed disabled:opacity-50",
        "aria-invalid:border-primary",
        className
      )}
      {...props}
    />
  )
}

/**
 * NativeSelect — the same field, for a real `<select>`.
 *
 * The registry's `Select` is Radix: a button and a portalled listbox. It is the
 * right control for a single governed choice, and the wrong one for a multiple
 * selection or for a list that must stay operable with `selectOptions` and the
 * platform's own keyboard behaviour. Without this, the Datastream setup wizard
 * hand-rolled `h-10 rounded-md border border-divider-base bg-surface-light px-3`
 * in ten places — a screen-local base control, which is exactly what the single
 * visual vocabulary exists to prevent.
 *
 * It carries the Input geometry and focus ring so a form does not read as two
 * different products depending on which field you look at. `multiple` grows it
 * instead of clipping.
 */
function NativeSelect({ className, multiple, ...props }: React.ComponentProps<"select">) {
  return (
    <select
      data-slot="native-select"
      multiple={multiple}
      className={cn(
        "w-full min-w-0 rounded-lg border border-divider-base bg-surface-light px-3.5 text-ui text-text transition-colors outline-none",
        multiple ? "min-h-24 py-2" : "h-field-height",
        "focus-visible:outline-3 focus-visible:outline-offset-2 focus-visible:outline-focus",
        "disabled:cursor-not-allowed disabled:opacity-50",
        "aria-invalid:border-primary",
        className
      )}
      {...props}
    />
  )
}

/** The same field, for copy that runs to several lines. */
function Textarea({ className, ...props }: React.ComponentProps<"textarea">) {
  return (
    <textarea
      data-slot="textarea"
      className={cn(
        "min-h-[88px] w-full min-w-0 rounded-lg border border-divider-base bg-surface-light px-3.5 py-2.5 text-ui leading-[var(--body-line-height)] text-text transition-colors outline-none",
        "placeholder:text-text-secondary",
        "focus-visible:outline-3 focus-visible:outline-offset-2 focus-visible:outline-focus",
        "disabled:cursor-not-allowed disabled:opacity-50",
        "aria-invalid:border-primary",
        className
      )}
      {...props}
    />
  )
}

export { Input, NativeSelect, Textarea }
