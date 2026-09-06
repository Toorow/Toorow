/**
 * THE HEADER SORTS, AND IT SAYS WHAT IT SORTED.
 *
 * `aria-sort` appeared ZERO times in the console before `ui/SortableHeader`. A
 * table whose order changes under a click and announces nothing leaves a
 * screen-reader user with a reshuffled list and no way to know which column
 * carries the order — which is worse than a table that never sorts.
 *
 * The second half is the one a test is actually needed for. `sortRows` reorders
 * the rows it was HANDED, and on a collection with a cursor those rows are one
 * page. A silent sort would let a reader conclude that the smallest value in the
 * collection is the smallest value on screen. `SortScopeNote` is the sentence
 * that closes it, and it is asserted here on the real collection layout rather
 * than on the primitive, because the mistake is only possible once a cursor
 * exists.
 */
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import {
  compareSortValues,
  nextSort,
  SortableHead,
  sortRows,
  useTableSort,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "../ui";
import { DataCollectionLayout, type DataColumn } from "../data/DataCollectionLayout";
import type { DataSurfaceEnvelope, DataSurfaceItem } from "../data/dataSurface";

// ---------------------------------------------------------------------------
// The primitive
// ---------------------------------------------------------------------------

const PEOPLE = [
  { id: "c", name: "Carol", runs: 2 },
  { id: "a", name: "Alice", runs: 30 },
  { id: "b", name: "Bob", runs: null as number | null },
];

function SortableTable() {
  const { sort, toggleSort } = useTableSort();
  const rows = sortRows(PEOPLE, sort, (person, key) =>
    key === "name" ? person.name : person.runs,
  );
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <SortableHead sortKey="name" sort={sort} onSort={toggleSort}>Name</SortableHead>
          <SortableHead sortKey="runs" sort={sort} onSort={toggleSort} numeric>Runs</SortableHead>
          <TableHead>Notes</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map((person) => (
          <TableRow key={person.id}>
            <TableCell>{person.name}</TableCell>
            <TableCell numeric>{person.runs ?? "—"}</TableCell>
            <TableCell>—</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

const shownNames = () =>
  screen
    .getAllByRole("row")
    .slice(1)
    .map((row) => within(row).getAllByRole("cell")[0].textContent);

describe("a sortable column header", () => {
  it("declares itself sortable before anyone clicks it", () => {
    render(<SortableTable />);
    const [name, runs, notes] = screen.getAllByRole("columnheader");
    // `none` says "this column CAN be sorted and currently is not" — a different
    // statement from a column that never sorts, which carries no attribute.
    expect(name).toHaveAttribute("aria-sort", "none");
    expect(runs).toHaveAttribute("aria-sort", "none");
    expect(notes).not.toHaveAttribute("aria-sort");
    // And it is reachable: a `<button>`, not a click handler on the `<th>`.
    expect(within(name).getByRole("button", { name: /name/i })).toBeInTheDocument();
    expect(within(notes).queryByRole("button")).toBeNull();
  });

  it("cycles ascending then descending, and writes it into aria-sort", async () => {
    render(<SortableTable />);
    const header = () => screen.getAllByRole("columnheader")[0];

    await userEvent.click(within(header()).getByRole("button"));
    expect(header()).toHaveAttribute("aria-sort", "ascending");
    expect(shownNames()).toEqual(["Alice", "Bob", "Carol"]);

    await userEvent.click(within(header()).getByRole("button"));
    expect(header()).toHaveAttribute("aria-sort", "descending");
    expect(shownNames()).toEqual(["Carol", "Bob", "Alice"]);

    // And back — two states, not three. The "order it arrived in" is the
    // server's and cannot be restored by a click.
    await userEvent.click(within(header()).getByRole("button"));
    expect(header()).toHaveAttribute("aria-sort", "ascending");
  });

  it("carries the order on ONE column at a time", async () => {
    render(<SortableTable />);
    await userEvent.click(within(screen.getAllByRole("columnheader")[0]).getByRole("button"));
    await userEvent.click(within(screen.getAllByRole("columnheader")[1]).getByRole("button"));

    const [name, runs] = screen.getAllByRole("columnheader");
    expect(name).toHaveAttribute("aria-sort", "none");
    expect(runs).toHaveAttribute("aria-sort", "ascending");
    // A new column starts ascending rather than inheriting the previous
    // direction, which would silently reverse a column nobody reversed.
    expect(shownNames()).toEqual(["Carol", "Alice", "Bob"]);
  });

  it("sorts numbers as numbers and puts an absence last in BOTH directions", () => {
    expect(compareSortValues(2, 30)).toBeLessThan(0);
    expect(compareSortValues(null, 2)).toBeGreaterThan(0);
    expect(compareSortValues(2, null)).toBeLessThan(0);
    const ascending = sortRows(PEOPLE, { key: "runs", direction: "ascending" }, (p) => p.runs);
    const descending = sortRows(PEOPLE, { key: "runs", direction: "descending" }, (p) => p.runs);
    expect(ascending.map((p) => p.id)).toEqual(["c", "a", "b"]);
    expect(descending.map((p) => p.id)).toEqual(["a", "c", "b"]);
  });

  it("never mutates the rows it was given, and keeps ties in the server's order", () => {
    const source = [{ k: "x", v: 1 }, { k: "y", v: 1 }, { k: "z", v: 0 }];
    const sorted = sortRows(source, { key: "v", direction: "ascending" }, (row) => row.v);
    expect(source.map((row) => row.k)).toEqual(["x", "y", "z"]);
    expect(sorted.map((row) => row.k)).toEqual(["z", "x", "y"]);
    expect(sortRows(source, null, (row) => row.v)).not.toBe(source);
  });

  it("a new column always starts ascending", () => {
    expect(nextSort(null, "name")).toEqual({ key: "name", direction: "ascending" });
    expect(nextSort({ key: "runs", direction: "descending" }, "name")).toEqual({
      key: "name",
      direction: "ascending",
    });
  });
});

// ---------------------------------------------------------------------------
// The adoption: what a Data collection promises when it holds one page
// ---------------------------------------------------------------------------

function item(id: string, label: string, freshness: string): DataSurfaceItem {
  return {
    object_ref: { object_type: "source_account", id },
    label,
    states: { freshness },
    evidence: {},
    evidence_as_of: null,
    links: {},
  };
}

const ITEMS = [
  item("s1", "Zulu", "stale"),
  item("s2", "Alpha", "active"),
  item("s3", "Mike", "failed"),
];

const COLUMNS: readonly DataColumn[] = [
  { key: "name", label: "Name", render: (row) => row.label, sortValue: (row) => row.label },
  { key: "freshness", label: "Freshness", render: (row) => row.states.freshness },
];

function envelope(nextCursor: string | null): DataSurfaceEnvelope {
  return {
    schema_version: "data-sources.v1",
    project_ref: { object_type: "project", id: "proj_EXAMPLE" },
    generated_at: "2026-08-17T09:00:00Z",
    evidence_as_of: "2026-08-17T09:00:00Z",
    items: ITEMS,
    unavailable_reasons: [],
    allowed_actions: [],
    total: 12,
    next_cursor: nextCursor,
  };
}

function renderCollection(nextCursor: string | null) {
  return render(
    <DataCollectionLayout
      title="Sources"
      description="Every source account of this Project."
      emptyTitle="No Source yet"
      emptyDescription="Authorize a connector to record one."
      state={{ status: "ready", envelope: envelope(nextCursor) }}
      reload={() => {}}
      columns={COLUMNS}
      onNextPage={() => {}}
    />,
  );
}

describe("a Data collection sorts the page it holds, and says so", () => {
  it("makes a state column sortable without the screen declaring anything", async () => {
    // The default is not a guess: a Data column is keyed by the state it
    // renders, so `freshness` sorts by the SENTENCE the reader can see —
    // "Active", "Failed", "Stale".
    renderCollection(null);
    const freshness = screen.getAllByRole("columnheader")[1];
    expect(freshness).toHaveAttribute("aria-sort", "none");

    await userEvent.click(within(freshness).getByRole("button"));
    expect(screen.getAllByRole("columnheader")[1]).toHaveAttribute("aria-sort", "ascending");
    const labels = screen
      .getAllByRole("row")
      .slice(1)
      .map((row) => within(row).getAllByRole("cell")[0].textContent);
    expect(labels).toEqual(["Alpha", "Mike", "Zulu"]);
  });

  it("says the order covers the page when a cursor holds the rest", () => {
    renderCollection("cursor_EXAMPLE");
    expect(screen.getByTestId("page-sort-note")).toHaveTextContent(/rows on this page only/i);
    expect(screen.getByTestId("page-sort-note")).toHaveTextContent(
      /rest of the collection keeps the order/i,
    );
  });

  it("stays silent on the last page, where the rows ARE the collection", () => {
    renderCollection(null);
    // A warning about a mistake nobody can make is noise, and noise is how a
    // real warning stops being read.
    expect(screen.queryByTestId("page-sort-note")).toBeNull();
  });
});
