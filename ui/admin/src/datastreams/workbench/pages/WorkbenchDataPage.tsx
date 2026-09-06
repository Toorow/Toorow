import { useEffect, useMemo, useState } from "react";
import {
  Button, CopyButton, EmptyState, EvidenceRows, Input, ObjectId, Panel, PanelBody,
  PanelHeader, SortableHead, Status,
  Table, TableBody, TableCell, TableHeader, TableRow, TableScroll,
  Timestamp, sortRows, useTableSort, type SortValue,
} from "../../../ui";
import { filledRecord, numberText, record, records, text, titleCase } from "../evidence";
import type { WorkbenchTabPayload } from "../workbenchTypes";
import DatastreamSample, { type SampleStage } from "../DatastreamSample";
import DatastreamExportDialog from "../DatastreamExportDialog";
import DatastreamDailyBreakdown from "../DatastreamDailyBreakdown";
import type { DimensionHistory } from "../dimensionDebt";
import DatastreamLandedFile from "../DatastreamLandedFile";
import WorkbenchMediaPlanPanel from "../WorkbenchMediaPlanPanel";
import {
  fetchMatches,
  type DatastreamMatch,
} from "../../../analyze/explorer/explorerClient";

const STAGES = ["collected", "mapped", "processed", "published"] as const;

function DatastreamMatchOpportunities({
  projectId,
  datastreamId,
  onOpenAnalytics,
}: {
  projectId: string;
  datastreamId: string;
  onOpenAnalytics?: (match: DatastreamMatch) => void;
}) {
  const [matches, setMatches] = useState<DatastreamMatch[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    void fetchMatches(projectId)
      .then((catalog) => {
        if (!live) return;
        setMatches(catalog.matches.filter((match) => (
          match.left.datastream_id === datastreamId || match.right.datastream_id === datastreamId
        )));
        setError(null);
      })
      .catch((reason: unknown) => {
        if (!live) return;
        setMatches([]);
        setError(
          reason instanceof Error ? reason.message : "Matching opportunities are unavailable.",
        );
      });
    return () => { live = false; };
  }, [datastreamId, projectId]);

  const openDatastreamsInAnalytics = (match: DatastreamMatch) => {
    if (!match.common_key || !match.explore_together) return;
    onOpenAnalytics?.(match);
  };

  return (
    <Panel flush data-testid="datastream-match-opportunities">
      <PanelHeader
        title="Datastreams you can cross"
        description="Governed opportunities that use this Datastream. Each names the common MDM key and exact version Analytics will revalidate."
      />
      <PanelBody className="flex flex-col gap-3">
        {matches === null && (
          <p role="status" className="m-0 text-body text-text-secondary">
            Finding governed matches…
          </p>
        )}
        {error && <Status as="block" tone="warning">{error} No absence is inferred.</Status>}
        {matches?.length === 0 && !error && (
          <EmptyState
            title="No governed cross includes this Datastream"
            description="A cross needs three things in order: a shared canonical dimension mapped on this Datastream, its common key declared in MDM, and that key pinned on a Semantic View relationship."
          />
        )}
        {matches?.map((match) => {
          const other = match.left.datastream_id === datastreamId ? match.right : match.left;
          return (
            <div
              key={`${match.left.datastream_id}:${match.right.datastream_id}:${match.common_key?.version_id ?? match.kind}`}
              className="grid gap-2 rounded-lg border border-divider-base p-4 md:grid-cols-[1fr_auto] md:items-center"
            >
              <div>
                <p className="m-0 font-semibold text-text">{other.name}</p>
                <p className="m-0 text-caption text-text-secondary">
                  {match.common_key
                    ? `${match.common_key.name} · v${match.common_key.version_number} · ${match.relationship?.cardinality ?? "cardinality unavailable"}`
                    : match.next_action ?? "A governed common key is still required."}
                </p>
                <p className="m-0 text-caption text-text-secondary">{match.analysis}</p>
              </div>
              {match.common_key && match.explore_together ? (
                <Button
                  type="button"
                  disabled={!onOpenAnalytics}
                  onClick={() => openDatastreamsInAnalytics(match)}
                >
                  Open in Analytics
                </Button>
              ) : <Status tone="warning">Governance required</Status>}
            </div>
          );
        })}
      </PanelBody>
    </Panel>
  );
}

