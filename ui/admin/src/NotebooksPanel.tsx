/**
 * NotebooksPanel — the project's saved notebooks (Story 6.5, AC6; Story 6.6, AC2).
 *
 * Lists the current project's notebooks with title, report reference, window
 * rule, last run time, last run status, scheduling, and a per-row "Run" action.
 *
 * Story 6.6 (AC2):
 *   - Per-notebook "Recurring (nightly)" toggle -> PATCH /api/notebooks/{id}/schedule
 *   - "Scheduled" badge shown for scheduled notebooks
 *
 * Styling: v3 design system. application.css (global, via the shell) supplies the
 * shell/layout classes (page-header, header-actions, panel, signal-label,
 * signal-mark, secondary-button, table-scroll, sr-only) + notebooks.css for the
 * page-specific table, cell layouts, and inline run-result note. Colors come only
 * from the application.css CSS variables; numbers use the Geist tabular treatment.
 *
 * AD-8: the admin console communicates exclusively through this API (no direct DB).
 */

import { useEffect, useState } from "react";
import "./shell/application.css";
import "./notebooks.css";
import { apiFetch } from "./lib/apiFetch";

// Play glyph inline — @mui/icons-material is not a dependency here.
function PlayGlyph() {
  return (
    <svg
      className="run-icon"
      xmlns="http://www.w3.org/2000/svg"
      width="14"
      height="14"
      viewBox="0 0 24 24"
      aria-hidden="true"
    >
      <path d="M8 5v14l11-7z" />
    </svg>
  );
}

interface Notebook {
  id: string;
  title: string;
  report_ref: string;
  window_rule: string;
  created_at: string;
  last_run_at: string | null;
  last_run_status: "success" | "error" | null;
  /** Story 6.6 (AC2): scheduling fields */
  scheduled?: boolean;
  schedule_rule?: string | null;
  /** Story 6.6 (AC3): share_token for detecting shared state */
  share_token?: string | null;
}

const PROJECT_ID =
  typeof window !== "undefined"
    ? (window as unknown as Record<string, string>).__TOOROW_PROJECT_ID__ ??
      "default"
    : "default";

const API_BASE = "";

