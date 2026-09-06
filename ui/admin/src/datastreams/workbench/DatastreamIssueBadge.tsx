/**
 * The open data-quality issues of one Datastream — story 59.2.
 *
 * ONE COMPONENT, TWO MOUNTS. The fleet list (`shell/pages/DataWorkspace.tsx`)
 * and the Workbench header (`DatastreamWorkbenchRoute.tsx`) draw the same fact
 * from the same server object, so they cannot say different things about the
 * same flux. It is the shape `DatastreamRunStateBadge` already established for
 * the run state, and for the same reason.
 *
 * FOUR STATES, AND THEY NEVER RENDER FOR EACH OTHER:
 *
 *   1. nothing watches this Datastream — no published monitor targets it, so
 *      nothing has looked. A sentence, never a count.
 *   2. monitors watch it and nothing is open — something DID look and found
 *      nothing. On 2026-08-08 preprod carries exactly this on a real row (2
 *      published monitors, 0 issues), which is why folding it into 1 would be a
 *      false claim about an existing Datastream rather than a simplification.
 *   3. a count, with the worst severity the server measured.
 *   4. the key is absent from the payload: the count could not be READ. Never
 *      `0` — the distinction `WorkbenchOverviewPage` already draws for
 *      `dq_monitors`, and the reason this column carried a sentence until today.
 *
 * THE SEVERITY WORD IS THE SERVER'S. `app.dq_issues.severity` is CHECK-
 * constrained to `blocking` / `degrading` / `informational` (migration 145) and
 * this file renders whichever of them arrives — it holds no list of severities
 * and invents no synonym. What it owns is the TONE, because no colour travels on
 * the wire: `blocking → error`, `degrading → warning`, `informational →
 * neutral`, the map settled by the orchestrator on 2026-08-08.
 *
 * NO EMOJI. `epic-59:50` asks for « 🔴 / ⚠️ » — two glyphs for three values, and
 * it contradicts `motion-iconography.md` § Signal: "State always combines color,
 * a 2 px low-opacity halo, an icon, and a written English label". `Status`
 * carries all four, and its mark is a SHAPE as well as a colour (a diamond for
 * `warning`, a dotted ring for `neutral`), which is what survives greyscale and
 * a colour-blind reader. The amendment is written in the story rather than
 * applied silently.
 */
import type { ReactNode } from "react";
import { buildPath, parsePath } from "../../shell/router";
import { Status,
  stateTone,
} from "../../ui";

/**
 * The evidence address of a faulty run, built BY THE ROUTER, never by string
 * concatenation (2026-08-30, data.md [10]/[38]): `href` is the server-composed
 * runs address; the router re-reads it and appends the evidence tail in its
 * own grammar. An address the router cannot read yields no evidence link at
 * all -- the runs address stays, unmodified.
 */
export function evidenceHrefFor(runsHref: string, executionId: string): string {
  const parsed = parsePath(runsHref);
  if (parsed.kind !== "resolved") return runsHref;
  return buildPath({ ...parsed.route, evidenceId: executionId });
}

/** What the server sends under `evidence.open_issues` / `header.open_issues`. */
export interface OpenIssueSummary {
  /** A PUBLISHED monitor targets this Datastream. Not "an issue exists". */
  monitored: boolean;
  count: number;
  by_severity: Record<string, number>;
  /** The worst severity currently open, or `null` when nothing is. */
  highest_severity: string | null;
  /** The most recent faulty execution ID, if any issues are tied to one. */
  faulty_execution_id?: string | null;
}

/*
 * THE SEVERITY MAP IS GONE (76-2 review), AND IT WAS RIGHT ALL ALONG.
 *
 * `blocking` / `degrading` / `informational` were drawn red / amber / grey here,
 * in `RunAnomalies`, and `blocking` red again in `mapping/mappingModel`. Three
 * files agreeing by coincidence is what `ui/stateVocabulary` exists to replace:
 * all three words are declared there now, at exactly these tones, and the
 * distinction the union had to make to hold them is written beside `blocking` --
 * `blocked` is what an object IS (repairable, amber), `blocking` is what a thing
 * DOES to everything downstream.
 */

