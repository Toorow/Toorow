/**
 * Data display — what shadcn has no counterpart for.
 *
 * The table, the badge and the tone scale used to live here as a second
 * implementation next to the shadcn ones. They are gone: `Table`/`Th`/`Td`
 * are `components/ui/table.tsx`, `Badge` is `components/ui/badge.tsx`, and the
 * tone scale is `ui/tone.ts`. What remains is the three objects the registry
 * does not have — `Status`, `Metric`, `EmptyState` — plus `TableScroll`, the
 * keyboard-focusable scroll region the console's wide tables need.
 *
 * Geometry from the validated v3 mockup (`application-v3.css`):
 *   .signal  { width: 8px; height: 8px; border-radius: 50%;
 *              box-shadow: 0 0 0 4px rgba(<tone>, .14) }      (l. 281)
 *   .metric  { min-height: 92px; padding: 20px }               (l. 317)
 *            label 13px muted · value 24px Geist 600 tabular · hint 12px muted
 */
import type { ReactNode, HTMLAttributes, CSSProperties } from "react";
import { cn } from "../lib/cn";
import { EDGE_BACKGROUND, EDGE_CLASS, HALO, TONE_EDGE_VARS, TONE_TEXT, type Fill } from "./tone";

/**
 * Horizontal scroller for wide tables. Keyboard-focusable and labelled, because
 * a scroll region that traps content away from keyboard users fails the desktop
 * accessibility requirement the console is held to.
 */
export function TableScroll({ label, className, ...rest }: HTMLAttributes<HTMLDivElement> & { label: string }) {
  return (
    <div
      role="region"
      aria-label={label}
      tabIndex={0}
      className={cn("overflow-x-auto focus-visible:outline-3 focus-visible:outline-focus", className)}
      {...rest}
    />
  );
}

/**
 * Status — one object, three densities.
 *
 * An inline dot in a table cell, a banner at the top of a screen and a toast are
 * the same statement about the same thing; they differ only in how much room
 * they get. Splitting them into `Signal` and `Alert` gave the console two tone
 * scales for one meaning, which is how six different ways of saying
 * green/amber/red ended up in the stylesheets. Toasts use the same `tone` and
 * are dispatched through `notify()`, never styled again at the call site.
 *
 * The tone is a `Fill`, so `accent` is available here too: a status can say
 * "this is the recommended direction" — the step to take, the field to fix —
 * and that is the same object as a rose dot anywhere else, not a lookalike.
 */
