/**
 * Ask an authorization for history it has not collected yet.
 *
 * WHAT WAS MISSING, AND IT WAS NOT THE SURFACE. `POST /api/connections/{id}/backfill`
 * has validated 1..365 days, split the range into windows of at most 31 days and
 * enqueued one job per window since Story 3.2. `execution-substrate.md:88-97`
 * cites that behaviour by name and describes the case it serves as "a human
 * waiting for the answer in the console". No screen ever pushed it.
 *
 * It could not. `data_surface.py` joined `app.connection_ref` and read four of
 * its columns without ever selecting its id, so a Source Account had no way to
 * name the connection it IS. The repair is one column plus this panel — and the
 * order matters: no amount of front-end work could have surfaced an action whose
 * address was never on the wire.
 *
 * BACKFILL IS NOT SCHEDULING, and this panel must not blur that. `Processing`
 * owns how often a plan runs; AI-144 records the same boundary for the
 * Datastream (`DatastreamReloadPanel.tsx`), which is why neither surface grows a
 * "run now" button. This asks for a one-off span of history, the server refuses
 * to auto-trigger it, and the vocabulary stays `backfill`.
 *
 * THE WINDOWS ARE THE POINT. A 365-day ask is twelve units of work, not one long
 * one, and the server returns that list with each window's job id and whether it
 * was deduplicated against an enqueue that already exists. Hiding it behind
 * "queued" would drop the only evidence that says what was really asked for —
 * including the windows that did nothing because the work was already pending.
 */
import { useState } from "react";
import { Button, EvidenceRows, Field, Input, Panel, PanelHeader, Status, Timestamp } from "../ui";
import { apiFetch } from "../lib/apiFetch";

/** The server's own bounds (`account_topology.validate_backfill_days`). Mirrored
 *  so an impossible ask is refused before a round trip — the server refuses it
 *  either way, with `invalid_days`. Mirrored, never re-decided: if the two ever
 *  disagree the server wins, and the refusal below quotes it. */
const MIN_DAYS = 1;
const MAX_DAYS = 365;

/** The server's window size (`compute_backfill_windows`). Used ONLY to say in
 *  advance how many units of work the ask becomes. The authoritative list is the
 *  one the response carries — this is a forecast, and it is labelled as one. */
const WINDOW_DAYS = 31;

export interface BackfillWindow {
  date_from?: unknown;
  date_to?: unknown;
  job_id?: unknown;
  state?: unknown;
  deduplicated?: unknown;
}

/** The refusal to state, or null when the ask is answerable. */
export function daysRefusal(raw: string): string | null {
  if (!raw.trim()) return "State how many days of history to collect.";
  if (!/^\d+$/.test(raw.trim())) return "Days must be a whole number.";
  const days = Number(raw);
  if (days < MIN_DAYS) return `Ask for at least ${MIN_DAYS} day.`;
  if (days > MAX_DAYS) {
    return `${days} days is beyond the ${MAX_DAYS}-day ceiling this authorization accepts.`;
  }
  return null;
}

/** How many windows the server will cut this ask into. Stated before asking,
 *  because "365 days" and "12 separate pulls" are different things to consent to. */
export function windowCount(days: number): number {
  return Math.ceil(days / WINDOW_DAYS);
}

/** One ask, as it was answered, with the instant it was made. */
interface BackfillAsk {
  at: string;
  windows: BackfillWindow[];
}

/**
 * What this session has already asked for, kept beyond one mount.
 *
 * The list lived in `useState`, and this panel is unmounted the instant the
 * reader changes tab. So walking from `Health` to `Used by` and back showed an
 * empty form and no trace of the twelve pulls just requested — which reads as
 * "nothing happened", on the one screen whose whole job is to say what was
 * asked. Module-level and keyed by the connection, the same shape `TopBar` uses
 * to keep object names across screens.
 *
 * It is a record of what THIS session asked, never a substitute for a server
 * read: a reload empties it, and the panel says so rather than implying it is
 * reading the queue.
 */
const ASKED = new Map<string, BackfillAsk>();

/** Test seam. The cache outlives a `render()`, so a case that asserts the empty
 *  panel must be able to start from nothing. */
export function resetBackfillAsks(): void {
  ASKED.clear();
}

