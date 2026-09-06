/**
 * The day-by-day read of a Datastream: the call, the window, and the four states
 * around them — story 58.2.
 *
 * FOUR STATES, NEVER THREE. The pattern is `shell/pages/CoverageStrip.tsx`, the
 * one element of this surface Jean named as correct: reading, broken, empty,
 * served. A screen that folds "it broke" into "there is nothing" publishes an
 * absence of evidence as evidence of absence.
 *
 * AND A FIFTH READING THAT IS NOT A FAILURE: the route can serve the days and
 * refuse the cells (`rows: null` with `rows_reason`), because the extract
 * registry lives in Postgres while the volumes live in the mart. That is a
 * served answer with a named gap, not a broken one, and it stays in the grid.
 *
 * The window defaults to 60 days — `CoverageStrip`'s own default. Three defaults
 * already live in this product (35, 60, 7); a fourth would make the same flux
 * tell two different stories on two screens answering the same question.
 *
 * ONE THING HERE WRITES, and it arrived with its confirmation — story 58.4. A
 * row's `Re-collect` opens the dialog that names the Datastream, the connector,
 * the source account and the day, and says `Not measured` for the spend with its
 * reason. After the `202` the WINDOW IS READ AGAIN (arbitrage 6): the row then
 * carries whatever the registry holds, rather than an id this screen was handed
 * and never observed — and the answer carries no id at all on this path today.
 */
import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "../../lib/apiFetch";
import { Button, Field, Input, Panel, PanelHeader, Status } from "../../ui";
import DateBreakdownGrid, {
  type BreakdownDay, type DailyBreakdown, type DayReading,
} from "./DateBreakdownGrid";
import type { DimensionHistory } from "./dimensionDebt";
import {
  RepullConfirmDialog, repullOutcomeSentence, repullScopeSentence, useRepull,
  type RepullOutcome,
} from "./repullDay";

/** `CoverageStrip`'s window, reused rather than re-decided (arbitrage 8). */
const WINDOW_DAYS = 60;

type State =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ok"; breakdown: DailyBreakdown };

/** The reading of one day: nothing asked for, reading, refused, or served. */
export type ReadingState =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "refused"; message: string }
  | { status: "ok"; reading: DayReading };

function isoDay(value: Date): string {
  return value.toISOString().slice(0, 10);
}

function defaultWindow(): { start: string; end: string } {
  const end = new Date();
  end.setUTCDate(end.getUTCDate() - 1); // through yesterday, as the ledger does
  const start = new Date(end);
  start.setUTCDate(start.getUTCDate() - (WINDOW_DAYS - 1));
  return { start: isoDay(start), end: isoDay(end) };
}

/**
 * Day arithmetic on UTC, which is the axis the extract ledger is keyed on.
 *
 * `new Date("2026-07-10")` is already midnight UTC, and every date this route
 * speaks is a bare `YYYY-MM-DD`: doing the arithmetic in the reader's zone would
 * move a preset by one day for anybody west of Greenwich, on a screen whose whole
 * subject is which day is missing.
 */
function shiftDays(day: string, delta: number): string {
  const moved = new Date(`${day}T00:00:00Z`);
  moved.setUTCDate(moved.getUTCDate() + delta);
  return isoDay(moved);
}

function monthStart(day: string): string {
  return `${day.slice(0, 7)}-01`;
}

/** The month BEFORE the anchor's, whole — its own first day to its own last. */
function previousMonth(day: string): { start: string; end: string } {
  const end = new Date(`${monthStart(day)}T00:00:00Z`);
  end.setUTCDate(0); // day 0 of a month is the last day of the one before it
  const last = isoDay(end);
  return { start: monthStart(last), end: last };
}

