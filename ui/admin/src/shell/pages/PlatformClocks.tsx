/**
 * Platform clocks — the sync between this interface and Cloud Scheduler.
 *
 * WHY THIS SCREEN EXISTS. Five Cloud Scheduler jobs drive the whole platform
 * (`dispatch-nightly`, `dispatch-hourly`, `reconcile-queues`, `poll-health`,
 * `drain-outbox`). Until now they existed ONLY in GCP: created by
 * `infra/gcp/provision_ad36_substrate.sh`, and known to no screen, no database
 * row and no test. Every consequence of that is silent — a job edited by hand,
 * or never created in a new environment, produces no signal at all, because a
 * clock that does not fire leaves NOTHING behind. That is `execution-substrate.md`
 * "Incomplete if" #2 in its purest form: *a scheduled run that does not happen
 * leaves no record that it did not happen*. Jean's requirement, literal: "on doit
 * avoir une SYNC entre l'interface et les cloud schedule".
 *
 * THE ONE RULE THIS SCREEN IS BUILT ON: DECLARED AND OBSERVED ARE NEVER MERGED.
 * Two columns, side by side, one per source of truth:
 *
 *   DECLARED   what this platform says the clock must be  (Postgres registry)
 *   OBSERVED   what Cloud Scheduler actually holds        (last reconciliation)
 *
 * and a VERDICT that is the comparison between them, never a state of either.
 * Nothing here repairs anything on its own: a drift stays visible until someone
 * chooses to apply the declaration, because silently re-imposing the declared
 * value destroys the only evidence that somebody changed a clock by hand.
 *
 * `unknown` IS NOT AN EMPTY STATE. When Cloud Scheduler could not be read, the
 * observed column says so and names the reason. It never falls back to the
 * declared value, and it never renders as agreement — an unavailable that is
 * indistinguishable from a blank is a defect this repository has shipped before
 * (finding F-010), and it would be at its worst here: the screen would read
 * "on time" about a platform whose clocks nobody could see.
 *
 * `unmanaged_in_gcp` IS THE DANGEROUS ONE, so it is shown as loudly as the rest:
 * a job running in Cloud Scheduler that this platform never declared. It is
 * reported and never absorbed into the registry (see the migration's
 * `ck_platform_clocks_never_declares_unmanaged`), which is why it has no
 * declared column and no "apply" — there is no declaration to apply, and
 * inventing one from the observed job is exactly how an undeclared job becomes
 * a declaration nobody made.
 *
 * WHAT THIS IS NOT — said on the screen, in English, not only here. This is the
 * PLATFORM heartbeat: one set per deployment, invisible to an end user, and it
 * exists whether a Datastream exists or not. The cadence of a Datastream is a
 * different object entirely: it lives in `app.datastream_schedule_state`, it is
 * edited in the Datastream workbench (`datastreams/workbench/SchedulePanel.tsx`,
 * Processing tab), and neither knob sets the other.
 *
 * Vocabulary: `ui/` primitives only (AD-35, Tailwind 4.3 + shadcn). No local
 * stylesheet, no literal colour, no base class of its own. The declared and
 * observed records are read with `EvidenceRows`, the shared "read me this
 * record" object, rather than with a table invented here.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { Badge, Button, Cluster, ConfirmDialog, Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, EmptyState, EvidenceRows, Field, formatDuration, formatTimestamp, Input, NativeSelect, PageFrame, PageHeader, Panel, PanelHeader, SectionHeader, Stack, Status, type Fill, Retry } from "../../ui";
import { apiFetch } from "../../lib/apiFetch";
import CacheHealthCard from "../../cache/CacheHealthCard";
import { clockApplyPath, clockListPath, clockPath, clockRunNowPath, DESIRED_STATES, driftDifferences, nightlyStepsPath, normalizeClockList, normalizeNightlySteps, type ClockVerdict, type DesiredState, type NightlyStep, type NightlyStepLedger, type PlatformClock, type PlatformClockList, type StepState, verdictOf } from "./clockRegistryContract";

/**
 * The five verdicts, each with the sentence that makes it actionable.
 *
 * `unknown` is `neutral` on purpose: `Status`/`Badge` render that tone as an
 * open dotted ring, the console's mark for "not started / not read". It is the
 * one tone that cannot be mistaken for the filled green of agreement, in colour
 * or in shape — which matters because the whole failure mode this screen exists
 * to end is an unread state that looks like a healthy one.
 */
