/**
 * Skeleton — the shape of what is coming, while it is not here yet.
 *
 * WHEN TO USE THIS RATHER THAN `Spinner`, because the two are not
 * interchangeable and picking the wrong one is a real cost to the reader:
 *
 *   Spinner   — "something is happening", no idea what shape the answer has.
 *               A button saving. A one-line refresh.
 *   Skeleton  — "the answer has THIS shape and it is on its way". A table of
 *               eight rows, a card, a chart. It holds the layout so the page
 *               does not jump when the data lands, and a jump is the thing a
 *               reader notices most.
 *
 * So a skeleton is only honest when the real content will occupy roughly this
 * box. Rendering three grey bars for a list that turns out to be empty is a
 * promise the screen then breaks — use `EmptyState` for that, and reach for a
 * skeleton only where the shape is known in advance.
 *
 * `--color-track-light` is the same track the progress bar lies in, so the
 * placeholder and the meter read as one material rather than two greys.
 *
 * The pulse stops dead under `prefers-reduced-motion`: an animation whose only
 * job is to say "still loading" is exactly the kind a vestibular disorder makes
 * unusable, and the grey box already carries the meaning without it.
 */
"use client"

import * as React from "react"

import { cn } from "@/lib/cn"

function Skeleton({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="skeleton"
      // `aria-hidden`: the placeholder is not content. The region that owns it
      // should carry `aria-busy`, so a screen reader is told the state once
      // instead of being read a wall of empty boxes.
      aria-hidden
      className={cn(
        "animate-pulse rounded-control bg-track-light motion-reduce:animate-none",
        className
      )}
      {...props}
    />
  )
}

export { Skeleton }
