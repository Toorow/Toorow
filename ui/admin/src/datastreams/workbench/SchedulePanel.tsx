/**
 * Configure WHEN a Datastream runs, and HOW MUCH history each run fetches.
 *
 * AI-119. Until this panel there was no screen at all: `PATCH /api/datastreams/{id}`
 * accepted the cadence and the window, and the only component that referenced
 * either field was `FlowSummary.tsx` — mounted nowhere and already recorded as
 * orphaned debt (deleted 2026-08-17, chantier 67-14). A setting reachable only by
 * curl is not a setting.
 *
 * TWO DIMENSIONS, AND THEY ARE NOT THE SAME KNOB.
 *   - Frequency — how OFTEN a run happens.
 *   - Window — how much history EACH run fetches. Pulling seven days every night
 *     rewrites the week, and that is correct by construction: the raw zone is
 *     append-only and staging supersedes by `pull_id`, so exactly one row per
 *     business key survives.
 *
 * The server reports ONE effective window plus its source, because
 * `refetch_days` is a legacy alias for the same setting rather than a second
 * one — showing both as separate fields would let someone set one while the
 * dispatcher obeys the other.
 *
 * WHERE THIS BELONGS — AMENDED 2026-08-05 (Jean), APPLIED BY STORY 58.9.
 * `datastream-workbench-and-wizard.md` used to show cadence in two tabs and say
 * nowhere that it could be CHANGED -- which is how the setting reached
 * production reachable only by an API call. Its section "Where the schedule is
 * edited" settled it on 2026-08-01, giving the edit to `Processing` and leaving
 * `Overview` cadence as POSTURE ONLY. That clause is reversed. The reference is
 * the Overview of an Adverity Datastream, whose `Scheduling` is the FIRST
 * column: "il est perdu dans Processing, j'ai même pas les informations
 * essentielles". Cadence, arrival hour and next run are now seen AND SET on
 * `Overview` as well; `Processing` keeps the execution plan, of which the
 * schedule is one parameter.
 *
 * SO THIS PANEL IS MOUNTED TWICE, AND THAT IS THE POINT. `Processing` mounts it
 * and the `Collect` stage of `Overview` mounts it. Two components would be two
 * regimes of consent and two ways to disagree; one component means THREE DOORS,
 * ONE ROW — this panel, the REST seam and the MCP tools all write the same
 * `app.datastreams` / `app.datastream_schedule_state` through one
 * `PUT /api/datastreams/{id}/schedule`. The `Incomplete if` list of that
 * document now holds the reverse open: cadence, arrival hour or next run ABSENT
 * from `Overview`, or the two doors writing two rows.
 *
 * AND WHETHER IT IS ARMED AT ALL — LOT D1 (issue #68).
 * `enabled` and `lifecycle_state` were typed on the `Schedule` interface below
 * and rendered NOWHERE, while the `PUT` door refused `enabled` outright. The
 * consequence is the whole reason this panel is being corrected: a person could
 * set a frequency, an arrival hour, a retrieval window, an extraction offset and
 * a next run on a Datastream that is stopped, press `Save schedule`, read
 * "Schedule saved.", and nothing on the screen said it would never run.
 * Measured on the live base: `enabled = false` on the Datastream that prompted
 * the review.
 *
 * The state is drawn FIRST, above the settings it qualifies, because it decides
 * what all of them mean. It is not `enabled` recombined here: the server derives
 * `run_state` from the same two columns the dispatcher reads
 * (`d.enabled = TRUE AND d.lifecycle_state = 'active'`), so the console, the
 * seam and the model say one word about one row. `draft` is NOT flattened into
 * "stopped": a Datastream nobody activated and one somebody paused take
 * different gestures, and this control performs only the second — activation
 * belongs to `publish_activate_mutation`, and a screen that set `enabled` on a
 * draft would be a second activation authority telling a comfortable lie.
 */
import { useEffect, useState } from "react";
import { Badge, Button, Field, formatTimestamp, Input, NativeSelect, NO_VALUE, Panel, PanelHeader, Status } from "../../ui";
import { ConfirmDialog } from "../../ui/ConfirmDialog";
import { apiFetch } from "../../lib/apiFetch";

/** The cadences the database accepts. Mirrors the CHECK constraint on
 *  `app.datastreams.schedule_mode` — offering a value the database refuses would
 *  surface as an opaque write error. `weekly` was added by migration 204. */