export function Status({
  tone = "neutral",
  as = "inline",
  active = false,
  title,
  action,
  children,
  className,
  id,
  // A test hook, and the only prop here that is not a design decision. A
  // status is frequently the thing a screen's test identifies — "the
  // last-known-good banner is showing" — and without this the screen has to
  // wrap it in a div that exists for no other reason.
  "data-testid": testId,
}: {
  tone?: Fill;
  "data-testid"?: string;
  /** So a status can be the target of an `aria-describedby` — `Field` needs it. */
  id?: string;
  /** `inline` fits a table cell or a header; `block` is a banner with room for a title. */
  as?: "inline" | "block";
  /**
   * The thing is happening right now — a run collecting, a backfill in flight.
   * The halo breathes instead of sitting still, which is the difference between
   * "this is the state" and "this is changing under you". Stops dead under
   * `prefers-reduced-motion`; the colour still carries the meaning.
   */
  active?: boolean;
  title?: ReactNode;
  action?: ReactNode;
  children?: ReactNode;
  className?: string;
}) {
  // The mark, per `motion-iconography.md` § Signal: the tone, a low-opacity
  // halo, and a SHAPE — the shape is what survives a colour-blind reader and a
  // greyscale print. 16px in a dense surface, 18px standalone, exactly as the
  // spec sets it.
  const mark = (
    <span
      aria-hidden
      className={cn(
        "relative shrink-0 rounded-pill border-4 bg-current",
        as === "block" ? "size-[18px]" : "size-4",
        HALO[tone],
        // `incomplete` is a diamond, not a circle — the one variant whose shape
        // differs, and the reason a warning is legible without its colour.
        tone === "warning" && "rotate-45 rounded-[4px]",
        // `not-started` is an open dotted ring, never a filled dot.
        tone === "neutral" && "border-2 border-dotted bg-transparent",
      )}
    >
      {/* `pending`: the information arc, 900 ms linear, TRANSFORM ONLY.
          motion-iconography.md l.78 forbids animating shadow blur — which the
          pulsing halo I wrote first did. Rotation is what the spec asks for. */}
      {active && (
        <span className="absolute -inset-1 animate-arc rounded-pill border-2 border-transparent border-t-current border-r-current motion-reduce:animate-none" />
      )}
    </span>
  );

  if (as === "inline") {
    return (
      // The size sits on the wrapper, not on the inner text, so a caller can
      // drop it to `text-caption` (a field message) without forking the
      // component. `aria-live` only when something is actually transitioning —
      // EXPERIENCE.md l.194: polite announcements are for async transitions,
      // not for every dot on the page.
      <span
        id={id}
        data-testid={testId}
        role={active ? "status" : undefined}
        aria-live={active ? "polite" : undefined}
        className={cn("inline-flex items-center gap-2 text-ui", TONE_TEXT[tone], className)}
      >
        {mark}
        <span className="text-text">{children}</span>
      </span>
    );
  }
  return (
    <div
      id={id}
      data-testid={testId}
      // Errors interrupt; everything else is announced politely.
      role={tone === "error" ? "alert" : "status"}
      aria-live={active ? "polite" : undefined}
      className={cn(
        // A rim, not a saturated fill across a full-width desktop page. The
        // accent alone gets a tinted surface — it is the recommended
        // direction, and it is the one banner meant to be noticed (Jean,
        // 2026-07-29). Every other tone is a plain white card with a quiet
        // edge, which is also what motion-iconography.md asks for: "routine
        // Signal instances have no capsule, border, or tinted background".
        "flex items-start gap-3 rounded-large p-4",
        EDGE_CLASS[tone],
        tone === "accent" && "bg-primary-container [&_.text-text]:text-on-primary",
        TONE_TEXT[tone],
        className,
      )}
      style={
        tone === "accent"
          ? undefined
          : ({
              background: EDGE_BACKGROUND,
              "--edge-from": TONE_EDGE_VARS[tone].from,
              "--edge-to": TONE_EDGE_VARS[tone].to,
            } as CSSProperties)
      }
    >
      {/* Centred on the title's line box, not pushed down by a guessed margin —
          which is what put the dot and the heading out of line. */}
      <span className="flex h-[calc(var(--text-h3)*1.3)] items-center">{mark}</span>
      <div className="min-w-0 flex-1">
        {title && <p className="m-0 text-h3 font-h3 leading-[1.3] text-text">{title}</p>}
        {children && <div className={cn("text-ui text-text", title ? "mt-1" : undefined)}>{children}</div>}
      </div>
      {action && <div className="shrink-0">{action}</div>}
    </div>
  );
}

/**
 * A single number with its label. Tabular figures so columns align.
 *
 * The mockup's metric is 24px, not the 30px page title: it sits in a strip of
 * four and has to stay subordinate to the heading above it. That 24px is a
 * named step now — it had been riding Tailwind's default `text-2xl`, the only
 * size in the interface not coming from the token file.
 */
export function Metric({
  label,
  value,
  hint,
  // A test hook, and the only prop here that is not a design decision — the same
  // exemption `Status` documents above. A metric is frequently the thing a
  // screen's test identifies ("the cache age reads 2 h"), and without it a
  // caller has to wrap the metric in a div that exists for no other reason, or
  // hand-roll the caption-over-number pair this component exists to own.
  "data-testid": testId,
}: {
  label: ReactNode;
  value: ReactNode;
  hint?: ReactNode;
  "data-testid"?: string;
}) {
  return (
    <div className="min-h-[92px] min-w-0 p-5" data-testid={testId}>
      <div className="text-label font-normal text-text-secondary">{label}</div>
      <div className="mt-2.5 font-numeric text-metric font-metric [font-variant-numeric:lining-nums_tabular-nums] text-text">
        {value}
      </div>
      {hint && <div className="mt-1.5 text-caption text-text-secondary">{hint}</div>}
    </div>
  );
}

