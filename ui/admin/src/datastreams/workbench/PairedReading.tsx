/**
 * The raw value and the value it becomes, ON ONE ROW — amendment 12, lot B2.
 *
 * WHAT THIS REPLACES. Epic 58 asked for « une colonne par champ du mapping actif
 * avec sa valeur brute ET sa valeur mappée » and « les deux côte à côte avec la
 * transformation surlignée ». What shipped was two tables that do not join and a
 * banner explaining why — and the double pill, the thing 58.2 built so that « on
 * peut vérifier un mapping en regardant la donnée », survived attached to no value
 * at all. Here it sits on the column it names, over the two values it names.
 *
 * NOTHING IS PAIRED HERE. Every pair on this screen was decided by the server from
 * the ACTIVE MAPPING's own `source → target`, and the join between the rows ran on
 * the values the database returned, before masking. This component matches no name,
 * compares no string and composes no sentence: it draws what came back, including
 * the refusals, verbatim.
 *
 * AND A ROW THAT COULD NOT BE PAIRED SAYS WHICH SIDE IT CAME FROM. Two rows the
 * declared grain cannot tell apart, and a row whose counterpart is not in the other
 * reading, are two different facts; both are shown, neither is resolved by choosing.
 * A row silently dropped would make the reading look complete, which is the exact
 * shape of a fabricated pairing.
 *
 * THE READING IS BOUNDED AND SAYS SO. Each side arrives capped at
 * `collected_mapped_reader.MAX_ROWS`; a truncated side makes a truncated pairing,
 * because a counterpart cut off at the bound reads exactly like a counterpart that
 * never existed.
 *
 * AND AN ACTIVE CAPABILITY IS SEEN HERE TOO — amendment 11, lot B3. This is the
 * one table where a person compares a raw value with what it becomes, so the
 * effect of a capability cannot stop at its edge: an amount carries its currency,
 * its rate and its rate date under EACH half, and a field bound to the canonical
 * country dimension is coloured on the pill that names it. Off, neither appears —
 * no line, no badge, no space held open — and the decision is the server's.
 */
import {
  Badge, Status, Table, TableBody, TableCell, TableHead, TableHeader, TableRow, TableScroll,
  wireWord,
} from "../../ui";
import { cn } from "../../lib/cn";
import { MoneyLine, isMarked, type ColumnMark } from "./readingMarks";
import type { PairedCell, PairedRow, ReadingPairing } from "./breakdownTypes";

/** A value as it arrived — already masked server-side. `—` is the absence of a
 *  value, and it is the same glyph the two reading blocks already use for it. */
function valueText(value: unknown): string {
  return value === null || value === undefined ? "—" : String(value);
}

/**
 * What separates the two values, said in words as well as in colour.
 *
 * The highlight alone would make the transformation readable by hue only, which is
 * not readable at all for part of the estate — so the same fact is on the element
 * as its title and in the cell's accessible text.
 */
const TRANSFORM_TITLE: Record<string, string> = {
  changed: "The mapping changed this value",
  unchanged: "Passed through unchanged",
  incomparable: "There is no counterpart to compare this value with",
};

function transformOf(cell: PairedCell): keyof typeof TRANSFORM_TITLE {
  if (cell.changed === null) return "incomparable";
  return cell.changed ? "changed" : "unchanged";
}

/** One cell: the raw value above, the value it becomes below, and the difference
 *  drawn. The two are never on one line — at the width a dozen paired columns
 *  leave, one line truncates both, which is what 58.2 already measured of the
 *  header pills. */
function PairedValue({ cell, money }: { cell: PairedCell; money: boolean }) {
  const transform = transformOf(cell);
  // The provenance of EACH half, and only where the server sent one. `money` is
  // the capability; a cell whose side carries no amount, or whose row has no such
  // side, gets nothing — a rate shown under an absent value would be evidence
  // about a row that is not there.
  const raw = money ? cell.raw_money : null;
  const mapped = money ? cell.mapped_money : null;
  return (
    <span
      className="grid gap-0.5"
      data-testid="paired-cell"
      data-transform={transform}
      title={TRANSFORM_TITLE[transform]}
    >
      <span className="block truncate font-mono text-caption">{valueText(cell.raw)}</span>
      {raw ? <MoneyLine values={raw} /> : null}
      <span className="flex min-w-0 items-center gap-1">
        <span aria-hidden="true" className="text-caption text-text-secondary">
          →
        </span>
        <span
          className={cn(
            "block truncate font-mono text-caption",
            // MUTED when the mapping changed nothing, HIGHLIGHTED when it did —
            // the same rule the header pills already hold for an identity rename,
            // so a field that was really transformed stands out at a glance.
            transform === "changed"
              ? "rounded-small bg-primary-container px-1"
              : "text-text-secondary",
          )}
        >
          {valueText(cell.mapped)}
        </span>
      </span>
      {mapped ? <MoneyLine values={mapped} /> : null}
    </span>
  );
}

/** Where a row that is not paired came from, and the server's reason for it. */
const SIDE_LABEL: Record<string, string> = {
  collected: "Collected only",
  mapped: "Mapped only",
};

