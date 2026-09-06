/**
 * Export this Datastream: a chosen period, a chosen column set, a masked CSV.
 *
 * `SCREEN-FUNCTION-MATRIX.md` Lot 1 names the Data tab's primary action as
 * "load or export the selected sample". The export half existed only as a
 * server route with no control anywhere in `ui/admin/src` — four review lenses
 * grepped for one independently and found zero.
 *
 * WHAT THIS IS NOT. Story 43.16 leaves the governed ASYNCHRONOUS export
 * unbuilt on purpose: it needs "a durable, authorized job/worker contract bound
 * to project, Datastream, stage, interval, filters and publication version",
 * creating that is listed under **Ask First**, and the substrate does not exist.
 * So this is the bounded, request-scoped door, and it says so rather than
 * implying it can hand over everything.
 *
 * THE THREE THINGS IT REFUSES TO HIDE:
 *
 *  - the CEILING. The server refuses past 50 000 rows rather than truncating,
 *    and the refusal is shown verbatim. A truncated export is a file a person
 *    treats as complete.
 *  - the MASKING. Columns that will arrive masked are labelled here, before the
 *    download, not discovered in the file.
 *  - the REFUSAL itself. Every server message is rendered as sent; none is
 *    paraphrased into "something went wrong".
 */
import { useEffect, useState } from "react";
import { Button, Checkbox, Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, Field, Input, Status } from "../../ui";
import { apiFetch } from "../../lib/apiFetch";

const STAGES = ["collected", "mapped", "processed", "published"] as const;

interface ColumnsPayload {
  project_id: string;
  datastream_id: string;
  columns?: string[];
  masked_columns?: string[];
}

type ColumnState =
  | { status: "loading" }
  | { status: "refused"; message: string }
  | { status: "ready"; columns: string[]; masked: string[] };

/** Seven days back from today, as ISO days — a starting point a person can
 *  widen, not a default that quietly exports a year. */
function defaultRange(today: Date): [string, string] {
  const end = new Date(today);
  const start = new Date(today);
  start.setUTCDate(start.getUTCDate() - 6);
  return [start.toISOString().slice(0, 10), end.toISOString().slice(0, 10)];
}

