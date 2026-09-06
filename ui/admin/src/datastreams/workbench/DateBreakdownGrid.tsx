/**
 * The Datastream read BY DAY — story 58.2.
 *
 * THE FIELDS ARE NOT THE COLUMNS OF THE DAY GRID, AND THAT IS STILL TRUE. The
 * header of `source field / canonical field` pairs comes from the active mapping;
 * the day rows come from the extract registry and the mart.
 * `datastream_mappings.target_field` has no key to `fact_daily_kpi.metric`, which
 * is a literal typed into the dbt model, so the route publishes
 * `column_row_join_available: false` and this screen SHOWS that non-join instead
 * of drawing a column per mapped field over rows keyed by nothing of the sort.
 *
 * WHAT THAT FLAG NEVER MEANT — amendment 12 of the 2026-08-11 review, lot B2. It
 * is a statement about the MART. It was read as a statement about the READING, and
 * that is how the epic's central promise — « la valeur brute ET sa valeur mappée »
 * on one row — was replaced by a paragraph explaining why it could not be kept. The
 * key between the collected reading and the mapped reading is the active mapping's
 * own `source → target`, the server lays it (`collected_mapped_pairing`), and
 * `DayReadingPanel` renders it. Nothing on this file changed for it except this
 * paragraph and the panel it delegates to.
 *
 * THE COUNTRY BLOCK EXISTS ONLY WHEN THE PROJECT SAYS SO — story 58.5. The state
 * comes from `app.project_capabilities` and travels on the SAME payload as the
 * days it qualifies; nothing here derives it, and nothing here holds a column
 * open. `datastream-workbench-and-wizard.md`, amendment 2 « Une capacité activée
 * AJOUTE son onglet » — off, a capability appears "ni onglet, ni panneau, ni
 * colonne", and that is what `country.active === false` renders: no header, no
 * width, no word. Only `ready` and `degraded` show it; `degraded` shows it AND
 * says it is degraded, because hiding rows already collected is worse than
 * showing them diminished.
 *
 * NO COST COLUMN STILL, and no space held for one: that is story 58.6. Story 58.7
 * adds no column either — it draws a LINE under a cell the server designated as an
 * amount, in the day reading and nowhere near the day grid, and a column the server
 * did not designate renders exactly as it did before.
 *
 * (Cited by NAME. That amendment was `…md:882-887` for one afternoon: the same
 * commit inserted seven lines above it and the numbers moved. A line number in a
 * living document is a citation that will mislead the next reader.)
 *
 * AND THE SPLIT IS NOT A DECOMPOSITION OF THE VOLUME BESIDE IT. `fact_daily_kpi`
 * is long-form and carries several PARALLEL series per day — `country`,
 * `country>device`, `device_category`, `user_type` — each of which independently
 * totals the day. `Rows at the mart` counts them all; the country unfolding counts
 * one of them. They are two columns and the panel says why, because drawing the
 * second inside the first would be a sum that does not add up, presented as one.
 *
 * AND NO `0` STANDS IN FOR AN ABSENCE. A volume the route sends as `null`
 * arrives with the reason it is null, and the reason is what the cell says. A
 * number that quietly vanished is the exact shape of a zero, which is the defect
 * story 58.1 spent itself removing one layer down.
 *
 * It fetches nothing: `DatastreamDailyBreakdown` owns the call and the four
 * states around it. This component is pure, which is what lets the component
 * sheet mount it and `scripts/measure_component_sheet.py` measure the three
 * refusals of the epic contract — the 52px row, the pill that must not take two
 * lines, and the column that must not leave the frame at 1128px.
 *
 * THE CONTRACT MOVED OUT, THE SCREEN DID NOT. This file was 1088 lines, over the
 * repository's own ceiling, so `breakdownTypes.ts` holds the payload shape and
 * `DayReadingPanel.tsx` the reading. Both are re-exported here, so no importer of
 * this module had to be touched to make room.
 */
import { Fragment, useMemo, useState } from "react";
import {
  Badge, Button, Checkbox, EmptyState, GAP_KEY_ORDER, Panel, PanelHeader,
  REPAIRABLE, Status,
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow, TableScroll,
  extractGapKey, extractGapLabel, extractGapSentence, extractGapShape,
  extractGapTone, gapKeyLabel, jobStateLabel, rowCountAbsence,
  type CoverageStatus,
  formatNumber,
} from "../../ui";
import { cn } from "../../lib/cn";
import type { ReadingState } from "./DatastreamDailyBreakdown";
import DayReadingPanel from "./DayReadingPanel";
import type { DimensionHistory } from "./dimensionDebt";
import { lateDimension, lateDimensionSentence } from "./lateDimensionNote";
import { numberText, titleCase } from "./evidence";
import type { BreakdownColumn, BreakdownDay, CountryValue, DailyBreakdown } from "./breakdownTypes";