const VERDICT: Record<ClockVerdict, { label: string; tone: Fill; meaning: string }> = {
  in_sync: {
    label: "In sync",
    tone: "success",
    meaning: "The declared cadence and the job in Cloud Scheduler agree, field for field.",
  },
  drifted: {
    label: "Drifted",
    tone: "warning",
    meaning:
      "Cloud Scheduler holds something other than what this platform declares. Nothing has been re-imposed: the differences below are the evidence that somebody changed it.",
  },
  missing_in_gcp: {
    label: "Missing in Cloud Scheduler",
    tone: "error",
    meaning:
      "Declared here, absent there. This clock never fires, and a clock that never fires leaves no trace of the runs it did not make.",
  },
  unmanaged_in_gcp: {
    label: "Unmanaged in Cloud Scheduler",
    tone: "error",
    meaning:
      "A job is running in Cloud Scheduler that this platform never declared. Nobody owns it, and nothing here can say what it does or when it was last changed.",
  },
  unknown: {
    label: "Not read",
    tone: "neutral",
    meaning:
      "Cloud Scheduler could not be read for this clock. This is not agreement and not health: the observed column is empty for that reason alone.",
  },
};

/** An instant in the reader's own time zone, or an honest word for its
 *  absence. `Unavailable` is this table's word — every other column here uses
 *  it — and the instant itself is the console's one rendering. */
function when(value: string | null): string {
  return value ? formatTimestamp(value) : "Unavailable";
}

function seconds(value: number | null): string {
  return value === null ? "Unavailable" : `${value} s`;
}

type Load =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ok"; data: PlatformClockList };

type NightlyLoad =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ok"; data: NightlyStepLedger };

type Feedback = { tone: "success" | "error"; message: string };

// ---------------------------------------------------------------------------
// Confirmation — the gate in front of every action with a real effect
// ---------------------------------------------------------------------------

/**
 * "Run now" and "Apply" both leave this console and change something outside it:
 * one execution that has already happened cannot be taken back, and an applied
 * declaration overwrites what Cloud Scheduler held — which is, until it is
 * overwritten, the only record that somebody edited the job by hand. Neither may
 * ever ride a single click. The dialog shows exactly what the action will do,
 * and CANCELLING EMITS NOTHING: the request is issued from `onConfirm` and from
 * nowhere else.
 */


// ---------------------------------------------------------------------------
// One clock: two columns of truth, one verdict, three actions
// ---------------------------------------------------------------------------

type OpenAction = null | "edit" | "apply" | "run";

