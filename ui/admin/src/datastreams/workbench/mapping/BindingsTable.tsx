/**
 * The bindings table — one row per source column, and nothing cut off.
 *
 * TWO FINDINGS OF THE 2026-08-12 VISUAL REVIEW MEET HERE.
 *
 * D-1 — thirteen columns, five of which said the same thing on all ten rows.
 * The readings now decide for themselves whether they are a column or a line of
 * a per-row detail (`bindingReadings.tsx`), and what stays visible on every row
 * is the subject of the screen: the source identity, the concept the column
 * becomes, its binding state, and the acts.
 *
 * D-2 — `Split…` was cut off at the right edge on every row, at 1600px. Three
 * controls in two columns (`Inclusion`, then a `Join` checkbox beside a `Split…`
 * button) cost about 270px of a table that had none to give. They are one menu
 * on the row now, and the menu carries a whole sentence per act instead of the
 * two words a cell had room for. The geometry is MEASURED — the table is mounted
 * on the component sheet and read by `scripts/measure_component_sheet.py`
 * against a real browser, exactly as the day grid is; before this, nothing
 * measured it, which is why it could overflow with nothing going red.
 *
 * Extracted from `WorkbenchMappingPage.tsx`, which was at 922 lines of a
 * 1 000-line ceiling. Nothing moved changed meaning.
 */
import { useState, type ReactNode } from "react";
import { ChevronDownIcon, ChevronRightIcon, MoreHorizontalIcon } from "lucide-react";
import {
  Button, DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel,
  DropdownMenuSeparator, DropdownMenuTrigger, SortableHead, Status,
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow, TableScroll,
  sortRows, useTableSort,
} from "../../../ui";
import { text, titleCase } from "../evidence";
import { bindingStatus, bindingTone } from "./mappingModel";
import {
  ALL_READINGS, CONFIDENCE, READINGS_AFTER_TARGET, READINGS_BEFORE_TARGET,
  readingContent, type BindingReading, type ReadingContext,
} from "./bindingReadings";

/**
 * The acts stay on the trailing edge whatever the table does behind them.
 *
 * The ground is not decoration: a sticky cell with no ground of its own lets the
 * scrolled columns read straight through it. The header keeps the header band's
 * tint and the body keeps the panel's, so the pinned column does not announce
 * itself as a different object — only the hairline says it is pinned.
 */
const ACTS_PINNED = "sticky right-0 border-l border-divider-base";
const ACTS_PINNED_HEAD = `${ACTS_PINNED} bg-background-light`;
const ACTS_PINNED_CELL = `${ACTS_PINNED} bg-surface-light`;