/**
 * The ranges a person actually asks for, computed from the data's own anchor.
 *
 * WHY AN ANCHOR AND NOT `today`. A Datastream whose collection stopped three
 * weeks ago answers « last 7 days » with seven empty rows, which says nothing
 * about the flux and everything about the calendar. So the ranges are counted
 * back from the most recent day this Datastream actually collected something,
 * when the payload carries one — and the sentence beside them SAYS which anchor
 * was used, because a range whose end nobody can name is a measurement nobody
 * can check. With no collected day on the wire there is nothing to derive from,
 * and the anchor falls back to yesterday, said as such.
 *
 * The anchor is stable under its own presets: it is a day with an extract, so it
 * stays inside any window that ends on it and stays the most recent collected
 * day of that window. Choosing a preset twice cannot walk the window backwards.
 */
function presetsFrom(anchor: string): { label: string; start: string; end: string }[] {
  return [
    { label: "7 days", start: shiftDays(anchor, -6), end: anchor },
    { label: "30 days", start: shiftDays(anchor, -29), end: anchor },
    { label: "60 days", start: shiftDays(anchor, -59), end: anchor },
    { label: "This month", start: monthStart(anchor), end: anchor },
    { label: "Last month", ...previousMonth(anchor) },
  ];
}

export default function DatastreamDailyBreakdown({
  projectId,
  datastreamId,
  mode,
  sourceAccountRef,
  onOpenRun,
  dimensionHistory = null,
}: {
  projectId: string;
  datastreamId: string;
  /**
   * `app.datastreams.source_kind`, and the ONLY field that carries the word
   * `managed_feed` — amendment 7 of the 2026-08-11 review.
   *
   * The guard below used to read `connector`, which the header composes as
   * `config.connector_name or module_name`. Both are NULL on all 4 of the 4 live
   * `managed_feed` fluxes, so the guard never fired and a file source was handed
   * a connector's pull axis: three positions of `Read one day` repeating "no
   * report profile ... which relation of the connector its rows land in" at a
   * Datastream that has no connector. A mode guard reads the mode.
   */
  mode?: string | null;
  /**
   * The provider account this Datastream reads — `header.identity`, handed down
   * by the route. The day payload does not carry it and must not invent one: a
   * confirmation that named no account would let somebody spend on a connection
   * they did not check.
   */
  sourceAccountRef?: string | null;
  onOpenRun?: (executionId: string) => void;
  /**
   * The late-dimension measurement of the tab payload — amendment 14, passed
   * through 2026-08-31.
   *
   * PASSED, NEVER FETCHED. This component owns one call (the day window); the
   * history is already on the `data` tab payload the route loaded, and a second
   * request for it here would give the same screen two readings of one fact.
   * It is only carried: the sentence is derived in `lateDimensionNote.ts` and
   * rendered by the two surfaces where the hollow is drawn.
   */
  dimensionHistory?: DimensionHistory | null;
}) {
  const [range, setRange] = useState(defaultWindow);
  const [state, setState] = useState<State>({ status: "loading" });
  const [reload, setReload] = useState(0);

  /** A source that is PUSHED has no pull axis — and no panel saying so.
   *
   *  The fields of the mapping stay: they are the one thing this tab owes every
   *  mode, and they are read from the mapping store, not from the extract
   *  registry. What goes is the day grid, the country block, the non-join banner
   *  and `Read one day` — four blocks that speak of report profiles, connector
   *  relations and collection windows to a Datastream that has none. */
  const pushedSource = mode === "managed_feed" || mode === "external_bq";

  /**
   * The opened day, and its two readings — story 58.3.
   *
   * A SECOND CALL, on purpose. The day grid answers a window and the reading
   * answers one day; folding them into one request would make choosing a day
   * re-read the whole strip, and the story's `Permet` line says the grid must not
   * move. The reading itself carries BOTH sides, so switching between
   * `Collected`, `Mapped` and `Side by side` asks nothing at all.
   */
  const [day, setDay] = useState<string | null>(null);
  /**
   * FOUR STATES HERE TOO, and the fourth is why this is not a boolean.
   *
   * The first version set `reading = null` on an HTTP failure, and the panel then
   * drew « No day is open » while the day selector showed the chosen date — a
   * read that BROKE, rendered as a read that was never asked for. It is the same
   * defect the grid above it was built to avoid, one call lower, and this was the
   * only reader of `datastreams/workbench/` without a `refused` branch of its own.
   */
  const [reading, setReading] = useState<ReadingState>({ status: "idle" });

  const load = useCallback(async () => {
    setState({ status: "loading" });
    const query = new URLSearchParams({ start: range.start, end: range.end });
    try {
      const response = await apiFetch(
        `/api/projects/${encodeURIComponent(projectId)}/datastreams/${encodeURIComponent(
          datastreamId,
        )}/daily-breakdown?${query.toString()}`,
        { cache: "no-store" },
      );
      const body = (await response.json().catch(() => ({}))) as Partial<DailyBreakdown> & {
        message?: string;
      };
      if (!response.ok) {
        // The route's own sentence when it wrote one — a window that is too wide
        // and a stage that is not materialised both say exactly why, and
        // replacing that with `HTTP 400` would throw the answer away.
        throw new Error(body.message ?? `HTTP ${response.status}`);
      }
      setState({
        status: "ok",
        breakdown: {
          connector: body.connector ?? null,
          window: body.window ?? {
            start: range.start,
            end: range.end,
            bounded_at: WINDOW_DAYS,
            bound_reached: false,
          },
          columns: Array.isArray(body.columns) ? body.columns : [],
          columns_reason: body.columns_reason ?? null,
          columns_source: body.columns_source ?? null,
          column_row_join_available: body.column_row_join_available === true,
          column_row_join_reason: body.column_row_join_reason ?? null,
          days: Array.isArray(body.days) ? body.days : [],
          reason: body.reason ?? null,
          rows_note: body.rows_note ?? null,
          // Story 58.5. `null` when the envelope carries none — an older payload
          // must not grow a country block out of a default written here.
          country: body.country ?? null,
          view_mode: body.view_mode ?? null,
          stage_relations: body.stage_relations ?? null,
        },
      });
      // The day the reading opens on is DERIVED from what the route sent — the
      // most recent day that actually collected something. Never invented, and
      // never a day the grid does not carry.
      const days = Array.isArray(body.days) ? body.days : [];
      const collected = [...days].reverse().find((entry) => (entry.extract_count ?? 0) > 0);
      setDay((current) =>
        current && days.some((entry) => entry.date === current)
          ? current
          : collected?.date ?? null,
      );
    } catch (error) {
      // No substituted days: an unreadable breakdown says so. A drawn grid would
      // be read as coverage.
      setState({
        status: "error",
        message: error instanceof Error ? error.message : "Request failed",
      });
    }
  }, [projectId, datastreamId, range.start, range.end, reload]);

  useEffect(() => {
    void load();
  }, [load]);

  const loadReading = useCallback(async () => {
    if (!day) {
      setReading({ status: "idle" });
      return;
    }
    setReading({ status: "loading" });
    const query = new URLSearchParams({ start: day, end: day, day });
    try {
      const response = await apiFetch(
        `/api/projects/${encodeURIComponent(projectId)}/datastreams/${encodeURIComponent(
          datastreamId,
        )}/daily-breakdown?${query.toString()}`,
        { cache: "no-store" },
      );
      const body = (await response.json().catch(() => ({}))) as {
        reading?: DayReading;
        message?: string;
      };
      if (!response.ok) {
        // The route's own sentence when it wrote one, and the status when it did
        // not — the pattern `DatastreamSample` already holds beside this one.
        // NO rows are substituted: a drawn table would be read as the day's data.
        setReading({
          status: "refused",
          message: body.message ?? `HTTP ${response.status}`,
        });
        return;
      }
      setReading(
        body.reading
          ? { status: "ok", reading: body.reading }
          : { status: "refused", message: "The response carried no reading for this day." },
      );
    } catch (error) {
      setReading({
        status: "refused",
        message: error instanceof Error ? error.message : "Request failed",
      });
    }
  }, [projectId, datastreamId, day, reload]);

  useEffect(() => {
    void loadReading();
  }, [loadReading]);

  /**
   * The days whose rows opened the confirmation, kept so the dialog can say WHAT
   * is missing on them. The dates alone would not: "Meta returned nothing" and
   * "Meta was never asked" are two different reasons to spend.
   *
   * A LIST SINCE 2026-08-18, because the bulk door and the row door open the same
   * confirmation and it must say the same kind of thing for one day and for
   * forty. One day still gets its own sentence, word for word.
   */
  const [asked, setAsked] = useState<BreakdownDay[]>([]);
  /** What the last re-collection did, in the words the route can support. */
  const [outcome, setOutcome] = useState<string | null>(null);
  const repull = useRepull(projectId, datastreamId, (result: RepullOutcome) => {
    setOutcome(repullOutcomeSentence(result));
    // ARBITRAGE 6: read the window again rather than writing the id we were
    // handed. The row then shows the run if there is one and nothing if there is
    // not — and on this path there is not, because the route can open no run for
    // a stream it accepts (proved in tests/integration/…daily_breakdown_api.py).
    setReload((n) => n + 1);
  });

  /**
   * The day the ranges are counted back from, and whether the DATA gave it.
   *
   * Derived from the payload — the most recent day that collected something — so
   * a flux whose collection stopped in June does not answer "last 7 days" with
   * seven empty rows of August. When the wire carries no collected day there is
   * nothing to derive from and yesterday stands in, which the sentence says out
   * loud rather than letting the reader assume the window is about their today.
   */
  const collectedAnchor =
    state.status === "ok"
      ? [...state.breakdown.days].reverse().find((entry) => (entry.extract_count ?? 0) > 0)?.date
        ?? null
      : null;
  const anchor = collectedAnchor ?? defaultWindow().end;

  return (
    <>
      {pushedSource ? (
        /* A PUSHED SOURCE KEEPS A SENTENCE WHERE ITS PULL AXIS WAS — amendment
           of 2026-08-18, and an `Incomplete if` of this surface: « a block is
           withdrawn from its `Data` tab without one sentence saying what is
           absent and where the same question is answered instead ».

           Amendment 7 of the 2026-08-11 review was right to take the day grid,
           the collection window and `Read one day` away from a mode that has no
           connector — but what replaced them was `null`, so four of the six live
           Datastreams opened `Data` onto a gap with no explanation. The panel
           below is not a substitute axis: it names what is absent, why the mode
           makes it absent, that re-collection is absent WITH its reason, and the
           reading that answers the same question for this mode. */
        <Panel flush data-testid="pushed-source-no-day-axis">
          <PanelHeader
            title="This Datastream is not read by day"
            description={
              mode === "managed_feed"
                ? "Its rows are pushed to it as a file, so there is no collection window to lay a calendar over."
                : "It reads a relation it does not own, so nothing here is collected on a window."
            }
          />
          <div className="px-5 pb-5">
            <Status as="block" tone="neutral" title="Where the same question is answered">
              {mode === "managed_feed"
                ? "What arrived, when it arrived and what it contained is read in "
                  + "“The last file that arrived”, below. No re-collection is offered: "
                  + "nothing is asked of a provider here, so a day cannot be asked for "
                  + "again — a file is sent to this Datastream, and the door it comes "
                  + "in by is named in that panel."
                : "The columns this Datastream reads are on Mapping, and its rows stay "
                  + "in the relation that owns them. No re-collection is offered: this "
                  + "Datastream pulls nothing, so there is no window to ask for again."}
            </Status>
          </div>
        </Panel>
      ) : (
      <Panel flush>
        <PanelHeader
          title="Read this Datastream by day"
          description="One row per calendar day, from the extract registry. Nothing here is interpolated, and a day the route did not send is not drawn."
          actions={
            <div className="flex items-end gap-3">
              <Field label="From">
                {(props) => (
                  <Input
                    {...props}
                    type="date"
                    value={range.start}
                    onChange={(event) =>
                      setRange((current) => ({ ...current, start: event.target.value }))
                    }
                  />
                )}
              </Field>
              <Field label="To">
                {(props) => (
                  <Input
                    {...props}
                    type="date"
                    value={range.end}
                    onChange={(event) =>
                      setRange((current) => ({ ...current, end: event.target.value }))
                    }
                  />
                )}
              </Field>
            </div>
          }
        />
        {/* THE RANGES A PERSON ACTUALLY ASKS FOR — amendment of 2026-08-18. Two
            date pickers make « the last 30 days » four keyboard gestures and an
            arithmetic, on the tab that is opened more than any other. The
            sentence under them names the day they are counted back from, and
            whether that day came from this Datastream or from the calendar:
            without it a preset is a window whose end nobody can check. */}
        <div className="border-t border-divider-base px-5 py-3">
          <div className="flex flex-wrap items-center gap-2" data-testid="range-presets">
            {presetsFrom(anchor).map((preset) => (
              <Button
                key={preset.label}
                variant={
                  range.start === preset.start && range.end === preset.end
                    ? "secondary"
                    : "ghost"
                }
                size="xs"
                aria-pressed={range.start === preset.start && range.end === preset.end}
                onClick={() => setRange({ start: preset.start, end: preset.end })}
              >
                {preset.label}
              </Button>
            ))}
          </div>
          <p className="m-0 mt-2 text-caption text-text-secondary">
            {collectedAnchor
              ? `Counted back from ${anchor}, the most recent day this Datastream collected anything.`
              : `Counted back from ${anchor} — yesterday — because no day of this window collected anything.`}
          </p>
        </div>
      </Panel>
      )}

      {state.status === "loading" && (
        <Panel>
          <p className="m-0 text-ui text-text-secondary" role="status">
            Reading the day-by-day breakdown…
          </p>
        </Panel>
      )}

      {state.status === "error" && (
        <Status
          as="block"
          tone="error"
          title="The daily breakdown could not be read"
          action={
            <Button size="sm" variant="secondary" onClick={() => setReload((n) => n + 1)}>
              Retry
            </Button>
          }
        >
          {state.message}. No day is drawn — an invented grid would look like evidence.
        </Status>
      )}

      {state.status === "ok" && (
        <>
          {/* WHAT THE LAST RE-COLLECTION DID, and it stays until the next one.
              Never "queued" for a window the queue refused, never a count of
              days where the route counted windows. */}
          {outcome && (
            <p className="m-0 text-caption text-text-secondary" role="status">
              {outcome}
            </p>
          )}
          <DateBreakdownGrid
            breakdown={state.breakdown}
            fieldsOnly={pushedSource}
            onOpenRun={onOpenRun}
            onRepullDay={(target) => {
              setAsked([target]);
              repull.ask([target.date]);
            }}
            /* ONE REQUEST FOR N DAYS, and that is the whole gesture: the route
               has always taken an array and `useRepull` has always held one, so
               nothing new writes here — what arrives is a second way to fill it.
               The days are passed in the order the grid holds them, which is the
               order the confirmation names them in. */
            onRepullDays={(targets) => {
              setAsked(targets);
              repull.ask(targets.map((target) => target.date));
            }}
            reading={reading}
            day={day}
            onSelectDay={setDay}
            onRetryReading={() => setReload((n) => n + 1)}
            dimensionHistory={dimensionHistory}
          />
          {repull.days && (
            <RepullConfirmDialog
              open
              onOpenChange={() => repull.close()}
              days={repull.days}
              datastreamId={datastreamId}
              connector={state.breakdown.connector}
              sourceAccountRef={sourceAccountRef}
              what={repullScopeSentence(asked, state.breakdown.connector)}
              busy={repull.busy}
              error={repull.error}
              onConfirm={() => void repull.confirm()}
            />
          )}
        </>
      )}
    </>
  );
}
