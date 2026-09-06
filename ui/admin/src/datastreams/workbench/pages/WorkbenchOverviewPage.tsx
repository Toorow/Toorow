import { useState } from "react";
import { Button, capabilityLabel, ConfirmDialog, formatNumber, formatPercent, Metric, ObjectId, Panel, PanelHeader, Status } from "../../../ui";
import { dateTime, record, records, text, titleCase } from "../evidence";
import type { Tab } from "../../../shell/pages/datastreamTabs";
import WorkbenchCapabilityPanel, { capabilityProjection } from "../WorkbenchCapabilityPanel";
import WorkbenchDeliveryPanel from "../WorkbenchDeliveryPanel";
import SchedulePanel from "../SchedulePanel";
import CoverageStrip from "../../../shell/pages/CoverageStrip";
import { CollectNowDialog, runStartedSentence, type RunStarted, useCollectNow } from "../collectNow";
import { ApiError, apiJson } from "../../../lib/apiFetch";
import type { OwnerReference } from "../../../shell/pages/ProjectSettings";
import type { WorkbenchHeader, WorkbenchTabPayload } from "../workbenchTypes";

/** The four next steps the server decides, and how urgently each reads.
 *
 *  `prepare_change` is the settled case and must not look like a call to act;
 *  `repair` is the one that costs data if it waits. A single tone for all four
 *  would make the quiet state shout and the loud one whisper. */
const ACTION_TONE: Record<string, "error" | "warning" | "neutral"> = {
  repair: "error",
  review_candidate: "warning",
  finish_setup: "warning",
  prepare_change: "neutral",
};

const ALL_STAGES = ["collected", "mapped", "processed", "published"];

/** Mapping health, in the three states a person acts on differently.
 *
 *  `absent` is not a worse `blocked`: with no mapping version there is nothing
 *  to unblock, and the work is to create one. Collapsing the two would send a
 *  person to review bindings that do not exist. */
function mappingHealth(evidence: Record<string, unknown> | null): {
  state: "healthy" | "blocked" | "absent";
  title: string;
  detail: string;
} {
  const versionId = evidence?.version_id;
  const proposedId = evidence?.proposed_version_id;
  if (!versionId && proposedId) {
    // Une version EXISTE et lie les champs ; elle n'est pas encore en vigueur.
    // Dire « rien ne lie les champs » d'un Datastream dont l'onglet Mapping
    // affiche neuf liaisons confirmees est faux, et decourage la seule action
    // qui reste a faire.
    return {
      state: "blocked",
      title: "No mapping is in force yet",
      detail: "A mapping version binds this Datastream's fields and is waiting to be published. "
        + "Nothing downstream reads it until it is.",
    };
  }
  if (!versionId) {
    return {
      state: "absent",
      title: "No mapping version",
      detail: "Nothing binds this Datastream's fields to canonical meaning, so nothing downstream can read it.",
    };
  }
  const executable = evidence?.executable;
  const blocking = typeof evidence?.blocking_count === "number" ? evidence.blocking_count : null;
  if (executable === false || (blocking !== null && blocking > 0)) {
    return {
      state: "blocked",
      title: "Mapping is not executable",
      detail: blocking !== null && blocking > 0
        ? `${blocking} binding${blocking === 1 ? "" : "s"} still block this mapping. The Datastream cannot publish against it until they are resolved.`
        : "The active mapping version is marked not executable. The Datastream cannot publish against it.",
    };
  }
  return { state: "healthy", title: "", detail: "" };
}

/** `3h`, `2d` — the age a person reads freshness as. An absolute timestamp is
 *  the evidence, not the answer: "is this current?" is a duration question, and
 *  the mockup leads with `3h` for that reason. The timestamp stays as the hint,
 *  so nothing is lost. */
function ageSince(value: string | null, now: Date = new Date()): string {
  if (!value) return "Unavailable";
  const at = new Date(value);
  if (Number.isNaN(at.getTime())) return "Unavailable";
  const hours = Math.floor((now.getTime() - at.getTime()) / 3_600_000);
  if (hours < 0) return "Unavailable";
  if (hours < 1) return "<1h";
  if (hours < 48) return `${hours}h`;
  return `${Math.floor(hours / 24)}d`;
}

/**
 * ONE STAGE OF THE FLOW — story 58.9.
 *
 * Every stage carries the OBJECT placed on it or an invitation to place one, and
 * never a negation and never a zero. `stage_coverage` is not what colours these:
 * `app.datastream_execution_stage_evidence` is empty across the whole of preprod,
 * so a stage reading it printed the same `0 / 4` for every Datastream in the
 * product — a measurement nobody made.
 *
 * THE BUTTONS SHARE ONE BASELINE. `mt-auto` inside a full-height flex column is
 * what does it: the four actions sit on the bottom edge of the row whatever the
 * height of the text above them, with no fixed height and no stylesheet.
 */