function ClockCard({
  clock,
  onChanged,
}: {
  clock: PlatformClock;
  onChanged: () => void;
}) {
  const name = clock.clock_name;
  const verdict = verdictOf(clock);
  const presentation = VERDICT[verdict];
  const declared = clock.declared;
  const observed = clock.observed;
  const differences = driftDifferences(clock);

  const [action, setAction] = useState<OpenAction>(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<Feedback | null>(null);
  const commandKeys = useRef<Record<string, string>>({});

  // The edit form is seeded from the DECLARED half only. Seeding it from the
  // observed one would let "edit" quietly adopt whatever GCP drifted to, which
  // is the silent repair this screen refuses to perform.
  const [schedule, setSchedule] = useState(declared?.schedule ?? "");
  const [timezone, setTimezone] = useState(declared?.timezone ?? "");
  const [desiredState, setDesiredState] = useState<DesiredState>(
    declared?.desired_state ?? "enabled",
  );

  function openEdit() {
    setSchedule(declared?.schedule ?? "");
    setTimezone(declared?.timezone ?? "");
    setDesiredState(declared?.desired_state ?? "enabled");
    setActionError(null);
    setAction("edit");
  }

  function close() {
    setAction(null);
    setActionError(null);
  }

  async function send(command: string, path: string, init: RequestInit, success: string) {
    commandKeys.current[command] ??= crypto.randomUUID();
    setBusy(true);
    setActionError(null);
    try {
      const response = await apiFetch(path, {
        ...init,
        headers: {
          ...Object.fromEntries(new Headers(init.headers).entries()),
          "Idempotency-Key": commandKeys.current[command],
        },
      });
      let payload: unknown = null;
      try {
        payload = await response.json();
      } catch {
        /* an empty or non-JSON body is fine; the status is the signal. */
      }
      if (!response.ok) {
        if (response.status < 500) delete commandKeys.current[command];
        const envelope = payload as { message?: string; detail?: string } | null;
        setActionError(envelope?.message ?? envelope?.detail ?? `HTTP ${response.status}`);
        return;
      }
      delete commandKeys.current[command];
      setAction(null);
      setFeedback({ tone: "success", message: success });
      onChanged();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  const saveDeclaration = () =>
    void send(
      `edit:${name}:${schedule}:${timezone}:${desiredState}`,
      clockPath(name),
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          declared_schedule: schedule,
          declared_timezone: timezone,
          desired_state: desiredState,
          confirm: name,
        }),
      },
      "The declared cadence was saved. Cloud Scheduler is unchanged until you apply it.",
    );

  const applyDeclaration = () =>
    void send(
      `apply:${name}`,
      clockApplyPath(name),
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ confirm: name }),
      },
      "The declared cadence was applied to Cloud Scheduler.",
    );

  const runNow = () =>
    void send(
      `run:${name}`,
      clockRunNowPath(name),
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ confirm: name }),
      },
      "The clock was fired once. Its outcome appears at the next reconciliation.",
    );

  // What "apply" would write, shown before it is written rather than after.
  const applyEvidence: Record<string, unknown> = {
    Clock: name,
    Schedule: declared?.schedule ?? null,
    "Time zone": declared?.timezone ?? null,
    "Target path": declared?.target_path ?? null,
    "Desired state": declared?.desired_state ?? null,
    "Currently observed in Cloud Scheduler": observed?.schedule ?? null,
  };

  return (
    <Panel flush data-testid={`clock-${name}`}>
      <PanelHeader
        title={name}
        description={declared?.purpose ?? "No purpose is declared for this clock."}
        actions={
          <Badge tone={presentation.tone} data-testid={`verdict-${name}`}>
            {presentation.label}
          </Badge>
        }
      />

      <div className="flex flex-col gap-4 p-5">
        <Status tone={presentation.tone} as="block" title={presentation.label}>
          {presentation.meaning}
        </Status>

        {/* The two columns. Never one merged record: a reader must always be
            able to say which side a value came from. */}
        <div className="grid min-w-0 gap-4 lg:grid-cols-2">
          <section className="min-w-0 rounded-large border border-divider-base p-4">
            <SectionHeader
              title="Declared"
              description="What this platform says the clock must be."
            />
            <div className="mt-3">
              {declared ? (
                <EvidenceRows
                  label={`Declared cadence for ${name}`}
                  source={{
                    Schedule: declared.schedule,
                    "Time zone": declared.timezone,
                    "Target path": declared.target_path,
                    "HTTP method": declared.http_method,
                    "Attempt deadline": seconds(declared.attempt_deadline_seconds),
                    "Desired state": declared.desired_state,
                  }}
                />
              ) : (
                <Status tone="error" data-testid={`declared-absent-${name}`}>
                  Not declared on this platform. This Cloud Scheduler job exists only there.
                </Status>
              )}
            </div>
          </section>

          <section className="min-w-0 rounded-large border border-divider-base p-4">
            <SectionHeader
              title="Observed"
              description="What Cloud Scheduler held at the last reconciliation."
            />
            <div className="mt-3">
              {verdict === "unknown" ? (
                // The whole point of the screen, in one branch: an unread state
                // is stated as unread. No declared value is echoed here, and no
                // default of "on time" is offered.
                <Status tone="neutral" as="block" title="Cloud Scheduler could not be read">
                  <span data-testid={`unknown-reason-${name}`}>
                    {clock.observation_error ??
                      "No reason was returned with the failed observation."}
                  </span>
                </Status>
              ) : verdict === "missing_in_gcp" ? (
                // A reconciliation DID run here and found nothing — which is a
                // different statement from "we could not look", and is reported
                // as such rather than as an empty record.
                <Status tone="error" as="block" title="No such job in Cloud Scheduler">
                  <span data-testid={`observed-absent-${name}`}>
                    Cloud Scheduler was read{observed?.observed_at ? ` at ${when(observed.observed_at)}` : ""} and holds no job with this
                    name. It has never fired and it never will until it is created.
                  </span>
                </Status>
              ) : !observed ? (
                <Status tone="neutral" as="block" title="Never observed">
                  <span data-testid={`observed-never-${name}`}>
                    No reconciliation has read Cloud Scheduler for this clock yet. Nothing is
                    claimed about it either way.
                  </span>
                </Status>
              ) : (
                <EvidenceRows
                  label={`Observed Cloud Scheduler job for ${name}`}
                  source={{
                    State: observed.state,
                    Schedule: observed.schedule,
                    "Time zone": observed.timezone,
                    "Target URI": observed.target_uri,
                    "HTTP method": observed.http_method,
                    "Attempt deadline": seconds(observed.attempt_deadline_seconds),
                    "Last attempt": when(observed.last_attempt_at),
                    "Last attempt status": observed.last_attempt_status,
                    "Read at": when(observed.observed_at),
                  }}
                />
              )}
            </div>
          </section>
        </div>

        {differences.length > 0 ? (
          <section className="min-w-0">
            <SectionHeader
              title="What differs"
              description="Each field where the two columns disagree, declared next to observed."
            />
            <div className="mt-3">
              <EvidenceRows
                label={`Differences for ${name}`}
                source={Object.fromEntries(
                  differences.map((difference) => [
                    difference.field,
                    { Declared: difference.declared, Observed: difference.observed },
                  ]),
                )}
              />
            </div>
          </section>
        ) : null}

        {feedback ? (
          <Status tone={feedback.tone === "success" ? "success" : "error"}>
            {feedback.message}
          </Status>
        ) : null}

        {declared ? (
          <Cluster className="justify-end">
            <Button
              type="button"
              variant="secondary"
              aria-label={`Edit the declared cadence for ${name}`}
              onClick={openEdit}
            >
              Edit declared cadence
            </Button>
            <Button
              type="button"
              variant="secondary"
              aria-label={`Apply the declared cadence for ${name} to Cloud Scheduler`}
              onClick={() => {
                setActionError(null);
                setAction("apply");
              }}
            >
              Apply declared
            </Button>
            <Button
              type="button"
              aria-label={`Run ${name} now`}
              onClick={() => {
                setActionError(null);
                setAction("run");
              }}
            >
              Run now
            </Button>
          </Cluster>
        ) : (
          // No declaration means no cadence to edit and none to apply, and
          // firing a job nobody declared would be firing something this console
          // cannot describe. The honest move is named instead of offered.
          <Status tone="warning" as="block" title="No action is offered from here">
            This Cloud Scheduler job has no declaration, so there is nothing to edit, nothing to apply and
            nothing this console can say it would run. Declare it — or delete it in Cloud
            Scheduler — and it will stop being unmanaged.
          </Status>
        )}
      </div>

      <ConfirmDialog
        open={action === "run"}
        onOpenChange={(next) => (next ? setAction("run") : close())}
        title={`Run ${name} now?`}
        description={
          <>
            This fires the clock once, immediately, against the live platform. An execution
            that has started cannot be taken back, and it will do everything a scheduled run
            of {name} does.
          </>
        }
        evidence={{
          Clock: name,
          "Target path": declared?.target_path ?? null,
          "Declared schedule": declared?.schedule ?? null,
          Verdict: presentation.label,
        }}
        evidenceLabel={`What running ${name} would do`}
        confirmLabel="Run it now"
        destructive
        busy={busy}
        error={action === "run" ? actionError : null}
        onConfirm={runNow}
      />

      <ConfirmDialog
        open={action === "apply"}
        onOpenChange={(next) => (next ? setAction("apply") : close())}
        title="Apply the declared cadence to Cloud Scheduler?"
        description={
          <>
            This writes the declared values onto the Cloud Scheduler job. Whatever it holds
            now is overwritten — including the evidence that somebody changed it by hand.
          </>
        }
        evidence={applyEvidence}
        evidenceLabel={`What applying ${name} would write`}
        confirmLabel="Overwrite Cloud Scheduler"
        destructive
        busy={busy}
        error={action === "apply" ? actionError : null}
        onConfirm={applyDeclaration}
      />

      {/* Editing the DECLARATION changes nothing outside this platform, so it is
          a form and not a confirmation: the irreversible step is "apply", and it
          keeps its own gate above. */}
      <Dialog open={action === "edit"} onOpenChange={(next) => (next ? openEdit() : close())}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Edit the declared cadence for {name}</DialogTitle>
            <DialogDescription>
              This changes what the platform DECLARES. Cloud Scheduler is untouched until you
              apply it, so the clock will read as drifted in between — which is correct.
            </DialogDescription>
          </DialogHeader>
          <div className="flex flex-col gap-4">
            <Field
              label="Schedule"
              hint="Five whitespace-separated cron fields, as Cloud Scheduler reads them."
            >
              {(props) => (
                <Input
                  {...props}
                  value={schedule}
                  onChange={(event) => setSchedule(event.target.value)}
                />
              )}
            </Field>
            <Field label="Time zone" hint="An IANA zone name, for example Europe/Paris.">
              {(props) => (
                <Input
                  {...props}
                  value={timezone}
                  onChange={(event) => setTimezone(event.target.value)}
                />
              )}
            </Field>
            <Field
              label="Desired state"
              hint="Retired means this clock must no longer exist in Cloud Scheduler at all."
            >
              {(props) => (
                <NativeSelect
                  {...props}
                  value={desiredState}
                  onChange={(event) => setDesiredState(event.target.value as DesiredState)}
                >
                  {DESIRED_STATES.map((state) => (
                    <option key={state} value={state}>
                      {state}
                    </option>
                  ))}
                </NativeSelect>
              )}
            </Field>
          </div>
          {action === "edit" && actionError ? (
            <Status tone="error">{actionError}</Status>
          ) : null}
          <DialogFooter>
            <Button type="button" variant="secondary" onClick={close}>
              Cancel
            </Button>
            <Button type="button" disabled={busy} onClick={saveDeclaration}>
              {busy ? "Saving…" : "Save declaration"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Panel>
  );
}