const CADENCES = [
  { value: "nightly", label: "Every night (daily)" },
  { value: "weekly", label: "Once a week (weekly)" },
  { value: "hourly", label: "Every hour (hourly)" },
  { value: "manual", label: "On demand only (manual)" },
] as const;

/** The cadences an arrival hour means something for (story 57.8, A3). An hourly
 *  Datastream re-pulls the accumulating day and has no single moment of arrival;
 *  a manual one has no clock at all. The field is ABSENT for those two and a
 *  sentence says why — a control greyed out without a word reads as a bug. */
const ARRIVAL_HOUR_CADENCES = ["nightly", "weekly"];

/** Why the arrival hour does not apply, per cadence. Named rather than derived
 *  from a template, so each sentence answers the question that cadence raises. */
const ARRIVAL_NOT_APPLICABLE: Record<string, string> = {
  hourly:
    "This Datastream runs every hour, so there is no single moment for the day to " +
    "arrive. The grain stays a date; an hourly cadence re-fetches the day as it fills.",
  manual:
    "This Datastream only runs on demand, so nothing arrives on a clock. Switch to a " +
    "daily or weekly frequency to choose an arrival hour.",
};

/** What the server says happens after a failed pull, in the words a person reads.
 *  Kept in step with `schedule_mcp._ON_FAILURE`: the console never invents an
 *  outcome the dispatcher does not implement. */
const ON_FAILURE_COPY: Record<string, string> = {
  retry_at_next_hour:
    "Retries at the next hour, once. If that attempt also fails, the Datastream waits " +
    "for its next arrival hour rather than retrying again.",
  next_hourly_run: "Nothing extra is scheduled — the next hourly run is an hour away anyway.",
  nothing_is_retried: "Nothing is retried. This Datastream only runs when someone asks it to.",
};

/**
 * WHAT THE SERVER SAYS ABOUT WHETHER THIS DATASTREAM RUNS, in the words a person
 * reads. Kept in step with `schedule_mcp.RUN_STATES`, and DERIVED there rather
 * than here: two columns recombined in the console would be free to disagree
 * with the dispatcher's own predicate.
 *
 * `label` is the state. `detail` is what it means for the days that are or are
 * not being collected. `action` is the verb of the control, and `null` means
 * there is no control — because the gesture that repairs that state is somewhere
 * else, and `detail` names it.
 */
const RUN_STATE: Record<
  string,
  { label: string; tone: "success" | "warning" | "neutral"; detail: string; action: string | null }
> = {
  running: {
    label: "Collecting on this schedule",
    tone: "success",
    detail: "Each run below happens. Stopping it keeps every setting and collects nothing.",
    action: "Stop collecting",
  },
  paused: {
    label: "Configured, not collecting",
    tone: "warning",
    detail:
      "Every setting below is saved and none of it runs — no day is collected while it is " +
      "stopped, and the days it misses come back only through a re-collection you ask for.",
    action: "Start collecting",
  },
  not_activated: {
    label: "Not activated yet",
    tone: "neutral",
    detail:
      "This Datastream has never been published, so there is no schedule to start here. " +
      "Publishing it from the setup wizard is what activates it, and activation starts it.",
    action: null,
  },
  archived: {
    label: "Archived",
    tone: "neutral",
    detail:
      "An archived Datastream collects nothing and cannot be started from here. Restoring it " +
      "is what makes it runnable again.",
    action: null,
  },
};

/**
 * THE SAME QUESTION, ASKED OF A SOURCE NOBODY FETCHES (amendment 1 of the
 * 2026-08-11 review). A `managed_feed` has no cadence to arm, so "armed" cannot
 * mean "the clock is wound". What it can honestly mean is measured, not
 * invented: `server/core/import_runner.py` reads neither `enabled` nor
 * `lifecycle_state`, and `server/inbound/receipt.py` reads only `org_id` — so
 * the flag that starts and stops a pull gates NOTHING on a pushed file. The
 * question is not hidden and it is not answered with a control that would stop
 * nothing: it is answered.
 */