function Stage({
  name,
  headline,
  detail,
  placed,
  action,
}: {
  name: string;
  headline: string;
  detail?: string;
  /** True when an object IS placed here. Drives nothing but the word: a stage
   *  with nothing placed reads as an invitation, not as a failure. */
  placed: boolean;
  action: React.ReactNode;
}) {
  return (
    <li
      className="flex h-full flex-col gap-2 p-5"
      data-testid={`stage-${name.toLowerCase()}`}
      data-placed={placed ? "yes" : "no"}
    >
      <h3 className="m-0 text-ui font-semibold text-text">{name}</h3>
      <p className="m-0 text-ui text-text">{headline}</p>
      {detail ? <p className="m-0 text-caption text-text-secondary">{detail}</p> : null}
      <div className="mt-auto pt-3" data-testid="stage-action">
        {action}
      </div>
    </li>
  );
}

/** The change a switch prepares, once it has been prepared. */
type PreparedChange =
  | { kind: "prepared"; capabilityKey: string; changeSetId: string; enabling: boolean }
  | { kind: "failed"; capabilityKey: string; message: string };

export default function WorkbenchOverviewPage({
  header,
  payload,
  projectId,
  datastreamId,
  connector,
  sourceAccountRef,
  onOpenOwner,
  onNavigateTab,
  onRepairMapping,
  onRetryCapabilities,
}: {
  header: WorkbenchHeader;
  payload: WorkbenchTabPayload;
  projectId: string;
  datastreamId: string;
  /** The provider and the account a re-collection would spend on. Handed down by
   *  the route from the header it already read, exactly as `Runs` receives them:
   *  `CoverageStrip` carries the confirmation of story 58.4, and a confirmation
   *  that named neither would let somebody spend on a connection they did not
   *  check. */
  connector?: string | null;
  sourceAccountRef?: string | null;
  onOpenOwner?: (owner: OwnerReference) => void;
  /** Re-reads the Workbench header, for the capability panel's error block
   *  (76-4). Owned by the route, because the route made the read. */
  onRetryCapabilities: () => void;
  onNavigateTab?: (tab: Tab) => void;
  onRepairMapping?: (rawImportId: string) => void;
}) {
  const state = payload.evidence.state === "available" ? "available" : "unavailable";
  const schedule = record(payload.evidence.schedule);
  const latest = record(payload.evidence.latest_execution);
  const stageCoverage = records(payload.evidence.stage_coverage);
  const downstreamCount = typeof payload.evidence.downstream_count === "number" ? payload.evidence.downstream_count : null;
  const publishedAt = text(payload.evidence.published_at, "") || null;
  const publishedRowCount = typeof payload.evidence.published_row_count === "number"
    ? payload.evidence.published_row_count
    : null;
  const freshness = ageSince(publishedAt);
  /**
   * TWO FRESHNESS READINGS SAT ON THIS PAGE, AND NEITHER SAID WHAT IT MEASURED
   * — finding D-4 of the 2026-08-12 visual review.
   *
   * `Freshness 9d` in the metrics and `Freshness watermark: Unavailable` in
   * `Operating posture` were two answers to one word, some 3000px apart. They
   * are genuinely different questions, so both are kept — in ONE tile, with
   * words that say which is which:
   *
   *   - the VALUE is the age of the PUBLICATION. `published_at` is the
   *     `state_changed_at` of the latest execution that reached `published`, so
   *     it dates what is being served.
   *   - the HINT carries `last_committed_watermark`, the DATA boundary the
   *     collection has committed up to. `test_schedule_control.py` keeps that
   *     column's name apart from the clock for exactly this reason: reporting a
   *     data boundary as a run time has someone read a date that never meant
   *     that.
   *
   * The word `Freshness` is now printed once on this page.
   */
  const watermark = schedule?.last_committed_watermark ? dateTime(schedule.last_committed_watermark) : null;
  const freshnessHint = [
    publishedAt ? `Published ${dateTime(publishedAt)}` : "Nothing published yet",
    watermark ? `data collected through ${watermark}` : null,
  ].filter(Boolean).join(" · ");
  const successEvidence = record(payload.evidence.run_success) ?? {};
  const runSuccess = {
    windowDays: typeof successEvidence.window_days === "number" ? successEvidence.window_days : 30,
    terminal: typeof successEvidence.terminal_count === "number" ? successEvidence.terminal_count : 0,
    // Story 63.1: `succeeded_count`, not `published_count`. A successful run may
    // now be `collected` -- a recurring retrieval that pulled its windows and
    // published nothing by design -- so counting only publications made the rate
    // of every scheduled Datastream fall toward 0%.
    succeeded: typeof successEvidence.succeeded_count === "number" ? successEvidence.succeeded_count : 0,
    get rate() {
      return formatPercent(this.terminal > 0 ? this.succeeded / this.terminal : 0);
    },
  };
  const nextRun = schedule?.next_run_at ? dateTime(schedule.next_run_at) : "Not scheduled";
  const mapping = mappingHealth(record(payload.evidence.mapping_health));
  // AI-113: an inbound Datastream needs its address BEFORE anything can arrive,
  // and a person had no way to obtain it -- the issuing surface existed and was
  // mounted nowhere. The ratified Overview contract puts "identity and
  // bindings; configuration readiness; blockers" on this tab with the
  // highest-priority safe next step as its action, which for an addressless
  // inbound Datastream is issuing one.
  //
  // `WorkbenchDeliveryPanel` is the PORT of the MUI panel that used to sit
  // unmounted in `datastreams/`. AD-35 makes MUI legacy, so mounting that one
  // was never the answer -- porting it was, and treating the rule as a reason
  // to stop would have left the address unreachable.
  const deliveryChannels = header.identity.delivery_channels ?? [];
  /** `source_kind`, and the only field that carries `managed_feed` — the same
   *  rule as amendment 7. Every mode branch on this page reads THIS. */
  const pushedSource = header.identity.mode === "managed_feed";
  /**
   * A MODE GATE READS THE MODE, AND A CHANNEL GATE READS THE CHANNELS — the same
   * class as amendment 7 of the 2026-08-11 review, one screen over. Found by the
   * tour-3 review of this tab (issue #56).
   *
   * `Boolean(header.identity.connector)` used to stand here, and `connector` is
   * composed `config.connector_name or module_name` — neither of which means
   * anything for a pushed source. Measured 2026-08-11 on the 4 live managed
   * feeds: `module_name` is NULL on all four, and `config.connector_name` holds
   * the literal string `"managed_feed"` on two of them and NULL on the other
   * two. So of the TWO Datastreams that actually declare `channels: ["email"]`,
   * `inbound-email-fin10` reached its address and `inbound-email-q1` did not —
   * the difference being a string somebody happened to write into config, not
   * anything about the flux. One inbound Datastream could not be handed the
   * address files are meant to be sent to, and nothing said why.
   *
   * The channels are the evidence, and they are already on this header.
   */
  const isInbound =
    header.identity.mode === "managed_feed" &&
    deliveryChannels.some((channel) => channel === "inbound_email" || channel === "email" || channel === "webhook");
  // The stages with no evidence, named. A COUNT is not printed here any more:
  // with no stage evidence at all — which is every Datastream of preprod — it
  // read `0 / 4 stages`, a fraction of a measurement nobody took. What the
  // reader needs is WHICH stage has none, and that has always been the second
  // half of this line.
  const coveredStages = new Set(stageCoverage.map((entry) => text(entry.stage).toLowerCase()));
  const missingStages = ALL_STAGES.filter((stage) => !coveredStages.has(stage));

  const action = header.primary_action;

  // ─── The `Check` stage's object: monitors placed on this Datastream ────────
  // An ABSENT key and a count of zero are two different answers and never
  // render for each other: one says the count could not be read, the other is a
  // real `COUNT(*)` whose answer happens to be nothing — and for that one the
  // stage shows its invitation rather than the digit.
  const monitors = record(payload.evidence.dq_monitors);
  const monitorCount = typeof monitors?.count === "number" ? monitors.count : null;

  const [scheduleOpen, setScheduleOpen] = useState(false);
  /**
   * ASKING FOR A COLLECTION, WHICH THE CONSOLE COULD NOT DO — 2026-08-18.
   *
   * `SchedulePanel`, five lines below this on the `Collect` stage, offers the
   * cadence `manual` and describes it as "On demand only… This Datastream only
   * runs when someone asks it to". Nothing in this console asked. The door has
   * existed on the server since story 8.2 (`POST /api/datastreams/{id}/run`)
   * and the Workbench simply never mounted it — so the one Datastream of the
   * live base that is active, enabled, mapped and `manual` had no way to be run
   * at all.
   *
   * IT BELONGS ON THIS STAGE AND NOWHERE ELSE. `Collect` is where the cadence is
   * seen and set (amendment of 2026-08-05), and "run it now" is the same
   * question as "when does it run" asked about this minute. Putting it on `Runs`
   * would separate the act from the setting that governs it.
   */
  const [runStarted, setRunStarted] = useState<RunStarted | null>(null);
  const collectNow = useCollectNow(projectId, datastreamId, (started) => {
    setRunStarted(started);
  });
  const [preparing, setPreparing] = useState<string | null>(null);
  const [prepared, setPrepared] = useState<PreparedChange | null>(null);
  const [confirmingCapability, setConfirmingCapability] = useState<{ key: string, next: boolean, label: string } | null>(null);

  /**
   * WHAT THE SWITCH WRITES — story 58.9, arbitrage 1.
   *
   * [Updated for Lot 5] Intercepting the capability switch on the Overview to
   * be interactive and immediately confirm the capability instead of only creating a draft.
   */
  async function confirmCapabilityChange(capabilityKey: string, enabling: boolean) {
    setPreparing(capabilityKey);
    setPrepared(null);
    try {
      const created = await apiJson<{ id: string }>(
        `/api/projects/${encodeURIComponent(projectId)}/settings/change-sets`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Idempotency-Key": newIdempotencyKey(),
          },
          body: JSON.stringify({
            intent: { capabilities: { [capabilityKey]: enabling ? "enabled" : "disabled" } },
          }),
        },
      );
      await apiJson<{ prepared_payload_hash: string }>(
        `/api/projects/${encodeURIComponent(projectId)}/settings/change-sets/`
          + `${encodeURIComponent(created.id)}/prepare`,
        { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" },
      );
      // IT STOPS HERE, AND THAT IS THE CONTRACT OF THIS SCREEN. A `Lot 5` change
      // carried this on through `/confirmations` and `/confirm` and then reloaded
      // the page -- which made the Workbench a SECOND activation authority,
      // minted a confirmation secret on a surface that must never hold one, and
      // decided `ready` or `degraded` on behalf of the person clicking. The
      // switch prepares; Project Settings > Changes confirms. Two calls, and the
      // notice below names the Change Set that is now waiting there.
      setPrepared({ kind: "prepared", capabilityKey, changeSetId: created.id, enabling });
      setPreparing(null);
      setConfirmingCapability(null);
    } catch (error) {
      setPrepared({
        kind: "failed",
        capabilityKey,
        message: error instanceof ApiError || error instanceof Error
          ? error.message
          : "The capability could not be changed.",
      });
      setPreparing(null);
      setConfirmingCapability(null);
    }
  }

  const capabilityTabs = header.capability_tabs;

  return (
    <>
      {/* ─── WHAT A PERSON READS, IN THIS ORDER ───────────────────────────────
          Finding D-4 of the 2026-08-12 visual review: this tab was 3742px tall
          and said the same thing three times, with the flow — the reason anyone
          opens it — eighth. The order below is the answer, and it is the
          ratified one: « cette page EST le flux ».

            1. the FLOW, directly under the four axes of the header, so the four
               stages and the step each one waits on are the first thing seen;
            2. the ONE next safe step the server decides, and the mapping banner
               when a mapping is what holds the flow up;
            3. the four numbers that qualify the state;
            4. the days collected;
            5. the modules, merged with what each compiles here (amendment 11);
            6. the exact references, which is where every pointer now lives.

          Three panels left this page and not one measurement did — each fact
          they carried is named in the block that absorbed it. */}

      {/* ─── THE FLOW ─────────────────────────────────────────────────────────
          `Collect · Map · Check · Publish` — our four words, aligned on the data
          stages `collected / mapped / processed / published`
          (`datastream-workbench-and-wizard.md`, « Et le vocabulaire est le
          nôtre »). Each stage names the object placed on it or invites one; none
          of them is a tile of negation. */}
      <Panel flush data-testid="pipeline">
        <PanelHeader
          title="Collect · Map · Check · Publish"
          description="What is placed on each stage of this Datastream, and where to place what is not."
        />
        <ol className="m-0 grid list-none grid-cols-4 divide-x divide-divider-base p-0 max-lg:grid-cols-2 max-lg:divide-y max-md:grid-cols-1 max-md:divide-x-0 max-md:divide-y">
          {/* THE STAGE SAYS WHAT THE PANEL UNDER IT SAYS — amendment 1 of the
              2026-08-11 review, finished. Found by the tour-3 review of this tab
              (issue #56), and it was a half-repair of my own: the amendment took
              the pull cadence out of `SchedulePanel` for a pushed source, and
              this summary — five lines above the very panel it summarises — kept
              telling the same person to « set a frequency and an arrival hour »
              for a file nobody fetches. Two sentences, one screen, opposite
              claims, on 4 of the 6 live Datastreams.

              A pushed source is PLACED when something has arrived, not when a
              clock has been armed: `never_ran` is the server's own flag, so the
              stage measures the same fact the panel prints. */}
          <Stage
            name="Collect"
            placed={pushedSource ? Boolean(schedule && !schedule.never_ran) : Boolean(schedule?.next_run_at)}
            headline={
              !schedule
                ? "No schedule state exists for the active plan."
                : pushedSource
                  ? schedule.never_ran
                    ? "No file has arrived yet."
                    : `Last arrival ${dateTime(schedule.last_run_at)}`
                  : schedule.next_run_at
                    ? `Next run ${dateTime(schedule.next_run_at)}`
                    : "No next run is placed on the clock."
            }
            detail={
              !schedule
                ? "This Datastream's clock has never been armed. Set a frequency and an arrival hour to arm it."
                : pushedSource
                  ? schedule.never_ran
                    ? deliveryChannels.length
                      ? `Files are pushed to this Datastream — nothing is fetched on a clock. Send one to its ${deliveryChannels.join(", ")} address, or upload one.`
                      : "Files are pushed to this Datastream — nothing is fetched on a clock. Upload one, or declare an inbound channel to give it an address."
                    : undefined
                  : schedule.next_run_at
                    ? undefined
                    : "Choose a frequency and an arrival hour, and the next run is written from them."
            }
            action={
              <div className="flex flex-wrap items-center gap-2">
                {/* TWO ACTS, AND THE ORDER IS THEIR CONSEQUENCE. `Collect now`
                    spends at the provider, so it leads; changing the schedule
                    spends nothing and steps down to `secondary` beside it.

                    ABSENT FOR A PUSHED SOURCE, not disabled: a `managed_feed`
                    has nothing to go and fetch — `gate_refusal` answers
                    `pushed_source` for exactly this row — and a greyed control
                    with no word is read as a bug. The stage's own detail already
                    says how a file reaches it. */}
                {!pushedSource && (
                  <Button
                    size="sm"
                    disabled={collectNow.phase === "checking" || collectNow.busy}
                    onClick={() => void collectNow.ask()}
                    data-testid="stage-collect-now"
                  >
                    {collectNow.phase === "checking" ? "Checking…" : "Collect now"}
                  </Button>
                )}
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() => setScheduleOpen((open) => !open)}
                  data-testid="stage-collect-action"
                >
                  {scheduleOpen
                    ? pushedSource ? "Hide arrival" : "Hide schedule"
                    : pushedSource ? "How it arrives" : "Change schedule"}
                </Button>
              </div>
            }
          />
          <Stage
            name="Map"
            placed={mapping.state === "healthy"}
            // The stage does NOT repeat the blocker banner above it. Two copies
            // of one sentence on one screen make a reader check whether they
            // agree; the banner carries the count and the reason, the stage
            // carries what is placed and the way in.
            headline={
              mapping.state === "healthy"
                ? "An active mapping binds this Datastream's fields."
                : mapping.state === "blocked"
                  ? "A mapping is placed, and it does not execute yet."
                  : "No mapping is placed on this Datastream."
            }
            detail={
              mapping.state === "healthy"
                ? undefined
                : mapping.state === "blocked"
                  ? "Open it to resolve the bindings that hold it back."
                  : "Bind its source fields to canonical meaning, and everything downstream becomes readable."
            }
            action={
              <Button
                variant="secondary"
                size="sm"
                disabled={!onNavigateTab}
                onClick={() => onNavigateTab?.("mapping")}
                data-testid="stage-map-action"
              >
                Open mapping
              </Button>
            }
          />
          <Stage
            name="Check"
            placed={monitorCount !== null && monitorCount > 0}
            headline={
              monitorCount === null
                ? "The data-quality monitors could not be read."
                : monitorCount === 0
                  ? "No check is placed on this Datastream."
                  : `${monitorCount} check${monitorCount === 1 ? "" : "s"} placed`
            }
            detail={
              monitorCount === null
                ? "This tab carried no monitor count, so nothing here says whether a check exists."
                : monitorCount === 0
                  ? "Nothing watches this Datastream's null rates, volumes or freshness yet. Checks are defined in Governance › Controls & Quality › Data Quality."
                  : `${typeof monitors?.published === "number" ? monitors.published : 0} of them published`
            }
            action={
              // NAMED AFTER WHAT IT DOES — story 58.9, arbitrage 6. Epic specifies it must say 'Add a check'
              // and route to the creation flow.
              <Button
                variant="secondary"
                size="sm"
                disabled={!onOpenOwner}
                onClick={() =>
                  onOpenOwner?.({
                    surface: "project",
                    workspace: "governance",
                    section: "controls-quality",
                    lens: "data-quality",
                    global_surface: null,
                    global_section: null,
                    // THE BUTTON IS NAMED AFTER ITS NAVIGATION, NOT AFTER AN ACT
                    // NOTHING PERFORMS — and the ratified document says so by
                    // name: « `Add a check` n'existe pas : aucun moniteur
                    // n'existe sur aucune base et rien ici n'en pose un, donc le
                    // bouton s'appelle d'après sa navigation, `Open Data
                    // Quality`, et mène à la collection
                    // `controls-quality/data-quality` ; poser un moniteur
                    // appartient à l'epic 59 ».
                    //
                    // It sent `object_type: "dq-monitor"` with `object_id: null`
                    // and `action: "create"`, and BOTH are refused by the router:
                    // `ContentRouter.tsx` returns silently when an object type
                    // arrives without an id, and neither the `controls-quality`
                    // section nor the `dq-monitor` contract declares a `create`
                    // action in `navigation.ts`. So the control did nothing at
                    // all, on every Datastream of every Project. A creation has
                    // no object id by definition — the address that exists is the
                    // COLLECTION, through its own lens.
                    object_type: null,
                    object_id: null,
                    tab: null,
                    action: null,
                    version_id: null,
                    evidence_id: null,
                  })
                }
                data-testid="stage-check-action"
              >
                Open Data Quality
              </Button>
            }
          />
          <Stage
            name="Publish"
            placed={typeof downstreamCount === "number" && downstreamCount > 0}
            headline={
              downstreamCount === null
                ? "The downstream usage could not be read."
                : downstreamCount === 0
                  ? "Nothing downstream reads this Datastream yet."
                  : `${downstreamCount} downstream consumer${downstreamCount === 1 ? "" : "s"}`
            }
            detail={
              downstreamCount === null
                ? "This tab carried no downstream count, so nothing here says what feeds on it."
                : downstreamCount === 0
                  ? "Semantic Views and Reports that read its outputs would appear here."
                  : undefined
            }
            action={
              <Button
                variant="secondary"
                size="sm"
                disabled={!onNavigateTab}
                onClick={() => onNavigateTab?.("outputs")}
                data-testid="stage-publish-action"
              >
                See what it feeds
              </Button>
            }
          />
        </ol>
        {/* THE WHOLE PANEL, NOT HALF OF IT — arbitrage 7. The amendment of
            2026-08-05 names three things as seen AND SET here: cadence, arrival
            hour and next run. Embedding only two would send the arrival hour
            back to `Processing`, which is exactly what the amendment took out of
            it. One component, two mounts, ONE `PUT …/schedule`. */}
        {/* WHAT THE COLLECTION ANSWERED, INSIDE THE STAGE THAT ASKED.
            A refusal is NOT rendered as a confirmation a person has to dismiss:
            `useCollectNow` reads the window first, and when the server names a
            gate there is no dialog at all — the sentence lands here, with the
            gesture that releases it, where the button that was pressed is.

            `not_armed` on an archived Datastream now reads "Restore it before
            collecting anything for it", and on a stopped one "Start it on the
            Schedule panel" — the panel that is one click below this line. Until
            2026-08-18 both said the second, including for archived streams that
            that panel refuses to start. */}
        {collectNow.refusal && (
          <div className="border-t border-divider-base p-5">
            <Status
              as="block"
              tone="warning"
              title="This Datastream will not collect yet"
              action={
                <Button variant="secondary" size="sm" onClick={() => collectNow.dismiss()}>
                  Dismiss
                </Button>
              }
            >
              {collectNow.refusal.message}
            </Status>
          </div>
        )}
        {/* ONLY THE PRE-FLIGHT FAILURE LANDS HERE, and `phase === "idle"` is what
            says so. Without that clause a run refused by the queue printed its
            sentence TWICE — once inside the confirmation, which stays open
            carrying it, and once again on the stage behind. Two copies of one
            sentence make a reader check whether they agree. */}
        {collectNow.error && collectNow.phase === "idle" && (
          <div className="border-t border-divider-base p-5">
            <Status
              as="block"
              tone="error"
              title="Nothing was asked of the provider"
              action={
                <Button variant="secondary" size="sm" onClick={() => collectNow.dismiss()}>
                  Dismiss
                </Button>
              }
            >
              {collectNow.error}
            </Status>
          </div>
        )}
        {runStarted && (
          <div className="border-t border-divider-base p-5">
            <Status
              as="block"
              tone="success"
              title="Collection queued"
              action={
                onNavigateTab && (
                  <Button variant="secondary" size="sm" onClick={() => onNavigateTab("runs")}>
                    Go to Runs
                  </Button>
                )
              }
            >
              {runStartedSentence(runStarted)}
            </Status>
          </div>
        )}
        {scheduleOpen ? (
          <div className="border-t border-divider-base p-5" data-testid="stage-collect-schedule">
            <SchedulePanel
              projectId={projectId}
              datastreamId={datastreamId}
              // Amendment 1 of the 2026-08-11 review: a pushed source has no
              // pull cadence, and the panel is the one place that knows it.
              mode={header.identity.mode}
              deliveryChannels={deliveryChannels}
            />
          </div>
        ) : null}
      </Panel>

      <CollectNowDialog
        open={collectNow.phase === "confirming"}
        onOpenChange={(next) => { if (!next) collectNow.dismiss(); }}
        preview={collectNow.preview}
        datastreamId={datastreamId}
        datastreamName={header.identity.name}
        connector={connector}
        sourceAccountRef={sourceAccountRef}
        busy={collectNow.busy}
        error={collectNow.error}
        onConfirm={() => void collectNow.confirm()}
      />

      {/* The next safe step, in words. The server decides it and sends its
          REASON with it; the reason lived only in the `title` of the header
          button — a tooltip, which is to say invisible. "Accompany the person"
          starts with saying why this is the step, not printing a verb.

          It sits UNDER the flow, not above it: the flow says which of the four
          stages waits, this says the one thing to do about it. Read the other
          way round, the verb arrives before the subject. */}
      <Status
        as="block"
        tone={ACTION_TONE[action.kind] ?? "neutral"}
        title={`Next step · ${action.label}`}
        action={
          onNavigateTab && (
            <Button variant="secondary" size="sm" onClick={() => onNavigateTab(action.tab)}>
              Go to {titleCase(action.tab)}
            </Button>
          )
        }
      >
        {action.reason}
      </Status>
      {state !== "available" && (
        <Status as="block" tone="warning" title="Overview evidence unavailable">
          No operational summary is inferred while canonical evidence is missing.
        </Status>
      )}

      {/* MAPPING HEALTH — the fourth thing this tab's function names, and the
          one the first repair did not carry. It is not a fifth metric tile: it
          is only ever a REASON TO GO SOMEWHERE, because nothing about a mapping
          is settled here. `blocking_count` is the actionable half — it names the
          distance to an executable mapping, where `executable` alone says only
          yes or no. When there is no mapping at all, that is a different and
          louder statement than "some bindings are unresolved". */}
      {mapping.state !== "healthy" && (
        <Status
          as="block"
          tone={mapping.state === "absent" ? "error" : "warning"}
          title={mapping.title}
          action={onNavigateTab && (
            <Button variant="secondary" size="sm" onClick={() => onNavigateTab("mapping")}>
              Go to Mapping
            </Button>
          )}
        >
          {mapping.detail}
        </Status>
      )}

      {/* THE FOUR NAMED METRICS. `datastream-workbench-and-wizard.md` asks
          Overview for "cadence, freshness and history coverage; latest run", and
          the validated mockup leads with exactly these four numbers. The tab
          rendered none of them — not because it drew them badly, but because the
          server composed no field for any of them.

          `Freshness` carries the watermark in its hint since D-4: the age of the
          publication and the data boundary are two questions, and they were
          answered by two blocks that both called themselves freshness. */}
      <Panel flush className="grid grid-cols-4 divide-x divide-divider-base max-lg:grid-cols-2">
        <Metric label="Freshness" value={freshness} hint={freshnessHint} />
        <Metric
          label="Run success"
          value={runSuccess.terminal > 0 ? `${runSuccess.succeeded}/${runSuccess.terminal}` : "No run yet"}
          hint={runSuccess.terminal > 0
            ? `${runSuccess.rate} · last ${runSuccess.windowDays} days`
            : `Terminal runs only, last ${runSuccess.windowDays} days`}
        />
        <Metric
          label="Latest volume"
          value={typeof publishedRowCount === "number" ? formatNumber(publishedRowCount) : "Unavailable"}
          hint="Rows in the current publication"
        />
        <Metric label="Next run" value={nextRun} hint={text(schedule?.mode, "No cadence set")} />
      </Panel>

      {/* THE DAYS COLLECTED, from the extract ledger's own vocabulary. The
          component is mounted AS IT IS — a read-only variant would be a second
          component on one endpoint, and the re-collection confirmation of story
          58.4 lives inside this one. */}
      <CoverageStrip
        projectId={projectId}
        datastreamId={datastreamId}
        connector={connector}
        sourceAccountRef={sourceAccountRef}
      />

      {/* ─── MODULES, AND WHAT EACH ONE COMPILES HERE — one panel ─────────────
          Amendment 6 of the ratified target: "Les capacités se pilotent depuis
          l'`Overview`. Un interrupteur par capacité, sa portée en une phrase, et
          le nom de l'onglet qu'elle ajoute. `Project Settings` reste le
          propriétaire de l'activation et de ses versions."

          And amendment 11, the half that stayed open until now: « Les deux
          panneaux `Modules` et `Project capabilities on this Datastream`
          fusionnent en un seul ». They enumerated the same six capabilities,
          2000px apart, one giving the Project decision and the other the
          compilation of that same capability against this Datastream. One row
          now carries both, and `WorkbenchCapabilityPanel` draws it — nothing
          was deleted to get there.

          The rows come from the SERVER — five until story 61.5, six since:
          neither number is written here, which is why the sixth capability was a
          payload change and not a rewrite of this screen. */}
      <WorkbenchCapabilityPanel
        projection={capabilityProjection(payload.evidence.capabilities)}
        // `undefined` on the header means the capability state could not be
        // read, and `null` is how this panel is told so — absent would make it
        // the projection-only panel of the other tabs instead.
        modules={capabilityTabs ?? null}
        onRetryCapabilities={onRetryCapabilities}
        onModuleToggle={(change) => setConfirmingCapability(change)}
        moduleBusy={preparing !== null}
        renderModuleNotice={(key) => {
          const notice = prepared?.capabilityKey === key ? prepared : null;
          if (notice?.kind === "prepared") {
            return (
              <Status
                as="block"
                tone="neutral"
                title={`A Change Set is prepared to turn ${capabilityLabel(key)} ${notice.enabling ? "on" : "off"}`}
                action={onOpenOwner && (
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() =>
                      onOpenOwner({
                        surface: "global",
                        workspace: null,
                        section: null,
                        global_surface: "project-settings",
                        global_section: "changes",
                        object_type: null,
                        object_id: null,
                        tab: null,
                        action: null,
                        version_id: null,
                        evidence_id: null,
                      })
                    }
                  >
                    Open Project Settings › Changes
                  </Button>
                )}
              >
                Nothing has changed yet. Change Set{" "}
                <ObjectId value={notice.changeSetId} title="Project Change Set" /> is waiting
                for its confirmation in Project Settings › Changes, which is the only place a
                capability is activated. The state it lands in — Ready or Degraded — is
                decided there, from the evidence, not by this switch.
              </Status>
            );
          }
          if (notice?.kind === "failed") {
            return (
              <Status as="block" tone="error" title={`No Change Set was prepared for ${capabilityLabel(key)}`}>
                {notice.message}
              </Status>
            );
          }
          return null;
        }}
        onOpenOwner={onOpenOwner}
      />

      <ConfirmDialog
        open={confirmingCapability !== null}
        onOpenChange={(open) => {
          if (!open) setConfirmingCapability(null);
        }}
        title={`Prepare a Change Set for ${confirmingCapability?.label ?? ""}`}
        description={
          confirmingCapability?.next
            ? `This prepares a Change Set to turn ${confirmingCapability?.label} on. Nothing changes until it is confirmed in Project Settings › Changes, which is the only place a capability is activated.`
            : `This prepares a Change Set to turn ${confirmingCapability?.label} off. Nothing changes until it is confirmed in Project Settings › Changes; metrics that depend on it would stop being collected from that moment.`
        }
        confirmLabel="Prepare change"
        busy={preparing !== null}
        error={prepared?.kind === "failed" ? prepared.message : null}
        onConfirm={() => {
          if (confirmingCapability) {
            void confirmCapabilityChange(confirmingCapability.key, confirmingCapability.next);
          }
        }}
      />

      {isInbound && (
        <WorkbenchDeliveryPanel
          datastreamId={header.identity.datastream_id}
          connectorName={header.identity.connector!}
          channels={deliveryChannels}
          onRepairMapping={onRepairMapping}
        />
      )}

      {/* ─── THE EXACT REFERENCES ─────────────────────────────────────────────
          ONE drawer where there were three panels, and every fact of the two
          that closed is in it. What closed, and where each of its facts went:

          `Latest run / Candidate / Current / Last-known-good` (four tiles) —
          the WORDS were the `Publication` axis of the header a screen higher,
          which already reads `current served · candidate waiting ·
          last-known-good retained`, and the `Outputs` tab renders the same three
          roles with the same three words AND opens the run behind each. So the
          duplicated states go, and the POINTERS — the only thing those tiles
          held that nothing else did — are the four rows below. `Latest run`
          keeps its state word here because no axis carries it.

          `Operating posture` (seven rows) — `Freshness watermark` is now the
          hint of the `Freshness` metric, where the two readings can no longer
          disagree in silence; `Downstream usage` is the `Publish` stage, which
          prints the same count as an invitation rather than a zero; the other
          five are here, unchanged, with `Physical` dropped from a label that was
          naming a table rather than a fact.

          Nothing here is a state anybody has to act on: it is what you cite when
          you open a ticket, which is why it sits last. */}
      <Panel flush>
        <PanelHeader
          title="Exact references"
          description="The versions pinned to this Datastream, the run behind each publication role, and what the last run recorded. Active and proposed versions remain independent."
        />
        <dl className="grid grid-cols-4 gap-5 p-5 text-ui max-lg:grid-cols-2 max-md:grid-cols-1">
          <div><dt className="text-text-secondary">Active plan</dt><dd className="m-0 font-mono text-text">{header.versions.active_plan ?? "Unavailable"}</dd></div>
          <div><dt className="text-text-secondary">Active mapping</dt><dd className="m-0 font-mono text-text">{header.versions.active_mapping ?? "Unavailable"}</dd></div>
          <div><dt className="text-text-secondary">Proposed plan</dt><dd className="m-0 font-mono text-text">{header.versions.proposed_plan ?? "None"}</dd></div>
          <div><dt className="text-text-secondary">Proposed mapping</dt><dd className="m-0 font-mono text-text">{header.versions.proposed_mapping ?? "None"}</dd></div>
          {/* A missing pointer says `None` and never `Unavailable`: no candidate
              is a real answer, an unreadable one would be a different fact. */}
          <div>
            <dt className="text-text-secondary">Latest run</dt>
            <dd className="m-0 text-text">
              {header.runs.latest ? (header.runs.latest_state ?? "Unknown state") : "None"}
              {header.runs.latest && (
                <span className="block"><ObjectId value={header.runs.latest} title="Run" /></span>
              )}
            </dd>
          </div>
          <div>
            <dt className="text-text-secondary">Candidate publication</dt>
            <dd className="m-0 text-text">
              {header.publications.candidate
                ? <ObjectId value={header.publications.candidate} title="Candidate publication" />
                : "None"}
            </dd>
          </div>
          <div>
            <dt className="text-text-secondary">Current publication</dt>
            <dd className="m-0 text-text">
              {header.publications.current
                ? <ObjectId value={header.publications.current} title="Current publication" />
                : "Nothing served"}
            </dd>
          </div>
          <div>
            <dt className="text-text-secondary">Last-known-good</dt>
            <dd className="m-0 text-text">
              {header.publications.last_known_good
                ? <ObjectId value={header.publications.last_known_good} title="Last-known-good publication" />
                : "None"}
            </dd>
          </div>
          <div>
            <dt className="text-text-secondary">Recorded stage evidence</dt>
            <dd className="m-0 text-text">
              {stageCoverage.length > 0
                ? `Evidence on ${ALL_STAGES.filter((stage) => coveredStages.has(stage)).map((stage) => titleCase(stage)).join(", ")}`
                : "No stage has recorded any evidence"}
              {missingStages.length > 0 && (
                <span className="block text-caption text-text-secondary">
                  No evidence: {missingStages.map((stage) => titleCase(stage)).join(", ")}
                </span>
              )}
            </dd>
          </div>
          <div>
            <dt className="text-text-secondary">Business domain</dt>
            <dd className="m-0 text-text">
              {Array.isArray(header.identity.business_domains) && header.identity.business_domains.length > 0
                ? header.identity.business_domains.map((domain: any) => text(domain.name ?? domain.id ?? domain)).join(", ")
                : "Unassigned"}
            </dd>
          </div>
          <div><dt className="text-text-secondary">Retries</dt><dd className="m-0 text-text">{typeof schedule?.retry_count === "number" ? schedule.retry_count : "Unavailable"}</dd></div>
          <div><dt className="text-text-secondary">Missed runs</dt><dd className="m-0 text-text">{typeof schedule?.missed_run_count === "number" ? schedule.missed_run_count : "Unavailable"}</dd></div>
          <div className="col-span-4 max-lg:col-span-2 max-md:col-span-1">
            <dt className="text-text-secondary">Latest blocker</dt>
            <dd className="m-0 text-text">
              {latest?.error_code
                ? `${text(latest.error_code)} · ${text(latest.error_detail)}`
                : "None evidenced"}
            </dd>
          </div>
        </dl>
      </Panel>
    </>
  );
}

/** A key the change-set endpoint can deduplicate on.
 *
 *  `crypto.randomUUID` is what `ProjectSettings` uses; it is absent from some
 *  test environments and from a non-secure origin, and a THROWN key would lose
 *  the write entirely. The fallback is unique enough for a deduplication window
 *  and never silently reuses one. */
function newIdempotencyKey(): string {
  const source = globalThis.crypto;
  if (source && typeof source.randomUUID === "function") return source.randomUUID();
  return `idem-${Date.now()}-${Math.random().toString(36).slice(2, 12)}`;
}
