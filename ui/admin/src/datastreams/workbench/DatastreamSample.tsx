/**
 * The bounded, masked sample — the thing `Data` exists to show.
 *
 * `datastream-workbench-and-wizard.md:75` contracts this tab as "stage selector
 * … and bounded masked samples tied to exact run/plan/mapping versions", and
 * `DESIGN.md:146` fixes the composition: **DataSampleDay** — "connected vertical
 * date rail, daily summary and deterministic first five eligible rows".
 *
 * Neither existed. The tab rendered a warning saying samples were unavailable,
 * driven by a server literal that was unconditional, while
 * `GET /api/datastreams/{id}/sample` had been reading exactly this — masked,
 * project-scoped, deterministic — since story 12.19 with no caller anywhere in
 * `ui/admin/src`. Four review lenses, twice each, confirmed it.
 *
 * ⚠️ NOT WIRED, AND DELIBERATELY SO. The endpoint this reads is NOT MOUNTED:
 * `_datastream_sample` exists in `admin_api.py`, no `Route` registers it, and
 * `test_datastream_rollback_workbench.py:21` asserts its absence. Story 47.5
 * decommissioned it because it "cannot serve one processed table under several
 * stage labels or return an unbound sample" (`:108`) and "reports no exact
 * version binding and aliases several requested stages" (`:341`) — that is, it
 * lied about the two things this tab promises.
 *
 * The first version of this component was demonstrated against the sandbox's
 * mocked `fetch` and called done; against the real router it is a 404. A mocked
 * test does not prove a route exists.
 *
 * This file stays because it is the trace of the work that remains, and because
 * it is correct and tested — it renders the moment a governed replacement
 * endpoint exists. Do not re-point it at the retired one.
 *
 * WHAT THIS COMPONENT REFUSES TO DO. It renders the endpoint's answer and never
 * substitutes for it. Every refusal underneath — an unknown stage, a warehouse
 * that cannot be reached, two Datastreams sharing one project/connector so the
 * rows cannot be told apart — arrives here as a message and is shown verbatim.
 * An invented row would look exactly like evidence, which is the one thing this
 * surface must never produce.
 */
import { useEffect, useState } from "react";
import {
  EmptyState, ObjectId, Panel, PanelHeader, Status,
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow, TableScroll,
} from "../../ui";
import { apiFetch } from "../../lib/apiFetch";

const STAGES = ["collected", "mapped", "processed", "published"] as const;
export type SampleStage = (typeof STAGES)[number];

interface SampleDay {
  date: string;
  sampled_row_count?: number;
  rejection_count?: number;
  field_count?: number;
  rows?: Record<string, unknown>[];
}

interface SamplePayload {
  project_id: string;
  datastream_id: string;
  stage: string;
  served_stage: string;
  stage_note?: string | null;
  sample_watermark?: string | null;
  masked_fields?: string[];
  masked_value_count?: number;
  days?: SampleDay[];
}

type State =
  | { status: "loading" }
  | { status: "refused"; message: string }
  | { status: "ready"; payload: SamplePayload };

/** The window the sample is read over. Bounded on purpose: this surface shows
 *  evidence, not an export, and an unbounded read of a warehouse is neither. */
const WINDOW_DAYS = 7;

function isoDay(value: Date): string {
  return value.toISOString().slice(0, 10);
}

function windowEndingAt(watermark: string | null | undefined, today: Date): [string, string] {
  const end = watermark ? new Date(watermark) : today;
  const usable = Number.isNaN(end.getTime()) ? today : end;
  const start = new Date(usable);
  start.setUTCDate(start.getUTCDate() - (WINDOW_DAYS - 1));
  return [isoDay(start), isoDay(usable)];
}

