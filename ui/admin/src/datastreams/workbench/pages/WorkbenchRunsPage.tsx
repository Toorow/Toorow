import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Button, ChoiceGroup, Collapsible, CollapsibleContent, CollapsibleTrigger, EmptyState, Input,
  NativeSelect, Pager, Panel, SortScopeNote, Table, useTableSort, sortRows,
  formatNumber,
} from "../../../ui";
import { apiGet } from "../../../lib/apiFetch";
import { records, text } from "../evidence";
import { EvaluationsWithoutRun } from "../RunAnomalies";
import DatastreamReloadPanel from "../DatastreamReloadPanel";
import CoverageStrip from "../../../shell/pages/CoverageStrip";
import type { Tab } from "../../../shell/pages/datastreamTabs";
import type { OwnerReference } from "../../../shell/pages/ProjectSettings";
import type { WorkbenchTabPayload } from "../workbenchTypes";
import { EXECUTION_STATES, executionStateLabel } from "../executionStates";
import { originLabel } from "../runOrigins";
import RunRow, { RunTableHeader } from "./runs/RunRow";
import {
  WHY_ABSENT_DETAIL, WHY_ABSENT_SUMMARY, WHY_ABSENT_TRIGGER, absencesOf, originText,
  rowsText, runDurationText, startedText, windowText,
} from "./runs/runFacts";

/**
 * THE RUN HISTORY — amended 2026-08-12 (visual review #69, findings D-1/D-7/D-9)
 * and again 2026-08-18 (the cap nobody could walk past).
 *
 * What was measured on the repaired sandbox at 1600px: each of the two run
 * blocks carried SEVEN explanatory paragraphs, naming four table columns, two
 * function names, three migration numbers and one story id. This tab is bounded
 * to sixty runs. That is four hundred paragraphs of the repository talking to
 * itself, on the screen somebody opens to ask which run failed.
 *
 * THE FACT STAYS; THE EXPLANATION MOVES. Nothing 58.10 established is withdrawn:
 * a counter that was never written still says so — never a `0`, never a dash,
 * never a silent fallback to a neighbouring measurement. Measured over 428
 * executions on 2026-08-07, the absences ARE what this tab mostly renders. What
 * moved is the REASON: it is a second question, asked about ONE run, and it is
 * answered inside that run when it is asked (`runFacts.absencesOf`). The
 * writers' own names — the columns, the functions, the migrations — are folded
 * ONCE for the whole tab, below, because they answer a question about the
 * platform rather than about a run.
 *
 * And the run is a ROW, not a panel: sixty panels is not a history anybody can
 * scan. `RunRow` carries the whole contract of one run.
 *
 * ---------------------------------------------------------------------------
 * AMENDMENT OF 2026-08-18 — THE CAP HAD NO DOOR, AND THE FILTER LIED ABOUT IT.
 *
 * What this tab did: it announced « Showing the 200 most recent runs; this
 * Datastream has more » and offered nothing that reached the rest. Every control
 * beside that sentence — the state chips — filtered the 200 rows already in
 * hand, so a chip over a capped list narrowed a SAMPLE and presented it as a
 * history: `failed` on a Datastream whose last failure was on page two answered
 * "no run in this state".
 *
 * FOUR CHANGES, AND THE FIRST DECIDES THE OTHER THREE.
 *
 *   1. THE NARROWING IS THE SERVER'S. `read_tab` takes `options` per tab and has
 *      since story 61.1 (`placements` reads `plan_id` off the same query
 *      string); the runs branch now reads `state`, `origin`, `from`, `to` and
 *      `q`, and answers how many runs MATCH over the whole collection. The
 *      ratified sentence « le filtre d'état est côté client ... sans `?state=`
 *      sur une route partagée » is withdrawn by the amendment of this date: it
 *      described a tab that drew one uncapped list, and it cannot survive
 *      paging.
 *   2. THE CHIPS ARE READ FROM THE COLLECTION, not from the page. The payload
 *      carries the states and origins that EXIST on this Datastream, so a chip
 *      never appears and disappears as somebody pages.
 *   3. THE PAGE IS WALKABLE. `Pager` over the server's keyset cursor —
 *      `First`, `Previous`, `Next`, with `Previous` DISABLED rather than absent
 *      on the first page, because the way back exists here (the cursors are held
 *      below) and hiding it would say it does not.
 *   4. THE ORDER ON SCREEN IS THE PAGE'S ORDER, AND IT SAYS SO. `SortableHead`
 *      on `Started` and `Duration` reorders the rows in hand; `SortScopeNote` is
 *      the sentence that stops a reader concluding the longest run on this page
 *      is the longest run of the Datastream.
 *
 * AND THE EXPORT IS THE PAGE, NEVER "the runs". One formatter (`runCells`)
 * feeds the table's own words and the file's, which is the pattern the Evidence
 * export shipped with: a second formatter is how a downloaded file comes to say
 * something the screen never said.
 */

