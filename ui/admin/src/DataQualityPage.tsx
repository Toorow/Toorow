/**
 * DataQualityPage — Governance › Data quality surface, restyled onto the v3
 * design system (Sources.tsx / ProjectMapping.tsx vocabulary).
 *
 * Endpoints (all verified against server/core/dq_api.py DQ_ROUTES):
 *   GET  /api/dq/summary
 *   GET  /api/dq/issues
 *   POST /api/dq/issues/{firing_id}/acknowledge
 *   GET  /api/dq/datastream-freshness
 *   POST /api/dq/evaluate
 *
 * HONESTY CONTRACT (this file used to break it):
 *   A failed /api/dq/issues call used to `setIssues([])`, which the table renders
 *   as "No issues detected — all of your monitors are green". An outage read as a
 *   clean bill of health. Same for freshness. Every load now carries its own
 *   error state and a FAILURE IS SAID: the panel shows "Could not load …" and NO
 *   table at all. An empty list returned by the API is still shown as empty —
 *   that is a truth, not a fallback.
 *
 *   The monitor-definition strip likewise no longer asserts "Five universal
 *   monitors" with hard-coded thresholds as if they were this project's
 *   configuration: it is derived from the monitors the API actually returned and
 *   is labelled as platform defaults.
 */
import { useCallback, useEffect, useState } from "react";
import { Alert, CircularProgress, Skeleton, Snackbar } from "@mui/material";

import MonitorCard from "./qualite/MonitorCard";
import IssuesTable from "./qualite/IssuesTable";
import CacheHealthCard from "./cache/CacheHealthCard";
import {
  DatastreamFreshness,
  DQIssue,
  DQSummaryResponse,
  MonitorSummary,
  MONITOR_META,
} from "./qualite/types";
import { apiFetch } from "./lib/apiFetch";
import "./shell/application.css";
import "./data-quality.css";

async function fetchSummary(projectId: string): Promise<DQSummaryResponse> {
  const res = await apiFetch(`/api/dq/summary?project_id=${encodeURIComponent(projectId)}`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json() as Promise<DQSummaryResponse>;
}

async function fetchIssues(projectId: string, monitor?: string | null): Promise<DQIssue[]> {
  const params = new URLSearchParams({ project_id: projectId });
  if (monitor) params.set("monitor", monitor);
  const res = await apiFetch(`/api/dq/issues?${params.toString()}`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const data = await res.json();
  return (data as { issues: DQIssue[] }).issues ?? [];
}

async function acknowledgeIssue(projectId: string, firingId: string): Promise<void> {
  const res = await apiFetch(
    `/api/dq/issues/${encodeURIComponent(firingId)}/acknowledge?project_id=${encodeURIComponent(projectId)}`,
    { method: "POST" }
  );
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
}

async function fetchFreshness(projectId: string): Promise<DatastreamFreshness[]> {
  const res = await apiFetch(
    `/api/dq/datastream-freshness?project_id=${encodeURIComponent(projectId)}`
  );
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const data = await res.json();
  return (data as { datastreams: DatastreamFreshness[] }).datastreams ?? [];
}

async function triggerEvaluate(projectId: string): Promise<void> {
  const res = await apiFetch(`/api/dq/evaluate?project_id=${encodeURIComponent(projectId)}`, {
    method: "POST",
  });
  if (res.status === 429) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.message ?? "Rate limit reached.");
  }
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
}

/** A load failure, said out loud — never an empty list standing in for one. */
function LoadFailure({ what, detail }: { what: string; detail: string | null }) {
  return (
    <p className="dq-empty dq-error" role="alert">
      Could not load {what}. This is a loading failure, not a clean result —
      nothing is being reported below.
      {detail ? <span className="dq-error-detail"> ({detail})</span> : null}
    </p>
  );
}

// ---------------------------------------------------------------------------
// Freshness state -> v3 signal-label class + label.
// ---------------------------------------------------------------------------

function freshnessSignal(status: DatastreamFreshness["last_status"]): {
  cls: "success" | "warning" | "error";
  label: string;
} {
  switch (status) {
    case "success":
      return { cls: "success", label: "Healthy" };
    case "partial":
      return { cls: "warning", label: "Partial" };
    case "error":
      return { cls: "error", label: "Failed" };
    default:
      return { cls: "warning", label: "Unknown" };
  }
}

function MonitorCardSkeleton() {
  return <Skeleton variant="rounded" width={220} height={120} sx={{ borderRadius: 3 }} />;
}

