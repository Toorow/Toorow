/**
 * Re-collecting days, and the confirmation that names what it spends — 58.4.
 *
 * ONE MODULE FOR TWO DOORS, and that is the story rather than a convenience.
 * The gesture already shipped on the `Runs` tab (`shell/pages/CoverageStrip`):
 * a `Collect N days` button and a span selection, both posting straight to
 * `/refetch` with no confirmation, no scope named and no provider account. A
 * confirmation added to the new day grid alone would have left the older door
 * spending without a sentence — the instance repaired and the class left open.
 * So the call, the vocabulary and the dialog live here and both screens use them.
 *
 * WHAT THE CONFIRMATION MAY NOT SAY. Not a number. `core/quota.py` publishes
 * `open|closed` and nothing else, its counter lives in a per-process singleton
 * no HTTP route reaches, and how many provider requests one day costs is known
 * only at the worker. So the spend line is `Not measured` WITH ITS REASON —
 * `NOT_MEASURED` is the word story 63.6 already put on this surface and it is
 * imported, not retyped. A figure invented to look precise is the defect this
 * repository keeps finding.
 *
 * IT IS A LIGHT CONFIRMATION, and deliberately. Jean, 2026-08-06: the gesture is
 * one call that returns a JSON. It names the scope — the Datastream, the
 * connector, the source account, the day — and it does not dramatise a cheap
 * action. `destructive` is not set: nothing is destroyed and a red button would
 * say otherwise.
 */
import { useState } from "react";
import { apiFetch } from "../../lib/apiFetch";
import {
  ConfirmDialog, type GapDay, JOB_STATE_LABEL, extractGapLabel, extractGapSentence,
} from "../../ui";
import { NOT_MEASURED } from "./DatastreamRunLive";

/**
 * Why there is no figure beside `Not measured`, in one sentence.
 *
 * Written here, once, because the dialog and any later reader of this outcome
 * must give the same reason. It states what was measured — the engine publishes
 * a breaker state — rather than apologising.
 */
export const SPEND_NOT_MEASURED_REASON =
  "the quota engine publishes an open/closed breaker and no balance, and the " +
  "number of provider requests a day costs is known only when the window runs";

/** A fact the console does not have, said as such — never a blank cell. */
export const NOT_REPORTED = "Not reported";

/**
 * One sentence, ended ONCE.
 *
 * The dialog joined what is missing to what it is about to do with a hard-coded
 * `. `, and since AI-307 the first half can be the CONNECTOR'S own sentence —
 * a whole sentence with its own full stop. Measured on the prevented day:
 * « ... then re-ask these dates.. The provider is asked for this window again »,
 * two dots in the first line a person reads before spending quota. The composer
 * cannot know how the connector wrote its sentence, so it asks.
 */
export function endSentence(text: string): string {
  const trimmed = text.trim();
  if (!trimmed) return "";
  return /[.!?…]$/.test(trimmed) ? `${trimmed} ` : `${trimmed}. `;
}

/**
 * How many days the confirmation spells out before it starts counting.
 *
 * A BOUNDED LIST, AND IT SAYS IT IS BOUNDED. The confirmation of a bulk
 * re-collection has one job — let a person check WHAT they are about to spend on
 * — and « 2026-07-11 → 2026-09-02 » does not do it: a selection is not an
 * interval, and rendering it as one told somebody who picked four failed days
 * out of sixty that they were re-collecting the whole window. So every day is
 * named while the list stays legible, and past that the remainder is COUNTED,
 * never silently cut: a list quietly truncated reads as the whole answer, which
 * is the defect the day grid above refuses one layer down.
 */
export const DAYS_NAMED = 12;

/**
 * The days, named — and the count first, because the count is what is spent.
 *
 * One day keeps its bare date: « 1 day — 2026-07-11 » would make the simplest
 * case read like a report.
 */
export function daysSentence(days: readonly string[]): string {
  if (days.length === 0) return NOT_REPORTED;
  if (days.length === 1) return days[0];
  const named = days.slice(0, DAYS_NAMED).join(", ");
  const rest = days.length - DAYS_NAMED;
  return rest > 0
    ? `${days.length} days — ${named}, and ${rest} more`
    : `${days.length} days — ${named}`;
}

/**
 * WHAT IS MISSING ON THE DAYS BEING ASKED FOR, whether they are one or forty.
 *
 * One day keeps the sentence `extractGapSentence` already composes — « meta-ads
 * returned nothing for 2026-06-09 » — because the row's button carries that same
 * string as its accessible name and the two must not drift apart. THEY DID, and
 * for one release: this function passed `days[0].extract_status` and the date and
 * dropped the window's state, so a prevented day read « did not allow this
 * collection » on the row and « was never asked for » in the confirmation that
 * row opens. `extractGapSentence` now takes the DAY, which is why the drift
 * cannot come back — there is no field left to forget.
 *
 * SEVERAL DAYS ARE COUNTED BY STATE, never flattened into one wording. « 12 days
 * are missing » hides that eleven were never asked for and one came back empty,
 * which are the two facts that decide whether re-asking is worth quota at all.
 * The counting reads `extractGapLabel`, the SAME distinction the single sentence
 * draws: counting `extract_status` alone said « 4 Never requested » about four
 * windows the source refused, because the ledger answers `never_fetched` for
 * `cancelled`, `superseded` and `prevented` alike. An extract state this build
 * does not know is still spelled as it arrived instead of mapped onto a
 * neighbour.
 */
