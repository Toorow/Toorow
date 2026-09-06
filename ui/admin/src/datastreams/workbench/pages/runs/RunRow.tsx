import type { ReactNode } from "react";
import {
  CopyButton, PhaseTimeline, SortableHead, Status, TableBody, TableCell, TableHead,
  TableHeader, TableRow,
  type Phase, type SortState,
  formatNumber,
} from "../../../../ui";
import { dateTime, record, records, text } from "../../evidence";
import RunAnomalies from "../../RunAnomalies";
import DatastreamRecoveryDialog from "../../DatastreamRecoveryDialog";
import type { Tab } from "../../../../shell/pages/datastreamTabs";
import { executionStateLabel, phaseOf } from "../../executionStates";
import { NOT_MEASURED, toneForPhase } from "../../DatastreamRunLive";
import { isRunMoving, isRunProgressing } from "../../datastreamProgress";
import {
  NO_PHASE_EVIDENCE, NO_STEP_SPAN, NOT_STARTED, NOT_STARTED_SENTENCE, STEP_NOT_REACHED,
  STEP_NOT_TIMED, STEP_STILL_RUNNING, absencesOf, durationText, originText, rowsText,
  runDurationText, startedText, windowText,
} from "./runFacts";

/**
 * ONE RUN, AS A ROW THAT UNFOLDS — amended 2026-08-12 (issue #69, D-1/D-7/D-9).
 *
 * 58.10 made the run the unit and was right; it made the run a PANEL, and that
 * only held up against a fixture with two runs in it. This tab is bounded to
 * sixty, and sixty panels — each carrying three measurement tiles, a four-step
 * track, an anomaly region, a diagnosis and a seven-phase timeline — is not a
 * history anybody can scan for the run that failed.
 *
 * So the run is a ROW, and the row is the whole first reading: which run, why it
 * exists, when it started, what it covered, what it landed, how long it took,
 * and whether anything was found on it. Nothing there needs a click.
 *
 * The unfolding carries the SECOND question — why did it end like that, how long
 * did each step take, what exactly was found, and what can I do about it — and
 * it is one disclosure, not two: the diagnosis used to sit behind a further
 * `Show diagnosis and phases` inside the block, which is a click charged for
 * arriving at the thing the tab exists to answer.
 *
 * THE RUN'S SUBTREE IS THE `tbody`. Both rows live in it, so `data-run-id` still
 * scopes everything about one run — including the anomaly region, whose
 * containment is the guard that refuses a modal (a dialog portals out of the
 * subtree and the assertion falls).
 */

/**
 * Story 63.1: read from the shared registry, never re-listed here.
 *
 * `running` and `queued` are PHASE-EVIDENCE values
 * (`app.datastream_execution_phase_evidence.phase_state`), not execution states,
 * so they are resolved here and everything else defers to the registry.
 */
function phaseState(value: unknown): Phase["state"] {
  const state = String(value ?? "").toLowerCase();
  if (state === "running" || state === "queued") return "running";
  return phaseOf(state);
}

/**
 * A failed or uncertain run must not read like a slow one.
 *
 * Story 63.5: the mapping itself is the one `toneForPhase` the live band uses,
 * so a run in flight is the same colour in the row, in the band above it and in
 * the fleet list.
 */
function runTone(state: unknown) {
  return toneForPhase(phaseState(state));
}

type StepSpan = {
  step: string;
  started_at: unknown;
  ended_at: unknown;
  duration_seconds: unknown;
};

function spansOf(run: Record<string, unknown>): StepSpan[] {
  return records(run.steps).map((span) => ({
    step: text(span.step, ""),
    started_at: span.started_at,
    ended_at: span.ended_at,
    duration_seconds: span.duration_seconds,
  }));
}

/**
 * The activity track: `Collect · Map · Check · Publish`, each with the time it
 * took -- story 58.10, and it is the run's OWN reading.
 *
 * IT IS NOT THE SEVEN-PHASE TIMELINE, AND IT IS NOT DERIVED FROM IT.
 * `datastream-workbench-and-wizard.md` ratifies that the four steps and the
 * seven phases of `app.datastream_execution_phase_evidence` are two
 * vocabularies and stay two; the spans come from
 * `app.datastream_execution_step_evidence` (migration 223), which the four
 * steps have to themselves. Folding phases onto steps at render time would fuse
 * the two in the read model, which is the same fusion in a different coat.
 *
 * THE FOUR NAMES COME FROM THE PAYLOAD. `evidence.step_vocabulary` is
 * `core.execution_progress.PROGRESS_STEPS`; a list typed here would be a second
 * vocabulary to keep in step, which is the defect `EXECUTION_STATES` exists
 * against.
 *
 * `ended_at` NULL is TWO different facts, and they are two sentences: a run
 * still moving is on that step right now, a run that ended recorded no end for
 * it. Neither is `0 s`.
 */