/** `date, campaign_id` — the grain reads as the columns it names. AC4 asks for
 *  "the resulting grain", which `JSON.stringify` does not give. */
function grainText(value: unknown): string {
  if (Array.isArray(value)) {
    return value.length === 0 ? "Not established" : value.map((entry) => String(entry)).join(", ");
  }
  const asRecord = record(value);
  const nested = asRecord?.grain ?? asRecord?.joint_grain;
  if (Array.isArray(nested)) return nested.map((entry) => String(entry)).join(", ");
  return "Unavailable";
}

export default function WorkbenchDataPage({
  payload, projectId, datastreamId, mode, sourceAccountRef, onOpenRun, onOpenAnalytics,
}: {
  payload: WorkbenchTabPayload;
  projectId: string;
  datastreamId: string;
  /** `app.datastreams.source_kind`. The only field that carries `managed_feed`,
   *  and therefore the only one a mode guard may read — amendment 7 of the
   *  2026-08-11 review. */
  mode?: string | null;
  /** The provider account a row's re-collection spends on — story 58.4. It is
   *  the header's, handed down by the route: this tab's evidence carries no
   *  account, and a confirmation naming none would let somebody spend on a
   *  connection they never checked. */
  sourceAccountRef?: string | null;
  /** Opening a day opens the run that collected it — story 58.2, arbitrage 7.
   *
   *  THE ID TRAVELS. An anomaly lives in its run and never in a floating window
   *  (amendment « une anomalie se déplie dans son run » of the ratified target),
   *  and a handler that took the person to the `Runs` tab without saying WHICH
   *  run left them a 60-day list to search — the gesture in appearance only. */
  onOpenRun?: (executionId: string) => void;
  onOpenAnalytics?: (match: DatastreamMatch) => void;
}) {
  const sampleState = text(payload.evidence.sample_state, "unavailable");
  /** The two modes that RECEIVE their rows instead of pulling them.
   *
   *  A mode guard reads the mode — amendment 7 of the 2026-08-11 review, whose
   *  `Incomplete if` is « or a mode guard tests anything other than
   *  `identity.mode` ». It is not derived from `sample_state` even though the
   *  two coincide today: `sample_state` answers a question about a sample, and a
   *  block that gates an EXPORT on it would be reading someone else's verdict.
   *
   *  Both modes carry `module_name = NULL` by construction — `create_datastream`
   *  (`server/core/datastreams.py`) sets it to None for `external_bq` and
   *  `managed_feed`, and refuses a `connector_pull` without one. */
  const pushedSource = mode === "managed_feed" || mode === "external_bq";
  const allStages = records(payload.evidence.stages);
  const availability = record(payload.evidence.availability) ?? {};
  /** WHY a stage is not selectable — story 58.3, arbitrage 8.
   *
   *  A CLASS FIX, not a fix to this screen: three of the four positions are
   *  greyed on every flux of the product, because `availability` reads the stage
   *  evidence table and that table holds `published` alone (139 rows measured,
   *  0 for the other three). A disabled control with no reason is unreadable, and
   *  the reason belongs to the server that computed the verdict — a sentence
   *  written here would be a second answer nobody could check. */
  const availabilityReason = record(payload.evidence.availability_reason) ?? {};
  /** The Stage Selector the target asks for — `Collected → Mapped → Processed →
   *  Published` as a CHOICE, not four read-only badges over a table that mixes
   *  every stage of every execution together. With a handful of runs that table
   *  is unreadable, and "show me what Published actually holds" was not a
   *  question this tab could answer.
   *
   *  Each cell also carries the latest row count for its stage, because the
   *  progression is the reading that matters: 10,000 collected → 9,800 published
   *  says where the rows went, and no single stage says it alone. The numbers
   *  come from the stage rows already on the wire; nothing is recomputed.
   *
   *  DRAWN FOR A PUSHED SOURCE TOO, AND THAT IS A JUDGEMENT. Amendment 7 of the
   *  2026-08-11 review removed from a `managed_feed` what is CONNECTOR
   *  vocabulary — a report profile, a relation of the connector, a day-by-day
   *  pull axis. `Collected → Mapped → Processed → Published` is none of those:
   *  they are the phases of an EXECUTION, they are read from
   *  `app.datastream_execution_stage_evidence`, which is keyed on an execution
   *  and never on a connector, and a pushed source has executions — the flux the
   *  review was written against had one, stuck at `created`. Nothing in this
   *  block or in the evidence table below names a provider, a profile or a pull.
   *
   *  Their emptiness is the same on every mode and is not a reason to remove
   *  them from one: the ratified `Data` cell promises the stage selector, that
   *  promise was erased once and restored in the commit of 58.1, and each
   *  position already carries the server's own sentence saying why it is greyed.
   *  Removing them here would leave a file source with no reading at all of its
   *  own executions, which is the opposite of what amendment 7 asked for. */
  const [stageFilter, setStageFilter] = useState<string | null>(null);
  const stages = stageFilter
    ? allStages.filter((stage) => text(stage.stage) === stageFilter)
    : allStages;
  const latestRowCount = (stage: string): string => {
    const rows = allStages.filter((item) => text(item.stage) === stage);
    return rows.length ? numberText(rows[0].row_count) : "—";
  };
  // A stage's profile and coverage are records, not counts. Selecting one stage
  // rather than expanding every row keeps the table scannable and the evidence
  // complete: "7 coverage signals" was a number that carried nothing.
  const [openStageId, setOpenStageId] = useState<string | null>(null);
  const openStage = stages.find((stage) => text(stage.id) === openStageId) ?? null;
  /** ONE READING, NOT TWO — finding D-6 of the visual review #69.
   *
   *  The four cards and the table below them stated the same row count, for the
   *  same four stages of the same execution, four hundred pixels apart, and
   *  neither said it was the summary of the other. The ratified `Data` cell now
   *  fixes the relation: the cards are the ENTRY, the table is their UNFOLDING.
   *
   *  NOTHING IS TAKEN AWAY FOR IT, and that is a criterion of this surface, not
   *  a preference: « the stage selector or the stage evidence is withdrawn from
   *  a pushed source » is an `Incomplete if`. The seven columns, the stage
   *  filter and the profile/coverage panel a row opens are unchanged — the
   *  control below names, before the click, every fact that is folded, so an
   *  operator never has to guess whether the versions or the schema hash are
   *  still somewhere. */
  const [stagesOpen, setStagesOpen] = useState(false);
  /**
   * FINDING THE RECORD, RATHER THAN READING EVERY ROW — amendment of 2026-08-18.
   *
   * This table is where a person arrives holding an id: a run number from a
   * ticket, a schema hash from a diff, a mapping version from `Mapping`. It had
   * no order but the server's and no way to ask for one line, so the gesture was
   * to read every row — which is exactly what `SortableHead` exists to end
   * (`aria-sort` appeared zero times in this console before it).
   *
   * EVERY RECORD IS HERE, so the sort is honest without a caveat. `PAGE_SORT_NOTE`
   * and `SortScopeNote` are the sentence for a collection read through a cursor,
   * where sorting a page can make a reader conclude the smallest value on screen
   * is the smallest in the collection. This table is not one: the stage evidence
   * arrives whole on the tab payload, so the ordering applies to all of it, and
   * the sentence below says THAT instead of borrowing a warning that is not true
   * here.
   */
  const { sort, toggleSort } = useTableSort();
  const [stageQuery, setStageQuery] = useState("");

  /** What a row is sorted by, per column key. Anything else must reduce to one. */
  const stageValue = (row: Record<string, unknown>, key: string): SortValue => {
    switch (key) {
      case "stage": return text(row.stage, "");
      case "execution": return text(row.execution_id, "");
      case "plan": return text(row.plan_version_id, "");
      case "schema": return text(row.schema_hash, "");
      case "rows": return typeof row.row_count === "number" ? row.row_count : null;
      case "grain": return grainText(row.grain_evidence);
      case "observed": return text(row.occurred_at, "");
      default: return null;
    }
  };

  /** Every word a person might arrive holding, in one haystack per row. */
  const stageHaystack = (row: Record<string, unknown>): string =>
    [
      row.stage, row.phase_state, row.execution_id, row.plan_version_id,
      row.mapping_version_id, row.schema_hash, row.artifact_ref,
    ]
      .map((value) => text(value, ""))
      .concat(grainText(row.grain_evidence))
      .join(" ")
      .toLowerCase();

  const query = stageQuery.trim().toLowerCase();
  const matched = useMemo(
    () => (query ? stages.filter((row) => stageHaystack(row).includes(query)) : stages),
    // `stages` is derived from the payload on every render; the search and the
    // stage filter are what actually change it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [payload.evidence.stages, stageFilter, query],
  );
  const orderedStages = useMemo(
    () => sortRows(matched, sort, stageValue),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [matched, sort],
  );

  return (
    <>
      {/* THE DATE IS THE AXIS, AND IT COMES FIRST — story 58.2, arbitrage 12.
          None of the questions an operator arrives with (which day is missing,
          which came back empty, which was re-pulled) could be asked of the stage
          table below: not one of its seven columns is a date.

          NOTHING IS REMOVED FOR IT. The stage selector, its evidence table and
          the bounded masked sample stay, unchanged, underneath: the ratified
          `Data` cell still promises "schema catalog, types, classifications,
          profile/coverage, resulting grain … and bounded masked samples", and
          that promise was erased once already and restored in the commit of
          58.1. Deleting it here would be repeating the fault a week later. */}
      <DatastreamDailyBreakdown
        projectId={projectId}
        datastreamId={datastreamId}
        mode={mode}
        sourceAccountRef={sourceAccountRef}
        onOpenRun={onOpenRun}
        /* WHY THE HOLLOW IS EXPLAINED ON THIS TAB — amendment 14 of the
           2026-08-11 review, delivered 2026-08-31.

           The amendment was built on `Processing`, where the change is COMPOSED,
           and the reading surfaces said nothing at all: a dimension added last
           month drew empty over every older day with no sentence saying that was
           why. « Un trou expliqué est une donnée ; un trou muet est un bug que
           l'opérateur impute au produit. »

           SERVED, NOT MEASURED HERE. `datastream_workbench.py` publishes the
           same `dimension_history` block on this tab that `Processing` reads, so
           the two tabs cannot disagree about which day carries what. This page
           only hands it down. */
        dimensionHistory={(record(payload.evidence.dimension_history) ?? null) as DimensionHistory | null}
      />

      {/* AFTER the day grid, and story 66.2 does not override arbitrage 12. The
          cross opportunity is what this Datastream lets somebody do NEXT; the
          person opening `Data` came to read the days. Placed first, it pushed
          the axis of this tab below the fold on the very screen 58.2 exists to
          make date-first. */}
      <DatastreamMatchOpportunities
        projectId={projectId}
        datastreamId={datastreamId}
        onOpenAnalytics={onOpenAnalytics}
      />

      {/* WHERE THE DAY GRID USED TO BE — lot B1, amendment 6 of the 2026-08-11
          review. Amendment 7 took the connector pull axis away from a pushed
          source, and rightly: a `managed_feed` has no report profile, no
          connector relation and no collection window. But nothing was put in its
          place, so 4 of the 6 live Datastreams — all four file sources — had no
          reading of their own data on this tab at all.

          The fields of the active mapping stay exactly where they are: they come
          from the component above, which keeps drawing them for every mode.
          What is added here is the other half of the question — not "which
          columns does the mapping declare" but "what did the last file actually
          contain".

          `managed_feed` ALONE, and not `pushedSource`. An `external_bq` source is
          pushed too, but it names an external relation rather than receiving a
          file: it has no arrival, no import ledger and its own vocabulary panel
          on `Mapping`. Widening the guard would offer it a file that can never
          arrive. */}
      {mode === "managed_feed" && (
        <DatastreamLandedFile projectId={projectId} datastreamId={datastreamId} />
      )}

      {/* WHAT THE FILES OF THIS DATASTREAM BECOME, when they become a media plan
          — ratified 2026-08-24 (`analyze-and-test.md`, « the media plan is
          created and imported in the carrier Datastream's Workbench »).

          BESIDE THE LANDED FILE AND NOT ELSEWHERE, because a plan's versions ARE
          this Datastream's dated imports: the panel above says which file
          arrived, this one says which dated version of the plan it became.

          THE GUARD IS THE SERVER'S VERDICT, not a mode test. `mode ===
          "managed_feed"` is necessary and nowhere near sufficient — most file
          sources land warehouse rows — so the panel renders only when
          `evidence.media_plan` exists, which the server writes from the
          Template's own `landing_target`. Reading the mode alone would offer a
          plan gesture on every file source in the product. */}
      <WorkbenchMediaPlanPanel
        projectId={projectId}
        datastreamId={datastreamId}
        mediaPlan={record(payload.evidence.media_plan) ?? null}
      />

      {/* The export, which `SCREEN-FUNCTION-MATRIX.md` Lot 1 names as half of
          this tab's primary action ("load or export the selected sample") and
          which had no control anywhere in the console. It sits above the stage
          selector because it acts on the WHOLE Datastream over a chosen period,
          not on the stage evidence below.

          KEPT FOR A PULL SOURCE, WITHDRAWN FROM A PUSHED ONE — and the second
          half is a measurement, not a preference. The export reads
          `fact_daily_kpi` through `_resolve_datastream_mart`
          (`server/core/admin_api.py`), which keys it on `module_name`;
          `export_columns_for` returns `([], [])` for an empty connector and
          `read_datastream_export` raises `no_materialization`
          (`server/core/cache_warehouse.py`). On a pushed source, whose
          `module_name` is NULL by construction, this control therefore opens a
          dialog with no column to choose and a button that always refuses —
          after asking for a period. A dead click is worse than an absence.

          THE TAB DOES NOT GET QUIETER FOR IT. The block that replaces the
          bounded sample below says, in the server's own sentence, that neither a
          sample nor an export is keyed to this Datastream and where its rows are
          read instead. One absence, one explanation, one place. */}
      {!pushedSource && (
        <div className="flex items-center justify-between gap-3">
          <p className="m-0 text-ui text-text-secondary">
            Read this Datastream's published rows, or take them away as a bounded CSV.
          </p>
          <DatastreamExportDialog
            projectId={projectId}
            datastreamId={datastreamId}
            stage={(stageFilter ?? "published") as SampleStage}
          />
        </div>
      )}

      <Panel flush>
        <PanelHeader
          title="Execution-scoped data stages"
          description="Pick a stage to read what it holds. Each count is the rows the latest execution left at that stage; stages are never aliased."
        />
        <div
          role="radiogroup"
          aria-label="Data stage"
          className="grid grid-cols-4 divide-x divide-divider-base max-lg:grid-cols-2"
        >
          {STAGES.map((stage) => {
            const available = availability[stage] === "available";
            const selected = stageFilter === stage;
            const reason = text(availabilityReason[stage], "");
            return (
              <button
                type="button"
                role="radio"
                aria-checked={selected}
                // An unavailable stage is not selectable: filtering to a stage with
                // no evidence would show an empty table, which reads as "no data"
                // rather than "this stage never ran".
                disabled={!available}
                key={stage}
                title={reason || undefined}
                onClick={() => setStageFilter(selected ? null : stage)}
                className={`p-5 text-left transition-colors disabled:cursor-not-allowed ${selected ? "bg-primary-container" : "hover:bg-background-light"}`}
              >
                <span className="mb-2 block text-label text-text-secondary">{titleCase(stage)}</span>
                <Status tone={available ? "success" : "neutral"}>
                  {available ? "Execution-bound" : "Unavailable"}
                </Status>
                {available ? (
                  <span className="mt-2 block font-numeric text-caption text-text-secondary">
                    {latestRowCount(stage)} rows
                  </span>
                ) : (
                  // The server's sentence, rendered whole. « Unavailable » on its
                  // own is the silence this story refuses.
                  <span className="mt-2 block text-caption text-text-secondary">{reason}</span>
                )}
              </button>
            );
          })}
        </div>

        {stageFilter && (
          <div className="border-t border-divider-base px-5 py-4">
            <Status as="block" tone="neutral" title={`Showing ${titleCase(stageFilter)} only`} action={
              <button type="button" className="text-primary underline" onClick={() => setStageFilter(null)}>
                Show every stage
              </button>
            }>
              The other stages are hidden, not absent — their evidence is unchanged.
            </Status>
          </div>
        )}

        {stages.length === 0 ? (
          <EmptyState
            title="Stage evidence unavailable"
            description="No collected, mapped, processed or published evidence exists for an exact execution, so there is nothing to unfold."
          />
        ) : (
          <>
            {/* THE CONTROL NAMES WHAT IS FOLDED, BEFORE THE CLICK. A disclosure
                that says only « show details » makes the person open it to find
                out whether the evidence they need is still in the product. The
                counts above are a summary of exactly these records, and this
                line is the one place that says so. */}
            <div className="border-t border-divider-base px-5 py-4">
              <button
                type="button"
                aria-expanded={stagesOpen}
                onClick={() => {
                  if (stagesOpen) setOpenStageId(null);
                  setStagesOpen(!stagesOpen);
                }}
                className="text-primary underline"
              >
                {stagesOpen
                  ? "Hide the executions behind these counts"
                  : "Read the executions behind these counts"}
              </button>
              <p className="m-0 mt-1 text-caption text-text-secondary">
                {stages.length === 1 ? "1 execution record" : `${stages.length} execution records`}
                {" — plan and mapping version, schema hash, resulting grain, row count and"}
                {" the instant each stage was observed. Opening one reads its profile and coverage."}
              </p>
            </div>
            {stagesOpen && (
              <>
                <div className="flex flex-wrap items-center gap-3 border-t border-divider-base px-5 py-3">
                  <Input
                    aria-label="Search the execution records"
                    placeholder="Search a run, a version, a schema hash"
                    value={stageQuery}
                    onChange={(event) => setStageQuery(event.target.value)}
                    className="max-w-sm"
                  />
                  <p className="m-0 text-caption text-text-secondary">
                    {query
                      ? `${orderedStages.length} of ${stages.length} records match “${stageQuery.trim()}”.`
                      : "Every execution record this tab was given is in this table, so"
                        + " the search and the order apply to all of them."}
                  </p>
                </div>
                {orderedStages.length === 0 ? (
                  <EmptyState
                    title="No execution record matches that"
                    description="Nothing is hidden beyond this table — clear the search to read every record again."
                  />
                ) : (
                <TableScroll label="Datastream stage evidence">
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <SortableHead sortKey="stage" sort={sort} onSort={toggleSort}>Stage</SortableHead>
                        <SortableHead sortKey="execution" sort={sort} onSort={toggleSort}>Execution</SortableHead>
                        <SortableHead sortKey="plan" sort={sort} onSort={toggleSort}>Versions</SortableHead>
                        <SortableHead sortKey="schema" sort={sort} onSort={toggleSort}>Schema</SortableHead>
                        <SortableHead sortKey="rows" sort={sort} onSort={toggleSort} numeric>Rows</SortableHead>
                        <SortableHead sortKey="grain" sort={sort} onSort={toggleSort}>Grain</SortableHead>
                        <SortableHead sortKey="observed" sort={sort} onSort={toggleSort}>Observed</SortableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {orderedStages.map((stage) => {
                        const id = text(stage.id);
                        const selected = id === openStageId;
                        return (
                          <TableRow
                            key={id}
                            onClick={() => setOpenStageId(selected ? null : id)}
                            aria-selected={selected}
                            className="cursor-pointer"
                          >
                            <TableCell>
                              <strong>{titleCase(stage.stage)}</strong>
                              <span className="block text-caption text-text-secondary">
                                {titleCase(stage.phase_state)}
                              </span>
                            </TableCell>
                            {/* AN ID IS AN OBJECT, NOT A STRING OF LETTERS. These
                                cells printed a bare ULID at full width: nothing
                                said which kind it was, nothing truncated it from
                                the side that carries the kind, and taking it to
                                another screen meant selecting 26 characters by
                                hand inside a row that opens a panel when clicked.
                                `ObjectId` keeps the full value on the element and
                                `CopyButton` owns the refusal a raw
                                `clipboard.writeText` swallowed. */}
                            <TableCell>
                              <ObjectId value={text(stage.execution_id, "")} title="Execution" />
                              <div
                                className="mt-1"
                                // The row opens a panel; copying an id is not
                                // asking for that panel.
                                onClick={(event) => event.stopPropagation()}
                              >
                                <CopyButton
                                  value={text(stage.execution_id, "")}
                                  label="Copy execution id"
                                  variant="ghost"
                                  size="xs"
                                  disabled={text(stage.execution_id, "") === ""}
                                />
                              </div>
                            </TableCell>
                            <TableCell>
                              <ObjectId value={text(stage.plan_version_id, "")} title="Plan version" />
                              <ObjectId value={text(stage.mapping_version_id, "")} title="Mapping version" />
                            </TableCell>
                            <TableCell>
                              <ObjectId value={text(stage.schema_hash, "")} title="Schema hash" />
                            </TableCell>
                            <TableCell numeric>{numberText(stage.row_count)}</TableCell>
                            <TableCell className="text-caption">{grainText(stage.grain_evidence)}</TableCell>
                            <TableCell>
                              <Timestamp
                                value={text(stage.occurred_at, "")}
                                absentMeaning="This stage recorded no instant"
                              />
                            </TableCell>
                          </TableRow>
                        );
                      })}
                    </TableBody>
                  </Table>
                </TableScroll>
                )}
              </>
            )}
          </>
        )}
      </Panel>

      {stagesOpen && openStage && (
        <Panel flush>
          <PanelHeader
            title={`${titleCase(openStage.stage)} profile and coverage`}
            description={`Execution ${text(openStage.execution_id)} · artifact ${text(openStage.artifact_ref)}`}
          />
          {/* THE IDS OF THE OPENED RECORD, TAKEABLE. The table names them and
              truncates them; this is where there is room to say what each one is
              before it is copied, so nobody pastes a mapping version into a field
              that wanted a plan version. */}
          <div className="flex flex-wrap gap-2 border-t border-divider-base px-5 py-3">
            {([
              ["Copy execution id", openStage.execution_id],
              ["Copy plan version id", openStage.plan_version_id],
              ["Copy mapping version id", openStage.mapping_version_id],
              ["Copy schema hash", openStage.schema_hash],
              ["Copy artifact reference", openStage.artifact_ref],
            ] as const)
              .filter(([, value]) => text(value, "") !== "")
              .map(([label, value]) => (
                <CopyButton key={label} value={text(value, "")} label={label} variant="ghost" size="xs" />
              ))}
          </div>
          <div className="grid gap-5 p-5 lg:grid-cols-2">
            <section>
              <h3 className="mb-2 text-label text-text-secondary">Safe profile</h3>
              {filledRecord(openStage.profile_evidence) ? (
                <EvidenceRows source={filledRecord(openStage.profile_evidence)!} label="Stage profile evidence" />
              ) : (
                <Status as="block" tone="warning">No safe profile was persisted for this stage.</Status>
              )}
            </section>
            <section>
              <h3 className="mb-2 text-label text-text-secondary">Coverage</h3>
              {filledRecord(openStage.coverage_evidence) ? (
                <EvidenceRows source={filledRecord(openStage.coverage_evidence)!} label="Stage coverage evidence" />
              ) : (
                <Status as="block" tone="warning">No coverage evidence was persisted for this stage.</Status>
              )}
            </section>
          </div>
          {text(openStage.safe_error, "") !== "" && (
            <div className="px-5 pb-5">
              <Status as="block" tone="error" title="Stage error">{text(openStage.safe_error)}</Status>
            </div>
          )}
        </Panel>
      )}

      {/* The bounded masked sample the target names (`:75`). This used to be a
          warning and nothing else, because the server answered `"unavailable"`
          unconditionally — so the tab's whole reason to exist rendered as an
          apology. The sample is now read from its own endpoint, which owns every
          refusal underneath it.

          WHEN THERE IS NO SAMPLE, IT COSTS ONE SENTENCE — finding D-7 of the
          visual review #69, ratified in the `Data` cell. What stood here was a
          titled block whose title restated its body, over a server sentence that
          named story 47.5 and the mart it was keyed by: a whole panel, in the
          repository's own vocabulary, to announce an absence. The sentence is
          still the SERVER's — it is what computed the verdict, and a phrase held
          by the console would pass its test against a server that never changed
          — but it is now the only thing rendered, and it says two things: what
          cannot be done here, and where the reading that DOES exist is found.

          A PUSHED SOURCE IS NOT WARNED, IT IS TOLD, and that is the one thing
          the console still decides. For that mode the refusal is a property of
          the mode: nothing to repair, nothing to wait for, so `warning` is the
          wrong register and `neutral` is the right one. */}
      {sampleState === "reachable" ? (
        <DatastreamSample
          projectId={projectId}
          datastreamId={datastreamId}
          stage={(stageFilter ?? "published") as SampleStage}
          watermark={text(payload.evidence.sample_watermark, "") || null}
          /* The EXECUTION of the opened stage, never the stage evidence row's
             own id — the two are different keys, and the sample route is keyed
             on the run. What stood here passed `openStageId`, which is
             `stage.id`, so opening a row addressed a run that does not exist. */
          executionId={text(openStage?.execution_id ?? stages[0]?.execution_id, "")}
        />
      ) : (
        <Status as="block" tone={pushedSource ? "neutral" : "warning"}>
          {text(payload.evidence.sample_reason, "No sample can be drawn on this tab.")}
        </Status>
      )}
    </>
  );
}
