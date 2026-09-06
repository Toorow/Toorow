/**
 * The tone scale — one definition, read by every component that carries state.
 *
 * The console had six ways of saying green/amber/red because each component
 * chose its own colours. The fix is not "be careful"; it is that `Status`, the
 * badge, the switch, the meter and the toaster all import these maps and none
 * of them holds a colour of its own. A seventh way can only appear by editing
 * this file, which is where the discussion belongs.
 *
 * The container fills are the `*-container` tokens from `ui/tokens/tokens.json`.
 * The v3 mockup drew its chips a shade paler; the token wins, because a literal
 * in a component is exactly the defect being removed. It is the one place the
 * shipped console reads slightly deeper than the mock, and it is deliberate.
 */

/** The five states a thing can be in. */
export type Tone = "neutral" | "success" | "warning" | "error" | "info";

/**
 * The five states plus the accent.
 *
 * `accent` is the rose, and it is not a sixth state: it means **this is the
 * recommended direction** — the step to take next, the entry to fix, the
 * control you are meant to operate (Jean, 2026-07-29). Everything that can
 * carry a tone can carry it, so the rose dot on a field message and the rose
 * dot in a banner are one object rather than two that happen to agree.
 */
export type Fill = Tone | "accent";

export const TONES: readonly Tone[] = ["neutral", "success", "warning", "error", "info"];
export const FILLS: readonly Fill[] = ["accent", "neutral", "success", "warning", "error", "info"];

/** As text or as a mark — the inline dot, the mark on a message. */
export const TONE_TEXT: Record<Fill, string> = {
  accent: "text-primary",
  neutral: "text-text-secondary",
  success: "text-success",
  warning: "text-warning",
  error: "text-error",
  info: "text-info",
};

/**
 * As a filled container — badges, banners, chips.
 *
 * TWO pastel scales exist on purpose, and the line between them is size:
 *
 *   `*-container`  a SMALL filled shape that carries text — a badge, a chip.
 *                  Light enough that ink stays readable on it.
 *   `*-surface`    a LARGE field of colour with no text on it — the coverage
 *                  marks. Deeper, because a big pale field reads as absence,
 *                  and because at that size hue matters more than legibility.
 *
 * They are not interchangeable and a component should not mix them. Written
 * down because the difference is invisible in a diff and would otherwise look
 * like drift the next time someone compares a badge to a coverage mark.
 */
export const TONE_CONTAINER: Record<Fill, string> = {
  accent: "bg-primary-container",
  neutral: "bg-background-light",
  success: "bg-success-container",
  warning: "bg-warning-container",
  error: "bg-error-container",
  info: "bg-info-container",
};

/**
 * The container plus the ink that reads on it — what a badge or a chip takes.
 *
 * Derived from `TONE_CONTAINER` rather than repeated beside it: the two maps had
 * the same six background classes written twice, which is the shape of a drift.
 * Tailwind still sees every literal it needs to emit — the backgrounds are in the
 * map above, `text-text` is the string below — because it scans this file's
 * source, not this module's runtime values.
 */
export const TONE_FILL: Record<Fill, string> = Object.fromEntries(
  FILLS.map((fill) => [fill, `${TONE_CONTAINER[fill]} text-text`]),
) as Record<Fill, string>;

/**
 * As a solid bar or track — the switch, the progress meter, a gauge.
 *
 * The same shape says two different things depending on which value it takes,
 * so it is a choice at the call site and never a colour baked into the
 * component. The one hard rule, from `design-direction-foundation.md`: **rose
 * is never a healthy state.** A coverage meter, a health bar, a freshness
 * gauge take `success`. Rose belongs to an operation you started and to a
 * control you switched on.
 */
export const FILL_BG: Record<Fill, string> = {
  accent: "bg-primary",
  neutral: "bg-text-secondary",
  success: "bg-success",
  warning: "bg-warning",
  error: "bg-error",
  info: "bg-info",
};

/**
 * The tone as a 1px RIM on an otherwise plain shape — a selected row, the edge
 * of a mark whose fill is a pale surface.
 *
 * Separate from `HALO`, which is the 4px *container*-coloured ring around a
 * status dot: this one is the full-strength line, and the two are not
 * interchangeable at any size.
 */
export const TONE_BORDER: Record<Fill, string> = {
  accent: "border-primary",
  neutral: "border-divider-base",
  success: "border-success",
  warning: "border-warning",
  error: "border-error",
  info: "border-info",
};