export type {
  BreakdownColumn, BreakdownDay, CountrySplit, CountryValue, DailyBreakdown, DayReading,
  MoneyColumn, MoneyProvenance, MoneyRowValues, PairedCell, PairedColumn, PairedRow,
  ReadingPairing, ReadingSide, UnpairedColumn, ViewModeOption,
} from "./breakdownTypes";

/** The store that answered, said in words. `null` means neither did. */
const COLUMNS_SOURCE_NOTE: Record<string, string> = {
  mapping_version: "Read from the active mapping version.",
  flat_table: "Read from the flat mapping table.",
};

/**
 * Why the rows half is absent, in this screen's words.
 *
 * The route says so explicitly: only the ambiguity refusal owns a ratified
 * sentence, which travels as `rows_note`; the other two are machine names a
 * screen renders. An unrecognised one is shown as it arrived — a reason nobody
 * has written a sentence for is still evidence, and swallowing it would leave
 * the cell blank, which reads as zero.
 */
const ROWS_ABSENCE: Record<string, string> = {
  connector_not_in_mart: "this connector is not modelled in the mart",
  warehouse_unavailable: "the warehouse could not be read",
  ambiguous_materialization: "the mart slice cannot be attributed to one Datastream",
  // Story 58.5. The capability is ON and this flux still shows nothing — a
  // different fact from every other line of this table, and from the capability
  // being off. Measured: 5 connectors of the 39 report a country at all.
  connector_reports_no_country: "this connector reports no country",
  no_country_row_in_window: "no country row was counted in this window",
};

function rowsAbsence(reason: string | null | undefined): string {
  if (!reason) return "no volume was reported";
  return ROWS_ABSENCE[reason] ?? reason;
}

function collectedAbsence(reason: string | null | undefined): string {
  return rowCountAbsence(reason) ?? (reason || "no volume was reported");
}

/**
 * One mapped field: the raw name the source reports, above the canonical name it
 * becomes.
 *
 * STACKED, not `source -> canonical` on one line. At 1128px of main column and a
 * mapping of a dozen fields, one line truncates BOTH names and the header stops
 * proving the only thing it exists to prove — that you can check a mapping by
 * looking at the data. Stacked, each name gets the full width.
 *
 * The lower pill is MUTED when the two words are the same, and it is still
 * drawn. Measured: 44 of the 44 rows of the flat store are identity renames
 * (`clicks -> clicks`). Collapsing them to a single pill would make "passed
 * through unchanged" and "not mapped at all" look identical, and the second is
 * precisely what this tab is opened to fix. Muting instead makes the fields that
 * were REALLY renamed stand out at a glance.
 *
 * AND THE SAME PILL NOW SITS OVER TWO VALUES, in `Side by side` — which is what it
 * was built for and what it had never been attached to.
 */
function FieldPills({ column }: { column: BreakdownColumn }) {
  const identical = column.target_field === column.source_field;
  return (
    <li className="flex min-w-0 flex-col items-start gap-1 rounded-large border border-divider-base p-3">
      <Badge outline className="max-w-full" title={column.source_field}>
        <span className="truncate">{column.source_field}</span>
      </Badge>
      {column.target_field === null ? (
        // Never an empty pill: a pill with nothing in it reads as a canonical
        // name too small to see. The word is the state.
        <span className="text-caption text-text-secondary">Unmapped</span>
      ) : (
        <Badge
          outline={identical}
          className="max-w-full"
          title={identical ? `${column.target_field} — passed through unchanged` : column.target_field}
        >
          <span className="truncate">{column.target_field}</span>
        </Badge>
      )}
      {column.binding_status ? (
        <span className="text-caption text-text-secondary">{titleCase(column.binding_status)}</span>
      ) : null}
    </li>
  );
}

/**
 * One day, unfolded into its countries — story 58.5, arbitrage 2.
 *
 * A DAY STAYS ONE ROW. A row per (day × country) would multiply the date axis 58.2
 * built by a cardinality nobody bounds — 92 days × 250 codes — so the day unfolds
 * instead, bounded, and SAYS its bound: a list silently cut at twelve reads as the
 * whole answer.
 *
 * AND THE NO-COUNTRY BUCKET IS NOT IN THE RANKING. It is not a country, so it does
 * not compete for a place in the list, it is never cut off by the bound, and it is
 * drawn apart at the end. Its LABEL is the server's; its raw identity never reaches
 * the DOM.
 */
