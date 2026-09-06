import { useMemo, type ReactNode } from "react";
import type { DataSurfaceItem, DataSurfaceState } from "./dataSurface";
import {
  Button,
  EmptyState,
  PageHeader,
  Pager,
  Panel,
  PanelHeader,
  SortableHead,
  SortScopeNote,
  sortRows,
  Stack,
  Status,
  StatusLegend,
  type StatusLegendEntry,
  stateLabel,
  stateTone,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
  TableScroll,
  useTableSort,
  type SortValue,
  type Tone,
  formatTimestamp,
} from "../ui";

export interface DataColumn {
  key: string;
  label: string;
  className?: string;
  render: (item: DataSurfaceItem) => ReactNode;
  /**
   * What this column sorts BY, when the default below cannot say.
   *
   * The default is not a guess: a Data column is keyed by the state it renders
   * (`{ key: "freshness", render: (item) => <StateValue value={item.states.freshness} /> }`),
   * so a column whose key names a state of the row sorts by that state's
   * SENTENCE — the words on screen, not the stored token, because a reader
   * ordering a column expects the order they can see. A column that renders
   * something else — a count, a name, a time — names its own value here.
   */
  sortValue?: (item: DataSurfaceItem) => SortValue;
  /** A column that must not sort even though its key names a state. */
  sortable?: false;
}

/** What the column above is ordered by, for one row. */
function columnSortValue(column: DataColumn, item: DataSurfaceItem): SortValue {
  if (column.sortValue) return column.sortValue(item);
  const state = item.states?.[column.key];
  return state === undefined ? null : stateLabel(state);
}

/** True when this column can be ordered at all over the rows actually loaded. */
function columnSorts(column: DataColumn, items: readonly DataSurfaceItem[]): boolean {
  if (column.sortable === false) return false;
  if (column.sortValue) return true;
  return items.some((item) => item.states?.[column.key] !== undefined);
}

/**
 * The tone and the sentence of a state — the console's, not this screen's.
 *
 * Both used to be declared here, in a `switch` and a table that five other
 * screens imported through this file. They are `ui/stateVocabulary` now, beside
 * the five other maps that said the same words: `active` was green in four
 * places by coincidence and nothing would have caught a fifth spelling it amber.
 * The two names stay exported because `Sources`, `Imports`, `ConnectorsCatalog`,
 * `DataWorkspace`, `DataObjectWorkbench` and `BusinessContextPanel` read them
 * from here, and a rename would be churn with no reader behind it.
 */
export const toneForState = (state: string | undefined): Tone => stateTone(state);
export const labelForState = (state: string | undefined): string => stateLabel(state);

/**
 * THE KEY TO THIS COLLECTION'S MARKS — only the tones the loaded rows actually
 * carry (`console-presentation.md` §3).
 *
 * Derived from the rows rather than declared: a Data collection is five screens
 * (`Sources`, `Imports`, `Datastreams`, `Connectors`, `Objects`) and a fixed
 * five-entry key would explain an error mark to a screen with no errors on it,
 * which is a list that says something untrue about the page. The MEANINGS are
 * this surface's — a warning here is a source that needs a gesture, and on Test
 * › Runs the same mark means an unresolved pin — which is why `StatusLegend`
 * takes them from the caller and holds none.
 */
const DATA_STATE_MEANING: Record<Tone, { label: string; meaning: string }> = {
  success: { label: "In place", meaning: "configured and working — nothing to do" },
  warning: { label: "Needs a gesture", meaning: "incomplete, held back, no longer fresh — or a state this console does not know" },
  error: { label: "Refused", meaning: "revoked, archived or failed — it cannot be read as it stands" },
  // `neutral` IS NOT "no state reported" — amendment 16 separates the two, and
  // this sentence was still teaching the old reading. A row the server said
  // nothing about is `Unknown` in the warning colour; neutral is the absence
  // the server CHOSE to state, or a switch somebody turned off.
  neutral: { label: "Unavailable", meaning: "the server stated an absence, or the row is switched off" },
  // And the warning mark carries both readings, because one screen cannot show
  // two amber diamonds meaning different things.
  info: { label: "Not offered here", meaning: "this deployment does not carry the capability" },
};

