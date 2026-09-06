/**
 * ChoiceGroup — a select whose options are all visible at once.
 *
 * Jean, 2026-07-29: *"we miss a component like the select but where we can see
 * option same as provided"*. That is a real gap, and it is not the same object
 * as either of the two it sits between:
 *
 *   Select        one of many, options hidden until asked for. Use when the
 *                 list is long or the choice is rare.
 *   ChoiceGroup   one of a few, every option readable without a click. Use
 *                 when the options ARE the information — the reader learns
 *                 what is possible by looking.
 *   Tabs          not a choice at all: it changes what you are looking at,
 *                 not what you are setting.
 *
 * It is a `RadioGroup` underneath, not a row of buttons, so it keeps the
 * semantics and the keyboard behaviour of the choice it is: arrow keys move
 * between options, only one is tabbable, and a screen reader says "radio group,
 * 2 of 4". A row of `<button>`s would look identical and be none of that.
 *
 * Geometry is the mockup's pill button — `application-v3.css` l. 209-234, the
 * same 40px/999px/13px-700 as every other control, so a ChoiceGroup and a
 * toolbar read as one family. The selected option inverts to the ink surface
 * rather than the accent: rose marks the recommended direction, and which
 * option you already picked is not a recommendation.
 *
 * `overflow` is the trailing `…` from the reference — for the options that do
 * not fit. It takes a node so the screen decides what opening it means (a
 * menu, a dialog); the component does not invent that.
 *
 * ## Two densities, one object — and a compact cut of the card
 *
 * `variant="pill"` is the compact row above. `variant="card"` is the detailed
 * choice — a bordered card with the radio mark, a title and a line of
 * explanation — and it is not an invention either: it is `.source-choice` in
 * the validated `mockups/datastream-create.html`, the control that picks
 * Connector report / Existing BigQuery / Managed feed.
 *
 * `density="compact"` (card only, 2026-08-10, 57.12 UX pass) is for the
 * catalogue case: thirty-nine detailed cards are a wall nobody reads, and the
 * ratified three-zone wizard leaves ~372px of task area at 1280px, where a
 * detailed card collapses to one word per line (measured live). The compact
 * card is one row — mark, icon, label, optional `badge` — so a grid of them
 * scans like a list of products. The `hint` is reserved for the exceptional
 * card (a stale pin) and takes a second row when it exists; the ordinary card
 * carries none, which is why compact has no wall. The naming rule below is
 * unchanged: the label stays the whole accessible name.
 *
 *     .source-choice          { min-height: 158px; padding: 18px;
 *                               border: 1px solid var(--line); border-radius: 16px }
 *     .source-choice.selected { border-color: var(--rose);
 *                               box-shadow: 0 0 0 3px rgba(255,153,200,.13) }
 *     .source-choice strong   { font: 600 15px Lexend }
 *     .source-choice p        { margin-top: 7px; color: var(--muted); font-size: 13px }
 *
 * The card is where rose IS right on the selected state, and the mockup says
 * so: a detailed choice is a decision you are being walked through, so the
 * selection and the recommended direction are the same thing. The pill sits in
 * a settled form, where they are not — which is why the two differ.
 *
 * ## The accessible name is the LABEL, never the label plus its explanation
 *
 * Story 57.5. Radix computes a radio's accessible name from its content, so a
 * `hint` turned `Connector pull` into `Connector pull Select a provider
 * report…` — every `getByRole("radio", { name })` in the console broke at once,
 * and so did every spoken announcement, which read a paragraph where a choice
 * was expected. The label is therefore stated as `aria-label` and the hint is
 * attached as `aria-describedby`: a description is what a screen reader reads
 * AFTER the name, on request, which is exactly what a hint is.
 */
"use client";

import { useId, type ReactNode } from "react";
import { CheckIcon } from "lucide-react";
import { RadioGroup as RadioGroupPrimitive } from "radix-ui";
import { cn } from "../lib/cn";

