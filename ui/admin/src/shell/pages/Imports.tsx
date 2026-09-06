import { useCallback, useMemo, useState } from "react";
import { DataCollectionLayout, EvidenceTime, labelForState, StateValue, type DataColumn } from "../../data/DataCollectionLayout";
import { useDataSurface, useDebouncedValue, usePageCursor } from "../../data/dataSurface";
import { Input, NativeSelect, ObjectId } from "../../ui";

export default function Imports({
  projectId,
  onOpenImport,
}: {
  projectId: string;
  onOpenImport?: (id: string) => void;
}) {
  const [query, setQuery] = useState("");
  const [stateFilter, setStateFilter] = useState("");
  const [datastreamFilter, setDatastreamFilter] = useState("");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const { cursor, canGoBack, goToFirstPage, goToNextPage, goToPreviousPage } = usePageCursor();
  // The box keeps every keystroke; the REQUEST waits for the typing to stop.
  const searched = useDebouncedValue(query.trim());
  const { state, reload } = useDataSurface(projectId, "imports", undefined, {
    q: searched || undefined,
    state: stateFilter || undefined,
    datastream: datastreamFilter || undefined,
    from: dateFrom || undefined,
    to: dateTo || undefined,
    cursor: cursor || undefined,
  });
  const clearFilters = useCallback(() => {
    setQuery("");
    setStateFilter("");
    setDatastreamFilter("");
    setDateFrom("");
    setDateTo("");
    goToFirstPage();
  }, [goToFirstPage]);
  const hasFilters = Boolean(query || stateFilter || datastreamFilter || dateFrom || dateTo);
  const columns = useMemo<readonly DataColumn[]>(() => [
    {
      key: "import",
      label: "Import",
      render: (item) => (
        <div>
          <strong className="block text-text">{item.name ?? item.object_ref.id}</strong>
          <ObjectId value={item.object_ref.id} title="Object" />
        </div>
      ),
    },
    { key: "datastream", label: "Datastream", render: (item) => <span className="font-mono text-ui">{item.datastream_ref?.id ?? "Unavailable"}</span> },
    // AC6 names these on the ROW, and the server already composes every one of
    // them in `project_import()`. They were being dropped on the floor.
    {
      key: "channel",
      label: "Channel / format",
      render: (item) => {
        const evidence = (item.evidence ?? {}) as { feed_format?: string | null; receipt?: { channel?: string | null } | null };
        const channel = evidence.receipt?.channel;
        const format = evidence.feed_format;
        return <span className="text-ui">{[channel, format].filter(Boolean).join(" · ") || "Unavailable"}</span>;
      },
    },
    { key: "lifecycle", label: "Outcome", render: (item) => <StateValue value={item.states.lifecycle} /> },
    {
      key: "rows",
      label: "Accepted / rejected",
      render: (item) => {
        const evidence = (item.evidence ?? {}) as { row_count?: number | null; rejected_row_count?: number | null };
        if (evidence.row_count == null && evidence.rejected_row_count == null) {
          return <span className="text-ui text-text-secondary">Unavailable</span>;
        }
        return <span className="font-numeric text-ui">{evidence.row_count ?? "—"} / {evidence.rejected_row_count ?? "—"}</span>;
      },
    },
    { key: "validation", label: "Validation", render: (item) => <StateValue value={item.states.validation} /> },
    { key: "candidate", label: "Candidate", render: (item) => <StateValue value={item.states.candidate} /> },
    { key: "publication", label: "Publication", render: (item) => <StateValue value={item.states.publication} /> },
    { key: "evidence", label: "Observed", render: (item) => <EvidenceTime value={item.evidence_as_of} /> },
  ], []);

  return (
    <DataCollectionLayout
      title="Imports"
      description="Project-wide managed-feed ledger with exact Datastream, candidate and publication evidence."
      emptyTitle="No file has been imported into this Project"
      emptyDescription="A file arrives through a Datastream that receives files — by email or another inbound channel, or by uploading one yourself from that Datastream's Mapping tab. Nothing has arrived yet, so there is no import to read."
      state={state}
      reload={reload}
      columns={columns}
      onOpen={onOpenImport ? (item) => onOpenImport(item.object_ref.id) : undefined}
      filters={
        <div className="grid w-full gap-3 md:grid-cols-[minmax(14rem,2fr)_repeat(4,minmax(9rem,1fr))]">
          <Input aria-label="Search Imports" placeholder="Search Imports" value={query} onChange={(event) => { setQuery(event.target.value); goToFirstPage(); }} />
          <NativeSelect aria-label="Filter Imports by state" value={stateFilter} onChange={(event) => { setStateFilter(event.target.value); goToFirstPage(); }}>
            <option value="">All states</option>
            {(state.status === "ready" ? state.envelope.filter_options?.states ?? [] : []).map((option) => <option key={option} value={option}>{labelForState(option)}</option>)}
          </NativeSelect>
          <NativeSelect aria-label="Filter Imports by Datastream" value={datastreamFilter} onChange={(event) => { setDatastreamFilter(event.target.value); goToFirstPage(); }}>
            <option value="">All Datastreams</option>
            {(state.status === "ready" ? state.envelope.filter_options?.datastreams ?? [] : []).map((option) => <option key={option} value={option}>{option}</option>)}
          </NativeSelect>
          <Input aria-label="Imports from date" type="date" value={dateFrom} onChange={(event) => { setDateFrom(event.target.value); goToFirstPage(); }} />
          <Input aria-label="Imports to date" type="date" value={dateTo} onChange={(event) => { setDateTo(event.target.value); goToFirstPage(); }} />
        </div>
      }
      onNextPage={() => { if (state.status === "ready" && state.envelope.next_cursor) goToNextPage(state.envelope.next_cursor); }}
      onPreviousPage={canGoBack ? goToPreviousPage : undefined}
      onFirstPage={cursor ? goToFirstPage : undefined}
      onClearFilters={hasFilters ? clearFilters : undefined}
    />
  );
}
