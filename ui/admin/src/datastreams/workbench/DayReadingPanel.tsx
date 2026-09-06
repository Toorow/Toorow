/**
 * The three readings of one day — story 58.3, and the pairing of lot B2.
 *
 * EXTRACTED FROM `DateBreakdownGrid.tsx`, which was 1088 lines. The repository
 * refuses a file over 1000 and this lot adds a table, so the panel moved out
 * before the pairing moved in. What changed on the way is one branch and not the
 * rest.
 *
 * NOTHING IS FETCHED WHEN THE POSITION CHANGES. The route sends both sides of the
 * opened day in one answer, so switching is local state: the window, the day grid
 * and the opened day cannot move, because nothing is asked again.
 *
 * AND `SIDE BY SIDE` NOW PAIRS — amendment 12. It used to render the two readings
 * one after the other under a banner about `fact_daily_kpi.metric`, which is a
 * sentence about the MART and was never a sentence about these two relations. The
 * key between them is the active mapping's own `source → target`, the server lays
 * it, and when it cannot the server says which refusal it is — no mapping, no
 * bound field, no column both relations carry, no key column. The screen renders
 * that sentence and, having nothing to pair, falls back to the two readings one
 * after the other, which is then an answer rather than an excuse.
 *
 * AND AN ACTIVE CAPABILITY IS SEEN ON THE DATA — amendment 11, lot B3. « Un
 * inventaire de modules n'est pas une fonctionnalité : l'effet l'est. » So there
 * is no list of capabilities anywhere below: `currency_fx` is the line under the
 * amount, `country` is the field coloured in the header, `reporting_timezone` is
 * the day-boundary sentence over the reading it qualifies. Off, each one is
 * absent — no panel, no column, no space held open — and the decision is the
 * server's `capabilities` block, never a state read in a browser.
 */
import { useState } from "react";
import {
  Button, ChoiceGroup, EmptyState, Field, NativeSelect, Panel, PanelHeader, Status,
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow, TableScroll,
} from "../../ui";
import type { ReadingState } from "./DatastreamDailyBreakdown";
import PairedReading from "./PairedReading";
import type { DimensionHistory } from "./dimensionDebt";
import { lateDimensionSentenceForDay } from "./lateDimensionNote";
import { MarkedColumnName, MoneyLine, markOf, type ColumnMark } from "./readingMarks";
import type {
  DailyBreakdown, MoneyProvenance, ReadingCapability, ReadingPairing, ReadingSide,
} from "./breakdownTypes";

/** The three capabilities whose whole effect happens inside a reading. The others
 *  open a tab of their own (`tax_fees` → `Cost`, `placement_mapping` →
 *  `Placements`) and none of them is enumerated here. */
const CURRENCY_FX = "currency_fx";
const COUNTRY = "country";
const REPORTING_TIMEZONE = "reporting_timezone";

/** The three positions, in the order the story names them. */
const POSITIONS = ["collected", "mapped", "side_by_side"] as const;
type Position = (typeof POSITIONS)[number];

const POSITION_LABEL: Record<Position, string> = {
  collected: "Collected",
  mapped: "Mapped",
  side_by_side: "Side by side",
};

/**
 * What the reading says about money BEFORE any cell is read — story 58.7.
 *
 * Three states and three sentences, and the first two are the ones this product
 * keeps confusing. A provenance that came back EMPTY is a measurement: nothing
 * declares an amount in this reading. A provenance that never arrived is a
 * failure. And a reading that HAS amounts still says that no reporting currency is
 * named, because a column of figures with no currency beside it is read in the
 * reader's own.
 */
function MoneyNote({ provenance }: { provenance: MoneyProvenance | null | undefined }) {
  if (provenance === undefined || provenance === null) {
    return (
      <Status as="block" tone="warning" title="The money provenance could not be read">
        This reading arrived without the block that says which of its columns is an
        amount, so no rate and no rate date are shown under any of them. No cell is
        annotated — an annotation composed here would be evidence nobody sent.
      </Status>
    );
  }
  if (provenance.columns.length === 0) {
    return (
      <Status as="block" tone="neutral" title={provenance.title ?? undefined}>
        {/* The server's sentence, whole -- AND its heading. A fixed title here read
            "No monetary column in this reading" under every reason, including the
            two that mean the opposite: `fx_columns_absent_in_raw_zone` and
            `fx_columns_absent_in_relation` say the amount IS there and its rate is
            not. The heading contradicted the body it sat on. Both lines come from
            the one authority now. */}
        {provenance.message ?? provenance.reason}
      </Status>
    );
  }
  return (
    <Status as="block" tone="neutral" title="These amounts are in the source's currency">
      {provenance.reporting_currency_reason}
      {provenance.reporting_currency_gate
        ? ` A reporting currency is confirmed on ${provenance.reporting_currency_gate}, and this reading names none.`
        : ""}
    </Status>
  );
}

