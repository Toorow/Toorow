import { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch } from "./lib/apiFetch";
import "./shell/application.css";
import "./renders.css";

type ToolName = "get_card" | "get_report";

export interface SnapshotFreshness { last_pull?: string | null; cadence_hours?: number | null; stale_since?: string | null; }
export interface SnapshotProvenanceEntry { source_system?: string | null; source_field?: string | null; pull_id?: string | null; pull_ids?: string[]; }
export type SnapshotProvenance = SnapshotProvenanceEntry | SnapshotProvenanceEntry[];
export interface SnapshotMeta { freshness?: SnapshotFreshness | null; provenance?: SnapshotProvenance | null; alerts?: unknown[]; }
export interface SnapshotRow {
  id: string; project_id: string; tool_name: ToolName;
  tool_args?: Record<string, unknown> | null; widget_uri?: string | null;
  summary_snippet?: string | null; question?: string | null; identity?: string | null;
  trace_id?: string | null; created_at: string; meta?: SnapshotMeta | null;
}
export interface SnapshotFull extends SnapshotRow { envelope: { schema_version: string; meta?: SnapshotMeta | null; data: Record<string, unknown>; }; }
export interface SnapshotShare { id: string; snapshot_id: string; share_token: string; share_url: string | null; shared_at: string; shared_by: string | null; revoked_at: string | null; }
interface RenderGalleryPageProps { projectId: string; }

type LoadStatus = "loading" | "ready" | "empty" | "error" | "denied";
type GalleryState = { key: string; status: LoadStatus; snapshots: SnapshotRow[]; message: string | null; };
type DetailState = { key: string | null; status: "closed" | "loading" | "ready" | "error" | "denied"; snapshot: SnapshotFull | null; message: string | null; };
type SharesState = { key: string | null; status: LoadStatus; shares: SnapshotShare[]; message: string | null; };