// ---------------------------------------------------------------------------
// Last night's steps — `Incomplete if` 2, one level below the clocks
//
// The cards above answer "did the heartbeat fire?". This answers "and did the
// work inside that beat happen?" — the question that had no answer anywhere
// until migration 325, because `scheduler._run_isolated_step` never re-raises
// and a step that silently never ran left no row at all.
//
// THE THREE SILENCES ARE THREE STATES, never one. `never_started` is a step the
// dispatch declared and nothing ever began; `unfinished` is a step that began
// and never closed, which is what a crash looks like from the outside; and
// `unrecorded` is a declared step the night holds no row for at all. Collapsing
// any pair — or rendering one of them as "OK" because it carries no error — is
// exactly the collapse the clause forbids.
//
// AND THE GESTURE IS THE OPERATOR'S SCHEDULER, never a deployment word. Every
// sentence below points at `dispatch-nightly`, whose card is on this same
// screen with its own "Run now". A person who reads this panel can act without
// leaving it.
// ---------------------------------------------------------------------------

const STEP: Record<StepState, { label: string; tone: Fill; meaning: string }> = {
  succeeded: {
    label: "Succeeded",
    tone: "success",
    meaning: "The step ran to the end and reported no error.",
  },
  failed: {
    label: "Failed",
    tone: "error",
    meaning:
      "The step raised, and the night carried on without it — that isolation is deliberate. The work it does was not done last night.",
  },
  unfinished: {
    label: "Started, never finished",
    tone: "warning",
    meaning:
      "The step began and no verdict was ever written for it. That is what a killed process leaves behind, and it is not a failure: nothing judged this step. Run dispatch-nightly once from its card above to give it another night.",
  },
  never_started: {
    label: "Never started",
    tone: "error",
    meaning:
      "The night declared this step and it never began. Nothing it does happened, and nothing else on this platform would have said so. Run dispatch-nightly once from its card above.",
  },
  unrecorded: {
    label: "Not recorded",
    tone: "neutral",
    meaning:
      "This step is part of the nightly sequence and the night holds no record of it at all. That is not a statement that it did not run — it is a statement that nobody can tell. Run dispatch-nightly once from its card above to get a night that says.",
  },
};

