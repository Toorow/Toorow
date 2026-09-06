/**
 * ActivityLog — what happened, in order, with who and when.
 *
 * Jean, 2026-07-29, from a reference feed. It is a different object from
 * `PhaseTimeline`, and the difference is worth keeping straight:
 *
 *   PhaseTimeline  a known sequence of phases, each ending in a state.
 *                  Solid connector, filled marks, a phase can be `todo`.
 *   ActivityLog    an open-ended record of events that already happened.
 *                  Dashed connector, open marks, nothing is ever pending.
 *
 * The **dashed** connector is the request and it earns its place: a solid line
 * says "these are the steps of one thing", a dashed line says "time passed
 * between these, and there may be more". The log is never complete, so its
 * line should not look closed.
 *
 * The marks are open rings, not filled dots — an event is a point in time, not
 * a state you are in. `tone` is available for the few entries that carry one
 * (a failure, a publication) and defaults to neutral, because most of a log is
 * neither good nor bad.
 *
 * **Links live inside the entry.** `children` is a node, so an entry composes
 * `<strong>` for the object acted on and `<a>` for the thing to open, exactly
 * as the reference does with `View Email`. `action` is the trailing affordance
 * on the timestamp row.
 */
import type { ReactNode } from "react";
import { cn } from "../lib/cn";
import { TONE_TEXT, type Fill } from "./tone";

export type ActivityEntry = {
  /** The sentence. Compose `<strong>` and `<a>` freely — it is a node. */
  children: ReactNode;
  /** When it happened. Absolute, never "3 hours ago" — a log is evidence. */
  at: ReactNode;
  /** One affordance on the timestamp row: `View email`, `Open run`. */
  action?: ReactNode;
  tone?: Fill;
};

export function ActivityLog({
  entries,
  className,
}: {
  entries: ActivityEntry[];
  className?: string;
}) {
  return (
    <ol className={cn("m-0 list-none p-0", className)}>
      {entries.map((entry, i) => (
        <li
          key={i}
          className={cn(
            "relative grid grid-cols-[18px_minmax(0,1fr)] gap-3 pb-6 last:pb-0",
            // Dashed, and stopping at the last entry — a line running past the
            // final event would promise one that has not happened.
            i < entries.length - 1 &&
              "before:absolute before:top-[15px] before:bottom-0 before:left-[5px] before:w-0 before:border-l-2 before:border-dashed before:border-border-control/60 before:content-['']",
          )}
        >
          <span
            aria-hidden
            className={cn(
              // An open ring: the event is a point, not a state.
              "relative z-10 mt-1 size-2.5 rounded-pill border-2 border-current bg-surface-light",
              TONE_TEXT[entry.tone ?? "neutral"],
            )}
          />
          <div className="min-w-0">
            <p className="m-0 text-ui leading-[1.5] text-text [&_a]:font-semibold [&_a]:text-primary [&_a]:underline-offset-2 [&_a:hover]:underline [&_strong]:font-semibold">
              {entry.children}
            </p>
            <p className="mt-1 mb-0 flex flex-wrap items-center gap-3 text-caption text-text-secondary [&_a]:font-label [&_a]:text-primary [&_a]:underline-offset-2 [&_a:hover]:underline">
              <span className="font-numeric [font-variant-numeric:lining-nums_tabular-nums]">
                {entry.at}
              </span>
              {entry.action}
            </p>
          </div>
        </li>
      ))}
    </ol>
  );
}