function FreshnessTable({
  datastreams,
  loading,
  error,
}: {
  datastreams: DatastreamFreshness[];
  loading: boolean;
  error: string | null;
}) {
  if (loading) {
    return (
      <div className="dq-table-loading">
        <Skeleton variant="text" width="60%" />
        <Skeleton variant="text" width="80%" />
      </div>
    );
  }
  if (error) {
    return <LoadFailure what="datastream freshness" detail={error} />;
  }
  if (datastreams.length === 0) {
    return <p className="dq-empty">No datastreams registered yet.</p>;
  }
  return (
    <div className="table-scroll" tabIndex={0} aria-label="Datastream freshness">
      <table className="dq-table">
        <thead>
          <tr>
            <th>Datastream</th>
            <th>Last pull</th>
            <th>Last success</th>
            <th className="dq-num">Rows</th>
            <th>State</th>
          </tr>
        </thead>
        <tbody>
          {datastreams.map((d) => {
            const sig = freshnessSignal(d.last_status);
            return (
              <tr key={d.datastream_id}>
                <td>
                  <strong>{d.datastream_name}</strong>
                </td>
                <td className="dq-num">
                  {d.last_pull_at ? new Date(d.last_pull_at).toLocaleString() : "Never"}
                </td>
                <td className="dq-num">
                  {d.last_success_at ? new Date(d.last_success_at).toLocaleString() : "Never"}
                </td>
                <td className="dq-num">
                  {d.row_count != null ? d.row_count.toLocaleString("en-US") : "—"}
                </td>
                <td>
                  <span className={`signal-label ${sig.cls}`}>
                    <span className="signal-mark" />
                    {sig.label}
                  </span>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

interface DataQualityPageProps {
  projectId?: string;
}

export default function DataQualityPage({ projectId }: DataQualityPageProps) {
  const [monitors, setMonitors] = useState<MonitorSummary[]>([]);
  const [summaryLoading, setSummaryLoading] = useState(true);
  const [summaryError, setSummaryError] = useState<string | null>(null);

  const [selectedMonitor, setSelectedMonitor] = useState<string | null>(null);

  const [issues, setIssues] = useState<DQIssue[]>([]);
  const [issuesLoading, setIssuesLoading] = useState(false);
  const [issuesError, setIssuesError] = useState<string | null>(null);
  const [ackError, setAckError] = useState<string | null>(null);
  const [acknowledging, setAcknowledging] = useState<Set<string>>(new Set());

  const [freshness, setFreshness] = useState<DatastreamFreshness[]>([]);
  const [freshnessLoading, setFreshnessLoading] = useState(false);
  const [freshnessError, setFreshnessError] = useState<string | null>(null);

  const [evaluating, setEvaluating] = useState(false);
  const [evalSuccess, setEvalSuccess] = useState(false);
  const [evalError, setEvalError] = useState<string | null>(null);

  const loadSummary = useCallback(async () => {
    if (!projectId) return;
    setSummaryLoading(true);
    setSummaryError(null);
    try {
      const data = await fetchSummary(projectId);
      setMonitors(data.monitors ?? []);
    } catch (err) {
      setSummaryError(err instanceof Error ? err.message : String(err));
    } finally {
      setSummaryLoading(false);
    }
  }, [projectId]);

  const loadIssues = useCallback(async () => {
    if (!projectId) return;
    setIssuesLoading(true);
    setIssuesError(null);
    try {
      const data = await fetchIssues(projectId, selectedMonitor);
      setIssues(data);
    } catch (err) {
      // A failed load is NEVER an empty issue list: "no issues" would read as
      // "quality is fine". Drop the stale rows and say the load failed.
      setIssues([]);
      setIssuesError(err instanceof Error ? err.message : String(err));
    } finally {
      setIssuesLoading(false);
    }
  }, [projectId, selectedMonitor]);

  const loadFreshness = useCallback(async () => {
    if (!projectId) return;
    setFreshnessLoading(true);
    setFreshnessError(null);
    try {
      const data = await fetchFreshness(projectId);
      setFreshness(data);
    } catch (err) {
      setFreshness([]);
      setFreshnessError(err instanceof Error ? err.message : String(err));
    } finally {
      setFreshnessLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    void loadSummary();
    void loadIssues();
    void loadFreshness();
  }, [loadSummary, loadIssues, loadFreshness]);

  const handleMonitorClick = (type: string) => {
    setSelectedMonitor((prev) => (prev === type ? null : type));
  };

  const handleAcknowledge = async (firingId: string) => {
    if (!projectId) return;
    setAcknowledging((prev) => new Set([...prev, firingId]));
    setAckError(null);

    setIssues((prev) =>
      prev.map((i) =>
        i.id === firingId ? { ...i, acknowledged: true, acknowledged_at: new Date().toISOString() } : i
      )
    );

    try {
      await acknowledgeIssue(projectId, firingId);
      await loadSummary();
    } catch {
      setIssues((prev) =>
        prev.map((i) =>
          i.id === firingId ? { ...i, acknowledged: false, acknowledged_at: null } : i
        )
      );
      setAckError("Could not acknowledge the issue");
    } finally {
      setAcknowledging((prev) => {
        const next = new Set(prev);
        next.delete(firingId);
        return next;
      });
    }
  };

  const handleEvaluate = async () => {
    if (!projectId) return;
    setEvaluating(true);
    setEvalError(null);
    try {
      await triggerEvaluate(projectId);
      setEvalSuccess(true);
      await loadSummary();
      await loadIssues();
      await loadFreshness();
    } catch (err) {
      setEvalError(err instanceof Error ? err.message : String(err));
    } finally {
      setEvaluating(false);
    }
  };

  if (!projectId) {
    return (
      <>
        <div className="page-header">
          <div>
            <h1>Data quality</h1>
            <p>Universal monitors that watch every datastream.</p>
          </div>
        </div>
        <p className="dq-empty">Select a project to view its data quality.</p>
      </>
    );
  }

  if (summaryError) {
    return (
      <>
        <div className="page-header">
          <div>
            <h1>Data quality</h1>
            <p>Universal monitors that watch every datastream.</p>
          </div>
        </div>
        <p className="dq-empty dq-error" role="alert">
          Could not load the quality monitors ({summaryError}). Nothing below can be
          reported — this is a loading failure, not a clean result.
        </p>
      </>
    );
  }

  const selectedLabel = selectedMonitor
    ? monitors.find((m) => m.type === selectedMonitor)?.label ?? selectedMonitor
    : null;

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Data quality</h1>
          <p>
            {summaryLoading
              ? "Loading the universal monitors…"
              : monitors.length === 0
                ? "No monitors have reported for this project yet."
                : `${monitors.length} universal monitor${monitors.length === 1 ? "" : "s"} reporting. Select one to filter the issues below.`}
          </p>
        </div>
        <div className="header-actions">
          <button
            className="secondary-button"
            type="button"
            disabled={evaluating}
            onClick={() => void handleEvaluate()}
          >
            {evaluating ? (
              <>
                <CircularProgress size={14} />
                Evaluating…
              </>
            ) : (
              "Run evaluation"
            )}
          </button>
        </div>
      </div>

      {summaryLoading || monitors.length > 0 ? (
        <section className="dq-monitors" role="list" aria-label="Quality monitors">
          {summaryLoading
            ? Array.from({ length: 5 }).map((_, i) => (
                <div key={i} role="listitem">
                  <MonitorCardSkeleton />
                </div>
              ))
            : monitors.map((monitor) => (
                <div key={monitor.type} role="listitem">
                  <MonitorCard
                    monitor={monitor}
                    selected={selectedMonitor === monitor.type}
                    onClick={() => handleMonitorClick(monitor.type)}
                  />
                </div>
              ))}
        </section>
      ) : (
        <p className="dq-empty">
          No monitor has reported for this project yet. Monitors report after the first
          evaluation over an enabled datastream.
        </p>
      )}

      <section className="panel dq-panel">
        <div className="dq-panel-header">
          <div>
            <h2>{selectedLabel ? `Issues · ${selectedLabel}` : "All issues"}</h2>
            <p>Firings raised by the monitors. Acknowledge an issue once it has been handled.</p>
          </div>
          {selectedMonitor && (
            <button className="quiet-button" type="button" onClick={() => setSelectedMonitor(null)}>
              Show all
            </button>
          )}
        </div>
        {issuesError && !issuesLoading ? (
          <LoadFailure what="the quality issues" detail={issuesError} />
        ) : (
          <IssuesTable
            issues={issues}
            loading={issuesLoading}
            onAcknowledge={handleAcknowledge}
            acknowledging={acknowledging}
          />
        )}
      </section>

      <section className="panel dq-panel">
        <div className="dq-panel-header">
          <div>
            <h2>Data freshness</h2>
            <p>Last pull, last success, and volume for each datastream in this project.</p>
          </div>
        </div>
        <FreshnessTable
          datastreams={freshness}
          loading={freshnessLoading}
          error={freshnessError}
        />
      </section>

      {/* What each REPORTING monitor checks. Derived from the monitors the API
          actually returned — this strip never claims a monitor that did not
          report, and it is labelled as a platform definition so it is not read
          as this project's configuration (the project configures none of it;
          thresholds are server-side defaults). */}
      {monitors.length > 0 && (
        <section className="dq-definitions" aria-label="Monitor definitions">
          <p className="dq-definitions-note">
            What each monitor checks. These are platform definitions applied to every
            project — not settings of this project.
          </p>
          {monitors.map((monitor) => {
            const meta = MONITOR_META[monitor.type];
            return (
              <div key={monitor.type} className="dq-definition">
                <strong>{meta?.label ?? monitor.label}</strong>
                <span>{meta?.description ?? "No published definition for this monitor."}</span>
              </div>
            );
          })}
        </section>
      )}

      <section className="dq-cache-section">
        <h2 className="dq-section-title">Data cache</h2>
        <CacheHealthCard />
      </section>

      <Snackbar open={!!ackError} autoHideDuration={6000} onClose={() => setAckError(null)}>
        <Alert onClose={() => setAckError(null)} severity="error">
          {ackError}
        </Alert>
      </Snackbar>

      <Snackbar open={evalSuccess} autoHideDuration={4000} onClose={() => setEvalSuccess(false)}>
        <Alert onClose={() => setEvalSuccess(false)} severity="success">
          Evaluation triggered. Results will be available in a few seconds.
        </Alert>
      </Snackbar>

      <Snackbar open={!!evalError} autoHideDuration={7000} onClose={() => setEvalError(null)}>
        <Alert onClose={() => setEvalError(null)} severity="error">
          {evalError}
        </Alert>
      </Snackbar>
    </>
  );
}