function RowOrigin({ row }: { row: PairedRow }) {
  if (row.side === null) {
    return (
      <span className="block truncate text-caption text-text-secondary">Paired</span>
    );
  }
  return (
    <span className="grid gap-0.5" data-testid="unpaired-row">
      <Badge outline className="max-w-full">
        <span className="truncate">{SIDE_LABEL[row.side] ?? wireWord(row.side)}</span>
      </Badge>
      {/* The server's sentence, whole. A wording written here would be a second
          answer to a question that already has one. */}
      <span className="block truncate text-caption text-text-secondary" title={row.message ?? undefined}>
        {row.message ?? row.reason}
      </span>
    </span>
  );
}

/**
 * The columns of the two relations that stayed on their own side.
 *
 * SHOWN, NOT SWALLOWED. A paired table that listed only what it could pair would
 * read as the whole reading, and a column missing from it would look like a column
 * the source stopped sending. Each one names its side and carries the server's
 * reason.
 */
function UnpairedColumns({ pairing }: { pairing: ReadingPairing }) {
  if (pairing.unpaired_columns.length === 0) return null;
  return (
    <Status as="block" tone="neutral" title="These columns are on one side only" data-testid="unpaired-columns">
      <ul className="m-0 flex list-none flex-wrap gap-x-4 gap-y-1 p-0">
        {pairing.unpaired_columns.map((column) => (
          <li key={`${column.side}:${column.name}`} className="text-caption text-text-secondary">
            {column.message ?? `${column.name} (${column.reason})`}
          </li>
        ))}
      </ul>
    </Status>
  );
}

export default function PairedReading({
  pairing,
  money = false,
  marks,
}: {
  pairing: ReadingPairing;
  /** `currency_fx` is active on this Project — the SERVER said so. */
  money?: boolean;
  /** The columns a capability lands on, per side. `undefined` is "no capability
   *  marks anything here", which is the answer on every live project today. */
  marks?: { collected: ColumnMark | null; mapped: ColumnMark | null };
}) {
  const sourceMarked = (name: string) => isMarked(marks?.collected, name);
  const targetMarked = (name: string) => isMarked(marks?.mapped, name);
  return (
    <div className="grid gap-2" data-testid="paired-reading">
      <p className="m-0 text-caption text-text-secondary">
        {pairing.paired_row_count} of {pairing.row_count} rows are paired through{" "}
        {pairing.key_columns.map((key) => key.source_field).join(", ")}
        {pairing.truncated ? " · this reading is truncated" : ""}
        {pairing.bounded_at ? ` · at most ${pairing.bounded_at} rows per side` : ""}
      </p>
      <UnpairedColumns pairing={pairing} />
      <TableScroll label="Collected and mapped, paired">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="w-1/6">Row</TableHead>
              {pairing.columns.map((column) => (
                <TableHead key={`${column.source_field}:${column.target_field}`}>
                  {/* THE DOUBLE PILL, ON THE COLUMN IT NAMES — and now over the two
                      values it names. The raw name the source reports sits above the
                      canonical name it becomes; without it nobody can check a
                      mapping by looking at the data, which is the whole sentence of
                      58.2. */}
                  <span className="grid min-w-0 gap-1">
                    {/* THE CAPABILITY COLOURS THE PILL THAT NAMES THE FIELD — lot
                        B3. Not a fourth chip above it and not a banner over the
                        table: the pill itself, where a person is already looking to
                        check a mapping. The sentence rides on its title, because a
                        mark carried by hue alone is not carried at all. */}
                    <Badge
                      outline={!sourceMarked(column.source_field)}
                      tone={sourceMarked(column.source_field) ? "info" : "neutral"}
                      className="max-w-full"
                      data-testid={
                        sourceMarked(column.source_field)
                          ? "capability-marked-field"
                          : undefined
                      }
                      title={
                        sourceMarked(column.source_field)
                          ? `${marks?.collected?.title} · ${marks?.collected?.message ?? ""}`
                          : column.source_field
                      }
                    >
                      <span className="truncate">{column.source_field}</span>
                    </Badge>
                    <Badge
                      outline={
                        column.source_field === column.target_field
                        && !targetMarked(column.target_field)
                      }
                      tone={targetMarked(column.target_field) ? "info" : "neutral"}
                      className="max-w-full"
                      data-testid={
                        targetMarked(column.target_field)
                          ? "capability-marked-field"
                          : undefined
                      }
                      title={
                        targetMarked(column.target_field)
                          ? `${marks?.mapped?.title} · ${marks?.mapped?.message ?? ""}`
                          : column.target_field
                      }
                    >
                      <span className="truncate">{column.target_field}</span>
                    </Badge>
                    {column.is_key_column ? (
                      <span className="text-caption text-text-secondary">Key</span>
                    ) : null}
                  </span>
                </TableHead>
              ))}
            </TableRow>
          </TableHeader>
          <TableBody>
            {pairing.rows.map((row, index) => (
              <TableRow key={index} density="compact" data-testid="paired-row">
                <TableCell>
                  <RowOrigin row={row} />
                </TableCell>
                {row.cells.map((cell) => (
                  <TableCell key={`${cell.source_field}:${cell.target_field}`}>
                    <PairedValue cell={cell} money={money} />
                  </TableCell>
                ))}
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </TableScroll>
    </div>
  );
}
