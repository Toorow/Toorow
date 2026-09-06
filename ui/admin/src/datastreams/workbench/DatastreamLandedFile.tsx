/**
 * The last file that ARRIVED on a file source, read back — lot B1.
 *
 * Amendment 6 of the 2026-08-11 review names the defect: « `Preview a sample` ne
 * prévisualise pas ce qui est arrivé ». On a Datastream that has already
 * received a file, the only preview the console owned asked the person to
 * re-upload one — the wrong question, asked of the wrong file.
 *
 * And amendment 7 removed the connector pull axis from a `managed_feed`, which
 * was right — a pushed source has no report profile, no connector relation and
 * no collection window — but left the `Data` tab of the four live file sources
 * with no reading of their own rows at all. This is what takes its place.
 *
 * TWO GESTURES, TWO NAMES. This panel reads what DID arrive and offers no
 * upload. `FileSourceSamplePanel`, on `Mapping`, tests a file that has NOT
 * arrived yet. Neither pretends to be the other, and the copy of each says which
 * one it is.
 *
 * EVERY REFUSAL IS THE SERVER'S. « No file has arrived yet », « the last file
 * could not be read » and « a file arrived and is still on its way in » are
 * three different sentences composed on the route, in one place, so a second
 * door would say the same words. Nothing here invents a row and nothing here
 * renders a `0` for a count the route sent as `null`.
 */
import { useEffect, useState } from "react";
import {
  EmptyState, Panel, PanelHeader, Status, Timestamp,
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow, TableScroll,
  formatNumber,
} from "../../ui";
import { apiFetch } from "../../lib/apiFetch";

/** What the route says arrived, whichever door it came through. */
interface Arrival {
  raw_import_id?: string | null;
  filename?: string | null;
  state?: string | null;
  size_bytes?: number | null;
  arrived_at?: string | null;
  error_code?: string | null;
}

/** What the import ledger recorded for it. `row_count` is `null` until measured. */
interface ImportRecord {
  ledger_id?: string | null;
  filename?: string | null;
  feed_format?: string | null;
  outcome?: string | null;
  row_count?: number | null;
  rejected_row_count?: number | null;
  error_code?: string | null;
  imported_at?: string | null;
  /** `snapshot_observed_at` — when the source this import read was observed. A
   *  Sheets sync has one and an uploaded file does not, so it is optional and
   *  never invented from `imported_at`: they answer different questions. */
  observed_at?: string | null;
}

interface LandedFilePayload {
  project_id: string;
  datastream_id: string;
  mode?: string | null;
  doors?: { channels?: string[]; upload_available?: boolean } | null;
  arrival?: Arrival | null;
  import?: ImportRecord | null;
  relation?: string | null;
  columns?: string[];
  rows?: Record<string, unknown>[] | null;
  row_count?: number | null;
  truncated?: boolean;
  masked_fields?: string[];
  reason?: string | null;
  message?: string | null;
}

type State =
  | { status: "loading" }
  | { status: "refused"; message: string }
  | { status: "ready"; payload: LandedFilePayload };

/** The reasons that are an ABSENCE of data, not a failure to read it. Split here
 *  because the two must never share a tone: an empty file source is a Datastream
 *  waiting for its first file, and a broken read is something to repair. */
const EMPTY_REASONS = new Set([
  "no_file_yet",
  "arrived_not_landed",
  "import_not_landed",
  "not_a_pushed_source",
]);

/** Where a file gets in, named from what the Datastream itself declares.
 *
 *  `config.channels` is the Datastream's own allowlist and the sentence says
 *  only what is in it. Upload is never inferred from that list — it is a gesture
 *  of the console, offered on `Mapping` under its own name, and it is named last
 *  because it is the door a person can use right now whatever the list says. */
function doorSentence(doors: LandedFilePayload["doors"]): string {
  const inbound = (doors?.channels ?? []).filter((channel) => channel !== "upload");
  const test = "You can test a file from Mapping before one arrives.";
  if (inbound.includes("email")) {
    return `It arrives by email, at this Datastream's inbound address. ${test}`;
  }
  if (inbound.length > 0) {
    return `It arrives by ${inbound.join(", ")}. ${test}`;
  }
  return `This Datastream declares no inbound channel, so a file reaches it by upload. ${test}`;
}