const PUSHED_RUN_STATE: Record<string, string> = {
  running:
    "Nothing is fetched on a clock here, so there is no schedule to start or stop. A file " +
    "pushed to this Datastream is imported when it arrives.",
  paused:
    "This Datastream is marked stopped, which stops a pull — and nothing pulls here. A file " +
    "pushed to it is still imported when it arrives.",
  not_activated:
    "This Datastream has never been published. Publishing it from the setup wizard is what " +
    "activates it; nothing on this panel does.",
  archived: "This Datastream is archived. Restoring it is what makes it usable again.",
};

export interface Schedule {
  cadence: string;
  enabled: boolean;
  lifecycle_state: string;
  /** Derived by the server from the two columns above plus `archived_at`, so one
   *  word crosses the seam instead of a recombination per door. */
  run_state?: string;
  archived?: boolean;
  window_days: number;
  window_offset_days: number;
  window_source: string;
  arrival_hour_local: number | null;
  arrival_hour_source: string;
  timezone: string;
  timezone_source: string;
  on_failure: string;
  retry_count: number;
  next_run_at: string | null;
  last_run_at: string | null;
  never_ran: boolean;
}

/** `06:00`, or the em dash when no hour was chosen. Never `0` standing in for an
 *  absence: `0` is a legal arrival hour (local midnight). */
function hourLabel(hour: number | null): string {
  return hour === null || hour === undefined ? "—" : `${String(hour).padStart(2, "0")}:00`;
}

const HOUR_OPTIONS = Array.from({ length: 24 }, (_, hour) => hour);

type Load =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ok"; schedule: Schedule };

function when(value: string | null): string {
  // `formatTimestamp` answers the dash for an absence and the raw text for a
  // value that is not a time, so the three branches this held are its own.
  return value ? formatTimestamp(value) : NO_VALUE;
}

/** An ISO instant the server accepts, from the value a datetime-local input gives. */
function toIso(local: string): string | null {
  if (!local) return null;
  const parsed = new Date(local);
  return Number.isNaN(parsed.getTime()) ? null : parsed.toISOString();
}