/** One reading, drawn as it came back — every value already masked server-side.
 *
 *  `money` and `mark` are the CAPABILITIES the server said are on. Neither is
 *  derived here: with `money` false not one line is drawn under a cell and the
 *  provenance note is not rendered at all, because a capability that is off must
 *  appear nowhere — « ni onglet, ni panneau, ni colonne ». */
function ReadingBlock({
  side,
  title,
  money,
  mark,
}: {
  side: ReadingSide;
  title: string;
  money: boolean;
  mark: ColumnMark | null;
}) {
  if (side.rows === null) {
    return (
      <Status as="block" tone="warning" title={`${title} is not available`}>
        {/* The server's sentence, whole. A screen that rewrote it would be a
            second answer to a question that already has one. */}
        {side.message ?? side.reason ?? "This reading could not be resolved."}
      </Status>
    );
  }
  if (side.rows.length === 0) {
    return (
      <EmptyState
        title={`No row landed for this day in ${side.relation}`}
        description={side.note_message ?? "This is not a failure: the relation was read and it answered."}
      />
    );
  }
  // Story 58.7. The SERVER's designation, read as a set — a column is an amount
  // because this list carries it and for no other reason. And since lot B3 the
  // set is empty unless `currency_fx` is on, so a reading of a Project that never
  // asked for the projection carries no annotation at all.
  const provenance = money ? side.money_provenance : undefined;
  const designated = new Set(
    money ? (provenance?.columns ?? []).map((entry) => entry.column) : [],
  );
  return (
    <div className="grid gap-2">
      <p className="m-0 text-caption text-text-secondary">
        {side.relation}
        {side.masked_fields.length > 0
          ? ` · ${side.masked_fields.length} of ${side.columns.length} fields are masked`
          : ""}
        {side.truncated ? " · this reading is truncated" : ""}
      </p>
      {side.note_message ? (
        <Status as="block" tone="neutral">{side.note_message}</Status>
      ) : null}
      {money ? <MoneyNote provenance={side.money_provenance} /> : null}
      <TableScroll label={`${title} rows`}>
        <Table>
          <TableHeader>
            <TableRow>
              {side.columns.map((column) => (
                <TableHead key={column}>
                  <MarkedColumnName name={column} mark={mark} />
                </TableHead>
              ))}
            </TableRow>
          </TableHeader>
          <TableBody>
            {side.rows.map((row, index) => {
              const money = provenance?.rows?.[index];
              return (
                <TableRow key={index} density="compact">
                  {side.columns.map((column) => (
                    <TableCell key={column} className="font-mono text-caption">
                      {row[column] === null || row[column] === undefined
                        ? "—"
                        : String(row[column])}
                      {/* A column the server did NOT designate is rendered exactly
                          as it was before this story: no line, no space held. */}
                      {designated.has(column) && money?.[column] ? (
                        <MoneyLine values={money[column]} />
                      ) : null}
                    </TableCell>
                  ))}
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </TableScroll>
    </div>
  );
}

/**
 * `Side by side`: the pairing when there is one, and the reason when there is not.
 *
 * THE FALLBACK IS THE TWO BLOCKS, AND IT SAYS SO WITH THE SERVER'S WORDS. A
 * refusal here is a state of the MAPPING — nothing bound, nothing both relations
 * carry, no key column — so it names the gesture that repairs it. That sentence
 * comes from the route, whole; the browser writes none of its own.
 */
function SideBySide({
  pairing,
  collected,
  mapped,
  money,
  marks,
}: {
  pairing: ReadingPairing | null | undefined;
  collected: ReadingSide;
  mapped: ReadingSide;
  money: boolean;
  marks: { collected: ColumnMark | null; mapped: ColumnMark | null };
}) {
  if (pairing && pairing.available) {
    return <PairedReading pairing={pairing} money={money} marks={marks} />;
  }
  return (
    <>
      <Status
        as="block"
        tone="warning"
        title="These two readings are not paired"
        data-testid="pairing-refused"
      >
        {/* A route that sent no pairing block at all and a pairing that refused are
            two different sentences — the first is a failure of the answer, the
            second is a measured state of the mapping. */}
        {pairing
          ? pairing.message ?? pairing.reason
          : "This reading arrived without the block that pairs the two sides, so no row can be shown carrying both values. The two readings are shown one after the other instead — a pairing composed here would be a join nobody sent."}
      </Status>
      <section data-testid="reading-collected">
        <h3 className="mb-2 text-label text-text-secondary">Collected</h3>
        <ReadingBlock
          side={collected}
          title="Collected"
          money={money}
          mark={marks.collected}
        />
      </section>
      <section data-testid="reading-mapped">
        <h3 className="mb-2 text-label text-text-secondary">Mapped</h3>
        <ReadingBlock side={mapped} title="Mapped" money={money} mark={marks.mapped} />
      </section>
    </>
  );
}

/**
 * What `reporting_timezone` does to a reading, and it is the only thing it may do.
 *
 * THE MODULE SIGNALS, IT NEVER RE-ALIGNS. That is a ratified rule of this product
 * — at DATE grain there is no hour to re-slice — so this draws a sentence over the
 * rows and not one value below it moves. The sentence is the server's, including
 * the Project's zone; a screen that named a zone would be naming a preference
 * nobody confirmed.
 */
function DayBoundarySignal({ capability }: { capability: ReadingCapability }) {
  return (
    <Status
      as="block"
      tone={capability.reason ? "neutral" : "warning"}
      title={
        capability.reason
          ? capability.title
          : `This day is the source's, not ${capability.project_reporting_timezone}`
      }
      data-testid="day-boundary-signal"
    >
      {capability.message}
    </Status>
  );
}

export default function DayReadingPanel({
  breakdown,
  reading,
  day,
  onSelectDay,
  onRetryReading,
  dimensionHistory = null,
}: {
  breakdown: DailyBreakdown;
  reading: ReadingState;
  day: string | null;
  onSelectDay?: (day: string) => void;
  onRetryReading?: () => void;
  /**
   * Why THIS day's reading is hollow, when it is — amendment 14 of the
   * 2026-08-11 review, delivered here 2026-08-31.
   *
   * The same server payload the grid reads, and a DIFFERENT sentence: the grid
   * explains the strip, this explains the one day open. A day that post-dates
   * every declaration gets nothing, because a note that always appears explains
   * nothing.
   */
  dimensionHistory?: DimensionHistory | null;
}) {
  const [position, setPosition] = useState<Position>("collected");
  const options = breakdown.view_mode?.available ?? [];
  const optionOf = (mode: string) => options.find((entry) => entry.mode === mode) ?? null;
  const collected = optionOf("collected");
  const mapped = optionOf("mapped");

  const choices = POSITIONS.map((value) => {
    const source =
      value === "side_by_side"
        ? [collected, mapped].find((entry) => entry && !entry.available) ?? null
        : optionOf(value);
    const available = value === "side_by_side"
      ? Boolean(collected?.available && mapped?.available)
      : Boolean(source?.available);
    return {
      value,
      label: POSITION_LABEL[value],
      // The relation when it can be read, the server's reason when it cannot.
      // Never a sentence written here.
      hint: available
        ? value === "side_by_side"
          ? `${collected?.relation} and ${mapped?.relation}`
          : source?.relation ?? undefined
        : source?.reason ?? undefined,
      disabled: !available,
    };
  });

  const active = choices.find((choice) => choice.value === position);
  const usable = active && !active.disabled ? position : null;

  // WHAT THE SERVER SAYS IS ON, AND NOTHING ELSE — lot B3. An absent key is a
  // capability that is off, and off appears nowhere. Nothing below reads a state,
  // compares one, or holds a column open for a key it did not find.
  const capabilities = (reading.status === "ok" ? reading.reading.capabilities : null) ?? [];
  const capabilityOf = (key: string) => capabilities.find((entry) => entry.key === key);
  const money = Boolean(capabilityOf(CURRENCY_FX));
  const country = capabilityOf(COUNTRY);
  const marks = {
    collected: markOf(country, "collected"),
    mapped: markOf(country, "mapped"),
  };
  const timezone = capabilityOf(REPORTING_TIMEZONE);
  /** Amendment 14: this day predates a column, or it does not. Never a maybe. */
  const lateNote = lateDimensionSentenceForDay(dimensionHistory, day);

  return (
    <Panel flush data-testid="day-reading">
      <PanelHeader
        title="Read one day"
        description="The rows exactly as they landed, and the rows after the mapping. Every value is masked on the server unless its field is classified as carrying no sensitive data."
        actions={
          breakdown.days.length > 0 ? (
            <Field label="Day">
              {(props) => (
                <NativeSelect
                  {...props}
                  value={day ?? ""}
                  onChange={(event) => onSelectDay?.(event.target.value)}
                >
                  {breakdown.days.map((entry) => (
                    <option key={entry.date} value={entry.date}>
                      {entry.date}
                    </option>
                  ))}
                </NativeSelect>
              )}
            </Field>
          ) : null
        }
      />
      <div className="grid gap-5 px-5 pb-5">
        <ChoiceGroup
          variant="card"
          aria-label="Reading"
          value={position}
          onValueChange={(value) => setPosition(value as Position)}
          choices={choices}
        />

        {usable === null ? (
          <Status as="block" tone="warning" title={`${POSITION_LABEL[position]} is not available`}>
            {/* The route's sentence or nothing at all. A default written here
                would be a reason nobody measured, shown as one. */}
            {active?.hint}
          </Status>
        ) : reading.status === "refused" ? (
          // BROKEN IS NOT EMPTY. This branch used to be folded into the one
          // below, so a `503` on the reading drew « No day is open » while the
          // selector showed the chosen date — a failed read presented as a day
          // nobody had asked for, which is the exact shape of a fabricated
          // absence.
          <Status
            as="block"
            tone="error"
            title={`The ${POSITION_LABEL[position]} reading could not be loaded`}
            action={
              onRetryReading ? (
                <Button size="sm" variant="secondary" onClick={onRetryReading}>
                  Retry
                </Button>
              ) : undefined
            }
          >
            {reading.message}. No row is drawn — an invented table would look like the day's data.
          </Status>
        ) : reading.status === "loading" ? (
          <p className="m-0 text-ui text-text-secondary" role="status">
            Reading {day}…
          </p>
        ) : reading.status !== "ok" ? (
          <EmptyState
            title="No day is open"
            description="Choose a day above to read its rows. Nothing is read until one is asked for."
          />
        ) : (
          <>
            {/* THE DAY IS WHAT THIS CAPABILITY QUALIFIES, so its sentence sits over
                the rows of the day and not in a list of modules. It appears only
                when the Project turned `reporting_timezone` on, and it moves
                nothing: the module signals a boundary and re-aligns no value. */}
            {timezone ? <DayBoundarySignal capability={timezone} /> : null}
            {/* WHY THIS DAY IS HOLLOW — amendment 14, and only for a day it is
                true of. Placed over the rows for the same reason the timezone
                signal is: it qualifies the whole reading below, and a person who
                reads an empty column has to be told before they read it that the
                column was never asked for on this day.

                It names no repair. The bounded, costed re-collection lives on
                `Processing` beside the change that creates the debt. */}
            {lateNote ? (
              <Status
                as="block"
                tone="neutral"
                title="This day predates one of the columns"
                data-testid="day-late-dimension-note"
              >
                {lateNote}
              </Status>
            ) : null}
            {/* `country` is ON and lands on no column of this reading. Said once,
                over the reading, because there is no header cell to colour — and
                the sentence names the gesture that would give it one. */}
            {country?.reason ? (
              <Status
                as="block"
                tone="neutral"
                title={country.title}
                data-testid="capability-nothing-to-mark"
              >
                {country.message}
              </Status>
            ) : null}
            {usable === "side_by_side" ? (
              <SideBySide
                pairing={reading.reading.pairing}
                collected={reading.reading.collected}
                mapped={reading.reading.mapped}
                money={money}
                marks={marks}
              />
            ) : usable === "collected" ? (
              <section data-testid="reading-collected">
                <h3 className="mb-2 text-label text-text-secondary">Collected</h3>
                <ReadingBlock
                  side={reading.reading.collected}
                  title="Collected"
                  money={money}
                  mark={marks.collected}
                />
              </section>
            ) : (
              <section data-testid="reading-mapped">
                <h3 className="mb-2 text-label text-text-secondary">Mapped</h3>
                <ReadingBlock
                  side={reading.reading.mapped}
                  title="Mapped"
                  money={money}
                  mark={marks.mapped}
                />
              </section>
            )}
          </>
        )}
      </div>
    </Panel>
  );
}
