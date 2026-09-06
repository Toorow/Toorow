/**
 * Button — adapted to the validated v3 mockup.
 *
 * Geometry read from `application-v3.css` lines 209-234, the sheet the rendered
 * desktop mocks were captured from:
 *
 *     .quiet-button, .secondary-button, .primary-button, .selector {
 *       height: 40px; border: 1px solid var(--line); padding: 0 16px;
 *       border-radius: 999px; font-size: 13px; font-weight: 700; gap: 8px;
 *     }
 *     .icon-button { width: 40px; border-radius: 10px; }
 *
 * So: every button carries a border — the primary's is rose, not absent — the
 * label is 13px/700, and an icon-only button is a 10px square, the one place
 * the pill radius does not apply.
 *
 * Four intents, which is what the spec contracts: `default` (primary rose),
 * `secondary` (white surface), `ghost` (the mockup's quiet button, page
 * background), `destructive`. `link` is a text link, a different object. There
 * is deliberately no `outline`: it would render exactly like `secondary`,
 * which is two names for one intent.
 *
 * **`default` is the recommended direction, not "the important one."** Rose
 * marks the step the person is meant to take next (Jean, 2026-07-29), so a
 * view carries one — two rose buttons side by side is two recommendations,
 * which is none. Everything else on the screen is `secondary` or `ghost`.
 *
 * **`secondary` and `ghost` carry `--color-border-control`, not the divider.**
 * **The resting edge is `border-quiet`, and it took two tries to find.** The
 * mockup's `--line` measures 1.19:1 on white — Jean: *"we cannot see it is a
 * button"*. `border-control` at 3.25:1 does read, but on a white panel it
 * boxes the button in — Jean again: *"the button on Panel is not okay"*. The
 * first fix went back to the hairline plus a shadow and quietly restored the
 * first complaint, which is what looking at the sheet caught.
 *
 * `border-quiet` sits between them at 2.04:1: visible as a control, not a box.
 * The control edge now appears on HOVER, where a firmer line confirms the
 * target instead of shouting at rest. Three neutral steps, three jobs —
 * hairline, button at rest, field edge — and all three in `tokens.json`.
 *
 * The two then differ by their ground: `secondary` is white and sits on the
 * page tint; `ghost` is the page tint and sits on a white panel.
 *
 * **Sizes are per usage, not per taste**:
 *
 *   default 40  the page's own actions — toolbars, form footers, page headers
 *   sm      32  inside a row: a table cell, a panel header band, a card footer,
 *               where 40 would break the 44/52px rhythm around it
 *   xs      24  inside dense content — a chip row, a cell with two lines
 *   lg      48  the single action of an empty state or a wizard step, where it
 *               is the only thing to click
 *
 * `default` 40 and `sm` 32 are measured from the mockups. `xs` 24 and `lg` 48
 * continue the same ladder and appear in no mockup — said here rather than
 * passed off as validated.
 */
import * as React from "react"
import { cva, type VariantProps } from "class-variance-authority"
import { Slot } from "radix-ui"

import { cn } from "@/lib/cn"

const buttonVariants = cva(
  // `justify-self-start` : UN BOUTON N'EST PAS UNE BARRE.
  //
  // `inline-flex` suffit tant que le parent est en flux ou en flex. Dans une
  // GRILLE, l'alignement par défaut est `stretch` : le bouton prend toute la
  // colonne et devient un bandeau rose pleine largeur. Nos panneaux de
  // formulaire sont des grilles (`Panel className="grid gap-5"`), donc chaque
  // bouton posé directement dedans sortait ainsi — l'étape 1 de l'assistant
  // Datastream en est la capture. La géométrie du mockup est une pilule de
  // 40px de haut dimensionnée par son libellé, jamais par sa colonne.
  //
  // Réglé ici et pas sur l'écran : le défaut appartient à la primitive, il vaut
  // pour toutes les grilles de la console. Un bouton qui DOIT remplir sa
  // colonne le dit encore, avec `w-full`.
  "inline-flex shrink-0 items-center justify-center justify-self-start gap-2 rounded-pill border text-label font-label whitespace-nowrap transition-colors outline-none focus-visible:outline-3 focus-visible:outline-offset-2 focus-visible:outline-focus disabled:pointer-events-none disabled:opacity-50 aria-invalid:border-primary [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-4",
  {
    variants: {
      variant: {
        default:
          "border-primary bg-primary text-on-primary hover:border-primary-hover hover:bg-primary-hover",
        secondary:
          "border-border-quiet bg-surface-light text-text shadow-card-light hover:border-border-control hover:bg-background-light",
        ghost:
          "border-border-quiet bg-background-light text-text hover:border-border-control hover:bg-surface-light",
        destructive: "border-error bg-error text-on-error hover:bg-error/90",
        link: "border-transparent bg-transparent text-text underline-offset-4 hover:underline",
      },
      size: {
        default: "h-control-height px-4",
        sm: "h-control-height-small gap-1.5 px-3.5",
        xs: "h-6 gap-1 px-2.5 text-caption [&_svg:not([class*='size-'])]:size-3",
        lg: "h-12 px-6",
        // Icon-only: square, and the one radius the mockup does not make a pill.
        icon: "size-control-height rounded-control px-0",
        "icon-sm": "size-control-height-small rounded-control px-0",
        "icon-xs": "size-6 rounded-sm px-0 [&_svg:not([class*='size-'])]:size-3",
        "icon-lg": "size-12 rounded-control px-0",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  }
)

function Button({
  className,
  variant = "default",
  size = "default",
  asChild = false,
  ...props
}: React.ComponentProps<"button"> &
  VariantProps<typeof buttonVariants> & {
    asChild?: boolean
  }) {
  const Comp = asChild ? Slot.Root : "button"

  return (
    <Comp
      data-slot="button"
      data-variant={variant}
      data-size={size}
      className={cn(buttonVariants({ variant, size, className }))}
      {...props}
    />
  )
}

export { Button, buttonVariants }