/**
 * The payload, or `null` when it was not sent.
 *
 * `null` is the fourth state and it is deliberately hard to reach by accident:
 * a payload without a numeric `count` is not a payload reporting zero.
 */
export function readOpenIssues(value: unknown): OpenIssueSummary | null {
  if (!value || typeof value !== "object") return null;
  const raw = value as Record<string, unknown>;
  if (typeof raw.count !== "number" || !Number.isFinite(raw.count)) return null;
  const severity = typeof raw.highest_severity === "string" && raw.highest_severity.trim()
    ? raw.highest_severity
    : null;
  return {
    monitored: raw.monitored === true,
    count: raw.count,
    by_severity:
      raw.by_severity && typeof raw.by_severity === "object"
        ? (raw.by_severity as Record<string, number>)
        : {},
    highest_severity: severity,
    faulty_execution_id: typeof raw.faulty_execution_id === "string" ? raw.faulty_execution_id : null,
  };
}

function Sentence({ children, why }: { children: ReactNode; why: string }) {
  return (
    <span className="text-text-secondary" title={why}>
      {children}
    </span>
  );
}

export default function DatastreamIssueBadge({
  summary,
  href,
  onOpen,
}: {
  /** Straight off the envelope — the component decides what its absence means. */
  summary: unknown;
  /**
   * Where the badge leads, ALREADY judged by the router (`resolvableHref`). The
   * ratified amendment says a badge leads to the run; the console's address
   * grammar stops at `/tab/{tab}` and most issues name no run at all, so it
   * lands on the `Runs` tab — as close as the grammar allows, and never a
   * floating window. The gap is named in the story rather than closed here.
   */
  href?: string | null;
  /** The same gesture where the object is already open: the tab, not an address. */
  onOpen?: (executionId?: string | null) => void;
}) {
  const read = readOpenIssues(summary);

  if (!read) {
    return (
      <Sentence why="This payload carries no issue count for this Datastream, so the console cannot say whether anything is open. It is never reported as zero: nothing was measured.">
        Issue count unavailable
      </Sentence>
    );
  }

  if (read.count === 0) {
    return read.monitored ? (
      <span title="A published data-quality monitor targets this Datastream and none of its findings is open. Something looked, and found nothing.">
        <Status tone="success">No open issue</Status>
      </span>
    ) : (
      <Sentence why="No published data-quality monitor targets this Datastream, so nothing has evaluated it. Monitors are placed per Project and listed on the Datastream's Overview.">
        No monitor watches this
      </Sentence>
    );
  }

  const severity = read.highest_severity;
  const label = `${read.count} open issue${read.count > 1 ? "s" : ""}${severity ? ` · ${severity}` : ""}`;
  const badge = <Status tone={stateTone(severity)}>{label}</Status>;
  const why = "Data-quality issues that are open and not yet reviewed. Acknowledging one removes it from this count.";

  if (href) {
    // Story 59: a faulty run opens on the Operations tab at its own evidence.
    // The tail is the router's, not a concatenation (see `evidenceHrefFor`).
    const targetHref = read.faulty_execution_id
      ? evidenceHrefFor(href, read.faulty_execution_id)
      : href;
    return (
      <a
        href={targetHref}
        title={why}
        className="underline-offset-2 hover:underline"
        // The row itself opens the Datastream; the badge opens its runs, and a
        // click must not do both.
        onClick={(event) => event.stopPropagation()}
      >
        {badge}
      </a>
    );
  }
  if (onOpen) {
    return (
      <button
        type="button"
        title={why}
        className="cursor-pointer underline-offset-2 hover:underline"
        onClick={() => onOpen(read.faulty_execution_id)}
      >
        {badge}
      </button>
    );
  }
  return <span title={why}>{badge}</span>;
}
