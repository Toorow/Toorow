/**
 * A column header that sorts, and says so to a screen reader.
 *
 * WHY IT EXISTS. `aria-sort` appeared ZERO times in the console. Every table
 * arrived in whatever order the server composed it, and the reader's only way to
 * find the oldest run or the largest table was to read every row. Where a screen
 * did offer an order, it offered it as a select beside the table — which orders
 * the data but never tells assistive technology which column carries the order.
 *
 * WHY IT IS NOT IN `components/ui/table.tsx`. That file is the shadcn source,
 * adapted to the v3 mockup and shared by thirty screens. Sorting is a behaviour
 * with state, and putting it there would mean every `TableHead` in the console
 * grows a `sortKey` it does not use. `SortableHead` composes `TableHead` instead,
 * so a table that does not sort is exactly the table it was, and the geometry
 * still has one owner.
 *
 * TWO STATES, NOT THREE. Clicking cycles ascending → descending → ascending. The
 * third state most libraries offer — "back to the order it arrived in" — is a
 * promise this component cannot keep on a paged collection, because the order it
 * arrived in is the server's and re-fetching it is not a click on a header.
 *
 * IT SORTS THE PAGE, AND THE SCREEN HAS TO SAY SO. `sortRows` reorders the rows
 * it was given and nothing else. On a collection with a cursor, the rows it was
 * given are one page — so a header that sorted silently would let a reader
 * conclude the smallest value in the collection is the smallest value on screen.
 * `PAGE_SORT_NOTE` is the sentence that closes that, and `SortScopeNote` renders
 * it; every adopter with a cursor mounts one.
 */
import * as React from "react";

import { cn } from "../lib/cn";
import { TableHead } from "../components/ui/table";

/** The two values `aria-sort` takes when a column carries the order. */
export type SortDirection = "ascending" | "descending";

export interface SortState {
  /** The column's key, not its label: a label is copy and can be translated. */
  key: string;
  direction: SortDirection;
}

/** What the next click produces. A new column always starts ascending. */
export function nextSort(current: SortState | null, key: string): SortState {
  if (current && current.key === key) {
    return { key, direction: current.direction === "ascending" ? "descending" : "ascending" };
  }
  return { key, direction: "ascending" };
}

/** The sort a table holds, and the one gesture that changes it. */
export function useTableSort(initial: SortState | null = null) {
  const [sort, setSort] = React.useState<SortState | null>(initial);
  const toggleSort = React.useCallback(
    (key: string) => setSort((current) => nextSort(current, key)),
    [],
  );
  return { sort, toggleSort, setSort };
}

/** What a cell can be sorted by. Anything else has to be reduced to one of these. */
export type SortValue = string | number | boolean | null | undefined;

/** Nothing to compare: `null`, `undefined`, or the empty string. */
function isAbsent(value: SortValue): boolean {
  return value === null || value === undefined || value === "";
}

/**
 * Compare two cells for an ascending order.
 *
 * ABSENT SORTS LAST here, and `sortRows` keeps it last in the descending order
 * too — see below. Numbers compare as numbers, so 2 comes before 30 rather than
 * after it, and strings compare with `numeric: true` so `run-2` comes before
 * `run-10`.
 */
export function compareSortValues(a: SortValue, b: SortValue): number {
  if (isAbsent(a) && isAbsent(b)) return 0;
  if (isAbsent(a)) return 1;
  if (isAbsent(b)) return -1;
  if (typeof a === "number" && typeof b === "number") return a - b;
  if (typeof a === "boolean" && typeof b === "boolean") return Number(a) - Number(b);
  return String(a).localeCompare(String(b), "en", { numeric: true, sensitivity: "base" });
}

/**
 * The rows, reordered. A stable sort over a copy — the caller's array is never
 * mutated, and two rows that compare equal keep the order the server gave them.
 *
 * THE ABSENCES ARE TAKEN OUT OF THE FLIP. A row with no value stays at the
 * bottom whichever way the arrow points: "no value" is not the smallest value,
 * and reversing a column of run durations should not promote the runs that never
 * started to the top. Applying the direction's sign to the whole comparison did
 * exactly that, and it is why the absence rule is enforced here rather than left
 * inside `compareSortValues`.
 */
export function sortRows<T>(
  rows: readonly T[],
  sort: SortState | null,
  valueOf: (row: T, key: string) => SortValue,
): T[] {
  if (!sort) return [...rows];
  const sign = sort.direction === "ascending" ? 1 : -1;
  return [...rows]
    .map((row, index) => ({ row, index }))
    .sort((left, right) => {
      const a = valueOf(left.row, sort.key);
      const b = valueOf(right.row, sort.key);
      if (isAbsent(a) || isAbsent(b)) {
        if (isAbsent(a) && isAbsent(b)) return left.index - right.index;
        return isAbsent(a) ? 1 : -1;
      }
      const compared = compareSortValues(a, b);
      if (compared !== 0) return sign * compared;
      return left.index - right.index;
    })
    .map((entry) => entry.row);
}

/** What a sort on a paged collection is actually promising. */
export const PAGE_SORT_NOTE =
  "Sorting reorders the rows on this page only. The rest of the collection keeps the order it was read in.";

/** The sentence above, where the reader can see it before they click. */
export function SortScopeNote({ className }: { className?: string }) {
  return (
    <p className={cn("m-0 text-caption text-text-secondary", className)} data-testid="page-sort-note">
      {PAGE_SORT_NOTE}
    </p>
  );
}

/** The arrow. Decoration only — `aria-sort` on the cell carries the meaning. */
function SortMark({ direction }: { direction: SortDirection | null }) {
  return (
    <span
      aria-hidden
      className={cn(
        "ml-1.5 inline-block leading-none transition-colors",
        direction ? "text-text" : "text-text-secondary opacity-40",
      )}
    >
      {direction === "ascending" ? "↑" : direction === "descending" ? "↓" : "↕"}
    </span>
  );
}

export interface SortableHeadProps extends React.ComponentProps<"th"> {
  /** The key this column sorts by. It is what `sortRows` is handed. */
  sortKey: string;
  /** The table's current order, or `null` while it is the one the server gave. */
  sort: SortState | null;
  /** Called with this column's key. `useTableSort().toggleSort` is the usual one. */
  onSort: (key: string) => void;
  numeric?: boolean;
}

/**
 * The header cell. A `<button>` inside the `<th>`, never a click handler on the
 * cell itself: a `th` with an `onClick` is unreachable by keyboard and announces
 * nothing, which is the pattern this component exists to stop being rewritten.
 */
export function SortableHead({
  sortKey,
  sort,
  onSort,
  numeric,
  className,
  children,
  ...props
}: SortableHeadProps) {
  const active = sort?.key === sortKey ? sort.direction : null;
  return (
    <TableHead
      {...props}
      numeric={numeric}
      // `none` and not "absent": the column CAN be sorted and currently is not,
      // which is a different statement from a column that never sorts.
      aria-sort={active ?? "none"}
      data-sortable="true"
      className={cn("p-0", className)}
    >
      <button
        type="button"
        onClick={() => onSort(sortKey)}
        className={cn(
          "flex h-full w-full cursor-pointer items-center px-4.5 py-0 text-caption font-label tracking-[0.035em] uppercase text-text-secondary transition-colors hover:text-text focus-visible:outline-3 focus-visible:-outline-offset-2 focus-visible:outline-focus",
          numeric ? "justify-end" : "justify-start",
        )}
      >
        <span>{children}</span>
        <SortMark direction={active} />
      </button>
    </TableHead>
  );
}