/**
 * The honest empty state. `title` says what is absent, `action` offers the one
 * thing that would fill it — never a decorative illustration standing in for an
 * explanation.
 *
 * `icon` IS NOT AN ILLUSTRATION, and the distinction is the whole reason it is
 * allowed here (76-4). What the paragraph above forbids is a picture standing
 * IN PLACE of the sentence; what §5 asks for is a mark that lets a reader tell,
 * at a glance and before reading, that this region answered rather than failed.
 * So it is a lucide glyph at the caption tone, above the title, `aria-hidden`
 * by construction — it carries no meaning the title does not already carry —
 * and it takes no colour of its own: an empty state is not a state, and a tone
 * here would put a sixth vocabulary beside `stateVocabulary`'s five.
 */
export function EmptyState({ icon, title, description, action, role = "status" }: {
  /** A lucide glyph, sized by the caller's `className` or left at 24px. Tokens only. */
  icon?: ReactNode;
  title: ReactNode;
  description?: ReactNode;
  action?: ReactNode;
  /**
   * `undefined` for a mount whose ARRIVAL is already announced by something
   * else — a Radix `DialogContent`, which carries `role="dialog"`, moves focus
   * into itself and reads its own content on open. Nesting a live region inside
   * one makes the same words arrive twice, every time it opens.
   *
   * MEASURED 2026-09-06, and the measurement is the reason this is a hatch and
   * not a migration: **zero** of the console's `EmptyState` sites are lexically
   * inside a `DialogContent`. Nine files import both symbols, which is what a
   * file-level grep sees, but every empty state in them answers for a collection
   * on the page behind the dialog. The prop exists so the first site that IS
   * inside one has somewhere to go other than a second empty state primitive.
   */
  role?: "status" | undefined;
}) {
  return (
    // `role="status"` announces the emptiness ONCE, on the transition that reveals it --
    // not on every navigation. That is safe here without touching the 57 call sites,
    // because every one of them mounts EmptyState inside the terminal branch of a
    // loading state machine: the node enters the DOM when the fetch has resolved and
    // the collection is empty, never before. Mounting IS the transition (AI-214).
    <div role={role} className="flex flex-col items-center gap-2 py-12 text-center">
      {icon && (
        <span aria-hidden className="mb-1 grid size-10 place-items-center text-text-secondary [&>svg]:size-6">
          {icon}
        </span>
      )}
      <p className="m-0 text-h3 font-h3 text-text">{title}</p>
      {description && <p className="m-0 max-w-[52ch] text-ui text-text-secondary">{description}</p>}
      {action && <div className="mt-2">{action}</div>}
    </div>
  );
}

/**
 * An immutable identifier, shown as one.
 *
 * `DESIGN.md:96` is explicit about the split: Geist carries **business
 * numbers**, amounts, percentages and dates; **JetBrains Mono is limited to
 * events, logs, traces, immutable IDs, hashes and raw technical versions**.
 * Putting an id in `Metric` breaks that rule in the loudest possible way — the
 * Datastream Overview rendered `run_01KYJ0NP…` at headline weight, in the
 * business-number typeface, in the four slots where the target asks for
 * freshness, run success and volume. A person reading that screen sees four
 * enormous strings that mean nothing and no metric at all.
 *
 * Truncation is from the RIGHT, and that is not cosmetic. The workbench used
 * `id.slice(-6)` and printed `…TW5FW`: the tail of a ULID is its least
 * distinguishing part, so two different objects can render identically while
 * the half that names the KIND (`dplan_`, `dmap_`, `run_`) is thrown away.
 *
 * The full value is always in `title`, so nothing is lost — it is demoted, not
 * hidden.
 */
export function ObjectId({ value, title }: { value?: string | null; title?: string }) {
  if (!value) return <span className="text-text-secondary">Unavailable</span>;
  return (
    <span
      className="inline-block max-w-full truncate align-bottom font-mono text-caption text-text-secondary"
      title={title ? `${title}: ${value}` : value}
    >
      {value}
    </span>
  );
}
