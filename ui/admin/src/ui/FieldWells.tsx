/**
 * Direct manipulation of a composition — one vocabulary, two surfaces.
 *
 * Ratified by `visualization-and-rendering.md`, *Composition is direct
 * manipulation, and the keyboard keeps parity* (Jean, 2026-08-15): a person
 * composes an analysis by MOVING FIELDS, not by filling forms. This file is the
 * only place that knows how that gesture works, because the Visualization
 * Builder's binding wells and the Analytics Explorer's Composition region are
 * the same act on two screens, and two implementations would drift into two
 * products.
 *
 * ## The three parts, and why they are three
 *
 *   FieldComposer   holds what is carried and applies every move. It owns the
 *                   ordering arithmetic so no screen re-derives it.
 *   DraggableField  a field where it is LISTED — in a shelf, a catalog rail, or
 *                   a pivot axis header. It is a `<button>` first and a drag
 *                   source second.
 *   FieldWell       a well, with its bound fields in order.
 *
 * A screen lays them out however its own composition requires; the Builder
 * keeps its left rail and the Explorer keeps its shelf. Only the gesture is
 * shared.
 *
 * ## THE KEYBOARD IS NOT THE FALLBACK, AND IT OFFERS EXACTLY AS MUCH
 *
 * WCAG 2.2 2.5.7 requires a single-pointer alternative to every dragging
 * action; 2.1.1 requires the whole operation from the keyboard. Both are met on
 * the SAME control: a field is picked up with Enter or Space, and every well
 * that accepts it then shows a real button that places it there — the same
 * elements the pointer drops onto. Reordering inside a well is two buttons on
 * the carried chip. Escape puts it back down. There is no move the pointer can
 * make that these cannot, which is the clause the amendment states and the one
 * a second interaction vocabulary always breaks first.
 *
 * This replaces decision D7 of story 50.4 ("keyboard first, and only"). D7 was
 * right that the keyboard path must exist whatever else is built; it was wrong
 * to conclude that a drag layer is therefore a second vocabulary. It is the
 * same vocabulary when both paths move the same fields through the same wells,
 * and that is enforced here rather than promised.
 *
 * ## NO COMPATIBILITY RULE IS INVENTED HERE
 *
 * A well takes a field when `field.role` is in `well.accepts`. Both come from
 * the server — the member's role and the family's declaration — and this file
 * only compares them. It never guesses that a thing named `date` is time, and
 * it never lets a drop through "because it looked close": a refused drop is
 * refused with the roles the well does take, said while the field is still in
 * the air.
 *
 * ## MOTION CARRIES POSITION, NEVER VALUE
 *
 * Chips animate as they are picked up, placed and removed, and a reordered list
 * slides to its new order through a FLIP measurement. Nothing here ever
 * animates a number: this component moves labels, and the surfaces it serves
 * compute no value at all. Every transition is `motion-safe:` and the FLIP hook
 * returns immediately under `prefers-reduced-motion: reduce`, so the reduced
 * setting removes the motion and none of the moves.
 */
"use client";

