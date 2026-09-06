/**
 * Stepper — the wizard rail: where you are in a task you are performing.
 *
 * Distinct from `PhaseTimeline`, which reports an operation the system is
 * running. A stepper is navigation through your own work; a phase timeline is
 * evidence of the machine's. They look alike and mean opposite things, so:
 *
 *   Stepper        you are doing this. Steps are numbered, revisitable, and
 *                  carry the decision each one asks for.
 *   PhaseTimeline  the system did this. Phases carry evidence and a time, and
 *                  you cannot go back into one.
 *
 * ## The orientation is a ratified decision, and Jean's reference differs
 *
 * `docs/product-architecture/datastream-workbench-and-wizard.md` l. 31:
 *
 *   > At desktop width, use a **persistent left stepper**, a central task area,
 *   > and a right `Configuration summary` panel. […] **Completed steps are
 *   > directly revisitable.** Sections with no operator decision remain visible
 *   > as `Automatic` evidence but do not create an extra stop.
 *
 * So the contracted rail for the Datastream wizard is **vertical and
 * persistent on the left**. It carries five sections since story 57.9 removed
 * the one that asked for nothing, and none of the five is `Automatic` — the
 * flag stays a capability of this primitive, used by other rails. The
 * reference Jean sent on 2026-07-29 is horizontal. Both are built here —
 * `orientation` defaults to `vertical`, which is the ratified one — and the
 * divergence is named rather than resolved in silence. If the horizontal rail
 * is an amendment to that document, say so and it becomes the default.
 *
 * `automatic` is the doc's own concept: a section with no operator decision
 * stays visible as evidence and is not a stop. It renders as a step you cannot
 * click, labelled, so the map stays honest about how many decisions are left.
 */
import type { ReactNode } from "react";
import { CheckIcon } from "lucide-react";
import { cn } from "../lib/cn";
import { FILL_BG, FILL_ON } from "./tone";

export type Step = {
  /** The decision this step asks for: `Choose where data comes from`. */
  label: ReactNode;
  /** One line on what it settles. The reference calls for it; so does the doc. */
  detail?: ReactNode;
  state: "done" | "current" | "todo";
  /**
   * No operator decision — the doc's `Automatic` section. Visible as evidence,
   * never a stop, never clickable.
   */
  automatic?: boolean;
  /** Completed steps are directly revisitable (the doc requires it). */
  onSelect?: () => void;
};

/**
 * The three step marks, READ FROM THE SCALE (story 76-2). `done` is a state and
 * takes the success fill; `current` is not a state at all — it is the step you
 * are on, which is what `accent` means in this file's scale — and `todo` is the
 * page surface, no tone.
 */
const MARK: Record<Step["state"], string> = {
  done: `border-transparent ${FILL_BG.success} ${FILL_ON.success}`,
  current: `border-transparent ${FILL_BG.accent} ${FILL_ON.accent}`,
  todo: "border-divider-base bg-background-light text-text-secondary",
};

export function Stepper({
  steps,
  orientation = "vertical",
  className,
}: {
  steps: Step[];
  /** `vertical` is the ratified rail. See the note above before changing it. */
  orientation?: "vertical" | "horizontal";
  className?: string;
}) {
  const vertical = orientation === "vertical";
  return (
    <ol
      data-slot="stepper"
      data-orientation={orientation}
      className={cn(
        "m-0 list-none p-0",
        // The horizontal rail WRAPS. `auto-cols-fr grid-flow-col` gave one rigid
        // row: nine steps in it are 9 columns at any width, so the Datastream
        // processing chain (`pages/ProcessingChainPanel.tsx`) crushed to ~90px a
        // step below 1200px and its labels became one word per line. `auto-fit`
        // keeps the single row while the steps fit — a 5-step wizard rail is
        // unchanged at every desktop width — and breaks to a second row instead
        // of squeezing. A rail that only reads at one viewport is not a rail.
        vertical
          ? "grid gap-0"
          : "grid grid-cols-[repeat(auto-fit,minmax(8rem,1fr))] gap-5",
        className,
      )}
    >
      {steps.map((step, i) => {
        const clickable = step.onSelect && !step.automatic && step.state !== "current";
        const Tag = clickable ? "button" : "div";
        return (
          <li
            key={i}
            aria-current={step.state === "current" ? "step" : undefined}
            className={cn(
              "relative",
              vertical
                ? "grid grid-cols-[24px_minmax(0,1fr)] gap-3 pb-5 last:pb-0"
                : "flex flex-col gap-2.5",
              // The connector. Vertical runs between the marks; horizontal is
              // the active underline the reference draws, which doubles as the
              // rail because a numbered row needs no line to read as a sequence.
              vertical &&
                i < steps.length - 1 &&
                "before:absolute before:top-[26px] before:bottom-1 before:left-[11px] before:w-0.5 before:bg-divider-base before:content-['']",
            )}
          >
            {!vertical && (
              <span
                aria-hidden
                className={cn(
                  "h-0.5 w-full rounded-pill",
                  step.state === "current"
                    ? FILL_BG.accent
                    : step.state === "done"
                      ? FILL_BG.success
                      : "bg-divider-base",
                )}
              />
            )}
            <Tag
              {...(clickable ? { type: "button" as const, onClick: step.onSelect } : {})}
              className={cn(
                "text-left outline-none",
                vertical ? "contents" : "flex items-start gap-2.5",
                clickable && "cursor-pointer focus-visible:outline-3 focus-visible:outline-offset-2 focus-visible:outline-focus",
              )}
            >
              <span
                aria-hidden
                className={cn(
                  "relative z-10 grid size-6 shrink-0 place-items-center rounded-pill border text-caption font-label",
                  MARK[step.state],
                )}
              >
                {step.state === "done" ? <CheckIcon className="size-3 [stroke-width:3]" /> : i + 1}
              </span>
              <span className="min-w-0">
                <span
                  className={cn(
                    "block text-label",
                    step.state === "todo" ? "font-normal text-text-secondary" : "text-text",
                  )}
                >
                  {step.label}
                </span>
                {step.detail && (
                  <span className="mt-0.5 block text-caption leading-[1.45] text-text-secondary">
                    {step.detail}
                  </span>
                )}
                {step.automatic && (
                  <span className="mt-1 inline-flex items-center rounded-pill bg-background-light px-2 py-0.5 text-caption text-text-secondary">
                    Automatic
                  </span>
                )}
              </span>
            </Tag>
          </li>
        );
      })}
    </ol>
  );
}