export type Choice = {
  value: string;
  label: ReactNode;
  /** One line under the label, when the option needs explaining. */
  hint?: ReactNode;
  /**
   * The mockup's `.choice-icon`, above the title on a card. Decorative and
   * hidden from assistive technology: it repeats the label, it does not add to
   * it. Ignored by `variant="pill"`, which has no room for one.
   */
  icon?: ReactNode;
  /** A short factual state, inline after the label on a compact card
   *  (`Authorized`). Decorative — the label alone stays the accessible name. */
  badge?: ReactNode;
  disabled?: boolean;
};

export function ChoiceGroup({
  id,
  choices,
  value,
  defaultValue,
  onValueChange,
  overflow,
  variant = "pill",
  density = "default",
  className,
  "aria-label": ariaLabel,
}: {
  /** The group element's own id, for a `Field` that labels it. */
  id?: string;
  choices: readonly Choice[];
  value?: string;
  defaultValue?: string;
  onValueChange?: (value: string) => void;
  /** The trailing `…` affordance, when not every option fits. */
  overflow?: ReactNode;
  /** `pill` for a settled form; `card` when the choice needs explaining. */
  variant?: "pill" | "card";
  /** Card only: `compact` is one row (mark, icon, label) for catalogues;
   *  the hint is the caller's to place, it is not rendered. */
  density?: "default" | "compact";
  className?: string;
  "aria-label"?: string;
}) {
  const groupId = useId();
  if (variant === "card" && density === "compact") {
    return (
      <RadioGroupPrimitive.Root
        id={id}
        data-slot="choice-group"
        data-variant="card"
        data-density="compact"
        aria-label={ariaLabel}
        value={value}
        defaultValue={defaultValue}
        onValueChange={onValueChange}
        className={cn("grid gap-2 sm:grid-cols-2", className)}
      >
        {choices.map((choice, index) => {
          const hintId = choice.hint ? `${groupId}-hint-${index}` : undefined;
          return (
          <RadioGroupPrimitive.Item
            key={choice.value}
            value={choice.value}
            disabled={choice.disabled}
            data-slot="choice"
            aria-label={typeof choice.label === "string" ? choice.label : undefined}
            aria-describedby={hintId}
            className={cn(
              // NO RADIO CIRCLE in compact: the rose border, halo and surface
              // already say "selected" (the same three the detailed card
              // uses), and the circle cost the 24px a product name needs —
              // measured: at 2 columns every "Google …" label truncated to
              // the same "Google …". The label WRAPS instead of truncating.
              "group grid grid-cols-[auto_minmax(0,1fr)] items-center gap-2 rounded-large border border-divider-base bg-surface-light px-2.5 py-2 text-left transition-colors outline-none",
              "hover:border-border-control",
              "focus-visible:outline-3 focus-visible:outline-offset-2 focus-visible:outline-focus",
              "disabled:pointer-events-none disabled:opacity-50",
              "data-[state=checked]:border-primary data-[state=checked]:bg-primary-container",
              "data-[state=checked]:shadow-[0_0_0_3px_color-mix(in_srgb,var(--color-primary)_13%,transparent)]",
            )}
          >
            {choice.icon && <span aria-hidden className="flex text-text-secondary group-data-[state=checked]:text-primary">{choice.icon}</span>}
            <span className="flex min-w-0 items-center gap-2">
              <span className="min-w-0 text-label font-label leading-snug text-text">{choice.label}</span>
              {choice.badge && <span aria-hidden className="shrink-0">{choice.badge}</span>}
            </span>
            {/* Rare enough to earn a second row when it exists (a stale pin);
             *  absent on the ordinary card, which is why compact has no wall. */}
            {choice.hint && (
              <span id={hintId} className="col-span-full text-caption text-text-secondary">
                {choice.hint}
              </span>
            )}
          </RadioGroupPrimitive.Item>
          );
        })}
      </RadioGroupPrimitive.Root>
    );
  }
  if (variant === "card") {
    return (
      <RadioGroupPrimitive.Root
        id={id}
        data-slot="choice-group"
        data-variant="card"
        aria-label={ariaLabel}
        value={value}
        defaultValue={defaultValue}
        onValueChange={onValueChange}
        className={cn("grid gap-3.5 sm:grid-cols-2", className)}
      >
        {choices.map((choice, index) => {
          const hintId = choice.hint ? `${groupId}-hint-${index}` : undefined;
          return (
          <RadioGroupPrimitive.Item
            key={choice.value}
            value={choice.value}
            disabled={choice.disabled}
            data-slot="choice"
            aria-label={typeof choice.label === "string" ? choice.label : undefined}
            aria-describedby={hintId}
            className={cn(
              "group grid grid-cols-[20px_minmax(0,1fr)] items-start gap-3 rounded-large border border-divider-base bg-surface-light p-4 text-left transition-colors outline-none",
              "hover:border-border-control",
              "focus-visible:outline-3 focus-visible:outline-offset-2 focus-visible:outline-focus",
              "disabled:pointer-events-none disabled:opacity-50",
              // The mockup's selected card: rose edge, rose halo, rose surface.
              "data-[state=checked]:border-primary data-[state=checked]:bg-primary-container",
              "data-[state=checked]:shadow-[0_0_0_3px_color-mix(in_srgb,var(--color-primary)_13%,transparent)]",
            )}
          >
            <span
              aria-hidden
              className={cn(
                "mt-0.5 grid size-5 place-items-center rounded-pill border-2 border-border-control bg-surface-light text-on-primary transition-colors",
                "group-data-[state=checked]:border-primary group-data-[state=checked]:bg-primary",
              )}
            >
              <CheckIcon className="size-3 opacity-0 [stroke-width:3.5] group-data-[state=checked]:opacity-100" />
            </span>
            <span className="min-w-0">
              {choice.icon && (
                <span
                  aria-hidden
                  className="mb-2 flex text-text-secondary group-data-[state=checked]:text-primary"
                >
                  {choice.icon}
                </span>
              )}
              <span className="block font-display text-ui font-semibold text-text">{choice.label}</span>
              {choice.hint && (
                <span
                  id={hintId}
                  className="mt-1.5 block text-label leading-[1.5] font-normal text-text-secondary"
                >
                  {choice.hint}
                </span>
              )}
            </span>
          </RadioGroupPrimitive.Item>
          );
        })}
      </RadioGroupPrimitive.Root>
    );
  }

  return (
    <RadioGroupPrimitive.Root
      id={id}
      data-slot="choice-group"
      data-variant="pill"
      aria-label={ariaLabel}
      value={value}
      defaultValue={defaultValue}
      onValueChange={onValueChange}
      className={cn("flex flex-wrap items-stretch gap-2.5", className)}
    >
      {choices.map((choice, index) => {
        const hintId = choice.hint ? `${groupId}-hint-${index}` : undefined;
        return (
        <RadioGroupPrimitive.Item
          key={choice.value}
          value={choice.value}
          disabled={choice.disabled}
          data-slot="choice"
          // The same naming rule as the card above: one component, one answer to
          // "what is this option called".
          aria-label={typeof choice.label === "string" ? choice.label : undefined}
          aria-describedby={hintId}
          className={cn(
            "group flex min-h-control-height flex-col justify-center rounded-pill border border-divider-base bg-surface-light px-4 py-1.5 text-left shadow-card-light transition-colors outline-none",
            "hover:border-border-control hover:bg-background-light",
            "focus-visible:outline-3 focus-visible:outline-offset-2 focus-visible:outline-focus",
            "disabled:pointer-events-none disabled:opacity-50",
            // Ink, not rose: this says "the one you chose", not "the one we
            // recommend". The two must not look the same.
            "data-[state=checked]:border-text data-[state=checked]:bg-text",
          )}
        >
          <span className="text-label font-label text-text group-data-[state=checked]:text-text-on-dark">
            {choice.label}
          </span>
          {choice.hint && (
            <span
              id={hintId}
              className="text-caption text-text-secondary group-data-[state=checked]:text-text-on-dark/80"
            >
              {choice.hint}
            </span>
          )}
        </RadioGroupPrimitive.Item>
        );
      })}
      {overflow && <div className="flex items-center">{overflow}</div>}
    </RadioGroupPrimitive.Root>
  );
}
