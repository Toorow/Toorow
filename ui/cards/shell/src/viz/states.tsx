/**
 * Story 50.5 AC13 -- the shared non-success states belong to the RUNTIME.
 *
 * Eleven distinct English states, each stating what remains usable and the next
 * safe action. They are here, once, and not in each renderer and not in each
 * screen -- which is the whole reason "the same answer" can mean the same thing
 * in the Console, in an MCP App and through a shared link.
 *
 * `empty` AND `unavailable` NEVER SHARE A RENDERING. The first means the question
 * was asked and nothing matched. The second means the question could not be asked
 * and must NAME THE MISSING LINK, exactly as the Story 50.1 execution already
 * does (`server/core/query_execution.py:214-227`). Collapsing them into one
 * "no data" panel is how a broken pipeline gets read as an honest zero.
 *
 * NO NEW STYLESHEET, NO NEW CLASS PREFIX (AC18). Everything here is Tailwind
 * utility classes on the token layer.
 */

import type { ReactNode } from "react";

export type VizStateKind =
  | "loading"
  | "empty"
  | "stale"
  | "partial"
  | "truncated"
  | "degraded"
  | "refused"
  | "unavailable"
  | "denied"
  | "unknown"
  | "errored";

/** Which states are a blocking panel, and which are a banner over a real visual. */
const BANNER_STATES: VizStateKind[] = ["stale", "partial", "truncated", "degraded"];

export function isBanner(kind: VizStateKind): boolean {
  return BANNER_STATES.includes(kind);
}

const TITLES: Record<VizStateKind, string> = {
  loading: "Loading this result",
  empty: "Nothing matched this question",
  stale: "This answer is older than its cadence",
  partial: "Part of this answer is missing",
  truncated: "The server returned part of the rows",
  degraded: "This answer was produced with a degraded source",
  refused: "This visualization was refused",
  unavailable: "This question could not be asked",
  denied: "You do not have access to this result",
  unknown: "This result is in a state this build does not know",
  errored: "This visualization could not be drawn",
};

const NEXT_ACTIONS: Record<VizStateKind, string> = {
  loading: "Nothing to do -- the result is being retrieved.",
  empty:
    "The query ran and returned no rows. Widen the time window or relax a filter, then run it again.",
  stale: "The values below are the last ones retrieved. Refresh the datastream to get newer ones.",
  partial:
    "The values that are present are exact. The missing part is named above; nothing has been filled in for it.",
  truncated:
    "Every row below was returned by the server. Narrow the question to see the rest, or export the full result.",
  degraded:
    "The values below are usable but their source reported a problem. Check the source before quoting them.",
  refused: "Change the Visualization Spec, or pick a family this build implements.",
  unavailable: "Fix the missing link named above, then run the query again.",
  denied: "Ask an administrator of this project for access.",
  unknown:
    "This build does not know how to present this state, so it presents nothing rather than guessing. Report the state name above.",
  errored: "Retry the load. If it still fails, open the Result and report the message above.",
};

export interface VizStatePanelProps {
  kind: VizStateKind;
  /** The exact server message, the missing link, or the refusal. Shown verbatim. */
  detail?: string | null;
  /** Named separately so `unavailable` can never render without it. */
  missingLink?: string | null;
  /** Surface-specific safe recovery when the generic state action is not accurate. */
  nextAction?: string | null;
  children?: ReactNode;
}

/**
 * One bounded `role="status"` announcement per async transition, and it never
 * steals focus. `errored` is `role="alert"` because a failure that a screen
 * reader user only discovers by exploring is a failure they discover too late.
 */
export function VizStatePanel(props: VizStatePanelProps) {
  const { kind, detail, missingLink, nextAction, children } = props;
  const banner = isBanner(kind);
  const tone =
    kind === "errored" || kind === "unavailable" || kind === "denied" || kind === "refused"
      ? "border-l-4 border-l-red-600"
      : kind === "stale" || kind === "degraded" || kind === "partial" || kind === "truncated"
        ? "border-l-4 border-l-amber-500"
        : "border-l-4 border-l-neutral-400";

  return (
    <div
      role={kind === "errored" ? "alert" : "status"}
      aria-live={kind === "errored" ? "assertive" : "polite"}
      data-viz-state={kind}
      className={`flex flex-col gap-2 rounded-md bg-[color:var(--color-surface-muted,transparent)] px-4 py-3 text-sm ${tone} ${
        banner ? "mb-3" : "my-3"
      }`}
    >
      {/* Status is never conveyed by colour alone: the state is named in text. */}
      <p className="m-0 font-semibold">{TITLES[kind]}</p>
      {detail ? <p className="m-0 opacity-90">{detail}</p> : null}
      {kind === "unavailable" ? (
        <p className="m-0 opacity-90">
          Missing link:{" "}
          <code className="font-mono">{missingLink || "not reported by the server"}</code>
        </p>
      ) : null}
        <p className="m-0 opacity-75">{nextAction ?? NEXT_ACTIONS[kind]}</p>
      {children}
    </div>
  );
}

export const VIZ_STATE_KINDS: VizStateKind[] = [
  "loading",
  "empty",
  "stale",
  "partial",
  "truncated",
  "degraded",
  "refused",
  "unavailable",
  "denied",
  "unknown",
  "errored",
];
