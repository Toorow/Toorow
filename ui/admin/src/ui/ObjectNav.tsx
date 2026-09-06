/**
 * ObjectHeader and NavTabs — the band that sits above every Datastream tab.
 *
 * `key-datastream-overview-v4.html` puts both in the frame: the object mark,
 * its name and source at the top left, and under it a row of tabs where the
 * active one carries a 3px rose mark on the divider.
 *
 *     .object       34px mark, name 14/600, source 12 secondary
 *     .local-tabs   52px band, 28px gap, 1px bottom rule
 *     .local-tab    14/600; .active adds the 3px mark and the full-strength ink
 *
 * This file exists because that markup was written by hand in two screens and
 * was about to be written in a third. Two copies is a coincidence; three is a
 * vocabulary being rebuilt one screen at a time, which is the exact failure
 * this migration is undoing. The rule the spec sets — a screen composes from
 * components or it changes the base — makes the answer here a component.
 *
 * The tabs stay ANCHORS rather than a Radix Tabs root: the tab IS the route,
 * so it has to be middle-clickable and copyable. `onNavigate` upgrades a plain
 * left-click to client-side navigation and leaves every other click alone.
 *
 * A tab with no `href` renders disabled and stays VISIBLE. The architecture
 * document contracts six tabs and two have no screen behind them; hiding those
 * two would erase the record of what is still missing, which the project
 * treats as destroying the inventory of remaining work.
 *
 * "No `href`" is not "nothing to do", and conflating the two cost a screen —
 * story 57.5. When the addresses stopped being composed by the callers, a mount
 * that passed `onNavigate` and no address (the `/debug/screen` sandbox, which
 * exists precisely so this design can be LOOKED at) rendered six disabled
 * `<span>`s and stopped navigating. A tab is therefore disabled only when it has
 * NEITHER an address NOR a callback. With a callback and no address it is a
 * `<button>`: it does something, so it must be reachable by keyboard and
 * announced as a control. A prop whose omission silently disables navigation is
 * a trap for the next caller, and the guard belongs here rather than in each
 * of them.
 */
import type { MouseEvent, ReactNode } from "react";
import { cn } from "../lib/cn";
import { ConnectorMark } from "./ConnectorMark";

const TAB_BASE =
  "relative inline-flex h-tabs-height items-center text-ui font-semibold whitespace-nowrap transition-colors outline-none focus-visible:outline-3 focus-visible:outline-offset-2 focus-visible:outline-focus";
const TAB_MARK =
  'after:absolute after:inset-x-0 after:-bottom-px after:h-[3px] after:rounded-t-[3px] after:bg-primary after:opacity-0 after:content-[""]';

/** The object being looked at: a mark, its name, and where it comes from. */
export function ObjectHeader({
  name,
  source,
  provider,
  className,
}: {
  name: string;
  source: ReactNode;
  /**
   * The provider whose mark identifies this object — a module id or a
   * `source_kind`. Resolved through the managed registry, so a screen never
   * picks an asset path and an unregistered provider gets the generic mark
   * rather than a letter.
   */
  provider?: string | null;
  className?: string;
}) {
  return (
    <div className={cn("flex items-center gap-3", className)}>
      <ConnectorMark provider={provider} />
      <div className="min-w-0">
        <strong className="block truncate text-ui font-semibold text-text">{name}</strong>
        <span className="block truncate text-caption text-text-secondary">{source}</span>
      </div>
    </div>
  );
}

export type NavTab = {
  key: string;
  label: string;
  /** Absent means contracted but unbuilt — shown, disabled, never hidden. */
  href?: string;
};

export function NavTabs({
  label,
  tabs,
  current,
  onNavigate,
  className,
}: {
  /** Names the nav for a screen reader — "Datastream", "Project". */
  label: string;
  tabs: readonly NavTab[];
  current: string;
  onNavigate?: (key: string) => void;
  className?: string;
}) {
  function handle(event: MouseEvent<HTMLAnchorElement>, key: string) {
    // Anything but a plain left-click belongs to the browser: a new tab, a
    // new window, a copied address. Only the plain case is intercepted.
    if (!onNavigate || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
      return;
    }
    event.preventDefault();
    onNavigate(key);
  }

  return (
    <nav
      aria-label={label}
      className={cn(
        "flex h-tabs-height w-full max-w-full items-stretch gap-7 overflow-x-auto border-b border-divider-base",
        className,
      )}
    >
      {tabs.map((tab) => {
        const marked = cn(
          TAB_BASE,
          TAB_MARK,
          tab.key === current ? "text-text after:opacity-100" : "text-text-secondary hover:text-text",
        );
        if (tab.href) {
          return (
            <a
              key={tab.key}
              className={marked}
              href={tab.href}
              aria-current={tab.key === current ? "page" : undefined}
              onClick={(event) => handle(event, tab.key)}
            >
              {tab.label}
            </a>
          );
        }
        // An address is what makes a tab copyable and middle-clickable, and it is
        // preferred. Without one the gesture still exists, so the control does.
        if (onNavigate) {
          return (
            <button
              key={tab.key}
              type="button"
              className={marked}
              aria-current={tab.key === current ? "page" : undefined}
              onClick={() => onNavigate(tab.key)}
            >
              {tab.label}
            </button>
          );
        }
        return (
          <span
            key={tab.key}
            className={cn(TAB_BASE, "cursor-not-allowed text-text-secondary/60")}
            aria-disabled="true"
          >
            {tab.label}
          </span>
        );
      })}
    </nav>
  );
}
