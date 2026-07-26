import { useEffect, useMemo, useState } from "react";
import type { DatastreamReadModel, PublishedExecution } from "../../datastreams/ops/opsTypes";
import { apiFetch } from "../../lib/apiFetch";
import "../application.css";
import "./datastream-overview.css";

const PROVIDER_LOGOS: Record<string, { src: string; alt: string }> = {
  meta: { src: "/connectors/meta.svg", alt: "Meta" },
  "meta-ads": { src: "/connectors/meta.svg", alt: "Meta" },
  "google-ads": { src: "/connectors/google-ads.png", alt: "Google Ads" },
  "google-analytics": { src: "/connectors/google-analytics.png", alt: "Google Analytics" },
  "google-sheets": { src: "/connectors/google-sheets.png", alt: "Google Sheets" },
};

type Tab = "overview" | "data" | "mapping" | "runs";
interface DatastreamOverviewProps { projectId: string; datastreamId: string; onNavigateTab?: (tab: Tab) => void; }
type FetchState =
  | { status: "loading"; key: string }
  | { status: "ok"; key: string; model: DatastreamReadModel }
  | { status: "error"; key: string; message: string };

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function isNullableString(value: unknown): value is string | null { return value === null || typeof value === "string"; }
function validExecution(value: unknown): boolean {
  return value === null || (isRecord(value) && typeof value.id === "string" && (value.state === undefined || isNullableString(value.state)));
}
function validateReadModel(value: unknown, datastreamId: string, projectId: string): DatastreamReadModel {
  if (!isRecord(value) || value.datastream_id !== datastreamId || value.project_id !== projectId || !isRecord(value.datastream) || value.datastream.id !== datastreamId) {
    throw new Error("The health response did not match the requested project and Datastream.");
  }
  for (const field of ["plan_versions", "mapping_versions", "publication_log", "recent_imports"] as const) {
    if (!Array.isArray(value[field]) || !value[field].every(isRecord)) throw new Error("The health response contract was invalid.");
  }
  if (!isNullableString(value.current_published_execution_id) || !validExecution(value.published_execution) || !validExecution(value.current_candidate) || !validExecution(value.latest_execution)) {
    throw new Error("The health response contract was invalid.");
  }
  const published = value.published_execution;
  const latest = value.latest_execution;
  if (published && isRecord(published) && value.current_published_execution_id !== published.id) {
    throw new Error("The published execution did not match its authoritative pointer.");
  }
  if (published && latest && isRecord(published) && isRecord(latest) && published.id === latest.id && String(latest.state ?? "").toLowerCase() !== "published") {
    throw new Error("The execution evidence was internally inconsistent.");
  }
  return value as unknown as DatastreamReadModel;
}

