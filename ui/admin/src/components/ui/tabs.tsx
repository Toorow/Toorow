/**
 * Tabs — adapted to the validated v3 mockup.
 *
 * The console's tabs are an underline band, never the segmented pill shadcn
 * ships. `application-v3.css` l. 403-431:
 *
 *     .local-tabs { height: 52px; gap: 28px; padding: 0 40px;
 *                   background: var(--surface); border-bottom: 1px solid var(--line);
 *                   align-items: flex-end }
 *     .local-tab  { height: 52px; font: 600 14px; color: var(--muted) }
 *     .local-tab.active { color: var(--ink) }
 *     .local-tab.active::after { left:0; right:0; bottom:-1px; height:3px;
 *                               border-radius: 3px 3px 0 0; background: var(--rose) }
 *
 * So `line` is the default here, not `default` — the segmented variant is kept
 * only for the one place a mockup uses it (a two-state filter inside a panel),
 * and it inherits the same 14px label so the two never disagree.
 *
 * The active mark is 3px of rose sitting *on* the divider (`-bottom-px`), which
 * is what makes it read as a tab rather than an underlined word.
 */
import * as React from "react"
import { cva, type VariantProps } from "class-variance-authority"
import { Tabs as TabsPrimitive } from "radix-ui"

import { cn } from "@/lib/cn"

function Tabs({
  className,
  orientation = "horizontal",
  ...props
}: React.ComponentProps<typeof TabsPrimitive.Root>) {
  return (
    <TabsPrimitive.Root
      data-slot="tabs"
      data-orientation={orientation}
      orientation={orientation}
      className={cn(
        "group/tabs flex data-[orientation=horizontal]:flex-col data-[orientation=vertical]:gap-6",
        className
      )}
      {...props}
    />
  )
}

const tabsListVariants = cva(
  "group/tabs-list flex items-stretch text-text-secondary group-data-[orientation=vertical]/tabs:h-fit group-data-[orientation=vertical]/tabs:flex-col",
  {
    variants: {
      variant: {
        /**
         * The mockup's band: an underline rail, full width, on the divider.
         *
         * No surface of its own. The mockup gives `.local-tabs` a white
         * background AND `padding: 0 40px`, because there it is a full-bleed
         * band under the topbar whose labels line up with `.main`'s 40px gutter.
         * In the console the band lives INSIDE the content column, so the
         * background arrived without the padding: a white box whose left edge
         * the first tab was flush against — Jean, 2026-08-11, "manque du padding
         * avant General". Padding it instead would push `General` 40px off the
         * `h1` directly above it.
         *
         * `NavTabs` — the same 52px rail, same 28px gap, same 3px rose mark —
         * already ships without the background on every Datastream screen. Two
         * renderings of one band is the thing this library exists to stop, so
         * the rail is transparent here too and the column keeps one left edge
         * from the breadcrumb down to the cards.
         */
        line: "h-tabs-height gap-7 border-b border-divider-base group-data-[orientation=vertical]/tabs:h-fit group-data-[orientation=vertical]/tabs:gap-0 group-data-[orientation=vertical]/tabs:border-r group-data-[orientation=vertical]/tabs:border-b-0",
        /** A segmented filter inside a panel. Same label size, pill container. */
        segmented:
          "w-fit items-center gap-1 rounded-pill border border-divider-base bg-background-light p-1",
      },
    },
    defaultVariants: {
      variant: "line",
    },
  }
)

function TabsList({
  className,
  variant = "line",
  ...props
}: React.ComponentProps<typeof TabsPrimitive.List> &
  VariantProps<typeof tabsListVariants>) {
  return (
    <TabsPrimitive.List
      data-slot="tabs-list"
      data-variant={variant}
      className={cn(tabsListVariants({ variant }), className)}
      {...props}
    />
  )
}

function TabsTrigger({
  className,
  ...props
}: React.ComponentProps<typeof TabsPrimitive.Trigger>) {
  return (
    <TabsPrimitive.Trigger
      data-slot="tabs-trigger"
      className={cn(
        "relative inline-flex items-center justify-center gap-2 text-ui font-semibold whitespace-nowrap text-text-secondary transition-colors hover:text-text focus-visible:outline-3 focus-visible:outline-offset-2 focus-visible:outline-focus disabled:pointer-events-none disabled:opacity-50 data-[state=active]:text-text [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-4",
        // line: the 3px rose mark, drawn over the divider.
        "group-data-[variant=line]/tabs-list:after:absolute group-data-[variant=line]/tabs-list:after:rounded-t-[3px] group-data-[variant=line]/tabs-list:after:bg-primary group-data-[variant=line]/tabs-list:after:opacity-0 group-data-[variant=line]/tabs-list:after:transition-opacity group-data-[variant=line]/tabs-list:data-[state=active]:after:opacity-100",
        "group-data-[variant=line]/tabs-list:group-data-[orientation=horizontal]/tabs:after:inset-x-0 group-data-[variant=line]/tabs-list:group-data-[orientation=horizontal]/tabs:after:-bottom-px group-data-[variant=line]/tabs-list:group-data-[orientation=horizontal]/tabs:after:h-[3px]",
        "group-data-[variant=line]/tabs-list:group-data-[orientation=vertical]/tabs:w-full group-data-[variant=line]/tabs-list:group-data-[orientation=vertical]/tabs:justify-start group-data-[variant=line]/tabs-list:group-data-[orientation=vertical]/tabs:py-2.5 group-data-[variant=line]/tabs-list:group-data-[orientation=vertical]/tabs:after:inset-y-0 group-data-[variant=line]/tabs-list:group-data-[orientation=vertical]/tabs:after:-right-px group-data-[variant=line]/tabs-list:group-data-[orientation=vertical]/tabs:after:w-[3px]",
        // segmented: the active tab lifts onto a white pill.
        "group-data-[variant=segmented]/tabs-list:h-control-height-small group-data-[variant=segmented]/tabs-list:rounded-pill group-data-[variant=segmented]/tabs-list:px-3.5 group-data-[variant=segmented]/tabs-list:data-[state=active]:bg-surface-light group-data-[variant=segmented]/tabs-list:data-[state=active]:shadow-card-light",
        className
      )}
      {...props}
    />
  )
}

function TabsContent({
  className,
  ...props
}: React.ComponentProps<typeof TabsPrimitive.Content>) {
  return (
    <TabsPrimitive.Content
      data-slot="tabs-content"
      className={cn("flex-1 outline-none", className)}
      {...props}
    />
  )
}

export { Tabs, TabsList, TabsTrigger, TabsContent, tabsListVariants }
