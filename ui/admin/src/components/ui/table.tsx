/**
 * Table — adapted to the validated v3 mockup.
 *
 * `application-v3.css` lines 361-384:
 *
 *     .table    { width: 100%; border-collapse: collapse }
 *     .table th { height: 44px; padding: 0 18px; background: the page tint;
 *                 border-bottom: 1px solid var(--line); color: var(--muted);
 *                 font: 700 12px; text-transform: uppercase; letter-spacing: .035em }
 *     .table td { height: 64px; padding: 10px 18px;
 *                 border-bottom: 1px solid var(--line); font-size: 14px }
 *     .table tr:last-child td { border-bottom: 0 }
 *
 * The 64px row is not generosity: the readability floor in
 * `design-direction-foundation.md` sets 60px minimum wherever a cell carries
 * primary and secondary information together, which is most tables in the
 * console. The 52px `--spacing-row-height` token stays the floor for a table
 * whose cells hold one value each — `density="compact"` selects it.
 *
 * The header tint in the mock has no token: it is the page background seen
 * against white. Written as `bg-background-light` rather than adding a colour
 * name nothing else would reuse.
 */
import * as React from "react"
import { cva, type VariantProps } from "class-variance-authority"

import { cn } from "@/lib/cn"

function Table({ className, ...props }: React.ComponentProps<"table">) {
  return (
    <div
      data-slot="table-container"
      className="relative w-full overflow-x-auto"
    >
      <table
        data-slot="table"
        className={cn("w-full border-collapse caption-bottom text-ui", className)}
        {...props}
      />
    </div>
  )
}

function TableHeader({ className, ...props }: React.ComponentProps<"thead">) {
  return (
    <thead
      data-slot="table-header"
      className={cn("bg-background-light", className)}
      {...props}
    />
  )
}

function TableBody({ className, ...props }: React.ComponentProps<"tbody">) {
  return (
    <tbody
      data-slot="table-body"
      className={cn("[&_tr:last-child_td]:border-b-0", className)}
      {...props}
    />
  )
}

function TableFooter({ className, ...props }: React.ComponentProps<"tfoot">) {
  return (
    <tfoot
      data-slot="table-footer"
      className={cn(
        "border-t border-divider-base bg-background-light font-medium",
        className
      )}
      {...props}
    />
  )
}

const tableRowVariants = cva(
  "transition-colors data-[state=selected]:bg-background-light",
  {
    variants: {
      /** `default` is the 64px mockup row; `compact` is the 52px token floor. */
      density: {
        default: "[&>td]:h-row-height-rich",
        compact: "[&>td]:h-row-height",
      },
      /** Only a row that leads somewhere lights up under the pointer. */
      interactive: {
        true: "hover:bg-background-light",
        false: "",
      },
    },
    defaultVariants: { density: "default", interactive: false },
  }
)

function TableRow({
  className,
  density,
  interactive,
  ...props
}: React.ComponentProps<"tr"> & VariantProps<typeof tableRowVariants>) {
  return (
    <tr
      data-slot="table-row"
      className={cn(tableRowVariants({ density, interactive }), className)}
      {...props}
    />
  )
}

/**
 * A column of figures: right-aligned, lining tabular digits, the numeric face.
 *
 * Stated once. It had been written out by hand in three screens as
 * `font-numeric [font-variant-numeric:lining-nums_tabular-nums] text-right`,
 * which is a rule living in the screens rather than in the table — the same
 * shape under three copies, one keystroke from drifting apart.
 */
const NUMERIC = "text-right font-numeric [font-variant-numeric:lining-nums_tabular-nums]"

function TableHead({ className, numeric, ...props }: React.ComponentProps<"th"> & { numeric?: boolean }) {
  return (
    <th
      data-slot="table-head"
      className={cn(
        "h-head-height border-b border-divider-base px-4.5 text-left align-middle text-caption font-label tracking-[0.035em] whitespace-nowrap text-text-secondary uppercase [&:has([role=checkbox])]:pr-0",
        numeric && "text-right",
        className
      )}
      {...props}
    />
  )
}

function TableCell({ className, numeric, ...props }: React.ComponentProps<"td"> & { numeric?: boolean }) {
  return (
    <td
      data-slot="table-cell"
      className={cn(
        "border-b border-divider-base px-4.5 py-2.5 align-middle text-ui [&:has([role=checkbox])]:pr-0",
        numeric && NUMERIC,
        className
      )}
      {...props}
    />
  )
}

function TableCaption({
  className,
  ...props
}: React.ComponentProps<"caption">) {
  return (
    <caption
      data-slot="table-caption"
      className={cn("mt-4 text-caption text-text-secondary", className)}
      {...props}
    />
  )
}

export {
  Table,
  TableHeader,
  TableBody,
  TableFooter,
  TableHead,
  TableRow,
  TableCell,
  TableCaption,
  tableRowVariants,
}