export function repullScopeSentence(
  days: readonly GapDay[],
  connector: string | null | undefined,
): string | null {
  if (days.length === 0) return null;
  if (days.length === 1) {
    return extractGapSentence(days[0], connector);
  }
  const counts = new Map<string, number>();
  for (const day of days) {
    const key = extractGapLabel(day);
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  const parts = [...counts.entries()].map(([label, count]) => `${count} ${label}`);
  const who = connector?.trim() || "The provider";
  return `${who} is asked again for ${days.length} days — ${parts.join("; ")}`;
}

/** One window as the route answered for it. */
export interface RepullJob {
  job_id?: string;
  state?: string;
  date_from?: string;
  date_to?: string;
  deduplicated?: boolean;
  /** The org's trial ceiling moved the window — story 34.3, carried since 58.4. */
  backfill_clamp?: { clamped?: boolean; date_from?: string; max_backfill_days?: number | null };
  /** The queue's refusal, when the entry is not a queued window at all. */
  code?: string;
  message?: string;
}

/**
 * Was this entry really queued?
 *
 * ASKED OF THE REGISTRY, not against a word. `queue.enqueue_pull` answers a
 * dict shaped exactly like a job — same keys, `jobs` array, `202` — whose
 * `state` is `refused`, which is NOT a value `app.pull_jobs.state` may hold and
 * therefore not a name `pull_job_states` declares. So the question is "is this a
 * state the registry knows", and anything else is an entry that spent nothing.
 * Comparing to the literal `"refused"` would have put a queue word with no
 * registry into the console, where the next one added would be counted as a
 * spend.
 */
function wasQueued(job: RepullJob): boolean {
  return Boolean(job.state && job.state in JOB_STATE_LABEL);
}

/** The run that holds the Datastream, when one does. Never a refusal. */
export interface ActiveRun {
  execution_id: string;
  state?: string;
  message: string;
}

export interface RepullOutcome {
  jobs: RepullJob[];
  /** The days that were asked for — the route groups them into fewer windows. */
  days: string[];
  activeRun: ActiveRun | null;
}

/**
 * Ask for these days again. Throws with the SERVER'S sentence on a refusal.
 *
 * The thrown message is what the screens show under "The re-collection could not
 * be queued", so a route that wrote a reason keeps it: `HTTP 422` alone tells a
 * person nothing they can act on. The address is project-scoped since 58.4 and
 * there is only one of it.
 */
export async function requestRepull(
  projectId: string,
  datastreamId: string,
  days: string[],
): Promise<RepullOutcome> {
  const response = await apiFetch(
    `/api/projects/${encodeURIComponent(projectId)}/datastreams/${encodeURIComponent(
      datastreamId,
    )}/refetch`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dates: days }),
    },
  );
  const body = (await response.json().catch(() => ({}))) as {
    jobs?: RepullJob[];
    active_run?: ActiveRun;
    message?: string;
  };
  if (!response.ok) {
    throw new Error(body.message ?? `HTTP ${response.status}`);
  }
  return {
    jobs: Array.isArray(body.jobs) ? body.jobs : [],
    days,
    activeRun: body.active_run ?? null,
  };
}

/**
 * What happened, in the words a person can check — and never "queued" for a
 * window the queue did not queue.
 *
 * THREE THINGS THE FIRST VERSION OF THIS SCREEN GOT WRONG, all of them measured:
 *
 *   * it counted `body.jobs.length` and said "Queued N windows". `enqueue_pull`
 *     answers `{state: "refused"}` for a connection whose account scope is not
 *     authorized, and that dict lands in `jobs` like any other — so a refusal
 *     was announced as a queue;
 *   * it ignored `deduplicated`, so a double click read as two spends;
 *   * it ignored the trial backfill clamp, so a day older than the org's ceiling
 *     was reported as queued while the row held a different window.
 */