function collectionLegend(items: readonly DataSurfaceItem[], columns: readonly DataColumn[]): StatusLegendEntry[] {
  const shown = new Set<Tone>();
  for (const item of items) {
    for (const column of columns) {
      const state = item.states?.[column.key];
      if (state !== undefined) shown.add(stateTone(state));
    }
  }
  return [...shown].map((tone) => ({ tone, ...DATA_STATE_MEANING[tone] }));
}

export function StateValue({ value }: { value: string | undefined }) {
  const shown = value || "unavailable";
  return <Status tone={stateTone(shown)}>{stateLabel(shown)}</Status>;
}

export function EvidenceTime({ value }: { value: string | null | undefined }) {
  // `Unavailable` is this surface's word for an absence, and it is said here
  // rather than delegated: `formatTimestamp` answers the dash, which a column
  // of evidence times has already used for something else.
  if (!value) return <span className="text-text-secondary">Unavailable</span>;
  // The RAW value, not the `Date`, so a server sending `"soon"` shows `soon`
  // and never `Invalid Date` — see the header of `ui/Timestamp.tsx`.
  return <span className="font-numeric text-text-secondary">{formatTimestamp(value)}</span>;
}

/** How many of what, in a sentence.
 *
 *  The footer used to end with `bound {25}`: `bound` is the server's page size
 *  (`data_surface.py:1047`), a word from the query and not from the reading, and
 *  it stood next to two numbers a person does have to compare. Only measured
 *  numbers are printed — when the server sends no total, the count of rows on
 *  the screen is the only thing this footer is entitled to say. */
function collectionSummary(title: string, shown: number, total: number | undefined, filtered: boolean): string {
  const subject = filtered ? `${title} matching your filters` : title;
  if (total === undefined) return `Showing ${shown} ${subject}`;
  if (shown >= total) return `Showing all ${total} ${subject}`;
  return `Showing ${shown} of ${total} ${subject}`;
}