function CountryUnfold({
  entry,
  boundedAt,
  columnCount,
}: {
  entry: { values: CountryValue[]; country_count: number };
  boundedAt: number | undefined;
  columnCount: number;
}) {
  const countries = entry.values.filter((value) => value.kind === "country");
  const absent = entry.values.filter((value) => value.kind !== "country");
  const hidden = entry.country_count - countries.length;
  return (
    <TableRow data-testid="country-unfold">
      <TableCell colSpan={columnCount} className="bg-background-light">
        <ul className="m-0 flex list-none flex-wrap items-center gap-2 p-0">
          {countries.map((value) => (
            <li key={value.value}>
              <Badge outline>
                {value.label} · {formatNumber(value.rows)}
              </Badge>
            </li>
          ))}
          {absent.map((value) => (
            // SEPARATED, and last. A bucket sitting in the run of country chips
            // would be read as one of them, which is the single thing the plan
            // forbids for it.
            <li
              key={value.value}
              className="ml-2 border-l border-divider-base pl-3"
              data-testid="country-absent"
            >
              <span className="text-caption text-text-secondary">
                {value.label} · {formatNumber(value.rows)}
              </span>
            </li>
          ))}
        </ul>
        {hidden > 0 ? (
          <p className="m-0 mt-2 text-caption text-text-secondary">
            {countries.length} of {entry.country_count} countries are shown
            {boundedAt ? ` · at most ${boundedAt} per day` : ""}.
          </p>
        ) : null}
      </TableCell>
    </TableRow>
  );
}

/**
 * The states this window actually contains, in the vocabulary's own order.
 *
 * THE SET IS CLOSED AND IT IS NOT RETYPED HERE. `GAP_KEY_ORDER`
 * (`ui/CoverageBars.tsx`) is the one table of these words — the strip, the
 * grid's pill, these chips and the confirmation all read it — so a state added
 * there becomes a filter here without a line changing. A state this build does
 * not know still gets its chip, spelled as it arrived: `gapKeyLabel` falls back
 * on the raw word rather than mapping it onto a neighbour, and a day nobody can
 * name must not become a day nobody can find.
 *
 * COUNTED ON THE DAY, NOT ON ITS VERDICT. This function read `extract_status`
 * alone, and the ledger answers `never_fetched` for a window the source REFUSED:
 * measured 2026-08-21 on `WorkbenchDataPage`, 2 prevented days and 1 day nobody
 * asked for produced a chip reading « Never requested · 3 » while the
 * confirmation of the SAME page read « 2 Prevented — the source did not allow
 * it; 1 Never requested ». `extractGapKey` is the one key both now ask for.
 *
 * ONLY WHAT IS PRESENT. A chip for a state no day carries would offer a filter
 * that empties the table, which reads as "this window has nothing" — the exact
 * confusion between an absence and a hidden row this grid refuses everywhere.
 */