/** A duration in the unit a person reads, or an honest dash.
 *
 *  `precise`: a nightly step that takes 4 ms and one that takes 900 ms are the
 *  same reading under `< 1 s`, and telling them apart is why this column
 *  exists. Above a minute the console's plain ladder takes over. */
function howLong(step: NightlyStep): string {
  return formatDuration(step.duration_ms, { precise: true });
}

function NightlyStepRow({ step }: { step: NightlyStep }) {
  const presentation = STEP[step.state];
  return (
    <div
      className="flex min-w-0 flex-wrap items-baseline justify-between gap-x-4 gap-y-1 border-t border-divider-base py-3 first:border-t-0"
      data-testid={`nightly-step-${step.step_name}`}
    >
      <div className="flex min-w-0 flex-col gap-1">
        <Cluster>
          <span className="text-body font-medium text-text-primary">{step.step_name}</span>
          <Badge tone={presentation.tone} data-testid={`nightly-state-${step.step_name}`}>
            {presentation.label}
          </Badge>
          {step.declared ? null : <Badge tone="neutral">No longer in the sequence</Badge>}
          {step.error_class ? (
            <Badge tone="error" data-testid={`nightly-error-${step.step_name}`}>
              {step.error_class}
            </Badge>
          ) : null}
        </Cluster>
        {step.state === "succeeded" ? null : (
          <span className="text-caption text-text-secondary">{presentation.meaning}</span>
        )}
      </div>
      <span className="text-caption tabular-nums text-text-secondary">{howLong(step)}</span>
    </div>
  );
}