export default function SourceBackfillPanel({
  connectionId,
  label,
  usedByHref,
}: {
  connectionId: string;
  label: string;
  /** The `Used by` tab of the same Source Account, when this console can open
   *  it. It answers the question the enqueued windows raise — WHICH Datastreams
   *  this history is being collected for — and it is the destination the
   *  success banner names. Built by the caller through `buildPath`; absent
   *  rather than composed here. */
  usedByHref?: string | null;
}) {
  const [days, setDays] = useState("");
  const [pending, setPending] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const [asked, setAsked] = useState<BackfillAsk | null>(() => ASKED.get(connectionId) ?? null);
  const windows = asked?.windows ?? null;

  const refusal = daysRefusal(days);
  const forecast = refusal ? null : windowCount(Number(days));

  async function ask() {
    setPending(true);
    setFailure(null);
    // The previous ask is NOT cleared here. It really happened, and a refusal of
    // the next one does not un-enqueue it; blanking the list while a second ask
    // is in flight was how a person lost the only record of the first.
    try {
      const response = await apiFetch(
        `/api/connections/${encodeURIComponent(connectionId)}/backfill`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ days: Number(days) }),
          cache: "no-store",
        },
      );
      const body: unknown = await response.json().catch(() => null);
      if (!response.ok) {
        const message =
          body && typeof body === "object" && "message" in body
            ? String((body as { message: unknown }).message)
            : `The backfill was refused (HTTP ${response.status}).`;
        setFailure(message);
        return;
      }
      const listed =
        body && typeof body === "object" && Array.isArray((body as { windows?: unknown }).windows)
          ? ((body as { windows: BackfillWindow[] }).windows)
          : [];
      const answered: BackfillAsk = { at: new Date().toISOString(), windows: listed };
      ASKED.set(connectionId, answered);
      setAsked(answered);
    } catch (reason) {
      setFailure(reason instanceof Error ? reason.message : "The backfill could not be requested.");
    } finally {
      setPending(false);
    }
  }

  return (
    <Panel flush>
      <PanelHeader
        title="Collect earlier history"
        description={`Ask ${label} for days it has not pulled yet. This is a one-off backfill — it does not change how often this authorization runs.`}
      />
      <div className="grid gap-4 p-5">
        <Field label="Days of history" hint={`Between ${MIN_DAYS} and ${MAX_DAYS}.`}>
          {({ id }) => (
            <Input
              id={id}
              inputMode="numeric"
              value={days}
              onChange={(event) => setDays(event.target.value)}
              placeholder="90"
            />
          )}
        </Field>

        {/* Said BEFORE the ask, not after: consenting to "365 days" and
            consenting to "12 separate pulls against this provider" are not the
            same consent, and the second is the one that spends quota. */}
        {forecast !== null && (
          <Status as="block" tone="info" title={`${days} days becomes ${forecast} Run${forecast === 1 ? "" : "s"}`}>
            The server cuts the range into windows of at most {WINDOW_DAYS} days and enqueues one
            Run per window. A window already pending is not enqueued twice.
          </Status>
        )}
        {refusal && days.trim() !== "" && (
          <Status as="block" tone="warning" title="Not asked yet">{refusal}</Status>
        )}
        {failure && (
          <Status as="block" tone="error" title="The backfill was refused">{failure}</Status>
        )}

        <div>
          <Button variant="secondary" disabled={Boolean(refusal) || pending} onClick={() => void ask()}>
            {pending ? "Requesting…" : "Request backfill"}
          </Button>
        </div>

        {asked !== null && windows !== null && (
          windows.length === 0 ? (
            <Status as="block" tone="warning" title="Nothing was enqueued">
              The server accepted the request and returned no window. Nothing is running.
              Asked <Timestamp value={asked.at} />.
            </Status>
          ) : (
            <div className="grid gap-3">
              <Status
                as="block"
                tone="success"
                title={`${windows.length} window${windows.length === 1 ? "" : "s"} enqueued`}
                action={usedByHref ? <Button asChild variant="secondary" size="sm"><a href={usedByHref}>Which Datastreams read this account</a></Button> : undefined}
              >
                {/* WHAT THE SENTENCE USED TO SAY, and why it could not be kept:
                    « Follow them in this authorization's jobs » named a screen
                    that does not exist. A Source Account has four tabs —
                    Overview, Accounts, Health, Used by — and none of them is a
                    job list. And there is nowhere else either: `enqueue_backfill`
                    calls `enqueue_pull` with no `datastream_id`
                    (`account_topology.py`), nothing ever sets that column
                    afterwards, and both the Runs tab and the day-by-day extract
                    registry read per Datastream. So the honest destination is
                    THIS list, and the gap is named rather than papered over with
                    a link that opens something adjacent. */}
                Each window is a separate pull, asked of this authorization. It names no Datastream,
                so no Datastream&apos;s Runs tab lists it — this list is the record of what was asked,
                and it is kept while you move between the tabs of this Source Account.
                Asked <Timestamp value={asked.at} />.
              </Status>
              {/* Every window, including the deduplicated ones. A window that did
                  nothing because the work was already pending is the answer to
                  "why did my backfill do less than I asked", and it only exists
                  in this response. */}
              {windows.map((window, index) => (
                <EvidenceRows
                  key={`${String(window.date_from ?? index)}-${String(window.date_to ?? index)}`}
                  source={{
                    from: window.date_from,
                    to: window.date_to,
                    job: window.job_id,
                    state: window.state,
                    already_pending: window.deduplicated === true,
                  }}
                  label={`Backfill window ${index + 1}`}
                />
              ))}
            </div>
          )
        )}
      </div>
    </Panel>
  );
}
