import { useCallback, useEffect, useState } from "react";
import "./shell/application.css";
import "./renders.css";

export interface SnapshotMeta {
  freshness?: string | null;
  provenance?: string | null;
  alerts?: unknown[];
}

export interface SnapshotRow {
  id: string;
  project_id: string;
  tool_name: "get_card" | "get_report";
  tool_args?: Record<string, unknown> | null;
  widget_uri?: string | null;
  summary_snippet?: string | null;
  question?: string | null;
  identity?: string | null;
  trace_id?: string | null;
  created_at: string;
  meta?: SnapshotMeta | null;
}

export interface SnapshotFull extends SnapshotRow {
  envelope: {
    schema_version: string;
    meta?: SnapshotMeta;
    data: Record<string, unknown>;
  };
}

interface RenderGalleryPageProps {
  projectId?: string;
}

function _authHeader(): Record<string, string> {
  const token =
    typeof window !== "undefined"
      ? (window as Window & { __TOOROW_API_KEY__?: string }).__TOOROW_API_KEY__ ??
        localStorage.getItem("api_token") ??
        ""
      : "";
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function _formatDate(iso: string): string {
  try {
    return new Intl.DateTimeFormat("en-US", {
      dateStyle: "short",
      timeStyle: "short",
    }).format(new Date(iso));
  } catch {
    return iso;
  }
}

function _toolLabel(name: string): string {
  if (name === "get_card") return "Card";
  if (name === "get_report") return "Report";
  return name;
}

function _freshnessBadge(freshness?: string | null) {
  if (freshness === "live") {
    return <span className="render-freshness live">Live</span>;
  }
  if (freshness === "stale") {
    return <span className="render-freshness stale">Stale</span>;
  }
  return null;
}

function GallerySkeleton() {
  return (
    <div className="renders-skeletons" aria-hidden="true">
      {[1, 2, 3, 4, 5, 6].map((i) => (
        <div key={i} className="render-skeleton" />
      ))}
    </div>
  );
}

function EmptyState() {
  return (
    <div className="renders-state">
      <strong>No renders yet</strong>
      <span>MCP cards and reports generated for this project will appear here.</span>
    </div>
  );
}

function WidgetModal({
  snapshot,
  onClose,
}: {
  snapshot: SnapshotFull | null;
  projectId: string;
  onClose: () => void;
}) {
  const widgetUri = snapshot?.widget_uri ?? "";

  const handleOpenWidget = useCallback(() => {
    if (!widgetUri || !snapshot) return;
    const w = window.open(widgetUri, "_blank", "noreferrer");
    if (!w) return;
    const payload = { type: "mcp:structuredContent" as const, data: snapshot.envelope };
    const targetOrigin = window.location.origin;
    let attempts = 0;
    const timer = setInterval(() => {
      attempts += 1;
      if (attempts > 12 || w.closed) {
        clearInterval(timer);
        return;
      }
      try {
        w.postMessage(payload, targetOrigin);
      } catch {
        clearInterval(timer);
      }
    }, 250);
  }, [snapshot, widgetUri]);

  if (!snapshot) return null;

  return (
    <div
      className="render-dialog-backdrop"
      role="presentation"
      onClick={onClose}
    >
      <div
        className="render-dialog"
        role="dialog"
        aria-modal="true"
        aria-label="Reopen render"
        onClick={(e) => e.stopPropagation()}
      >
        <h2>Reopen render</h2>

        <div className="render-field">
          <span className="render-field-label">Type</span>
          <p className="render-field-value">{_toolLabel(snapshot.tool_name)}</p>
        </div>

        {snapshot.question && (
          <div className="render-field">
            <span className="render-field-label">Question / report</span>
            <p className="render-field-value">{snapshot.question}</p>
          </div>
        )}

        <div className="render-field">
          <span className="render-field-label">Rendered on</span>
          <p className="render-field-value date">{_formatDate(snapshot.created_at)}</p>
        </div>

        <div className="render-field">
          <span className="render-field-label">Freshness</span>
          {_freshnessBadge(snapshot.meta?.freshness) ?? (
            <p className="render-field-value">—</p>
          )}
        </div>

        {snapshot.summary_snippet && (
          <pre className="render-snippet">{snapshot.summary_snippet}</pre>
        )}

        <div className="render-dialog-actions">
          {widgetUri && (
            <button className="primary-button" type="button" onClick={handleOpenWidget}>
              Open widget
            </button>
          )}
          <button className="secondary-button" type="button" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
    </div>
  );
}

export default function RenderGalleryPage({ projectId = "default" }: RenderGalleryPageProps) {
  const [snapshots, setSnapshots] = useState<SnapshotRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [toolFilter, setToolFilter] = useState<"" | "get_card" | "get_report">("");

  const [openSnap, setOpenSnap] = useState<SnapshotFull | null>(null);
  const [loadingSnap, setLoadingSnap] = useState(false);
  const [snapError, setSnapError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams({ project_id: projectId });
      if (toolFilter) params.set("tool_name", toolFilter);

      const res = await fetch(`/api/snapshots?${params.toString()}`, {
        headers: _authHeader(),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const body = (await res.json()) as { snapshots?: SnapshotRow[] };
      setSnapshots(body.snapshots ?? []);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load renders");
      setSnapshots([]);
    } finally {
      setLoading(false);
    }
  }, [projectId, toolFilter]);

  useEffect(() => {
    void load();
  }, [load]);

  const handleOpen = useCallback(
    async (summary: SnapshotRow) => {
      setLoadingSnap(true);
      try {
        const res = await fetch(
          `/api/snapshots/${encodeURIComponent(summary.id)}?project_id=${encodeURIComponent(projectId)}`,
          { headers: _authHeader() }
        );
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const full = (await res.json()) as SnapshotFull;
        setOpenSnap(full);
      } catch (err) {
        setSnapError(err instanceof Error ? err.message : "Failed to load snapshot");
      } finally {
        setLoadingSnap(false);
      }
    },
    [projectId]
  );

  const filters: Array<{ value: "" | "get_card" | "get_report"; label: string }> = [
    { value: "", label: "All" },
    { value: "get_card", label: "Cards" },
    { value: "get_report", label: "Reports" },
  ];

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Renders</h1>
          <p>Inspect MCP widget cards and reports rendered for this project.</p>
        </div>

        <div className="renders-controls">
          <div className="renders-filters" role="group" aria-label="Filter renders by type">
            {filters.map((f) => (
              <button
                key={f.value || "all"}
                type="button"
                className={`renders-filter${toolFilter === f.value ? " active" : ""}`}
                aria-pressed={toolFilter === f.value}
                onClick={() => setToolFilter(f.value)}
              >
                {f.label}
              </button>
            ))}
          </div>

          <button
            type="button"
            className="renders-refresh"
            onClick={load}
            aria-label="Refresh renders"
            title="Refresh"
          >
            <svg width="16" height="16" viewBox="0 0 24 24" aria-hidden="true">
              <polyline points="23 4 23 10 17 10" />
              <polyline points="1 20 1 14 7 14" />
              <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15" />
            </svg>
          </button>
        </div>
      </div>

      {loading ? (
        <GallerySkeleton />
      ) : error ? (
        <div className="renders-state error" role="alert">
          <strong>Could not load renders</strong>
          <span>{error}</span>
        </div>
      ) : snapshots.length === 0 ? (
        <EmptyState />
      ) : (
        <section className="renders-grid">
          {snapshots.map((snap) => (
            <button
              key={snap.id}
              type="button"
              className="render-card"
              onClick={() => void handleOpen(snap)}
              aria-label={`Reopen render: ${snap.question || snap.summary_snippet || "untitled"}`}
            >
              <div className="render-card-top">
                <span className="render-kind">{_toolLabel(snap.tool_name)}</span>
                {_freshnessBadge(snap.meta?.freshness)}
              </div>
              <h2 className="render-title">
                {snap.question || snap.summary_snippet || "—"}
              </h2>
              <div className="render-meta">
                <span className="render-date">{_formatDate(snap.created_at)}</span>
                <span className="action-link" aria-hidden="true">
                  Reopen →
                </span>
              </div>
            </button>
          ))}
        </section>
      )}

      {loadingSnap && (
        <div className="render-loading-overlay" role="status" aria-label="Loading render">
          <div className="render-spinner" />
        </div>
      )}

      {openSnap && (
        <WidgetModal snapshot={openSnap} projectId={projectId} onClose={() => setOpenSnap(null)} />
      )}

      {snapError && (
        <div className="render-toast" role="alert">
          <span>{snapError}</span>
          <button type="button" onClick={() => setSnapError(null)} aria-label="Dismiss">
            Dismiss
          </button>
        </div>
      )}
    </>
  );
}