/**
 * The LARGE pale field — the second pastel scale this file's `TONE_FILL` comment
 * names, and the one the coverage marks are drawn in.
 *
 * It is a `Tone` and not a `Fill`: `ui/tokens/tokens.json` declares
 * `*-surface`/`*-surface-hover` for the four semantic tones only, and there is no
 * `primary-surface`. `neutral` maps to the muted surface, which is what a day
 * nobody asked for was already painted with. Inventing `bg-primary-surface` here
 * would emit a class Tailwind cannot resolve — a silent no-op, the worst kind of
 * colour bug — so the type says what exists.
 */
export const TONE_SURFACE: Record<Tone, string> = {
  neutral: "bg-surface-muted",
  success: "bg-success-surface",
  warning: "bg-warning-surface",
  error: "bg-error-surface",
  info: "bg-info-surface",
};

/** The same field under the pointer. Separate, because a SELECTED row takes the
 *  surface without a hover and a clickable mark takes both. */
export const TONE_SURFACE_HOVER: Record<Tone, string> = {
  neutral: "hover:bg-surface-muted-hover",
  success: "hover:bg-success-surface-hover",
  warning: "hover:bg-warning-surface-hover",
  error: "hover:bg-error-surface-hover",
  info: "hover:bg-info-surface-hover",
};

/**
 * A SOLID dot inside a container-coloured ring — the phase mark of a timeline.
 *
 * `HALO` is the same idea for a `Status` mark, where the fill is `bg-current` and
 * the tone travels through the text colour. A phase dot has no text colour to
 * inherit, so it carries both halves itself.
 */
export const TONE_DOT: Record<Fill, string> = {
  accent: "border-primary-container bg-primary",
  neutral: "border-divider-base bg-text-secondary",
  success: "border-success-container bg-success",
  warning: "border-warning-container bg-warning",
  error: "border-error-container bg-error",
  info: "border-info-container bg-info",
};

/**
 * A destructive action that lives IN A ROW, not at the head of a screen.
 *
 * `Button variant="destructive"` is a solid red block; putting one in every row
 * of a list makes the list read as a wall of alarms and hides the one deletion
 * that matters. A bordered, error-inked secondary says the same thing at row
 * weight. It is a named constant rather than a class literal at the call site so
 * the second screen that needs it cannot pick a different opacity.
 */
export const DESTRUCTIVE_ROW_ACTION = "border-error/30 text-error";

/**
 * The same fill as a gradient, for a bar with volume — the progress meter.
 *
 * `accent` runs **violet to rose** (Jean, 2026-07-29): the two brand colours
 * read as one movement, and it is the only place the decorative lavender is
 * allowed to touch an interactive object. Every other tone travels within its
 * own hue, light to full, so the bar has depth without becoming a second
 * colour. `neutral` is the grey version — a meter that reports nothing yet.
 */
export const FILL_GRADIENT: Record<Fill, string> = {
  accent: "bg-[linear-gradient(90deg,var(--color-info),var(--color-primary))]",
  neutral:
    "bg-[linear-gradient(90deg,var(--color-divider-base),var(--color-border-control))]",
  success:
    "bg-[linear-gradient(90deg,var(--color-success-container),var(--color-success))]",
  warning:
    "bg-[linear-gradient(90deg,var(--color-warning-container),var(--color-warning))]",
  error:
    "bg-[linear-gradient(90deg,var(--color-error-container),var(--color-error))]",
  info: "bg-[linear-gradient(90deg,var(--color-info-container),var(--color-info))]",
};

/**
 * The tone as a 2px gradient EDGE on a plain surface — the discreet banner.
 *
 * A full-width saturated container shouts (Jean, 2026-07-29: "more discreet").
 * This says the same thing with a rim: the surface stays white, a 2px border
 * carries the tone, and the tone fades along it so the edge has direction
 * rather than weight. `accent` gets the violet-to-rose signature; every other
 * tone travels from itself to a third of itself, which is the sobriety asked
 * for.
 *
 * The technique is the two-layer background trick — a solid painted to the
 * padding box, the gradient painted to the border box — because CSS has no
 * `border-image` that respects `border-radius`.
 *
 * It is applied as an inline `style`, not as a Tailwind arbitrary property.
 * The arbitrary form was tried first and silently produced nothing:
 * `background-image` computed to `none` on the rendered page while the class
 * sat there looking correct. Two nested `linear-gradient()`s with their own
 * commas are past what the arbitrary-value parser will take. The inline form
 * carries no literal colour — every value is a `var()` — so the palette is
 * still the token file's.
 */