// The vocabulary of the four absences keeps ONE home, and the page re-exports it
// so the tab's tests and any sibling surface read the same words.
export {
  WINDOW_NOT_RECORDED, WINDOW_ABSENCE, DURATION_ABSENCE, ROWS_ABSENCE, ORIGIN_ABSENCE,
  NO_STEP_SPAN, NO_PHASE_EVIDENCE, NOT_STARTED, NOT_STARTED_SENTENCE, START_NOT_RECORDED,
  STEP_NOT_REACHED, STEP_STILL_RUNNING, STEP_NOT_TIMED, WHY_ABSENT_SUMMARY, WHY_ABSENT_TRIGGER,
  WHY_ABSENT_DETAIL, durationText, startedText, absencesOf,
} from "./runs/runFacts";
// The two silences of the anomaly region live with the region itself (story
// 59.1) and are re-exported so the one wording keeps one home.
export { NO_MONITOR_EVALUATED, NO_ANOMALY_ON_RUN } from "../RunAnomalies";

/** The chip that selects no state at all. Not a state name, and never one. */
export const ALL_RUNS = "all";

/** The value of the origin selector that narrows on no origin. */
export const ALL_ORIGINS = "all";

/**
 * The state chips -- from `EXECUTION_STATES`, never from a list typed here.
 *
 * Only the states the loaded runs actually carry get a chip: fourteen chips
 * over a list of three runs would offer eleven filters that empty the screen.
 * The registry decides both the words and the order, so a chip can never name a
 * state the server does not know, nor spell one differently from the badge
 * directly under it.
 *
 * AMENDED 2026-08-18: `present` is the server's list of the states that exist on
 * the COLLECTION when it sends one (`run_states_present`), and the states of the
 * rows in hand only when it does not. Deriving it from the page made a chip
 * vanish the moment somebody paged past the last run carrying that state — and
 * with it the only control that could bring them back.
 */
export function stateChoices(runs: Array<Record<string, unknown>>, present?: readonly string[]) {
  const seen = new Set(
    (present ?? runs.map((run) => String(run.state ?? ""))).map((state) => state.toLowerCase()),
  );
  return [
    { value: ALL_RUNS, label: "All runs" },
    ...EXECUTION_STATES.filter((entry) => seen.has(entry.name)).map((entry) => ({
      value: entry.name,
      label: executionStateLabel(entry.name),
    })),
  ];
}

/**
 * ONE RUN AS TEXT, in the column order of the table above it.
 *
 * There is one of these and both readers use it: the row renders these strings
 * and the export writes them. A second formatter would let a downloaded file say
 * `0` where the screen says `Not measured`, which is the whole failure mode of
 * an export bolted onto a table — and on this tab, whose ordinary case IS an
 * absence, it is the failure that would matter most.
 */
export function runCells(run: Record<string, unknown>): string[] {
  const started = startedText(run);
  const notStarted = started === "Not started";
  return [
    executionStateLabel(run.state),
    text(run.id, ""),
    originText(run) ?? "Not measured",
    started,
    // A run that has not begun renders NO measurement on screen — not a dash,
    // not a zero. The file says the same nothing rather than a friendlier one.
    notStarted ? "" : windowText(run),
    notStarted ? "" : rowsText(run),
    notStarted ? "" : runDurationText(run),
    text(run.error_code, ""),
  ];
}

/** The header the file carries, and the words the table shows. */
export const RUN_EXPORT_COLUMNS: readonly string[] = [
  "State", "Run", "Origin", "Started", "What it covered", "Rows collected", "Duration",
  "Error code",
];

