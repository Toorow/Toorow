/**
 * The pager — First, Previous, Next, and the sentence that says what they move.
 *
 * WHY IT EXISTS. Three screens paged three ways. `DataCollectionLayout` drew
 * First / Previous / Next in a bordered footer beside a count sentence;
 * `GovernanceCollection` drew First and Next in a bare flex row with no Previous
 * at all; the Explorer's pivot drew four buttons — Previous rows, Next rows,
 * Next columns, Previous columns — in whatever order the bounds happened to
 * allow, so the two directions of one axis sat on either side of the other
 * axis's. Same gesture, three geometries, three sets of disabled rules, and
 * three answers to the question "what does a control that leads nowhere look
 * like".
 *
 * IT FETCHES NOTHING, AND IT KNOWS NOTHING ABOUT A PAGE. A cursor, an offset and
 * an address are three ways to hold the reader's place and this file holds none
 * of them: it owns the buttons, what disables them, and what they are called to
 * a screen reader. Every call site keeps its own request exactly as it was — the
 * pivot's push/pop history refs in particular are untouched by this component,
 * which is why adopting it could not move a single offset.
 *
 * ONE PAGER PER AXIS, RATHER THAN A PAGER THAT KNOWS ABOUT AXES. The pivot pages
 * rows and columns independently, so it mounts two, each naming its own `unit`.
 * Nothing here special-cases a second dimension: `unit` is the noun the buttons
 * are spelled with — "page" gives `Next page`, "rows" gives `Next rows` — so two
 * pagers on one screen cannot collide in the accessibility tree, and a third
 * axis would need no change here at all.
 *
 * THREE STATES PER DIRECTION, NOT TWO.
 *
 *   `undefined`  this pager has no such direction   — nothing is drawn
 *   `null`       it has one and there is nowhere to go — drawn, disabled
 *   a handler    drawn, enabled
 *
 * The distinction is the one a boolean cannot make. Governance pages a
 * collection with a cursor and CANNOT walk back, so it passes `undefined` for
 * Previous and no dead control appears; a Data collection can walk back and is
 * merely standing on the first page, so it passes `null` and the reader can see
 * that the way back exists. Drawing a permanently disabled control in the first
 * case would promise a gesture the screen does not have.
 */
import type { ReactNode } from "react";

import { cn } from "../lib/cn";
import { Button } from "../components/ui/button";

/**
 * One direction of a pager.
 *
 * `undefined` — not offered here. `null` — offered, nowhere to go. A function —
 * offered and reachable. See the header: the three are not interchangeable.
 */
export type PagerStep = (() => void) | null | undefined;

export interface PagerProps {
  /**
   * The noun the buttons are spelled with, singular or plural as the screen
   * says it: `page` (the default) gives "Next page", `rows` gives "Next rows".
   * It is the whole of what makes two pagers on one screen distinguishable, so
   * it is never omitted where there are two.
   */
  unit?: string;
  /** The group's name for assistive technology. Two pagers, two names. */
  label?: string;
  onFirst?: PagerStep;
  onPrevious?: PagerStep;
  onNext?: PagerStep;
  /**
   * An answer is already in flight. Every direction is disabled for its
   * duration — a second press before the first landed is how a page history
   * ends up recording a page nobody ever saw.
   */
  busy?: boolean;
  /**
   * What these buttons are moving through, in the reader's words: how many of
   * what is shown, and — on a sorted collection — that the order covers this
   * page only. It sits BESIDE the buttons rather than above the table, because
   * the sentence is about the gesture and a reader reaching for Next is looking
   * here.
   */
  children?: ReactNode;
  className?: string;
  "data-testid"?: string;
}

/** One button, and the two reasons it can be dead. */
function Step({ step, name, busy }: { step: PagerStep; name: string; busy: boolean }) {
  if (step === undefined) return null;
  return (
    <Button
      type="button"
      size="sm"
      variant="secondary"
      disabled={busy || step === null}
      onClick={step ?? undefined}
    >
      {name}
    </Button>
  );
}

/**
 * The control. The scope sentence and the buttons are the two halves of one
 * footer: on a wide screen they sit at either end, on a narrow one they wrap,
 * and a pager with no sentence is just the buttons.
 */
export function Pager({
  unit = "page",
  label = "Pagination",
  onFirst,
  onPrevious,
  onNext,
  busy = false,
  children,
  className,
  "data-testid": testId,
}: PagerProps) {
  // A pager with no direction at all is not a pager. It happens — a collection
  // whose whole answer fits on one screen — and drawing an empty button row
  // there would be a frame around nothing.
  if (onFirst === undefined && onPrevious === undefined && onNext === undefined) return null;
  return (
    <div
      className={cn("flex flex-wrap items-center justify-between gap-3", className)}
      data-testid={testId}
    >
      {children ? (
        <div className="flex flex-col gap-1 text-caption text-text-secondary">{children}</div>
      ) : null}
      <div className="flex gap-2" role="group" aria-label={label}>
        <Step step={onFirst} name={`First ${unit}`} busy={busy} />
        <Step step={onPrevious} name={`Previous ${unit}`} busy={busy} />
        <Step step={onNext} name={`Next ${unit}`} busy={busy} />
      </div>
    </div>
  );
}