async function listNotebooks(projectId: string): Promise<Notebook[]> {
  const resp = await apiFetch(
    `${API_BASE}/api/notebooks?project_id=${encodeURIComponent(projectId)}`,
  );
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

async function runNotebook(
  notebookId: string,
): Promise<{ run_id: string; summary: string }> {
  const resp = await apiFetch(`${API_BASE}/api/notebooks/${notebookId}/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),
  });
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

async function scheduleNotebook(
  notebookId: string,
  scheduled: boolean,
): Promise<Notebook> {
  const body = scheduled
    ? { scheduled: true, schedule_rule: "nightly" }
    : { scheduled: false, schedule_rule: null };
  const resp = await apiFetch(`${API_BASE}/api/notebooks/${notebookId}/schedule`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

function StatusBadge({ status }: { status: "success" | "error" | null }) {
  if (!status) {
    return <span className="muted">—</span>;
  }
  const signal = status === "success" ? "success" : "error";
  const label = status === "success" ? "Success" : "Failed";
  return (
    <span className={`signal-label ${signal}`}>
      <span className="signal-mark" />
      {label}
    </span>
  );
}

interface NotebooksPanelProps {
  /** Story 7.1 (AC6): active project scope. Defaults to the window/global fallback. */
  projectId?: string;
}

export default function NotebooksPanel({ projectId }: NotebooksPanelProps = {}) {
  const activeProjectId = projectId ?? PROJECT_ID;
  const [notebooks, setNotebooks] = useState<Notebook[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [runningId, setRunningId] = useState<string | null>(null);
  const [runMessage, setRunMessage] = useState<string | null>(null);
  const [runError, setRunError] = useState(false);
  const [schedulingId, setSchedulingId] = useState<string | null>(null);

  const fetchNotebooks = () => {
    setLoading(true);
    setError(null);
    listNotebooks(activeProjectId)
      .then(setNotebooks)
      .catch((err: Error) => setError(err.message))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    fetchNotebooks();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeProjectId]);

  const handleRun = async (nb: Notebook) => {
    setRunningId(nb.id);
    setRunMessage(null);
    setRunError(false);
    try {
      const result = await runNotebook(nb.id);
      setRunMessage(`Run complete (run_id: ${result.run_id})`);
      fetchNotebooks(); // refresh last_run_at / status
    } catch (err) {
      setRunError(true);
      setRunMessage(`Run failed: ${String(err)}`);
    } finally {
      setRunningId(null);
    }
  };

  /** Story 6.6 (AC2): toggle scheduled flag via PATCH /schedule */
  const handleScheduleToggle = async (nb: Notebook, newScheduled: boolean) => {
    setSchedulingId(nb.id);
    try {
      const updated = await scheduleNotebook(nb.id, newScheduled);
      // Update in-place
      setNotebooks((prev) =>
        prev.map((n) => (n.id === nb.id ? { ...n, ...updated } : n)),
      );
    } catch (err) {
      setRunError(true);
      setRunMessage(`Scheduling failed: ${String(err)}`);
    } finally {
      setSchedulingId(null);
    }
  };

  if (loading) {
    return (
      <>
        <div className="page-header">
          <div>
            <h1>Notebooks</h1>
            <p>Saved analyses you can re-run and schedule for this project.</p>
          </div>
        </div>
        <div className="notebooks-state">
          <span className="notebooks-spinner" aria-hidden="true" />
          Loading notebooks…
        </div>
      </>
    );
  }

  if (error) {
    return (
      <>
        <div className="page-header">
          <div>
            <h1>Notebooks</h1>
            <p>Saved analyses you can re-run and schedule for this project.</p>
          </div>
        </div>
        <div className="notebooks-note error" role="alert">
          <div>
            <strong>Couldn't load notebooks.</strong>
            {error}
            <div style={{ marginTop: 10 }}>
              <button
                className="secondary-button"
                type="button"
                onClick={fetchNotebooks}
              >
                Try again
              </button>
            </div>
          </div>
        </div>
      </>
    );
  }

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Notebooks</h1>
          <p>Saved analyses you can re-run and schedule for this project.</p>
        </div>
      </div>

      {runMessage && (
        <div
          className={`notebooks-note${runError ? " error" : ""}`}
          role="status"
        >
          <div>{runMessage}</div>
        </div>
      )}

      {notebooks.length === 0 ? (
        <section className="panel">
          <p className="notebooks-empty">
            No notebooks saved yet. Create one with the <code>save_notebook</code>{" "}
            MCP tool.
          </p>
        </section>
      ) : (
        <section className="panel notebooks-panel">
          <div className="notebooks-panel-header">
            <div>
              <h2>Saved notebooks</h2>
              <p>Each notebook re-runs its report over the configured window.</p>
            </div>
          </div>
          <div className="table-scroll" tabIndex={0} aria-label="Saved notebooks">
            <table className="notebooks-table">
              <thead>
                <tr>
                  <th>Title</th>
                  <th>Report</th>
                  <th>Window</th>
                  <th>Last run</th>
                  <th>Status</th>
                  {/* Story 6.6 (AC2): scheduling column */}
                  <th>Schedule</th>
                  <th>
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {notebooks.map((nb) => (
                  <tr key={nb.id}>
                    <td>
                      <div className="notebook-title">
                        <strong>{nb.title}</strong>
                        {/* Story 6.6 (AC2): "Scheduled" badge */}
                        {nb.scheduled && (
                          <span
                            className="schedule-badge"
                            aria-label="Scheduled nightly"
                          >
                            Scheduled
                          </span>
                        )}
                      </div>
                    </td>
                    <td>
                      <code className="report-ref">{nb.report_ref}</code>
                    </td>
                    <td>
                      <span className="window-chip">{nb.window_rule}</span>
                    </td>
                    <td className="number">
                      {nb.last_run_at
                        ? new Date(nb.last_run_at).toLocaleString("en-GB")
                        : "—"}
                    </td>
                    <td>
                      <StatusBadge status={nb.last_run_status} />
                    </td>
                    {/* Story 6.6 (AC2): schedule toggle */}
                    <td>
                      <label className="schedule-toggle">
                        <input
                          type="checkbox"
                          checked={nb.scheduled ?? false}
                          disabled={schedulingId !== null}
                          onChange={(e) =>
                            handleScheduleToggle(nb, e.target.checked)
                          }
                          aria-label={`Recurring (nightly) for ${nb.title}`}
                          data-testid={`schedule-toggle-${nb.id}`}
                        />
                        <span>Recurring (nightly)</span>
                      </label>
                    </td>
                    <td className="actions-cell">
                      <div className="notebooks-run-actions">
                        <button
                          className="secondary-button"
                          type="button"
                          disabled={runningId !== null}
                          onClick={() => handleRun(nb)}
                          aria-label={`Run notebook ${nb.title}`}
                        >
                          {runningId === nb.id ? (
                            <span
                              className="notebooks-spinner"
                              aria-hidden="true"
                            />
                          ) : (
                            <PlayGlyph />
                          )}
                          Run
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </>
  );
}
