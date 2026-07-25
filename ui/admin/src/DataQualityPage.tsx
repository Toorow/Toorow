/**
 * DataQualityPage — Governance › Data quality surface, restyled onto the v3
 * design system (Sources.tsx / ProjectMapping.tsx vocabulary).
 *
 * Behavior is unchanged from the previous version: same /api/dq/* endpoints,
 * same summary → monitors → issues → freshness → definitions → cache layout,
 * same monitor filtering, acknowledge, and manual-evaluate flows. Only the
 * chrome (page-header + panels via application.css / data-quality.css) and the
 * copy (English) changed.
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
} from "./qualite/types";
import "./shell/application.css";
import "./data-quality.css";

function _authHeader(): Record<string, string> {
  const token =
    typeof window !== "undefined"
      ? (window as Window & { __TOOROW_API_KEY__?: string }).__TOOROW_API_KEY__ ??
        localStorage.getItem("api_token") ??
        ""
      : "";
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function fetchSummary(projectId: string): Promise<DQSummaryResponse> {
  const res = await fetch(`/api/dq/summary?project_id=${encodeURIComponent(projectId)}`, {
    headers: _authHeader(),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json() as Promise<DQSummaryResponse>;
}

async function fetchIssues(projectId: string, monitor?: string | null): Promise<DQIssue[]> {
  const params = new URLSearchParams({ project_id: projectId });
  if (monitor) params.set("monitor", monitor);
  const res = await fetch(`/api/dq/issues?${params.toString()}`, {
    headers: _authHeader(),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const data = await res.json();
  return (data as { issues: DQIssue[] }).issues ?? [];
}

async function acknowledgeIssue(projectId: string, firingId: string): Promise<void> {
  const res = await fetch(
    `/api/dq/issues/${encodeURIComponent(firingId)}/acknowledge?project_id=${encodeURIComponent(projectId)}`,
    { method: "POST", headers: _authHeader() }
  );
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
}

async function fetchFreshness(projectId: string): Promise<DatastreamFreshness[]> {
  const res = await fetch(
    `/api/dq/datastream-freshness?project_id=${encodeURIComponent(projectId)}`,
    { headers: _authHeader() }
  );
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const data = await res.json();
  return (data as { datastreams: DatastreamFreshness[] }).datastreams ?? [];
}

async function triggerEvaluate(projectId: string): Promise<void> {
  const res = await fetch(
    `/api/dq/evaluate?project_id=${encodeURIComponent(projectId)}`,
    { method: "POST", headers: _authHeader() }
  );
  if (res.status === 429) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.message ?? "Rate limit reached.");
  }
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
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
}: {
  datastreams: DatastreamFreshness[];
  loading: boolean;
}) {
  if (loading) {
    return (
      <div className="dq-table-loading">
        <Skeleton variant="text" width="60%" />
        <Skeleton variant="text" width="80%" />
      </div>
    );
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
  const [ackError, setAckError] = useState<string | null>(null);
  const [acknowledging, setAcknowledging] = useState<Set<string>>(new Set());

  const [freshness, setFreshness] = useState<DatastreamFreshness[]>([]);
  const [freshnessLoading, setFreshnessLoading] = useState(false);

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
    try {
      const data = await fetchIssues(projectId, selectedMonitor);
      setIssues(data);
    } catch {
      setIssues([]);
    } finally {
      setIssuesLoading(false);
    }
  }, [projectId, selectedMonitor]);

  const loadFreshness = useCallback(async () => {
    if (!projectId) return;
    setFreshnessLoading(true);
    try {
      const data = await fetchFreshness(projectId);
      setFreshness(data);
    } catch {
      setFreshness([]);
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
            <p>Universal monitors that watch every datastream — no configuration required.</p>
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
            <p>Universal monitors that watch every datastream — no configuration required.</p>
          </div>
        </div>
        <p className="dq-empty dq-error">
          Could not load the quality monitors. Try again in a moment.
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
            Five universal monitors — no configuration required. Select a monitor to filter the
            issues below.
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
        <IssuesTable
          issues={issues}
          loading={issuesLoading}
          onAcknowledge={handleAcknowledge}
          acknowledging={acknowledging}
        />
      </section>

      <section className="panel dq-panel">
        <div className="dq-panel-header">
          <div>
            <h2>Data freshness</h2>
            <p>Last pull, last success, and volume for each datastream in this project.</p>
          </div>
        </div>
        <FreshnessTable datastreams={freshness} loading={freshnessLoading} />
      </section>

      <section className="dq-definitions">
        {[
          {
            label: "Volume",
            desc: "Median + 2.6 sigma over 30 days, minimum 10 days of history.",
          },
          {
            label: "Timeliness",
            desc: "A valid extract before the cutoff (default 09:00, project time zone).",
          },
          {
            label: "Duplicates",
            desc: "Identical rows in the raw table for the evaluated day.",
          },
          {
            label: "Schema consistency",
            desc: "Column set vs. reference. Auto-updates after an alert.",
          },
          {
            label: "Rejected rows",
            desc: "Malformed rows or rows out of date range dropped during extraction.",
          },
        ].map(({ label, desc }) => (
          <div key={label} className="dq-definition">
            <strong>{label}</strong>
            <span>{desc}</span>
          </div>
        ))}
      </section>

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