export default function BindingsTable({
  rows,
  folded,
  editable,
  excludedOf,
  joinSelection,
  onToggleExclusion,
  onToggleJoin,
  onSplit,
  targetCell,
  targetSortValue,
}: {
  rows: ReadingContext[];
  /** The reading keys that say the same thing on every row of `rows`. */
  folded: Set<string>;
  editable: boolean;
  excludedOf: (row: ReadingContext) => boolean;
  joinSelection: string[];
  onToggleExclusion: (row: ReadingContext) => void;
  onToggleJoin: (id: string, wanted: boolean) => void;
  onSplit: (id: string) => void;
  /** The concept cell, built by the caller: it needs the project catalogue, the
   *  claimed concepts and the pending picks, none of which is the table's
   *  business. Handed in as a render so the component sheet can mount the very
   *  same cell without a project behind it. */
  targetCell: (row: ReadingContext, excluded: boolean) => ReactNode;
  /**
   * What the concept column is ORDERED by — the name a person reads, which this
   * table cannot compose: the cell is built by the caller because it needs the
   * project catalogue and the picks not yet confirmed. Sorting by the registry
   * identity while showing the name would order the rows by a string that is not
   * on screen. Absent, the column simply does not sort.
   */
  targetSortValue?: (row: ReadingContext) => string;
}) {
  const [unfolded, setUnfolded] = useState<Record<string, boolean>>({});
  /**
   * THE ORDER, AND WHAT IT COVERS. Every binding of the selected version is on
   * this page — the tab read carries them all and there is no cursor — so the
   * sort is over the whole collection and no page-scope note is owed. Mounting
   * `SortScopeNote` here would promise a limitation this table does not have.
   *
   * Only the three readings that never fold can carry it: what a version calls a
   * column, what it becomes, and whether the binding is settled. A sortable
   * header on a reading that disappears when it is uniform would be an order
   * that vanishes with the column it names.
   */
  const { sort, toggleSort } = useTableSort(null);
  const ordered = sortRows(rows, sort, (row, key) => {
    if (key === "identity") return text(row.field.source_identity ?? row.field.field_id, "");
    if (key === "target") return targetSortValue ? targetSortValue(row) : null;
    if (key === "binding") return bindingStatus(row.field);
    return null;
  });
  const before = READINGS_BEFORE_TARGET.filter((reading) => !folded.has(reading.key));
  const after = READINGS_AFTER_TARGET.filter((reading) => !folded.has(reading.key));
  const detail = ALL_READINGS.filter((reading) => folded.has(reading.key));
  // Identity + concept + binding, plus the acts when the version is editable.
  const columnCount = 3 + before.length + after.length + (editable ? 1 : 0);

  const head = (reading: BindingReading) => <TableHead key={reading.key}>{reading.label}</TableHead>;

  return (
    <TableScroll label="Physical field bindings">
      <Table>
        <TableHeader>
          <TableRow>
            <SortableHead sortKey="identity" sort={sort} onSort={toggleSort}>
              Source identity
            </SortableHead>
            {before.map(head)}
            {targetSortValue ? (
              <SortableHead sortKey="target" sort={sort} onSort={toggleSort}>
                Canonical / MDM target
              </SortableHead>
            ) : (
              <TableHead>Canonical / MDM target</TableHead>
            )}
            {after.map(head)}
            <SortableHead sortKey="binding" sort={sort} onSort={toggleSort}>
              Binding
            </SortableHead>
            {/* ONE column for the three acts — D-2. `Inclusion` and
                `Join / split` were two, and the second one left the frame.
                PINNED to the trailing edge: the folded table fits at 1128px, but
                a version whose readings all differ puts every one of them back
                as a column and the table scrolls. What may scroll out of view is
                a reading; what may never is the way to act on the row. */}
            {editable && <TableHead className={ACTS_PINNED_HEAD}>Acts</TableHead>}
          </TableRow>
        </TableHeader>
        <TableBody>
          {ordered.map((row) => {
            const id = row.id;
            const excluded = excludedOf(row);
            const open = unfolded[id] === true;
            const picked = joinSelection.includes(id);
            const status = bindingStatus(row.field);
            return [
              <TableRow key={id}>
                <TableCell className="font-mono">
                  <div className="flex items-center gap-2">
                    {/* The chevron exists only where there is something behind
                        it. A control that opens an empty drawer is worse than no
                        control: it teaches that the drawer is empty. */}
                    {detail.length > 0 && (
                      <Button
                        variant="ghost"
                        size="icon-xs"
                        aria-expanded={open}
                        aria-label={`Readings folded on ${id}`}
                        data-testid={`unfold-${id}`}
                        onClick={() => setUnfolded((current) => ({ ...current, [id]: !open }))}
                      >
                        {open ? <ChevronDownIcon /> : <ChevronRightIcon />}
                      </Button>
                    )}
                    {text(row.field.source_identity ?? row.field.field_id)}
                  </div>
                </TableCell>
                {before.map((reading) => (
                  <TableCell
                    key={reading.key}
                    className={reading.className}
                    data-testid={reading.testId?.(id)}
                  >
                    {readingContent(reading, row)}
                  </TableCell>
                ))}
                <TableCell>{targetCell(row, excluded)}</TableCell>
                {after.map((reading) => (
                  <TableCell
                    key={reading.key}
                    className={reading.className}
                    data-testid={reading.testId?.(id)}
                  >
                    {readingContent(reading, row)}
                  </TableCell>
                ))}
                <TableCell>
                  <Status tone={bindingTone(status)}>{titleCase(status)}</Status>
                  {!folded.has(CONFIDENCE.key) && (
                    <span className="block text-caption text-text-secondary">
                      {readingContent(CONFIDENCE, row)}
                    </span>
                  )}
                </TableCell>
                {editable && (
                  <TableCell className={ACTS_PINNED_CELL}>
                    <div className="flex items-center gap-2">
                      <DropdownMenu>
                        <DropdownMenuTrigger asChild>
                          <Button
                            variant="secondary"
                            size="icon-sm"
                            aria-label={`What to do with ${id}`}
                            data-testid={`acts-${id}`}
                          >
                            <MoreHorizontalIcon />
                          </Button>
                        </DropdownMenuTrigger>
                        <DropdownMenuContent align="end" className="w-72">
                          <DropdownMenuItem
                            data-testid={`toggle-exclusion-${id}`}
                            onSelect={() => onToggleExclusion(row)}
                          >
                            {excluded ? "Let this column land again" : "Stop this column landing"}
                          </DropdownMenuItem>
                          <DropdownMenuSeparator />
                          {/* An excluded column never lands, so it can feed
                              neither a join nor a split: offering the control
                              would promise a value built out of nothing, and the
                              server refuses it (`treatment_source_excluded`). */}
                          {excluded ? (
                            <DropdownMenuLabel>Excluded columns feed nothing</DropdownMenuLabel>
                          ) : (
                            <>
                              <DropdownMenuItem
                                data-testid={`join-pick-${id}`}
                                onSelect={() => onToggleJoin(id, !picked)}
                              >
                                {picked
                                  ? "Take this column out of the join"
                                  : "Join this column with others"}
                              </DropdownMenuItem>
                              <DropdownMenuItem
                                data-testid={`split-${id}`}
                                onSelect={() => onSplit(id)}
                              >
                                Split this column into several concepts…
                              </DropdownMenuItem>
                            </>
                          )}
                        </DropdownMenuContent>
                      </DropdownMenu>
                      {/* A pick that is only legible inside a closed menu is a
                          state nobody can see. It costs one word on the rows
                          that carry it and nothing on the rest. */}
                      {picked && (
                        <span
                          className="text-caption text-text-secondary"
                          data-testid={`join-picked-${id}`}
                        >
                          Joining
                        </span>
                      )}
                    </div>
                  </TableCell>
                )}
              </TableRow>,
              open && detail.length > 0 ? (
                <TableRow key={`${id}-detail`} data-testid={`detail-${id}`}>
                  <TableCell colSpan={columnCount}>
                    <dl className="grid gap-x-8 gap-y-2 sm:grid-cols-2 lg:grid-cols-3">
                      {detail.map((reading) => (
                        <div key={reading.key} className="grid gap-0.5">
                          <dt className="text-label text-text-secondary">{reading.label}</dt>
                          <dd
                            className={reading.className ?? "text-ui"}
                            data-testid={reading.testId?.(id)}
                          >
                            {readingContent(reading, row)}
                          </dd>
                        </div>
                      ))}
                    </dl>
                  </TableCell>
                </TableRow>
              ) : null,
            ];
          })}
        </TableBody>
      </Table>
    </TableScroll>
  );
}