/** RFC 4180: quote a field carrying a separator, a quote or a newline. */
function csvField(value: string): string {
  return /[",\r\n]/.test(value) ? `"${value.replaceAll('"', '""')}"` : value;
}

/** The rows ON SCREEN, with the headers above them. Nothing is fetched here. */
export function runsCsv(runs: Array<Record<string, unknown>>): string {
  return [RUN_EXPORT_COLUMNS, ...runs.map(runCells)]
    .map((row) => row.map(csvField).join(","))
    .join("\r\n");
}

/** A value safe in a file name, with its own bound. */
function slug(value: string): string {
  return value.replace(/[^A-Za-z0-9._-]+/g, "-").slice(0, 40);
}

/**
 * A name that carries what the file actually holds: the Datastream, every
 * narrowing that produced it, whether it is a later page, and the row count —
 * so two exports of one Datastream under two filters never land on one another.
 */
export function runsExportName(
  datastreamId: string,
  filters: Record<string, string>,
  count: number,
): string {
  const parts = ["runs", slug(datastreamId)];
  for (const key of Object.keys(filters).sort()) {
    if (key === "cursor" || !filters[key]) continue;
    parts.push(`${key}-${slug(filters[key])}`);
  }
  if (filters.cursor) parts.push("later-page");
  parts.push(`${count}-rows`);
  return `${parts.join("_")}.csv`;
}

/**
 * WHAT WRITES EACH MEASUREMENT — folded, and written ONCE for the tab.
 *
 * The technical cause is the only honest answer to "what fills this in, exactly",
 * and it is the answer somebody REPAIRING the platform needs. It is not the
 * answer somebody reading a history needs, and charging them sixty copies of it
 * is what this amendment removed. So it lives here: one disclosure, under the
 * count, and only when a run on screen actually has an absence — an explanation
 * offered for a problem nobody has is noise of a second kind.
 *
 * The summary above the fold has to be TRUE WITHOUT IT: somebody who never opens
 * this must still leave knowing these are readings nobody took, not readings
 * that failed, and that nothing is back-filled afterwards.
 */
function WhyMeasurementsAreAbsent({ runs }: { runs: Array<Record<string, unknown>> }) {
  if (!runs.some((run) => absencesOf(run).length > 0)) return null;
  return (
    <Collapsible>
      <p className="m-0 text-caption text-text-secondary" data-testid="runs-absence-summary">
        {WHY_ABSENT_SUMMARY}
      </p>
      <CollapsibleTrigger
        className="mt-2 rounded-pill border border-divider-base px-4 py-1.5 text-label font-label text-text"
        data-testid="runs-absence-trigger"
      >
        {WHY_ABSENT_TRIGGER}
      </CollapsibleTrigger>
      <CollapsibleContent data-testid="runs-absence-detail">
        <dl className="m-0 mt-3 grid gap-2">
          {WHY_ABSENT_DETAIL.map((entry) => (
            <div key={entry.term}>
              <dt className="text-caption text-text-secondary">{entry.term}</dt>
              <dd className="m-0 mt-1 font-mono text-caption text-text-secondary">
                {entry.account}
              </dd>
            </div>
          ))}
        </dl>
      </CollapsibleContent>
    </Collapsible>
  );
}

/** The five narrowings this tab asks the server for. Empty means "do not narrow". */
interface RunFilters {
  state: string;
  origin: string;
  from: string;
  to: string;
  q: string;
}

const NO_FILTERS: RunFilters = { state: ALL_RUNS, origin: ALL_ORIGINS, from: "", to: "", q: "" };

/** What travels in the query string. The two "all" sentinels are not narrowings. */
function asQuery(filters: RunFilters, cursor: string | null): Record<string, string> {
  const query: Record<string, string> = {};
  if (filters.state !== ALL_RUNS) query.state = filters.state;
  if (filters.origin !== ALL_ORIGINS) query.origin = filters.origin;
  if (filters.from) query.from = filters.from;
  if (filters.to) query.to = filters.to;
  if (filters.q.trim()) query.q = filters.q.trim();
  if (cursor) query.cursor = cursor;
  return query;
}

/**
 * A text field that commits on Enter or on blur.
 *
 * Its draft lives here and nowhere else: committing on every keystroke would put
 * half-typed run ids in the server's log and re-read the collection six times
 * for one search. The pattern is `EvidenceCollection.CommittedInput`.
 */
function CommittedSearch({
  value,
  onCommit,
}: {
  value: string;
  onCommit: (next: string) => void;
}) {
  const [draft, setDraft] = useState(value);
  useEffect(() => setDraft(value), [value]);
  return (
    <Input
      aria-label="Search by run id or error code"
      placeholder="Run id or error code"
      value={draft}
      data-testid="runs-search"
      onChange={(event) => setDraft(event.target.value)}
      onBlur={() => draft !== value && onCommit(draft)}
      onKeyDown={(event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          onCommit(draft);
        }
      }}
    />
  );
}

