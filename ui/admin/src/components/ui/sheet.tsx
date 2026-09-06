/**
 * Sheet — the side panel, once, in the two shapes the product actually uses.
 *
 * There were ten of them: `.kg-drawer`, `.chain-drawer-cell`, the dead
 * `.drawer/.drawer-layout/.drawer-header/.drawer-body/.drawer-section` block in
 * `application.css`, MUI `Drawer` in `FieldDetailDrawer` and `Sidebar`, and
 * hand-rolled panels in `SkillEditorDrawer`, `ResultWorkbench` and
 * `VisualizationBuilder`. Ten radii, ten widths, ten close behaviours.
 *
 * TWO MODES, AND THE DIFFERENCE IS NOT COSMETIC.
 *
 *   `modal`   floats over the page, dims it, traps focus, closes on Escape.
 *             For a task you must finish or abandon.
 *   `inline`  sits IN the flow beside the content and takes 384px, exactly as
 *             `datastream-recovery.html` draws `.recovery-drawer`. The page
 *             stays live and readable next to it. For evidence you consult
 *             while working — a node's detail, a run's diagnosis.
 *
 * The mockups draw the second one, and it is the one no library ships: a modal
 * Sheet cannot be it, because dimming the page is the opposite of what a
 * side-by-side evidence panel is for. That is why every screen wrote its own —
 * shadcn's Sheet is modal-only.
 *
 * `inline` is therefore NOT a Radix Dialog. It is a labelled `<aside>` with a
 * close control: no overlay, no focus trap, no Escape hijack — trapping focus
 * in a panel the person is reading alongside a table would make the table
 * unreachable by keyboard. It carries `aria-labelledby` so it is still an
 * addressable region, which is what a screen reader needs from it.
 *
 * GEOMETRY. 384px and the 1px left rule come from `.recovery-drawer`; the
 * header/body rhythm is the card's 24px `--spacing-card-padding`, so a Sheet
 * and a Panel sitting next to each other agree. The close control is the
 * mockup's 40px icon button — a `Button`, never a bare `<button>` with its own
 * opacity rules.
 *
 * MOTION. Slide only. `motion-iconography.md:78` forbids animating width — a
 * panel that grows from zero reflows the page it is meant to sit beside.
 */
"use client"

import * as React from "react"
import { XIcon } from "lucide-react"
import { Dialog as SheetPrimitive } from "radix-ui"

import { cn } from "@/lib/cn"
import { Button } from "@/components/ui/button"

function Sheet({ ...props }: React.ComponentProps<typeof SheetPrimitive.Root>) {
  return <SheetPrimitive.Root data-slot="sheet" {...props} />
}

function SheetTrigger({
  ...props
}: React.ComponentProps<typeof SheetPrimitive.Trigger>) {
  return <SheetPrimitive.Trigger data-slot="sheet-trigger" {...props} />
}

function SheetClose({
  ...props
}: React.ComponentProps<typeof SheetPrimitive.Close>) {
  return <SheetPrimitive.Close data-slot="sheet-close" {...props} />
}

function SheetPortal({
  ...props
}: React.ComponentProps<typeof SheetPrimitive.Portal>) {
  return <SheetPrimitive.Portal data-slot="sheet-portal" {...props} />
}

function SheetOverlay({
  className,
  ...props
}: React.ComponentProps<typeof SheetPrimitive.Overlay>) {
  return (
    <SheetPrimitive.Overlay
      data-slot="sheet-overlay"
      className={cn(
        "fixed inset-0 z-50 bg-text/45 data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:animate-in data-[state=open]:fade-in-0",
        className
      )}
      {...props}
    />
  )
}

/** The modal side panel. `side` defaults to `right`, which is where every
 *  mockup puts it; `left` exists for a navigation drawer and nothing else. */