function statesPresent(days: BreakdownDay[]): { status: string; count: number }[] {
  const counts = new Map<string, number>();
  for (const day of days) {
    const key = extractGapKey(day);
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  const known = GAP_KEY_ORDER.filter((key) => counts.has(key));
  const unknown = [...counts.keys()].filter(
    (key) => !GAP_KEY_ORDER.includes(key) && key !== "",
  );
  return [...known, ...unknown].map((status) => ({ status, count: counts.get(status) ?? 0 }));
}

export default function DateBreakdownGrid({
  breakdown,
  fieldsOnly = false,
  onOpenRun,
  onRepullDay,
  onRepullDays,
  reading = { status: "idle" },
  day = null,
  onSelectDay,
  onRetryReading,
  dimensionHistory = null,
}: {
  breakdown: DailyBreakdown;
  /**
   * A source that is PUSHED keeps the fields and loses the pull axis — amendment
   * 7 of the 2026-08-11 review.
   *
   * The four blocks below the fields — the non-join banner, the country block,
   * the day grid and `Read one day` — are all keyed to a collection: a report
   * profile, a connector relation, a collection window, an extract status. A
   * `managed_feed` has none of them, and rendering them anyway is what made
   * `Read one day` repeat "which relation of the connector its rows land in"
   * three times at a Datastream with no connector.
   *
   * The FIELDS stay, on every mode: they come from the mapping store, they are
   * the one reading this tab owes a file source, and they are the line a person
   * opens this tab to repair.
   */
  fieldsOnly?: boolean;
  /** Opening a day opens the RUN that collected it (arbitrage 7). A day with no
   *  execution is not clickable and does not pretend to be. */
  onOpenRun?: (executionId: string) => void;
  /**
   * Ask for this day again — story 58.4, arbitrage 5.
   *
   * A CELL, not a hover and not a bar under the grid. A control that appears on
   * hover is unreachable by keyboard and invisible on touch; a bar under the
   * grid loses the link to the row, which is the whole subject of the gesture.
   * The row is 52px and 58.2 refuses a cell that folds, so the button is compact
   * and `scripts/measure_component_sheet.py` measures it against a real browser.
   *
   * Absent handler, absent column: this component is mounted by the component
   * sheet and by a screen, and a column of dead buttons would be a control that
   * does nothing.
   */
  onRepullDay?: (day: BreakdownDay) => void;
  /**
   * Ask for SEVERAL days again, in one gesture — amendment of 2026-08-18.
   *
   * The row action was the only door, so repairing a window meant one dialog and
   * one `POST` per day: sixteen never-requested days cost sixteen confirmations,
   * and nobody reads the sixteenth. The route has always taken an array
   * (`{dates: [...]}`) and the hook has always held one — what was missing was a
   * way to say WHICH days. Selection lives on this grid because the days are
   * here; the confirmation, the call and the wording stay in `repullDay.tsx`, so
   * the sentence a person reads before spending is still one sentence in one
   * place.
   *
   * Absent handler, absent selection: this component is mounted by the component
   * sheet too, and a checkbox column that leads to no action is a control that
   * does nothing.
   */
  onRepullDays?: (days: BreakdownDay[]) => void;
  /** The two readings of the opened day — story 58.3. */
  reading?: ReadingState;
  day?: string | null;
  onSelectDay?: (day: string) => void;
  onRetryReading?: () => void;
  /**
   * Why this window is hollow, when it is — amendment 14 of the 2026-08-11
   * review, delivered here 2026-08-31.
   *
   * The measurement is the server's (`core/datastream_dimension_history.py`) and
   * the sentence is derived in `lateDimensionNote.ts` from that one payload.
   * Absent history, absent note: this grid is mounted by the component sheet
   * too, and inventing a cause there would be worse than the silence.
   */
  dimensionHistory?: DimensionHistory | null;
}) {
  const { columns, days, window } = breakdown;
  // Story 58.5. A capability that is off decides NOTHING here: the block is absent
  // and so is its column, its header and its width.
  const country = breakdown.country?.active ? breakdown.country : null;
  const countryDays = country?.days ?? null;
  const [openCountryDay, setOpenCountryDay] = useState<string | null>(null);
  /** The day state the reader narrowed to, or every state. */
  const [stateFilter, setStateFilter] = useState<string | null>(null);
  /**
   * The days picked for one re-collection, by date.
   *
   * BY DATE AND NOT BY INDEX, and the selection is DERIVED from the current
   * window rather than stored as rows: after a `202` the parent reads the window
   * again, and a selection holding row objects would then be pointing at days
   * that no longer exist. A date that leaves the window leaves the selection with
   * it, which is the only behaviour that cannot spend on something unseen.
   */
  const [picked, setPicked] = useState<ReadonlySet<string>>(new Set());

  const states = useMemo(() => statesPresent(days), [days]);
  /** The dimension whose arrival explains the hollow, or `null` — amendment 14. */
  const late = useMemo(() => lateDimension(dimensionHistory), [dimensionHistory]);
  const shown = useMemo(
    // FILTERED ON THE SAME KEY THE CHIP COUNTED. Narrowing on `extract_status`
    // under a chip counted on the day would have shown 3 rows behind a chip that
    // said 2 — a filter that disagrees with its own count.
    () => (stateFilter ? days.filter((day) => extractGapKey(day) === stateFilter) : days),
    [days, stateFilter],
  );
  /**
   * What the bulk gesture would actually ask for — never `ok`, never `running`.
   *
   * THIS ONE READS THE VERDICT ALONE, ON PURPOSE. `REPAIRABLE` answers "is
   * anything worth asking for again", which is a question about what LANDED, and
   * the verdict is exactly that: a prevented window landed nothing, reports
   * `never_fetched`, and IS worth re-asking once the grant exists -- the
   * connector's own sentence ends « then re-ask these dates ». The readings that
   * had to move to the day are the ones that NAME or COUNT what happened; this
   * one names nothing.
   */
  const selected = useMemo(
    () => days.filter(
      (day) => picked.has(day.date) && REPAIRABLE.has(day.extract_status as CoverageStatus),
    ),
    [days, picked],
  );
  const selectable = shown.filter(
    (day) => REPAIRABLE.has(day.extract_status as CoverageStatus),
  );
  // The verdict again, and legitimately: `failed` is the one word no window
  // state collapses into. `cancelled`, `superseded` and `prevented` all report
  // `never_fetched`, so nothing the source refused can be counted here.
  const failed = shown.filter((day) => day.extract_status === "failed");
  const bulk = Boolean(onRepullDays);
  const toggle = (date: string) =>
    setPicked((current) => {
      const next = new Set(current);
      if (next.has(date)) next.delete(date);
      else next.add(date);
      return next;
    });
  const take = (batch: BreakdownDay[]) =>
    setPicked((current) => {
      const next = new Set(current);
      for (const day of batch) next.add(day.date);
      return next;
    });
  // Date, Extracts, Rows collected, Rows at the mart, [Countries], Extract,
  // Collection window, [Re-ask] — counted, because the unfolded row spans them.
  const columnCount = 6 + (country ? 1 : 0) + (onRepullDay ? 1 : 0);

  return (
    <>
      <Panel flush data-testid="daily-breakdown-fields">
        <PanelHeader
          title="Fields of the active mapping"
          description={
            columns.length
              ? `${COLUMNS_SOURCE_NOTE[breakdown.columns_source ?? ""] ?? "Read from the mapping."} The raw name the source reports sits above the canonical name it becomes.`
              : "Neither mapping store holds a field for this Datastream, so there is no header to draw."
          }
        />
        <div className="px-5 pb-5">
          {columns.length === 0 ? (
            <Status as="block" tone="warning" title="No mapped field">
              {breakdown.columns_reason
                ? `The mapping has no field to name (${breakdown.columns_reason}).`
                : "The mapping has no field to name."}
            </Status>
          ) : (
            <ul className="m-0 grid list-none grid-cols-4 gap-3 p-0 max-lg:grid-cols-2">
              {columns.map((column) => (
                <FieldPills key={column.source_field} column={column} />
              ))}
            </ul>
          )}
        </div>
      </Panel>

      {fieldsOnly ? null : (
      <>
      {/* THE NON-JOIN OF THE DAY GRID, SHOWN — and only of the day grid. Hiding it
          would mean drawing a column per mapped field over rows that are keyed by
          nothing of the sort. It says nothing about `Side by side`, which pairs the
          two readings through the mapping itself (amendment 12). */}
      {!breakdown.column_row_join_available && (
        <Status as="block" tone="neutral" title="These fields are not the columns below">
          The mapping's target has no key to the mart's metric
          {breakdown.column_row_join_reason ? ` (${breakdown.column_row_join_reason})` : ""}, so the
          days below carry what each day COLLECTED and not a value per field. Open a day and read it
          `Side by side` to see each field's raw value and the value it becomes.
        </Status>
      )}

      {/* WHY THE STRIP IS HOLLOW, WHEN IT IS — amendment 14, and it is stated
          HERE because this is where the hollow is drawn. The amendment's last
          line is the whole reason: « un trou expliqué est une donnée ; un trou
          muet est un bug que l'opérateur impute au produit. »

          ABOVE the grid and not inside a row: the fact is about the WINDOW, and
          a badge repeated on forty rows would say one thing forty times. The
          day panel says the day-scoped half, and only for a day it applies to.

          It offers no repair. The bounded, costed re-collection lives on
          `Processing`, beside the change that creates the debt; a second door
          onto it here would be the same gesture asked twice. */}
      {late ? (
        <Status
          as="block"
          tone="neutral"
          title="Part of this window predates a column"
          data-testid="late-dimension-note"
        >
          {lateDimensionSentence(late)}
        </Status>
      ) : null}

      {/* THE COUNTRY BLOCK, and it exists only because the project turned the
          capability on. It says what the split IS and what it is NOT: the country
          series is one of several parallel series of the mart, so its values do not
          add up to `Rows at the mart` and nothing here suggests they should. */}
      {country ? (
        <Status
          as="block"
          tone={country.degraded ? "warning" : "neutral"}
          title={
            country.degraded
              ? "Country split — the capability is degraded"
              : "Country split"
          }
          data-testid="country-block"
        >
          {country.reason
            ? `No country value is counted here: ${rowsAbsence(country.reason)}.`
            : "Each day unfolds into the country values the mart carries for it."}{" "}
          {country.note ?? ""} These values are one of several parallel series of the
          mart, so they are not the parts of the day's total rows.
          {country.degraded
            ? " The capability is degraded: what was collected is shown, diminished, rather than hidden."
            : ""}
        </Status>
      ) : null}

      <Panel flush data-testid="daily-breakdown-grid">
        <PanelHeader
          title="Day by day"
          description={
            `${window.start} to ${window.end} · at most ${window.bounded_at} days may be read at once` +
            (window.bound_reached ? " · this window is at that bound" : "")
          }
        />
        {/* NARROWING THE WINDOW BY WHAT A DAY DID — amendment of 2026-08-18.
            Sixty rows, no way to ask « which ones failed », and the only reading
            of that question was to scan the pill column by eye. The chips are the
            states this window CONTAINS, counted, in the vocabulary's own order;
            a state no day carries has no chip, because a filter that empties the
            table reads as an empty window.

            IT HIDES, IT DOES NOT MEASURE. The sentence under the chips says how
            many of how many are shown and offers the way back, so a narrowed
            table can never be mistaken for the whole answer — the same rule the
            stage filter of this tab already holds one panel below. */}
        {days.length > 0 && states.length > 1 ? (
          <div
            className="flex flex-wrap items-center gap-2 border-t border-divider-base px-5 py-3"
            role="group"
            aria-label="Filter the days by what the extract did"
            data-testid="day-state-filter"
          >
            <Button
              variant={stateFilter === null ? "secondary" : "ghost"}
              size="xs"
              aria-pressed={stateFilter === null}
              onClick={() => setStateFilter(null)}
            >
              Every day · {days.length}
            </Button>
            {states.map(({ status, count }) => (
              <Button
                key={status}
                variant={stateFilter === status ? "secondary" : "ghost"}
                size="xs"
                aria-pressed={stateFilter === status}
                onClick={() => setStateFilter(stateFilter === status ? null : status)}
              >
                {gapKeyLabel(status)} · {count}
              </Button>
            ))}
          </div>
        ) : null}

        {stateFilter ? (
          <div className="border-t border-divider-base px-5 py-3">
            <Status
              as="block"
              tone="neutral"
              title={`Showing ${shown.length} of ${days.length} days — ${gapKeyLabel(stateFilter)}`}
              action={
                <button
                  type="button"
                  className="text-primary underline"
                  onClick={() => setStateFilter(null)}
                >
                  Show every day
                </button>
              }
            >
              The other days are hidden, not absent — the window and every reading
              in it are unchanged.
            </Status>
          </div>
        ) : null}

        {days.length === 0 ? (
          <div className="px-5 pb-5">
            <EmptyState
              title="No day was collected in this window"
              description={`Nothing covered ${window.start} to ${window.end}${
                breakdown.reason ? ` (${breakdown.reason})` : ""
              }. No row is drawn — an invented day would look like evidence.`}
            />
          </div>
        ) : (
          <TableScroll label="Day-by-day breakdown">
            {/* `table-fixed`: every cell holds ONE line, and a sentence that
                cannot fit is truncated with its full text on the element rather
                than folded onto a second line. A row that grows is a row that
                stops being 52px. */}
            <Table className="table-fixed">
              <TableHeader>
                <TableRow>
                  {/* THE SHARES SUM TO ONE, and story 58.4 is why it is written
                      down: adding the seventh column to the six of 58.2 took the
                      total to 12.8/12, and `scripts/measure_component_sheet.py`
                      caught the grid scrolling at 1128px (996 available, 1028
                      wanted) — the exact refusal of the epic contract. The two
                      volume columns give up their extra twelfth; their absence
                      sentences were already truncated with the full text on the
                      element. */}
                  <TableHead className="w-1/6">Date</TableHead>
                  <TableHead numeric className="w-1/12">Extracts</TableHead>
                  <TableHead numeric className="w-1/6">Rows collected</TableHead>
                  <TableHead numeric className="w-1/6">Rows at the mart</TableHead>
                  {/* Story 58.5. The eighth column exists only when the capability
                      is on, and the twelfth it needs is taken from the collection
                      window rather than from a volume or a pill: that cell already
                      truncates with its full text on the element, and the shares
                      still sum to one — 58.4 measured what happens when they do
                      not (the grid scrolled at 1128px). */}
                  {country ? (
                    <TableHead numeric className="w-1/12">Countries</TableHead>
                  ) : null}
                  <TableHead className="w-1/6">Extract</TableHead>
                  <TableHead className={country ? "w-1/12" : "w-1/6"}>
                    Collection window
                  </TableHead>
                  {onRepullDay ? (
                    <TableHead className="w-1/12 px-2">Re-ask</TableHead>
                  ) : null}
                </TableRow>
              </TableHeader>
              <TableBody>
                {shown.map((day) => {
                  const windowState = jobStateLabel(day.job_state);
                  // What is missing on this day, with the connector and the date
                  // — composed in the vocabulary, never here, because the
                  // confirmation this action opens says the same sentence.
                  const gap = extractGapSentence(day, breakdown.connector);
                  const repairable = REPAIRABLE.has(day.extract_status as CoverageStatus);
                  const split = countryDays?.[day.date] ?? null;
                  const unfolded = openCountryDay === day.date;
                  return (
                    <Fragment key={day.date}>
                    <TableRow density="compact">
                      <TableCell className="font-numeric [font-variant-numeric:lining-nums_tabular-nums]">
                        {/* THE CHECKBOX SHARES THE DATE'S CELL, and that is a
                            measurement rather than a taste. The seven columns
                            already sum to exactly twelve twelfths and story 58.4
                            measured what an eighth does: the grid scrolled at
                            1128px, which is the epic contract's own refusal. The
                            date column holds two twelfths for a ten-character
                            date, so the 18px mark fits beside it and no width
                            moves. It is 18px against the 24px control already on
                            this row, so the 52px row does not move either. */}
                        <div className="flex min-w-0 items-center gap-2">
                          {bulk && repairable ? (
                            <Checkbox
                              checked={picked.has(day.date)}
                              onCheckedChange={() => toggle(day.date)}
                              aria-label={gap ?? `Select ${day.date} for re-collection`}
                            />
                          ) : null}
                          {day.execution_id && onOpenRun ? (
                            <button
                              type="button"
                              className="truncate text-primary underline"
                              title={`Open the run that collected this day (${day.execution_id})`}
                              onClick={() => onOpenRun(day.execution_id as string)}
                            >
                              {day.date}
                            </button>
                          ) : (
                            <span className="truncate">{day.date}</span>
                          )}
                        </div>
                      </TableCell>
                      <TableCell numeric>{numberText(day.extract_count)}</TableCell>
                      <TableCell numeric>
                        {day.row_count === null ? (
                          <span
                            className="block truncate text-caption text-text-secondary"
                            title={collectedAbsence(day.row_count_reason)}
                          >
                            {collectedAbsence(day.row_count_reason)}
                          </span>
                        ) : (
                          formatNumber(day.row_count)
                        )}
                      </TableCell>
                      <TableCell numeric>
                        {day.rows === null ? (
                          <span
                            className="block truncate text-caption text-text-secondary"
                            title={rowsAbsence(day.rows_reason)}
                          >
                            {rowsAbsence(day.rows_reason)}
                          </span>
                        ) : (
                          formatNumber(day.rows)
                        )}
                      </TableCell>
                      {country ? (
                        <TableCell numeric className="px-2">
                          {split && split.values.length > 0 ? (
                            // THE COUNT IS THE CONTROL, AND IT ONLY APPEARS WHEN
                            // THERE IS SOMETHING TO COUNT. A day whose rows ALL
                            // landed in the absence bucket has zero NAMED countries
                            // and plenty of rows: printing `0` there is the
                            // `0`-for-an-absence this grid refuses everywhere else,
                            // and it is the LIKELY day — 34 connectors of the 39
                            // report no country at all. The word takes over, and it
                            // is the server's word.
                            <Button
                              variant="ghost"
                              // `xs` (24px), not `sm` (36px): this cell sits on the
                              // 52px compact row and a `sm` control makes it 53 --
                              // measured by `scripts/measure_component_sheet.py`
                              // against a real browser, and invisible to the eye.
                              size="xs"
                              className="max-w-full px-2"
                              aria-expanded={unfolded}
                              aria-label={
                                split.country_count > 0
                                  ? `${unfolded ? "Hide" : "Show"} the ${
                                      split.country_count
                                    } country values of ${day.date}`
                                  : `${unfolded ? "Hide" : "Show"} the rows of ${
                                      day.date
                                    } that carry no country`
                              }
                              onClick={() =>
                                setOpenCountryDay(unfolded ? null : day.date)
                              }
                            >
                              <span className="truncate">
                                {split.country_count > 0
                                  ? formatNumber(split.country_count)
                                  : (split.values.find((value) => value.kind !== "country")
                                      ?.label ?? "")}
                              </span>
                              {/* `leading-none`: the disclosure glyph has a taller
                                  line box than Latin text and pushed the compact row
                                  to 53px — `measure_component_sheet.py` caught it
                                  against a real browser, the eye did not. */}
                              <span aria-hidden="true" className="ml-1 leading-none">
                                {unfolded ? "▾" : "▸"}
                              </span>
                            </Button>
                          ) : (
                            // NEVER A `0`: the route sent no split for this day, and
                            // the reason it sent is the reason shown — the same rule
                            // the two volume cells beside it hold.
                            <span
                              className="block truncate text-caption text-text-secondary"
                              title={rowsAbsence(
                                breakdown.country?.reason ?? "no_country_row_in_window",
                              )}
                            >
                              {rowsAbsence(
                                breakdown.country?.reason ?? "no_country_row_in_window",
                              )}
                            </span>
                          )}
                        </TableCell>
                      ) : null}
                      <TableCell>
                        {/* THE DAY, NEVER ONE FIELD OF IT. This pill read
                            `extract_status` alone, so a row whose window the
                            source REFUSED said « Never requested » two cells
                            away from a button named « ... did not allow this
                            collection for 2026-07-12 — Request the reviews
                            allowlist ... »: one line, two words, and only one of
                            them true. The refusal now says its own name here,
                            which repeats the `Collection window` cell beside it
                            — one fact stated twice is a redundancy, one fact
                            contradicted is a person quoting the wrong half back.

                            ONE line, always: the epic contract refuses a pill
                            that takes two.

                            AND A SHAPE, since story 58.4: `empty` and
                            `never_fetched` share the neutral tone on purpose —
                            a semantic colour on one would make the column
                            scannable by hue and stop it being read — so the
                            filled/dashed shape is what separates them without
                            a hover. It comes from the vocabulary, so the strip
                            and this grid cannot drift apart. */}
                        <Status
                          tone={extractGapTone(day)}
                          data-testid="day-extract-status"
                          className={cn(
                            "max-w-full [&>span]:truncate",
                            extractGapShape(day),
                          )}
                        >
                          {extractGapLabel(day)}
                        </Status>
                      </TableCell>
                      <TableCell>
                        {windowState ? (
                          <span className="block truncate" title={windowState}>{windowState}</span>
                        ) : (
                          // TRUNCATES LIKE ITS SIBLING. It did not, and it was the
                          // only cell of this grid whose two branches behaved
                          // differently: at one twelfth of 1128px — the width this
                          // column takes when the country column exists — the words
                          // wrapped and the row grew to 53px, which
                          // `measure_component_sheet.py` reported against the epic
                          // contract's 52.
                          <span
                            className="block truncate text-text-secondary"
                            title="No collecting window"
                          >
                            No collecting window
                          </span>
                        )}
                      </TableCell>
                      {onRepullDay ? (
                        // `px-2`, not the grid's `px-4.5`: at one twelfth of
                        // 1128px the cell holds 83px, and 36px of padding left
                        // the control 47px. The measurement is what decided it,
                        // not the eye — the sheet reported the grid scrolling.
                        <TableCell className="px-2">
                          {repairable ? (
                            // THE SENTENCE IS THE BUTTON'S NAME. The row is one
                            // line per cell, so "Meta returned nothing for
                            // 2026-06-09" cannot be a visible paragraph here —
                            // it is the accessible name and the tooltip, and the
                            // confirmation repeats it in full before anything is
                            // spent. A bare "Re-collect" would be a control
                            // whose object a screen reader could not name.
                            <Button
                              variant="secondary"
                              size="sm"
                              className="px-2"
                              aria-label={gap ?? `Re-collect ${day.date}`}
                              title={gap ?? `Re-collect ${day.date}`}
                              onClick={() => onRepullDay(day)}
                            >
                              Re-ask
                            </Button>
                          ) : (
                            // A day already collected, or one being collected
                            // right now. Never a disabled button with no reason:
                            // the word says why there is nothing to ask for, and
                            // it truncates with its full text on the element —
                            // the pattern four other cells of this grid hold.
                            <span
                              className="block truncate text-caption text-text-secondary"
                              title="Nothing to re-ask"
                            >
                              Nothing to re-ask
                            </span>
                          )}
                        </TableCell>
                      ) : null}
                    </TableRow>
                    {unfolded && split ? (
                      <CountryUnfold
                        entry={split}
                        boundedAt={breakdown.country?.bounded_at}
                        columnCount={columnCount}
                      />
                    ) : null}
                    </Fragment>
                  );
                })}
              </TableBody>
            </Table>
          </TableScroll>
        )}

        {/* ONE RE-COLLECTION FOR MANY DAYS — amendment of 2026-08-18.
            The row action was the only door, so a window with sixteen holes cost
            sixteen dialogs, and the sixteenth is confirmed without being read.
            The selection is stated here in full before anything opens: how many
            days are picked, how many of them can actually be re-collected, and
            the way back. `ok` and `running` are never in that count — re-asking
            either spends quota to learn nothing, which is the same set
            (`REPAIRABLE`) the row action and the `Runs` strip already read. */}
        {bulk && selectable.length > 0 ? (
          <div
            className="flex flex-wrap items-center justify-between gap-3 border-t border-divider-base px-5 py-3"
            data-testid="bulk-repull-bar"
          >
            <Status tone={selected.length ? "accent" : "neutral"}>
              {selected.length === 0
                ? `${selectable.length} of the ${shown.length} days shown can be re-collected`
                : `${selected.length} ${selected.length === 1 ? "day" : "days"} selected` +
                  ` · ${selected[0].date} to ${selected[selected.length - 1].date}`}
            </Status>
            <div className="flex flex-wrap items-center gap-2">
              {failed.length > 0 ? (
                <Button variant="ghost" size="sm" onClick={() => take(failed)}>
                  Select the {failed.length} failed
                </Button>
              ) : null}
              <Button variant="ghost" size="sm" onClick={() => take(selectable)}>
                Select all {selectable.length} re-collectable
              </Button>
              {selected.length > 0 ? (
                <Button variant="ghost" size="sm" onClick={() => setPicked(new Set())}>
                  Clear
                </Button>
              ) : null}
              <Button
                size="sm"
                disabled={selected.length === 0}
                onClick={() => onRepullDays?.(selected)}
              >
                Re-collect {selected.length || ""}
              </Button>
            </div>
          </div>
        ) : null}
      </Panel>

      {/* No availability on the wire, no control. An envelope that carries no
          verdict for the four modes cannot be turned into one here, and a
          selector whose positions are all greyed for no stated reason is the
          silence this story exists to remove. */}
      {(breakdown.view_mode?.available?.length ?? 0) > 0 && (
        <DayReadingPanel
          breakdown={breakdown}
          reading={reading}
          day={day}
          onSelectDay={onSelectDay}
          onRetryReading={onRetryReading}
          dimensionHistory={dimensionHistory}
        />
      )}
      </>
      )}
    </>
  );
}