function toLocalInput(iso: string | null): string {
  if (!iso) return "";
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${parsed.getFullYear()}-${pad(parsed.getMonth() + 1)}-${pad(parsed.getDate())}` +
    `T${pad(parsed.getHours())}:${pad(parsed.getMinutes())}`;
}

export default function SchedulePanel({
  projectId,
  datastreamId,
  mode,
  deliveryChannels,
}: {
  projectId: string;
  datastreamId: string;
  /**
   * `app.datastreams.source_kind` — amendment 1 of the 2026-08-11 review.
   *
   * A SOURCE THAT IS PUSHED HAS NO CADENCE. The five settings below all describe
   * one thing: the pull WINDOW — how often to reach out, how far back to reach,
   * how much lag to allow, at what hour to land, when to go next. A
   * `managed_feed` reaches out for nothing: its file is pushed, by an upload or
   * by an inbound channel. Jean, 2026-08-11: « tu ne peux pas mettre un truc qui
   * tourne alors que c'est un fichier plat uploadé, il ne peut pas avoir de mise
   * à jour auto ». Measured the same day: 4 of the 6 live Datastreams are
   * `managed_feed`, and all 4 were carrying the five pull settings.
   *
   * What replaces them is not an apology — it is the answer this mode actually
   * has: by which door the file arrives, and when the last one did.
   */
  mode?: string | null;
  /** `config.channels`, from the header. Names the inbound door when there is
   *  one; empty means the file arrives by upload and nothing else. */
  deliveryChannels?: string[];
}) {
  const pushedSource = mode === "managed_feed";
  const [load, setLoad] = useState<Load>({ status: "loading" });
  const [cadence, setCadence] = useState("");
  const [windowDays, setWindowDays] = useState("");
  const [windowOffsetDays, setWindowOffsetDays] = useState("1");
  const [nextRun, setNextRun] = useState("");
  const [arrivalHour, setArrivalHour] = useState("");
  const [confirming, setConfirming] = useState(false);
  const [saving, setSaving] = useState(false);
  // `null` means nobody is being asked anything. `true` / `false` is the act
  // awaiting consent, held rather than derived from the current state so the
  // dialog names what it is about to do even while the write is in flight.
  const [armingTo, setArmingTo] = useState<boolean | null>(null);
  const [arming, setArming] = useState(false);
  const [feedback, setFeedback] = useState<{ tone: "success" | "error"; message: string } | null>(
    null,
  );

  useEffect(() => {
    let cancelled = false;
    setLoad({ status: "loading" });
    void (async () => {
      try {
        const resp = await apiFetch(
          `/api/datastreams/${encodeURIComponent(datastreamId)}/schedule` +
            `?project_id=${encodeURIComponent(projectId)}`,
        );
        if (!resp.ok) {
          // "we could not ask" is never rendered as "there is nothing".
          if (!cancelled) setLoad({ status: "error", message: `HTTP ${resp.status}` });
          return;
        }
        const schedule = (await resp.json()) as Schedule;
        if (cancelled) return;
        setLoad({ status: "ok", schedule });
        setCadence(schedule.cadence ?? "");
        setWindowDays(String(schedule.window_days ?? ""));
        setWindowOffsetDays(String(schedule.window_offset_days ?? 1));
        setNextRun(toLocalInput(schedule.next_run_at));
        // `null` becomes the empty option, not `0`: the control has to be able
        // to show "nobody chose an hour" as distinct from "midnight".
        setArrivalHour(
          schedule.arrival_hour_local === null || schedule.arrival_hour_local === undefined
            ? ""
            : String(schedule.arrival_hour_local),
        );
      } catch (err) {
        if (!cancelled) {
          setLoad({ status: "error", message: err instanceof Error ? err.message : String(err) });
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [projectId, datastreamId]);

  async function save() {
    setSaving(true);
    setFeedback(null);
    try {
      const body: Record<string, unknown> = { project_id: projectId };
      if (cadence) body.cadence = cadence;
      if (windowDays) body.window_days = Number(windowDays);
      if (windowOffsetDays) body.window_offset_days = Number(windowOffsetDays);
      // Sent for the cadences that can honour it (A3), INCLUDING when it is
      // empty: an empty control means "no arrival hour", and `null` is how that
      // erasure crosses the seam. Omitting the key made the server keep the old
      // hour while the dialog had just announced "06:00 → Not set" and the panel
      // then reported "Schedule saved." — clearing an hour was impossible, and
      // the screen said otherwise.
      if (ARRIVAL_HOUR_CADENCES.includes(cadence)) {
        body.arrival_hour = arrivalHour === "" ? null : Number(arrivalHour);
      }
      const iso = toIso(nextRun);
      if (iso) body.next_run_at = iso;

      const resp = await apiFetch(
        `/api/datastreams/${encodeURIComponent(datastreamId)}/schedule`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        },
      );
      const payload = await resp.json();
      if (!resp.ok) {
        // The server's refusals each name what to do — an unknown cadence, an
        // absurd window, a Datastream that was never activated. Showing that
        // message beats replacing it with a generic failure.
        setFeedback({ tone: "error", message: payload?.message ?? `HTTP ${resp.status}` });
        return;
      }
      const saved = payload as Schedule;
      setLoad({ status: "ok", schedule: saved });
      setWindowOffsetDays(String(saved.window_offset_days ?? 1));
      setNextRun(toLocalInput(saved.next_run_at));
      setArrivalHour(
        saved.arrival_hour_local === null || saved.arrival_hour_local === undefined
          ? ""
          : String(saved.arrival_hour_local),
      );
      // "Schedule saved." on a stopped Datastream is the sentence that made the
      // defect invisible: it is true, and it lets a person walk away believing
      // a run was armed. The state travels with the confirmation of the write.
      setFeedback({
        tone: "success",
        message:
          saved.run_state === "paused"
            ? "Schedule saved. This Datastream is stopped, so none of it runs until it is started."
            : "Schedule saved.",
      });
    } catch (err) {
      setFeedback({
        tone: "error",
        message: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setSaving(false);
      setConfirming(false);
    }
  }

  /**
   * START OR STOP, AND NOTHING ELSE (lot D1).
   *
   * The body carries `enabled` alone. Sending the form's current values with it
   * would commit edits the person has typed and not confirmed — pressing
   * `Stop collecting` would silently write a frequency they were still looking
   * at. `set_schedule` writes only what it is given, and this is the door's half
   * of that contract.
   */
  async function setArmed(next: boolean) {
    setArming(true);
    setFeedback(null);
    try {
      const resp = await apiFetch(
        `/api/datastreams/${encodeURIComponent(datastreamId)}/schedule`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ project_id: projectId, enabled: next }),
        },
      );
      const payload = await resp.json();
      if (!resp.ok) {
        // The refusals name the gesture that repairs: a Datastream that was
        // never activated, one that is archived, an org over its trial
        // allowance with its own count in the sentence.
        setFeedback({ tone: "error", message: payload?.message ?? `HTTP ${resp.status}` });
        return;
      }
      setLoad({ status: "ok", schedule: payload as Schedule });
      setFeedback({
        tone: "success",
        message: next
          ? "Collecting. The next run happens at the moment set below."
          : "Stopped. Every setting is kept and nothing runs until it is started again.",
      });
    } catch (err) {
      setFeedback({
        tone: "error",
        message: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setArming(false);
      setArmingTo(null);
    }
  }

  if (load.status === "loading") {
    return (
      <Panel>
        <PanelHeader title={pushedSource ? "How this file arrives" : "Schedule"} />
        <Status tone="neutral">
          {pushedSource ? "Reading the last arrival…" : "Loading the schedule…"}
        </Status>
      </Panel>
    );
  }

  if (load.status === "error") {
    return (
      <Panel>
        <PanelHeader title="Schedule" />
        <Status tone="error">
          Could not read the schedule ({load.message}). It is unknown, not unset — nothing
          here has been changed.
        </Status>
      </Panel>
    );
  }

  const { schedule } = load;
  // A partial or unexpected payload must not take down the tab that hosts this
  // panel. Found by a neighbouring Processing-tab test whose stub answered
  // without `window_source`: the panel threw and the whole page went with it.
  // A control that cannot render its own state is a control that must say so.
  if (!schedule || typeof schedule !== "object") {
    return (
      <Panel>
        <PanelHeader title="Schedule" />
        <Status tone="error">
          The schedule could not be read in a usable shape. Nothing has been changed.
        </Status>
      </Panel>
    );
  }

  // The word the server derived from the pair the dispatcher reads. An ABSENT
  // key is a read that did not answer, and it is never rendered as "stopped":
  // the two statements would look identical and take opposite gestures.
  const runState =
    typeof schedule.run_state === "string" ? RUN_STATE[schedule.run_state] : undefined;
  const armTarget = schedule.run_state !== "running";

  /**
   * AN ACT'S WEIGHT FOLLOWS ITS CONSEQUENCE — finding D-8 of the visual review
   * #69, and the last of it left open.
   *
   * The two acts of this panel were `variant={armTarget ? "default" : "secondary"}`
   * against a `Save schedule` with no variant at all, which is `default`. So a
   * RUNNING Datastream drew `Stop collecting` as the quiet button and `Save
   * schedule` as the rose one — the act that loses days read lighter than the act
   * that saves a form. And a STOPPED one drew both in rose, which by the
   * primitive's own rule (`components/ui/button.tsx`: "two rose buttons side by
   * side is two recommendations, which is none") is no recommendation at all.
   *
   * `destructive` is not reserved here for the irreversible: `Disconnect Google`
   * and the datastream `Archive` of the component sheet both carry it, and
   * archiving is described there as "Collection stops. Published data stays
   * readable" — the same consequence class as this. And the consent this trigger
   * opens ALREADY declares itself destructive (`destructive={armingTo === false}`
   * below, which `ConfirmDialog` renders as the red confirm button), so the
   * trigger and its confirmation were two registers for one act.
   *
   * Rose then goes to whichever act is the step to take. On a stopped Datastream
   * that is starting it — settings saved onto something that never runs are not a
   * next step — so the footer steps down to `secondary` there and keeps the
   * panel's single recommendation intact.
   */
  const armVariant = armTarget ? "default" : "destructive";
  const saveVariant = runState?.action && armTarget ? "secondary" : "default";

  /**
   * A PUSHED SOURCE, AND THE FIVE PULL SETTINGS ARE NOT DRAWN AT ALL.
   *
   * Not greyed, not "not applicable" beside a filled-in `Every night (daily)`:
   * absent. A control a person can read a value out of is a control they will
   * believe, and `Every night` on a workbook that will never be fetched is a
   * promise the dispatcher has no way to keep.
   *
   * The two facts that ARE true of this mode take their place: which door the
   * file comes through, and when the last one came. `never_ran` is the server's
   * own flag, so "nothing has arrived yet" is a measurement and not a dash.
   */
  if (pushedSource) {
    const channels = (deliveryChannels ?? []).filter(Boolean);
    return (
      <Panel data-testid="schedule-panel">
        <PanelHeader
          title="How this file arrives"
          description="This Datastream is fed by files that are pushed to it. Nothing is fetched on a clock, so there is no frequency, no retrieval window and no next run to set."
        />
        <div className="grid gap-4 sm:grid-cols-2">
          <div>
            <span className="mb-1 block text-label text-text-secondary">Arrives by</span>
            <span className="text-ui text-text">
              {channels.length
                ? `Upload, and ${channels.join(", ")}`
                : "Upload only — no inbound channel is declared"}
            </span>
            <span className="mt-1 block text-caption text-text-secondary">
              {channels.length
                ? "The address a channel delivers to is issued on Overview, with the delivery credential it accepts."
                : "Declaring an inbound channel gives this Datastream an address files can be sent to."}
            </span>
          </div>
          <div>
            <span className="mb-1 block text-label text-text-secondary">Last arrival</span>
            <span className="text-ui text-text">
              {schedule.never_ran ? "Nothing has arrived yet" : when(schedule.last_run_at)}
            </span>
            <span className="mt-1 block text-caption text-text-secondary">
              Read from this Datastream&apos;s own run ledger, not from a clock.
            </span>
          </div>
          {/* THE QUESTION IS ANSWERED, NOT HIDDEN (lot D1). A pushed source has
              no clock to arm, so "started" cannot mean what it means for a pull
              — and the honest answer is measured rather than guessed: nothing
              in the import path reads the flag that starts and stops a pull. A
              control here would stop nothing while claiming to. */}
          <div className="sm:col-span-2">
            <span className="mb-1 block text-label text-text-secondary">Started or stopped</span>
            <span className="text-ui text-text" data-testid="schedule-run-state">
              {typeof schedule.run_state === "string" && PUSHED_RUN_STATE[schedule.run_state]
                ? PUSHED_RUN_STATE[schedule.run_state]
                : "Whether this Datastream is started could not be read. It is unknown, not stopped."}
            </span>
          </div>
        </div>
      </Panel>
    );
  }

  return (
    <Panel>
      <PanelHeader
        title="Schedule"
        description="When this Datastream runs, and how much history each run fetches."
      />

      {/* WHETHER ANY OF THE SETTINGS BELOW WILL EVER HAPPEN — FIRST, because it
          decides what all of them mean. A full schedule under "Configured, not
          collecting" is the state the live base was in, and it read exactly
          like a running one. */}
      <div
        className="flex flex-wrap items-start justify-between gap-3 rounded-md border border-divider-base p-4"
        data-testid="schedule-run-state"
      >
        <div className="flex max-w-xl flex-col gap-1">
          {runState ? (
            <span>
              <Badge tone={runState.tone}>{runState.label}</Badge>
            </span>
          ) : (
            <span>
              <Badge tone="neutral">Not read</Badge>
            </span>
          )}
          <span className="text-caption text-text-secondary">
            {runState
              ? runState.detail
              : "Whether this Datastream is collecting could not be read. It is unknown, not " +
                "stopped, so nothing here offers to change it."}
          </span>
        </div>
        {/* NO CONTROL WHERE THIS PANEL IS NOT THE AUTHORITY. A draft is
            activated by publishing it and an archived Datastream by restoring
            it; offering a button that the server would refuse would name a
            gesture that does not exist here. The sentence above names the one
            that does. */}
        {runState?.action ? (
          <Button
            variant={armVariant}
            disabled={arming}
            onClick={() => setArmingTo(armTarget)}
            data-testid="schedule-arm"
          >
            {arming ? "Saving…" : runState.action}
          </Button>
        ) : null}
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Frequency">
          {(props) => (
            <NativeSelect
              {...props}
              value={cadence}
              onChange={(event) => setCadence(event.target.value)}
              data-testid="schedule-cadence"
            >
              {CADENCES.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </NativeSelect>
          )}
        </Field>

        {/* THE HOUR THE DAY IS EXPECTED TO ARRIVE (story 57.8). Second, right
            after the frequency, because the two answer one question together:
            how often, and when. */}
        {ARRIVAL_HOUR_CADENCES.includes(cadence) ? (
          <Field
            label="Arrival hour"
            hint={
              <>
                The hour of the project's day this pull should land.{" "}
                <span data-testid="schedule-arrival-zone">
                  Read in <strong>{schedule.timezone}</strong>
                  {schedule.timezone_source === "project_preference"
                    ? " (this project's reporting timezone)"
                    : " (no reporting timezone confirmed for this project)"}
                </span>
                , never in your browser's.
              </>
            }
          >
            {(props) => (
              <NativeSelect
                {...props}
                value={arrivalHour}
                onChange={(event) => setArrivalHour(event.target.value)}
                data-testid="schedule-arrival-hour"
              >
                <option value="">Not set</option>
                {HOUR_OPTIONS.map((hour) => (
                  <option key={hour} value={String(hour)}>
                    {hourLabel(hour)}
                  </option>
                ))}
              </NativeSelect>
            )}
          </Field>
        ) : (
          // A3: absent AND explained. Rendering the control `disabled` without a
          // word reads as a broken field rather than a refusal.
          <div className="flex flex-col gap-1 text-sm" data-testid="schedule-arrival-not-applicable">
            <span className="font-medium">Arrival hour</span>
            <span className="text-text-secondary">
              {ARRIVAL_NOT_APPLICABLE[cadence] ?? ARRIVAL_NOT_APPLICABLE.manual}
            </span>
          </div>
        )}

        {/* WHAT HAPPENS WHEN A PULL DOES NOT ARRIVE. Read from the server, never
            asserted here: the console must not promise a catch-up the
            dispatcher does not make. */}
        <div className="flex flex-col gap-1 text-sm">
          <span className="font-medium">If a Run fails</span>
          <span className="text-text-secondary" data-testid="schedule-on-failure">
            {ON_FAILURE_COPY[schedule.on_failure] ?? ON_FAILURE_COPY.nothing_is_retried}
          </span>
          {/* Shown only once a catch-up has actually been armed. A permanent
              `0` would read as a measurement where there is nothing to report. */}
          {Number(schedule.retry_count) > 0 ? (
            <span data-testid="schedule-retry-count">
              <Badge tone="warning">
                {schedule.retry_count} catch-up run{schedule.retry_count === 1 ? "" : "s"} armed
              </Badge>
            </span>
          ) : null}
        </div>

        {ARRIVAL_HOUR_CADENCES.includes(cadence) && arrivalHour === "" ? (
          <Status tone="neutral">
            No arrival hour set — this Datastream runs at the platform tick, which places it
            at local midnight. Choose an hour above to fix when the day should land.
          </Status>
        ) : null}

        <Field
          label="History fetched each run"
          hint={
            <>
              Days. Fetching 7 days every night rewrites the week — later rows supersede
              earlier ones, so no duplicate survives.
              {String(schedule.window_source ?? "").includes("legacy") ? (
                <> Currently inherited from <code>refetch_days</code>; saving pins it explicitly.</>
              ) : null}
            </>
          }
        >
          {(props) => (
            <Input
              {...props}
              type="number"
              min={1}
              max={365}
              value={windowDays}
              onChange={(event) => setWindowDays(event.target.value)}
              data-testid="schedule-window"
            />
          )}
        </Field>

        <Field
          label="Extraction offset (lag)"
          hint="Days back from today where window ends (default 1 = yesterday; 3 = J-3 for delayed sources like GSC)."
        >
          {(props) => (
            <Input
              {...props}
              type="number"
              min={1}
              max={90}
              value={windowOffsetDays}
              onChange={(event) => setWindowOffsetDays(event.target.value)}
              data-testid="schedule-offset"
            />
          )}
        </Field>

        <Field
          label="Next run"
          hint={
            cadence === "manual"
              ? "Datastream is set to on demand only — next run timestamp is disabled."
              : "Your local time. This instant decides when the next run happens."
          }
        >
          {(props) => (
            <Input
              {...props}
              type="datetime-local"
              disabled={cadence === "manual"}
              value={cadence === "manual" ? "" : nextRun}
              onChange={(event) => setNextRun(event.target.value)}
              data-testid="schedule-next-run"
            />
          )}
        </Field>

        <div className="flex flex-col gap-1 text-sm">
          <span className="font-medium">Last run</span>
          <span data-testid="schedule-last-run">
            {schedule.never_ran ? (
              <Badge tone="warning">Never run</Badge>
            ) : (
              when(schedule.last_run_at)
            )}
          </span>
        </div>
      </div>

      {feedback ? (
        <Status tone={feedback.tone === "success" ? "success" : "error"}>{feedback.message}</Status>
      ) : null}

      <div className="flex justify-end">
        <Button
          variant={saveVariant}
          onClick={() => setConfirming(true)}
          disabled={saving}
          data-testid="schedule-save"
        >
          {saving ? "Saving…" : "Save schedule"}
        </Button>
      </div>

      {/* THE SAME WRITE, THE SAME CONSENT. The MCP door onto this row is
          declared `confirmation_mode="human"` (`schedule_mcp.register`), and
          this panel wrote without asking anything: one write reached by two
          doors cannot have two regimes of consent. The scope is named BEFORE
          the write, with the count of what changes, not after. */}
      <ConfirmDialog
        open={confirming}
        onOpenChange={setConfirming}
        title="Change when this Datastream runs"
        description={
          <>
            This changes the schedule of <strong>{datastreamId}</strong>.{" "}
            {ARRIVAL_HOUR_CADENCES.includes(cadence) ? (
              <>
                Its arrival hour goes from{" "}
                <strong>
                  {schedule.arrival_hour_local === null
                    ? "Not set"
                    : hourLabel(schedule.arrival_hour_local)}
                </strong>{" "}
                to{" "}
                <strong>
                  {arrivalHour === ""
                    ? "Not set (local midnight)"
                    : hourLabel(Number(arrivalHour))}
                </strong>{" "}
                ({schedule.timezone}).{" "}
              </>
            ) : null}
            Moving a schedule can put an extra pull on the provider, and an extra pull spends
            provider quota against this project.{" "}
            {schedule.run_state === "paused" ? (
              <strong data-testid="schedule-confirm-paused">
                This Datastream is stopped: these settings are saved and none of them runs until
                it is started.
              </strong>
            ) : null}
          </>
        }
        evidence={{
          datastream: datastreamId,
          frequency: cadence,
          arrival_hour: ARRIVAL_HOUR_CADENCES.includes(cadence)
            ? arrivalHour === ""
              ? "Not set — cleared, runs at local midnight"
              : hourLabel(Number(arrivalHour))
            : "not applicable to this frequency",
          history_fetched_each_run: `${windowDays} day(s)`,
          extraction_offset: `${windowOffsetDays} day(s)`,
        }}
        evidenceLabel="What this write sets"
        confirmLabel="Save schedule"
        busy={saving}
        onConfirm={() => void save()}
        data-testid="schedule-confirm"
        cancelTestId="schedule-confirm-cancel"
        confirmTestId="schedule-confirm-accept"
      />

      {/* STARTING AND STOPPING IS THE MOST CONSEQUENTIAL WRITE THIS PANEL MAKES,
          and it is the one that had no consent at all — because it had no door.
          Each direction names its OWN consequence, because they are not the same
          one reversed: starting spends on the source account from the next run,
          and stopping loses days that come back only through a bounded
          re-collection somebody has to ask for, day by day. */}
      <ConfirmDialog
        open={armingTo !== null}
        onOpenChange={(open) => setArmingTo(open ? armingTo : null)}
        title={armingTo ? "Start collecting on this schedule" : "Stop collecting"}
        destructive={armingTo === false}
        description={
          armingTo ? (
            <>
              <strong>{datastreamId}</strong> starts running on the schedule shown below. Every
              run reaches its source account and spends against that account&apos;s quota — the
              first one happens at the next run shown here, not later.
            </>
          ) : (
            <>
              <strong>{datastreamId}</strong> stops running. Every setting is kept and nothing is
              collected: the days that pass while it is stopped come back only through a
              re-collection asked for day by day, and only as far back as the source still
              serves them.
            </>
          )
        }
        evidence={{
          datastream: datastreamId,
          from: runState?.label ?? "Not read",
          to: armingTo ? "Collecting on this schedule" : "Configured, not collecting",
          frequency: schedule.cadence,
          next_run: armingTo ? when(schedule.next_run_at) : "no longer honoured",
        }}
        evidenceLabel={armingTo ? "What starts" : "What stops"}
        confirmLabel={armingTo ? "Start collecting" : "Stop collecting"}
        busy={arming}
        onConfirm={() => void setArmed(armingTo === true)}
        data-testid="schedule-arm-confirm"
        cancelTestId="schedule-arm-confirm-cancel"
        confirmTestId="schedule-arm-confirm-accept"
      />
    </Panel>
  );
}
