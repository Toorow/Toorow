/**
 * PhaseTimeline — the ordered sequence of an operation, each phase carrying
 * its own evidence.
 *
 * **This replaces the horizontal `Stepper` I built, which was the wrong
 * object.** The contracted component is named in `EXPERIENCE.md` l. 202:
 *
 *   > PhaseTimeline — first pull and recovery progress. Shows authorization,
 *   > extraction, validation, publication and backfill as **independently
 *   > evidenced phases** with bounded live announcements.
 *
 * and in `DESIGN.md` l. 152: *"one ordered first-pull sequence. Recent
 * publication reaches a terminal state **independently** from historical
 * backfill, whose progress never blocks already usable data."*
 *
 * Independently evidenced is the whole point, and a horizontal rail cannot
 * express it: a rail says step 4 waits on step 3. Here publication can be
 * finished while the backfill behind it is still running, which is exactly the
 * product's claim about recoverable data.
 *
 * Geometry from `mockups/first-publication.html`, the validated composition
 * `EXPERIENCE.md` l. 99 points at:
 *
 *     .phase        { display: grid; grid-template-columns: 22px minmax(0,1fr) auto;
 *                     gap: 14px; min-height: 78px }
 *     .phase:not(:last-child)::before
 *                   { left: 7px; top: 18px; bottom: -2px; width: 2px;
 *                     background: var(--line) }
 *     .phase-mark   { width: 16px; height: 16px; margin-top: 2px;
 *                     border: 4px solid <tone container>; border-radius: 50%;
 *                     background: <tone> }
 *     .phase-copy strong { font-size: 14px }
 *     .phase-copy small  { margin-top: 5px; color: var(--muted); font-size: 12px }
 *     .phase-time   { color: var(--muted); font: 12px Geist }
 *
 * The 4px ring in the tone's container colour is the halo `motion-iconography.md`
 * asks for — "the halo belongs to the icon rather than the entire row" — and it
 * is what the 2px connector runs behind.
 *
 * **The connector is dashed, and that is an amendment**: the mockup draws it
 * solid (`background: var(--line)`). Jean, 2026-07-29 — a dashed line between
 * phases reads as time elapsed rather than as a pipe carrying something, which
 * is the truer statement for phases that complete independently. It is also a
 * shade darker than the divider, because at `--line` on white it was barely
 * there.
 */
import type { ReactNode } from "react";
import { cn } from "../lib/cn";
import { Progress } from "../components/ui/progress";
import { TONE_DOT, type Fill } from "./tone";

/**
 * The four phase marks, READ FROM THE SCALE (story 76-2). They were four class
 * literals, which made this file a second place the console decided what a
 * success looks like; `TONE_DOT` is the one place now. `todo` is the exception
 * that is not a tone at all: a phase nobody has reached yet is drawn on the page
 * surface, not in the neutral ink a `Status` uses for an absence.
 */
const MARK: Record<"done" | "running" | "todo" | "failed", string> = {
  done: TONE_DOT.success,
  running: TONE_DOT.info,
  todo: "border-divider-base bg-surface-light",
  failed: TONE_DOT.error,
};

export type Phase = {
  /** What the phase is, in a few words: `Recent extraction`. */
  label: ReactNode;
  /** Its evidence — never a restatement of the label. `1–21 Jul · 186,420 rows`. */
  evidence?: ReactNode;
  /** When it happened, or `Running`. Right-aligned, tabular. */
  at?: ReactNode;
  state: "done" | "running" | "todo" | "failed";
  /**
   * 0-100. A phase that reports its own advance — the historical backfill is
   * the case the mockup draws. It sits inside the phase, because its progress
   * must never read as the operation's progress.
   */
  percent?: number;
  /** The tone of that inner bar. Defaults to `info`, as the mockup draws it. */
  percentTone?: Fill;
};

export function PhaseTimeline({ phases, className }: { phases: Phase[]; className?: string }) {
  return (
    <ol className={cn("m-0 list-none p-0", className)}>
      {phases.map((phase, i) => (
        <li
          key={i}
          aria-current={phase.state === "running" ? "step" : undefined}
          className={cn(
            "relative grid min-h-[78px] grid-cols-[22px_minmax(0,1fr)_auto] gap-3.5",
            // The connector: 2px, from just under this mark to the next one.
            // Never after the last phase — a line into nothing reads as a
            // sixth phase that failed to render.
            i < phases.length - 1 &&
              "before:absolute before:top-[18px] before:bottom-[-2px] before:left-[7px] before:w-0 before:border-l-2 before:border-dashed before:border-border-control/60 before:content-['']",
          )}
        >
          <span
            aria-hidden
            className={cn(
              "relative z-10 mt-0.5 size-4 rounded-pill border-4",
              MARK[phase.state],
            )}
          />
          <div className="min-w-0">
            <strong className="block text-ui font-semibold text-text">{phase.label}</strong>
            {phase.evidence && (
              <small className="mt-1.5 block text-caption leading-[1.5] text-text-secondary">
                {phase.evidence}
              </small>
            )}
            {phase.percent !== undefined && (
              <Progress
                value={phase.percent}
                tone={phase.percentTone ?? "info"}
                size="thin"
                className="mt-2.5"
                // The bar belongs to ONE phase, and since 2026-08-06 it
                // announces its value -- a number with no subject ("34") is a
                // worse answer than the indeterminate bar it replaced. The
                // phase's own label is the subject, and it is right above it.
                aria-label={`${phase.label} progress`}
              />
            )}
          </div>
          {phase.at && (
            <span className="font-numeric text-caption whitespace-nowrap text-text-secondary [font-variant-numeric:lining-nums_tabular-nums]">
              {phase.at}
            </span>
          )}
        </li>
      ))}
    </ol>
  );
}