function NightlyStepsPanel({ load, onRetry }: { load: NightlyLoad; onRetry: () => void }) {
  if (load.status === "loading") {
    return (
      <Panel>
        <PanelHeader title="Last night&rsquo;s steps" />
        <div className="p-5">
          <Status tone="neutral" active>
            Reading the nightly step ledger…
          </Status>
        </div>
      </Panel>
    );
  }

  if (load.status === "error") {
    return (
      <Panel>
        <PanelHeader title="Last night&rsquo;s steps" />
        <div className="p-5">
          {/* "We could not ask" is never rendered as "nothing went wrong". */}
          <Status tone="error" as="block" title="The nightly steps could not be read"
          action={<Retry onClick={onRetry} />}
        >
            {load.message}. Nothing here is a statement about last night: the ledger itself did
            not answer, so no step is succeeded, failed or missing — they are unread.
          </Status>
        </div>
      </Panel>
    );
  }

  const night = load.data.runs[0] ?? null;

  if (!load.data.has_run || night === null) {
    return (
      <Panel>
        <PanelHeader title="Last night&rsquo;s steps" />
        <div className="p-5">
          <EmptyState
            title="No night has been recorded"
            description="The nightly sequence has not run once since this ledger existed. Nothing is broken and nothing is late — there is simply no night to read. Run dispatch-nightly once from its card above, and this panel will hold every step it took."
          />
        </div>
      </Panel>
    );
  }

  const unresolved = night.unresolved.length;

  return (
    <Panel flush data-testid="nightly-steps">
      <PanelHeader
        title="Last night&rsquo;s steps"
        description={
          night.as_of_date
            ? `The ${night.as_of_date} run of the nightly sequence, step by step, in the order it was dispatched.`
            : "The last run of the nightly sequence, step by step, in the order it was dispatched."
        }
        actions={
          <Badge tone={unresolved === 0 ? "success" : "warning"} data-testid="nightly-unresolved">
            {unresolved === 0
              ? `${night.steps.length} steps, all succeeded`
              : `${unresolved} of ${night.steps.length} unresolved`}
          </Badge>
        }
      />
      <div className="flex flex-col gap-4 p-5">
        <Status
          tone="info"
          as="block"
          title="Every declared step is listed, whether or not it ran"
        >
          The sequence is written before the first step begins, so a step that never started is a
          line here rather than an absence. That is the whole difference between this panel and a
          log.
        </Status>
        <div className="flex min-w-0 flex-col">
          {night.steps.map((step) => (
            <NightlyStepRow key={step.step_name} step={step} />
          ))}
        </div>
      </div>
    </Panel>
  );
}

