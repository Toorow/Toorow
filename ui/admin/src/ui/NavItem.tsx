/**
 * NavItem, NavSubItem, NavBranch — the left rail's vocabulary.
 *
 * This file exists because the rail was built out of `Button`, and `Button` is
 * contractually a BORDERED PILL: its header quotes the mockup rule *"every
 * button carries a border"* and `rounded-pill` is in its base class. Six
 * workspaces plus their sections rendered as a stack of outlined pills — which
 * is not a menu, and is not what the validated mockup draws.
 *
 * Geometry read from `ux-connector-2026-07-23/.working/application-v3.css`,
 * the sheet the desktop mocks were captured from (lines 279-345):
 *
 *     .nav-item        min-height 44px; padding 0 14px; gap 12px;
 *                      border-radius 10px; 15px/500; colour --muted
 *     .nav-item.active background --rose-soft; --ink; 700;
 *                      ::before  3px rose bar, left 0, inset 10px, radius 4
 *     .nav-item svg    20px
 *     .subnav          margin 4px 0 8px 24px; padding-left 24px; border-left
 *     .subnav-item     min-height 38px; padding 0 12px; radius 8px; 14px
 *     .subnav-item.active  background #f4f3f8; --ink; 700
 *
 * So: **no border and no pill anywhere in the rail.** Selection is carried by
 * a soft rose ground plus the 3px rose mark, never by an outline — the same
 * grammar as `NavTabs`, where the active tab carries a 3px rose mark on the
 * divider. One product, one way of saying "you are here".
 *
 * Token mapping: `--rose-soft` is `primary-container`, `--rose` is `primary`,
 * `--muted` is `text-secondary`, `--ink` is `text`, and the subnav's `#f4f3f8`
 * is `surface-subtle` (#F0F0F5). The 8px subnav radius has no token — the
 * ladder is 6/10/12/16 — so it rounds to `rounded-small`.
 *
 * These are BUTTONS, not anchors, because the rail drives a client-side router
 * whose canonical URLs are built by `router.tsx`, not by the caller. When the
 * rail gains real hrefs the base classes below carry over unchanged.
 */
import type { ComponentProps, ReactNode } from "react";
import { cn } from "../lib/cn";

const BASE =
  "relative flex w-full items-center rounded-control text-left transition-colors outline-none focus-visible:outline-3 focus-visible:outline-offset-2 focus-visible:outline-focus disabled:pointer-events-none disabled:opacity-50";

/** The 3px rose bar the mockup puts on the active row, inset 10px vertically. */
const MARK =
  'before:absolute before:left-0 before:top-2.5 before:bottom-2.5 before:w-[3px] before:rounded-[4px] before:bg-primary before:content-[""]';

export type NavItemProps = ComponentProps<"button"> & {
  active?: boolean;
  /** Rendered at 20px, per the mockup, and never allowed to shrink. */
  icon?: ReactNode;
  /** Sits at `margin-left: auto` — the disclosure chevron, a count, a dot. */
  trailing?: ReactNode;
  /**
   * Let the label wrap instead of truncating. One caller: the Datastream tree,
   * whose generated names ("Google Search Console - Custom selection [...]")
   * are only readable over two lines. Everything else in a 248px rail truncates.
   */
  wrap?: boolean;
};

/** A Level 1 workspace row. */
export function NavItem({ active = false, icon, trailing, wrap = false, className, children, ...props }: NavItemProps) {
  return (
    <button
      type="button"
      data-slot="nav-item"
      data-active={active ? "true" : undefined}
      aria-current={active ? "page" : undefined}
      className={cn(
        BASE,
        "min-h-11 gap-3 px-3.5 text-body font-medium",
        active
          ? cn(MARK, "bg-primary-container font-bold text-text dark:bg-primary/20 dark:text-text-on-dark")
          : "text-text-secondary hover:bg-surface-subtle hover:text-text dark:hover:bg-divider-subtle dark:hover:text-text-on-dark",
        className,
      )}
      {...props}
    >
      {icon ? <span className="flex size-5 shrink-0 items-center justify-center [&_svg]:size-5">{icon}</span> : null}
      <span className={cn("min-w-0 flex-1", wrap ? "break-words" : "truncate")}>{children}</span>
      {trailing ? <span className="ml-auto flex shrink-0 items-center gap-2">{trailing}</span> : null}
    </button>
  );
}

/** A Level 2 section row, one step quieter and one step smaller. */
export function NavSubItem({ active = false, icon, trailing, wrap = false, className, children, ...props }: NavItemProps) {
  return (
    <button
      type="button"
      data-slot="nav-sub-item"
      data-active={active ? "true" : undefined}
      aria-current={active ? "page" : undefined}
      className={cn(
        BASE,
        "min-h-[38px] gap-2 rounded-small px-3 text-ui",
        active
          ? "bg-surface-subtle font-bold text-text dark:bg-divider-medium dark:text-text-on-dark"
          : "text-text-secondary hover:bg-surface-subtle hover:text-text dark:hover:bg-divider-subtle dark:hover:text-text-on-dark",
        className,
      )}
      {...props}
    >
      {icon ? <span className="flex shrink-0 items-center justify-center">{icon}</span> : null}
      <span className={cn("min-w-0 flex-1", wrap ? "break-words" : "truncate")}>{children}</span>
      {trailing ? <span className="ml-auto flex shrink-0 items-center gap-2">{trailing}</span> : null}
    </button>
  );
}

/**
 * The indented rule that gathers a row's children — `.subnav` in the mockup.
 * `tight` is the Datastream tree, which nests one level deeper again and would
 * otherwise walk off the 248px rail.
 */
export function NavBranch({ tight = false, className, children }: { tight?: boolean; className?: string; children: ReactNode }) {
  return (
    <div
      className={cn(
        "border-l border-divider-base dark:border-divider-medium",
        tight ? "my-1 ml-3 space-y-0.5 pl-2" : "mb-2 ml-6 mt-1 space-y-0.5 pl-6",
        className,
      )}
    >
      {children}
    </div>
  );
}