import {
  createContext,
  useCallback,
  useContext,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { GripVerticalIcon, XIcon } from "lucide-react";
import { cn } from "../lib/cn";

/** A field as it is moved: what the caller stores, plus what a reader needs. */
export type WellField = {
  /** The caller's own id. It is what comes back in `onChange`. */
  id: string;
  label: string;
  /** The SERVER's role for this member. Never derived from the label. */
  role: string;
  /** One line under the label — an aggregation, a Datastream, a grain. */
  hint?: ReactNode;
};

export type WellSpec = {
  name: string;
  label: string;
  /** The roles this well takes, as the server declares them. */
  accepts: string[];
  /** One line under the well's name, saying what it does to the reading. */
  hint?: ReactNode;
  /** `null` for unbounded. A full well refuses a drop and says it is full. */
  max?: number | null;
  required?: boolean;
  /** A well whose role has no source is DISABLED, NOT HIDDEN. */
  available?: boolean;
  unavailableReason?: string;
  unavailableOwner?: string;
};

/** What a completed move was. The caller decides its consequence — a
 *  re-projection or a query that must be run — because only the caller knows
 *  whether the field was in the pinned Query Spec. */
export type WellChange = {
  field: WellField;
  from: string | null;
  to: string | null;
  index: number;
};

export type WellBindings = Record<string, string[]>;

type Carried = { field: WellField; from: string | null } | null;

type ComposerContext = {
  fields: Map<string, WellField>;
  wells: WellSpec[];
  bindings: WellBindings;
  carried: Carried;
  pick: (field: WellField, from: string | null) => void;
  release: () => void;
  place: (well: string, index?: number) => void;
  move: (field: WellField, from: string | null, to: string | null, index: number) => void;
  refusal: (well: WellSpec, field: WellField) => string | null;
  announce: string;
  idPrefix: string;
};

const Composer = createContext<ComposerContext | null>(null);

function useComposer(part: string): ComposerContext {
  const context = useContext(Composer);
  if (!context) {
    throw new Error(`<${part}> must be rendered inside <FieldComposer>.`);
  }
  return context;
}

/**
 * Why a well cannot take this field, in the words of what it does take. Null
 * when it can. This is the whole compatibility surface, and it compares two
 * server declarations.
 */
function refusalFor(well: WellSpec, field: WellField, bound: string[]): string | null {
  if (well.available === false) {
    return well.unavailableReason ?? `${well.label} is unavailable here.`;
  }
  if (!well.accepts.includes(field.role)) {
    return `${well.label} takes ${well.accepts.join(" or ")}, and ${field.label} is a ${field.role}.`;
  }
  if (bound.includes(field.id)) {
    return `${field.label} is already in ${well.label}.`;
  }
  if (well.max != null && bound.length >= well.max) {
    return `${well.label} holds ${well.max === 1 ? "one field" : `${well.max} fields`}. Move one out first.`;
  }
  return null;
}

/**
 * FLIP: measure before, measure after, animate the difference away.
 *
 * A list that re-renders in a new order jumps. Jumping is what makes a
 * composition feel like a form — the reader loses which chip is which and
 * re-reads the whole well. Under `prefers-reduced-motion: reduce` this returns
 * before touching a style, so the new order simply appears.
 */
function useFlip<T extends HTMLElement>(deps: string) {
  const container = useRef<T | null>(null);
  const previous = useRef<Map<string, DOMRect>>(new Map());

  useLayoutEffect(() => {
    const node = container.current;
    if (!node) return;
    const reduced =
      typeof window !== "undefined" &&
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const items = Array.from(node.querySelectorAll<HTMLElement>("[data-flip-id]"));
    const now = new Map<string, DOMRect>();
    for (const item of items) {
      const key = item.dataset.flipId ?? "";
      const rect = item.getBoundingClientRect();
      now.set(key, rect);
      if (reduced) continue;
      const before = previous.current.get(key);
      if (!before) continue;
      const dx = before.left - rect.left;
      const dy = before.top - rect.top;
      // jsdom reports every rect as 0 — the guard keeps the hook inert there
      // rather than writing transforms no test can observe.
      if (Math.abs(dx) < 1 && Math.abs(dy) < 1) continue;
      item.style.transition = "none";
      item.style.transform = `translate(${dx}px, ${dy}px)`;
      requestAnimationFrame(() => {
        item.style.transition = "transform 160ms cubic-bezier(0.2, 0, 0, 1)";
        item.style.transform = "";
      });
    }
    previous.current = now;
  }, [deps]);

  return container;
}

export function FieldComposer({
  fields,
  wells,
  bindings,
  onChange,
  children,
  className,
}: {
  /** Every field a well may hold — the shelf's and the bound ones together. */
  fields: readonly WellField[];
  wells: readonly WellSpec[];
  bindings: WellBindings;
  /** The new bindings, and what the move was. */
  onChange: (next: WellBindings, change: WellChange) => void;
  children: ReactNode;
  className?: string;
}) {
  const idPrefix = useId();
  const [carried, setCarried] = useState<Carried>(null);
  const [announce, setAnnounce] = useState("");

  const byId = useMemo(() => new Map(fields.map((field) => [field.id, field])), [fields]);
  const wellList = useMemo(() => [...wells], [wells]);

  const pick = useCallback((field: WellField, from: string | null) => {
    setCarried({ field, from });
    setAnnounce(`${field.label} picked up. Choose a well to place it in, or press Escape.`);
  }, []);

  const release = useCallback(() => {
    setCarried((current) => {
      if (current) setAnnounce(`${current.field.label} put back down.`);
      return null;
    });
  }, []);

  const move = useCallback(
    (field: WellField, from: string | null, to: string | null, index: number) => {
      const next: WellBindings = { ...bindings };
      if (from) next[from] = (next[from] ?? []).filter((id) => id !== field.id);
      if (to) {
        const current = (next[to] ?? []).filter((id) => id !== field.id);
        const at = Math.max(0, Math.min(index, current.length));
        next[to] = [...current.slice(0, at), field.id, ...current.slice(at)];
      }
      setCarried(null);
      setAnnounce(
        to
          ? `${field.label} placed in ${wellList.find((w) => w.name === to)?.label ?? to}, position ${index + 1}.`
          : `${field.label} removed.`,
      );
      onChange(next, { field, from, to, index });
    },
    [bindings, onChange, wellList],
  );

  const place = useCallback(
    (well: string, index?: number) => {
      if (!carried) return;
      const bound = (bindings[well] ?? []).filter((id) => id !== carried.field.id);
      move(carried.field, carried.from, well, index ?? bound.length);
    },
    [bindings, carried, move],
  );

  const refusal = useCallback(
    (well: WellSpec, field: WellField) =>
      refusalFor(well, field, (bindings[well.name] ?? []).filter((id) => id !== field.id)),
    [bindings],
  );

  const context: ComposerContext = {
    fields: byId,
    wells: wellList,
    bindings,
    carried,
    pick,
    release,
    place,
    move,
    refusal,
    announce,
    idPrefix,
  };

  return (
    <Composer.Provider value={context}>
      <div
        className={className}
        data-slot="field-composer"
        data-carrying={carried ? carried.field.id : undefined}
        onKeyDown={(event) => {
          if (event.key === "Escape" && carried) {
            event.stopPropagation();
            release();
          }
        }}
      >
        {children}
        {/* One live region for the whole composition: a move announced by both
         *  the chip and the well would be read twice. */}
        <span aria-live="polite" role="status" className="sr-only" data-testid="composer-announce">
          {announce}
        </span>
      </div>
    </Composer.Provider>
  );
}

/**
 * A field where it is listed. `origin` is the well it currently sits in, or
 * null when it is listed in a shelf, a catalog rail or an axis header that owns
 * nothing.
 */
export function DraggableField({
  field,
  origin = null,
  children,
  className,
  compact = false,
  testId,
}: {
  field: WellField;
  origin?: string | null;
  /** Rendered instead of label + hint, for a rail that already draws them. */
  children?: ReactNode;
  className?: string;
  compact?: boolean;
  /** Set by the well that holds it, so the same field listed in two places --
   *  a well and the axis bar of the table it projects -- stays addressable. */
  testId?: string;
}) {
  const { carried, pick, release } = useComposer("DraggableField");
  const isCarried = carried?.field.id === field.id && carried.from === origin;

  return (
    <button
      type="button"
      draggable
      data-slot="draggable-field"
      data-field={field.id}
      data-role={field.role}
      data-carried={isCarried ? "true" : undefined}
      data-testid={testId ?? `field-${field.id}`}
      aria-pressed={isCarried}
      aria-label={
        isCarried
          ? `${field.label}, ${field.role}, picked up. Press Escape to put it down.`
          : `${field.label}, ${field.role}. Press Enter to pick it up.`
      }
      onClick={() => (isCarried ? release() : pick(field, origin))}
      onDragStart={(event) => {
        event.dataTransfer.effectAllowed = "move";
        // A payload is set because some browsers refuse a drag without one; the
        // move itself is read from context, never parsed back out of here.
        event.dataTransfer.setData("text/plain", field.label);
        pick(field, origin);
      }}
      onDragEnd={() => release()}
      className={cn(
        "group flex cursor-grab items-start gap-2 rounded-medium border border-divider-base bg-surface-light text-left outline-none",
        "motion-safe:transition-[transform,box-shadow,border-color,opacity] motion-safe:duration-150",
        "hover:border-border-control focus-visible:outline-3 focus-visible:outline-offset-2 focus-visible:outline-focus",
        "data-[carried=true]:cursor-grabbing data-[carried=true]:border-primary data-[carried=true]:bg-primary-container",
        "data-[carried=true]:motion-safe:scale-[1.02] data-[carried=true]:shadow-[0_0_0_3px_color-mix(in_srgb,var(--color-primary)_13%,transparent)]",
        compact ? "px-2 py-1.5" : "px-3 py-2",
        className,
      )}
    >
      <GripVerticalIcon aria-hidden className="mt-0.5 size-4 shrink-0 text-text-secondary" />
      <span className="min-w-0 flex-1">
        {children ?? (
          <>
            <span className="block truncate text-label font-label text-text">{field.label}</span>
            {field.hint && (
              <span className="block truncate text-caption text-text-secondary">{field.hint}</span>
            )}
          </>
        )}
      </span>
    </button>
  );
}

/**
 * One well, with what it holds in order.
 *
 * While a field is carried the well renders its own placement button — the same
 * element the pointer drops onto — so the two paths are not two code paths. A
 * well that refuses the carried field says why THERE, next to the gesture,
 * rather than after the drop has failed.
 */
export function FieldWell({
  well,
  /** A sentence the caller adds under the well: what changing it costs. */
  consequence,
  className,
  emptyHint,
  /** Server refusals about THIS well. Rendered inside it, because a refusal
   *  read anywhere but on the well it is about is a page banner. */
  refusals,
  /** The test-id and label-id namespace, when the SAME well is rendered twice
   *  on one screen — the Composition region and the axis bar of the table it
   *  projects. Same well, same bindings, two places it can be operated from. */
  instance = "well",
  /** One row instead of a column, for a well shown beside the table it
   *  projects. The SAME well — the hint is dropped because the table beside it
   *  is the explanation — never a second control over the same fact. */
  compact = false,
}: {
  well: WellSpec;
  consequence?: ReactNode;
  className?: string;
  emptyHint?: string;
  refusals?: ReactNode;
  instance?: string;
  compact?: boolean;
}) {
  const { fields, bindings, carried, refusal, move, place } = useComposer("FieldWell");
  const tid = `${instance}-${well.name}`;
  const [over, setOver] = useState(false);
  // The chip the carried field would be inserted BEFORE. Drawn while the field
  // is in the air, so the drop never lands somewhere the person did not see.
  const [overIndex, setOverIndex] = useState<number | null>(null);
  const bound = bindings[well.name] ?? [];
  const listRef = useFlip<HTMLUListElement>(`${well.name}:${bound.join(",")}`);
  const refused = carried ? refusal(well, carried.field) : null;
  const unavailable = well.available === false;

  const drop = (index?: number) => {
    setOver(false);
    setOverIndex(null);
    if (!carried || refused) return;
    place(well.name, index);
  };

  /** `place` inserts into the list WITHOUT the carried field; a chip position
   *  after the carried field's own slot shifts by one when the move stays in
   *  this well. */
  const insertionIndex = (index: number) => {
    if (carried && carried.from === well.name) {
      const at = bound.indexOf(carried.field.id);
      if (at !== -1 && at < index) return index - 1;
    }
    return index;
  };

  return (
    <section
      data-slot="field-well"
      data-well={well.name}
      data-over={over && !refused ? "true" : undefined}
      data-refuses={carried && refused ? "true" : undefined}
      data-testid={tid}
      aria-labelledby={`${tid}-label`}
      // A well with no source is disabled and SAYS SO to a screen reader, with
      // the reason as its description -- not merely drawn paler.
      aria-disabled={unavailable ? "true" : undefined}
      aria-describedby={unavailable ? `${tid}-reason` : undefined}
      onDragOver={(event) => {
        if (unavailable) return;
        event.preventDefault();
        event.dataTransfer.dropEffect = refused ? "none" : "move";
        setOver(true);
      }}
      onDragLeave={() => {
        setOver(false);
        setOverIndex(null);
      }}
      onDrop={(event) => {
        event.preventDefault();
        drop();
      }}
      className={cn(
        "flex flex-col gap-2 rounded-large border border-dashed border-divider-base bg-surface-light p-3",
        compact && "gap-1.5 p-2",
        "motion-safe:transition-[border-color,background-color] motion-safe:duration-150",
        "data-[over=true]:border-solid data-[over=true]:border-primary data-[over=true]:bg-primary-container",
        "data-[refuses=true]:border-destructive",
        unavailable && "opacity-60",
        className,
      )}
    >
      <div className="flex items-baseline justify-between gap-3">
        <span id={`${tid}-label`} className="text-ui font-semibold text-text">
          {well.label}
        </span>
        <span className="text-caption text-text-secondary">
          {well.accepts.join(" or ")}
          {well.required ? " · required" : ""}
          {well.max != null ? ` · up to ${well.max}` : ""}
        </span>
      </div>
      {well.hint && !compact && (
        <p className="m-0 text-caption text-text-secondary">{well.hint}</p>
      )}

      {unavailable ? (
        <p
          id={`${tid}-reason`}
          className="m-0 text-caption text-text-secondary"
          data-testid={`${tid}-unavailable`}
        >
          {well.unavailableReason ?? `${well.label} is unavailable.`}
          {well.unavailableOwner ? ` Owner: ${well.unavailableOwner}.` : ""}
        </p>
      ) : (
        <>
          <ul
            ref={listRef}
            className={cn(
              "m-0 flex list-none gap-1.5 p-0",
              compact ? "flex-wrap items-center" : "flex-col",
            )}
          >
            {bound.map((id, index) => {
              const field = fields.get(id);
              if (!field) return null;
              const isCarried = carried?.field.id === id && carried.from === well.name;
              return (
                <li
                  key={id}
                  data-flip-id={id}
                  className="relative flex items-center gap-1"
                  onDragOver={(event) => {
                    if (unavailable) return;
                    event.preventDefault();
                    event.dataTransfer.dropEffect = refused ? "none" : "move";
                    if (!refused) setOverIndex(index);
                  }}
                  onDragLeave={() =>
                    setOverIndex((current) => (current === index ? null : current))
                  }
                  onDrop={(event) => {
                    event.preventDefault();
                    event.stopPropagation();
                    drop(insertionIndex(index));
                  }}
                >
                  {overIndex === index && carried && !refused && (
                    <span
                      aria-hidden
                      data-testid={`${tid}-insertion-${index}`}
                      className={cn(
                        "pointer-events-none absolute rounded-full bg-primary",
                        compact
                          ? "-left-1 bottom-0 top-0 w-0.5"
                          : "-top-1 left-0 right-0 h-0.5",
                      )}
                    />
                  )}
                  <DraggableField
                    field={field}
                    origin={well.name}
                    compact
                    testId={`${tid}-field-${id}`}
                    className={compact ? "max-w-56" : "min-w-0 flex-1"}
                  />
                  {/* The single-pointer, keyboard-reachable equivalent of
                   *  dragging within the well. Shown on the carried chip so an
                   *  idle well stays readable. */}
                  {isCarried && (
                    <>
                      <button
                        type="button"
                        disabled={index === 0}
                        onClick={() => move(field, well.name, well.name, index - 1)}
                        data-testid={`${tid}-up-${id}`}
                        aria-label={`Move ${field.label} earlier in ${well.label}`}
                        className="rounded-medium border border-divider-base px-2 py-1 text-caption text-text disabled:opacity-40"
                      >
                        ↑
                      </button>
                      <button
                        type="button"
                        disabled={index === bound.length - 1}
                        onClick={() => move(field, well.name, well.name, index + 1)}
                        data-testid={`${tid}-down-${id}`}
                        aria-label={`Move ${field.label} later in ${well.label}`}
                        className="rounded-medium border border-divider-base px-2 py-1 text-caption text-text disabled:opacity-40"
                      >
                        ↓
                      </button>
                    </>
                  )}
                  <button
                    type="button"
                    onClick={() => move(field, well.name, null, 0)}
                    data-testid={`${tid}-remove-${id}`}
                    aria-label={`Remove ${field.label} from ${well.label}`}
                    className="rounded-medium border border-transparent p-1 text-text-secondary hover:border-divider-base hover:text-text"
                  >
                    <XIcon aria-hidden className="size-4" />
                  </button>
                </li>
              );
            })}
          </ul>

          {bound.length === 0 && !carried && (
            <p className="m-0 text-caption text-text-secondary" data-testid={`${tid}-empty`}>
              {emptyHint ?? `Drag a ${well.accepts.join(" or ")} here, or pick one up and place it.`}
            </p>
          )}

          {carried &&
            (refused ? (
              <p
                className="m-0 text-caption text-destructive"
                data-testid={`${tid}-refusal`}
              >
                {refused}
              </p>
            ) : (
              <button
                type="button"
                onClick={() => drop()}
                data-testid={`${tid}-place`}
                className={cn(
                  "rounded-medium border border-primary bg-primary-container px-3 py-2 text-label font-label text-text",
                  "motion-safe:transition-colors focus-visible:outline-3 focus-visible:outline-offset-2 focus-visible:outline-focus",
                )}
              >
                Place {carried.field.label} in {well.label}
              </button>
            ))}
        </>
      )}

      {refusals}

      {consequence && (
        <p className="m-0 text-caption text-text-secondary" data-testid={`${tid}-consequence`}>
          {consequence}
        </p>
      )}
    </section>
  );
}

/** What the carried field is, for a screen that wants to say it in its own
 *  words (a toolbar hint, a pivot header's affordance). */
export function useCarriedField(): WellField | null {
  const context = useContext(Composer);
  return context?.carried?.field ?? null;
}

/**
 * The place unbound fields are listed — the Explorer's shelf, the Builder's
 * catalog rail — as a DROP TARGET. Dropping a bound field here unbinds it, the
 * same consequence as that field's remove button on its well
 * (`visualization-and-rendering.md`, *The pointer places at a position, and the
 * shelf takes a field back*, 2026-08-17). While a bound field is carried, the
 * shelf shows the return control the pointer drops onto — the keyboard's parity
 * for this gesture, on the same element.
 *
 * A field carried FROM the shelf is not removable — it is already unbound — so
 * the shelf neither lights up nor offers the button for it.
 */
export function FieldShelf({
  children,
  className,
  testId = "field-shelf",
}: {
  children: ReactNode;
  className?: string;
  testId?: string;
}) {
  const { carried, move } = useComposer("FieldShelf");
  const [over, setOver] = useState(false);
  const removable = carried !== null && carried.from !== null;

  const putBack = () => {
    setOver(false);
    if (!removable || !carried) return;
    move(carried.field, carried.from, null, 0);
  };

  return (
    <div
      data-slot="field-shelf"
      data-testid={testId}
      data-over={over && removable ? "true" : undefined}
      onDragOver={(event) => {
        if (!removable) return;
        event.preventDefault();
        event.dataTransfer.dropEffect = "move";
        setOver(true);
      }}
      onDragLeave={() => setOver(false)}
      onDrop={(event) => {
        event.preventDefault();
        putBack();
      }}
      className={cn(
        "rounded-large border border-transparent",
        "motion-safe:transition-[border-color,background-color] motion-safe:duration-150",
        "data-[over=true]:border-dashed data-[over=true]:border-primary data-[over=true]:bg-primary-container",
        className,
      )}
    >
      {children}
      {removable && carried && (
        <button
          type="button"
          onClick={putBack}
          data-testid={`${testId}-return`}
          className={cn(
            "mt-2 rounded-medium border border-primary bg-primary-container px-3 py-2 text-label font-label text-text",
            "motion-safe:transition-colors focus-visible:outline-3 focus-visible:outline-offset-2 focus-visible:outline-focus",
          )}
        >
          Return {carried.field.label} to the shelf
        </button>
      )}
    </div>
  );
}