function formatCell(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

/** A count the route measured, or the sentence that says it never was. */
function countText(value: number | null | undefined): string {
  return typeof value === "number" ? String(value) : "Not measured";
}

/** One dated fact of the chain a file walks: arrived, observed, imported. */
interface ArrivalEvent {
  at: string;
  what: string;
  rows: number | null;
}

/**
 * THE DAY READING OF A FILE SOURCE — amendment of 2026-08-18.
 *
 * A `connector_pull` Datastream has a calendar because it asks a provider for a
 * window; a `managed_feed` has none, and amendment 7 of the 2026-08-11 review was
 * right to take the pull grid away from it. But « no calendar » is not « no
 * dates »: the envelope of the last file carries three instants, each answering a
 * different question, and until now not one of them reached a day axis. This
 * composes what IS on the wire and never more than that.
 *
 * WHAT IT IS NOT. It is not a history. `read_landed_file` publishes the LAST
 * arrival and the last import that wrote — the ledger it reads holds more and the
 * envelope does not carry them — so the panel says so and names the collection
 * that does hold them. A table that quietly showed one file while being titled
 * "by day" would be the silently-narrowed list this surface refuses everywhere.
 */
function arrivalEvents(payload: LandedFilePayload): ArrivalEvent[] {
  const arrival = payload.arrival ?? null;
  const record = payload.import ?? null;
  const candidates: (ArrivalEvent | null)[] = [
    arrival?.arrived_at
      ? {
          at: arrival.arrived_at,
          what: arrival.filename
            ? `${arrival.filename} arrived`
            : "A file arrived",
          rows: null,
        }
      : null,
    record?.observed_at
      ? {
          at: record.observed_at,
          what: "The source it reads was observed at this instant",
          rows: null,
        }
      : null,
    record?.imported_at
      ? {
          at: record.imported_at,
          // The outcome is the server's own word — `written`, `published`,
          // `failed`, `rejected`, `opened`, `noop` — rendered rather than
          // translated, so a seventh outcome added to the ledger is legible
          // here on the day it appears.
          what: `Import ${record.outcome ?? "recorded"}`,
          rows: typeof record.row_count === "number" ? record.row_count : null,
        }
      : null,
  ];
  return candidates
    .filter((event): event is ArrivalEvent => event !== null)
    .sort((left, right) => left.at.localeCompare(right.at));
}

/** The calendar day an instant falls on, as the instant itself spells it. */
function dayOf(instant: string): string {
  return instant.slice(0, 10);
}

export default function DatastreamLandedFile({
  projectId,
  datastreamId,
}: {
  projectId: string;
  datastreamId: string;
}) {
  const [state, setState] = useState<State>({ status: "loading" });

  // THE READ THIS SCREEN'S ERROR BLOCK OFFERS TO REPEAT (76-4). An error
  // that names no way forward is a dead end; this token is what `Retry`
  // moves, and the effect below is the read it re-runs.
  const [reloadToken, setReloadToken] = useState(0);
  useEffect(() => {
    let alive = true;
    setState({ status: "loading" });
    (async () => {
      try {
        const response = await apiFetch(
          `/api/projects/${encodeURIComponent(projectId)}`
            + `/datastreams/${encodeURIComponent(datastreamId)}/workbench/landed-file`,
          { cache: "no-store" },
        );
        const body = await response.json().catch(() => null);
        if (!alive) return;
        if (!response.ok) {
          setState({
            status: "refused",
            message: typeof body?.message === "string"
              ? body.message
              : `The last file could not be read (HTTP ${response.status}).`,
          });
          return;
        }
        // The echo check every reader of this workbench applies: a payload that
        // does not name the Project and Datastream that were asked for is
        // refused rather than rendered.
        if (body?.project_id !== projectId || body?.datastream_id !== datastreamId) {
          setState({
            status: "refused",
            message: "The answer does not name the Project or Datastream that was asked about.",
          });
          return;
        }
        setState({ status: "ready", payload: body as LandedFilePayload });
      } catch {
        if (alive) setState({ status: "refused", message: "The last file could not be reached." });
      }
    })();
    return () => { alive = false; };
  }, [projectId, datastreamId, reloadToken]);

  if (state.status === "loading") {
    return (
      <Panel flush data-testid="landed-file">
        <PanelHeader title="The last file that arrived" description="Reading it back…" />
      </Panel>
    );
  }

  if (state.status === "refused") {
    return (
      <Panel flush data-testid="landed-file">
        <PanelHeader title="The last file that arrived" />
        <div className="p-5">
          <Status as="block" tone="error" title="The last file could not be read"
          action={<Retry onClick={() => setReloadToken((token) => token + 1)} />}
        >
            {state.message} No row has been substituted — nothing below is an approximation.
          </Status>
        </div>
      </Panel>
    );
  }

  const { payload } = state;
  const arrival = payload.arrival ?? null;
  const record = payload.import ?? null;
  const columns = payload.columns ?? [];
  const rows = payload.rows ?? null;
  const masked = payload.masked_fields ?? [];
  const name = record?.filename || arrival?.filename || null;
  const when = record?.imported_at || arrival?.arrived_at || null;
  const events = arrivalEvents(payload);

  return (
    <Panel flush data-testid="landed-file">
      <PanelHeader
        title="The last file that arrived"
        description={
          name
            ? `${name}${when ? ` · ${when}` : ""} · ${countText(record?.row_count)} rows landed`
            : "What this Datastream last received, read from where its rows landed."
        }
      />

      {/* WHAT ARRIVED, BY DAY — amendment of 2026-08-18. A file source had no
          dated reading of its own on this tab at all: the pull grid was removed
          from it, rightly, and nothing put a day axis in its place, so four of
          the six live Datastreams answered « when did anything last reach this
          flux » with nothing. Every instant below is on the envelope; none is
          derived from another, and the panel says what the envelope does NOT
          carry rather than letting one file read as a history. */}
      {payload.reason !== "not_a_pushed_source" && (
        <div className="border-t border-divider-base" data-testid="arrival-by-day">
          {events.length === 0 ? (
            <div className="px-5 py-4">
              <Status as="block" tone="neutral" title="No arrival of this Datastream is dated">
                Nothing on this answer carries an instant — no file has been
                received and no import has been opened — so there is no day to
                read. Every file this Datastream does receive is dated in
                Data › Imports, which holds them all.
              </Status>
            </div>
          ) : (
            <>
              <TableScroll label="What arrived, by day">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Day</TableHead>
                      <TableHead>What happened</TableHead>
                      <TableHead>At</TableHead>
                      <TableHead numeric>Rows landed</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {events.map((event) => (
                      <TableRow key={`${event.at}:${event.what}`} density="compact">
                        <TableCell className="font-numeric [font-variant-numeric:lining-nums_tabular-nums]">
                          {dayOf(event.at)}
                        </TableCell>
                        <TableCell>{event.what}</TableCell>
                        <TableCell><Timestamp value={event.at} /></TableCell>
                        <TableCell numeric>
                          {event.rows === null ? (
                            // NEVER A `0`. `row_count` is NULL until an import
                            // measures it, and a zero here would read as a file
                            // that contained nothing — the same rule the day grid
                            // of a pull source holds on its own volumes.
                            <span className="text-caption text-text-secondary">Not measured</span>
                          ) : (
                            formatNumber(event.rows)
                          )}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </TableScroll>
              <p className="m-0 px-5 py-3 text-caption text-text-secondary">
                This reading covers the last file only — the arrival and the import
                that wrote it. Earlier arrivals of this Datastream are not on this
                answer; Data › Imports holds every one of them, with its date.
              </p>
            </>
          )}
        </div>
      )}

      {payload.reason && EMPTY_REASONS.has(payload.reason) && (
        <EmptyState
          title={
            payload.reason === "no_file_yet"
              ? "No file has arrived yet"
              : payload.reason === "not_a_pushed_source"
                ? "No file is pushed to this Datastream"
                : "A file has arrived and has not landed yet"
          }
          description={
            <>
              {payload.message}
              {payload.reason === "no_file_yet" && ` ${doorSentence(payload.doors)}`}
            </>
          }
        />
      )}

      {payload.reason && !EMPTY_REASONS.has(payload.reason) && (
        <div className="p-5">
          <Status as="block" tone="error" title="The last file could not be read"
          action={<Retry onClick={() => setReloadToken((token) => token + 1)} />}
        >
            {payload.message}
            {record?.error_code ? ` (${record.error_code})` : ""} No row has been
            substituted — nothing here is an approximation.
          </Status>
        </div>
      )}

      {rows !== null && (
        <>
          <div className="grid gap-3 px-5 pt-1">
            {masked.length > 0 && (
              <Status as="block" tone="neutral" title="Masked before it left the server">
                {masked.join(", ")} · a column is shown only where the mapping classifies it
                as carrying nothing sensitive.
              </Status>
            )}
            {payload.truncated && (
              <Status as="block" tone="neutral" title="Bounded on purpose">
                The first {rows.length} rows of this file. A wider read is an export, not
                evidence.
              </Status>
            )}
            {typeof record?.rejected_row_count === "number" && record.rejected_row_count > 0 && (
              <Status as="block" tone="warning" title="Rows this file left behind">
                {record.rejected_row_count} row(s) of this file were rejected and are not below.
              </Status>
            )}
          </div>

          {rows.length === 0 || columns.length === 0 ? (
            <EmptyState
              title="This file landed no row"
              description="The import wrote nothing readable to its landing, so there is no row to show. Nothing has been substituted for it."
            />
          ) : (
            <TableScroll label="Rows of the last file that arrived">
              <Table>
                <TableHeader>
                  <TableRow>
                    {columns.map((column) => (
                      <TableHead key={column}>
                        {column}
                        {masked.includes(column) && (
                          <span className="ml-1 text-caption font-normal text-text-secondary">
                            masked
                          </span>
                        )}
                      </TableHead>
                    ))}
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {rows.map((row, index) => (
                    <TableRow key={index}>
                      {columns.map((column) => (
                        <TableCell key={column}>{formatCell(row[column])}</TableCell>
                      ))}
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableScroll>
          )}
        </>
      )}
    </Panel>
  );
}