// ---------------------------------------------------------------------------
// The screen
// ---------------------------------------------------------------------------

export default function PlatformClocks() {
  const [load, setLoad] = useState<Load>({ status: "loading" });
  const [nightly, setNightly] = useState<NightlyLoad>({ status: "loading" });

  const read = useCallback(async (signal: { cancelled: boolean }) => {
    try {
      const response = await apiFetch(clockListPath(), { cache: "no-store" });
      if (!response.ok) {
        // "We could not ask" is never rendered as "there is nothing".
        if (!signal.cancelled) {
          setLoad({ status: "error", message: `HTTP ${response.status}` });
        }
        return;
      }
      const data = normalizeClockList(await response.json());
      if (!signal.cancelled) setLoad({ status: "ok", data });
    } catch (err) {
      if (!signal.cancelled) {
        setLoad({ status: "error", message: err instanceof Error ? err.message : String(err) });
      }
    }
  }, []);

  // Read SEPARATELY from the clocks, and stored separately. One request that
  // carried both would make a ledger outage read as "the clocks could not be
  // read", and this screen's first rule is that two facts are never merged.
  const readNightly = useCallback(async (signal: { cancelled: boolean }) => {
    try {
      const response = await apiFetch(nightlyStepsPath(), { cache: "no-store" });
      if (!response.ok) {
        if (!signal.cancelled) {
          setNightly({ status: "error", message: `HTTP ${response.status}` });
        }
        return;
      }
      const data = normalizeNightlySteps(await response.json());
      if (!signal.cancelled) setNightly({ status: "ok", data });
    } catch (err) {
      if (!signal.cancelled) {
        setNightly({
          status: "error",
          message: err instanceof Error ? err.message : String(err),
        });
      }
    }
  }, []);

  const [attempt, setAttempt] = useState(0);
  const [refreshing, setRefreshing] = useState(false);
  useEffect(() => {
    const signal = { cancelled: false };
    // Only the FIRST read empties the screen. A re-read after an action keeps
    // the cards mounted — otherwise the confirmation of what just happened is
    // unmounted with them, and the operator is told nothing at all about the
    // irreversible thing they just did.
    if (attempt === 0) setLoad({ status: "loading" });
    else setRefreshing(true);
    void Promise.all([read(signal), readNightly(signal)]).finally(() => {
      if (!signal.cancelled) setRefreshing(false);
    });
    return () => {
      signal.cancelled = true;
    };
  }, [read, readNightly, attempt]);

  const reload = useCallback(() => setAttempt((value) => value + 1), []);

  const header = (
    <PageHeader
      eyebrow="Platform"
      title="Platform clocks"
      description="Every scheduled job this deployment depends on, with what the platform declares next to what Cloud Scheduler actually holds. The two are never merged: a difference between them is the point of this screen."
      actions={
        <Button type="button" variant="secondary" disabled={refreshing} onClick={reload}>
          {refreshing ? "Re-reading…" : "Re-read Cloud Scheduler"}
        </Button>
      }
    />
  );

  // Said on the screen, in English, because the confusion is easy and expensive:
  // someone who reads "schedule" here and changes it expecting a Datastream to
  // move would be changing the platform's heartbeat instead.
  const distinction = (
    <Status tone="info" as="block" title="These are not Datastream schedules">
      These clocks are the platform&apos;s own heartbeat — one set per deployment, shared by
      every organization, and present whether a Datastream exists or not. How often ONE
      Datastream runs is a different setting, on a different object: it lives in that
      Datastream&apos;s Processing tab. Nothing on this screen changes it, and nothing there
      changes these.
    </Status>
  );

  if (load.status === "loading") {
    return (
      <PageFrame>
        {header}
        <Panel>
          <Status tone="neutral" active>
            Reading the clock registry…
          </Status>
        </Panel>
        <NightlyStepsPanel load={nightly} onRetry={reload} />
      </PageFrame>
    );
  }

  if (load.status === "error") {
    return (
      <PageFrame>
        {header}
        <Panel>
          <Status tone="error" as="block" title="The platform clocks could not be read"
          action={<Retry onClick={reload} />}
        >
            {load.message}. Nothing on this screen is a statement about Cloud Scheduler: the
            registry itself did not answer, so no clock is in sync, drifted or missing — they
            are unread.
          </Status>
        </Panel>
        <NightlyStepsPanel load={nightly} onRetry={reload} />
      </PageFrame>
    );
  }

  const { clocks, reconciled_at, reconciliation_error } = load.data;
  const counted = new Map<ClockVerdict, number>();
  for (const clock of clocks) {
    const verdict = verdictOf(clock);
    counted.set(verdict, (counted.get(verdict) ?? 0) + 1);
  }

  return (
    <PageFrame>
      {header}
      <Stack>
        {distinction}

        {reconciliation_error ? (
          <Status tone="warning" as="block" title="The last reconciliation did not complete">
            {reconciliation_error} Every clock below whose verdict reads &ldquo;Not read&rdquo;
            is unread for this reason, not because it is healthy.
          </Status>
        ) : null}

        <Panel>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <Cluster>
              {clocks.length === 0 ? null : (
                <>
                  {(Object.keys(VERDICT) as ClockVerdict[])
                    .filter((verdict) => (counted.get(verdict) ?? 0) > 0)
                    .map((verdict) => (
                      <Badge key={verdict} tone={VERDICT[verdict].tone}>
                        {counted.get(verdict)} {VERDICT[verdict].label}
                      </Badge>
                    ))}
                </>
              )}
            </Cluster>
            <span className="text-caption text-text-secondary">
              {reconciled_at
                ? `Cloud Scheduler last read at ${when(reconciled_at)}.`
                : "Cloud Scheduler has not been read yet."}
            </span>
          </div>
        </Panel>

        {clocks.length === 0 ? (
          <Panel>
            <EmptyState
              title="No platform clock is declared"
              description="The registry answered, and it holds nothing. Either this deployment has never been provisioned, or its clocks were created in Cloud Scheduler without ever being declared here — in which case they are running unseen."
              action={
                <Button type="button" variant="secondary" disabled={refreshing} onClick={reload}>
                  Look again
                </Button>
              }
            />
          </Panel>
        ) : (
          clocks.map((clock) => (
            <ClockCard key={clock.clock_name} clock={clock} onChanged={reload} />
          ))
        )}
        <NightlyStepsPanel load={nightly} onRetry={reload} />
        <section aria-label="Warehouse cache health" className="mt-4">
          <CacheHealthCard />
        </section>
      </Stack>
    </PageFrame>
  );
}