export default function DatastreamSample({
  projectId, datastreamId, stage, watermark, executionId, today = new Date(),
}: {
  projectId: string;
  datastreamId: string;
  /** The stage the person selected above. The endpoint may SERVE another one
   *  and says so; that substitution is surfaced, never silently accepted. */
  stage: SampleStage;
  watermark?: string | null;
  executionId: string;
  /** Injected so a test is not bound to the day it runs on. */
  today?: Date;
}) {
  const [state, setState] = useState<State>({ status: "loading" });
  const [dateFrom, dateTo] = windowEndingAt(watermark, today);

  useEffect(() => {
    let alive = true;
    setState({ status: "loading" });
    (async () => {
      // The PROJECT-SCOPED path, which is the one actually mounted. The
      // retired `/api/datastreams/{id}/sample` is not, deliberately.
      const query = new URLSearchParams({
        stage, date_from: dateFrom, date_to: dateTo, limit: "5",
      });
      try {
        const response = await apiFetch(
          `/api/projects/${encodeURIComponent(projectId)}`
            + `/datastreams/${encodeURIComponent(datastreamId)}`
            + `/workbench/runs/${encodeURIComponent(executionId)}/sample?${query}`,
        );
        const body = await response.json().catch(() => null);
        if (!alive) return;
        if (!response.ok) {
          setState({
            status: "refused",
            message: typeof body?.message === "string"
              ? body.message
              : `The sample could not be read (HTTP ${response.status}).`,
          });
          return;
        }
        // The echo check this codebase applies everywhere: a payload that does
        // not name the Project and Datastream that were asked for is refused
        // rather than rendered. Three fixtures were rejected for missing it
        // before anyone realised it was deliberate.
        if (body?.project_id !== projectId || body?.datastream_id !== datastreamId) {
          setState({
            status: "refused",
            message: "The sample response does not match the requested Project or Datastream.",
          });
          return;
        }
        setState({ status: "ready", payload: body as SamplePayload });
      } catch {
        if (alive) setState({ status: "refused", message: "The sample could not be reached." });
      }
    })();
    return () => { alive = false; };
  }, [projectId, datastreamId, stage, dateFrom, dateTo]);

  if (state.status === "loading") {
    return (
      <Panel flush>
        <PanelHeader title="Bounded masked sample" description="Reading the governed window…" />
      </Panel>
    );
  }

  if (state.status === "refused") {
    return (
      <Panel flush>
        <PanelHeader title="Bounded masked sample" />
        <div className="p-5">
          <Status as="block" tone="warning" title="No sample is shown">
            {state.message} No rows have been substituted — nothing below is an approximation.
          </Status>
        </div>
      </Panel>
    );
  }

  const { payload } = state;
  const days = payload.days ?? [];
  const columns: string[] = [];
  for (const day of days) {
    for (const row of day.rows ?? []) {
      for (const key of Object.keys(row)) if (!columns.includes(key)) columns.push(key);
    }
  }
  const maskedFields = payload.masked_fields ?? [];
  const servedElsewhere = payload.served_stage && payload.served_stage !== stage;

  return (
    <Panel flush>
      <PanelHeader
        title="Bounded masked sample"
        description={`${dateFrom} to ${dateTo} · the first ${5} eligible rows of each day, deterministic.`}
      />
      <div className="grid gap-3 px-5 pt-1">
        {servedElsewhere && (
          <Status as="block" tone="neutral" title={`Served from ${payload.served_stage}`}>
            {payload.stage_note ?? "The requested stage has no materialisation of its own."}
          </Status>
        )}
        {maskedFields.length > 0 && (
          <Status as="block" tone="neutral" title="Masked before it left the server">
            {maskedFields.join(", ")}
            {typeof payload.masked_value_count === "number"
              ? ` · ${payload.masked_value_count} values masked`
              : ""}
          </Status>
        )}
      </div>

      {days.length === 0 || columns.length === 0 ? (
        <EmptyState
          title="No eligible row in this window"
          description={`Nothing was published between ${dateFrom} and ${dateTo}. The window is bounded on purpose; a wider read is an export, not evidence.`}
        />
      ) : (
        days.map((day) => (
          /* DataSampleDay: the connected vertical date rail of `DESIGN.md:146`.
             The rail is what ties a row to its day — without it the rows of
             seven days read as one undated table. */
          <section key={day.date} className="border-t border-divider-base first:border-t-0">
            <div className="flex items-baseline gap-3 px-5 py-3">
              <span className="font-numeric text-label font-semibold text-text">{day.date}</span>
              <span className="text-caption text-text-secondary">
                {day.sampled_row_count ?? (day.rows?.length ?? 0)} sampled
                {typeof day.rejection_count === "number" && day.rejection_count > 0
                  ? ` · ${day.rejection_count} rejected`
                  : ""}
                {typeof day.field_count === "number" ? ` · ${day.field_count} fields` : ""}
              </span>
            </div>
            {(day.rows ?? []).length === 0 ? (
              <p className="m-0 px-5 pb-4 text-caption text-text-secondary">
                No eligible row on this day.
              </p>
            ) : (
              <TableScroll label={`Sample rows for ${day.date}`}>
                <Table>
                  <TableHeader>
                    <TableRow>
                      {columns.map((column) => (
                        <TableHead key={column}>
                          {column}
                          {maskedFields.includes(column) && (
                            <span className="ml-1 text-caption font-normal text-text-secondary">masked</span>
                          )}
                        </TableHead>
                      ))}
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {(day.rows ?? []).map((row, index) => (
                      <TableRow key={`${day.date}-${index}`}>
                        {columns.map((column) => (
                          <TableCell key={column}>{formatCell(row[column])}</TableCell>
                        ))}
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </TableScroll>
            )}
          </section>
        ))
      )}

      {payload.sample_watermark && (
        <div className="border-t border-divider-base px-5 py-3 text-caption text-text-secondary">
          Sample watermark <ObjectId value={payload.sample_watermark} title="Sample watermark" />
        </div>
      )}
    </Panel>
  );
}

function formatCell(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}
