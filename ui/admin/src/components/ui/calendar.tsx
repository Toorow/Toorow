/**
 * Calendar — adapted to the console's geometry.
 *
 * Installed 2026-07-29 at Jean's request ("you will need it"), and he is right:
 * the console asks for a date in at least four places — recovery picks an
 * interval, coverage reads a calendar, imports name a window, a schedule names
 * a start. Those were going to be four date pickers.
 *
 * No mockup draws a calendar, and this file says so rather than implying one.
 * Its geometry comes from the rules that do apply: a 36px cell so a row of
 * seven matches the 44px control rhythm around it; the 10px `--radius-control`
 * on a day, the same step as a menu row; ONE day selected is rose and a RANGE
 * is ink (Jean, 2026-07-29 — a period reads better in black); a day carries no
 * chip of its own until it is chosen, because a grid of 42 filled pills is not
 * a calendar;
 * because picking a date IS the recommended direction while the picker is
 * open; and the numerals in the numeric font with tabular figures, which is
 * the product rule that makes columns of digits line up.
 *
 * Weekday headers are the column-header treatment from the table — 12px/700
 * uppercase, muted — so a calendar and a data table read as one family.
 */
"use client"

import * as React from "react"

// The console's one locale. `"default"` and no-locale meant this calendar
// spelled its months and its `data-day` differently on every reader's
// machine; `console-presentation.md` §2 pins one, and the gate
// (`__tests__/FormattingIsCentral.test.ts`) names this file as the exception
// it is, with the reason.
import { CONSOLE_LOCALE } from "../../ui/format"
import {
  ChevronDownIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
} from "lucide-react"
import {
  DayPicker,
  getDefaultClassNames,
  type DayButton,
} from "react-day-picker"

import { cn } from "@/lib/cn"
import { Button, buttonVariants } from "@/components/ui/button"

function Calendar({
  className,
  classNames,
  showOutsideDays = true,
  captionLayout = "label",
  buttonVariant = "ghost",
  formatters,
  components,
  ...props
}: React.ComponentProps<typeof DayPicker> & {
  buttonVariant?: React.ComponentProps<typeof Button>["variant"]
}) {
  const defaultClassNames = getDefaultClassNames()

  return (
    <DayPicker
      showOutsideDays={showOutsideDays}
      className={cn(
        "group/calendar bg-surface-light p-3 font-numeric [--cell-size:--spacing(9)] [font-variant-numeric:lining-nums_tabular-nums] [[data-slot=card-content]_&]:bg-transparent [[data-slot=popover-content]_&]:bg-transparent",
        String.raw`rtl:**:[.rdp-button\_next>svg]:rotate-180`,
        String.raw`rtl:**:[.rdp-button\_previous>svg]:rotate-180`,
        className
      )}
      captionLayout={captionLayout}
      formatters={{
        formatMonthDropdown: (date) =>
          date.toLocaleString(CONSOLE_LOCALE, { month: "short" }),
        ...formatters,
      }}
      classNames={{
        root: cn("w-fit", defaultClassNames.root),
        months: cn(
          "relative flex flex-col gap-4 md:flex-row",
          defaultClassNames.months
        ),
        month: cn("flex w-full flex-col gap-4", defaultClassNames.month),
        nav: cn(
          "absolute inset-x-0 top-0 flex w-full items-center justify-between gap-1",
          defaultClassNames.nav
        ),
        button_previous: cn(
          buttonVariants({ variant: buttonVariant }),
          "size-(--cell-size) p-0 select-none aria-disabled:opacity-50",
          defaultClassNames.button_previous
        ),
        button_next: cn(
          buttonVariants({ variant: buttonVariant }),
          "size-(--cell-size) p-0 select-none aria-disabled:opacity-50",
          defaultClassNames.button_next
        ),
        month_caption: cn(
          "flex h-(--cell-size) w-full items-center justify-center px-(--cell-size)",
          defaultClassNames.month_caption
        ),
        dropdowns: cn(
          "flex h-(--cell-size) w-full items-center justify-center gap-1.5 text-sm font-medium",
          defaultClassNames.dropdowns
        ),
        dropdown_root: cn(
          "relative rounded-md border border-input shadow-xs has-focus:border-ring has-focus:ring-[3px] has-focus:ring-ring/50",
          defaultClassNames.dropdown_root
        ),
        dropdown: cn(
          "absolute inset-0 bg-popover opacity-0",
          defaultClassNames.dropdown
        ),
        caption_label: cn(
          "font-medium select-none",
          captionLayout === "label"
            ? "text-sm"
            : "flex h-8 items-center gap-1 rounded-md pr-1 pl-2 text-sm [&>svg]:size-3.5 [&>svg]:text-text-secondary",
          defaultClassNames.caption_label
        ),
        month_grid: cn("w-full border-collapse", defaultClassNames.month_grid),
        weekdays: cn("flex", defaultClassNames.weekdays),
        weekday: cn(
          "flex-1 rounded-md text-caption font-label tracking-[0.035em] text-text-secondary uppercase select-none",
          defaultClassNames.weekday
        ),
        week: cn("mt-2 flex w-full", defaultClassNames.week),
        week_number_header: cn(
          "w-(--cell-size) select-none",
          defaultClassNames.week_number_header
        ),
        week_number: cn(
          "text-[0.8rem] text-text-secondary select-none",
          defaultClassNames.week_number
        ),
        day: cn(
          "group/day relative aspect-square h-full w-full p-0 text-center select-none [&:last-child[data-selected=true]_button]:rounded-r-md",
          props.showWeekNumber
            ? "[&:nth-child(2)[data-selected=true]_button]:rounded-l-md"
            : "[&:first-child[data-selected=true]_button]:rounded-l-md",
          defaultClassNames.day
        ),
        range_start: cn(
          "rounded-l-control",
          defaultClassNames.range_start
        ),
        range_middle: cn("rounded-none", defaultClassNames.range_middle),
        range_end: cn("rounded-r-control", defaultClassNames.range_end),
        today: cn(
          "[&_button]:ring-1 [&_button]:ring-border-control data-[selected=true]:[&_button]:ring-0",
          defaultClassNames.today
        ),
        outside: cn(
          "text-text-secondary aria-selected:text-text-secondary",
          defaultClassNames.outside
        ),
        disabled: cn(
          "text-text-secondary opacity-50",
          defaultClassNames.disabled
        ),
        hidden: cn("invisible", defaultClassNames.hidden),
        ...classNames,
      }}
      components={{
        Root: ({ className, rootRef, ...props }) => {
          return (
            <div
              data-slot="calendar"
              ref={rootRef}
              className={cn(className)}
              {...props}
            />
          )
        },
        Chevron: ({ className, orientation, ...props }) => {
          if (orientation === "left") {
            return (
              <ChevronLeftIcon className={cn("size-4", className)} {...props} />
            )
          }

          if (orientation === "right") {
            return (
              <ChevronRightIcon
                className={cn("size-4", className)}
                {...props}
              />
            )
          }

          return (
            <ChevronDownIcon className={cn("size-4", className)} {...props} />
          )
        },
        DayButton: CalendarDayButton,
        WeekNumber: ({ children, ...props }) => {
          return (
            <td {...props}>
              <div className="flex size-(--cell-size) items-center justify-center text-center">
                {children}
              </div>
            </td>
          )
        },
        ...components,
      }}
      {...props}
    />
  )
}