function SheetContent({
  className,
  children,
  side = "right",
  showCloseButton = true,
  ...props
}: React.ComponentProps<typeof SheetPrimitive.Content> & {
  side?: "right" | "left"
  showCloseButton?: boolean
}) {
  return (
    <SheetPortal>
      <SheetOverlay />
      <SheetPrimitive.Content
        data-slot="sheet-content"
        data-side={side}
        className={cn(
          "fixed inset-y-0 z-50 flex h-full w-96 max-w-[100vw] flex-col bg-surface shadow-overlay",
          "data-[state=closed]:animate-out data-[state=open]:animate-in",
          side === "right"
            ? "right-0 border-l border-divider-base data-[state=closed]:slide-out-to-right data-[state=open]:slide-in-from-right"
            : "left-0 border-r border-divider-base data-[state=closed]:slide-out-to-left data-[state=open]:slide-in-from-left",
          className
        )}
        {...props}
      >
        {children}
        {showCloseButton ? (
          <SheetPrimitive.Close asChild>
            <Button
              variant="ghost"
              size="icon-sm"
              className="absolute top-4 right-4 border-transparent bg-transparent"
              aria-label="Close"
            >
              <XIcon aria-hidden />
            </Button>
          </SheetPrimitive.Close>
        ) : null}
      </SheetPrimitive.Content>
    </SheetPortal>
  )
}

/**
 * The in-flow panel — `.recovery-drawer`.
 *
 * Not a Dialog on purpose: no overlay and no focus trap, because the person is
 * reading it *beside* live content. `title` is required rather than optional —
 * an unlabelled region is unreachable by the rotor, and every one of the ten
 * hand-rolled panels this replaces had a heading anyway.
 */
function SheetInline({
  className,
  children,
  title,
  description,
  onClose,
  actions,
  ...props
}: React.ComponentProps<"aside"> & {
  title: React.ReactNode
  description?: React.ReactNode
  /** Omit and the panel has no close control — for one permanently open beside its list. */
  onClose?: () => void
  /** The panel's own actions, in its header, to the left of the close control. */
  actions?: React.ReactNode
}) {
  const id = React.useId()
  return (
    <aside
      data-slot="sheet-inline"
      aria-labelledby={`${id}-title`}
      className={cn(
        "flex w-96 shrink-0 flex-col self-stretch border-s border-divider-base bg-surface",
        className
      )}
      {...props}
    >
      <div className="flex items-start gap-3 border-b border-divider-base p-card">
        <div className="min-w-0 flex-1">
          <h2 id={`${id}-title`} className="text-panel-title text-text">
            {title}
          </h2>
          {description ? (
            <p className="mt-1 text-ui text-text-secondary">{description}</p>
          ) : null}
        </div>
        {actions}
        {onClose ? (
          <Button
            variant="ghost"
            size="icon-sm"
            className="border-transparent bg-transparent"
            onClick={onClose}
            aria-label="Close"
          >
            <XIcon aria-hidden />
          </Button>
        ) : null}
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto p-card">{children}</div>
    </aside>
  )
}

function SheetHeader({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="sheet-header"
      className={cn("flex flex-col gap-1 border-b border-divider-base p-card pe-14", className)}
      {...props}
    />
  )
}

function SheetBody({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="sheet-body"
      className={cn("min-h-0 flex-1 overflow-y-auto p-card", className)}
      {...props}
    />
  )
}

function SheetFooter({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="sheet-footer"
      className={cn("flex items-center justify-end gap-2 border-t border-divider-base p-card", className)}
      {...props}
    />
  )
}

function SheetTitle({
  className,
  ...props
}: React.ComponentProps<typeof SheetPrimitive.Title>) {
  return (
    <SheetPrimitive.Title
      data-slot="sheet-title"
      className={cn("text-panel-title text-text", className)}
      {...props}
    />
  )
}

function SheetDescription({
  className,
  ...props
}: React.ComponentProps<typeof SheetPrimitive.Description>) {
  return (
    <SheetPrimitive.Description
      data-slot="sheet-description"
      className={cn("text-ui text-text-secondary", className)}
      {...props}
    />
  )
}

export {
  Sheet,
  SheetBody,
  SheetClose,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetInline,
  SheetOverlay,
  SheetPortal,
  SheetTitle,
  SheetTrigger,
}
