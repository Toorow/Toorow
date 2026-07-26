import { useEffect, useMemo, useState, type MouseEvent } from "react";
import { apiFetch } from "../../lib/apiFetch";
import "../application.css";
import "./datastream-data.css";

type Stage = "collected" | "mapped" | "processed" | "published";
type Tab = "overview" | "data" | "mapping" | "runs";
type SampleCell = string | number | boolean | null;

interface SampleDay {
  date: string;
  sampled_row_count: number;
  rejection_count: number | null;
  field_count: number;
  rows: Record<string, SampleCell>[];
}

interface SampleResponse {
  datastream_id: string;
  project_id: string;
  datastream: { id: string; name?: string | null; module_name?: string | null; source_kind?: string | null };
  stage: Stage;
  served_stage: Stage;
  stage_note: string | null;
  collection_expected: boolean;
  materialization_available: boolean;
  sample_watermark: string | null;
  date_from: string;
  date_to: string;
  limit: number;
  masked_fields: string[];
  masked_value_count: number;
  version_binding_available: boolean;
  days: SampleDay[];
}

type LoadState =
  | { status: "loading"; key: string }
  | { status: "ok"; key: string; data: SampleResponse }
  | { status: "error"; key: string; message: string };

interface DatastreamDataProps {
  projectId: string;
  datastreamId: string;
  onNavigateTab?: (tab: Tab) => void;
}

const STAGES: readonly Stage[] = ["collected", "mapped", "processed", "published"];
const DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function isStage(value: unknown): value is Stage { return typeof value === "string" && STAGES.includes(value as Stage); }
function isNullableString(value: unknown): value is string | null { return value === null || typeof value === "string"; }
function isNonNegativeInteger(value: unknown): value is number { return Number.isInteger(value) && Number(value) >= 0; }
function isSampleCell(value: unknown): value is SampleCell {
  return value === null || ["string", "number", "boolean"].includes(typeof value);
}
function isIsoDay(value: unknown): value is string {
  if (typeof value !== "string" || !DATE_PATTERN.test(value)) return false;
  const date = new Date(`${value}T00:00:00Z`);
  return !Number.isNaN(date.getTime()) && date.toISOString().slice(0, 10) === value;
}

function validateSampleResponse(
  value: unknown,
  expected: { projectId: string; datastreamId: string; stage: Stage; from: string; to: string; limit: number },
): SampleResponse {
  if (!isRecord(value) || value.project_id !== expected.projectId || value.datastream_id !== expected.datastreamId) {
    throw new Error("The sample response did not match the requested project and Datastream.");
  }
  if (value.stage !== expected.stage || value.date_from !== expected.from || value.date_to !== expected.to || value.limit !== expected.limit) {
    throw new Error("The sample response did not match the requested stage and interval.");
  }
  if (!isStage(value.stage) || !isStage(value.served_stage) || !isRecord(value.datastream) || value.datastream.id !== expected.datastreamId) {
    throw new Error("The sample response contract was invalid.");
  }
  if (!isNullableString(value.stage_note) || typeof value.collection_expected !== "boolean" || typeof value.materialization_available !== "boolean" || !isNullableString(value.sample_watermark) || (value.sample_watermark !== null && !isIsoDay(value.sample_watermark)) || typeof value.version_binding_available !== "boolean") {
    throw new Error("The sample response contract was invalid.");
  }
  if (!Array.isArray(value.masked_fields) || !value.masked_fields.every((field) => typeof field === "string") || !isNonNegativeInteger(value.masked_value_count) || !Array.isArray(value.days)) {
    throw new Error("The sample response contract was invalid.");
  }
  for (const day of value.days) {
    if (!isRecord(day) || !isIsoDay(day.date) || !isNonNegativeInteger(day.sampled_row_count) || !(day.rejection_count === null || isNonNegativeInteger(day.rejection_count)) || !isNonNegativeInteger(day.field_count) || !Array.isArray(day.rows)) {
      throw new Error("The sample response contained invalid daily evidence.");
    }
    for (const row of day.rows) {
      if (!isRecord(row) || !Object.values(row).every(isSampleCell)) {
        throw new Error("The sample response contained invalid row evidence.");
      }
    }
    if (day.sampled_row_count !== day.rows.length || day.sampled_row_count > expected.limit) {
      throw new Error("The sample response exceeded its bounded row contract.");
    }
  }
  return value as unknown as SampleResponse;
}