export function DataCollectionLayout({
  title,
  description,
  emptyTitle,
  emptyDescription,
  emptyAction,
  state,
  reload,
  columns,
  onOpen,
  actions,
  filters,
  onNextPage,
  onFirstPage,
  onPreviousPage,
  onClearFilters,
}: {
  title: string;
  description: string;
  emptyTitle: string;
  emptyDescription: string;
  /** The gesture the empty sentence NAMES, made reachable from here. A screen
   *  that tells a person where to go and offers no way there is a dead end. */
  emptyAction?: ReactNode;
  state: DataSurfaceState;
  reload: () => void;
  columns: readonly DataColumn[];
  onOpen?: (item: DataSurfaceItem) => void;
  actions?: ReactNode;
  filters?: ReactNode;
  onNextPage?: () => void;
  onFirstPage?: () => void;
  /** Absent while the reader is on the first page — there is nowhere back. */
  onPreviousPage?: () => void;
  /** The gesture an empty FILTERED list names. Without it the screen would tell
   *  a person to go and create what they have merely hidden. */
  onClearFilters?: () => void;
}) {
  const envelope = state.status === "ready" ? state.envelope : undefined;
  const hasFilters = Object.values(envelope?.applied_filters ?? {}).some(Boolean);
  // A footer that can say nothing is not drawn: Sources and Events get their
  // pager the day their envelope carries the page metadata Imports already has.
  const hasPager = envelope !== undefined && (envelope.total !== undefined || Boolean(envelope.next_cursor));

  // THE ORDER IS THE READER'S, and it is theirs over THIS PAGE. The server
  // composes the collection and pages it with a cursor; nothing here can reorder
  // what it has not read. `SortScopeNote` below says exactly that, and it is
  // mounted only when there is a next page to be wrong about.
  const { sort, toggleSort } = useTableSort();
  const items = envelope?.items ?? [];
  const rows = useMemo(
    () =>
      sortRows(items, sort, (item, key) => {
        const column = columns.find((candidate) => candidate.key === key);
        return column ? columnSortValue(column, item) : null;
      }),
    [items, sort, columns],
  );
  const anyColumnSorts = columns.some((column) => columnSorts(column, items));
  const orderIsPageWide = anyColumnSorts && Boolean(envelope?.next_cursor);

  return (
    <Stack data-owner={`data/${title.toLowerCase().replaceAll(" ", "-")}`}>
      <PageHeader
        title={title}
        description={description}
        actions={actions}
        legend={<StatusLegend entries={collectionLegend(items, columns)} label={`What a ${title} mark means`} />}
      />
      {state.status === "loading" && <p role="status" className="text-body text-text-secondary">Loading {title}…</p>}
      {state.status === "error" && (
        <Status as="block" tone="error" title={`${title} unavailable`} action={<Button variant="secondary" onClick={reload}>Retry</Button>}>
          {state.message}. No evidence has been substituted.
        </Status>
      )}
      {state.status === "ready" && (
        <Panel flush>
          <PanelHeader
            title={`${title} inventory`}
            description={state.envelope.evidence_as_of ? `Evidence as of ${formatTimestamp(state.envelope.evidence_as_of)}` : "No evidence timestamp is available."}
          />
          {filters ? <div className="border-b border-divider-base p-4">{filters}</div> : null}
          {/* The rows below are the PREVIOUS answer while this one is read. Said
              once, quietly, instead of unmounting the table. */}
          {state.refreshing ? (
            <p role="status" className="border-b border-divider-base px-4 py-2 text-caption text-text-secondary">Updating {title}…</p>
          ) : null}
          {state.envelope.items.length === 0 && hasFilters ? (
            <EmptyState
              title={`No ${title} match the filters you set`}
              description={`Every ${title} of this Project is still here — the filters above are hiding them. Widen a filter, or clear them all, to see them again.`}
              action={onClearFilters ? <Button variant="secondary" onClick={onClearFilters}>Clear filters</Button> : undefined}
            />
          ) : state.envelope.items.length === 0 ? (
            // THE SCREEN'S OWN SENTENCE, ALWAYS. This used to prefer
            // `unavailable_reasons[0].message`, which is a server template —
            // `_empty_reason` in `data_surface.py` builds "No owned {lens}
            // evidence is available for this Project." — and it fires on EVERY
            // empty list. So the server sentence always won and the four lenses'
            // own copy was dead code: every empty Data screen said "evidence",
            // "owned" and the lens token, and named no gesture. The reason
            // carries nothing the screen does not already know (it is a constant
            // formatted from the lens name), so nothing is lost by dropping it.
            <EmptyState title={emptyTitle} description={emptyDescription} action={emptyAction} />
          ) : (
            <TableScroll label={`${title} inventory`}>
              <Table>
                <TableHeader>
                  <TableRow>
                    {columns.map((column) =>
                      columnSorts(column, state.envelope.items) ? (
                        <SortableHead
                          key={column.key}
                          sortKey={column.key}
                          sort={sort}
                          onSort={toggleSort}
                          className={column.className}
                        >
                          {column.label}
                        </SortableHead>
                      ) : (
                        <TableHead key={column.key} className={column.className}>{column.label}</TableHead>
                      ),
                    )}
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {rows.map((item) => (
                    <TableRow
                      key={item.object_ref.id}
                      className={onOpen ? "cursor-pointer" : undefined}
                      tabIndex={onOpen ? 0 : undefined}
                      onClick={() => onOpen?.(item)}
                      onKeyDown={(event) => { if (onOpen && (event.key === "Enter" || event.key === " ")) onOpen(item); }}
                    >
                      {columns.map((column) => <TableCell key={column.key} className={column.className}>{column.render(item)}</TableCell>)}
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableScroll>
          )}
          {hasPager ? (
            // THE THREE STATES OF A DIRECTION, said in the props rather than in
            // three different button expressions. `First` is absent when this
            // collection has no way to restart (`undefined`); `Previous` is
            // always here and dead on the first page (`null`), because
            // forward-only paging made every page but the first a dead end —
            // the reader could go on, or start over, and nothing in between;
            // `Next` is dead when the server sent no cursor.
            <Pager
              className="border-t border-divider-base p-4"
              label={`${title} pages`}
              onFirst={onFirstPage}
              onPrevious={onPreviousPage ?? null}
              onNext={state.envelope.next_cursor ? onNextPage ?? null : null}
            >
              <span>{collectionSummary(title, state.envelope.items.length, state.envelope.total, hasFilters)}</span>
              {/* Only when there IS a next page. On the last page the rows on
                  screen ARE the collection, and the sentence would be a
                  warning about a mistake nobody can make. */}
              {orderIsPageWide ? <SortScopeNote /> : null}
            </Pager>
          ) : null}
        </Panel>
      )}
    </Stack>
  );
}