export function repullOutcomeSentence(outcome: RepullOutcome): string {
  const queued = outcome.jobs.filter(wasQueued);
  const refused = outcome.jobs.filter((job) => !wasQueued(job));
  const deduplicated = queued.filter((job) => job.deduplicated).length;
  const clamped = outcome.jobs.find((job) => job.backfill_clamp?.clamped);

  const dayCount = outcome.days.length;
  const parts: string[] = [];
  if (queued.length > 0) {
    parts.push(
      `Queued ${queued.length} pull ${queued.length === 1 ? "window" : "windows"} for ` +
        `${dayCount} ${dayCount === 1 ? "day" : "days"}.`,
    );
  }
  if (deduplicated > 0) {
    parts.push(
      `${deduplicated} of them ${deduplicated === 1 ? "was" : "were"} already in flight ` +
        "and no second pull was created.",
    );
  }
  if (refused.length > 0) {
    // The queue's own sentence, whole. It says which scope is missing.
    parts.push(
      `${refused.length} ${refused.length === 1 ? "window was" : "windows were"} refused ` +
        `by the queue: ${refused[0].message ?? refused[0].state}.`,
    );
  }
  if (clamped?.backfill_clamp?.date_from) {
    parts.push(
      `The window was moved to start on ${clamped.backfill_clamp.date_from} — this ` +
        "organisation's backfill ceiling does not reach the day that was asked for.",
    );
  }
  if (outcome.activeRun) {
    parts.push(
      `${outcome.activeRun.message} This re-collection has no run line of its own.`,
    );
  }
  if (parts.length === 0) {
    // The route answered `202` with no window at all. Saying "queued 0" would
    // read as a success; saying nothing would read as one too.
    return "The route accepted the request and queued no pull window.";
  }
  return parts.join(" ");
}

/**
 * The confirmation, controlled by its caller.
 *
 * The two doors own their own trigger — a compact cell button on a row, a span
 * footer under the strip — and share the dialog, so the sentence a person reads
 * before spending is one sentence in one place.
 */
export function RepullConfirmDialog({
  open,
  onOpenChange,
  days,
  datastreamId,
  connector,
  sourceAccountRef,
  what,
  busy,
  error,
  onConfirm,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  days: string[];
  datastreamId: string;
  connector?: string | null;
  sourceAccountRef?: string | null;
  /** What is missing on this day, when the caller knows — `extractGapSentence`. */
  what?: string | null;
  busy: boolean;
  error: string | null;
  onConfirm: () => void;
}) {
  const one = days.length === 1;
  return (
    <ConfirmDialog
      open={open}
      onOpenChange={(next) => {
        if (busy) return;
        onOpenChange(next);
      }}
      title={one ? "Re-collect this day" : `Re-collect ${days.length} days`}
      description={
        (what ? endSentence(what) : "") +
        "The provider is asked for this window again. Nothing already collected " +
        "is removed: a day that comes back with rows replaces its own reading, " +
        "and a day that comes back empty stays empty."
      }
      evidenceLabel="What this asks for"
      evidence={{
        Datastream: datastreamId,
        Connector: connector || NOT_REPORTED,
        // The bound account, or the word for its absence. A blank row would read
        // as an account whose name is too small to see.
        "Source account": sourceAccountRef || NOT_REPORTED,
        // NOT AN INTERVAL. This row read `first → last` for every multi-day ask,
        // and a bulk selection is not a span: four failed days picked out of
        // sixty were announced as « 2026-07-11 → 2026-09-02 », which is the
        // whole window and sixty times the spend. The days are named, the count
        // comes first, and a list too long to read says how many it did not
        // spell out.
        [one ? "Day" : "Days"]: daysSentence(days),
        "Estimated spend": `${NOT_MEASURED} — ${SPEND_NOT_MEASURED_REASON}`,
      }}
      confirmLabel={one ? "Re-collect this day" : "Re-collect these days"}
      busy={busy}
      error={error}
      onConfirm={onConfirm}
      data-testid="repull-confirm"
      cancelTestId="repull-cancel"
      confirmTestId="repull-go"
    />
  );
}

/**
 * The whole gesture as one hook: open, confirm, post, report.
 *
 * Both doors keep the same order of events — a click opens, a confirm writes,
 * a failure stays IN the dialog with the server's sentence (the pattern 63.6
 * settled: closing on a refusal leaves a person believing something happened),
 * and a success closes and hands the outcome back so the screen can re-read its
 * window instead of drawing a run nothing observed.
 */
export function useRepull(
  projectId: string,
  datastreamId: string,
  onQueued: (outcome: RepullOutcome) => void,
) {
  const [days, setDays] = useState<string[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  return {
    days,
    busy,
    error,
    ask(next: string[]) {
      setError(null);
      setDays(next);
    },
    close() {
      if (busy) return;
      setDays(null);
      setError(null);
    },
    async confirm() {
      if (busy || !days) return;
      setBusy(true);
      setError(null);
      try {
        const outcome = await requestRepull(projectId, datastreamId, days);
        setDays(null);
        onQueued(outcome);
      } catch (reason) {
        setError(
          "The re-collection could not be queued. " +
            (reason instanceof Error ? reason.message : "The request failed."),
        );
      } finally {
        setBusy(false);
      }
    },
  };
}