function localIsoDate(date: Date): string {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}
function formatDay(value: string | null | undefined): string {
  if (!value || !isIsoDay(value)) return "Unknown";
  const date = new Date(`${value}T00:00:00`);
  return date.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
}
function titleCase(value: string): string { return value.replace(/[_-]+/g, " ").replace(/\b\w/g, (char) => char.toUpperCase()); }
function renderCell(value: SampleCell): string {
  if (value == null) return "—";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number") return value.toLocaleString("en-US");
  return value;
}
function columnsFor(rows: Record<string, SampleCell>[]): string[] {
  const seen = new Set<string>();
  for (const row of rows) for (const key of Object.keys(row)) seen.add(key);
  return [...seen];
}
function tabHref(projectId: string, datastreamId: string, tab: Tab): string {
  return `/p/${encodeURIComponent(projectId)}/data/datastreams/o/datastream/${encodeURIComponent(datastreamId)}/${tab}`;
}
function requestKey(projectId: string, datastreamId: string, request: { stage: Stage; from: string; to: string; sequence: number }): string {
  return [projectId, datastreamId, request.stage, request.from, request.to, request.sequence].join("\u0000");
}

export default function DatastreamData({ projectId, datastreamId, onNavigateTab }: DatastreamDataProps) {
  const initialRange = useMemo(() => {
    const to = new Date();
    const from = new Date(to);
    from.setDate(to.getDate() - 6);
    return { from: localIsoDate(from), to: localIsoDate(to) };
  }, []);
  const [stage, setStage] = useState<Stage>("published");
  const [dateFrom, setDateFrom] = useState(initialRange.from);
  const [dateTo, setDateTo] = useState(initialRange.to);
  const [request, setRequest] = useState({ stage: "published" as Stage, ...initialRange, sequence: 0 });
  const key = requestKey(projectId, datastreamId, request);
  const [state, setState] = useState<LoadState>({ status: "loading", key });
  const visibleState: LoadState = state.key === key ? state : { status: "loading", key };

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 15_000);
    setState({ status: "loading", key });
    const query = new URLSearchParams({
      project_id: projectId,
      stage: request.stage,
      date_from: request.from,
      date_to: request.to,
      limit: "5",
    });
    void apiFetch(`/api/datastreams/${encodeURIComponent(datastreamId)}/sample?${query.toString()}`, { cache: "no-store", signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) {
          const body = await response.json().catch(() => null) as { message?: string } | null;
          throw new Error(body?.message || `Bounded sample could not be loaded (HTTP ${response.status}).`);
        }
        const body: unknown = await response.json();
        return validateSampleResponse(body, { projectId, datastreamId, stage: request.stage, from: request.from, to: request.to, limit: 5 });
      })
      .then((data) => { if (!cancelled) setState({ status: "ok", key, data }); })
      .catch((error: unknown) => {
        if (!cancelled) setState({ status: "error", key, message: error instanceof Error ? error.message : "Bounded sample could not be loaded." });
      })
      .finally(() => window.clearTimeout(timeout));
    return () => { cancelled = true; controller.abort(); window.clearTimeout(timeout); };
  }, [datastreamId, key, projectId, request]);

  const data = visibleState.status === "ok" ? visibleState.data : null;
  const name = data?.datastream.name?.trim() || datastreamId;
  const source = data?.datastream.module_name?.trim() || data?.datastream.source_kind?.trim() || "Source unavailable";
  const totalSamples = data?.days.reduce((sum, day) => sum + day.sampled_row_count, 0) ?? 0;
  const navigate = (event: MouseEvent<HTMLAnchorElement>, tab: Tab) => {
    if (!onNavigateTab || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    onNavigateTab(tab);
  };
  const load = () => {
    if (!isIsoDay(dateFrom) || !isIsoDay(dateTo)) {
      setState({ status: "error", key, message: "Both interval dates are required and must be valid." });
      return;
    }
    if (dateFrom > dateTo) {
      setState({ status: "error", key, message: "The interval start must not be after its end." });
      return;
    }
    setRequest({ stage, from: dateFrom, to: dateTo, sequence: request.sequence + 1 });
  };

  return (
    <>
      <div className="object" style={{ marginBottom: 22 }}>
        <div className="object-logo provider-logo"><span aria-hidden="true">{name.slice(0, 1).toUpperCase()}</span></div>
        <div><strong>{name}</strong><span>{source}</span></div>
      </div>

      <nav className="local-tabs" aria-label="Datastream">
        {(["overview", "data", "mapping", "runs"] as const).map((tab) => (
          <a key={tab} className={`local-tab${tab === "data" ? " active" : ""}`} href={tabHref(projectId, datastreamId, tab)} aria-current={tab === "data" ? "page" : undefined} onClick={(event) => navigate(event, tab)}>{titleCase(tab)}</a>
        ))}
        <span className="local-tab" aria-disabled="true">Processing unavailable</span>
        <span className="local-tab" aria-disabled="true">Outputs unavailable</span>
      </nav>

      <div className="full-main">
        <div className="page-header"><div><h1>Data sample</h1><p>Deterministic first-N evidence per day. Samples are not published-volume totals.</p></div></div>

        <div className="sample-toolbar">
          <label>Date from <input aria-label="Date from" type="date" required value={dateFrom} onChange={(event) => setDateFrom(event.target.value)} /></label>
          <label>Date to <input aria-label="Date to" type="date" required value={dateTo} onChange={(event) => setDateTo(event.target.value)} /></label>
          <label>Stage <select aria-label="Stage" value={stage} onChange={(event) => setStage(event.target.value as Stage)}>{STAGES.map((value) => <option key={value} value={value}>{titleCase(value)}</option>)}</select></label>
          <div className="right">
            <button className="secondary-button" type="button" disabled title="A durable governed export job is not available yet.">Export Excel unavailable</button>
            <button className="primary-button" type="button" onClick={load}>Load sample</button>
          </div>
        </div>
        <p className="mask-note">Excel export stays unavailable until a durable authorized job can bind the project, Datastream, stage, interval, filters and publication version.</p>

        {visibleState.status === "loading" && <section className="panel" role="status"><h2>Loading bounded sample…</h2><p>No sample evidence is substituted while the request is pending.</p></section>}
        {visibleState.status === "error" && <section className="panel" role="alert"><h2>Sample evidence unavailable</h2><p>{visibleState.message}</p><button className="secondary-button" type="button" onClick={load}>Retry</button></section>}

        {data && (
          <>
            {data.stage_note && <div className="sample-note" role="status"><b>Requested {titleCase(data.stage)}.</b> {data.stage_note}</div>}
            {!data.collection_expected && <section className="panel" data-testid="collection-not-expected"><h2>Collection is not enabled</h2><p>Historical evidence may remain inspectable, but no new collection is currently expected.</p></section>}
            {data.collection_expected && !data.materialization_available && <section className="panel" data-testid="materialization-missing"><h2>Sample materialisation unavailable</h2><p>The Datastream is enabled, but no readable governed materialisation exists for this interval.</p></section>}
            {data.collection_expected && data.materialization_available && totalSamples === 0 && <section className="panel" data-testid="zero-sample-rows"><h2>No eligible rows</h2><p>The materialisation exists, but the selected stage and interval returned zero sample rows.</p></section>}

            <div className="sample-note">
              <span className="signal-label"><span className="signal-mark" />Sample watermark (last day with sampled rows): {formatDay(data.sample_watermark)}</span>{" "}
              <span>{data.version_binding_available ? "Version-bound sample" : "No version-bound sample available"}</span>{" "}
              <span className="mask-note"><b>{data.masked_value_count.toLocaleString("en-US")} values masked across {data.masked_fields.length} sensitive fields</b></span>
            </div>

            {data.materialization_available && totalSamples > 0 && <div className="sample-days">
              {data.days.map((day) => {
                const columns = columnsFor(day.rows);
                return <section key={day.date} className="sample-day">
                  <div className="sample-day-header"><strong>{formatDay(day.date)}</strong><span className="summary">First {day.sampled_row_count} eligible rows</span><div className="day-stats"><span><b className="number">{day.sampled_row_count}</b> sampled</span><span><b className="number">{day.rejection_count == null ? "Unknown" : day.rejection_count}</b> rejected</span><span><b className="number">{day.field_count}</b> fields</span></div></div>
                  {day.rows.length === 0 ? <p>No eligible rows on this day.</p> : <div className="table-scroll"><table className="table sample-table"><thead><tr>{columns.map((column) => <th key={column}>{titleCase(column)}</th>)}</tr></thead><tbody>{day.rows.map((row, rowIndex) => <tr key={rowIndex}>{columns.map((column) => <td key={column}>{renderCell(row[column] ?? null)}</td>)}</tr>)}</tbody></table></div>}
                </section>;
              })}
            </div>}
          </>
        )}
      </div>
    </>
  );
}