async function fetchReadModel(datastreamId: string, projectId: string): Promise<DatastreamReadModel> {
  const query = new URLSearchParams({ project_id: projectId });
  const response = await apiFetch(`/api/datastreams/${encodeURIComponent(datastreamId)}/read-model?${query.toString()}`, { cache: "no-store" });
  if (!response.ok) {
    const body = await response.json().catch(() => null) as { message?: string } | null;
    throw new Error(body?.message || `Datastream health could not be loaded (HTTP ${response.status}).`);
  }
  return validateReadModel(await response.json() as unknown, datastreamId, projectId);
}
function formatDateTime(value: string | null | undefined): string {
  if (!value) return "Unknown";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Unknown";
  return date.toLocaleString("en-GB", { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
}
function publicationAge(execution: PublishedExecution | null): string {
  if (!execution?.state_changed_at) return "Unknown";
  const changedAt = new Date(execution.state_changed_at).getTime();
  if (Number.isNaN(changedAt) || changedAt > Date.now()) return "Unknown";
  const minutes = Math.floor((Date.now() - changedAt) / 60_000);
  if (minutes < 60) return `${minutes}m`;
  if (minutes < 2_880) return `${Math.floor(minutes / 60)}h`;
  return `${Math.floor(minutes / 1_440)}d`;
}
function executionTone(execution: PublishedExecution | null, hasPublication: boolean) {
  const state = (execution?.state ?? "").toLowerCase();
  if (!execution) return { tone: "warning", label: hasPublication ? "Current state unknown" : "No publication history" };
  if (["failed", "cancelled"].includes(state)) return { tone: "error", label: "Latest processing failed" };
  if (state === "degraded") return { tone: "warning", label: "Latest processing degraded" };
  if (state === "published" && hasPublication) return { tone: "success", label: "Published" };
  return { tone: "warning", label: `Processing ${state || "unknown"}` };
}
function tabHref(projectId: string, datastreamId: string, tab: Tab): string {
  return `/p/${encodeURIComponent(projectId)}/data/datastreams/o/datastream/${encodeURIComponent(datastreamId)}/${tab}`;
}

export default function DatastreamOverview({ projectId, datastreamId, onNavigateTab }: DatastreamOverviewProps) {
  const [reload, setReload] = useState(0);
  const key = `${projectId}\u0000${datastreamId}\u0000${reload}`;
  const [state, setState] = useState<FetchState>({ status: "loading", key });
  const visibleState: FetchState = state.key === key ? state : { status: "loading", key };

  useEffect(() => {
    let cancelled = false;
    setState({ status: "loading", key });
    void fetchReadModel(datastreamId, projectId)
      .then((model) => { if (!cancelled) setState({ status: "ok", key, model }); })
      .catch((error: unknown) => { if (!cancelled) setState({ status: "error", key, message: error instanceof Error ? error.message : "Datastream health could not be loaded." }); });
    return () => { cancelled = true; };
  }, [datastreamId, key, projectId]);

  const model = visibleState.status === "ok" ? visibleState.model : null;
  const published = model?.published_execution ?? null;
  const latest = model?.latest_execution ?? model?.current_candidate ?? published;
  const signal = executionTone(latest, Boolean(published));
  const datastream = model?.datastream;
  const name = datastream?.name?.trim() || datastreamId;
  const moduleName = datastream?.module_name?.trim() || datastream?.source_kind?.trim() || "Source unavailable";
  const logo = PROVIDER_LOGOS[datastream?.module_name ?? ""];
  const runEvidence = useMemo(() => {
    if (model?.datastream.source_kind !== "managed_feed") return null;
    const rows = model.recent_imports.filter((row) => ["published", "noop", "rejected", "failed"].includes((row.outcome ?? "").toLowerCase()));
    if (rows.length === 0) return null;
    return { succeeded: rows.filter((row) => ["published", "noop"].includes((row.outcome ?? "").toLowerCase())).length, total: rows.length };
  }, [model]);
  const publishedPlan = model?.plan_versions.find((version) => version.id === published?.plan_version_id);
  const publishedMapping = model?.mapping_versions.find((version) => version.id === published?.mapping_version_id);
  const versions = published ? [publishedPlan?.version_number == null ? published.plan_version_id : `plan_v${publishedPlan.version_number}`, publishedMapping?.version_number == null ? published.mapping_version_id : `mapping_v${publishedMapping.version_number}`].filter(Boolean).join(" · ") || "Unknown" : "Not published";
  const dqState = published?.dq_state ?? published?.dq?.state ?? null;
  const navigate = (event: React.MouseEvent<HTMLAnchorElement>, tab: Tab) => {
    if (!onNavigateTab || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    onNavigateTab(tab);
  };
  const noHistory = Boolean(model && !model.current_published_execution_id && !published && !latest && model.recent_imports.length === 0);
  const brokenPointer = Boolean(model?.current_published_execution_id && !published);

  return <>
    <div className="topbar-left ds-object-header"><div className="object"><div className="object-logo provider-logo">{logo ? <img src={logo.src} alt={logo.alt} /> : <span aria-hidden="true">{name.slice(0, 1).toUpperCase()}</span>}</div><div><strong>{name}</strong><span>{moduleName}</span></div></div></div>
    <nav className="local-tabs" aria-label="Datastream">
      {(["overview", "data", "mapping", "runs"] as const).map((tab) => <a key={tab} className={`local-tab${tab === "overview" ? " active" : ""}`} href={tabHref(projectId, datastreamId, tab)} aria-current={tab === "overview" ? "page" : undefined} onClick={(event) => navigate(event, tab)}>{tab[0].toUpperCase() + tab.slice(1)}</a>)}
      <span className="local-tab" aria-disabled="true">Processing unavailable</span><span className="local-tab" aria-disabled="true">Outputs unavailable</span>
    </nav>
    <div className="full-main">
      <div className="page-header"><div><h1>Datastream health</h1><p>Authorized publication and processing evidence for {name}.</p></div>{visibleState.status === "ok" && <div className="header-actions"><span className={`signal-label ${signal.tone}`}><span className="signal-mark" />{signal.label}</span><a className="secondary-button action-link" href={tabHref(projectId, datastreamId, "mapping")} onClick={(event) => navigate(event, "mapping")}>Configure mapping</a><a className="primary-button action-link primary" href={tabHref(projectId, datastreamId, "data")} onClick={(event) => navigate(event, "data")}>View bounded data</a></div>}</div>
      {visibleState.status === "loading" && <section className="panel" role="status"><h2>Loading health evidence…</h2><p>No health conclusion is shown until the authorized read completes.</p></section>}
      {visibleState.status === "error" && <section className="panel" role="alert"><h2>Health evidence unavailable</h2><p>{visibleState.message}</p><button className="secondary-button" type="button" onClick={() => setReload((value) => value + 1)}>Retry</button></section>}
      {visibleState.status === "ok" && noHistory && <section className="panel" data-testid="no-publication-history"><h2>No publication history</h2><p>This Datastream has no authorized publication, processing, or import evidence yet. Health is not known.</p></section>}
      {visibleState.status === "ok" && brokenPointer && <section className="panel" role="alert" data-testid="broken-publication-pointer"><h2>Publication reference unavailable</h2><p>The authoritative publication pointer could not be resolved. Published health is unknown.</p></section>}
      {visibleState.status === "ok" && published && latest?.id !== published.id && ["failed", "degraded", "cancelled"].includes((latest?.state ?? "").toLowerCase()) && <section className="panel" role="alert" data-testid="last-known-good-limitation"><h2>Published data remains available</h2><p>The latest processing state is {latest?.state}; the evidence below remains bound to publication {published.id}.</p><a href={tabHref(projectId, datastreamId, "runs")} onClick={(event) => navigate(event, "runs")}>Inspect the responsible run</a></section>}
      {visibleState.status === "ok" && <>
        <section className="metric-strip">
          <div className="metric"><span>Publication age</span><strong>{publicationAge(published)}</strong><small>From published state change</small></div>
          <div className="metric"><span>Run success</span><strong>{runEvidence ? `${runEvidence.succeeded} / ${runEvidence.total}` : "Unknown"}</strong><small>{runEvidence ? `Bounded to ${runEvidence.total} terminal managed-feed ledger entries` : "No authoritative run denominator available"}</small></div>
          <div className="metric"><span>Published row volume</span><strong>{published?.row_count == null ? "Unknown" : published.row_count.toLocaleString("en-US")}</strong><small>No expected-range policy available</small></div>
          <div className="metric"><span>Next run</span><strong>{formatDateTime(datastream?.next_run_at)}</strong><small>{datastream?.next_run_at ? "Scheduler evidence" : "No scheduler evidence"}</small></div>
        </section>
        <section className="health-grid"><article className="panel health-chart"><div className="section-header"><div><h2>Recent collection evidence</h2><p>Returned ledger entries; not a fabricated 30-day series.</p></div></div>{model.recent_imports.length === 0 ? <p>No collection history was returned.</p> : <div className="table-scroll"><table className="table"><thead><tr><th>Date</th><th>Outcome</th><th className="amount">Imported rows</th><th className="amount">Rejected</th></tr></thead><tbody>{model.recent_imports.slice(0, 10).map((row, index) => <tr key={row.id ?? `${row.snapshot_observed_at ?? row.created_at}-${index}`}><td>{formatDateTime(row.snapshot_observed_at ?? row.created_at)}</td><td>{row.outcome || "Unknown"}</td><td className="number amount">{row.row_count == null ? "Unknown" : row.row_count.toLocaleString("en-US")}</td><td className="number amount">{row.rejected_row_count == null ? "Unknown" : row.rejected_row_count.toLocaleString("en-US")}</td></tr>)}</tbody></table></div>}</article><div className="health-side"><article className="health-card"><h3>Completeness / DQ</h3><span className="value">{dqState || "Unknown"}</span><p>{dqState ? "Reported by the published execution." : "No completeness evidence is available."}</p></article><article className="health-card"><h3>Mapping health</h3><span className="value">{published?.mapping_version_id ? "Versioned" : "Unknown"}</span><p>{published?.mapping_version_id || "No published mapping version is available."}</p></article></div></section>
        <section className="detail-strip"><div className="detail-cell"><span>Published through</span><b>{formatDateTime(published?.state_changed_at)}</b></div><div className="detail-cell"><span>Current published versions</span><b className="event">{versions}</b></div><div className="detail-cell"><span>Last-known-good</span><b>{published ? formatDateTime(published.state_changed_at) : "None"}</b></div></section>
      </>}
    </div>
  </>;
}