function StepTrack({
  run,
  vocabulary,
}: {
  run: Record<string, unknown>;
  vocabulary: readonly string[];
}) {
  const spans = spansOf(run);
  const moving = isRunMoving(run.state);

  if (spans.length === 0) {
    return (
      <p className="m-0 text-caption text-text-secondary" data-testid="run-track-absent">
        {NO_STEP_SPAN}
      </p>
    );
  }

  return (
    <ol
      className="m-0 grid list-none grid-cols-4 gap-2 p-0 max-lg:grid-cols-2"
      data-testid="run-track"
      aria-label="Step track"
    >
      {vocabulary.map((name) => {
        const span = spans.find((entry) => entry.step === name);
        const raw = typeof span?.duration_seconds === "number" ? span.duration_seconds : null;
        // MEASURED means an elapsed time that is really elapsed. `0` is what a
        // span written twice in one transaction produces, and it is an absence,
        // not a fast step.
        const seconds = raw !== null && raw > 0 ? raw : null;
        // STILL RUNNING IS THE RUN'S STATE'S ANSWER, NOT THE SPAN'S. Four
        // writers reach a terminal state without closing anything, so an open
        // span on a finished run is an unmeasured step — never a working one.
        const stillRunning = moving && span?.ended_at == null;
        const said =
          span === undefined
            ? STEP_NOT_REACHED
            : seconds !== null
              ? durationText(seconds)
              : stillRunning
                ? STEP_STILL_RUNNING
                : STEP_NOT_TIMED;
        return (
          <li
            key={name}
            data-testid={`run-step-${name}`}
            data-measured={seconds !== null ? "true" : "false"}
            className="rounded-large border border-divider-base bg-surface-light px-3 py-2"
          >
            <span className="block text-caption text-text-secondary">{name}</span>
            <span className="mt-1 block text-ui text-text">{said}</span>
          </li>
        );
      })}
    </ol>
  );
}