export default function WorkbenchRunsPage({
  payload,
  projectId,
  datastreamId,
  connector,
  sourceAccountRef,
  onConfirmed,
  onNavigateTab,
  selectedRunId = null,
  onOpenOwner: _onOpenOwner,
}: {
  payload: WorkbenchTabPayload;
  projectId: string;
  datastreamId: string;
  /** The provider and the account the coverage strip's re-collection spends on
   *  — story 58.4. They live on `header.identity` and this tab receives only its
   *  own evidence, so the route hands them down rather than the strip inventing
   *  a second read of the header. */
  connector?: string | null;
  sourceAccountRef?: string | null;
  onConfirmed: () => void;
  onNavigateTab?: (tab: Tab) => void;
  /** The run somebody arrived HERE to read — story 58.2, arbitrage 7.
   *
   *  Opening a day of the `Data` grid means opening the run that collected that
   *  day. Landing on an unfiltered list of a 60-day window and asking the person
   *  to find it again is the gesture pretending to be delivered. */
  selectedRunId?: string | null;
  onOpenOwner?: (owner: OwnerReference) => void;
}) {
  /**
   * THE EVIDENCE ON SCREEN, which is the server's — the tab payload at first,
   * then whatever a narrowing or a page turn returned.
   *
   * A LOCAL RE-READ AND NOT A ROUTE RELOAD, the pattern `WorkbenchPlacementsPage`
   * already follows for its plan selector: narrowing a run list changes one
   * table, and reloading the route would re-read the header, the axes and the
   * issue badge to redraw it.
   */
  const [evidence, setEvidence] = useState<Record<string, unknown>>(
    payload.evidence as Record<string, unknown>,
  );
  useEffect(() => setEvidence(payload.evidence as Record<string, unknown>), [payload]);

  const [filters, setFilters] = useState<RunFilters>(NO_FILTERS);
  /** The cursor of the page on screen, and the ones walked to reach it.
   *
   *  A stack rather than an arithmetic: the collection is paged by KEYSET, so
   *  "the previous page" is not "this cursor minus a page" — it is the cursor
   *  somebody was standing on, and only the reader who walked there holds it. */
  const [cursor, setCursor] = useState<string | null>(null);
  const [walked, setWalked] = useState<Array<string | null>>([]);
  const [reading, setReading] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);

  const runs = records(evidence.runs);
  const timeline = records(evidence.timeline);
  const vocabulary = useMemo(
    () =>
      Array.isArray(evidence.step_vocabulary)
        ? evidence.step_vocabulary.filter((step): step is string => typeof step === "string")
        : [],
    [evidence.step_vocabulary],
  );
  const [selectedId, setSelectedId] = useState<string | null>(selectedRunId);
  // The caller can change which run it is asking for while this page is mounted
  // (two days opened in a row from the grid), and a `useState` initialiser only
  // reads its argument once.
  useEffect(() => {
    if (selectedRunId) setSelectedId(selectedRunId);
  }, [selectedRunId]);
  // WHICH RUNS ARE UNFOLDED. `null` means "nobody has decided yet", and the run
  // somebody arrived for — or, failing that, the most recent one — opens with
  // the tab: arriving from a day of the `Data` grid and being handed a folded
  // row is the second click this amendment set out to remove, not add.
  const [openIds, setOpenIds] = useState<Set<string> | null>(null);
  // AI-144: the coverage strip reads the ledger on mount and owns its own
  // refresh. Anything confirmed from THIS page changes what that ledger says,
  // so it is remounted rather than left showing the state before the operation.
  const [coverageEpoch, setCoverageEpoch] = useState(0);

  const read = useCallback(
    async (next: RunFilters, nextCursor: string | null) => {
      setReading(true);
      setFailure(null);
      try {
        const query = new URLSearchParams(asQuery(next, nextCursor)).toString();
        // `apiGet`, never a bare `fetch`: it is the one seam that attaches the
        // bearer, and it throws an `ApiError` carrying the SERVER'S sentence.
        const body = await apiGet<{ evidence?: Record<string, unknown> }>(
          `/api/projects/${encodeURIComponent(projectId)}/datastreams/${encodeURIComponent(datastreamId)}/workbench/runs${query ? `?${query}` : ""}`,
        );
        setEvidence((body?.evidence ?? {}) as Record<string, unknown>);
        setCursor(nextCursor);
        // A NARROWING RESETS THE UNFOLDING, never the other way round: the run
        // that was open belongs to the list that is gone.
        setOpenIds(null);
      } catch (error: unknown) {
        setFailure(
          error instanceof Error ? error.message : "This run history could not be read.",
        );
      } finally {
        setReading(false);
      }
    },
    [datastreamId, projectId],
  );

  /** Narrow: back to the first page, and the walked cursors are no longer a path. */
  const narrow = useCallback(
    (next: RunFilters) => {
      setFilters(next);
      setWalked([]);
      void read(next, null);
    },
    [read],
  );

  const nextCursor =
    typeof evidence.runs_next_cursor === "string" ? evidence.runs_next_cursor : null;
  const truncated = evidence.runs_truncated === true;
  const limit = typeof evidence.runs_limit === "number" ? evidence.runs_limit : null;
  const matching = typeof evidence.runs_matching === "number" ? evidence.runs_matching : null;
  const statesPresent = Array.isArray(evidence.run_states_present)
    ? evidence.run_states_present.filter((s): s is string => typeof s === "string")
    : undefined;
  const originsPresent = Array.isArray(evidence.run_origins_present)
    ? evidence.run_origins_present.filter((s): s is string => typeof s === "string")
    : [];

  const choices = useMemo(() => stateChoices(runs, statesPresent), [runs, statesPresent]);

  /**
   * THE ORDER ON THIS PAGE. Two columns sort — the two a person compares runs
   * by — and the note beside the pager says what a sort on a paged collection
   * can and cannot promise.
   */
  const { sort, toggleSort } = useTableSort(null);
  const sorted = useMemo(
    () =>
      sortRows(runs, sort, (run, key) => {
        if (key === "started") {
          const at = typeof run.started_at === "string" ? run.started_at : run.created_at;
          return typeof at === "string" ? at : null;
        }
        if (key === "duration") {
          // An unmeasured duration is ABSENT, not zero — `sortRows` keeps an
          // absence at the bottom whichever way the arrow points, which is why
          // `0` must never be substituted here.
          return typeof run.duration_seconds === "number" && run.duration_seconds > 0
            ? run.duration_seconds
            : null;
        }
        return null;
      }),
    [runs, sort],
  );

  const selected = sorted.find((run) => run.id === selectedId) ?? sorted[0] ?? null;

  const confirmed = () => {
    setCoverageEpoch((epoch) => epoch + 1);
    onConfirmed();
  };

  const isOpen = (runId: string) =>
    openIds === null ? runId === text(selected?.id) : openIds.has(runId);
  const toggle = (runId: string) =>
    setOpenIds((current) => {
      const next = new Set(current ?? (selected ? [text(selected.id)] : []));
      if (next.has(runId)) next.delete(runId);
      else next.add(runId);
      return next;
    });

  /** The page on screen, as a file. No route, and no promise of completeness. */
  const exportPage = () => {
    const blob = new Blob([runsCsv(sorted)], { type: "text/csv;charset=utf-8" });
    const href = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = href;
    anchor.download = runsExportName(
      datastreamId,
      { ...asQuery(filters, cursor) },
      sorted.length,
    );
    anchor.click();
    URL.revokeObjectURL(href);
  };

  const narrowed =
    filters.state !== ALL_RUNS ||
    filters.origin !== ALL_ORIGINS ||
    Boolean(filters.from || filters.to || filters.q.trim());

  return (
    <>
      {/* THE LIVE BAND IS NOT MOUNTED HERE — amendment 3 of the 2026-08-11
          review. It was, from story 63.5, and the route mounts it too, above
          `NavTabs`: one subject drawn twice on one Datastream, and on this tab
          the two sat a screen apart over the same poll. The route's mount is the
          one that survives: above the tabs it is read from all six, which is the
          reason 63.5 gave for putting it there. */}

      {/* AI-144: day-by-day coverage and its repair were BUILT and mounted
          nowhere — `CoverageStrip` was imported only by its own test, exactly
          the orphan-component defect AI-119 closed for the schedule. It leads
          this tab because "which days am I missing" comes before "what did run
          number seven do", and it stays visible when there are NO runs at all:
          that is precisely when every day reads `never_fetched`. */}
      <CoverageStrip
        key={coverageEpoch}
        projectId={projectId}
        datastreamId={datastreamId}
        connector={connector}
        sourceAccountRef={sourceAccountRef}
      />

      {/* NO PROPS: this panel no longer reaches the server and confirms nothing.
          It states what each of the three bounded verbs would do and where the
          gesture that exists lives — `Synchronize` and `Reload` are retired
          because the schedule and story 58.4's day re-collection deliver them,
          and `Reprocess` is the one the product intends and has not built.
          Handing it a project, a Datastream and a confirmation callback would
          say it still writes.

          FOLDED SINCE 2026-08-18: it says only what cannot be asked for, and it
          stood above the run list on the tab a person opens during an incident.
          The sentence is intact inside it; the history leads. */}
      <DatastreamReloadPanel />

      {failure && (
        <Panel>
          <EmptyState
            title="This run history could not be read"
            description={failure}
            action={
              <Button variant="secondary" onClick={() => void read(filters, cursor)}>
                Read it again
              </Button>
            }
          />
        </Panel>
      )}

      {runs.length === 0 && !narrowed ? (
        <Panel>
          <EmptyState
            title="No runs"
            description="No execution evidence exists for this Datastream. That is not a failure to read the history: nothing has run. The coverage above still reads the extract ledger, and a range can still be re-collected."
          />
        </Panel>
      ) : (
        <>
          <Panel className="grid gap-3">
            <ChoiceGroup
              aria-label="Filter runs by state"
              choices={choices}
              value={filters.state}
              onValueChange={(state) => narrow({ ...filters, state })}
              variant="pill"
            />
            {/* THE THREE NARROWINGS THE CHIPS COULD NOT EXPRESS. Somebody
                arrives at this tab holding a date (« it broke on the 12th »), a
                run id out of a ticket, or an error code out of an alert; none of
                those is a state, and before this row none of them could be
                asked. Each one is a QUERY on the collection, not a filter over
                the page — which is the whole reason they can be trusted. */}
            <div
              className="flex flex-wrap items-end gap-3"
              role="group"
              aria-label="Narrow the run history"
            >
              <label className="grid gap-1 text-caption text-text-secondary">
                <span>From</span>
                <Input
                  type="date"
                  value={filters.from}
                  data-testid="runs-from"
                  onChange={(event) => narrow({ ...filters, from: event.target.value })}
                />
              </label>
              <label className="grid gap-1 text-caption text-text-secondary">
                <span>To</span>
                <Input
                  type="date"
                  value={filters.to}
                  data-testid="runs-to"
                  onChange={(event) => narrow({ ...filters, to: event.target.value })}
                />
              </label>
              {/* ONLY THE ORIGINS THIS DATASTREAM'S RUNS CARRY. The registry
                  knows eleven; offering the nine this flux has never produced
                  would be nine choices that empty the screen — the same rule the
                  state chips have followed since 58.10. A run minted before the
                  origin was stamped carries none, and no selector entry can
                  reach it: `Not measured` is an absence, not a value. */}
              {originsPresent.length > 0 && (
                <label className="grid gap-1 text-caption text-text-secondary">
                  <span>Origin</span>
                  <NativeSelect
                    value={filters.origin}
                    data-testid="runs-origin"
                    onChange={(event) => narrow({ ...filters, origin: event.target.value })}
                  >
                    <option value={ALL_ORIGINS}>Any origin</option>
                    {originsPresent.map((key) => (
                      <option key={key} value={key}>
                        {originLabel(key) ?? key}
                      </option>
                    ))}
                  </NativeSelect>
                </label>
              )}
              <label className="grid gap-1 text-caption text-text-secondary">
                <span>Search</span>
                <CommittedSearch
                  value={filters.q}
                  onCommit={(q) => narrow({ ...filters, q })}
                />
              </label>
              {narrowed && (
                <Button
                  variant="secondary"
                  size="sm"
                  data-testid="runs-clear-filters"
                  onClick={() => narrow(NO_FILTERS)}
                >
                  Clear narrowing
                </Button>
              )}
              <Button
                variant="secondary"
                size="sm"
                disabled={sorted.length === 0}
                data-testid="runs-export"
                title="Exactly the rows on this page, with the columns above them. The rest of the history is paged by the server and is not asked for here."
                onClick={exportPage}
              >
                {`Export this page (${sorted.length} run${sorted.length === 1 ? "" : "s"})`}
              </Button>
            </div>
            {/* The cap is said, because a list silently stopping at 200 reads as
                "this Datastream has run 200 times" — a different, false claim.
                Since 2026-08-18 it says the DENOMINATOR too, which is what turns
                a cap into a position: `200 of 428` is a measurement somebody can
                act on, `the 200 most recent` was one they could only distrust. */}
            <p className="m-0 text-caption text-text-secondary" data-testid="runs-count">
              {matching !== null
                ? `${formatNumber(runs.length)} of ${formatNumber(matching)} run(s)${narrowed ? " matching this narrowing" : " on this Datastream"}.`
                : truncated
                  ? `Showing the ${limit ?? runs.length} most recent runs; this Datastream has more.`
                  : `${formatNumber(runs.length)} run(s) on this Datastream.`}
            </p>
            <WhyMeasurementsAreAbsent runs={sorted} />
            {/* Story 59.1, arbitrage 7: at the grain of the FLUX, and ONCE. An
                evaluation that could name no run belongs to no run block, and
                repeating it under each would attribute it to collections that
                never produced it. */}
            <EvaluationsWithoutRun count={evidence.evaluations_without_run} />
          </Panel>

          {sorted.length === 0 ? (
            <Panel>
              <EmptyState
                title="No run matches this narrowing"
                description="The other runs of this Datastream are still there — clear the narrowing to read them. Nothing has been removed."
                action={
                  <Button variant="secondary" onClick={() => narrow(NO_FILTERS)}>
                    Clear narrowing
                  </Button>
                }
              />
            </Panel>
          ) : (
            <Panel flush>
              <Table>
                <RunTableHeader sort={sort} onSort={toggleSort} />
                {sorted.map((run) => {
                  const runId = text(run.id);
                  return (
                    <RunRow
                      key={runId}
                      run={run}
                      vocabulary={vocabulary}
                      timeline={timeline}
                      open={isOpen(runId)}
                      selected={run.id === selected?.id}
                      projectId={projectId}
                      datastreamId={datastreamId}
                      onSelect={() => setSelectedId(runId)}
                      onToggle={() => toggle(runId)}
                      onConfirmed={confirmed}
                      onNavigateTab={onNavigateTab}
                    />
                  );
                })}
              </Table>
              <div className="px-4.5 py-3">
                <Pager
                  unit="page"
                  label="Run history pages"
                  busy={reading}
                  // `First` and `Previous` are OFFERED and dead on page one
                  // rather than absent: the way back exists on this collection
                  // (the cursors are held above), and hiding it would say it
                  // does not — the distinction `PagerStep` was built for.
                  onFirst={walked.length > 0 ? () => { setWalked([]); void read(filters, null); } : null}
                  onPrevious={
                    walked.length > 0
                      ? () => {
                          const previous = walked[walked.length - 1] ?? null;
                          setWalked((stack) => stack.slice(0, -1));
                          void read(filters, previous);
                        }
                      : null
                  }
                  onNext={
                    nextCursor
                      ? () => {
                          setWalked((stack) => [...stack, cursor]);
                          void read(filters, nextCursor);
                        }
                      : null
                  }
                  data-testid="runs-pager"
                >
                  <SortScopeNote />
                </Pager>
              </div>
            </Panel>
          )}
        </>
      )}
    </>
  );
}