function CalendarDayButton({
  className,
  day,
  modifiers,
  ...props
}: React.ComponentProps<typeof DayButton>) {
  const defaultClassNames = getDefaultClassNames()

  const ref = React.useRef<HTMLButtonElement>(null)
  React.useEffect(() => {
    if (modifiers.focused) ref.current?.focus()
  }, [modifiers.focused])

  return (
    <Button
      ref={ref}
      variant="ghost"
      size="icon"
      data-day={day.date.toLocaleDateString(CONSOLE_LOCALE)}
      data-selected-single={
        modifiers.selected &&
        !modifiers.range_start &&
        !modifiers.range_end &&
        !modifiers.range_middle
      }
      data-range-start={modifiers.range_start}
      data-range-end={modifiers.range_end}
      data-range-middle={modifiers.range_middle}
      className={cn(
"flex aspect-square size-auto w-full min-w-(--cell-size) flex-col gap-1 rounded-control border-transparent bg-transparent leading-none font-normal shadow-none hover:border-transparent hover:bg-background-light group-data-[focused=true]/day:relative group-data-[focused=true]/day:z-10 group-data-[focused=true]/day:outline-3 group-data-[focused=true]/day:outline-offset-2 group-data-[focused=true]/day:outline-focus data-[selected-single=true]:bg-primary data-[selected-single=true]:text-on-primary data-[range-start=true]:rounded-l-control data-[range-start=true]:rounded-r-none data-[range-start=true]:bg-text data-[range-start=true]:text-text-on-dark data-[range-end=true]:rounded-r-control data-[range-end=true]:rounded-l-none data-[range-end=true]:bg-text data-[range-end=true]:text-text-on-dark data-[range-middle=true]:rounded-none data-[range-middle=true]:bg-text/10 data-[range-middle=true]:text-text [&>span]:text-xs [&>span]:opacity-70",
        defaultClassNames.day,
        className
      )}
      {...props}
    />
  )
}

export { Calendar, CalendarDayButton }