export default function DatastreamExportDialog({
  projectId,
  datastreamId,
  stage: initialStage = "published",
  today = new Date(),
}: {
  projectId: string;
  datastreamId: string;
  stage?: (typeof STAGES)[number];
  today?: Date;
}) {
  const [open, setOpen] = useState(false);
  const [state, setState] = useState<ColumnState>({ status: "loading" });
  const [selected, setSelected] = useState<string[]>([]);
  const [stage, setStage] = useState<string>(initialStage);
  const [initialFrom, initialTo] = defaultRange(today);
  const [dateFrom, setDateFrom] = useState(initialFrom);
  const [dateTo, setDateTo] = useState(initialTo);
  const [failure, setFailure] = useState<string | null>(null);
  const [running, setRunning] = useState(false);

  useEffect(() => {
    if (!open) return;
    let alive = true;
    setState({ status: "loading" });
    (async () => {
      try {
        const response = await apiFetch(
          `/api/projects/${encodeURIComponent(projectId)}/datastreams/${encodeURIComponent(datastreamId)}/export/columns`,
        );
        const body = (await response.json().catch(() => null)) as ColumnsPayload | null;
        if (!alive) return;
        if (!response.ok || !body) {
          setState({
            status: "refused",
            message: typeof (body as { message?: string })?.message === "string"
              ? (body as { message?: string }).message!
              : `The column list could not be read (HTTP ${response.status}).`,
          });
          return;
        }
        // The same echo check the rest of this console applies: a payload that
        // does not name what was asked for is refused, not rendered.
        if (body.project_id !== projectId || body.datastream_id !== datastreamId) {
          setState({
            status: "refused",
            message: "The column list does not match the requested Project or Datastream.",
          });
          return;
        }
        setState({ status: "ready", columns: body.columns ?? [], masked: body.masked_columns ?? [] });
      } catch {
        if (alive) setState({ status: "refused", message: "The column list could not be reached." });
      }
    })();
    return () => { alive = false; };
  }, [open, projectId, datastreamId]);

  const toggle = (column: string) =>
    setSelected((current) =>
      current.includes(column) ? current.filter((c) => c !== column) : [...current, column],
    );

  async function run() {
    setFailure(null);
    setRunning(true);
    const query = new URLSearchParams({ stage, date_from: dateFrom, date_to: dateTo });
    // No `columns` parameter means every column — stated in the label, so the
    // empty selection is a choice rather than an oversight.
    if (selected.length > 0) query.set("columns", selected.join(","));
    try {
      const response = await apiFetch(
        `/api/projects/${encodeURIComponent(projectId)}/datastreams/${encodeURIComponent(datastreamId)}/export?${query}`,
      );
      if (!response.ok) {
        const body = await response.json().catch(() => null);
        setFailure(
          typeof body?.message === "string"
            ? body.message
            : `The export was refused (HTTP ${response.status}).`,
        );
        return;
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `datastream_${datastreamId}_${stage}_${dateFrom}_${dateTo}.csv`;
      anchor.click();
      URL.revokeObjectURL(url);
      setOpen(false);
    } catch {
      setFailure("The export could not be reached.");
    } finally {
      setRunning(false);
    }
  }

  const maskedSelected = state.status === "ready"
    ? state.masked.filter((c) => selected.length === 0 || selected.includes(c))
    : [];

  return (
    <>
      <Button variant="secondary" size="sm" onClick={() => setOpen(true)}>Export…</Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="sm:max-w-[560px]">
          <DialogHeader>
            <DialogTitle>Export this Datastream</DialogTitle>
            <DialogDescription>
              A bounded, masked CSV over the period and columns you choose. This is not a
              full-history export: the server refuses a range it cannot serve in one read
              rather than handing you a truncated file.
            </DialogDescription>
          </DialogHeader>

          <div className="grid gap-4 py-2">
            <div className="grid grid-cols-2 gap-3">
              <Field label="From">
                {(props) => (
                  <Input {...props} type="date" value={dateFrom} onChange={(e) => setDateFrom(e.target.value)} />
                )}
              </Field>
              <Field label="To">
                {(props) => (
                  <Input {...props} type="date" value={dateTo} onChange={(e) => setDateTo(e.target.value)} />
                )}
              </Field>
            </div>

            <Field label="Stage" hint="Which physical stage of the Datastream to read.">
              {(props) => (
                <select
                  {...props}
                  className="h-9 rounded-control border border-divider-base bg-surface-light px-3 text-ui"
                  value={stage}
                  onChange={(event) => setStage(event.target.value)}
                >
                  {STAGES.map((entry) => <option key={entry} value={entry}>{entry}</option>)}
                </select>
              )}
            </Field>

            <div>
              <p className="m-0 mb-2 text-label font-label text-text">
                Columns
                <span className="ml-2 font-normal text-caption text-text-secondary">
                  {selected.length === 0 ? "none selected — every column is exported" : `${selected.length} selected`}
                </span>
              </p>
              {state.status === "loading" && (
                <p className="m-0 text-caption text-text-secondary">Reading the column list…</p>
              )}
              {state.status === "refused" && (
                <Status as="block" tone="warning" title="Columns unavailable">
                  {state.message} You can still export every column.
                </Status>
              )}
              {state.status === "ready" && state.columns.length === 0 && (
                <Status as="block" tone="warning" title="No column is available">
                  This Datastream has no consolidated mart to read columns from.
                </Status>
              )}
              {state.status === "ready" && state.columns.length > 0 && (
                <ul className="m-0 grid max-h-52 list-none grid-cols-2 gap-2 overflow-y-auto p-0">
                  {state.columns.map((column) => (
                    <li key={column} className="flex items-center gap-2">
                      <Checkbox
                        id={`export-col-${column}`}
                        checked={selected.includes(column)}
                        onCheckedChange={() => toggle(column)}
                      />
                      <label htmlFor={`export-col-${column}`} className="text-ui text-text">
                        {column}
                        {state.masked.includes(column) && (
                          <span className="ml-1 text-caption text-text-secondary">masked</span>
                        )}
                      </label>
                    </li>
                  ))}
                </ul>
              )}
            </div>

            {maskedSelected.length > 0 && (
              /* Said BEFORE the download, not discovered in the file. */
              <Status as="block" tone="neutral" title="Masked in the export">
                {maskedSelected.join(", ")} — these arrive masked, exactly as they do on screen.
              </Status>
            )}

            {failure && (
              <Status as="block" tone="error" title="The export was refused">
                {failure}
              </Status>
            )}
          </div>

          <DialogFooter>
            <Button type="button" variant="secondary" onClick={() => setOpen(false)} disabled={running}>
              Cancel
            </Button>
            <Button type="button" onClick={() => void run()} disabled={running || !dateFrom || !dateTo}>
              {running ? "Exporting…" : "Export CSV"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