export const TONE_EDGE_VARS: Record<Fill, { from: string; to: string }> = {
  accent: { from: "var(--color-info)", to: "var(--color-primary)" },
  neutral: { from: "var(--color-border-control)", to: "var(--color-divider-base)" },
  success: { from: "var(--color-success)", to: "var(--color-success-container)" },
  warning: { from: "var(--color-warning)", to: "var(--color-warning-container)" },
  error: { from: "var(--color-error)", to: "var(--color-error-container)" },
  info: { from: "var(--color-info)", to: "var(--color-info-container)" },
};

/** The two layers. Reads `--edge-from` / `--edge-to`, set per tone. */
export const EDGE_BACKGROUND =
  "linear-gradient(var(--color-surface-light),var(--color-surface-light)) padding-box," +
  "linear-gradient(100deg,var(--edge-from),var(--edge-to)) border-box";

/**
 * The halo of a status mark — a 4px ring in the tone's container colour.
 *
 * `motion-iconography.md` § Signal: *"State always combines color, a 2 px
 * low-opacity halo, an icon, and a written English label. The halo belongs to
 * the icon rather than the entire row."* The validated
 * `mockups/first-publication.html` renders that halo as a 4px ring in the
 * container tone (a 4px ring in the container tone on a success mark), which is the same idea with a token instead of an alpha.
 */
export const HALO: Record<Fill, string> = {
  accent: "border-primary-container text-primary",
  neutral: "border-divider-base text-text-secondary",
  success: "border-success-container text-success",
  warning: "border-warning-container text-warning",
  error: "border-error-container text-error",
  info: "border-info-container text-info",
};

/**
 * The rim weight of a banner.
 *
 * `accent` keeps 2px and gains a tinted surface: it is the recommended
 * direction and the one banner meant to be noticed. Every other tone drops to
 * 1px on white (Jean, 2026-07-29: "reduce the border gradient for all other"),
 * which also satisfies `motion-iconography.md` — routine status carries no
 * capsule, border or tinted background, so the quieter the rim the closer it
 * is to the contract.
 */
export const EDGE_CLASS: Record<Fill, string> = {
  accent: "border-2 border-transparent",
  neutral: "border border-transparent",
  success: "border border-transparent",
  warning: "border border-transparent",
  error: "border border-transparent",
  info: "border border-transparent",
};

/** What sits on top of that fill — a switch thumb, a label inside a bar. */
export const FILL_ON: Record<Fill, string> = {
  accent: "text-on-primary",
  neutral: "text-text-on-dark",
  success: "text-on-success",
  warning: "text-on-warning",
  error: "text-on-error",
  info: "text-on-info",
};

/**
 * The same fill, applied only in the `checked` state — for a control whose
 * state Radix owns and React cannot read (an uncontrolled switch).
 *
 * It is a second literal map because Tailwind needs whole class names at build
 * time and cannot see one assembled from a variable. It sits next to `FILL_BG`
 * so the two are read together and cannot drift apart unnoticed.
 */
export const FILL_BG_CHECKED: Record<Fill, string> = {
  accent: "data-[state=checked]:bg-primary",
  neutral: "data-[state=checked]:bg-text-secondary",
  success: "data-[state=checked]:bg-success",
  warning: "data-[state=checked]:bg-warning",
  error: "data-[state=checked]:bg-error",
  info: "data-[state=checked]:bg-info",
};

/**
 * A field the person has to fix — rose, not red (Jean, 2026-07-29).
 *
 * This is an **amendment** to `design-direction-foundation.md`, which fixes
 * the error colour in the token file. It applies to *form validation only*:
 * "this entry is not valid yet" is a correction you are being asked to make,
 * not a failure that happened. Something that actually failed — a run, a publication, a
 * connection — keeps the red. The two were the same colour and are not the
 * same statement.
 *
 * `INVALID` is `accent` precisely because rose marks the recommended
 * direction, and the field you must fix *is* the next step.
 *
 * The message text does NOT go rose, and that is not a half-measure: rose on
 * white measures **1.97:1** and `primary-hover` **2.43:1**, against the 4.5:1
 * a body string needs — an unreadable validation message is worse than a harsh
 * one. So the rose carries the signal where it is decorative (the control's
 * border, the asterisk, the dot) and the message stays ink. The meaning never
 * rests on colour anyway: `Field` writes `aria-invalid` and wires the message
 * through `aria-describedby`.
 */
export const INVALID: Fill = "accent";
export const INVALID_BORDER = "aria-invalid:border-primary";
export const INVALID_MESSAGE = "text-text";
