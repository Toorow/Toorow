import { AlertTriangle, Ban, Construction, History } from "lucide-react";
import { Button } from "../ui";

/**
 * `stale` and `unavailable` are NOT interchangeable.
 *
 * `stale` says the reference itself is finished: retired, superseded, no longer
 * current. `unavailable` says the reference is fine and WE cannot open it yet,
 * because the owning workbench is not built. Using `stale` for both told an
 * operator that a perfectly valid pinned version had been retired — a false
 * statement, and a catch-all wearing the costume of a route state.
 */
export type RouteStateKind = "unknown" | "denied" | "stale" | "unavailable";

const copy: Record<RouteStateKind, { eyebrow: string; title: string; detail: string }> = {
  unknown: {
    eyebrow: "Unknown route",
    title: "This address is not registered",
    detail: "The requested workspace, section, object, tab, version, or action is not part of this project route model.",
  },
  denied: {
    eyebrow: "Access unavailable",
    title: "This project route cannot be opened",
    detail: "We cannot confirm access to the requested scope. No information about another organization, project, or object is disclosed.",
  },
  stale: {
    eyebrow: "Reference unavailable",
    title: "This saved reference is no longer current",
    detail: "The requested object or version was retired, superseded, or is no longer available from its owner.",
  },
  unavailable: {
    eyebrow: "Owner not built yet",
    title: "This route is registered, but its workbench does not exist yet",
    detail: "The address is part of the canonical route model and your reference is intact. The surface that owns it has not been delivered, so nothing is shown in its place.",
  },
};

export default function RouteState({ kind, reason, onBackToOverview, backLabel = "Back to Project Overview" }: {
  kind: RouteStateKind;
  reason?: string;
  onBackToOverview?: () => void;
  backLabel?: string;
}) {
  const state = copy[kind];
  const Icon =
    kind === "unknown" ? AlertTriangle
      : kind === "denied" ? Ban
        : kind === "unavailable" ? Construction
          : History;
  return (
    <section className="mx-auto flex min-h-[28rem] max-w-3xl items-center px-8 py-16" data-route-state={kind} aria-labelledby={`route-state-${kind}`}>
      <div className="w-full border-l-2 border-l-warning pl-8">
        <Icon className="mb-6 size-6 text-muted-foreground" aria-hidden="true" />
        <p className="font-mono text-xs font-semibold uppercase tracking-[0.14em] text-muted-foreground">{state.eyebrow}</p>
        <h1 id={`route-state-${kind}`} className="mt-2 text-3xl font-semibold tracking-tight text-foreground">{state.title}</h1>
        <p className="mt-4 max-w-2xl text-sm leading-6 text-muted-foreground">{reason || state.detail}</p>
        {onBackToOverview ? <Button className="mt-8" onClick={onBackToOverview}>{backLabel}</Button> : null}
      </div>
    </section>
  );
}