class RenderApiError extends Error {
  constructor(readonly status: number, readonly code: string, message: string) { super(message); this.name = "RenderApiError"; }
}
function isRecord(value: unknown): value is Record<string, unknown> { return typeof value === "object" && value !== null && !Array.isArray(value); }
function optionalString(value: unknown): value is string | null | undefined { return value === undefined || value === null || typeof value === "string"; }
function isoInstant(value: unknown): value is string { return typeof value === "string" && value.length > 0 && Number.isFinite(Date.parse(value)); }
function parseFreshness(value: unknown): SnapshotFreshness | null | undefined {
  if (value === undefined || value === null) return value;
  if (!isRecord(value)) throw new Error("Snapshot freshness must be an object");
  if ((value.last_pull !== undefined && value.last_pull !== null && !isoInstant(value.last_pull)) || (value.stale_since !== undefined && value.stale_since !== null && !isoInstant(value.stale_since))) throw new Error("Snapshot freshness timestamps are invalid");
  if (value.cadence_hours !== undefined && value.cadence_hours !== null && typeof value.cadence_hours !== "number") throw new Error("Snapshot freshness cadence is invalid");
  return { last_pull: value.last_pull, cadence_hours: value.cadence_hours, stale_since: value.stale_since };
}
function parseProvenanceEntry(value: unknown): SnapshotProvenanceEntry {
  if (!isRecord(value)) throw new Error("Snapshot provenance must be an object");
  if (!optionalString(value.source_system) || !optionalString(value.source_field) || !optionalString(value.pull_id) || (value.pull_ids !== undefined && (!Array.isArray(value.pull_ids) || !value.pull_ids.every((item) => typeof item === "string")))) throw new Error("Snapshot provenance is invalid");
  return { source_system: value.source_system, source_field: value.source_field, pull_id: value.pull_id, pull_ids: value.pull_ids };
}
function parseProvenance(value: unknown): SnapshotProvenance | null | undefined {
  if (value === undefined || value === null) return value;
  return Array.isArray(value) ? value.map(parseProvenanceEntry) : parseProvenanceEntry(value);
}
function parseMeta(value: unknown): SnapshotMeta | null | undefined {
  if (value === undefined || value === null) return value;
  if (!isRecord(value)) throw new Error("Snapshot metadata must be an object");
  if (value.alerts !== undefined && !Array.isArray(value.alerts)) throw new Error("Snapshot alerts must be an array");
  return { freshness: parseFreshness(value.freshness), provenance: parseProvenance(value.provenance), alerts: value.alerts };
}
function parseSnapshotRow(value: unknown, projectId: string, snapshotId?: string): SnapshotRow {
  if (!isRecord(value)) throw new Error("Snapshot row must be an object");
  if (typeof value.id !== "string" || !value.id || (snapshotId !== undefined && value.id !== snapshotId) || value.project_id !== projectId || (value.tool_name !== "get_card" && value.tool_name !== "get_report") || !isoInstant(value.created_at) || !optionalString(value.widget_uri) || !optionalString(value.summary_snippet) || !optionalString(value.question) || !optionalString(value.identity) || !optionalString(value.trace_id) || (value.tool_args !== undefined && value.tool_args !== null && !isRecord(value.tool_args))) throw new Error("Snapshot row violates the scoped render contract");
  return { id: value.id, project_id: projectId, tool_name: value.tool_name, tool_args: value.tool_args, widget_uri: value.widget_uri, summary_snippet: value.summary_snippet, question: value.question, identity: value.identity, trace_id: value.trace_id, created_at: value.created_at, meta: parseMeta(value.meta) };
}
function parseSnapshotList(value: unknown, projectId: string): SnapshotRow[] {
  if (!isRecord(value) || !Array.isArray(value.snapshots)) throw new Error("Render gallery response is invalid");
  return value.snapshots.map((row) => parseSnapshotRow(row, projectId));
}
function parseSnapshotDetail(value: unknown, projectId: string, snapshotId: string): SnapshotFull {
  const row = parseSnapshotRow(value, projectId, snapshotId);
  if (!isRecord(value) || !isRecord(value.envelope)) throw new Error("Snapshot envelope is missing");
  const envelope = value.envelope;
  if (typeof envelope.schema_version !== "string" || !envelope.schema_version || !isRecord(envelope.data)) throw new Error("Snapshot envelope violates the canonical contract");
  return { ...row, envelope: { schema_version: envelope.schema_version, meta: parseMeta(envelope.meta), data: envelope.data } };
}
function parseShare(value: unknown, snapshotId: string): SnapshotShare {
  if (!isRecord(value) || typeof value.id !== "string" || !value.id || value.snapshot_id !== snapshotId || typeof value.share_token !== "string" || (value.share_url !== null && typeof value.share_url !== "string") || !isoInstant(value.shared_at) || (value.shared_by !== null && typeof value.shared_by !== "string") || (value.revoked_at !== null && !isoInstant(value.revoked_at))) throw new Error("Snapshot share violates the scoped share contract");
  return { id: value.id, snapshot_id: snapshotId, share_token: value.share_token, share_url: value.share_url, shared_at: value.shared_at, shared_by: value.shared_by, revoked_at: value.revoked_at };
}
function parseShares(value: unknown, snapshotId: string): SnapshotShare[] {
  if (!isRecord(value) || !Array.isArray(value.shares)) throw new Error("Snapshot shares response is invalid");
  return value.shares.map((share) => parseShare(share, snapshotId));
}
function parseCreatedShare(value: unknown): void {
  if (!isRecord(value) || typeof value.share_id !== "string" || !value.share_id || typeof value.share_token !== "string" || !value.share_token || typeof value.share_url !== "string" || !value.share_url) throw new Error("Create-share response is invalid");
}
async function requestJson(path: string, init: RequestInit = {}): Promise<unknown> {
  let response: Response;
  try { response = await apiFetch(path, init); }
  catch (error) { throw new RenderApiError(0, "unreachable", error instanceof Error ? error.message : "Network error"); }
  if (!response.ok) {
    let code = "unavailable"; let message = `HTTP ${response.status}`;
    try { const body = (await response.json()) as unknown; if (isRecord(body)) { if (typeof body.code === "string") code = body.code; if (typeof body.message === "string") message = body.message; } } catch { /* status remains authoritative */ }
    throw new RenderApiError(response.status, code, message);
  }
  if (response.status === 204) return undefined;
  try { return await response.json(); } catch { throw new RenderApiError(200, "invalid_response", "The server returned invalid JSON"); }
}
function denied(error: unknown): boolean { return error instanceof RenderApiError && (error.status === 401 || error.status === 403 || error.status === 404); }
function messageOf(error: unknown, fallback: string): string { return error instanceof Error && error.message ? error.message : fallback; }
function formatDate(iso?: string | null): string { if (!iso) return "Not provided"; return new Intl.DateTimeFormat("en-US", { dateStyle: "medium", timeStyle: "short" }).format(new Date(iso)); }
function freshnessText(freshness?: SnapshotFreshness | null): string {
  if (!freshness) return "Not provided"; const parts: string[] = [];
  if (freshness.last_pull) parts.push(`Last pull ${formatDate(freshness.last_pull)}`);
  if (freshness.stale_since) parts.push(`Stale since ${formatDate(freshness.stale_since)}`);
  if (typeof freshness.cadence_hours === "number") parts.push(`Cadence ${freshness.cadence_hours}h`);
  return parts.length ? parts.join(" · ") : "Recorded without timestamps";
}
function provenanceText(provenance?: SnapshotProvenance | null): string {
  if (!provenance) return "Not provided"; const entries = Array.isArray(provenance) ? provenance : [provenance];
  return entries.map((entry) => { const source = entry.source_system || "Unknown source"; const field = entry.source_field ? ` · ${entry.source_field}` : ""; const pulls = entry.pull_ids?.length ? entry.pull_ids.join(", ") : entry.pull_id || "no pull id"; return `${source}${field} · ${pulls}`; }).join("; ");
}
function GallerySkeleton() { return <div className="renders-skeletons" aria-label="Loading renders" role="status">{[1, 2, 3, 4, 5, 6].map((item) => <div key={item} className="render-skeleton" aria-hidden="true" />)}</div>; }
function RenderState({ kind, title, message, onRetry }: { kind: "empty" | "error" | "denied"; title: string; message: string; onRetry?: () => void; }) {
  return <div className={`renders-state ${kind}`} role={kind === "error" ? "alert" : "status"}><strong>{title}</strong><span>{message}</span>{onRetry && <button type="button" className="secondary-button" onClick={onRetry}>Try again</button>}</div>;
}
type DetailDialogProps = {
  detail: DetailState; shares: SharesState; shareAction: string | null; shareActionError: string | null;
  onClose: () => void; onRetryDetail: () => void; onRetryShares: () => void;
  onCreateShare: () => void; onRevokeShare: (shareId: string) => void;
};
function RenderDetailDialog(props: DetailDialogProps) {
  const { detail, shares, shareAction, shareActionError, onClose, onRetryDetail, onRetryShares, onCreateShare, onRevokeShare } = props;
  const snapshot = detail.snapshot; const meta = snapshot?.envelope.meta;
  const openWidget = useCallback(() => {
    const widgetUri = snapshot?.widget_uri; if (!widgetUri || !snapshot) return;
    const opened = window.open(widgetUri, "_blank", "noreferrer"); if (!opened) return;
    const payload = { type: "mcp:structuredContent" as const, data: snapshot.envelope };
    let attempts = 0; const timer = window.setInterval(() => {
      attempts += 1; if (attempts > 12 || opened.closed) { window.clearInterval(timer); return; }
      try { opened.postMessage(payload, window.location.origin); } catch { window.clearInterval(timer); }
    }, 250);
  }, [snapshot]);
  if (detail.status === "closed") return null;
  return (
    <div className="render-dialog-backdrop" role="presentation" onClick={onClose}>
      <div className="render-dialog" role="dialog" aria-modal="true" aria-label="Render details" onClick={(event) => event.stopPropagation()}>
        <div className="render-dialog-heading"><div><span className="signal-label">Persisted render</span><h2>Render details</h2></div><button type="button" className="quiet-button" onClick={onClose}>Close</button></div>
        {detail.status === "loading" ? <div className="render-detail-loading" role="status">Loading render details…</div>
          : detail.status === "denied" ? <RenderState kind="denied" title="Render unavailable" message={detail.message ?? "You do not have access to this render."} />
          : detail.status === "error" || !snapshot ? <RenderState kind="error" title="Could not load this render" message={detail.message ?? "The persisted render could not be loaded."} onRetry={onRetryDetail} />
          : <>
            <div className="render-detail-grid">
              <div className="render-field"><span className="render-field-label">Type</span><p className="render-field-value">{snapshot.tool_name === "get_card" ? "Card" : "Report"}</p></div>
              <div className="render-field"><span className="render-field-label">Envelope version</span><p className="render-field-value">{snapshot.envelope.schema_version}</p></div>
              <div className="render-field"><span className="render-field-label">Rendered on</span><p className="render-field-value date">{formatDate(snapshot.created_at)}</p></div>
              <div className="render-field"><span className="render-field-label">Frozen freshness</span><p className="render-field-value">{freshnessText(meta?.freshness)}</p></div>
            </div>
            <div className="render-field"><span className="render-field-label">Prompt / purpose</span><p className="render-field-value">{snapshot.question || snapshot.summary_snippet || "Not provided"}</p></div>
            <div className="render-field"><span className="render-field-label">Provenance</span><p className="render-field-value">{provenanceText(meta?.provenance)}</p></div>
            {snapshot.summary_snippet && snapshot.question && <pre className="render-snippet">{snapshot.summary_snippet}</pre>}
            <section className="render-shares" aria-labelledby="render-shares-title">
              <div className="render-shares-heading"><div><h3 id="render-shares-title">Share links</h3><p>Links expose this frozen snapshot and can be revoked.</p></div><button type="button" className="primary-button" disabled={shareAction !== null || shares.status === "denied"} onClick={onCreateShare}>{shareAction === "create" ? "Creating…" : "Create share link"}</button></div>
              {shareActionError && <p className="render-share-error" role="alert">{shareActionError}</p>}
              {shares.status === "loading" ? <p className="render-share-state" role="status">Loading share links…</p>
                : shares.status === "denied" ? <p className="render-share-state denied">Share links are unavailable to your account.</p>
                : shares.status === "error" ? <div className="render-share-state error"><span>{shares.message ?? "Could not load share links."}</span><button type="button" className="quiet-button" onClick={onRetryShares}>Try again</button></div>
                : shares.status === "empty" ? <p className="render-share-state">No share links yet.</p>
                : <ul className="render-share-list">{shares.shares.map((share) => <li key={share.id} className="render-share-row"><div><div className="render-share-status"><span className={`render-share-badge ${share.revoked_at ? "revoked" : "active"}`}>{share.revoked_at ? "Revoked" : "Active"}</span><span>{formatDate(share.shared_at)}</span></div>{share.share_url && !share.revoked_at ? <a href={share.share_url} target="_blank" rel="noreferrer" className="render-share-url">{share.share_url}</a> : <span className="render-share-url">{share.share_url ?? "Link unavailable"}</span>}</div>{!share.revoked_at && <button type="button" className="secondary-button" disabled={shareAction !== null} onClick={() => onRevokeShare(share.id)}>{shareAction === share.id ? "Revoking…" : "Revoke"}</button>}</li>)}</ul>}
            </section>
            <div className="render-dialog-actions">{snapshot.widget_uri && <button className="primary-button" type="button" onClick={openWidget}>Open widget</button>}<button className="secondary-button" type="button" onClick={onClose}>Close</button></div>
          </>}
      </div>
    </div>
  );
}
export default function RenderGalleryPage({ projectId }: RenderGalleryPageProps) {
  const [toolFilter, setToolFilter] = useState<"" | ToolName>("");
  const galleryKey = `${projectId}\u0000${toolFilter}`;
  const [gallery, setGallery] = useState<GalleryState>({ key: galleryKey, status: "loading", snapshots: [], message: null });
  const galleryRequest = useRef(0); const galleryAbort = useRef<AbortController | null>(null);
  const [selectedSnapshotId, setSelectedSnapshotId] = useState<string | null>(null);
  const detailKey = selectedSnapshotId ? `${projectId}\u0000${selectedSnapshotId}` : null;
  const detailKeyRef = useRef<string | null>(detailKey); detailKeyRef.current = detailKey;
  const [detail, setDetail] = useState<DetailState>({ key: null, status: "closed", snapshot: null, message: null });
  const [shares, setShares] = useState<SharesState>({ key: null, status: "empty", shares: [], message: null });
  const detailRequest = useRef(0); const detailAbort = useRef<AbortController | null>(null);
  const sharesRequest = useRef(0); const sharesAbort = useRef<AbortController | null>(null);
  const [shareAction, setShareAction] = useState<string | null>(null);
  const [shareActionError, setShareActionError] = useState<string | null>(null);
  const validProjectScope = projectId.length > 0 && projectId === projectId.trim();

  const loadGallery = useCallback(async () => {
    const key = galleryKey; const requestId = ++galleryRequest.current;
    galleryAbort.current?.abort(); const controller = new AbortController(); galleryAbort.current = controller;
    if (!validProjectScope) { setGallery({ key, status: "denied", snapshots: [], message: "No valid active project scope is available." }); return; }
    setGallery({ key, status: "loading", snapshots: [], message: null });
    const params = new URLSearchParams({ project_id: projectId }); if (toolFilter) params.set("tool_name", toolFilter);
    try {
      const body = await requestJson(`/api/rendus/snapshots?${params.toString()}`, { method: "GET", cache: "no-store", signal: controller.signal });
      const snapshots = parseSnapshotList(body, projectId);
      if (requestId !== galleryRequest.current || controller.signal.aborted) return;
      setGallery({ key, status: snapshots.length ? "ready" : "empty", snapshots, message: null });
    } catch (error) {
      if (requestId !== galleryRequest.current || controller.signal.aborted) return;
      setGallery({ key, status: denied(error) ? "denied" : "error", snapshots: [], message: messageOf(error, "Failed to load renders") });
    }
  }, [galleryKey, projectId, toolFilter, validProjectScope]);
  useEffect(() => { void loadGallery(); return () => { galleryRequest.current += 1; galleryAbort.current?.abort(); }; }, [loadGallery]);

  const loadDetail = useCallback(async (snapshotId: string) => {
    const key = `${projectId}\u0000${snapshotId}`; const requestId = ++detailRequest.current;
    detailAbort.current?.abort(); const controller = new AbortController(); detailAbort.current = controller;
    setDetail({ key, status: "loading", snapshot: null, message: null });
    try {
      const body = await requestJson(`/api/rendus/snapshots/${encodeURIComponent(snapshotId)}?project_id=${encodeURIComponent(projectId)}`, { method: "GET", cache: "no-store", signal: controller.signal });
      const snapshot = parseSnapshotDetail(body, projectId, snapshotId);
      if (requestId !== detailRequest.current || controller.signal.aborted || detailKeyRef.current !== key) return;
      setDetail({ key, status: "ready", snapshot, message: null });
    } catch (error) {
      if (requestId !== detailRequest.current || controller.signal.aborted || detailKeyRef.current !== key) return;
      setDetail({ key, status: denied(error) ? "denied" : "error", snapshot: null, message: messageOf(error, "Failed to load this render") });
    }
  }, [projectId]);

  const loadShares = useCallback(async (snapshotId: string): Promise<boolean> => {
    const key = `${projectId}\u0000${snapshotId}`; const requestId = ++sharesRequest.current;
    sharesAbort.current?.abort(); const controller = new AbortController(); sharesAbort.current = controller;
    setShares({ key, status: "loading", shares: [], message: null });
    try {
      const body = await requestJson(`/api/rendus/snapshots/${encodeURIComponent(snapshotId)}/shares?project_id=${encodeURIComponent(projectId)}`, { method: "GET", cache: "no-store", signal: controller.signal });
      const serverShares = parseShares(body, snapshotId);
      if (requestId !== sharesRequest.current || controller.signal.aborted || detailKeyRef.current !== key) return false;
      setShares({ key, status: serverShares.length ? "ready" : "empty", shares: serverShares, message: null }); return true;
    } catch (error) {
      if (requestId !== sharesRequest.current || controller.signal.aborted || detailKeyRef.current !== key) return false;
      setShares({ key, status: denied(error) ? "denied" : "error", shares: [], message: messageOf(error, "Failed to load share links") }); return false;
    }
  }, [projectId]);

  useEffect(() => {
    if (!selectedSnapshotId || !validProjectScope) { setDetail({ key: null, status: "closed", snapshot: null, message: null }); return; }
    setShareAction(null); setShareActionError(null); void loadDetail(selectedSnapshotId); void loadShares(selectedSnapshotId);
  }, [loadDetail, loadShares, selectedSnapshotId, validProjectScope]);
  useEffect(() => () => { detailRequest.current += 1; sharesRequest.current += 1; detailAbort.current?.abort(); sharesAbort.current?.abort(); }, []);
  const closeDetail = useCallback(() => { detailRequest.current += 1; sharesRequest.current += 1; detailAbort.current?.abort(); sharesAbort.current?.abort(); setSelectedSnapshotId(null); setShareAction(null); setShareActionError(null); }, []);

  const createShare = useCallback(async () => {
    if (!selectedSnapshotId || shareAction !== null) return; const key = `${projectId}\u0000${selectedSnapshotId}`;
    setShareAction("create"); setShareActionError(null);
    try {
      const body = await requestJson(`/api/rendus/snapshots/${encodeURIComponent(selectedSnapshotId)}/share?project_id=${encodeURIComponent(projectId)}`, { method: "POST" });
      parseCreatedShare(body); if (detailKeyRef.current !== key) return;
      if (!(await loadShares(selectedSnapshotId)) && detailKeyRef.current === key) setShareActionError("The share was created, but server state could not be refreshed.");
    } catch (error) { if (detailKeyRef.current === key) setShareActionError(messageOf(error, "Could not create the share link")); }
    finally { if (detailKeyRef.current === key) setShareAction(null); }
  }, [loadShares, projectId, selectedSnapshotId, shareAction]);
  const revokeShare = useCallback(async (shareId: string) => {
    if (!selectedSnapshotId || shareAction !== null) return; const key = `${projectId}\u0000${selectedSnapshotId}`;
    setShareAction(shareId); setShareActionError(null);
    try {
      await requestJson(`/api/rendus/shares/${encodeURIComponent(shareId)}?project_id=${encodeURIComponent(projectId)}`, { method: "DELETE" });
      if (detailKeyRef.current !== key) return;
      if (!(await loadShares(selectedSnapshotId)) && detailKeyRef.current === key) setShareActionError("The share was revoked, but server state could not be refreshed.");
    } catch (error) { if (detailKeyRef.current === key) setShareActionError(messageOf(error, "Could not revoke the share link")); }
    finally { if (detailKeyRef.current === key) setShareAction(null); }
  }, [loadShares, projectId, selectedSnapshotId, shareAction]);

  const activeGallery: GalleryState = gallery.key === galleryKey ? gallery : { key: galleryKey, status: "loading", snapshots: [], message: null };
  const activeDetail: DetailState = !detailKey || !validProjectScope ? { key: null, status: "closed", snapshot: null, message: null } : detail.key === detailKey ? detail : { key: detailKey, status: "loading", snapshot: null, message: null };
  const activeShares: SharesState = detailKey && shares.key === detailKey
    ? shares
    : { key: detailKey, status: "loading", shares: [], message: null };
  const filters: Array<{ value: "" | ToolName; label: string }> = [{ value: "", label: "All" }, { value: "get_card", label: "Cards" }, { value: "get_report", label: "Reports" }];
  return <>
    <div className="page-header"><div><h1>Renders</h1><p>Inspect persisted MCP cards and reports for this project.</p></div><div className="renders-controls"><div className="renders-filters" role="group" aria-label="Filter renders by type">{filters.map((filter) => <button key={filter.value || "all"} type="button" className={`renders-filter${toolFilter === filter.value ? " active" : ""}`} aria-pressed={toolFilter === filter.value} onClick={() => setToolFilter(filter.value)}>{filter.label}</button>)}</div><button type="button" className="renders-refresh" onClick={() => void loadGallery()} disabled={activeGallery.status === "loading"} aria-label="Refresh renders" title="Refresh"><svg width="16" height="16" viewBox="0 0 24 24" aria-hidden="true"><polyline points="23 4 23 10 17 10" /><polyline points="1 20 1 14 7 14" /><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15" /></svg></button></div></div>
    {activeGallery.status === "loading" ? <GallerySkeleton />
      : activeGallery.status === "denied" ? <RenderState kind="denied" title="Renders unavailable" message={activeGallery.message ?? "You do not have access to renders in this project."} />
      : activeGallery.status === "error" ? <RenderState kind="error" title="Could not load renders" message={activeGallery.message ?? "The render API is unavailable."} onRetry={() => void loadGallery()} />
      : activeGallery.status === "empty" ? <RenderState kind="empty" title="No renders yet" message="MCP cards and reports generated for this project will appear here." />
      : <section className="renders-grid" aria-label="Persisted renders">{activeGallery.snapshots.map((snapshot) => <button key={snapshot.id} type="button" className="render-card" onClick={() => setSelectedSnapshotId(snapshot.id)} aria-label={`Open render: ${snapshot.question || snapshot.summary_snippet || "untitled"}`}><div className="render-card-top"><span className="render-kind">{snapshot.tool_name === "get_card" ? "Card" : "Report"}</span><span className="render-freshness">{freshnessText(snapshot.meta?.freshness)}</span></div><h2 className="render-title">{snapshot.question || snapshot.summary_snippet || "Untitled render"}</h2><div className="render-meta"><span className="render-date">{formatDate(snapshot.created_at)}</span><span className="action-link" aria-hidden="true">Details →</span></div></button>)}</section>}
    <RenderDetailDialog detail={activeDetail} shares={activeShares} shareAction={shareAction} shareActionError={shareActionError} onClose={closeDetail} onRetryDetail={() => { if (selectedSnapshotId) void loadDetail(selectedSnapshotId); }} onRetryShares={() => { if (selectedSnapshotId) void loadShares(selectedSnapshotId); }} onCreateShare={() => void createShare()} onRevokeShare={(shareId) => void revokeShare(shareId)} />
  </>;
}