/** Why this run ended the way it did, and what produced it. */
function RunDiagnosis({
  run,
  onNavigateTab,
}: {
  run: Record<string, unknown>;
  onNavigateTab?: (tab: Tab) => void;
}) {
  const code = text(run.error_code, "");
  const detail = text(run.error_detail, "");
  const bindings: ReadonlyArray<[string, string, Tab | null]> = [
    ["Plan version", text(run.plan_version_id, "Unavailable"), "processing"],
    ["Mapping version", text(run.mapping_version_id, "Unavailable"), "mapping"],
    ["Adapter", text(run.adapter_ref, "Unavailable"), null],
    ["Content hash", text(run.content_hash, "Unavailable"), null],
  ];
  return (
    <div className="grid gap-4">
      {/* THE RUN'S IDENTIFIER, TAKEABLE — amended 2026-08-18.
          This id is the handle every other surface asks for: a ticket, a log
          search, an MCP call, the `q` box at the top of this very tab. It was
          drawn as a truncated mono string on the row and nowhere else, so the
          only way to carry it anywhere was to select seven characters of a
          truncation by hand. `CopyButton` is the one clipboard write in this
          console that reports a REFUSAL instead of announcing a success the
          browser never granted.
          There is no permalink beside it, and that is measured rather than
          forgotten: the console's address grammar stops at `/tab/{tab}` and the
          opened run is component state on `DatastreamWorkbenchRoute`. A "copy
          link to this run" would hand somebody an address that resolves to the
          unfiltered list — worse than none.
          AND THE ID IS NOT DRAWN AGAIN HERE. It is on the row four lines above,
          in full in its `title`; printing it a second time inside its own
          disclosure is exactly the "same word twice" this tab's amendment of
          2026-08-12 spent itself removing. The button carries what it copies in
          its label, which is what `CopyButton` asks of every adopter. */}
      <div className="flex flex-wrap items-center gap-3" data-testid="run-identity">
        <CopyButton
          value={text(run.id, "")}
          label="Copy run id"
          size="sm"
          data-testid="run-copy-id"
        />
      </div>
      {code ? (
        // THE CODE EXACTLY AS IT ARRIVED. `titleCase` turned an error code the
        // server sent into a sentence that looks like a product word, so a code
        // this build has never seen read as a designed message. A raw key a
        // person can search for is worth more than a plausible wrong word.
        <Status as="block" tone={runTone(run.state)} title={code}>
          {detail || "The provider returned this code without a message."}
        </Status>
      ) : (
        <Status as="block" tone={runTone(run.state)} title={executionStateLabel(run.state)}>
          No error was recorded for this execution.
        </Status>
      )}
      {/* The versions that produced this run. Clicking them navigates directly to the corresponding tab. */}
      <dl className="m-0 grid grid-cols-2 gap-x-4 gap-y-2 max-lg:grid-cols-1">
        {bindings.map(([label, value, targetTab]) => (
          <div key={label}>
            <dt className="text-caption text-text-secondary">{label}</dt>
            <dd className="m-0 truncate font-mono text-caption text-text" title={value}>
              {targetTab && onNavigateTab && value !== "Unavailable" ? (
                <button
                  type="button"
                  className="font-mono text-link hover:underline"
                  onClick={() => onNavigateTab(targetTab)}
                >
                  {value}
                </button>
              ) : (
                value
              )}
            </dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

/**
 * WHAT THIS RUN DID NOT MEASURE — the fact repeated with its reason, once,
 * inside the run it belongs to.
 *
 * Only the absences this run actually carries appear, so a complete run shows
 * nothing here at all. The term is ONE text node (`label · said`) on purpose: a
 * nested span carrying the reading alone would be a second element with the same
 * text as the row's cell, and "the same word twice" is what this whole amendment
 * is against.
 */
function WhatWasNotMeasured({ run }: { run: Record<string, unknown> }) {
  const absences = absencesOf(run);
  if (absences.length === 0) return null;
  return (
    <div className="grid gap-2" data-testid="run-absences">
      <h4 className="m-0 text-label font-label text-text">What this run did not measure</h4>
      <dl className="m-0 grid gap-2">
        {absences.map((absence) => (
          <div key={absence.label}>
            <dt className="text-caption text-text-secondary">{`${absence.label} · ${absence.said}`}</dt>
            <dd className="m-0 mt-1 text-caption text-text-secondary">{absence.why}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

/** A cell that says nothing at all when the run has not begun — never a dash. */
function Measurement({ started, children }: { started: boolean; children: ReactNode }) {
  return <TableCell>{started ? children : null}</TableCell>;
}

export const RUN_COLUMNS: readonly string[] = [
  "State", "Run", "Started", "What it covered", "Rows collected", "Duration", "Anomalies",
];

/**
 * Which columns carry an order, and the key each one sorts by — amended
 * 2026-08-18.
 *
 * TWO, NOT SEVEN. `Started` and `Duration` are the two a person compares runs
 * by; the other five are either a word already offered as a chip (`State`), an
 * identifier already searchable (`Run`), or an absence on most rows (`What it
 * covered`, `Rows collected`, `Anomalies`) — a header that sorts a column of
 * `Not measured` promises an order over nothing.
 */
const RUN_SORT_KEYS: Readonly<Record<string, string>> = { Started: "started", Duration: "duration" };

/**
 * The header the run rows hang under. One header for sixty runs, not sixty
 * labels.
 *
 * `SortableHead` composes `TableHead`, so a column that does not sort is exactly
 * the cell it was — and `aria-sort` lands on the two that do rather than on all
 * seven, which is the difference between "this column can be ordered and is not"
 * and "this column never orders".
 */
export function RunTableHeader({
  sort = null,
  onSort,
}: {
  sort?: SortState | null;
  onSort?: (key: string) => void;
} = {}) {
  return (
    <TableHeader>
      <TableRow>
        {RUN_COLUMNS.map((column) => {
          const sortKey = RUN_SORT_KEYS[column];
          return sortKey && onSort ? (
            <SortableHead key={column} sortKey={sortKey} sort={sort} onSort={onSort}>
              {column}
            </SortableHead>
          ) : (
            <TableHead key={column}>{column}</TableHead>
          );
        })}
        <TableHead className="text-right">
          <span className="sr-only">Detail</span>
        </TableHead>
      </TableRow>
    </TableHeader>
  );
}

export default function RunRow({
  run,
  vocabulary,
  timeline,
  open,
  selected,
  projectId,
  datastreamId,
  onSelect,
  onToggle,
  onConfirmed,
  onNavigateTab,
}: {
  run: Record<string, unknown>;
  vocabulary: readonly string[];
  timeline: Array<Record<string, unknown>>;
  open: boolean;
  selected: boolean;
  projectId: string;
  datastreamId: string;
  onSelect: () => void;
  onToggle: () => void;
  onConfirmed: () => void;
  onNavigateTab?: (tab: Tab) => void;
}) {
  const runId = text(run.id);
  const started = startedText(run) !== NOT_STARTED;
  const origin = originText(run);
  const counts = record(run.anomalies);
  const found = typeof counts?.anomalies === "number" ? counts.anomalies : 0;
  const evaluated = typeof counts?.evaluations === "number" ? counts.evaluations : 0;
  const detailId = `run-detail-${runId}`;
  const phases = timeline
    .filter((item) => item.execution_id === run.id)
    .map<Phase>((item) => ({
      // The seven phases are a WIRE vocabulary with no console registry, so the
      // key is shown as it arrived. `titleCase` invented a label for it, and a
      // phase this build does not know would have read as a designed word.
      label: text(item.phase, "Unknown phase"),
      evidence: `${text(item.plan_version_id)} · ${text(item.mapping_version_id)}`,
      at: dateTime(item.occurred_at),
      state: phaseState(item.phase_state),
    }));

  return (
    <TableBody
      data-testid="run-block"
      data-run-id={runId}
      // `data-state`, NOT `data-selected` — the cross-tab contract of story 58.2
      // pins this exact attribute: opening a day on `Data` navigates here with
      // the id of the run that collected it, and `WorkbenchDataPage.test.tsx`
      // asserts the marked subtree is that run. Renaming it while moving the
      // block to a row broke « l'id voyage » with every unit test still green,
      // because the assertion that catches it lives on the OTHER tab.
      data-state={selected ? "selected" : undefined}
      className={selected ? "bg-surface-light" : undefined}
    >
      <TableRow
        interactive
        data-testid="run-summary"
        onClick={() => {
          onSelect();
          onToggle();
        }}
      >
        <TableCell>
          {/* The registry's word, never `titleCase`: a state this build does
              not know reads `Unknown` instead of a fabricated label. */}
          <Status tone={runTone(run.state)} active={isRunProgressing(run.state)}>
            {executionStateLabel(run.state)}
          </Status>
        </TableCell>
        <TableCell>
          <span className="block truncate font-mono text-ui text-text" title={runId}>
            {runId}
          </span>
          {/* WHY THIS RUN EXISTS sits under WHICH RUN, because it is the same
              subject: before story 63.7 a nightly collection and a mapping
              change were two identical lines. */}
          <span className="block text-caption text-text-secondary" data-testid="run-origin">
            {origin ?? NOT_MEASURED}
          </span>
        </TableCell>
        <TableCell className="whitespace-nowrap">{startedText(run)}</TableCell>
        <Measurement started={started}>{windowText(run)}</Measurement>
        <Measurement started={started}>{rowsText(run)}</Measurement>
        <Measurement started={started}>{runDurationText(run)}</Measurement>
        <TableCell>
          {found > 0 ? (
            <Status tone="warning">{`${formatNumber(found)} found`}</Status>
          ) : evaluated > 0 ? (
            <span className="text-caption text-text-secondary">None found</span>
          ) : null}
        </TableCell>
        <TableCell className="text-right">
          <button
            type="button"
            className="rounded-pill border border-divider-base px-3 py-1 text-label font-label text-text"
            data-testid="run-details-trigger"
            aria-expanded={open}
            aria-controls={detailId}
            onClick={(event) => {
              // The row already toggles; without this the two handlers fire and
              // the detail closes the instant it opens.
              event.stopPropagation();
              onSelect();
              onToggle();
            }}
          >
            {open ? "Close" : "Open"}
          </button>
        </TableCell>
      </TableRow>
      {/* THE DETAIL ROW IS ALWAYS MOUNTED, and `hidden` is what closes it. A row
          rendered only when open would make "is this run's anomaly region inside
          this run's subtree" unanswerable for every folded run, which is the one
          assertion that refuses a modal anomaly. */}
      <TableRow hidden={!open} id={detailId} data-testid="run-detail">
        <TableCell colSpan={RUN_COLUMNS.length + 1} className="p-0">
          <div className="grid gap-4 px-4.5 py-4">
            <RunDiagnosis run={run} onNavigateTab={onNavigateTab} />
            {started ? (
              <StepTrack run={run} vocabulary={vocabulary} />
            ) : (
              <p className="m-0 text-ui text-text-secondary" data-testid="run-not-started">
                {NOT_STARTED_SENTENCE}
              </p>
            )}
            <WhatWasNotMeasured run={run} />
            <RunAnomalies run={run} projectId={projectId} datastreamId={datastreamId} />
            {phases.length > 0 ? (
              <PhaseTimeline phases={phases} />
            ) : (
              <p className="m-0 text-caption text-text-secondary">{NO_PHASE_EVIDENCE}</p>
            )}
            {/* Arbitrage 6: `Run it again` is this dialog, already the only
                surface that prepares and confirms ONE run's recovery. A second
                door would be a second set of refusals to keep in step. */}
            <div>
              <DatastreamRecoveryDialog
                projectId={projectId}
                datastreamId={datastreamId}
                run={run}
                onConfirmed={onConfirmed}
              />
            </div>
          </div>
        </TableCell>
      </TableRow>
    </TableBody>
  );
}
