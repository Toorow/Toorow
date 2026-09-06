/**
 * Field — the label/control/message wiring, and nothing else.
 *
 * The controls themselves are the shadcn ones: `Input`, `Select`, `Checkbox`,
 * `RadioGroup`, `Switch`. This file used to carry a second `Select` and a
 * second `Checkbox` next to them, which is the defect the migration exists to
 * remove — so they are gone, and only the part shadcn has no counterpart for
 * remains: the `id` / `aria-describedby` / `aria-invalid` association, which is
 * left to chance everywhere it is written by hand.
 *
 * The focus ring is defined on the controls, once each: `outline,
 * outline-offset` was written 25 times across 15 stylesheets.
 */
import type { ReactNode } from "react";
import { useId } from "react";
import { cn } from "../lib/cn";
import { Status } from "./Data";
import { INVALID, TONE_TEXT } from "./tone";

export type FieldProps = {
  label: ReactNode;
  /** Shown under the control. Use for the rule, not for an error. */
  hint?: ReactNode;
  /** When set, the control is marked invalid and this replaces the hint. */
  error?: ReactNode;
  required?: boolean;
  children: (props: { id: string; "aria-describedby"?: string; "aria-invalid"?: true }) => ReactNode;
};

/**
 * Wraps a control with its label and message, wiring `id`/`aria-describedby`
 * so the association is never left to chance.
 */
export function Field({ label, hint, error, required, children }: FieldProps) {
  const id = useId();
  const messageId = `${id}-message`;
  const message = error ?? hint;
  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={id} className="text-label font-label text-text">
        {label}
        {required && <span aria-hidden className={cn("ml-1", TONE_TEXT[INVALID])}>*</span>}
      </label>
      {children({
        id,
        "aria-describedby": message ? messageId : undefined,
        "aria-invalid": error ? true : undefined,
      })}
      {/* An invalid field IS a status at its smallest density, so it is a
          `Status`, not a lookalike built here out of a dot and a colour. The
          hand-rolled version this replaces is exactly how the console ended up
          with one object under several names. */}
      {error ? (
        <Status tone={INVALID} className="text-caption" id={messageId}>
          {error}
        </Status>
      ) : (
        hint && (
          <p id={messageId} className="m-0 text-caption text-text-secondary">
            {hint}
          </p>
        )
      )}
    </div>
  );
}
